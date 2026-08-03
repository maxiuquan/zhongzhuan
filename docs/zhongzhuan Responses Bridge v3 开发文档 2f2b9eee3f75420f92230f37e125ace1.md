# zhongzhuan Responses Bridge v3 开发文档

<aside>
🎯

**版本目标：**将 Responses → Chat Completions/Anthropic 桥接层升级为可供 Codex 长时间稳定运行的防循环实现。v3 以协议正确性、流式工具调用完整性、确定性终止和故障可诊断性为第一优先级。

</aside>

## 1. 背景与问题定义

`zhongzhuan` 以 OpenAI Responses API 作为 Codex 入站协议，将请求转换为 Chat Completions，并根据上游类型继续转为 OpenAI Chat Completions 或 Anthropic Messages；回程再统一转换为 Responses SSE。

现有实现已具备基础请求转换、文本流、工具调用参数累积、usage 捕获和安全收尾，但仍存在以下发布阻断问题：

- 历史 `input[].type = reasoning` 会被重新写成 `reasoning_content`，可能造成推理内容回灌和重复循环。
- 请求转换采用复制原请求再删除少数字段的 denylist 方式，新增或未知 Responses 参数可能被错误透传上游并触发 400。
- 工具调用仅以局部 `index` 聚合，函数名未可靠累积，且 Responses `output_index` 与 message/reasoning 可能冲突。
- `response.created` 依赖上游首个有效 choice；首 token 前断流时可能直接进入 completed。
- 超时只有单一总值，缺少首 token、读空闲、连接、写入及反向代理层的统一约束。
- 异常流被统一标记 completed，缺少终止原因和残缺工具调用隔离。

## 2. v3 目标

### 2.1 必须达成

- 历史 reasoning 永不进入下一轮上游消息。
- 任意分片方式下，工具名称和 arguments 均能完整、只完成一次地重组。
- 每条下游流都具有确定、幂等且可验证的 Responses SSE 生命周期。
- 所有发往上游的参数均来自显式 allowlist 和上游能力声明。
- 深度推理模型在 60–120 秒首 token 延迟下保持连接，不因默认短超时触发重放。
- 客户端断开、上游断流、解析失败和代理超时均可区分、可观测、可回放。
- 保持现有多 key、熔断、计费、粘性会话和 Anthropic 双段翻译能力。
- 完整实现 OpenAI Responses API 对外协议，包括全部正式端点、请求字段、输入/输出 item、流式事件、状态管理、后台任务和官方工具类型。
- 对无法通过 Chat Completions 等价降级的能力，必须通过原生 Responses 上游直通、本地服务模拟或能力路由实现，不得静默丢失语义。

### 2.2 完整兼容定义

v3 的“完全兼容”是指客户端可使用 OpenAI 官方 SDK 按 Responses API 调用 `zhongzhuan`，无需编写供应商特定适配代码；所有官方 Responses 请求和事件均被正确处理、直通或模拟。

完整兼容不等于所有模型天然具备相同能力。模型或上游不支持某项能力时，CapabilityRouter 必须选择具备该能力的原生 Responses 上游或本地执行器；生产环境缺少必要执行器时必须在启动或配置阶段暴露能力缺口，不得在运行时假装成功。

以下不再属于 v3 非目标：

- Responses 服务端状态存储。
- `previous_response_id` 会话续接。
- retrieve、delete、cancel、compact 和 input items 等资源操作。
- background responses。
- hosted tools、remote MCP、图像生成、Code Interpreter 和 computer use。
- 全量 Responses streaming event 类型。

## 3. 五条协议铁律

### 铁律 1：reasoning 只出不进

模型当轮返回的 `reasoning_content` 只允许转换为当轮 Responses reasoning 展示事件。Codex 下一轮传回的任何 reasoning item，包括 summary、content、encrypted content，必须在请求净化阶段丢弃。

禁止：

- 历史 reasoning → `assistant.reasoning_content`
- 历史 reasoning → system/user 文本
- 历史 reasoning → tool 参数
- reasoning 参与粘性会话指纹
- reasoning 写入可重放的消息历史

### 铁律 2：工具调用完整后才能完成

工具调用的 `name`、`call_id` 和 `arguments` 都可能跨多个 SSE chunk 到达。桥接层必须在内存中聚合，在可靠结束信号到达后验证完整性，再发送 `.done`。

禁止：

- 因为中途 arguments 恰好是合法 JSON 就提前完成
- 将残缺 arguments 发送为完整 function call
- 同一调用重复发送 `.done`
- 只依赖 `index` 区分并行工具调用

### 铁律 3：SSE 生命周期完整

正常兼容模式下事件序列必须满足：

```
response.created
response.in_progress
零个或多个 output item 事件
response.completed
data: [DONE]
```

`response.created` 在下游连接建立后立即发送，不等待上游首 token。终止操作必须幂等。

### 铁律 4：未知参数静默丢弃

转换时只构造上游明确支持的字段。未知字段不得透传，不得导致 400；只允许写入 debug 日志和指标。

### 铁律 5：长推理链路超时不低于 300 秒

300 秒定义为首 token/读空闲下限，而不是唯一的请求总时长。代理、上游客户端和反向代理必须统一配置，并通过 SSE heartbeat 防止中间链路空闲断开。

## 4. 总体架构

```
Codex
  │ Responses request
  ▼
RequestSanitizer
  ├─ 丢弃历史 reasoning
  ├─ 参数 allowlist
  ├─ tool/schema/call_id 规范化
  └─ 生成标准内部请求
  ▼
ProtocolRouter
  ├─ Chat Completions upstream
  └─ Anthropic upstream
  ▼
UpstreamStreamParser
  ▼
TurnAccumulator
  ├─ TextAccumulator
  ├─ EphemeralReasoningAccumulator
  └─ ToolCallAccumulator[]
  ▼
ResponsesEventEmitter
  ▼
Codex
```

核心原则：解析、状态累积和事件生成必须分离。转换器不得同时承担 TCP 分帧、业务聚合、索引分配和终止策略。

## 4.1 向后兼容与改动隔离

<aside>
🔒

**兼容性原则：**只要把 v3 严格限定在 `inbound_protocol == "responses"`，保留旧转换器接口，并通过特性开关和全量回归测试上线，原有格式中转不应受到影响。

</aside>

v3 必须保持三条独立链路：

```
Chat Completions → Chat Completions：保留原样透传路径
Chat Completions ↔ Anthropic：继续使用现有转换器
Responses → Chat Completions/Anthropic：仅此链路启用 v3
```

实现约束：

- v3 的 RequestSanitizer、reasoning 丢弃、参数 allowlist、output index 分配和 Responses SSE 状态机只允许在 `inbound_protocol == "responses"` 分支执行。
- 历史 reasoning 丢弃仅针对 Responses 的 `input[].type = reasoning`；不得全局删除旧 Chat 请求中的 `reasoning_content`。
- Responses 参数 allowlist 不得应用于 `/v1/chat/completions` 同协议透传和 `/v1/messages` 原有路径。
- ResponsesEventEmitter 只用于 `/v1/responses`；旧 Chat SSE 和 Anthropic SSE 的事件名称、字段、顺序及终止方式保持不变。
- 保留 `ResponsesStreamTranslator`、`StreamO2A`、`StreamA2O` 的既有外部接口；内部可重构，但不得要求旧调用方同步迁移。
- call ID 归一化和稳定 ID 合成仅作用于 Responses 桥接，不得改写旧 Chat 客户端的原始 call ID。
- heartbeat 只写入 Responses SSE 流，不得进入非流式响应或改变旧协议负载。
- timeout、客户端取消和连接池等共享基础设施如有修改，必须单独完成旧协议回归测试。

路由边界必须明确：

```python
if inbound_protocol == "responses":
    return await handle_responses_v3(...)

return await handle_legacy_protocol(...)
```

不得将 v3 判断分散在多个旧协议分支中。`handle_legacy_protocol()` 在 v3 开关关闭或请求不是 Responses 时，必须保持现有行为。

特性开关要求：

```yaml
responses_bridge:
  version: v3
  enabled: true
```

- 支持按全局、model、group 或 key 灰度。
- 关闭开关后立即回退现有 Responses 实现，不影响 Chat/Anthropic 路径。
- v3 上线期间保留至少一个版本周期的快速回滚能力。

旧协议回归门槛：

- Chat → Chat：流式、非流式、文本、工具调用和错误响应行为不变。
- Chat → Anthropic：请求转换和回程转换行为不变。
- Anthropic → Chat：请求转换和回程转换行为不变。
- 未启用 v3 时，现有测试输出保持一致。
- 共享组件的修改必须增加 golden fixture 或字节级事件序列对比。
- 任何旧协议测试失败均视为 v3 发布 NO-GO。

## 4.2 OpenAI Responses API 全量兼容范围

<aside>
🌐

**v3 最终目标：**不仅兼容 Codex Responses Profile，还要兼容 OpenAI 官方 Responses API 全部正式能力。任何官方字段、资源端点、output item 或 SSE 事件缺失，均不能宣布“完全兼容”。

</aside>

### 4.2.1 实现策略：直通、模拟、转换

每个请求由 CapabilityRouter 选择一种执行模式：

```
Native Pass-through
  原生支持 Responses 的上游：尽量保持请求、事件和资源语义直通

Local Emulation
  状态、后台任务、资源生命周期和可本地执行工具：由 zhongzhuan 实现

Protocol Translation
  纯文本、函数工具等可无损降级能力：Responses ↔ Chat/Anthropic 转换
```

优先级：

1. 原生 Responses 直通。
2. 本地完整模拟。
3. 可证明语义等价的 Chat/Anthropic 转换。
4. 不允许静默降级、伪造成功或丢弃有语义的正式字段。

### 4.2.2 Responses 资源端点

必须实现并与官方 SDK 兼容：

```
POST   /v1/responses
GET    /v1/responses/{response_id}
DELETE /v1/responses/{response_id}
POST   /v1/responses/{response_id}/cancel
POST   /v1/responses/compact
GET    /v1/responses/{response_id}/input_items
```

如官方稳定 API 增加新的 Responses 资源方法，兼容矩阵必须同步更新。不得继续对 retrieve/delete 返回统一 405。

资源层新增 `ResponseStore`，至少持久化：

- response object、status 和生命周期时间。
- 原始净化请求与标准化 input items。
- output items 和 usage。
- previous response 关系。
- background task 状态和取消标记。
- 错误、incomplete details 和 terminal reason。
- 必要的 tool execution 状态；不得持久化原始私密 reasoning 文本。

### 4.2.3 会话状态

完整支持：

- `store=true/false`。
- `previous_response_id`。
- response retrieve/delete。
- input items 列举和分页。
- instructions 不自动继承等官方状态语义。
- 删除父 response 后的引用处理。
- workspace/租户隔离和访问控制。

`previous_response_id` 不得简单丢弃；桥接层必须从 ResponseStore 恢复可见上下文，并按照铁律过滤历史 reasoning 后再构造上游消息。

### 4.2.4 Background mode

实现完整后台状态机：

```
queued → in_progress → completed
                     → failed
                     → incomplete
                     → cancelled
```

要求：

- `background=true` 后立即返回可查询 response。
- 支持轮询 retrieve。
- 支持 cancel，且取消向上游传播。
- 支持后台任务完成后的 streaming catch-up；事件不得重复或乱序。
- 进程重启后任务状态可恢复或明确标记失败。
- 设置任务 TTL、并发上限和租户配额。

### 4.2.5 全量输入与输出 item

建立版本化 item registry，覆盖官方正式类型：

- message、input/output text、拒答和 annotations。
- input image、文件和其他正式多模态输入。
- reasoning、reasoning summary 和 encrypted reasoning 元数据。
- function call/function call output。
- web search call。
- file search call。
- computer call/computer call output。
- code interpreter call。
- image generation call。
- MCP call、approval request 和 approval response。
- 其他官方新增 item。

铁律 1 仍然有效：为了 API 对象完整性可以保存必要的 reasoning item 元数据或不透明标识，但不得将已消费的 reasoning 文本重新写入下一轮 Chat/Anthropic 历史。

### 4.2.6 官方工具能力

必须支持：

- Function calling。
- Web search。
- File search 与 vector store 集成。
- Computer use。
- Code Interpreter。
- Image generation。
- Remote MCP。
- Tool search。
- 工具审批和 approval 流程。
- 并行工具调用及 allowed tools/tool choice。

实现方式：

- 上游原生支持时优先直通。
- `zhongzhuan` 具备本地执行器时在网关执行，并生成官方 Responses output items/events。
- 不能转换的工具必须路由到支持该工具的 Responses 上游。
- hosted tool 不得再以“没有 name”为理由直接丢弃。
- 工具副作用必须具备审批、幂等键、超时、审计和租户隔离。

### 4.2.7 全量请求参数

建立版本化 Responses schema，不再把所有非 Chat 字段视为未知参数。至少覆盖：

- model、input、instructions。
- tools、tool choice、parallel tool calls。
- text format、structured outputs、verbosity。
- reasoning effort、summary 及相关正式选项。
- store、previous response ID、conversation。
- background。
- include。
- metadata、user、安全标识字段。
- max output tokens、temperature、top p、truncation。
- service tier、prompt cache、stream options。
- 官方后续增加的正式字段。

参数处理必须分两步：先按 Responses schema 验证，再按执行模式决定直通、消费、模拟或转换。只有真正未知的非官方字段才静默丢弃。

### 4.2.8 全量 Streaming Events

建立版本化 event registry，并以官方 schema 作为唯一契约。覆盖：

- response 生命周期：queued、created、in progress、completed、failed、incomplete。
- output item added/done。
- content part added/done。
- output text delta/done、refusal delta/done、annotations。
- reasoning summary/reasoning text 相关事件。
- function call arguments delta/done。
- web/file search、computer、Code Interpreter、image generation 和 MCP 事件。
- approval request/response。
- error 事件和 usage。

要求：

- `sequence_number` 全局严格递增。
- catch-up stream 与实时 stream 使用同一事件日志。
- 每个 added/delta/done 生命周期可验证。
- 不把 `[DONE]` 当作唯一完成依据；以正式终止事件决定状态。
- 事件 schema 与 OpenAI 官方 SDK 反序列化兼容。

### 4.2.9 原生 Responses 直通

模型/key 配置新增：

```yaml
upstream_mode: responses_native
capabilities:
  - stateful_responses
  - background
  - web_search
  - file_search
  - computer
  - code_interpreter
  - image_generation
  - remote_mcp
```

原生模式不得先降级为 Chat Completions。代理只执行鉴权替换、模型映射、租户策略、审计和必要的 schema 兼容，不改写合法 output item 或事件语义。

### 4.2.10 官方兼容性测试

除 Codex 真机测试外，增加：

- OpenAI Python、TypeScript、Go、Java 和 .NET SDK 合约测试。
- 官方 create/retrieve/delete/cancel/compact/input items 调用测试。
- store/previous response/background 多轮测试。
- 每种官方 tool 和 output item 的 fixture。
- 全量 streaming event schema 测试。
- 原生 OpenAI Responses 与 zhongzhuan 的 differential test。
- 新版 OpenAI SDK/API schema 变更监测。

声明“完全兼容”前，必须生成版本化兼容报告，列出目标 OpenAI API/SDK 版本、全部端点、字段、item 和事件的通过状态。

## 5. 模块设计

### 5.1 RequestSanitizer

新增：

```
src/zhongzhuan/proxy/protocol/responses_request.py
```

职责：

- 校验 Responses 请求的基本结构。
- 标准化字符串或数组形式的 `input`。
- 删除全部 `type=reasoning` 历史项。
- 将 message、function call、function call output 转为内部消息。
- 将 Responses tools 转为内部工具定义。
- 根据目标上游能力生成最终请求。
- 输出 dropped fields，供日志和指标使用。

建议返回结构：

```python
@dataclass
class SanitizedRequest:
    payload: dict
    dropped_fields: list[str]
    normalized_call_ids: dict[str, str]
    warnings: list[str]
```

请求构造必须使用 allowlist，不再执行 `result = dict(body)`。

```python
CHAT_BASE_ALLOWED = {
    "model",
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "stream",
    "tools",
    "tool_choice",
    "response_format",
    "seed",
    "user",
    "reasoning_effort",
}
```

上游可进一步收窄：

```python
PROVIDER_CAPABILITIES = {
    "deepseek": {...},
    "openai_compatible": {...},
    "anthropic": {...},
}
```

以下 Responses 字段默认消费或丢弃，不得原样透传：

- `input`
- `instructions`
- `previous_response_id`
- `store`
- `parallel_tool_calls`
- `metadata`
- `include`
- `background`
- `prompt_cache_key`
- `client_metadata`
- `service_tier`
- `truncation`
- `text`
- 未知未来字段

### 5.2 Reasoning 策略

请求方向：

```python
if item_type == "reasoning":
    continue
```

不得再维护：

- `pending_reasoning`
- `pending_reasoning_encrypted`
- `attach_pending_reasoning()`

响应方向允许维护仅限当轮、仅限内存的 reasoning buffer：

```python
@dataclass
class EphemeralReasoningAccumulator:
    output_index: int
    item_id: str
    text: str = ""
    added: bool = False
    done: bool = False
```

终止后立即释放，不进入 session、数据库或下一轮请求。

reasoning 输出事件做成可配置方言：

```
reasoning_summary_text
reasoning_text
disabled
```

默认值由 Codex 实际兼容性测试确定。

### 5.3 ToolCallAccumulator

新增：

```python
@dataclass
class ToolCallAccumulator:
    source_index: int
    output_index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    item_added: bool = False
    arguments_done: bool = False
    item_done: bool = False
```

维护双索引：

```python
tools_by_call_id: dict[str, ToolCallAccumulator]
tools_by_source_index: dict[int, ToolCallAccumulator]
```

匹配优先级：

1. 已存在的 `call_id`。
2. 已建立的 source index → accumulator 映射。
3. 新建 accumulator。

当 call ID 延迟出现时，必须将按 index 建立的 accumulator 绑定到 call ID。若上游始终不返回 ID，则使用稳定合成值：

```
call_{response_id}_{source_index}
```

函数名采用追加或完整值替换策略，兼容“分片名称”和“重复完整名称”两类上游。

arguments 处理规则：

- 每个非空 fragment 原样追加。
- delta 可实时发送，但不得在结束信号前发 `.done`。
- 结束时执行 JSON 解析校验。
- 默认要求顶层为 object。
- 解析失败时不得创建可执行的完整 function call。

### 5.4 全局 output index

Responses 的 message、reasoning、function call 共用一个全局 output 索引空间。新增：

```python
class OutputIndexAllocator:
    def allocate(self) -> int: ...
```

每个 output item 仅在首次创建时分配一次。禁止直接把 Chat Completions 的 choice index 或 tool index 当作 Responses output index。

### 5.5 SSE Parser

新增独立 `SSEParser`：

- 支持 `\n\n` 和 `\r\n\r\n`。
- 支持一个事件包含多行 `data:`。
- 支持任意 TCP 字节分片。
- 正确处理 UTF-8 多字节字符边界。
- 对注释行、event、id、retry 字段保持兼容。
- 对无法解析的 JSON 帧记录指标，不静默吞掉终止原因。

推荐输入输出：

```python
async def feed(chunk: bytes) -> list[SSEEvent]
async def finish() -> list[SSEEvent]
```

### 5.6 ResponsesEventEmitter

负责：

- 分配并维护单调递增的 `sequence_number`。
- 立即发送 created/in progress。
- 确保每个 item 的 added/done 成对。
- 确保 completed 和 `[DONE]` 各发送一次。
- completed 后拒绝任何新 delta。
- 每 10–15 秒发送 SSE comment heartbeat。

显式状态机：

```
INIT → CREATED → IN_PROGRESS → STREAMING → COMPLETING → COMPLETED
```

非法状态转换必须被拒绝并记录：

- `INIT → COMPLETED`
- `COMPLETED → DELTA`
- 同一 item 重复 added
- item done 后继续追加
- completed 重复发送

## 6. 请求转换规则

### 6.1 instructions

`instructions` 转为首条 system 消息。若已有 system/developer 消息，按稳定顺序合并，不得重复注入。

### 6.2 message

- `input_text`、`output_text` → Chat text。
- `input_image` → image URL 或 data URL；由能力声明决定是否保留。
- 未知 content block 静默丢弃并记录。
- 不得把未知 block 字典原样透传。

### 6.3 function call

- name 为空：丢弃并记录 warning。
- arguments 非字符串：序列化为 JSON 字符串。
- call ID 统一规范化，并在 function call output 中复用同一映射。
- 不生成空 `tool_calls: []` assistant 消息。

### 6.4 function call output

- 找不到对应 call ID：不猜测绑定关系；记录协议异常。
- 非字符串 output：使用稳定 JSON 序列化。
- 大型输出按配置截断，并保留明确截断标记。

### 6.5 tools

- 只接受可降级的 function tool。
- hosted tools 默认丢弃。
- 缺失 parameters 时补 `{type: object, properties: {}}`。
- `strict` 仅在目标上游声明支持时保留。
- schema 不合法时在本地修正或丢弃，不将明显非法 schema 发送上游。

## 7. 流式生命周期

### 7.1 正常文本流

```
response.created
response.in_progress
response.output_item.added(message)
response.content_part.added
response.output_text.delta × N
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
data: [DONE]
```

### 7.2 reasoning + tool call

```
response.created
response.in_progress
response.output_item.added(reasoning)
reasoning delta × N
reasoning done
response.output_item.done(reasoning)
response.output_item.added(function_call)
response.function_call_arguments.delta × N
response.function_call_arguments.done
response.output_item.done(function_call)
response.completed
data: [DONE]
```

### 7.3 上游断流

兼容模式：

- 保证已建立 item 被安全关闭。
- 残缺 tool call 不发送 arguments done，不进入可执行 output。
- 发送 completed 和 `[DONE]`，并记录 `terminal_reason=upstream_truncated`。
- completed 响应中尽可能附带 incomplete details。

严格模式：

- 发送 failed/incomplete 终止事件。
- 最后发送 `[DONE]`。

默认采用 Codex 兼容模式，严格模式通过配置启用。

## 8. 超时与连接管理

推荐默认值：

```
connect_timeout = 15s
pool_timeout = 15s
write_timeout = 60s
read_idle_timeout = 300s
first_token_timeout = 300s
total_timeout = 900s 或关闭硬限制
heartbeat_interval = 15s
```

要求：

- 客户端断开后立即取消上游读取。
- 客户端主动取消不得标记上游 key 失败。
- 连接错误、首 token 超时、读空闲超时、总超时分别分类。
- Nginx/反代 `proxy_read_timeout` 必须大于应用 read timeout。
- SSE 禁止响应压缩，避免事件边界和缓冲延迟。
- 每次 heartbeat 都刷新链路活性，但不改变 Responses 状态。

## 9. 重试与幂等

### 9.1 允许自动重试

仅在尚未向 Codex 发送任何业务 delta 时允许切换 key 重试：

- DNS/连接失败。
- TLS 失败。
- 上游 429/5xx。
- 首字节前断开。

### 9.2 禁止自动重试

一旦已经发送以下任一事件后的业务内容，不得透明重放到另一上游：

- output text delta
- reasoning delta
- function arguments delta

否则可能产生重复文本或重复工具执行。此时只允许终止当前流并返回可诊断状态。

### 9.3 工具调用幂等

- 同一 call ID 只能产生一个完成的 function call item。
- 重复 chunk 不得导致重复 `.done`。
- 迟到 chunk 在 item done 后直接丢弃并计数。
- 可选维护短 TTL 的 completed call ID 集合，用于检测重放。

### 9.4 无限循环硬熔断

<aside>
♾️

**有限终止原则：**无论模型、上游、工具、状态存储或网络发生何种异常，每个 response 和 background task 都必须在有限预算内进入 completed、failed、incomplete 或 cancelled，禁止无限生成、无限工具重试和无限状态链恢复。

</aside>

#### 状态链防环

处理 `previous_response_id` 和 conversation 状态时必须：

- 拒绝 response 自引用。
- 沿 parent 链检测祖先循环；恢复过程中维护 visited response ID 集合。
- 设置最大链深，默认 64，可按租户收窄。
- 设置最大恢复 input items 数量和最大恢复 token 数。
- parent 不存在、已删除、跨租户或形成环时返回标准 Responses 错误，不得回退为无状态请求。
- compact 后以 compacted item 为新边界，避免每轮无限展开全部历史。

#### 工具轮数与重复签名熔断

每个 response 维护执行预算：

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

工具调用签名使用稳定规范化结果：

```
sha256(tool_name + canonical_json(arguments) + normalized_result_or_error)
```

规则：

- 相同 `tool_name + arguments` 连续出现超过阈值时终止。
- 相同 `tool_name + arguments + result/error` 重复出现，判定模型没有取得进展并立即熔断。
- 同一工具连续失败超过阈值时终止，不得让模型无限重试。
- 模型仅改变无意义空白、JSON 键顺序或 call ID 时，仍视为同一调用。
- tool result 过大时截断并标记，不能因截断触发无限补取。
- 并行工具调用计入总调用预算，并分别执行幂等检查。

#### 流式重放熔断

- 尚未发送业务 delta 前，最多允许有限次数上游切换；默认 2 次。
- 发送任意文本、reasoning 或工具参数 delta 后，禁止透明重放。
- 同一 request ID、idempotency key 或 response ID 的重复请求必须返回已有状态或明确冲突，不重复执行有副作用工具。
- 上游反复在同一字节/事件位置断流时，进入 circuit open 状态并停止自动重试。
- `[DONE]`、completed、failed、incomplete 或 cancelled 任一正式终止后，拒绝全部迟发事件。

#### Background task 预算

- background task 必须有最大墙钟时间、最大工具轮数、最大 token、最大存储和最大外部调用次数。
- 进程重启后只能从已持久化 checkpoint 恢复一次，不得形成 crash-restart 循环。
- cancel 必须设置持久化取消标记；worker 在每个工具调用前后检查。
- 超预算统一进入 `incomplete`，并写入明确的 `incomplete_details.reason`。

#### 熔断终止原因

至少支持：

```
max_tool_rounds
max_total_tool_calls
repeated_tool_call
repeated_tool_failure
max_response_time
max_output_budget
response_chain_cycle
response_chain_too_deep
retry_budget_exhausted
background_budget_exhausted
```

熔断后：

1. 停止读取或取消上游。
2. 停止调度新工具。
3. 回滚尚未提交的副作用，或记录需人工处理的不可回滚副作用。
4. 关闭已开始的 output item。
5. 发送且只发送一个正式终止事件和 `[DONE]`。
6. 写入结构化日志、指标和审计事件。

#### 防循环验收

- 单 response 在任意故障注入下都能在预算内终止。
- 自引用和多节点 response 环均被拒绝。
- 相同工具调用重复三次以内触发熔断。
- 工具持续返回相同错误时不会无限重试。
- worker 重启不会重复执行已完成的副作用工具。
- 流中途断开不会导致 Codex 无限重放。
- 长稳测试中不存在持续增长且无终止状态的 response/background task。

## 10. 错误模型

内部统一错误分类：

```
invalid_client_request
unsupported_input_block
upstream_connect_error
upstream_rate_limited
upstream_server_error
first_token_timeout
read_idle_timeout
upstream_truncated
invalid_sse_frame
invalid_tool_arguments
client_disconnected
internal_translation_error
```

错误响应不得暴露 API key、完整 Authorization header、原始敏感 tool output 或 reasoning 内容。

## 11. 可观测性

### 11.1 指标

- `responses_requests_total`
- `responses_streams_completed_total`
- `responses_streams_truncated_total`
- `responses_unknown_params_dropped_total`
- `responses_reasoning_history_dropped_total`
- `responses_tool_calls_total`
- `responses_tool_call_json_invalid_total`
- `responses_duplicate_tool_chunks_total`
- `responses_late_chunks_total`
- `responses_first_token_seconds`
- `responses_stream_duration_seconds`
- `responses_heartbeat_total`
- `responses_client_disconnect_total`

### 11.2 结构化日志字段

```
request_id
session_id_hash
model
upstream_protocol
upstream_key_id
stream
attempt
first_token_ms
duration_ms
dropped_fields
reasoning_history_items_dropped
tool_call_count
terminal_reason
client_disconnected
```

禁止记录：

- API key
- Authorization header
- 完整 reasoning 文本
- 默认情况下的完整 tool arguments/output

### 11.3 调试回放

提供可选匿名化 capture：

- 保留事件类型、时间、索引、ID 哈希、fragment 长度。
- 文本和参数内容默认脱敏。
- capture 设置大小和 TTL 上限。
- 提供离线 replay 测试入口。

## 12. 测试计划

### 12.1 单元测试

必须覆盖：

- 历史 reasoning、summary、encrypted content 全部丢弃。
- reasoning 不进入粘性 session hash。
- 未知顶层字段和未知 content block 静默丢弃。
- `input=[]` 不绕过净化。
- previous response ID、store、metadata、parallel tool calls 不透传。
- tool name 和 arguments 任意分片。
- call ID 延迟出现或缺失。
- 并行 tool call 交错分片。
- 相同 source index、不同 call ID 的异常上游行为。
- arguments 含 Unicode、转义字符和嵌套 JSON。
- output index 全局唯一。
- sequence number 严格单调。
- 每个 added 恰好对应一个 done。
- completed 和 `[DONE]` 各一次。
- 首 chunk 前断流仍先发送 created。
- usage-only、空 choices、无 `[DONE]` 流。
- completed 后迟发 chunk 被丢弃。

### 12.2 属性测试

使用 Hypothesis 将同一 SSE 流随机切分成任意字节 chunk，验证输出语义不变：

- UTF-8 多字节字符边界。
- JSON 字符串转义边界。
- `\r\n` 与 `\n` 混合。
- 一个 chunk 多事件、一个事件多 chunk。
- tool arguments 每字节分片。

### 12.3 集成测试

建立兼容矩阵：

```
Codex CLI × DeepSeek reasoning model
Codex CLI × OpenAI-compatible non-reasoning model
Codex CLI × Anthropic upstream
单工具调用
并行工具调用
工具失败后下一轮
首 token 延迟 120 秒
上游中途断流
客户端主动取消
```

### 12.4 长稳测试

- 单会话连续 100 轮。
- 连续 1,000 次工具调用。
- 10–50 并发 Codex 会话。
- 随机注入 429、5xx、断流和延迟。
- 验证无无限重试、无重复工具执行、内存不持续增长。

## 13. 文件改造建议

```
src/zhongzhuan/proxy/protocol/
  responses_request.py       # 请求净化与转换
  responses_models.py        # dataclass 和内部事件模型
  sse_parser.py              # 通用 SSE 分帧
  tool_accumulator.py        # 工具调用聚合
  turn_accumulator.py        # 文本/reasoning/tool 状态
  responses_emitter.py       # Responses 事件生成与状态机
  responses.py               # 对外兼容入口，逐步瘦身
```

现有 `ResponsesStreamTranslator` 先保留外部接口，通过组合上述模块实现，避免一次性改动 handler 的全部调用点。

`CompositeStreamTranslator.finish_safely()` 必须统一为 async，禁止同步方法调用 async `feed()`，也禁止遍历列表时向同一列表追加。

## 13.1 项目整体优化审查

以下问题来自对当前 `main` 分支核心代码、配置、存储、鉴权、调度和打包结构的审查。它们不全属于 Responses 协议，但会影响 v3 的稳定性、安全性和可维护性。

### P0：开发 v3 前必须修复

#### 1. 实际默认超时仍为 30 秒

`LimitsConfig.proxy_request_timeout` 和 `ProxyServer` 当前默认值为 30 秒，启动流程会把该值传给 `UpstreamClient`。这会覆盖 handler 中的 300 秒默认值，直接违反铁律 5。

整改：

- 配置默认 read/first-token timeout 改为至少 300 秒。
- 拆分 connect、pool、write、first token、read idle 和 total timeout。
- 启动日志打印最终生效值。
- 增加配置合并测试，防止 YAML/.env 再把值意外降回 30 秒。

#### 2. `store/logs.py` 存在明显损坏代码

当前 `cleanup_old_logs()` 中混入统计逻辑，存在未定义的 `since`、`daily_rows`、`days`，DELETE SQL 也错误包含 `GROUP BY day ORDER BY day`。该路径可能导致日志清理和后台统计失败。

整改：

- 恢复独立的 `cleanup_old_logs()` 和 usage report 函数。
- 清理 SQL 使用 `DELETE FROM request_logs WHERE ts < ?`。
- 为 SQLite 和 TiDB 分别增加集成测试。
- 在 CI 中运行模块导入、类型检查和日志清理测试。

#### 3. 数据库迁移吞掉全部异常

SQLite migration 当前对所有 Exception 直接 `pass`，会把磁盘故障、锁冲突、SQL 错误和真实 schema 不一致误判为“列已存在”。

整改：

- 建立 `schema_migrations` 版本表和事务化迁移。
- 只忽略经过错误码确认的 duplicate column。
- 迁移失败必须阻断启动并输出具体版本和 SQL。
- 启动前备份 SQLite；提供迁移回滚或恢复说明。

#### 4. 访问令牌明文存储和展示

access token 当前以明文存入数据库，列表接口返回完整 token；首次自动生成 token 还会写入日志。数据库、日志或后台页面泄露都会直接暴露代理访问权。

整改：

- 只在创建时展示一次完整 token。
- 数据库存储 `token_prefix + keyed_hash`，验证时常量时间比较。
- 列表接口只返回掩码和 prefix。
- 禁止把 token、API key、JWT 和 Authorization 写入日志。
- 增加 token rotation、last-used-at、created-by 和 revoke audit。

#### 5. 打包元数据与运行依赖不一致

`pyproject.toml` 未声明代码实际使用的 aiosqlite、cryptography、python-dotenv，以及 VPS 模式所需 aiomysql、PyJWT、bcrypt；同时引用仓库中不存在的 README，可能导致标准安装或构建失败。

整改：

- 统一 `pyproject.toml` 为依赖事实源。
- 使用 `sqlite`、`tidb`、`admin`、`build` 等 extras。
- 补齐 README 或取消错误引用。
- CI 测试 clean environment 下 wheel/sdist 安装、CLI 启动和导入。
- 锁定支持范围并增加依赖漏洞扫描。

#### 6. Responses 全量兼容缺少持久化基础

当前 schema 没有 responses、input items、event log、background jobs、tool executions 和 idempotency records 表，无法正确实现 retrieve、cancel、compact、catch-up stream 和幂等恢复。

整改：

- 按 ResponseStore 设计新增表、索引、TTL 和租户键。
- event log 采用 append-only sequence number。
- tool execution 记录幂等键、状态、审批和副作用结果。
- background worker 使用 lease/heartbeat，避免多实例重复执行。

### P1：v3 主开发阶段完成

#### 7. `handler.py` 体积过大且职责耦合

当前 handler 同时承担鉴权后处理、路由、模型选择、协议转换、重试、流式写入、计费、日志和 key 健康更新，修改 Responses 容易回归旧协议。

拆分为：

```
RequestContextBuilder
ProtocolRouter
CapabilityRouter
AttemptManager
StreamingPipeline
AccountingService
ResponseRepository
LegacyProtocolHandler
ResponsesV3Handler
```

共享对象通过显式 RequestContext 传递，禁止使用散落的局部状态和重复分支。

#### 8. 重试分类会错误惩罚请求侧 4xx

`classify_failure()` 对普通 4xx 调用 `mark_failure()` 后返回不可重试，实际上仍把 key 标成 server error。无效参数、上下文过长或工具 schema 错误不应降低 key 健康度。

整改：

- 400/404/409/413/422 等请求侧错误不修改 key 健康状态。
- 仅认证、限流、服务端和网络错误影响相应维度。
- 失败计数拆分为 total failures 和 consecutive failures。
- `mark_success()` 重置 consecutive failures，避免历史偶发错误永久放大退避。

#### 9. 分组调度策略可能未真正生效

handler 当前把 group 成员合并后仍交给通用 `pick_key()`，配置中的 round robin、weighted、failover 语义容易被健康评分覆盖；独立的 group scheduler 与实际入口需要统一。

整改：

- 先按 group strategy 选 model，再在 model 内按 key 健康选择。
- failover 严格遵守顺序。
- weighted 使用可测试的平滑加权轮询。
- sticky session 只在选定模型仍健康且能力兼容时生效。

#### 10. 当前粘性会话哈希跨轮不稳定

使用最后三条 messages/input 计算哈希时，每轮内容变化都会改变 fingerprint，无法可靠保证同一会话持续命中同一 key。

整改：

- 优先使用显式 conversation、previous response ID 或客户端 session header。
- 对 Responses 在 ResponseStore 中持久化 session → route binding。
- 无显式 ID 时使用首轮稳定指纹，而不是滚动消息尾部。
- route binding 设置 TTL、能力校验和故障迁移记录。

#### 11. 配置加载缺少类型和范围校验

当前 `_merge()` 会把 YAML 值直接写入 dataclass，未知字段静默忽略，错误类型和非法范围可能直到运行时才暴露。

整改：

- 使用 Pydantic 或等价严格 schema。
- 对端口、timeout、并发、TTL、路径和 URL 做范围验证。
- 未知配置字段默认报错，提供显式兼容模式。
- 输出脱敏后的 effective config 和配置来源。

#### 12. 请求体被多个层重复读取和解析

鉴权 middleware 与 handler 都读取并解析 JSON。大请求、多模态输入和高并发下会增加 CPU、内存和延迟。

整改：

- 入口只解析一次，保存到 `request["json_body"]` 和 RequestContext。
- 设置按端点区分的请求大小上限。
- 文件/图像输入采用流式或临时对象存储，不把所有内容永久留在内存。

#### 13. SQLite 每次 execute 都立即 commit

高并发日志、usage、event 和 background 状态写入会形成写放大；单连接也会成为 Responses event log 的瓶颈。

整改：

- 显式事务和批量写入。
- request logs/event logs 使用队列批处理。
- SQLite 模式设置容量边界并明确单实例定位。
- 多实例生产环境使用 TiDB/MySQL，并增加连接池、事务隔离和幂等约束测试。

### P2：上线前完善

#### 14. 默认安全策略需要收紧

- CORS 当前默认 `*`，应改为可配置 allowlist。
- 管理端鉴权和代理鉴权不应在公网部署中默认关闭。
- 管理 UI/API 需要 CSRF、登录限速、审计日志和安全响应头。
- JWT secret 未配置时进程内随机生成，重启会使所有会话失效；生产模式应要求持久化 secret 并支持轮换。
- 默认 dummy key 会让缺少配置的实例继续启动并向 OpenAI 发送无效认证；生产模式应 fail closed。
- OpenCode Free fallback 默认启用会产生外部数据出站，应改为显式 opt-in，并在 UI 中显示隐私提示。

#### 15. 健康检查过于浅层

当前 `/healthz` 固定返回 `ok`。新增：

- liveness：事件循环和进程存活。
- readiness：数据库迁移完成、至少一个可用 route、worker lease 正常。
- dependency status：存储、上游、工具执行器。
- 不在公开健康接口泄露 key、内部 URL 或敏感拓扑。

#### 16. 可观测性和数据保留

- 增加 Prometheus/OpenTelemetry 指标与 tracing。
- 日志写入异步化，防止数据库慢查询阻塞响应。
- 修复并定期执行 retention；对 response/event/tool audit 分别设置 TTL。
- 记录 TTFT、每事件延迟、重试原因、能力路由和熔断原因。
- 对错误文本、tool 参数和用户输入执行脱敏与大小限制。

#### 17. CI 与工程质量

新增强制检查：

```
ruff/format
mypy 或 pyright
pytest + coverage
Hypothesis 流式分片属性测试
bandit/依赖漏洞扫描
wheel/sdist clean install
SQLite/TiDB 双后端集成测试
Windows/Linux 启停测试
旧协议 golden fixtures
OpenAI SDK contract tests
```

仓库不得提交运行时数据库的 `data.db-wal`、`data.db-shm` 或构建产物；增加 secret scanning 和提交前检查。

#### 18. 性能与容量治理

- 确认并真正执行 `global_concurrent`，为每租户、每模型和 background worker 设置独立 semaphore。
- 对排队时间设置上限，过载时快速返回标准 429/503。
- 工具执行、模型流和持久化使用隔离的并发池，防止一种工作负载拖垮全部服务。
- 建立响应对象、事件日志、文件和调试 capture 的容量模型及磁盘水位保护。

## 14. 分阶段实施

### Phase 0：全仓稳定化

- 修复日志模块损坏代码。
- 将实际生效的首 token/read timeout 提升到至少 300 秒。
- 建立可靠数据库迁移机制。
- 修复 token 明文存储、完整展示和日志泄露。
- 统一打包依赖并通过 clean-install 测试。
- 为旧协议建立 golden fixtures。

发布门槛：现有功能全绿、无已知 P0，且 Chat/Anthropic 旧路径输出保持不变。

### Phase 1：循环阻断与参数安全

- 删除 reasoning 历史回写。
- session hash 排除 reasoning。
- denylist 改 allowlist。
- call ID 统一映射。
- 增加五条铁律的基础测试。

发布门槛：历史 reasoning 无法出现在任何上游 payload。

### Phase 2：流式状态机与工具聚合

- 引入独立 SSE parser。
- 引入 ToolCallAccumulator。
- 全局 output index。
- created 前置。
- 终止幂等和迟发 chunk 防护。
- 修复 Composite translator 的 async 收尾。

发布门槛：随机分片和并行工具调用测试全部通过。

### Phase 3：超时、心跳与可观测性

- 分层 timeout。
- SSE heartbeat。
- 客户端取消传播。
- 指标、结构化日志和匿名化回放。
- 故障注入和长稳测试。

发布门槛：120 秒首 token 测试、断流测试和 100 轮会话测试通过。

### Phase 4：灰度与默认启用

- 增加 `RESPONSES_BRIDGE_V3` 特性开关。
- 按 key/model/group 灰度。
- 同步记录 v2/v3 结果差异，不重复调用真实工具。
- 观察循环率、截断率、首 token 和工具失败率。
- 达标后切换默认，保留一个版本的回滚能力。

## 15. 配置建议

```yaml
responses_bridge:
  version: v3
  compatibility_profile: openai_responses_full
  native_passthrough_preferred: true
  state_store_enabled: true
  background_enabled: true
  hosted_tools_enabled: true
  drop_unknown_params: true
  discard_historical_reasoning: true
  reasoning_event_mode: reasoning_summary_text
  stream:
    heartbeat_seconds: 15
    compatibility_terminal_event: completed
  timeout:
    connect_seconds: 15
    first_token_seconds: 300
    read_idle_seconds: 300
    total_seconds: 900
  tools:
    require_json_object_arguments: true
    synthesize_missing_call_id: true
    reject_incomplete_on_finish: true
  debug_capture:
    enabled: false
    redact_content: true
    ttl_hours: 24
```

生产环境不得允许关闭 `discard_historical_reasoning`。该项即使出现在配置中，也应视为不可变安全策略，而不是普通功能开关。

## 16. 验收标准

v3 只有同时满足以下条件才允许默认启用：

- [ ]  抓包证明下一轮上游 payload 不含上一轮 reasoning 文本或 encrypted content。
- [ ]  所有未知 Responses 字段均不会进入上游 payload。
- [ ]  DeepSeek 工具 arguments 任意分片后可完整重组。
- [ ]  两个及以上并行工具调用不会混流。
- [ ]  每个 Responses output item 有唯一 output index。
- [ ]  所有流都先 created，后 completed/failed，最后 `[DONE]`。
- [ ]  上游首 token 延迟 120 秒时连接保持正常。
- [ ]  上游断流不会触发无限重放。
- [ ]  工具调用不会因重复 chunk 执行两次。
- [ ]  客户端断开不会错误惩罚上游 key。
- [ ]  100 轮连续会话无 reasoning 回灌、无循环、无持续内存增长。
- [ ]  新增指标可以定位每次异常终止的具体原因。
- [ ]  OpenAI 官方 SDK 可直接完成 create、retrieve、delete、cancel、compact 和 input items 操作。
- [ ]  `store`、`previous_response_id` 和 background mode 通过多轮及重启恢复测试。
- [ ]  Function、web search、file search、computer use、Code Interpreter、image generation 和 remote MCP 均通过能力测试。
- [ ]  全部正式 Responses output item 和 streaming event 均通过 schema 与生命周期测试。
- [ ]  原生 Responses 上游请求不会被错误降级到 Chat Completions。
- [ ]  已生成版本化 OpenAI Responses 全量兼容报告，且不存在未支持项。

<aside>
🛡️

**发布阻断项：**任何历史 reasoning 写回、残缺工具参数被标记完成、缺少正式终止事件、未知参数透传导致 400，或任何目标版本 OpenAI Responses 正式端点、字段、item、tool、状态能力和 SSE 事件未实现，均视为 v3 NO-GO。

</aside>