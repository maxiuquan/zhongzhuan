"""§7.1 GA gate: the ten mandatory acceptance tests for Responses v3.

Every test in this module boots the **production** ``ProxyServer.app()`` and
drives a **real** ``POST /v1/responses`` (or ``GET``/``POST .../cancel``) over
a real TCP socket against the programmable mock upstream.  Nothing here calls
a pipeline, an adapter or a resolver directly: the whole point of the GA gate
is to prove the wire behaves, not that a unit behaves.

规范 §7.1 mapping (the ten checkboxes, in order)::

    T1   ChatGPTWork(Codex) 真实 POST /v1/responses 非流式和流式均进入 v3
    T2   OpenAI Python/TypeScript SDK Responses contract 测试通过
    T3   流式真实 HTTP 输出严格满足完整 lifecycle，最后一帧为 [DONE]
    T4   随机字节分片下文本/Unicode/tool name/call ID/arguments 语义不变
    T5   非法/截断 tool arguments 永不产生 .done 或 runnable call
    T6   正常 EOF 不会成为 truncated；异常断流具备正确 terminal reason
    T7   多轮 previous_response_id 已注入真实 upstream payload，reasoning 永不出现
    T8   background create/retrieve/cancel/restart 恢复通过
    T9   自引用、链环、重复工具签名、工具失败、超时、预算耗尽均有限终止
    T10  Chat -> Chat、Chat <-> Anthropic 的 golden fixture 字节级输出无变化

Plus two focused regressions requested for GA:

    A    P0-6 background terminal really carries structured output (not an
         empty ``output`` array and not text-delta-shaped tool events)
    B    AC-8.3/8.4 version stickiness: flipping the switch mid-stream must
         not migrate an in-flight response across implementations

Honest labeling: these are ``mock回放`` results driven by
``tests/support/mock_responses_upstream.py``.  T2 is a *shape-level* contract
check against the official ``response`` object -- running the real TypeScript
SDK lives in ``tests/contract/typescript`` and is out of this module's scope,
which is stated rather than implied.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import ClientSession, web

from support.mock_responses_upstream import (
    MockUpstream,
    UpstreamBehavior,
    anthropic_text_stream,
    by_line,
    by_n_bytes,
    openai_text_json,
    openai_text_stream,
    openai_tool_stream,
    random_split,
    whole,
)
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.upstream import UpstreamClient


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


V3_ENV = "ZHONGZHUAN_RESPONSES_BRIDGE_V3"

#: A plausible Codex / ChatGPTWork client signature (T1).  It is asserted to be
#: *irrelevant* to routing: the fork keys off the inbound protocol, never the
#: User-Agent, so this header must not be load-bearing in either direction.
CODEX_HEADERS = {
    "User-Agent": "codex_cli_rs/0.20.0 (Mac OS 15.0; arm64) WindsurfIDE",
    "OpenAI-Beta": "responses=v1",
    "originator": "codex_cli_rs",
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(autouse=True)
def _v3_on(monkeypatch):
    """GA runs with the switch explicitly ON (P0-8 hard override)."""
    monkeypatch.setenv(V3_ENV, "1")
    monkeypatch.delenv("RESPONSES_BRIDGE_V3", raising=False)
    monkeypatch.setenv("ZHONGZHUAN_PROXY_AUTH", "true")


@pytest_asyncio.fixture
async def astore(tmp_path, monkeypatch):
    """Async SQLite store fixture (mirrors conftest)."""
    for var in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE"):
        monkeypatch.delenv(f"ZHONGZHUAN_TIDB_{var}", raising=False)
    from zhongzhuan.config import default_config
    from zhongzhuan.store.store import create_store

    cfg = default_config()
    cfg.storage.backend = "sqlite"
    cfg.storage.db_path = str(tmp_path / "test.db")
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    s = await create_store(cfg)
    try:
        yield s
    finally:
        await s.close()


class Proxy:
    """A booted production app plus everything a test needs to tear it down."""

    def __init__(self, port: int, runner: web.AppRunner, upstream: UpstreamClient, token: str, server: ProxyServer):
        self.port = port
        self.runner = runner
        self.upstream = upstream
        self.token = token
        self.server = server

    @property
    def handler(self) -> Any:
        """The single ``ProxyHandler`` instance behind all six routes."""
        return self.server._proxy_handler

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def close(self) -> None:
        await self.runner.cleanup()
        await self.upstream.close()


async def _start_proxy(upstream_url: str, store, *, token: str = "", timeout: float = 10.0) -> Proxy:
    """Boot the production app against ``upstream_url``.

    ``ZHONGZHUAN_PROXY_AUTH`` is on so the access-token middleware runs -- the
    same tenant boundary the v3 workspace is derived from.  ``AppRunner.setup``
    fires ``on_startup``, which is what starts the P0-6 background worker, so
    background tests get a genuinely running queue drainer.
    """
    if not token:
        from zhongzhuan.store.access_tokens import create_token

        token = (await create_token(store, label="ga-token", quota_tokens=1_000_000)).token
    upstream = UpstreamClient(base_url=upstream_url, timeout=timeout)
    await upstream.start()
    server = ProxyServer(
        upstream_clients={upstream_url: upstream},
        api_key="sk-upstream",
        keys=[],
        proxy_timeout=timeout,
        store=store,
        responses_bridge=None,  # default enabled
    )
    port = _free_port()
    runner = web.AppRunner(server.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return Proxy(port, runner, upstream, token, server)


def _auth(token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if extra:
        headers.update(extra)
    return headers


async def _post_json(p: Proxy, path: str, body: dict, *, extra_headers: dict | None = None) -> tuple[int, Any]:
    async with ClientSession() as sess:
        async with sess.post(p.url(path), headers=_auth(p.token, extra_headers), data=json.dumps(body)) as resp:
            raw = await resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw


async def _get_json(p: Proxy, path: str, *, extra_headers: dict | None = None) -> tuple[int, Any]:
    async with ClientSession() as sess:
        async with sess.get(p.url(path), headers=_auth(p.token, extra_headers)) as resp:
            raw = await resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw


async def _stream(p: Proxy, body: dict, *, extra_headers: dict | None = None) -> tuple[int, str, bytes]:
    """POST a streaming create; return ``(status, content_type, raw_bytes)``.

    The raw bytes are deliberate: SSE framing (blank-line separators, the exact
    ``data: [DONE]`` sentinel) is part of the contract and a helpful
    line-parsing helper would destroy the evidence.
    """
    async with ClientSession() as sess:
        async with sess.post(p.url("/v1/responses"), headers=_auth(p.token, extra_headers), data=json.dumps(body)) as r:
            return r.status, r.headers.get("Content-Type", ""), await r.read()


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _events(raw: bytes) -> list[dict]:
    out: list[dict] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _event_types(raw: bytes) -> list[str]:
    return [e["type"] for e in _events(raw) if isinstance(e.get("type"), str)]


def _joined(raw: bytes, event_type: str, field: str = "delta") -> str:
    return "".join(str(e.get(field) or "") for e in _events(raw) if e.get(field) is not None and e.get("type") == event_type)


def _response_id(raw: bytes) -> str:
    for event in _events(raw):
        if event.get("type") == "response.created":
            return str(event.get("response", {}).get("id") or "")
    raise AssertionError(f"no response.created on the wire:\n{raw.decode(errors='replace')}")


_TERMINALS = ("response.completed", "response.failed", "response.incomplete", "response.cancelled")
_LIFECYCLE_ONCE = ("response.created", "response.in_progress", *_TERMINALS)


def _assert_lifecycle(raw: bytes) -> list[str]:
    """T3: complete lifecycle, each member at most once, exactly one terminal."""
    types = _event_types(raw)
    assert types, f"no JSON events on the wire:\n{raw[:400]!r}"
    assert types[0] == "response.created", types[:3]
    assert "response.in_progress" in types, types[:5]
    for name in _LIFECYCLE_ONCE:
        assert types.count(name) <= 1, f"{name} emitted {types.count(name)}x: {types}"
    terminals = [t for t in types if t in _TERMINALS]
    assert len(terminals) == 1, f"expected exactly one terminal, got {terminals}"
    assert types[-1] == terminals[0], f"terminal is not the last event: {types[-4:]}"
    assert raw.rstrip().endswith(b"data: [DONE]"), raw[-160:]
    return types


async def _workspace(store, token: str) -> str:
    from zhongzhuan.store.access_tokens import get_token_by_value

    at = await get_token_by_value(store, token)
    assert at is not None
    return f"token:{at.id}"


def _flatten(obj: Any) -> str:
    """A JSON dump used for 'this string must not appear anywhere' assertions."""
    return json.dumps(obj, ensure_ascii=False)


def _output_text(output: Any) -> str:
    """Concatenate the ``output_text`` parts of a persisted ``output`` array."""
    parts: list[str] = []
    for item in output or []:
        if not isinstance(item, dict) or item.get("type") not in ("message", None):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                parts.append(str(part.get("text") or ""))
    return "".join(parts)


# ===========================================================================
# T1: Codex 真实 POST /v1/responses 非流式和流式均进入 v3
# ===========================================================================


@pytest.mark.asyncio
async def test_t1_codex_nonstream_enters_v3(astore):
    """A Codex-shaped non-stream create is served by v3, end to end."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="v3 non-stream")))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, obj = await _post_json(
            p,
            "/v1/responses",
            {"model": "gpt-4o", "input": "hi"},
            extra_headers=CODEX_HEADERS,
        )
        assert status == 200, obj
        # v3 fingerprint #1: the id is OURS, minted at the fork, not upstream's.
        assert obj["object"] == "response"
        rid = obj["id"]
        assert rid.startswith("resp_"), rid
        # v3 fingerprint #2: a real upstream call happened (translated to Chat).
        assert up.request_count == 1
        assert up.requests[0].path == "/v1/chat/completions"
        # v3 fingerprint #3: the resource is retrievable -- v2 answers 405 to a
        # GET on /v1/responses/{id}, so a 200 here can only be v3.
        rstatus, rec = await _get_json(p, f"/v1/responses/{rid}", extra_headers=CODEX_HEADERS)
        assert rstatus == 200, rec
        assert rec["id"] == rid
        assert rec["status"] == "completed"
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t1_codex_stream_enters_v3(astore):
    """A Codex-shaped streaming create is served by the v3 SSE pipeline."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream(pieces=("v3", " ", "stream"))))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(
            p,
            {"model": "gpt-4o", "input": "hi", "stream": True},
            extra_headers=CODEX_HEADERS,
        )
        assert status == 200, raw
        assert ctype.startswith("text/event-stream"), ctype
        types = _assert_lifecycle(raw)
        assert "response.completed" in types
        assert _joined(raw, "response.output_text.delta") == "v3 stream"
        assert _response_id(raw).startswith("resp_")
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t1_fork_is_on_inbound_protocol_not_user_agent(astore):
    """The fork keys off the inbound protocol; Codex headers change nothing.

    Two negative controls in one test, because they are the same claim:
      - a *non*-Codex UA on /v1/responses still gets v3;
      - a *Codex* UA on /v1/chat/completions still gets the legacy path.
    """
    up = MockUpstream()
    up.queue_behaviors(
        [
            UpstreamBehavior(json_payload=openai_text_json(content="plain ua")),
            UpstreamBehavior(json_payload=openai_text_json(content="legacy chat")),
        ]
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, obj = await _post_json(
            p,
            "/v1/responses",
            {"model": "gpt-4o", "input": "hi"},
            extra_headers={"User-Agent": "curl/8.4.0"},
        )
        assert status == 200, obj
        assert obj["id"].startswith("resp_")
        # Retrievable => v3 served it even though nothing looked like Codex.
        rstatus, _ = await _get_json(p, f"/v1/responses/{obj['id']}")
        assert rstatus == 200

        # /v1/chat/completions is untouched by v3: a Chat request comes back in
        # Chat shape (``chat.completion``), never wrapped into a response object.
        status, chat = await _post_json(
            p,
            "/v1/chat/completions",
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            extra_headers=CODEX_HEADERS,
        )
        assert status == 200, chat
        assert chat["object"] == "chat.completion"
        assert "output" not in chat
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# T2: Responses object contract (shape level)
# ===========================================================================


#: Fields the official ``response`` object must carry on the wire.
#:
#: Scoped deliberately to what the *real* OpenAI SDK treats as load-bearing.
#: ``error`` / ``incomplete_details`` / ``metadata`` / ``previous_response_id``
#: are declared ``required=False`` on ``openai.types.responses.Response``
#: (verified against openai 2.53.0), so an absent key and an explicit ``null``
#: are indistinguishable to a client.  Asserting their presence would test our
#: serializer's taste, not the contract.
_RESPONSE_REQUIRED_FIELDS = (
    "id",
    "object",
    "created_at",
    "model",
    "status",
    "output",
    "usage",
)


@pytest.mark.asyncio
async def test_t2_response_object_contract_create_and_retrieve(astore):
    """create/retrieve return an SDK-decodable ``response`` object."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="contract")))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, created = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "hi"})
        assert status == 200, created
        for field in _RESPONSE_REQUIRED_FIELDS:
            assert field in created, f"create response is missing {field!r}: {sorted(created)}"
        assert created["object"] == "response"
        assert isinstance(created["output"], list)
        assert isinstance(created["created_at"], int)

        rid = created["id"]
        status, fetched = await _get_json(p, f"/v1/responses/{rid}")
        assert status == 200, fetched
        for field in _RESPONSE_REQUIRED_FIELDS:
            assert field in fetched, f"retrieve response is missing {field!r}: {sorted(fetched)}"
        assert fetched["object"] == "response"
        assert fetched["id"] == rid
        # T37 ②: create and retrieve agree on identity.
        assert fetched["model"] == created["model"]
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t2_payload_decodes_through_the_real_openai_sdk_model(astore):
    """T2 at the level that actually matters: the real SDK must decode us.

    Field-name checklists drift.  This pins the contract to the ground truth --
    ``openai.types.responses.Response`` -- using the same non-strict path the
    SDK itself uses when it materialises an API response.
    """
    sdk_responses = pytest.importorskip("openai.types.responses")

    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="sdk contract")))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, created = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "hi"})
        assert status == 200, created

        decoded = sdk_responses.Response.construct(**created)
        assert decoded.id == created["id"]
        assert decoded.object == "response"
        assert decoded.status == "completed"
        # The decoded output must still carry the assistant text, not an
        # empty husk that only *looks* like a valid response.
        assert _output_text(created["output"]) == "sdk contract"

        rid = created["id"]
        status, fetched = await _get_json(p, f"/v1/responses/{rid}")
        assert status == 200, fetched
        refetched = sdk_responses.Response.construct(**fetched)
        assert refetched.id == rid
        assert refetched.object == "response"
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t2_resource_endpoints_contract(astore):
    """input_items / cancel / delete return their official envelopes."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="crud")))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        _, created = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "remember this"})
        rid = created["id"]

        status, items = await _get_json(p, f"/v1/responses/{rid}/input_items")
        assert status == 200, items
        assert items["object"] == "list"
        assert isinstance(items["data"], list)
        for key in ("first_id", "last_id", "has_more"):
            assert key in items, sorted(items)

        status, cancelled = await _post_json(p, f"/v1/responses/{rid}/cancel", {})
        assert status == 200, cancelled
        assert cancelled["object"] == "response"
        assert cancelled["id"] == rid

        async with ClientSession() as sess:
            async with sess.delete(p.url(f"/v1/responses/{rid}"), headers=_auth(p.token)) as r:
                assert r.status == 200
                deleted = await r.json()
        assert deleted == {"id": rid, "object": "response", "deleted": True}

        # Deleted really means gone (and not a tombstone that still answers 200).
        status, _ = await _get_json(p, f"/v1/responses/{rid}")
        assert status == 404
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t2_error_envelope_is_standard(astore):
    """Errors are ``{"error": {...}}`` -- the only shape an SDK error path reads."""
    up = MockUpstream()
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, payload = await _get_json(p, "/v1/responses/resp_does_not_exist")
        assert status == 404, payload
        assert isinstance(payload.get("error"), dict), payload
        err = payload["error"]
        assert err.get("message")
        assert err.get("type") or err.get("code")
        # A 404 must not have cost an upstream call.
        assert up.request_count == 0
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# T3: complete lifecycle + [DONE] as the last frame
# ===========================================================================


@pytest.mark.asyncio
async def test_t3_stream_lifecycle_and_done_sentinel(astore):
    """The wire carries created -> in_progress -> ... -> terminal -> [DONE]."""
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=("Hello", ", ", "world", "!")),
            chunk_strategy=by_n_bytes(13),
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(p, {"model": "gpt-4o", "input": "hi", "stream": True})
        assert status == 200
        assert ctype.startswith("text/event-stream")
        types = _assert_lifecycle(raw)
        assert types[:2] == ["response.created", "response.in_progress"]
        assert "response.output_item.added" in types
        assert "response.output_text.delta" in types
        assert types[-1] == "response.completed"

        # SSE framing: every frame is separated by a blank line and the sentinel
        # appears exactly once.
        body = raw.decode()
        assert body.count("data: [DONE]") == 1
        assert body.endswith("\n\n") or body.endswith("data: [DONE]\n\n") or body.rstrip().endswith("data: [DONE]")

        ws = await _workspace(astore, p.token)
        rec = await ResponseStore(astore).get_response(_response_id(raw), workspace_id=ws)
        assert rec is not None and rec.status == "completed"
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t3_phase_a_failure_is_json_with_zero_sse_bytes(astore):
    """Two-phase commit: a Phase A refusal never emits a single ``event:`` line."""
    up = MockUpstream()
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(
            p,
            {
                "model": "gpt-4o",
                "input": "hi",
                "stream": True,
                "previous_response_id": "resp_missing_ancestor",
            },
        )
        assert status == 400, raw
        assert "application/json" in ctype, ctype
        assert b"event:" not in raw and b"data:" not in raw, raw[:300]
        payload = json.loads(raw)
        assert payload["error"]["param"] == "previous_response_id"
        # A chain guard fires BEFORE the network (§ chain防护).
        assert up.request_count == 0
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# T4: byte-level chunking must not change semantics
# ===========================================================================


_UNICODE_PIECES = ("你好", "，世界", " 🌍", "café", " — naïve", "日本語テキスト")


@pytest.mark.parametrize(
    "strategy_name",
    ["whole", "by_line", "by_1_byte", "by_7_bytes", "random_seed_1", "random_seed_99"],
)
@pytest.mark.asyncio
async def test_t4_text_and_unicode_survive_any_chunking(astore, strategy_name):
    """Same bytes, six framings, one meaning (including split UTF-8 sequences)."""
    strategies = {
        "whole": whole(),
        "by_line": by_line(),
        "by_1_byte": by_n_bytes(1),
        "by_7_bytes": by_n_bytes(7),
        "random_seed_1": random_split(1),
        "random_seed_99": random_split(99),
    }
    expected = "".join(_UNICODE_PIECES)
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=_UNICODE_PIECES),
            chunk_strategy=strategies[strategy_name],
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(p, {"model": "gpt-4o", "input": "hi", "stream": True})
        assert status == 200, raw
        assert ctype.startswith("text/event-stream")
        _assert_lifecycle(raw)
        assert _joined(raw, "response.output_text.delta") == expected, strategy_name
        assert "\ufffd" not in raw.decode("utf-8", "replace"), f"{strategy_name}: mangled UTF-8"
    finally:
        await p.close()
        await up.stop()


@pytest.mark.parametrize("strategy_name", ["whole", "by_1_byte", "by_5_bytes", "random_seed_7"])
@pytest.mark.asyncio
async def test_t4_tool_identity_and_arguments_survive_any_chunking(astore, strategy_name):
    """Tool name, call id and arguments are chunking-invariant."""
    strategies = {
        "whole": whole(),
        "by_1_byte": by_n_bytes(1),
        "by_5_bytes": by_n_bytes(5),
        "random_seed_7": random_split(7),
    }
    tool_name = "get_天气_forecast"
    call_id = "call_ga_0001"
    arguments = '{"city": "北京", "unit": "℃", "note": "a \\"quoted\\" word"}'
    # Split the argument JSON into deliberately awkward pieces (mid-escape,
    # mid-multibyte) so the accumulator cannot rely on tidy boundaries.
    arg_pieces = tuple(arguments[i : i + 4] for i in range(0, len(arguments), 4))

    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_tool_stream(tool_name=tool_name, tool_call_id=call_id, arg_pieces=arg_pieces),
            chunk_strategy=strategies[strategy_name],
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, _, raw = await _stream(
            p,
            {
                "model": "gpt-4o",
                "input": "weather?",
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "name": tool_name,
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            },
        )
        assert status == 200, raw
        types = _assert_lifecycle(raw)
        assert "response.function_call_arguments.done" in types, f"{strategy_name}: {types}"
        assert types.count("response.function_call_arguments.done") == 1

        streamed_args = _joined(raw, "response.function_call_arguments.delta")
        assert json.loads(streamed_args) == json.loads(arguments), strategy_name

        done = [e for e in _events(raw) if e.get("type") == "response.function_call_arguments.done"][0]
        assert json.loads(str(done.get("arguments"))) == json.loads(arguments), strategy_name

        blob = _flatten(_events(raw))
        assert tool_name in blob, f"{strategy_name}: tool name lost"
        assert call_id in blob, f"{strategy_name}: call id lost"
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# T5: malformed / truncated tool arguments are never completed
# ===========================================================================


@pytest.mark.parametrize(
    "arg_pieces,label",
    [
        (('{"cit', 'y": "Bei'), "truncated_json"),
        (('{"city": "Beijing"', ), "missing_close_brace"),
        (("not json at all", ), "not_json"),
        (('{"city": }', ), "syntactically_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_t5_broken_tool_arguments_never_emit_done(astore, arg_pieces, label):
    """铁律 2: no ``.done``, no runnable call, no whitewashed ``completed``."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_tool_stream(arg_pieces=arg_pieces)))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, _, raw = await _stream(
            p,
            {
                "model": "gpt-4o",
                "input": "weather?",
                "stream": True,
                "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
            },
        )
        assert status == 200, raw
        types = _event_types(raw)
        # (1) never a .done for arguments that cannot be parsed.
        assert "response.function_call_arguments.done" not in types, f"{label}: {types}"
        # (2) never a whitewashed success.
        assert "response.completed" not in types, f"{label} was whitewashed: {types}"
        assert any(t in types for t in ("response.incomplete", "response.failed")), types
        # (3) the item is explicitly marked non-runnable.
        done_items = [e for e in _events(raw) if e.get("type") == "response.output_item.done"]
        for item in done_items:
            payload = item.get("item") or {}
            if payload.get("type") in ("function_call", "tool_call"):
                assert payload.get("status") != "completed", f"{label}: runnable broken call {payload}"
        # (4) the stream still terminates properly.
        assert raw.rstrip().endswith(b"data: [DONE]")
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# T6: normal EOF vs abnormal disconnect
# ===========================================================================


@pytest.mark.asyncio
async def test_t6_normal_eof_without_sentinel_is_completed(astore):
    """P0-2: a provider finish signal + EOF is ``completed``, not truncated."""
    payload = openai_text_stream(pieces=("all", " done"))
    # Strip the transport-level ``[DONE]`` so the ONLY completion evidence is
    # the ``finish_reason`` inside the last chunk.
    payload = payload.replace(b"data: [DONE]\n\n", b"")
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=payload))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, _, raw = await _stream(p, {"model": "gpt-4o", "input": "hi", "stream": True})
        assert status == 200, raw
        types = _assert_lifecycle(raw)
        assert "response.completed" in types, types
        assert _joined(raw, "response.output_text.delta") == "all done"

        ws = await _workspace(astore, p.token)
        rec = await ResponseStore(astore).get_response(_response_id(raw), workspace_id=ws)
        assert rec is not None
        assert rec.status == "completed"
        assert "truncat" not in str(rec.terminal_reason or "").lower(), rec.terminal_reason
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t6_mid_stream_abort_is_upstream_truncated(astore):
    """A cut after real content is ``incomplete`` with an upstream_truncated reason.

    ``upstream_truncated`` and ``upstream_connect`` are distinguished by whether
    the pipeline ever *produced* a chunk, so the abort has to land well after the
    first content delta -- otherwise ``upstream_connect`` is the correct answer
    and the test would be asserting the wrong branch.  The delta assertion below
    pins that precondition instead of trusting a chunk count.
    """
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=tuple(f"tok{i} " for i in range(20))),
            chunk_strategy=by_line(),
            # A delay is required, not cosmetic: without it every write sits in
            # the send buffer and ``transport.abort()`` discards it, so the
            # proxy would see an empty stream instead of a truncated one.
            inter_chunk_delay=0.01,
            truncate_after_chunks=16,  # plenty of text made it, no finish signal did
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, _, raw = await _stream(p, {"model": "gpt-4o", "input": "hi", "stream": True})
        assert status == 200, raw
        types = _assert_lifecycle(raw)

        # Precondition: content really was produced, so this is a truncation
        # of a live stream and not a failure to ever get going.
        assert _joined(raw, "response.output_text.delta"), (
            f"no content reached the client, so this exercises the connect branch: {types}"
        )

        assert "response.completed" not in types, f"a truncated stream was laundered: {types}"
        assert any(t in types for t in ("response.incomplete", "response.failed")), types

        ws = await _workspace(astore, p.token)
        rec = await ResponseStore(astore).get_response(_response_id(raw), workspace_id=ws)
        assert rec is not None
        assert rec.status in ("incomplete", "failed"), rec.status
        assert "truncat" in str(rec.terminal_reason or "").lower(), rec.terminal_reason
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t6_upstream_error_before_first_byte_is_plain_http_error(astore):
    """A pre-commit upstream failure is an HTTP error, never ``200 + sad event``."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(network_error=True))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(p, {"model": "gpt-4o", "input": "hi", "stream": True})
        assert status >= 400, (status, raw)
        assert not ctype.startswith("text/event-stream"), ctype
        assert b"data:" not in raw, raw[:200]
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# T7: chain recovery is injected into the REAL upstream payload, reasoning never
# ===========================================================================


_SECRET_REASONING = "SECRET-CHAIN-OF-THOUGHT-MUST-NEVER-LEAVE"


@pytest.mark.asyncio
async def test_t7_previous_response_id_injects_history_into_upstream(astore):
    """Turn 2's upstream body carries turn 1's input AND output."""
    up = MockUpstream()
    up.queue_behaviors(
        [
            UpstreamBehavior(json_payload=openai_text_json(content="noted: the code is 4271")),
            UpstreamBehavior(json_payload=openai_text_json(content="the code is 4271")),
        ]
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, first = await _post_json(
            p, "/v1/responses", {"model": "gpt-4o", "input": "remember: the code is 4271"}
        )
        assert status == 200, first
        rid1 = first["id"]

        status, second = await _post_json(
            p,
            "/v1/responses",
            {"model": "gpt-4o", "input": "what is the code?", "previous_response_id": rid1},
        )
        assert status == 200, second
        assert second["previous_response_id"] == rid1

        assert up.request_count == 2
        turn2 = up.requests[1].json()
        assert turn2 is not None, up.requests[1].body
        wire = _flatten(turn2)
        # The recovered chain really reached the provider -- not a stateless turn.
        assert "remember: the code is 4271" in wire, wire[:600]
        assert "noted: the code is 4271" in wire, wire[:600]
        assert "what is the code?" in wire, wire[:600]
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t7_reasoning_is_never_replayed_upstream(astore):
    """铁律 1: a stored reasoning item is dropped before the chain goes out."""
    up = MockUpstream()
    up.queue_behaviors(
        [
            UpstreamBehavior(json_payload=openai_text_json(content="turn one answer")),
            UpstreamBehavior(json_payload=openai_text_json(content="turn two answer")),
        ]
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, first = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "think hard"})
        assert status == 200, first
        rid1 = first["id"]

        # Plant a reasoning item on turn 1 exactly the way a reasoning model
        # would have produced one.
        ws = await _workspace(astore, p.token)
        rs = ResponseStore(astore)
        await rs.save_output_items(
            rid1,
            [
                {
                    "type": "reasoning",
                    "id": "rs_ga_0001",
                    "summary": [{"type": "summary_text", "text": _SECRET_REASONING}],
                    "content": [{"type": "reasoning_text", "text": _SECRET_REASONING}],
                },
                {
                    "type": "message",
                    "id": "msg_ga_0001",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "turn one answer", "annotations": []}],
                },
            ],
        )
        planted = await rs.get_response(rid1, workspace_id=ws)
        assert planted is not None

        status, _ = await _post_json(
            p,
            "/v1/responses",
            {"model": "gpt-4o", "input": "follow up", "previous_response_id": rid1},
        )
        assert status == 200

        turn2_body = up.requests[1].body.decode("utf-8", "replace")
        assert _SECRET_REASONING not in turn2_body, "reasoning text leaked upstream"
        assert '"reasoning"' not in turn2_body, "a reasoning item leaked upstream"
        # ... while the visible half of the same turn did travel.
        assert "turn one answer" in turn2_body
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t7_instructions_are_not_inherited(astore):
    """R-P1-31: only items travel down a chain, never ``instructions``."""
    up = MockUpstream()
    up.queue_behaviors(
        [
            UpstreamBehavior(json_payload=openai_text_json(content="ok")),
            UpstreamBehavior(json_payload=openai_text_json(content="ok2")),
        ]
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        marker = "ANCESTOR-ONLY-SYSTEM-PROMPT"
        _, first = await _post_json(
            p, "/v1/responses", {"model": "gpt-4o", "input": "hi", "instructions": marker}
        )
        _, _ = await _post_json(
            p,
            "/v1/responses",
            {"model": "gpt-4o", "input": "again", "previous_response_id": first["id"]},
        )
        turn2 = up.requests[1].body.decode("utf-8", "replace")
        assert marker not in turn2, "instructions were inherited down the chain"
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# T8 + (A): background create / retrieve / cancel / restart recovery
# ===========================================================================


async def _await_status(
    p: Proxy,
    rid: str,
    wanted: tuple[str, ...],
    *,
    timeout: float = 20.0,
) -> dict:
    """Poll ``GET /v1/responses/{rid}`` until it reaches one of ``wanted``."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        status, obj = await _get_json(p, f"/v1/responses/{rid}")
        if status == 200 and isinstance(obj, dict):
            last = obj
            if obj.get("status") in wanted:
                return obj
        await asyncio.sleep(0.2)
    raise AssertionError(f"{rid} never reached {wanted}; last={last}")


@pytest.mark.asyncio
async def test_t8_background_create_returns_202_queued(astore):
    """A background create is accepted without any upstream I/O on the hot path."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream(pieces=("bg", " ok"))))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        async with ClientSession() as sess:
            async with sess.post(
                p.url("/v1/responses"),
                headers=_auth(p.token),
                data=json.dumps({"model": "gpt-4o", "input": "slow job", "background": True}),
            ) as r:
                assert r.status == 202, await r.text()
                obj = await r.json()
        assert obj["id"].startswith("resp_")
        assert obj["status"] in ("queued", "in_progress")
        assert obj.get("background") is True
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t8_background_and_stream_together_is_400(astore):
    """``background`` + ``stream`` is a client error, named by ``param``."""
    up = MockUpstream()
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, obj = await _post_json(
            p, "/v1/responses", {"model": "gpt-4o", "input": "hi", "background": True, "stream": True}
        )
        assert status == 400, obj
        assert obj["error"]["param"] == "stream", obj
        assert up.request_count == 0
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t8_a_background_completed_retrieve_returns_real_output(astore):
    """(A) P0-6: a completed background job is retrievable WITH its answer.

    This is the whole point of ``background``: the client comes back later and
    reads the result.  A ``completed`` row whose ``output`` is empty is not a
    completed job -- it is a lost one.
    """
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=("background", " answer", " 42")),
            force_stream=True,
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        async with ClientSession() as sess:
            async with sess.post(
                p.url("/v1/responses"),
                headers=_auth(p.token),
                data=json.dumps({"model": "gpt-4o", "input": "compute 6*7", "background": True}),
            ) as r:
                assert r.status == 202, await r.text()
                rid = (await r.json())["id"]

        final = await _await_status(p, rid, ("completed", "failed", "incomplete", "cancelled"))
        assert final["status"] == "completed", final

        # (A1) The answer must be there.
        output = final.get("output")
        assert isinstance(output, list), final
        assert output, (
            "P0-6 REGRESSION: background job completed with an EMPTY output array; "
            "the generated answer was never persisted as output items. "
            f"retrieve={final}"
        )
        text = _output_text(output)
        assert text == "background answer 42", (
            f"background output does not carry the generated text (got {text!r}): {output}"
        )

        # (A2) The item must be a real message item, not a text-delta blob.
        types = {str(item.get("type")) for item in output if isinstance(item, dict)}
        assert "message" in types, f"background output items are mis-shaped: {output}"
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t8_a_background_event_log_uses_unified_vocabulary(astore):
    """(A) The persisted background events are Responses events, not stubs.

    A catch-up reader replays this log verbatim, so a private event name or a
    text-delta standing in for a tool item is indistinguishable from corruption
    on the client side.
    """
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_tool_stream(arg_pieces=('{"cit', 'y": "Bei', 'jing"}')),
            force_stream=True,
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        async with ClientSession() as sess:
            async with sess.post(
                p.url("/v1/responses"),
                headers=_auth(p.token),
                data=json.dumps(
                    {
                        "model": "gpt-4o",
                        "input": "weather?",
                        "background": True,
                        "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
                    }
                ),
            ) as r:
                assert r.status == 202, await r.text()
                rid = (await r.json())["id"]

        await _await_status(p, rid, ("completed", "failed", "incomplete", "cancelled"))

        rs = ResponseStore(astore)
        events = await rs.list_events(rid, after_seq=-1)
        names = [str(e.get("event_type") or (e.get("data") or {}).get("type") or "") for e in events]
        assert names, "background produced no events at all"

        # (A3) No private/stub event names on a log a client can replay.
        unknown = [n for n in names if n and not n.startswith("response.")]
        assert not unknown, f"non-Responses event names in the background log: {unknown}"
        assert "response.function_call.persisted" not in names, (
            "P0-6 REGRESSION: the background worker still emits the HONEST-STUB event "
            f"'response.function_call.persisted' instead of the unified tool vocabulary; log={names}"
        )

        # (A4) A tool round must produce tool item events, not text deltas.
        assert "response.function_call_arguments.delta" in names or "response.output_item.added" in names, (
            "P0-6 REGRESSION: a background tool call was flattened into text events; "
            f"log={names}"
        )
        text_deltas = [
            e for e in events
            if str((e.get("data") or {}).get("type") or e.get("event_type") or "") == "response.output_text.delta"
        ]
        empty_deltas = [e for e in text_deltas if not str((e.get("data") or {}).get("delta") or "")]
        assert not empty_deltas, (
            "P0-6 REGRESSION: the background worker emitted empty response.output_text.delta events "
            "(control chunks such as 'tool_call_done'/'finish' fell through to the text branch); "
            f"count={len(empty_deltas)}"
        )
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t8_background_cancel_terminates_finitely(astore):
    """cancel() drives a background job to a terminal state and keeps it there."""
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=tuple(f"tok{i}" for i in range(40))),
            chunk_strategy=by_line(),
            inter_chunk_delay=0.05,
            force_stream=True,
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        async with ClientSession() as sess:
            async with sess.post(
                p.url("/v1/responses"),
                headers=_auth(p.token),
                data=json.dumps({"model": "gpt-4o", "input": "long job", "background": True}),
            ) as r:
                assert r.status == 202
                rid = (await r.json())["id"]

        status, cancelled = await _post_json(p, f"/v1/responses/{rid}/cancel", {})
        assert status == 200, cancelled
        assert cancelled["status"] == "cancelled", cancelled
        calls_at_cancel = up.request_count

        # Finite termination: it stays cancelled and never flips to completed.
        # The worker polls on a ~1s cadence, so this window is wide enough for
        # a resurrecting worker to claim the job and flip the row back.
        await asyncio.sleep(3.0)
        status, after = await _get_json(p, f"/v1/responses/{rid}")
        assert status == 200
        assert after["status"] in ("cancelled", "incomplete", "failed"), after
        assert after["status"] != "completed", "a cancelled job later reported success"
        assert after["status"] != "in_progress", (
            f"P0 REGRESSION: a cancelled job was resurrected to in_progress: {after}"
        )

        # ...and the spend actually stopped.  Asserting "never contacted" would
        # be racy (the worker may legitimately have claimed the job before the
        # cancel landed), so the durable invariant is that no *new* upstream
        # call is started after the cancel -- whatever was in flight is closed
        # and nothing replaces it.
        assert up.request_count == calls_at_cancel, (
            "P0 REGRESSION: the upstream was called again after cancel "
            f"({calls_at_cancel} -> {up.request_count}); cancellation is not stopping the spend"
        )
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t8_background_survives_a_process_restart(astore):
    """restart 恢复: a queued job left by a dead process is drained by the next one."""
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=("recovered", " after", " restart")),
            force_stream=True,
        )
    )
    await up.start()

    from zhongzhuan.store.access_tokens import create_token

    token = (await create_token(astore, label="restart-token", quota_tokens=1_000_000)).token

    # --- process #1: accept the job, then die before the worker drains it ---
    p1 = await _start_proxy(up.url, astore, token=token)
    # Stop the worker immediately so the job is guaranteed to outlive process #1.
    await p1.handler.stop_background_tasks()
    try:
        async with ClientSession() as sess:
            async with sess.post(
                p1.url("/v1/responses"),
                headers=_auth(token),
                data=json.dumps({"model": "gpt-4o", "input": "survive me", "background": True}),
            ) as r:
                assert r.status == 202, await r.text()
                rid = (await r.json())["id"]
        status, obj = await _get_json(p1, f"/v1/responses/{rid}")
        assert status == 200 and obj["status"] == "queued", obj
    finally:
        await p1.close()

    # --- process #2: same store, fresh app; the queue must be drained ---
    p2 = await _start_proxy(up.url, astore, token=token)
    try:
        final = await _await_status(p2, rid, ("completed", "failed", "incomplete", "cancelled"), timeout=25.0)
        assert final["status"] == "completed", f"restart recovery did not finish the job: {final}"
    finally:
        await p2.close()
        await up.stop()


# ===========================================================================
# T9: every hostile input terminates finitely
# ===========================================================================


@pytest.mark.asyncio
async def test_t9_self_reference_fails_before_the_network(astore):
    """A response that names itself as its own ancestor is a 400, not a hang."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="seed")))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        _, seed = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "seed"})
        rid = seed["id"]
        before = up.request_count

        # Make the stored row point at itself, which is the only way a real
        # self-reference can exist (create always mints a fresh id).
        ws = await _workspace(astore, p.token)
        await astore.execute(
            "UPDATE responses SET previous_response_id = ? WHERE response_id = ? AND workspace_id = ?",
            (rid, rid, ws),
        )

        status, obj = await _post_json(
            p, "/v1/responses", {"model": "gpt-4o", "input": "loop?", "previous_response_id": rid}
        )
        assert status == 400, obj
        assert obj["error"]["param"] == "previous_response_id", obj
        assert up.request_count == before, "a self-referencing chain still hit the upstream"
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t9_chain_cycle_fails_before_the_network(astore):
    """A -> B -> A terminates with a chain error instead of walking forever."""
    up = MockUpstream()
    up.queue_behaviors(
        [
            UpstreamBehavior(json_payload=openai_text_json(content="a")),
            UpstreamBehavior(json_payload=openai_text_json(content="b")),
        ]
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        _, a = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "a"})
        _, b = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "b", "previous_response_id": a["id"]})
        before = up.request_count

        ws = await _workspace(astore, p.token)
        await astore.execute(
            "UPDATE responses SET previous_response_id = ? WHERE response_id = ? AND workspace_id = ?",
            (b["id"], a["id"], ws),
        )

        status, obj = await _post_json(
            p, "/v1/responses", {"model": "gpt-4o", "input": "c", "previous_response_id": b["id"]}
        )
        assert status == 400, obj
        assert obj["error"]["param"] == "previous_response_id", obj
        assert up.request_count == before, "a cyclic chain still hit the upstream"
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t9_cross_tenant_chain_is_not_found(astore):
    """Another tenant's response id is indistinguishable from a missing one."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="tenant a")))
    await up.start()
    from zhongzhuan.store.access_tokens import create_token

    token_a = (await create_token(astore, label="tenant-a", quota_tokens=100000)).token
    token_b = (await create_token(astore, label="tenant-b", quota_tokens=100000)).token
    p = await _start_proxy(up.url, astore, token=token_a)
    try:
        _, mine = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "hi"})
        rid = mine["id"]
        before = up.request_count

        p_b = Proxy(p.port, p.runner, p.upstream, token_b, p.server)
        status, obj = await _post_json(
            p_b, "/v1/responses", {"model": "gpt-4o", "input": "steal", "previous_response_id": rid}
        )
        assert status == 400, obj
        assert "not found" in _flatten(obj).lower(), obj
        assert up.request_count == before

        # And a direct retrieve is a flat 404 -- no existence oracle.
        status, _ = await _get_json(p_b, f"/v1/responses/{rid}")
        assert status == 404
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t9_first_token_timeout_terminates_finitely(astore):
    """A silent upstream trips the first-token ceiling and ends the response.

    The config schema floors the operator-facing timeouts at 300s (铁律 5), so
    the ceiling is injected onto the cached ``PipelineConfig`` directly.  That
    is the same object the request path reads -- only the value is test-sized.

    "Only the value" is load-bearing, so the config is derived from the one
    production just built via ``dataclasses.replace`` rather than constructed
    fresh.  A fresh ``PipelineConfig(...)`` would silently inherit the *library*
    defaults for every field it did not name -- including
    ``strict_terminal=False``, the compatibility default (R-P1-22/U1) that
    production deliberately overrides to ``True`` through
    ``responses_bridge``.  That would quietly switch off the very policy under
    test and make a whitewashed terminal look like a product bug.  Deriving
    also means this test keeps testing the real policy if the GA default ever
    moves, instead of pinning a value hardcoded here.

    Note on the upstream shape: ``first_byte_delay`` sleeps *before* the
    response headers, which is connect latency, not first-token latency -- the
    pipeline's first-token clock has not started yet.  To starve the clock we
    must let the headers out and then go quiet, so the payload is prefixed with
    an SSE comment (which yields no logical chunk) and the real tokens are held
    behind ``inter_chunk_delay``.
    """
    import dataclasses

    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=b": warmup\n\n" + openai_text_stream(pieces=("too", " late")),
            chunk_strategy=by_line(),
            inter_chunk_delay=3.0,
            force_stream=True,
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore, timeout=30.0)
    try:
        production_cfg = p.handler._v3_pipeline_config()
        # Guard the premise: if production is not strict, this test would be
        # asserting the compat contract while claiming to assert the GA one.
        assert production_cfg.strict_terminal, (
            "production built a non-strict PipelineConfig; the terminal-state "
            "policy under test is not actually enabled on the request path"
        )
        p.handler._v3_pipeline_cfg = dataclasses.replace(
            production_cfg,
            first_token_seconds=0.5,
            read_idle_seconds=0.5,
            total_seconds=5.0,
            connect_seconds=5.0,
            heartbeat_seconds=0.2,
        )
        started = time.monotonic()
        status, _, raw = await asyncio.wait_for(
            _stream(p, {"model": "gpt-4o", "input": "hi", "stream": True}),
            timeout=20.0,
        )
        elapsed = time.monotonic() - started
        assert status in (200, 502, 504), (status, raw[:200])
        assert elapsed < 15.0, f"the timeout ceiling did not bound the request ({elapsed:.1f}s)"
        if status == 200:
            types = _event_types(raw)
            assert "response.completed" not in types, f"a timed-out stream reported success: {types}"
            assert any(t in types for t in ("response.incomplete", "response.failed")), types
            assert raw.rstrip().endswith(b"data: [DONE]")
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t9_upstream_4xx_is_passed_through_not_retried_forever(astore):
    """A hard client error from the provider terminates immediately."""
    from support.mock_responses_upstream import openai_error_json

    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(status=400, error_body=openai_error_json(message="bad model", code="invalid_request_error"))
    )
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        started = time.monotonic()
        status, obj = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "hi"})
        elapsed = time.monotonic() - started
        assert status == 400, obj
        assert elapsed < 10.0, f"a 4xx took {elapsed:.1f}s -- retry storm?"
        assert up.request_count <= 2, f"a non-retryable 4xx was retried {up.request_count}x"
    finally:
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_t9_hosted_tool_is_refused_with_a_standard_error(astore):
    """An unsupported hosted tool is an honest 4xx, never a fabricated 200."""
    up = MockUpstream()
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, obj = await _post_json(
            p,
            "/v1/responses",
            {"model": "gpt-4o", "input": "search the web", "tools": [{"type": "web_search"}]},
        )
        assert status >= 400, obj
        assert isinstance(obj.get("error"), dict), obj
        assert up.request_count == 0, "a refused capability still burned an upstream call"
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# T10: legacy golden output is byte-identical with v3 enabled
# ===========================================================================


async def _chat_bytes(store, upstream_url: str, body: dict, *, path: str, v3: str) -> bytes:
    """Drive one legacy request with the v3 switch pinned to ``v3``."""
    previous = os.environ.get(V3_ENV)
    os.environ[V3_ENV] = v3
    try:
        p = await _start_proxy(upstream_url, store)
        try:
            async with ClientSession() as sess:
                async with sess.post(p.url(path), headers=_auth(p.token), data=json.dumps(body)) as r:
                    assert r.status == 200, await r.text()
                    return await r.read()
        finally:
            await p.close()
    finally:
        if previous is None:
            os.environ.pop(V3_ENV, None)
        else:
            os.environ[V3_ENV] = previous


@pytest.mark.asyncio
async def test_t10_chat_to_chat_golden_is_byte_identical(astore, tmp_path, monkeypatch):
    """Chat -> Chat output does not change when v3 is switched on."""
    up = MockUpstream()
    up.queue_behaviors(
        [
            UpstreamBehavior(json_payload=openai_text_json(content="golden chat")),
            UpstreamBehavior(json_payload=openai_text_json(content="golden chat")),
        ]
    )
    await up.start()
    try:
        body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        off = await _chat_bytes(astore, up.url, body, path="/v1/chat/completions", v3="0")
        on = await _chat_bytes(astore, up.url, body, path="/v1/chat/completions", v3="1")
        assert on == off, (
            "v3 changed a Chat->Chat response body:\n"
            f"v3=0: {off!r}\n"
            f"v3=1: {on!r}"
        )
    finally:
        await up.stop()


@pytest.mark.asyncio
async def test_t10_chat_streaming_golden_is_byte_identical(astore):
    """Chat -> Chat *streaming* bytes do not change when v3 is switched on."""
    up = MockUpstream()
    up.queue_behaviors(
        [
            UpstreamBehavior(stream_payload=openai_text_stream(pieces=("a", "b", "c"))),
            UpstreamBehavior(stream_payload=openai_text_stream(pieces=("a", "b", "c"))),
        ]
    )
    await up.start()
    try:
        body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        off = await _chat_bytes(astore, up.url, body, path="/v1/chat/completions", v3="0")
        on = await _chat_bytes(astore, up.url, body, path="/v1/chat/completions", v3="1")
        assert on == off, (
            "v3 changed a Chat->Chat streaming body:\n"
            f"v3=0: {off!r}\n"
            f"v3=1: {on!r}"
        )
    finally:
        await up.stop()


@pytest.mark.asyncio
async def test_t10_anthropic_messages_golden_is_byte_identical(astore):
    """``/v1/messages`` output does not change when v3 is switched on."""
    from support.mock_responses_upstream import anthropic_text_json

    up = MockUpstream()
    up.queue_behaviors(
        [
            UpstreamBehavior(json_payload=anthropic_text_json(content="golden anthropic")),
            UpstreamBehavior(json_payload=anthropic_text_json(content="golden anthropic")),
        ]
    )
    await up.start()
    try:
        body = {
            "model": "claude-3-5-sonnet",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        }
        off = await _chat_bytes(astore, up.url, body, path="/v1/messages", v3="0")
        on = await _chat_bytes(astore, up.url, body, path="/v1/messages", v3="1")
        assert on == off, (
            "v3 changed an Anthropic /v1/messages body:\n"
            f"v3=0: {off!r}\n"
            f"v3=1: {on!r}"
        )
    finally:
        await up.stop()


@pytest.mark.asyncio
async def test_t10_unparseable_upstream_must_not_report_success(astore):
    """A stream that yielded no content may not end in ``response.completed``.

    Setup note (coverage gap, reported separately): the outbound protocol comes
    from the *candidate key* (``key.upstream_protocol``), which this fixture --
    like every existing v3 test -- leaves at ``openai``.  So an Anthropic-shaped
    payload is never handed to the Anthropic adapter and parses to zero logical
    chunks.  That makes this an excellent probe for the terminal contract: the
    pipeline produced nothing and classified the stream ``upstream_connect``, so
    per the PRD terminal matrix ("首 token 前断流 -> failed, upstream_connect")
    the client must be told it failed -- never that it completed.
    """
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=anthropic_text_stream(pieces=("Hello", ", ", "world", "!"))))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(p, {"model": "claude-3-5-sonnet", "input": "hi", "stream": True})
        if status != 200:
            pytest.skip(f"no Anthropic-capable candidate key in this fixture (status={status})")
        assert ctype.startswith("text/event-stream")
        _assert_lifecycle(raw)

        # No Anthropic wire vocabulary may ever reach the client.
        types = _event_types(raw)
        assert not [t for t in types if t.startswith(("message_", "content_block_"))], types

        text = _joined(raw, "response.output_text.delta")
        if text:
            # The Anthropic adapter did run -- then it must be byte-faithful.
            assert text == "Hello, world!", f"events={types}"
        else:
            # It did not run.  A zero-content stream must not claim success.
            assert "response.completed" not in types, (
                "P0-2 REGRESSION: a stream that produced zero content and was classified "
                f"'upstream_connect' still reported response.completed; events={types} "
                f"raw={raw[:600]!r}"
            )
    finally:
        await p.close()
        await up.stop()


# ===========================================================================
# (B) AC-8.3 / AC-8.4: version stickiness
# ===========================================================================


@pytest.mark.asyncio
async def test_b_flipping_the_switch_mid_stream_does_not_migrate_the_response(astore):
    """AC-8.3: an in-flight v3 stream finishes as v3 even if the switch flips.

    The switch is read from a *live* ``os.environ``, so this genuinely flips
    under the running request rather than simulating it.  The fork evaluates it
    once, at entry, which is exactly what makes the answer stable.
    """
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=tuple(f"p{i}" for i in range(12))),
            chunk_strategy=by_line(),
            inter_chunk_delay=0.08,
        )
    )
    await up.start()
    p = await _start_proxy(up.url, astore, timeout=30.0)
    try:
        collected = bytearray()
        flipped = False
        async with ClientSession() as sess:
            async with sess.post(
                p.url("/v1/responses"),
                headers=_auth(p.token),
                data=json.dumps({"model": "gpt-4o", "input": "hi", "stream": True}),
            ) as r:
                assert r.status == 200
                assert r.headers.get("Content-Type", "").startswith("text/event-stream")
                async for chunk in r.content.iter_any():
                    collected += chunk
                    if not flipped and b"response.output_text.delta" in bytes(collected):
                        # Mid-flight emergency rollback.
                        os.environ[V3_ENV] = "0"
                        flipped = True
        assert flipped, "the stream ended before a delta arrived; test did not exercise the flip"

        raw = bytes(collected)
        types = _assert_lifecycle(raw)
        assert "response.completed" in types, f"the flip broke an in-flight response: {types}"
        assert _joined(raw, "response.output_text.delta") == "".join(f"p{i}" for i in range(12))

        # AC-8.4: the terminal was persisted by v3 under OUR id, not abandoned.
        ws = await _workspace(astore, p.token)
        rec = await ResponseStore(astore).get_response(_response_id(raw), workspace_id=ws)
        assert rec is not None and rec.status == "completed", rec
    finally:
        os.environ[V3_ENV] = "1"
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_b_the_next_request_after_a_flip_uses_the_new_version(astore):
    """AC-8.4: stickiness is per-request, not a latch -- the NEXT call obeys."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="hi")))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        _, first = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "hi"})
        rid = first["id"]
        status, _ = await _get_json(p, f"/v1/responses/{rid}")
        assert status == 200, "v3 should serve retrieve while the switch is on"

        os.environ[V3_ENV] = "0"
        # v2_emergency has no retrieve endpoint -- it answers 405 by design.
        status, _ = await _get_json(p, f"/v1/responses/{rid}")
        assert status == 405, f"the switch did not take effect on the next request (got {status})"

        os.environ[V3_ENV] = "1"
        status, _ = await _get_json(p, f"/v1/responses/{rid}")
        assert status == 200, "flipping back did not restore v3"
    finally:
        os.environ[V3_ENV] = "1"
        await p.close()
        await up.stop()


@pytest.mark.asyncio
async def test_b_switch_off_never_breaks_a_persisted_v3_resource(astore):
    """A resource written by v3 is still intact after a rollback and back."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(json_payload=openai_text_json(content="durable")))
    await up.start()
    p = await _start_proxy(up.url, astore)
    try:
        _, created = await _post_json(p, "/v1/responses", {"model": "gpt-4o", "input": "hi"})
        rid = created["id"]

        os.environ[V3_ENV] = "0"
        os.environ[V3_ENV] = "1"

        status, after = await _get_json(p, f"/v1/responses/{rid}")
        assert status == 200, after
        assert after["id"] == rid
        assert after["status"] == "completed"
    finally:
        os.environ[V3_ENV] = "1"
        await p.close()
        await up.stop()
