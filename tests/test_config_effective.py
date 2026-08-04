"""T32 判据② — effective config 快照无密钥且标注来源（R-P1-62）。"""

from __future__ import annotations

import yaml
import pytest

from zhongzhuan.config import default_config, load_config
from zhongzhuan.config.effective import (
    FALLBACK_PRIVACY_NOTICE,
    effective_config_snapshot,
    format_effective_config,
    is_secret_path,
)


def _write(tmp_path, data: dict) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 脱敏：密钥字段 → "***"，快照整体无明文密钥
# ---------------------------------------------------------------------------


def test_snapshot_redacts_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "super-secret-jwt-42")
    path = _write(tmp_path, {"fallback": {"api_key": "public-key-value"}})
    cfg = load_config(str(path))

    snap = effective_config_snapshot(cfg)
    assert snap["auth.jwt_secret"]["value"] == "***"
    assert snap["fallback.api_key"]["value"] == "***"

    blob = str(snap)
    assert "super-secret-jwt-42" not in blob
    assert "public-key-value" not in blob


def test_snapshot_has_no_plaintext_secret_anywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "topsecret")
    path = _write(tmp_path, {"fallback": {"api_key": "also-secret"}})
    cfg = load_config(str(path))

    lines = format_effective_config(cfg)
    rendered = "\n".join(lines)
    assert "topsecret" not in rendered
    assert "also-secret" not in rendered


def test_redacted_snapshot_matches_criterion(monkeypatch):
    """判据② 的直接断言：快照里没有密钥，且每条都有 source 标注。"""
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "rot-secret")
    cfg = load_config(None)
    snap = effective_config_snapshot(cfg)
    for path, entry in snap.items():
        assert "source" in entry
        assert "value" in entry
    secrets = [p for p in snap if is_secret_path(p)]
    assert secrets, "expected at least one secret-bearing path in the snapshot"
    for path in secrets:
        assert snap[path]["value"] in ("***", "", []), f"{path} leaked a value"


# ---------------------------------------------------------------------------
# 来源标注：default / YAML / env
# ---------------------------------------------------------------------------


def test_snapshot_annotates_sources(tmp_path, monkeypatch):
    path = _write(tmp_path, {"server": {"proxy": {"port": 9000}}})
    monkeypatch.setenv("ZHONGZHUAN_ADMIN_PORT", "9100")
    cfg = load_config(str(path))

    snap = effective_config_snapshot(cfg)
    assert snap["server.proxy.port"]["source"] == "YAML"
    assert snap["server.proxy.port"]["value"] == 9000
    assert snap["server.admin.port"]["source"] == "env"
    assert snap["server.admin.port"]["value"] == 9100
    assert snap["server.admin.host"]["source"] == "default"
    assert snap["env"]["source"] == "default"


def test_snapshot_db_alias_source(tmp_path):
    path = _write(tmp_path, {"storage": {"db_path": "custom.db"}})
    cfg = load_config(str(path))
    snap = effective_config_snapshot(cfg)
    assert snap["storage.sqlite_db_path"]["source"] == "YAML"
    assert snap["storage.sqlite_db_path"]["value"] == "custom.db"


def test_timeout_fields_carry_sources(tmp_path):
    path = _write(tmp_path, {"timeouts": {"connect_seconds": 7}})
    cfg = load_config(str(path))
    snap = effective_config_snapshot(cfg)
    assert snap["timeouts.connect_seconds"]["source"] == "YAML"
    assert snap["timeouts.connect_seconds"]["value"] == 7.0
    assert snap["timeouts.first_token_seconds"]["source"] == "default"


# ---------------------------------------------------------------------------
# 工具函数 & 隐私提示（R-P2-06）
# ---------------------------------------------------------------------------


def test_secret_path_detection():
    assert is_secret_path("auth.jwt_secret")
    assert is_secret_path("fallback.api_key")
    assert is_secret_path("auth.jwt_previous_secrets")
    # 证书/密钥文件路径不是密钥值，不应被脱敏。
    assert is_secret_path("server.tls.key_file") is False
    assert is_secret_path("server.tls.cert_file") is False
    # 「per key」限流字段不是密钥，不应被脱敏。
    assert is_secret_path("limits.default_rpm_per_key") is False
    assert is_secret_path("limits.default_tpm_per_key") is False
    assert is_secret_path("limits.default_rpd_per_key") is False
    assert is_secret_path("limits.per_key_window_seconds") is False
    assert is_secret_path("auth.jwt_grace_period_seconds") is False


def test_rate_limit_fields_not_redacted():
    """非密钥字段保持原值（避免过度脱敏）。"""
    cfg = default_config()
    snap = effective_config_snapshot(cfg)
    assert snap["limits.default_rpm_per_key"]["value"] == 60
    assert snap["limits.default_tpm_per_key"]["value"] == 100000
    assert snap["limits.global_concurrent"]["value"] == 64


def test_fallback_privacy_notice_present():
    assert FALLBACK_PRIVACY_NOTICE
    assert "外部" in FALLBACK_PRIVACY_NOTICE or "出站" in FALLBACK_PRIVACY_NOTICE
    assert "OpenCode Free" in FALLBACK_PRIVACY_NOTICE


def test_default_fallback_disabled():
    """判据⑦：默认配置下 fallback 关闭（显式 opt-in）。"""
    assert default_config().fallback.enabled is False
