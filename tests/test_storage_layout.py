import json
from pathlib import Path

import pytest

from src.application.storage_layout import parse_backup_filename
from src.interfaces.cli.main import main


def test_backup_layout_paths_are_rotation_compatible_and_read_only(tmp_path, capsys):
    volume = tmp_path / "External Disk"
    assert main([
        "service", "backup-layout", "--series", "liuxie-incus",
        "--timestamp", "2026-12-31T235959Z", "--restored-at", "2027-01-01T000100Z",
        "--retired-at", "2027-01-02T000000Z", "--plan-sha256", "a" * 64,
    ]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    filename = "2026-12-31T235959Z_liuxie-incus_full.tar.gz.cms"
    base = Path("OM/backups/prod/liuxie-incus")
    assert data["package"] == str(base / "packages" / filename)
    assert parse_backup_filename(filename) == ("2026-12-31T235959Z", "liuxie-incus", "full")
    assert data["rotation_inventory"] == str(base / "rotation.json")
    assert data["rotation_entry"]["verification"] == (
        "receipts/2026-12-31T235959Z_liuxie-incus_full.restored.2027-01-01T000100Z.json"
    )
    assert data["retirement_receipt"] == str(base / "retirements/2027-01-02T000000Z_aaaaaaaaaaaa.retire.jsonl")
    assert not volume.exists()


@pytest.mark.parametrize("kind,when,extra,expected", [
    ("research", "2026-11-17", ["--market", "us", "--dataset", "watchlist"],
     "research/us/2026/2026-11-17_us_watchlist_batch-001"),
    ("incidents", "2026-11-18T021500Z", [],
     "incidents/2026/2026-11-18T021500Z_batch-001"),
    ("maintenance", "2026-11-18T021500Z", [],
     "maintenance/2026/2026-11-18T021500Z_batch-001"),
])
def test_archive_layout_cli_plans_expected_directory(tmp_path, capsys, kind, when, extra, expected):
    assert main([
        "research", "archive", "layout",
        "--kind", kind, "--when", when, "--batch-id", "batch-001", *extra,
    ]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert Path(data["archive_root"]) == Path("OM/archives") / expected
    assert data["legacy_inbox"] == "OM/inbox/legacy"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("filename", [
    "2026-02-30T000000Z_host_full.tar.gz.cms",
    "2026-11-18T240000Z_host_full.tar.gz.cms",
    "2026-11-18T021500Z_../host_full.tar.gz.cms",
    "2026-11-18T021500Z_host_monthly.tar.gz.cms",
])
def test_bad_package_names_rejected(filename):
    with pytest.raises(ValueError):
        parse_backup_filename(filename)
