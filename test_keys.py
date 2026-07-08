#!/usr/bin/env python3
"""Batch test API keys from database to check their validity."""
import asyncio
import sqlite3
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

    # Clean URL
    upstream_base = upstream_base.replace("`", "").replace('"', '').strip().rstrip("/")
    if not upstream_base.startswith(("http://", "https://")):
        upstream_base = "https://" + upstream_base

    test_url = f"{upstream_base}/v1/chat/completions"
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


async def test_all_keys(db_path: str = "data.db", concurrent: int = 5) -> None:
    """Test all enabled API keys from the database."""
    print(f"\n{'='*80}")
    print(f"API Key Tester - Testing keys from {db_path}")
    print(f"{'='*80}\n")

    # Connect to SQLite (synchronous for reading)
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
        print("No enabled API keys found in database.")
        return

    print(f"Found {len(rows)} enabled API key(s)\n")

    # Decrypt keys (simple implementation matching the crypto module)
    from zhongzhuan.crypto import decrypt

    keys_to_test = []
    for row in rows:
        try:
            plain_key = decrypt(row["key_cipher"]).decode("utf-8", errors="replace")
            model_name = row["model_name"] or ""
            upstream_base = (row["upstream_base"] or "").replace("`", "").replace('"', '').strip()
            upstream_model = (row["upstream_model"] or "").replace("`", "").replace('"', '').strip()

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


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data.db"
    asyncio.run(test_all_keys(db_path))


if __name__ == "__main__":
    main()
