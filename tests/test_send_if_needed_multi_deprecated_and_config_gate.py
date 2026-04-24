from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


def test_market_session_and_opend_alert_use_single_source_of_truth() -> None:
    from domain.domain import select_markets_to_run
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

    direct_out = select_markets_to_run(now_utc, cfg, 'auto')
    assert direct_out == []

    with TemporaryDirectory() as td:
        base = Path(td)
        direct_first = should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600)
        direct_second = should_send_opend_alert(base, 'OPEND_RATE_LIMIT', cooldown_sec=600)
        assert direct_first is True
        assert direct_second is False


def test_multi_tick_main_enforces_canonical_runtime_config_without_derived_gate() -> None:
    src = Path("scripts/multi_tick/main.py").read_text(encoding="utf-8")
    assert "OM_ALLOW_DERIVED_CONFIG" not in src
    assert "resolve_allow_derived_config_gate(" not in src
    assert "allow_derived=" not in src
    contract_src = Path("domain/domain/config_contract.py").read_text(encoding="utf-8")
    assert "resolve_allow_derived_config_gate" not in contract_src


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


def test_ensure_runtime_canonical_config_rejects_derived_configs() -> None:
    from domain.domain import ensure_runtime_canonical_config

    try:
        ensure_runtime_canonical_config("config.market_us.json", "us")
        raise AssertionError("expected canonical config guard failure")
    except SystemExit as e:
        assert "runtime config must be canonical" in str(e)


def test_ensure_runtime_canonical_config_requires_sibling_external_when_present() -> None:
    from domain.domain import ensure_runtime_canonical_config

    with TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "options-monitor-prod"
        repo.mkdir()
        local_cfg = repo / "config.hk.json"
        local_cfg.write_text("{}", encoding="utf-8")

        canonical_dir = root / "options-monitor-config"
        canonical_dir.mkdir()
        canonical_cfg = canonical_dir / "config.hk.json"
        canonical_cfg.write_text("{}", encoding="utf-8")

        try:
            ensure_runtime_canonical_config(
                local_cfg,
                "hk",
                repo_base=repo,
                require_sibling_external=True,
            )
            raise AssertionError("expected sibling canonical config guard failure")
        except SystemExit as e:
            assert "must use sibling canonical config when present" in str(e)
            assert str(local_cfg.resolve()) in str(e)
            assert str(canonical_cfg.resolve()) in str(e)

        out = ensure_runtime_canonical_config(
            canonical_cfg,
            "hk",
            repo_base=repo,
            require_sibling_external=True,
        )
        assert out["is_sibling_canonical"] is True


def test_ensure_runtime_canonical_config_allows_repo_local_when_no_sibling_external_exists() -> None:
    from domain.domain import ensure_runtime_canonical_config

    with TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "options-monitor-prod"
        repo.mkdir()
        local_cfg = repo / "config.hk.json"
        local_cfg.write_text("{}", encoding="utf-8")

        out = ensure_runtime_canonical_config(
            local_cfg,
            "hk",
            repo_base=repo,
            require_sibling_external=True,
        )
        assert out["resolved_path"] == str(local_cfg.resolve())
        assert out["sibling_canonical_exists"] is False


def test_production_entrypoints_enable_sibling_external_guard() -> None:
    send_src = Path("scripts/send_if_needed.py").read_text(encoding="utf-8")
    multi_src = Path("scripts/multi_tick/main.py").read_text(encoding="utf-8")

    assert "require_sibling_external=True" in send_src
    assert "ensure_runtime_canonical_config(" in send_src
    assert "require_sibling_external=True" in multi_src
    assert "config_source_path" in multi_src
