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
            "backup_scope_sha256": "a" * 64,
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


def test_retire_binds_preview_and_never_removes_recovery_point(rotation, tmp_path):
    args, names = rotation
    expected = service_backup.preview_backup_rotation(**args)["plan_sha256"]
    receipt = tmp_path / "retirement.jsonl"
    apply = {**args, "expected_plan_sha256": expected, "receipt_path": receipt}
    with pytest.raises(OfflineBackupError, match="requires confirmation"):
        service_backup.retire_backup_rotation(**apply, confirm=False)
    assert not receipt.exists()
    with pytest.raises(OfflineBackupError, match="preview changed"):
        service_backup.retire_backup_rotation(**{**apply, "expected_plan_sha256": "0" * 64}, confirm=True)
    assert not receipt.exists()

    result = service_backup.retire_backup_rotation(**apply, confirm=True)
    assert result["removed"] == names[7:11]
    assert {p.name for p in args["target"].iterdir()} == set(names[:7] + names[11:])
    events = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert [x["status"] for x in events] == ["started"] + ["deleting", "removed"] * 4 + ["complete"]
    assert events[0]["retained_restore"] == names[1]
    assert events[-1]["removed_count"] == 4
    with pytest.raises(OfflineBackupError, match="new path"):
        service_backup.retire_backup_rotation(**apply, confirm=True)


def test_retire_rejects_fresh_plan_if_candidate_content_is_wrong(rotation, tmp_path):
    args, names = rotation
    bad = args["target"] / names[10]
    bad.write_bytes(b"not the expected data")
    expected = service_backup.preview_backup_rotation(**args)["plan_sha256"]
    receipt = tmp_path / "retirement.jsonl"
    with pytest.raises(OfflineBackupError, match="candidate readback mismatch"):
        service_backup.retire_backup_rotation(
            **args, expected_plan_sha256=expected, receipt_path=receipt, confirm=True,
        )
    assert not receipt.exists()
    assert len(list(args["target"].iterdir())) == 12


def test_interrupted_retirement_leaves_durable_progress(rotation, tmp_path, monkeypatch):
    args, names = rotation
    expected = service_backup.preview_backup_rotation(**args)["plan_sha256"]
    receipt = tmp_path / "retirement.jsonl"
    original = service_backup.os.unlink

    def interrupted(path, *a, **kw):
        if path == names[8]:
            raise OSError("injected failure")
        return original(path, *a, **kw)

    with monkeypatch.context() as ctx:
        ctx.setattr(service_backup.os, "unlink", interrupted)
        with pytest.raises(OSError, match="injected failure"):
            service_backup.retire_backup_rotation(
                **args, expected_plan_sha256=expected, receipt_path=receipt, confirm=True,
            )
    events = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert [x["status"] for x in events] == ["started", "deleting", "removed", "deleting"]
    assert not (args["target"] / names[7]).exists()
    assert (args["target"] / names[8]).exists()
    assert (args["target"] / names[1]).exists()


def test_partial_new_backup_cannot_replace_full_recovery_scope(rotation):
    args, _ = rotation
    publication = args["inventory"].parent / "publication-1.json"
    proof = args["inventory"].parent / "verification.json"
    newer, verified = (json.loads(path.read_bytes()) for path in (publication, proof))
    newer["backup_scope_sha256"] = "b" * 64
    publication.write_text(json.dumps(newer))
    verified["backup_scope_sha256"] = newer["backup_scope_sha256"]
    verified["publication_receipt_sha256"] = hashlib.sha256(publication.read_bytes()).hexdigest()
    proof.write_text(json.dumps(verified))

    with pytest.raises(OfflineBackupError, match="different recovery scopes"):
        service_backup.preview_backup_rotation(**args)
    assert len(list(args["target"].iterdir())) == 12


def test_pre_upgrade_rotation_requires_explicit_closed_window(rotation):
    args, names = rotation
    first = service_backup.preview_backup_rotation(**args)
    assert names[11] not in first["deletion_candidates"]
    payload = json.loads(args["inventory"].read_bytes())
    payload["entries"][11]["rollback_window_closed"] = True
    args["inventory"].write_text(json.dumps(payload))
    closed = service_backup.preview_backup_rotation(**args)
    assert names[11] in closed["deletion_candidates"]
    assert closed["plan_sha256"] != first["plan_sha256"]


@pytest.mark.parametrize("field", ["publication", "verification"])
@pytest.mark.parametrize("path", ["/absolute/path.json", "../outside.json"])
def test_rotation_rejects_nonportable_receipt_paths(rotation, field, path):
    args, _ = rotation
    payload = json.loads(args["inventory"].read_bytes())
    payload["entries"][1][field] = path
    args["inventory"].write_text(json.dumps(payload))
    with pytest.raises(OfflineBackupError, match="relative to the inventory directory"):
        service_backup.preview_backup_rotation(**args)
