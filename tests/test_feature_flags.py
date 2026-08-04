"""Responses v3 feature-flag precedence and live rollback tests."""

from __future__ import annotations

from types import SimpleNamespace

from zhongzhuan.proxy.feature_flags import ResponsesFeatureFlags


def _config(*, enabled=True, models=None, groups=None, keys=None):
    return SimpleNamespace(
        enabled=enabled,
        rollout=SimpleNamespace(
            models=models or {},
            groups=groups or {},
            keys=keys or {},
        ),
    )


def _ctx(model: str):
    return SimpleNamespace(requested_model=model)


def test_namespaced_environment_override_wins_over_legacy():
    flags = ResponsesFeatureFlags(
        _config(enabled=False, keys={7: False}),
        environ={
            "ZHONGZHUAN_RESPONSES_BRIDGE_V3": "true",
            "RESPONSES_BRIDGE_V3": "false",
        },
    )
    assert flags.v3_enabled(_ctx("gpt-4o")) is True
    assert flags.v3_key_allowed(7) is True


def test_environment_false_overrides_enabled_key():
    flags = ResponsesFeatureFlags(
        _config(enabled=True, keys={7: True}),
        environ={"ZHONGZHUAN_RESPONSES_BRIDGE_V3": "off"},
    )
    assert flags.v3_enabled(_ctx("gpt-4o")) is False
    assert flags.v3_key_allowed(7) is False


def test_environment_true_overrides_disabled_key_and_global():
    flags = ResponsesFeatureFlags(
        _config(enabled=False, keys={7: False}),
        environ={"RESPONSES_BRIDGE_V3": "yes"},
    )
    assert flags.v3_enabled(_ctx("gpt-4o")) is True
    assert flags.v3_key_allowed(7) is True


def test_invalid_runtime_environment_fails_closed():
    flags = ResponsesFeatureFlags(
        _config(enabled=True, keys={7: True}),
        environ={"ZHONGZHUAN_RESPONSES_BRIDGE_V3": "invalid"},
    )
    assert flags.v3_enabled(_ctx("gpt-4o")) is False
    assert flags.v3_key_allowed(7) is False


def test_model_then_group_name_then_global_precedence():
    flags = ResponsesFeatureFlags(
        _config(
            enabled=False,
            models={"direct-model": True},
            groups={"group-alias": True},
        ),
        environ={},
    )
    assert flags.v3_enabled(_ctx("direct-model")) is True
    assert flags.v3_enabled(_ctx("group-alias")) is True
    assert flags.v3_enabled(_ctx("other")) is False


def test_key_rollout_applies_only_without_environment_override():
    flags = ResponsesFeatureFlags(
        _config(keys={1: False, 2: True}),
        environ={},
    )
    assert flags.v3_key_allowed(1) is False
    assert flags.v3_key_allowed(2) is True
    assert flags.v3_key_allowed(3) is True


def test_live_environment_mapping_affects_next_request():
    environ: dict[str, str] = {}
    flags = ResponsesFeatureFlags(_config(enabled=True), environ=environ)
    assert flags.v3_enabled(_ctx("gpt-4o")) is True

    environ["ZHONGZHUAN_RESPONSES_BRIDGE_V3"] = "false"
    assert flags.v3_enabled(_ctx("gpt-4o")) is False
