from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

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
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    svc.persist_manual_open_event(
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
        ["option_positions.py", "--data-config", str(data_config), "events", "--format", "json", "--account", "lx"],
    )

    cli_mod.main()

    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["account"] == "lx"
    assert rows[0]["position_effect"] == "open"
    assert rows[0]["symbol"] == "TSLA"


def test_option_positions_cli_rebuild_reports_summary(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    svc.persist_manual_open_event(
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
    monkeypatch.setattr(sys, "argv", ["option_positions.py", "--data-config", str(data_config), "rebuild"])

    cli_mod.main()

    out = capsys.readouterr().out
    assert "[DONE] rebuilt position_lots" in out
    assert "trade_events=1" in out
    assert "position_lots=1" in out
    assert "unmatched_explicit_close=0" in out


def test_option_positions_cli_inspect_reports_projection_state(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    svc.persist_manual_open_event(
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
    patched_fields = dict(lot["fields"])
    patched_fields["feishu_record_id"] = "rec_sync_1"
    repo.update_position_lot_fields(lot["record_id"], patched_fields)

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "option_positions.py",
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
    assert payload["current_lots"][0]["fields"]["feishu_record_id"] == "rec_sync_1"
    assert payload["projected_lots"][0]["record_id"] == lot["record_id"]
    assert payload["related_events"][0]["event_id"].startswith("manual-open-")


def test_option_positions_cli_inspect_reports_orphan_close_event_diagnostics(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.ledger import TradeEvent

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    svc._persist_trade_event_object(
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
                "source": "option_positions.py",
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
            "option_positions.py",
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
    assert payload["projection_diagnostics"][0]["code"] == "close_explicit_target_not_found"


def test_option_positions_cli_add_dry_run_infers_hkd_currency_from_hk_symbol(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "option_positions.py",
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
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "option_positions.py",
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
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    near_exp = (datetime.now().date() + timedelta(days=1)).isoformat()
    far_exp = (datetime.now().date() + timedelta(days=21)).isoformat()
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    svc.persist_manual_open_event(
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
    svc.persist_manual_open_event(
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
            "option_positions.py",
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


def test_option_positions_cli_void_event_reports_result(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    open_result = svc.persist_manual_open_event(
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
        ["option_positions.py", "--data-config", str(data_config), "void-event", "--event-id", str(open_result["event_id"])],
    )

    cli_mod.main()

    out = capsys.readouterr().out
    assert f"[DONE] voided event_id={open_result['event_id']}" in out
    assert repo.list_position_lots() == []


def test_option_positions_cli_adjust_lot_dry_run_outputs_patch(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    svc.persist_manual_open_event(
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
            "option_positions.py",
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
    import scripts.option_positions as cli_mod
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    svc.persist_manual_open_event(
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
    close_result = svc.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.0,
        close_reason="manual_buy_to_close",
        as_of_ms=1500,
    )
    adjust_result = svc.persist_manual_adjust_event(
        repo,
        record_id=lot["record_id"],
        fields=repo.get_position_lot_fields(lot["record_id"]),
        premium_per_share=3.1,
        as_of_ms=2000,
    )
    svc.persist_manual_void_event(
        repo,
        target_event_id=str(close_result["event_id"]),
        void_reason="close_was_wrong",
        as_of_ms=2500,
    )
    svc.persist_manual_void_event(
        repo,
        target_event_id=str(adjust_result["event_id"]),
        void_reason="adjust_was_wrong",
        as_of_ms=2600,
    )

    monkeypatch.setattr(cli_mod, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        sys,
        "argv",
        ["option_positions.py", "--data-config", str(data_config), "history", "--record-id", lot["record_id"], "--format", "json"],
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
