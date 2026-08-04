#!/usr/bin/env python3
"""Generate the versioned OpenAI Responses compatibility report (T38 / R-P1-50).

The report is a **generated artifact**: every row below is the single source
of truth, and ``docs/v3/03-兼容报告.md`` is produced by running::

    python scripts/gen_compat_report.py

CI runs the same generator with ``--check`` (``scripts/gen_compat_report.py
--check``) and fails when the committed report drifts from the generator
output (R-P1-50 criterion ②: CI validates report coverage).

State vocabulary (四态, defined in docs/v3/01-PRD-需求池.md §3.3):
    * 通过             —— end-to-end verified against a real native Responses
                         upstream (真机)
    * 直通可用          —— native passthrough path verified with a mock
                         upstream that asserts the request side (mock回放),
                         awaiting a real-key pass
    * 无执行器-标准错误  —— recognised surface answered with the standard
                         400 ``unsupported_tool`` error (framework-ready)
    * 明确不做          —— out of scope for this delivery, exempted by the
                         §3.3 ruling (needs explicit sign-off)

Every row also carries a 验证方式 (verification method) ∈ {真机, mock回放,
单测}.  No fuzzy wording ("部分支持 / 基本可用 / 已适配") is allowed in the
generated report; the generator refuses to emit such rows.

The status matrix is intentionally *declarative*: it mirrors the actual
implementation surveyed at commit time (see the per-row ``evidence`` notes
which cite the exact source location), so the report cannot silently claim a
capability that does not exist.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Target versions (single source: the schema the bridge implements)
# ---------------------------------------------------------------------------

#: OpenAI Responses API schema version targeted by the bridge
#: (src/zhongzhuan/proxy/protocol/responses_schema.py:387).
TARGET_API_VERSION = "2025-03-26"
#: OpenAI Python SDK version exercised by the contract suite.
TARGET_PY_SDK = "openai>=1.40 (contract suite ran against 2.53.0)"
#: OpenAI TypeScript SDK version exercised by the contract suite.
TARGET_TS_SDK = "openai-node>=4.104.0"

# ---------------------------------------------------------------------------
# State vocabulary helpers
# ---------------------------------------------------------------------------

PASS = "通过"
PASSTHROUGH = "直通可用"
NO_EXECUTOR = "无执行器-标准错误"
NOT_DONE = "明确不做"

REAL = "真机"
MOCK = "mock回放"
UNIT = "单测"

#: Every allowed status; anything else (e.g. "部分支持") is a generator error.
ALLOWED_STATUSES = frozenset({PASS, PASSTHROUGH, NO_EXECUTOR, NOT_DONE})
#: Every allowed verification method.
ALLOWED_METHODS = frozenset({REAL, MOCK, UNIT})

#: Phrases that are never allowed in the generated report (R-P1-50 criterion ①).
FORBIDDEN_PHRASES = (
    "部分支持",
    "基本可用",
    "已适配",
    "大部分支持",
    "待完善",
    "可能支持",
)


@dataclass(frozen=True)
class Row:
    """One compatibility row: name + status + verification + evidence."""

    name: str
    status: str
    method: str
    note: str = ""
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.status not in ALLOWED_STATUSES:
            raise ValueError(f"invalid status {self.status!r} for row {self.name!r}")
        if self.method not in ALLOWED_METHODS:
            raise ValueError(f"invalid method {self.method!r} for row {self.name!r}")


@dataclass
class Section:
    """A named matrix in the report."""

    title: str
    columns: tuple[str, ...]
    rows: list[Row] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Single source of truth: the compatibility matrix
# ---------------------------------------------------------------------------

#: Endpoint matrix — the six official Responses endpoints.
ENDPOINT_ROWS = [
    Row("POST /v1/responses (create, non-stream)", PASSTHROUGH, MOCK,
        "真实 upstream 执行链（capability 路由 → key 调度 → Responses→Chat/Anthropic 翻译 → 统一 resp_<uuid> → 终态持久化）；请求侧断言路径为 /v1/responses",
        "src/zhongzhuan/proxy/handler.py:348 (_dispatch_v3_create), tests/test_proxy_v3_create.py"),
    Row("POST /v1/responses (create, stream=true)", NO_EXECUTOR, UNIT,
        "流式 create 仍走 resource skeleton，返回 in_progress JSON（非真实 SSE）；完整 SSE 管线已实现但未接生产路径，诚实标注为本次不交付项",
        "src/zhongzhuan/proxy/handler.py:1120-1124, src/zhongzhuan/responses_v3/endpoints.py:84-104"),
    Row("GET /v1/responses/{id} (retrieve)", PASS, UNIT,
        "真实 ResponseStore 读取，支持租户边界（token:{id} workspace）",
        "src/zhongzhuan/responses_v3/endpoints.py:108-121, tests/test_proxy_v3_create.py"),
    Row("DELETE /v1/responses/{id} (delete)", PASS, UNIT,
        "真实 ResponseStore 删除，租户守卫",
        "src/zhongzhuan/responses_v3/endpoints.py:124-137"),
    Row("POST /v1/responses/{id}/cancel", PASS, UNIT,
        "真实 ResponseStore 状态更新（set_cancelled），租户守卫",
        "src/zhongzhuan/responses_v3/endpoints.py:140-161"),
    Row("POST /v1/responses/compact", NO_EXECUTOR, UNIT,
        "诚实 501（not_implemented）：compact 语义（token 压缩）未实现，返回标准错误而非伪造成功",
        "src/zhongzhuan/responses_v3/endpoints.py:164-177"),
    Row("GET /v1/responses/{id}/input_items", PASS, UNIT,
        "真实 ResponseStore 分页读取（seq 游标，after < -1 才 400）",
        "src/zhongzhuan/responses_v3/endpoints.py:180-202, tests/test_responses_v3.py"),
]

#: Response object field matrix — top-level fields of GET /v1/responses/{id}.
FIELD_ROWS = [
    Row("id", PASS, UNIT, "统一 resp_<uuid>（fork 点生成，写回上游返回对象）",
        "src/zhongzhuan/proxy/handler.py:291-294"),
    Row("object", PASS, UNIT, "恒值 response",
        "src/zhongzhuan/responses_v3/schema.py:20"),
    Row("created_at", PASS, UNIT, "store 行真实时间戳",
        "src/zhongzhuan/responses_v3/schema.py:21"),
    Row("model", PASSTHROUGH, MOCK, "客户端请求 model（别名感知），翻译产物不泄漏上游 model id",
        "src/zhongzhuan/proxy/protocol/responses.py:185-236, T37 修复"),
    Row("status", PASS, UNIT, "store 行真实状态（in_progress/completed/cancelled/failed）",
        "src/zhongzhuan/responses_v3/schema.py:22"),
    Row("output", PASS, UNIT, "store 行真实 output items",
        "src/zhongzhuan/responses_v3/schema.py:24"),
    Row("usage", PASS, UNIT, "store 行真实 usage",
        "src/zhongzhuan/responses_v3/schema.py:25"),
    Row("error", PASS, UNIT, "store 行真实 error",
        "src/zhongzhuan/responses_v3/schema.py:26"),
    Row("incomplete_details", PASS, UNIT, "store 行 + terminal_reason 注入",
        "src/zhongzhuan/responses_v3/schema.py:45-47"),
    Row("previous_response_id", PASS, UNIT, "store 行真实（chain 恢复）",
        "src/zhongzhuan/responses_v3/schema.py:30, src/zhongzhuan/responses_v3/chain.py"),
    Row("background", PASS, UNIT, "store 行真实（background 任务标记）",
        "src/zhongzhuan/responses_v3/schema.py:31"),
    Row("store", PASS, UNIT, "请求回显（store=true 持久化 / store=false 不持久化）；本地 store 行为，不依赖上游 key",
        "src/zhongzhuan/responses_v3/schema.py:41, tests/test_proxy_v3_create.py"),
    Row("instructions", NOT_DONE, UNIT, "schema 输出恒 None：本交付不宣称指令字段回显",
        "src/zhongzhuan/responses_v3/schema.py:28"),
    Row("metadata", NOT_DONE, UNIT, "schema 输出恒 {}：本交付不宣称 metadata 回显",
        "src/zhongzhuan/responses_v3/schema.py:29"),
    Row("tools", NOT_DONE, UNIT, "schema 输出恒 []：请求级 tools 由 create 翻译链消费，响应对象不回显",
        "src/zhongzhuan/responses_v3/schema.py:32"),
    Row("tool_choice", NOT_DONE, UNIT, "schema 输出恒 auto",
        "src/zhongzhuan/responses_v3/schema.py:33"),
    Row("parallel_tool_calls", NOT_DONE, UNIT, "schema 输出恒 True",
        "src/zhongzhuan/responses_v3/schema.py:34"),
    Row("temperature", NOT_DONE, UNIT, "schema 输出恒 None",
        "src/zhongzhuan/responses_v3/schema.py:35"),
    Row("top_p", NOT_DONE, UNIT, "schema 输出恒 None",
        "src/zhongzhuan/responses_v3/schema.py:36"),
    Row("max_output_tokens", NOT_DONE, UNIT, "schema 输出恒 None",
        "src/zhongzhuan/responses_v3/schema.py:37"),
    Row("text", NOT_DONE, UNIT, "schema 输出恒 None",
        "src/zhongzhuan/responses_v3/schema.py:38"),
    Row("truncation", NOT_DONE, UNIT, "schema 输出恒 None",
        "src/zhongzhuan/responses_v3/schema.py:39"),
    Row("user", NOT_DONE, UNIT, "schema 输出恒 None",
        "src/zhongzhuan/responses_v3/schema.py:40"),
    Row("include", NOT_DONE, UNIT, "schema 输出恒 []；reasoning.encrypted_content 回放划入 v3.1",
        "src/zhongzhuan/responses_v3/schema.py:42, docs/v3/00-交付决策记录.md D7.1"),
    Row("stream", NOT_DONE, UNIT, "schema 输出恒 False（响应对象为 JSON 非 SSE）",
        "src/zhongzhuan/responses_v3/schema.py:43"),
    Row("reasoning (item)", PASS, UNIT, "reasoning item 占位对象（summary/content 空，不落库明文）",
        "src/zhongzhuan/proxy/protocol/item_registry.py:191-208, docs/v3/00-交付决策记录.md D7.1"),
    Row("speech (item)", NOT_DONE, UNIT, "本交付不宣称 speech item 支持",
        "src/zhongzhuan/proxy/protocol/item_registry.py:32"),
]

#: Item type matrix — input/output item support (18 official types).
ITEM_ROWS = [
    Row("message", PASS, UNIT, "input + output",
        "src/zhongzhuan/proxy/protocol/item_registry.py:90-109"),
    Row("function_call", PASS, UNIT, "output-only",
        "src/zhongzhuan/proxy/protocol/item_registry.py:41-57"),
    Row("function_call_output", PASS, UNIT, "input-only",
        "src/zhongzhuan/proxy/protocol/item_registry.py:41-57"),
    Row("reasoning", PASS, UNIT, "output-only；明文不落库（铁律 1）",
        "src/zhongzhuan/proxy/protocol/item_registry.py:191-208"),
    Row("custom_tool_call", NO_EXECUTOR, UNIT, "识别+序列化，无执行器（标准 400 unsupported_tool）",
        "src/zhongzhuan/proxy/protocol/item_registry.py:90-109, src/zhongzhuan/responses_v3/hosted_tools.py"),
    Row("custom_tool_call_output", NO_EXECUTOR, UNIT, "识别+序列化，无执行器",
        "src/zhongzhuan/proxy/protocol/item_registry.py:90-109"),
    Row("file_search_call", NO_EXECUTOR, UNIT, "框架就绪，无执行器（T26 判据：标准 400）",
        "src/zhongzhuan/responses_v3/hosted_tools.py"),
    Row("web_search_call", NO_EXECUTOR, UNIT, "框架就绪，无执行器（T26 判据：标准 400）",
        "src/zhongzhuan/responses_v3/hosted_tools.py"),
    Row("computer_call", NO_EXECUTOR, UNIT, "框架就绪，无执行器",
        "src/zhongzhuan/responses_v3/hosted_tools.py"),
    Row("computer_call_output", NO_EXECUTOR, UNIT, "框架就绪，无执行器",
        "src/zhongzhuan/responses_v3/hosted_tools.py"),
    Row("code_interpreter_call", NOT_DONE, UNIT, "§3.3 #11：本地 Code Interpreter 沙箱执行器明确不做",
        "docs/v3/01-PRD-需求池.md:319"),
    Row("image_generation_call", NOT_DONE, UNIT, "§3.3 范围裁定：本次不交付",
        "docs/v3/01-PRD-需求池.md §3.3"),
    Row("local_shell_call", NOT_DONE, UNIT, "§3.3 范围裁定：本地 shell 执行器明确不做",
        "docs/v3/01-PRD-需求池.md §3.3"),
    Row("local_shell_call_output", NOT_DONE, UNIT, "随 local_shell_call",
        "docs/v3/01-PRD-需求池.md §3.3"),
    Row("mcp_call", PASSTHROUGH, MOCK, "完整实现（真实 HTTP MCP client，T27）；默认关闭需 opt-in",
        "src/zhongzhuan/responses_v3/mcp_client.py, src/zhongzhuan/proxy/protocol/responses_models.py:312"),
    Row("mcp_list_tools", PASSTHROUGH, MOCK, "完整实现（T27）",
        "src/zhongzhuan/responses_v3/mcp_client.py"),
    Row("mcp_approval_request", PASSTHROUGH, MOCK, "完整实现（T27 审批流程）",
        "src/zhongzhuan/responses_v3/mcp_client.py"),
    Row("mcp_approval_response", PASSTHROUGH, MOCK, "完整实现（T27 审批响应）",
        "src/zhongzhuan/responses_v3/mcp_client.py"),
]

#: Event matrix — streaming SSE events (honest: stream create is 本次不交付).
EVENT_ROWS = [
    Row("response.created", NOT_DONE, UNIT, "完整管线已实现（ResponsePipeline / ResponsesEventEmitter）但流式 create 未接生产路径",
        "src/zhongzhuan/responses_v3/pipeline.py:468-471, src/zhongzhuan/proxy/protocol/responses_emitter.py:142-166"),
    Row("response.in_progress", NOT_DONE, UNIT, "同上（skeleton 返回 in_progress JSON 而非事件）",
        "src/zhongzhuan/responses_v3/pipeline.py:472-475"),
    Row("response.completed", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:431-439"),
    Row("response.failed", NOT_DONE, UNIT, "同上（strict 模式事件）",
        "src/zhongzhuan/responses_v3/pipeline.py:402-421"),
    Row("response.incomplete", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:402-421"),
    Row("response.output_item.added", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:204-211"),
    Row("response.output_item.done", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:289-328"),
    Row("response.content_part.added", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:272-279"),
    Row("response.content_part.done", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:272-279"),
    Row("response.output_text.delta", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:213-222"),
    Row("response.output_text.done", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:341-349"),
    Row("response.function_call_arguments.delta", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:257-267"),
    Row("response.function_call_arguments.done", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:278-288"),
    Row("response.reasoning_summary_text.*", NOT_DONE, UNIT, "同上（legacy 桥已实现 ReasoningEventMode）",
        "src/zhongzhuan/proxy/protocol/responses_bridge.py:311-397"),
    Row("response.reasoning_text.*", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/proxy/protocol/responses_bridge.py:311-397"),
    Row("heartbeat (: hb)", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py:557, src/zhongzhuan/proxy/protocol/responses_emitter.py:319-323"),
    Row("[DONE]", NOT_DONE, UNIT, "同上",
        "src/zhongzhuan/responses_v3/pipeline.py SSE_DONE_FRAME"),
]

#: v2/v3 result differences (criterion ③) — derived from the survey, does not
#: re-invoke any real tool.
V2_V3_DIFFS = [
    ("create（非流式）", "v2：translate 到 Chat/Anthropic，返回翻译后的 Responses 对象，不持久化",
     "v3：真实 upstream 链 + 统一 resp_<uuid> + store=true 终态持久化（可 retrieve）"),
    ("create（流式）", "v2：真实 SSE 流（ResponsesStreamTranslator 翻译上游事件）",
     "v3：返回 in_progress skeleton JSON（SSE 管线已实现未接线，本次不交付）"),
    ("retrieve / delete / cancel / input_items", "v2：无（GET/DELETE /v1/responses 返回 405）",
     "v3：真实 store 端点，支持租户边界 + 分页"),
    ("compact", "v2：无（compact 未注册，走 405/翻译路径）",
     "v3：诚实 501（not_implemented）"),
    ("hosted tool", "v2：translate 路径忽略 tool 能力声明",
     "v3：capability 路由（NATIVE>EMULATE>TRANSLATE），无执行器 → 标准 400 unsupported_tool / 503"),
    ("请求事实解析", "v2：handler 内联解析",
     "v3：RequestSanitizer 统一事实源（sticky 与 capability 路由共用）"),
]

#: §3.3 明确不做豁免清单（5 项 + reasoning 回放）。
NOT_DONE_EXEMPTIONS = [
    ("本地 Code Interpreter 沙箱执行器", "§3.3 #11：安全边界超出网关职责，由 MCP 或原生上游提供"),
    ("本地 Computer use 执行器", "§3.3 #12：同 #7 安全边界"),
    ("本地 vector store 实现", "§3.3 #13：通过 MCP 接入外部向量库"),
    ("Go / Java / .NET SDK 合约测试", "§3.3 #14：Python+TS 覆盖 >90% 实际使用量，三语言 CI 投入产出比过低"),
    ("多实例 SQLite 部署", "§3.3 #15：SQLite = 单实例，多实例强制 TiDB/MySQL"),
    ("reasoning.encrypted_content 回放", "交付决策记录 D7.1：回放能力划入 v3.1"),
]

#: 待真机复验清单（拿到原生 key 后一次性跑通，R-P1-50 补充硬要求 3）。
REAL_KEY_VERIFY_LIST = [
    "原生 Responses 上游真机：非流式 create 端到端（断言请求路径 /v1/responses、body 未被降级改写、Authorization 已替换为上游 key）",
    "原生 Responses 上游真机：全部 7 类官方 hosted tool 端到端",
    "原生 Responses 上游真机：differential test（与官方 OpenAI 输出 schema 一致性对比）",
    "流式 create 真实 SSE 管线（ResponsePipeline 接线后真机验证事件序列）",
    "MCP hosted tool 真机（开启 mcp_enabled 后端到端）",
]

# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _row_table(section: Section) -> str:
    """Render one section as a markdown table (name/status/verification/note/evidence)."""
    header = "| " + " | ".join(section.columns) + " |"
    sep = "|" + "|".join("---" for _ in section.columns) + "|"
    lines = [header, sep]
    for row in section.rows:
        cells = [row.name, row.status, row.method, row.note, row.evidence]
        # Escape pipes inside cell text.
        cells = [c.replace("|", "\\|") for c in cells]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _count_by(section: Section, col_index: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in section.rows:
        val = row.status if col_index == 1 else row.method
        counts[val] = counts.get(val, 0) + 1
    return counts


def _status_summary(sections: list[Section]) -> str:
    """Roll up status counts across all sections."""
    total: dict[str, int] = {}
    for section in sections:
        for row in section.rows:
            total[row.status] = total.get(row.status, 0) + 1
    parts = " · ".join(f"{k} {v}" for k, v in sorted(total.items(), key=lambda kv: -kv[1]))
    return parts or "（空）"


def _validate(sections: list[Section]) -> None:
    """Reject fuzzy wording anywhere in the generated report (criterion ①)."""
    for section in sections:
        for row in section.rows:
            blob = f"{section.title} {row.name} {row.status} {row.method} {row.note} {row.evidence}"
            for phrase in FORBIDDEN_PHRASES:
                if phrase in blob:
                    raise ValueError(
                        f"forbidden phrase {phrase!r} in section {section.title!r} row {row.name!r}"
                    )


def _collect_exemptions() -> str:
    lines = ["", "| 项 | 豁免依据 |", "|---|---|"]
    for name, basis in NOT_DONE_EXEMPTIONS:
        lines.append(f"| {name} | {basis} |")
    return "\n".join(lines)


def _collect_verify_list() -> str:
    lines = [""]
    for i, item in enumerate(REAL_KEY_VERIFY_LIST, 1):
        lines.append(f"{i}. {item}")
    return "\n".join(lines)


def _collect_diffs() -> str:
    lines = ["", "| 能力 | v2（legacy） | v3（bridge） |", "|---|---|---|"]
    for name, v2, v3 in V2_V3_DIFFS:
        safe_v2 = v2.replace("|", "\\|")
        safe_v3 = v3.replace("|", "\\|")
        lines.append(f"| {name} | {safe_v2} | {safe_v3} |")
    return "\n".join(lines)


def build_report() -> str:
    sections = [
        Section("一、端点（六条官方 Responses 路由）", ("端点", "状态", "验证方式", "说明", "证据"), ENDPOINT_ROWS),
        Section("二、Response 对象字段（GET /v1/responses/{id}）", ("字段", "状态", "验证方式", "说明", "证据"), FIELD_ROWS),
        Section("三、item 类型（input/output）", ("item", "状态", "验证方式", "说明", "证据"), ITEM_ROWS),
        Section("四、streaming 事件", ("事件", "状态", "验证方式", "说明", "证据"), EVENT_ROWS),
    ]
    _validate(sections)

    lines: list[str] = []
    lines.append("# ZhongZhuan v3 · OpenAI Responses 兼容报告")
    lines.append("")
    lines.append("> **生成物**：本文件由 `scripts/gen_compat_report.py` 自动生成，禁止手改。")
    lines.append("> 修改状态矩阵请编辑生成器，然后运行 `python scripts/gen_compat_report.py` 重新生成；")
    lines.append("> CI 的 `compat-report` job 以 `--check` 模式校验提交物与生成器输出一致（R-P1-50 判据②）。")
    lines.append("")
    lines.append(f"**目标 API 版本**：OpenAI Responses API `{TARGET_API_VERSION}`"
                 f"（`src/zhongzhuan/proxy/protocol/responses_schema.py:387`）")
    lines.append("")
    lines.append(f"**目标 SDK 版本**：Python `{TARGET_PY_SDK}`；TypeScript `{TARGET_TS_SDK}`")
    lines.append("")
    lines.append("## 状态与验证方式词汇表")
    lines.append("")
    lines.append("| 状态 | 含义 | 验证方式 | 含义 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| {PASS} | 端到端验证通过（本地 store 端点：真实 store + 真实 HTTP 集成测试，不依赖上游 key） | {UNIT} | 单元/集成测试（含真实 ProxyServer.app() + 真实 SQLite store） |")
    lines.append(f"| {PASSTHROUGH} | 依赖上游 key 的路径经 mock 回放验证（请求侧强断言），待真机复验 | {MOCK} | mock 录制回放（tests/support/mock_responses_upstream.py） |")
    lines.append(f"| {NO_EXECUTOR} | 已识别表面，标准 400/501/流式 skeleton 错误（框架就绪） | {UNIT} | 单元/集成测试 |")
    lines.append(f"| {NOT_DONE} | 本次交付明确不做（v3.1 或 §3.3 豁免） |  |  |")
    lines.append("")
    lines.append("> 铁律：本报告**禁止**使用「部分支持 / 基本可用 / 已适配」等模糊措辞（R-P1-50 判据①）。")
    lines.append("> 当前环境无原生 Responses 上游 key，凡标注 `直通可用` 的条目均以 mock 回放验证，")
    lines.append("> 并列入文末「待真机复验清单」。")
    lines.append("")
    lines.append(f"**状态汇总**：{_status_summary(sections)}")
    lines.append("")

    for section in sections:
        lines.append(f"## {section.title}")
        lines.append("")
        lines.append(_row_table(section))
        lines.append("")

    lines.append("## 五、v2 / v3 结果差异（R-P1-50 判据③）")
    lines.append("")
    lines.append("> 本表为静态对照记录，不重复调用任何真实工具。")
    lines.append(_collect_diffs())
    lines.append("")

    lines.append("## 六、明确不做豁免清单（§3.3 裁定，需 team-lead 签字）")
    lines.append("")
    lines.append("> 依据 docs/v3/01-PRD-需求池.md §3.3 与 docs/v3/00-交付决策记录.md D7.1。")
    lines.append(_collect_exemptions())
    lines.append("")

    lines.append("## 七、待真机复验清单（R-P1-50 补充硬要求 3）")
    lines.append("")
    lines.append("> 拿到原生 Responses 上游 key 后，一次性跑通以下清单并把对应条目升级为 `通过` / `真机`。")
    lines.append(_collect_verify_list())
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed report matches generator output; exit 1 on drift",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "docs" / "v3" / "03-兼容报告.md"),
        help="output path (default: docs/v3/03-兼容报告.md)",
    )
    args = parser.parse_args(argv)

    report = build_report()
    out = Path(args.out)

    if args.check:
        if not out.exists():
            print(f"compat report missing: {out}", file=sys.stderr)
            return 1
        current = out.read_text(encoding="utf-8")
        if current != report:
            print("compat report drift detected: regenerate with "
                  "`python scripts/gen_compat_report.py`", file=sys.stderr)
            return 1
        print("compat report up-to-date")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"compat report written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
