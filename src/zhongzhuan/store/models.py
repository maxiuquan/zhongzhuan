"""Model CRUD (async)."""

from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass
class Model:
    name: str
    upstream_base: str
    upstream_model: str
    rpm_limit: int = 0
    tpm_limit: int = 0
    enabled: bool = True
    weight: int = 1
    protocol: str = "openai"  # "openai" | "anthropic"
    anthropic_version: str = "2023-06-01"
    max_tokens_default: int = 4096
    # 上游完整地址覆盖：非空时直接用作请求路径/URL，不自动拼接 /v1/chat/completions 等
    upstream_path_override: str = ""
    # 兜底上游标记：True 表示这是 OpenCode Free 等免 key 兜底模型，调度器按 fallback_penalty 降权
    is_fallback: bool = False
    # 模型别名：逗号分隔的多个别名，客户端用别名请求时路由到此模型
    aliases: str = ""
    # 模型能力声明：逗号分隔的 Capability 名称，如 "code_interpreter,web_search"；
    # "" 表示未声明任何能力（T25 / R-P1-44，v005 新增列）
    capabilities: str = ""
    # 上游执行模式：bonded(未声明) | native | emulate | translate（T25 / R-P1-44，v005 新增列）
    upstream_mode: str = "bonded"
    id: int | None = None
    created_at: int | None = None
    updated_at: int | None = None

    def matches_alias(self, requested: str) -> bool:
        """检查 requested 是否匹配本模型的名称或别名。"""
        if not requested:
            return False
        if self.name == requested:
            return True
        if not self.aliases:
            return False
        alias_list = [a.strip() for a in self.aliases.split(",") if a.strip()]
        return requested in alias_list


# 列顺序：id,name,upstream_base,upstream_model,rpm_limit,tpm_limit,enabled,weight,
#         protocol,anthropic_version,max_tokens_default,upstream_path_override,
#         is_fallback,aliases,capabilities,upstream_mode,created_at,updated_at
_COLS = (
    "id,name,upstream_base,upstream_model,rpm_limit,tpm_limit,enabled,weight,"
    "protocol,anthropic_version,max_tokens_default,upstream_path_override,"
    "is_fallback,aliases,capabilities,upstream_mode,created_at,updated_at"
)


def _row(r: tuple) -> Model:
    return Model(
        id=r[0],
        name=r[1],
        upstream_base=r[2],
        upstream_model=r[3],
        rpm_limit=r[4],
        tpm_limit=r[5],
        enabled=bool(r[6]),
        weight=r[7],
        protocol=r[8] if len(r) > 8 and r[8] else "openai",
        anthropic_version=r[9] if len(r) > 9 and r[9] else "2023-06-01",
        max_tokens_default=r[10] if len(r) > 10 and r[10] else 4096,
        upstream_path_override=r[11] if len(r) > 11 and r[11] else "",
        is_fallback=bool(r[12]) if len(r) > 12 else False,
        aliases=r[13] if len(r) > 13 and r[13] else "",
        capabilities=r[14] if len(r) > 14 and r[14] else "",
        upstream_mode=r[15] if len(r) > 15 and r[15] else "bonded",
        created_at=r[16] if len(r) > 16 else None,
        updated_at=r[17] if len(r) > 17 else None,
    )


async def create_model(s: Store, m: Model) -> Model:
    now = Store.now()
    m.id = await s.execute(
        """INSERT INTO models(name, upstream_base, upstream_model, rpm_limit, tpm_limit, enabled, weight, protocol, anthropic_version, max_tokens_default, upstream_path_override, is_fallback, aliases, capabilities, upstream_mode, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            m.name,
            m.upstream_base,
            m.upstream_model,
            m.rpm_limit,
            m.tpm_limit,
            int(m.enabled),
            m.weight,
            m.protocol,
            m.anthropic_version,
            m.max_tokens_default,
            m.upstream_path_override,
            int(m.is_fallback),
            m.aliases,
            getattr(m, "capabilities", ""),
            getattr(m, "upstream_mode", "bonded"),
            now,
            now,
        ),
    )
    m.created_at = now
    m.updated_at = now
    return m


async def get_model(s: Store, name: str) -> Model | None:
    r = await s.fetchone(
        f"SELECT {_COLS} FROM models WHERE name=?",
        (name,),
    )
    return _row(r) if r else None


async def get_model_by_id(s: Store, model_id: int) -> Model | None:
    r = await s.fetchone(
        f"SELECT {_COLS} FROM models WHERE id=?",
        (model_id,),
    )
    return _row(r) if r else None


async def list_models(s: Store) -> list[Model]:
    rows = await s.fetchall(f"SELECT {_COLS} FROM models ORDER BY id")
    return [_row(r) for r in rows]


async def update_model(s: Store, model_id: int, m: Model) -> None:
    now = Store.now()
    await s.execute(
        """UPDATE models SET name=?, upstream_base=?, upstream_model=?, rpm_limit=?, tpm_limit=?, enabled=?, weight=?, protocol=?, anthropic_version=?, max_tokens_default=?, upstream_path_override=?, is_fallback=?, aliases=?, capabilities=?, upstream_mode=?, updated_at=? WHERE id=?""",
        (
            m.name,
            m.upstream_base,
            m.upstream_model,
            m.rpm_limit,
            m.tpm_limit,
            int(m.enabled),
            m.weight,
            m.protocol,
            m.anthropic_version,
            m.max_tokens_default,
            m.upstream_path_override,
            int(m.is_fallback),
            m.aliases,
            getattr(m, "capabilities", ""),
            getattr(m, "upstream_mode", "bonded"),
            now,
            model_id,
        ),
    )


async def delete_model(s: Store, model_id: int) -> None:
    await s.execute("DELETE FROM models WHERE id=?", (model_id,))
