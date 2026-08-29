from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class _FakeRunlog:
    run_id = "run-auto-1"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        payload = {"step": step, "status": status}
        payload.update(kwargs)
        self.events.append(payload)


def test_run_auto_close_expired_processes_config_accounts_and_writes_run_state(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import auto_close as mod

    base = tmp_path / "repo"
    base.mkdir()
    cfg_path = base / "config.hk.json"
    cfg_path.write_text("{}", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        mod,
        "load_config",
        lambda **_kwargs: {
            "accounts": ["lx", "sy"],
            "portfolio": {"data_config": "portfolio.runtime.json", "broker": "富途"},
            "option_positions": {"auto_close": {"enabled": True}},
        },
    )

    def _run_maintenance(**kwargs):
        calls.append(dict(kwargs))
        return {
            "mode": "applied",
            "account": kwargs["account"],
            "broker": "富途",
            "positions_checked": 1,
            "candidates_should_close": 1,
            "applied_closed": 1,
            "skipped_already_closed": 0,
            "errors": [],
            "applied": [{"record_id": f"rec_{kwargs['account']}"}],
            "summary_text": "Auto-close expired positions (grace_days=1)\napplied_closed: 1\nERRORS: 0",
        }

    monkeypatch.setattr(mod, "run_expired_position_maintenance_for_account", _run_maintenance)
    monkeypatch.setattr(
        mod,
        "safe_send_auto_close_receipt",
        lambda **_kwargs: {"status": "sent", "delivery_confirmed": True, "message_id": "msg-1"},
    )

    runlog = _FakeRunlog()
    result = mod.run_auto_close_expired(
        base=base,
        config_path=cfg_path,
        data_config=None,
        accounts=[],
        broker=None,
        apply_mode=True,
        no_send=False,
        as_of_ms=1777766400000,
        runlog=runlog,  # type: ignore[arg-type]
    )

    assert result["status"] == "applied"
    assert result["summary"]["accounts"] == 2
    assert result["summary"]["applied_closed"] == 2
    assert [call["account"] for call in calls] == ["lx", "sy"]
    assert all(call["dry_run"] is False for call in calls)
    assert calls[0]["cfg"]["portfolio"]["account"] == "lx"
    assert calls[0]["cfg"]["option_positions"]["auto_close"]["enabled"] is True
    assert (base / "output_runs" / "run-auto-1" / "accounts" / "lx" / "state" / "expired_position_maintenance.json").exists()
    assert (base / "output_runs" / "run-auto-1" / "accounts" / "sy" / "state" / "expired_position_maintenance.json").exists()
    assert (base / "output_shared" / "state" / "last_run_dir.txt").read_text(encoding="utf-8").strip().endswith("run-auto-1")
    shared = json.loads((base / "output_shared" / "state" / "auto_close_expired.json").read_text(encoding="utf-8"))
    assert shared["schema_kind"] == "option_positions_auto_close_expired_run"
    assert shared["summary"]["applied_closed"] == 2


def test_run_auto_close_expired_no_send_dry_run_attaches_skipped_receipt(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import auto_close as mod

    base = tmp_path / "repo"
    base.mkdir()
    calls: list[dict[str, Any]] = []

    def _run_maintenance(**kwargs):
        calls.append(dict(kwargs))
        return {
            "mode": "dry_run",
            "account": kwargs["account"],
            "positions_checked": 1,
            "candidates_should_close": 1,
            "applied_closed": 0,
            "skipped_already_closed": 0,
            "errors": [],
            "applied": [],
            "summary_text": "Auto-close expired positions (grace_days=1)\ncandidates_should_close: 1\nERRORS: 0",
        }

    monkeypatch.setattr(mod, "run_expired_position_maintenance_for_account", _run_maintenance)
    monkeypatch.setattr(
        mod,
        "safe_send_auto_close_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no-send must not send receipt")),
    )

    result = mod.run_auto_close_expired(
        base=base,
        config_path=None,
        data_config="portfolio.runtime.json",
        accounts=["lx"],
        broker="富途",
        apply_mode=False,
        no_send=True,
        runlog=_FakeRunlog(),  # type: ignore[arg-type]
    )

    assert result["status"] == "dry_run"
    assert calls[0]["dry_run"] is True
    assert calls[0]["send_receipt"] is False
    receipt = result["account_results"][0]["result"]["receipt"]
    assert receipt["status"] == "skipped"
    assert receipt["reason"] == "skipped_no_send"


def test_run_auto_close_expired_reports_projection_refresh_failure(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import auto_close as mod
    from src.application.positions import maintenance

    base = tmp_path / "repo"
    base.mkdir()
    config_path = base / "config.hk.json"
    config_path.write_text("{}", encoding="utf-8")
    data_config = base / "portfolio.runtime.json"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(base / "positions.sqlite3")}}),
        encoding="utf-8",
    )

    class FakeRepo:
        def count_trade_events(self) -> int:
            return 1

    monkeypatch.setattr(
        mod,
        "load_config",
        lambda **_kwargs: {
            "accounts": ["lx"],
            "portfolio": {"data_config": str(data_config), "broker": "富途"},
            "option_positions": {"auto_close": {"enabled": True}},
        },
    )
    monkeypatch.setattr(maintenance, "open_position_ledger", lambda *_args, **_kwargs: FakeRepo())
    monkeypatch.setattr(
        maintenance,
        "refresh_position_lot_projection",
        lambda _repo: (_ for _ in ()).throw(RuntimeError("locking protocol")),
    )
    monkeypatch.setattr(
        maintenance,
        "_load_expiry_close_position_lots",
        lambda _repo: (_ for _ in ()).throw(AssertionError("failed refresh must stop before lot load")),
    )

    result = mod.run_auto_close_expired(
        base=base,
        config_path=config_path,
        data_config=None,
        accounts=[],
        broker=None,
        apply_mode=True,
        no_send=True,
        runlog=_FakeRunlog(),  # type: ignore[arg-type]
    )

    account_result = result["account_results"][0]["result"]
    assert result["status"] == "failed"
    assert account_result["mode"] == "error"
    assert account_result["positions_checked"] == 0
    assert "projection refresh failed before auto-close: locking protocol" in account_result["errors"][0]
    assert account_result["receipt"]["reason"] == "skipped_no_send"


def test_auto_close_expired_main_writes_runtime_outputs_outside_release(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import auto_close as mod

    release = tmp_path / "releases" / "1.2.157"
    runtime = tmp_path / "runtime"
    cfg_path = runtime / "config.hk.json"
    runtime.mkdir(parents=True)
    cfg_path.write_text("{}", encoding="utf-8")
    calls: list[dict[str, Any]] = []

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    monkeypatch.setattr(mod, "__file__", str(release / "src" / "application" / "positions" / "auto_close.py"))
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda **_kwargs: {
            "accounts": ["lx"],
            "portfolio": {"data_config": "portfolio.runtime.json", "broker": "富途"},
            "option_positions": {"auto_close": {"enabled": True}},
        },
    )

    def _run_maintenance(**kwargs):
        calls.append(dict(kwargs))
        return {
            "mode": "applied",
            "account": kwargs["account"],
            "broker": "富途",
            "positions_checked": 0,
            "candidates_should_close": 0,
            "applied_closed": 0,
            "skipped_already_closed": 0,
            "errors": [],
            "applied": [],
            "summary_text": "",
        }

    monkeypatch.setattr(mod, "run_expired_position_maintenance_for_account", _run_maintenance)
    monkeypatch.setattr(
        mod,
        "safe_send_auto_close_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("--no-send must not send receipt")),
    )

    rc = mod.main([
        "--config",
        str(cfg_path),
        "--accounts",
        "lx",
        "--apply",
        "--yes",
        "--no-send",
        "--quiet",
    ])

    assert rc == 0
    assert calls and calls[0]["base"] == runtime.resolve()
    assert (runtime / "audit" / "run_logs").is_dir()
    assert (runtime / "output_runs").is_dir()
    assert (runtime / "output_shared" / "state" / "auto_close_expired.json").exists()
    assert not (release / "audit").exists()
    assert not (release / "output_runs").exists()
    assert not (release / "output_shared").exists()


def test_auto_close_expired_main_accepts_explicit_runtime_root(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import auto_close as mod

    release = tmp_path / "releases" / "1.2.158"
    runtime = tmp_path / "runtime"
    cfg_path = runtime / "config.hk.json"
    runtime.mkdir(parents=True)
    cfg_path.write_text("{}", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    lock_modes: list[int] = []

    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(mod.fcntl, "flock", lambda _fd, mode: lock_modes.append(mode))
    monkeypatch.setattr(mod, "__file__", str(release / "src" / "application" / "positions" / "auto_close.py"))
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda **_kwargs: {
            "accounts": ["lx"],
            "portfolio": {"data_config": "portfolio.runtime.json", "broker": "富途"},
            "option_positions": {"auto_close": {"enabled": True}},
        },
    )

    def _run_maintenance(**kwargs):
        calls.append(dict(kwargs))
        return {
            "mode": "applied",
            "account": kwargs["account"],
            "broker": "富途",
            "positions_checked": 0,
            "candidates_should_close": 0,
            "applied_closed": 0,
            "skipped_already_closed": 0,
            "errors": [],
            "applied": [],
            "summary_text": "",
        }

    monkeypatch.setattr(mod, "run_expired_position_maintenance_for_account", _run_maintenance)
    monkeypatch.setattr(
        mod,
        "safe_send_auto_close_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("--no-send must not send receipt")),
    )

    rc = mod.main([
        "--config",
        str(cfg_path),
        "--accounts",
        "lx",
        "--apply",
        "--yes",
        "--no-send",
        "--quiet",
        "--runtime-root",
        str(runtime),
    ])

    assert rc == 0
    assert calls and calls[0]["base"] == runtime.resolve()
    assert lock_modes == [mod.fcntl.LOCK_EX]
    assert (runtime / "locks" / "auto-close-expired.lock").exists()
    assert (runtime / "audit" / "run_logs").is_dir()
    assert (runtime / "output_runs").is_dir()
    assert (runtime / "output_shared" / "state" / "auto_close_expired.json").exists()
    assert not (release / "audit").exists()
    assert not (release / "output_runs").exists()
    assert not (release / "output_shared").exists()


def test_auto_close_expired_main_returns_failed_for_missing_data_config(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import auto_close as mod

    runtime = tmp_path / "runtime"
    cfg_path = runtime / "config.hk.json"
    runtime.mkdir(parents=True)
    cfg_path.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda **_kwargs: {
            "accounts": ["lx"],
            "portfolio": {"data_config": "missing.runtime.json", "broker": "富途"},
            "option_positions": {"auto_close": {"enabled": True}},
        },
    )

    rc = mod.main([
        "--config",
        str(cfg_path),
        "--accounts",
        "lx",
        "--apply",
        "--yes",
        "--no-send",
        "--quiet",
    ])

    payload = json.loads((runtime / "output_shared" / "state" / "auto_close_expired.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["status"] == "failed"
    account_result = payload["account_results"][0]["result"]
    assert account_result["mode"] == "error"
    assert account_result["reason"] == "missing_data_config"
    assert account_result["errors"]


def test_option_positions_cli_dispatches_auto_close_expired(monkeypatch) -> None:
    from src.interfaces.cli import option_positions as cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "run_option_positions_auto_close", lambda argv: calls.append(list(argv)) or 0)

    rc = cli.main([
        "auto-close-expired",
        "--config",
        "config.hk.json",
        "--accounts",
        "lx",
        "sy",
        "--apply",
        "--yes",
        "--no-send",
        "--quiet",
    ])

    assert rc == 0
    assert calls == [[
        "--config",
        "config.hk.json",
        "--accounts",
        "lx",
        "sy",
        "--apply",
        "--yes",
        "--no-send",
        "--format",
        "json",
        "--quiet",
    ]]


def test_option_positions_cli_passes_auto_close_runtime_root(monkeypatch, tmp_path: Path) -> None:
    from src.interfaces.cli import option_positions as cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "run_option_positions_auto_close", lambda argv: calls.append(list(argv)) or 0)

    runtime = tmp_path / "runtime"
    rc = cli.main([
        "--runtime-root",
        str(runtime),
        "auto-close-expired",
        "--config",
        "config.hk.json",
        "--accounts",
        "lx",
        "--apply",
        "--yes",
        "--quiet",
    ])

    assert rc == 0
    assert calls == [[
        "--config",
        "config.hk.json",
        "--runtime-root",
        str(runtime),
        "--accounts",
        "lx",
        "--apply",
        "--yes",
        "--format",
        "json",
        "--quiet",
    ]]


def test_option_positions_cli_passes_auto_close_subcommand_runtime_root(monkeypatch, tmp_path: Path) -> None:
    from src.interfaces.cli import option_positions as cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "run_option_positions_auto_close", lambda argv: calls.append(list(argv)) or 0)

    runtime = tmp_path / "runtime"
    rc = cli.main([
        "auto-close-expired",
        "--config",
        "config.hk.json",
        "--runtime-root",
        str(runtime),
        "--accounts",
        "lx",
        "--apply",
        "--yes",
        "--quiet",
    ])

    assert rc == 0
    assert calls == [[
        "--config",
        "config.hk.json",
        "--runtime-root",
        str(runtime),
        "--accounts",
        "lx",
        "--apply",
        "--yes",
        "--format",
        "json",
        "--quiet",
    ]]
