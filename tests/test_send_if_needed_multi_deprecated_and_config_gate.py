from __future__ import annotations

import warnings
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


def test_private_compat_exports_emit_deprecation_warning_and_keep_behavior() -> None:
    import scripts.send_if_needed_multi as s
    from om.domain import select_markets_to_run
    from scripts.multi_tick.opend_guard import should_send_opend_alert

    cfg = {
        'schedule_hk': {
            'enabled': True,
            'market_timezone': 'Asia/Hong_Kong',
            'market_open': '09:30',
            'market_close': '16:00',
            'monitor_off_hours': False,
            'market_break_start': '12:00',
            'market_break_end': '13:00',
            'beijing_timezone': 'Asia/Shanghai',
            'sparse_after_beijing': '02:00',
        },
        'schedule': {
            'enabled': False,
            'market_timezone': 'America/New_York',
            'market_open': '09:30',
            'market_close': '16:00',
            'monitor_off_hours': False,
            'beijing_timezone': 'Asia/Shanghai',
            'sparse_after_beijing': '02:00',
        },
    }
    now_utc = datetime(2026, 4, 1, 4, 30, 0, tzinfo=timezone.utc)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', DeprecationWarning)
        compat_out = s._select_markets_to_run(now_utc, cfg, 'auto')

    direct_out = select_markets_to_run(now_utc, cfg, 'auto')
    assert compat_out == direct_out
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    warning_messages = [str(w.message) for w in caught]
    assert any('om.domain.select_markets_to_run' in msg for msg in warning_messages)

    with TemporaryDirectory() as td:
        base = Path(td)
        with warnings.catch_warnings(record=True) as caught_alert:
            warnings.simplefilter('always', DeprecationWarning)
            compat_first = s._should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600)

        direct_second = should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600)
        assert compat_first is True
        assert direct_second is False
        assert any(issubclass(w.category, DeprecationWarning) for w in caught_alert)
        warning_messages = [str(w.message) for w in caught_alert]
        assert any('scripts.multi_tick.opend_guard.should_send_opend_alert' in msg for msg in warning_messages)


def test_om_allow_derived_config_gate_is_env_controlled_in_multi_tick_main() -> None:
    src = Path("scripts/multi_tick/main.py").read_text(encoding="utf-8")
    assert "OM_ALLOW_DERIVED_CONFIG" in src
    assert "allow_derived=allow_derived_config" in src
    assert "OM_ALLOW_DERIVED_CONFIG_INVALID" in src
    assert "OM_ALLOW_DERIVED_CONFIG_ENABLED" in src
