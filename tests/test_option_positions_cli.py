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

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
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

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
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
    monkeypatch.setattr(sys, "argv", ["om option-positions", "--data-config", str(data_config), "rebuild", "--apply"])

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
    assert "legacy_sqlite_path" not in payload["ledger_store"]
    assert payload["ledger_store"]["warnings"] == []


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
    assert "legacy_sqlite_path" not in payload["active"]
    assert payload["summary"]["multiple_populated"] is False
    assert payload["warnings"] == []
    by_path = {item["path"]: item for item in payload["candidates"]}
    assert by_path[str(active_db.resolve())]["is_active"] is True
    assert str(legacy_db.resolve()) not in by_path


def test_option_positions_cli_inspect_reports_projection_state(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
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
            lot["record_id"],
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["matched_record_ids"] == [lot["record_id"]]
    assert payload["ledger_store"]["sqlite_path"] == str((tmp_path / "output_shared" / "state" / "option_positions.sqlite3").resolve())
    assert payload["ledger_store"]["trade_event_count"] == 1
    assert payload["ledger_store"]["position_lot_count"] == 1
    assert payload["projection_verify_checkpoint_id"] is None
    assert payload["projected_lots"][0]["current_contracts"] == 1
    assert payload["baseline_lots"] == []
    assert payload["latest_projection_verify_report"] is None
    assert payload["latest_projection_verify_summary"] == {}
    assert payload["related_events"][0]["event_id"].startswith("manual-open-")


def test_option_positions_cli_parent_runtime_root_survives_inspect_subparser(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    def _fake_resolve(**kwargs: object) -> tuple[Path, object]:
        captured.update(kwargs)
        return data_config, repo

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", _fake_resolve)

    rc = cli_mod.main(
        [
            "--data-config",
            str(data_config),
            "--runtime-root",
            str(runtime_root),
            "inspect",
            "--record-id",
            "missing-lot",
        ]
    )

    assert rc == 0
    json.loads(capsys.readouterr().out)
    assert captured["runtime_root"] == str(runtime_root)


def test_option_positions_cli_inspect_accepts_subcommand_runtime_root(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    def _fake_resolve(**kwargs: object) -> tuple[Path, object]:
        captured.update(kwargs)
        return data_config, repo

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", _fake_resolve)

    rc = cli_mod.main(
        [
            "--data-config",
            str(data_config),
            "inspect",
            "--runtime-root",
            str(runtime_root),
            "--record-id",
            "missing-lot",
        ]
    )

    assert rc == 0
    json.loads(capsys.readouterr().out)
    assert captured["runtime_root"] == str(runtime_root)


def test_option_positions_cli_inspect_reports_orphan_close_event_diagnostics(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
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


def test_option_positions_cli_verify_projection_writes_report_and_checkpoint(monkeypatch, tmp_path: Path, capsys) -> None:
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
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "verify-projection",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["source_of_truth"] == "trade_events"
    assert payload["projection"] == "position_lots"
    assert payload["mode_used"] == "full_replay"
    assert payload["summary"]["matched"] == 1
    assert (
        tmp_path
        / "output_shared"
        / "state"
        / "option_positions"
        / "current"
        / "projection_verify.latest.json"
    ).exists()
    assert (
        tmp_path
        / "output_shared"
        / "state"
        / "option_positions"
        / "current"
        / "projection_verify.checkpoint.json"
    ).exists()

    cli_mod.main()
    reused = json.loads(capsys.readouterr().out)
    assert reused["ok"] is True
    assert reused["mode_used"] == "checkpoint_reuse"
    assert reused["checkpoint_reused"] is True


def test_option_positions_cli_inspect_surfaces_projection_verify_state(monkeypatch, tmp_path: Path, capsys) -> None:
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
    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "verify-projection",
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
    assert payload["projected_lots"][0]["current_contracts"] == 1
    assert payload["projection_verify_checkpoint_id"]
    assert payload["latest_projection_verify_summary"]["matched"] == 1
    assert payload["latest_projection_verify_report"]["source_of_truth"] == "trade_events"


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
            "--premium-per-share",
            "1.235",
            "--dry-run",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    fields = json.loads(out[out.index("{"):])
    assert fields["currency"] == "HKD"
    assert fields["premium"] == 1.235


def test_option_positions_cli_add_dry_run_accepts_strategy_snapshot(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    group_id = "combo_yield:lx:combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP"

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    cli_mod.main([
        "--data-config",
        str(data_config),
        "add",
        "--account",
        "lx",
        "--symbol",
        "PDD",
        "--option-type",
        "put",
        "--side",
        "short",
        "--contracts",
        "1",
        "--strike",
        "80",
        "--multiplier",
        "100",
        "--exp",
        "2026-08-21",
        "--premium-per-share",
        "1.0",
        "--strategy-snapshot-json",
        json.dumps({
            "strategy": "combo_yield",
            "leg_role": "sell_put",
            "strategy_group_id": group_id,
            "expiry_structure": "diagonal",
        }),
        "--dry-run",
        "--format",
        "json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert payload["fields"]["strategy"] == "combo_yield"
    assert payload["fields"]["strategy_group_id"] == group_id
    assert payload["fields"]["strategy_snapshot"]["expiry_structure"] == "diagonal"


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
            "--premium-per-share",
            "1.235",
            "--dry-run",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    fields = json.loads(out[out.index("{"):])
    assert fields["currency"] == "USD"
    assert fields["premium"] == 1.235


def test_option_positions_cli_add_apply_alone_requires_confirm() -> None:
    import src.interfaces.cli.option_positions as cli_mod

    with pytest.raises(SystemExit, match="use --confirm or --yes"):
        cli_mod.main([
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
            "--premium-per-share",
            "1.235",
            "--apply",
        ])


def test_option_positions_cli_add_confirm_json_outputs_write_contract(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))

    cli_mod.main([
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
        "--premium-per-share",
        "1.235",
        "--confirm",
        "--format",
        "json",
    ])

    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is False
    assert out["write_applied"] is True
    assert out["backup_path"] is None
    assert out["audit_id"].startswith("audit_")
    assert out["rollback_hint"]
    assert out["result"]["event_id"]
    assert repo.count_trade_events() == 1


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


def test_option_positions_cli_assign_confirm_writes_assignment_event(monkeypatch, tmp_path: Path, capsys) -> None:
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
            symbol="TIGR",
            option_type="put",
            side="short",
            contracts=10,
            currency="USD",
            strike=6.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=0.15,
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
            "assign",
            "--account",
            "lx",
            "--symbol",
            "TIGR",
            "--option-type",
            "put",
            "--strike",
            "6",
            "--exp",
            "2026-05-22",
            "--contracts",
            "10",
            "--stock-side",
            "buy",
            "--stock-qty",
            "1000",
            "--stock-price",
            "6",
            "--confirm",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "manual_assignment"
    assert payload["mode"] == "applied"
    assert payload["write_applied"] is True
    events = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"]
    assert len(events) == 1
    assert events[0]["raw_payload"]["stock_settlement"]["shares"] == 1000
    assert events[0]["raw_payload"]["stock_settlement"]["side"] == "buy"
    assert events[0]["raw_payload"]["close_type"] == "assignment"


def test_option_positions_cli_assign_rejects_wrong_stock_side(monkeypatch, tmp_path: Path) -> None:
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
            symbol="TIGR",
            option_type="put",
            side="short",
            contracts=10,
            currency="USD",
            strike=6.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=0.15,
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
            "assign",
            "--account",
            "lx",
            "--symbol",
            "TIGR",
            "--option-type",
            "put",
            "--strike",
            "6",
            "--exp",
            "2026-05-22",
            "--contracts",
            "10",
            "--stock-side",
            "sell",
            "--stock-qty",
            "1000",
            "--stock-price",
            "6",
            "--dry-run",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    assert "manual assignment stock side must be buy" in str(exc_info.value)
    assert [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"] == []


def test_option_positions_cli_exercise_confirm_writes_exercise_event(monkeypatch, tmp_path: Path, capsys) -> None:
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
            symbol="AAPL",
            option_type="call",
            side="long",
            contracts=2,
            currency="USD",
            strike=200.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=1.5,
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
            "exercise",
            "--account",
            "lx",
            "--symbol",
            "AAPL",
            "--option-type",
            "call",
            "--strike",
            "200",
            "--exp",
            "2026-05-22",
            "--contracts",
            "2",
            "--stock-side",
            "buy",
            "--stock-qty",
            "200",
            "--stock-price",
            "200",
            "--confirm",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "manual_exercise"
    assert payload["mode"] == "applied"
    assert payload["write_applied"] is True
    events = [item for item in repo.list_trade_events() if item.get("event_type") == "exercise"]
    assert len(events) == 1
    assert events[0]["raw_payload"]["stock_settlement"]["shares"] == 200
    assert events[0]["raw_payload"]["stock_settlement"]["side"] == "buy"
    assert events[0]["raw_payload"]["close_type"] == "exercise"


def test_option_positions_cli_lifecycle_list_includes_evidence(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "lc_tigr_assignment",
            "case_key": "富途|lx|TIGR|put|short|6|2026-05-22",
            "account": "lx",
            "symbol": "TIGR",
            "option_type": "put",
            "position_side": "short",
            "strike": 6,
            "expiration_ymd": "2026-05-22",
            "status": "waiting_settlement_evidence",
            "decision_type": "needs_review",
            "target_lot_ids": [],
        }
    )
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "ev_option_close",
            "case_id": "lc_tigr_assignment",
            "source_type": "futu_trade_push",
            "source_event_id": "deal-option-close",
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "symbol": "TIGR",
            "raw": {"deal_id": "deal-option-close"},
        }
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "list",
            "--status",
            "waiting_settlement_evidence",
            "--include-evidence",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["cases"][0]["case_id"] == "lc_tigr_assignment"
    assert payload["cases"][0]["evidence"][0]["evidence_id"] == "ev_option_close"


def test_option_positions_cli_lifecycle_inspect_shows_case_evidence(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "lc_tigr_conflict",
            "case_key": "富途|lx|TIGR|put|short|6|2026-05-22",
            "account": "lx",
            "symbol": "TIGR",
            "option_type": "put",
            "position_side": "short",
            "strike": 6,
            "expiration_ymd": "2026-05-22",
            "status": "conflict",
            "decision_type": "assignment",
            "target_lot_ids": [],
        }
    )
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "ev_stock_settlement",
            "case_id": "lc_tigr_conflict",
            "source_type": "futu_trade_push",
            "source_event_id": "deal-stock",
            "evidence_type": "stock_settlement_leg",
            "account": "lx",
            "symbol": "TIGR",
            "raw": {"deal_id": "deal-stock"},
        }
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "inspect",
            "--case-id",
            "lc_tigr_conflict",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["case"]["case_id"] == "lc_tigr_conflict"
    assert payload["case"]["status"] == "conflict"
    assert payload["case"]["evidence"][0]["evidence_id"] == "ev_stock_settlement"


def test_option_positions_cli_lifecycle_confirm_expired_records_expire_close(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
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
            strike=440.0,
            multiplier=100,
            expiration_ymd="2026-06-05",
            premium_per_share=0.86,
            opened_at_ms=1780354364000,
        ),
    )
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "lc_0700_expire_pending",
            "case_key": "富途|lx|0700.HK|put|short|440|2026-06-05",
            "broker": "富途",
            "account": "lx",
            "symbol": "0700.HK",
            "option_type": "put",
            "position_side": "short",
            "strike": 440,
            "expiration_ymd": "2026-06-05",
            "contracts": 2,
            "multiplier": 100,
            "status": "waiting_settlement_evidence",
            "decision_type": "needs_review",
            "target_lot_ids": [],
            "event_time_ms": 1780657845000,
        }
    )
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "ev_0700_option_zero",
            "case_id": "lc_0700_expire_pending",
            "source_type": "futu_trade_push",
            "source_event_id": "775828694842258876",
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "symbol": "0700.HK",
            "trade_time_ms": 1780657845000,
            "raw": {"deal_id": "775828694842258876"},
        }
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "confirm-expired",
            "--deal-id",
            "775828694842258876",
            "--confirm",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "lifecycle_confirm_expired"
    assert payload["mode"] == "applied"
    assert payload["write_applied"] is True
    assert payload["reason"] == "expire_close_recorded"
    assert payload["operations"][0]["ledger_preflight"]["event_type"] == "expire_close"
    assert repo.list_trade_lifecycle_cases()[0]["decision_type"] == "expire_close"
    events = [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"]
    assert len(events) == 1
    assert events[0]["raw_payload"]["evidence_ids"] == ["ev_0700_option_zero"]


def test_option_positions_cli_lifecycle_confirm_expired_canonicalizes_futu_root_alias(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
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
            strike=440.0,
            multiplier=100,
            expiration_ymd="2026-06-05",
            premium_per_share=0.86,
            opened_at_ms=1780354364000,
        ),
    )
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "lc_tch_expire_pending",
            "case_key": "富途|lx|TCH|put|short|440|2026-06-05",
            "broker": "富途",
            "account": "lx",
            "symbol": "TCH",
            "option_type": "put",
            "position_side": "short",
            "strike": 440,
            "expiration_ymd": "2026-06-05",
            "contracts": 2,
            "multiplier": 100,
            "status": "waiting_settlement_evidence",
            "decision_type": "needs_review",
            "target_lot_ids": [],
            "event_time_ms": 1780657845000,
        }
    )
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "ev_tch_option_zero",
            "case_id": "lc_tch_expire_pending",
            "source_type": "futu_trade_push",
            "source_event_id": "775828694842258876",
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "symbol": "TCH",
            "trade_time_ms": 1780657845000,
            "raw": {"deal_id": "775828694842258876", "symbol": "TCH"},
        }
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "confirm-expired",
            "--deal-id",
            "775828694842258876",
            "--confirm",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "lifecycle_confirm_expired"
    assert payload["mode"] == "applied"
    assert payload["reason"] == "expire_close_recorded"
    assert payload["diagnostics"]["lifecycle_case"]["symbol"] == "0700.HK"
    events = [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"]
    assert len(events) == 1
    assert events[0]["symbol"] == "0700.HK"
    assert events[0]["raw_payload"]["evidence_ids"] == ["ev_tch_option_zero"]


def test_option_positions_cli_void_event_reports_result(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3")
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
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
            [
                "om option-positions",
                "--data-config",
                str(data_config),
                "void-event",
                "--event-id",
                str(open_result.event_id),
                "--confirm",
            ],
        )

    cli_mod.main()

    out = capsys.readouterr().out
    assert f"[DONE] voided event_id={open_result.event_id}" in out
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


def test_option_positions_cli_adjust_lot_dry_run_outputs_strategy_metadata(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
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
            option_type="call",
            side="long",
            contracts=1,
            currency="USD",
            strike=140.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.0,
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
            "--strategy",
            "yield_enhancement",
            "--leg-role",
            "enhancement_call",
            "--yield-enhancement-mode",
            "income_upside_enhancement",
            "--dry-run",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    assert "[DRY_RUN] adjust fields:" in out
    assert '"strategy": "yield_enhancement"' in out
    assert '"leg_role": "enhancement_call"' in out
    assert '"yield_enhancement_mode": "income_upside_enhancement"' in out


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
        target_event_id=str(close_result.event_id),
        void_reason="close_was_wrong",
        as_of_ms=2500,
    )
    ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(adjust_result.event_id),
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
    assert rows[0]["trade_time_beijing"] == "1970-01-01 08:00:01 北京时间"
    assert rows[1]["trade_time_beijing"] == "1970-01-01 08:00:01 北京时间"
    assert rows[3]["void_target_event_id"] == close_result.event_id
    assert rows[4]["void_target_event_id"] == adjust_result.event_id


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
    assert payload["return_summary"][0]["account"] == "lx"
    assert payload["return_summary"][0]["cash_secured_by_ccy"] == {"USD": 10000.0}
    assert payload["return_summary"][0]["premium_return_rate"] == round(1800.0 / 72000.0, 6)


def test_option_positions_cli_assigned_stock_sale_records_independent_event(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    import src.application.ledger.read_model as read_model
    from domain.domain.option_position_lots import OpenPositionCommand
    from src.application.ledger.commands import record_manual_assignment

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
    record_manual_assignment(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=2000,
    )
    assignment_event = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"][0]
    stock_lot_id = f"assigned-stock-{assignment_event['event_id']}"

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(read_model, "get_exchange_rates_or_fetch_latest", lambda **_kwargs: {"rates": {"USDCNY": 7.2}})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "assigned-stock-sale",
            "--target-stock-lot-id",
            stock_lot_id,
            "--account",
            "lx",
            "--symbol",
            "NVDA",
            "--currency",
            "USD",
            "--shares",
            "100",
            "--price",
            "105",
            "--trade-time-ms",
            "3000",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    dry_run_payload = json.loads(capsys.readouterr().out)
    assert dry_run_payload["operation"] == "manual_assigned_stock_sale"
    assert dry_run_payload["write_model"] == "assigned_stock_events"
    assert dry_run_payload["write_applied"] is False
    assert dry_run_payload["sale_event"]["fees"] == 2.5261
    assert dry_run_payload["sale_event"]["fee_provenance"]["basis"] == "estimated"
    assert repo.list_assigned_stock_events() == []

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "assigned-stock-sale",
            "--target-stock-lot-id",
            stock_lot_id,
            "--account",
            "lx",
            "--symbol",
            "NVDA",
            "--currency",
            "USD",
            "--shares",
            "100",
            "--price",
            "105",
            "--trade-time-ms",
            "3000",
            "--confirm",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    applied_payload = json.loads(capsys.readouterr().out)
    assert applied_payload["write_applied"] is True
    assert applied_payload["result"]["created"] is True
    assert len(repo.list_assigned_stock_events()) == 1
    assert repo.list_assigned_stock_events()[0]["fee_provenance"]["basis"] == "estimated"

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
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    report = json.loads(capsys.readouterr().out)
    lifecycle = [row for row in report["assignment_lifecycle_rows"] if row["stock_lot_id"] == stock_lot_id][0]
    assert lifecycle["status"] == "closed"
    assert lifecycle["assigned_stock_realized_pnl"] == 497.4739
    assert lifecycle["option_premium_attribution"] == 250.0
    assert lifecycle["assignment_lifecycle_pnl"] == 747.4739


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
    assert "| - | - | - | - | - | - | - | - | 0 | 0 | 0 | 0 | 0 |" in out
