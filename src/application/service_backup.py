"""Operator workflow for offline backup packages and durable readback receipts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any

from src.application.encrypted_backup_package import _mounted_target, publish_encrypted_backup, restore_encrypted_backup
from src.application.offline_backup_snapshot import OfflineBackupError, stage_offline_backup


_RECEIPT_SCHEMA = "om_backup_publication.v1"
_RESTORE_SCHEMA = "om_backup_restore_verification.v1"
_CONTROL_BYTES = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _control_object(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise OfflineBackupError("backup control file must be a regular file")
    with path.open("rb") as handle:
        raw = handle.read(_CONTROL_BYTES + 1)
    if len(raw) > _CONTROL_BYTES:
        raise OfflineBackupError("backup control file exceeds size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise OfflineBackupError("backup control file must contain an object")
    return value, raw


def _new_receipt_path(path: Path) -> Path:
    path = path.absolute()
    if path != path.resolve() or not path.parent.is_dir() or os.path.lexists(path):
        raise OfflineBackupError("receipt requires a new path in an existing directory")
    return path


def _write_receipt(path: Path, record: dict[str, Any]) -> None:
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with NamedTemporaryFile(prefix=".backup-receipt-", dir=path.parent) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        os.link(handle.name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if path.read_bytes() != data:
        raise OfflineBackupError("backup receipt readback mismatch")


def create_service_backup(
    *, source: Path, files_path: Path, staging_root: Path, target: Path, mount: Path,
    expected_device: int, filename: str, recipient_cert: Path, receipt_path: Path,
    max_bytes: int, timeout: float, writers_stopped: bool,
) -> dict[str, Any]:
    if writers_stopped is not True:
        raise OfflineBackupError("all source writers must be stopped before backup")
    receipt_path = _new_receipt_path(receipt_path)
    source = source.resolve(strict=True)
    staging_root = staging_root.absolute()
    if (
        staging_root != staging_root.resolve(strict=True) or not staging_root.is_dir()
        or staging_root.is_relative_to(source) or source.is_relative_to(staging_root)
        or staging_root.is_relative_to(mount.resolve())
        or receipt_path.is_relative_to(source)
    ):
        raise OfflineBackupError("staging must be separate from source and backup mount; receipt must be outside source")
    if type(max_bytes) is not int or max_bytes <= 0 or shutil.disk_usage(staging_root).free < 4 * max_bytes:
        raise OfflineBackupError("backup staging requires four times the byte budget in free space")
    files, _ = _control_object(files_path)
    if not files or any(not isinstance(kind, str) for kind in files.values()):
        raise OfflineBackupError("file list must map relative paths to file or sqlite")
    snapshot_at = _utc_now()
    with TemporaryDirectory(prefix=".om-backup-", dir=staging_root) as tmp:
        snapshot = Path(tmp) / "snapshot"
        manifest = stage_offline_backup(
            source=source, destination=snapshot, files=files, max_bytes=max_bytes,
        )
        ciphertext = publish_encrypted_backup(
            snapshot=snapshot, manifest_sha256=manifest, target=target.absolute(),
            mount=mount.absolute(), expected_device=expected_device, filename=filename,
            recipient_cert=recipient_cert, max_bytes=max_bytes, timeout=timeout,
        )
    record = {
        "schema": _RECEIPT_SCHEMA,
        "status": "published_readback",
        "snapshot_at_utc": snapshot_at,
        "published_at_utc": _utc_now(),
        "filename": filename,
        "ciphertext_sha256": ciphertext,
        "manifest_sha256": manifest,
    }
    _write_receipt(receipt_path, record)
    return record


def restore_service_backup(
    *, package: Path, receipt_path: Path, verification_receipt: Path,
    destination: Path, private_key: Path, max_bytes: int, max_files: int, timeout: float,
) -> dict[str, Any]:
    receipt, receipt_bytes = _control_object(receipt_path)
    verification_receipt = _new_receipt_path(verification_receipt)
    destination = destination.absolute()
    if (
        receipt.get("schema") != _RECEIPT_SCHEMA or receipt.get("status") != "published_readback"
        or receipt.get("filename") != package.name
        or verification_receipt.is_relative_to(destination)
    ):
        raise OfflineBackupError("publication receipt does not match package or restore target")
    for key in ("ciphertext_sha256", "manifest_sha256"):
        value = receipt.get(key)
        if not _valid_digest(value):
            raise OfflineBackupError("publication receipt has an invalid digest")
    if type(max_bytes) is not int or max_bytes <= 0 or shutil.disk_usage(destination.parent).free < 2 * max_bytes:
        raise OfflineBackupError("restore requires twice the byte budget in free space")
    restore_encrypted_backup(
        package=package, ciphertext_sha256=receipt["ciphertext_sha256"],
        manifest_sha256=receipt["manifest_sha256"], destination=destination,
        private_key=private_key, max_bytes=max_bytes, max_files=max_files, timeout=timeout,
    )
    record = {
        "schema": _RESTORE_SCHEMA,
        "status": "package_restored_and_verified",
        "restored_at_utc": _utc_now(),
        "filename": package.name,
        "publication_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "ciphertext_sha256": receipt["ciphertext_sha256"],
        "manifest_sha256": receipt["manifest_sha256"],
    }
    _write_receipt(verification_receipt, record)
    return record


def _receipt_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise OfflineBackupError("backup receipt timestamp is missing")
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(None):
        raise OfflineBackupError("backup receipt timestamp must be UTC")
    return timestamp


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")


def preview_backup_rotation(
    *, inventory: Path, target: Path, mount: Path, expected_device: int, max_verify_bytes: int,
) -> dict[str, Any]:
    """Plan, but never delete, packages listed by a trusted backup operator."""
    if type(max_verify_bytes) is not int or max_verify_bytes <= 0:
        raise OfflineBackupError("verification byte budget must be positive")
    inventory_data, _ = _control_object(inventory)
    entries = inventory_data.get("entries")
    if inventory_data.get("schema") != "om_backup_rotation_inventory.v1" or not isinstance(entries, list) or not 0 < len(entries) <= 1000:
        raise OfflineBackupError("backup rotation needs a bounded explicit receipt inventory")
    target, mount = target.absolute(), mount.absolute()
    rows: dict[str, dict[str, Any]] = {}
    series = inventory_data.get("series")
    if not isinstance(series, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", series):
        raise OfflineBackupError("rotation must identify one backup series")
    filename_pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z_" + re.escape(series) + r"_(full|pre-upgrade)\.tar\.gz\.cms")
    now = _receipt_time(_utc_now())
    with _mounted_target(mount, target, expected_device) as fd:
        target_inode = os.fstat(fd).st_ino
        for item in entries:
            if (
                not isinstance(item, dict)
                or set(item) - {"publication", "verification", "pinned"}
                or not isinstance(item.get("publication"), str)
                or type(item.get("pinned", False)) is not bool
                or ("verification" in item and not isinstance(item["verification"], str))
            ):
                raise OfflineBackupError("backup rotation entry is invalid")
            receipt, raw = _control_object(inventory.parent / item["publication"])
            receipt_hash = hashlib.sha256(raw).hexdigest()
            filename = receipt.get("filename")
            if (
                receipt.get("schema") != _RECEIPT_SCHEMA or receipt.get("status") != "published_readback"
                or not isinstance(filename, str) or not filename_pattern.fullmatch(filename)
                or filename in rows
                or not all(_valid_digest(receipt.get(key)) for key in ("ciphertext_sha256", "manifest_sha256"))
            ):
                raise OfflineBackupError("backup rotation publication receipt is invalid or duplicated")
            snapshot = _receipt_time(receipt.get("snapshot_at_utc"))
            published = _receipt_time(receipt.get("published_at_utc"))
            if not snapshot <= published <= now:
                raise OfflineBackupError("backup publication chronology is invalid")
            package_info = os.stat(filename, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(package_info.st_mode) or package_info.st_size <= 0:
                raise OfflineBackupError("backup rotation package is not a regular nonempty file")
            verified = False
            proof_hash = None
            if "verification" in item:
                proof, proof_raw = _control_object(inventory.parent / item["verification"])
                if (
                    proof.get("schema") != _RESTORE_SCHEMA
                    or proof.get("status") != "package_restored_and_verified"
                    or proof.get("publication_receipt_sha256") != receipt_hash
                    or not published <= _receipt_time(proof.get("restored_at_utc")) <= now
                    or any(proof.get(key) != receipt[key] for key in ("filename", "ciphertext_sha256", "manifest_sha256"))
                ):
                    raise OfflineBackupError("restore verification receipt does not bind the publication")
                verified = True
                proof_hash = hashlib.sha256(proof_raw).hexdigest()
            rows[filename] = {
                "filename": filename, "snapshot_at_utc": snapshot.isoformat(),
                "ciphertext_sha256": receipt["ciphertext_sha256"],
                "publication_receipt_sha256": receipt_hash, "verification_receipt_sha256": proof_hash,
                "pinned": item.get("pinned", False), "restore_verified": verified,
                "package_identity": [package_info.st_dev, package_info.st_ino, package_info.st_size,
                                     package_info.st_mtime_ns, package_info.st_ctime_ns],
                "allocated_file_bytes": package_info.st_blocks * 512,
            }
        ordered = sorted(rows.values(), key=lambda row: (row["snapshot_at_utc"], row["filename"]), reverse=True)
        witness = next((row for row in ordered if row["restore_verified"]), None)
        if witness:
            if witness["package_identity"][2] > max_verify_bytes:
                raise OfflineBackupError("verified backup exceeds readback byte budget")
            with os.fdopen(os.open(witness["filename"], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), "rb") as handle:
                info = os.fstat(handle.fileno())
                current = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                if current != witness["package_identity"]:
                    raise OfflineBackupError("verified backup changed")
                digest = hashlib.sha256()
                remaining = info.st_size
                while remaining:
                    block = handle.read(min(1024 * 1024, remaining))
                    if not block:
                        raise OfflineBackupError("verified backup was truncated")
                    remaining -= len(block)
                    digest.update(block)
                if digest.hexdigest() != witness["ciphertext_sha256"] or os.fstat(handle.fileno()).st_ctime_ns != info.st_ctime_ns:
                    raise OfflineBackupError("verified backup readback mismatch")

    periods: dict[str, set] = {"daily": set(), "weekly": set(), "monthly": set()}
    limits = {"daily": 7, "weekly": 4, "monthly": 3}
    for row in ordered:
        timestamp = _receipt_time(row["snapshot_at_utc"])
        day, week = timestamp.date(), timestamp.isocalendar()
        buckets = {"daily": day, "weekly": (week.year, week.week), "monthly": (day.year, day.month)}
        reasons = []
        for name, key in buckets.items():
            if key not in periods[name] and len(periods[name]) < limits[name]:
                periods[name].add(key)
                reasons.append(name)
        if row["pinned"] or row["filename"].endswith("_pre-upgrade.tar.gz.cms"):
            reasons.append("pinned_or_pre_upgrade")
        if witness is None:
            reasons.append("restore_verification_unavailable")
        elif row["snapshot_at_utc"] >= witness["snapshot_at_utc"]:
            reasons.append("at_or_after_latest_verified_restore")
        row["retain_reasons"] = reasons
    candidates = [row for row in ordered if not row["retain_reasons"]]
    plan = {
        "schema": "om_backup_rotation_plan.v1", "target": str(target),
        "mount": str(mount), "expected_device": expected_device, "target_inode": target_inode,
        "series": series, "retention": limits,
        "minimum_restorable_backup": None if witness is None else witness["filename"],
        "entries": ordered, "deletion_candidates": [row["filename"] for row in candidates],
        "estimated_allocated_file_bytes": sum(row["allocated_file_bytes"] for row in candidates),
    }
    plan["plan_sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return plan
