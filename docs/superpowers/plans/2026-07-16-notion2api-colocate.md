# Notion2API + Zhongzhuan 同机部署方案

> 版本 v1.0 · 2026-07-16
> 目标：在同一台 VPS 上同时运行 zhongzhuan（对外 HTTPS 网关）和 Notion2API（Notion AI → OpenAI 兼容桥接），让 Claude Code 通过 zhongzhuan 访问 Notion AI 提供的 GPT-5.x / Claude Sonnet/Opus 4.x / Gemini 等模型，**零代码改动**，纯配置集成。

---

## 1. 架构总览

```
                                    ┌─────────────────────────────────────┐
                                    │              VPS (单机)              │
                                    │                                     │
Claude Code ────HTTPS:8443─────────►│  zhongzhuan:8443                    │
  (本地)                            │   ├─ inbound: anthropic/openai      │
  ANTHROPIC_BASE_URL=               │   ├─ /v1/messages  → a2o 翻译       │
  https://<host>:8443               │   └─ /v1/chat/completions → 透传    │
  ANTHROPIC_API_KEY=<zz-token>      │        │                            │
                                    │        ├─► OpenAI 官方 (HTTPS 出站) │
                                    │        ├─► Anthropic 官方          │
                                    │        └─► 127.0.0.1:8787 ──────────┼──► Notion2API:8787
                                    │              (本机回环)             │   ├─ OpenAI 兼容端点
                                    │                                     │   ├─ 账号池 + 登录态刷新
                                    │  admin:8089 (0.0.0.0 或 SSH 隧道)   │   └─ uTLS 伪装 → Notion 网页端
                                    └─────────────────────────────────────┘
```

### 端口规划

| 端口 | 服务 | 监听地址 | 对外暴露 | 说明 |
|---|---|---|---|---|
| 22 | sshd | 0.0.0.0 | ✅ | SSH + 隧道 |
| 8443 | zhongzhuan proxy | 0.0.0.0 | ✅ (TLS) | Claude Code 入口 |
| 8089 | zhongzhuan admin | 0.0.0.0 或 127.0.0.1 | 可选 | 配后台 |
| 8787 | Notion2API | **127.0.0.1** | ❌ | 仅本机，被 zhongzhuan 调用 |
| 80 | certbot standalone | 临时 | ✅ | 仅签发证书时占用 |

**关键**：Notion2API 必须绑 `127.0.0.1`，不对外暴露——它没有 zhongzhuan 那套 access token 鉴权层，直接暴露等于把 Notion 账号池裸奔给公网。

### 协议链路（Claude Code → Notion AI）

1. Claude Code 用 Anthropic 协议打 `POST https://<host>:8443/v1/messages`
2. zhongzhuan `detect_inbound_protocol` → `anthropic`
3. 选中的 key 关联的 Model `protocol=openai`，`upstream_base=http://127.0.0.1:8787/v1`
4. `need_translation = True`（anthropic ≠ openai）
5. `translate_request_a2o` 把 Anthropic body 翻成 OpenAI body，path 改为 `/v1/chat/completions`
6. 加 `Authorization: Bearer <notion2api-api_key>` 转发到 Notion2API
7. Notion2API 内部：账号池选号 → uTLS 伪装 → 调 Notion 网页 AI workflow → 拿到流式响应
8. Notion2API 返回标准 OpenAI `chat.completion` / SSE `chat.completion.chunk`
9. zhongzhuan `translate_response_o2a` 把 OpenAI 响应翻回 Anthropic 格式（流式走 `StreamO2A` 状态机）
10. Claude Code 收到标准 Anthropic 响应

---

## 2. 前置条件

### 2.1 VPS 资源

| 项 | 最低 | 推荐 |
|---|---|---|
| CPU | 1 核 | 2 核 |
| 内存 | 1 GB | 2 GB（Go 编译吃内存） |
| 磁盘 | 5 GB | 10 GB |
| 系统 | Ubuntu 22.04+ / Debian 12+ / CentOS 9+ | Ubuntu 24.04 |
| 公网 | 有域名 A 记录指向 VPS，或仅有公网 IP | 有域名 |

### 2.2 软件依赖

```bash
# zhongzhuan 侧（deploy.sh 会自动装）
python3 python3-pip openssl ufw certbot

# Notion2API 侧
go >= 1.25      # 编译用；或直接用 Docker
git curl
```

### 2.3 Notion 账号准备

Notion2API 需要至少一个 Notion 账号的登录态 `probe.json`，**这是前置硬性依赖**，没有它整个链路跑不通。

获取方式（二选一）：

**方式 A：本地引导登录（推荐）**
1. 在本地（非 VPS）克隆 Notion2API，跑 `./notion2api --login`，会打开浏览器
2. 用 Notion 账号登录，脚本自动抓取 probe.json
3. 把 `probe_files/notion_accounts/<email>/probe.json` 整个目录上传到 VPS

**方式 B：VPS 上无头登录**
1. VPS 装 chromium-headless
2. 在 VPS 上跑登录流程（较折腾，不推荐）

详细登录步骤见 Notion2API 仓库 README 的"登录引导"章节。

---

## 3. 部署步骤

### 3.1 部署 zhongzhuan（已完成可跳过）

如果还没部署 zhongzhuan，先跑：
```bash
sudo ./deploy.sh --cert-path letsencrypt --domain api.macc.eu.cc
# 或仅有 IP：
sudo ./deploy.sh --cert-path selfsign --ip <VPS公网IP> --san-dns api.macc.eu.cc
```

确认 `curl https://<host>:8443/healthz` 返回 `ok`。

### 3.2 部署 Notion2API

#### 3.2.1 拉取代码 + 编译

```bash
sudo mkdir -p /opt/notion2api
sudo chown -R $USER:$USER /opt/notion2api
cd /opt/notion2api
git clone https://github.com/maxiuquan/Notion2API.git .
# 编译（需 Go 1.25+）
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o notion2api ./cmd/notion2api
# 验证
./notion2api --version || echo "二进制就绪"
```

> 如果 VPS 内存 < 2GB，编译可能 OOM。改用 Docker 方式（见 3.2.5）或在本地编译后上传二进制。

#### 3.2.2 上传 Notion 账号 probe.json

把本地抓好的 probe.json 目录传到 VPS：
```bash
# 本地执行
scp -r probe_files/notion_accounts/alice@example.com root@<VPS>:/opt/notion2api/probe_files/notion_accounts/
```

#### 3.2.3 生成配置文件

```bash
cat > /opt/notion2api/config/config.json <<'EOF'
{
  "host": "127.0.0.1",
  "port": 8787,
  "api_key": "GENERATE_A_RANDOM_STRING_HERE",
  "upstream_base_url": "https://www.notion.so",
  "upstream_origin": "https://www.notion.so",
  "model_id": "auto",
  "default_model": "auto",
  "timeout_sec": 180,
  "admin": {
    "enabled": false,
    "password": "change-me",
    "static_dir": "static/admin"
  },
  "storage": {
    "sqlite_path": "data/notion2api.sqlite",
    "persist_conversations": true,
    "persist_responses": true,
    "persist_continuation_sessions": true
  },
  "features": {
    "use_web_search": true,
    "force_fresh_thread_per_request": true,
    "ai_surface": "ai_module",
    "thread_type": "workflow"
  },
  "session_refresh": {
    "enabled": true,
    "interval_sec": 900,
    "startup_check": true,
    "auto_switch_account": true
  },
  "accounts": [
    {
      "email": "alice@example.com",
      "probe_json": "probe_files/notion_accounts/alice@example.com/probe.json",
      "profile_dir": "probe_files/notion_accounts/alice@example.com",
      "storage_state_path": "probe_files/notion_accounts/alice@example.com/storage_state.json",
      "priority": 100,
      "disabled": false
    }
  ]
}
EOF
```

**关键配置点**：
- `host: "127.0.0.1"` — **必须**绑回环，不对外
- `api_key` — 生成一个强随机串：`openssl rand -hex 24`，记下来后面要填到 zhongzhuan
- `admin.enabled: false` — 不需要 Notion2API 自己的 admin（用 zhongzhuan 的统一管理）
- `accounts[].email` 和 `probe_json` 路径要和你实际上传的 probe.json 对应

#### 3.2.4 systemd 服务

```bash
sudo tee /etc/systemd/system/notion2api.service <<'EOF'
[Unit]
Description=notion2api bridge service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/notion2api
ExecStart=/opt/notion2api/notion2api --config /opt/notion2api/config/config.json
Restart=always
RestartSec=5
TimeoutStopSec=20
Environment=TZ=Asia/Shanghai
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now notion2api
sleep 2
curl -s http://127.0.0.1:8787/healthz   # 应返回 ok
```

#### 3.2.5 备选：Docker 方式

如果不想装 Go 编译，用 Docker：

```bash
cd /opt/notion2api
# 把 probe_files 挂进容器
docker build -t notion2api:latest .
docker run -d --name notion2api \
  --restart unless-stopped \
  -p 127.0.0.1:8787:8787 \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/probe_files:/app/probe_files \
  notion2api:latest
```

注意 `-p 127.0.0.1:8787:8787` 前面的 `127.0.0.1` 前缀，保证只绑回环。

### 3.3 在 zhongzhuan 后台配置 Notion2API 上游

1. 浏览器开 `http://<VPS>:8089`（admin 已对外的场景）或 SSH 隧道后开 `http://127.0.0.1:8089`
2. 登录 → 切到"模型"页 → 点"+ 添加模型"
3. 按下表填：

| 字段 | 值 | 说明 |
|---|---|---|
| 名称 | `notion-sonnet` | 对外暴露给 Claude Code 的模型名，可自定义 |
| 上游地址 | `http://127.0.0.1:8787/v1` | 本机 Notion2API |
| 上游模型名 | `sonnet-4.6` | Notion2API 的模型 ID（见下表） |
| 上游协议 | `openai` | Notion2API 是 OpenAI 兼容 |
| RPM 限制 | `60` | 看 Notion 账号风控情况调 |
| TPM 限制 | `0` | 0 = 不限 |
| 启用 | 是 | |

4. 保存后切到"Key 池"页 → "+ 添加 Key"
   - 标签：`notion-pool-1`
   - 模型：选刚建的 `notion-sonnet`
   - Key：填 Notion2API config.json 里的 `api_key` 那个随机串
   - 优先级：`100`
   - 启用：是

5. 可选：把多个 Notion 模型都配上（每个模型一条 Model 记录，共用同一个 key）。

下表是 Notion2API 仓库 `main` 分支 `builtinModelDefinitions` 里**内置**的模型映射（截至 2026-07-16）：

| Notion2API 模型 ID | Notion 内部代号 | 家族 | zhongzhuan 里建议的对外名 |
|---|---|---|---|
| `auto` | (自动) | system | `notion-auto` |
| `gpt-5.2` | oatmeal-cookie | openai | `notion-gpt52` |
| `gpt-5.4` | oval-kumquat-medium | openai | `notion-gpt54` |
| `gemini-2.5-flash` | vertex-gemini-2.5-flash | gemini | `notion-gemini-flash` |
| `gemini-3.1-pro` | galette-medium-thinking | gemini | `notion-gemini-pro` |
| `sonnet-4.6` | almond-croissant-low | anthropic | `notion-sonnet` |
| `opus-4.7` | apricot-sorbet-medium | anthropic | `notion-opus` |
| `haiku-4.5` | (见 models.go) | anthropic | `notion-haiku` |

> ⚠️ **Notion 网页端 AI 选择器已更新到更新的模型**（见下表），但 Notion2API 仓库的内置映射表尚未跟进这些新代号。下面列的是 Notion 网页端当前可选的最新模型，**Notion2API 暂未内置对应代号**，需用 3.3.1 节的方法动态获取：

| Notion 网页端模型 | 发布时间 | Notion2API 内置？ | 备注 |
|---|---|---|---|
| Claude Opus 4.8 | 2026-05-28 | ❌ 暂未内置 | Anthropic 旗舰，1M 上下文，$5/$25 |
| GPT-5.6 (Sol/Terra/Luna) | 2026-07-09 GA | ❌ 暂未内置 | 三档：Sol 旗舰 / Terra 中端 / Luna 低成本 |
| Claude Sonnet 4.8 | 跳级发布 | ❌ 暂未内置 | 跳过 4.7 直接发布 |

### 3.3.1 获取最新模型代号（opus-4.8 / gpt-5.6 等）

Notion2API 有 probe 机制会从 Notion 动态拉取当前账号可用的模型列表（`probeModelsEnvelope`），即使内置表没有，也可能通过 probe 发现。获取可用模型的正确做法：

```bash
# 1. 先确保 Notion2API 已启动且账号 probe.json 有效
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer <notion2api-api_key>" | jq '.data[].id'
```

返回的列表里如果出现了 `opus-4.8` / `gpt-5.6-sol` 等新 ID，说明 probe 已动态发现，**直接用这些 ID 填到 zhongzhuan 后台的"上游模型名"字段即可**，无需改 Notion2API 代码。

如果 `/v1/models` 没返回新模型（probe 没发现或 Notion2API 版本旧），有两种方式用上新模型：

**方式 A：用 `auto` 让 Notion2API 自动选（最省事）**
- zhongzhuan 后台该模型的"上游模型名"填 `auto`
- 在 Notion 网页端把默认 AI 模型设成 Opus 4.8 或 GPT-5.6
- Notion2API 调用时会用 Notion 账号当前的默认模型

**方式 B：手动加 model_aliases（精确指定）**
在 Notion2API 的 `config.json` 里加别名映射，把新模型名指向 probe 发现的代号：
```json
{
  "model_aliases": {
    "opus-4.8": "<probe发现的opus4.8代号>",
    "gpt-5.6": "<probe发现的gpt5.6代号>"
  }
}
```
然后 zhongzhuan 后台"上游模型名"填 `opus-4.8` 或 `gpt-5.6`。probe 发现的代号可通过 Notion2API 日志（开 `debug_upstream: true`）或抓包 Notion 网页端请求获得。

**方式 C：等 Notion2API 仓库更新**
关注 [Notion2API 仓库](https://github.com/maxiuquan/Notion2API) 的 `internal/app/models.go`，维护者会逐步补全新模型代号。更新后 `git pull` 重新编译即可。

### 3.4 防火墙确认

```bash
# Notion2API 必须只绑回环，无需防火墙规则
# 确认没有把 8787 暴露到公网
sudo ss -tlnp | grep 8787
# 应看到: LISTEN 127.0.0.1:8787   ← 正确
# 若看到: LISTEN 0.0.0.0:8787     ← 错误，改 config.json 的 host 后重启

# zhongzhuan 端口照旧
sudo ufw status
# 应有: 22, 80(可选), 8443, 8089(可选)
```

---

## 4. 端到端验证

### 4.1 Notion2API 直连测试（在 VPS 上）

```bash
# 列模型
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer <notion2api-api_key>" | jq '.data[].id'

# 发一条对话
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer <notion2api-api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sonnet-4.6",
    "messages": [{"role":"user","content":"说一句你好"}],
    "max_tokens": 50
  }' | jq .
```

如果返回 `choices[0].message.content` 有内容，说明 Notion2API + Notion 账号链路通了。

### 4.2 通过 zhongzhuan 测试（在 VPS 上）

用 OpenAI 协议打 zhongzhuan（走 o2o 透传，验证基础链路）：
```bash
curl -s https://127.0.0.1:8443/v1/chat/completions \
  -k \
  -H "Authorization: Bearer <zhongzhuan-access-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "notion-sonnet",
    "messages": [{"role":"user","content":"说一句你好"}],
    "max_tokens": 50
  }' | jq .
```

用 Anthropic 协议打 zhongzhuan（走 a2o→o2a 全翻译链路，这是 Claude Code 的真实路径）：
```bash
curl -s https://127.0.0.1:8443/v1/messages \
  -k \
  -H "x-api-key: <zhongzhuan-access-token>" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "notion-sonnet",
    "max_tokens": 50,
    "messages": [{"role":"user","content":"说一句你好"}]
  }' | jq .
```

应返回：
```json
{
  "id": "msg_...",
  "type": "message",
  "role": "assistant",
  "model": "notion-sonnet",
  "content": [{"type": "text", "text": "你好！..."}],
  "stop_reason": "end_turn",
  "usage": {"input_tokens": ..., "output_tokens": ...}
}
```

### 4.3 Claude Code 真实联调

在本地配置 Claude Code：
```bash
# 路径 A（Let's Encrypt）
export ANTHROPIC_BASE_URL=https://api.macc.eu.cc:8443
export ANTHROPIC_API_KEY=<zhongzhuan-access-token>

# 路径 B（自签）
export ANTHROPIC_BASE_URL=https://<VPS-IP>:8443
export ANTHROPIC_API_KEY=<zhongzhuan-access-token>
export NODE_EXTRA_CA_CERTS=/本地路径/local-ca.crt

# 指定用 Notion 提供的模型
export ANTHROPIC_MODEL=notion-sonnet
```

跑一次普通对话 + 一次工具调用（比如让 Claude Code 读个文件）。能正常工作即全链路打通。

---

## 5. 运维

### 5.1 日常检查

```bash
# 两个服务状态
sudo systemctl status zhongzhuan notion2api

# 端口监听
sudo ss -tlnp | grep -E '8443|8089|8787'

# 日志
sudo journalctl -u zhongzhuan -f
sudo journalctl -u notion2api -f
```

### 5.2 Notion 账号掉登录

Notion2API 的 session_refresh 会自动刷新登录态（默认 900s 一次），但 Notion 那边主动踢号时仍会失效。症状：
- zhongzhuan 日志报 401/403 from upstream
- Notion2API 日志报 `session expired` / `auth error`

处理：
1. 本地重新跑 `./notion2api --login` 抓新的 probe.json
2. `scp` 上传覆盖 VPS 上的旧文件
3. `sudo systemctl restart notion2api`

### 5.3 多 Notion 账号轮询

Notion2API 原生支持账号池，在 config.json 的 `accounts[]` 里加多个账号即可。zhongzhuan 这边不用改——它看到的始终是同一个 `http://127.0.0.1:8787/v1`，账号调度由 Notion2API 内部处理。

如果想把不同 Notion 账号暴露成不同模型（比如号 A 跑 sonnet、号 B 跑 opus），在 Notion2API 里不好隔离——它是按账号优先级统一调度。建议保持单池，让 Notion2API 自己轮询。

### 5.4 zhongzhuan 后台加新 Notion 模型

不用改任何配置文件，直接在 admin 后台加一条 Model 记录 + 挂同一个 Notion2API key 即可，热生效。

### 5.5 升级 Notion2API

```bash
cd /opt/notion2api
git pull
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o notion2api ./cmd/notion2api
sudo systemctl restart notion2api
```

zhongzhuan 侧无需改动。

### 5.6 升级 zhongzhuan

```bash
cd <zhongzhuan安装目录>
git pull
sudo systemctl restart zhongzhuan
```

Notion2API 侧无需改动。

---

## 6. 故障排查

### 6.1 zhongzhuan 报 503 `no keys configured for model`

zhongzhuan 后台没给对应模型挂 key，或模型名拼写不一致。检查：
- admin → 模型页：确认模型 `name` 字段与请求里的 `model` 完全一致
- admin → Key 池页：确认有 key 关联到该模型，且 key 启用

### 6.2 zhongzhuan 报 503 `upstream unreachable`

Notion2API 没起来或端口不对。检查：
```bash
sudo systemctl status notion2api
curl http://127.0.0.1:8787/healthz
```
确认监听在 127.0.0.1:8787，且 zhongzhuan 后台该模型的 `upstream_base` 填的是 `http://127.0.0.1:8787/v1`（注意末尾有 `/v1`）。

### 6.3 zhongzhuan 报 401/403 from upstream

两种可能：
1. **Notion2API 的 api_key 不对** — zhongzhuan 后台 key 池里填的值与 Notion2API config.json 的 `api_key` 不一致
2. **Notion 账号掉登录** — 直连 Notion2API 测试（4.1 步骤），如果也 401 就是 probe.json 失效，按 5.2 重新抓

### 6.4 Claude Code 报 `content: []` 空响应

这是旧版 zhongzhuan 把上游错误响应硬翻译成空壳的 bug。确认 VPS 上跑的是 `91812f3` 之后的代码：
```bash
cd <zhongzhuan安装目录>
git log --oneline -1
# 应看到 91812f3 或更新的 commit
```
不是的话 `git pull && sudo systemctl restart zhongzhuan`。

### 6.5 流式响应中断或乱码

Notion2API 的 `stream_chunk_runes` 默认 24，会把文本切成小块。zhongzhuan 的 StreamO2A 状态机对极小 chunk 可能有边界问题。如遇乱码，在 Notion2API config.json 里把 `stream_chunk_runes` 调大到 `128` 或 `256` 后重启。

### 6.6 Notion2API 日志报 `rate limited` / `429`

Notion 对单账号有调用频率限制。处理：
1. 加更多 Notion 账号到 `accounts[]`
2. 在 zhongzhuan 后台把该模型的 RPM 限制调低（如 30）
3. Notion2API config.json 里 `dispatch.probe_cache_ttl_seconds` 调大

---

## 7. 安全清单

- [ ] Notion2API `host` 绑 `127.0.0.1`，不对外
- [ ] Notion2API `api_key` 是强随机串（非 `change-me`）
- [ ] zhongzhuan `PROXY_AUTH=true` 已开启
- [ ] zhongzhuan admin 后台已开鉴权，密码已改
- [ ] 防火墙只放行 22 / 8443 / 8089(可选)，8787 不放行
- [ ] `probe.json` 文件权限 `chmod 600`（含 Notion 登录态，等价于账号密码）
- [ ] 自签证书场景下 `NODE_EXTRA_CA_CERTS` 指向绝对路径
- [ ] 定期 `git pull` 更新两个项目

---

## 8. 与单部署的差异总结

| 维度 | 仅 zhongzhuan | zhongzhuan + Notion2API |
|---|---|---|
| VPS 内存 | ~200MB | ~400MB（+Notion2API） |
| 进程 | 1 个 | 2 个（互相独立） |
| 配置 | zhongzhuan admin | zhongzhuan admin + Notion2API config.json |
| 上游 | OpenAI/Anthropic 官方 | + Notion AI（经 Notion2API 桥接） |
| 维护 | 单项目升级 | 两项目独立升级，互不影响 |
| 额外依赖 | 无 | Go 1.25+（编译）或 Docker |
| 前置准备 | API key | Notion 账号 probe.json |

**核心收益**：用 Notion AI 订阅（$10/月）白嫖 GPT-5.x / Claude Sonnet 4.6 / Gemini 3.1 Pro 等模型，经 zhongzhuan 统一暴露成 Anthropic 协议给 Claude Code 用。
