import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from src.application.encrypted_backup_package import publish_encrypted_backup, restore_encrypted_backup
from src.application.offline_backup_snapshot import OfflineBackupError, stage_offline_backup


def test_encrypted_publish_and_independent_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL CMS is unavailable")
    data = tmp_path / "stopped"
    snapshot = tmp_path / "snapshot"
    mount = tmp_path / "volume"
    data.mkdir()
    mount.mkdir()
    (data / "config.yaml").write_bytes(b"secret: isolated-test-value\n")
    manifest = stage_offline_backup(
        source=data, destination=snapshot, files={"config.yaml": "file"}, max_bytes=2000,
    )
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=isolated-test", "-out", str(cert), "-keyout", str(key),
        ],
        capture_output=True, check=True,
    )
    filename = "2026-11-18T021500Z_test_full.tar.gz.cms"
    args = {
        "snapshot": snapshot, "manifest_sha256": manifest, "target": mount, "mount": mount,
        "expected_device": mount.stat().st_dev, "filename": filename, "recipient_cert": cert,
        "max_bytes": 50_000, "timeout": 15,
    }
    with pytest.raises(OfflineBackupError, match="configured mount"):
        publish_encrypted_backup(**args)
    assert list(mount.iterdir()) == []
    monkeypatch.setattr(Path, "is_mount", lambda path: path == mount)
    with pytest.raises(OfflineBackupError, match="device mismatch"):
        publish_encrypted_backup(**{**args, "expected_device": 0})
    ciphertext = publish_encrypted_backup(**args)
    with pytest.raises(FileExistsError):
        publish_encrypted_backup(**args)
    package = mount / filename
    assert list(mount.iterdir()) == [package]
    assert b"isolated-test-value" not in package.read_bytes()
    restored = tmp_path / "restored"
    restore_args = {
        "package": package, "ciphertext_sha256": ciphertext, "manifest_sha256": manifest,
        "private_key": key, "max_bytes": 50_000, "max_files": 2, "timeout": 15,
    }
    with pytest.raises(OfflineBackupError, match="restore budget"):
        restore_encrypted_backup(**{**restore_args, "max_files": 1}, destination=restored)
    assert not restored.exists()
    restore_encrypted_backup(**restore_args, destination=restored)
    assert (restored / "config.yaml").read_bytes() == b"secret: isolated-test-value\n"
    raw = package.read_bytes()
    package.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(OfflineBackupError, match="identity"):
        restore_encrypted_backup(**restore_args, destination=tmp_path / "bad")
    tampered = bytearray(raw)
    tampered[len(raw) // 2] ^= 1
    package.write_bytes(tampered)
    with pytest.raises(OfflineBackupError, match="decryption"):
        restore_encrypted_backup(
            **{**restore_args, "ciphertext_sha256": hashlib.sha256(tampered).hexdigest()},
            destination=tmp_path / "bad",
        )
    assert not (tmp_path / "bad").exists()
