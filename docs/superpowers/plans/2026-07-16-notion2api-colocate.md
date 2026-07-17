# Notion2API + Zhongzhuan 同机部署方案

> 版本 v2.0 · 2026-07-17
> 目标：在同一台 VPS 上同时运行 zhongzhuan（对外 HTTPS 网关）和 Notion2API（Notion AI → OpenAI 兼容桥接），让 Claude Code 通过 zhongzhuan 访问 Notion AI 提供的 GPT-5.x / Claude Sonnet/Opus 4.x / Gemini 等模型，**零代码改动**，纯配置集成。
>
> **v2.0 变更**：纠正 v1.0 中关于 `--login` 子命令的错误描述（仓库无此命令，登录实际走 `/admin` WebUI 的邮箱+OTP 流程）；补全每一步的预期输出与报错兜底；更新内置模型列表为 13 个；修正 accounts 目录命名规则。

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
                                    │              (本机回环)             │   ├─ /v1/* OpenAI 兼容端点
                                    │                                     │   ├─ /admin WebUI（登录账号、管理）
                                    │  admin:8089 (0.0.0.0 或 SSH 隧道)   │   └─ uTLS 伪装 → Notion 网页端
                                    └─────────────────────────────────────┘
```

### 端口规划

| 端口 | 服务 | 监听地址 | 对外暴露 | 说明 |
|---|---|---|---|---|
| 22 | sshd | 0.0.0.0 | ✅ | SSH + 隧道 |
| 8443 | zhongzhuan proxy | 0.0.0.0 | ✅ (TLS) | Claude Code 入口 |
| 8089 | zhongzhuan admin | 0.0.0.0 或 127.0.0.1 | 可选 | zhongzhuan 后台 |
| 8787 | Notion2API | **127.0.0.1** | ❌ | 仅本机，被 zhongzhuan 调用；/admin 也只本机访问 |
| 80 | certbot standalone | 临时 | ✅ | 仅签发证书时占用 |

**关键**：Notion2API 必须绑 `127.0.0.1`，不对外暴露——它没有 zhongzhuan 那套 access token 鉴权层（虽然有 `api_key`，但 `/admin` WebUI 是登录账号的必经入口，暴露等于把 Notion 账号管理面裸奔给公网）。

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

zhongzhuan 侧由 `deploy.sh` 自动安装，无需手动处理。Notion2API 侧有三种方式，**推荐方式 A（预编译二进制，零依赖）**：

| 方式 | 依赖 | 适用场景 | 见 |
|---|---|---|---|
| A 预编译二进制（推荐） | 无 | 只想快速部署，不想装 Go/Docker | [3.2.1 方式 A](#方式-a下载预编译二进制推荐) |
| B 源码编译 | Go 1.25+ | 要改代码 / 想用最新未发布提交 / 二进制跑不了 | [2.2 方式 B](#方式-b装-go-125用于源码编译) + [3.2.1 方式 B](#方式-b源码编译备选) |
| C Docker | Docker | 不想装 Go，想用容器隔离 | [3.2.5 Docker 方式](#325-备选docker-方式) |

> **方式 A 不需要本节装任何东西**，直接跳到 [3.2 部署](#32-部署-notion2api)。只有在选方式 B 时才需要按下面装 Go。

#### 方式 B：装 Go 1.25+（用于源码编译）

```bash
# 1. 下载 Go 1.25（以 1.25.0 为例，去 https://go.dev/dl/ 看最新小版本号）
cd /tmp
wget https://go.dev/dl/go1.25.0.linux-amd64.tar.gz

# 2. 解压到 /usr/local（需要 sudo）
sudo rm -rf /usr/local/go
sudo tar -C /usr/local -xzf go1.25.0.linux-amd64.tar.gz

# 3. 加到 PATH（写入 ~/.bashrc 永久生效）
echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
source ~/.bashrc

# 4. 验证——应输出 go1.25.0 linux/amd64
go version
```

**预期输出**：
```
go version go1.25.0 linux/amd64
```

**报错兜底**：
- `wget: command not found` → `sudo apt install -y wget`（Debian/Ubuntu）或 `sudo dnf install -y wget`（CentOS）
- `go: command not found` → 检查 `source ~/.bashrc` 是否执行，或重新登录 SSH
- 下载慢 → 换国内镜像：`wget https://golang.google.cn/dl/go1.25.0.linux-amd64.tar.gz`
- 内存 < 1GB 编译 OOM → 换方式 A（预编译二进制）或方式 C（Docker）

### 2.3 Notion 账号准备

Notion2API 需要至少一个 Notion 账号的登录态（`probe.json`），**这是前置硬性依赖**，没有它整个链路跑不通。

> ⚠️ **v1.0 文档纠错**：仓库**没有** `--login` 子命令。登录是通过 Notion2API 自带的 `/admin` WebUI 完成——填邮箱 → Notion 发验证码到邮箱 → 回填验证码 → 服务端自动生成 `probe.json`。完整步骤见 [3.2.2](#322-启动服务并登录-notion-账号webui-邮箱otp-流程)。

**你需要提前准备**：
1. 一个可用的 Notion 账号（免费版即可，但能用的模型受账号套餐限制）
2. 该账号绑定的邮箱能正常收 Notion 发送的 OTP 验证码邮件
3. 邮箱最好支持 IMAP/网页收件，方便快速取码（验证码有效期短）

---

## 3. 部署步骤

### 3.1 部署 zhongzhuan（已完成可跳过）

如果还没部署 zhongzhuan，先跑：
```bash
sudo ./deploy.sh --cert-path letsencrypt --domain api.macc.eu.cc
# 或仅有 IP：
sudo ./deploy.sh --cert-path selfsign --ip <VPS公网IP> --san-dns api.macc.eu.cc
```

**验证**（应返回 `ok`）：
```bash
curl -k https://127.0.0.1:8443/healthz
# 输出: ok
```

如果失败，先解决 zhongzhuan 再继续——后续步骤依赖它。

### 3.2 部署 Notion2API

#### 3.2.1 获取 Notion2API 二进制

Notion2API 的二进制获取有三种方式，**推荐方式 A（预编译二进制，零依赖）**：

##### 方式 A：下载预编译二进制（推荐）

本项目 `mod/` 目录已提供预编译的 linux/amd64 二进制（静态链接、stripped，约 18MB，基于 Notion2API 仓库 main 分支 `d767484` 构建）。直接下载即可，**无需装 Go**。

```bash
# 1. 建目录
sudo mkdir -p /opt/notion2api
sudo chown -R $USER:$USER /opt/notion2api
cd /opt/notion2api

# 2. 下载预编译二进制（把 <branch> 换成实际分支名，如 main 或 trae/agent-7ODVJo）
#    也可用 jsDelivr CDN 加速: https://cdn.jsdelivr.net/gh/maxiuquan/zhongzhuan@<branch>/mod/notion2api-linux-amd64
wget -O notion2api https://raw.githubusercontent.com/maxiuquan/zhongzhuan/<branch>/mod/notion2api-linux-amd64

# 3. 赋可执行权限
chmod +x notion2api

# 4. 校验完整性（应输出: cb626a6608b99891b2a4fdaa2775fbfd021eaf8788e6ee39889f49baa722f064  notion2api）
sha256sum notion2api

# 5. 验证二进制能跑（会打印帮助/参数列表，按 q 退出）
./notion2api --help 2>&1 | head -20 || true
```

**预期输出**（校验 + 帮助）：
```
cb626a6608b99891b2a4fdaa2775fbfd021eaf8788e6ee39889f49baa722f064  notion2api
Usage of /opt/notion2api/notion2api:
  -api-key string
        ...
  -config string
        ...
```

> ⚠️ **v1.0 文档纠错**：仓库**没有** `--version` 子命令。验证二进制靠 `--help` 看参数列表，或直接启动看日志（下一步）。

**报错兜底**：
- `wget: command not found` → `sudo apt install -y wget`
- 下载慢/超时 → 换 jsDelivr CDN（见上面注释）或 `git clone` 本项目后从 `mod/` 目录取
- `sha256sum` 不匹配 → 下载不完整或被篡改，重新下载；仍不对就换方式 B 自己编译
- `./notion2api: cannot execute binary file: Exec format error` → 你的 VPS 不是 x86-64（如 ARM），预编译二进制不适用，换方式 B 自己编译
- `./notion2api: /lib64/ld-linux-x86-64.so.2: not found` → 不会出现，二进制是静态链接（CGO_ENABLED=0）；如真出现说明下载错了，重下

##### 方式 B：源码编译（备选）

适合要改 Notion2API 代码、或预编译二进制跑不了（非 x86-64 架构）、或想用最新未发布提交的场景。需先按 [2.2 方式 B](#方式-b装-go-125用于源码编译) 装 Go 1.25+。

```bash
# 1. 建目录并克隆 Notion2API 源码
sudo mkdir -p /opt/notion2api
sudo chown -R $USER:$USER /opt/notion2api
cd /opt/notion2api
git clone https://github.com/maxiuquan/Notion2API.git .

# 2. 编译（需 Go 1.25+，见 2.2 方式 B）
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o notion2api ./cmd/notion2api

# 3. 赋可执行权限 + 验证
chmod +x notion2api
./notion2api --help 2>&1 | head -5
```

**预期输出**（编译成功 + 二进制大小约 15-25 MB）：
```
Usage of /opt/notion2api/notion2api:
  -api-key string
```

**报错兜底**：
- `go: command not found` → 回 [2.2 方式 B](#方式-b装-go-125用于源码编译) 装 Go
- `go: errors during go build` / `module requires Go 1.25` → Go 版本太低，重装 1.25+
- `fatal: destination path '.' already exists and is not an empty directory` → 目录非空，换空目录或 `rm -rf /opt/notion2api/*` 重来
- 编译卡住/OOM → 内存不足，改用方式 A 或 [3.2.5 Docker 方式](#325-备选docker-方式)

#### 3.2.2 启动服务并登录 Notion 账号（WebUI 邮箱+OTP 流程）

这一步是**整个部署最关键也最容易卡住**的环节，按顺序操作。

**第 1 步：准备最小配置文件先启动服务**

```bash
cd /opt/notion2api
mkdir -p config data probe_files/notion_accounts

# 生成一个强随机的 api_key（用于 zhongzhuan 调用 Notion2API 时鉴权）
NOTION2API_KEY=$(openssl rand -hex 24)
echo "请记下这个 api_key，后面要填到 zhongzhuan: $NOTION2API_KEY"

# 生成一个强随机的 admin 密码（用于登录 /admin WebUI）
ADMIN_PASS=$(openssl rand -hex 16)
echo "请记下 admin 密码: $ADMIN_PASS"

# 写最小配置（账号先留空，登录后由 WebUI 管理）
cat > config/config.json <<EOF
{
  "probe_json": "",
  "host": "127.0.0.1",
  "port": 8787,
  "api_key": "$NOTION2API_KEY",
  "upstream_base_url": "https://www.notion.so",
  "upstream_origin": "https://www.notion.so",
  "model_id": "auto",
  "default_model": "auto",
  "timeout_sec": 180,
  "poll_interval_sec": 1.5,
  "poll_max_rounds": 40,
  "debug_upstream": false,
  "stream_chunk_runes": 24,
  "admin": {
    "enabled": true,
    "password": "$ADMIN_PASS",
    "token_ttl_hours": 24,
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
  "login_helper": {
    "sessions_dir": "probe_files/notion_accounts",
    "timeout_sec": 120
  },
  "session_refresh": {
    "enabled": true,
    "interval_sec": 900,
    "startup_check": true,
    "auto_switch_account": true
  },
  "dispatch": { "probe_cache_ttl_seconds": 45 },
  "accounts": []
}
EOF
```

**第 2 步：前台启动服务（先看日志，确认能跑起来）**

```bash
cd /opt/notion2api
./notion2api --config ./config/config.json
```

**预期输出**（启动成功，看到 listening 字样）：
```
[notion2api-go] listening on http://127.0.0.1:8787 default_model=auto
```

**报错兜底**：
- `bind: address already in use` → 8787 被占用，`sudo ss -tlnp | grep 8787` 查进程，kill 掉或改 config.json 的 port
- `panic: ...` → 配置文件 JSON 语法错，`cat config/config.json | python3 -m json.tool` 验证
- 启动后立刻退出 → 看完整报错栈，常见是 `static_dir` 路径不存在（确保在 /opt/notion2api 下执行，static/admin 在仓库里已有）

**第 3 步：SSH 隧道访问 /admin WebUI（服务保持前台运行，另开一个 SSH 窗口）**

因为 Notion2API 绑了 127.0.0.1，外部访问不了，需要 SSH 隧道：

```bash
# 在你的本地电脑执行（不是 VPS）
ssh -L 8787:127.0.0.1:8787 root@<VPS-IP>
# 保持这个 SSH 连接不断
```

然后本地浏览器开：`http://127.0.0.1:8787/admin`

**第 4 步：在 WebUI 登录管理台 + 添加 Notion 账号**

1. 浏览器开 `http://127.0.0.1:8787/admin`
2. 用刚才生成的 `$ADMIN_PASS` 登录管理台
3. 找到"账号管理"或"Accounts"面板，点"添加账号"
4. 填入 Notion 账号邮箱（如 `alice@example.com`），点"发送验证码"
5. 去该邮箱收 Notion 发来的 OTP 验证码邮件（6 位数字）
6. 回到 WebUI 填入验证码，点"验证"
7. 验证成功后，Notion2API 服务端自动在 `probe_files/notion_accounts/<email_slug>/` 下生成：
   - `probe.json`（最终登录态，供推理用）
   - `storage_state.json`（cookie 持久化）
   - `pending_login.json`（登录中间态，登录完成后保留作状态记录）

> **email_slug 规则**：邮箱小写 + 非字母数字字符替换为 `_`。例如 `alice@example.com` → `alice_example_com`。所以 probe.json 路径是 `probe_files/notion_accounts/alice_example_com/probe.json`。

**第 5 步：验证登录成功**

```bash
# 在 VPS 上（另开窗口），用 api_key 调 /v1/models
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer $NOTION2API_KEY" | python3 -m json.tool
```

**预期输出**（能看到模型列表，含 auto/sonnet-4.6/gpt-5.4 等）：
```json
{
  "object": "list",
  "data": [
    {"id": "auto", "object": "model", ...},
    {"id": "sonnet-4.6", "object": "model", ...},
    {"id": "gpt-5.4", "object": "model", ...},
    ...
  ]
}
```

也可以查健康检查（无需鉴权）：
```bash
curl -s http://127.0.0.1:8787/healthz | python3 -m json.tool
# 应看到 user_email 字段已是你登录的邮箱，说明登录态已就绪
```

**报错兜底**：
- WebUI 打不开 → SSH 隧道没建好，检查本地 `ssh -L 8787:...` 是否还连着
- 验证码收不到 → 检查邮箱垃圾箱；Notion 对同一邮箱频繁发码会限流，等 5 分钟再试
- 验证码报 `invalid_code` → 验证码已过期或填错，重新点"发送验证码"
- 验证码报 `rate_limited` → Notion 限流，等 5-15 分钟
- /v1/models 返回 401 → api_key 填错，用 config.json 里的值
- /v1/models 返回空 `data: []` → 登录态没建立成功，回 WebUI 看账号状态是不是 `ready`

**第 6 步：停掉前台服务，准备用 systemd 托管**

在运行 `./notion2api` 的窗口按 `Ctrl+C` 停止。登录态已落盘到 probe_files，重启不会丢。继续 3.2.4 配 systemd。

#### 3.2.3 配置文件字段说明（可选阅读）

上一节的最小配置已够用。如果你要调优，参考下表（基于仓库 `config.example.json`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `host` | string | 监听地址，**必须 `127.0.0.1`** |
| `port` | int | 端口，默认 8787 |
| `api_key` | string | 调 `/v1/*` 的 Bearer token，填到 zhongzhuan |
| `upstream_base_url` | string | Notion 上游，固定 `https://www.notion.so` |
| `upstream_origin` | string | Origin/Referer 头，固定 `https://www.notion.so` |
| `default_model` | string | 默认模型 ID，`auto` 让 Notion 用账号当前默认模型 |
| `timeout_sec` | int | 单次请求超时，默认 180 |
| `poll_interval_sec` | float | 轮询 Notion 响应间隔，默认 1.5 |
| `poll_max_rounds` | int | 最大轮询轮数，默认 40（×1.5s ≈ 60s 上限） |
| `stream_chunk_runes` | int | 流式分块字符数，默认 24；**乱码时调大到 128 或 256** |
| `debug_upstream` | bool | 打印上游请求/响应明细，排查问题时开 |
| `admin.enabled` | bool | 是否开 /admin WebUI，**建议 true**（管理账号需要） |
| `admin.password` | string | /admin 登录密码，**必须改强密码** |
| `storage.sqlite_path` | string | SQLite 路径，默认 `data/notion2api.sqlite` |
| `features.use_web_search` | bool | 是否启用 Notion 的联网搜索 |
| `features.force_fresh_thread_per_request` | bool | 每次请求开新会话（建议 true，避免串话） |
| `login_helper.sessions_dir` | string | 登录态文件目录，默认 `probe_files/notion_accounts` |
| `session_refresh.interval_sec` | int | 登录态自动刷新间隔，默认 900（15 分钟） |
| `session_refresh.auto_switch_account` | bool | 账号失败自动切下一个，建议 true |
| `dispatch.probe_cache_ttl_seconds` | int | probe 缓存 TTL，默认 45 |
| `accounts` | array | 账号列表；**WebUI 加账号后会自动管理，通常留空 `[]` 即可**；如需手动声明，每项含 `email`/`probe_json`/`profile_dir`/`storage_state_path`/`priority`/`disabled` |
| `model_aliases` | object | 模型别名映射，如 `{"opus-4.8": "<代号>"}`，用于 3.3.1 节 |

> **关于 accounts 数组**：推荐用 WebUI 管理账号（accounts 留空）。如要手动声明，参考仓库 `config.example.json` 的 accounts 字段格式，注意 `profile_dir` 用 email_slug 规则（`alice@example.com` → `alice_example_com`）。

#### 3.2.4 systemd 服务（推荐生产用）

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

# 验证服务状态——应看到 active (running)
sudo systemctl status notion2api --no-pager | head -15

# 验证健康检查——应输出含 "ok":true 的 JSON
curl -s http://127.0.0.1:8787/healthz | python3 -m json.tool
```

**预期输出**（systemctl status 关键行）：
```
● notion2api.service - notion2api bridge service
     Loaded: loaded (/etc/systemd/system/notion2api.service; enabled; preset: enabled)
     Active: active (running) since ...
```

**报错兜底**：
- `Active: failed` → `sudo journalctl -u notion2api -n 50 --no-pager` 看日志，常见是 config.json 路径错或权限不足
- 反复重启 → `Restart=always` 会一直拉起，看日志找根因
- 改了 config.json 不生效 → `sudo systemctl restart notion2api`

#### 3.2.5 备选：Docker 方式

如果不想装 Go，用 Docker：

```bash
cd /opt/notion2api
# 用仓库自带的 docker-compose（默认配置 config.docker.json）
# 先改 config.docker.json 里的 api_key 和 admin.password
# 用 sed 一键替换（把 YOUR_API_KEY 和 YOUR_ADMIN_PASS 换成你生成的随机串）
sed -i 's/"api_key": "change-me-openai-key"/"api_key": "YOUR_API_KEY"/' config.docker.json
sed -i 's/"password": "change-me-admin-password"/"password": "YOUR_ADMIN_PASS"/' config.docker.json

# 启动（默认端口映射 8787:8787，需改成只绑回环）
# 编辑 docker-compose.yml，把 ports 改成 "127.0.0.1:8787:8787"
docker compose up -d --build

# 验证
docker compose ps
# 应看到 notion2api 状态 running
curl -s http://127.0.0.1:8787/healthz | python3 -m json.tool
```

**关键**：务必把 `docker-compose.yml` 的端口映射从 `8787:8787` 改成 `127.0.0.1:8787:8787`，否则 Notion2API 会暴露到公网。

Docker 方式的登录流程同 [3.2.2](#322-启动服务并登录-notion-账号webui-邮箱otp-流程)：SSH 隧道 `-L 8787:127.0.0.1:8787` → 浏览器开 `/admin` → 邮箱+OTP 登录。账号文件会落到容器内 `/app/data/notion_accounts/`（对应宿主 `./data/notion_accounts/`）。

### 3.3 在 zhongzhuan 后台配置 Notion2API 上游

1. 浏览器开 `http://<VPS>:8089`（admin 已对外的场景）或 SSH 隧道后开 `http://127.0.0.1:8089`
2. 登录 → 切到"模型"页 → 点"+ 添加模型"
3. 按下表填：

| 字段 | 值 | 说明 |
|---|---|---|
| 名称 | `notion-sonnet` | 对外暴露给 Claude Code 的模型名，可自定义 |
| 上游地址 | `http://127.0.0.1:8787/v1` | 本机 Notion2API，**末尾 `/v1` 不能少** |
| 上游模型名 | `sonnet-4.6` | Notion2API 的模型 ID（见下表） |
| 上游协议 | `openai` | Notion2API 是 OpenAI 兼容 |
| 上游完整地址 | 留空 | Notion2API 走标准 `/v1/chat/completions`，不需要覆盖 |
| RPM 限制 | `60` | 看 Notion 账号风控情况调 |
| TPM 限制 | `0` | 0 = 不限 |
| 启用 | 是 | |

4. 保存后切到"Key 池"页 → "+ 添加 Key"
   - 标签：`notion-pool-1`
   - 模型：选刚建的 `notion-sonnet`
   - Key：填 [3.2.2](#322-启动服务并登录-notion-账号webui-邮箱otp-流程) 生成的 `$NOTION2API_KEY`
   - 优先级：`100`
   - 启用：是

5. 可选：把多个 Notion 模型都配上（每个模型一条 Model 记录，共用同一个 key）。

下表是 Notion2API 仓库 `main` 分支 `internal/app/models.go` 的 `builtinModelDefinitions()` 里**内置**的 13 个模型（截至 2026-07-17）：

| Notion2API 模型 ID | Notion 内部代号 | 家族 | zhongzhuan 里建议的对外名 |
|---|---|---|---|
| `auto` | (自动) | system | `notion-auto` |
| `gpt-5.2` | oatmeal-cookie | openai | `notion-gpt52` |
| `gpt-5.4` | oval-kumquat-medium | openai | `notion-gpt54` |
| `gpt-5.4-mini` | oregon-grape-medium | openai | `notion-gpt54-mini` |
| `gpt-5.4-nano` | otaheite-apple-medium | openai | `notion-gpt54-nano` |
| `gemini-2.5-flash` | vertex-gemini-2.5-flash | gemini | `notion-gemini-flash` |
| `gemini-3.1-pro` | galette-medium-thinking | gemini | `notion-gemini-pro` |
| `gemini-3-flash` | gingerbread | gemini | `notion-gemini-3-flash` |
| `sonnet-4.6` | almond-croissant-low | anthropic | `notion-sonnet` |
| `opus-4.7` | apricot-sorbet-medium | anthropic | `notion-opus-47` |
| `opus-4.6` | avocado-froyo-medium | anthropic | `notion-opus-46` |
| `haiku-4.5` | anthropic-haiku-4.5 | anthropic | `notion-haiku` |
| `minimax-m2.5` | fireworks-minimax-m2.5 | mystery | `notion-minimax` |

> ⚠️ **Notion 网页端 AI 选择器已更新到更新的模型**（见下表），但 Notion2API 仓库的内置映射表尚未跟进这些新代号。下面列的是 Notion 网页端当前可选的最新模型，**Notion2API 暂未内置对应代号**，需用 3.3.1 节的方法动态获取：

| Notion 网页端模型 | 发布时间 | Notion2API 内置？ | 备注 |
|---|---|---|---|
| Claude Opus 4.8 | 2026-05-28 | ❌ 暂未内置 | Anthropic 旗舰，1M 上下文，$5/$25 |
| GPT-5.6 (Sol/Terra/Luna) | 2026-07-09 GA | ❌ 暂未内置 | 三档：Sol 旗舰 / Terra 中端 / Luna 低成本 |
| Claude Sonnet 4.8 | 跳级发布 | ❌ 暂未内置 | 跳过 4.7 直接发布 |

#### 3.3.1 获取最新模型代号（opus-4.8 / gpt-5.6 等）

Notion2API 有 probe 机制会从 Notion 动态拉取当前账号可用的模型列表（合并到 `/v1/models` 返回），即使内置表没有，也可能通过 probe 发现。获取可用模型的正确做法：

```bash
# 1. 先确保 Notion2API 已启动且账号 probe.json 有效（见 3.2.2 第 5 步）
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer <notion2api-api_key>" | python3 -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

**预期输出**（每行一个模型 ID）：
```
auto
gpt-5.2
gpt-5.4
sonnet-4.6
...
```

如果返回的列表里出现了 `opus-4.8` / `gpt-5.6-sol` 等新 ID，说明 probe 已动态发现，**直接用这些 ID 填到 zhongzhuan 后台的"上游模型名"字段即可**，无需改 Notion2API 代码。

如果 `/v1/models` 没返回新模型（probe 没发现或 Notion2API 版本旧），有三种方式用上新模型：

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
然后 zhongzhuan 后台"上游模型名"填 `opus-4.8` 或 `gpt-5.6`。probe 发现的代号可通过两种方式获得：
- 开 `debug_upstream: true` 后重启 Notion2API，发一次请求，在 `journalctl -u notion2api -f` 日志里找 Notion 返回的模型代号
- 浏览器开 Notion 网页端，F12 抓 AI 请求的 payload，里面带模型代号

**方式 C：等 Notion2API 仓库更新**
关注 [Notion2API 仓库](https://github.com/maxiuquan/Notion2API) 的 `internal/app/models.go`，维护者会逐步补全新模型代号。更新后 `git pull` 重新编译即可（见 5.5）。

### 3.4 防火墙确认

```bash
# Notion2API 必须只绑回环，无需防火墙规则
# 确认没有把 8787 暴露到公网
sudo ss -tlnp | grep 8787
# 应看到: LISTEN 127.0.0.1:8787   ← 正确
# 若看到: LISTEN 0.0.0.0:8787     ← 错误，改 config.json 的 host 为 127.0.0.1 后重启

# zhongzhuan 端口照旧
sudo ufw status
# 应有: 22, 80(可选), 8443, 8089(可选)
```

---

## 4. 端到端验证

### 4.1 Notion2API 直连测试（在 VPS 上）

这一步验证 Notion2API 本身能工作，不经过 zhongzhuan。

```bash
# 1. 列模型——应看到 3.3 节的模型列表
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer <notion2api-api_key>" | python3 -m json.tool

# 2. 发一条对话
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer <notion2api-api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sonnet-4.6",
    "messages": [{"role":"user","content":"说一句你好"}],
    "max_tokens": 50
  }' | python3 -m json.tool
```

**预期输出**（对话成功）：
```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": ...,
  "model": "sonnet-4.6",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "你好！很高兴..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {...}
}
```

**失败排查**：
- `{"error":{"message":"unauthorized"}}` → api_key 填错
- `{"error":{"message":"no available account"}}` → 账号没登录或全 disabled，回 3.2.2 重新登录
- `{"error":{"message":"rate limited"}}` → Notion 限流，等几分钟或加账号
- 返回 `content` 为空 → 账号可能掉登录，看 `journalctl -u notion2api -n 50` 是否有 `session expired`
- 超时（30s+ 无响应）→ Notion 上游慢，调大 config.json 的 `timeout_sec` 到 300

如果 `/v1/models` 能列但 `/v1/chat/completions` 失败，通常是账号掉登录或 Notion 风控。

### 4.2 通过 zhongzhuan 测试（在 VPS 上）

先确认 zhongzhuan 已配好 3.3 节的模型和 key。

**用 OpenAI 协议打 zhongzhuan（走 o2o 透传，验证基础链路）**：
```bash
curl -s https://127.0.0.1:8443/v1/chat/completions \
  -k \
  -H "Authorization: Bearer <zhongzhuan-access-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "notion-sonnet",
    "messages": [{"role":"user","content":"说一句你好"}],
    "max_tokens": 50
  }' | python3 -m json.tool
```

**用 Anthropic 协议打 zhongzhuan（走 a2o→o2a 全翻译链路，这是 Claude Code 的真实路径）**：
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
  }' | python3 -m json.tool
```

**预期输出**（Anthropic 协议成功）：
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

**失败排查**：
- `{"error":{"message":"no keys configured for model 'notion-sonnet'"}}` → zhongzhuan 后台没给该模型挂 key，或模型名拼写不一致
- `{"error":{"message":"upstream unreachable: ..."}}` → Notion2API 没起来，`systemctl status notion2api` 检查
- 返回 401/403 → zhongzhuan 后台 key 池里填的 Notion2API api_key 不对
- `content: []` 空响应 → zhongzhuan 版本旧（403 错误处理 bug），见 6.4
- 超时 → Notion2API 上游慢，调大 zhongzhuan 的 proxy_timeout

**看 zhongzhuan 实时日志辅助排查**（另开窗口）：
```bash
sudo tail -f /var/log/zhongzhuan.log
# 或
sudo journalctl -u zhongzhuan -f
```
关键日志行：
- `processing POST /v1/messages model=notion-sonnet` — 收到请求
- `translated anthropic->openai path=/v1/chat/completions` — 翻译成功
- `success status=200` — 上游返回成功
- `using upstream_path_override=...` — 如果配了完整地址覆盖会看到这行

### 4.3 Claude Code 真实联调

在本地配置 Claude Code：

```bash
# 路径 A（Let's Encrypt 证书）
export ANTHROPIC_BASE_URL=https://api.macc.eu.cc:8443
export ANTHROPIC_API_KEY=<zhongzhuan-access-token>

# 路径 B（自签证书）
export ANTHROPIC_BASE_URL=https://<VPS-IP>:8443
export ANTHROPIC_API_KEY=<zhongzhuan-access-token>
export NODE_EXTRA_CA_CERTS=/本地路径/local-ca.crt

# 指定用 Notion 提供的模型
export ANTHROPIC_MODEL=notion-sonnet
```

> `NODE_EXTRA_CA_CERTS` 用绝对路径，自签场景必须有这一步否则 Claude Code 不信任证书。

跑一次普通对话 + 一次工具调用（比如让 Claude Code 读个文件）。能正常工作即全链路打通。

**Claude Code 报错对照**：
- `fetch failed` / `ECONNREFUSED` → VPS 端口没开或服务没起
- `certificate has expired` → 证书过期，certbot 续期没生效，`sudo certbot renew` 手动跑一次
- `self-signed certificate` → 自签场景没设 `NODE_EXTRA_CA_CERTS`
- `401 Unauthorized` → `ANTHROPIC_API_KEY` 填的不是 zhongzhuan 的 access token
- 一直转圈无响应 → 看 zhongzhuan 日志是不是卡在 upstream，按 4.2 排查

---

## 5. 运维

### 5.1 日常检查

```bash
# 两个服务状态
sudo systemctl status zhongzhuan notion2api --no-pager | head -30

# 端口监听（确认 8787 只在 127.0.0.1，8443/8089 在 0.0.0.0）
sudo ss -tlnp | grep -E '8443|8089|8787'

# 实时日志（两个窗口分别开）
sudo tail -f /var/log/zhongzhuan.log          # zhongzhuan 日志文件
sudo journalctl -u notion2api -f                # Notion2API journal
```

**日志关键字速查**：

| 关键字 | 出处 | 含义 |
|---|---|---|
| `listening on http://127.0.0.1:8787` | Notion2API | 启动成功 |
| `session expired` / `auth error` | Notion2API | 账号掉登录，见 5.2 |
| `rate limited` / `429` | Notion2API | Notion 限流，见 6.6 |
| `processing POST /v1/messages` | zhongzhuan | 收到请求 |
| `translated anthropic->openai` | zhongzhuan | 协议翻译成功 |
| `success status=200` | zhongzhuan | 上游成功 |
| `upstream unreachable` | zhongzhuan | Notion2API 没起或端口错 |
| `no keys configured for model` | zhongzhuan | 后台没挂 key |

### 5.2 Notion 账号掉登录

Notion2API 的 session_refresh 会自动刷新登录态（默认 900s 一次），但 Notion 那边主动踢号时仍会失效。症状：
- zhongzhuan 日志报 401/403 from upstream
- Notion2API 日志报 `session expired` / `auth error`

处理流程：
1. 确认是不是真的掉登录——直连 Notion2API 测试（4.1 步骤），如果也 401 就是 probe.json 失效
2. SSH 隧道开 `/admin` WebUI（同 3.2.2 第 3 步）
3. 在 WebUI 找到该账号，点"重新登录"或删掉重新加
4. 走邮箱+OTP 流程重新登录，生成新 probe.json
5. 退出 WebUI，`sudo systemctl restart notion2api`（一般不需要，WebUI 登录会热生效，但重启更稳）

> **v1.0 文档纠错**：v1.0 说"本地跑 `./notion2api --login` 抓新的 probe.json"——**这个命令不存在**。重新登录的唯一方式是 WebUI 邮箱+OTP 流程。

### 5.3 多 Notion 账号轮询

Notion2API 原生支持账号池。加账号方式：SSH 隧道开 `/admin` → 账号管理 → 添加账号 → 每个账号走一次邮箱+OTP 登录。Notion2API 会按 `priority` 调度，失败自动切换（`auto_switch_account: true`）。

zhongzhuan 这边不用改——它看到的始终是同一个 `http://127.0.0.1:8787/v1`，账号调度由 Notion2API 内部处理。

如果想把不同 Notion 账号暴露成不同模型（比如号 A 跑 sonnet、号 B 跑 opus），在 Notion2API 里不好隔离——它是按账号优先级统一调度。建议保持单池，让 Notion2API 自己轮询。

### 5.4 zhongzhuan 后台加新 Notion 模型

不用改任何配置文件，直接在 admin 后台加一条 Model 记录 + 挂同一个 Notion2API key 即可，热生效（zhongzhuan 有 reload 机制）。

### 5.5 升级 Notion2API

```bash
cd /opt/notion2api
git pull
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o notion2api ./cmd/notion2api
sudo systemctl restart notion2api
sleep 2
curl -s http://127.0.0.1:8787/healthz | python3 -m json.tool
```

zhongzhuan 侧无需改动。

**报错兜底**：
- `git pull` 冲突 → 你改过仓库文件，`git stash` 再 pull
- 编译失败 → Go 版本不够或代码不兼容，看报错；可 `git log --oneline -5` 看最新提交是否稳定
- 重启后账号丢失 → 不会丢，probe_files 在仓库外，git pull 不影响

### 5.6 升级 zhongzhuan

```bash
cd <zhongzhuan安装目录>
git pull
sudo systemctl restart zhongzhuan
```

Notion2API 侧无需改动。

### 5.7 证书自动续期（Let's Encrypt 场景）

zhongzhuan 的 deploy.sh 已配 systemd timer 自动续期 + deploy-hook 自动重启 zhongzhuan。验证：

```bash
# 看 timer 状态
sudo systemctl list-timers certbot-renew-zhongzhuan.timer

# 手动测一次续期（不真签，dry-run）
sudo certbot renew --dry-run

# 看 deploy-hook
cat /etc/letsencrypt/renewal-hooks/deploy/restart-zhongzhuan.sh
```

---

## 6. 故障排查

### 6.1 zhongzhuan 报 503 `no keys configured for model`

zhongzhuan 后台没给对应模型挂 key，或模型名拼写不一致。检查：
- admin → 模型页：确认模型 `name` 字段与请求里的 `model` 完全一致（大小写敏感）
- admin → Key 池页：确认有 key 关联到该模型，且 key 启用
- 改完后台后等几秒让 zhongzhuan reload，或 `sudo systemctl restart zhongzhuan`

### 6.2 zhongzhuan 报 503 `upstream unreachable`

Notion2API 没起来或端口不对。检查：
```bash
sudo systemctl status notion2api --no-pager
curl -s http://127.0.0.1:8787/healthz
```
- 服务挂了 → `sudo journalctl -u notion2api -n 50` 看报错，修后 `sudo systemctl restart notion2api`
- 健康检查失败但服务在跑 → 配置文件错或账号全掉登录
- 确认 zhongzhuan 后台该模型的 `upstream_base` 填的是 `http://127.0.0.1:8787/v1`（**末尾 `/v1` 不能少**）

### 6.3 zhongzhuan 报 401/403 from upstream

两种可能：
1. **Notion2API 的 api_key 不对** — zhongzhuan 后台 key 池里填的值与 Notion2API config.json 的 `api_key` 不一致。去 VPS `cat /opt/notion2api/config/config.json | grep api_key` 拿真实值，填回 zhongzhuan 后台
2. **Notion 账号掉登录** — 直连 Notion2API 测试（4.1 步骤），如果也 401 就是 probe.json 失效，按 5.2 重新登录

### 6.4 Claude Code 报 `content: []` 空响应

这是旧版 zhongzhuan 把上游错误响应硬翻译成空壳的 bug（403 被当成"成功"翻译成空 Anthropic 响应）。确认 VPS 上跑的是修复后的代码：
```bash
cd <zhongzhuan安装目录>
git log --oneline -1
# 应看到 eedcafb 或更新的 commit（403 错误处理修复）
```
不是的话 `git pull && sudo systemctl restart zhongzhuan`。

如果已是新版还出 `content: []`，看 zhongzhuan 日志里 `final error: status=... msg=...` 那行，里面有真实的上游错误。

### 6.5 流式响应中断或乱码

Notion2API 的 `stream_chunk_runes` 默认 24，会把文本切成小块。zhongzhuan 的 StreamO2A 状态机对极小 chunk 可能有边界问题。

处理：
```bash
# 改 Notion2API config.json
sudo sed -i 's/"stream_chunk_runes": 24/"stream_chunk_runes": 128/' /opt/notion2api/config/config.json
sudo systemctl restart notion2api
```

如果还乱码，调到 256。注意调大会增加首字延迟。

### 6.6 Notion2API 日志报 `rate limited` / `429`

Notion 对单账号有调用频率限制。处理：
1. 加更多 Notion 账号到账号池（5.3）
2. 在 zhongzhuan 后台把该模型的 RPM 限制调低（如 30）
3. Notion2API config.json 里 `dispatch.probe_cache_ttl_seconds` 调大（如 300），减少 probe 调用

### 6.7 Notion2API 启动报 `bind: address already in use`

8787 被占用：
```bash
sudo ss -tlnp | grep 8787
# 看是哪个进程
sudo kill <PID>
# 或改 config.json 的 port 到别的（如 8788），同步改 zhongzhuan 后台的 upstream_base
```

### 6.8 WebUI 登录 Notion 账号时验证码收不到

- 检查邮箱垃圾箱
- Notion 对同一邮箱频繁发码会限流，等 5-15 分钟再试
- 确认邮箱是 Notion 账号绑定的邮箱
- 换个 Notion 账号试（确认不是 Notion 服务端问题）

### 6.9 zhongzhuan 后台改了模型但不生效

zhongzhuan 有 reload 机制但偶有延迟。强制生效：
```bash
sudo systemctl restart zhongzhuan
```

### 6.10 升级后模型列表变了

Notion2API 升级后内置模型可能增减。重新拉模型列表确认：
```bash
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer <notion2api-api_key>" | python3 -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```
如果之前用的模型 ID 没了，改 zhongzhuan 后台的"上游模型名"为新 ID，或用 `auto`。

---

## 7. 安全清单

- [ ] Notion2API `host` 绑 `127.0.0.1`，不对外（`sudo ss -tlnp | grep 8787` 确认）
- [ ] Notion2API `api_key` 是强随机串（非 `change-me-openai-key`）
- [ ] Notion2API `admin.password` 已改成强随机串（非 `change-me-admin-password`）
- [ ] Docker 场景端口映射是 `127.0.0.1:8787:8787` 不是 `8787:8787`
- [ ] zhongzhuan `PROXY_AUTH=true` 已开启
- [ ] zhongzhuan admin 后台已开鉴权，密码已改
- [ ] 防火墙只放行 22 / 8443 / 8089(可选)，8787 不放行
- [ ] `probe_files/` 目录权限收紧（含 Notion 登录态，等价于账号密码）：`chmod -R 600 /opt/notion2api/probe_files`
- [ ] 自签证书场景下 `NODE_EXTRA_CA_CERTS` 指向绝对路径
- [ ] 定期 `git pull` 更新两个项目
- [ ] Let's Encrypt 证书自动续期 timer 正常（`sudo systemctl list-timers certbot-renew-zhongzhuan.timer`）

---

## 8. 与单部署的差异总结

| 维度 | 仅 zhongzhuan | zhongzhuan + Notion2API |
|---|---|---|
| VPS 内存 | ~200MB | ~400MB（+Notion2API） |
| 进程 | 1 个 | 2 个（互相独立） |
| 配置 | zhongzhuan admin | zhongzhuan admin + Notion2API config.json + /admin WebUI |
| 上游 | OpenAI/Anthropic 官方 | + Notion AI（经 Notion2API 桥接） |
| 维护 | 单项目升级 | 两项目独立升级，互不影响 |
| 额外依赖 | 无 | 无（预编译二进制，推荐）/ Go 1.25+（自编译）/ Docker |
| 前置准备 | API key | Notion 账号 + 能收 OTP 的邮箱 |
| 登录方式 | 无 | Notion2API /admin WebUI 邮箱+OTP |

**核心收益**：用 Notion AI 订阅（$10/月）白嫖 GPT-5.x / Claude Sonnet 4.6 / Gemini 3.1 Pro 等模型，经 zhongzhuan 统一暴露成 Anthropic 协议给 Claude Code 用。
