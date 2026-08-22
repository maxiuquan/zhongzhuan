# 中继日志「又断了」根因诊断（2026-08-20 / 08-21）

## 结论先行
中继进程**没有崩溃**（`systemctl is-active zhongzhuan` → active，PID 2809775，已运行 3h24m，内存 103MB）。
「断了」的真相：**两个流式请求在网关侧陷入了无限重试循环**，SSE 连接被一直挂着不返回，
Pi Agent 侧看到的是连接挂起 / 超时断开。

## 证据
- 卡住的请求：
  - `[7fc6e01d0550]` `POST /v1/chat/completions model='juhe/glm-5.2' stream=True`，17:04:40 进入，**截至 17:23:54 仍每 30s 重试**（已 ~19 分钟，零字节）。
  - `[7faece792010]` `model='juhe/mimo-v2.5-pro'`，108 轮失败，全部 `status=429 rate_limited`。
- 今天全量非 200 状态码：429×108、554×27、503×8、402×30、502×10…… 失败集中在这两个模型组。

## 根因 A — 上游 / 配置健康度（触发因素，非本地面代码 bug）
`juhe/glm-5.2` 组已无可用 key：
- key 300010 `ReadTimeout`（GLM 上游挂起）
- key 210003 `429 insufficient_quota`（workspace quota exceeded）
- 兜底组 300140 / 300123 → `503 model_not_found: No available channel for model …kimi-k3…`（**兜底映射指向不存在的 kimi-k3，等于废组**）
- key 300133 `554`（网关错误，空 body，反复出现，唯一"有响应"的 key）
- key 300135 `503 error`
→ 该组实际无健康 key，请求永远无法成功。

`juhe/mimo-v2.5-pro`（团长）组被限流：`429 insufficient_quota`。

## 根因 B — 本地面流式重试循环缺"死线"（「挂起/断开」的直接原因）
`src/zhongzhuan/proxy/handler.py : _stream_proxy`（约行 4780–5103）的 `while True` 重试：
- 仅对**异常类**失败（ReadTimeout/ConnectError）有熔断（行 5060 `if attempt>0 and all_same_type and first_exc_type`），立即 502。
- 对**状态码类**失败（554/503/429）走 `classify_failure` 冷却后，按注释 5054–5059 **故意**落入退避重试，而此分支**没有最大轮数 / 墙钟死线** → 退避封顶 30s 后无限循环。
- 后果：上游不恢复时连接被永久挂着、零字节 → Pi 侧 HTTP 响应永不结束 → 超时/断开 = "断了"。

> 与 8-20「3 天未重启冷却累积」是**不同根因**：那天是进程长跑致内存冷却累积 + 失败不进审计；
> 今天触发是上游本身退化 + 流式循环无死线。8-20 的 b404405 只加了门控审计/降级/reload 归位，**没给流式重试循环加死线**，故本次又挂。

## 责任划分与修复建议
1. 上游 / 配置侧（relay owner / provider，非客户端 hack）：
   - `juhe/glm-5.2`：下线/修复 554 的 300133；修正兜底组映射（kimi-k3 → glm-5.2），否则兜底等于废；核查 210003 quota。
   - `juhe/mimo-v2.5-pro`：核查限流/quota，必要时提额或降并发。
2. 本地面代码（可做，需拍板「改吧」）：
   - 给 `_stream_proxy` 加**墙钟死线**（如 `STREAM_HARD_DEADLINE_SECONDS=300/600`）：超时后走与现有熔断相同路径
     —— 写 SSE error 事件 + `_log_gate_failure` 审计 + 返回 502/504，而非无限挂起。
   - 这样即使上游退化，请求也快速失败，Pi 立刻收到错误而非挂死。

## 立即缓解（可选，治标）
重启中继可清掉两个卡死连接：`systemctl restart zhongzhuan`。但若上游仍退化，Pi 重连后会再次循环，
故根因修复仍是上面两条。
