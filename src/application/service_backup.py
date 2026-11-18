"""Operator workflow for offline backup packages and durable readback receipts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any

from src.application.encrypted_backup_package import publish_encrypted_backup, restore_encrypted_backup
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
        if not isinstance(value, str) or len(value) != 64 or not set(value) <= set("0123456789abcdef"):
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
