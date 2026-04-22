from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _write_pm_config(path: Path, *, sqlite_path: Path, with_feishu: bool = True) -> Path:
    payload: dict[str, object] = {
        "option_positions": {"sqlite_path": str(sqlite_path)},
    }
    if with_feishu:
        payload["feishu"] = {
            "app_id": "app_id",
            "app_secret": "app_secret",
            "tables": {"option_positions": "app_token/table_id"},
        }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_load_option_positions_repo_bootstraps_from_feishu_when_sqlite_empty(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    pm_config = _write_pm_config(tmp_path / "pm.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    old_list = svc.FeishuOptionPositionsRepository.list_records
    try:
        svc.FeishuOptionPositionsRepository.list_records = lambda self, page_size=500: [  # type: ignore[assignment]
            {
                "record_id": "rec_1",
                "fields": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "NVDA",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": 1,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                },
            }
        ]
        repo = svc.load_option_positions_repo(pm_config)
    finally:
        svc.FeishuOptionPositionsRepository.list_records = old_list  # type: ignore[assignment]

    records = repo.list_records(page_size=10)
    assert len(records) == 1
    assert records[0]["record_id"] == "rec_1"
    entry = repo.primary_repo.get_entry("rec_1")
    assert entry["backup_status"] == svc.BACKUP_STATUS_SYNCED
    assert entry["backup_record_id"] == "rec_1"


def test_primary_with_backup_create_and_update_syncs_to_backup(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    class _FakeBackupRepo:
        def __init__(self):
            self.creates: list[dict] = []
            self.updates: list[tuple[str, dict]] = []

        def create_record(self, fields: dict[str, object]) -> dict[str, object]:
            self.creates.append(dict(fields))
            return {"record": {"record_id": "bk_1"}}

        def update_record(self, record_id: str, fields: dict[str, object]) -> dict[str, object]:
            self.updates.append((str(record_id), dict(fields)))
            return {"record": {"record_id": str(record_id)}}

    sqlite_repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    backup_repo = _FakeBackupRepo()
    repo = svc.PrimaryWithBackupOptionPositionsRepository(primary_repo=sqlite_repo, backup_repo=backup_repo)  # type: ignore[arg-type]

    created = repo.create_record({"account": "lx", "symbol": "NVDA", "status": "open"})
    record_id = created["record"]["record_id"]
    entry = sqlite_repo.get_entry(record_id)
    assert entry["backup_status"] == svc.BACKUP_STATUS_SYNCED
    assert entry["backup_record_id"] == "bk_1"
    assert backup_repo.creates == [{"account": "lx", "symbol": "NVDA", "status": "open"}]

    repo.update_record(record_id, {"status": "close", "contracts_open": 0})
    entry2 = sqlite_repo.get_entry(record_id)
    assert entry2["backup_status"] == svc.BACKUP_STATUS_SYNCED
    assert backup_repo.updates
    backup_record_id, synced_fields = backup_repo.updates[-1]
    assert backup_record_id == "bk_1"
    assert synced_fields["account"] == "lx"
    assert synced_fields["symbol"] == "NVDA"
    assert synced_fields["status"] == "close"
    assert synced_fields["contracts_open"] == 0


def test_sync_backup_recovers_failed_backup_record(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    class _FlakyBackupRepo:
        def __init__(self):
            self.create_attempts = 0

        def create_record(self, fields: dict[str, object]) -> dict[str, object]:
            self.create_attempts += 1
            if self.create_attempts == 1:
                raise RuntimeError("backup unavailable")
            return {"record": {"record_id": "bk_retry"}}

        def update_record(self, record_id: str, fields: dict[str, object]) -> dict[str, object]:
            return {"record": {"record_id": str(record_id)}}

    sqlite_repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    backup_repo = _FlakyBackupRepo()
    repo = svc.PrimaryWithBackupOptionPositionsRepository(primary_repo=sqlite_repo, backup_repo=backup_repo)  # type: ignore[arg-type]

    created = repo.create_record({"account": "sy", "symbol": "AAPL", "status": "open"})
    record_id = created["record"]["record_id"]
    failed_entry = sqlite_repo.get_entry(record_id)
    assert failed_entry["backup_status"] == svc.BACKUP_STATUS_ERROR
    assert failed_entry["backup_record_id"] is None
    assert "backup unavailable" in str(failed_entry["backup_error"])

    sync_result = repo.sync_backup(limit=10, dry_run=False)
    assert sync_result["processed"] == 1
    assert sync_result["synced"] == 1
    recovered_entry = sqlite_repo.get_entry(record_id)
    assert recovered_entry["backup_status"] == svc.BACKUP_STATUS_SYNCED
    assert recovered_entry["backup_record_id"] == "bk_retry"


def test_load_option_positions_repo_supports_sqlite_only_mode(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    pm_config = _write_pm_config(
        tmp_path / "pm.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
        with_feishu=False,
    )

    repo = svc.load_option_positions_repo(pm_config)
    created = repo.create_record({"account": "lx", "symbol": "TSLA", "status": "open"})
    record_id = created["record"]["record_id"]

    records = repo.list_records(page_size=10)
    assert len(records) == 1
    assert records[0]["record_id"] == record_id
    entry = repo.primary_repo.get_entry(record_id)
    assert entry["backup_status"] == svc.BACKUP_STATUS_DISABLED
    assert entry["backup_record_id"] is None


def test_sqlite_list_records_keeps_full_scan_semantics(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for idx in range(550):
        repo.create_record(
            {"account": "lx", "symbol": f"SYM{idx}", "status": "open"},
            record_id=f"rec_{idx}",
            backup_status=svc.BACKUP_STATUS_DISABLED,
        )

    records = repo.list_records(page_size=500)
    assert len(records) == 550


def test_load_option_positions_repo_raises_on_malformed_feishu_config(tmp_path: Path) -> None:
    import pytest
    import scripts.option_positions_core.service as svc

    pm_config = tmp_path / "pm.json"
    pm_config.write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": str(tmp_path / "option_positions.sqlite3")},
                "feishu": {"app_id": "app_only"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="pm config missing feishu app_id/app_secret/option_positions"):
        svc.load_option_positions_repo(pm_config)


def test_load_option_positions_repo_rejects_non_object_feishu_config(tmp_path: Path) -> None:
    import pytest
    import scripts.option_positions_core.service as svc

    pm_config = tmp_path / "pm.json"
    pm_config.write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": str(tmp_path / "option_positions.sqlite3")},
                "feishu": "invalid",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="pm config feishu must be a JSON object"):
        svc.load_option_positions_repo(pm_config)


def test_load_option_positions_repo_bootstrap_failure_does_not_block_sqlite(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    pm_config = _write_pm_config(tmp_path / "pm.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    old_list = svc.FeishuOptionPositionsRepository.list_records
    old_create = svc.FeishuOptionPositionsRepository.create_record
    try:
        def _raise(*_args, **_kwargs):
            raise RuntimeError("feishu unavailable")

        svc.FeishuOptionPositionsRepository.list_records = _raise  # type: ignore[assignment]
        svc.FeishuOptionPositionsRepository.create_record = _raise  # type: ignore[assignment]
        repo = svc.load_option_positions_repo(pm_config)
        assert repo.primary_repo.count_records() == 0
        assert repo.backup_repo is not None
        created = repo.create_record({"account": "lx", "symbol": "AMD", "status": "open"})
        entry = repo.primary_repo.get_entry(str(created["record"]["record_id"]))
        assert entry["backup_status"] == svc.BACKUP_STATUS_ERROR
    finally:
        svc.FeishuOptionPositionsRepository.list_records = old_list  # type: ignore[assignment]
        svc.FeishuOptionPositionsRepository.create_record = old_create  # type: ignore[assignment]
