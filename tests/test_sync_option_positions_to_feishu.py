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


def test_build_feishu_payload_coerces_numeric_fields_from_strings() -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    payload = sync_mod.build_feishu_payload(
        "rec_local_1",
        {
            "broker": "富途",
            "account": "lx",
            "symbol": "TSLA",
            "option_type": "put",
            "side": "short",
            "contracts": "2",
            "contracts_open": "1",
            "contracts_closed": "1",
            "currency": "USD",
            "strike": "85",
            "premium": "1.25",
            "cash_secured_amount": "17000",
            "underlying_share_locked": "0",
            "status": "open",
        },
    )

    assert payload["contracts"] == 2
    assert isinstance(payload["contracts"], int)
    assert payload["contracts_open"] == 1
    assert isinstance(payload["contracts_open"], int)
    assert payload["strike"] == 85
    assert isinstance(payload["strike"], int)
    assert payload["premium"] == 1.25
    assert isinstance(payload["premium"], float)
    assert payload["cash_secured_amount"] == 17000
    assert isinstance(payload["cash_secured_amount"], int)


def test_build_outgoing_payload_applies_schema_filter_and_type_hints() -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    payload = sync_mod.build_outgoing_payload(
        "rec_local_1",
        {
            "broker": "富途",
            "account": "lx",
            "symbol": "TSLA",
            "option_type": "put",
            "side": "short",
            "contracts_open": "1",
            "strike": "85",
            "premium": "1.25",
            "note": "keep",
        },
        [
            {"field_name": "contracts_open", "type": "number"},
            {"field_name": "strike", "type": "number"},
            {"field_name": "premium", "type": "currency"},
            {"field_name": "note", "type": "text"},
        ],
    )

    assert payload == {
        "contracts_open": 1,
        "strike": 85,
        "premium": 1.25,
        "note": "keep",
    }


def test_sync_dry_run_accepts_read_only_repo(monkeypatch, tmp_path: Path) -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")

    class _ReadOnlyRepo:
        def list_position_lots(self) -> list[dict[str, Any]]:
            return [
                {
                    "record_id": "rec_local_1",
                    "fields": {
                        "broker": "富途",
                        "account": "lx",
                        "symbol": "TSLA",
                        "option_type": "put",
                        "side": "short",
                        "contracts": 1,
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100.0,
                        "expiration": 1781827200000,
                        "status": "open",
                    },
                }
            ]

    monkeypatch.setattr(sync_mod, "get_tenant_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(sync_mod, "bitable_fields", lambda *_args, **_kwargs: [{"field_name": "broker"}, {"field_name": "local_record_id"}])
    monkeypatch.setattr(sync_mod, "bitable_list_records", lambda *_args, **_kwargs: [])

    rows = sync_mod.sync_option_positions(repo=_ReadOnlyRepo(), data_config=data_config, apply_mode=False)
    assert rows[0]["record_id"] == "rec_local_1"


def test_sync_dry_run_reports_remote_orphan_delete_when_enabled(monkeypatch, tmp_path: Path) -> None:
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
    local_record_id = repo.list_position_lots()[0]["record_id"]

    monkeypatch.setattr(sync_mod, "get_tenant_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(sync_mod, "bitable_fields", lambda *_args, **_kwargs: [{"field_name": "broker"}, {"field_name": "local_record_id"}])
    monkeypatch.setattr(
        sync_mod,
        "bitable_list_records",
        lambda *_args, **_kwargs: [
            {"record_id": "rec_remote_keep", "fields": {"local_record_id": local_record_id}},
            {"record_id": "rec_remote_orphan", "fields": {"local_record_id": "lot_deleted"}},
        ],
    )

    rows = sync_mod.sync_option_positions(
        repo=repo,
        data_config=data_config,
        apply_mode=False,
        prune_remote_missing_local=True,
    )

    delete_rows = [row for row in rows if row.get("action") == "delete"]
    assert len(delete_rows) == 1
    assert delete_rows[0]["remote_record_id"] == "rec_remote_orphan"
    assert delete_rows[0]["reason"] == "remote_local_record_missing_from_local_projection"


def test_sync_dry_run_does_not_prune_remote_when_scan_is_limited(monkeypatch, tmp_path: Path) -> None:
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
    second = svc.persist_manual_open_event(
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
            premium_per_share=1.11,
            opened_at_ms=2000,
        ),
    )

    monkeypatch.setattr(sync_mod, "get_tenant_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(sync_mod, "bitable_fields", lambda *_args, **_kwargs: [{"field_name": "broker"}, {"field_name": "local_record_id"}])
    monkeypatch.setattr(
        sync_mod,
        "bitable_list_records",
        lambda *_args, **_kwargs: [
            {"record_id": "rec_remote_second", "fields": {"local_record_id": second["record_id"]}},
        ],
    )

    rows = sync_mod.sync_option_positions(
        repo=repo,
        data_config=data_config,
        apply_mode=False,
        prune_remote_missing_local=True,
        limit=1,
    )

    assert all(row.get("action") != "delete" for row in rows)


def test_sync_apply_deletes_remote_orphan_when_enabled(monkeypatch, tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc
    import scripts.sync_option_positions_to_feishu as sync_mod
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
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
    svc.persist_manual_void_event(repo, target_event_id=str(open_result["event_id"]), void_reason="cleanup", as_of_ms=2000)

    deleted: list[str] = []
    monkeypatch.setattr(sync_mod, "get_tenant_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(sync_mod, "bitable_fields", lambda *_args, **_kwargs: [{"field_name": "broker"}, {"field_name": "local_record_id"}])
    monkeypatch.setattr(
        sync_mod,
        "bitable_list_records",
        lambda *_args, **_kwargs: [
            {"record_id": "rec_remote_orphan", "fields": {"local_record_id": open_result["record_id"]}},
        ],
    )
    monkeypatch.setattr(sync_mod, "bitable_create_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not create")))
    monkeypatch.setattr(sync_mod, "bitable_update_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not update")))
    monkeypatch.setattr(sync_mod, "bitable_delete_record", lambda _t, _a, _tb, record_id: (deleted.append(record_id) or {}))

    rows = sync_mod.sync_option_positions(
        repo=repo,
        data_config=data_config,
        apply_mode=True,
        prune_remote_missing_local=True,
    )

    assert deleted == ["rec_remote_orphan"]
    assert any(row.get("action") == "delete" and row.get("remote_record_id") == "rec_remote_orphan" for row in rows)
    assert sync_mod.summarize_result(rows)["delete"] == 1


def test_match_remote_record_prefers_unique_local_record_id_before_duplicate_position_id() -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    record_id, reason = sync_mod.match_remote_record(
        "local_row_1",
        {
            "position_id": "PDD_20260515_90P_short",
            "source_event_id": "deal-open-1",
        },
        [
            {"record_id": "rec_dup_1", "fields": {"position_id": "PDD_20260515_90P_short"}},
            {"record_id": "rec_dup_2", "fields": {"position_id": "PDD_20260515_90P_short"}},
            {"record_id": "rec_unique", "fields": {"local_record_id": "local_row_1"}},
        ],
    )

    assert record_id == "rec_unique"
    assert reason == "local_record_id"


def test_match_remote_record_requires_account_safe_position_id_match() -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    record_id, reason = sync_mod.match_remote_record(
        "local_row_sy",
        {
            "account": "sy",
            "broker": "富途",
            "symbol": "0883.HK",
            "option_type": "call",
            "side": "short",
            "position_id": "0883_HK_20260528_30C_short",
            "source_event_id": "manual-open-2",
        },
        [
            {
                "record_id": "rec_lx",
                "fields": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "0883.HK",
                    "option_type": "call",
                    "side": "short",
                    "position_id": "0883_HK_20260528_30C_short",
                },
            },
            {
                "record_id": "rec_sy",
                "fields": {
                    "account": "sy",
                    "broker": "富途",
                    "symbol": "0883.HK",
                    "option_type": "call",
                    "side": "short",
                    "position_id": "0883_HK_20260528_30C_short",
                },
            },
        ],
    )

    assert record_id == "rec_sy"
    assert reason == "account+position_id"


def test_match_remote_record_does_not_cross_match_position_id_into_other_account() -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    record_id, reason = sync_mod.match_remote_record(
        "local_row_sy",
        {
            "account": "sy",
            "broker": "富途",
            "symbol": "0883.HK",
            "option_type": "call",
            "side": "short",
            "position_id": "0883_HK_20260528_30C_short",
        },
        [
            {
                "record_id": "rec_lx_only",
                "fields": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "0883.HK",
                    "option_type": "call",
                    "side": "short",
                    "position_id": "0883_HK_20260528_30C_short",
                },
            }
        ],
    )

    assert record_id is None
    assert reason == "no_remote_match"


def test_match_remote_record_reports_conflict_for_duplicate_source_event_id() -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    record_id, reason = sync_mod.match_remote_record(
        "lot_manual-open-1",
        {
            "account": "lx",
            "broker": "富途",
            "symbol": "TSLA",
            "option_type": "put",
            "side": "short",
            "source_event_id": "manual-open-1",
        },
        [
            {"record_id": "rec_dup_1", "fields": {"source_event_id": "manual-open-1"}},
            {"record_id": "rec_dup_2", "fields": {"source_event_id": "manual-open-1"}},
        ],
    )

    assert record_id is None
    assert reason.startswith("conflict: duplicate remote rows by source_event_id")


def test_sync_skips_unchanged_payload_even_when_last_synced_at_changes(monkeypatch, tmp_path: Path) -> None:
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
            symbol="NFLX",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=300.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.5,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]
    schema_fields = [
        {"field_name": "position_id"},
        {"field_name": "source_event_id"},
        {"field_name": "broker"},
        {"field_name": "account"},
        {"field_name": "symbol"},
        {"field_name": "option_type"},
        {"field_name": "side"},
        {"field_name": "contracts"},
        {"field_name": "contracts_open"},
        {"field_name": "currency"},
        {"field_name": "strike"},
        {"field_name": "expiration"},
        {"field_name": "premium"},
        {"field_name": "status"},
        {"field_name": "opened_at"},
        {"field_name": "last_action_at"},
        {"field_name": "local_record_id"},
        {"field_name": "last_synced_at"},
    ]
    payload = sync_mod.build_outgoing_payload(lot["record_id"], lot["fields"], schema_fields)
    patched_fields = dict(lot["fields"])
    patched_fields["feishu_record_id"] = "rec_existing"
    patched_fields["feishu_sync_hash"] = sync_mod.sync_payload_hash(payload)
    patched_fields["feishu_last_synced_at_ms"] = 1000
    repo.update_position_lot_fields(lot["record_id"], patched_fields)

    monkeypatch.setattr(sync_mod, "get_tenant_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(
        sync_mod,
        "bitable_fields",
        lambda *_args, **_kwargs: schema_fields,
    )
    monkeypatch.setattr(sync_mod, "bitable_list_records", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sync_mod, "bitable_update_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unchanged payload should skip update")))

    rows = sync_mod.sync_option_positions(repo=repo, data_config=data_config, apply_mode=True)

    assert rows[0]["action"] == "skip"
    assert rows[0]["reason"] == "payload_unchanged"


def test_select_candidates_keeps_rows_older_than_since_updated_ms() -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    rows = sync_mod.select_candidates(
        [
            {"record_id": "rec_old", "fields": {"feishu_last_synced_at_ms": 1000}},
            {"record_id": "rec_new", "fields": {"feishu_last_synced_at_ms": 5000}},
            {"record_id": "rec_never", "fields": {}},
        ],
        only_record_id=None,
        only_open=False,
        since_updated_ms=3000,
        limit=None,
    )

    selected = [row.record_id for row in rows]
    assert "rec_old" in selected
    assert "rec_never" in selected
    assert "rec_new" not in selected


def test_with_table_token_refreshes_once_on_auth_error(monkeypatch) -> None:
    import scripts.sync_option_positions_to_feishu as sync_mod

    class _Ref:
        app_id = "app_id"
        app_secret = "app_secret"

    calls: list[bool] = []

    def _fake_get_token(_app_id: str, _app_secret: str, *, force_refresh: bool = False) -> str:
        calls.append(force_refresh)
        return "fresh-token" if force_refresh else "stale-token"

    attempts = {"n": 0}

    def _fn(token: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sync_mod.FeishuAuthError(f"expired:{token}")
        return token

    monkeypatch.setattr(sync_mod, "get_tenant_access_token", _fake_get_token)

    out = sync_mod._with_table_token(_Ref(), _fn)

    assert out == "fresh-token"
    assert calls == [False, True]
    assert attempts["n"] == 2


def test_sync_apply_create_persists_metadata_without_touching_business_fields(monkeypatch, tmp_path: Path) -> None:
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
    lot = repo.list_position_lots()[0]
    original_fields = dict(lot["fields"])

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
    monkeypatch.setattr(
        sync_mod,
        "bitable_create_record",
        lambda *_args, **_kwargs: {"record": {"record_id": "rec_created_1"}},
    )
    monkeypatch.setattr(sync_mod, "bitable_update_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not update")))

    rows = sync_mod.sync_option_positions(repo=repo, data_config=data_config, apply_mode=True)

    refreshed_fields = repo.get_position_lot_fields(lot["record_id"])
    assert rows[0]["action"] == "create"
    assert refreshed_fields["broker"] == original_fields["broker"]
    assert refreshed_fields["account"] == original_fields["account"]
    assert refreshed_fields["position_id"] == original_fields["position_id"]
    assert refreshed_fields["source_event_id"] == original_fields["source_event_id"]
    assert refreshed_fields["contracts_open"] == original_fields["contracts_open"]
    assert refreshed_fields["status"] == original_fields["status"]
    assert refreshed_fields["feishu_record_id"] == "rec_created_1"
    assert refreshed_fields["feishu_sync_hash"]
    assert int(refreshed_fields["feishu_last_synced_at_ms"]) > 0


def test_sync_apply_update_sends_numeric_payload_types(monkeypatch, tmp_path: Path) -> None:
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
            symbol="PDD",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=90.0,
            multiplier=100,
            expiration_ymd="2026-05-15",
            premium_per_share=0.8,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]
    patched_fields = dict(lot["fields"])
    patched_fields["feishu_record_id"] = "rec_existing"
    patched_fields["strike"] = "85"
    patched_fields["contracts_open"] = "1"
    patched_fields["premium"] = "0.75"
    patched_fields["cash_secured_amount"] = "8500"
    repo.update_position_lot_fields(lot["record_id"], patched_fields)

    captured: dict[str, Any] = {}

    monkeypatch.setattr(sync_mod, "get_tenant_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(
        sync_mod,
        "bitable_fields",
        lambda *_args, **_kwargs: [
            {"field_name": "position_id"},
            {"field_name": "contracts_open"},
            {"field_name": "strike"},
            {"field_name": "premium"},
            {"field_name": "cash_secured_amount"},
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
    monkeypatch.setattr(sync_mod, "bitable_create_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not create")))

    rows = sync_mod.sync_option_positions(repo=repo, data_config=data_config, apply_mode=True)

    sent = captured["fields"]
    assert rows[0]["action"] == "update"
    assert sent["strike"] == 85
    assert isinstance(sent["strike"], int)
    assert sent["contracts_open"] == 1
    assert isinstance(sent["contracts_open"], int)
    assert sent["premium"] == 0.75
    assert isinstance(sent["premium"], float)
    assert sent["cash_secured_amount"] == 8500
    assert isinstance(sent["cash_secured_amount"], int)
