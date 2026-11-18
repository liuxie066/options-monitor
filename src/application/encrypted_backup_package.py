"""Seal offline OM snapshots and verify restoration without touching service state."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from typing import Iterator
from uuid import uuid4

from src.application.offline_backup_snapshot import OfflineBackupError, verify_offline_backup


def _openssl(*args: str, timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise OfflineBackupError("backup crypto timeout must be positive and finite")
    try:
        subprocess.run(
            ["openssl", "cms", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OfflineBackupError("backup encryption or decryption failed") from exc


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _mount_identity(mount: Path) -> tuple[int, int]:
    if mount != mount.resolve() or not mount.is_mount():
        raise OfflineBackupError("backup target is not the configured mount")
    info = mount.stat()
    return info.st_dev, info.st_ino


@contextmanager
def _mounted_target(mount: Path, target: Path, expected_device: int) -> Iterator[int]:
    identity = _mount_identity(mount)
    if identity[0] != expected_device:
        raise OfflineBackupError("backup target device mismatch")
    if target != target.resolve() or not target.is_relative_to(mount):
        raise OfflineBackupError("backup target escapes the configured mount")
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_dev != expected_device or identity != _mount_identity(mount):
            raise OfflineBackupError("backup target mount changed")
        yield fd
        if identity != _mount_identity(mount):
            raise OfflineBackupError("backup publication needs independent readback")
        current = target.stat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise OfflineBackupError("backup destination changed")
    finally:
        os.close(fd)


def publish_encrypted_backup(
    *, snapshot: Path, manifest_sha256: str, target: Path, mount: Path,
    expected_device: int, filename: str, recipient_cert: Path,
    max_bytes: int, timeout: float,
) -> str:
    """Write only ciphertext to an existing, explicitly bound backup directory."""
    if (
        not filename or Path(filename).name != filename or not filename.endswith(".tar.gz.cms")
        or "\\" in filename or filename.startswith(".")
        or type(max_bytes) is not int or max_bytes <= 0
    ):
        raise OfflineBackupError("invalid backup filename or budget")
    snapshot = snapshot.resolve(strict=True)
    if snapshot.is_relative_to(mount) or target.is_relative_to(snapshot):
        raise OfflineBackupError("backup staging and destination must be separate")
    with _mounted_target(mount, target, expected_device):
        pass
    verify_offline_backup(root=snapshot, manifest_sha256=manifest_sha256)
    with TemporaryDirectory(prefix=".backup-seal-", dir=snapshot.parent) as tmp:
        archive, encrypted = Path(tmp) / "snapshot.tar.gz", Path(tmp) / filename
        with tarfile.open(archive, "w:gz", compresslevel=3) as tar:
            for path in sorted(snapshot.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(snapshot).as_posix(), recursive=False)
        _openssl(
            "-encrypt", "-binary", "-stream", "-aes-256-gcm", "-outform", "DER",
            "-in", str(archive), "-out", str(encrypted), "-recip", str(recipient_cert),
            timeout=timeout,
        )
        size = encrypted.stat().st_size
        if size > max_bytes:
            raise OfflineBackupError("encrypted backup exceeds byte budget")
        digest = _digest(encrypted)
        with _mounted_target(mount, target, expected_device) as fd:
            disk = os.fstatvfs(fd)
            if disk.f_bavail * disk.f_frsize < size:
                raise OfflineBackupError("backup target lacks free space")
            pending = f".{filename}.{uuid4().hex}.pending"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            with os.fdopen(os.open(pending, flags, 0o600, dir_fd=fd), "wb") as output:
                try:
                    with encrypted.open("rb") as source:
                        shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                    with os.fdopen(os.open(pending, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd), "rb") as check:
                        if hashlib.file_digest(check, "sha256").hexdigest() != digest:
                            raise OfflineBackupError("encrypted backup target readback mismatch")
                    os.link(pending, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                finally:
                    os.unlink(pending, dir_fd=fd)
                os.fsync(fd)
        return digest


def restore_encrypted_backup(
    *, package: Path, ciphertext_sha256: str, manifest_sha256: str,
    destination: Path, private_key: Path, max_bytes: int, max_files: int, timeout: float,
    max_ciphertext_bytes: int | None = None,
) -> None:
    """Recover and verify a new private directory using a trusted receipt."""
    ciphertext_limit = max_bytes if max_ciphertext_bytes is None else max_ciphertext_bytes
    if (
        type(max_bytes) is not int or max_bytes <= 0
        or type(ciphertext_limit) is not int or not 0 < ciphertext_limit <= max_bytes
        or type(max_files) is not int or max_files <= 0
        or destination != destination.resolve()
        or package.is_symlink() or not package.is_file()
        or package.stat().st_size > ciphertext_limit
        or _digest(package) != ciphertext_sha256
    ):
        raise OfflineBackupError("encrypted backup package identity or budget mismatch")
    destination.mkdir(mode=0o700)
    try:
        with TemporaryDirectory(prefix=".backup-open-", dir=destination.parent) as tmp:
            archive = Path(tmp) / "snapshot.tar.gz"
            _openssl(
                "-decrypt", "-binary", "-inform", "DER", "-in", str(package),
                "-out", str(archive), "-inkey", str(private_key), timeout=timeout,
            )
            total = 0
            with tarfile.open(archive, "r|gz") as tar:
                for count, member in enumerate(tar, 1):
                    relative = Path(member.name)
                    total += member.size
                    if (
                        not member.isreg() or not relative.parts or relative.is_absolute()
                        or relative.as_posix() != member.name or ".." in relative.parts
                        or "\\" in member.name or count > max_files or total > max_bytes
                    ):
                        raise OfflineBackupError("unsafe archive member or restore budget exceeded")
                    source = tar.extractfile(member)
                    if source is None:
                        raise OfflineBackupError("backup archive member is unreadable")
                    target = destination / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                    with source, os.fdopen(os.open(target, flags, 0o600), "wb") as output:
                        shutil.copyfileobj(source, output)
            verify_offline_backup(root=destination, manifest_sha256=manifest_sha256)
    except BaseException:
        shutil.rmtree(destination)
        raise
