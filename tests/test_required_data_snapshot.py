from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
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
    _validate_complete_required_data_bundle,
    _validate_physical_binding,
    load_required_data_snapshot_manifest_snapshot,
    resolve_frozen_required_data,
    resolve_frozen_required_data_csv_bytes_batch,
    seal_required_data_snapshot,
)
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
    required_data_plan_id,
)
from src.application.source_receipts import (
    SourceReceiptError,
    publish_source_receipt,
    validate_source_receipt,
)


_OBSERVED_AT = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _workspace(tmp_path: Path, run_id: str = "run-1") -> tuple[Path, Path]:
    run_dir = tmp_path / "output_runs" / run_id
    root = run_dir / "required_data"
    (root / "raw").mkdir(parents=True)
    (root / "parsed").mkdir(parents=True)
    state = run_dir / "state"
    state.mkdir()
    return root, state / "required_data_snapshot_manifest.json"


def _underlier_code(symbol: str) -> str:
    symbol_norm = str(symbol).strip().upper()
    if symbol_norm.endswith(".HK"):
        return f"HK.{int(symbol_norm[:-3]):05d}"
    return f"US.{symbol_norm}"


def _fetch_plan(
    symbol: str,
    *,
    outcome: str = "success_rows",
    observed_at: str = _OBSERVED_AT,
) -> dict:
    success_rows = outcome == "success_rows"
    expirations = ["2026-08-28"] if success_rows else []
    side_plan = {
        "option_type": "put",
        "min_dte": 20,
        "max_dte": 40,
        "explicit_expirations": expirations,
        "strike_window": {
            "min_strike": 100,
            "max_strike": 100,
            "source": "position_exact",
            "buffer_applied": False,
            "buffer_pct": 0.0,
            "base_min_strike": 100,
            "base_max_strike": 100,
        },
        "planning_reason": "fixture exact position contract",
        "source_fields": ["position.strike"],
        "spot_reference": None,
        "min_strike": 100,
        "max_strike": 100,
        "expiration_count": len(expirations),
        "required_exact_strikes_by_expiration": (
            {expiration: [100.0] for expiration in expirations}
        ),
    }
    request = {
        "symbol": symbol,
        "limit_expirations": 0,
        "host": "127.0.0.1",
        "port": 11111,
        "option_types": ["put"],
        "explicit_expirations": expirations,
        "min_dte": 20,
        "max_dte": 40,
        "side_strike_windows": {
            "put": {
                "min_strike": 100,
                "max_strike": 100,
            }
        },
        "include_realized_volatility": True,
        "side_plans": [side_plan],
        "planning_reason": "fixture exact position request",
        "trading_date": "2026-08-04",
    }
    return {
        "symbol": symbol,
        "spot_reference": None,
        "side_plans": [side_plan],
        "merged_requests": ([request] if success_rows else []),
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": outcome,
            "reason_code": (None if success_rows else "no_expirations"),
            "expirations": expirations,
            "observed_at_utc": observed_at,
            "completed_at_utc": observed_at,
            "request_identity": {
                "symbol": symbol,
                "underlier": _underlier_code(symbol),
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": "2026-08-04",
            },
            "error": None,
        },
        "projection_outcome": outcome,
        "projected_expirations": expirations,
        "require_realized_volatility": True,
    }


def _expected_contract(
    symbol: str,
    *,
    outcome: str = "success_rows",
    fetch_plan: dict | None = None,
) -> dict:
    return build_required_data_expected_fetch_contract(
        symbol=symbol,
        fetch_plan=fetch_plan or _fetch_plan(symbol, outcome=outcome),
        source="opend",
        host="127.0.0.1",
        port=11111,
    )


@pytest.mark.parametrize(
    ("observed_port", "expected_port"),
    [
        (True, 11111),
        (11111.0, 11111),
        (11111, True),
        (11111, 11111.0),
    ],
)
def test_snapshot_physical_binding_rejects_non_integer_ports(
    observed_port: object,
    expected_port: object,
) -> None:
    with pytest.raises(
        SourceReceiptError,
        match="required-data manifest physical binding is invalid",
    ):
        _validate_physical_binding(
            observed={
                "source": "opend",
                "host": "127.0.0.1",
                "port": observed_port,
            },
            expected={
                "source": "opend",
                "host": "127.0.0.1",
                "port": expected_port,
            },
            subject="manifest",
        )


def _publish_quote(
    root: Path,
    *,
    run_id: str,
    symbol: str = "3690.HK",
    canonical_blob: bool = False,
) -> None:
    fetch_plan = _fetch_plan(symbol)
    contract = _expected_contract(symbol, fetch_plan=fetch_plan)
    observed_at = _OBSERVED_AT
    term_hash = hashlib.sha256(
        f"fixture:{symbol}:2026-08-28".encode()
    ).hexdigest()
    term = {
        "schema_version": "term_matched_rv.v1",
        "expiration": "2026-08-28",
        "status": "ok",
        "reason": None,
        "term_matched_rv": 0.2,
        "remaining_sessions": 24,
        "lookback_sessions": 24,
        "input_start": "2026-07-01",
        "input_end": "2026-08-03",
        "input_close_session_count": 25,
        "input_return_count": 24,
        "input_hash": term_hash,
        "missing_sessions": [],
        "legacy_weighted_rv": 0.2,
        "shadow_difference": 0.0,
    }
    payload = {
        "symbol": symbol,
        "underlier_code": _underlier_code(symbol),
        "expiration_count": 1,
        "expirations": ["2026-08-28"],
        "meta": {
            "status": "ok",
            "source_outcome": "success_rows",
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "trading_date": "2026-08-04",
            "snapshot_complete": True,
            "snapshot_requested_code_set": [f"{symbol}-P"],
            "snapshot_returned_code_set": [f"{symbol}-P"],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "realized_volatility": {
                "status": "ok",
                "realized_volatility_20": 0.2,
                "realized_volatility_60": 0.2,
                "realized_volatility_120": 0.2,
                "realized_volatility_estimate": 0.2,
                "reason": None,
                "sample_count": 120,
                "estimation_policy": "term_matched_sessions_v1",
                "term_matched": {"2026-08-28": term},
                "qfq_history": {
                    "status": "ok",
                    "market": ("HK" if symbol.endswith(".HK") else "US"),
                    "underlier_code": _underlier_code(symbol),
                    "autype": "QFQ",
                    "cache_identity": (
                        f"{'HK' if symbol.endswith('.HK') else 'US'}:"
                        f"{_underlier_code(symbol)}:QFQ"
                    ),
                    "completed_before": "2026-08-04",
                    "session_count": 120,
                    "input_hash": hashlib.sha256(
                        f"fixture:qfq:{symbol}".encode()
                    ).hexdigest(),
                },
                "trading_calendar": {
                    "status": "ok",
                    "market": ("HK" if symbol.endswith(".HK") else "US"),
                    "start": "2026-07-01",
                    "end": "2026-08-28",
                    "session_count": 42,
                },
            },
            "source_observed_at": observed_at,
            "completed_at_utc": observed_at,
        },
        "rows": [
            {
                "symbol": symbol,
                "option_type": "put",
                "expiration": "2026-08-28",
                "dte": 24,
                "contract_symbol": f"{symbol}-P",
                "strike": 100,
                "spot": 110,
                "bid": 2.0,
                "ask": 2.2,
                "implied_volatility": 0.3,
                "realized_volatility_20": 0.2,
                "realized_volatility_60": 0.2,
                "realized_volatility_120": 0.2,
                "realized_volatility_estimate": 0.2,
                "term_matched_rv": 0.2,
                "term_matched_rv_status": "ok",
                "term_matched_rv_reason": None,
                "term_matched_rv_remaining_sessions": 24,
                "term_matched_rv_lookback_sessions": 24,
                "term_matched_rv_input_start": "2026-07-01",
                "term_matched_rv_input_end": "2026-08-03",
                "term_matched_rv_input_session_count": 25,
                "term_matched_rv_input_hash": term_hash,
                "term_matched_rv_legacy_shadow": 0.2,
                "term_matched_rv_shadow_difference": 0.0,
                "multiplier": 100,
            }
        ],
    }
    raw_path, csv_path = save_outputs(root.parent.parent.parent, symbol, payload, output_root=root)
    publish_required_data_quote_snapshot(
        runtime_root=(root.parents[2] if canonical_blob else None),
        producer_root=root,
        producer_run_id=run_id,
        symbol=symbol,
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=fetch_plan,
        fetch_policy={
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
        },
        expected_fetch_contract=contract,
        source_observed_at=observed_at,
        completed_at=observed_at,
    )


def _publish_empty_quote(
    root: Path,
    *,
    run_id: str,
    symbol: str = "3690.HK",
) -> str:
    source_observed_at = _OBSERVED_AT
    fetch_plan = _fetch_plan(
        symbol,
        outcome="success_empty",
        observed_at=source_observed_at,
    )
    contract = _expected_contract(
        symbol,
        outcome="success_empty",
        fetch_plan=fetch_plan,
    )
    payload = {
        "symbol": symbol,
        "underlier_code": _underlier_code(symbol),
        "expiration_count": 0,
        "expirations": [],
        "meta": {
            "status": "ok",
            "source_outcome": "success_empty",
            "reason_code": "no_expirations",
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "trading_date": "2026-08-04",
            "snapshot_complete": True,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "realized_volatility": {
                "status": "not_applicable_no_contracts"
            },
            "source_observed_at": source_observed_at,
            "completed_at_utc": source_observed_at,
        },
        "rows": [],
    }
    raw_path, csv_path = save_outputs(
        root.parent.parent.parent,
        symbol,
        payload,
        output_root=root,
    )
    publish_required_data_quote_snapshot(
        producer_root=root,
        producer_run_id=run_id,
        symbol=symbol,
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=fetch_plan,
        fetch_policy={
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
        },
        expected_fetch_contract=contract,
        source_observed_at=source_observed_at,
        completed_at=source_observed_at,
    )
    return source_observed_at


def _summary(
    *symbols: str,
    outcomes: dict[str, str] | None = None,
    fetch_plans: dict[str, dict] | None = None,
) -> dict:
    plan_items = []
    for symbol in symbols:
        outcome = (outcomes or {}).get(symbol, "success_rows")
        fetch_plan = (fetch_plans or {}).get(symbol) or _fetch_plan(
            symbol,
            outcome=outcome,
        )
        contract = _expected_contract(
            symbol,
            outcome=outcome,
            fetch_plan=fetch_plan,
        )
        plan_items.append(
            {
                "symbol": symbol,
                "source": "opend",
                "fetch_binding": dict(contract["fetch_binding"]),
                "fetch_plan": fetch_plan,
                "expected_fetch_contract": contract,
                "projection_outcome": outcome,
                "discovery_status": "complete",
            }
        )
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


def test_canonical_root_resolves_without_legacy_and_corruption_fails_closed(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1", canonical_blob=True)
    old_receipt_path = next(root.glob("source_receipts/quotes/*/*/*/receipt.json"))
    old_receipt = json.loads(old_receipt_path.read_text(encoding="utf-8"))
    validated = validate_source_receipt(
        old_receipt,
        producer_root=root,
        now=datetime.now(timezone.utc),
        expected_source_kind="quotes",
    )
    bundle = json.loads(validated["payload_bytes"])
    bundle.pop("raw_json_base64")
    bundle.pop("required_data_csv_base64")
    old_receipt_path.unlink()
    payload_bytes = (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    publish_source_receipt(
        producer_root=root,
        receipt_relpath="source_receipts/quotes/future/run/symbol/receipt.json",
        payload_relpath="source_receipts/quotes/future/run/symbol/payload.json",
        payload_bytes=payload_bytes,
        source_kind="quotes",
        producer_schema_version=str(old_receipt["producer_schema_version"]),
        producer_run_id="run-1",
        broker="futu",
        included_markets=["HK"],
        source_native_id=str(old_receipt["source_native_id"]),
        source_observed_at=str(old_receipt["source_observed_at"]),
        completed_at=str(old_receipt["completed_at"]),
        producer_policy_hash=str(old_receipt["producer_policy_hash"]),
    )
    (root / bundle["raw_json_relpath"]).unlink()
    (root / bundle["required_data_csv_relpath"]).unlink()

    manifest = seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
    )
    entry = manifest["symbols"]["3690.HK"]
    evidence = resolve_frozen_required_data(
        manifest_path=manifest_path,
        expected_run_id="run-1",
        symbol="3690.HK",
        required_data_root=root,
    )

    assert entry["scan_blob_ref"] == bundle["scan_blob_ref"]
    assert evidence["scan_blob_ref"] == entry["scan_blob_ref"]
    assert evidence["read_source"] == "canonical_blob"

    dataset = tmp_path / "shadow-dataset"
    dataset.mkdir()
    (dataset / "candidate_snapshots.jsonl").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "account": "lx",
                "status": "accepted",
                "symbol": "3690.HK",
                "option_type": "put",
                "contract_symbol": "3690.HK-P",
                "expiration": "2026-08-28",
                "strike": 100,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    from src.application.shadow_replay import mark_shadow_replay_dataset

    marking = mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=root,
        as_of=str(manifest["sealed_at_utc"]),
        repo_root=tmp_path,
        write=False,
    )
    assert marking["summary"]["matched_quote_count"] == 1
    assert marking["summary"]["required_data_read_source_counts"] == {
        "canonical_blob": 1,
        "legacy_snapshot": 0,
    }
    assert marking["summary"]["required_data_legacy_read_count"] == 0

    blob_path = tmp_path / entry["scan_blob_ref"]["blob_relpath"]
    blob_path.write_bytes(b"corrupt")
    with pytest.raises(
        FrozenRequiredDataUnavailable,
        match="receipt_or_payload_mismatch",
    ):
        resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="3690.HK",
            required_data_root=root,
        )


def test_shadow_mark_rows_are_identical_for_legacy_and_canonical_reads(
    tmp_path: Path,
) -> None:
    from src.application.shadow_replay import mark_shadow_replay_dataset

    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1", canonical_blob=True)
    dataset = tmp_path / "shadow-parity"
    dataset.mkdir()
    (dataset / "candidate_snapshots.jsonl").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "account": "lx",
                "status": "accepted",
                "symbol": "3690.HK",
                "option_type": "put",
                "contract_symbol": "3690.HK-P",
                "expiration": "2026-08-28",
                "strike": 100,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mark_at = datetime.fromisoformat(_OBSERVED_AT.replace("Z", "+00:00"))
    legacy = mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=root,
        as_of=_OBSERVED_AT,
        repo_root=tmp_path,
        write=False,
    )
    seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
        sealed_at=mark_at,
    )
    canonical = mark_shadow_replay_dataset(
        dataset=dataset,
        required_data_root=root,
        as_of=_OBSERVED_AT,
        repo_root=tmp_path,
        write=False,
    )

    assert canonical["generated_mark_snapshots"] == legacy[
        "generated_mark_snapshots"
    ]
    assert legacy["summary"]["required_data_legacy_read_count"] == 1
    assert canonical["summary"]["required_data_read_source_counts"] == {
        "canonical_blob": 1,
        "legacy_snapshot": 0,
    }


def test_manifest_snapshot_returns_the_exact_validated_generation(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1")
    sealed = seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK"),
    )

    payload, resolved_root, manifest_bytes = (
        load_required_data_snapshot_manifest_snapshot(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            expected_required_data_root=root,
        )
    )
    manifest_path.write_text("{}\n", encoding="utf-8")

    assert payload == sealed
    assert resolved_root == root.resolve()
    assert json.loads(manifest_bytes) == sealed


def test_sealed_snapshot_accepts_positive_success_empty_evidence(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    observed_at = _publish_empty_quote(root, run_id="run-1")

    manifest = seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary(
            "3690.HK",
            outcomes={"3690.HK": "success_empty"},
        ),
    )

    assert manifest["status"] == "complete"
    entry = manifest["symbols"]["3690.HK"]
    assert entry["status"] == "ready"
    assert entry["source_outcome"] == "success_empty"
    assert entry["reason_code"] == "no_expirations"
    evidence = resolve_frozen_required_data(
        manifest_path=manifest_path,
        expected_run_id="run-1",
        symbol="3690.HK",
        required_data_root=root,
    )
    assert evidence["source_outcome"] == "success_empty"
    assert evidence["reason_code"] == "no_expirations"
    assert evidence["source_observed_at"] == observed_at


@pytest.mark.parametrize(
    "source_outcome",
    ("provider_error", "parse_error", "not_attempted"),
)
def test_frozen_bundle_rejects_rows_with_non_success_source_outcome(
    source_outcome: str,
) -> None:
    raw_payload = {
        "meta": {
            "status": "ok",
            "source_outcome": source_outcome,
            "reason_code": "UPSTREAM_FAILURE",
        },
        "rows": [{"symbol": "3690.HK"}],
    }
    bundle = {
        "raw_json_base64": base64.b64encode(
            json.dumps(raw_payload).encode("utf-8")
        ).decode("ascii"),
    }

    with pytest.raises(
        SourceReceiptError,
        match="non-success required-data bundle contains rows",
    ):
        _validate_complete_required_data_bundle(bundle)


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
    batch = resolve_frozen_required_data_csv_bytes_batch(
        manifest_path=manifest_path,
        expected_run_id="run-1",
        required_data_root=root,
    )
    evidence, csv_bytes = batch.resolve("3690.HK")
    assert evidence["snapshot_id"] == manifest["symbols"]["3690.HK"][
        "snapshot_id"
    ]
    assert b"3690.HK" in csv_bytes
    with pytest.raises(FrozenRequiredDataUnavailable) as failed:
        batch.resolve("9898.HK")
    assert failed.value.reason == "empty_chain"
    with pytest.raises(FrozenRequiredDataUnavailable) as compatible:
        resolve_frozen_required_data(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            symbol="9898.HK",
            required_data_root=root,
            now=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
    assert compatible.value.reason == "empty_chain"


def test_batch_validates_each_ready_symbol_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import required_data_snapshot as snapshot

    root, manifest_path = _workspace(tmp_path)
    for symbol in ("3690.HK", "9898.HK"):
        _publish_quote(root, run_id="run-1", symbol=symbol)
    seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=_summary("3690.HK", "9898.HK"),
    )
    original_validate = snapshot._validate_ready_entry
    validated_symbols: list[str] = []

    def _count_validate(**kwargs):
        validated_symbols.append(str(kwargs["symbol"]))
        return original_validate(**kwargs)

    monkeypatch.setattr(snapshot, "_validate_ready_entry", _count_validate)
    batch = snapshot.resolve_frozen_required_data_csv_bytes_batch(
        manifest_path=manifest_path,
        expected_run_id="run-1",
        required_data_root=root,
    )

    assert validated_symbols == ["3690.HK", "9898.HK"]
    assert b"3690.HK" in batch.resolve("3690.HK")[1]
    assert b"9898.HK" in batch.resolve("9898.HK")[1]
    assert validated_symbols == ["3690.HK", "9898.HK"]


def test_contract_mismatch_fails_one_symbol_without_losing_ready_peer(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    _publish_quote(root, run_id="run-1", symbol="3690.HK")
    _publish_quote(root, run_id="run-1", symbol="9898.HK")
    mismatched_plan = _fetch_plan("9898.HK")
    mismatched_plan["side_plans"][0]["planning_reason"] = (
        "fixture mismatched exact position contract"
    )
    summary = _summary(
        "3690.HK",
        "9898.HK",
        fetch_plans={"9898.HK": mismatched_plan},
    )

    manifest = seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id="run-1",
        prefetch_summary=summary,
    )

    assert manifest["status"] == "partial"
    assert manifest["symbols"]["3690.HK"]["status"] == "ready"
    assert manifest["symbols"]["9898.HK"] == {
        "status": "failed",
        "reason": "quote_receipt_unavailable",
        "error_type": "RequiredDataFetchError",
    }


def test_snapshot_seal_rejects_self_inconsistent_plan_authority(
    tmp_path: Path,
) -> None:
    root, manifest_path = _workspace(tmp_path)
    summary = _summary("3690.HK")
    plan = summary["global_required_data_plan"]
    plan["symbols"][0]["fetch_plan"]["projection_outcome"] = (
        "success_empty"
    )
    plan["plan_id"] = required_data_plan_id(plan["symbols"])

    with pytest.raises(
        RequiredDataSnapshotError,
        match="global plan fetch plan contradicts its contract",
    ):
        seal_required_data_snapshot(
            manifest_path=manifest_path,
            required_data_root=root,
            run_id="run-1",
            prefetch_summary=summary,
        )
    assert not manifest_path.exists()


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


def test_frozen_snapshot_rejects_forged_manifest_contract(
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
    forged_plan = _fetch_plan("3690.HK")
    forged_plan["side_plans"][0]["planning_reason"] = (
        "fixture forged exact position contract"
    )
    forged_contract = _expected_contract(
        "3690.HK",
        fetch_plan=forged_plan,
    )
    entry = payload["symbols"]["3690.HK"]
    entry["fetch_plan"] = forged_plan
    entry["expected_fetch_contract"] = forged_contract
    entry["expected_fetch_contract_sha256"] = forged_contract[
        "contract_sha256"
    ]
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
