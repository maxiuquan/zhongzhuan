# zhongzhuan Responses Bridge v3 需求池与交付范围说明

| 项 | 内容 |
|---|---|
| 文档类型 | 工程交付范围说明书（非市场型 PRD） |
| 版本 | v1.0 |
| 上游依据 | `docs/zhongzhuan Responses Bridge v3 开发文档 2f2b9eee3f75420f92230f37e125ace1.md`（全文 1322 行） |
| 需求条目总数 | **123**（P0 = 35 / P1 = 70 / P2 = 18） |
| 编写人 | 许清楚（产品经理） |
| 下游消费者 | 架构师（架构设计）、开发、测试 |
| 本文档不含 | 竞品分析、市场调研、UI 设计稿、架构方案、代码 |

> **阅读顺序建议**：先读 §1 交付目标 → §3 范围裁定（有需要拍板的产品判断）→ §4 待确认问题 → 再回到 §2 需求池逐条认领。

---

## 1. 交付目标与一句话定义

### 1.1 一句话定义

> **v3 把 zhongzhuan 从「能让 Codex 跑起来的 Responses 桥接」升级为「OpenAI 官方 SDK 可直接对接、且在任意故障下都能在有限预算内确定性终止的 Responses API 网关」。**

### 1.2 交付完成后客户端能做什么它现在做不到的事

| # | 现在做不到 | v3 之后 | 对应需求簇 |
|---|---|---|---|
| 1 | 多轮会话中历史 reasoning 会被回灌上游，导致重复推理/死循环 | 历史 reasoning 在净化阶段 100% 丢弃，抓包可证 | R-P0-12~14 |
| 2 | 工具 arguments 分片时可能被提前判完成，发出残缺 function call | 任意字节分片下工具名/call_id/arguments 完整重组，校验通过才发 `.done` | R-P0-15~16 |
| 3 | 首 token 前断流会直接进 completed，客户端拿到空响应且无从诊断 | `created` 前置发送，断流有 `terminal_reason` + `incomplete_details` | R-P0-17~18 |
| 4 | 新增 Responses 参数被透传上游触发 400 | allowlist 构造上游请求，未知字段静默丢弃 + 计数 | R-P0-19~20 |
| 5 | 深度推理模型 60–120s 首 token 会被 30s 默认超时打断并重放 | 分层超时，首 token/读空闲下限 300s + 15s heartbeat | R-P0-01、R-P0-21 |
| 6 | `GET/DELETE/POST cancel` 等资源操作一律返回 405 | 6 个官方资源端点真实实现，官方 SDK 直接可用 | R-P1-28 |
| 7 | 无服务端状态，`store` / `previous_response_id` 被丢弃 | ResponseStore 持久化，会话可续接、可 retrieve、可 delete | R-P1-29~33 |
| 8 | 无 background mode | `background=true` 立即返回可轮询 response，支持 cancel 与 catch-up 流 | R-P1-34~38 |
| 9 | hosted tool 因「没有 name」被直接丢弃且客户端无感知 | 识别、持久化、路由；无能力时返回标准错误而非伪造成功 | R-P1-44~46、§3 |
| 10 | 异常终止统一标记 completed，无法归因 | 12 类错误 + 10 类熔断原因 + 13 个指标 + 结构化日志 | R-P0-32、R-P1-51~55 |
| 11 | 工具/状态链可能无限循环，无硬止损 | ExecutionBudget + 状态链防环 + 重放熔断，任意故障有限终止 | R-P0-27~34 |
| 12 | 旧协议改动无回归保护 | 三链路隔离 + golden fixture 字节级对比 + 特性开关秒级回滚 | R-P0-22~26 |

### 1.3 不在本次交付内（一句话）

网关本地不内置 Code Interpreter 沙箱、Computer use VM、vector store 与搜索引擎执行器；这些能力通过**原生 Responses 上游直通**提供，无可用上游时按标准错误拒绝。详见 §3。

---

## 2. 结构化需求池

### 2.0 优先级定义与 NO-GO 口径

| 优先级 | 定义（对齐文档 §13.1） | 门槛 |
|---|---|---|
| **P0** | 发布阻断。开发 v3 前/中必须完成的地基、五条铁律、改动隔离、有限终止 | 未完成 → v3 **禁止以任何形式启用**（含灰度） |
| **P1** | v3 主开发阶段。功能主体，含全量 Responses 兼容 | 未完成 → 可灰度但**不得对外宣称「OpenAI Responses 全量兼容」**，只能声明「Codex Responses Profile 兼容」 |
| **P2** | 上线前完善。安全、健康、CI、容量治理 | 未完成 → **禁止公网部署**，仅限本地/内网 |

> **口径裁定（需拍板）**：文档 §16 把任何未实现项列为 NO-GO，与「分阶段交付」存在张力。本文档裁定：§16 是**「默认启用 + 宣称全量兼容」的门槛**，不是「合并入 main」的门槛。P0 全绿即可灰度；P0+P1 全绿且兼容报告无未支持项，方可切默认。

### 2.1 P0 — 发布阻断（35 条）

#### 簇 A：仓库稳定化（源自 §13.1 P0 六项 + §14 Phase 0）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P0-01 | 超时拆分为 connect(15s)/pool(15s)/write(60s)/read_idle(300s)/first_token(300s)/total(900s) 六项独立配置，且 `first_token` 与 `read_idle` 下限强制 ≥300s，配置低于下限时启动报错 | §3铁律5、§8、§13.1-P0-1 | 单测：注入 YAML `first_token_seconds: 30` 断言启动抛配置异常；集成：mock 上游首 token 延迟 120s，请求成功 | — |
| R-P0-02 | 启动日志打印六项超时的**最终生效值**及来源（default/YAML/env）；存在配置合并回归测试锁定 30s 不会复现 | §13.1-P0-1 | 单测：`test_effective_timeout_logged`；断言 `.env`+YAML+默认三层合并后 `read_idle>=300` | R-P0-01 |
| R-P0-03 | `store/logs.py` 的 `cleanup_old_logs()` 仅执行 `DELETE FROM request_logs WHERE ts < ?`，与 `get_usage_stats()` 完全分离，SQLite 与 TiDB 双后端各有集成测试 | §13.1-P0-2 | 集成测试：两后端各插 100 行跨 30 天数据，调用 cleanup(14) 后断言剩余行数；模块导入测试进 CI | — |
| R-P0-04 | 存在 `schema_migrations` 版本表，每次迁移在事务内执行并记录版本号、SQL 摘要、耗时 | §13.1-P0-3 | 单测：连续启动两次，断言迁移只执行一次且版本表递增 | — |
| R-P0-05 | 迁移异常只按**错误码**确认的 duplicate column 才忽略；其余异常阻断启动并输出失败版本号与 SQL 原文；SQLite 迁移前自动备份 | §13.1-P0-3 | 单测：注入磁盘只读/锁冲突，断言进程退出码非 0 且日志含版本号；断言备份文件生成 | R-P0-04 |
| R-P0-06 | `access_tokens` 表存储 `token_prefix` + `keyed_hash`（HMAC，密钥来自配置），不存明文；校验使用常量时间比较 | §13.1-P0-4 | 单测：创建 token 后查库断言无明文子串；`test_constant_time_compare`；迁移脚本对存量明文 token 完成一次性哈希化 | R-P0-04 |
| R-P0-07 | token 列表接口只返回 `prefix + 掩码`；token / API key / JWT / Authorization 在任意日志级别下均不落盘；新增 rotation、last_used_at、created_by、revoke audit 字段 | §13.1-P0-4 | 单测：捕获全部日志输出断言不含 token 全文；接口测试断言响应体无完整 token | R-P0-06 |
| R-P0-08 | `pyproject.toml` 成为依赖唯一事实源，声明 aiosqlite/cryptography/python-dotenv，并提供 `sqlite`/`tidb`/`admin`/`build` extras（含 aiomysql/PyJWT/bcrypt/pyinstaller）；仓库存在 `README.md`（当前缺失但被 `readme=` 引用） | §13.1-P0-5 | CI：`pip install .` 与 `pip install .[tidb,admin]` 在 clean venv 成功；`python -c "import zhongzhuan"` 成功 | — |
| R-P0-09 | CI 在纯净环境构建并安装 wheel 与 sdist，执行 CLI 启动冒烟；依赖锁定支持范围并接入漏洞扫描 | §13.1-P0-5 | CI job `clean-install` 绿；`zhongzhuan --help` 退出码 0 | R-P0-08 |
| R-P0-10 | 新增 ResponseStore 六类表：`responses`、`response_input_items`、`response_event_log`、`background_jobs`、`tool_executions`、`idempotency_records`，均含租户键、索引与 TTL 字段 | §13.1-P0-6、§4.2.2 | 迁移测试：SQLite/TiDB 双后端建表成功；schema 快照测试 | R-P0-04 |
| R-P0-11 | `response_event_log` 为 append-only，携带全局严格递增 `sequence_number`（同一 response 内无重复、无空洞） | §13.1-P0-6、§4.2.8 | 单测：并发写 1000 事件后断言 sequence 连续且唯一；断言无 UPDATE/DELETE 路径 | R-P0-10 |

#### 簇 B：五条协议铁律（源自 §3、§5.2）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P0-12 | 请求净化阶段丢弃 `input[]` 中全部 `type=reasoning` 项（含 summary、content、encrypted_content），发往上游的 payload 序列化后不含任何历史 reasoning 文本 | §3铁律1、§5.2 | 单测：构造含 4 种 reasoning 形态的 input，断言上游 payload JSON 中无对应字符串；集成抓包用例 | — |
| R-P0-13 | 代码中不存在 `pending_reasoning`、`pending_reasoning_encrypted`、`attach_pending_reasoning()` 三个符号 | §5.2 | 静态检查：grep 断言 0 命中，纳入 CI lint 规则 | R-P0-12 |
| R-P0-14 | reasoning 内容不参与粘性会话指纹计算、不写入可重放消息历史、不持久化到 ResponseStore 的可读文本字段 | §3铁律1、§4.2.5、§11.2 | 单测：同一会话两轮 reasoning 不同但 session hash 相同；查库断言 reasoning 文本列为空/仅元数据 | R-P0-12 |
| R-P0-15 | 工具调用的 `name`、`call_id`、`arguments` 在内存中跨 chunk 聚合，仅在可靠结束信号（finish_reason / stream 终止）后校验完整性再发 `.done` | §3铁律2、§5.3 | 属性测试：同一工具调用流按 1 字节切分，输出 `.done` 恰好 1 次且 arguments 与原文逐字节一致 | — |
| R-P0-16 | 中途 arguments 恰为合法 JSON 不触发提前完成；残缺 arguments 不产生可执行 function call item；同一 call 的 `.done` 不重复发送 | §3铁律2、§7.3 | 单测：`{"a":1}` 后继续追加 `,"b":2}` 的分片流，断言只在结束时完成一次；断流用例断言无 `function_call_arguments.done` | R-P0-15 |
| R-P0-17 | `response.created` 在下游 HTTP 连接建立后立即发送，不等待上游首 token；上游首 chunk 前断流的流仍先收到 `created` | §3铁律3、§7.1 | 单测：mock 上游立即断开，断言事件序列首个为 `response.created` | — |
| R-P0-18 | 每条流中 `response.completed`（或 failed/incomplete/cancelled）与 `data: [DONE]` 各恰好出现一次；终止操作幂等，重复调用不产生额外事件 | §3铁律3、§5.6 | 单测：对同一流连续调用 finish 三次，断言事件计数不变；全量流式用例统一断言器 | R-P0-17 |
| R-P0-19 | 上游请求体由显式 allowlist 构造，代码中不存在 `result = dict(body)` 式复制-删除写法 | §3铁律4、§5.1 | 静态检查 + 单测：注入 20 个虚构字段，断言上游 payload key 集合 ⊆ allowlist | — |
| R-P0-20 | 未知 Responses 字段既不透传上游也不导致 400，仅写 debug 日志并累加 `responses_unknown_params_dropped_total` | §3铁律4 | 单测：含未知字段的请求返回 200，指标 +N，`dropped_fields` 日志字段非空 | R-P0-19 |
| R-P0-21 | SSE 流每 15s 发送 comment heartbeat 刷新链路活性且不改变 Responses 状态机状态；部署文档明确要求反代 `proxy_read_timeout` > 应用 read timeout | §3铁律5、§8 | 集成：静默上游 120s，断言收到 ≥7 个 heartbeat 且状态仍为 STREAMING；文档检查项 | R-P0-01 |
| R-P0-35 | `discard_historical_reasoning` 在生产模式下为不可变安全策略：配置为 false 时启动直接失败，不作为普通功能开关 | §15 | 单测：`env=production` + `discard_historical_reasoning: false` 断言启动异常 | R-P0-12 |

#### 簇 C：向后兼容与改动隔离（源自 §4.1、§13）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P0-22 | 路由存在**唯一**分叉点 `if inbound_protocol == "responses": handle_responses_v3() else: handle_legacy_protocol()`，v3 判断不散落在旧协议分支中 | §4.1 | 静态检查：`responses_bridge`/`v3` 相关条件判断出现在 ≤1 处路由函数；代码走查项 | — |
| R-P0-23 | RequestSanitizer、reasoning 丢弃、参数 allowlist、OutputIndexAllocator、Responses 状态机、call ID 归一化、heartbeat 均不作用于 `/v1/chat/completions` 与 `/v1/messages` 路径 | §4.1 | 单测：Chat→Chat 透传含 `reasoning_content` 的请求，断言字段原样到达上游；Anthropic 路径同理 | R-P0-22 |
| R-P0-24 | `ResponsesStreamTranslator`、`StreamO2A`、`StreamA2O` 的公开方法签名与行为不变，旧调用方无需同步迁移 | §4.1、§13 | 接口快照测试：inspect 签名对比 golden；旧测试文件零修改通过 | R-P0-22 |
| R-P0-25 | 特性开关 `responses_bridge.{version,enabled}` 支持 global/model/group/key 四级灰度，关闭后立即回退现有 Responses 实现且不影响 Chat/Anthropic | §4.1、§14 Phase4 | 单测：四级开关矩阵各 1 例；集成：运行时关闭开关后下一请求走 v2 路径 | R-P0-22 |
| R-P0-26 | 旧协议建立 golden fixture：Chat→Chat、Chat→Anthropic、Anthropic→Chat 的流式/非流式/文本/工具调用/错误响应共 ≥12 条**字节级**事件序列基线；任一失败即 NO-GO | §4.1、§14 Phase0 | CI job `legacy-golden` 绿；fixture 文件纳入版本控制 | — |

#### 簇 D：有限终止与防无限循环（源自 §9）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P0-27 | 每个 response 持有 `ExecutionBudget`：max_tool_rounds=32、max_calls_per_tool=8、max_identical_call_repeats=2、max_total_tool_calls=64、max_wall_time_seconds=900、max_output_tokens_total=200000，任一触顶即熔断 | §9.4 | 单测：6 个阈值各 1 例故障注入，断言终止且 `incomplete_details.reason` 正确 | R-P0-10 |
| R-P0-28 | 工具调用签名 `sha256(tool_name + canonical_json(arguments) + normalized_result_or_error)`：相同 name+arguments 连续超阈值终止；相同 name+arguments+result 重复即判无进展熔断；仅空白/键序/call_id 差异视为同一调用 | §9.4 | 单测：同一工具连调 3 次相同参数断言熔断；变更 JSON 键序后仍判定为重复 | R-P0-27 |
| R-P0-29 | 状态链防环：拒绝自引用、沿 parent 链维护 visited 集合检测祖先环、最大链深默认 64（租户可收窄）、限制最大恢复 items 数与 token 数；parent 不存在/已删除/跨租户/成环时返回标准 Responses 错误而非降级为无状态 | §9.4、§4.2.3 | 单测：自引用、A→B→A 双节点环、65 层深链、跨租户 parent 各 1 例，断言标准错误码 | R-P0-10 |
| R-P0-30 | 尚未发送任何业务 delta 前最多允许 2 次上游切换；已发送 text/reasoning/tool arguments delta 后禁止透明重放；上游反复在同一位置断流时进入 circuit open 停止自动重试 | §9.2、§9.4 | 单测：首字节前失败 3 次断言第 3 次不再重试；delta 后断流断言无第二次上游请求 | — |
| R-P0-31 | `[DONE]`/completed/failed/incomplete/cancelled 任一正式终止后，全部迟发事件被拒绝并累加 `responses_late_chunks_total` | §9.4、§12.1 | 单测：completed 后注入 5 个 delta，断言下游零输出且指标 +5 | R-P0-18 |
| R-P0-32 | 支持 10 类熔断终止原因枚举（max_tool_rounds / max_total_tool_calls / repeated_tool_call / repeated_tool_failure / max_response_time / max_output_budget / response_chain_cycle / response_chain_too_deep / retry_budget_exhausted / background_budget_exhausted）；熔断后严格执行 6 步：停上游读取→停调度新工具→回滚或记录副作用→关闭已开 item→发且仅发一个终止事件+[DONE]→写日志/指标/审计 | §9.4 | 单测：10 个原因各 1 例断言枚举值；熔断动作顺序断言器 | R-P0-27、R-P0-29 |
| R-P0-33 | 同一 call_id 只产生一个完成的 function call item；重复 chunk 不产生重复 `.done`；item done 后迟到 chunk 丢弃并计入 `responses_duplicate_tool_chunks_total`；维护短 TTL completed call ID 集合用于重放检测 | §9.3 | 单测：同一 call_id 的完整分片流重放两遍，断言下游只有 1 个完成 item | R-P0-15 |
| R-P0-34 | 自动重试仅限 DNS/连接失败、TLS 失败、上游 429/5xx、首字节前断开四类；其余一律不重试 | §9.1 | 单测：四类可重试 + 400/422/客户端取消不可重试，共 7 例 | R-P0-30 |

**P0 小计：35 条**（簇 A 11 + 簇 B 12 + 簇 C 5 + 簇 D 8，其中 R-P0-35 归入簇 B）

### 2.2 P1 — 主开发阶段（70 条）

#### 簇 E：模块设计（§5）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P1-01 | 新增 `responses_request.py` 实现 RequestSanitizer，返回 `SanitizedRequest{payload, dropped_fields, normalized_call_ids, warnings}` | §5.1 | 单测：结构字段齐全；dropped_fields 与实际丢弃项一致 | R-P0-19 |
| R-P1-02 | 定义 `CHAT_BASE_ALLOWED`（12 字段）与 `PROVIDER_CAPABILITIES`（deepseek / openai_compatible / anthropic），上游可在基础 allowlist 上进一步收窄 | §5.1 | 单测：三种上游各断言 payload key 集合；DeepSeek 不含其不支持字段 | R-P1-01 |
| R-P1-03 | 13 个 Responses 专有字段（input/instructions/previous_response_id/store/parallel_tool_calls/metadata/include/background/prompt_cache_key/client_metadata/service_tier/truncation/text）被消费或丢弃，不原样透传上游 | §5.1 | 单测：13 字段全传，断言上游 payload 均不含 | R-P1-01 |
| R-P1-04 | `EphemeralReasoningAccumulator{output_index,item_id,text,added,done}` 仅存活于当轮内存，流终止后立即释放 | §5.2 | 单测：流结束后断言对象被回收/字段清空；内存增长测试 | R-P0-14 |
| R-P1-05 | `reasoning_event_mode` 支持 `reasoning_summary_text` / `reasoning_text` / `disabled` 三档，运行时可配置 | §5.2 | 单测：三档各断言下游事件类型；见 §4-Q1 默认值裁定 | R-P1-04 |
| R-P1-06 | `ToolCallAccumulator` 维护 `tools_by_call_id` 与 `tools_by_source_index` 双索引，按「已有 call_id → 已有 source index → 新建」三级优先级匹配 | §5.3 | 单测：并行 2 工具交错分片；相同 source index 不同 call_id 异常上游行为 | R-P0-15 |
| R-P1-07 | call_id 延迟出现时将按 index 建立的 accumulator 绑定到 call_id；上游始终不返回 ID 时使用稳定合成值 `call_{response_id}_{source_index}` | §5.3 | 单测：ID 延迟 3 chunk 出现；ID 全程缺失断言合成值稳定可复现 | R-P1-06 |
| R-P1-08 | 函数名支持「分片追加」与「重复完整值替换」两类上游行为，最终名称正确 | §5.3 | 单测：`get_` + `weather` 分片；`get_weather` 重复 3 次，均得 `get_weather` | R-P1-06 |
| R-P1-09 | arguments 每个非空 fragment 原样追加；delta 可实时发送但结束前不发 `.done`；结束时 JSON 解析校验，默认要求顶层为 object；解析失败不创建可执行 function call | §5.3 | 单测：Unicode/转义/嵌套 JSON 各 1 例；顶层为数组时断言拒绝 | R-P1-06 |
| R-P1-10 | `OutputIndexAllocator` 为 message / reasoning / function_call 分配**全局唯一**递增 output index，每个 item 仅首次创建时分配一次；禁止复用 choice index 或 tool index | §5.4 | 单测：reasoning+2 工具+message 混合流，断言 index 集合为 `{0,1,2,3}` 无重复 | — |
| R-P1-11 | 独立 `SSEParser` 支持 `\n\n` 与 `\r\n\r\n` 分隔、单事件多行 `data:`、任意 TCP 字节分片、UTF-8 多字节边界、注释行与 event/id/retry 字段 | §5.5 | 属性测试（Hypothesis）：同一流随机切分 1000 次，输出语义恒等 | — |
| R-P1-12 | 无法解析的 JSON 帧累加指标并记录，不静默吞掉终止原因 | §5.5 | 单测：注入畸形 JSON 帧，断言指标 +1 且流仍能正确终止 | R-P1-11 |
| R-P1-13 | `ResponsesEventEmitter` 实现显式状态机 `INIT→CREATED→IN_PROGRESS→STREAMING→COMPLETING→COMPLETED`，拒绝并记录 5 类非法转换（INIT→COMPLETED、COMPLETED→DELTA、同 item 重复 added、item done 后追加、completed 重复发送） | §5.6 | 单测：5 类非法转换各 1 例断言抛错/丢弃 + 日志 | R-P0-18 |
| R-P1-14 | `sequence_number` 在单条 response 内全局严格递增，实时流与 catch-up 流共用同一事件日志编号 | §5.6、§4.2.8 | 单测：断言严格单调；catch-up 与实时序列号一致性对比 | R-P0-11 |
| R-P1-65 | `CompositeStreamTranslator.finish_safely()` 统一为 async；禁止同步方法调用 async `feed()`；禁止遍历列表时向同一列表追加 | §13 | 单测：并发 finish 无 RuntimeWarning；静态检查无同步调用 async 点 | R-P0-24 |
| R-P1-66 | 新增 7 个协议模块文件（responses_request / responses_models / sse_parser / tool_accumulator / turn_accumulator / responses_emitter），`responses.py` 瘦身为对外兼容入口 | §13 | 文件存在性检查；`responses.py` 行数较基线 646 行下降 ≥50% | R-P0-24 |

#### 簇 F：请求转换规则（§6）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P1-15 | `instructions` 转为首条 system 消息；已有 system/developer 消息时按稳定顺序合并，同一 instructions 不重复注入 | §6.1 | 单测：无 system / 有 system / 重复调用三例，断言合并结果与顺序稳定 | R-P1-01 |
| R-P1-16 | `input_text`/`output_text` → Chat text；`input_image` → image URL 或 data URL（由上游能力声明决定保留与否）；未知 content block 静默丢弃并记 warning，不原样透传字典 | §6.2 | 单测：4 类 block + 1 类未知 block，断言输出与 warnings | R-P1-01 |
| R-P1-17 | function call 转换：name 为空则丢弃并记 warning；arguments 非字符串则序列化为 JSON 字符串；call ID 统一规范化并在 function call output 中复用同一映射；不生成空 `tool_calls: []` 的 assistant 消息 | §6.3 | 单测：4 条规则各 1 例 | R-P1-01 |
| R-P1-18 | function call output 转换：找不到对应 call ID 时不猜测绑定并记录协议异常；非字符串 output 使用稳定 JSON 序列化（键序确定）；超限输出按配置截断并保留明确截断标记 | §6.4 | 单测：孤儿 output、dict output、超长 output 各 1 例；断言两次序列化字节一致 | R-P1-17 |
| R-P1-19 | tools 转换：只接受可降级 function tool；缺失 parameters 补 `{"type":"object","properties":{}}`；`strict` 仅在上游声明支持时保留；非法 schema 本地修正或丢弃，不发送明显非法 schema 上游 | §6.5 | 单测：4 条规则各 1 例；hosted tool 处理见 R-P1-46 | R-P1-02 |

#### 簇 G：流式生命周期（§7）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P1-20 | 正常文本流事件序列严格等于 §7.1 的 9 步（created→in_progress→output_item.added→content_part.added→output_text.delta×N→output_text.done→content_part.done→output_item.done→completed→[DONE]） | §7.1 | golden 事件序列对比测试 | R-P1-13 |
| R-P1-21 | reasoning + tool call 流事件序列严格等于 §7.2 的 11 步 | §7.2 | golden 事件序列对比测试 | R-P1-13、R-P1-05 |
| R-P1-22 | 兼容模式（默认）：已建立 item 被安全关闭；残缺 tool call 不发 arguments done 且不进入可执行 output；发送 completed + `[DONE]`；记录 `terminal_reason=upstream_truncated`；completed 响应携带 `incomplete_details` | §7.3 | 单测：文本中途断流、工具参数中途断流各 1 例，断言四项 | R-P0-16 |
| R-P1-23 | 严格模式：发送 `response.failed`/`response.incomplete` 终止事件后再发 `[DONE]`；通过配置开关启用 | §7.3 | 单测：同一断流场景在两种模式下事件类型不同，`[DONE]` 均为最后一帧 | R-P1-22 |

#### 簇 H：超时与连接管理（§8）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P1-24 | 客户端断开后立即取消上游读取（不等待当前超时窗口耗尽） | §8 | 集成：客户端在 2s 断开，断言上游连接在 ≤1s 内关闭 | R-P0-01 |
| R-P1-25 | 客户端主动取消不调用 `mark_failure()`，不降低上游 key 健康度 | §8 | 单测：取消后断言 key_health 各维度数值不变；指标 `responses_client_disconnect_total` +1 | R-P1-24 |
| R-P1-26 | 连接错误、首 token 超时、读空闲超时、总超时四类分别归类上报，日志与指标可区分 | §8、§10 | 单测：四类超时各 1 例，断言 `terminal_reason` 取值互不相同 | R-P0-01 |
| R-P1-27 | Responses SSE 响应禁用压缩（不设置 `Content-Encoding: gzip`），避免事件边界与缓冲延迟 | §8 | 接口测试：断言响应头无压缩编码；heartbeat 到达间隔 ≤16s | R-P0-21 |

#### 簇 I：全量 Responses API 兼容（§4.2）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P1-28 | 6 个资源端点真实实现：`POST /v1/responses`、`GET /v1/responses/{id}`、`DELETE /v1/responses/{id}`、`POST /v1/responses/{id}/cancel`、`POST /v1/responses/compact`、`GET /v1/responses/{id}/input_items`；移除 `handler.py:387` 的非 POST 统一 405 | §4.2.2 | 接口测试：6 端点各返回官方 schema 对象；断言无 405；OpenAI Python SDK 直调通过 | R-P0-10 |
| R-P1-29 | ResponseStore 持久化 8 类内容：response object+status+生命周期时间、净化请求与标准化 input items、output items 与 usage、previous response 关系、background 状态与取消标记、error/incomplete_details/terminal_reason、必要 tool execution 状态；**不持久化原始私密 reasoning 文本** | §4.2.2 | 单测：8 类各断言可读回；查库断言 reasoning 文本列为空 | R-P0-10、R-P0-14 |
| R-P1-30 | 完整支持 `store=true/false` 语义：false 时不落库且 retrieve 返回官方 404 语义 | §4.2.3 | 接口测试：store=false 后 GET 返回 404；store=true 后可 retrieve | R-P1-28 |
| R-P1-31 | `previous_response_id` 从 ResponseStore 恢复可见上下文，按铁律 1 过滤历史 reasoning 后构造上游消息；不得简单丢弃该字段；instructions 不自动继承 | §4.2.3 | 集成：三轮会话，断言第三轮上游 payload 含第一轮 user 文本但无任何 reasoning；断言 instructions 未继承 | R-P1-29、R-P0-29 |
| R-P1-32 | `GET /input_items` 支持列举与分页（limit/after/before/order），返回官方 schema | §4.2.3 | 接口测试：50 条 items 分 3 页取回，断言无重复无遗漏 | R-P1-28 |
| R-P1-33 | 删除父 response 后子引用返回标准错误而非崩溃或静默降级；ResponseStore 全表按 workspace/租户键隔离，跨租户访问返回 404 | §4.2.3 | 单测：删父后引用、跨租户 retrieve 各 1 例 | R-P1-29 |
| R-P1-34 | background 状态机完整实现 `queued → in_progress → {completed, failed, incomplete, cancelled}`；`background=true` 立即返回可查询 response | §4.2.4 | 单测：5 个终态各 1 例；接口测试断言 create 响应 <1s 返回且 status=queued | R-P1-29 |
| R-P1-35 | `POST /cancel` 设置持久化取消标记并向上游传播；worker 在每次工具调用前后检查取消标记 | §4.2.4、§9.4 | 集成：运行中 cancel，断言上游连接关闭且最终 status=cancelled | R-P1-34 |
| R-P1-36 | background 完成后支持 streaming catch-up，事件不重复、不乱序，与实时流共用同一事件日志 | §4.2.4、§4.2.8 | 单测：catch-up 事件序列与实时序列 sequence_number 逐条相等 | R-P1-14、R-P1-34 |
| R-P1-37 | 进程重启后 background 任务从已持久化 checkpoint **只恢复一次**，无法恢复则明确标记 failed，不形成 crash-restart 循环 | §4.2.4、§9.4 | 集成：kill -9 后重启，断言任务恢复一次；第二次崩溃后标记 failed | R-P1-34 |
| R-P1-38 | background task 设置 TTL、并发上限、租户配额及独立预算（最大墙钟/工具轮数/token/存储/外部调用次数）；超预算统一进 `incomplete` 并写 `incomplete_details.reason` | §4.2.4、§9.4 | 单测：5 类预算各 1 例断言 incomplete + reason | R-P0-27、R-P1-34 |
| R-P1-39 | 建立**版本化 item registry**，覆盖官方正式类型：message / input_text / output_text / refusal / annotations / input_image / input_file / reasoning(+summary+encrypted 元数据) / function_call / function_call_output / web_search_call / file_search_call / computer_call(+output) / code_interpreter_call / image_generation_call / mcp_call / mcp_approval_request / mcp_approval_response | §4.2.5 | 每类 item 一个 fixture，断言序列化/反序列化与官方 SDK 兼容 | R-P0-10 |
| R-P1-40 | 为 API 对象完整性保存 reasoning item 的**元数据或不透明标识**，但已消费的 reasoning 文本绝不重新写入下一轮 Chat/Anthropic 历史 | §4.2.5 | 单测：retrieve 返回 reasoning item 占位但下一轮上游 payload 无其文本 | R-P0-12、R-P1-29 |
| R-P1-41 | 建立**版本化 Responses 请求 schema**，覆盖 §4.2.7 全部正式字段；参数处理分两步：先按 schema 验证，再按执行模式决定直通/消费/模拟/转换；只有真正未知的非官方字段才静默丢弃 | §4.2.7 | 单测：官方字段传入不进 dropped_fields；虚构字段进 dropped_fields；非法值返回 400 | R-P1-01 |
| R-P1-42 | 建立**版本化 streaming event registry**，覆盖 §4.2.8 全部事件族；每个 added/delta/done 生命周期可验证；事件 schema 与 OpenAI 官方 SDK 反序列化兼容 | §4.2.8 | 每个事件族一个 fixture；OpenAI Python SDK `client.responses.stream()` 全程无反序列化异常 | R-P1-13 |
| R-P1-43 | 不把 `[DONE]` 当作唯一完成依据，以正式终止事件（completed/failed/incomplete/cancelled）决定最终 status | §4.2.8 | 单测：仅有 `[DONE]` 无终止事件的上游流，断言本地判定为 incomplete 而非 completed | R-P1-22 |
| R-P1-44 | 模型/key 配置支持 `upstream_mode: responses_native` + `capabilities` 列表（stateful_responses/background/web_search/file_search/computer/code_interpreter/image_generation/remote_mcp）；原生模式**不得**先降级为 Chat Completions，代理只做鉴权替换、模型映射、租户策略、审计和必要 schema 兼容 | §4.2.9 | 集成：配置原生上游后抓包断言请求路径为 `/v1/responses` 且 output item 未被改写 | R-P1-41 |
| R-P1-45 | `CapabilityRouter` 按「原生直通 > 本地完整模拟 > 可证明等价转换」三级优先级选择执行模式；生产环境缺少必要执行器时在**启动或配置阶段**输出能力缺口清单，运行时不假装成功 | §4.2.1、§2.2 | 单测：三种模式各 1 例路由断言；启动测试断言能力缺口 WARN/fail-closed（见 §4-Q4） | R-P1-44 |
| R-P1-46 | hosted tool 不得再以「没有 name」为理由直接丢弃：请求侧必须被 schema 识别、校验并持久化到 ResponseStore；无可用能力路由时按 §4-Q4 语义返回标准错误 | §4.2.6 | 单测：7 类 hosted tool 各 1 例，断言未被静默丢弃；无能力时返回 400 而非 200 | R-P1-45、R-P1-41 |
| R-P1-47 | 工具副作用具备审批流程（mcp_approval_request/response）、幂等键、超时、审计记录与租户隔离 | §4.2.6 | 单测：approval 往返 1 例；同一幂等键重复请求不二次执行 | R-P1-29 |
| R-P1-48 | 支持并行工具调用及 `allowed_tools` / `tool_choice`（auto/none/required/具体工具）语义 | §4.2.6 | 单测：4 种 tool_choice + 并行 3 工具各 1 例 | R-P1-06 |
| R-P1-49 | OpenAI 官方 SDK 合约测试：**Python 与 TypeScript 必做**（create/retrieve/delete/cancel/compact/input_items + store/previous_response/background 多轮 + 全量 streaming event schema + 与原生 OpenAI 的 differential test）；Go/Java/.NET 见 §3 裁定 | §4.2.10 | CI job `sdk-contract` 绿 | R-P1-28、R-P1-42 |
| R-P1-50 | 生成**版本化兼容报告**：列出目标 OpenAI API/SDK 版本、全部端点/字段/item/事件的通过状态，与 §3 范围裁定逐项对齐 | §4.2.10、§16 | 报告文件产出且 CI 校验其覆盖度；每项状态 ∈ {通过, 直通可用, 无执行器-标准错误, 明确不做} | R-P1-49 |

#### 簇 J：错误模型与可观测性（§10、§11）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P1-51 | 实现 12 类内部错误分类（invalid_client_request / unsupported_input_block / upstream_connect_error / upstream_rate_limited / upstream_server_error / first_token_timeout / read_idle_timeout / upstream_truncated / invalid_sse_frame / invalid_tool_arguments / client_disconnected / internal_translation_error），并按 §4-Q4 扩展 2 类能力错误 | §10 | 单测：14 类各 1 例，断言分类值与映射的 HTTP 状态码 | R-P1-26 |
| R-P1-52 | 错误响应不暴露 API key、完整 Authorization header、原始敏感 tool output 或 reasoning 内容 | §10 | 单测：构造含密钥的上游错误，断言下游响应体不含密钥子串 | R-P1-51 |
| R-P1-53 | 实现 13 个指标：responses_requests_total / streams_completed_total / streams_truncated_total / unknown_params_dropped_total / reasoning_history_dropped_total / tool_calls_total / tool_call_json_invalid_total / duplicate_tool_chunks_total / late_chunks_total / first_token_seconds / stream_duration_seconds / heartbeat_total / client_disconnect_total | §11.1 | 单测：13 个指标各 1 例断言存在并可被触发；`/metrics` 快照测试 | — |
| R-P1-54 | 结构化日志包含 14 个字段：request_id / session_id_hash / model / upstream_protocol / upstream_key_id / stream / attempt / first_token_ms / duration_ms / dropped_fields / reasoning_history_items_dropped / tool_call_count / terminal_reason / client_disconnected | §11.2 | 单测：解析一条完整请求日志 JSON 断言 14 键齐全 | R-P0-32 |
| R-P1-55 | 日志禁止记录 API key、Authorization header、完整 reasoning 文本，默认不记录完整 tool arguments/output | §11.2 | 单测：全量日志捕获后正则断言零命中敏感模式 | R-P1-54 |
| R-P1-56 | 可选匿名化 debug capture：保留事件类型/时间/索引/ID 哈希/fragment 长度，文本与参数默认脱敏，设置大小与 TTL 上限，提供离线 replay 测试入口 | §11.3 | 单测：capture 后断言无明文内容；replay 入口可复现事件序列 | R-P1-53 |

#### 簇 K：项目整体优化 P1（§13.1 P1 七项）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P1-57 | `handler.py`（当前 1118 行）拆分为 9 个组件：RequestContextBuilder / ProtocolRouter / CapabilityRouter / AttemptManager / StreamingPipeline / AccountingService / ResponseRepository / LegacyProtocolHandler / ResponsesV3Handler；共享对象通过显式 RequestContext 传递 | §13.1-P1-7 | 文件存在性 + `handler.py` 行数 ≤300；旧协议 golden fixture 全绿 | R-P0-22、R-P0-26 |
| R-P1-58 | `classify_failure()` 对 400/404/409/413/422 等请求侧 4xx **不调用** `mark_failure()`、不修改 key 健康状态；仅认证、限流、服务端和网络错误影响对应维度 | §13.1-P1-8 | 单测：5 个请求侧状态码断言 key_health 不变；4 类应影响的错误断言生效 | — |
| R-P1-59 | 失败计数拆分为 `total_failures` 与 `consecutive_failures`；`mark_success()` 重置 consecutive，避免历史偶发错误永久放大退避 | §13.1-P1-8 | 单测：失败 3 次后成功 1 次，断言 consecutive=0、total=3、退避恢复 | R-P1-58 |
| R-P1-60 | 分组调度先按 group strategy 选 model、再在 model 内按 key 健康选 key；failover 严格遵守配置顺序；weighted 使用可测试的平滑加权轮询；sticky session 仅在选定模型健康且能力兼容时生效 | §13.1-P1-9 | 单测：round_robin/weighted/failover 各断言 100 次调用分布；failover 顺序断言 | R-P1-57 |
| R-P1-61 | 粘性会话优先使用显式 conversation / previous_response_id / 客户端 session header；Responses 在 ResponseStore 持久化 session→route binding；无显式 ID 时使用**首轮稳定指纹**而非滚动消息尾部；binding 具备 TTL、能力校验与故障迁移记录 | §13.1-P1-10 | 单测：10 轮会话内容各异，断言 fingerprint 恒定、命中同一 key | R-P1-29、R-P0-14 |
| R-P1-62 | 配置加载使用 Pydantic 或等价严格 schema；对端口、timeout、并发、TTL、路径、URL 做范围验证；未知配置字段默认报错并提供显式兼容模式；启动输出脱敏后的 effective config 与配置来源 | §13.1-P1-11 | 单测：类型错误/越界值/未知字段各断言启动失败；effective config 快照无密钥 | R-P0-02 |
| R-P1-63 | 请求体在入口只解析一次并保存到 `request["json_body"]` 与 RequestContext，鉴权 middleware 与 handler 不重复解析；按端点设置请求大小上限；文件/图像输入采用流式或临时对象存储 | §13.1-P1-12 | 单测：注入解析计数器断言 =1；超限请求返回 413 | R-P1-57 |
| R-P1-64 | SQLite 使用显式事务与批量写入；request logs 与 event logs 走队列批处理；SQLite 模式设置容量边界并明确「单实例」定位；多实例生产使用 TiDB/MySQL 并具备连接池、事务隔离与幂等约束测试 | §13.1-P1-13 | 基准测试：1000 条日志写入的 commit 次数 ≤ 批次数；TiDB 幂等约束集成测试 | R-P0-10 |

#### 簇 L：测试体系（§12）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P1-67 | 单元测试覆盖 §12.1 全部 17 项（reasoning 全形态丢弃 / 不入 session hash / 未知字段与未知 block / `input=[]` 不绕过净化 / 5 字段不透传 / name+arguments 任意分片 / call ID 延迟或缺失 / 并行交错分片 / 同 index 异 call_id / Unicode 转义嵌套 / output index 唯一 / sequence 单调 / added-done 配对 / completed 与 [DONE] 各一次 / 首 chunk 前断流仍先 created / usage-only 与空 choices 与无 [DONE] / completed 后迟发丢弃） | §12.1 | 17 项各至少 1 个测试用例，CI 断言用例名覆盖清单 | 簇 B、E |
| R-P1-68 | Hypothesis 属性测试覆盖 5 类分片场景（UTF-8 多字节边界 / JSON 字符串转义边界 / `\r\n` 与 `\n` 混合 / 一 chunk 多事件与一事件多 chunk / tool arguments 每字节分片），验证输出语义不变 | §12.2 | CI job `property-tests` 绿，min examples ≥200 | R-P1-11 |
| R-P1-69 | 集成兼容矩阵覆盖 9 项（Codex×DeepSeek reasoning / Codex×OpenAI 兼容非推理 / Codex×Anthropic / 单工具 / 并行工具 / 工具失败后下一轮 / 首 token 延迟 120s / 上游中途断流 / 客户端主动取消） | §12.3 | 9 个集成用例全绿（可用 mock 上游 + 真机各一套） | 簇 G、H |
| R-P1-70 | 长稳测试：单会话连续 100 轮、连续 1000 次工具调用、10–50 并发 Codex 会话、随机注入 429/5xx/断流/延迟；验证无无限重试、无重复工具执行、内存不持续增长 | §12.4 | nightly job；断言 RSS 增长 <10%、重复工具执行数 =0、无未终止 response | 簇 D |

**P1 小计：70 条**（E 16 + F 5 + G 4 + H 4 + I 23 + J 6 + K 8 + L 4）

### 2.3 P2 — 上线前完善（18 条，源自 §13.1 P2 五项）

| ID | 需求描述（可验证） | 来源 | 验收方式 | 依赖 |
|---|---|---|---|---|
| R-P2-01 | CORS 由默认 `*` 改为可配置 allowlist，生产模式下空 allowlist 拒绝跨域 | §13.1-P2-14 | 单测：allowlist 内/外来源各 1 例 | R-P1-62 |
| R-P2-02 | 生产模式下管理端鉴权与代理鉴权默认开启，显式关闭需二次确认配置项 | §13.1-P2-14 | 单测：`env=production` 且鉴权关闭断言启动失败 | R-P1-62 |
| R-P2-03 | 管理 UI/API 具备 CSRF 防护、登录限速、审计日志与安全响应头（HSTS/X-Content-Type-Options/X-Frame-Options/CSP） | §13.1-P2-14 | 接口测试：无 CSRF token 的写操作 403；连续登录失败触发限速；响应头断言 | R-P0-07 |
| R-P2-04 | JWT secret 未配置时生产模式 fail closed（不再进程内随机生成）；支持 secret 轮换且轮换期间旧会话可平滑过渡 | §13.1-P2-14 | 单测：生产模式缺 secret 启动失败；轮换后旧 token 在宽限期内仍有效 | R-P1-62 |
| R-P2-05 | 生产模式下默认 dummy key fail closed，不向 OpenAI 发送无效认证 | §13.1-P2-14 | 单测：无有效 key 时生产模式启动失败；开发模式仍可启动并告警 | R-P1-62 |
| R-P2-06 | OpenCode Free fallback 改为显式 opt-in（默认关闭），UI 显示外部数据出站隐私提示 | §13.1-P2-14 | 单测：默认配置下 fallback 未注册；UI 快照含提示文案 | — |
| R-P2-07 | `/healthz` 分层为 liveness（事件循环与进程存活）、readiness（迁移完成、≥1 可用 route、worker lease 正常）、dependency status（存储、上游、工具执行器） | §13.1-P2-15 | 接口测试：迁移未完成时 readiness 返回 503；三层各断言字段 | R-P0-04、R-P1-45 |
| R-P2-08 | 公开健康接口不泄露 key、内部 URL 或敏感拓扑 | §13.1-P2-15 | 单测：响应体正则断言无 URL/密钥模式 | R-P2-07 |
| R-P2-09 | 接入 Prometheus 指标导出与 OpenTelemetry tracing；记录 TTFT、每事件延迟、重试原因、能力路由决策与熔断原因 | §13.1-P2-16 | `/metrics` 端点可被 Prometheus 抓取；trace span 含 5 类属性 | R-P1-53 |
| R-P2-10 | 日志写入异步化，数据库慢查询不阻塞响应链路 | §13.1-P2-16 | 基准测试：注入 500ms DB 延迟，断言 P99 响应延迟增幅 <50ms | R-P1-64 |
| R-P2-11 | retention 定期执行，对 response / event log / tool audit 分别设置独立 TTL（默认见 §4-Q3） | §13.1-P2-16 | 集成：写入过期数据后触发 retention，断言各表按各自 TTL 清理 | R-P0-03、R-P1-29 |
| R-P2-12 | 错误文本、tool 参数与用户输入执行脱敏与大小限制后再落盘 | §13.1-P2-16 | 单测：超长与含敏感模式的内容落盘后被截断/脱敏 | R-P1-55 |
| R-P2-13 | CI 强制 9 项检查：ruff/format、mypy 或 pyright、pytest+coverage、Hypothesis 属性测试、bandit+依赖漏洞扫描、wheel/sdist clean install、SQLite/TiDB 双后端集成、Windows/Linux 启停、旧协议 golden fixtures、OpenAI SDK contract tests | §13.1-P2-17 | 全部 job 为 required check，PR 无法绕过 | R-P0-09、R-P0-26、R-P1-49 |
| R-P2-14 | 仓库禁止提交 `data.db-wal`、`data.db-shm` 与构建产物；接入 secret scanning 与提交前检查 | §13.1-P2-17 | `.gitignore` 覆盖 + CI 检查历史与增量；pre-commit hook 存在 | — |
| R-P2-15 | `global_concurrent` 被真正执行，并为每租户、每模型、background worker 设置独立 semaphore | §13.1-P2-18 | 压测：超限并发被排队而非全部放行；三层 semaphore 各 1 例单测 | R-P1-57 |
| R-P2-16 | 排队时间设置上限，过载时快速返回标准 429/503 而非无限排队 | §13.1-P2-18 | 压测：队列超时后返回 429，断言 Retry-After 头存在 | R-P2-15 |
| R-P2-17 | 工具执行、模型流与持久化使用相互隔离的并发池，一种工作负载饱和不拖垮其余 | §13.1-P2-18 | 压测：工具池打满时模型流请求成功率 >95% | R-P2-15 |
| R-P2-18 | 建立响应对象、事件日志、文件与 debug capture 的容量模型及磁盘水位保护（水位超限触发提前回收 + 告警） | §13.1-P2-18 | 集成：模拟磁盘占用超阈值，断言触发回收与告警 | R-P2-11 |

**P2 小计：18 条**

### 2.4 需求池统计

| 优先级 | 条数 | 占比 | 主要簇 |
|---|---|---|---|
| P0 | 35 | 28.5% | 仓库稳定化 11 / 五条铁律 12 / 改动隔离 5 / 有限终止 8 |
| P1 | 70 | 56.9% | 模块 16 / 请求转换 5 / 流式 4 / 超时 4 / 全量兼容 23 / 错误可观测 6 / 项目优化 8 / 测试 4 |
| P2 | 18 | 14.6% | 安全 6 / 健康 2 / 观测保留 4 / CI 2 / 容量 4 |
| **合计** | **123** | 100% | — |

> **文档覆盖度自查**：§3 铁律 5 条 ✅ / §4.1 全部约束 ✅ / §4.2 九个子节 ✅ / §5 六个模块 ✅ / §6 五类规则 ✅ / §7 三种生命周期 ✅ / §8 全部要求 ✅ / §9 四个子节 ✅ / §10 错误模型 ✅ / §11 三个子节 ✅ / §12 四类测试 ✅ / §13 文件改造 ✅ / §13.1 P0×6+P1×7+P2×5 ✅ / §15 配置约束 ✅。

---

## 3. 交付范围裁定

### 3.1 裁定原则（对齐 §4.2.1）

```
优先级：原生 Responses 直通 > 本地完整模拟 > 可证明语义等价的转换
硬约束：不允许静默降级、伪造成功或丢弃有语义的正式字段
```

**关键区分（本文档提出的核心裁定）**：把「**协议兼容性**」与「**能力可用性**」分开验收。

- 协议兼容性 = 网关必须**识别、校验、持久化、路由、正确回放**每一种官方能力的 item / event / 参数。这是 100% 交付项，无例外。
- 能力可用性 = 该能力**实际由谁执行**。可以由原生上游执行（直通），也可以由网关本地执行器执行。本次交付选择前者。

### 3.2 「框架就绪 + 能力路由」如何满足「不静默丢失语义」

这是 §16 NO-GO 条款下必须回答的问题。落地形态包含五道强制闸门，缺一不可：

| 闸门 | 要求 | 对应需求 ID | 反例（视为不达标） |
|---|---|---|---|
| ① 请求侧不丢弃 | hosted tool 定义被 schema 识别、校验并持久化到 ResponseStore | R-P1-41、R-P1-46、R-P1-29 | 因「没有 name」把 tool 从 tools 数组剔除后照常调用模型 |
| ② 路由侧显式判定 | CapabilityRouter 检查目标 model/key 的 `capabilities` 声明 | R-P1-44、R-P1-45 | 不检查能力直接降级为 Chat Completions |
| ③ 有能力则完整可用 | 声明该 capability 的上游走原生直通，item/event 语义不被改写 | R-P1-44 | 直通时改写 hosted tool 的 output item |
| ④ 无能力则标准报错 | 返回官方格式错误对象（见 §4-Q4），**绝不**丢掉 tool 后返回一个正常的文本回答 | R-P1-46、R-P1-51 | 返回 200 + 普通文本，客户端以为工具没被调用是模型的选择 |
| ⑤ 启动期暴露缺口 | 启动/配置阶段打印能力缺口清单；生产模式 fail closed | R-P1-45、§4-Q4 | 运行时才发现没有执行器 |

**结论**：闸门 ④ 是「不静默丢失语义」的实现载体。客户端得到的是**明确的、可编程处理的失败**，而不是一个语义被悄悄阉割的成功。这严格符合 §4.2.1 第 4 条。

**对 §16 第 15 条验收口径的裁定（需拍板）**：

| 轨道 | 内容 | 是否本次 NO-GO 项 |
|---|---|---|
| A · 直通轨 | 配置原生 Responses 上游时，7 类 tool 端到端可用 | ✅ 是 |
| B · 无执行器轨 | 无能力上游时返回标准错误 + 启动期告警 + 不伪造成功 | ✅ 是 |
| C · 本地执行器轨 | 网关自建沙箱/VM/向量库执行 hosted tool | ❌ 否，列入 v3.1 |

### 3.3 逐项裁定表

| # | 能力（§4.2.6） | 裁定 | 理由 | 无能力时行为 | 需求 ID |
|---|---|---|---|---|---|
| 1 | **Function calling** | 🟢 完整实现 | 核心能力，现有基础可复用，Chat/Anthropic 双向可无损转换 | N/A | R-P0-15/16、R-P1-06~09、R-P1-17~19 |
| 2 | **并行工具调用 / allowed_tools / tool_choice** | 🟢 完整实现 | 纯协议语义，无外部依赖 | N/A | R-P1-48 |
| 3 | **工具审批 approval 流程** | 🟢 完整实现 | 纯协议 + 状态机，网关可自持，且是工具副作用安全的前提 | N/A | R-P1-47 |
| 4 | **Remote MCP** | 🟢 完整实现（网关内置 MCP client） | MCP 是纯网络协议，无需沙箱/浏览器/GPU 等重资产；是 7 类 hosted tool 中唯一网关可低成本自持的，且能反向补齐其他能力（用户可用 MCP server 接入自己的搜索/代码执行） | N/A | R-P1-39、R-P1-46、R-P1-47 |
| 5 | **Web search** | 🟡 框架就绪 + 能力路由 | 需绑定外部搜索供应商（计费、配额、合规各异），不宜由网关内置默认实现 | 400 `unsupported_tool` | R-P1-39/41/44/45/46 |
| 6 | **File search + vector store** | 🟡 框架就绪 + 能力路由 | 需 embedding 模型 + ANN 索引 + 文件存储，是独立子系统，工作量超过 v3 全部其余需求之和 | 400 `unsupported_tool` | 同上 |
| 7 | **Computer use** | 🟡 框架就绪 + 能力路由 | 需 VM/浏览器农场，且存在最高等级的安全与合规风险（远程控制），不适合与 API 网关同进程 | 400 `unsupported_tool` | 同上 |
| 8 | **Code Interpreter** | 🟡 框架就绪 + 能力路由 | 需强隔离沙箱（容器/gVisor），本地/VPS 部署形态下无法保证逃逸防护 | 400 `unsupported_tool` | 同上 |
| 9 | **Image generation** | 🟡 框架就绪 + 能力路由 | 需图像模型与产物存储；作为 hosted tool 语义与 Images API 不同，本地模拟难以证明等价 | 400 `unsupported_tool` | 同上 |
| 10 | **Tool search** | 🟡 框架就绪 + 能力路由 | 官方能力较新、schema 变动风险高，registry 预留但不内置执行 | 400 `unsupported_tool` | 同上 |
| 11 | 本地 Code Interpreter 沙箱执行器 | 🔴 明确不做 | 见 #8；安全边界超出网关职责，应作为独立服务由 MCP 或原生上游提供 | — | — |
| 12 | 本地 Computer use 执行器 | 🔴 明确不做 | 见 #7 | — | — |
| 13 | 本地 vector store 实现 | 🔴 明确不做 | 见 #6；建议通过 MCP 接入外部向量库 | — | — |
| 14 | Go / Java / .NET SDK 合约测试 | 🔴 明确不做 | §4.2.10 列了 5 种 SDK。Python 与 TypeScript 覆盖 >90% 实际使用量且 CI 成本可控；三语言额外 CI 基础设施投入产出比过低 | 兼容报告中标注为「未验证」 | R-P1-49 |
| 15 | 多实例 SQLite 部署 | 🔴 明确不做 | §13.1-P1-13 已定调 SQLite = 单实例；多实例强制 TiDB/MySQL | 启动时检测多实例配置 + SQLite → 报错 | R-P1-64 |

**裁定汇总**：完整实现 4 项 / 框架就绪 6 项 / 明确不做 5 项。

### 3.4 对「完全兼容」宣称的约束

本次交付**可以**宣称：
- ✅ OpenAI 官方 SDK 无需供应商特定代码即可完成全部 6 个资源端点操作
- ✅ 全量正式请求参数、input/output item、streaming event 均被正确处理
- ✅ 配置原生 Responses 上游时，全部 7 类官方 tool 端到端可用

本次交付**不得**宣称：
- ❌ 「zhongzhuan 内置全部 hosted tool 执行器」
- ❌ 在无任何原生 Responses 上游的纯 Chat/Anthropic 部署下「完全兼容」

兼容报告（R-P1-50）必须对每项标注四态之一：`通过` / `直通可用` / `无执行器-标准错误` / `明确不做`。

---

## 4. 待确认问题清单（含建议默认值）

> 全部问题均已给出建议答案，未收到反对意见即按建议值执行。

### Q1 · `reasoning_event_mode` 默认值

| 项 | 内容 |
|---|---|
| 矛盾点 | §5.2 说「默认值由 Codex 实际兼容性测试确定」，§15 配置样例已写死 `reasoning_summary_text` |
| **建议默认值** | **`reasoning_summary_text`** |
| 理由 | ① §15 已给出显式值，取之消除歧义；② `response.reasoning_summary_text.delta` 是较早稳定的官方事件，老版本 Codex 兼容性更好；③ `reasoning_text` 为较新事件，老客户端可能无法反序列化，风险不对称（选错 summary 最多显示粒度差异，选错 text 可能直接解析失败） |
| 附带裁定 | 三档配置化能力仍必须实现（R-P1-05）；建议在 Phase 3 集成测试中对 Codex CLI 实测后写入兼容报告；DeepSeek 等强推理上游建议保持 summary 模式以控制下游事件量 |

### Q2 · 兼容模式 vs 严格模式默认

| 项 | 内容 |
|---|---|
| 出处 | §7.3「默认采用 Codex 兼容模式」、§15 `compatibility_terminal_event: completed` |
| **建议默认值** | **兼容模式**（`compatibility_terminal_event: completed`），严格模式经 `responses_bridge.stream.strict_terminal: true` 开启 |
| 理由 | Codex 收到 `response.failed` 会中止整个会话；兼容模式下发 completed 可让会话继续，同时通过 `terminal_reason` + `incomplete_details` 保留可诊断性 |
| **附带硬约束（重要）** | 兼容模式下**必须**写入 `terminal_reason` 与 `incomplete_details`，否则等同于「伪造成功」，违反 §4.2.1 第 4 条。此约束写入 R-P1-22 验收，不达标视为 P0 缺陷 |
| 建议启用严格模式的场景 | 非 Codex 的官方 SDK 客户端、differential test、CI 环境 |

### Q3 · ResponseStore 在 SQLite 单实例下的容量与 TTL 默认

| 对象 | 建议 TTL | 建议容量上限 | 理由 |
|---|---|---|---|
| `responses` | 30 天 | 200,000 行 | 对齐主流 store 语义预期；本地部署场景 30 天足够回溯 |
| `response_input_items` | 随父 response 联动 | — | 避免孤儿数据 |
| `response_event_log` | **7 天** | 5,000,000 行 | catch-up 只需覆盖 background 最大墙钟(900s) + 客户端重连窗口，7 天有巨大冗余，而事件行数是最大体积来源 |
| `background_jobs` | 30 天 | 10,000 行 | 与 responses 对齐 |
| `tool_executions` | **90 天** | 500,000 行 | 审计属性，保留期应长于业务数据 |
| `idempotency_records` | **24 小时** | 100,000 行 | 只需覆盖客户端重试窗口 |
| `debug_capture` | 24 小时（§15 已定） | 按大小上限 | 沿用文档 |

**附加建议**：
- SQLite DB 文件软上限 **8 GB**；超过触发按 TTL 提前回收 + 磁盘水位告警（挂接 R-P2-18）
- 定位声明：**SQLite = 单实例 / 本地 / 小团队**；`global_concurrent > 32` 或检测到多实例配置时启动告警并建议迁移 TiDB
- 上述数值全部配置化，以上为 default

### Q4 · hosted tools 无执行器时的具体错误语义

**建议采用「请求期拒绝优先」策略**：

| 判定时机 | 建议行为 |
|---|---|
| **请求校验阶段**（未连上游、未发任何 delta，可静态判定） | 直接返回 **HTTP 400**，不开启 SSE。与 OpenAI 官方在 create 时即报不支持 tool 的行为一致 |
| **上游中途返回不可执行的 hosted tool call**（运行期才暴露） | 走 SSE 路径：已开 item 安全关闭 → `response.incomplete`（严格模式）或 `response.completed` + `incomplete_details`（兼容模式）→ `[DONE]`，`terminal_reason=capability_route_unavailable` |

**建议错误体（官方 Responses error 格式）**：

```json
{
  "error": {
    "type": "invalid_request_error",
    "code": "unsupported_tool",
    "param": "tools[2].type",
    "message": "Tool type 'code_interpreter' is not supported by the selected model/upstream route. Configure an upstream with capability 'code_interpreter' (upstream_mode: responses_native)."
  }
}
```

**建议新增 2 类错误分类**（扩展 §10 的 12 类为 14 类，纳入 R-P1-51）：
- `unsupported_tool_capability` — 请求的 tool 无任何路由可承载
- `capability_route_unavailable` — 有声明该能力的路由但当前不可用（熔断/限流/宕机）

**启动期行为建议**：

| 配置 | 建议行为 |
|---|---|
| `hosted_tools_enabled: true` 且存在能力缺口 | 启动日志 WARN 打印缺口清单：`capability gap: code_interpreter, computer (no upstream declares capability)` |
| 生产模式 + `strict_capability_startup: true`（**建议生产默认 true**） | **fail closed**，拒绝启动，避免上线后才发现 |
| 开发模式 | 仅 WARN，允许启动 |

### Q5 · §2.1「完整实现全部端点」 vs §4.2.2「不得继续对 retrieve/delete 返回 405」

| 项 | 内容 |
|---|---|
| 是否矛盾 | **不矛盾**。§2.1 是目标陈述，§4.2.2 是对现状（已核实 `handler.py:387` 对非 POST 一律 405）的整改要求，两者同向 |
| **建议裁定** | **确认真实实现全部 6 个端点**（R-P1-28），移除 405 兜底 |
| `compact` 端点的特别说明 | 该端点在官方 API 中相对较新。落地口径：实现端点 + 生成 compacted item 作为状态链新边界（§9.4 已要求），语义以**兼容报告中锁定的目标 OpenAI API 版本**为准；若目标版本 schema 后续变更，通过 registry 版本化机制升级，不算破坏性变更 |

### Q6 · §2.2 双重否定表述导致的误读风险（新增）

| 项 | 内容 |
|---|---|
| 问题 | §2.2「以下**不再属于** v3 **非目标**」后接 6 项，双重否定，实际含义是「这 6 项都是 v3 的目标」。开发极易误读为「这 6 项不做」 |
| **建议** | 在需求池中已固化为正向表述（R-P1-28~44）。建议架构设计文档同样使用正向表述，并在此处标注：**Responses 状态存储、previous_response_id、6 个资源操作、background、hosted tools、全量 streaming event —— 全部是 v3 交付目标** |

### Q7 · `reasoning` 参数的嵌套 → 扁平映射规则（新增）

| 项 | 内容 |
|---|---|
| 问题 | §5.1 allowlist 含扁平的 `reasoning_effort`，但 Responses 的官方形态是嵌套对象 `reasoning: {effort, summary}`；同时 §5.1 又把 `text` 列入「默认消费或丢弃」，而 `text.format` 承载 structured outputs 语义（§4.2.7 要求支持） |
| **建议裁定** | ① `reasoning.effort` → Chat `reasoning_effort`，**仅当**上游 `PROVIDER_CAPABILITIES` 声明支持时写入；② `reasoning.summary` **不发上游**，只决定下游 reasoning 事件方言（联动 Q1）；③ `text.format` **不是**简单丢弃，而是「消费后转换」为 Chat `response_format`（已在 allowlist 内）；`text.verbosity` 在上游不支持时丢弃并计数 |
| 影响 | 修正 R-P1-03 的实现口径：「消费或丢弃」中的 `text` 属于**消费**（转换后写入 response_format），不是纯丢弃。已在 R-P1-41 两步处理中体现 |

### Q8 · ExecutionBudget 墙钟 vs §8 total_timeout 的冲突（新增）

| 项 | 内容 |
|---|---|
| 冲突 | §8 允许 `total_timeout = 900s 或关闭硬限制`；§9.4 要求 `ExecutionBudget.max_wall_time_seconds = 900` 作为有限终止基石。若 total_timeout 被关闭，预算是否也失效？ |
| **建议裁定** | **两者分离且语义不同**：`total_timeout` 是 **HTTP 客户端层**对单次上游连接的限制，可关闭；`ExecutionBudget.max_wall_time_seconds` 是 **response 生命周期层**的硬上限，**不可关闭**（配置为 0 或 null 时启动报错） |
| 建议默认值 | 同步 response：900s；background task：3600s（单独配置项 `background.max_wall_time_seconds`） |
| 理由 | 有限终止原则（§9.4）是 v3 的核心承诺，不能被一个 HTTP 层配置项旁路 |

### Q9 · `response.queued` 事件的发送时机（新增）

| 项 | 内容 |
|---|---|
| 问题 | 铁律 3（§3.3）的事件序列以 `response.created` 开头，未包含 `queued`；但 §4.2.8 要求覆盖 `queued` 事件 |
| **建议裁定** | `response.queued` **仅在 `background=true` 时**发出，位于 `created` 之前；同步流式请求不发 `queued`，§3.3 序列保持不变 |
| 影响 | R-P1-20/21 的 golden 序列不含 queued；R-P1-34 的 background 序列以 queued 开头 |

### Q10 · 特性开关的配置键与优先级（新增）

| 项 | 内容 |
|---|---|
| 不一致 | §4.1 使用 YAML `responses_bridge.{version,enabled}`；§14 Phase 4 使用环境变量 `RESPONSES_BRIDGE_V3` |
| **建议裁定** | 以 YAML 为唯一权威 schema；环境变量 `ZHONGZHUAN_RESPONSES_BRIDGE_V3=1/0` 作为覆盖项。优先级：**env > YAML > 默认** |
| **建议默认值** | 本次交付默认 **`enabled: false`**（灰度开启）；Phase 4 观测达标后由运维显式切换为 true，并保留一个版本周期回滚能力 |
| 理由 | 默认关闭符合 §4.1「保留快速回滚能力」，也让 v3 可以先合入 main 而不影响存量用户 |

### Q11 · §13.1-P0-2 现状核验发现（新增，可减工时）

| 项 | 内容 |
|---|---|
| 文档描述 | 「`cleanup_old_logs()` 中混入统计逻辑，存在未定义的 `since`、`daily_rows`、`days`，DELETE SQL 错误包含 `GROUP BY day ORDER BY day`」 |
| **实地核验** | 当前 `src/zhongzhuan/store/logs.py:103-105` 的 `cleanup_old_logs()` 已是干净实现（`DELETE FROM request_logs WHERE ts<?`），`get_usage_stats()` 已在 108 行独立定义，`since`/`daily_rows` 均有定义。**该缺陷似已在文档撰写后被修复** |
| **建议裁定** | R-P0-03 从「修复损坏代码」**降级为「回归确认 + 补齐 SQLite/TiDB 双后端集成测试 + 纳入 CI 导入检查」**。预计节省 0.5 人日，但测试补齐不可省（文档要求的测试保护仍缺失） |
| 其余 P0 核验结论 | P0-1（30s 超时）✅ 属实 · P0-3（迁移吞异常）✅ 属实 · P0-4（`schema.py:120 token TEXT NOT NULL UNIQUE` 明文）✅ 属实 · P0-5（pyproject 4 依赖 vs requirements 7，且 `readme="README.md"` 但文件不存在）✅ 属实 · P0-6（10 张表无 responses/event_log/background_jobs）✅ 属实 · §4.2.2（`handler.py:387` 非 POST 一律 405）✅ 属实 |

---

## 5. 分阶段交付计划

### 5.1 阶段划分（对 §14 的补充）

> **产品判断（重要）**：文档 §14 的 Phase 0–4 **完全没有覆盖 §4.2 的全量兼容工作**（6 个端点、ResponseStore、background、item/event registry、CapabilityRouter、原生直通），而这部分是 P1 中最大的一块（23 条）。这是原文档的结构缺口。本文档**新增 Phase 2.5「Responses 资源与状态层」**予以填补。

| Phase | 名称 | 范围（需求 ID） | 条数 | 发布门槛 |
|---|---|---|---|---|
| **0** | 全仓稳定化 | R-P0-01~11、R-P0-26 | 12 | 现有功能全绿、无已知 P0；Chat/Anthropic 旧路径 golden fixture 逐字节一致 |
| **1** | 循环阻断与参数安全 | R-P0-12~14、19、20、22~25、35；R-P1-01~03、15~19 | 19 | **抓包证明历史 reasoning 无法出现在任何上游 payload**；未知字段零透传 |
| **2** | 流式状态机与工具聚合 | R-P0-15~18、31、33；R-P1-04~14、20、21、65~68 | 25 | 随机分片（Hypothesis ≥200 examples）与并行工具调用测试全部通过；output index 全局唯一；sequence 严格单调 |
| **2.5** ⭐ | Responses 资源与状态层（**新增**） | R-P0-10（深化）、27~30、32、34；R-P1-28~48 | 28 | 6 端点通过 OpenAI Python SDK 直调；store/previous_response_id/background 多轮 + 重启恢复通过；任意故障注入下 response 有限终止 |
| **3** | 超时、心跳与可观测性 | R-P0-21；R-P1-22~27、51~56、69、70 | 16 | 120s 首 token 测试、上游断流测试、100 轮会话测试通过；13 指标可定位每次异常终止原因 |
| **3.5** | 上线前加固（与 3 并行） | R-P2-01~18 | 18 | 公网部署安全基线达标；CI 9 项 required check 全绿 |
| **4** | 灰度与默认启用 | R-P0-25（灰度验证）、R-P1-49、R-P1-50 | 3 | v2/v3 差异记录无回归；循环率/截断率/首 token/工具失败率达标；兼容报告生成 |

（Phase 间需求数合计 121，另 R-P0-10 与 R-P0-25 跨阶段重复计数）

### 5.2 本次交付覆盖范围

| Phase | 本次交付 | 说明 |
|---|---|---|
| Phase 0 | ✅ 完整交付 | 地基，不可省 |
| Phase 1 | ✅ 完整交付 | 五条铁律核心 |
| Phase 2 | ✅ 完整交付 | 流式正确性核心 |
| Phase 2.5 | ✅ 完整交付（按 §3 裁定口径） | hosted tools 按「框架就绪 + 能力路由」交付；本地执行器不做 |
| Phase 3 | ✅ 完整交付 | 可观测性与长稳 |
| Phase 3.5 | ✅ 完整交付 | 公网部署前置 |
| Phase 4 | 🟡 **机制就绪，默认关闭** | 灰度开关、差异记录、观测面板全部实现并可用；**默认 `enabled: false`**，切默认由运维在观测达标后执行（见 §4-Q10） |

**一句话**：本次交付覆盖 **Phase 0 → Phase 3.5 全量 + Phase 4 的机制**，交付物为「可灰度、可回滚、可宣称 OpenAI Responses 协议全量兼容（能力可用性以配置的上游为准）」的 v3。

### 5.3 阶段依赖关系

```
Phase 0 ─────┬──> Phase 1 ──> Phase 2 ──┬──> Phase 2.5 ──> Phase 3 ──> Phase 4
             │                          │
             └──> Phase 3.5 (可与 1/2/2.5 并行) ─────────────────────────┘
```

关键路径：**Phase 0 → 1 → 2 → 2.5 → 3 → 4**。Phase 3.5 无阻塞依赖，建议并行以免成为上线瓶颈。

---

## 6. 验收清单（§16 的 18 条测试化）

> 全部 18 条均为「默认启用 + 宣称全量兼容」的 NO-GO 门槛。勾选需附测试用例 ID 与执行记录。

| # | 验收条目 | 满足需求 ID | 验证方式 | 阶段 | ☐ |
|---|---|---|---|---|---|
| 1 | 抓包证明下一轮上游 payload 不含上一轮 reasoning 文本或 encrypted content | R-P0-12、13、14 | 集成抓包用例 `test_no_reasoning_replay_e2e`（3 轮会话，逐字节 grep 上游 body）+ 单测 4 种 reasoning 形态 | P1 | ☐ |
| 2 | 所有未知 Responses 字段均不会进入上游 payload | R-P0-19、20；R-P1-02、03、41 | 单测：注入 20 个虚构字段 + 13 个 Responses 专有字段，断言上游 key 集合 ⊆ allowlist | P1 | ☐ |
| 3 | DeepSeek 工具 arguments 任意分片后可完整重组 | R-P0-15；R-P1-06、09、11 | Hypothesis 属性测试 `test_tool_args_arbitrary_split`（≥200 examples，含 1 字节分片） | P2 | ☐ |
| 4 | 两个及以上并行工具调用不会混流 | R-P1-06、07、48 | 单测 `test_parallel_tool_calls_interleaved`（2/3 工具交错分片 + 同 index 异 call_id 异常上游） | P2 | ☐ |
| 5 | 每个 Responses output item 有唯一 output index | R-P1-10 | 单测 `test_output_index_globally_unique`（reasoning + 2 工具 + message 混合流，断言 index 集合无重复） | P2 | ☐ |
| 6 | 所有流都先 created，后 completed/failed，最后 `[DONE]` | R-P0-17、18；R-P1-13、20、21、43 | 全量流式用例统一断言器 `assert_lifecycle()`；含首 chunk 前断流、usage-only、空 choices、无 `[DONE]` 四种边界 | P2 | ☐ |
| 7 | 上游首 token 延迟 120 秒时连接保持正常 | R-P0-01、02、21；R-P1-27 | 集成 `test_slow_first_token_120s`（mock 上游延迟 120s，断言成功 + ≥7 heartbeat + 无压缩头） | P3 | ☐ |
| 8 | 上游断流不会触发无限重放 | R-P0-30；R-P1-22、23 | 单测：delta 后断流断言无第二次上游请求；同位置反复断流断言 circuit open | P2.5 | ☐ |
| 9 | 工具调用不会因重复 chunk 执行两次 | R-P0-16、33 | 单测 `test_duplicate_tool_chunks_idempotent`（同一 call_id 完整分片流重放 2 遍，断言 1 个完成 item） | P2 | ☐ |
| 10 | 客户端断开不会错误惩罚上游 key | R-P1-24、25、58 | 单测：取消后断言 key_health 各维度不变；请求侧 4xx 同样不惩罚 | P3 | ☐ |
| 11 | 100 轮连续会话无 reasoning 回灌、无循环、无持续内存增长 | R-P0-12、27、28；R-P1-04、70 | 长稳 nightly `test_100_turn_session`（断言 RSS 增长 <10%、reasoning grep 零命中、无未终止 response） | P3 | ☐ |
| 12 | 新增指标可以定位每次异常终止的具体原因 | R-P0-32；R-P1-51、53、54 | 单测：10 类熔断原因 + 14 类错误分类各触发一次，断言指标与日志 `terminal_reason` 可反查 | P3 | ☐ |
| 13 | OpenAI 官方 SDK 可直接完成 create/retrieve/delete/cancel/compact/input_items | R-P1-28、30、32、49 | CI job `sdk-contract`：Python + TypeScript SDK 各跑 6 端点，零供应商特定代码 | P2.5 | ☐ |
| 14 | `store`、`previous_response_id` 和 background mode 通过多轮及重启恢复测试 | R-P1-29~31、34~38；R-P0-29 | 集成：3 轮 store 会话 + previous_response_id 续接 + background kill -9 重启恢复 + 状态链防环 4 例 | P2.5 | ☐ |
| 15 | Function、web search、file search、computer use、Code Interpreter、image generation、remote MCP 均通过能力测试 | R-P1-39、44、45、46、47 | **按 §3.2 双轨口径**：轨道 A（配原生上游，7 类端到端）+ 轨道 B（无能力上游，7 类均返回标准 400 且启动期告警）。轨道 C 不在本次范围 | P2.5 | ☐ |
| 16 | 全部正式 Responses output item 和 streaming event 均通过 schema 与生命周期测试 | R-P1-39、42、14 | 每类 item / event 族一个 fixture；OpenAI SDK 反序列化零异常；added-delta-done 生命周期断言器 | P2.5 | ☐ |
| 17 | 原生 Responses 上游请求不会被错误降级到 Chat Completions | R-P1-44、45 | 集成：配 `upstream_mode: responses_native` 后抓包断言请求路径为 `/v1/responses`、output item 未被改写 | P2.5 | ☐ |
| 18 | 已生成版本化 OpenAI Responses 全量兼容报告，且不存在未支持项 | R-P1-50 | 报告文件产出 + CI 覆盖度校验。**口径修订（需拍板）**：允许状态为 `直通可用` 与 `无执行器-标准错误`；`明确不做` 项（§3.3 #11~15 共 5 项）需 team-lead 签字豁免 | P4 | ☐ |

### 6.1 附加验收（§4.1 旧协议回归门槛，与 §16 同级 NO-GO）

| # | 验收条目 | 满足需求 ID | 验证方式 | ☐ |
|---|---|---|---|---|
| A1 | Chat → Chat 流式/非流式/文本/工具调用/错误响应行为不变 | R-P0-23、24、26 | golden fixture 字节级对比 ≥4 条 | ☐ |
| A2 | Chat → Anthropic 请求与回程转换行为不变 | R-P0-23、24、26 | golden fixture ≥4 条 | ☐ |
| A3 | Anthropic → Chat 请求与回程转换行为不变 | R-P0-23、24、26 | golden fixture ≥4 条 | ☐ |
| A4 | 未启用 v3 时现有测试输出保持一致 | R-P0-25 | 开关关闭下跑全量存量测试（30 个测试文件）零修改通过 | ☐ |
| A5 | 共享组件（timeout / 客户端取消 / 连接池）修改后旧协议单独回归 | R-P0-01、24；R-P1-24 | 旧协议专项回归 job | ☐ |

### 6.2 §9.4 防循环验收（7 条，与 §16 同级）

| # | 验收条目 | 满足需求 ID | 验证方式 | ☐ |
|---|---|---|---|---|
| B1 | 单 response 在任意故障注入下都能在预算内终止 | R-P0-27、32 | 故障注入矩阵（6 类预算 × 4 类故障） | ☐ |
| B2 | 自引用和多节点 response 环均被拒绝 | R-P0-29 | 单测：自引用、A→B→A、A→B→C→A | ☐ |
| B3 | 相同工具调用重复三次以内触发熔断 | R-P0-28 | 单测：连续 3 次相同 name+arguments | ☐ |
| B4 | 工具持续返回相同错误时不会无限重试 | R-P0-28 | 单测：`repeated_tool_failure` 熔断 | ☐ |
| B5 | worker 重启不会重复执行已完成的副作用工具 | R-P1-37、47 | 集成：kill -9 后重启，断言副作用执行次数 =1 | ☐ |
| B6 | 流中途断开不会导致 Codex 无限重放 | R-P0-30、31 | 集成：断流后断言无自动重试 + 迟发事件被拒 | ☐ |
| B7 | 长稳测试中不存在持续增长且无终止状态的 response/background task | R-P1-70、38 | nightly：断言 `status IN ('queued','in_progress')` 且超 TTL 的行数 =0 | ☐ |

**验收总计：18（§16）+ 5（§4.1）+ 7（§9.4）= 30 条**

---

## 7. 交付物清单

| # | 交付物 | 责任角色 | 依据 |
|---|---|---|---|
| 1 | 本文档《需求池与交付范围说明》 | 产品经理 | — |
| 2 | 架构设计文档（模块边界、数据模型、状态机、接口契约） | 架构师 | 本文档 §2 需求池 |
| 3 | 7 个新增协议模块 + handler 9 组件拆分 | 开发 | R-P1-57、66 |
| 4 | ResponseStore 6 张表 + 迁移脚本 | 开发 | R-P0-10 |
| 5 | 测试套件（单元 17 项 + 属性 5 类 + 集成 9 项 + 长稳 4 项 + golden fixture ≥12 条） | 测试 | R-P1-67~70、R-P0-26 |
| 6 | 版本化 OpenAI Responses 兼容报告 | 开发 + 测试 | R-P1-50 |
| 7 | CI 9 项 required check 配置 | 开发 | R-P2-13 |
| 8 | 部署文档更新（反代超时、SQLite/TiDB 选型、能力配置、灰度操作手册） | 开发 | R-P0-21、R-P1-44、R-P0-25 |

---

## 8. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v1.0 | — | 首版。基于开发文档 1322 行提取 123 条需求；完成 hosted tools 15 项范围裁定；提出 11 个待确认问题及建议默认值；新增 Phase 2.5 填补 §14 结构缺口；§16 18 条转 30 条可勾选验收 | 许清楚 |
