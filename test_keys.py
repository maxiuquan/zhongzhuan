#!/usr/bin/env python3
"""Batch test API keys from database to check their validity."""
import asyncio
import os
import sys
import time
from dataclasses import dataclass

import httpx


@dataclass
class KeyTestResult:
    key_id: int
    label: str
    upstream_base: str
    upstream_model: str
    model_name: str
    masked_key: str
    is_valid: bool
    status_code: int = 0
    error_message: str = ""
    response_time_ms: float = 0


async def test_single_key(
    client: httpx.AsyncClient,
    key_id: int,
    api_key: str,
    upstream_base: str,
    upstream_model: str,
    model_name: str,
) -> KeyTestResult:
    """Test a single API key by sending a minimal request."""
    result = KeyTestResult(
        key_id=key_id,
        label="",
        upstream_base=upstream_base,
        upstream_model=upstream_model,
        model_name=model_name,
        masked_key=api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***",
        is_valid=False,
    )

    # Clean URL - remove backticks and quotes
    upstream_base = upstream_base.replace("`", "").replace('"', '').strip().rstrip("/")
    if not upstream_base:
        upstream_base = "https://macc.eu.cc"
    if not upstream_base.startswith(("http://", "https://")):
        upstream_base = "https://" + upstream_base
    
    # Ensure the test endpoint doesn't duplicate path
    # If upstream_base already ends with /v1, test endpoint is /chat/completions
    # Otherwise, test endpoint is /v1/chat/completions
    if upstream_base.endswith("/v1"):
        test_endpoint = "/chat/completions"
    else:
        test_endpoint = "/v1/chat/completions"
    
    test_url = f"{upstream_base}{test_endpoint}"
    payload = {
        "model": upstream_model or model_name,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    start = time.time()
    try:
        resp = await client.post(test_url, json=payload, headers=headers, timeout=10.0)
        elapsed = (time.time() - start) * 1000
        result.response_time_ms = elapsed
        result.status_code = resp.status_code

        if resp.status_code == 200:
            result.is_valid = True
            result.label = "VALID"
        elif resp.status_code == 401 or resp.status_code == 403:
            result.error_message = "Unauthorized (key invalid or expired)"
        elif resp.status_code == 429:
            result.error_message = "Rate limited"
        else:
            try:
                error_data = resp.json()
                msg = error_data.get("error", {}).get("message", "")
                result.error_message = msg or resp.text[:200]
            except Exception:
                result.error_message = resp.text[:200]

        # Check for common error patterns
        if not result.error_message and resp.status_code >= 400:
            result.error_message = f"HTTP {resp.status_code}"

    except httpx.TimeoutException:
        elapsed = (time.time() - start) * 1000
        result.response_time_ms = elapsed
        result.error_message = "Timeout (10s)"
    except httpx.ConnectError as e:
        elapsed = (time.time() - start) * 1000
        result.response_time_ms = elapsed
        result.error_message = f"Connection error: {e}"
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        result.response_time_ms = elapsed
        result.error_message = f"{type(e).__name__}: {e}"

    return result


async def test_tidb_keys(tidb_config: dict, concurrent: int = 5) -> None:
    """Test all API keys from TiDB database."""
    import aiomysql
    from zhongzhuan.crypto import init as crypto_init, decrypt
    from pathlib import Path

    print(f"\nConnecting to TiDB: {tidb_config['host']}:{tidb_config['port']}...")

    ssl_ctx = None
    if tidb_config.get("ssl"):
        import ssl as _ssl
        ssl_ctx = _ssl.create_default_context()

    pool = await aiomysql.create_pool(
        host=tidb_config["host"],
        port=tidb_config["port"],
        user=tidb_config["user"],
        password=tidb_config["password"],
        db=tidb_config["database"],
        autocommit=True,
        minsize=1,
        maxsize=3,
        connect_timeout=10,
        ssl=ssl_ctx,
        charset="utf8mb4",
    )

    print("Connected to TiDB\n")

    # Initialize crypto with TiDB store
    async def _get_config(key_name: str) -> str | None:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT value FROM system_config WHERE `key`=%s", (key_name,))
                row = await cur.fetchone()
                return row[0] if row else None
    
    # Debug: check if secret_key exists in TiDB
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM system_config WHERE `key`=%s", ("secret_key",))
            row = await cur.fetchone()
            if row:
                print(f"secret_key from TiDB (hex): {row[0][:64]}...")
            else:
                print("WARNING: secret_key NOT FOUND in TiDB system_config!")
    
    await crypto_init(Path("."), store_get_key=_get_config)
    print("Crypto initialized\n")
    
    # Test decryption with first key
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT key_cipher FROM api_keys WHERE id=1")
            test_row = await cur.fetchone()
            if test_row:
                try:
                    from zhongzhuan.crypto import decrypt as _decrypt
                    test_plain = _decrypt(test_row[0]).decode("utf-8")
                    print(f"Decryption test SUCCESS: key starts with {test_plain[:10]}...")
                except Exception as e:
                    import traceback
                    print(f"Decryption test FAILED: {e}")
                    traceback.print_exc()

    # Get all enabled keys with their model info
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT ak.id, ak.label, ak.key_cipher, ak.enabled,
                       m.name as model_name, m.upstream_base, m.upstream_model
                FROM api_keys ak
                LEFT JOIN models m ON ak.model_id = m.id
                WHERE ak.enabled = 1
                ORDER BY ak.id
            """)
            rows = await cur.fetchall()

    if not rows:
        print("No enabled API keys found in TiDB database.")
        pool.close()
        await pool.wait_closed()
        return

    print(f"Found {len(rows)} enabled API key(s)\n")

    # Decrypt and prepare keys
    keys_to_test = []
    for row in rows:
        try:
            key_cipher = row[2]  # key_cipher from TiDB
            
            # Handle different data types from TiDB
            if isinstance(key_cipher, bytes):
                cipher_bytes = key_cipher
            elif isinstance(key_cipher, str):
                cipher_bytes = key_cipher.encode('utf-8')
            else:
                cipher_bytes = str(key_cipher).encode('utf-8') if key_cipher else b''
            
            # Decrypt the key using initialized crypto
            try:
                plain_key = decrypt(cipher_bytes).decode("utf-8", errors="replace")
            except Exception as dec_err:
                raise Exception(f"Decrypt failed for key_id={row[0]}, cipher_type={type(key_cipher)}, cipher_hex={cipher_bytes[:30].hex() if cipher_bytes else 'empty'}: {dec_err}")

            model_name = (row[4] or "").replace("`", "").replace('"', '').strip()
            upstream_base = ((row[5] or "")).replace("`", "").replace('"', '').strip()
            upstream_model = ((row[6] or "")).replace("`", "").replace('"', '').strip()

            keys_to_test.append({
                "key_id": row[0],
                "label": row[1] or f"key_{row[0]}",
                "api_key": plain_key,
                "upstream_base": upstream_base,
                "upstream_model": upstream_model,
                "model_name": model_name,
            })
        except Exception as e:
            print(f"Warning: Failed to decrypt key_id={row[0]}: {e}")

    pool.close()
    await pool.wait_closed()

    if not keys_to_test:
        print("No keys could be processed.")
        return

    # Test keys concurrently
    async with httpx.AsyncClient(timeout=30.0) as client:
        semaphore = asyncio.Semaphore(concurrent)

        async def test_with_semaphore(key_info):
            async with semaphore:
                return await test_single_key(
                    client,
                    key_info["key_id"],
                    key_info["api_key"],
                    key_info["upstream_base"],
                    key_info["upstream_model"],
                    key_info["model_name"],
                )

        print("Testing keys... (this may take a moment)\n")
        tasks = [test_with_semaphore(k) for k in keys_to_test]
        results = await asyncio.gather(*tasks)

    # Print results
    print_results(results)


async def test_sqlite_keys(db_path: str, concurrent: int = 5) -> None:
    """Test all enabled API keys from SQLite database."""
    import sqlite3
    from zhongzhuan.crypto import init as crypto_init, decrypt
    from pathlib import Path

    print(f"\nLoading keys from SQLite: {db_path}\n")

    # Initialize crypto with local key file
    await crypto_init(Path("."))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all enabled keys with their model info
    cursor.execute("""
        SELECT ak.id, ak.label, ak.key_cipher, ak.enabled,
               m.name as model_name, m.upstream_base, m.upstream_model
        FROM api_keys ak
        LEFT JOIN models m ON ak.model_id = m.id
        WHERE ak.enabled = 1
        ORDER BY ak.id
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No enabled API keys found in SQLite database.")
        return

    print(f"Found {len(rows)} enabled API key(s)\n")

    # Decrypt keys
    keys_to_test = []
    for row in rows:
        try:
            cipher_bytes = row["key_cipher"]
            if isinstance(cipher_bytes, str):
                cipher_bytes = cipher_bytes.encode('utf-8')
            plain_key = decrypt(cipher_bytes).decode("utf-8", errors="replace")
            model_name = (row["model_name"] or "").replace("`", "").replace('"', '').strip()
            upstream_base = ((row["upstream_base"] or "")).replace("`", "").replace('"', '').strip()
            upstream_model = ((row["upstream_model"] or "")).replace("`", "").replace('"', '').strip()

            keys_to_test.append({
                "key_id": row["id"],
                "label": row["label"] or f"key_{row['id']}",
                "api_key": plain_key,
                "upstream_base": upstream_base,
                "upstream_model": upstream_model,
                "model_name": model_name,
            })
        except Exception as e:
            print(f"Warning: Failed to decrypt key_id={row['id']}: {e}")

    if not keys_to_test:
        print("No keys could be decrypted.")
        return

    # Test keys concurrently
    async with httpx.AsyncClient(timeout=30.0) as client:
        semaphore = asyncio.Semaphore(concurrent)

        async def test_with_semaphore(key_info):
            async with semaphore:
                return await test_single_key(
                    client,
                    key_info["key_id"],
                    key_info["api_key"],
                    key_info["upstream_base"],
                    key_info["upstream_model"],
                    key_info["model_name"],
                )

        print("Testing keys... (this may take a moment)\n")
        tasks = [test_with_semaphore(k) for k in keys_to_test]
        results = await asyncio.gather(*tasks)

    # Print results
    print_results(results)


def print_results(results: list) -> None:
    """Print test results in a formatted way."""
    print(f"\n{'='*80}")
    print(f"Results")
    print(f"{'='*80}\n")

    valid_count = 0
    invalid_count = 0

    for result in sorted(results, key=lambda x: x.key_id):
        status_icon = "[OK]" if result.is_valid else "[FAIL]"
        status_color = "VALID" if result.is_valid else "INVALID"

        print(f"{status_icon} Key #{result.key_id} ({result.label})")
        print(f"       Upstream: {result.upstream_base}")
        print(f"       Model: {result.model_name} -> {result.upstream_model}")
        print(f"       Key: {result.masked_key}")

        if result.is_valid:
            valid_count += 1
            print(f"       Status: {result.status_code} - Valid!")
        else:
            invalid_count += 1
            print(f"       Status: {result.status_code}")
            if result.error_message:
                print(f"       Error: {result.error_message}")

        if result.response_time_ms > 0:
            print(f"       Response time: {result.response_time_ms:.0f}ms")

        print()

    # Summary
    print(f"{'='*80}")
    print(f"Summary: {valid_count} valid, {invalid_count} invalid, {len(results)} total")
    print(f"{'='*80}\n")

    if valid_count == 0:
        print("WARNING: No valid keys found! Please check your API keys at the upstream provider.")
    elif invalid_count > 0:
        print(f"NOTE: {invalid_count} key(s) are invalid. Consider replacing them.")


async def detect_and_test() -> None:
    """Auto-detect database type and test keys."""
    # Check for TiDB config in environment variables
    tidb_host = os.environ.get("ZHONGZHUAN_TIDB_HOST")

    if tidb_host:
        # TiDB mode
        print("="*80)
        print("TiDB Mode Detected")
        print("="*80)

        tidb_config = {
            "host": tidb_host,
            "port": int(os.environ.get("ZHONGZHUAN_TIDB_PORT", "4000")),
            "user": os.environ.get("ZHONGZHUAN_TIDB_USER", ""),
            "password": os.environ.get("ZHONGZHUAN_TIDB_PASSWORD", ""),
            "database": os.environ.get("ZHONGZHUAN_TIDB_DATABASE", "zhongzhuan"),
            "ssl": os.environ.get("ZHONGZHUAN_TIDB_SSL", "true").lower() == "true",
        }

        await test_tidb_keys(tidb_config)
    else:
        # SQLite mode - check command line args or default
        db_path = sys.argv[1] if len(sys.argv) > 1 else "data.db"

        if not os.path.exists(db_path):
            print(f"Error: SQLite database not found: {db_path}")
            print("Please specify the database path or set TiDB environment variables.")
            sys.exit(1)

        print("="*80)
        print("SQLite Mode Detected")
        print("="*80)

        await test_sqlite_keys(db_path)


def main():
    asyncio.run(detect_and_test())


if __name__ == "__main__":
    main()
