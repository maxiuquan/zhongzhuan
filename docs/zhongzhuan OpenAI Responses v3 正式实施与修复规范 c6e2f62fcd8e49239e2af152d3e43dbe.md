# zhongzhuan OpenAI Responses v3 正式实施与修复规范

<aside>
🎯

**正式决策：**v3 的触发条件是**下游入站协议为 OpenAI Responses API**，不是客户端额外声明某个“v3 profile”。例如 ChatGPTWork（Codex）请求 `POST /v1/responses` 时，必须进入 Responses v3。Chat Completions 与 Anthropic Messages 保持旧链路，不受影响。

</aside>

## 1. 范围、路由与最终行为

### 1.1 入站协议决定实现版本

路由只以已经存在的协议识别结果决定：

```python
if inbound_protocol == "responses":
    return await handle_responses_v3(...)

return await handle_legacy_protocol(...)
```

具体要求：

- `POST /v1/responses`、Responses resource 端点和 Codex/ChatGPTWork 的 Responses 流量：**正常情况下全部进入 v3**。
- `POST /v1/chat/completions`：继续走既有 Chat 路径。
- `POST /v1/messages`：继续走既有 Anthropic 路径。
- 不依据 User-Agent、模型名、请求头或客户端品牌猜测协议；URL、HTTP method 与既有 protocol detector 是唯一入口事实源。
- 不增加自定义 profile header，不要求 Codex/ChatGPTWork/官方 OpenAI SDK 修改请求。

### 1.2 v2 的唯一角色：紧急、全局、可审计回滚

v2 不是按单请求的能力回退目标。它仅用于 v3 出现发布事故时的紧急兼容回滚：

```bash
ZHONGZHUAN_RESPONSES_BRIDGE_V3=0
```

规则：

- 该开关关闭后，所有新入站 Responses 请求统一走旧 v2 Responses 链路。
- 开关开启后，所有新入站 Responses 请求统一走 v3；不得因为 `stream`、工具、background、`previous_response_id` 或某项参数未完成就悄悄切回 v2。
- v3 未具备安全语义的正式能力必须返回标准 400/501/503 或由原生 Responses 上游直通；不得删除字段、返回 JSON skeleton 冒充 SSE，或对单请求静默走 v2。
- 已开始的请求固定在其起始版本；已经写出任何 HTTP body 或 SSE frame 后绝不跨版本迁移。
- 每次环境开关、管理端回滚和恢复都必须写入审计日志，记录操作者、时间、原因和生效版本。

这一规则确保 ChatGPTWork（Codex）等 Responses 客户端的行为稳定、可定位，不会在一次会话中随机混用两套实现。

## 2. Responses v3 的协议铁律

### 铁律 1：reasoning 只出不进

- 当轮上游模型返回的 `reasoning_content` 只转换为当轮 Responses SSE 展示事件。
- 任何历史 `input[].type = reasoning`、summary、content 或 encrypted content 都不得写回 Chat/Anthropic 上游历史。
- ResponseStore 可以保存必要的脱敏元数据以维持 API 对象完整性，但不保存或重放 reasoning 明文。

### 铁律 2：工具调用完整后才能完成

- `name`、`call_id` 和 `arguments` 按稳定内部调用对象累积，支持任意字节分片与并行交错。
- 仅在明确完成信号后执行 `validate_arguments(require_object=True)`。
- JSON 不完整、顶层不是 object、name 缺失或调用身份冲突时：绝不发送 `response.function_call_arguments.done`，该 item 标为 `incomplete`，response 进入可诊断的失败/残缺终止。
- 禁止将空或残缺 arguments 自动改写为 `{}` 后标为 completed。

### 铁律 3：SSE 生命周期完整且唯一

```
response.created
response.in_progress
零个或多个 output item / content part / delta 事件
response.completed | response.failed | response.incomplete | response.cancelled
data: [DONE]
```

- `created` 和 `in_progress` 在下游 SSE 连接建立时立即发出，不等待上游首 token。
- 每个 item 的 added/done、content part 的 added/done、正式终止事件和 `[DONE]` 均必须幂等且只出现一次。
- terminal 状态后拒绝迟发 delta、迟发工具分片和重复 `[DONE]`。

### 铁律 4：未知参数只在确认无语义后丢弃

- v3 先以版本化 Responses schema 验证官方字段，再按 native、local executor 或 translate 的能力决定直通、消费、实现或标准错误。
- 只有真正未知的非官方字段可静默丢弃并计数。
- 已知但尚未实现的正式能力不是“未知字段”；必须明确错误或选择支持它的原生 Responses 上游。

### 铁律 5：长推理与有限终止

- first-token 和 read-idle timeout 不低于 300 秒；推荐总时间上限 900 秒。
- response、background task、工具轮数、token、输出、存储和外部调用均必须有预算。
- 任意异常都必须在有限时间内进入 completed、failed、incomplete 或 cancelled，绝不无限重放、无限工具重试或无限状态链恢复。

## 3. 必须修复的 P0 发布阻断项

以下修复是 Responses v3 正式启用的前置条件，不是后续优化项。

### P0-1：把真实 `stream=true` 接入 v3 生产路径

当前问题：v3 `stream=true` 仍可能返回 resource skeleton/in-progress JSON，而非真实 SSE；这对 Codex/ChatGPTWork 是协议退化。

修复：

```
HTTP Responses request
→ v3 handler
→ capability route / scheduler / upstream stream
→ SSEParser
→ TurnAccumulator + ToolCallAccumulator
→ ResponsesEventEmitter
→ EventLog / ResponseStore
→ aiohttp SSE response
```

要求：

- `stream=true` 必须设置 `Content-Type: text/event-stream`，不是 JSON。
- v3 handler 必须成为真实上游流、emitter 和客户端写入的唯一编排者。
- `response_id` 在请求开始时生成一次，贯穿 upstream、event log、catch-up、retrieve、cancel 和 terminal persistence。
- ResponsePipeline 与旧 `ResponsesStreamTranslator` 只能保留一个生产事件所有者；禁止两个组件各自输出 lifecycle 事件。

### P0-2：区分正常完成与异常 EOF

当前风险：ResponsePipeline 若将所有产生过 chunk 的上游结束都判为 `upstream_truncated`，正常模型完成也会被错误终止。

修复：

```python
saw_provider_finish = False
saw_provider_done = False

# provider 的 finish_reason、[DONE] 或原生 terminal event 任一成立
# 才把随后的 EOF 判为正常 completed。
if upstream_eof and not (saw_provider_finish or saw_provider_done):
    terminal_reason = UPSTREAM_TRUNCATED
```

必须覆盖：正常文本、正常 tool call、usage-only 尾包、`[DONE]` 后 EOF、无完成信号 EOF、首 token 前断流、原生 Responses terminal event 后 EOF。

### P0-3：修复工具参数验证与完成顺序

当前风险：bridge 收尾若直接发 `arguments.done` 与 completed item，会将非法 JSON 变成可执行工具调用。

修复顺序：

1. 收到明确 provider finish/tool done。
2. 对 accumulator 执行严格 JSON object 验证。
3. 通过后发送 `function_call_arguments.done`。
4. 再发送 `output_item.done(status=completed)`。
5. 未通过则只发送 `output_item.done(status=incomplete)`，并以 `invalid_tool_arguments` 或 `upstream_truncated` 终止；绝不发送 arguments.done。

### P0-4：稳定 tool item 身份，支持分片 name 与迟到 call ID

当前风险：以可变 `call_id` 动态计算 item ID，或只 `replace_name()`，会使 added、delta、done 指向不同对象，或丢失分片函数名。

修复：

```python
@dataclass
class ToolCallAccumulator:
    source_index: int
    output_index: int
    item_id: str                 # 创建时固定，永不随 call_id 变化
    call_id: str | None
    name: str
    arguments: str
```

- `item_id` 由 `response_id + output_index` 在首次分片时固定。
- call ID 晚到时只更新 call-ID 映射，绝不更换 item ID。
- name 同时支持逐片 append 与“重复完整值替换”；根据 provider profile 或片段规则选择，不能无条件 replace。
- 同一 `call_id`、source index 或 stable item ID 只允许完成一次。
- 增加 name 按字符拆分、call ID 延迟出现、并行调用交错、重复分片和迟发分片的属性测试。

### P0-5：`previous_response_id` 必须真正进入上游上下文

当前风险：只校验和持久化状态链，却没有把恢复结果写入 outbound input，会产生“API 看似续接、模型实际失忆”的多轮错误。

正确实现：

```python
resolution = await chain_resolver.resolve_chain(
    previous_response_id,
    workspace_id,
)
if not resolution.ok:
    return chain_error_response(resolution)

body_for_translation = sanitized_body.copy()
body_for_translation["input"] = build_upstream_input(
    resolution,
    current_input=sanitized_body.get("input"),
)
```

然后再将 `body_for_translation` 转为 Chat/Anthropic 消息。验证要求：

- 恢复根到父 response 的可见 message、function call、function output 顺序正确。
- 历史 reasoning、summary、encrypted reasoning 永不进入上游 payload。
- instructions 不从父 response 自动继承。
- 自引用、祖先环、跨租户、已删除 parent、超深、超 item/token 预算全部在网络请求前失败。

### P0-6：background 不能只回显字段

`background=true` 必须走真实的持久化 worker，而不能同步等待上游后才返回，或只在 response object 中回显 `background`。

最低要求：

```
queued → in_progress → completed | failed | incomplete | cancelled
```

- create 立即返回可 retrieve 的 response。
- worker 使用持久化 job、lease、heartbeat 与幂等 key。
- cancel 写入持久化取消标记，并取消上游/工具执行。
- 重启后仅从 checkpoint 恢复未完成任务一次，不得 crash-restart 循环或重复副作用。
- 每个 task 继承 response budget，并持续检查取消和预算。

## 4. 能力路由：不安全时标准错误，能原生支持时直通

Responses v3 内部可选择三种执行方式：

```
Native pass-through：上游原生支持 OpenAI Responses，保持正式 item/event 语义
Local execution：本地具备安全执行器与持久化的能力
Protocol translation：可证明语义等价的 Responses → Chat/Anthropic 转换
```

优先级为 Native → Local → Translate。若三者都不安全或不存在：

- 参数非法：400。
- 已知能力尚未实现：501。
- 已声明执行器但当前不可用：503。
- 绝不将该请求偷换为旧 v2 或删掉相关字段再发送。

## 5. 无限循环、重复执行与重试防线

### 5.1 状态链预算

- 禁止 self-reference 与祖先环。
- 最大祖先深度默认 64。
- 最大恢复 items 默认 2,000，最大恢复 token 默认 200,000。
- compact 完成后必须写入 compact boundary，防止无限回溯。

### 5.2 工具与 response 预算

```python
@dataclass
class ExecutionBudget:
    max_tool_rounds: int = 32
    max_calls_per_tool: int = 8
    max_identical_call_repeats: int = 2
    max_total_tool_calls: int = 64
    max_wall_time_seconds: int = 900
    max_output_tokens_total: int = 200_000
```

重复签名按 `tool_name + canonical_json(arguments) + normalized_result_or_error` 计算。相同调用/结果重复、同一工具持续失败、重试预算耗尽或超时都必须停止新工具调度并确定性终止。

### 5.3 重试边界

- 仅在未向下游提交任何业务事件前，允许切 key 或重试。
- 一旦发出文本、reasoning 或工具参数 delta，禁止透明重放。
- client disconnect 不得惩罚 upstream key。
- 同一 idempotency key、response ID 或工具副作用只能执行一次。

## 6. 超时、SSE 与可观测性

推荐默认值：

```yaml
responses_bridge:
  version: v3
  enabled: true
  timeout:
    connect_seconds: 15
    first_token_seconds: 300
    read_idle_seconds: 300
    total_seconds: 900
  stream:
    heartbeat_seconds: 15
    strict_terminal: false
  loop_guard:
    max_previous_response_depth: 64
    max_tool_rounds: 32
    max_total_tool_calls: 64
    max_identical_call_repeats: 2
    max_response_seconds: 900
    max_total_tokens: 200000
```

必须记录：

```
request_id
inbound_protocol
responses_implementation=v3|v2_emergency
route=native|local|translate
response_id
attempt
first_token_ms
duration_ms
terminal_reason
bytes_committed
tool_call_count
reasoning_history_items_dropped
```

指标至少包含：Responses 请求数、v3/v2 emergency 路由数、首 token、流时长、截断流、invalid tool arguments、重复 tool chunks、状态链环、预算熔断、取消和 heartbeat。

## 7. 测试与发布门槛

### 7.1 v3 默认启用前的强制测试

- [ ]  ChatGPTWork（Codex）真实 `POST /v1/responses` 非流式和流式测试均进入 v3。
- [ ]  OpenAI Python/TypeScript SDK Responses contract 测试通过。
- [ ]  流式真实 HTTP 输出严格满足完整 lifecycle，最后一帧为 `[DONE]`。
- [ ]  随机字节分片下文本、Unicode、tool name、call ID 和 arguments 的输出语义不变。
- [ ]  非法/截断 tool arguments 永不产生 `.done` 或 runnable call。
- [ ]  正常 EOF 不会成为 truncated；异常断流具备正确 terminal reason。
- [ ]  多轮 `previous_response_id` 已将链恢复内容注入真实 upstream payload，且 reasoning 永不出现。
- [ ]  background create/retrieve/cancel/restart 恢复通过。
- [ ]  自引用、链环、重复工具签名、工具失败、超时、预算耗尽均有限终止。
- [ ]  Chat → Chat、Chat ↔ Anthropic 的 golden fixture 字节级输出无变化。

### 7.2 发布与回滚

- v3 先在测试 token 和 ChatGPTWork（Codex）小流量实例启用。
- 观察 `terminal_reason`、截断率、首 token、工具 JSON 失败率、重复执行率、内存与 event log 增长。
- 任何 P0 指标异常时，用全局环境硬开关统一切回 v2；不得只对单个已开始请求回滚。
- 真机 Responses 流式、Hosted tools、MCP 与原生 Responses 上游验证完成后，更新兼容报告；在报告仍存在缺口时不得宣称“完全兼容 OpenAI Responses API”。

<aside>
🛡️

**发布 NO-GO：**stream=true 未进入真实 SSE、正常 EOF 被误判截断、残缺工具参数被 `.done`、tool item 身份不稳定、状态链未注入上游、background 仅字段回显、历史 reasoning 回灌、或任一异常路径可能无限循环时，Responses v3 均不得作为 Codex/ChatGPTWork 的正式默认实现。

</aside>