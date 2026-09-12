# 自动化代理节点抓取、测速与订阅生成系统 详细架构设计方案与规则审定书

## 一、 系统架构总览

本系统设计为轻量级、模块化、高可扩展的自动化代理节点流水线。核心包含 4 大职责清晰的业务子模块与统一调度层：

```
+-------------------------------------------------------------+
|                     调度中心 (main.py)                      |
+-------------------------------------------------------------+
         |                        |                  |
         v                        v                  v
+------------------+     +------------------+ +---------------+
|  抓取器 Crawler  |     |  测速器 Tester   | | 导出器 Export |
|  - GitHub Search |     |  - URL Parser    | | - Base64 Sub  |
|  - Regex Extract |     |  - sing-box In/Out| | - sing-box cfg|
|  - Rate Limit    |     |  - Delay & Speed | +---------------+
+------------------+     +------------------+
         |                        ^
         v                        |
+-------------------------------------------------------------+
|                  数据库抽象层 (database.py)                 |
|             Cloudflare D1 (Serverless Edge SQL)             |
|          REST API: /accounts/{id}/d1/database/{id}/query    |
+-------------------------------------------------------------+
```

---

## 二、 核心数据模型与状态流转机

### 1. 数据库规范：Cloudflare D1 (`database.py`)

Cloudflare D1 是基于 SQLite 引擎的全球分布式 Serverless SQL 数据库。本地或 Runner 通过 Cloudflare v4 REST API 与 D1 交互，支持批量执行 SQL 事务。

#### 连接与配置规范
* **环境变量**：
  * `CLOUDFLARE_ACCOUNT_ID`：Cloudflare 账户 ID。
  * `CLOUDFLARE_DATABASE_ID`：目标 D1 数据库 UUID。
  * `CLOUDFLARE_API_TOKEN`：具备 `D1:Edit` 权限的 API Token。
* **API 交互标准**：
  * 端点：`POST https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query`
  * 单次请求支持批处理数组：`[{"sql": "...", "params": [...]}, ...]`，极大降低网络 RTT。

#### 表一：`nodes`（节点资产主表）
| 字段名 | 类型 | 约束 | 说明 |
| :--- | :--- | :--- | :--- |
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增主键 |
| `node_url` | TEXT | UNIQUE NOT NULL | 完整节点 URL（唯一索引，防重复） |
| `protocol` | TEXT | NOT NULL | 协议类型（`vless`, `vmess`, `trojan`, `ss`, `hy2` 等） |
| `first_seen` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 首次入库时间 |
| `last_tested`| TIMESTAMP | NULL | 最近一次测试时间 |
| `status` | TEXT | DEFAULT 'untested' | 状态枚举：`untested`, `active`, `dead` |
| `delay_ms` | INTEGER | DEFAULT -1 | HTTP 响应延迟（ms），-1 表示未测或失败 |
| `speed_mbps` | REAL | DEFAULT 0.0 | 实测下载带宽（Mbps） |
| `fail_count` | INTEGER | DEFAULT 0 | 连续测试失败次数计数器 |

#### 表二：`fetch_logs`（爬虫执行历史表）
| 字段名 | 类型 | 约束 | 说明 |
| :--- | :--- | :--- | :--- |
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增主键 |
| `last_pushed_date` | TEXT | NOT NULL | 当次增量搜索覆盖的最大 `pushed` 日期 (`YYYY-MM-DD`) |
| `nodes_found` | INTEGER | DEFAULT 0 | 当次提取到的节点原始总数 |
| `nodes_added` | INTEGER | DEFAULT 0 | 当次新增入库数（去除重复后） |
| `fetched_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 爬虫执行时间戳 |

### 2. 节点状态生命周期机

```
               [ 爬虫抓取发现新节点 ]
                        │
                        ▼
                ┌───────────────┐
                │   untested    │
                └───────┬───────┘
                        │
                        ▼ [进入测试队列]
            ┌───────────────────────┐
            │   sing-box 连通性测试  │
            └───────┬───────┬───────┘
  (握手成功/HTTP<1500ms)│       │ (握手失败/超时)
            ┌───────┘       └───────┐
            ▼                       ▼
     ┌──────────────┐         ┌───────────────┐
     │    active    │         │ 失败 fail_count++
     │(fail_count=0)│         └───────┬───────┘
     └──────┬───────┘                 │ fail_count >= 3
            │ 再次测试失败             ▼
            └───────────────> ┌───────────────┐
                              │     dead      │
                              │ (淘汰不再轮询) │
                              └───────────────┘
```

---

## 三、 模块详细设计与核心算法

### 模块一：GitHub 增量抓取器 (`crawler.py`)
1. **增量区间判定**：
   - 启动时查询 D1 `fetch_logs` 最近一条 `last_pushed_date`。
   - 若不存在，默认以 3 天前作为起始日期。
2. **GitHub API 交互与防封流控**：
   - Endpoint: `https://api.github.com/search/code`
   - Header: 传入 `Authorization: Bearer <GITHUB_TOKEN>`。
   - Query 模板: `{keyword} pushed:>{last_date} in:file`。
   - 分页与频控：严格控制每次请求间隔 >= 2 秒；捕获 403/429 rate limit，智能休眠重试。
3. **节点提取正则引擎**：
   - 模式串：
     `r'(?:vless|vmess|trojan|ss|ssr|hysteria|hysteria2|hy2|tuic|tuic-v5|socks5|http|https|wireguard|snell)://[^\s"\'\<\>\[\]]+'`
   - 提取后进行基本的 URL 规范化清洗，协议字段统一归一化（如 `hysteria2` -> `hy2`）。

---

### 模块二：基于 sing-box 的自动化测速器 (`tester.py`)
1. **URL 转 sing-box Outbound 配置转换器**：
   - 支持解析标准 `vless://`, `trojan://`, `ss://`, `hy2://` 等主流协议链接，将其转换为 sing-box 原生 JSON Outbound 结构体。
2. **轻量级隔离测试架构**：
   - 为单次测试启动轻量级临时 sing-box 进程，配置动态端口（如 `127.0.0.1:19800+`）的 SOCKS5/HTTP Inbound，Outbound 绑定待测节点。
   - 生成临时 JSON 配置文件并启动进程：`sing-box run -c <temp_config.json>`。
   - 发起探测请求并精细计时，超时阈值设定为 2500ms。
   - 测速完成后安全终止 sing-box 进程并清理临时配置文件，避免资源泄漏。
3. **可选带宽测速（Speed Test）**：
   - 仅对延迟合格节点（`delay_ms > 0` 且 `delay_ms < 1500`）触发。
   - 通过本地代理端口拉取定长测试资源，计算实际下载吞吐率 Mbps。

---

### 模块三：节点导出与订阅输出器 (`exporter.py`)
1. **筛选与排序**：
   - 查询 D1：`SELECT * FROM nodes WHERE status = 'active' AND delay_ms > 0 ORDER BY delay_ms ASC, speed_mbps DESC LIMIT ?`。
2. **双格式输出**：
   - **`output/subscription.txt`**：将有效 `node_url` 换行拼接后进行 Base64 编码。
   - **`output/config.json`**：基于 sing-box 官方模板，生成一个包含全部精选节点的客户端可用配置。
3. **扩展能力**：
   - 由于数据库部署在 Cloudflare D1，可无缝搭配一个轻量级 Cloudflare Worker，直接读取 D1 并向公网提供动态订阅 URL。

---

### 模块四：开发语言选型技术论证（Python vs Rust）

| 比较维度 | Python (推荐选用) | Rust |
| :--- | :--- | :--- |
| **外部进程调度 (sing-box)** | 原生 `subprocess` / `asyncio`，极其灵活直观 | `tokio::process::Command`，稳定但生命周期控制代码冗长 |
| **D1 REST API 批处理** | `requests` / `httpx`，原生 JSON 序列化极快 | `reqwest` + `serde_json`，类型严格但修改结构需频繁调整结构体 |
| **URL 协议正则与动态解析** | 原生 `re` + `urllib.parse`，开发迭代敏捷，支持多变链接格式 | 需组合 `regex`, `url`, base64 等多个 crate，语法容错处理成本较高 |
| **开发与调试效率** | **极高**，几行代码即可验证网络探针与配置输出 | 较低，编译等待时间较长，借用检查与边界错误调试开销高 |
| **并发与执行性能** | `ThreadPoolExecutor` / `asyncio`，足以跑满百级节点测试 | 原生极致性能与超低内存，适合千级长期驻留守护进程 |
| **分发与交付** | 需 Python 解释器环境，易于在本地/CI 运行 | 单一自包含二进制文件，分发极其干净 |

**架构师建议**：
鉴于本项目核心为 **外部工具编排器（sing-box CLI 调度）、GitHub API 爬取、D1 REST 接口调用与文本/JSON 配置生成**，属于典型的 **I/O 密集型与原型迭代型任务**：
* **首选 Python 3.10+** 作为核心开发语言：以最短周期达成生产级质量，代码易于维护、调试与扩展。
* 若后续需要将测速节点部署至无 Python 环境的超轻量 Docker 或边缘嵌入式设备，可基于已验证的 Python 业务模型移植为 Rust。

---

## 四、 架构师审查与质量门禁判定

1. **D1 访问防频控与批处理**：D1 HTTP API 对 QPS 与 Payload 大小有限制，设计中采用分批提交机制（每批 50~100 条 SQL）。
2. **异常回滚与本地降级**：若未配置 Cloudflare D1 凭证，提供本地 SQLite 文件模式兼容，确保在无网络/无凭证环境下仍可本地单测与开发。
3. **生命周期自愈与淘汰**：3 次失败淘汰机制（`dead`），避免频繁探测失效坏死节点。

---
**架构师审定结论**：
经全面技术审查与架构推演：
1. 数据库由本地 SQLite3 平滑演进至 **Cloudflare D1（Serverless SQL REST API + 本地 SQLite 兼容降级）** 方案审定通过。
2. 开发语言选型明确推荐采用 **Python 3.10+**（并设计清晰的模块抽象，便于后续迁移）。
**【阶段一：方案与规则审定通过，准予进入阶段二代码开发】**
