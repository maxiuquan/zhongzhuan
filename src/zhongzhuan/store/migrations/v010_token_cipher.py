"""v010 -- access_tokens 可复制完整 Key 的加密密文列。

背景（T04 安全迁移的副作用）
--------------------------
v003 把 access_tokens 的明文 token 列清空，只保留不可逆 HMAC 哈希，
"复制完整 Key" 从此无法从数据库恢复（f1b3434 时代可以直接读明文，
之后的所有令牌都无法再复制）。用户要求：列表继续脱敏，但点击复制
必须能拿到完整 Key。

方案
----
新增 ``token_cipher`` 列，存 ``crypto.encrypt(plaintext)`` 的 AES-GCM
密文（与 api_keys.key_cipher 同一套机制、同一把 AES key）：

* 新建令牌：同时写 ``token_hash``（认证，不可逆）和 ``token_cipher``
  （复制，可解密）。两者不矛盾：哈希用于运行时校验，密文仅用于
  admin 复制接口。
* 历史令牌（密文为空）：继续有效，但无法恢复完整 Key；复制时后台
  明确返回 404 + 说明，绝不换 Key、不废止。
* 列表接口永不返回密文或明文，只返回掩码。

为什么不加密 token_hash：哈希本身不可逆，无需加密。

Idempotency
-----------
bare ``ADD COLUMN``，engine 的 error-code 白名单已识别
(SQLite ``duplicate column name`` / MySQL errno 1060)，同一组 SQL
同时作为 baseline。
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite: BLOB 直接存 AES 密文（prefix + nonce + tag + data）。
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE access_tokens ADD COLUMN token_cipher BLOB",
)

#: MySQL / TiDB: VARBINARY 512 足够容纳 AES-GCM 密文（~100 bytes）。
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE access_tokens ADD COLUMN token_cipher VARBINARY(512)",
)


MIGRATION = Migration(
    version=10,
    name="token_cipher",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="access_tokens",
)
