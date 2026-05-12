from __future__ import annotations

import json
from pathlib import Path

import pytest


def _repo_with_open_event(tmp_path: Path):
    from domain.domain.option_position_lots import OpenPositionCommand
    from src.application import option_positions_service as svc

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    svc.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )
    event_id = repo.list_trade_events()[0]["event_id"]
    return repo, event_id


def test_trade_events_repair_dry_run_does_not_mutate(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["target_event"]["event_id"] == event_id
    assert out["repair_event"]["strike"] == 500.0
    assert out["projection_preview"]["position_lot_count"] == 1
    assert out["projection_preview"]["projection_diagnostic_count"] == 0
    assert len(repo.list_trade_events()) == 1
    assert repo.list_position_lots()[0]["fields"]["strike"] == 480.0


def test_trade_events_repair_apply_voids_and_replaces_event(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--apply", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "applied"
    assert out["target_event_id"] == event_id
    assert out["void_event_id"].startswith("manual-repair-void-")
    assert out["repair_event_id"].startswith("manual-repair-")
    events = repo.list_trade_events()
    assert len(events) == 3
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["strike"] == 500.0


def test_trade_events_repair_rejects_second_repair(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--apply", "--format", "json"]) == 0
    capsys.readouterr()

    assert cli.main(["repair", event_id, "--strike", "510", "--apply"]) == 2

    out = capsys.readouterr().out
    assert "trade event already voided" in out
    assert len(repo.list_trade_events()) == 3
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["strike"] == 500.0


def test_trade_events_repair_rejects_open_event_with_downstream_close(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from src.application import option_positions_service as svc

    repo, event_id = _repo_with_open_event(tmp_path)
    lot = repo.list_position_lots()[0]
    svc.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--apply"]) == 2

    out = capsys.readouterr().out
    assert "cannot repair an open event with downstream close/adjust dependencies" in out
    assert "explicit_target" in out
    assert repo.list_position_lots()[0]["fields"]["contracts_open"] == 0


def test_trade_events_void_dry_run_includes_projection_preview(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["void", event_id, "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["projection_preview"]["position_lot_count"] == 0
    assert len(repo.list_trade_events()) == 1
    assert len(repo.list_position_lots()) == 1


def test_trade_events_rejects_apply_and_dry_run_together(monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    with pytest.raises(SystemExit, match="--apply and --dry-run are mutually exclusive"):
        cli.main(["repair", event_id, "--strike", "500", "--apply", "--dry-run"])


def test_trade_events_replay_dry_run_reports_projection(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, _event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["replay", "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["trade_event_count"] == 1
    assert out["position_lot_count"] == 1
