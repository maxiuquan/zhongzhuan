"""Six-layer timeout policy for upstream calls (T01 / R-P0-01 / R-P0-02).

Historically the whole proxy shared a single ``limits.proxy_request_timeout``
(30 seconds) which was mapped onto httpx as an overall read timeout.  That is
far too small for reasoning models: a Codex style request routinely needs
60-180 seconds before the first token arrives, so the connection was cut long
before the upstream had a chance to answer.

This module replaces the single knob with six independent layers:

===========================  =======  ==================================================
field                        default  meaning
===========================  =======  ==================================================
``connect_seconds``             15.0  TCP + TLS handshake budget
``first_token_seconds``        600.0  request sent -> first upstream byte received
``read_idle_seconds``          600.0  max silence between two streaming chunks
``total_seconds``             1800.0  wall clock budget of a single upstream attempt
``write_seconds``               60.0  budget for pushing the request body out
``pool_seconds``                30.0  budget for acquiring a pooled connection
===========================  =======  ==================================================

``first_token_seconds`` and ``read_idle_seconds`` have a **hard floor of 300
seconds**.  Configuring anything below the floor raises
:class:`TimeoutConfigError` at startup - it is a configuration error, not a
warning, because a too-small value silently breaks every long running request.

YAML shape (top level section)::

    timeouts:
      connect_seconds: 15
      first_token_seconds: 600
      read_idle_seconds: 600
      total_seconds: 1800
      write_seconds: 60
      pool_seconds: 30

Every field can be overridden by an environment variable named
``ZHONGZHUAN_TIMEOUT_<FIELD>`` (e.g. ``ZHONGZHUAN_TIMEOUT_FIRST_TOKEN_SECONDS``).
Precedence is ``env > YAML > default``; :func:`resolve_timeouts` reports the
winning source for every field so the startup banner can be audited by ops.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any, Mapping

__all__ = [
    "TimeoutConfigError",
    "TimeoutPolicy",
    "resolve_timeouts",
    "format_effective_timeouts",
    "log_effective_timeouts",
    "DEFAULT_TIMEOUT_POLICY",
    "MIN_FIRST_TOKEN_SECONDS",
    "MIN_READ_IDLE_SECONDS",
    "ENV_PREFIX",
    "SOURCE_DEFAULT",
    "SOURCE_YAML",
    "SOURCE_ENV",
]

# Hard floors (R-P0-01).  Below these values long running reasoning requests
# are guaranteed to be truncated, so we refuse to start.
MIN_FIRST_TOKEN_SECONDS: float = 300.0
MIN_READ_IDLE_SECONDS: float = 300.0

ENV_PREFIX: str = "ZHONGZHUAN_TIMEOUT_"

SOURCE_DEFAULT: str = "default"
SOURCE_YAML: str = "YAML"
SOURCE_ENV: str = "env"


class TimeoutConfigError(ValueError):
    """Raised when the timeout configuration is invalid.

    This is intentionally fatal: ``__main__`` lets it propagate so a bad
    ``timeouts.*`` section aborts startup instead of silently degrading every
    long running request.
    """


@dataclass(frozen=True)
class TimeoutPolicy:
    """Immutable, validated set of the six upstream timeout layers.

    All values are seconds.  ``total_seconds`` accepts ``0`` which means
    "no hard wall clock limit for a single attempt"; every other field must be
    strictly positive.
    """

    connect_seconds: float = 15.0
    first_token_seconds: float = 600.0
    read_idle_seconds: float = 600.0
    total_seconds: float = 1800.0
    write_seconds: float = 60.0
    pool_seconds: float = 30.0

    def __post_init__(self) -> None:
        # dataclass is frozen -> normalise through object.__setattr__.
        for f in fields(self):
            object.__setattr__(self, f.name, _coerce_seconds(getattr(self, f.name), f.name))
        self._validate()

    # ---- validation ----

    def _validate(self) -> None:
        """Enforce positivity, the 300s floors and internal consistency."""
        for name in ("connect_seconds", "first_token_seconds", "read_idle_seconds", "write_seconds", "pool_seconds"):
            value = getattr(self, name)
            if value <= 0:
                raise TimeoutConfigError(f"timeouts.{name} must be > 0 seconds, got {value!r}")
        if self.total_seconds < 0:
            raise TimeoutConfigError(
                f"timeouts.total_seconds must be >= 0 (0 disables the hard limit), got {self.total_seconds!r}"
            )
        if self.first_token_seconds < MIN_FIRST_TOKEN_SECONDS:
            raise TimeoutConfigError(
                f"timeouts.first_token_seconds must be >= {MIN_FIRST_TOKEN_SECONDS:.0f} "
                f"seconds (reasoning models need minutes before the first token), "
                f"got {self.first_token_seconds!r}"
            )
        if self.read_idle_seconds < MIN_READ_IDLE_SECONDS:
            raise TimeoutConfigError(
                f"timeouts.read_idle_seconds must be >= {MIN_READ_IDLE_SECONDS:.0f} "
                f"seconds (upstream may stay silent between chunks), "
                f"got {self.read_idle_seconds!r}"
            )
        if self.total_seconds > 0:
            longest_leg = max(self.first_token_seconds, self.read_idle_seconds)
            if self.total_seconds < longest_leg:
                raise TimeoutConfigError(
                    f"timeouts.total_seconds ({self.total_seconds}) must be >= "
                    f"max(first_token_seconds, read_idle_seconds) ({longest_leg}); "
                    f"a single attempt can never outlive its own total budget"
                )

    # ---- derived values ----

    @property
    def read_timeout_seconds(self) -> float:
        """Read timeout handed to httpx.

        httpx exposes a single ``read`` knob that covers both "waiting for the
        first byte" and "waiting for the next chunk", so the transport level
        value must be the *looser* of the two.  The stricter per-phase
        semantics (first token vs. idle) are enforced by the streaming
        pipeline, which owns the phase information.
        """
        return max(self.first_token_seconds, self.read_idle_seconds)

    @property
    def has_total_limit(self) -> bool:
        """True when a single attempt has a hard wall clock budget."""
        return self.total_seconds > 0

    def to_dict(self) -> dict[str, float]:
        """Return the six values as a plain dict (YAML friendly)."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _coerce_seconds(value: Any, name: str) -> float:
    """Convert a YAML/env scalar into a float number of seconds."""
    if isinstance(value, bool):
        raise TimeoutConfigError(f"timeouts.{name} must be a number, got bool {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise TimeoutConfigError(f"timeouts.{name} must be a number, got empty string")
        try:
            return float(text)
        except ValueError as exc:
            raise TimeoutConfigError(f"timeouts.{name} must be a number, got {value!r}") from exc
    raise TimeoutConfigError(f"timeouts.{name} must be a number, got {type(value).__name__} {value!r}")


# NOTE: this module-level instance must be defined *after* ``_coerce_seconds``,
# because ``TimeoutPolicy.__post_init__`` calls ``_coerce_seconds`` for every
# field.  Instantiating it at module load with the function defined later
# raises ``NameError`` (a prior interrupt left it in that broken order).
DEFAULT_TIMEOUT_POLICY: TimeoutPolicy = TimeoutPolicy()

_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(TimeoutPolicy))


def _env_var_name(field_name: str) -> str:
    """Map ``first_token_seconds`` -> ``ZHONGZHUAN_TIMEOUT_FIRST_TOKEN_SECONDS``."""
    return ENV_PREFIX + field_name.upper()


def resolve_timeouts(
    yaml_section: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[TimeoutPolicy, dict[str, str]]:
    """Build a :class:`TimeoutPolicy` from defaults + YAML + environment.

    Args:
        yaml_section: The raw ``timeouts:`` mapping from ``config.yaml``.
            ``None`` or ``{}`` means "defaults only".
        env: Environment mapping; defaults to ``os.environ``.

    Returns:
        ``(policy, sources)`` where ``sources`` maps every field name to the
        winning source label (``default`` / ``YAML`` / ``env``).

    Raises:
        TimeoutConfigError: On unknown keys, non-numeric values or any value
            violating the floors enforced by :class:`TimeoutPolicy`.
    """
    environ: Mapping[str, str] = os.environ if env is None else env

    values: dict[str, float] = dict(DEFAULT_TIMEOUT_POLICY.to_dict())
    sources: dict[str, str] = {name: SOURCE_DEFAULT for name in _FIELD_NAMES}

    if yaml_section:
        if not isinstance(yaml_section, Mapping):
            raise TimeoutConfigError(f"'timeouts' must be a mapping, got {type(yaml_section).__name__}")
        unknown = [str(k) for k in yaml_section.keys() if str(k) not in _FIELD_NAMES]
        if unknown:
            raise TimeoutConfigError(
                f"unknown key(s) in 'timeouts': {', '.join(sorted(unknown))}; "
                f"supported keys are {', '.join(_FIELD_NAMES)}"
            )
        for name in _FIELD_NAMES:
            if name in yaml_section:
                values[name] = _coerce_seconds(yaml_section[name], name)
                sources[name] = SOURCE_YAML

    for name in _FIELD_NAMES:
        raw = environ.get(_env_var_name(name))
        if raw is not None and str(raw).strip():
            values[name] = _coerce_seconds(raw, name)
            sources[name] = SOURCE_ENV

    return TimeoutPolicy(**values), sources


def format_effective_timeouts(
    policy: TimeoutPolicy,
    sources: Mapping[str, str] | None = None,
) -> list[str]:
    """Render one audit line per timeout layer (value + winning source)."""
    src = dict(sources or {})
    lines: list[str] = []
    for name in _FIELD_NAMES:
        value = getattr(policy, name)
        origin = src.get(name, SOURCE_DEFAULT)
        suffix = " (disabled)" if name == "total_seconds" and value == 0 else ""
        lines.append(f"timeouts.{name} = {value:g}s [{origin}]{suffix}")
    return lines


def log_effective_timeouts(
    policy: TimeoutPolicy,
    sources: Mapping[str, str] | None = None,
    logger: Any = None,
) -> list[str]:
    """Print the effective timeout table at startup, one line per layer.

    Returns the rendered lines so callers/tests can assert on them.
    """
    if logger is None:
        from loguru import logger as _logger

        logger = _logger
    lines = format_effective_timeouts(policy, sources)
    for line in lines:
        logger.info(line)
    return lines
