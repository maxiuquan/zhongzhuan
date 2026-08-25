"""FR-12（增量修订 v3.0）：spawn_agent 空参数补丁（中继侧）。

背景：v2.0 切「方案 A 透传」后，团长 juhe/mimo-v2.5-pro 稳定发出
``spawn_agent({})`` 空参（V1 schema ``required=None``，所有字段可选），
Codex 桌面端本地 ``SpawnAgentHandler`` 因 message 为空拒执行 → 子代理
零执行、无产物、团长不汇总。客户端层（AGENTS.md / PreToolUse hook /
schema）已穷尽无解，唯一落点在中继侧：透传回桌面端**之前**拦截并补参。

本模块提供纯函数（可单测）：
- :func:`extract_last_user_text` —— 从父会话 input 提取最近一条 user 文本。
- :func:`patch_spawn_agent_arguments` —— 空参时合成自包含 message +
  角色路由注入 model + 防递归后缀；非空参原样返回（FR-12d 回归保护）。
- :func:`role_model_for` —— 角色标记 → 模型映射（复用 v1.9 C.13.2 值）。
- :func:`detect_role_tag` —— 角色识别的唯一判定点（前缀优先、子串兜底），
  multi_agent._spawn 与本模块共用，消除两套匹配语义的漂移。
- :func:`inject_role_model` —— FR-8 角色路由注入（model + agent_type），
  供空参合成与非标准字段迁移两条路径复用。
"""

from __future__ import annotations

import json
from typing import Any

#: 角色 → 模型（与 multi_agent.ROLE_MODEL_MAP 同源；C.13.2 live 回归验证值）。
ROLE_MODEL_MAP: dict[str, str] = {
    "explorer": "juhe/deepseek-v4-flash",
    "tester": "juhe/deepseek-v4-flash",
    "implementer": "juhe/glm-5.2",
    "docwriter": "juhe/qwen3.7-flash",
    "scrubber": "juhe/agnes-2.5-flash",
}

#: 角色标记前缀（团长文本 / message 中 `[explorer]` 等）。
_ROLE_TAGS: tuple[str, ...] = tuple(ROLE_MODEL_MAP.keys())

#: 合成 message 追加的防递归后缀（FR-12b.5 / 风险 R4）。
_ANTI_RECURSE_SUFFIX = (
    "\n\n(独立完成任务并直接回报结果，不要再次调用 spawn_agent 派生子代理。)"
)


def detect_role_tag(text: str) -> tuple[str, str]:
    """识别文本中的专家团角色标记，返回 ``(role_tag, model)``；未命中 ``("", "")``。

    v3.2 整改：这是 :func:`role_model_for`（子串匹配）与
    :meth:`multi_agent.MultiAgentOrchestrator._spawn`（前缀匹配）两条历史路径
    **统一后的共享判定**——前缀命中（``[explorer] 任务…``）优先，未命中再退回
    子串扫描（团长文本里标记可能出现在任意位置）。两条路径从此对同一个输入
    给出同一个角色结论。
    """
    low = (text or "").lower()
    for tag in _ROLE_TAGS:
        if low.startswith("[" + tag + "]"):
            return tag, ROLE_MODEL_MAP[tag]
    for tag in _ROLE_TAGS:
        if ("[" + tag + "]") in low:
            return tag, ROLE_MODEL_MAP[tag]
    return "", ""


def role_model_for(text: str) -> str:
    """从文本中解析角色标记（``[explorer]`` 等），命中返回对应模型；否则 ``""``。

    判定统一走 :func:`detect_role_tag`（前缀优先、子串兜底）。
    """
    return detect_role_tag(text)[1]


def inject_role_model(args: dict[str, Any], *texts: str) -> dict[str, Any]:
    """FR-8 角色路由注入：按给定文本顺序解析角色标记，命中时注入 ``model``
    与 ``agent_type``。原地修改并返回同一个 ``args``。

    抽取自合成路径的收尾段，供**空参合成**与**非标准字段迁移**两条路径复用
    （v3.2 整改：迁移分支此前提前 return，丢掉了角色注入）。角色与模型取自
    同一次 :func:`detect_role_tag` 判定——历史上「先解析模型再反查 tag」在
    两个角色共享同一模型（explorer/tester）时会把 ``agent_type`` 定错。
    """
    for text in texts:
        tag, model = detect_role_tag(text)
        if tag:
            args["model"] = model
            # v3.1 质量增强②：注入 agent_type 恢复 FR-8 角色路由（V1 handler
            # 读 ``args.agent_type`` → ``role_name``）。
            args["agent_type"] = tag
            return args
    return args


def _strip_leading_role_tag(msg: str) -> str:
    """剥除 message 开头的角色前缀（FR-12d：不破坏角色标记本身）。"""
    low = (msg or "").lower()
    for tag in _ROLE_TAGS:
        prefix = "[" + tag + "]"
        if low.startswith(prefix):
            return msg[len(prefix):].lstrip()
    return msg


def extract_last_user_text(input_items: Any) -> str:
    """从父会话 input 中提取**最近一条** user 消息的纯文本。

    兼容 Responses 形态（``{"type":"message","role":"user","content":[...]}``
    content 数组含 ``input_text``/``text``/``input_image`` 等）与 chat 形态
    （``{"role":"user","content":"..."}`` 字符串）。取最后一条非空 user 文本。
    """
    if not isinstance(input_items, (list, tuple)):
        return ""
    last = ""
    for item in input_items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        item_type = str(item.get("type") or "")
        if role == "reasoning" or item_type == "reasoning":
            continue
        if role not in ("user",):
            continue
        content = item.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") in ("input_text", "text", "output_text") and c.get("text"):
                    parts.append(str(c["text"]))
            text = " ".join(parts)
        if text and text.strip():
            last = text.strip()
    return last


def _extract_nonempty_message(args: dict[str, Any]) -> str:
    """取 V1 权威字段 ``message`` 中 trim 后非空者（v3.1：V1 handler 只读
    ``args.message``，``task_name`` 是 V2 才有的字段；``instruction``/``task``/
    ``content`` 等**不视为有效**——否则 mimo 发 ``{"task":...}`` 会被当有效
    原样透传，而桌面端 V1 handler 读不到 message → 仍零执行）。
    """
    val = args.get("message")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return ""


def _migrate_nonstandard_fields(args: dict[str, Any]) -> dict[str, Any] | None:
    """把非标准字段（``task``/``instruction``/``name``/``description`` 等）的
    非空内容**迁移到 ``message``**（9.6.3 决定性整改项）：V1 handler 只读
    ``args.message``，内容落在别处等于没给。返回补 `message` 后的 dict；
    无任何非空内容时返回 ``None``（调用方继续走空参合成）。
    """
    out = dict(args)
    for key in ("instruction", "task", "name", "description", "content", "prompt"):
        val = out.get(key)
        if isinstance(val, str) and val.strip():
            if "message" not in out or not str(out.get("message") or "").strip():
                out["message"] = val.strip()
                return out
    return None


def patch_spawn_agent_arguments(
    raw_args: Any,
    last_user_text: str,
    leader_text: str = "",
) -> dict[str, Any] | None:
    """FR-12 核心：空参补丁（纯函数）。

    解析 ``raw_args``（dict 或 JSON 字符串）：
    - 已含非空 ``message`` → 原样返回（FR-12d：正常调用不改写）。
    - 内容落在非标准字段（``task``/``instruction``/``name`` 等）→ **迁移到
      ``message``**（v3.1 决定性整改：V1 handler 只读 message）。
    - 全空：从上下文合成非空自包含 ``message``（最近用户消息 + 角色标记剥离 +
      防递归后缀），并重放 FR-8 角色路由注入 ``model`` 与 ``agent_type``。
    - 上下文不足（无用户文本）→ 返回 ``None``（调用方落 FR-12c 拒绝重试）。

    返回补参后的 arguments dict；``raw_args`` 无法解析时按空参处理。
    """
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except (ValueError, TypeError):
            args = {}
    elif isinstance(raw_args, dict):
        args = dict(raw_args)
    else:
        args = {}
    # json.loads 可能成功解析出非 dict（"null"/"[]"/"123"）——下游一律 .get()，
    # 非 dict 按空参处理，防止 AttributeError 冲出流式消费循环炸掉 SSE。
    if not isinstance(args, dict):
        args = {}

    existing = _extract_nonempty_message(args)
    if existing:
        return args  # FR-12d：正常调用原样透传

    # 内容落在非标准字段 → 迁移到 message（v3.1 决定性整改）。
    # v3.2 整改：迁移分支此前直接 return，丢掉了下方合成路径才有的角色注入
    # （``{"task": "[explorer] …"}`` 迁移后 model/agent_type 恒缺，FR-8 角色路由
    # 断路）。现在复用同一小函数注入 model/agent_type；message 本身保持迁移
    # 原样（回归测试断言 message 逐字不变，且角色标记留给下游剥除）。
    migrated = _migrate_nonstandard_fields(args)
    if migrated is not None:
        return inject_role_model(migrated, str(migrated.get("message") or ""), leader_text)

    # 空参 → 上下文合成（FR-12b）
    base = last_user_text.strip()
    if not base:
        return None  # FR-12c：上下文不足，调用方拒绝重试

    msg = _strip_leading_role_tag(base) + _ANTI_RECURSE_SUFFIX
    args["message"] = msg
    # 角色标记：优先从 leader 文本（团长推理/计划片段），其次合成 message 本身。
    return inject_role_model(args, leader_text, base)
