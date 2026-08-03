"""T32 判据①④⑤⑥ — 严格校验 + 生产 fail-closed（配置层）。

判据①：类型错误 / 越界值 / 未知字段各断言启动失败。
判据④：``env=production`` 且鉴权关闭断言启动失败（R-P2-02）。
判据⑤：生产模式缺 JWT secret 启动失败（R-P2-04）。
判据⑥：无有效 key 时生产模式启动失败；开发模式可启动并告警（R-P2-05）。
"""
from __future__ import annotations

import yaml
import pytest

from zhongzhuan.config import ConfigError, load_config


def _write(tmp_path, data: dict) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 判据① 类型错误 / 越界值 / 未知字段 → 启动失败
# ---------------------------------------------------------------------------


def test_type_error_fails_startup(tmp_path):
    path = _write(tmp_path, {"server": {"proxy": {"port": "not-a-port"}}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_type_error_bool_for_concurrency_fails(tmp_path):
    path = _write(tmp_path, {"limits": {"global_concurrent": True}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_out_of_range_port_fails_startup(tmp_path):
    path = _write(tmp_path, {"server": {"proxy": {"port": 99999}}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_out_of_range_concurrency_fails_startup(tmp_path):
    path = _write(tmp_path, {"limits": {"global_concurrent": 0}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_out_of_range_ttl_fails_startup(tmp_path):
    path = _write(tmp_path, {"limits": {"sticky_session_ttl": -5}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_url_fails_startup(tmp_path):
    path = _write(tmp_path, {"fallback": {"upstream_base": "not-a-url"}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_penalty_range_fails_startup(tmp_path):
    path = _write(tmp_path, {"fallback": {"fallback_penalty": 5.0}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_top_level_field_fails_startup(tmp_path):
    path = _write(tmp_path, {"bogus_section": {"x": 1}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_nested_field_fails_startup(tmp_path):
    path = _write(tmp_path, {"server": {"proxy": {"not_a_real_option": 1}}})
    with pytest.raises(ConfigError):
        load_config(path)


def test_timeout_floor_still_fails_startup(tmp_path):
    from zhongzhuan.config import TimeoutConfigError

    path = _write(tmp_path, {"timeouts": {"first_token_seconds": 30}})
    with pytest.raises((ConfigError, TimeoutConfigError)):
        load_config(path)


def test_invalid_env_mode_fails_startup(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_ENV", "staging")
    with pytest.raises(ConfigError):
        load_config(None)


# ---------------------------------------------------------------------------
# R-P1-62 显式兼容模式：未知字段被忽略，类型/范围校验仍然生效
# ---------------------------------------------------------------------------


def test_compat_mode_ignores_unknown_fields(tmp_path):
    path = _write(tmp_path, {"bogus_section": {"x": 1}})
    cfg = load_config(path, compat=True)
    assert cfg.server.proxy.port == 8088


def test_compat_mode_env_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_CONFIG_COMPAT", "1")
    path = _write(tmp_path, {"bogus_section": {"x": 1}})
    cfg = load_config(path)
    assert cfg.server.proxy.port == 8088


def test_compat_mode_still_validates_types(tmp_path):
    # 兼容模式只放宽「未知字段」，类型错误仍然启动失败。
    path = _write(tmp_path, {"server": {"proxy": {"port": "not-a-port"}}})
    with pytest.raises(ConfigError):
        load_config(path, compat=True)


def test_compat_mode_unknown_nested_keeps_defaults(tmp_path):
    path = _write(tmp_path, {"server": {"proxy": {"bogus": 1}}})
    cfg = load_config(path, compat=True)
    assert cfg.server.proxy.port == 8088


# ---------------------------------------------------------------------------
# 判据④ 生产模式 + 鉴权关闭 → 启动失败（R-P2-02）
# ---------------------------------------------------------------------------


def test_production_admin_auth_off_fails_startup(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.setenv("ZHONGZHUAN_ADMIN_AUTH", "false")
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "x")  # 隔离 JWT 因素
    with pytest.raises(ConfigError) as exc:
        load_config(None)
    assert "admin auth is disabled" in str(exc.value)


def test_production_proxy_auth_off_fails_startup(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.setenv("ZHONGZHUAN_PROXY_AUTH", "false")
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "x")
    with pytest.raises(ConfigError) as exc:
        load_config(None)
    assert "proxy auth is disabled" in str(exc.value)


def test_production_auth_off_confirmed_allows(monkeypatch):
    # 「显式关闭需二次确认」：打开 allow_insecure_disable 后允许。
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.setenv("ZHONGZHUAN_ADMIN_AUTH", "false")
    monkeypatch.setenv("ZHONGZHUAN_PROXY_AUTH", "false")
    monkeypatch.setenv("ZHONGZHUAN_ALLOW_INSECURE_DISABLE", "true")
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "x")
    cfg = load_config(None)
    assert cfg.auth.admin_enabled is False
    assert cfg.auth.proxy_enabled is False


def test_production_auth_defaults_on(monkeypatch):
    # 生产模式未显式配置鉴权 → 默认开启。
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "x")
    cfg = load_config(None)
    assert cfg.auth.admin_enabled is True
    assert cfg.auth.proxy_enabled is True


# ---------------------------------------------------------------------------
# 判据⑤ 生产模式缺 JWT secret → 启动失败（R-P2-04）
# ---------------------------------------------------------------------------


def test_production_missing_jwt_secret_fails(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.delenv("ZHONGZHUAN_JWT_SECRET", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_config(None)
    assert "JWT_SECRET" in str(exc.value)


def test_production_jwt_secret_present_ok(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "a-valid-production-secret")
    cfg = load_config(None)
    assert cfg.auth.jwt_secret == "a-valid-production-secret"


# ---------------------------------------------------------------------------
# 判据⑥ 生产模式无有效 key → 启动失败；开发模式启动并告警（R-P2-05）
# ---------------------------------------------------------------------------


def test_production_no_api_keys_fails(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "x")
    with pytest.raises(ConfigError) as exc:
        load_config(None, api_key_count=0)
    assert "no valid API keys" in str(exc.value)


def test_dev_no_api_keys_starts_with_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("ZHONGZHUAN_ENV", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_JWT_SECRET", raising=False)
    with pytest.warns(UserWarning, match="no API keys"):
        cfg = load_config(str(tmp_path / "missing.yaml"), api_key_count=0)
    assert cfg.env == "development"


def test_dev_default_startup_ok(tmp_path, monkeypatch):
    monkeypatch.delenv("ZHONGZHUAN_ENV", raising=False)
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg.env == "development"
    assert cfg.server.proxy.port == 8088
