"""V1 多代理协议服务端实现测试（APIAADBPW-REQ-MA-001 / FR-1~FR-6）。

覆盖：
* ``build_tool_search_output`` 形状与 ``multi_agent_v1`` namespace 断言（FR-2）。
* ``spawn_agent`` 描述含两段 ``###`` 必含文案（需求文档 §4.2）。
* ``MultiAgentOrchestrator`` 状态机：spawn / wait / close / 未知 agent /
  未知 namespace / max_threads 并发上限 / 并发隔离（FR-3 / FR-4 / NFR-6）。
* ``ResponsePipeline`` 特殊调用拦截：``tool_search`` 合成、``multi_agent_v1``
  namespaced 调用执行并回传 ``function_call_output``（FR-2 / FR-3）；关闭时透传。
* ``hosted_tool_emulated_capabilities`` 在 ``tool_search_enabled`` 时包含
  ``TOOL_SEARCH``，消除 ``no route can serve capability: tool_search``（AC-1）。
* ``_build_codex_model_info`` 在开启时声明 ``multi_agent_version`` 等能力（FR-5）。
"""

from __future__ import annotations

import json

import pytest

from zhongzhuan.responses_v3.multi_agent import (
    MULTI_AGENT_NAMESPACE,
    MULTI_AGENT_TOOLS,
    TOOL_SEARCH_NAME,
    MultiAgentOrchestrator,
    build_function_call_output,
    build_tool_search_output,
)
from zhongzhuan.responses_v3.pipeline import ResponsePipeline


# ---------------------------------------------------------------------------
# 1. tool_search_output 合成（FR-2）
# ---------------------------------------------------------------------------


def test_tool_search_output_shape():
    item = build_tool_search_output(
        output_index=2, call_id="call_t1", response_id="resp_9", query="find", limit=5
    )
    assert item["type"] == "tool_search_output"
    assert item["status"] == "completed"
    assert item["call_id"] == "call_t1"
    tools = item["output"]["tools"]
    assert len(tools) == 1
    ns = tools[0]
    assert ns["type"] == "namespace"
    assert ns["name"] == MULTI_AGENT_NAMESPACE
    # 5 个 deferred 子工具，全部 defer_loading: true。
    sub = ns["tools"]
    assert [t["name"] for t in sub] == list(MULTI_AGENT_TOOLS)
    for t in sub:
        assert t.get("defer_loading") is True
    # spawn_agent 必须带两段 ### 描述文案（§4.2 硬性约束）。
    spawn = next(t for t in sub if t["name"] == "spawn_agent")
    desc = spawn["description"]
    assert "### Designing delegated subtasks" in desc
    assert "### When to delegate vs. do the subtask yourself" in desc


def test_build_tool_search_call_shape():
    # FR-2.1 / C.10.3：tool_search_call(execution="client") 形态。
    from zhongzhuan.responses_v3.multi_agent import build_tool_search_call

    item = build_tool_search_call(
        output_index=0, call_id="call_t1", arguments='{"query":"x","limit":5}',
        response_id="r1", execution="client",
    )
    assert item["type"] == "tool_search_call"
    assert item["execution"] == "client"
    assert item["name"] == TOOL_SEARCH_NAME
    assert item["call_id"] == "call_t1"
    assert item["arguments"] == '{"query":"x","limit":5}'
    assert item["status"] == "completed"


def test_function_call_output_shape():
    item = build_function_call_output(
        output_index=0, call_id="c1", response_id="r1", output="hello"
    )
    assert item["type"] == "function_call_output"
    assert item["status"] == "completed"
    assert item["call_id"] == "c1"
    assert item["output"] == "hello"


def test_build_tool_search_function_call_output_shape():
    # C.7.5 验收（FR-2 v1.3 / 26.803 形态）：function_call_output.output 是
    # JSON 字符串，tools[0].name == multi_agent_v1，5 子工具 defer_loading 全 true。
    from zhongzhuan.responses_v3.multi_agent import build_tool_search_function_call_output

    item = build_tool_search_function_call_output(
        output_index=1, call_id="call_t1", response_id="resp_9", query="find", limit=5
    )
    assert item["type"] == "function_call_output"
    assert item["status"] == "completed"
    assert item["call_id"] == "call_t1"
    # output 是 JSON 字符串（不是对象）。
    assert isinstance(item["output"], str)
    payload = json.loads(item["output"])
    tools = payload["tools"]
    assert len(tools) == 1
    ns = tools[0]
    assert ns["type"] == "namespace"
    assert ns["name"] == MULTI_AGENT_NAMESPACE
    sub = ns["tools"]
    assert [t["name"] for t in sub] == list(MULTI_AGENT_TOOLS)
    for t in sub:
        assert t.get("defer_loading") is True  # 必须存在且为 true，不是 null/缺省
    spawn = next(t for t in sub if t["name"] == "spawn_agent")
    assert "### Designing delegated subtasks" in spawn["description"]
    assert "### When to delegate vs. do the subtask yourself" in spawn["description"]


# ---------------------------------------------------------------------------
# 2. MultiAgentOrchestrator 状态机（FR-3 / FR-4 / NFR-6）
# ---------------------------------------------------------------------------


async def _fake_runner(instruction: str, model: str, session_id: str) -> str:
    return "RESULT:" + instruction


async def test_orchestrator_spawn_role_tag_routes_model():
    # FR-8 / 附录 C.12.4（方案 A.1）：[role] 前缀确定子代理模型并剥除前缀。
    from zhongzhuan.responses_v3.multi_agent import ROLE_MODEL_MAP

    orch = MultiAgentOrchestrator(runner=_fake_runner, default_model="juhe/mimo-v2.5-pro")
    spawn = await orch.handle(
        MULTI_AGENT_NAMESPACE, "spawn_agent", "c1",
        json.dumps({"instruction": "[explorer] research the files", "model": ""}),
        output_index=0,
    )
    agent_id = json.loads(spawn["output"])["agent_id"]
    st = orch._agents[agent_id]
    assert st.model == ROLE_MODEL_MAP["explorer"]  # 角色标记路由到 deepseek
    assert st.instruction == "research the files"  # 前缀已剥除


async def test_orchestrator_spawn_role_tag_explicit_model_wins():
    # 角色标记优先于 default_model；显式 model 字段保留（MCP 桥接路径）。
    from zhongzhuan.responses_v3.multi_agent import ROLE_MODEL_MAP

    orch = MultiAgentOrchestrator(runner=_fake_runner, default_model="juhe/mimo-v2.5-pro")
    # 无角色标记 + 显式 model → 用显式 model。
    spawn = await orch.handle(
        MULTI_AGENT_NAMESPACE, "spawn_agent", "c1",
        json.dumps({"instruction": "do it", "model": "juhe/glm5.2"}),
        output_index=0,
    )
    agent_id = json.loads(spawn["output"])["agent_id"]
    assert orch._agents[agent_id].model == "juhe/glm5.2"
    # 无角色标记 + 无 model → 父模型 default。
    spawn2 = await orch.handle(
        MULTI_AGENT_NAMESPACE, "spawn_agent", "c2",
        json.dumps({"instruction": "do it again"}),
        output_index=0,
    )
    agent_id2 = json.loads(spawn2["output"])["agent_id"]
    assert orch._agents[agent_id2].model == "juhe/mimo-v2.5-pro"


async def test_orchestrator_spawn_session_fallback():
    # FR-8 / NFR-2：session_id 空时继承父请求会话（Codex 原生 spawn 不传 session_id）。
    orch = MultiAgentOrchestrator(runner=_fake_runner, default_model="m1", default_session="tok:7")
    spawn = await orch.handle(
        MULTI_AGENT_NAMESPACE, "spawn_agent", "c1",
        json.dumps({"instruction": "do X"}),
        output_index=0,
    )
    agent_id = json.loads(spawn["output"])["agent_id"]
    assert orch._agents[agent_id].session_id == "tok:7"
    # 显式 session_id 仍优先。
    spawn2 = await orch.handle(
        MULTI_AGENT_NAMESPACE, "spawn_agent", "c2",
        json.dumps({"instruction": "do Y", "session_id": "tok:9"}),
        output_index=0,
    )
    agent_id2 = json.loads(spawn2["output"])["agent_id"]
    assert orch._agents[agent_id2].session_id == "tok:9"


async def test_orchestrator_spawn_wait_close():
    orch = MultiAgentOrchestrator(runner=_fake_runner, default_model="m1")
    # spawn
    spawn = await orch.handle(
        MULTI_AGENT_NAMESPACE, "spawn_agent", "c1",
        json.dumps({"instruction": "do X", "model": "", "session_id": "s1"}),
        output_index=0,
    )
    assert spawn["type"] == "function_call_output"
    agent_id = json.loads(spawn["output"])["agent_id"]
    assert orch.active_count("s1") >= 1
    # wait（执行 runner 并取回结果）
    wait = await orch.handle(
        MULTI_AGENT_NAMESPACE, "wait_agent", "c2",
        json.dumps({"agent_id": agent_id}), output_index=1,
    )
    out = json.loads(wait["output"])
    assert out["status"] == "completed"
    assert out["result"] == "RESULT:do X"
    # close
    close = await orch.handle(
        MULTI_AGENT_NAMESPACE, "close_agent", "c3",
        json.dumps({"agent_id": agent_id}), output_index=2,
    )
    assert json.loads(close["output"])["closed"] is True
    assert orch.active_count("s1") == 0


async def test_orchestrator_unknown_agent_and_namespace():
    orch = MultiAgentOrchestrator(runner=_fake_runner)
    r = await orch.handle(
        MULTI_AGENT_NAMESPACE, "wait_agent", "c1",
        json.dumps({"agent_id": "nope"}), output_index=0,
    )
    assert "error" in json.loads(r["output"])
    # 错误 namespace 直接报错。
    r2 = await orch.handle("wrong_ns", "spawn_agent", "c2", "{}", output_index=1)
    assert "error" in json.loads(r2["output"])


async def test_orchestrator_max_threads():
    orch = MultiAgentOrchestrator(runner=_fake_runner, max_threads=1)
    a = await orch.handle(
        MULTI_AGENT_NAMESPACE, "spawn_agent", "c1",
        json.dumps({"instruction": "A", "session_id": "s"}), output_index=0,
    )
    assert "agent_id" in json.loads(a["output"])
    # 第二个并发超过上限 → 返回 error（不创建第二个 agent）。
    b = await orch.handle(
        MULTI_AGENT_NAMESPACE, "spawn_agent", "c2",
        json.dumps({"instruction": "B", "session_id": "s"}), output_index=1,
    )
    assert "error" in json.loads(b["output"])
    assert orch.active_count("s") == 1


async def test_orchestrator_concurrent_isolation():
    orch = MultiAgentOrchestrator(runner=_fake_runner, max_threads=4)
    ids = []
    for i in range(3):
        r = await orch.handle(
            MULTI_AGENT_NAMESPACE, "spawn_agent", f"c{i}",
            json.dumps({"instruction": f"task{i}", "session_id": "s"}), output_index=i,
        )
        ids.append(json.loads(r["output"])["agent_id"])
    assert len(ids) == 3
    assert orch.active_count("s") == 3


# ---------------------------------------------------------------------------
# 3. ResponsePipeline 特殊调用拦截（FR-2 / FR-3）
# ---------------------------------------------------------------------------


async def _collect(pipe: ResponsePipeline, chunks: list[dict]) -> list[dict]:
    frames: list[dict] = []
    for chunk in chunks:
        for raw in await pipe._translate_chunk(chunk):
            # raw 形如 b"event: X\ndata: {...}\n\n"
            text = raw.decode("utf-8")
            idx = text.index("data: ") + len("data: ")
            frames.append(json.loads(text[idx:].strip()))
    return frames


async def test_pipeline_tool_search_synthesized():
    # FR-2.1 / 附录 C.10.3（方案 A，默认）：tool_search 改写为
    # tool_search_call(execution="client")；不返回 function_call(name=tool_search)、
    # 不返回顶级 tool_search_output。
    pipe = ResponsePipeline("resp_ts", multi_agent=None, tool_search_enabled=True)
    frames = await _collect(pipe, [
        {"type": "tool_call", "call_id": "t1", "name": TOOL_SEARCH_NAME,
         "arguments": '{"query": "x"}'},
        {"type": "tool_call_done", "call_id": "t1", "arguments": '{"query": "x"}'},
    ])
    items = [f["item"] for f in frames if f.get("type") == "response.output_item.added"]
    # 1) tool_search_call(execution="client") 存在（FR-2.1 PASS 判据）。
    tsc = next((it for it in items if it["type"] == "tool_search_call"), None)
    assert tsc is not None, "expected tool_search_call"
    assert tsc["execution"] == "client"
    assert tsc["name"] == TOOL_SEARCH_NAME
    assert tsc["call_id"] == "t1"
    # 2) 不再以 function_call(name=tool_search) 作为返回形态；无顶级 tool_search_output。
    fc = [it for it in items if it["type"] == "function_call" and it.get("name") == TOOL_SEARCH_NAME]
    assert fc == []
    assert all(it["type"] != "tool_search_output" for it in items)
    # 3) 方案 A 不回 function_call_output。
    assert all(it["type"] != "function_call_output" for it in items)


async def test_pipeline_tool_search_server_mode():
    # FR-2.1 方案 B：tool_search_call + function_call_output 兜底。
    pipe = ResponsePipeline("resp_tsb", multi_agent=None, tool_search_enabled=True,
                            tool_search_mode="server")
    frames = await _collect(pipe, [
        {"type": "tool_call", "call_id": "t1", "name": TOOL_SEARCH_NAME,
         "arguments": '{"query": "x"}'},
        {"type": "tool_call_done", "call_id": "t1", "arguments": '{"query": "x"}'},
    ])
    items = [f["item"] for f in frames if f.get("type") == "response.output_item.added"]
    tsc = next((it for it in items if it["type"] == "tool_search_call"), None)
    assert tsc is not None and tsc["execution"] == "client"
    fco = next((it for it in items if it["type"] == "function_call_output"
                and it.get("call_id") == "t1"), None)
    assert fco is not None, "expected function_call_output in server mode"
    payload = json.loads(fco["output"])
    assert payload["tools"][0]["name"] == MULTI_AGENT_NAMESPACE


async def test_pipeline_tool_search_disabled_passthrough():
    # 关闭时：tool_search 透传为普通 function_call（不合成 tool_search_output）。
    pipe = ResponsePipeline("resp_off", multi_agent=None, tool_search_enabled=False)
    frames = await _collect(pipe, [
        {"type": "tool_call", "call_id": "t1", "name": TOOL_SEARCH_NAME,
         "arguments": '{"query": "x"}'},
        {"type": "tool_call_done", "call_id": "t1", "arguments": '{"query": "x"}'},
    ])
    items = [f["item"] for f in frames if f.get("type") == "response.output_item.added"]
    assert all(it["type"] != "tool_search_output" for it in items)
    assert all(it["type"] != "function_call_output" for it in items)
    fc = [it for it in items if it["type"] == "function_call" and it.get("name") == TOOL_SEARCH_NAME]
    assert fc != []  # 透传为普通 function_call


async def test_pipeline_multi_agent_executed():
    orch = MultiAgentOrchestrator(runner=_fake_runner, default_model="m1")
    pipe = ResponsePipeline("resp_ma", multi_agent=orch, tool_search_enabled=False)
    args = json.dumps({"instruction": "sub", "model": "", "session_id": "s1"})
    frames = await _collect(pipe, [
        {"type": "tool_call", "call_id": "c1", "name": "spawn_agent",
         "namespace": MULTI_AGENT_NAMESPACE, "arguments": args},
        {"type": "tool_call_done", "call_id": "c1", "arguments": args},
    ])
    items = [f["item"] for f in frames if f.get("type") == "response.output_item.added"]
    # 回显的 function_call（带 namespace）存在。
    fc = next((it for it in items if it["type"] == "function_call"
               and it.get("namespace") == MULTI_AGENT_NAMESPACE), None)
    assert fc is not None
    # 编排器回传的 function_call_output 存在（spawn 返回 running，结果由后续
    # wait_agent 取回，这是 V1 协议的正确行为）。
    fco = next((it for it in items if it["type"] == "function_call_output"), None)
    assert fco is not None
    assert "agent_id" in json.loads(fco["output"])


async def test_pipeline_multi_agent_namespaced_flat_name():
    # 摊平风格名字 mcp__multi_agent_v1__-spawn_agent 也能被识别。
    orch = MultiAgentOrchestrator(runner=_fake_runner)
    pipe = ResponsePipeline("resp_flat", multi_agent=orch, tool_search_enabled=False)
    args = json.dumps({"instruction": "sub", "session_id": "s1"})
    frames = await _collect(pipe, [
        {"type": "tool_call", "call_id": "c1", "name": "mcp__multi_agent_v1__-spawn_agent",
         "arguments": args},
        {"type": "tool_call_done", "call_id": "c1", "arguments": args},
    ])
    items = [f["item"] for f in frames if f.get("type") == "response.output_item.added"]
    fco = next((it for it in items if it["type"] == "function_call_output"), None)
    assert fco is not None


async def test_pipeline_multi_agent_upstream_flattened_name():
    # 上游自行摊平的形态 multi_agent_v1-spawn_agent 也要被识别并执行
    # （2026-08-15 流式探针 P2 实证：部分上游把 namespace 摊平成 {ns}-{subtool} 命名）。
    orch = MultiAgentOrchestrator(runner=_fake_runner)
    pipe = ResponsePipeline("resp_upflat", multi_agent=orch, tool_search_enabled=False)
    args = json.dumps({"instruction": "sub", "session_id": "s1"})
    frames = await _collect(pipe, [
        {"type": "tool_call", "call_id": "c1", "name": "multi_agent_v1-spawn_agent",
         "arguments": args},
        {"type": "tool_call_done", "call_id": "c1", "arguments": args},
    ])
    items = [f["item"] for f in frames if f.get("type") == "response.output_item.added"]
    fc = next((it for it in items if it["type"] == "function_call"), None)
    assert fc is not None
    # 回显名必须是纯子工具名 + namespace（客户端期望的 V1 形态）。
    assert fc["name"] == "spawn_agent"
    assert fc.get("namespace") == MULTI_AGENT_NAMESPACE
    fco = next((it for it in items if it["type"] == "function_call_output"), None)
    assert fco is not None


def test_pipeline_output_items_include_synthesized():
    # 验证 output_items() 合并 _synthesized_items（retrieve 一致性）。
    import asyncio

    async def _run():
        orch = MultiAgentOrchestrator(runner=_fake_runner)
        pipe = ResponsePipeline("resp_out", multi_agent=orch, tool_search_enabled=True)
        args = json.dumps({"instruction": "sub", "session_id": "s1"})
        for chunk in [
            {"type": "tool_call", "call_id": "t1", "name": TOOL_SEARCH_NAME,
             "arguments": "{}"},
            {"type": "tool_call_done", "call_id": "t1", "arguments": "{}"},
            {"type": "tool_call", "call_id": "c1", "name": "spawn_agent",
             "namespace": MULTI_AGENT_NAMESPACE, "arguments": args},
            {"type": "tool_call_done", "call_id": "c1", "arguments": args},
        ]:
            list(await pipe._translate_chunk(chunk))
        return pipe.output_items()

    items = asyncio.run(_run())
    types = [it["type"] for it in items]
    assert "tool_search_output" not in types  # 26.803 不认顶级形态（C.7）
    assert "tool_search_call" in types        # FR-2.1：tool_search 改写形态
    assert "function_call" in types           # spawn 回显
    assert "function_call_output" in types    # spawn 的 fco


# ---------------------------------------------------------------------------
# 4. 能力路由 / 模型声明（AC-1 / FR-5）
# ---------------------------------------------------------------------------


def _make_cfg(tool_search_enabled: bool = False, ma_enabled: bool = False):
    from types import SimpleNamespace

    hosted = SimpleNamespace(tool_search_enabled=tool_search_enabled, mcp_enabled=False)
    multi_agent = SimpleNamespace(
        enabled=ma_enabled,
        max_threads=4,
        job_max_runtime_seconds=1800,
        minimal_client_version="0.144.0",
    )
    return SimpleNamespace(hosted_tools=hosted, multi_agent=multi_agent)


def test_emulated_caps_includes_tool_search_when_enabled():
    from zhongzhuan.proxy.protocol.responses_models import Capability
    from zhongzhuan.responses_v3.hosted_tools import hosted_tool_emulated_capabilities

    # 两个开关必须同时为真（避免半残状态：暴露 namespace 却无法执行）。
    caps = hosted_tool_emulated_capabilities(
        _make_cfg(tool_search_enabled=True, ma_enabled=True)
    )
    assert Capability.TOOL_SEARCH in caps


def test_emulated_caps_excludes_tool_search_when_only_one_flag():
    from zhongzhuan.proxy.protocol.responses_models import Capability
    from zhongzhuan.responses_v3.hosted_tools import hosted_tool_emulated_capabilities

    # 只开其一不应声称可服务 tool_search。
    assert Capability.TOOL_SEARCH not in hosted_tool_emulated_capabilities(
        _make_cfg(tool_search_enabled=True, ma_enabled=False)
    )
    assert Capability.TOOL_SEARCH not in hosted_tool_emulated_capabilities(
        _make_cfg(tool_search_enabled=False, ma_enabled=True)
    )


def test_codex_model_info_declares_multi_agent_when_enabled(monkeypatch):
    import zhongzhuan.config as pconfig

    ma = type("MA", (), {"enabled": True, "max_threads": 4,
                         "job_max_runtime_seconds": 1800,
                         "minimal_client_version": "0.144.0"})()
    ht = type("HT", (), {"tool_search_enabled": True, "mcp_enabled": False})()

    class FakeCfg:
        multi_agent = ma
        hosted_tools = ht

    monkeypatch.setattr(pconfig, "default_config", lambda: FakeCfg())

    from zhongzhuan.proxy.server import ProxyServer

    info = ProxyServer._build_codex_model_info("oc-test")
    assert info.get("multi_agent_version") == "v1"
    assert info.get("supports_search_tool") is True
    assert info.get("minimal_client_version") == "0.144.0"


def test_codex_model_info_no_multi_agent_when_disabled(monkeypatch):
    import zhongzhuan.config as pconfig

    ma = type("MA", (), {"enabled": False})()
    ht = type("HT", (), {"tool_search_enabled": False, "mcp_enabled": False})()

    class FakeCfg:
        multi_agent = ma
        hosted_tools = ht

    monkeypatch.setattr(pconfig, "default_config", lambda: FakeCfg())

    from zhongzhuan.proxy.server import ProxyServer

    info = ProxyServer._build_codex_model_info("oc-test")
    assert "multi_agent_version" not in info


# ---------------------------------------------------------------------------
# 运行时配置注入（2026-08-15 修复：default_config 原先只返回全新默认值，导致
# 生产环境 ma_active 恒 False、响应侧合成全死——T2b 实证）
# ---------------------------------------------------------------------------


def test_set_current_config_injects_runtime_config(monkeypatch):
    from zhongzhuan.config import default_config
    from zhongzhuan.config.config import Config, set_current_config

    try:
        set_current_config(None)
        # 未注入：返回全新默认值（多代理关闭）。
        assert default_config().multi_agent.enabled is False
        # 注入：default_config 返回同一实例，字段真实生效。
        cfg = Config()
        cfg.multi_agent.enabled = True
        set_current_config(cfg)
        assert default_config() is cfg
        assert default_config().multi_agent.enabled is True
    finally:
        set_current_config(None)


# ---------------------------------------------------------------------------
# capability router emulated 接线（2026-08-15 修复：_capability_router 原先不传
# emulated，默认集不含 TOOL_SEARCH——T2a 线上仍 400）
# ---------------------------------------------------------------------------


def test_capability_router_wires_emulated_tool_search(monkeypatch):
    import zhongzhuan.config as pconfig
    from zhongzhuan.proxy.handler import ProxyHandler
    from zhongzhuan.proxy.protocol.responses_models import Capability

    ma = type("MA", (), {"enabled": True, "max_threads": 4,
                         "job_max_runtime_seconds": 1800,
                         "minimal_client_version": "0.144.0"})()
    ht = type("HT", (), {"tool_search_enabled": True, "mcp_enabled": False})()

    class FakeCfg:
        multi_agent = ma
        hosted_tools = ht

    monkeypatch.setattr(pconfig, "default_config", lambda: FakeCfg())

    handler = ProxyHandler.__new__(ProxyHandler)
    router = handler._capability_router([])
    assert Capability.TOOL_SEARCH in router.emulated_capabilities


def test_capability_router_default_emulated_without_flag(monkeypatch):
    import zhongzhuan.config as pconfig
    from zhongzhuan.proxy.handler import ProxyHandler
    from zhongzhuan.proxy.protocol.responses_models import Capability

    ma = type("MA", (), {"enabled": False})()
    ht = type("HT", (), {"tool_search_enabled": False, "mcp_enabled": False})()

    class FakeCfg:
        multi_agent = ma
        hosted_tools = ht

    monkeypatch.setattr(pconfig, "default_config", lambda: FakeCfg())

    handler = ProxyHandler.__new__(ProxyHandler)
    router = handler._capability_router([])
    assert Capability.TOOL_SEARCH not in router.emulated_capabilities


# ---------------------------------------------------------------------------
# 请求侧归一化（2026-08-15 修复：hosted tool_search 与 multi_agent_v1 namespace
# 摊平成上游模型可调用的普通 function——T3 实证原样下发时模型看不到 spawn_agent）
# ---------------------------------------------------------------------------


def test_ma_flatten_tools_hosted_tool_search_and_namespace():
    from zhongzhuan.proxy.handler import _ma_flatten_tools

    tools = [
        {"type": "tool_search"},
        {
            "type": "namespace",
            "name": "multi_agent_v1",
            "tools": [
                {"type": "function", "name": "spawn_agent", "description": "Spawn",
                 "parameters": {"type": "object", "properties": {}}},
                {"type": "function", "name": "wait_agent", "description": "Wait"},
            ],
        },
        {"type": "function", "name": "plain_fn", "description": "keep"},
    ]
    out = _ma_flatten_tools(tools)
    assert out is not None
    assert [t["name"] for t in out] == ["tool_search", "spawn_agent", "wait_agent", "plain_fn"]
    assert all(t["type"] == "function" for t in out)
    # hosted tool_search 变成可调用的 function 形态（FR-1 不依赖上游原生支持）。
    assert out[0]["name"] == "tool_search"
    assert "parameters" in out[0]


def test_ma_flatten_tools_noop_on_plain_request():
    from zhongzhuan.proxy.handler import _ma_flatten_tools

    tools = [{"type": "function", "name": "web_search_probe", "description": "x"}]
    assert _ma_flatten_tools(tools) is None  # 无关请求零改动


def test_ma_normalize_upstream_body_including_additional_tools():
    from zhongzhuan.proxy.handler import _ma_normalize_upstream_body

    body = {
        "tools": [{"type": "tool_search"}],
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "multi_agent_v1",
                        "tools": [{"type": "function", "name": "close_agent"}],
                    }
                ],
            }
        ],
    }
    _ma_normalize_upstream_body(body)
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["name"] == "tool_search"
    assert body["input"][0]["tools"][0]["type"] == "function"
    assert body["input"][0]["tools"][0]["name"] == "close_agent"
    # additional_tools 的子工具必须合并进顶层 tools（juhe 不认 additional_tools，
    # 但认顶层 tools 里的普通 function——T3 实证）。
    top_names = [t["name"] for t in body["tools"] if isinstance(t, dict)]
    assert "close_agent" in top_names
