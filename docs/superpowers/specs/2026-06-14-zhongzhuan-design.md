# Zhongzhuan (中转) - 本地 OpenAI API 中转代理

| 字段 | 值 |
|---|---|
| 项目代号 | `zhongzhuan` |
| 文档版本 | v0.2 (Python 重写) |
| 创建日期 | 2026-06-14 |
| 修订日期 | 2026-06-14 |
| 开源协议 | MIT |
| 目标平台 | Windows 10/11 (x64) |
| 技术栈 | Python 3.10+ / aiohttp / httpx / SQLite (stdlib) |

> 修订说明:从 Go 切换到 Python。理由:1) 本地工具性能要求低,Python 异步生态(aiohttp+httpx)足够;2) Python 在 Windows 上无需编译即可运行,极大简化分发;3) 后续维护成本更低。

---

## 1. 背景与目标

### 1.1 痛点

在 Cursor / Cline / Continue / Aider 等 AI 编程工具的「Agent / Solo 模式」下，工具会向 LLM 上游并发地持续发请求（多轮补全、子任务委派、工具调用循环）。当上游是免费 / 限流较严的中转 API 时：

- **429 Too Many Requests**：单 key 配额耗尽 → 工具直接报错，Agent 链路中断
- **500/502/503/504**：上游瞬时过载 → 工具要么 retry 要么放弃，体验断崖
- **多 key 轮换麻烦**：用户得手工改环境变量、切 base URL
- **多模型 fallback 麻烦**：GPT-4o 限流时不能让 Agent 自动转用 Claude

### 1.2 目标

提供一个**常驻 Windows 本地的中转代理**，让编程工具只需把 `base_url` 指过来即可获得：

1. **透明 fallback**：429/5xx 时**对编程工具无感**地切换 key / 切模型
2. **多 key 轮转**：单个模型可配 N 个 key，按健康度+滑动窗口选最不挤的那个
3. **多模型轮转**：可把多个模型组成一个"组"（Group），让 Agent 在多模型间轮询 / 故障转移
4. **速率整形**：限制并发上限、限制单 key 的滑动窗口 RPM/TPM，避免打挂上游
5. **可视化**：Web 后台配置模型/Key/分组/限流，实时看 QPS 和错误率
6. **Windows 原生**：可注册为 Windows 服务、开机自启，并允许用户在运行时**启动/停止服务、修改自启策略**

### 1.3 非目标（v1 不做）

- 多租户 / 鉴权 / 配额售卖
- Anthropic / Gemini 协议
- 跨机器集群
- 自动更新（v1 让用户手动重装安装包）
- 计费 / 用量统计
- Linux / macOS（v1 只 Windows；代码层面不锁平台，但 v1 验收只验 Windows）

---

## 2. 整体架构

### 2.1 单进程双端口

```
┌──────────────────────────────────────────────────────────────────┐
│  Windows 本机 (127.0.0.1)                                        │
│                                                                  │
│  ┌──────────────────┐    ┌────────────────────────────────────┐  │
│  │ Cursor / Cline / │───▶│  zhongzhuan.exe (PyInstaller)      │  │
│  │ Continue / Aider │    │                                    │  │
│  │ (base_url=本地)  │    │  ┌──────────────────────────────┐  │  │
│  └──────────────────┘    │  │  代理端口 (默认 8088)         │  │  │
│        OpenAI 协议        │  │  /v1/chat/completions         │  │  │
│                          │  │  /v1/completions              │  │  │
│                          │  │  /v1/embeddings               │  │  │
│                          │  │  /v1/models                   │  │  │
│  ┌──────────────────┐    │  └──────────┬───────────────────┘  │  │
│  │  浏览器管理后台   │◀──▶│             ▼                      │  │
│  │  http://127...   │    │  ┌──────────────────────────────┐  │  │
│  │  :8089           │    │  │   调度器 (asyncio)            │  │  │
│  └──────────────────┘    │  │   • 选模型组 → 选模型         │  │  │
│       REST + 静态页       │  │   • 选 Key (健康度+窗口)     │  │  │
│                          │  │   • 限流整形                  │  │  │
│                          │  │   • 失败重试 / Fallback       │  │  │
│                          │  └──────────┬───────────────────┘  │  │
│                          │             ▼                      │  │
│                          │  ┌──────────────────────────────┐  │  │
│                          │  │  上游 HTTP 客户端             │  │  │
│                          │  │  httpx.AsyncClient (SSE 透传) │  │  │
│                          │  └──────────┬───────────────────┘  │  │
│                          │             │                      │  │
│  ┌──────────────────┐    │  ┌──────────▼───────────────────┐  │  │
│  │ Windows Service  │◀──▶│  │  存储 (SQLite stdlib)        │  │  │
│  │ Controller       │    │  │  配置 / Key / 日志 / 指标    │  │  │
│  └──────────────────┘    │  └──────────────────────────────┘  │  │
│                          │  管理端口 (默认 8089)               │  │
│                          └────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                  OpenAI 兼容上游 (官方 / 中转 / 免费)
```

### 2.2 运行模式

- **服务模式（默认）**：注册为 Windows Service，开机自启，UI 通过浏览器访问管理端口
- **前台模式**：直接双击 exe / 命令行启动，方便排错，行为与服务模式完全一致
- **安装器 / 绿色版**：
  - 安装器 (Inno Setup) 默认装到 `C:\Program Files\Zhongzhuan`、注册服务、设置自启
  - 绿色版 = 单 exe + `install.bat`（可选注册 Run 键实现用户级自启），无需管理员

### 2.3 关键决策

| 决策 | 选定 | 理由 |
|---|---|---|
| 协议 | OpenAI Chat Completions (含 Embeddings) | v1 唯一支持；SSE 透传不解析 body，天然兼容所有 OpenAI 兼容变体 |
| 语言 | Python 3.10+ | 无需编译；asyncio 满足高并发；维护成本低 |
| 异步框架 | aiohttp | 单进程同时跑两个 HTTP 服务(代理+管理)，aiohttp App 隔离 |
| 上游 HTTP | httpx.AsyncClient | 友好的 SSE/Streaming API；连接池复用 |
| 存储 | SQLite (Python stdlib `sqlite3`) | 零依赖、文件级备份 |
| 配置格式 | YAML 文件 + DB | YAML 装默认 / 端口 / 监听地址；DB 存可变的状态/Key/指标 |
| 鉴权 | 默认无（仅 loopback） | 用户明确说本机专用；可改为 LAN 监听 |
| 加密 | Windows DPAPI 加密 Key 落 DB（通过 `ctypes` 调 Crypt32） | 防误拷 DB 泄露明文 Key；不依赖第三方包 |
| HTTPS 终止 | v1 支持 TLS，可选开关 | 用户勾选"可以"；提供自签证书生成 |
| 前端 | Vanilla JS（无构建步骤） | 单文件 HTML，admin 后端直接 serve |
| 打包 | PyInstaller --onefile | 产出单 exe；带 Python 运行时 |
| Windows 服务 | `sc.exe` + 注册表 Run 键 | 不依赖 pywin32（pywin32 安装复杂），直接调系统命令 |

---

## 3. 数据模型

### 3.1 配置文件 `config.yaml`

```yaml
# 文件位置：%ProgramData%\Zhongzhuan\config.yaml (服务模式)
#           或 exe 同目录 / 用户指定 (绿色模式)

server:
  proxy:
    host: "127.0.0.1"          # 默认仅 loopback
    port: 8088
  admin:
    host: "127.0.0.1"
    port: 8089
  tls:
    enabled: false             # v1 支持，证书路径见下
    cert_file: ""
    key_file: ""

limits:
  global_concurrent: 64         # 全局信号量上限
  per_key_window_seconds: 60    # 滑动窗口长度
  default_rpm_per_key: 60       # 未显式配置时的兜底
  default_tpm_per_key: 100000
  proxy_request_timeout: 30     # 秒

storage:
  db_path: "data.db"
  log_dir: "logs"

windows_service:
  display_name: "Zhongzhuan API Relay"
  auto_start: true             # 启动服务时写入服务的 StartType
  service_name: "Zhongzhuan"   # sc.exe 使用的服务名
```

### 3.2 SQLite Schema

```sql
-- 模型定义（一个"模型"=一个上游 base_url + 一个 model 名 + 一组 key）
CREATE TABLE models (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,                -- 编程工具请求时使用的模型名
    upstream_base TEXT NOT NULL,                -- 如 https://api.deepseek.com/v1
    upstream_model TEXT NOT NULL,               -- 转发到上游时的 model 字段
    rpm_limit     INTEGER DEFAULT 0,            -- 单 key 60s 请求数，0 = 不限
    tpm_limit     INTEGER DEFAULT 0,            -- 单 key 60s token 数，0 = 不限
    enabled       INTEGER NOT NULL DEFAULT 1,
    weight        INTEGER NOT NULL DEFAULT 1,   -- group 内权重
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    UNIQUE(name)
);

-- API Key 池
CREATE TABLE api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    label       TEXT NOT NULL DEFAULT '',
    key_cipher  BLOB NOT NULL,                  -- DPAPI 加密后的字节
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 0,     -- 大者优先
    created_at  INTEGER NOT NULL
);

-- 模型分组
CREATE TABLE model_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,        -- 编程工具请求时使用的"虚拟模型名"
    strategy        TEXT NOT NULL CHECK(strategy IN ('round_robin','weighted','failover')),
    fallback_enabled INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL
);

CREATE TABLE group_models (
    group_id  INTEGER NOT NULL REFERENCES model_groups(id) ON DELETE CASCADE,
    model_id  INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    weight    INTEGER NOT NULL DEFAULT 1,
    ord       INTEGER NOT NULL DEFAULT 0,        -- failover 顺序
    PRIMARY KEY (group_id, model_id)
);

-- 请求日志（按 ts 索引，14 天后清理）
CREATE TABLE request_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    client_ip   TEXT,
    model_name  TEXT NOT NULL,                   -- 客户端请求的（可能是 group 名）
    resolved_model_id INTEGER,                   -- 实际命中的 model
    key_id      INTEGER,
    status      INTEGER NOT NULL,               -- HTTP 状态码
    latency_ms  INTEGER NOT NULL,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    error       TEXT DEFAULT '',                -- 错误摘要
    request_id  TEXT NOT NULL                    -- 透传客户端 X-Request-ID 或自生成
);
CREATE INDEX idx_logs_ts ON request_logs(ts);
CREATE INDEX idx_logs_model ON request_logs(model_name, ts);
```

### 3.3 内存态：Key 健康度

```python
@dataclass
class KeyHealth:
    key_id: int
    cooldown_until: float        # 该时刻前不参与调度
    window: SlidingWindow        # 60s 滑动窗口
    success_count: int           # 启动以来
    failure_count: int           # 启动以来
    recent_429_count: int        # 最近 1 分钟内 429 次数


class SlidingWindow:
    """60 个 1s 桶的循环数组，每秒推进一格；复杂度 O(1)。"""
    def __init__(self, window_seconds: int = 60):
        self.buckets = [0] * window_seconds
        self.last_rotate = time.time()

    def allow(self, n: int) -> bool: ...
    def rotate(self) -> None: ...
    def current_usage(self) -> int: ...
```

---

## 4. 调度算法（核心）

### 4.1 请求处理流程

```
Client 请求 POST /v1/chat/completions
   │
   ▼
[1] 解析 body → 拿到 model 字段
   │
   ├─ 不在配置中 → 404 {"error":"unknown_model"}
   │
   ▼
[2] model 字段 = "gpt-4o-group" 这样的 group 名?
   │
   ├─ YES → 进入 Group 调度（4.3）
   └─ NO  → 视为直指某个 model，进入 Model 调度（4.2）
   │
   ▼
[3] 拿到候选 (group → model) 列表后，对每个候选：
    a. 找该 model 的所有 enabled key
    b. 过滤: 冷却中 / 窗口已满 / disabled
    c. 评分:  health_score = α*success_rate - β*Recent429 - γ*Cooldown
              + 抖动(避免热点)
    d. 选 top-1
   │
   ▼
[4] 全局信号量 (max_concurrent) 等待
   │
   ▼
[5] 发起上游请求
    │
    ├─ stream=true  → 透传 SSE，遇断开则根据末段状态判定
    └─ stream=false → 拿到 status 后判定
   │
   ▼
[6] 状态判定:
    2xx       → 记成功日志 → 返回给 client
    429/5xx   → 标记该 key 短时 cooldown（指数退避，上限 60s）
                → 切下一个 key 重试（同一 model 内）
                → 同 model 全失败 → fallback 下一个 model
                → 全失败 → 排队等待（不立即 5xx）
    4xx(非429)→ 记录 → 直接返回 client（不该重试）
   │
   ▼
[7] 返回响应（或排队的 503）
```

### 4.2 单 Model 调度（同 model 多 key）

- **评分函数**（每次选 key 调一次）：
  ```
  health_score = 
      + 0.50 * (1 - recent_429 / max(1, recent_total))   // 成功率
      + 0.30 * (1 - current_window_usage / window_limit) // 窗口余量
      + 0.15 * (priority_normalized)                     // 用户优先级
      + 0.05 * random.random()                             // 抖动
      - penalty: cooldown 中直接 0
  ```
- **Cooldown 时长**（命中 429/5xx 时设置）：
  - 第 1 次：5s
  - 第 2 次：10s
  - 第 3 次：30s
  - 上限 60s；成功后 1 分钟内无 429 则重置

### 4.3 Group 调度（多模型轮转）

- **strategy = `round_robin`**：
  - 维护一个轮询指针（持久化在内存），按 group_models 顺序选
  - 该 model 全部 key 失败才往下走
  - 走完一圈都失败 → 全组 cooldown 10s，期间直接 503（不无限重试）
- **strategy = `weighted`**：
  - 按 `weight` 加权随机抽 model
  - 失败时该 model 临时降权 50%，1 分钟后恢复
- **strategy = `failover`**：
  - 按 `ord` 字段固定顺序，永远先试第一个
  - 失败才往下走（适合"主力 GPT-4o + 备用 DeepSeek"）

- **fallback_enabled**：开启后，组内所有 model 都失败时会再尝试一次（每个 model 重置 key cooldown）；关闭则直接 503

### 4.4 限流整形

**层 1 - 全局并发信号量**

- `asyncio.Semaphore(global_concurrent)` 实现
- 拿不到时进入等待队列，等待 `proxy_request_timeout`（默认 30s）后返回 503
- 流式请求按"开始时占位、结束时释放"处理

**层 2 - Key 维度滑动窗口**

- 每个 key 维护一个 60s 滑窗
- 维度：RPM（请求数）+ TPM（token 数），两个维度任一超限即熔断
- token 计数来源：响应头 `x-usage-*` / body 末尾 usage 块（如果上游返回）；非流式取得到，流式根据 chunk 累计
- 窗口实现：60 个 1s 桶的循环数组，每秒推进一格；复杂度 O(1)

### 4.5 协议透传细节

为保证**对所有 OpenAI 兼容变体零修改可用**：

- **请求**：method / path / headers / body 完全透传
  - 替换 `Authorization: Bearer xxx` 为选中的 key
  - 透传 `X-Request-ID`，无则自生成 ULID 写日志
  - 不修改 body（必要时重写 model 字段为 `upstream_model`）
- **响应**：
  - 非流式：直接读全部 body，再写回 client（保留 status / headers）
  - 流式：边读上游 chunk 边写 client；读到 `event: error` 或连接断 → 标记该次失败
- **错误判定依据**（不解析 body）：
  - 非流式：HTTP status >= 500 || == 429 → 失败
  - 流式：上游连接提前断（无 `data: [DONE]`） → 失败
  - 4xx（非 429）：非失败，原样返回

---

## 5. HTTP 路由

### 5.1 代理端口（仅 loopback 默认）

| 方法 | 路径 | 说明 |
|---|---|---|
| 任意 | `/v1/*` | 透传，命中调度器 |
| GET | `/v1/models` | 返回合并的 `{"data":[{"id":"gpt-4o",...}, ...]}`，id 来自 models + model_groups.name |
| GET | `/healthz` | `200 OK`，无 body（健康检查用） |
| GET | `/version` | 返回 `zhongzhuan/0.1.0` |

> **注**：`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings` 三个是必须支持的（编程工具常用）；其他 `/v1/*` 路径也透传但不计入日志指标。

### 5.2 管理端口（仅 loopback 默认）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 返回 `web/index.html`（importlib.resources） |
| GET | `/static/*` | 静态资源 |
| GET/POST/PUT/DELETE | `/api/models` | 模型 CRUD |
| GET/POST/PUT/DELETE | `/api/keys` | Key CRUD（响应中 key_value 永远遮显） |
| GET/POST/PUT/DELETE | `/api/groups` | 分组 CRUD |
| GET | `/api/stats?range=1h` | QPS、成功率、Top 错误（结构化 JSON） |
| GET | `/api/logs?cursor=&limit=&model=&status=` | 日志分页 |
| GET | `/api/service/status` | Windows 服务状态（running/stopped/...） |
| POST | `/api/service/start` | 启动服务 |
| POST | `/api/service/stop` | 停止服务 |
| POST | `/api/service/autostart` | 切换开机自启（`{"enabled": true}`） |
| POST | `/api/reload` | 热重载配置（不重启进程） |
| GET | `/api/export` | 导出全部配置（YAML+Keys，Key 用 DPAPI 在导出端解密） |
| POST | `/api/import` | 导入配置（**可选** DPAPI 重新加密 or 保持明文） |
| GET | `/api/config` | 返回当前生效配置（脱敏） |

> **服务控制权限**：服务控制 API 要求**当前进程以管理员身份运行**（安装器安装后默认是）。如果当前不是管理员，则返回 403 提示用管理员启动管理端口。

### 5.3 错误码（对客户端可见）

| 场景 | 状态码 | Body |
|---|---|---|
| 未知模型 | 404 | `{"error":{"message":"unknown model: xxx","type":"invalid_request_error"}}` |
| 全组 cooldown | 503 | `{"error":{"message":"all models in cooldown, retry after 10s","type":"server_overloaded"}}` |
| 排队超时 | 503 | `{"error":{"message":"upstream busy, queued too long","type":"server_overloaded"}}` |
| 4xx 透传 | 同上 | 同上 |
| 5xx/429 全部重试失败 | 502 | `{"error":{"message":"upstream failed after retries","type":"upstream_error"}}`（带上原 status） |

---

## 6. Web 管理后台

### 6.1 页面结构

单页应用（SPA 不需要框架，hash router + 标签页即可），5 个 tab：

1. **总览（Overview）**
   - 顶部 4 个卡片：当前 QPS、成功率、平均延迟、活跃 Key 数
   - 中部折线图（Canvas 自绘，每 5s 重画一次）：QPS、错误率
   - 底部：按模型分组的实时错误率 Top 5
2. **模型（Models）**
   - 列表：name / upstream / 限流 / 启用 / key 数
   - 编辑：弹窗，可改 name / upstream / upstream_model / rpm / tpm / 启用
3. **Key 池（Keys）**
   - 列表：label / 模型 / 优先级 / 健康度（绿/黄/红条 + cooldown 倒计时）/ 启用
   - 新增：粘贴 key → 遮显为 `sk-***abc123`；保存时 DPAPI 加密入库
   - 操作：测试（发一个最小 chat completion 请求，看是否 200）、删除
4. **分组（Groups）**
   - 列表：name / strategy / 包含的模型
   - 编辑：选 strategy、从已有模型里勾选、按 strategy 设权重或顺序
5. **日志（Logs）**
   - 流式表格，列：时间 / 模型 / key label / status / 延迟 / 错误摘要
   - 过滤：模型 / status 范围 / 时间 / 关键字
   - 实时刷新（每 2s）

### 6.2 顶部工具条

- 左侧：服务状态徽章（绿点=运行中 / 红点=已停止 / 黄点=未注册）
- 中间：服务控制按钮：「启动」「停止」「注册为服务」「卸载服务」「开机自启: ✓/✗」
- 右侧：「导出配置」「导入配置」「关于」

---

## 7. Windows 集成

### 7.1 Windows Service 封装

> Python 切换后**不依赖** `kardianos/service`。直接用 `sc.exe` 子命令管理服务，`pythonw.exe` + 我们的 exe 作为服务入口。

**服务注册原理**：

1. 安装时调用 `sc.exe create Zhongzhuan binPath= "<exe_path> --service" start= auto`
2. `--service` 标志：进程不再监听 SIGINT 退出，而是 `sc.exe` 给的 stop 命令触发优雅关闭
3. 启动：`net start Zhongzhuan` 或 `sc start Zhongzhuan`
4. 状态：`sc query Zhongzhuan`
5. 卸载：`sc delete Zhongzhuan`

**子命令清单**：

```
zhongzhuan.exe                     # 前台运行
zhongzhuan.exe --service           # Windows Service 入口（仅由 SCM 调用）
zhongzhuan.exe install             # 注册服务（管理员）
zhongzhuan.exe uninstall           # 卸载服务
zhongzhuan.exe start               # 启动服务
zhongzhuan.exe stop                # 停止服务
zhongzhuan.exe restart             # 重启服务
zhongzhuan.exe status              # 查询服务
zhongzhuan.exe autostart on|off    # 切换 StartType
zhongzhuan.exe tls selfsign ...    # 生成自签证书
zhongzhuan.exe open-admin          # 浏览器打开管理后台
```

服务注册信息：
- `ServiceName`: `Zhongzhuan`（可在 config.yaml 改）
- `DisplayName`: `Zhongzhuan API Relay`
- `StartType`: 由 `config.yaml` 的 `windows_service.auto_start` 决定（默认 `auto`）
- **自启修改时机**：用户在 Web 后台切换"开机自启" → 调 `sc config Zhongzhuan start= <auto|demand>` 立即生效

### 7.2 服务 vs 前台

- 服务模式：服务进程启动后，UI 控制通过管理端口 → 调用 `sc start/stop` 或服务控制 API
- 前台模式：用户双击 exe，浏览器自动打开 `http://127.0.0.1:8089`（5s 内未打开则提示）
- 两种模式互斥：检测到服务已注册并运行时，前台启动直接退出并提示"已在服务中运行"

### 7.3 服务自启策略

- `auto_start = true` → 服务 StartType = `auto`（默认）
- `auto_start = false` → StartType = `demand`（用户需要时手动启动）
- **延迟自启**（可选）：通过 `sc config Zhongzhuan start= delayed-auto` 实现，避免开机风暴
- 切换方式：
  - Web 后台工具条
  - CLI：`zhongzhuan.exe autostart on|off`
  - 直接改 `config.yaml` 后 `zhongzhuan.exe reload`

### 7.4 安装包 vs 绿色版

| 维度 | 安装包 (Inno Setup) | 绿色版 |
|---|---|---|
| 路径 | `C:\Program Files\Zhongzhuan\` | 用户自选 |
| 服务注册 | ✅ 默认注册为 Windows Service | ❌ 可选 `green-install.bat` 注册 |
| 开机自启 | 由服务 StartType 控制 | 通过 Run 注册表键（HKCU）实现用户级自启 |
| 卸载 | 控制面板"程序和功能" | 删除文件夹 + 取消 Run 键 |
| 权限 | 需要管理员 | 不需要 |

### 7.5 防火墙

- 代理端口 / 管理端口默认绑 127.0.0.1，无需防火墙规则
- 若用户改为 LAN 监听（`host: 0.0.0.0`），安装器提示"将开放 Windows 防火墙入站规则（可选）"
- v1 不自动添加防火墙规则，由用户手动开

---

## 8. TLS / HTTPS（v1 可选支持）

- `config.yaml` 中 `server.tls.enabled = true` 时启动 `https.ListenAndServeTLS`
- 证书路径：`server.tls.cert_file` / `server.tls.key_file`
- 内置工具：`zhongzhuan.exe tls selfsign --cn localhost --out cert.pem --key key.pem`（v1 提供，基于 `cryptography` 或 stdlib `ssl` + `pyOpenSSL`）
- 编程工具对自签证书的处理：用户需在 Cursor/Cline 等配置中关闭"verify ssl"或手动信任

---

## 9. 配置导入 / 导出

### 9.1 导出

```
GET /api/export
→ 返回 zip 流，结构：
  config.yaml
  keys.json   # {"<model_name>": [{"label": "...", "key": "sk-..."}, ...]}
```

- Key 始终以**明文**导出（这是用户主动行为）
- zip 可选密码加密（v1 不做，v2 再加）

### 9.2 导入

```
POST /api/import
  Content-Type: multipart/form-data
  file: <zip>
```

行为：
- 校验 zip 结构
- 合并策略（v1 简单版）：**全量覆盖**（导入前先提示"将清空现有配置"）
- 导入完成后 `reload`

### 9.3 用途

- 换机迁移
- 多机部署（开发机、家里台式、公司本）
- 备份

---

## 10. 安全

| 关注点 | 措施 |
|---|---|
| Key 明文落盘 | **DPAPI**（`CryptProtectData` via ctypes）加密后存 SQLite，绑定当前用户 |
| 管理端口被局域网嗅探 | 默认绑 127.0.0.1；改为 0.0.0.0 时 Web 后台顶部加显眼的"已暴露到 LAN"警告 |
| 日志泄露 Key | 日志只写 `key_id` 和 `label`，绝不写 key_value |
| 错误响应泄露上游 URL | 错误响应只暴露 group 名 / model 名，不含 upstream_base |
| 服务控制 API | 仅当进程以管理员运行时开放；非管理员返回 403 + 提示用管理员启动 |
| 路径穿越 | 静态资源白名单 + importlib.resources |
| SQL 注入 | 参数化查询（`sqlite3` + `?` 占位符） |
| CSRF | 管理 API 同源；如有需要可加 token 校验（v1 不做） |
| Web 暴露 | 默认 127.0.0.1，绑 0.0.0.0 时控制台打 WARN |

---

## 11. 项目结构

```
zhongzhuan/
├── src/zhongzhuan/                # 主包（避免与目录同名冲突）
│   ├── __init__.py
│   ├── __main__.py                # 入口：python -m zhongzhuan
│   ├── app.py                     # AppContext + 启动编排
│   ├── config/
│   │   ├── __init__.py
│   │   ├── config.py              # YAML 加载 + 默认值 (dataclass)
│   │   └── paths.py               # %ProgramData% 路径解析
│   ├── observability/
│   │   ├── __init__.py
│   │   └── log.py                 # loguru / logging 配置
│   ├── store/
│   │   ├── __init__.py
│   │   ├── store.py               # SQLite 初始化、迁移
│   │   ├── schema.py              # CREATE TABLE 字符串
│   │   ├── models.py              # 模型 CRUD
│   │   ├── keys.py                # Key CRUD
│   │   ├── groups.py              # 分组 CRUD
│   │   └── logs.py                # 日志 + 统计
│   ├── crypto/
│   │   ├── __init__.py
│   │   ├── dpapi_windows.py       # DPAPI (Crypt32 via ctypes)
│   │   └── dpapi_other.py         # 非 Windows 平台 stub (抛错)
│   ├── upstream/
│   │   ├── __init__.py
│   │   └── client.py              # httpx.AsyncClient 包装
│   ├── proxy/
│   │   ├── __init__.py
│   │   ├── server.py              # aiohttp App: 代理端口
│   │   ├── handler.py             # /v1/* 路由
│   │   ├── scheduler.py           # 选 model / 选 key
│   │   ├── ratelimit.py           # 信号量 + 滑动窗口
│   │   ├── retry.py               # 重试 & fallback
│   │   └── stream.py              # SSE 透传
│   ├── admin/
│   │   ├── __init__.py
│   │   ├── server.py              # aiohttp App: 管理端口
│   │   ├── api_models.py
│   │   ├── api_keys.py
│   │   ├── api_groups.py
│   │   ├── api_service.py         # 服务控制 (调 sc.exe)
│   │   ├── api_stats.py
│   │   ├── api_logs.py
│   │   ├── api_export_import.py
│   │   └── ui.py                  # 静态资源 (importlib.resources)
│   ├── service/
│   │   ├── __init__.py
│   │   └── service.py             # sc.exe 封装
│   └── web/                       # 前端 (embed)
│       ├── index.html
│       ├── app.js
│       └── style.css
├── scripts/
│   ├── build.ps1                  # PyInstaller + Inno Setup
│   ├── dev.ps1                    # 开发模式
│   ├── installer.iss              # Inno Setup 脚本
│   ├── green-install.bat          # 绿色版注册 Run 键
│   └── green-uninstall.bat
├── tests/                         # pytest
│   ├── test_config.py
│   ├── test_upstream.py
│   ├── test_proxy.py
│   ├── test_ratelimit.py
│   ├── test_scheduler.py
│   ├── test_store.py
│   ├── test_crypto.py
│   └── test_admin.py
├── pyproject.toml                 # 项目元数据 + 依赖
├── requirements.txt               # 运行时依赖
├── LICENSE                        # MIT
└── README.md
```

---

## 12. 关键依赖

| 库 | 用途 | 备注 |
|---|---|---|
| `aiohttp` (3.9+) | HTTP server (代理 + 管理) | 异步路由，SSE 友好 |
| `httpx` (0.27+) | 上游 HTTP 客户端 | 异步，SSE 流式 |
| `pyyaml` | YAML 配置 | |
| `sqlite3` (stdlib) | 数据库 | Python 内置 |
| `cryptography` (可选) | TLS 自签证书生成 | 备用方案：用 `openssl` 命令行 |
| `pyinstaller` (build only) | 打包成单 exe | dev 依赖 |

**刻意不引入**：
- 任何 ORM（用原生 SQL）
- 任何前端框架（vanilla JS）
- pywin32（用 ctypes 调 Crypt32 + subprocess 调 sc.exe）
- 任何大型框架（Django / FastAPI / SQLAlchemy）

---

## 13. 配置 & 数据目录

| 内容 | 路径（服务模式） | 路径（绿色模式） |
|---|---|---|
| 配置 | `%ProgramData%\Zhongzhuan\config.yaml` | exe 同目录 `config.yaml` |
| 数据库 | `%ProgramData%\Zhongzhuan\data.db` | exe 同目录 `data.db` |
| 日志 | `%ProgramData%\Zhongzhuan\logs\app-YYYY-MM-DD.log` | exe 同目录 `logs\` |
| 证书 | `%ProgramData%\Zhongzhuan\certs\` | exe 同目录 `certs\` |

> 绿色版首次启动时检测到目录不可写 → 提示"将使用 %APPDATA%\Zhongzhuan\"。

---

## 14. 启动序列

```
1. 解析命令行 (--config / --port / --service / install / uninstall / start / stop / autostart / ...)
2. 加载 config.yaml，不存在则生成默认
3. 初始化日志（logging → 文件 + stderr）
4. 打开 SQLite (sqlite3 + WAL)，跑迁移
5. 初始化 DPAPI provider（Windows；非 Windows 仅 dev 模式）
6. 加载 models / keys / groups 到内存
7. 启动限流器（asyncio.Semaphore）
8. 启动代理 HTTP 服务（aiohttp，绑 proxy host:port）
9. 启动管理 HTTP 服务（aiohttp，绑 admin host:port）
10. 若前台模式 → 5s 后打开浏览器 http://127.0.0.1:admin_port
11. 监听 SIGINT / SIGTERM / SCM stop → 优雅关闭（30s 超时）
```

---

## 15. 验收场景（M7 终点）

- [ ] 启动 zhongzhuan，Cursor 把 `base_url` 改成 `http://127.0.0.1:8088/v1`，能正常对话
- [ ] 配置 2 个 model，5 个 key；压测发送 100 并发请求，所有 429 在 5s 内被吸收，客户端无感
- [ ] 上游宕机时，fallback 到第二个 model，客户端 0 中断
- [ ] Web 后台能看到实时 QPS、错误率、每个 key 的健康度
- [ ] `zhongzhuan.exe install` 注册服务成功，sc query Zhongzhuan 显示 running
- [ ] Web 后台"开机自启"开关切换后，`sc qc Zhongzhuan` 中 StartType 同步变化
- [ ] 重启 Windows，服务自动拉起
- [ ] 导出配置 → 在另一台机器导入 → 立即可用
- [ ] 卸载（控制面板）后，%ProgramData% 数据保留，配置/日志不丢
- [ ] PyInstaller 打包后 exe < 30MB（Python 运行时 + 依赖）

---

## 16. 里程碑

| M | 内容 | 验收 |
|---|---|---|
| M1 | 最小可跑：Python 起 aiohttp、透传 `/v1/chat/completions`、硬编码 1 key、命令行启动 | curl 能通 |
| M2 | 调度器：多 key 轮转、滑动窗口、429/5xx 重试 | 压测脚本验证 |
| M3 | 持久化：SQLite + 基础管理 API（模型/Key CRUD）+ Web 后台 | Web 后台能增删 |
| M4 | 多模型分组 + 三种 strategy + fallback | group 调度验证 |
| M5 | Windows Service (sc.exe 封装) + 安装包 (Inno Setup) + 绿色版 | 服务能装能卸 |
| M6 | 指标可视化、请求日志、Key 加密 (DPAPI via ctypes) | 后台能看到历史 |
| M7 | 配置导入/导出、TLS 可选开关、PyInstaller 打包、文档与发布 | README + exe 下载 |

---

## 17. 风险与权衡

| 风险 | 权衡 |
|---|---|
| Python 启动慢 + 内存占用高 | 本机单用户场景下无感；前台启动比 Go 多 200ms |
| asyncio + Windows 兼容性 | aiohttp 在 Windows ProactorEventLoop 下稳定；避免使用多进程 |
| pywin32 缺失导致服务注册复杂 | 用 `sc.exe` 子命令 + 路径参数，逻辑直白且无需编译依赖 |
| PyInstaller 打包体积大 | 单 exe < 30MB 可接受；可后续上 `nuitka` 进一步压缩 |
| 单 key 上游配额语义不一致 | 用 RPM/TPM 兜底，配额耗尽行为交给上游 429 |
| SSE 中途 429 难以判定 | 通过"读到 data: [DONE] 才算成功"保守判断，重试会浪费已读 token |
| DPAPI 跨用户/跨机器不可移植 | Key 备份需要导出；这与"配置导入/导出"对齐 |
| Windows Service 权限问题 | 服务控制 API 限制为管理员；非管理员启动时仅开放只读管理 API |
| v1 不做自动更新 | 用户手动重装安装包；减小 v1 复杂度 |

---

## 18. 待确认（已确认则删除本节）

- [x] v1 仅 OpenAI Chat Completions（含 Embeddings）
- [x] Web 后台 + Windows 服务，运行时可启停
- [x] 默认端口可改（proxy 8088 / admin 8089）
- [x] MIT 协议
- [x] 配置导入/导出
- [x] 默认本机监听（127.0.0.1，可改）
- [x] 安装器（Inno Setup）+ 绿色版（双形态）
- [x] 实现语言：Python 3.10+（已从 Go 切换）
