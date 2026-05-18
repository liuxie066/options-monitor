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
            "portfolio": {"data_config": "secrets/portfolio.sqlite.json", "broker": "富途"},
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
        data_config="secrets/portfolio.sqlite.json",
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
        "--no-send",
        "--format",
        "json",
        "--quiet",
    ]]
