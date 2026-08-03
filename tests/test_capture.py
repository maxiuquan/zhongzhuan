"""T30 tests: anonymised debug capture + offline replay (R-P1-56).

Acceptance mapping
------------------
① no plaintext after capture ....... test_capture_contains_no_plaintext
                                     test_capture_keeps_only_metadata_fields
                                     test_same_id_hashes_deterministically
                                     test_capture_disabled_is_noop
                                     test_capture_module_imports_stdlib_only
② replay reproduces event sequence . test_replay_reproduces_event_sequence
                                     test_replay_roundtrips_via_json
③ size & TTL caps .................. test_max_entries_drops_oldest
                                     test_max_bytes_evicts_oldest
                                     test_oversized_single_entry_still_recorded
                                     test_ttl_filters_expired_entries
                                     test_ttl_prunes_expired_on_write
                                     test_ttl_zero_disables_ttl

All timing is injectable-clock driven -- zero real waits.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from zhongzhuan.observability.capture import (
    CaptureConfig,
    DebugCapture,
    capture as default_capture,
)

# Hard-coded sensitive samples (criterion ①: regex must never hit these).
API_KEY_SAMPLE = "sk-test-1234"
AUTH_HEADER_SAMPLE = "Authorization: Bearer t0k3n"
REASONING_SAMPLE = ("The model reasoned at length about the internal design of "
                    "the tokenizer, then concluded nothing.")
JWT_SAMPLE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJzZWNyZXQifQ.abc"
TEXT_DELTA_SAMPLE = "今天天气很好，适合部署 MCP server。"
ARGS_JSON_SAMPLE = '{"path": "/etc/x", "content": "hello"}'
RESPONSE_ID_SAMPLE = "resp-super-secret-001"
ITEM_ID_SAMPLE = "item-msg-0001"
CALL_ID_SAMPLE = "call-fc-0001"


class FakeClock:
    """Injectable clock; tests advance it by hand (no real waiting)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _capture(**cfg_overrides) -> tuple[DebugCapture, FakeClock]:
    clock = FakeClock()
    config = CaptureConfig(enabled=True, **cfg_overrides)
    return DebugCapture(config, clock=clock), clock


def _sensitive_stream() -> list[dict]:
    """A Responses-shaped event stream stuffed with plaintext everywhere."""
    return [
        {"type": "response.created", "response_id": RESPONSE_ID_SAMPLE, "seq": 0,
         "prompt": "请用 sk-test-1234 访问 https://example.invalid"},
        {"type": "response.output_item.added", "seq": 1, "output_index": 0,
         "item_id": ITEM_ID_SAMPLE,
         "item": {"id": ITEM_ID_SAMPLE, "type": "message",
                  "status": "in_progress", "role": "assistant"}},
        {"type": "response.output_text.delta", "seq": 2, "output_index": 0,
         "delta": TEXT_DELTA_SAMPLE},
        {"type": "response.output_text.done", "seq": 3, "output_index": 0},
        {"type": "response.function_call_arguments.delta", "seq": 4,
         "output_index": 1, "call_id": CALL_ID_SAMPLE, "name": "write_file",
         "arguments": ARGS_JSON_SAMPLE},
        {"type": "response.function_call_arguments.done", "seq": 5,
         "output_index": 1, "call_id": CALL_ID_SAMPLE, "name": "write_file",
         "arguments": ARGS_JSON_SAMPLE},
        {"type": "response.reasoning_summary_text.delta", "seq": 6,
         "output_index": 2, "reasoning_summary_text": REASONING_SAMPLE},
        {"type": "response.failed", "seq": 7, "response_id": RESPONSE_ID_SAMPLE,
         "error": {"message": "bad " + API_KEY_SAMPLE + " " + AUTH_HEADER_SAMPLE},
         "jwt": JWT_SAMPLE, "token": "ghp_deadbeefdeadbeefdeadbeef"},
    ]


def _capture_stream(stream: list[dict], **cfg_overrides) -> tuple[DebugCapture, FakeClock]:
    store, clock = _capture(**cfg_overrides)
    for event in stream:
        store.capture(event)
    return store, clock


# ---------------------------------------------------------------------------
# ① R-P1-56 -- after capture there is no plaintext
# ---------------------------------------------------------------------------


def test_capture_contains_no_plaintext():
    """Capture a stream stuffed with secrets; the serialised capture hits none."""
    store, _clock = _capture_stream(_sensitive_stream())
    blob = store.to_json()

    # Every mandated sensitive family must be absent -- including the hash
    # inputs (response/item/call IDs are never stored, only their hashes).
    for sample in (
        API_KEY_SAMPLE, AUTH_HEADER_SAMPLE, REASONING_SAMPLE, JWT_SAMPLE,
        TEXT_DELTA_SAMPLE, ARGS_JSON_SAMPLE, RESPONSE_ID_SAMPLE,
        ITEM_ID_SAMPLE, CALL_ID_SAMPLE,
        "ghp_deadbeefdeadbeefdeadbeef", "t0k3n", "/etc/x", "hello",
    ):
        assert re.search(re.escape(sample), blob) is None, \
            "plaintext leaked: {0}".format(sample)

    # The output is still a valid, replayable capture document.
    parsed = json.loads(blob)
    assert parsed["format"] == "zhongzhuan.debug_capture.v1"
    assert len(parsed["entries"]) == len(_sensitive_stream())


def test_capture_keeps_only_metadata_fields():
    """Only type / timestamp / seq / index / ID-hash / fragment-length survive."""
    store, _clock = _capture_stream(_sensitive_stream())
    entries = store.replay()
    assert len(entries) == 8

    allowed_keys = {"type", "timestamp", "seq", "index",
                    "response_id", "item_id", "call_id", "lengths"}
    for entry in entries:
        assert set(entry.keys()) <= allowed_keys, entry

    # Content becomes a *length* only, and the length is the true one.
    delta_entry = entries[2]
    assert delta_entry["type"] == "response.output_text.delta"
    assert delta_entry["index"] == 0
    assert delta_entry["lengths"] == {"delta": len(TEXT_DELTA_SAMPLE)}
    # The text itself is never stored -- only its character count is.
    assert delta_entry["lengths"]["delta"] == len(TEXT_DELTA_SAMPLE)

    args_entry = entries[4]
    assert args_entry["lengths"] == {"arguments": len(ARGS_JSON_SAMPLE)}

    reasoning_entry = entries[6]
    assert reasoning_entry["lengths"] == {"reasoning_summary_text": len(REASONING_SAMPLE)}


def test_same_id_hashes_deterministically():
    """The same ID hashes to the same short token; different IDs differ."""
    store, _clock = _capture_stream([
        {"type": "response.created", "response_id": RESPONSE_ID_SAMPLE, "seq": 0},
        {"type": "response.output_text.delta", "seq": 1,
         "response_id": RESPONSE_ID_SAMPLE, "delta": "a"},
        {"type": "response.created", "response_id": "resp-other-999", "seq": 2},
    ])
    entries = store.replay()
    assert entries[0]["response_id"] == entries[1]["response_id"]
    assert entries[0]["response_id"] != entries[2]["response_id"]
    # Hash is a fixed-length hex token, not the raw ID.
    assert re.fullmatch(r"[0-9a-f]{12}", entries[0]["response_id"])


def test_capture_disabled_is_noop():
    """Default config is disabled; capture() records nothing."""
    store = DebugCapture()  # default CaptureConfig().enabled is False
    assert store.enabled is False
    store.capture({"type": "response.created", "response_id": RESPONSE_ID_SAMPLE,
                   "delta": TEXT_DELTA_SAMPLE})
    assert store.replay() == []
    assert store.stats.entries == 0

    # The module-level singleton is also disabled by default.
    assert default_capture.enabled is False


def test_capture_module_imports_stdlib_only():
    """capture.py has zero third-party top-level imports (imports anywhere)."""
    source = Path(__file__).resolve().parent.parent / "src" / "zhongzhuan" / "observability" / "capture.py"
    text = source.read_text(encoding="utf-8")
    imported: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("import ") or stripped.startswith("from "):
            parts = stripped.split()
            if parts[0] == "from":
                imported.add(parts[1].split(".")[0])
            else:
                for name in parts[1:]:
                    head = name.split(".")[0]
                    if head.isidentifier():
                        imported.add(head)
    assert imported, "expected to find imports"
    for mod in imported:
        assert mod in sys.stdlib_module_names, \
            "third-party import in capture.py: {0}".format(mod)


# ---------------------------------------------------------------------------
# ② R-P1-56 -- replay reproduces the (anonymised) event sequence
# ---------------------------------------------------------------------------


def test_replay_reproduces_event_sequence():
    """Replay preserves type + seq + index + lengths in stream order."""
    stream = _sensitive_stream()
    store, _clock = _capture_stream(stream)
    replay = store.replay()

    # Same length and same order as the captured stream.
    assert [entry["seq"] for entry in replay] == list(range(len(stream)))

    expected_types = [event["type"] for event in stream]
    assert [entry["type"] for entry in replay] == expected_types

    # output_index survives as index.
    assert [entry.get("index") for entry in replay] == [
        None, 0, 0, 0, 1, 1, 2, None,
    ]

    # Fragment lengths survive.
    assert replay[2]["lengths"]["delta"] == len(TEXT_DELTA_SAMPLE)
    assert replay[4]["lengths"]["arguments"] == len(ARGS_JSON_SAMPLE)
    assert replay[6]["lengths"]["reasoning_summary_text"] == len(REASONING_SAMPLE)


def test_replay_roundtrips_via_json():
    """to_json -> load reproduces the exact same anonymised sequence."""
    store, clock = _capture_stream(_sensitive_stream())
    first = store.replay()

    rebuilt = DebugCapture.load(store.to_json(), clock=clock)
    assert rebuilt.replay() == first

    # A brand-new buffer (no clock) fed from disk replays the same sequence.
    from_disk = DebugCapture.load(store.to_json())
    assert [e["type"] for e in from_disk.replay()] == [e["type"] for e in first]
    assert [e["seq"] for e in from_disk.replay()] == [e["seq"] for e in first]


# ---------------------------------------------------------------------------
# ③ R-P1-56 -- size and TTL caps
# ---------------------------------------------------------------------------


def test_max_entries_drops_oldest():
    """Over the entry cap, the *oldest* entries are dropped first."""
    store, _clock = _capture(max_entries=3)
    for i in range(5):
        store.capture({"type": "response.output_text.delta", "seq": i,
                       "delta": "x" * 4})
    replay = store.replay()
    assert [e["seq"] for e in replay] == [2, 3, 4]
    assert store.stats.dropped == 2
    assert store.stats.entries == 3


def test_max_bytes_evicts_oldest():
    """Over the byte budget, entries are evicted until the buffer fits."""
    store, _clock = _capture(max_bytes=250)
    for i in range(6):
        store.capture({"type": "response.created", "seq": i,
                       "response_id": "resp-{0}".format(i)})
    replay = store.replay()
    # The buffer stays within budget, keeps a contiguous tail, and dropped
    # at least one entry.
    assert store.stats.bytes <= 250
    assert store.stats.dropped >= 1
    assert len(replay) == 6 - store.stats.dropped
    seqs = [e["seq"] for e in replay]
    assert seqs == list(range(seqs[0], seqs[-1] + 1))
    # The surviving tail is the most recent events, not the oldest.
    assert seqs[-1] == 5


def test_oversized_single_entry_still_recorded():
    """A lone entry is never dropped, even when it exceeds max_bytes."""
    store, _clock = _capture(max_bytes=1)
    store.capture({"type": "response.created", "seq": 0,
                   "response_id": RESPONSE_ID_SAMPLE})
    assert len(store.replay()) == 1
    assert store.stats.dropped == 0


def test_ttl_filters_expired_entries():
    """Entries older than ttl_seconds are filtered out on replay."""
    store, clock = _capture(ttl_seconds=100)
    store.capture({"type": "response.created", "seq": 0})       # t=0
    clock.advance(50)
    store.capture({"type": "response.created", "seq": 1})       # t=50
    clock.advance(100)
    store.capture({"type": "response.created", "seq": 2})       # t=150

    clock.advance(10)                                           # now t=160
    replay = store.replay()
    # t=0  -> 160s old  (expired); t=50 -> 110s old (expired); t=150 -> 10s old (ok)
    assert [e["seq"] for e in replay] == [2]
    assert replay[0]["timestamp"] == 150.0


def test_ttl_prunes_expired_on_write():
    """A later capture prunes the expired entries out of the buffer."""
    store, clock = _capture(ttl_seconds=100)
    store.capture({"type": "response.created", "seq": 0})       # t=0
    clock.advance(200)
    store.capture({"type": "response.created", "seq": 1})       # t=200
    assert store.stats.expired == 1
    assert store.stats.entries == 1
    assert [e["seq"] for e in store.replay()] == [1]


def test_ttl_zero_disables_ttl():
    """ttl_seconds=0 means nothing ever expires."""
    store, clock = _capture(ttl_seconds=0)
    store.capture({"type": "response.created", "seq": 0})
    clock.advance(1_000_000)
    store.capture({"type": "response.created", "seq": 1})
    assert len(store.replay()) == 2
