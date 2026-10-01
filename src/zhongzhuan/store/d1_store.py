"""Cloudflare D1 store: SQLite dialect over the D1 REST API.

为什么是 D1（2026-10-01 选型）
==============================
Aiven 免费层按 *闲置关停*（2026-09-30 事故：约 20h 微负载仍被平台断 DNS），
TiDB Serverless 按 *在线税*（有连接就烧 ≈20 RU/s）。D1 两者皆无：

* **无连接概念**：REST ``/raw`` 端点，每次语句一个 HTTPS 请求，无池可管、
  无空闲连接被掐、无关停机制（scale-to-zero 但 API 随叫随到）；
* **SQLite 方言**：``dialect="sqlite"``，复用现有 sqlite 分支的全部 SQL 与
  迁移脚本（``ON CONFLICT DO UPDATE`` / ``INSERT OR REPLACE`` / ``PRAGMA``
  实测均支持）；
* 免费额度：500 万行读 + 10 万行写/天（本项目 ~13k 读 + ~3k 写/天 ≈ 0.3%）。

协议行为（2026-10-01 对真库实测，scripts/_d1_probe*.py）
=========================================================
* ``POST /raw`` 返回 ``{columns: [...], rows: [[...]]}`` —— **位置数组**，
  重名 JOIN 列不塌缩（``/query`` 的 dict 行会塌缩，故读路径一律走 /raw）；
* **BLOB 双向**：写——bytes 参数无法过 JSON，内联为 SQLite ``X'hex'`` 字面量；
  读——REST 把 BLOB 序列化成 ``[222, 173, ...]`` 字节数组，按值还原为 bytes；
* **事务被禁**：``BEGIN`` / ``COMMIT`` 返回错误 7500（要求走 Durable Objects
  事务 API）。REST 每条语句独立 autocommit，``transaction()`` 沿用基类
  no-op 实现，迁移引擎的 begin/commit 同样为 no-op（迁移的原子性由
  「全量幂等 DDL + 失败即退出」兜底，仅存在于全新空库首建场景）;
* 单条 sql 字符串里的**多语句**会被依序执行（结果列表按语句展开）——
  本实现恒发单语句，结果列表长度非 1 视为协议破坏。

延迟模型：每条语句 = 1 个 HTTPS 请求（美西 VPS → WNAM ≈ 10-40ms RTT）。
比连接池贵，换来的是零在线税 + 零关停风险。
"""

from __future__ import annotations

import asyncio
import os
import time

import aiohttp

from .store import Store
from .migration_engine import MigrationExecutor, _SQLITE_IGNORABLE, run_migrations_or_exit
from .migrations import MIGRATIONS

DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"

#: 请求级默认超时（秒）。D1 正常 <100ms，超时大概率是网络/平台故障。
DEFAULT_TIMEOUT_SECONDS: float = 15.0

#: 可安全重试一次的 HTTP 状态码：限流 + 平台侧瞬断。4xx（除 429）是请求
#: 本身的错，重试只会再失败一次。
_RETRYABLE_HTTP: frozenset[int] = frozenset({429, 500, 502, 503, 504, 522, 523, 524, 527})

#: 重试前的退避（秒）。只重试一次，短退避足以跳过瞬断窗口。
_RETRY_BACKOFF_SECONDS: float = 0.5


class D1Error(RuntimeError):
    """D1 API returned an error payload (HTTP-level or embedded ``success:false``)."""

    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self.payload = payload
        errors = payload.get("errors") if isinstance(payload, dict) else None
        detail = "; ".join(
            f"{e.get('code', '?')}: {e.get('message', '')}" for e in (errors or [])
        ) or str(payload)[:300]
        super().__init__(f"D1 API error (HTTP {status_code}): {detail}")


class D1Store(Store):
    """Async Cloudflare D1 store (stateless HTTP, no connection pool)."""

    dialect = "sqlite"

    def __init__(
        self,
        *,
        account_id: str,
        database_id: str,
        api_token: str,
        api_base: str = DEFAULT_API_BASE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._endpoint = f"{api_base.rstrip('/')}/accounts/{account_id}/d1/database/{database_id}/raw"
        self._api_token = api_token
        self._timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        self._session: aiohttp.ClientSession | None = None
        self._closed = False
        self._migrated = False
        #: 纯内存健康快照字段（/healthz 零 SQL 消费）。
        self._last_query_epoch: int | None = None
        self._consecutive_errors: int = 0
        self._total_queries: int = 0

    # ------------------------------------------------------------------
    # 构建入口
    # ------------------------------------------------------------------
    @classmethod
    async def create(
        cls,
        *,
        account_id: str,
        database_id: str,
        api_token: str,
        api_base: str = DEFAULT_API_BASE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> D1Store:
        """构建并立即跑 migration（失败抛异常，由调用方决定降级或退出）。"""
        store = cls(
            account_id=account_id,
            database_id=database_id,
            api_token=api_token,
            api_base=api_base,
            timeout_seconds=timeout_seconds,
        )
        await store.ensure_ready()
        return store

    async def ensure_ready(self) -> None:
        """一生一次跑 migration（HTTP 无懒建池问题，只防重复执行）。"""
        if self._migrated:
            return
        await run_migrations_or_exit(D1MigrationExecutor(self), MIGRATIONS)
        self._migrated = True

    # ------------------------------------------------------------------
    # HTTP 层
    # ------------------------------------------------------------------
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Authorization 头按请求注入（见 _raw），会话本身不携带凭据。
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _raw(self, sql: str, params: tuple | list | None):
        """执行单语句并返回该语句的 result dict（columns/rows/meta）。

        重试语义与 TiDBStore 一致：单语句 autocommit，连接级/平台级瞬断
        （网络错误、429/5xx）**重试一次**是安全的；其余错误原样抛出。
        """
        sql, params = self._inline_blob_params(sql, params)
        body: dict = {"sql": sql}
        if params:
            body["params"] = list(params)

        last_exc: BaseException | None = None
        for attempt in (1, 2):
            try:
                session = await self._ensure_session()
                async with session.post(
                    self._endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_token}"},
                ) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception as exc:  # noqa: BLE001 - 非 JSON 响应体
                        raise D1Error(resp.status, {"errors": [{"message": f"non-JSON response: {exc}"}]}) from exc
                    if resp.status == 200 and isinstance(data, dict) and data.get("success"):
                        results = data.get("result") or []
                        if len(results) != 1:
                            # 单语句契约被破坏（意外多语句/空结果）。
                            raise D1Error(
                                200,
                                {"errors": [{"message": f"expected exactly 1 statement result, got {len(results)}"}]},
                            )
                        self._on_success()
                        return results[0]
                    raise D1Error(resp.status, data)
            except D1Error as exc:
                last_exc = exc
                retryable = exc.status_code in _RETRYABLE_HTTP
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:
                last_exc = exc
                retryable = True
            if not retryable or attempt == 2:
                break
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)

        self._consecutive_errors += 1
        assert last_exc is not None
        raise last_exc

    def _on_success(self) -> None:
        self._consecutive_errors = 0
        self._last_query_epoch = int(time.time())
        self._total_queries += 1

    @staticmethod
    def _inline_blob_params(sql: str, params: tuple | list | None) -> tuple[str, list]:
        """bytes 参数无法过 JSON：内联为 SQLite ``X'hex'`` BLOB 字面量。

        其余参数保持 ``?`` 占位符走服务端绑定（注入面最小化）。前提：本
        代码库的 SQL 全部是代码内常量，``?`` 只作占位符出现，不会出现在
        字符串字面量里（ TiDBStore 的 ``sql.replace("?", "%s")`` 同一假设）。
        """
        if not params:
            return sql, []
        params = list(params)
        if sql.count("?") != len(params):
            # 占位符/参数不匹配：原样发出去，让服务端给出规范错误。
            return sql, params
        parts = sql.split("?")
        buf = [parts[0]]
        out_params: list = []
        for i, value in enumerate(params):
            if isinstance(value, (bytes, bytearray, memoryview)):
                buf.append("X'" + bytes(value).hex() + "'")
            else:
                buf.append("?")
                out_params.append(value)
            buf.append(parts[i + 1])
        return "".join(buf), out_params

    @staticmethod
    def _decode_value(value):
        """REST 把 BLOB 序列化成 ``[222, 173, ...]`` 字节数组——还原 bytes。

        只对「非空 + 全元素均为 0-255 整数」的 list 生效；本项目 SQL 不使用
        JSON1 聚合（不会返回数组列），空 list 保留原样（密文永不为空）。
        """
        if (
            isinstance(value, list)
            and value
            and all(isinstance(x, int) and 0 <= x <= 255 for x in value)
        ):
            return bytes(value)
        return value

    # ------------------------------------------------------------------
    # Store 接口
    # ------------------------------------------------------------------
    async def execute(self, sql: str, params: tuple | None = None) -> int:
        """执行写语句，返回 lastrowid。"""
        result = await self._raw(sql, params or ())
        meta = result.get("meta") or {}
        return int(meta.get("last_row_id") or 0)

    async def execute_rowcount(self, sql: str, params: tuple | None = None) -> int:
        """同 :meth:`execute`，返回受影响行数（meta.changes，钳非负）。"""
        result = await self._raw(sql, params or ())
        meta = result.get("meta") or {}
        affected = int(meta.get("changes") or 0)
        return affected if affected > 0 else 0

    async def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None:
        result = await self._raw(sql, params or ())
        rows = (result.get("results") or {}).get("rows") or []
        if not rows:
            return None
        return tuple(self._decode_value(v) for v in rows[0])

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        result = await self._raw(sql, params or ())
        rows = (result.get("results") or {}).get("rows") or []
        return [tuple(self._decode_value(v) for v in row) for row in rows]

    async def close(self) -> None:
        self._closed = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def status(self) -> dict:
        """/healthz 纯内存快照（零 SQL）。

        注意**不设** ``idle_seconds``：HTTP 后端没有常驻池，空闲 ≠ 故障，
        healthz 的常驻池陈旧检测（``idle_release_seconds == 0`` 分支）对本
        后端天然跳过；连续错误数由 ``consecutive_db_errors`` 表达。
        """
        return {
            "backend": "d1",
            "mode": "http-rest",
            "idle_release_seconds": None,
            "migrated": self._migrated,
            "last_query_epoch": self._last_query_epoch,
            "consecutive_db_errors": self._consecutive_errors,
            "total_queries": self._total_queries,
        }


class D1MigrationExecutor(MigrationExecutor):
    """:class:`MigrationRunner` 的 D1 执行器（sqlite 方言语句直发 REST）。

    事务为 no-op：D1 REST 禁止 ``BEGIN``/``COMMIT``（错误 7500）。代价是
    迁移不具备原子性——仅存在于全新空库首建场景（全幂等 DDL、失败即
    ``sys.exit``），风险可接受；存量库迁移继续走 SQLite/TiDB 本地通道。
    """

    dialect = "sqlite"

    def __init__(self, store: D1Store) -> None:
        self._store = store

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self._store._raw(sql, params)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        return await self._store.fetchall(sql, params)

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        return await self._store.fetchone(sql, params)

    async def begin(self) -> None:  # pragma: no cover - no-op by design
        pass

    async def commit(self) -> None:  # pragma: no cover - no-op by design
        pass

    async def rollback(self) -> None:  # pragma: no cover - no-op by design
        pass

    async def table_exists(self, table: str) -> bool:
        row = await self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        return row is not None

    def is_ignorable(self, exc: BaseException) -> bool:
        message = str(exc)
        return any(p.search(message) for p in _SQLITE_IGNORABLE)


async def create_store_from_env() -> D1Store:
    """从环境变量构建 D1Store（供 :func:`.store.create_store` 调用）。"""
    from loguru import logger

    account_id = os.getenv("ZHONGZHUAN_D1_ACCOUNT_ID", "")
    database_id = os.getenv("ZHONGZHUAN_D1_DATABASE_ID", "")
    api_token = os.getenv("ZHONGZHUAN_D1_API_TOKEN", "")
    if not (account_id and database_id and api_token):
        raise RuntimeError(
            "D1 backend requested but ZHONGZHUAN_D1_ACCOUNT_ID / "
            "ZHONGZHUAN_D1_DATABASE_ID / ZHONGZHUAN_D1_API_TOKEN are not all set"
        )
    timeout_raw = os.getenv("ZHONGZHUAN_D1_TIMEOUT_SECONDS", "")
    timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
    api_base = os.getenv("ZHONGZHUAN_D1_API_BASE", DEFAULT_API_BASE)
    store = D1Store(
        account_id=account_id,
        database_id=database_id,
        api_token=api_token,
        api_base=api_base,
        timeout_seconds=timeout,
    )
    try:
        await store.ensure_ready()
        logger.info(f"使用 Cloudflare D1 存储 (database={database_id[:8]}…, timeout={timeout:g}s)")
    except Exception as exc:  # noqa: BLE001 - 启动降级：不杀进程，按需重试
        logger.warning(
            f"D1 不可达，进入降级模式（{type(exc).__name__}: {exc}）；"
            "进程继续启动，DB 查询将在下次执行时重试"
        )
    return store
