from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

import src.application.ledger.bootstrap as ledger_bootstrap
import src.application.ledger.interventions as ledger_interventions
import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository
from src.application.ledger.sync_metadata import PositionLotSyncMetadataPatch
import src.application.ledger.writer as ledger_writer

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _write_data_config(path: Path, *, sqlite_path: Path) -> Path:
    payload = {
        "option_positions": {"sqlite_path": str(sqlite_path)},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_option_positions_cli_events_json(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        ["om option-positions", "--data-config", str(data_config), "events", "--format", "json", "--account", "lx"],
    )

    cli_mod.main()

    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["account"] == "lx"
    assert rows[0]["position_effect"] == "open"
    assert rows[0]["symbol"] == "TSLA"


def test_option_positions_cli_rebuild_reports_summary(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(sys, "argv", ["om option-positions", "--data-config", str(data_config), "rebuild"])

    cli_mod.main()

    out = capsys.readouterr().out
    assert "[DONE] rebuilt canonical position_lots projection" in out
    assert "trade_events=1" in out
    assert "position_lots=1" in out
    assert "diagnostics=0" in out
    assert "unmatched_explicit_close=0" in out


def test_option_positions_cli_rebuild_ignores_deprecated_sqlite_path(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_bootstrap.load_option_positions_repo(data_config)
    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        ["om option-positions", "--data-config", str(data_config), "rebuild", "--format", "json"],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["ledger_store"]["sqlite_path"] == str((tmp_path / "output_shared" / "state" / "option_positions.sqlite3").resolve())
    assert payload["ledger_store"]["legacy_sqlite_path"] == str((tmp_path / "option_positions.sqlite3").resolve())
    assert any("ignored" in item for item in payload["ledger_store"]["warnings"])


def test_option_positions_cli_store_inspect_reports_parallel_sqlite_candidates(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    legacy_db = tmp_path / "legacy" / "option_positions.sqlite3"
    active_db = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=legacy_db)
    for db_path, symbol in ((active_db, "TSLA"), (legacy_db, "NVDA")):
        repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
        ledger_manual_trades.persist_manual_open_event(
            repo,
            OpenPositionCommand(
                broker="富途",
                account="lx",
                symbol=symbol,
                option_type="put",
                side="short",
                contracts=1,
                currency="USD",
                strike=100.0,
                multiplier=100,
                expiration_ymd="2026-06-19",
                premium_per_share=1.23,
                opened_at_ms=1000,
            ),
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["om option-positions", "--data-config", str(data_config), "store", "inspect", "--format", "json"],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["active"]["sqlite_path"] == str(active_db.resolve())
    assert payload["active"]["legacy_sqlite_path"] == str(legacy_db.resolve())
    assert payload["summary"]["multiple_populated"] is True
    assert any("multiple ledger sqlite candidates contain rows" in item for item in payload["warnings"])
    by_path = {item["path"]: item for item in payload["candidates"]}
    assert by_path[str(active_db.resolve())]["is_active"] is True
    assert "legacy_configured_sqlite_path" in by_path[str(legacy_db.resolve())]["roles"]


def test_option_positions_cli_inspect_reports_projection_state(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]
    repo.update_position_lot_sync_metadata(
        lot["record_id"],
        PositionLotSyncMetadataPatch(feishu_record_id="rec_sync_1"),
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "inspect",
            "--feishu-record-id",
            "rec_sync_1",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["matched_record_ids"] == [lot["record_id"]]
    assert payload["ledger_store"]["sqlite_path"] == str((tmp_path / "option_positions.sqlite3").resolve())
    assert payload["ledger_store"]["trade_event_count"] == 1
    assert payload["ledger_store"]["position_lot_count"] == 1
    assert payload["current_lots"][0]["fields"]["feishu_record_id"] == "rec_sync_1"
    assert payload["baseline_snapshot_id"] is None
    assert payload["projected_lots"][0]["current_contracts"] == 1
    assert payload["baseline_lots"] == []
    assert payload["related_events"][0]["event_id"].startswith("manual-open-")


def test_option_positions_cli_inspect_reports_orphan_close_event_diagnostics(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_writer.persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="manual-close-missing-lot",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="sy",
            symbol="0700.HK",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=1.2,
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            currency="HKD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "om option-positions",
                "mode": "manual_close",
                "record_id": "rec_missing",
                "close_target_source_event_id": "open-missing",
                "close_reason": "expired",
            },
        ),
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "inspect",
            "--record-id",
            "rec_missing",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["matched_record_ids"] == []
    assert payload["current_lots"] == []
    assert payload["projected_lots"] == []
    assert payload["related_events"][0]["event_id"] == "manual-close-missing-lot"
    assert payload["projection_diagnostics"][0]["code"] == "target_lot_not_found"


def test_option_positions_cli_reconcile_writes_verification_snapshot_and_report(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )
    snapshot_path = tmp_path / "verify.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "snapshot_id": "verify_cli_1",
                "snapshot_type": "verification",
                "snapshot_at_utc": "2026-05-09T00:00:00+00:00",
                "source_name": "test_cli",
                "lots": [
                    {
                        "account": "lx",
                        "broker": "富途",
                        "symbol": "TSLA",
                        "option_type": "put",
                        "side": "short",
                        "strike": 100,
                        "expiration_ymd": "2026-06-19",
                        "currency": "USD",
                        "multiplier": 100,
                        "contracts": 2,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "reconcile",
            "--snapshot-file",
            str(snapshot_path),
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot_id"] == "verify_cli_1"
    assert payload["source_of_truth"] == "trade_events"
    assert payload["projection"] == "position_lots"
    assert payload["summary"]["quantity_mismatch"] == 1
    assert payload["latest_verification_snapshot_id"] == "verify_cli_1"
    assert payload["verification_snapshot_count"] == 1
    assert payload["accepted_verification_snapshot_count"] == 1
    assert (
        tmp_path
        / "output_shared"
        / "state"
        / "option_positions"
        / "current"
        / "reconciliation.latest.json"
    ).exists()


def test_option_positions_cli_inspect_surfaces_verification_and_reconciliation_state(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]
    snapshot_path = tmp_path / "verify.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "snapshot_id": "verify_cli_2",
                "snapshot_type": "verification",
                "snapshot_at_utc": "2026-05-09T00:00:00+00:00",
                "source_name": "test_cli",
                "lots": [
                    {
                        "account": "lx",
                        "broker": "富途",
                        "symbol": "TSLA",
                        "option_type": "put",
                        "side": "short",
                        "strike": 100,
                        "expiration_ymd": "2026-06-19",
                        "currency": "USD",
                        "multiplier": 100,
                        "contracts": 2,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "reconcile",
            "--snapshot-file",
            str(snapshot_path),
            "--format",
            "json",
        ],
    )
    cli_mod.main()
    capsys.readouterr()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "inspect",
            "--record-id",
            lot["record_id"],
        ],
    )
    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["persisted_baseline_snapshot_id"] is None
    assert payload["projection_checkpoint_snapshot_id"] == "verify_cli_2"
    assert payload["baseline_snapshot_id"] is None
    assert payload["latest_verification_snapshot_id"] == "verify_cli_2"
    assert payload["verification_snapshot_count"] == 1
    assert payload["accepted_verification_snapshot_count"] == 1
    assert payload["projected_lots"][0]["current_contracts"] == 1
    assert payload["latest_reconciliation_summary"]["quantity_mismatch"] == 1
    assert payload["latest_reconciliation_report"]["snapshot_id"] == "verify_cli_2"
    assert payload["latest_reconciliation_report"]["source_of_truth"] == "trade_events"


def test_option_positions_cli_add_dry_run_infers_hkd_currency_from_hk_symbol(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "add",
            "--account",
            "lx",
            "--symbol",
            "0700.HK",
            "--option-type",
            "put",
            "--side",
            "short",
            "--contracts",
            "1",
            "--strike",
            "510",
            "--multiplier",
            "100",
            "--exp",
            "2026-06-29",
            "--dry-run",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    fields = json.loads(out[out.index("{"):])
    assert fields["currency"] == "HKD"


def test_option_positions_cli_add_dry_run_infers_usd_currency_from_us_symbol(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "add",
            "--account",
            "lx",
            "--symbol",
            "PLTR",
            "--option-type",
            "put",
            "--side",
            "short",
            "--contracts",
            "1",
            "--strike",
            "30",
            "--multiplier",
            "100",
            "--exp",
            "2026-05-15",
            "--dry-run",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    fields = json.loads(out[out.index("{"):])
    assert fields["currency"] == "USD"


def test_option_positions_cli_list_filters_by_local_expiration(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    near_exp = (datetime.now().date() + timedelta(days=1)).isoformat()
    far_exp = (datetime.now().date() + timedelta(days=21)).isoformat()
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd=near_exp,
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=110.0,
            multiplier=100,
            expiration_ymd=far_exp,
            premium_per_share=1.5,
            opened_at_ms=2000,
        ),
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "list",
            "--account",
            "lx",
            "--format",
            "json",
            "--exp-within-days",
            "7",
        ],
    )

    cli_mod.main()

    rows = json.loads(capsys.readouterr().out)
    assert [row["symbol"] for row in rows] == ["TSLA"]
    assert rows[0]["expiration_ymd"] == near_exp
    assert rows[0]["strike"] == 100.0
    assert rows[0]["multiplier"] == 100.0


def test_option_positions_cli_buy_close_auto_matches_unique_selector(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=2,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "buy-close",
            "--account",
            "lx",
            "--symbol",
            "0700.HK",
            "--option-type",
            "put",
            "--strike",
            "480",
            "--exp",
            "2026-04-29",
            "--contracts",
            "1",
            "--close-price",
            "1.2",
            "--dry-run",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    lot = repo.list_position_lots()[0]
    assert f"[MATCH] rule=strict_contract_unique record_id={lot['record_id']}" in out
    assert '"contracts_open": 1' in out
    assert repo.get_record_fields(lot["record_id"])["contracts_open"] == 2


def test_option_positions_cli_buy_close_auto_match_lists_multiple_candidates(monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    for opened_at in (1000, 2000):
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
                opened_at_ms=opened_at,
            ),
        )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "buy-close",
            "--account",
            "lx",
            "--symbol",
            "0700.HK",
            "--option-type",
            "put",
            "--strike",
            "480",
            "--exp",
            "2026-04-29",
            "--contracts",
            "1",
            "--close-price",
            "1.2",
            "--dry-run",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    message = str(exc_info.value)
    assert "[MATCH_FAIL] multiple_matches" in message
    for lot in repo.list_position_lots():
        assert lot["record_id"] in message


def test_option_positions_cli_void_event_reports_result(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    open_result = ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        ["om option-positions", "--data-config", str(data_config), "void-event", "--event-id", str(open_result["event_id"])],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    assert f"[DONE] voided event_id={open_result['event_id']}" in out
    assert repo.list_position_lots() == []


def test_option_positions_cli_adjust_lot_dry_run_outputs_patch(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "adjust-lot",
            "--record-id",
            lot["record_id"],
            "--premium-per-share",
            "3.1",
            "--dry-run",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    assert "[DRY_RUN] adjust fields:" in out
    assert '"premium": 3.1' in out


def test_option_positions_cli_history_json_includes_related_events(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.0,
        close_reason="manual_buy_to_close",
        as_of_ms=1500,
    )
    adjust_result = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        record_id=lot["record_id"],
        fields=repo.get_position_lot_fields(lot["record_id"]),
        premium_per_share=3.1,
        as_of_ms=2000,
    )
    ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(close_result["event_id"]),
        void_reason="close_was_wrong",
        as_of_ms=2500,
    )
    ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(adjust_result["event_id"]),
        void_reason="adjust_was_wrong",
        as_of_ms=2600,
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        ["om option-positions", "--data-config", str(data_config), "history", "--record-id", lot["record_id"], "--format", "json"],
    )

    cli_mod.main()

    rows = json.loads(capsys.readouterr().out)
    event_ids = [row["event_id"] for row in rows]
    effects = [row["position_effect"] for row in rows]
    assert len(rows) == 5
    assert effects == ["open", "close", "adjust", "void", "void"]
    assert event_ids[0].startswith("manual-open-")
    assert event_ids[1].startswith("manual-close-")
    assert event_ids[2].startswith("manual-adjust-")
    assert rows[3]["void_target_event_id"] == close_result["event_id"]
    assert rows[4]["void_target_event_id"] == adjust_result["event_id"]


def test_option_positions_cli_report_monthly_income_json(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    import src.application.ledger.read_model as read_model
    from domain.domain.option_position_lots import OpenPositionCommand, parse_exp_to_ms

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]

    opened_at = parse_exp_to_ms("2026-04-03")
    assert opened_at is not None
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=opened_at,
        ),
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(read_model, "get_exchange_rates_or_fetch_latest", lambda **_kwargs: {"rates": {"USDCNY": 7.2}})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "report",
            "monthly-income",
            "--broker",
            "富途",
            "--account",
            "lx",
            "--month",
            "2026-04",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["filters"]["account"] == "lx"
    assert payload["filters"]["broker"] == "富途"
    assert payload["filters"]["month"] == "2026-04"
    assert len(payload["summary"]) == 1
    row = payload["summary"][0]
    assert {key: row.get(key) for key in {
        "month",
        "account",
        "currency",
        "net_cashflow_gross",
        "realized_pnl_gross",
        "open_basis_lifecycle_pnl_gross",
        "realized_gross",
        "premium_received_gross",
        "premium_received_gross_cny",
        "closed_contracts",
        "positions",
        "premium_contracts",
        "premium_positions",
    }} == {
        "month": "2026-04",
        "account": "lx",
        "currency": "USD",
        "net_cashflow_gross": 250.0,
        "realized_pnl_gross": 0.0,
        "open_basis_lifecycle_pnl_gross": 250.0,
        "realized_gross": 0.0,
        "premium_received_gross": 250.0,
        "premium_received_gross_cny": 1800.0,
        "closed_contracts": 0,
        "positions": 0,
        "premium_contracts": 1,
        "premium_positions": 1,
    }


def test_option_positions_cli_report_monthly_income_text(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    import src.application.ledger.read_model as read_model

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")

    class _EmptyRepo:
        def list_records(self, *, page_size: int = 500):
            return []

        def list_position_lots(self):
            return []

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, _EmptyRepo()))
    monkeypatch.setattr(read_model, "get_exchange_rates_or_fetch_latest", lambda **_kwargs: {"rates": {}})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "report",
            "monthly-income",
            "--account",
            "lx",
            "--month",
            "2026-04",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    assert "# Position Lots Monthly Income" in out
    assert "filters: month=2026-04 | account=lx | broker=富途" in out
    assert "| - | - | - | - | - | - | - | 0 | 0 | 0 | 0 |" in out
