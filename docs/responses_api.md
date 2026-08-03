# Responses API 支持（Codex CLI）

本文档说明 `zhongzhuan` 对 OpenAI **Responses API**（`POST /v1/responses`）的支持，
该协议是 Codex CLI 使用的默认协议。实现思路移植自 `9router_research` 的
`open-sse` 翻译层（`responsesApi.js` / `responsesTransformer.js`）。

## 1. 设计思路

`zhongzhuan` 的上游只有两种协议：`openai`（Chat Completions）和 `anthropic`（Messages）。
Responses API 不作为独立上游协议存在，而是作为**入站协议**接入，在代理内部翻译：

```
Codex ──POST /v1/responses──▶ zhongzhuan ──▶ /v1/chat/completions ──▶ 上游(openai)
                                        └──▶ /v1/messages        ──▶ 上游(anthropic)
```

回程同理，把上游响应逐层翻回 Responses 格式再返回给 Codex。
这样任意一个仅支持 Chat Completions 的上游，都能直接给 Codex 用。

## 2. 协议识别

`src/zhongzhuan/proxy/protocol/detect.py`：

| 路径 | 入站协议 |
| --- | --- |
| `/v1/responses`、`/v1/responses/*` | `responses` |
| `/v1/messages*` | `anthropic` |
| 其他（含 `x-api-key` / `anthropic-version` 头） | `anthropic` / `openai` |

路径优先级高于请求头，因此 Codex 即使带了 `x-api-key` 也会正确落到 `responses`。

只支持 `POST`。`GET /v1/responses/{id}`（retrieve）与 `DELETE` 返回 **405**，
避免 Codex 挂起或反复重试。

## 3. 请求翻译（Responses → Chat Completions）

实现：`convert_responses_request_to_chatcompletions()`。

| Responses 字段 | Chat Completions |
| --- | --- |
| `instructions` | 首条 `{"role":"system"}` 消息 |
| `input`（字符串） | 单条 user 消息，内容规范化为 `input_text` 块 |
| `input[].type = message` | 同角色消息，`input_text`/`output_text` → `text` |
| `input[].type = function_call` | 合并进 assistant 消息的 `tool_calls[]` |
| `input[].type = function_call_output` | `{"role":"tool","tool_call_id":...}` |
| `input[].type = reasoning` | 挂到下一条 assistant 的 `reasoning_content` / `encrypted_content` |
| `tools[]`（`{type:"function", name, parameters}`） | `{type:"function", function:{name, parameters}}` |
| `max_output_tokens` | `max_tokens` |
| `reasoning.effort` | `reasoning_effort` |
| `include` / `store` / `prompt_cache_key` / `client_metadata` | 丢弃 |

细节约定：

- **无名 `function_call` 直接跳过**，且不会产生空的 `tool_calls: []` assistant 壳子
  （严格上游会对空数组返回 400）。
- **hosted tools**（如 `{"type":"request_user_input"}` 这类没有 `name` 的）直接丢弃。
- `parameters` 缺 `properties` 时补 `{"type":"object","properties":{}}`，
  否则部分上游会因 schema 不合法而 400。
- `call_id` 会被裁剪到上游可接受的长度。
- 流式请求会自动注入 `stream_options.include_usage = true`，保证能拿到 usage 计费。

## 4. 响应翻译

### 非流式

`chatcompletions_to_responses()`：

- `choices[0].message.content` → `output[]` 中一个 `message` 项，
  内容为 `[{type:"output_text", text, annotations:[]}]`
- `choices[0].message.tool_calls[]` → 若干 `function_call` 项（带 `call_id`/`name`/`arguments`）
- `usage.prompt_tokens/completion_tokens/total_tokens`
  → `usage.input_tokens/output_tokens/total_tokens`
- 顶层补 `object: "response"`、`status: "completed"`、`created_at`

### 流式

`ResponsesStreamTranslator` 把 Chat Completions 的 SSE 转成 Responses 事件序列：

```
response.created
response.in_progress
response.output_item.added        (message / function_call / reasoning)
response.content_part.added
response.output_text.delta        (可多次)
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
data: [DONE]
```

工具调用额外发 `response.function_call_arguments.delta` / `.done`；
推理内容发 `response.reasoning_summary_text.delta` / `.done`。

关键保障：

- **一定会收尾**。上游中途断流时 `finish_safely()` 仍会补齐
  `response.completed` 和 `data: [DONE]`，否则 Codex 会一直挂住。
- `finish_safely()` **幂等**，不会重复发送终止事件。
- SSE 帧被 TCP 切断到多个 chunk 时由内部 buffer 正确拼接。
- `translator.usage` 保留 Chat Completions 形态的 usage，供上层计费直接复用。

### Anthropic 上游

`CompositeStreamTranslator(StreamA2O(...), ResponsesStreamTranslator(...))`
先把 Anthropic SSE 翻成 Chat Completions SSE，再翻成 Responses SSE，串成一条流水线。

## 5. 与既有能力的衔接

| 能力 | 状态 |
| --- | --- |
| 多 key 重试 / 熔断 | ✅ 复用，未改动 |
| Token 计费 | ✅ 流式走 `stream_options.include_usage`；非流式同时兼容 `input_tokens`/`prompt_tokens` |
| 粘性会话 | ✅ `_session_key()` 在无 `messages` 时回退读 `input` |
| 分组名 / `upstream_model` 替换 | ✅ 翻译后再覆盖 `model` |
| 错误信封 | ✅ responses + openai 上游时原样透传（不会被误当 Anthropic 错误翻译） |

## 6. 已知限制

- 上游即使原生支持 `/v1/responses`，也会被降级成 Chat Completions 转发。
  如需直连，可给 key 配 `upstream_path_override`。
- 不支持 `store=true` 的服务端会话保存与 `GET /v1/responses/{id}` 回查。
- 图片等非文本 `input_image` 块按 Chat Completions 的 `image_url` 语义处理，
  上游不支持时会由上游报错。

## 7. 测试

`tests/test_responses.py`（32 个用例）覆盖：协议识别、请求翻译（含 Codex 真实形态的
`function_call` / `function_call_output` / reasoning / tools）、非流式响应翻译、
流式事件序列、usage 抓取、断流收尾、幂等收尾、chunk 边界切分、粘性会话指纹。

```bash
pytest tests/test_responses.py -q
```
