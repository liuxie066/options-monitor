from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _write_data_config(path: Path, *, sqlite_path: Path) -> Path:
    payload = {
        "option_positions": {"sqlite_path": str(sqlite_path)},
        "feishu": {
            "app_id": "app_id",
            "app_secret": "app_secret",
            "tables": {"option_positions": "app_token/table_id"},
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_sync_script_dry_run_reports_create(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions_core.service as svc
    import scripts.sync_option_positions_to_feishu as sync_mod
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
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

    monkeypatch.setattr(sync_mod, "get_tenant_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(
        sync_mod,
        "bitable_fields",
        lambda *_args, **_kwargs: [
            {"field_name": "position_id"},
            {"field_name": "source_event_id"},
            {"field_name": "broker"},
            {"field_name": "account"},
            {"field_name": "symbol"},
            {"field_name": "option_type"},
            {"field_name": "side"},
            {"field_name": "contracts"},
            {"field_name": "contracts_open"},
            {"field_name": "contracts_closed"},
            {"field_name": "currency"},
            {"field_name": "strike"},
            {"field_name": "expiration"},
            {"field_name": "premium"},
            {"field_name": "status"},
            {"field_name": "opened_at"},
            {"field_name": "last_action_at"},
            {"field_name": "note"},
            {"field_name": "local_record_id"},
            {"field_name": "last_synced_at"},
        ],
    )
    monkeypatch.setattr(sync_mod, "bitable_list_records", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sync_mod, "bitable_create_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run should not create")))
    monkeypatch.setattr(sync_mod, "bitable_update_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run should not update")))
    monkeypatch.setattr(sys, "argv", ["sync_option_positions_to_feishu.py", "--data-config", str(data_config), "--dry-run"])

    sync_mod.main()

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["action"] == "create"
    assert lines[0]["reason"] == "missing_feishu_record_id"
    assert lines[-1]["summary"]["create"] == 1
    assert lines[-1]["summary"]["mode_apply"] == 0


def test_sync_script_apply_updates_existing_feishu_row_and_persists_metadata(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions_core.service as svc
    import scripts.sync_option_positions_to_feishu as sync_mod
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
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
            strike=90.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.0,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]
    existing_fields = dict(lot["fields"])
    existing_fields["feishu_record_id"] = "rec_existing"
    repo.update_position_lot_fields(lot["record_id"], existing_fields)

    captured: dict[str, object] = {}

    monkeypatch.setattr(sync_mod, "get_tenant_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(
        sync_mod,
        "bitable_fields",
        lambda *_args, **_kwargs: [
            {"field_name": "position_id"},
            {"field_name": "source_event_id"},
            {"field_name": "broker"},
            {"field_name": "account"},
            {"field_name": "symbol"},
            {"field_name": "option_type"},
            {"field_name": "side"},
            {"field_name": "contracts"},
            {"field_name": "contracts_open"},
            {"field_name": "contracts_closed"},
            {"field_name": "currency"},
            {"field_name": "strike"},
            {"field_name": "expiration"},
            {"field_name": "premium"},
            {"field_name": "status"},
            {"field_name": "opened_at"},
            {"field_name": "last_action_at"},
            {"field_name": "note"},
            {"field_name": "local_record_id"},
            {"field_name": "last_synced_at"},
        ],
    )
    monkeypatch.setattr(sync_mod, "bitable_list_records", lambda *_args, **_kwargs: [])

    def _fake_update(_token, _app_token, _table_id, record_id, fields):
        captured["record_id"] = record_id
        captured["fields"] = dict(fields)
        return {"record": {"record_id": record_id}}

    monkeypatch.setattr(sync_mod, "bitable_update_record", _fake_update)
    monkeypatch.setattr(sync_mod, "bitable_create_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("apply update should not create")))

    rows = sync_mod.sync_option_positions(repo=repo, data_config=data_config, apply_mode=True)

    refreshed_fields = repo.get_position_lot_fields(lot["record_id"])
    assert captured["record_id"] == "rec_existing"
    assert refreshed_fields["feishu_record_id"] == "rec_existing"
    assert refreshed_fields["feishu_sync_hash"]
    assert int(refreshed_fields["feishu_last_synced_at_ms"]) > 0
    assert rows[0]["action"] == "update"
    assert sync_mod.summarize_result(rows)["update"] == 1
