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


def role_model_for(text: str) -> str:
    """从文本中解析角色标记（``[explorer]`` 等），命中返回对应模型；否则 ``""``。"""
    low = (text or "").lower()
    for tag in _ROLE_TAGS:
        if ("[" + tag + "]") in low:
            return ROLE_MODEL_MAP[tag]
    return ""


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

    existing = _extract_nonempty_message(args)
    if existing:
        return args  # FR-12d：正常调用原样透传

    # 内容落在非标准字段 → 迁移到 message（v3.1 决定性整改）
    migrated = _migrate_nonstandard_fields(args)
    if migrated is not None:
        return migrated

    # 空参 → 上下文合成（FR-12b）
    base = last_user_text.strip()
    if not base:
        return None  # FR-12c：上下文不足，调用方拒绝重试

    # 角色标记：优先从 leader 文本（团长推理/计划片段），其次合成 message 本身
    role_model = role_model_for(leader_text)
    role_tag = ""
    if not role_model:
        role_model = role_model_for(base)
    if role_model:
        for tag in _ROLE_TAGS:
            if ROLE_MODEL_MAP[tag] == role_model:
                role_tag = tag
                break

    msg = base
    if role_model:
        # 剥除 message 中的角色前缀后再注入 model（FR-12d：不破坏角色标记）
        low = msg.lower()
        for tag in _ROLE_TAGS:
            prefix = "[" + tag + "]"
            if low.startswith(prefix):
                msg = msg[len(prefix):].lstrip()
                break
    msg = msg + _ANTI_RECURSE_SUFFIX
    args["message"] = msg
    if role_model:
        args["model"] = role_model
        # v3.1 质量增强②：注入 agent_type 恢复 FR-8 角色路由（V1 handler 读
        # ``args.agent_type`` → ``role_name``）。
        if role_tag:
            args["agent_type"] = role_tag
    return args
