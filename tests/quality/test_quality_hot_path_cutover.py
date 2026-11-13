from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from threading import Barrier

import pytest

import scripts.benchmark_current_decision_projection_slice2 as current_decision_benchmark
from scripts.benchmark_current_decision_projection_slice2 import (
    _forbidden_call_count,
)
from src.application.quality.cutover import (
    CUTOVER_EVIDENCE_SCHEMA,
    activate_quality_hot_path_cutover,
    quality_current_consumer_inventory_sha256,
    quality_hot_path_cutover_preview,
    read_quality_hot_path_cutover_receipt,
)


def _evidence(*, days_per_market: int = 14) -> dict:
    start = date(2026, 7, 1)
    days = [
        {
            "market": market,
            "market_date": (start + timedelta(days=index)).isoformat(),
            "scheduled_open": True,
            "comparison_status": "matched",
            "legacy_read_count": 0,
            "unexplained_read_count": 0,
        }
        for market in ("hk", "us")
        for index in range(days_per_market)
    ]
    return {
        "schema_version": CUTOVER_EVIDENCE_SCHEMA,
        "eligible_market_days": days,
        "static_consumer_inventory_sha256": (
            quality_current_consumer_inventory_sha256()
        ),
        "deployment_access": {
            "evidence_sha256": "a" * 64,
            "unexplained_reader_count": 0,
        },
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def test_cutover_requires_fourteen_clean_days_per_market(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    _write(evidence, _evidence(days_per_market=13))
    with pytest.raises(ValueError, match="14 eligible days"):
        quality_hot_path_cutover_preview(evidence)

    payload = _evidence()
    payload["eligible_market_days"][-1]["legacy_read_count"] = 1
    _write(evidence, payload)
    with pytest.raises(ValueError, match="legacy or unexplained"):
        quality_hot_path_cutover_preview(evidence)


def test_cutover_preview_and_immutable_apply(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    receipt = tmp_path / "state" / "cutover.json"
    _write(evidence, _evidence())

    preview = quality_hot_path_cutover_preview(evidence)
    assert preview["status"] == "eligible"
    assert preview["eligible_market_day_counts"] == {"hk": 14, "us": 14}
    assert not receipt.exists()

    activated = activate_quality_hot_path_cutover(
        evidence,
        receipt_path=receipt,
        now=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )
    assert activated["status"] == "active"
    assert read_quality_hot_path_cutover_receipt(receipt) == activated
    assert activate_quality_hot_path_cutover(
        evidence,
        receipt_path=receipt,
        now=datetime(2026, 8, 17, tzinfo=timezone.utc),
    ) == activated

    changed = _evidence()
    changed["deployment_access"]["evidence_sha256"] = "b" * 64
    _write(evidence, changed)
    with pytest.raises(ValueError, match="already exists"):
        activate_quality_hot_path_cutover(evidence, receipt_path=receipt)


def test_cutover_cli_hard_exit_during_write_leaves_final_path_retryable(tmp_path, monkeypatch) -> None:
    from src.interfaces.quality import cli

    evidence = tmp_path / "evidence.json"
    receipt = tmp_path / "state" / "cutover.json"
    _write(evidence, _evidence())
    script = """
import argparse, os, sys
from pathlib import Path
from src.application.quality import cutover
from src.interfaces.quality import cli
cli.OMQualityService = lambda: None
cli.default_quality_hot_path_cutover_receipt_path = lambda: Path(sys.argv[2])
write = os.write
def hard_exit(fd, data):
    write(fd, data[:8])
    os._exit(77)
cutover.os.write = hard_exit
cli.handle_quality_command(argparse.Namespace(
    quality_command="cutover", apply=True, evidence=sys.argv[1]))
"""
    interrupted = subprocess.run([sys.executable, "-c", script, str(evidence), str(receipt)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)
    assert interrupted.returncode == 77, interrupted.stderr
    assert not receipt.exists()
    assert read_quality_hot_path_cutover_receipt(receipt)["status"] == "inactive"
    monkeypatch.setattr(cli, "OMQualityService", lambda: None)
    monkeypatch.setattr(cli, "default_quality_hot_path_cutover_receipt_path", lambda: receipt)
    result = cli.handle_quality_command(argparse.Namespace(
        quality_command="cutover", apply=True, evidence=str(evidence)))
    assert read_quality_hot_path_cutover_receipt(receipt) == result
    assert result["status"] == "active"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_cutover_publishes_only_after_file_sync_and_syncs_parent(tmp_path, monkeypatch) -> None:
    from src.application.quality import cutover

    evidence = tmp_path / "evidence.json"
    receipt = tmp_path / "state" / "cutover.json"
    _write(evidence, _evidence())
    events = []
    sync, link = os.fsync, os.link

    def tracked_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            assert receipt.exists()
            events.append("directory_sync")
        else:
            assert not receipt.exists()
            events.append("file_sync")
        return sync(fd)

    def tracked_link(source, target, **kwargs):
        assert events == ["file_sync"]
        result = link(source, target, **kwargs)
        events.append("publish")
        return result

    monkeypatch.setattr(cutover.os, "fsync", tracked_sync)
    monkeypatch.setattr(cutover.os, "link", tracked_link)
    activate_quality_hot_path_cutover(evidence, receipt_path=receipt)
    assert events == ["file_sync", "publish", "directory_sync"]
    assert not list(receipt.parent.glob(".*.tmp"))


@pytest.mark.parametrize("same_evidence", [False, True])
def test_cutover_concurrent_publish_never_overwrites(tmp_path, monkeypatch, same_evidence) -> None:
    from src.application.quality import cutover

    evidence = tmp_path / "evidence.json"
    other = tmp_path / "other.json"
    receipt = tmp_path / "state" / "cutover.json"
    _write(evidence, _evidence())
    payload = _evidence()
    payload["deployment_access"]["evidence_sha256"] = "b" * 64
    _write(other, payload)
    barrier = Barrier(2)
    link = os.link

    def concurrent_link(source, target, **kwargs):
        barrier.wait(timeout=10)
        return link(source, target, **kwargs)

    def activate(path):
        try:
            return activate_quality_hot_path_cutover(path, receipt_path=receipt)
        except ValueError as exc:
            assert "already exists" in str(exc)
            return None

    monkeypatch.setattr(cutover.os, "link", concurrent_link)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(activate, [evidence, evidence if same_evidence else other]))
    current = read_quality_hot_path_cutover_receipt(receipt)
    if same_evidence:
        assert results == [current, current]
    else:
        assert results.count(None) == 1
        assert current == next(item for item in results if item is not None)
    assert current["status"] == "active"
    assert not list(receipt.parent.glob(".*.tmp"))


@pytest.mark.parametrize("zero_progress", [False, True])
def test_cutover_write_failure_cleans_unpublished_files(tmp_path, monkeypatch, zero_progress) -> None:
    from src.application.quality import cutover

    evidence = tmp_path / "evidence.json"
    receipt = tmp_path / "state" / "cutover.json"
    _write(evidence, _evidence())

    def fail_write(*args):
        if zero_progress:
            return 0
        raise OSError("injected write failure")

    monkeypatch.setattr(cutover.os, "write", fail_write)
    with pytest.raises(OSError, match="write"):
        activate_quality_hot_path_cutover(evidence, receipt_path=receipt)
    assert not receipt.exists()
    assert not list(receipt.parent.iterdir())


def test_cutover_preserves_preexisting_incomplete_receipt(tmp_path) -> None:
    evidence = tmp_path / "evidence.json"
    receipt = tmp_path / "state" / "cutover.json"
    _write(evidence, _evidence())
    receipt.parent.mkdir()
    receipt.write_bytes(b"")
    with pytest.raises(ValueError, match="already exists"):
        activate_quality_hot_path_cutover(evidence, receipt_path=receipt)
    assert receipt.read_bytes() == b""
    assert read_quality_hot_path_cutover_receipt(receipt)["status"] == "inactive"
    assert list(receipt.parent.iterdir()) == [receipt]


def test_cutover_rejects_inventory_drift_and_symlink_receipt(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence.json"
    payload = _evidence()
    payload["static_consumer_inventory_sha256"] = "f" * 64
    _write(evidence, payload)
    with pytest.raises(ValueError, match="inventory"):
        quality_hot_path_cutover_preview(evidence)

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.symlink_to(target)
    with pytest.raises(OSError):
        read_quality_hot_path_cutover_receipt(receipt)


def test_quality_benchmark_counter_is_executable() -> None:
    def build_ledger_datasets() -> None:
        return None

    assert _forbidden_call_count(build_ledger_datasets) == 1


@pytest.mark.parametrize(
    "relative_path",
    (
        "domain/domain/combo_identity.py",
        "domain/domain/lifecycle_allocation.py",
        "domain/domain/symbol_identity.py",
        "domain/domain/ledger/projection.py",
        "src/application/ledger/current_decision_payload.py",
        "src/application/ledger/event_codec.py",
        "src/application/ledger/position_projection_publication.py",
        "src/application/ledger/position_projection_runtime.py",
        "src/application/ledger/projector_implementation.py",
        "src/application/ledger/publisher.py",
        "src/application/ledger/repository_trade_events.py",
        "src/application/ledger/writer_trade_events.py",
        "scripts/benchmark_data_storage_projection.py",
        "scripts/benchmark_support.py",
        "src/infrastructure/quality/artifact_repository.py",
    ),
)
def test_quality_benchmark_source_hash_tracks_split_implementation(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    target = current_decision_benchmark.REPO_ROOT / relative_path
    original_read_bytes = Path.read_bytes
    baseline = current_decision_benchmark._source_sha256()

    def changed_read_bytes(path: Path) -> bytes:
        data = original_read_bytes(path)
        return data + b"\n# benchmark source changed\n" if path == target else data

    monkeypatch.setattr(Path, "read_bytes", changed_read_bytes)

    assert current_decision_benchmark._source_sha256() != baseline
