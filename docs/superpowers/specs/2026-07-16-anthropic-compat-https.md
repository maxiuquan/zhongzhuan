# Zhongzhuan HTTPS 改造方案（VPS 部署 · 面向 Claude Code 入站）

| 字段 | 值 |
|---|---|
| 项目代号 | `zhongzhuan` |
| 文档版本 | v0.2 |
| 创建日期 | 2026-07-16 |
| 修订日期 | 2026-07-16 |
| 依赖文档 | `2026-07-16-anthropic-compat.md` / `2026-06-14-zhongzhuan-design.md` |
| 部署形态 | 远程 VPS，Claude Code 跨公网访问 |
| 触发原因 | Claude Code 要求 `ANTHROPIC_BASE_URL` 必须为 `https://`；VPS 跨公网传输也必须加密 |

> 本方案只动「入站监听」的传输层 + 公网暴露安全加固，不涉及 §8 跨协议翻译逻辑。目标是让部署在 VPS 上的代理以 `https://<vps>:8443` 对外提供 `/v1/messages`，并让 Claude Code 能信任、能鉴权。

---

## 1. 背景与现状

- `run_foreground` 在 [src/zhongzhuan/__main__.py](file:///workspace/src/zhongzhuan/__main__.py) 第 198-201 行用 `web.AppRunner` + `web.TCPSite` 启动代理，**未传 `ssl_context`**，故仅监听明文 HTTP。
- `TLSConfig`（[config.py](file:///workspace/src/zhongzhuan/config/config.py) 第 17-22 行）已定义 `enabled / cert_file / key_file`，但全工程无人读取——「占位未实现」。
- `proxy_auth_enabled()`（[auth.py](file:///workspace/src/zhongzhuan/proxy/auth.py) 第 11-13 行）靠 `ZHONGZHUAN_PROXY_AUTH=true` 开启，代码注释即标为 "VPS mode"。但中间件第 33-34 行**只认 `Authorization: Bearer`，不认 `x-api-key`**——而 Claude Code 发的是 `x-api-key`，这是 VPS 场景下必须一并修掉的拦路点。
- VPS 上 admin 端口（8089）若直接绑 `0.0.0.0` 裸 HTTP 暴露公网，后台无加密无保护，风险极高。

## 2. 方案选型

证书路径按「是否有域名」二选一：

| 路径 | 适用 | 证书来源 | 客户端是否需额外信任 |
|---|---|---|---|
| **A. Let's Encrypt（首选）** | VPS 有域名指向它 | `certbot` / `acme.sh` 自动签发，标准 CA | 否，Node/系统默认信任 |
| **B. 自签 CA + 叶子证书（兜底）** | 只有公网 IP，无域名 | `tls selfsign` 子命令生成 | 是，需 `NODE_EXTRA_CA_CERTS` |

放弃的选项：mkcert（引入外部二进制，与「不引大型工具」约束相悖）、Caddy/nginx 反代（多一层常驻进程，本可由 aiohttp 直接承担）、`NODE_TLS_REJECT_UNAUTHORIZED=0`（公网场景**禁止**，中间人风险）。

**有域名一律走 A**；无域名才走 B。下文 §3/§6/§7 对两条路径分别说明。

## 3. 证书获取

### 3.1 路径 A：Let's Encrypt（有域名）

前提：`zhongzhuan.example.com` 的 A 记录已指向 VPS 公网 IP。

```bash
# certbot 签发（standalone 会临时占 80，需先停代理或用 webroot）
sudo certbot certonly --standalone -d zhongzhuan.example.com

# 产物（标准路径）：
#   /etc/letsencrypt/live/zhongzhuan.example.com/fullchain.pem  ← cert_file
#   /etc/letsencrypt/live/zhongzhuan.example.com/privkey.pem    ← key_file
```

- 续期：`certbot renew`，可挂 cron/systemd timer；续期后需重启 zhongzhuan 进程重载证书（或 §4.6 热重载）。
- 权限：`privkey.pem` 默认仅 root 可读；让 zhongzhuan 进程能读，要么以 root 跑（不推荐），要么 `chgrp` 到进程用户组并 `chmod 640`。

### 3.2 路径 B：自签（无域名，只有公网 IP）

```bash
python -m zhongzhuan tls selfsign \
  --cn zhongzhuan-vps \
  --san-ip <VPS公网IP> \
  --san-dns zhongzhuan.example.com \
  --out-cert data/server.crt \
  --out-key  data/server.key \
  --out-ca   data/local-ca.crt \
  --days 3650
```

生成逻辑（基于已列为可选依赖的 `cryptography`）：

1. 生成 CA 私钥 + 自签 CA 证书（`CA:TRUE`, 10 年）。
2. 生成叶子私钥 + CSR，由 CA 签发叶子证书（1 年可续）。
3. 叶子证书 **必须**含 SAN：`IP:<VPS公网IP>`（必填），`DNS:<域名>`（若有）。**无 SAN 的自签证书会被 Node 直接拒绝**。
4. 输出三份文件；`local-ca.crt` 即需分发给 Claude Code 客户端的信任根。
5. 私钥落盘 `chmod 600`。

> 无 `cryptography` 时退化为调系统 `openssl` 命令行。不引入新运行时强依赖。

## 4. 服务端改造

### 4.1 新增 `build_ssl_context(cfg) -> ssl.SSLContext | None`

新建 `proxy/tls.py`：

```python
import ssl

def build_ssl_context(tls_cfg) -> ssl.SSLContext | None:
    if not tls_cfg.enabled:
        return None
    if not (tls_cfg.cert_file and tls_cfg.key_file):
        raise RuntimeError("tls.enabled=true 但 cert_file/key_file 未配置")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tls_cfg.cert_file, tls_cfg.key_file)
    # VPS 公网暴露，建议开启（可选）双向 TLS 白名单进一步加固；默认不强求客户端证书
    return ctx
```

### 4.2 `__main__.py` 接线（第 200-209 行）

```python
ssl_ctx = build_ssl_context(cfg.server.tls)
proxy_site = web.TCPSite(
    proxy_runner, cfg.server.proxy.host, cfg.server.proxy.port,
    ssl_context=ssl_ctx,  # None 时 aiohttp 回退明文 HTTP
)
scheme = "https" if ssl_ctx else "http"
logger.info(f"proxy listening on {scheme}://{cfg.server.proxy.host}:{cfg.server.proxy.port}")
```

- **代理端口**：`host=0.0.0.0`，`port=8443`，启用 TLS。
- **管理端口**：`host=127.0.0.1`（**仅本机**），保持 HTTP。公网访问走 SSH 隧道（§7.2），不裸暴露。

### 4.3 端口与监听约定

| 端口 | 协议 | 绑定 | 用途 |
|---|---|---|---|
| 8443 | HTTPS | `0.0.0.0` | 代理 `/v1/messages`，Claude Code 入口 |
| 8089 | HTTP | `127.0.0.1` | 管理后台，仅本机 + SSH 隧道 |

不用 443：需 root 特权端口，且与 VPS 上其它 Web 服务易冲突；8443 语义清晰。

### 4.4 配置示例

```yaml
server:
  proxy:
    host: "0.0.0.0"          # VPS 对外
    port: 8443
  admin:
    host: "127.0.0.1"        # 仅本机
    port: 8089
  tls:
    enabled: true
    # 路径 A（Let's Encrypt）
    cert_file: "/etc/letsencrypt/live/zhongzhuan.example.com/fullchain.pem"
    key_file:  "/etc/letsencrypt/live/zhongzhuan.example.com/privkey.pem"
    # 路径 B（自签）则改为 data/server.crt / data/server.key
```

### 4.5 `.env` 覆盖

在 `load_config` 中补一组（与现有 `ZHONGZHUAN_PROXY_PORT` 同模式）：

- `ZHONGZHUAN_TLS_ENABLED=true|false`
- `ZHONGZHUAN_TLS_CERT` / `ZHONGZHUAN_TLS_KEY`

便于 systemd 服务模式下不碰 YAML 即可切换。同时 VPS 必备：

- `ZHONGZHUAN_PROXY_AUTH=true`（强制开启 access token 鉴权）
- `ZHONGZHUAN_PROXY_HOST=0.0.0.0` / `ZHONGZHUAN_ADMIN_HOST=127.0.0.1`

### 4.6 证书热重载（可选，路径 A 续期后免重启）

`SSLContext` 没有原生热重载，但可：
- 监听 `SIGHUP` → 重建 `SSLContext` → `await proxy_runner.cleanup()` 后用新 ctx 重建 `TCPSite`。
- 或更简单：`certbot renew --deploy-hook "systemctl restart zhongzhuan"`，接受秒级中断。

v0.2 先走 deploy-hook 重启；热重载列为后续优化。

## 5. 修复 proxy_auth 对 `x-api-key` 的支持（VPS 必修）

[auth.py](file:///workspace/src/zhongzhuan/proxy/auth.py) 第 32-35 行只解析 `Authorization: Bearer`，Claude Code 发的是 `x-api-key`。VPS 上 proxy_auth 必须开（否则公网裸奔），故必须同时识别两个头：

```python
# 优先 x-api-key（Anthropic 客户端），回退 Authorization: Bearer（OpenAI 客户端）
token = request.headers.get("x-api-key", "").strip()
if not token:
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
if not token or not await db_verify_token(store, token):
    return web.json_response(
        {"error": {"message": "invalid or missing access token", "type": "unauthorized"}},
        status=401,
    )
```

- 同一个 access token 两种头都能用，客户端无感。
- `/v1/models` GET 仍放行（模型发现）。
- 错误信封按入站协议打包（Anthropic 客户端收 `{type:"error",...}`）属 §11 翻译范畴，这里先保证 401 不误杀。

> 这与 `anthropic-compat.md` §8 的 `_extract_access_token` 兼容 `x-api-key` 是同一件事，可合并实现。

## 6. Claude Code 客户端配置

### 6.1 路径 A（Let's Encrypt，有域名）

标准 CA，Node 默认信任，无需额外环境变量：

```bash
export ANTHROPIC_BASE_URL=https://zhongzhuan.example.com:8443
export ANTHROPIC_API_KEY=<zhongzhuan_access_token>   # 即代理的 access token
```

### 6.2 路径 B（自签，无域名只有 IP）

需让 Node 信任本地 CA：

```bash
export ANTHROPIC_BASE_URL=https://<VPS公网IP>:8443
export ANTHROPIC_API_KEY=<zhongzhuan_access_token>
export NODE_EXTRA_CA_CERTS=/本地绝对路径/local-ca.crt   # 关键
```

说明：
- `NODE_EXTRA_CA_CERTS` 是 Node 启动期读取的环境变量，Claude Code 及其内部 Anthropic SDK（基于 `fetch`/undici，Node 18+）会将其并入信任链——官方支持的标准做法。
- CA 路径**必须用绝对路径**，相对路径在部分 Node 版本下不生效。
- base_url 主机名必须与证书 SAN 精确匹配（IP 证书 → base_url 用 IP；域名证书 → 用域名），否则 `ERR_TLS_CERT_ALTNAME_INVALID`。

## 7. VPS 公网安全加固

### 7.1 防火墙

只放行必要端口，admin 端口不对外：

```bash
# 仅放行 8443（代理）+ 22（SSH）
ufw default deny incoming
ufw allow 22/tcp
ufw allow 8443/tcp
ufw enable
# 8089 不放行，仅 127.0.0.1 本机访问
```

### 7.2 admin 后台访问方式

admin 绑 `127.0.0.1:8089`，公网不可达。远程访问用 SSH 隧道：

```bash
# 本地机器执行，把 VPS 的 8089 转到本地 8089
ssh -L 8089:127.0.0.1:8089 user@<VPS>
# 然后本地浏览器开 http://127.0.0.1:8089
```

隧道内为明文，但整段链路已被 SSH 加密包裹，安全。

### 7.3 鉴权强制

- `ZHONGZHUAN_PROXY_AUTH=true` 必须开启；VPS 不开等于把上游 API key 池暴露给公网。
- access token 通过 admin 后台生成，妥善保管；Claude Code 侧作为 `ANTHROPIC_API_KEY`。
- **禁用 `NODE_TLS_REJECT_UNAUTHORIZED=0`**：本地方案曾列为兜底，VPS 跨公网场景绝对禁止，会令整条链路失去防中间人能力。

### 7.4 私钥保护

- `data/server.key`（路径 B）或 `privkey.pem`（路径 A）设 `0600` / `0640`，归属运行进程的用户。
- 与上游 API key 的加密无关——传输层私钥落盘即可，VPS 上靠文件权限 + 防火墙保障。

### 7.5 证书 SAN 与主机名一致

路径 B 自签时，SAN 必须包含 Claude Code 实际连接的主机名（IP 或域名）。漏 SAN 是自签方案最常见失败点，§8 验证步骤会显式检查。

## 8. 验证步骤

1. **证书 SAN 校验**：
   ```bash
   openssl x509 -in <cert_file> -noout -text | grep -A1 "Subject Alternative Name"
   # 路径 A 期望 DNS:zhongzhuan.example.com
   # 路径 B 期望 IP Address:<VPS公网IP>（及 DNS:<域名> 若有）
   ```
2. **curl 走 TLS**：
   ```bash
   # 路径 A：系统信任 Let's Encrypt，直接打
   curl https://zhongzhuan.example.com:8443/healthz
   # 路径 B：指定自签 CA
   curl --cacert local-ca.crt https://<VPS公网IP>:8443/healthz
   # 期望 ok
   ```
3. **鉴权验证**：带 `x-api-key` 打 `/v1/messages`，期望正常；不带或错 token 期望 401。
4. **Claude Code 真实联调**：设好 §6 环境变量后，跑一次普通对话 + 一次工具调用，确认无 TLS 报错、流式正常、`x-api-key` 鉴权通过。
5. **admin 隔离回归**：VPS 公网 `curl http://<VPS公网IP>:8089` 应连接拒绝/超时；SSH 隧道后本地 `http://127.0.0.1:8089` 可打开后台。

## 9. 落地清单（给实现者）

1. `proxy/tls.py`（新）：`build_ssl_context(cfg)` + `selfsign(...)` 工具函数。
2. `__main__.py`：第 200-209 行 `TCPSite` 接入 `ssl_context`；新增 `tls selfsign` 子命令分支。
3. `proxy/auth.py`：第 32-35 行同时识别 `x-api-key` 与 `Authorization: Bearer`（VPS + Claude Code 必修）。
4. `config/config.py`：补 `ZHONGZHUAN_TLS_ENABLED/CERT/KEY` 三项 `.env` 覆盖。
5. `config.yaml` 模板：VPS 部署样例（proxy `0.0.0.0:8443` + tls.enabled，admin `127.0.0.1:8089`）。
6. `docs/`：在 Claude Code 接入说明（anthropic-compat.md §13.1）补充 `https://` 写法，区分域名/IP 两种 base_url + 是否需 `NODE_EXTRA_CA_CERTS`。
7. 部署文档：新增「VPS 部署」一节，含防火墙、SSH 隧道访问 admin、Let's Encrypt 续期 deploy-hook。
8. `tests/`：`test_tls.py` 断言 `build_ssl_context(None/enabled/disabled)` 行为、`selfsign` 产物含 SAN；`test_auth.py` 增加 `x-api-key` 头通过用例。

> VPS 场景三件套：**TLS 监听（Let's Encrypt 优先）+ proxy_auth 兼容 x-api-key + admin 端口隔离**。证书只是其中一环，公网暴露的鉴权与端口隔离同等关键。
