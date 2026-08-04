# VPS 部署验证与 P0 迁移故障修复报告

| 项 | 内容 |
|---|---|
| **报告编号** | ZZ-OPS-20260805-01 |
| **日期** | 2026-08-05 |
| **执行人** | WorkBuddy（经 ssh-mcp-server） |
| **服务器** | root@34.4.111.79:22（hostname: instance-20260620-030757，Kernel 6.10.10 x86_64） |
| **项目** | zhongzhuan（OpenAI 兼容 API 中转网关） |
| **涉及版本** | `f1b3434` → `a4cafac`（拉取）→ `4b2cb20`（修复后） |
| **结论** | ✅ 全部通过；发现并修复 1 个 P0 生产故障 |

---

## 1. 背景与目标

在 VPS 上拉取 zhongzhuan 项目远端 main 分支（含此前 PR #24 responses-v3 合并），完成代码同步与全量测试验证，并确保生产服务正常启动。

## 2. 执行过程

### 2.1 远端同步

| 步骤 | 结果 |
|---|---|
| 环境检查 | git 2.39.5 / Python 3.11.2 就绪 |
| 远端核对 | `origin = https://github.com/maxiuquan/zhongzhuan.git` |
| 拉取前版本 | `f1b3434`（落后 origin/main 48 个提交） |
| 拉取后版本 | `a4cafac`（Merge PR #24，含 responses-v3 全部生产代码 + 测试） |
| 工作区冲突 | 无（仅未跟踪的 .env 备份文件） |

### 2.2 依赖环境

- 系统 Python 3.11（Debian 12，PEP 668 管理），需以 `pip3 install --break-system-packages` 安装缺失依赖
- 补充安装：`pydantic`、`pytest`、`pytest-asyncio`、`pytest-timeout`、`pytest-xdist`、`openai`、`hypothesis`、`psutil`
- 生产服务为 systemd：`zhongzhuan.service`（ExecStart=`python3 -m zhongzhuan`，EnvironmentFile=/root/zhongzhuan/.env）

## 3. 测试结果

### 3.1 环境干扰（重要发现）

直接在**生产目录** `/root/zhongzhuan` 运行测试会失败（45 failed）——根因是生产 `config.yaml` / `.env` / `secret.key` 存在于工作目录，`load_config` 在给定路径缺失时会**回退读取工作目录下的真实生产配置**（端口 8443、Let's Encrypt 证书路径），导致断言默认值（端口 8088）失败。本地测试环境无此文件，因此本地全绿、VPS 复现失败。

**处置**：在干净目录 `/tmp/zz_clean` 重新 clone（不含生产配置）运行测试。

### 3.2 干净环境全量测试

| 指标 | 数值 |
|---|---|
| **Passed** | **1166** |
| **Skipped** | 12 |
| **Deselected** | 5（soak 长稳默认排除） |
| **Failed** | 1（见下） |
| 耗时 | 3 分 13 秒 |

**唯一失败项分析**：`test_responses_integration_property.py::test_utf8_multibyte_boundary_fragmentation`

- 原因：hypothesis `FailedHealthCheck: Input generation is slow`（生成 UTF-8 文本输入耗时 1.23s 超过健康检查阈值）
- 判定：**环境 flaky，非代码缺陷**——VPS CPU 较慢触发；用失败 seed（`155456942932738524143862954110395675551`）复跑通过
- 建议：该用例可加 `@settings(suppress_health_check=[HealthCheck.too_slow])`（见遗留事项 6.1）

### 3.3 环境专项坑（沉淀）

1. **Python 3.11 + pytest traceback 格式化崩溃**：`--tb=short/long` 在格式化失败栈时触发 `SystemError: AST constructor recursion depth mismatch`（CPython 3.11 已知 bug），导致整个会话 INTERNALERROR。规避：`--tb=line`。
2. **xdist 并行不可用**：`-n auto` 下 aiomysql 连接在事件循环关闭时抛 `RuntimeError: Event loop is closed`，需串行。
3. **SSH 命令 30s 硬超时**：长任务用 `nohup ... &` 后台 + 轮询日志。

## 4. P0 生产故障：v003 迁移在 TiDB 上启动失败 🔴

### 4.1 故障现象

拉取新代码后重启服务失败：

```
systemctl is-active zhongzhuan  →  activating (auto-restart)，exit code 3
SCHEMA MIGRATION FAILED - refusing to start
  version    : 3
  name       : token_hash
  cause      : IntegrityError: (1062, "Duplicate entry '' for key 'access_tokens.token'")
```

生产服务（VPS 使用 **TiDB Cloud** 而非 SQLite）陷入崩溃重启循环。

### 4.2 根因分析

`v003_token_hash` 迁移的 hook `_hash_legacy_tokens` 将遗留明文 token 哈希后，把 `token` 列清空为**空字符串 `''`**：

```sql
UPDATE access_tokens SET token_prefix=?, token_hash=?, token='' WHERE id=?
```

- **SQLite**：v003 通过重建表移除了 `token` 列的 UNIQUE 约束 → `''` 无冲突 → 本地测试永不暴露
- **MySQL/TiDB**：保留 `token` 列的 **UNIQUE 索引** → UNIQUE 允许**多个 NULL** 但**不允许多个空字符串** → 处理第 2 条遗留行时，第二个 `''` 与第一个 `''` 冲突 → `ER_DUP_ENTRY (1062)`

生产库状态佐证（只读诊断）：

```
表结构：已是 v003 终态（7 个新列存在、token 已 NULL 可空、UNIQUE 保留）
schema_migrations：仅 v1（v3 从未记录成功）
数据：6 行 token；id=1 已哈希（token=''），其余 5 行为明文
```

即：DDL（ALTER）已执行成功（MySQL DDL 隐式提交），hook 在处理第 2 行明文时失败。

### 4.3 修复方案

`src/zhongzhuan/store/migrations/v003_token_hash.py` —— 按方言区分清空值：

| 方言 | 清空值 | 理由 |
|---|---|---|
| SQLite | `''` | 重建表后无 UNIQUE 约束，行为不变 |
| MySQL/TiDB | `NULL` | UNIQUE 索引允许多个 NULL |

```python
clear_token: str | None = None if getattr(ex, "dialect", "") == "mysql" else ""
```

并新增回归测试 `test_v003_mysql_dialect_clears_legacy_tokens_to_null`：以 mysql-dialect executor 驱动 hook，在带 UNIQUE 约束、含两条明文 token 的 post-ALTER 状态表上验证——修复后 token 全部清为 NULL 且已哈希，不再触发重复键。

### 4.4 回归验证

| 测试 | 结果 |
|---|---|
| `tests/test_migrations.py`（含新用例） | 13 passed |
| `tests/test_access_tokens_hash.py` | 7 passed |
| 本地合计 | 20 passed |

### 4.5 交付

| 项 | 内容 |
|---|---|
| 提交 | `4b2cb20` fix(migrations): clear legacy token to NULL on MySQL/TiDB in v003 hook |
| 变更 | 2 files，+94 / −3 |
| 推送 | `a4cafac..4b2cb20  main -> main`（直推 GitHub 成功） |
| VPS 同步 | `git pull origin main` 快进至 4b2cb20 |

## 5. 生产验证（最终状态）

| 检查项 | 结果 |
|---|---|
| 服务状态 | ✅ active（systemd，PID 139214） |
| 迁移记录 | ✅ v1 baseline / **v3~v8 全部 applied**（token_hash、response_store、model_capabilities、tool_executions、schema_realign、route_bindings） |
| 遗留 token 哈希 | ✅ 5 条明文全部哈希（token_prefix + HMAC-SHA256），token 清为 NULL |
| 健康探针 | ✅ `https://127.0.0.1:8443/healthz` → HTTP 200 `{"status":"ok"}` |
| 监听端口 | proxy: `0.0.0.0:8443`（HTTPS），admin: `0.0.0.0:8089` |

## 6. 遗留事项与建议

| # | 事项 | 优先级 | 说明 |
|---|---|---|---|
| 6.1 | hypothesis 健康检查调优 | P2 | `test_utf8_multibyte_boundary_fragmentation` 等属性测试加 `suppress_health_check=[HealthCheck.too_slow]`，避免慢机器 flaky |
| 6.2 | 测试隔离 | P2 | 建议测试运行在干净目录或 `tmp_path` 隔离，避免生产 config.yaml 污染；或给 conftest 增加工作目录守卫 |
| 6.3 | 生产迁移演练 | P1 | 建议预发布在真实 TiDB 上做一次全量迁移演练（本次已在生产验证，但 v9+ 后续迁移应先行验证） |
| 6.4 | 凭据管理 | P1 | VPS `.env` 含 TiDB Cloud 明文凭据 + `secret.key`；建议权限收紧并轮换 |
| 6.5 | Python 3.11 升级 | P3 | VPS Python 3.11.2 存在 pytest traceback 崩溃 bug；可评估升级 3.12+ |

---

## 附录 A：生产数据库信息（脱敏）

- 引擎：TiDB Cloud（AWS us-west-2）
- 连接：`gateway01.us-west-2.prod.aws.tidbcloud.com:4000/zhongzhuan`（SSL）
- 配置来源：`/root/zhongzhuan/.env`（ZHONGZHUAN_TIDB_*）

## 附录 B：诊断命令速查

```bash
# 服务状态
systemctl status zhongzhuan --no-pager

# 干净环境测试（避免生产配置污染）
git clone -b main https://github.com/maxiuquan/zhongzhuan.git /tmp/zz_clean
cd /tmp/zz_clean && python3 -m pytest -q --tb=line -p no:cacheprovider

# 后台跑长任务（SSH 30s 超时规避）
nohup bash -c "python3 -m pytest ... > /tmp/x.log 2>&1" &

# 生产库只读诊断（TiDB）
set -a && source .env && set +a && python3 /tmp/diag.py
```
