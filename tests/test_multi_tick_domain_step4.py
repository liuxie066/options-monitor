from __future__ import annotations

import pytest

from datetime import time
from pathlib import Path


def test_evaluate_dnd_quiet_hours_cross_midnight_window() -> None:
    from domain.domain.multi_tick import evaluate_dnd_quiet_hours

    out = evaluate_dnd_quiet_hours(
        quiet_hours={'start': '23:00', 'end': '06:00'},
        no_send=False,
        now_bj_time=time(0, 30),
        parse_hhmm_fn=lambda s: time.fromisoformat(s),
    )

    assert out['enabled'] is True
    assert out['quiet_window'] == '23:00-06:00'
    assert out['is_quiet'] is True
    assert out['parse_error'] is None


def test_evaluate_dnd_quiet_hours_parse_error_keeps_non_blocking_behavior() -> None:
    from domain.domain.multi_tick import evaluate_dnd_quiet_hours

    out = evaluate_dnd_quiet_hours(
        quiet_hours={'start': 'BAD', 'end': '06:00'},
        no_send=False,
        now_bj_time=time(3, 0),
        parse_hhmm_fn=lambda s: time.fromisoformat(s),
    )

    assert out['enabled'] is True
    assert out['is_quiet'] is False
    assert isinstance(out['parse_error'], str) and bool(out['parse_error'])


def test_decide_notify_dispatch_preserves_route_and_target_rules() -> None:
    from domain.domain.multi_tick import decide_notify_dispatch

    assert decide_notify_dispatch(no_send=True, target='chat-id', dnd_is_quiet=False) == {
        'should_send': False,
        'effective_target': None,
        'config_error': None,
        'reason': 'no_send',
    }

    assert decide_notify_dispatch(no_send=False, target='', dnd_is_quiet=False) == {
        'should_send': False,
        'effective_target': '',
        'config_error': 'notifications.target is required',
        'reason': 'config_error',
    }

    assert decide_notify_dispatch(no_send=False, target='chat-id', dnd_is_quiet=True) == {
        'should_send': False,
        'effective_target': 'chat-id',
        'config_error': None,
        'reason': 'quiet_hours',
    }


def test_resolve_notification_channel_target_keeps_fallback_order() -> None:
    from domain.domain.multi_tick import resolve_notification_channel_target

    out_default = resolve_notification_channel_target(
        notifications={'target': 'user:cfg'},
        cli_channel=None,
        cli_target=None,
    )
    assert out_default == {'provider': 'wechat_clawbot', 'channel': 'wechat_clawbot', 'target': 'user:cfg'}

    out_cli = resolve_notification_channel_target(
        notifications={'channel': 'cfg-chan', 'target': 'user:cfg'},
        cli_channel='cli-chan',
        cli_target='user:cli',
    )
    assert out_cli == {'provider': 'wechat_clawbot', 'channel': 'wechat_clawbot', 'target': 'user:cli'}


def test_notification_channel_helpers_accept_wechat_clawbot() -> None:
    from domain.domain.multi_tick import (
        is_openclaw_notification_channel,
        is_supported_notification_provider,
        is_supported_notification_channel,
        normalize_notification_channel,
        normalize_notification_provider,
        resolve_openclaw_transport_channel,
    )

    assert normalize_notification_provider("openclaw-weixin") == "openclaw-weixin"
    assert normalize_notification_channel(" WeChat_Clawbot ") == "wechat_clawbot"
    assert is_supported_notification_provider("openclaw") is False
    assert is_supported_notification_channel("openclaw-weixin") is False
    assert is_supported_notification_channel("wechat_clawbot") is True
    assert is_openclaw_notification_channel("wechat_clawbot") is False
    assert resolve_openclaw_transport_channel("wechat_clawbot") == "wechat_clawbot"
    with pytest.raises(ValueError) as _caught:
        resolve_openclaw_transport_channel("openclaw-weixin")
    exc = _caught.value
    assert "OpenClaw notification routing has been removed" in str(exc)
    assert is_supported_notification_channel("sms") is False


def test_resolve_notification_route_from_config_centralizes_notifications_reads() -> None:
    from domain.domain.multi_tick import resolve_notification_route_from_config

    out = resolve_notification_route_from_config(
        config={'notifications': {'target': 'user:cfg'}},
    )
    assert out == {
        'notifications': {'target': 'user:cfg'},
        'provider': 'wechat_clawbot',
        'channel': 'wechat_clawbot',
        'target': 'user:cfg',
    }

    out_cli = resolve_notification_route_from_config(
        config={'notifications': {'channel': 'cfg-chan', 'target': 'user:cfg'}},
        cli_channel='cli-chan',
        cli_target='user:cli',
    )
    assert out_cli == {
        'notifications': {'channel': 'cfg-chan', 'target': 'user:cfg'},
        'provider': 'wechat_clawbot',
        'channel': 'wechat_clawbot',
        'target': 'user:cli',
    }


def test_resolve_notification_route_rejects_removed_openclaw_values() -> None:
    from domain.domain.multi_tick import resolve_notification_route_from_config

    cases = [
        {'notifications': {'provider': 'openclaw', 'channel': 'wechat_clawbot', 'target': 'wechat:ops'}},
        {'notifications': {'provider': 'wechat_clawbot', 'channel': 'openclaw-weixin', 'target': 'wechat:ops'}},
        {'notifications': {'transport_channel': 'openclaw-weixin', 'target': 'wechat:ops'}},
    ]
    for config in cases:
        with pytest.raises(ValueError) as _caught:
            resolve_notification_route_from_config(config=config)
        exc = _caught.value
        assert "OpenClaw notification routing has been removed" in str(exc)

    with pytest.raises(ValueError) as _caught:
        resolve_notification_route_from_config(config={'notifications': {'target': 'wechat:ops'}}, cli_channel='openclaw-weixin')
    exc = _caught.value
    assert "OpenClaw notification routing has been removed" in str(exc)


def test_resolve_scheduler_state_path_supports_legacy_state_override() -> None:
    from domain.domain.multi_tick import resolve_scheduler_state_path

    base = Path('/tmp/base-test')
    state = resolve_scheduler_state_path(
        base_dir=base,
        state_dir='output_shared/state',
        state_override='custom/state.json',
    )
    assert str(state) == '/tmp/base-test/custom/state.json'

    state_default = resolve_scheduler_state_path(
        base_dir=base,
        state_dir='output_shared/state',
        state_override=None,
    )
    assert str(state_default) == '/tmp/base-test/output_shared/state/scheduler_state.json'
