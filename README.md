# zhongzhuan（中转）

本地优先的 **LLM API 中转代理**：对下游暴露统一的 OpenAI / Anthropic / OpenAI Responses 三种协议入口，
对上游转发到任意 OpenAI 兼容或 Anthropic 兼容服务，并在三种协议之间做**双向转换**。

面向的典型场景是「AI 编码工具（Codex / Claude Code / Cursor / Cline / Trae）+ 多个上游 key」：
工具只认一种协议，而你手上的 key 可能是另一种协议、分散在多个供应商、各有各的限额。
zhongzhuan 负责把这些差异抹平。

---

## 1. 能力概览

| 能力 | 说明 |
|---|---|
| 协议转换 | OpenAI Chat Completions ⇄ Anthropic Messages ⇄ OpenAI Responses，流式与非流式均支持 |
| 多 key 调度 | 按健康度打分选 key，401/403 拉黑、429 冷却、5xx 退避，自动切换下一个 key |
| 限额治理 | 每 key 独立 RPM / TPM / RPD 滑动窗口；从上游 `x-ratelimit-*` 响应头自动学习真实限额 |
| 会话粘滞 | 同一会话默认粘在同一个 key 上，避免多轮对话中途换模型 |
| 管理后台 | 内置 Web UI：模型 / key / 分组 / 令牌 / 日志 / 统计 / 计费 |
| 存储后端 | SQLite（本地默认）或 TiDB Cloud（VPS 部署） |
| 凭据加密 | 上游 API key 以 AES-256 加密落库，Windows 上可叠加 DPAPI |
| 兜底上游 | 无可用 key 时可降权路由到免费兜底上游 |

支持的下游入口：

```
POST /v1/chat/completions     # OpenAI Chat Completions
POST /v1/messages             # Anthropic Messages
POST /v1/responses            # OpenAI Responses（Codex）
GET  /v1/models
GET  /healthz
GET  /version
```

---

## 2. 安装

要求 **Python >= 3.10**。

### 2.1 标准安装

```bash
git clone <repo-url> zhongzhuan
cd zhongzhuan
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/macOS:  source .venv/bin/activate

pip install .
```

验证：

```bash
python -c "import zhongzhuan; print(zhongzhuan.__version__)"
zhongzhuan --help
```

### 2.2 可选依赖分组（extras）

`pyproject.toml` 是**依赖的唯一事实源**。`requirements.txt` / `requirements-vps.txt` 都是它的派生物，
不要单独修改那两个文件。

| extra | 内容 | 何时需要 |
|---|---|---|
| `sqlite` | 空（`aiosqlite` 已在核心依赖） | 占位，保持 CLI 口径一致 |
| `tidb` | `aiomysql` | 使用 TiDB Cloud 作为存储后端 |
| `admin` | `PyJWT`、`bcrypt` | 启用管理后台登录鉴权 |
| `build` | `pyinstaller` | 打包单文件 exe |
| `mcp` | `mcp` | Remote MCP hosted tool（v3 Phase 2） |
| `metrics` | `prometheus-client`、`opentelemetry-sdk`、otlp exporter | `/metrics` 与链路追踪 |
| `test` | `pytest`、`pytest-asyncio`、`pytest-cov`、`hypothesis`、`respx` | 跑测试 |
| `lint` | `ruff`、`mypy`、`bandit`、`pip-audit` | 静态检查与依赖扫描 |
| `dev` | 以上除 `build` 外的合集 | 本地开发一把梭 |

常用组合：

```bash
pip install .                    # 最小运行时
pip install ".[tidb,admin]"      # VPS 部署（TiDB + 后台鉴权）
pip install ".[dev]"             # 本地开发
pip install -e ".[dev]"          # 可编辑安装
```

---

## 3. 配置

配置有三层，**优先级由低到高**：内置默认值 < `config.yaml` < 环境变量。

### 3.1 `config.yaml`

默认从当前工作目录读取，可用 `--config` 指定路径。

```yaml
server:
  proxy:                      # 下游客户端连这个端口
    host: 127.0.0.1
    port: 8088
  admin:                      # 管理后台
    host: 127.0.0.1
    port: 8089
  tls:
    enabled: false
    cert_file: ""
    key_file: ""

limits:
  global_concurrent: 64       # 全局并发上限
  per_key_window_seconds: 60  # 限流滑动窗口长度
  default_rpm_per_key: 60
  default_tpm_per_key: 100000
  default_rpd_per_key: 0      # 0 = 不限
  sticky_session_ttl: 1800    # 会话粘滞 TTL（秒）
  proxy_request_timeout: 30   # 上游请求超时（秒）；AGENTS 类模型建议 300

storage:
  backend: auto               # auto | sqlite | tidb
  db_path: data.db
  log_dir: logs

fallback:
  enabled: true               # 无可用 key 时是否走免费兜底上游
  fallback_penalty: 0.1       # 兜底 key 的调度降权系数
```

### 3.2 环境变量

支持 `.env` 文件（参考仓库根目录的 `.env.example`）。常用项：

| 变量 | 作用 |
|---|---|
| `ZHONGZHUAN_PROXY_HOST` / `ZHONGZHUAN_PROXY_PORT` | 代理监听地址与端口 |
| `ZHONGZHUAN_ADMIN_HOST` / `ZHONGZHUAN_ADMIN_PORT` | 管理后台监听地址与端口 |
| `ZHONGZHUAN_PROXY_REQUEST_TIMEOUT` | 上游请求超时（秒） |
| `ZHONGZHUAN_ADMIN_AUTH` | `true` 启用后台登录鉴权（需 `[admin]` extra） |
| `ZHONGZHUAN_ADMIN_USER` / `ZHONGZHUAN_ADMIN_PASSWORD` | 初始管理员账号 |
| `ZHONGZHUAN_JWT_SECRET` | 后台 JWT 签名密钥，留空自动生成 |
| `ZHONGZHUAN_PROXY_AUTH` | `true` 启用 `/v1/*` 访问令牌鉴权 |
| `ZHONGZHUAN_SECRET_KEY` | AES-256 主密钥（hex），留空自动生成到 `data/secret.key` |
| `ZHONGZHUAN_TIDB_*` | TiDB Cloud 连接参数（需 `[tidb]` extra） |
| `ZHONGZHUAN_UPSTREAM` / `ZHONGZHUAN_KEY` | 不用后台时的单上游快速启动 |

---

## 4. 启动

```bash
# 用 config.yaml 启动（代理 8088 / 后台 8089）
zhongzhuan

# 指定配置文件
zhongzhuan --config /etc/zhongzhuan/config.yaml

# 不用后台，直接指定单个上游
zhongzhuan --upstream https://api.openai.com --key sk-xxxx --port 8088

# 打开管理后台
zhongzhuan --open-admin

# 生成自签 TLS 证书
zhongzhuan --tls-selfsign --cn localhost --san-ip 127.0.0.1
```

Windows 服务（需管理员权限）：

```bash
zhongzhuan --install      # 安装为系统服务
zhongzhuan --start
zhongzhuan --stop
zhongzhuan --uninstall
```

下游客户端指向：

```
OpenAI 兼容工具        →  http://127.0.0.1:8088/v1
Anthropic 兼容工具     →  http://127.0.0.1:8088
Codex（Responses）     →  http://127.0.0.1:8088/v1
```

---

## 5. v3 特性开关：Responses 桥接层

v3 把 `/v1/responses` 的桥接实现重写为**防循环、完全兼容官方 API**的新实现，并用特性开关控制新旧切换。
开关**只作用于 `inbound_protocol == "responses"`**，Chat→Chat 与 Chat↔Anthropic 链路完全不受影响。

### 5.1 YAML

```yaml
responses_bridge:
  enabled: true          # 默认 true，使用 v3 实现
```

### 5.2 环境变量硬覆盖

环境变量优先级**高于** YAML，用于线上紧急回滚而不必改配置文件：

```bash
ZHONGZHUAN_RESPONSES_BRIDGE_V3=1   # 强制启用 v3
ZHONGZHUAN_RESPONSES_BRIDGE_V3=0   # 强制回退到 v2（旧 ResponsesStreamTranslator 路径）
```

### 5.3 运行时切换

改完 `config.yaml` 后调用 reload，**下一个请求**即生效，无需重启：

```bash
curl -X POST http://127.0.0.1:8089/api/reload
```

> 说明：`responses_bridge` 配置节由 v3 Phase 1（T12 特性开关 + 精确路由）引入。
> 在该任务合入之前，`/v1/responses` 走的仍是既有实现，设置该开关不会报错但也不会改变行为。

---

## 6. 开发

```bash
pip install -e ".[dev]"
```

### 6.1 跑测试

```bash
# 完整套件（跳过需要真实网络的用例）
pytest -q --ignore=tests/test_real_e2e.py --ignore=tests/test_real_agnes.py

# 只跑旧协议 golden 基线回归
pytest -q tests/test_legacy_golden.py

# 重新生成 golden 基线（**仅在确认行为变更是预期的时候**执行）
UPDATE_GOLDEN=1 pytest -q tests/test_legacy_golden.py
```

### 6.2 静态检查

```bash
ruff check .
mypy src/zhongzhuan
bandit -r src/zhongzhuan -ll
pip-audit
```

### 6.3 需要真实上游的手工检查

这些脚本会发起真实网络请求，**不属于测试套件**，需要手工执行：

```bash
python scripts/live_check.py --base-url http://127.0.0.1:8088 --model your-model
python tests/seed_admin_api.py          # 需先设置 AGNES_API_KEY 环境变量
```

### 6.4 目录结构

```
src/zhongzhuan/
  config/         配置加载与路径解析
  crypto/         AES-256 + DPAPI 凭据加密
  proxy/          代理服务、鉴权、限流、调度、重试
    protocol/     三协议互转（请求 / 响应 / 流式状态机）
  upstream/       上游 httpx 客户端
  store/          SQLite / TiDB 存储层
  admin/          管理后台 API + UI
  observability/  日志
tests/
  support/        测试基建（SSE 断言器、可编程 mock 上游）
  golden/legacy/  旧协议字节级基线
scripts/          手工运维 / 检查脚本
docs/v3/          v3 改造的决策记录、PRD、架构设计
```

---

## 7. 许可证

见 [LICENSE](LICENSE)。
