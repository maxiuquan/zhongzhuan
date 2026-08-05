"""内置上游客户端指纹预设。

约定
----
* 预设 key 即 ``models.client_preset`` 的存值（``""`` 和 ``"custom"`` 除外）。
* 空字符串 ``""`` 表示不模拟，``"custom"`` 表示自定义，**均不在** ``PRESETS``
  字典中。
* :func:`list_presets` 只返回内置预设，不包含"不模拟"和"自定义"——前端负责
  在列表头部加"不模拟"、尾部加"自定义"。这保证了"自定义"永远在最后一位，
  未来新增预设自动排在它之前。

新增预设
--------
在 ``PRESETS`` 字典中添加一条即可，例如::

    PRESETS["another_client"] = {
        "label": "AnotherClient (xxx 限免)",
        "headers": [...],
    }

新预设自动出现在下拉列表中"自定义"之前，无需改动前端结构。
"""

from __future__ import annotations

import json
import logging
from typing import Any

_log = logging.getLogger(__name__)

#: 内置预设字典。按插入顺序排列，前端据此生成下拉中间选项。
PRESETS: dict[str, dict[str, Any]] = {
    "workbuddy": {
        "label": "WorkBuddy (freemodel.dev 限免)",
        # 以下头为真实 WorkBuddy 5.3.8 客户端抓包所得（2026-08-05 实测，
        # 缺失这些指纹头时 freemodel.dev 返回 403 unsupported_client）。
        # 动态变量 {{uuid}} 每次请求生成新值；X-Conversation-* 用固定模板
        # 即可通过校验（上游校验存在性，不校验会话一致性）。
        # require_system=True：上游（freemodel.dev）还通过请求体里的 system
        # 消息识别 WorkBuddy 来源，转发/测试时请求体缺 system 需自动补一条。
        # fingerprint_system：特征 system 内容模板，{model} 会被替换为上游模型名。
        # 注意：请求体自带非特征 system（如 Trae 的 "powered by TRAE"）也会被
        # 上游识别为异类而 403，因此注入逻辑会把该内容强制放在 messages 最前面。
        "require_system": True,
        "fingerprint_system": "This conversation is powered by {model}",
        "headers": [
            ("User-Agent", "WorkBuddy/5.3.8 WorkBuddy/5.3.8 CLI/2.115.0"),
            ("X-Requested-With", "XMLHttpRequest"),
            ("X-Request-ID", "{{uuid}}"),
            ("X-IDE-Type", "WorkBuddy"),
            ("X-IDE-Name", "WorkBuddy"),
            ("X-IDE-Version", "5.3.8"),
            ("X-CodeBuddy-Request", "1"),
            ("X-Domain", "www.codebuddy.cn"),
            ("X-Product", "SaaS"),
            ("X-Conversation-ID", "{{uuid}}"),
            ("X-Conversation-Request-ID", "{{uuid}}"),
            ("X-Stainless-Arch", "x64"),
            ("X-Stainless-Lang", "js"),
            ("X-Stainless-Os", "Windows"),
            ("X-Stainless-Package-Version", "6.25.0"),
            ("X-Stainless-Runtime", "node"),
            ("X-Stainless-Runtime-Version", "v22.21.1"),
            ("Accept", "application/json"),
            ("Cache-Control", "no-cache"),
        ],
    },
}

#: 受控头黑名单：自定义模式下禁止设置这些头，防止误操作覆盖关键头。
#: P0 禁止 Authorization（走 key 注入逻辑）；P1 可考虑放开。
_FORBIDDEN_HEADERS = frozenset(
    {
        "content-length",
        "transfer-encoding",
        "host",
        "connection",
        "authorization",
    }
)


def get_headers(preset_name: str) -> list[tuple[str, str]]:
    """返回预设的 ``(name, value_template)`` 列表；预设不存在返回空列表。

    返回的是新列表，调用方可安全修改。
    """
    preset = PRESETS.get(preset_name)
    if not preset:
        return []
    return [(name, value) for name, value in preset["headers"]]


def needs_system_message(preset_name: str) -> bool:
    """判断预设是否要求请求体携带 system 消息（``require_system`` 标记）。

    部分上游（如 freemodel.dev）通过请求体中的 system 消息识别客户端来源
    （WorkBuddy 请求必带系统提示词），缺失会返回 403 unsupported_client。
    测试 Key 与正常转发在注入指纹头时，若请求体缺 system 消息且本函数
    返回 True，则自动补一条。
    """
    preset = PRESETS.get(preset_name)
    if not preset:
        return False
    return bool(preset.get("require_system", False))


def get_fingerprint_system_prefix(preset_name: str, model_name: str = "") -> str:
    """返回预设要求的 system 消息特征内容（``fingerprint_system`` 模板）。

    用于 ``require_system`` 预设：无论请求体缺 system 还是自带非特征 system
    （如 Trae 的 "powered by TRAE"），都必须在 messages 最前面放一条该内容，
    上游才能识别为目标客户端。支持 ``{model}`` 占位符。
    无模板时返回默认的 "This conversation is powered by {model}"。
    """
    preset = PRESETS.get(preset_name)
    tpl = ""
    if preset:
        tpl = str(preset.get("fingerprint_system", "") or "")
    if not tpl:
        tpl = "This conversation is powered by {model}"
    try:
        return tpl.replace("{model}", str(model_name or ""))
    except Exception:
        return tpl


def list_presets() -> list[dict[str, str]]:
    """返回内置预设列表 ``[{key, label}]``，按字典插入顺序排列。

    前端需自行在头部加"不模拟"、尾部加"自定义"。
    """
    return [{"key": k, "label": v["label"]} for k, v in PRESETS.items()]


def is_valid_preset_name(name: str) -> bool:
    """校验 ``client_preset`` 取值是否合法。

    合法值：``""`` (不模拟) / ``"custom"`` (自定义) / ``PRESETS`` 中的 key。
    """
    if not name:
        return True
    if name == "custom":
        return True
    return name in PRESETS


def validate_custom_header_name(name: str) -> str | None:
    """校验自定义头名称。返回错误消息字符串，合法返回 ``None``。"""
    if not name or not name.strip():
        return "Header 名称不能为空"
    stripped = name.strip()
    if stripped.lower() in _FORBIDDEN_HEADERS:
        return f"不允许设置受控头: {stripped}"
    if len(stripped) > 128:
        return "Header 名称过长（最多128字符）"
    return None


def parse_custom_headers(raw: str) -> list[tuple[str, str]]:
    """解析 ``Model.custom_headers`` JSON 字符串为有序 ``[(name, value)]``。

    用于加载链（``_load_keys_from_store``）。解析失败或格式不符时记 warning
    并返回空列表（该 key 退化为不注入），避免单条配置错误拖垮整个启动。

    容忍的格式：``[{"name":"X-Foo","value":"bar"}, ...]``。每项必须含非空
    ``name``；``value`` 缺省按空字符串处理。
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        _log.warning("custom_headers JSON 解析失败, 退化为不注入: %s", e)
        return []
    if not isinstance(data, list):
        _log.warning("custom_headers 不是 JSON 数组, 退化为不注入")
        return []
    out: list[tuple[str, str]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            _log.warning("custom_headers[%d] 不是对象, 跳过", i)
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        value = str(item.get("value", ""))
        out.append((name, value))
    return out


def serialize_custom_headers(headers: list[tuple[str, str]]) -> str:
    """将 ``[(name, value)]`` 序列化为 JSON 字符串供 DB 存储。"""
    return json.dumps(
        [{"name": n, "value": v} for n, v in headers],
        ensure_ascii=False,
    )
