from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest  # pyright: ignore[reportMissingImports]

import src.application.ledger.bootstrap as ledger_bootstrap
import src.application.ledger.interventions as ledger_interventions
import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository
import src.application.ledger.writer as ledger_writer



def test_projection_migration_inventory_uses_read_only_store_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = tmp_path / "data.json"
    data_config.write_text("{}\n", encoding="utf-8")
    sqlite_path = tmp_path / "ledger.sqlite3"
    sqlite_path.touch()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli_mod,
        "resolve_ledger_store",
        lambda *_args, **_kwargs: SimpleNamespace(sqlite_path=sqlite_path),
    )
    def inventory(path: Path) -> dict[str, str]:
        captured["path"] = path
        return {"path": str(path)}

    monkeypatch.setattr(
        cli_mod,
        "build_position_projection_migration_inventory",
        inventory,
    )

    assert cli_mod.main(
        [
            "--data-config",
            str(data_config),
            "projection-migration",
            "inventory",
        ]
    ) == 0

    assert captured["path"] == sqlite_path
    assert json.loads(capsys.readouterr().out) == {"path": str(sqlite_path)}


def test_projection_migration_writes_require_apply_and_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = tmp_path / "data.json"
    data_config.write_text("{}\n", encoding="utf-8")
    manifest = tmp_path / "inventory.json"
    manifest.write_text('{"schema_version":"test"}\n', encoding="utf-8")
    sqlite_path = tmp_path / "ledger.sqlite3"
    sqlite_path.touch()
    common = [
        "--data-config",
        str(data_config),
        "projection-migration",
        "apply",
        "--manifest",
        str(manifest),
    ]

    with pytest.raises(SystemExit, match="requires --apply"):
        cli_mod.main(common)
    with pytest.raises(SystemExit, match="use --confirm or --yes"):
        cli_mod.main([*common, "--apply"])

    monkeypatch.setattr(cli_mod, "_guard_write", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        cli_mod,
        "resolve_ledger_store",
        lambda *_args, **_kwargs: SimpleNamespace(sqlite_path=sqlite_path),
    )
    monkeypatch.setattr(
        cli_mod,
        "apply_position_projection_migration",
        lambda path, payload: {
            "operation": "apply",
            "path": str(path),
            "input_schema": payload["schema_version"],
        },
    )

    assert cli_mod.main([*common, "--apply", "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "operation": "apply",
        "path": str(sqlite_path),
        "input_schema": "test",
    }


@pytest.mark.parametrize(
    ("command", "function_name"),
    (
        ("inventory", "build_current_decision_projection_migration_inventory"),
        ("verify", "verify_current_decision_projection_migration"),
        ("status", "current_decision_projection_migration_status"),
    ),
)
def test_decision_projection_reads_use_resolved_store_without_write_guard(
    command: str,
    function_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = tmp_path / "data.json"
    data_config.write_text("{}\n", encoding="utf-8")
    sqlite_path = tmp_path / "ledger.sqlite3"
    sqlite_path.touch()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli_mod,
        "resolve_ledger_store",
        lambda *_args, **_kwargs: SimpleNamespace(sqlite_path=sqlite_path),
    )
    monkeypatch.setattr(
        cli_mod,
        function_name,
        lambda path: captured.update(path=path) or {"path": str(path)},
    )
    monkeypatch.setattr(
        cli_mod,
        "_guard_write",
        lambda **_kwargs: pytest.fail("read command invoked write guard"),
    )

    assert cli_mod.main(
        ["--data-config", str(data_config), "decision-projection", command]
    ) == 0
    assert captured["path"] == sqlite_path
    assert json.loads(capsys.readouterr().out) == {"path": str(sqlite_path)}


def test_decision_projection_apply_requires_manifest_and_high_risk_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = tmp_path / "data.json"
    data_config.write_text("{}\n", encoding="utf-8")
    manifest = tmp_path / "inventory.json"
    manifest.write_text('{"schema_version":"test"}\n', encoding="utf-8")
    sqlite_path = tmp_path / "ledger.sqlite3"
    sqlite_path.touch()
    common = [
        "--data-config",
        str(data_config),
        "decision-projection",
        "apply",
        "--manifest",
        str(manifest),
    ]

    with pytest.raises(SystemExit, match="requires --apply"):
        cli_mod.main(common)
    with pytest.raises(SystemExit, match="use --confirm or --yes"):
        cli_mod.main([*common, "--apply"])

    monkeypatch.setattr(cli_mod, "_guard_write", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        cli_mod,
        "resolve_ledger_store",
        lambda *_args, **_kwargs: SimpleNamespace(sqlite_path=sqlite_path),
    )
    monkeypatch.setattr(
        cli_mod,
        "apply_current_decision_projection_migration",
        lambda path, payload: {
            "operation": "apply",
            "path": str(path),
            "input_schema": payload["schema_version"],
        },
    )

    assert cli_mod.main([*common, "--apply", "--yes"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "operation": "apply",
        "path": str(sqlite_path),
        "input_schema": "test",
    }


def test_projection_migration_activate_requires_both_evidence_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = tmp_path / "data.json"
    data_config.write_text("{}\n", encoding="utf-8")
    acceptance = tmp_path / "acceptance.json"
    shadow = tmp_path / "shadow.json"
    acceptance.write_text('{"kind":"acceptance"}\n', encoding="utf-8")
    shadow.write_text('{"kind":"shadow"}\n', encoding="utf-8")
    sqlite_path = tmp_path / "ledger.sqlite3"
    sqlite_path.touch()
    monkeypatch.setattr(cli_mod, "_guard_write", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        cli_mod,
        "resolve_ledger_store",
        lambda *_args, **_kwargs: SimpleNamespace(sqlite_path=sqlite_path),
    )
    monkeypatch.setattr(
        cli_mod,
        "activate_position_projection_checkpoints",
        lambda path, **kwargs: {
            "operation": "activate",
            "path": str(path),
            "acceptance": kwargs["acceptance_manifest"]["kind"],
            "shadow": kwargs["shadow_manifest"]["kind"],
        },
    )

    assert cli_mod.main(
        [
            "--data-config",
            str(data_config),
            "projection-migration",
            "activate",
            "--acceptance-manifest",
            str(acceptance),
            "--shadow-manifest",
            str(shadow),
            "--apply",
            "--yes",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "activate"


def test_combo_confirmation_mode_is_account_scoped_and_fail_closed(
    tmp_path: Path,
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    inference = {"account": "lx", "market": "US"}
    off_path = tmp_path / "off.json"
    off_path.write_text(
        json.dumps(
            {
                "accounts": ["lx"],
                "trade_intake": {
                    "combo_reconciliation": {
                        "default_mode": "off",
                        "accounts": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    args = type("Args", (), {"config": str(off_path)})()
    with pytest.raises(SystemExit, match="effective mode=off"):
        cli_mod._require_combo_confirmation_mode(
            base=tmp_path,
            args=args,
            inference=inference,
        )

    confirm_path = tmp_path / "confirm.json"
    confirm_path.write_text(
        json.dumps(
            {
                "accounts": ["lx"],
                "trade_intake": {
                    "combo_reconciliation": {
                        "default_mode": "off",
                        "accounts": {"lx": "confirm"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    args.config = str(confirm_path)
    assert cli_mod._require_combo_confirmation_mode(
        base=tmp_path,
        args=args,
        inference=inference,
    )["mode"] == "confirm"

    config = json.loads(confirm_path.read_text(encoding="utf-8"))
    config["trade_intake"]["combo_reconciliation"]["accounts"]["lx"] = "auto"
    confirm_path.write_text(json.dumps(config), encoding="utf-8")
    assert cli_mod._require_combo_confirmation_mode(
        base=tmp_path,
        args=args,
        inference=inference,
    )["mode"] == "auto"


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
    repo.upsert_trade_event(
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
            "--publish-evidence",
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

    checkpoint_path = (
        tmp_path
        / "output_shared"
        / "state"
        / "option_positions"
        / "current"
        / "projection_verify.checkpoint.json"
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["projection_contract_version"] = "position_lot_projection.v1"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    cli_mod.main()
    stale_contract = json.loads(capsys.readouterr().out)
    assert stale_contract["ok"] is True
    assert stale_contract["mode_used"] == "full_replay"
    assert stale_contract["checkpoint_reused"] is False


def test_option_positions_cli_verify_projection_is_read_only_by_default(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
    )
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
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
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
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
    assert payload["evidence_published"] is False
    assert not (
        tmp_path
        / "output_shared"
        / "state"
        / "option_positions"
    ).exists()


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
            "--publish-evidence",
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
            "--request-id",
            "test-add-hk-dry-run",
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
            "--request-id",
            "test-add-us-dry-run",
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
            "--request-id",
            "test-add-apply-guard",
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


def test_lifecycle_write_requires_apply_and_confirmation_together() -> None:
    import src.interfaces.cli.option_positions as cli_mod

    common = [
        "lifecycle",
        "resolve",
        "--case-id",
        "case-1",
        "--expected-revision",
        "1",
        "--reason",
        "assignment",
        "--broker-ref",
        "futu:lx:1001:deal-1",
        "--note",
        "operator evidence",
    ]
    with pytest.raises(
        SystemExit,
        match="use --confirm or --yes",
    ):
        cli_mod.main([*common, "--apply"])
    with pytest.raises(
        SystemExit,
        match="requires --apply together",
    ):
        cli_mod.main([*common, "--confirm"])


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
        "--request-id",
        "test-add-confirm",
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
        "2027-06-29",
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
            "--request-id",
            "test-assignment-confirm",
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
            "--request-id",
            "test-assignment-wrong-side",
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
            "--request-id",
            "test-exercise-confirm",
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


def test_option_positions_cli_lifecycle_confirm_expired_is_retired(
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

    with pytest.raises(
        SystemExit,
        match="lifecycle confirm-expired is retired",
    ):
        cli_mod.main()

    events = [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"]
    assert events == []


def test_option_positions_cli_lifecycle_reconcile_dry_run_then_apply_discovery(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand
    from domain.domain.option_lifecycle import expiration_observation_start_ms

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
    )
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
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
            strike=100,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=1,
            opened_at_ms=1_700_000_000_000,
        ),
    )
    observation_start = expiration_observation_start_ms("2026-08-21", "US")
    assert observation_start is not None
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "reconcile",
            "--account",
            "lx",
            "--observed-at-ms",
            str(observation_start),
            "--dry-run",
            "--format",
            "json",
        ],
    )
    cli_mod.main()
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["operation"] == "lifecycle_reconcile"
    assert dry_run["dry_run"] is True
    assert len(dry_run["discovery"]["would_create_case_ids"]) == 1
    assert repo.list_trade_lifecycle_cases() == []

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "reconcile",
            "--account",
            "lx",
            "--observed-at-ms",
            str(observation_start),
            "--apply",
            "--confirm",
            "--format",
            "json",
        ],
    )
    cli_mod.main()
    applied = json.loads(capsys.readouterr().out)
    assert applied["dry_run"] is False
    assert applied["write_applied"] is True
    assert len(applied["discovery"]["created_case_ids"]) == 1
    assert applied["read_models"][0]["lifecycle_state"] == "settlement_pending"
    assert len(repo.list_trade_lifecycle_cases()) == 1


def test_option_positions_cli_reconcile_due_preview_does_not_build_gateways(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
    )
    runtime_config = tmp_path / "runtime.json"
    runtime_config.write_text("{}\n", encoding="utf-8")
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_trade_intake_config",
        lambda _cfg: {
            "sources": [
                {
                    "id": "lx",
                    "account": "lx",
                    "futu_account_ids": ["1001"],
                }
            ]
        },
    )

    def fail_gateway(**kwargs):
        raise AssertionError(f"gateway built during preview: {kwargs}")

    monkeypatch.setattr(
        cli_mod,
        "build_ready_futu_broker_gateway",
        fail_gateway,
    )
    monkeypatch.setattr(
        cli_mod,
        "build_ready_futu_quote_gateway",
        fail_gateway,
    )

    def reconcile(_repo, **kwargs):
        captured.update(kwargs)
        return {"results": [], "status": "ok"}

    monkeypatch.setattr(
        cli_mod,
        "reconcile_due_lifecycle_cases_for_source",
        reconcile,
    )

    assert (
        cli_mod.main(
            [
                "--data-config",
                str(data_config),
                "lifecycle",
                "reconcile-due",
                "--account",
                "lx",
                "--config",
                str(runtime_config),
                "--observed-at-ms",
                "123",
                "--dry-run",
                "--format",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert captured["apply_changes"] is False
    assert "broker_gateway" not in captured
    assert "quote_gateway" not in captured


@pytest.mark.parametrize(
    ("seal_status", "expected_return_code"),
    [("not_required", 0), ("seal_persist_failed", 1)],
)
def test_option_positions_cli_reconcile_due_apply_checkpoints_before_gateways(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    seal_status: str,
    expected_return_code: int,
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
    )
    runtime_config = tmp_path / "runtime.json"
    runtime_config.write_text("{}\n", encoding="utf-8")
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    order: list[str] = []
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_trade_intake_config",
        lambda _cfg: {
            "sources": [
                {
                    "id": "lx",
                    "account": "lx",
                    "audit_path": "audit.jsonl",
                    "futu_account_ids": ["1001"],
                }
            ]
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_account_broker_binding_sets",
        lambda _items: {
            "lx": SimpleNamespace(
                ok=True,
                host="127.0.0.1",
                port=11111,
                required_account_ids=("1001",),
                trd_env="REAL",
            )
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_futu_quote_route",
        lambda _cfg: SimpleNamespace(
            ok=True,
            host="127.0.0.1",
            port=11111,
        ),
    )
    original_checkpoint = cli_mod.append_lifecycle_attempt_checkpoint_seal

    def checkpoint(*args, **kwargs):
        order.append("checkpoint")
        return original_checkpoint(*args, **kwargs)

    class Gateway:
        def close(self):
            return None

    def gateway(kind: str):
        def build(**_kwargs):
            order.append(kind)
            assert (tmp_path / "audit.jsonl").is_file()
            return Gateway()

        return build

    monkeypatch.setattr(
        cli_mod,
        "append_lifecycle_attempt_checkpoint_seal",
        checkpoint,
    )
    monkeypatch.setattr(
        cli_mod,
        "build_ready_futu_broker_gateway",
        gateway("broker"),
    )
    monkeypatch.setattr(
        cli_mod,
        "build_ready_futu_quote_gateway",
        gateway("quote"),
    )

    def reconcile(_repo, **kwargs):
        order.append("runtime")
        assert callable(kwargs["seal_sink"])
        return {
            "account": "lx",
            "source_id": "lx",
            "results": [],
            "seal_status": seal_status,
            "run_seal": None,
        }

    monkeypatch.setattr(
        cli_mod,
        "reconcile_due_lifecycle_cases_for_source",
        reconcile,
    )

    assert (
        cli_mod.main(
            [
                "--data-config",
                str(data_config),
                "lifecycle",
                "reconcile-due",
                "--runtime-root",
                str(tmp_path),
                "--account",
                "lx",
                "--config",
                str(runtime_config),
                "--apply",
                "--confirm",
                "--format",
                "json",
            ]
        )
        == expected_return_code
    )

    assert order == ["checkpoint", "broker", "quote", "runtime"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["seal_status"] == seal_status


def test_option_positions_cli_checkpoint_failure_blocks_gateways(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
    )
    runtime_config = tmp_path / "runtime.json"
    runtime_config.write_text("{}\n", encoding="utf-8")
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_trade_intake_config",
        lambda _cfg: {
            "sources": [
                {
                    "id": "lx",
                    "account": "lx",
                    "audit_path": "audit.jsonl",
                    "futu_account_ids": ["1001"],
                }
            ]
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_account_broker_binding_sets",
        lambda _items: {
            "lx": SimpleNamespace(
                ok=True,
                host="127.0.0.1",
                port=11111,
                required_account_ids=("1001",),
                trd_env="REAL",
            )
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_futu_quote_route",
        lambda _cfg: SimpleNamespace(
            ok=True,
            host="127.0.0.1",
            port=11111,
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "append_lifecycle_attempt_checkpoint_seal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("disk full")
        ),
    )
    gateway_calls = 0

    def fail_gateway(**_kwargs):
        nonlocal gateway_calls
        gateway_calls += 1
        raise AssertionError("gateway must remain unopened")

    monkeypatch.setattr(
        cli_mod,
        "build_ready_futu_broker_gateway",
        fail_gateway,
    )
    monkeypatch.setattr(
        cli_mod,
        "build_ready_futu_quote_gateway",
        fail_gateway,
    )

    with pytest.raises(SystemExit, match="seal_persist_failed: OSError"):
        cli_mod.main(
            [
                "--data-config",
                str(data_config),
                "lifecycle",
                "reconcile-due",
                "--runtime-root",
                str(tmp_path),
                "--account",
                "lx",
                "--config",
                str(runtime_config),
                "--apply",
                "--confirm",
            ]
        )

    assert gateway_calls == 0


def test_option_positions_cli_lifecycle_confirm_expired_alias_path_is_retired(
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

    with pytest.raises(
        SystemExit,
        match="lifecycle confirm-expired is retired",
    ):
        cli_mod.main()

    events = [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"]
    assert events == []


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
            "combo_yield",
            "--leg-role",
            "enhancement_call",
            "--dry-run",
        ],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    assert "[DRY_RUN] adjust fields:" in out
    assert '"strategy": "combo_yield"' in out
    assert '"leg_role": "enhancement_call"' in out
    assert "yield_enhancement_mode" not in out


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


def test_option_positions_cli_history_reads_voided_open_tombstone(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
    )
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    open_result = ledger_manual_trades.persist_manual_open_event(
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
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=1000,
        ),
    )
    lot_id = str(open_result.record_id)
    ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(open_result.event_id),
        void_reason="bad open",
        as_of_ms=2000,
    )
    assert repo.list_position_lots() == []

    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    cli_mod.main(
        [
            "--data-config",
            str(data_config),
            "history",
            "--record-id",
            lot_id,
            "--format",
            "json",
        ]
    )

    rows = json.loads(capsys.readouterr().out)
    assert [row["position_effect"] for row in rows] == ["open", "void"]
    assert rows[1]["void_target_event_id"] == open_result.event_id




def test_option_positions_cli_assigned_stock_sale_records_independent_event(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.option_position_lots import OpenPositionCommand
    from src.application.ledger.commands import record_manual_assignment
    from src.application.positions.assigned_stock_view import build_assigned_stock_view

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
    assert dry_run_payload["sale_event"]["fees"] == 0.0
    assert dry_run_payload["sale_event"]["fee_provenance"]["basis"] == "estimated"
    assert dry_run_payload["sale_event"]["fee_provenance"]["amount"] == "2.5261"
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

    report = build_assigned_stock_view(repo, broker="富途", account="lx", as_of_ms=3000)
    lifecycle = [row for row in report["assigned_stock_lots"] if row["stock_lot_id"] == stock_lot_id][0]
    assert lifecycle["status"] == "closed"
    assert lifecycle["assigned_stock_realized_pnl"] == 497.4739
    assert lifecycle["option_premium_attribution"] == 250.0
    assert lifecycle["assignment_lifecycle_pnl"] == 747.4739
def test_option_positions_cli_adopt_combo_identity_dry_run(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.option_positions as cli_mod
    from domain.domain.ledger import ContractKey, TradeEvent

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
    )
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]

    def _event(*, event_id: str, option_type: str) -> TradeEvent:
        side = "short" if option_type == "put" else "long"
        strike = 145 if option_type == "put" else 220
        role = "funding_put" if option_type == "put" else "participation_call"
        contract = ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="9992.HK",
            option_type=option_type,
            position_side=side,
            strike=strike,
            expiration_ymd="2026-09-29",
        )
        return TradeEvent(
            event_id=event_id,
            event_type="open",
            event_time_ms=1_700_000_000_000,
            contract_key=contract,
            contracts=1,
            price=2.0,
            currency="HKD",
            source="test",
            lot_id=f"lot-{event_id}",
            raw_payload={
                "fields": {
                    "broker": "futu",
                    "account": "lx",
                    "symbol": "9992.HK",
                    "option_type": option_type,
                    "side": side,
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "currency": "HKD",
                    "strike": strike,
                    "expiration_ymd": "2026-09-29",
                    "multiplier": 100,
                    "premium": 2.0,
                    "strategy": "combo_yield",
                    "strategy_group_id": "combo:lx:9992",
                    "leg_role": role,
                }
            },
        )

    ledger_writer.persist_trade_event_object(
        repo,
        _event(event_id="put-open", option_type="put"),
    )
    ledger_writer.persist_trade_event_object(
        repo,
        _event(event_id="call-open", option_type="call"),
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "adopt-combo-identity",
            "--strategy-group-id",
            "combo:lx:9992",
            "--funding-put-record-id",
            "lot-put-open",
            "--funding-put-open-event-id",
            "put-open",
            "--participation-call-record-id",
            "lot-call-open",
            "--participation-call-open-event-id",
            "call-open",
            "--expected-contracts",
            "1",
            "--dry-run",
            "--format",
            "json",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == ("existing_combo_identity_adoption.v1")
    assert payload["status"] == "dry_run"
    assert payload["write_applied"] is False
    assert payload["identity_created"] is False
    assert repo.get_strategy_group_identity("combo:lx:9992") is None


def _lifecycle_receipt_cli_context(
    monkeypatch,
    tmp_path: Path,
    *,
    receipt_enabled: bool = True,
    sy_intake_enabled: bool = True,
):
    import src.interfaces.cli.option_positions as cli_mod

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "legacy" / "option_positions.sqlite3",
    )
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    )
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    runtime_config = tmp_path / "runtime.json"
    runtime_config.write_text(
        json.dumps(
            {
                "accounts": ["lx", "sy"],
                "account_settings": {
                    "lx": {
                        "type": "futu",
                        "futu": {
                            "account_id": "REAL_LX",
                            "host": "127.0.0.1",
                            "port": 11111,
                        },
                    },
                    "sy": {
                        "type": "futu",
                        "trade_intake_enabled": sy_intake_enabled,
                        "futu": {
                            "account_id": "REAL_SY",
                            "host": "127.0.0.1",
                            "port": 11112,
                        },
                    },
                },
                "notifications": {
                    "provider": "wechat_clawbot",
                    "target": "wechat:ops",
                },
                "trade_intake": {
                    "receipt": {"enabled": receipt_enabled}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    return cli_mod, data_config, runtime_config, repo


def _enqueue_lifecycle_cli_intents(
    repo,
    *,
    count: int = 2,
    accounts: list[str] | None = None,
) -> list[dict]:
    from src.application.ledger.notification_outbox import (
        build_notification_intent,
    )

    rows = []
    for index in range(count):
        account = (
            accounts[index % len(accounts)]
            if accounts
            else "lx"
        )
        intent = build_notification_intent(
            case_id=f"case-cli-{index}",
            transition_type="needs_review",
            resolution_revision=1,
            transition_key=(
                f"lifecycle:case-cli-{index}:needs_review"
            ),
            state_fingerprint=f"state-cli-{index}",
            payload={
                "account": account,
                "case_id": f"case-cli-{index}",
                "transition_type": "needs_review",
                "symbol": f"SYM{index}",
            },
        )
        assert repo.insert_trade_lifecycle_notification_once(intent)
        row = repo.get_trade_lifecycle_notification(intent["outbox_id"])
        assert row is not None
        rows.append(row)
    return rows


def _create_lifecycle_cli_batch(repo, rows: list[dict]) -> dict:
    from src.application.trades.lifecycle_outbox import (
        QUIET_WINDOW_MS,
        build_notification_batch_route,
        plan_notification_batch,
    )

    result = plan_notification_batch(
        repo,
        route=build_notification_batch_route(
            provider="wechat_clawbot",
            channel="wechat_clawbot",
            target="wechat:ops",
        ),
        now_ms=(
            max(int(row["created_at_ms"]) for row in rows)
            + QUIET_WINDOW_MS
        ),
    )
    assert result["status"] == "created"
    return result["batch"]


def test_lifecycle_receipt_cli_inspects_batch_and_members(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    cli_mod, data_config, _runtime_config, repo = (
        _lifecycle_receipt_cli_context(monkeypatch, tmp_path)
    )
    rows = _enqueue_lifecycle_cli_intents(repo)
    batch = _create_lifecycle_cli_batch(repo, rows)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "receipts",
            "inspect",
            "--batch-id",
            str(batch["batch_id"]),
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["batch"]["batch_id"] == batch["batch_id"]
    assert len(payload["members"]) == 2
    assert payload["outbox"] is None
    assert "wechat:ops" not in str(payload)


def test_lifecycle_receipt_cli_refuses_multi_member_reconcile_by_outbox(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cli_mod, data_config, _runtime_config, repo = (
        _lifecycle_receipt_cli_context(monkeypatch, tmp_path)
    )
    rows = _enqueue_lifecycle_cli_intents(repo)
    batch = _create_lifecycle_cli_batch(repo, rows)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "receipts",
            "reconcile",
            "--outbox-id",
            str(rows[0]["outbox_id"]),
            "--mark",
            "confirmed",
            "--broker-ref",
            "provider-check",
            "--note",
            "verified",
            "--dry-run",
        ],
    )

    with pytest.raises(
        SystemExit,
        match=f"re-run with --batch-id {batch['batch_id']}",
    ):
        cli_mod.main()


def test_lifecycle_receipt_cli_batch_reconcile_dry_run_is_no_write(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from src.application.trades.lifecycle_outbox import (
        QUIET_WINDOW_MS,
        build_notification_batch_route,
        dispatch_notification_batch_once,
    )

    cli_mod, data_config, _runtime_config, repo = (
        _lifecycle_receipt_cli_context(monkeypatch, tmp_path)
    )
    rows = _enqueue_lifecycle_cli_intents(repo)
    result = dispatch_notification_batch_once(
        repo,
        route=build_notification_batch_route(
            provider="wechat_clawbot",
            channel="wechat_clawbot",
            target="wechat:ops",
        ),
        send_fn=lambda _payload: {"status": "unknown"},
        now_ms=(
            max(int(row["created_at_ms"]) for row in rows)
            + QUIET_WINDOW_MS
        ),
    )
    batch_id = str(result["batch"]["batch_id"])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "receipts",
            "reconcile",
            "--batch-id",
            batch_id,
            "--mark",
            "confirmed",
            "--broker-ref",
            "provider-check",
            "--note",
            "verified",
            "--dry-run",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["batch_id"] == batch_id
    assert payload["member_count"] == 2
    assert payload["apply_changes"] is False
    assert payload["write_applied"] is False
    assert repo.get_trade_lifecycle_notification_batch(batch_id)[
        "status"
    ] == "unknown"


def test_lifecycle_receipt_cli_dispatch_dry_run_does_not_bind_or_send(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from src.application.trades.lifecycle_outbox import QUIET_WINDOW_MS

    cli_mod, data_config, runtime_config, repo = (
        _lifecycle_receipt_cli_context(monkeypatch, tmp_path)
    )
    rows = _enqueue_lifecycle_cli_intents(repo)
    monkeypatch.setattr(
        cli_mod,
        "utc_now_ms",
        lambda: (
            max(int(row["created_at_ms"]) for row in rows)
            + QUIET_WINDOW_MS
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "receipts",
            "dispatch",
            "--once",
            "--account",
            "lx",
            "--config",
            str(runtime_config),
            "--dry-run",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["preview"]["status"] == "ready"
    assert payload["preview"]["candidate_count"] == 2
    assert payload["write_applied"] is False
    assert repo.list_trade_lifecycle_notification_batches() == []
    assert {
        row["status"]
        for row in repo.list_trade_lifecycle_notifications()
    } == {"pending"}


def test_lifecycle_receipt_cli_applied_dispatch_rejects_account_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cli_mod, data_config, runtime_config, repo = (
        _lifecycle_receipt_cli_context(monkeypatch, tmp_path)
    )
    monkeypatch.setattr(
        cli_mod,
        "_guard_write",
        lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "receipts",
            "dispatch",
            "--once",
            "--account",
            "lx",
            "--config",
            str(runtime_config),
            "--apply",
            "--confirm",
        ],
    )

    with pytest.raises(
        SystemExit,
        match="applied lifecycle receipt dispatch is global",
    ):
        cli_mod.main()
    assert repo.list_trade_lifecycle_notification_batches() == []


def test_lifecycle_receipt_cli_applied_dispatch_filters_disabled_account(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from src.application.trades.lifecycle_outbox import QUIET_WINDOW_MS

    cli_mod, data_config, runtime_config, repo = (
        _lifecycle_receipt_cli_context(
            monkeypatch,
            tmp_path,
            sy_intake_enabled=False,
        )
    )
    rows = _enqueue_lifecycle_cli_intents(
        repo,
        accounts=["lx", "sy"],
    )
    current = (
        max(int(row["created_at_ms"]) for row in rows)
        + QUIET_WINDOW_MS
    )
    calls: list[dict] = []
    monkeypatch.setattr(cli_mod, "utc_now_ms", lambda: current)
    monkeypatch.setattr(
        cli_mod,
        "_guard_write",
        lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        cli_mod,
        "send_trade_lifecycle_outbox_payload",
        lambda **kwargs: calls.append(dict(kwargs))
        or {
            "status": "confirmed",
            "delivery_confirmed": True,
            "message_id": "provider-message",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "receipts",
            "dispatch",
            "--once",
            "--config",
            str(runtime_config),
            "--apply",
            "--confirm",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "confirmed"
    assert payload["batch"]["member_count"] == 1
    assert payload["write_applied"] is True
    assert len(calls) == 1
    assert {
        member["payload"]["account"]
        for member in calls[0]["payload"]["members"]
    } == {"lx"}
    stored = {
        row["payload"]["account"]: row
        for row in repo.list_trade_lifecycle_notifications()
    }
    assert stored["lx"]["status"] == "confirmed"
    assert stored["sy"]["status"] == "pending"
    assert stored["sy"]["delivery_batch_id"] is None


def test_lifecycle_receipt_cli_disabled_config_is_no_write(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    from src.application.trades.lifecycle_outbox import QUIET_WINDOW_MS

    cli_mod, data_config, runtime_config, repo = (
        _lifecycle_receipt_cli_context(
            monkeypatch,
            tmp_path,
            receipt_enabled=False,
        )
    )
    rows = _enqueue_lifecycle_cli_intents(repo)
    current = (
        max(int(row["created_at_ms"]) for row in rows)
        + QUIET_WINDOW_MS
    )
    calls: list[dict] = []
    monkeypatch.setattr(cli_mod, "utc_now_ms", lambda: current)
    monkeypatch.setattr(
        cli_mod,
        "_guard_write",
        lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        cli_mod,
        "send_trade_lifecycle_outbox_payload",
        lambda **kwargs: calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "receipts",
            "dispatch",
            "--once",
            "--config",
            str(runtime_config),
            "--apply",
            "--confirm",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "idle"
    assert payload["reason"] == "notification_receipt_disabled"
    assert payload["write_applied"] is False
    assert calls == []
    assert repo.list_trade_lifecycle_notification_batches() == []
    assert {
        row["status"]
        for row in repo.list_trade_lifecycle_notifications()
    } == {"pending"}


def test_lifecycle_receipt_cli_applied_idle_reports_no_write(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    cli_mod, data_config, runtime_config, repo = (
        _lifecycle_receipt_cli_context(monkeypatch, tmp_path)
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        cli_mod,
        "_guard_write",
        lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        cli_mod,
        "send_trade_lifecycle_outbox_payload",
        lambda **kwargs: calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "om option-positions",
            "--data-config",
            str(data_config),
            "lifecycle",
            "receipts",
            "dispatch",
            "--once",
            "--config",
            str(runtime_config),
            "--apply",
            "--confirm",
        ],
    )

    cli_mod.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "idle"
    assert payload["reason"] == "no_eligible_unbound_intents"
    assert payload["write_applied"] is False
    assert calls == []
    assert repo.list_trade_lifecycle_notification_batches() == []
