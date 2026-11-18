from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from src.application import service_backup
from src.application.offline_backup_snapshot import OfflineBackupError


@pytest.fixture
def rotation(tmp_path, monkeypatch):
    mount = tmp_path / "volume"
    mount.mkdir()
    monkeypatch.setattr(Path, "is_mount", lambda path: path == mount)
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(service_backup, "_utc_now", lambda: now.isoformat())
    inventory = tmp_path / "inventory.json"
    entries, filenames = [], []

    def write(path, value):
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    for index in range(12):
        date = now - timedelta(days=index * 10 + 1)
        purpose = "pre-upgrade" if index == 11 else "full"
        name = date.strftime("%Y-%m-%dT%H%M%SZ") + f"_prod_a_{purpose}.tar.gz.cms"
        filenames.append(name)
        data = f"isolated fixture {index}".encode()
        (mount / name).write_bytes(data)
        identity = {
            "filename": name,
            "ciphertext_sha256": hashlib.sha256(data).hexdigest(),
            "manifest_sha256": "f" * 64,
        }
        publication = f"publication-{index}.json"
        receipt_hash = write(tmp_path / publication, {
            **identity, "schema": "om_backup_publication.v1", "status": "published_readback",
            "snapshot_at_utc": date.isoformat(), "published_at_utc": date.isoformat(),
        })
        row = {"publication": publication}
        if index == 1:
            row["verification"] = "verification.json"
            write(tmp_path / row["verification"], {
                **identity, "schema": "om_backup_restore_verification.v1",
                "status": "package_restored_and_verified", "restored_at_utc": now.isoformat(),
                "publication_receipt_sha256": receipt_hash,
            })
        entries.append(row)
    write(inventory, {"schema": "om_backup_rotation_inventory.v1", "series": "prod_a", "entries": entries})
    args = dict(inventory=inventory, target=mount, mount=mount,
                expected_device=mount.stat().st_dev, max_verify_bytes=1000)
    return args, filenames


def test_rotation_preview_keeps_calendar_points_and_restore_witness(rotation):
    args, names = rotation
    first = service_backup.preview_backup_rotation(**args)
    assert first == service_backup.preview_backup_rotation(**args)
    assert first["minimum_restorable_backup"] == names[1]
    assert first["deletion_candidates"] == names[7:11]
    assert sorted(p.name for p in args["target"].iterdir()) == sorted(names)
    assert {x["filename"] for x in first["entries"] if "monthly" in x["retain_reasons"]} == {
        names[0], names[2], names[5],
    }


def test_rotation_fails_closed_without_bound_verification(rotation):
    args, names = rotation
    witness = args["target"] / names[1]
    witness.write_bytes(b"isolated fixture 9")
    with pytest.raises(OfflineBackupError, match="readback mismatch"):
        service_backup.preview_backup_rotation(**args)
    with pytest.raises(OfflineBackupError, match="byte budget"):
        service_backup.preview_backup_rotation(**{**args, "max_verify_bytes": 1})
    payload = json.loads(args["inventory"].read_bytes())
    payload["entries"][1].pop("verification")
    args["inventory"].write_text(json.dumps(payload))
    plan = service_backup.preview_backup_rotation(**args)
    assert plan["minimum_restorable_backup"] is None
    assert plan["deletion_candidates"] == []


def test_rotation_cli_prints_plan_without_deleting(rotation, capsys):
    from src.interfaces.cli.main import main

    args, names = rotation
    assert main(["service", "backup-rotation-preview"] + [
        value for key, argument in args.items() for value in ("--" + key.replace("_", "-"), str(argument))
    ]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["data"]["deletion_candidates"] == names[7:11]
    assert len(list(args["target"].iterdir())) == len(names)


def test_rotation_refuses_mixing_series_and_respects_pins(rotation):
    args, names = rotation
    payload = json.loads(args["inventory"].read_bytes())
    payload["entries"][7]["pinned"] = True
    args["inventory"].write_text(json.dumps(payload))
    assert service_backup.preview_backup_rotation(**args)["deletion_candidates"] == names[8:11]

    payload["series"] = "different_host"
    args["inventory"].write_text(json.dumps(payload))
    with pytest.raises(OfflineBackupError, match="publication receipt"):
        service_backup.preview_backup_rotation(**args)
