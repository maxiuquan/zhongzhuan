"""D1Store 单元测试（HTTP 层全 mock，不触真库）。

覆盖：
* bytes 参数内联为 X'hex' 字面量（_inline_blob_params）
* BLOB 读取还原（REST 字节数组 → bytes）
* execute/execute_rowcount/fetchone/fetchall 对 /raw 响应的映射
* 重试语义：429/5xx/网络错误重试一次成功；4xx 立即抛
* 多语句契约破坏检测
* status() 无 idle_seconds（healthz 常驻池陈旧检测天然跳过）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from zhongzhuan.store.d1_store import D1Error, D1Store


# --------------------------------------------------------------------------- #
# Fake aiohttp session
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """按脚本顺序回放响应；记录每个请求的 body。"""

    def __init__(self, script: list[Any]) -> None:
        # script 元素：dict（成功响应）或 int（HTTP 状态码）或 Exception 实例
        self._script = list(script)
        self.bodies: list[dict] = []
        self.closed = False

    def post(self, url, json=None, headers=None):
        self.bodies.append({"url": url, "json": json, "headers": headers})
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            payload = {"success": False, "errors": [{"code": 1000, "message": "err"}]}
            return _FakeResponse(item, payload)
        return _FakeResponse(200, item)

    async def close(self):
        self.closed = True


def _ok_raw(results_rows=None, *, last_row_id=0, changes=0) -> dict:
    return {
        "result": [
            {
                "results": {"columns": [], "rows": results_rows or []}
                if results_rows is not None
                else {"columns": [], "rows": []},
                "success": True,
                "meta": {
                    "last_row_id": last_row_id,
                    "changes": changes,
                    "duration": 0.1,
                },
            }
        ],
        "success": True,
        "errors": [],
    }


def _store_with(script: list[Any]) -> tuple[D1Store, _FakeSession]:
    store = D1Store(
        account_id="acct",
        database_id="dbid",
        api_token="tok",
        api_base="https://api.example.com/client/v4",
    )
    fake = _FakeSession(script)
    store._session = fake  # type: ignore[assignment]
    return store, fake


# --------------------------------------------------------------------------- #
# 参数内联 / BLOB 还原
# --------------------------------------------------------------------------- #
def test_inline_blob_params_replaces_bytes_with_hex_literal():
    sql, params = D1Store._inline_blob_params(
        "INSERT INTO t(a, b, c) VALUES(?, ?, ?)", (b"\x00\xff", "txt", 7)
    )
    assert sql == "INSERT INTO t(a, b, c) VALUES(X'00ff', ?, ?)"
    assert params == ["txt", 7]


def test_inline_blob_params_passthrough_without_params():
    assert D1Store._inline_blob_params("SELECT 1", None) == ("SELECT 1", [])
    assert D1Store._inline_blob_params("SELECT ?", (1,)) == ("SELECT ?", [1])


def test_inline_blob_params_mismatch_sent_as_is():
    # 占位符/参数数不匹配 → 原样发出，由服务端给规范错误
    sql, params = D1Store._inline_blob_params("SELECT ?, ?", (1,))
    assert sql == "SELECT ?, ?" and params == [1]


def test_decode_value_restores_blob_array():
    assert D1Store._decode_value([222, 173, 190, 239]) == b"\xde\xad\xbe\xef"
    assert D1Store._decode_value("str") == "str"
    assert D1Store._decode_value(5) == 5
    assert D1Store._decode_value(None) is None
    # 空 list 保留（密文永不为空；空 BLOB 语义歧义留给调用方）
    assert D1Store._decode_value([]) == []


# --------------------------------------------------------------------------- #
# Store 接口映射
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_execute_returns_lastrowid():
    store, fake = _store_with([_ok_raw(last_row_id=42)])
    rid = await store.execute("INSERT INTO t VALUES(?)", (1,))
    assert rid == 42
    assert fake.bodies[0]["json"]["sql"] == "INSERT INTO t VALUES(?)"
    assert fake.bodies[0]["json"]["params"] == [1]


@pytest.mark.asyncio
async def test_execute_rowcount_clamps_negative():
    store, _ = _store_with([_ok_raw(changes=3)])
    assert await store.execute_rowcount("UPDATE t SET a=1") == 3
    store, _ = _store_with([_ok_raw(changes=0)])
    assert await store.execute_rowcount("UPDATE t SET a=1") == 0


@pytest.mark.asyncio
async def test_fetchone_fetchall_positional_with_blob_decode():
    payload = _ok_raw()
    payload["result"][0]["results"] = {
        "columns": ["id", "key_cipher", "label"],
        "rows": [[1, [65, 69, 83, 58], "lbl"], [2, None, "l2"]],
    }
    store, _ = _store_with([payload, payload])
    row = await store.fetchone("SELECT id, key_cipher, label FROM t")
    assert row == (1, b"AES:", "lbl")
    rows = await store.fetchall("SELECT id, key_cipher, label FROM t")
    assert rows == [(1, b"AES:", "lbl"), (2, None, "l2")]


@pytest.mark.asyncio
async def test_fetchone_empty_returns_none():
    store, _ = _store_with([_ok_raw(results_rows=[])])
    assert await store.fetchone("SELECT 1") is None


# --------------------------------------------------------------------------- #
# 重试与错误
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_retry_on_500_then_success():
    store, fake = _store_with([500, _ok_raw(last_row_id=7)])
    assert await store.execute("INSERT INTO t VALUES(1)") == 7
    assert len(fake.bodies) == 2  # 重试了一次


@pytest.mark.asyncio
async def test_retry_on_network_error_then_success():
    store, fake = _store_with([asyncio.TimeoutError(), _ok_raw(last_row_id=1)])
    assert await store.execute("INSERT INTO t VALUES(1)") == 1
    assert len(fake.bodies) == 2


@pytest.mark.asyncio
async def test_no_retry_on_client_error_4xx():
    store, fake = _store_with([400])
    with pytest.raises(D1Error):
        await store.execute("INSERT INTO t VALUES(1)")
    assert len(fake.bodies) == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_error():
    store, fake = _store_with([500, 502])
    with pytest.raises(D1Error):
        await store.execute("INSERT INTO t VALUES(1)")
    assert len(fake.bodies) == 2


@pytest.mark.asyncio
async def test_multi_statement_contract_violation():
    bad = {"result": [{"meta": {}}, {"meta": {}}], "success": True, "errors": []}
    store, _ = _store_with([bad])
    with pytest.raises(D1Error):
        await store.execute("SELECT 1; SELECT 2")


@pytest.mark.asyncio
async def test_success_false_payload_raises():
    payload = {"result": [], "success": False, "errors": [{"code": 7500, "message": "boom"}]}
    store, _ = _store_with([payload])
    with pytest.raises(D1Error) as excinfo:
        await store.execute("SELECT 1")
    assert "7500" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# status() / 健康快照
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_status_tracks_consecutive_errors_and_reset():
    store, fake = _store_with([500, 500])
    with pytest.raises(D1Error):
        await store.execute("SELECT 1")
    status = store.status()
    # 计数的是**失败的语句数**（一次调用 = 重试耗尽后仍失败 = 1），不是 HTTP 尝试数
    assert status["consecutive_db_errors"] == 1
    assert len(fake.bodies) == 2  # 重试确实发生了
    # D1 后端不得上报 idle_seconds：healthz 常驻池陈旧检测必须天然跳过
    assert "idle_seconds" not in status
    assert status["backend"] == "d1"

    store2, _ = _store_with([_ok_raw()])
    await store2.execute("SELECT 1")
    st2 = store2.status()
    assert st2["consecutive_db_errors"] == 0
    assert st2["total_queries"] == 1


@pytest.mark.asyncio
async def test_close_closes_session():
    store, fake = _store_with([])
    await store.close()
    assert fake.closed


# --------------------------------------------------------------------------- #
# 迁移执行器
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_migration_executor_table_exists_and_ignorable():
    from zhongzhuan.store.d1_store import D1MigrationExecutor

    payload = _ok_raw()
    payload["result"][0]["results"] = {"columns": ["name"], "rows": [["models"]]}
    store, fake = _store_with([payload])
    ex = D1MigrationExecutor(store)
    assert await ex.table_exists("models") is True
    assert fake.bodies[0]["json"]["sql"].startswith("SELECT name FROM sqlite_master")

    class _Exc(Exception):
        pass

    assert ex.is_ignorable(_Exc("duplicate column name: foo"))
    assert not ex.is_ignorable(_Exc("no such table: foo"))
    # no-op 事务
    await ex.begin()
    await ex.commit()
    await ex.rollback()
