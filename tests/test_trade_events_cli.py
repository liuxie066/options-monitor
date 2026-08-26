from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest  # pyright: ignore[reportMissingImports]

import src.application.ledger.bootstrap as ledger_bootstrap
import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository


def _repo_with_open_event(tmp_path: Path):
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
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


def _append_canonical_void_event(
    repo,
    *,
    target_event_id: str,
    event_id: str,
    event_time_ms: int,
) -> None:
    from domain.domain.ledger import TradeEvent
    from src.application.ledger.event_codec import stored_trade_event_to_ledger_event

    target_payload = next(
        item
        for item in repo.list_trade_events()
        if str(item.get("event_id") or "").strip() == str(target_event_id).strip()
    )
    target, diagnostics = stored_trade_event_to_ledger_event(target_payload)
    assert target is not None
    assert [item.code for item in diagnostics if item.severity == "error"] == []
    repo.upsert_trade_event(
        TradeEvent(
            event_id=event_id,
            event_type="void",
            event_time_ms=event_time_ms,
            contract_key=target.contract_key,
            contracts=0,
            price=0.0,
            currency=target.currency,
            source="test",
            multiplier=target.multiplier,
            target_event_id=target_event_id,
            raw_payload={},
        )
    )


def _insert_invalid_legacy_void_event(
    repo,
    *,
    target_event_id: str,
    event_id: str,
    event_time_ms: int,
) -> None:
    payload = {
        "event_id": event_id,
        "trade_time_ms": event_time_ms,
        "position_effect": "void",
        "raw_payload": {"void_target_event_id": target_event_id},
    }
    with sqlite3.connect(str(repo.db_path)) as conn:
        # This fixture deliberately represents a row written before the
        # pagination schema and its canonical write guards existed.
        for trigger_name in ledger_repository.TRADE_EVENT_PAGINATION_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        conn.execute(
            """
            INSERT INTO trade_events (event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, json.dumps(payload, ensure_ascii=False), event_time_ms, event_time_ms, event_time_ms),
        )


def test_trade_events_list_text_shows_trade_time_beijing(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, _event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["list", "--format", "text"]) == 0

    out = capsys.readouterr().out
    assert "time 1970-01-01 08:00:01 北京时间" in out


def test_trade_events_fee_sync_defaults_to_dry_run(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    config_path = tmp_path / "config.us.json"
    data_config = tmp_path / "data.json"
    calls: list[dict] = []

    class _Client:
        def __init__(self, **kwargs):
            calls.append({"client": kwargs})

        def close(self):
            calls.append({"closed": True})

    monkeypatch.setattr(cli, "load_runtime_config", lambda **_kwargs: (config_path, {"accounts": ["lx"]}))
    monkeypatch.setattr(cli, "resolve_position_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        cli,
        "resolve_account_broker_binding_sets",
        lambda _values: {
            "lx": SimpleNamespace(
                ok=True,
                host="127.0.0.1",
                port=11111,
                trd_env="REAL",
                required_account_ids=("123",),
            )
        },
    )
    monkeypatch.setattr(cli, "OpenDHistoryDealClient", _Client)

    def _sync(_repo, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"sync": kwargs})
        return {
            "selected_order_count": 0,
            "actual_observation_count": 0,
            "reason_counts": {},
            "migration": {"status_counts": {}},
        }

    monkeypatch.setattr(cli, "sync_order_fees", _sync)

    result = cli.main(
        [
            "fees-sync",
            "--config-key",
            "us",
            "--account",
            "lx",
            "--start-date",
            "2026-08-01",
            "--end-date",
            "2026-08-23",
            "--format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["dry_run"] is True
    assert payload["write_applied"] is False
    assert next(item["sync"] for item in calls if "sync" in item)["apply"] is False
    assert next(item["sync"] for item in calls if "sync" in item)[
        "allowed_futu_account_ids"
    ] == ("123",)
    assert calls[-1] == {"closed": True}


def test_trade_events_list_treats_canonical_void_as_voided(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    _append_canonical_void_event(
        repo,
        target_event_id=event_id,
        event_id="canonical-void-open",
        event_time_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["list", "--status", "voided", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert [item["event_id"] for item in out] == [event_id]


def test_trade_events_list_ignores_invalid_void_when_filtering_active(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    _insert_invalid_legacy_void_event(
        repo,
        target_event_id=event_id,
        event_id="invalid-legacy-void-open",
        event_time_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["list", "--status", "active", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert [item["event_id"] for item in out] == [event_id]


def test_trade_events_show_json_includes_trade_time_beijing(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["show", event_id, "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["event"]["trade_time_ms"] == 1000
    assert out["event"]["trade_time_beijing"] == "1970-01-01 08:00:01 北京时间"


def test_trade_events_repair_dry_run_does_not_mutate(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert out["backup_path"] is None
    assert out["audit_id"].startswith("audit_")
    assert out["target_event"]["event_id"] == event_id
    assert out["repair_event"]["contract_key"]["strike"] == 500.0
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["ledger_preflight"]["event_type"] == "repair"
    assert out["ledger_preflight"]["target_event_id"] == event_id
    assert out["projection_preview"]["position_lot_count"] == 1
    assert out["projection_preview"]["projection_diagnostic_count"] == 0
    assert len(repo.list_trade_events()) == 1
    assert repo.list_position_lots()[0]["fields"]["strike"] == 480.0


def test_trade_events_repair_apply_voids_and_replaces_event(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--confirm", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "applied"
    assert out["dry_run"] is False
    assert out["write_applied"] is True
    assert out["backup_path"] is None
    assert out["audit_id"].startswith("audit_")
    assert out["rollback_hint"]
    assert out["target_event_id"] == event_id
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["ledger_preflight"]["event_type"] == "repair"
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

    assert cli.main(["repair", event_id, "--strike", "500", "--confirm", "--format", "json"]) == 0
    capsys.readouterr()

    assert cli.main(["repair", event_id, "--strike", "510", "--confirm"]) == 2

    out = capsys.readouterr().out
    assert "trade event already voided" in out
    assert len(repo.list_trade_events()) == 3
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["strike"] == 500.0


def test_trade_events_repair_rejects_canonical_void_without_legacy_payload(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    _append_canonical_void_event(
        repo,
        target_event_id=event_id,
        event_id="canonical-void-open",
        event_time_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--dry-run"]) == 2

    out = capsys.readouterr().out
    assert "trade event already voided" in out
    assert "canonical-void-open" in out
    assert len(repo.list_trade_events()) == 2


def test_trade_events_repair_rejects_open_event_with_downstream_close(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    lot = repo.list_position_lots()[0]
    ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--confirm"]) == 2

    out = capsys.readouterr().out
    assert "cannot repair an open event with downstream close/adjust dependencies" in out
    assert "explicit_target" in out
    assert repo.list_position_lots()[0]["fields"]["contracts_open"] == 0


def test_trade_events_repair_close_record_id_updates_canonical_target_lot(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand

    repo, _event_id = _repo_with_open_event(tmp_path)
    ledger_manual_trades.persist_manual_open_event(
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
            premium_per_share=4.0,
            opened_at_ms=1100,
        ),
    )
    first_lot, second_lot = repo.list_position_lots()
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=first_lot["record_id"],
        fields=first_lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main([
        "repair",
        str(close_result.event_id),
        "--record-id",
        second_lot["record_id"],
        "--close-target-source-event-id",
        second_lot["fields"]["source_event_id"],
        "--dry-run",
        "--format",
        "json",
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["repair_event"]["target_lot_id"] == second_lot["record_id"]
    assert out["repair_event"]["raw_payload"]["record_id"] == second_lot["record_id"]
    assert out["projection_preview"]["projection_diagnostic_count"] == 0


def test_trade_events_repair_allows_open_when_downstream_close_was_canonical_voided(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    lot = repo.list_position_lots()[0]
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    _append_canonical_void_event(
        repo,
        target_event_id=str(close_result.event_id),
        event_id="canonical-void-close",
        event_time_ms=3000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["repair_event"]["contract_key"]["strike"] == 500.0
    assert len(repo.list_trade_events()) == 3


def test_trade_events_void_dry_run_includes_projection_preview(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["void", event_id, "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["ledger_preflight"]["event_type"] == "void"
    assert out["ledger_preflight"]["target_event_id"] == event_id
    assert out["projection_preview"]["position_lot_count"] == 0
    assert len(repo.list_trade_events()) == 1
    assert len(repo.list_position_lots()) == 1


def test_trade_events_rejects_apply_and_dry_run_together(monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    with pytest.raises(SystemExit, match="--dry-run cannot be combined"):
        cli.main(["repair", event_id, "--strike", "500", "--apply", "--dry-run"])


def test_trade_events_repair_apply_alone_requires_confirm(monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    with pytest.raises(SystemExit, match="use --confirm or --yes"):
        cli.main(["repair", event_id, "--strike", "500", "--apply"])

    assert len(repo.list_trade_events()) == 1


def test_trade_events_replay_dry_run_reports_projection(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, _event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["replay", "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["trade_event_count"] == 1
    assert out["position_lot_count"] == 1
    assert out["ledger_store"]["sqlite_path"] == str((tmp_path / "option_positions.sqlite3").resolve())
    assert out["ledger_store"]["trade_event_count"] == 1
    assert out["ledger_store"]["position_lot_count"] == 1


def test_trade_events_replay_apply_ignores_deprecated_sqlite_path(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "option_positions.sqlite3")}}),
        encoding="utf-8",
    )
    repo = ledger_bootstrap.load_option_positions_repo(data_config)
    ledger_manual_trades.persist_manual_open_event(
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
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))

    assert cli.main(["replay", "--apply", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "applied"
    assert out["ledger_store"]["sqlite_path"] == str((tmp_path / "output_shared" / "state" / "option_positions.sqlite3").resolve())
    assert "legacy_sqlite_path" not in out["ledger_store"]
    assert out["ledger_store"]["warnings"] == []


def test_trade_events_replay_accepts_explicit_runtime_root(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = tmp_path / "release" / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("{}", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    )
    ledger_manual_trades.persist_manual_open_event(
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

    assert cli.main([
        "--data-config",
        str(data_config),
        "replay",
        "--runtime-root",
        str(runtime_root),
        "--dry-run",
        "--format",
        "json",
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["trade_event_count"] == 1
    assert out["ledger_store"]["runtime_root"] == str(runtime_root.resolve())
    assert out["ledger_store"]["runtime_root_source"] == "argument"


def test_trade_events_repair_apply_outputs_explicit_runtime_root_store(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = tmp_path / "release" / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("{}", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    )
    ledger_manual_trades.persist_manual_open_event(
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
    event_id = str(repo.list_trade_events()[0]["event_id"])

    assert cli.main([
        "--data-config",
        str(data_config),
        "repair",
        "--runtime-root",
        str(runtime_root),
        event_id,
        "--strike",
        "500",
        "--confirm",
        "--format",
        "json",
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "applied"
    assert out["ledger_store"]["sqlite_path"] == str(
        (runtime_root / "output_shared" / "state" / "option_positions.sqlite3").resolve()
    )
    assert out["ledger_store"]["runtime_root"] == str(runtime_root.resolve())
    assert out["ledger_store"]["runtime_root_source"] == "argument"
