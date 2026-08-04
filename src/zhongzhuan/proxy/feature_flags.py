"""Responses v3 feature-flag evaluation (T22 / R-P0-25).

The evaluator is intentionally small and side-effect free.  Request-level
precedence is:

    environment hard override > model > group > global enabled > default true

Key rollout is evaluated separately while candidate upstream keys are being
filtered, because a request may have several candidate keys and excluding one
must not disable v3 for the remaining candidates.

T04 / P0-8 adds :meth:`ResponsesFeatureFlags.audit_record`: a structured,
five-field record describing *which* implementation is in force and *why*.  The
startup hook writes exactly one such line so an operator reading the log can
always answer "was this box serving v3 or the v2 emergency path?" without
guessing from request traffic.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_ENV_NAMES = ("ZHONGZHUAN_RESPONSES_BRIDGE_V3", "RESPONSES_BRIDGE_V3")

#: The two implementations a request can be served by (AC-8.5 vocabulary).
VERSION_V3 = "v3"
VERSION_V2_EMERGENCY = "v2_emergency"

#: Where the effective switch value came from.
SOURCE_ENV = "env"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "default"

#: AC-8.1: the audit line must carry all five fields, in this order.
AUDIT_FIELDS: tuple[str, ...] = (
    "operator",
    "timestamp",
    "reason",
    "effective_version",
    "source",
)


class ResponsesFeatureFlags:
    """Evaluate the Responses v3 global/model/group/key rollout."""

    def __init__(self, config: Any = None, *, environ: Mapping[str, str] | None = None) -> None:
        self._config = config
        # Keep os.environ live so an operational hard override affects the next
        # request; tests may inject an ordinary mapping for deterministic cases.
        self._environ = os.environ if environ is None else environ

    def _environment_override_entry(self) -> tuple[str, bool] | None:
        """Return ``(env_var_name, value)`` for the winning hard override."""
        for name in _ENV_NAMES:
            raw = self._environ.get(name)
            if raw is None:
                continue
            normalized = str(raw).strip().lower()
            if normalized in _TRUE:
                return name, True
            if normalized in _FALSE:
                return name, False
            # load_config rejects this at startup.  If an operator mutates the
            # process environment afterwards, fail closed for v3 rather than
            # raising from the request path.
            return name, False
        return None

    def _environment_override(self) -> bool | None:
        entry = self._environment_override_entry()
        return None if entry is None else entry[1]

    @property
    def bridge_config(self) -> Any:
        """The ``responses_bridge`` config section (may be ``None``).

        Exposed so collaborators such as ``PipelineConfig.from_config`` can
        reach ``responses_bridge.timeout.*`` (AC-7.4) without reading a
        private attribute off the evaluator.
        """
        return self._config

    @property
    def _rollout(self) -> Any:
        return getattr(self._config, "rollout", None)

    def v3_enabled(self, ctx: Any) -> bool:
        """Return whether this Responses request should use the v3 bridge."""
        override = self._environment_override()
        if override is not None:
            return override

        requested_model = str(getattr(ctx, "requested_model", "") or "")
        rollout = self._rollout
        models = getattr(rollout, "models", {}) if rollout is not None else {}
        groups = getattr(rollout, "groups", {}) if rollout is not None else {}
        if requested_model in models:
            return bool(models[requested_model])
        if requested_model in groups:
            return bool(groups[requested_model])

        if self._config is not None and hasattr(self._config, "enabled"):
            return bool(self._config.enabled)
        return True

    def v3_key_allowed(self, key_id: int) -> bool:
        """Return whether one candidate key participates in the v3 rollout."""
        override = self._environment_override()
        if override is not None:
            return override
        rollout = self._rollout
        keys = getattr(rollout, "keys", {}) if rollout is not None else {}
        if key_id in keys:
            return bool(keys[key_id])
        return True

    # ------------------------------------------------------------------
    # T04 / P0-8: switch audit
    # ------------------------------------------------------------------

    def effective_switch(self) -> tuple[bool, str]:
        """Return ``(v3_on, source_label)`` for the *global* switch.

        This deliberately ignores the per-model / per-group / per-key rollout
        maps: those are request-scoped and cannot be summarised in a single
        startup line.  The audit answers the coarse question an operator asks
        during an incident — "is the emergency v2 switch flipped?".
        """
        entry = self._environment_override_entry()
        if entry is not None:
            name, value = entry
            return value, f"{SOURCE_ENV}:{name}"
        if self._config is not None and hasattr(self._config, "enabled"):
            return bool(self._config.enabled), f"{SOURCE_CONFIG}:responses_bridge.enabled"
        return True, SOURCE_DEFAULT

    def effective_version(self) -> str:
        """``"v3"`` when the bridge is on, ``"v2_emergency"`` otherwise."""
        on, _source = self.effective_switch()
        return VERSION_V3 if on else VERSION_V2_EMERGENCY

    def audit_record(
        self,
        source: str | None = None,
        reason: str = "boot",
        *,
        operator: str = "startup",
        now: datetime | None = None,
    ) -> dict[str, str]:
        """Build the five-field switch-audit record (AC-8.1).

        Args:
            source: Override for the detected switch source.  Defaults to the
                auto-detected label (``env:<VAR>`` / ``config:<path>`` /
                ``default``) so callers cannot silently mislabel an override.
            reason: Free-form cause, e.g. ``"boot"`` or ``"ops rollback"``.
            operator: Who flipped it; ``"startup"`` for the boot-time line.
            now: Injectable clock for deterministic tests.

        Returns:
            A ``dict`` with exactly the :data:`AUDIT_FIELDS` keys, all strings.
        """
        on, detected_source = self.effective_switch()
        moment = now or datetime.now(timezone.utc)
        return {
            "operator": str(operator),
            "timestamp": _iso_utc(moment),
            "reason": str(reason),
            "effective_version": VERSION_V3 if on else VERSION_V2_EMERGENCY,
            "source": str(source) if source else detected_source,
        }

    def log_audit_record(
        self,
        source: str | None = None,
        reason: str = "boot",
        *,
        operator: str = "startup",
        now: datetime | None = None,
        logger: Any = None,
    ) -> dict[str, str]:
        """Emit the audit line through ``logger`` and return the record."""
        record = self.audit_record(source, reason, operator=operator, now=now)
        if logger is None:  # pragma: no cover - trivial default wiring
            from loguru import logger as _logger

            logger = _logger
        logger.info(format_audit_line(record))
        return record


def _iso_utc(moment: datetime) -> str:
    """Render a datetime as an ISO-8601 UTC instant ending in ``Z``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def format_audit_line(record: Mapping[str, str]) -> str:
    """Render an audit record as one greppable ``[v3-switch] k=v ...`` line."""
    parts = [f"{name}={record.get(name, '')}" for name in AUDIT_FIELDS]
    return "[v3-switch] " + " ".join(parts)


__all__ = [
    "AUDIT_FIELDS",
    "SOURCE_CONFIG",
    "SOURCE_DEFAULT",
    "SOURCE_ENV",
    "VERSION_V2_EMERGENCY",
    "VERSION_V3",
    "ResponsesFeatureFlags",
    "format_audit_line",
]
