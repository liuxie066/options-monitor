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
    assert "resolve_allow_derived_config_gate(" in src
    assert "OM_ALLOW_DERIVED_CONFIG_LEGACY_DISABLED" in Path(
        "om/domain/config_contract.py"
    ).read_text(encoding="utf-8")
    assert "OM_ALLOW_DERIVED_CONFIG_ENABLED" in src


def test_multi_tick_main_current_run_id_accessor_is_public_compat() -> None:
    import importlib

    mod = importlib.import_module("scripts.multi_tick.main")

    old = mod._CURRENT_RUN_ID
    try:
        mod._CURRENT_RUN_ID = "test-run-id"
        assert mod.current_run_id() == "test-run-id"
    finally:
        mod._CURRENT_RUN_ID = old


def test_multi_entrypoint_uses_public_run_id_accessor() -> None:
    src = Path("scripts/send_if_needed_multi.py").read_text(encoding="utf-8")
    assert "importlib" not in src
    assert "current_run_id as _current_run_id" in src
    assert "run_id=_current_run_id()" in src


def test_resolve_allow_derived_config_gate_defaults_to_disabled_and_has_migration_hint() -> None:
    from om.domain import resolve_allow_derived_config_gate

    out_default = resolve_allow_derived_config_gate("")
    assert out_default["allow_derived"] is False
    assert out_default["error_code"] is None
    assert "OM_ALLOW_DERIVED_CONFIG=strict" in str(out_default["migration_hint"])

    out_legacy = resolve_allow_derived_config_gate("true")
    assert out_legacy["allow_derived"] is False
    assert out_legacy["error_code"] == "OM_ALLOW_DERIVED_CONFIG_LEGACY_DISABLED"

    out_strict = resolve_allow_derived_config_gate("strict")
    assert out_strict["allow_derived"] is True
