"""Responses v3 feature-flag evaluation (T22 / R-P0-25).

The evaluator is intentionally small and side-effect free.  Request-level
precedence is:

    environment hard override > model > group > global enabled > default true

Key rollout is evaluated separately while candidate upstream keys are being
filtered, because a request may have several candidate keys and excluding one
must not disable v3 for the remaining candidates.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_ENV_NAMES = ("ZHONGZHUAN_RESPONSES_BRIDGE_V3", "RESPONSES_BRIDGE_V3")


class ResponsesFeatureFlags:
    """Evaluate the Responses v3 global/model/group/key rollout."""

    def __init__(self, config: Any = None, *, environ: Mapping[str, str] | None = None) -> None:
        self._config = config
        # Keep os.environ live so an operational hard override affects the next
        # request; tests may inject an ordinary mapping for deterministic cases.
        self._environ = os.environ if environ is None else environ

    def _environment_override(self) -> bool | None:
        for name in _ENV_NAMES:
            raw = self._environ.get(name)
            if raw is None:
                continue
            normalized = str(raw).strip().lower()
            if normalized in _TRUE:
                return True
            if normalized in _FALSE:
                return False
            # load_config rejects this at startup.  If an operator mutates the
            # process environment afterwards, fail closed for v3 rather than
            # raising from the request path.
            return False
        return None

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


__all__ = ["ResponsesFeatureFlags"]
