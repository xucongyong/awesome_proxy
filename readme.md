# 自动化代理节点抓取、测速与订阅生成系统 需求文档 (PRD)

## 1. 项目概述
本系统旨在通过自动化方式，定期从 GitHub 增量抓取公开的代理节点，将其统一存入 Cloudflare D1 边缘数据库进行去重与生命周期管理，并调用 sing-box 内核进行真实 HTTP 连通性与下载速度测试，最终将筛选出的优质节点导出为可直接使用的本地订阅/配置文件。

---

## 2. 核心功能模块规范

### 模块一：GitHub 增量搜寻与节点提取 (`crawler`)
1. **支持的协议前缀**：
   * 包含但不限于：`vless://`, `vmess://`, `trojan://`, `ss://`, `ssr://`, `hysteria://`, `hysteria2://`, `hy2://`, `tuic://`, `tuic-v5://`, `socks5://`, `http://`, `https://`, `wireguard://`, `snell://`。
2. **正则表达式提取**：
   * 必须使用通用的正则匹配模式，从抓取到的 Raw 文本内容中精准提取整条节点链接：
     `(?:vless|vmess|trojan|ss|ssr|hysteria|hysteria2|hy2|tuic|tuic-v5|socks5|http|https|wireguard|snell)://[^\s"'\<\>\[\]]+`
3. **增量搜索逻辑**：
   * 从 Cloudflare D1 数据库 `fetch_logs` 表中读取上一次成功拉取的时间节点（格式：`YYYY-MM-DD`）。
   * 调用 GitHub Code Search API (`https://api.github.com/search/code`)，在 Query 中拼接 `pushed:>YYYY-MM-DD in:file` 进行增量拉取。
   * 支持通过环境变量或配置传入 `GITHUB_TOKEN`（Personal Access Token），以提升 API 速率限制。
   * 请求之间添加合适的延迟（如 `time.sleep(2)`）以防触发速率限制。

---

### 模块二：数据库持久化与自动去重 (`database`)
1. **数据库选型**：Cloudflare D1（Serverless SQL 边缘数据库，基于 SQLite 引擎，支持 REST API 与 Cloudflare Workers 原生绑定）。
2. **连接与鉴权**：
   * 通过 Cloudflare D1 REST API 或 Wrangler 进行交互。
   * 环境变量配置：`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_DATABASE_ID`、`CLOUDFLARE_API_TOKEN`。
3. **数据表设计**：
   * **`nodes`（节点记录表）**：
     * `node_url` (TEXT, UNIQUE): 节点完整链接，设置唯一索引，使用 `INSERT OR IGNORE` 实现写入时自动去重。
     * `protocol` (TEXT): 协议类型（从 `node_url` 自动解析，如 `vless`, `hy2`）。
     * `first_seen` (TIMESTAMP): 首次入库时间，默认当前时间。
     * `last_tested` (TIMESTAMP): 上一次测速时间。
     * `status` (TEXT): 当前状态（`untested` / `active` / `dead`），默认 `untested`。
     * `delay_ms` (INTEGER): HTTP 响应延迟（ms）。
     * `speed_mbps` (REAL): 实际测速带宽（Mbps）。
     * `fail_count` (INTEGER): 连续测试失败计数，默认 `0`。
   * **`fetch_logs`（爬虫执行日志表）**：
     * `last_pushed_date` (TEXT): 当次增量搜索的最终时间点。
     * `nodes_found` (INTEGER): 发现节点总数。
     * `nodes_added` (INTEGER): 实际新增入库数。
     * `fetched_at` (TIMESTAMP): 执行记录时间。

---

### 模块三：基于 sing-box 的自动化测速 (`tester`)
1. **测试引擎**：命令行调用本地安装的 **sing-box** 执行真连接与延迟测试。
2. **测试流程**：
   * 从 Cloudflare D1 提取状态为 `status IN ('untested', 'active')` 的节点。
   * **动态配置生成**：将目标节点组装为 sing-box 格式的临时 JSON 配置文件（包含 Outbound 节点信息与本地入站测试端口）。
   * **HTTP 延迟测试（Delay Test）**：
     * 运行 sing-box 实例，对测试目标（如 `http://cp.cloudflare.com` 或 `https://www.gstatic.com/generate_204`）发起 HTTP 请求，获取 HTTP 握手响应延迟 (`delay_ms`)。
   * **下载带宽测速（Speed Test - 可选）**：
     * 对延迟合格（如 `delay_ms < 1500`）的节点，通过代理拉取测试文件，计算实际下载速率 (`speed_mbps`)。
3. **状态更新与淘汰规则**：
   * **测试通过**：更新 `status = 'active'`, `delay_ms`, `speed_mbps`，重置 `fail_count = 0`。
   * **测试失败**：`fail_count` 自动加 1。若连续失败次数 `fail_count >= 3`，更新 `status = 'dead'`，后续自动忽略。

---

### 模块四：可用节点导出与订阅输出 (`exporter`)
1. **数据筛选**：
   * 从 Cloudflare D1 拉取 `status == 'active'` 且 `delay_ms > 0` 的节点。
   * 按照 `delay_ms` 升序、`speed_mbps` 降序排列，截取前 $N$ 个（例如前 50 条）最佳节点。
2. **输出格式**：
   * **Base64 订阅文本**：生成符合 V2Ray/Clash 标准的 Base64 编码文本文件 `subscription.txt`。
   * **sing-box 配置模板**：直接渲染一份包含筛选节点的 `config.json`，方便客户端直接导入。

---

## 3. 开发语言选型与执行任务指南
* **推荐开发语言**：Python 3.10+（兼顾极速原型开发、进程管理与 Cloudflare D1 REST API 批处理；若追求极致单二进制分发与零依赖运行时亦可采用 Rust）。
* 核心脚本组织：`crawler.py`, `database.py`, `tester.py`, `exporter.py`, `main.py`。
# awesome_proxy
# awesome_proxy
# awesome_proxy
