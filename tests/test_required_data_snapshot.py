from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.opend_symbol_outputs import (
    publish_required_data_quote_snapshot,
    save_outputs,
)
from src.application.required_data_snapshot import (
    FrozenRequiredDataUnavailable,
    RequiredDataSnapshotError,
    resolve_frozen_required_data,
    seal_required_data_snapshot,
)
from src.application.required_data_plan_identity import required_data_plan_id


def _workspace(tmp_path: Path, run_id: str = "run-1") -> tuple[Path, Path]:
    run_dir = tmp_path / "output_runs" / run_id
    root = run_dir / "required_data"
    (root / "raw").mkdir(parents=True)
    (root / "parsed").mkdir(parents=True)
    state = run_dir / "state"
    state.mkdir()
    return root, state / "required_data_snapshot_manifest.json"


def _publish_quote(root: Path, *, run_id: str, symbol: str = "3690.HK") -> None:
    payload = {
        "meta": {"status": "ok"},
        "rows": [
            {
                "symbol": symbol,
                "option_type": "put",
                "expiration": "2026-08-28",
                "dte": 31,
                "contract_symbol": f"{symbol}-P",
                "strike": 100,
                "spot": 110,
                "bid": 2.0,
                "ask": 2.2,
                "implied_volatility": 0.3,
                "realized_volatility_20": 0.2,
                "multiplier": 100,
            }
        ],
    }
    raw_path, csv_path = save_outputs(root.parent.parent.parent, symbol, payload, output_root=root)
    publish_required_data_quote_snapshot(
        producer_root=root,
        producer_run_id=run_id,
        symbol=symbol,
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan={"symbol": symbol, "min_dte": 20, "max_dte": 40},
        fetch_policy={"source": "futu"},
        source_observed_at=datetime.now(timezone.utc),
    )


def _summary(*symbols: str) -> dict:
    plan_items = [
        {
            "symbol": symbol,
            "source": "futu",
            "fetch_plan": {"symbol": symbol},
            "discovery_status": "complete",
        }
        for symbol in symbols
    ]
    plan_id = required_data_plan_id(plan_items)
    return {
        "schema_version": "1.0",
        "errors": 0,
        "global_required_data_plan": {
            "plan_id": plan_id,
            "symbols": plan_items,
            "symbols_count": len(plan_items),
            "discovery_complete": True,
        },
        "symbols": [],
        "results": [],
    }


def test_sealed_snapshot_resolves_exact_current_run_bytes(tmp_path: Path) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1")

    manifest = seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
    )

    assert manifest["status"] == "complete"
    assert manifest["summary"] == {"symbols_total": 1, "ready": 1, "failed": 0}
    entry = manifest["symbols"]["3690.HK"]
    tracked_paths = [
        root / entry["raw_json_relpath"],
        root / entry["required_data_csv_relpath"],
        root / entry["receipt_relpath"],
    ]
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tracked_paths
    }
    evidence = None
    for _account in ("lx", "sy"):
        evidence = resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="3690.HK",
            required_data_root=root,
        )
    assert evidence is not None
    assert evidence["snapshot_id"] == manifest["symbols"]["3690.HK"]["snapshot_id"]
    assert evidence["plan_id"] == _summary("3690.HK")[
        "global_required_data_plan"
    ]["plan_id"]
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tracked_paths
    } == before


def test_sealed_snapshot_rejects_tampered_csv_and_other_run_receipt(tmp_path: Path) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1")
    seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
    )
    csv_path = root / "parsed" / "3690.HK_required_data.csv"
    csv_path.write_bytes(csv_path.read_bytes() + b"\n")

    with pytest.raises(FrozenRequiredDataUnavailable) as tampered:
        resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="3690.HK",
            required_data_root=root,
        )
    assert tampered.value.reason == "receipt_or_payload_mismatch"

    other_root, other_manifest = _workspace(tmp_path / "other")
    _publish_quote(other_root, run_id="older-run")
    failed = seal_required_data_snapshot(
        manifest_path=other_manifest,
        required_data_root=other_root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
    )
    assert failed["status"] == "failed"
    assert failed["symbols"]["3690.HK"]["reason"] == "quote_receipt_unavailable"


def test_partial_snapshot_keeps_ready_symbol_and_types_failed_symbol(tmp_path: Path) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1", symbol="3690.HK")
    summary = _summary("3690.HK", "9898.HK")
    summary["errors"] = 1
    summary["symbols"] = [
        {
            "symbol": "9898.HK",
            "status": "error",
            "message": "empty_chain",
            "error_type": "RequiredDataFetchError",
        }
    ]

    manifest = seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=summary,
    )

    assert manifest["status"] == "partial"
    assert manifest["symbols"]["3690.HK"]["status"] == "ready"
    assert manifest["symbols"]["9898.HK"]["status"] == "failed"
    with pytest.raises(FrozenRequiredDataUnavailable) as failed:
        resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="9898.HK",
            required_data_root=root,
        )
    assert failed.value.reason == "empty_chain"


def test_frozen_snapshot_rejects_manifest_content_tampering(tmp_path: Path) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1")
    seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["status"] = "partial"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FrozenRequiredDataUnavailable) as tampered:
        resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="3690.HK",
            required_data_root=root,
        )
    assert tampered.value.reason == "manifest_invalid"


def test_snapshot_seal_rejects_mismatched_plan_id(tmp_path: Path) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1")
    summary = _summary("3690.HK")
    summary["global_required_data_plan"]["plan_id"] = "a" * 64

    with pytest.raises(
        RequiredDataSnapshotError,
        match="global required-data plan id mismatch",
    ):
        seal_required_data_snapshot(
            manifest_path=manifest_path,
            required_data_root=root,
            run_id="run-1",
            prefetch_summary=summary,
        )
    assert not manifest_path.exists()


def test_frozen_snapshot_cross_checks_manifest_plan_against_receipt(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1")
    seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["symbols"]["3690.HK"]["fetch_plan"]["min_dte"] = 999
    payload["content_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "content_sha256"
        }
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FrozenRequiredDataUnavailable) as mismatched:
        resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="3690.HK",
            required_data_root=root,
        )
    assert mismatched.value.reason == "receipt_or_payload_mismatch"


def test_frozen_snapshot_rejects_expired_receipt_and_missing_manifest(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1")
    seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
    )

    with pytest.raises(FrozenRequiredDataUnavailable) as expired:
        resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="3690.HK",
            required_data_root=root,
            now=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
    assert expired.value.reason == "receipt_or_payload_mismatch"

    manifest_path.unlink()
    with pytest.raises(FrozenRequiredDataUnavailable) as missing:
        resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="3690.HK",
            required_data_root=root,
        )
    assert missing.value.reason == "manifest_invalid"
