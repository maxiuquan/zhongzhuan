"""分层健康检查（T33 / R-P2-07、R-P2-08）。

R-P2-07：``/healthz`` 分层为
* **liveness** —— 进程 / 事件循环存活（进程活着就 200）；
* **readiness** —— 依赖就绪才 200：迁移完成 + ≥1 可用 route + worker lease 正常，
  迁移未完成返回 503；
* **dependency status** —— 每个依赖项（存储 / 上游 / 工具执行器）逐项状态。

R-P2-08：公开健康接口不泄露 key、内部 URL 或敏感拓扑。所有字段值在出网前
经过 :func:`sanitize_health_text`（复用 :func:`redact` 的密钥模式 + URL 模式），
:func:`find_leaks` 供测试用正则断言「零命中」。

三层字段约定（判据①逐层断言）：
* liveness  -> ``{"status", "component", "timestamp"}``
* readiness -> ``{"status", "checks": {migration: {...}, routes: {...}, worker: {...}}}``
* deps      -> ``{"status", "dependencies": [{"name", "status", "detail"}]}``
"""
from __future__ import annotations

import re
import time
from typing import Any

from ..proxy.protocol.responses_errors import redact

#: 健康响应体的最大字段值长度（防注入 / 防拖库）。
MAX_HEALTH_FIELD_CHARS: int = 256

#: R-P2-08：URL 模式（内部地址也不允许出现在公开健康响应里）。
_URL_PATTERN = re.compile(r"\bhttps?://[^\s\"'\]\},]+")

REDACTED: str = "[REDACTED]"


# ---------------------------------------------------------------------------
# 迁移完成判定（readiness 的核心）
# ---------------------------------------------------------------------------

MIGRATION_TABLE = "schema_migrations"


async def migration_status(store: Any | None) -> tuple[bool, str]:
    """返回 ``(完成?, 详情)``：``schema_migrations`` 最高版本 == 注册表最大版本。

    ``store`` 为 ``None``（proxy 无存储）时视为未就绪。
    """
    if store is None:
        return False, "store unavailable"
    try:
        rows = await store.fetchall(f"SELECT version FROM {MIGRATION_TABLE}")
    except Exception as exc:  # noqa: BLE001 - 健康检查绝不允许抛给调用方
        return False, f"migration check failed: {type(exc).__name__}"
    if not rows:
        return False, "no migrations applied"
    from ..store.migrations import MIGRATIONS
    expected = max(m.version for m in MIGRATIONS)
    applied = max(int(r[0]) for r in rows)
    if applied < expected:
        return False, f"migration incomplete: {applied} < {expected}"
    return True, f"migrations complete (v{applied})"


# ---------------------------------------------------------------------------
# 脱敏 / 泄露检测（R-P2-08）
# ---------------------------------------------------------------------------


def sanitize_health_text(value: str, *, max_chars: int = MAX_HEALTH_FIELD_CHARS) -> str:
    """脱敏一个健康字段值：先 redact（密钥），再替换 URL，最后截断。

    顺序有讲究：先替换 URL / 密钥，再截断 —— 敏感模式靠近截断边界时不会
    留下可识别的残片（与 T29 ``sanitize_text`` 同思路）。
    """
    text = str(value)
    # 1) 密钥模式 -> [REDACTED]（复用 responses_errors.redact）
    text = redact(text)
    # 2) URL 模式 -> [REDACTED]
    text = _URL_PATTERN.sub(REDACTED, text)
    # 3) 截断
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "... [truncated]"
    return text


def sanitize_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """递归脱敏整个健康 payload（URL / 密钥 / 长值）。"""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            out[key] = sanitize_health_payload(value)
        elif isinstance(value, list):
            out[key] = [
                sanitize_health_payload(v) if isinstance(v, dict)
                else sanitize_health_text(v) if isinstance(v, str)
                else v
                for v in value
            ]
        elif isinstance(value, str):
            out[key] = sanitize_health_text(value)
        else:
            out[key] = value
    return out


def find_leaks(text: str) -> list[str]:
    """扫描一段文本，返回发现的泄露模式列表（空列表 = 零泄露）。

    R-P2-08 判据②：公开健康响应体正则断言无 URL / 密钥模式。测试用这个函数
    断言 ``find_leaks(rendered_body) == []``。
    """
    found: list[str] = []
    for m in _URL_PATTERN.finditer(text):
        found.append(f"url:{m.group(0)[:40]}")
    for m in re.finditer(r"\bsk-[A-Za-z0-9_\-]{6,}", text):
        found.append(f"key:{m.group(0)[:40]}")
    for m in re.finditer(
        r"(?i)(\"?(?:api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret)\"?"
        r"\s*[:=]\s*)(?:\"|')?[A-Za-z0-9_\-.~+/=]{6,}",
        text,
    ):
        found.append("secret:" + m.group(0)[:40])
    return found


# ---------------------------------------------------------------------------
# 三层 payload 构造
# ---------------------------------------------------------------------------


def build_liveness(*, now: float | None = None) -> dict[str, Any]:
    """liveness：进程活着就 ok。"""
    return {
        "status": "ok",
        "component": "liveness",
        "timestamp": int(now) if now is not None else int(time.time()),
    }


def build_readiness(
    *,
    migration_ok: bool,
    migration_detail: str,
    routes_ok: bool,
    routes_detail: str,
    worker_ok: bool,
    worker_detail: str,
) -> tuple[dict[str, Any], int]:
    """readiness：迁移 + 路由 + worker 全部就绪才 200，否则 503。"""
    ok = migration_ok and routes_ok and worker_ok
    payload: dict[str, Any] = {
        "status": "ready" if ok else "not_ready",
        "checks": {
            "migration": {"ok": migration_ok, "detail": migration_detail},
            "routes": {"ok": routes_ok, "detail": routes_detail},
            "worker": {"ok": worker_ok, "detail": worker_detail},
        },
    }
    return payload, 200 if ok else 503


def build_dependency_status(
    dependencies: list[dict[str, Any]],
) -> dict[str, Any]:
    """dependency status：逐项状态。每项 ``{"name", "status", "detail"}``。"""
    all_ok = all(d.get("status") == "ok" for d in dependencies)
    return {
        "status": "ok" if all_ok else "degraded",
        "dependencies": dependencies,
    }


def dependency_item(
    name: str,
    ok: bool,
    detail: str = "",
    *,
    optional: bool = False,
) -> dict[str, Any]:
    """构造一个 dependency 项。``optional`` 项失败不导致整体 degraded。"""
    status = "ok" if ok else ("optional_unavailable" if optional else "down")
    return {"name": name, "status": status, "detail": detail}


__all__ = [
    "MIGRATION_TABLE",
    "migration_status",
    "sanitize_health_text",
    "sanitize_health_payload",
    "find_leaks",
    "build_liveness",
    "build_readiness",
    "build_dependency_status",
    "dependency_item",
]
