"""客户端指纹头模板变量渲染。

支持变量
--------
* ``{{uuid}}`` —— 每次调用生成新 UUID4 字符串。用于 ``X-Request-ID`` 等需要
  每请求唯一的头。

设计
----
纯函数、无网络、无状态。找不到对应变量的占位符原样保留（不报错），便于
调试：用户在自定义头里写错变量名也能看到原始占位符，而非静默吞掉。

P1 将扩展 ``{{ts}}`` / ``{{ts_ms}}`` / ``{{random:N}}`` 等。
"""

from __future__ import annotations

import re
import uuid

#: 匹配 ``{{var}}`` 占位符。非贪婪, 允许一行中出现多个变量。
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _replace(match: re.Match[str]) -> str:
    var = match.group(1).lower()
    if var == "uuid":
        return str(uuid.uuid4())
    # 未知变量原样保留（含原始大小写与空格）
    return match.group(0)


def render(value: str) -> str:
    """渲染模板字符串。

    >>> render("abc")
    'abc'
    >>> render("{{uuid}}")  # doctest: +SKIP
    'a3b4c5d6-...'  # 每次不同
    >>> render("id={{uuid}}&keep={{unknown}}")  # doctest: +SKIP
    'id=a3b4c5d6...&keep={{unknown}}'
    """
    if not value or "{{" not in value:
        return value
    return _VAR_RE.sub(_replace, value)
