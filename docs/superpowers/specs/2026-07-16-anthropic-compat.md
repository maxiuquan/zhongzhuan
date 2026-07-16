# Zhongzhuan Anthropic 协议兼容方案

| 字段 | 值 |
|---|---|
| 项目代号 | `zhongzhuan` |
| 文档版本 | v0.1 (Anthropic 兼容草案) |
| 创建日期 | 2026-07-16 |
| 修订日期 | 2026-07-16 |
| 开源协议 | MIT |
| 依赖文档 | `docs/superpowers/specs/2026-06-14-zhongzhuan-design.md` |
| 目标客户端 | Claude Code（及其它 Anthropic SDK 客户端） |

> 本方案在现有 OpenAI Chat Completions 透传代理之上，新增 **Anthropic Messages API** 协议支持，实现「任意协议入站 → 任意协议出站」的双向中转。原 v1 设计把 Anthropic 协议列为非目标，本方案即解除该限制的设计依据。

---

## 1. 背景与目标

### 1.1 痛点

当前 zhongzhuan 仅支持 OpenAI Chat Completions 协议透传。但：

- **Claude Code**（Anthropic 官方 CLI）只说 Anthropic Messages 协议，无法直连本代理。
- 用户手上既有 OpenAI 兼容上游（DeepSeek、Kimi、本地 vLLM…），也有真正的 Anthropic API key，希望在一个代理后混合调度 / fallback。
- 现有的多 key 轮转、滑动窗口限流、健康度评分、多模型分组 fallback 等能力，对 Anthropic 协议的流量同样需要。

### 1.2 目标

1. **入站 Anthropic 协议**：代理作为 Anthropic 服务端，接收 `POST /v1/messages`、`x-api-key` + `anthropic-version` 头，让 Claude Code 等 Anthropic 客户端把 `ANTHROPIC_BASE_URL` 指过来即可使用。
2. **出站 Anthropic 协议**：代理能把请求转发到真正的 Anthropic API（或 Anthropic 兼容中转），用 `x-api-key` 而非 `Authorization: Bearer`。
3. **双向跨协议翻译**：入站协议与上游协议不一致时，自动在 OpenAI ⇄ Anthropic 之间翻译请求 / 非流式响应 / SSE 流式响应 / 错误信封，使「Claude Code → OpenAI 上游」「Cursor → Anthropic 上游」都能跑通。
4. **复用现有调度能力**：多 key 轮转、滑动窗口、健康度、分组 fallback、限流整形、请求日志对 Anthropic 流量同样生效，零特例。
5. **Claude Code 零配置可用**：只需设两个环境变量即可接入。

### 1.3 非目标（本方案不做）

- Gemini 协议（仍不在范围内）。
- Anthropic 的 Files / Batches / Fine-tunes 等非 Messages 端点。
- 计费 / 配额售卖。
- 自动版本协商（`anthropic-version` 透传或取默认，不主动升降级）。
- 训练侧的 prompt caching 命中率统计（cache_control 透传，但不做指标）。

---

## 2. 兼容矩阵

入站协议由「路径 + 头」识别，出站协议由 model 配置的 `protocol` 字段决定。共四种组合：

| 入站（客户端） | 出站（上游） | 处理方式 | 说明 |
|---|---|---|---|
| OpenAI | OpenAI | 透传 | 现有能力，不变 |
| Anthropic | Anthropic | 透传 | 新增：Claude Code → 真 Anthropic 上游 |
| Anthropic | OpenAI | 翻译 | 新增：Claude Code → DeepSeek/Kimi 等 OpenAI 上游 |
| OpenAI | Anthropic | 翻译 | 新增：Cursor/Cline → Anthropic 上游 |

> 分组（Group）内允许混入不同 `protocol` 的 model。fallback 跨协议时，每次 attempt 按该 model 的 `protocol` 决定是否翻译，对客户端完全无感。

---

## 3. Anthropic Messages API 要点速查

实现翻译层前必须对齐的协议事实（以 `anthropic-version: 2023-06-01` 为基线）：

### 3.1 端点与鉴权

| 项 | OpenAI | Anthropic |
|---|---|---|
| 主端点 | `POST /v1/chat/completions` | `POST /v1/messages` |
| Token 计数 | 无 | `POST /v1/messages/count_tokens` |
| 模型列表 | `GET /v1/models` | `GET /v1/models`（格式略不同） |
| 鉴权头 | `Authorization: Bearer <key>` | `x-api-key: <key>` |
| 必需头 | — | `anthropic-version: 2023-06-01`（必需） |
| 可选头 | — | `anthropic-beta: <feature1>,<feature2>` |
| 流式标志 | `stream: true` + `text/event-stream` | `stream: true` + `text/event-stream` |

### 3.2 请求体差异

| 字段 | OpenAI | Anthropic | 备注 |
|---|---|---|---|
| `model` | ✓ | ✓ | 透传 / 重写 |
| `messages` | ✓ | ✓ | 结构不同（见 3.3） |
| `system` 指令 | `messages[].role="system"` | 顶层 `system` 字段（string 或 content block 数组） | 翻译关键点 |
| `max_tokens` | 可选 | **必需** | O→A 翻译时缺失需补默认值（见 §8.4） |
| `temperature` | ✓ | ✓ | 直接映射 |
| `top_p` | ✓ | ✓ | 直接映射 |
| `top_k` | 无 | `top_k` | A→O 丢弃 |
| `stop` / `stop_sequences` | `stop`（string\|array） | `stop_sequences`（array） | 互转 |
| `n` | ✓ | 无 | O→A 丢弃（Anthropic 不支持多候选） |
| `presence_penalty` / `frequency_penalty` | ✓ | 无 | O→A 丢弃 |
| `logprobs` / `top_logprobs` | ✓ | 无 | O→A 丢弃 |
| `response_format` | ✓ | 无（v1 基线无） | A→O 丢弃；O→A 时若为 json_schema 需转 prompt 提示或丢弃并告警 |
| `seed` | ✓ | 无 | O→A 丢弃 |
| `tools` | OpenAI function 格式 | Anthropic 自定义格式 | 见 §8.5 |
| `tool_choice` | `"auto"`\|`"none"`\|`{type:"function",function:{name}}` | `"auto"`\|`"any"`\|`{type:"tool",name}`\|`{type:"auto"}` | 见 §8.5 |
| `metadata` | 无 | `{user_id}` | A→O 丢弃 |
| `stream_options` | `{include_usage}` | 无（usage 在 `message_delta` 里） | 见 §10 |

### 3.3 messages / content 结构差异

**OpenAI**：`messages[].role ∈ {system, user, assistant, tool}`，`content` 为 string 或多模态数组；assistant 的工具调用放在 `tool_calls`；工具结果用 `role:"tool"` + `tool_call_id`。

**Anthropic**：`messages[].role ∈ {user, assistant}`（**无 system role**，system 在顶层）；`content` 为 string 或 content block 数组：

- `{type:"text", text}`
- `{type:"image", source:{type:"base64"\|"url", media_type, data\|url}}`
- `{type:"tool_use", id, name, input}`（assistant 侧）
- `{type:"tool_result", tool_use_id, content, is_error}`（user 侧）
- `{type:"...","cache_control":{type:"ephemeral"}}`（prompt caching beta，任意 block 可附带）

### 3.4 响应结构差异

**非流式**：

| 字段 | OpenAI | Anthropic |
|---|---|---|
| 顶层 | `{id, object:"chat.completion", created, model, choices:[...], usage}` | `{id, type:"message", role:"assistant", model, content:[...], stop_reason, stop_sequence, usage}` |
| 文本 | `choices[0].message.content` (string) | `content[].{type:"text",text}` |
| 工具调用 | `choices[0].message.tool_calls[]` | `content[].{type:"tool_use",id,name,input}` |
| 结束原因 | `choices[0].finish_reason` | `stop_reason` |
| usage | `{prompt_tokens, completion_tokens, total_tokens}` | `{input_tokens, output_tokens, cache_creation_input_tokens?, cache_read_input_tokens?}` |

`stop_reason` ⇄ `finish_reason` 映射：

| Anthropic `stop_reason` | OpenAI `finish_reason` |
|---|---|
| `end_turn` | `stop` |
| `max_tokens` | `length` |
| `stop_sequence` | `stop` |
| `tool_use` | `tool_calls` |

### 3.5 SSE 事件差异（翻译层最难点）

**OpenAI 流式**：每个 chunk 为 `data: {choices:[{index, delta:{role?, content?, tool_calls?}, finish_reason?}]}`，以 `data: [DONE]` 结束。usage 仅在 `stream_options.include_usage=true` 时于末尾 chunk 出现。

**Anthropic 流式**：命名事件，每个事件两行 `event: <name>\ndata: {json}`：

| 事件 | data 关键字段 | 时机 |
|---|---|---|
| `message_start` | `{message:{id, model, usage:{input_tokens, output_tokens:0}}}` | 流开始，发一次 |
| `content_block_start` | `{index, content_block:{type:"text"\|"tool_use", ...}}` | 每个 content block 开始 |
| `content_block_delta` | `{index, delta:{type:"text_delta",text}\|{type:"input_json_delta",partial_json}}` | block 增量 |
| `content_block_stop` | `{index}` | block 结束 |
| `message_delta` | `{delta:{stop_reason?, stop_sequence?}, usage:{output_tokens}}` | 末尾，发一次（含最终 stop_reason 与累计 output_tokens） |
| `message_stop` | `{}` | 流结束 |
| `ping` | `{}` | 心跳 |
| `error` | `{type:"error",error:{type,message}}` | 出错 |

---

## 4. 整体架构

```
┌────────────────────────────────────────────────────────────────────────┐
│  客户端                                                                 │
│   • Claude Code  (Anthropic 协议,  x-api-key)                          │
│   • Cursor/Cline (OpenAI 协议,     Authorization: Bearer)              │
└───────────────────────────┬────────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────────┐
│  ProxyServer  (aiohttp,  :8088)                                        │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  1. 协议识别 (InboundDetector)                                    │  │
│  │     /v1/messages* + x-api-key/anthropic-version → "anthropic"    │  │
│  │     /v1/chat/completions* + Bearer                → "openai"      │  │
│  │     /v1/models / /healthz / /version              → 本地处理      │  │
│  └───────────────────────────┬──────────────────────────────────────┘  │
│                              ▼                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  2. 鉴权 (proxy_auth_middleware, 扩展)                            │  │
│  │     OpenAI 入站: Authorization: Bearer <access_token>             │  │
│  │     Anthropic 入站: x-api-key: <access_token>  (兼容 Claude Code) │  │
│  └───────────────────────────┬──────────────────────────────────────┘  │
│                              ▼                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  3. 调度 (scheduler, 不变)                                        │  │
│  │     按 model/group → 健康度评分选 key → 限流整形 → fallback        │  │
│  └───────────────────────────┬──────────────────────────────────────┘  │
│                              ▼                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  4. 协议适配 (ProtocolAdapter)  ← 本方案核心新增                   │  │
│  │     inbound_protocol  (来自步骤 1)                                │  │
│  │     outbound_protocol (来自 model.protocol)                       │  │
│  │     相同 → 走原透传路径 (handler._passthrough)                    │  │
│  │     不同 → translate_request → 上游 → translate_response/stream   │  │
│  └───────────────────────────┬──────────────────────────────────────┘  │
│                              ▼                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  5. 上游客户端 (UpstreamClient, 扩展)                             │  │
│  │     OpenAI 上游:  Authorization: Bearer <upstream_key>            │  │
│  │     Anthropic 上游: x-api-key: <upstream_key> + anthropic-version │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.1 新增模块

```
src/zhongzhuan/proxy/
├── protocol/                 # 新增：协议识别 + 翻译层
│   ├── __init__.py
│   ├── detect.py             # InboundDetector: 路径+头 → 协议
│   ├── adapter.py            # ProtocolAdapter: 透传/翻译分发
│   ├── translate_o2a.py      # OpenAI 请求/响应 → Anthropic
│   ├── translate_a2o.py      # Anthropic 请求/响应 → OpenAI
│   ├── stream_o2a.py         # OpenAI SSE → Anthropic SSE 状态机
│   ├── stream_a2o.py         # Anthropic SSE → OpenAI SSE 状态机
│   └── errors.py             # 错误信封双向转换
└── handler.py                # 改造：调用 ProtocolAdapter
```

---

## 5. 数据模型变更

### 5.1 `models` 表新增 `protocol` 字段

```sql
-- SQLite 迁移
ALTER TABLE models ADD COLUMN protocol TEXT NOT NULL DEFAULT 'openai';
-- 约束在应用层校验: protocol IN ('openai', 'anthropic')

-- MySQL/TiDB 迁移
ALTER TABLE models ADD COLUMN protocol VARCHAR(16) NOT NULL DEFAULT 'openai';
```

- `protocol` 决定该 model 上游说什么协议，进而决定出站鉴权头与是否翻译。
- 默认 `'openai'`，保证存量配置零行为变化。
- `model_groups` 不加字段：分组与协议正交，组内可混协议（见 §2）。

### 5.2 `api_keys` 表

不变。Anthropic 上游的 key 同样加密入库；区别仅在转发时用 `x-api-key` 而非 `Bearer`，由出站适配层决定。

### 5.3 `KeyHealth` 内存态

`ratelimit.KeyHealth` 新增一个只读字段（来自 model 配置，不入库）：

```python
@dataclass
class KeyHealth:
    # ... 现有字段 ...
    upstream_protocol: str = "openai"   # "openai" | "anthropic"，来自 model.protocol
    anthropic_version: str = "2023-06-01"  # 透传用默认；可由 model 配置覆盖
```

### 5.4 Schema 迁移策略

- 启动时 `store.py` 的迁移逻辑检测 `models` 表是否有 `protocol` 列，无则 `ALTER TABLE ADD COLUMN`。
- 存量数据全部默认 `'openai'`，不破坏现有行为。
- 迁移幂等（`PRAGMA table_info` 探测列存在性）。

---

## 6. 协议识别与路由

### 6.1 入站协议识别（`InboundDetector`）

依据优先级：**路径 > 头**。

| 路径 | 方法 | 入站协议 | 备注 |
|---|---|---|---|
| `/v1/messages` | POST | anthropic | Claude Code 主端点 |
| `/v1/messages/count_tokens` | POST | anthropic | token 计数（见 §12） |
| `/v1/chat/completions` | POST | openai | 现有 |
| `/v1/completions` | POST | openai | 现有（遗留） |
| `/v1/embeddings` | POST | openai | 现有 |
| `/v1/models` | GET | 本地 | 返回合并模型列表（见 §6.3） |
| `/healthz` `/version` | GET | 本地 | 不变 |

辅助判定（仅用于日志/纠错，不改变上述结论）：
- 头含 `anthropic-version` → 倾向 anthropic
- 头含 `x-api-key` 且无 `Authorization` → 倾向 anthropic
- 头含 `Authorization: Bearer` → 倾向 openai

### 6.2 路由改造

`proxy/server.py` 的路由从单一 `* /v1/{tail:.*}` 改为显式分发：

```python
app.router.add_post("/v1/messages", handler)             # anthropic 入站
app.router.add_post("/v1/messages/count_tokens", handler)
app.router.add_post("/v1/chat/completions", handler)     # openai 入站
app.router.add_post("/v1/completions", handler)
app.router.add_post("/v1/embeddings", handler)
app.router.add_route("*", "/v1/{tail:.*}", handler)      # 兜底透传（按路径识别协议）
```

`handler.__call__` 开头调用 `InboundDetector.detect(request)` 得到 `inbound_protocol`，后续逻辑都带这个上下文。

### 6.3 `/v1/models` 兼容

OpenAI 客户端与 Anthropic 客户端都会拉模型列表。返回结构以 OpenAI 格式为基底（`{object:"list", data:[{id, object:"model", ...}]}`）：

- Anthropic SDK 拉列表只读 `data[].id`，对多余字段无感，可复用同一响应。
- `id` 来源：`models.name` + `model_groups.name`（不变）。
- 不区分协议：同一个 model 名既可被 OpenAI 客户端也可被 Anthropic 客户端请求，由该 model 的 `protocol` 决定出站。

---

## 7. 鉴权处理

### 7.1 代理自身的 access token（入站鉴权）

现 `proxy/auth.py` 仅认 `Authorization: Bearer`。扩展为同时认 `x-api-key`，以兼容 Claude Code（它只发 `x-api-key`）：

```python
def _extract_access_token(request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    xak = request.headers.get("x-api-key", "")
    if xak:
        return xak.strip()
    return ""
```

- 两种入站协议共用同一组 `access_tokens`，无新增表。
- `proxy_auth_enabled()` 关闭时（本机默认）依然无鉴权直通。
- `/v1/models` GET 仍免鉴权（客户端模型发现需要）。
- 入站鉴权失败时，**错误信封按入站协议返回**（见 §11），否则 Claude Code 解析报错会很难看。

### 7.2 上游 key（出站鉴权）

由 `ProtocolAdapter` 在拼装上游 headers 时按 `model.protocol` 决定：

| 出站协议 | 鉴权头 | 额外头 |
|---|---|---|
| openai | `Authorization: Bearer <upstream_key>` | （现有） |
| anthropic | `x-api-key: <upstream_key>` | `anthropic-version: <version>`（默认 `2023-06-01`，透传客户端值或 model 配置覆盖） |

- Anthropic 上游的 `anthropic-version` 优先级：model 配置 > 客户端传入头 > 默认 `2023-06-01`。
- `anthropic-beta` 头：透传场景下原样透传；翻译场景下若上游是 Anthropic 则透传客户端的，若上游是 OpenAI 则丢弃（OpenAI 不认）。
- 移除 `Authorization`/`x-api-key`/`Host` 后再注入选中的 key，避免泄露客户端的 access token 到上游。

---

## 8. 请求翻译

### 8.1 总流程

```
client_request (inbound)
   │
   ▼
[parse body] → inbound_obj
   │
   ▼ inbound_protocol == outbound_protocol ?
   ├─ YES → passthrough (仅重写 model 名 + 注入上游 key 头)
   └─ NO  → translate_request(inbound_obj) → outbound_obj
            │
            ▼
            注入出站鉴权头 → 上游
```

`translate_request` 派发：

| inbound | outbound | 函数 |
|---|---|---|
| openai | anthropic | `translate_o2a.translate_request` |
| anthropic | openai | `translate_a2o.translate_request` |

### 8.2 OpenAI 请求 → Anthropic 上游（`translate_o2a.translate_request`）

**messages 拆分**：

1. 遍历 OpenAI `messages`，把所有 `role=="system"` 抽出，按顺序拼接成顶层 `system`（string；若含多模态则拼成 content block 数组）。
2. 剩余 messages 映射为 Anthropic `messages`：
   - `role:"user"` / `"assistant"` → 同名 role
   - `role:"tool"`（工具结果） → 转成 `{role:"user", content:[{type:"tool_result", tool_use_id:<tool_call_id>, content:<content>}]}`
3. **相邻同 role 合并**：Anthropic 要求 user/assistant 严格交替。OpenAI 允许连续多条 user，翻译时把连续同 role 的 content 合并成一条 message（content block 数组拼接）。

**content 映射**：

| OpenAI content | Anthropic content |
|---|---|
| `content: "字符串"` | `content: "字符串"`（或 `[{type:"text",text}]`，二选一保持一致） |
| `content: [{type:"text",text}]` | `[{type:"text",text}]` |
| `content: [{type:"image_url",image_url:{url:"data:<mime>;base64,<data>"}}]` | `[{type:"image", source:{type:"base64", media_type:<mime>, data:<data>}}]` |
| `content: [{type:"image_url",image_url:{url:"https://..."}}]` | `[{type:"image", source:{type:"url", url:"https://..."}}]` |
| assistant `tool_calls:[{id,type:"function",function:{name,arguments}}]` | content block `{type:"tool_use", id, name, input:<JSON.parse(arguments)>}`（arguments 反序列化为对象） |

**字段映射**：

| OpenAI | Anthropic | 规则 |
|---|---|---|
| `model` | `model` | 重写为 `upstream_model` |
| `max_tokens` | `max_tokens` | 缺失则补默认（见 §8.4） |
| `temperature` | `temperature` | 直传 |
| `top_p` | `top_p` | 直传 |
| `stop` (string) | `stop_sequences:[s]` | 包成数组 |
| `stop` (array) | `stop_sequences` | 直传 |
| `stream` | `stream` | 直传 |
| `tools` | `tools` | 见 §8.5 |
| `tool_choice` | `tool_choice` | 见 §8.5 |
| `n`,`presence_penalty`,`frequency_penalty`,`logprobs`,`seed`,`response_format`,`stream_options` | — | 丢弃；`response_format` 为 json_schema 时记 WARN（无法无损翻译） |

### 8.3 Anthropic 请求 → OpenAI 上游（`translate_a2o.translate_request`）

**system 注入**：顶层 `system`（string 或 block 数组）→ 在 messages 头部插入一条 `{role:"system", content:<拼接文本>}`。

**messages 映射**：

| Anthropic message | OpenAI message |
|---|---|
| `{role:"user", content:"str"}` | `{role:"user", content:"str"}` |
| `{role:"user", content:[{type:"text",text}, ...]}` | `{role:"user", content:[{type:"text",text}, ...]}` |
| `{role:"user", content:[{type:"tool_result", tool_use_id, content}]}` | `{role:"tool", tool_call_id:<tool_use_id>, content:<content 文本>}` |
| `{role:"user", content:[{type:"image", source:{base64/url}}]}` | `{role:"user", content:[{type:"image_url", image_url:{url}}]}` |
| `{role:"assistant", content:[{type:"text",text}]}` | `{role:"assistant", content:<text>}` |
| `{role:"assistant", content:[{type:"tool_use", id, name, input}]}` | `{role:"assistant", content:null, tool_calls:[{id, type:"function", function:{name, arguments:<JSON.stringify(input)>}}]}` |

- **拆分同 role 连续块**：Anthropic 一条 assistant message 可能同时含 text + 多个 tool_use。翻译为 OpenAI 时合并为一条 assistant（content=text，tool_calls=数组）。
- tool_result 在 Anthropic 里是 user message 的 content block；翻译为 OpenAI 时变 `role:"tool"`。若一个 user message 里同时有 text 和 tool_result，需拆成多条 OpenAI message（tool 结果 + 后续 user text）。

**字段映射**：

| Anthropic | OpenAI | 规则 |
|---|---|---|
| `model` | `model` | 重写为 `upstream_model` |
| `max_tokens` | `max_tokens` | 直传（OpenAI 可选，无碍） |
| `temperature` / `top_p` | 同名 | 直传 |
| `top_k` | — | 丢弃 |
| `stop_sequences` | `stop` | 数组直传；单元素也保持数组（OpenAI 接受） |
| `stream` | `stream` | 直传 |
| `tools` | `tools` | 见 §8.5 |
| `tool_choice` | `tool_choice` | 见 §8.5 |
| `metadata.user_id` | `user` | 映射（可选） |
| `cache_control` | — | 丢弃（OpenAI 无对应；记 DEBUG） |
| `anthropic-beta` 特性 | — | 丢弃 |

### 8.4 `max_tokens` 默认值

Anthropic 上游 `max_tokens` 必填。OpenAI 客户端常省略。策略：

1. 客户端显式给 → 用客户端值。
2. 客户端未给 → 用 model 配置 `max_tokens_default`（新增可选字段，见 §14）；未配则取 **4096**。
3. 翻译层在 O→A 时若补了默认值，记 INFO 日志便于排查。

### 8.5 tools / tool_choice 互转

**tools**：

```
OpenAI:   {type:"function", function:{name, description, parameters:<JSONSchema>}}
Anthropic:{name, description, input_schema:<JSONSchema>}
```

双向直接改结构。`parameters` ⇄ `input_schema` 同为 JSON Schema，原样搬运。

**tool_choice**：

| OpenAI | Anthropic | 备注 |
|---|---|---|
| `"auto"` | `{type:"auto"}` | |
| `"none"` | `{type:"auto"}`（无 none，靠提示约束） | Anthropic 无真正 none；翻译时记 WARN |
| `"required"` | `{type:"any"}` | OpenAI 1.x 的 required |
| `{type:"function", function:{name}}` | `{type:"tool", name}` | |
| `{type:"auto"}` | `{type:"auto"}` | |

反向（A→O）对称映射。

---

## 9. 非流式响应翻译

### 9.1 Anthropic 响应 → OpenAI 客户端（A 出 → O 入）

把 Anthropic 上游返回翻译成 OpenAI 格式回给 OpenAI 客户端：

```python
content_blocks = anthropic_resp["content"]
text_parts = [b["text"] for b in content_blocks if b["type"] == "text"]
tool_uses  = [b for b in content_blocks if b["type"] == "tool_use"]

openai_resp = {
    "id": anthropic_resp["id"],
    "object": "chat.completion",
    "created": <now>,
    "model": <client 请求的 model 名>,
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": "".join(text_parts) or None,
            "tool_calls": [
                {"id": t["id"], "type": "function",
                 "function": {"name": t["name"],
                              "arguments": json.dumps(t["input"], ensure_ascii=False)}}
                for t in tool_uses
            ] or None,
        },
        "finish_reason": MAP_STOP_REASON_A2O[anthropic_resp["stop_reason"]],
    }],
    "usage": {
        "prompt_tokens": anthropic_resp["usage"]["input_tokens"],
        "completion_tokens": anthropic_resp["usage"]["output_tokens"],
        "total_tokens": input_tokens + output_tokens,
    },
}
```

`MAP_STOP_REASON_A2O`：见 §3.4 表。`stop_sequence` 一并映射到 `stop`。

### 9.2 OpenAI 响应 → Anthropic 客户端（O 出 → A 入）

```python
msg = openai_resp["choices"][0]["message"]
content = []
if msg.get("content"):
    content.append({"type": "text", "text": msg["content"]})
for tc in msg.get("tool_calls") or []:
    content.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"] or "{}")})

anthropic_resp = {
    "id": openai_resp["id"],
    "type": "message",
    "role": "assistant",
    "model": <client 请求的 model 名>,
    "content": content,
    "stop_reason": MAP_FINISH_REASON_O2A[openai_resp["choices"][0]["finish_reason"]],
    "stop_sequence": None,
    "usage": {
        "input_tokens": openai_resp["usage"]["prompt_tokens"],
        "output_tokens": openai_resp["usage"]["completion_tokens"],
    },
}
```

`MAP_FINISH_REASON_O2A`：`stop`→`end_turn`（若命中 stop_sequence 则 `stop_sequence`，但 OpenAI 不区分，统一 `end_turn`），`length`→`max_tokens`，`tool_calls`→`tool_use`，`content_filter`→`end_turn`（无对应，降级 + WARN）。

---

## 10. SSE 流式翻译（核心难点）

翻译流式必须在两个 SSE 协议间做**有状态转换**，逐 chunk 读取上游、按状态机产出下游事件。两条方向各一个状态机。

### 10.1 OpenAI SSE → Anthropic SSE（`stream_o2a`）

> 场景：客户端是 Claude Code（Anthropic 入站），上游是 OpenAI 兼容。

**立即响应**：先发 `200 + text/event-stream` 头（与现有 `_stream_proxy` 一致，防客户端超时），再启 keepalive。

**状态机**：

```
状态: INIT
  收到首个 OpenAI chunk (delta.role="assistant" 或 delta.content)
    → 发 event: message_start
        data: {"message":{"id":<id>,"type":"message","role":"assistant",
                          "model":<model>,"content":[],
                          "usage":{"input_tokens":0,"output_tokens":0}}}
    → 发 event: ping  data:{}
    → 进入 TEXT_BLOCK 状态（若 delta.content 非空）
    → 发 event: content_block_start
        data:{"index":0,"content_block":{"type":"text","text":""}}

状态: TEXT_BLOCK
  delta.content 非空
    → 发 event: content_block_delta
        data:{"index":0,"delta":{"type":"text_delta","text":<delta.content>}}
  delta.tool_calls 出现 (首个 tool_use)
    → 发 event: content_block_stop  data:{"index":0}   # 关掉文本块
    → 进入 TOOL_BLOCK 状态，index=1
    → 发 event: content_block_start
        data:{"index":1,"content_block":{"type":"tool_use","id":<tc.id>,"name":<tc.function.name>,"input":{}}}

状态: TOOL_BLOCK
  delta.tool_calls[].function.arguments 增量
    → 发 event: content_block_delta
        data:{"index":<idx>,"delta":{"type":"input_json_delta","partial_json":<arguments 片段>}}
  出现新的 tool_call (index 递增)
    → 发 event: content_block_stop data:{"index":<idx>}
    → idx++ ; content_block_start (新 tool_use 块)

任意状态:
  finish_reason 出现 (或 data:[DONE])
    → 关闭当前 block: content_block_stop
    → 发 event: message_delta
        data:{"delta":{"stop_reason":MAP_FINISH_REASON_O2A[finish_reason],"stop_sequence":null},
              "usage":{"output_tokens":<累计>}}
    → 发 event: message_stop  data:{}
    → 结束
```

**usage 处理**：OpenAI 默认流式不带 usage。`output_tokens` 无法精确，策略：
- 若客户端开了 `stream_options.include_usage` 且上游给了末尾 usage → 用其 `completion_tokens`。
- 否则按 chars/4 粗估 `output_tokens`，`input_tokens` 用 0 或本地 tiktoken 估（见 §12.2）。仅用于日志/限流，不回传客户端（Anthropic 客户端会读 `message_delta.usage`，给估值即可）。

### 10.2 Anthropic SSE → OpenAI SSE（`stream_a2o`）

> 场景：客户端是 Cursor/Cline（OpenAI 入站），上游是 Anthropic。

**状态机**：

```
状态: INIT
  event: message_start
    → 发 data: {"id":<id>,"object":"chat.completion.chunk","created":<ts>,
                "model":<model>,"choices":[{"index":0,
                "delta":{"role":"assistant","content":""},"finish_reason":null}]}
    → 状态 TEXT_OPEN=false

event: content_block_start  (type=text)
  → 不发 chunk（OpenAI 首个 delta 已含 role）；TEXT_OPEN=true；index 记录

event: content_block_delta  (text_delta)
  → 发 data: {"choices":[{"index":0,
                "delta":{"content":<delta.text>},"finish_reason":null}]}

event: content_block_start  (type=tool_use)
  → TEXT_OPEN=false
  → 发 data: {"choices":[{"index":0,"delta":{"tool_calls":[{
                "index":<tool 序号>,"id":<id>,"type":"function",
                "function":{"name":<name>,"arguments":""}}]},"finish_reason":null}]}
  → 记录当前 tool index

event: content_block_delta  (input_json_delta)
  → 发 data: {"choices":[{"index":0,"delta":{"tool_calls":[{
                "index":<tool 序号>,"function":{"arguments":<partial_json>}}]},"finish_reason":null}]}

event: content_block_stop
  → (仅记录，不发 chunk；OpenAI 不需要 block 边界)

event: message_delta
  → 记录 stop_reason；不发 chunk（等 message_stop 一起发终态）

event: message_stop
  → 发 data: {"choices":[{"index":0,"delta":{},
                "finish_reason":MAP_STOP_REASON_A2O[<stop_reason>]}]}
  → 发 data: [DONE]
  → 结束

event: error
  → 见 §11 错误处理
```

### 10.3 流式翻译与多 key 重试的协调

现有 `_stream_proxy` 在发了 200 头后无限重试上游。翻译模式下：

- **重试只发生在「首个下游事件发出之前」**。一旦开始向客户端吐翻译后的事件（`message_start` / 首个 OpenAI delta），就**不再重试**——因为部分事件已发，无法回滚。
- 因此翻译流式的策略：缓冲上游到「确认 200 + 首个有效事件」才向客户端发首事件；上游在首事件前就 429/5xx → 切下一个 key（对客户端无感，仍在 keepalive 阶段）；上游在首事件后失败 → 视为成功路径中断，按 Anthropic `error` 事件或 OpenAI 错误 chunk 收尾。
- keepalive 期间（翻译前）发 SSE 注释 `: keepalive`，与现有逻辑一致。

### 10.4 翻译失败的兜底

- 翻译过程抛异常（如 JSON 解析失败）：入站是 anthropic → 发 `event: error` + Anthropic 错误信封后结束流；入站是 openai → 发一个 `data: [DONE]` 前的错误 chunk（或直接断流 + 记 ERROR 日志）。
- 永远不让翻译异常导致进程崩溃或 hang 住客户端连接。

---

## 11. 错误信封翻译

### 11.1 信封格式

| 协议 | 错误响应体 |
|---|---|
| OpenAI | `{"error":{"message":"...","type":"...","param":...,"code":"...}}` |
| Anthropic | `{"type":"error","error":{"type":"...","message":"..."}}` |

**规则**：错误信封始终按**入站协议**返回（客户端只认自己的格式）。上游返回的错误若协议不同，先归一化为内部错误对象，再按入站协议重新打包。

### 11.2 状态码与 type 映射

| HTTP 状态 | 场景 | OpenAI `type` | Anthropic `type` |
|---|---|---|---|
| 400 | 请求格式错 | `invalid_request_error` | `invalid_request_error` |
| 401 | 鉴权失败 | `invalid_request_error` | `authentication_error` |
| 403 | 权限不足 | `invalid_request_error` | `permission_error` |
| 404 | 模型不存在 | `invalid_request_error` | `not_found_error` |
| 413 | 请求过大 | `invalid_request_error` | `request_too_large` |
| 429 | 限流 | `rate_limit_error` | `rate_limit_error` |
| 500 | 上游内部错 | `api_error` | `api_error` |
| 503 | 过载 | `api_error` | `overloaded_error` |
| 529 | Anthropic 过载（透传） | `api_error` | `overloaded_error` |

### 11.3 代理自身错误

- `unknown_model`（503）：入站 openai → `{error:{message,type:"model_not_found"}}`；入站 anthropic → `{type:"error",error:{type:"not_found_error",message:...}}`。
- 全组 cooldown / 排队超时（503）：同上按入站协议打包，Anthropic 用 `overloaded_error`。
- 所有重试失败（502/503）：按入站协议打包，message 不含 upstream_base。

---

## 12. 边缘端点与计数

### 12.1 `/v1/messages/count_tokens`

Claude Code 在发大请求前会调此端点预估 input_tokens。

| 出站协议 | 处理 |
|---|---|
| anthropic | 透传到上游 `/v1/messages/count_tokens`，原样回传 `{input_tokens}` |
| openai | OpenAI 无此端点。本地估算：用 `tiktoken`（cl100k_base）近似计 input_tokens；无 tiktoken 则按 `len(text)/4` 估值。返回 `{input_tokens:<est>}`。仅用于客户端的配额预估，不要求精确。 |

请求体翻译同 §8（count_tokens 的 body 是 messages 子集，无 max_tokens）。

### 12.2 token 计数用于限流（TPM）

现有 TPM 限流依赖响应 usage。翻译模式下：

- A 出 → O 入：OpenAI 上游非流式有 usage → 正常累计。
- O 出 → A 入：Anthropic 上游流式 `message_delta` 带 `output_tokens`，`message_start` 带 `input_tokens` → 累计。
- O 出 → A 入（非流式）：Anthropic 非流式 usage 直接读。
- OpenAI 流式无 usage 时：output_tokens 估值（见 §10.1），input_tokens 用本地估算（无 tiktoken 则 0）。限流精度下降但不会失灵（RPM 维度仍精确）。

### 12.3 其它透传路径

`/v1/completions`、`/v1/embeddings` 维持纯 OpenAI 透传，不参与 Anthropic 翻译（Anthropic 客户端不会调这些）。

---

## 13. Claude Code 接入说明

### 13.1 客户端配置

Claude Code 通过环境变量配置：

```bash
# 指向本代理（默认无 TLS）
export ANTHROPIC_BASE_URL=http://127.0.0.1:8088
# 用代理的 access token（若启用 proxy_auth）；否则任意值
export ANTHROPIC_API_KEY=<zhongzhuan_access_token>
# 若代理开了 TLS，用 https:// 并信任自签证书
```

或在 Claude Code 的配置文件里设同名字段。

### 13.2 模型名约定

- Claude Code 默认请求 `model="claude-sonnet-4-5"` 之类。用户需在 zhongzhuan 后台建一个 **name = `claude-sonnet-4-5`** 的 model（或同名 group），指向真正的上游：
  - 上游是真 Anthropic → `protocol=anthropic`，`upstream_base=https://api.anthropic.com`，`upstream_model=claude-sonnet-4-5`。
  - 上游是 OpenAI 兼容（如想用 DeepSeek 冒充）→ `protocol=openai`，`upstream_base=https://api.deepseek.com/v1`，`upstream_model=deepseek-chat`，翻译层自动把 Anthropic 请求翻成 OpenAI。
- 也可建 group `claude-sonnet-4-5` 含多个 model 做 fallback（如：Anthropic 官方主 + DeepSeek 备）。

### 13.3 已知行为对齐

- Claude Code 会发 `anthropic-beta` 头（prompt caching 等）：透传到 Anthropic 上游 OK；翻译到 OpenAI 上游时这些 beta 特性丢失（cache_control 一并丢），功能可用但失去缓存收益。
- Claude Code 偶尔发 `count_tokens`：见 §12.1。
- Claude Code 期望 SSE 严格按 §3.5 事件序列；翻译状态机须保证 `message_start` → `content_block_*` → `message_delta` → `message_stop` 顺序，缺一客户端会报流解析错误。

---

## 14. 配置与后台变更

### 14.1 `models` 配置新增字段

后台「模型编辑」表单与 `/api/models` 增字段：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `protocol` | enum: `openai`\|`anthropic` | `openai` | 上游协议 |
| `anthropic_version` | string | `2023-06-01` | 透传给 Anthropic 上游的版本头（可选） |
| `max_tokens_default` | int | 4096 | O→A 翻译时 max_tokens 缺省补值（可选） |

### 14.2 `config.yaml`

无需新增（协议相关都在 DB model 配置里）。可选加一个全局开关：

```yaml
anthropic_compat:
  enabled: true        # false 时关闭 /v1/messages 路由，回退纯 OpenAI 行为
  default_max_tokens: 4096
```

### 14.3 后台 UI

- 模型列表/编辑：加「上游协议」下拉（OpenAI / Anthropic）。
- Key 池：不变（key 与协议解耦，协议在 model 上）。
- 日志：`request_logs` 增加可选列 `inbound_protocol`、`outbound_protocol`、`translated`（bool），便于排查翻译问题。迁移加列：

```sql
ALTER TABLE request_logs ADD COLUMN inbound_protocol TEXT DEFAULT '';
ALTER TABLE request_logs ADD COLUMN outbound_protocol TEXT DEFAULT '';
ALTER TABLE request_logs ADD COLUMN translated INTEGER DEFAULT 0;
```

---

## 15. 测试策略

### 15.1 单元测试（`tests/protocol/`）

- `test_detect.py`：路径 + 头 → 协议识别矩阵。
- `test_translate_o2a_request.py` / `test_translate_a2o_request.py`：
  - system 抽取与拼接（单条/多条/多模态）。
  - content block 互转（text/image/tool_use/tool_result）。
  - 相邻同 role 合并 / 拆分。
  - tools / tool_choice 互转。
  - max_tokens 缺省补值。
  - 丢弃字段告警。
- `test_translate_response.py`：非流式响应双向翻译 + stop_reason/finish_reason 映射。
- `test_errors.py`：错误信封双向转换 + 状态码映射。
- `test_stream_o2a.py` / `test_stream_a2o.py`：用录制好的 SSE chunk 序列喂状态机，断言产出事件序列（含 tool_use 中途出现、多 tool、error 中断）。

### 15.2 集成测试

- 复用 `tests/mock_upstream.py`，扩展为可模拟 OpenAI 与 Anthropic 两种上游。
- 四象限端到端：`test_e2e_anthropic_compat.py`
  - Claude Code 风格请求 → OpenAI mock 上游（翻译入）。
  - Claude Code 风格请求 → Anthropic mock 上游（透传）。
  - OpenAI 风格请求 → Anthropic mock 上游（翻译出）。
  - OpenAI 风格请求 → OpenAI mock 上游（原透传回归）。
  - 流式四象限同上。
- 多 key fallback 跨协议：Anthropic 入站，group 含 1 个 Anthropic model（429）+ 1 个 OpenAI model，验证 fallback 时自动翻译。

### 15.3 真实联调

- `test_real_claude_code.py`：起代理，Claude Code 指 base_url，跑一次真实对话 + 一次工具调用，断言不报错。
- 与现有 `test_real_e2e.py` / `test_real_agnes.py` 同级，需真实 key，CI 默认 skip。

---

## 16. 里程碑

| M | 内容 | 验收 |
|---|---|---|
| A1 | 协议识别 + 路由 + `models.protocol` 迁移 + 非流式纯透传（A 入→A 出） | curl 模拟 Claude Code `/v1/messages` 打通真 Anthropic 上游 |
| A2 | 非流式双向翻译（O↔A 请求 + 响应） | 四象限非流式 e2e 通过 |
| A3 | 错误信封翻译 + 鉴权兼容 x-api-key | 401/429/503 按入站协议正确返回 |
| A4 | SSE 双向翻译状态机 | 四象限流式 e2e 通过（含 tool_use 流） |
| A5 | count_tokens + 流式翻译与多 key 重试协调 | 翻译流式 fallback 不污染客户端流 |
| A6 | 后台 UI 加协议字段 + 日志列 + 文档 | 后台能配 Anthropic model；Claude Code 接入文档可照做 |

---

## 17. 风险与权衡

| 风险 | 权衡 / 缓解 |
|---|---|
| 翻译无法 100% 无损 | OpenAI 的 `n`/`logprobs`/`response_format`、Anthropic 的 `top_k`/`cache_control`/beta 特性无法跨协议对齐。翻译层丢弃并记 WARN，文档明示。功能性请求（对话 + 工具）可无损。 |
| SSE 翻译状态机复杂、易出 bug | 用录制 chunk 序列驱动单测；状态机实现保持纯函数（无副作用），便于断言；先支持 text + tool_use，image 流暂不支持翻译（透传场景才用）。 |
| 流式翻译后无法重试 | 限定「首事件前可重试，首事件后不重试」；首事件前用 keepalive 兜住客户端。极端情况（首事件后上游断）按错误事件收尾，不静默 hang。 |
| OpenAI 流式无 usage → TPM 限流不准 | RPM 维度仍精确；TPM 用估值，配置时留 buffer。可选引入 tiktoken 提升估值（新增可选依赖）。 |
| Anthropic API 版本演进 | `anthropic-version` 透传优先用客户端值；新增字段未识别时原样保留在 passthrough 路径，翻译路径按已知字段处理、未知字段丢弃。 |
| Claude Code 发 beta 头 | 透传场景无害；翻译到 OpenAI 场景丢失缓存能力，功能仍可用，文档提示。 |
| 跨协议 fallback 改变模型行为 | group 内混协议时，fallback 到不同协议模型可能语义跳变（如 OpenAI 模型不认 Anthropic 的 system 风格）。属用户配置选择，文档提示。 |
| 翻译层性能开销 | 单请求 JSON 改写 < 1ms，可忽略；SSE 翻译每个 chunk 多一次小对象构造，本机单用户场景无感。 |

---

## 18. 落地清单（给实现者）

1. `store/schema.py` + `store/store.py`：加 `models.protocol` / `models.anthropic_version` / `models.max_tokens_default` 列与迁移；加 `request_logs` 三列。
2. `store/models.py`：CRUD 增 `protocol` 等字段。
3. `proxy/ratelimit.py`：`KeyHealth` 加 `upstream_protocol` / `anthropic_version`。
4. `proxy/protocol/`：新建 `detect.py` / `adapter.py` / `translate_o2a.py` / `translate_a2o.py` / `stream_o2a.py` / `stream_a2o.py` / `errors.py`。
5. `proxy/handler.py`：`__call__` 与 `_stream_proxy` 接入 `InboundDetector` + `ProtocolAdapter`；按 `inbound_protocol == outbound_protocol` 分流透传/翻译。
6. `proxy/auth.py`：`_extract_access_token` 兼容 `x-api-key`；错误响应按入站协议打包。
7. `proxy/server.py`：显式路由 `/v1/messages*`。
8. `upstream/client.py`：`request`/`stream` 支持注入 Anthropic 鉴权头（由 adapter 传入 headers，client 本身不强绑协议）。
9. `admin/api_models.py` + `admin/ui.py`：模型表单加协议下拉。
10. `tests/protocol/` + `tests/test_e2e_anthropic_compat.py`：见 §15。

> 实现遵循现有「不引入大型框架 / 原生 SQL / vanilla JS / 不引 pywin32」约束。翻译层为纯 Python 函数，无新运行时依赖（tiktoken 为可选优化，非必需）。
