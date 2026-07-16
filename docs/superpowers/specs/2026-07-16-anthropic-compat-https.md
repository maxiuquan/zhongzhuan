# Zhongzhuan HTTPS 改造方案（面向 Claude Code 入站）

| 字段 | 值 |
|---|---|
| 项目代号 | `zhongzhuan` |
| 文档版本 | v0.1 |
| 创建日期 | 2026-07-16 |
| 依赖文档 | `2026-07-16-anthropic-compat.md` / `2026-06-14-zhongzhuan-design.md` |
| 触发原因 | Claude Code 要求其上游 `ANTHROPIC_BASE_URL` 必须为 `https://`，纯 `http://127.0.0.1:8088` 无法接入 |

> 本方案只动「入站监听」的传输层，不涉及 §8 跨协议翻译逻辑。目标是让代理在 `https://localhost:8443` 上以 TLS 方式提供 `/v1/messages`，并让 Claude Code 信任本代理的自签证书。

---

## 1. 背景与现状

- 当前 `run_foreground` 在 [src/zhongzhuan/__main__.py](file:///workspace/src/zhongzhuan/__main__.py) 第 198-201 行用 `web.AppRunner` + `web.TCPSite` 启动代理，**未传 `ssl_context`**，故仅监听明文 HTTP。
- `TLSConfig` 数据类（[config.py](file:///workspace/src/zhongzhuan/config/config.py) 第 17-22 行）已定义 `enabled / cert_file / key_file`，但全工程没有任何代码读取它——属于「占位未实现」。
- Claude Code 是 Node.js CLI，对自签证书的信任依赖 Node 的 CA 解析，而非浏览器证书库，这点决定本方案的客户端配置（见 §6）。

## 2. 方案选型

| 方案 | 说明 | 取舍 |
|---|---|---|
| A. 本地 CA + 自签叶子证书（推荐） | 用 `cryptography` 生成一个本地 CA，再签发 `CN=localhost`、SAN 含 `localhost`/`127.0.0.1` 的叶子证书；CA 交给客户端信任 | 一次生成长期可用；可被 Node/curl/浏览器统一信任；安全性最好 |
| B. mkcert | 外部工具自动建 CA 并写入系统证书库 | 体验最省事，但引入外部二进制依赖，与「不引大型工具」约束相悖 |
| C. 反向代理（Caddy/nginx） | 前置一层自动 TLS 的反代 | 多一个常驻进程与端口，本机单用户场景过重 |
| D. 关闭校验 `NODE_TLS_REJECT_UNAUTHORIZED=0` | 不验证任何证书 | 全局关闭 TLS 校验，存在中间人风险，仅作兜底 |

**选定 A**：自带 `tls selfsign` 子命令生成 CA + 叶子证书，服务端启用 aiohttp `SSLContext`，客户端通过 `NODE_EXTRA_CA_CERTS` 信任该 CA。D 仅在 A 仍被拒时作为临时兜底（见 §7.3）。

## 3. 证书生成（`tls selfsign` 子命令）

### 3.1 命令形态

```bash
python -m zhongzhuan tls selfsign \
  --cn localhost \
  --out-cert data/localhost.crt \
  --out-key  data/localhost.key \
  --out-ca   data/local-ca.crt \
  --days 3650
```

### 3.2 生成逻辑（基于已列为可选依赖的 `cryptography`）

1. 生成 CA 私钥 + 自签 CA 证书（`CA:TRUE`, 10 年）。
2. 生成叶子私钥 + CSR，由 CA 签发叶子证书（1 年可续）。
3. 叶子证书 **必须**包含 SAN：
   - `DNS:localhost`
   - `IP:127.0.0.1`（若监听其它地址，按 `--san` 追加）
4. 输出三份文件；`local-ca.crt` 即需分发给客户端的信任根。
5. 私钥文件落盘后 `chmod 600`（Windows 下限制 ACL）。

> 无 `cryptography` 时退化为调用系统 `openssl` 命令行，提示用户安装。不引入新运行时强依赖。

## 4. 服务端改造

### 4.1 新增 `build_ssl_context(cfg) -> ssl.SSLContext | None`

在 `proxy/server.py` 或新建 `proxy/tls.py` 中：

```python
import ssl

def build_ssl_context(tls_cfg) -> ssl.SSLContext | None:
    if not tls_cfg.enabled:
        return None
    if not (tls_cfg.cert_file and tls_cfg.key_file):
        raise RuntimeError("tls.enabled=true 但 cert_file/key_file 未配置")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tls_cfg.cert_file, tls_cfg.key_file)
    # 本机回环、自签，不强制客户端证书
    return ctx
```

### 4.2 `__main__.py` 接线

将第 200-201 行改为：

```python
ssl_ctx = build_ssl_context(cfg.server.tls)
proxy_site = web.TCPSite(
    proxy_runner, cfg.server.proxy.host, cfg.server.proxy.port,
    ssl_context=ssl_ctx,  # None 时 aiohttp 回退明文 HTTP
)
scheme = "https" if ssl_ctx else "http"
logger.info(f"proxy listening on {scheme}://{cfg.server.proxy.host}:{cfg.server.proxy.port}")
```

- **仅代理端口启用 TLS**（Claude Code 只打代理端口）。
- **管理端口保持 HTTP**：admin UI 在浏览器打开，自签证书会弹警告，徒增摩擦；admin 不暴露给 Claude Code。`webbrowser.open` 仍用 `http://127.0.0.1:{admin_port}`。

### 4.3 端口约定

- 默认监听 `https://localhost:8443`（HTTPS 惯例端口，避开 8088 明文语义）。
- `config.yaml` 中 `server.proxy.port: 8443`，`server.tls.enabled: true`。
- 不用 443：需要管理员/特权端口，本机工具没必要。

### 4.4 配置示例

```yaml
server:
  proxy:
    host: "127.0.0.1"
    port: 8443
  admin:
    host: "127.0.0.1"
    port: 8089
  tls:
    enabled: true
    cert_file: "data/localhost.crt"
    key_file:  "data/localhost.key"
```

### 4.5 `.env` 覆盖

在 `load_config` 中补一组覆盖（与现有 `ZHONGZHUAN_PROXY_PORT` 同模式）：

- `ZHONGZHUAN_TLS_ENABLED=true|false`
- `ZHONGZHUAN_TLS_CERT` / `ZHONGZHUAN_TLS_KEY`

便于服务模式下不碰 YAML 即可切换。

## 5. 与现有逻辑的兼容性

- **上游出站不受影响**：`UpstreamClient`（httpx）访问 `api.anthropic.com` / DeepSeek 仍走公网 TLS，用系统 CA，与本改造无关。
- **SSE / 翻译状态机无感**：传输层加密对 §9-§10 的应用层翻译透明，状态机代码零改动。
- **`_stream_proxy` 的 keepalive / 首事件前重试**逻辑不变；TLS 握手在首个下游事件之前完成，不破坏「首事件前可重试」窗口。
- **鉴权**：`x-api-key` / `Authorization` 头在 TLS 隧道内传输，反而比明文更安全，`proxy/auth.py` 无需改动。

## 6. Claude Code 客户端配置

Claude Code 是 Node 进程，**不读系统/浏览器证书库**，必须显式喂 CA。

```bash
# 1) 指向本代理（必须是 https）
export ANTHROPIC_BASE_URL=https://localhost:8443

# 2) 让 Node 信任本代理的本地 CA（关键）
export NODE_EXTRA_CA_CERTS=/绝对路径/data/local-ca.crt

# 3) 代理 access token（若开了 proxy_auth）
export ANTHROPIC_API_KEY=<zhongzhuan_access_token>
```

说明：
- `NODE_EXTRA_CA_CERTS` 是 Node 启动期读取的环境变量，Claude Code 及其内部的 Anthropic SDK（基于 `fetch`/undici，Node 18+）会将其并入信任链——这是被官方支持的标准做法。
- CA 路径**必须用绝对路径**，相对路径在某些 Node 版本下不生效。
- 用 `localhost` 而非 `127.0.0.1` 作为 base_url 主机名，与证书 SAN 的 `DNS:localhost` 对齐；证书同时含 `IP:127.0.0.1` SAN，故写 IP 亦可。

## 7. 边缘情况与安全

### 7.1 绑定地址

- 默认 `host=127.0.0.1`，TLS 仅服务于本机回环。若需 LAN 暴露，用户须自行评估并加 SAN，本方案不默认放开。

### 7.2 证书轮换

- 叶子证书 1 年有效期，到期前用同一 CA 重签即可，CA 10 年。重签后仅需重启代理，客户端 CA 不变，无需改 `NODE_EXTRA_CA_CERTS`。
- `tls selfsign` 支持 `--renew-leaf` 只重发叶子证书、复用现有 CA。

### 7.3 兜底：若 Claude Code 仍报证书错

极少数情况（旧版 Node / undici 行为差异）下 `NODE_EXTRA_CA_CERTS` 不生效，按顺序尝试：

1. 确认 `local-ca.crt` 是 CA 而非叶子证书，路径为绝对路径。
2. 把 CA 装入系统证书库：Windows `certutil -user -addstore Root data\local-ca.crt`（浏览器/curl 受益）。
3. 最后兜底：`export NODE_TLS_REJECT_UNAUTHORIZED=0`（**仅本机回环可接受**，会关闭 Node 全局 TLS 校验，切勿用于暴露公网的场景）。

### 7.4 私钥保护

- `data/localhost.key` 设 `0600`；服务模式下放 `%ProgramData%\Zhongzhuan\` 并限制 ACL。
- 与上游 API key 的 DPAPI 加密无关——传输层私钥落盘即可，本机专用不强制加密。

## 8. 验证步骤

1. **证书校验**：
   ```bash
   openssl x509 -in data/localhost.crt -noout -text | grep -A1 "Subject Alternative Name"
   # 期望看到 DNS:localhost, IP Address:127.0.0.1
   ```
2. **curl 走 TLS + 指定 CA**：
   ```bash
   curl --cacert data/local-ca.crt https://localhost:8443/healthz
   # 期望 ok
   ```
3. **Claude Code 真实联调**：设好 §6 三个环境变量后，跑一次普通对话 + 一次工具调用，确认无 TLS 报错、流式正常。
4. **回归**：明文 admin 端口 `http://127.0.0.1:8089` 仍可打开后台。

## 9. 落地清单（给实现者）

1. `proxy/tls.py`（新）：`build_ssl_context(cfg)` + `selfsign(...)` 工具函数。
2. `__main__.py`：第 200-201 行 `TCPSite` 接入 `ssl_context`；新增 `tls selfsign` 子命令分支。
3. `config/config.py`：补 `ZHONGZHUAN_TLS_ENABLED/CERT/KEY` 三项 `.env` 覆盖。
4. `config.yaml` 模板：默认 `tls.enabled: false`，注释里给出启用样例。
5. `docs/`：在 Claude Code 接入说明（anthropic-compat.md §13.1）补充 `https://` + `NODE_EXTRA_CA_CERTS` 写法。
6. `tests/`：`test_tls.py` 断言 `build_ssl_context(None/enabled/disabled)` 行为；`selfsign` 产物含 SAN。

> 改造范围小、纯传输层、对翻译逻辑零侵入。核心是「证书生成 + aiohttp ssl_context + Node 侧 `NODE_EXTRA_CA_CERTS`」三件套。
