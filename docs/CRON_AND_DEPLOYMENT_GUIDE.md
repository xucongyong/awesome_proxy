# 🌐 全自动代理池：国内外双机协同与定时任务 (Cron) 部署运维手册

本文档为 **国内外双服务器 24 小时全自动代理池** 的标准化部署与定时任务调度手册。通过本方案，可以实现 **海外粗筛洗盘 + 国内精筛出库**，在彻底避免国内服务器网络风控的同时，保证订阅节点 100% 可用。

---

## 目录
1. [双机协同核心架构](#1-双机协同核心架构)
2. [数据库双视角指标设计](#2-数据库双视角指标设计)
3. [定时任务 (Crontab) 调度规则清单](#3-定时任务-crontab-调度规则清单)
   - [海外服务器 Crontab 配置](#海外服务器-crontab-配置-粗筛与爬取)
   - [国内服务器 Crontab 配置](#国内服务器-crontab-配置-精筛与出库)
4. [服务器标准化部署步骤](#4-服务器标准化部署步骤)
5. [日常运维与排查速查命令](#5-日常运维与排查速查命令)
6. [常见问题与故障排查 (FAQ)](#6-常见问题与故障排查-faq)

---

## 1. 双机协同核心架构

公网抓取的历史代理节点中，**85%~95% 的服务端在海外就已经断网或关机**。如果直接在国内服务器上盲目探测上万个死 IP，国内机房网卡会被运营商/云厂商标记为异常端口扫描，甚至引发封机。

因此，采用 **上下游流水线协作**：

```mermaid
flowchart TD
    GH[GitHub 实时仓库 & 代码搜索] -->|无封锁极速拉取| O_CRAWL[海外服务器: 增量/历史爬虫]
    O_CRAWL -->|海量新节点写入| CF_D1[(Cloudflare D1 全球边缘云数据库)]

    subgraph Overseas [海外服务器 24/7 (粗筛洗盘中心)]
        O_CRAWL
        O_TEST[海外粗筛: TCP预检 + 轻量HTTP延迟]
        O_RETEST[死节点每日打捞复活]
    end

    CF_D1 <-->|读取待测 / 写入 global_is_active| O_TEST
    CF_D1 <-->|每日打捞复活| O_RETEST

    subgraph Domestic [国内服务器 24/7 (精筛与订阅中心)]
        CN_TEST[国内精筛: 只测海外活节点 WHERE global_is_active=1]
        CN_SPEED[存活节点定期带宽抽样测速]
        CN_EXPORT[导出国内专属高质量订阅: Base64 / sing-box]
    end

    CF_D1 <-->|只取 global_is_active=1 的千级节点| CN_TEST
    CF_D1 -->|取国内延迟最优节点| CN_SPEED
    CF_D1 -->|读取 cn_is_active=1| CN_EXPORT
    CN_EXPORT --> CLIENTS([手机 / 电脑 / 路由器客户端])
```

---

## 2. 数据库双视角指标设计

系统通过 Cloudflare D1（全球边缘数据库）实现双端实时同步，核心字段如下：

| 字段名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `global_is_active` | `INTEGER` (1/0/NULL) | **海外视角存活**：1=节点服务端在线；0=海外无法连接（服务端真死）。 |
| `global_delay_ms` | `INTEGER` | 海外直连节点的延迟（毫秒）。 |
| `cn_is_active` | `INTEGER` (1/0/NULL) | **国内视角存活**：1=能穿透 GFW 在大陆正常使用；0=国内直连失败。 |
| `cn_delay_ms` | `INTEGER` | 中国大陆直连节点的延迟（毫秒），决定大陆客户端订阅排序。 |
| `speed_mbps` | `REAL` | 节点下行下载带宽（Mbps，针对延迟优秀的节点抽样）。 |
| `fail_count` | `INTEGER` | 连续失败计数（达到 3 次标记永久淘汰）。 |

> [!TIP]
> **GFW 被墙节点识别**：如果 `global_is_active = 1` 且 `cn_is_active = 0`，代表节点服务端活得很好，只是被 GFW 封锁了 IP。未来搭建中转机或前置中继时，这批节点是极好的出口落地机！

---

## 3. 定时任务 (Crontab) 调度规则清单

登录服务器后，执行 `crontab -e` 即可配置定时任务。

### 海外服务器 Crontab 配置 (粗筛与爬取)
> 假设项目部署在 `/data/get_all_proxy`，Python 路径为 `python3`

```bash
# -------------------------------------------------------------
# 1. 每 2 小时运行一次 GitHub 增量爬虫，挖掘最新上线的节点
# -------------------------------------------------------------
0 */2 * * * cd /data/get_all_proxy && python3 main.py --crawl --pages 5 >> logs/cron_crawl.log 2>&1

# -------------------------------------------------------------
# 2. 每 1 小时对新入库未测试的节点进行海外粗筛 (30 并发，只测延迟)
# -------------------------------------------------------------
10 * * * * cd /data/get_all_proxy && python3 main.py --test --limit 300 --concurrency 30 --no-speed-test --region global >> logs/cron_test_global.log 2>&1

# -------------------------------------------------------------
# 3. 每天凌晨 3:00 对失效节点做一次“打捞复活”（死节点中常有 3%~5% 是临时关机重启）
# -------------------------------------------------------------
0 3 * * * cd /data/get_all_proxy && python3 main.py --test --limit 1000 --concurrency 50 --no-speed-test --region global >> logs/cron_revive.log 2>&1
```

---

### 国内服务器 Crontab 配置 (精筛与出库)
> 国内服务器只测试在海外已被确认为活的节点，负担极轻、安全无风控！

```bash
# -------------------------------------------------------------
# 1. 每 30 分钟：对海外存活的节点进行国内连通性与时延测试 (高频轻量)
# -------------------------------------------------------------
*/30 * * * * cd /data/get_all_proxy && python3 main.py --test --limit 200 --concurrency 20 --no-speed-test --region cn >> logs/cron_test_cn.log 2>&1

# -------------------------------------------------------------
# 2. 每 4 小时：对当前国内存活的最优节点进行真实下载带宽测速 (抽样 2 秒，计算真实 Mbps)
# -------------------------------------------------------------
15 */4 * * * cd /data/get_all_proxy && python3 main.py --test --limit 50 --concurrency 10 --region cn >> logs/cron_speed_cn.log 2>&1

# -------------------------------------------------------------
# 3. 每次测完后自动导出国内专属可用订阅 (生成 Base64 与 sing-box 配置)
# -------------------------------------------------------------
35 * * * * cd /data/get_all_proxy && python3 main.py --export --limit 50 --region cn >> logs/cron_export.log 2>&1
```

---

## 4. 服务器标准化部署步骤

海外服务器和国内服务器部署步骤完全一致：

### 第一步：安装依赖环境 (以 Debian / Ubuntu 为例)
```bash
# 1. 安装基础依赖
sudo apt update && sudo apt install -y python3 python3-pip curl git

# 2. 安装 Python requests 库
pip3 install requests

# 3. 一键安装 sing-box 官方最新二进制内核
bash -c "$(curl -fsSL https://sing-box.app/deb-install.sh)"

# 验证内核安装
sing-box version
```

### 第二步：拉取代码并配置环境
```bash
# 1. 进入工作目录并克隆/拷贝代码
mkdir -p /data && cd /data
git clone <你的代码仓库URL> get_all_proxy
cd /data/get_all_proxy

# 2. 创建日志目录
mkdir -p logs output

# 3. 配置 .env 文件 (确保配置了 Cloudflare D1 凭据与 GitHub Token)
cat << 'EOF' > .env
# GitHub Token (海外机器必填，用于爬虫)
GITHUB_TOKEN="ghp_xxxxxxxxxxxx"

# Cloudflare D1 Credentials (国内外机器共享同一个云库)
CLOUDFLARE_ACCOUNT_ID="6b0c25aa8340183bdcaaae0c67508c5e"
CLOUDFLARE_DATABASE_ID="3b55a783-16d9-4f31-bd9f-312972752269"
CLOUDFLARE_API_TOKEN="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
EOF
```

---

## 5. 日常运维与排查速查命令

### 1. 终端查看当前最优质存活节点榜单
```bash
# 查看国内直连最优的 Top 20 节点 (带星级、时延与带宽)
python3 main.py --top 20 --region cn

# 查看海外机房直连最优的 Top 20 节点
python3 main.py --top 20 --region global
```

### 2. 查看数据库节点全貌统计画像
```bash
python3 main.py --stats
```

### 3. 手动触发批量测速
```bash
# 快速测活 100 个节点（只测延迟，极速）
python3 main.py --test --limit 100 --concurrency 30 --no-speed-test --region cn

# 深度完整测速（包含下载带宽测速）
python3 main.py --test --limit 20 --concurrency 10 --region cn
```

### 4. 手动导出订阅
```bash
python3 main.py --export --limit 50 --region cn
# 输出文件位于 output/subscription.txt (Base64通用) 与 output/config.json (sing-box 专用)
```

---

## 6. 常见问题与故障排查 (FAQ)

#### Q1: 为什么海外测出来活的节点，国内测全部是不可达？
* **原因**：这是正常现象，说明这些节点的 IP/端口被 GFW 阻断了。
* **处理方式**：无需删除。系统已将其记录为 `global_is_active = 1` 且 `cn_is_active = 0`，国内导出订阅时会自动过滤，不会推送给你的手机/电脑。

#### Q2: 会不会耗尽 Cloudflare D1 的免费配额？
* **答案**：不会。
  * Cloudflare D1 免费额度为 **每天 500 万次读取 + 10 万次写入**。
  * 按照本文档的 Cron 规划：海外每小时写入约 300 次，国内每半小时写入约 200 次，全天总写入量在 **1.5 万次左右**，仅消耗免费额度的 15%，完全免费且绰绰有余。

#### Q3: 为什么不要直接物理删除 (`DELETE`) 死节点？
* **答案**：如果直接 `DELETE`，爬虫下次重新扫描 GitHub 历史仓库时，又会将这些死节点当成“新节点”重新入库并再次测试，陷入无限重复浪费算力的死循环。保留 `is_active = 0` / `status = 'dead'` 能利用数据库 `node_url UNIQUE` 索引永远将其拦截在外。
