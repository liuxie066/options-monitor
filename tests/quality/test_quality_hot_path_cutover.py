from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

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
