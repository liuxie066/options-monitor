from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


def test_cli_run_tick_delegates_to_application_tick(
    example_config_path: Path,
    monkeypatch,
) -> None:
    from src.interfaces.cli import main as cli_main

    seen: list[list[str]] = []
    monkeypatch.setattr(cli_main, "run_tick", lambda argv: seen.append(list(argv)) or 7)

    rc = cli_main.main(
        [
            "run",
            "tick",
            "--config",
            str(example_config_path),
            "--accounts",
            "lx",
            "sy",
            "--default-account",
            "sy",
            "--market-config",
            "us",
            "--no-send",
            "--force",
            "--debug",
        ]
    )

    assert rc == 7
    assert seen == [
        [
            "--config",
            str(example_config_path),
            "--accounts",
            "lx",
            "sy",
            "--default-account",
            "sy",
            "--market-config",
            "us",
            "--no-send",
            "--force",
            "--debug",
        ]
    ]


def test_cli_run_tick_cron_dry_run_outputs_plan(capsys) -> None:
    from src.interfaces.cli import main as cli_main

    rc = cli_main.main(
        [
            "run",
            "tick-cron",
            "--market",
            "hk",
            "--accounts",
            "lx",
            "sy",
            "--timeout",
            "700",
            "--dry-run-command",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    data = payload["data"]
    assert data["market"] == "hk"
    assert data["config_path"] == "config.hk.json"
    assert data["lock_path"] == "/tmp/om-tick-hk.lock"
    assert data["timeout_seconds"] == 700
    assert data["trigger_env"]["OM_TRIGGER_JOB_ID"] == "om-tick-hk"
    assert data["command"] == [
        "./om",
        "run",
        "tick",
        "--config",
        "config.hk.json",
        "--market-config",
        "hk",
        "--accounts",
        "lx",
        "sy",
    ]


def test_legacy_tick_script_entrypoints_are_removed() -> None:
    assert not (ROOT / "scripts" / "send_if_needed.py").exists()
    assert not (ROOT / "scripts" / "send_if_needed_multi.py").exists()


def test_market_session_and_opend_alert_use_single_source_of_truth() -> None:
    from domain.domain import select_markets_to_run
    from src.application.multi_tick.opend_guard import should_send_opend_alert

    cfg = {
        "schedule_hk": {
            "enabled": True,
            "timezone": "Asia/Hong_Kong",
            "run_window": {
                "start": "09:30",
                "end": "16:00",
                "breaks": [
                    {"start": "12:00", "end": "13:00"},
                ],
            },
            "beijing_timezone": "Asia/Shanghai",
        },
        "schedule": {
            "enabled": False,
            "timezone": "America/New_York",
            "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
            "beijing_timezone": "Asia/Shanghai",
        },
    }
    now_utc = datetime(2026, 4, 1, 4, 30, 0, tzinfo=timezone.utc)

    direct_out = select_markets_to_run(now_utc, cfg, "auto")
    assert direct_out == []

    with TemporaryDirectory() as td:
        base = Path(td)
        direct_first = should_send_opend_alert(base, "OPEND_RATE_LIMIT", cooldown_sec=600)
        direct_second = should_send_opend_alert(base, "OPEND_RATE_LIMIT", cooldown_sec=600)
        assert direct_first is True
        assert direct_second is False


def test_multi_tick_main_enforces_canonical_runtime_config_without_derived_gate() -> None:
    src = Path("src/application/multi_account_tick.py").read_text(encoding="utf-8")
    assert "OM_ALLOW_DERIVED_CONFIG" not in src
    assert "resolve_allow_derived_config_gate(" not in src
    assert "allow_derived=" not in src
    contract_src = Path("domain/domain/config_contract.py").read_text(encoding="utf-8")
    assert "resolve_allow_derived_config_gate" not in contract_src


def test_multi_account_tick_current_run_id_accessor_is_public() -> None:
    import importlib

    mod = importlib.import_module("src.application.multi_account_tick")

    old = mod._CURRENT_RUN_ID
    try:
        mod._CURRENT_RUN_ID = "test-run-id"
        assert mod.current_run_id() == "test-run-id"
    finally:
        mod._CURRENT_RUN_ID = old


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


def test_ensure_runtime_schedule_matches_market_rejects_hk_config_with_us_schedule() -> None:
    from domain.domain import ensure_runtime_schedule_matches_market

    with TemporaryDirectory() as td:
        cfg = Path(td) / "config.hk.json"
        runtime_config = {
            "schedule": {
                "enabled": True,
                "timezone": "America/New_York",
                "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
            }
        }

        try:
            ensure_runtime_schedule_matches_market(runtime_config, config_path=cfg, market_config="hk")
            raise AssertionError("expected schedule market guard failure")
        except SystemExit as e:
            msg = str(e)
            assert "runtime schedule timezone does not match market" in msg
            assert "market: hk" in msg
            assert "expected: Asia/Hong_Kong" in msg
            assert "got: America/New_York" in msg


def test_ensure_runtime_schedule_matches_market_accepts_hk_day_schedule() -> None:
    from domain.domain import ensure_runtime_schedule_matches_market

    with TemporaryDirectory() as td:
        cfg = Path(td) / "config.hk.json"
        runtime_config = {
            "schedule": {
                "enabled": True,
                "timezone": "Asia/Hong_Kong",
                "run_window": {
                    "start": "09:30",
                    "end": "16:00",
                    "breaks": [{"start": "12:00", "end": "13:00"}],
                },
            }
        }

        out = ensure_runtime_schedule_matches_market(runtime_config, config_path=cfg, market_config="auto")

    assert out == {
        "market": "hk",
        "schedule_key": "schedule",
        "timezone": "Asia/Hong_Kong",
        "validated": True,
    }


def test_production_tick_entrypoints_enable_sibling_external_guard() -> None:
    cli_src = Path("src/interfaces/cli/main.py").read_text(encoding="utf-8")
    multi_src = Path("src/application/multi_account_tick.py").read_text(encoding="utf-8")

    assert "from src.application.multi_account_tick import run_tick" in cli_src
    assert "return int(run_tick(tick_argv))" in cli_src
    assert 'run_sub.add_parser("tick-cron"' in cli_src
    assert "run_tick_cron(" in cli_src
    assert "require_sibling_external=True" in multi_src
    assert "ensure_runtime_schedule_matches_market(" in multi_src
    assert "build_trigger_context()" in multi_src
    assert "'trigger_context': trigger_context" in multi_src
    assert "config_source_path" in multi_src
