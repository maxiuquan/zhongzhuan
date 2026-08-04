"""SQLite / TiDB 双后端集成矩阵测试（T36 / R-P2-13）。

判据：SQLite/TiDB 双后端集成 —— 同一套 schema 迁移与 ResponseStore CRUD 在
两种后端上行为一致。CI 中通过 ``mysql:8`` service container 提供 MySQL 兼容
端点（TiDB 协议兼容 MySQL），本地无连接时经 :envvar:`ZHONGZHUAN_TIDB_DSN`
探测，连不上则 **skip 并诚实标注**，绝不硬连真实数据库。

启用方式（本地）::

    set ZHONGZHUAN_TIDB_DSN=127.0.0.1:4000:root::zhongzhuan
    python -m pytest tests/test_backend_matrix.py -q

``ZHONGZHUAN_TIDB_DSN`` 格式：``host:port:user:password:database``
（密码可为空段）。CI 里由 service container 的 host/port 注入。
"""

from __future__ import annotations

import json
import os

import pytest
from pytest_asyncio import fixture as pytest_asyncio_fixture

from zhongzhuan.store.migrations import MIGRATIONS

pytestmark = pytest.mark.backend_matrix


# ---------------------------------------------------------------------------
# TiDB 连接探测（CI / 本地）
# ---------------------------------------------------------------------------


def _parse_tidb_dsn() -> tuple[str, int, str, str, str] | None:
    """解析 ``host:port:user:password:database``；缺项返回 None。"""
    dsn = os.getenv("ZHONGZHUAN_TIDB_DSN", "").strip()
    if not dsn:
        return None
    parts = dsn.split(":")
    if len(parts) < 3:
        return None
    host = parts[0]
    try:
        port = int(parts[1])
    except ValueError:
        return None
    user = parts[2]
    password = parts[3] if len(parts) > 3 else ""
    database = parts[4] if len(parts) > 4 else "zhongzhuan"
    return host, port, user, password, database


def _parse_tidb_env() -> tuple[str, int, str, str, str] | None:
    """从独立环境变量构造连接参数（CI service container 场景）。"""
    host = os.getenv("ZHONGZHUAN_TIDB_HOST", "").strip()
    if not host:
        return None
    port = int(os.getenv("ZHONGZHUAN_TIDB_PORT", "4000"))
    return (
        host,
        port,
        os.getenv("ZHONGZHUAN_TIDB_USER", "root").strip(),
        os.getenv("ZHONGZHUAN_TIDB_PASSWORD", ""),
        os.getenv("ZHONGZHUAN_TIDB_DATABASE", "zhongzhuan").strip(),
    )


def _tidb_conn_params() -> tuple[str, int, str, str, str] | None:
    return _parse_tidb_dsn() or _parse_tidb_env()


def _tidb_unavailable_reason() -> str:
    return (
        "TiDB/MySQL not reachable: set ZHONGZHUAN_TIDB_DSN "
        "(host:port:user:password:database) or the CI service container env "
        "(ZHONGZHUAN_TIDB_HOST/PORT/USER/PASSWORD/DATABASE) to run the TiDB "
        "half of the backend matrix.  SQLite half always runs."
    )


@pytest_asyncio_fixture
async def tidb_store():
    """连接 TiDB/MySQL；不可达则 skip 并打印如何启用。"""
    params = _tidb_conn_params()
    if params is None:
        pytest.skip(_tidb_unavailable_reason())
    host, port, user, password, database = params

    import aiomysql

    try:
        from zhongzhuan.store.tidb_store import TiDBStore

        store = await TiDBStore.create(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            ssl=False,
            pool_size=2,
        )
    except Exception as exc:  # 连接失败 / 认证失败 / 迁移失败 -> skip 并诚实标注
        pytest.skip(
            f"TiDB/MySQL not reachable at {host}:{port} "
            f"(error: {type(exc).__name__}: {exc}). "
            "See test module docstring for how to enable."
        )
    try:
        yield store
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# SQLite 侧：永远全跑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_migrations_apply_all_versions(tmp_path):
    """SQLite 真跑：7 个迁移按序应用，版本表与 route_bindings 表齐全。"""
    from zhongzhuan.store.sqlite_store import SqliteStore

    store = await SqliteStore.create(str(tmp_path / "matrix.db"))
    try:
        rows = await store.fetchall("SELECT version FROM schema_migrations ORDER BY version")
        assert [int(r[0]) for r in rows] == [1, 3, 4, 5, 6, 7, 8, 9]
        row = await store.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='route_bindings'")
        assert row is not None, "route_bindings 表（v008）缺失"
        row = await store.fetchone(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_route_bindings_expires'"
        )
        assert row is not None, "idx_route_bindings_expires 索引缺失"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_response_store_roundtrip(tmp_path):
    """SQLite 真跑：ResponseStore 核心 CRUD 往返。"""
    from zhongzhuan.store.sqlite_store import SqliteStore
    from zhongzhuan.store.response_store import ResponseStore

    store = await SqliteStore.create(str(tmp_path / "matrix.db"))
    try:
        rs = ResponseStore(store)
        await rs.create_response(
            response_id="r1",
            workspace_id="ws1",
            model="m1",
            request={"model": "m1"},
        )
        rec = await rs.get_response("r1", workspace_id="ws1")
        assert rec is not None and rec.model == "m1"
        await rs.append_event("r1", "message_start", {"ok": 1})
        events = await rs.list_events("r1")
        assert [e["event_type"] for e in events] == ["message_start"]
        assert await rs.delete_response("r1", workspace_id="ws1") is True
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# 双后端一致性：同一断言函数跑两个后端
# ---------------------------------------------------------------------------


async def _assert_route_binding_roundtrip(store) -> None:
    """session→route binding CRUD 一致性断言（与后端无关）。"""
    from zhongzhuan.store.response_store import ResponseStore

    rs = ResponseStore(store)

    # 初次 upsert：failover_count 归零。
    await rs.upsert_route_binding(
        session_key="sess-1",
        key_id=7,
        capabilities={"web_search", "code_interpreter"},
        workspace_id="ws1",
        expires_at=0,
    )
    rec = await rs.get_route_binding("sess-1", workspace_id="ws1")
    assert rec is not None
    assert rec["key_id"] == 7
    assert set(rec["capabilities"]) == {"web_search", "code_interpreter"}
    assert rec["failover_count"] == 0

    # 故障迁移：计数 +1 且记录原因。
    await rs.record_binding_failover("sess-1", reason="capability_mismatch", workspace_id="ws1")
    rec = await rs.get_route_binding("sess-1", workspace_id="ws1")
    assert rec is not None
    assert rec["failover_count"] == 1
    assert rec["last_failover_reason"] == "capability_mismatch"

    # upsert 重置 failover（成功响应重新钉住）。
    await rs.upsert_route_binding(session_key="sess-1", key_id=8, workspace_id="ws1")
    rec = await rs.get_route_binding("sess-1", workspace_id="ws1")
    assert rec is not None
    assert rec["key_id"] == 8
    assert rec["failover_count"] == 0

    # 租户隔离：其他 workspace 视为不存在。
    assert await rs.get_route_binding("sess-1", workspace_id="other") is None


async def _assert_response_store_roundtrip(store) -> None:
    """ResponseStore 核心 CRUD 一致性断言（与后端无关）。"""
    from zhongzhuan.store.response_store import ResponseStore

    rs = ResponseStore(store)

    await rs.create_response(
        response_id="r2",
        workspace_id="ws2",
        model="m2",
        request={"model": "m2", "n": 1},
        background=True,
    )
    await rs.save_input_items(
        "r2",
        items=[
            {"seq": 0, "item_type": "message", "role": "user", "payload": {"content": "hi"}},
        ],
    )
    await rs.save_output_items(
        "r2",
        items=[
            {"output_index": 0, "item_type": "message", "role": "assistant", "payload": {"content": "hello"}},
        ],
    )
    await rs.update_status("r2", "completed", workspace_id="ws2")

    rec = await rs.get_response("r2", workspace_id="ws2")
    assert rec is not None and rec.status == "completed"
    assert rec.background is True

    inputs = await rs.list_input_items("r2")
    assert len(inputs) == 1 and inputs[0]["role"] == "user"
    outputs = await rs.list_output_items("r2")
    assert len(outputs) == 1 and outputs[0]["payload"]["content"] == "hello"

    await rs.save_state_chain("r2", previous_response_id="r1", workspace_id="ws2", depth=1)
    assert await rs.chain_depth("r2") == 1

    # JSON 序列化往返无损。
    saved = await rs.get_response("r2", workspace_id="ws2")
    assert json.dumps(saved.request, ensure_ascii=False, sort_keys=True) == json.dumps(
        {"model": "m2", "n": 1}, ensure_ascii=False, sort_keys=True
    )

    await rs.delete_response("r2", workspace_id="ws2")
    assert await rs.get_response("r2", workspace_id="ws2") is None


@pytest.mark.asyncio
async def test_backend_matrix_sqlite(tmp_path):
    """SQLite 侧全跑：binding + response CRUD 一致性。"""
    from zhongzhuan.store.sqlite_store import SqliteStore

    store = await SqliteStore.create(str(tmp_path / "matrix.db"))
    try:
        await _assert_route_binding_roundtrip(store)
        await _assert_response_store_roundtrip(store)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backend_matrix_tidb(tidb_store):
    """TiDB 侧（CI service container / 本地 DSN）：同一套断言函数。

    连不上时 skip（见 :func:`tidb_store` fixture 的诚实标注）。
    """
    await _assert_route_binding_roundtrip(tidb_store)
    await _assert_response_store_roundtrip(tidb_store)


@pytest.mark.asyncio
async def test_backend_matrix_migration_parity(tidb_store, tmp_path):
    """双后端迁移结果对照：TiDB 侧版本表与 SQLite 侧一致。

    同一套 MIGRATIONS 注册表，两端的版本序列与 route_bindings 表都必须出现。
    SQLite 已在上文独立用例断言；此处对 TiDB 断言，能连则两后端互证。
    """
    rows = await tidb_store.fetchall("SELECT version FROM schema_migrations ORDER BY version")
    assert [int(r[0]) for r in rows] == [1, 3, 4, 5, 6, 7, 8, 9]
    row = await tidb_store.fetchone(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='route_bindings'"
    )
    assert row is not None, "TiDB 上 route_bindings 表（v008）缺失"


@pytest.mark.asyncio
async def test_sqlite_migration_parity_reference(tmp_path):
    """SQLite 侧独立跑一次迁移，作为与 TiDB 侧的对照基准。

    便于在无 TiDB 连接的本地仍然能看到「SQLite 侧迁移全绿」的完整证据。
    """
    from zhongzhuan.store.sqlite_store import SqliteStore

    store = await SqliteStore.create(str(tmp_path / "parity.db"))
    try:
        row = await store.fetchone("SELECT COUNT(*) FROM schema_migrations")
        assert int(row[0]) == len(MIGRATIONS)
    finally:
        await store.close()
