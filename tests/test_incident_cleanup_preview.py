from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.application import incident_cleanup_preview as preview


def test_preview_protects_latest_backup_and_recent_log(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    tmp = tmp_path / "tmp"
    apps = tmp_path / "apps"
    units = tmp_path / "units"
    opend = tmp_path / "opend"
    proc = tmp_path / "proc"
    for path in (runtime, tmp, apps, units, opend, proc):
        path.mkdir()
    state = runtime / "output_shared" / "state"
    state.mkdir(parents=True)
    snapshot = state / preview.SNAPSHOTS[0]
    snapshot.mkdir()
    (snapshot / "data").write_bytes(b"abc")
    backups = state / "backups"
    backups.mkdir()
    (backups / "latest.sqlite3").write_bytes(b"backup")
    log = opend / "today.ftlog"
    log.write_bytes(b"log")
    monkeypatch.setattr(preview, "_open_references", lambda paths, root: {p: [] for p in paths})
    monkeypatch.setattr(preview, "_text_references", lambda paths, roots: {p: [] for p in paths})
    monkeypatch.setattr(preview, "_symlink_references", lambda paths, roots: {p: [] for p in paths})
    result = preview.preview_incident_cleanup(runtime_root=runtime, tmp_root=tmp, apps_root=apps,
        unit_root=units, opend_log_root=opend, proc_root=proc,
        now=datetime.now(timezone.utc) + timedelta(minutes=1))
    by_path = {item["path"]: item for item in result["items"]}
    assert by_path[str(snapshot)]["protected"] is False
    assert by_path[str(backups)]["reason"] == ["latest_migration_backup"]
    assert by_path[str(log)]["reason"] == ["within_7_days"]
    assert result["estimated_releasable_bytes"] >= 3
    assert "delete" not in result


def test_unknown_reference_check_protects_candidate(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    snapshot = runtime / "output_shared" / "state" / preview.SNAPSHOTS[0]
    snapshot.mkdir(parents=True)
    monkeypatch.setattr(preview, "_open_references", lambda paths, root: {p: None for p in paths})
    monkeypatch.setattr(preview, "_text_references", lambda paths, roots: {p: None for p in paths})
    monkeypatch.setattr(preview, "_symlink_references", lambda paths, roots: {p: None for p in paths})
    out = preview.preview_incident_cleanup(runtime_root=runtime, tmp_root=tmp_path / "tmp",
        apps_root=tmp_path / "apps", unit_root=tmp_path / "units",
        opend_log_root=tmp_path / "opend", proc_root=tmp_path / "proc")
    item = next(item for item in out["items"] if item["path"] == str(snapshot))
    assert item["protected"] is True
    assert "open_file_check_unavailable" in item["reason"]
    assert out["estimated_releasable_bytes"] == 0
