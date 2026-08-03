"""Anonymised debug capture + offline replay (T30 / R-P1-56).

A debug-time **optional** side-channel that records what happened on a response
stream without recording the content that happened.  It is deliberately the
opposite of :mod:`.logfields`: the request log is a runtime audit trail that
may carry (redacted) text; capture is a forensic snapshot that keeps **only**

* the event **type** (``response.created`` / ``response.output_text.delta`` --
  an enumerated label, not sensitive);
* the capture **timestamp**;
* the **sequence index** of the event in the stream, plus the item
  ``output_index`` when the event carries one;
* a **hash** of each ID (``response_id`` / ``item_id`` / ``call_id``) -- the
  hash is for cross-event correlation, not for secrecy: two events carrying the
  same ID produce the same hash, while the raw ID never appears;
* the **length** of every text fragment / tool arguments / reasoning fragment.

Raw text, tool arguments, reasoning text and any credential-bearing value are
**never** stored.  This makes the guarantee structural: there is no code path
that copies content into the capture buffer, so the "no plaintext" property of
:func:`normalize_event` cannot silently regress into a redact-then-hope.

The buffer honours a maximum entry count and a maximum byte budget (oldest
entries are dropped first), and a TTL (entries older than ``ttl_seconds`` are
filtered out on read and pruned on write).  All timing is injectable via the
``clock`` callable -- tests never wait.

``capture`` is a module-level singleton that is **disabled by default** (debug
capture is opt-in); :class:`DebugCapture` is the full stateful store.

Top-level imports are standard library only, so ``capture.py`` imports cleanly
in any environment.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# ---------------------------------------------------------------------------
# 1. Constants / config
# ---------------------------------------------------------------------------

#: Default retention for a debug capture (architecture §5.9: 24h).
DEFAULT_TTL_SECONDS: float = 24 * 3600.0

#: Default entry-count cap for one capture buffer.
DEFAULT_MAX_ENTRIES: int = 10_000

#: Default byte budget for one capture buffer (0 = unbounded).
DEFAULT_MAX_BYTES: int = 1_000_000

#: How many hex characters of the SHA-256 ID digest we keep.  12 hex chars =
#: 48 bits of entropy -- enough to correlate events carrying the same ID, while
#: revealing nothing about the ID itself.
ID_HASH_LENGTH: int = 12


@dataclass(frozen=True)
class CaptureConfig:
    """Tunables for one debug-capture buffer (defaults: disabled)."""

    #: Master switch -- capture is opt-in (debug only), default off.
    enabled: bool = False
    #: Seconds a captured entry stays readable (injectable clock, no real waits).
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    #: Maximum number of entries; oldest entries are dropped first.
    max_entries: int = DEFAULT_MAX_ENTRIES
    #: Maximum aggregate byte budget (serialised entries); 0 = unbounded.
    max_bytes: int = DEFAULT_MAX_BYTES


#: Keys whose string values are treated as *content* -- stored only as a
#: character length, never in full.  Key names are matched case-insensitively
#: against the raw event dict.
_CONTENT_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(^|_)(text|delta|content|output|arguments|input|prompt|"
               r"query|reasoning|summary)(_|$)"),
    re.compile(r"(?i)secret|token|api[_-]?key|password|authorization"),
)

#: Field names that carry a raw event's ID and are hashed on capture.
_ID_KEYS: tuple[str, ...] = ("response_id", "item_id", "call_id")


# ---------------------------------------------------------------------------
# 2. Hashing / length helpers
# ---------------------------------------------------------------------------


def _id_hash(value: Any) -> str:
    """SHA-256 of ``str(value)`` truncated to :data:`ID_HASH_LENGTH` hex chars.

    Deterministic so the same ID always hashes to the same short token across
    events (that is the whole point of hashing rather than encrypting: the
    client can correlate fragments of one response without ever seeing its ID).
    """
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:ID_HASH_LENGTH]


def _fragment_length(value: Any) -> int:
    """Character length of one content fragment (dict -> canonical JSON length)."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return len(str(value))
    if value is None:
        return 0
    return len(str(value))


def _is_content_key(key: str) -> bool:
    """Whether ``key`` names a content field (kept only as a length)."""
    return any(pattern.search(key) for pattern in _CONTENT_KEY_PATTERNS)


# ---------------------------------------------------------------------------
# 3. Entry model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureEntry:
    """One anonymised captured event.

    Every field is derived from the raw event; none of it is raw content.
    ``lengths`` maps each content key name to the character length of its value
    (text ``delta`` -> ``len``, tool ``arguments`` -> ``len``, ...).
    """

    type: str
    timestamp: float
    seq: int
    index: int | None = None
    response_id: str = ""
    item_id: str = ""
    call_id: str = ""
    lengths: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready plain dict (round-trips through :func:`from_dict`)."""
        out: dict[str, Any] = {
            "type": self.type,
            "timestamp": self.timestamp,
            "seq": self.seq,
        }
        if self.index is not None:
            out["index"] = self.index
        for key in ("response_id", "item_id", "call_id"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.lengths:
            out["lengths"] = dict(sorted(self.lengths.items()))
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CaptureEntry":
        """Rebuild an entry from :meth:`to_dict` output (offline replay)."""
        return cls(
            type=str(data.get("type") or ""),
            timestamp=float(data.get("timestamp") or 0.0),
            seq=int(data.get("seq") or 0),
            index=int(data["index"]) if data.get("index") is not None else None,
            response_id=str(data.get("response_id") or ""),
            item_id=str(data.get("item_id") or ""),
            call_id=str(data.get("call_id") or ""),
            lengths={
                str(k): int(v)
                for k, v in dict(data.get("lengths") or {}).items()
            },
        )


# ---------------------------------------------------------------------------
# 4. Normalisation (raw event -> entry)
# ---------------------------------------------------------------------------


def _event_type(raw: Mapping[str, Any]) -> str:
    """The event's type label (``type`` or ``event_type``)."""
    value = raw.get("type", raw.get("event_type", ""))
    return str(value or "")


def normalize_event(
    raw: Mapping[str, Any],
    *,
    timestamp: float,
    seq: int,
) -> CaptureEntry:
    """Project one raw event dict onto an anonymised :class:`CaptureEntry`.

    Only the R-P1-56 fields survive:

    * event type / timestamp / sequence / output index (enumerated or numeric);
    * IDs -> :func:`_id_hash`;
    * content fields (text / delta / arguments / reasoning / tokens / keys)
      -> **character length**; the value itself is discarded.

    Every other key is dropped -- there is no code path that copies a content
    value into the entry, so the "no plaintext" guarantee is structural.
    """
    ids: dict[str, str] = {}
    for key in _ID_KEYS:
        value = raw.get(key)
        if value not in (None, ""):
            ids[key] = _id_hash(value)

    index: int | None = None
    value = raw.get("index", raw.get("output_index"))
    if value is not None:
        try:
            index = int(value)
        except (TypeError, ValueError):
            index = None

    lengths: dict[str, int] = {}
    for key, value in raw.items():
        if key in ("index", "output_index", "seq", "sequence_number"):
            continue
        if not isinstance(value, (str, Mapping, list, tuple)):
            continue  # bool / int / float / None -- nothing sensitive
        if _is_content_key(key):
            lengths[key] = _fragment_length(value)

    return CaptureEntry(
        type=_event_type(raw),
        timestamp=timestamp,
        seq=seq,
        index=index,
        response_id=ids.get("response_id", ""),
        item_id=ids.get("item_id", ""),
        call_id=ids.get("call_id", ""),
        lengths=lengths,
    )


# ---------------------------------------------------------------------------
# 5. The store
# ---------------------------------------------------------------------------


@dataclass
class CaptureStats:
    """Observability for one capture buffer (tests assert on these)."""

    entries: int = 0
    bytes: int = 0
    dropped: int = 0  # entries evicted by the size caps
    expired: int = 0  # entries evicted by the TTL


class DebugCapture:
    """Bounded, TTL-aware, fully-anonymised capture buffer (R-P1-56).

    The pipeline drives one buffer from one coroutine, so the buffer is
    deliberately thread-free.  ``clock`` defaults to :func:`time.monotonic`
    (never goes backwards); tests inject a fake clock and advance it by hand --
    zero real waiting.
    """

    def __init__(
        self,
        config: CaptureConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or CaptureConfig()
        self._clock = clock or time.monotonic
        self._entries: deque[CaptureEntry] = deque()
        self._bytes = 0
        #: Persistent per-store sequence counter -- monotonic across evictions
        #: so a sequence number never gets reused after a TTL/size prune.
        self._counter = 0
        self.stats = CaptureStats()

    # -- public API ---------------------------------------------------------

    def capture(self, event: Mapping[str, Any]) -> None:
        """Record ``event`` if capture is enabled; else a no-op.

        The event dict may be any of the pipeline's ``_emit`` payloads; only
        the anonymised projection (:func:`normalize_event`) is kept.
        """
        if not self.config.enabled:
            return
        now = self._clock()
        self._prune_expired(now)
        entry = normalize_event(
            event,
            timestamp=now,
            seq=self._counter,
        )
        self._counter += 1
        self._append(entry)

    def replay(self) -> list[dict[str, Any]]:
        """Return the readable entries in stream order (oldest first).

        Entries older than ``ttl_seconds`` are filtered out here, so a replay
        reflects what the TTL says is still readable.  The result is
        JSON-ready and feeds :meth:`DebugCapture.load` for offline replay.
        """
        now = self._clock()
        out: list[dict[str, Any]] = []
        for entry in self._entries:
            if (self.config.ttl_seconds > 0
                    and now - entry.timestamp > self.config.ttl_seconds):
                continue
            out.append(entry.to_dict())
        return out

    def to_json(self) -> str:
        """Serialise the whole readable buffer (JSON) for offline replay."""
        payload: dict[str, Any] = {
            "format": "zhongzhuan.debug_capture.v1",
            "entries": self.replay(),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=False)

    @classmethod
    def load(
        cls,
        serialised: str,
        *,
        config: CaptureConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> "DebugCapture":
        """Rebuild a capture from :meth:`to_json` output (offline replay).

        The reconstructed buffer replays the exact anonymised event sequence --
        types, sequence indices, output indices and fragment lengths survive
        the round trip.
        """
        data = json.loads(serialised)
        entries_data = data.get("entries", []) if isinstance(data, Mapping) else []
        store = cls(config=config, clock=clock)
        for item in entries_data:
            store._append(CaptureEntry.from_dict(item))  # noqa: SLF001
        if store._entries:  # noqa: SLF001
            store._counter = store._entries[-1].seq + 1  # noqa: SLF001
        return store

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def clear(self) -> None:
        """Drop every entry (tests / teardown)."""
        self._entries.clear()
        self._bytes = 0
        self.stats = CaptureStats()

    # -- internals ----------------------------------------------------------

    def _append(self, entry: CaptureEntry) -> None:
        """Add ``entry`` under the size caps (oldest evicted first)."""
        size = len(json.dumps(entry.to_dict(), ensure_ascii=False))
        if self.config.max_entries > 0 and len(self._entries) >= self.config.max_entries:
            self._evict_oldest()
        if self.config.max_bytes > 0:
            while self._entries and self._bytes + size > self.config.max_bytes:
                self._evict_oldest()
        self._entries.append(entry)
        self._bytes += size
        self.stats.entries = len(self._entries)
        self.stats.bytes = self._bytes

    def _evict_oldest(self) -> None:
        """Drop the oldest entry and account for its size."""
        if not self._entries:
            return
        old = self._entries.popleft()
        self._bytes -= len(json.dumps(old.to_dict(), ensure_ascii=False))
        if self._bytes < 0:
            self._bytes = 0
        self.stats.dropped += 1

    def _prune_expired(self, now: float) -> None:
        """Drop entries whose TTL has elapsed (called on write)."""
        if self.config.ttl_seconds <= 0:
            return
        while self._entries and now - self._entries[0].timestamp > self.config.ttl_seconds:
            self._evict_oldest()
            self.stats.expired += 1


# ---------------------------------------------------------------------------
# 6. Module-level convenience singleton (disabled by default)
# ---------------------------------------------------------------------------


#: The default capture used by the pipeline -- **disabled** until a caller
#: swaps in an enabled :class:`DebugCapture` (debug capture is opt-in).
capture = DebugCapture()


__all__ = [
    "CaptureConfig",
    "CaptureEntry",
    "CaptureStats",
    "DebugCapture",
    "capture",
    "normalize_event",
]
