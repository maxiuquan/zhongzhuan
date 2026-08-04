# 生产安全加固方案：多端接入 + 代理认证

| 项 | 内容 |
|---|---|
| **编号** | ZZ-SEC-20260805-01 |
| **日期** | 2026-08-05 |
| **服务器** | root@34.4.111.79（instance-20260620-030757） |
| **状态** | ✅ **已执行（2026-08-05）**，验证通过 |
| **原则** | 保持公网 HTTPS 8443 多端接入；最小改动、可回滚、客户端不中断 |

---

## 1. 现状与问题确认

| # | 检查项 | 现状 | 风险 |
|---|---|---|---|
| 1 | `ZHONGZHUAN_PROXY_AUTH` | `false`（代理接口无认证） | 🔴 公网任意 IP 可无认证调用 `/v1/*` 消耗上游额度（已实测 `/v1/models`、`/v1/responses` 均 200） |
| 2 | 监听地址 | proxy `0.0.0.0:8443`、admin `0.0.0.0:8089` | 🔴 全部公网暴露 |
| 3 | 防火墙 | 无 ufw、iptables INPUT 策略 ACCEPT | 🔴 无任何访问控制 |
| 4 | `ZHONGZHUAN_JWT_SECRET` | 空（dev 随机生成，重启即变） | 🟠 admin 会话重启失效；随机密钥非持久 |
| 5 | `ZHONGZHUAN_ADMIN_AUTH` | `true` | ✅ admin 已启用登录 |
| 6 | TLS | Let's Encrypt（api.macc.eu.cc） | ✅ HTTPS 就绪 |
| 7 | 文件权限 | `.env`/`secret.key`/`config.yaml`/`data.db` 已 600（本方案执行前已收紧） | ✅ 已修复 |
| 8 | git 历史 | `.env`、`secret.key` 从未入库；唯一命中为测试占位符 | ✅ 无泄漏 |

**核心结论**：现有 6 条 access token（已哈希存储）**继续有效**——认证时按明文哈希比对，客户端已持有的 `zz-*` 明文 token 无需更换即可通过校验。

---

## 2. 目标架构（多端接入 + 认证）

```
公网客户端 (OpenAI/Anthropic 兼容)
        │  HTTPS 8443
        │  Authorization: Bearer <token>  或  x-api-key: <token>
        ▼
┌─────────────────────────────┐
│  zhongzhuan proxy (8443)     │
│  ZHONGZHUAN_PROXY_AUTH=true  │──► 校验 token（哈希比对）→ 配额/白名单 → 上游
│  /v1/models  GET 免认证       │
└─────────────────────────────┘
        │
        ▼
   TiDB Cloud (上游多模型)
```

- 每个渠道 = 一条独立 access token（可设 label / 配额 / 模型白名单 / 过期）
- `/v1/models` GET 保持免认证（OpenAI 客户端模型发现惯例）
- admin 8089 仅限本机/白名单访问

---

## 3. 实施步骤（共 4 步，全部可回滚）

### 步骤 A：生成持久 JWT 密钥（一次）

```bash
# 生成 32 字节随机密钥
openssl rand -hex 32
# 例：a3f9...（输出后写入 .env）
```

在 `/root/zhongzhuan/.env` 追加：

```ini
ZHONGZHUAN_JWT_SECRET=<上面生成的64位hex>
```

> 支持轮换：以后换密钥时把旧值写入 `ZHONGZHUAN_JWT_SECRET_PREVIOUS`，旧会话在 `ZHONGZHUAN_JWT_GRACE_PERIOD_SECONDS`（默认 3600s）内仍有效。

### 步骤 B：启用代理认证

编辑 `/root/zhongzhuan/.env`：

```ini
# 原值
ZHONGZHUAN_PROXY_AUTH=false
# 改为
ZHONGZHUAN_PROXY_AUTH=true
```

**影响**：开启后 `/v1/*`（除 `/v1/models` GET）必须带有效 token，否则 401。

### 步骤 C：为各渠道创建独立 token（admin API）

```bash
# 登录 admin 获取 JWT（密码在 .env 的 ZHONGZHUAN_ADMIN_PASSWORD）
JWT=$(curl -s -X POST https://127.0.0.1:8089/api/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"<admin>\",\"password\":\"<password>\"}" | jq -r .token)

# 创建 token（label 区分渠道；quota_tokens=-1 不限；expires_days=0 永不过期）
curl -s -X POST https://127.0.0.1:8089/api/tokens \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"label":"channel-a", "quota_tokens":-1, "expires_days":0}'

# 返回值中的 token 字段即该渠道的明文密钥，仅此一次显示
```

> 也可在管理后台 UI（`https://api.macc.eu.cc:8089` 或本机 http）创建。
> 现有 6 条 token 明文保留在各自客户端配置中，无需重建。

### 步骤 D：防火墙与监听收紧

**方案 D1（推荐）：安装 ufw，只放行必要端口**

```bash
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp          # SSH
ufw allow 443/tcp         # HTTPS（若用反代/证书校验）
ufw allow 8443/tcp        # 中转 API（多端接入必需）
ufw allow 80/tcp          # ACME 续期（Let's Encrypt）
ufw --force enable
```

**方案 D2：admin 8089 不对公网开放**

修改 `.env` 将 admin 绑定到本机（若后续要远程管理，用 SSH 隧道）：

```ini
ZHONGZHUAN_ADMIN_HOST=127.0.0.1
```

> 注意：D2 需配合防火墙 D1 才完整；仅改 host 不能防 iptables ACCEPT 下的端口转发，但可减少暴露面。若管理员常驻本机，也可保持 8089 公网 + 已有 JWT 认证（降级为 🟠）。

---

## 4. 验证清单（执行后逐项确认）

| # | 验证 | 预期 |
|---|---|---|
| 1 | 无 token 调 `/v1/chat/completions` | HTTP 401 |
| 2 | 带旧 token 调 `/v1/chat/completions` | HTTP 200 |
| 3 | 无 token 调 `/v1/models` GET | HTTP 200（免认证） |
| 4 | 错误 token | HTTP 401 |
| 5 | 重启后 admin 登录 | 旧会话失效、新登录成功（JWT 持久） |
| 6 | `ss -tlnp` | 仅预期端口监听 |
| 7 | 外部扫描 `nmap` 未放行端口 | filtered/closed |

---

## 5. 回滚方案

```bash
# 1) 关闭代理认证
sed -i 's/ZHONGZHUAN_PROXY_AUTH=true/ZHONGZHUAN_PROXY_AUTH=false/' /root/zhongzhuan/.env
# 2) 还原监听（若改了 ADMIN_HOST）
# 3) 禁用防火墙
ufw --force disable
# 4) 重启
systemctl restart zhongzhuan
```

---

## 6. 风险与说明

- **客户端兼容**：OpenAI 系用 `Authorization: Bearer`，Anthropic 系用 `x-api-key`——两条路径代码都已支持，无需客户端改 header 格式，只需确保配置了 token。
- **现有 token 不失效**：哈希比对设计，明文存量 token 直接可用。
- **`/v1/models` 例外**：保持免认证（模型发现），不泄露业务数据。
- **配额防护**：每个渠道 token 可单独设 `quota_tokens` 上限与模型白名单，防止单一渠道滥用。
- **待用户决策**：admin 8089 是否公网保留（D2 取舍）；现有 6 条 token 的配额分配。

---

## 7. 执行记录（2026-08-05）

| 步骤 | 执行内容 | 验证结果 |
|---|---|---|
| A | `openssl rand -hex 32` 生成固定 JWT_SECRET 写入 `.env`（备份 `.env.bak.20260804193129`） | ✅ 服务重启正常 |
| B | `.env` `ZHONGZHUAN_PROXY_AUTH=false → true` | ✅ 无 token 调 `/v1/chat/completions`、`/v1/responses` → 401；错误 token → 401；`/v1/models` GET 免认证 → 200；有效 token → 200（responses `completed`） |
| C | 存量 6 条 token 无需更换（哈希比对）；各渠道 token 用 admin API `/api/tokens` 创建 | ✅ 临时 token 验证后已清理 |
| D | 安装 ufw：默认 deny incoming / allow outgoing；放行 22/80/443/8443（8089 保留，管理后台有 JWT 登录保护） | ✅ 公网 80 拒绝；公网带有效 token 调 8443 `/v1/responses` → 200 completed |
| 端到端 | 公网 `https://34.4.111.79:8443` 多端接入 | ✅ 认证 + 防火墙 + 转发全链路正常 |

**遗留决策**：admin 8089 如需更严（仅 SSH 隧道访问），移除 ufw 放行规则即可。
