"""Config tests."""
from zhongzhuan.config.config import Config, default_config, load_config


def test_default_proxy_port():
    cfg = default_config()
    assert cfg.server.proxy.port == 8088


def test_default_admin_port():
    cfg = default_config()
    assert cfg.server.admin.port == 8089


def test_default_host():
    cfg = default_config()
    assert cfg.server.proxy.host == "127.0.0.1"
    assert cfg.server.admin.host == "127.0.0.1"


def test_default_global_concurrent():
    cfg = default_config()
    assert cfg.limits.global_concurrent == 64


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg.server.proxy.port == 8088


def test_service_name_default():
    cfg = default_config()
    assert cfg.windows_service.service_name == "Zhongzhuan"