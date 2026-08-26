from __future__ import annotations

import json
import os
from pathlib import Path

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_evidence_history import (
    NOT_SCANNED,
    SUPPORTED,
    SUPPORTED_LIMITED_LEGACY_SNAPSHOT,
    UNSUPPORTED_LEGACY_CSV_ONLY,
    UNSUPPORTED_SNAPSHOT_MISSING,
    UNSUPPORTED_SNAPSHOT_SCHEMA,
    load_account_candidate_evidence,
    summarize_run_candidate_evidence,
)
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    publish_candidate_snapshot_manifest,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
)
from src.application.strategy_scan_status import (
    publish_strategy_scan_status_index_v2,
)
from src.application.tick_run_workspace import publish_account_run_config


POLICY_HASH = "b" * 64


def _account_dir(base: Path, *, run_id: str = "run-1", account: str = "lx") -> Path:
    path = base / "output_runs" / run_id / "accounts" / account
    (path / "state").mkdir(parents=True, exist_ok=True)
    return path


def _classify(base: Path, *, run_id: str = "run-1", account: str = "lx"):
    return load_account_candidate_evidence(
        base=base,
        run_id=run_id,
        account=account,
    )


def _publish_empty_modern_bundle(base: Path) -> None:
    account_dir = _account_dir(base)
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256="a" * 64,
        expected=[],
    )
    publish_candidate_snapshot_manifest(
        base=base,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256=POLICY_HASH,
        sealed_at="2026-08-12T01:00:00Z",
    )


def _write_legacy_combo_bundle(base: Path, *, variant: str = "sp_lc") -> str:
    account_dir = _account_dir(base)
    authority = publish_account_run_config(
        base=base,
        run_id="run-1",
        account="lx",
        config={
            "portfolio": {"account": "lx"},
            "symbols": [
                {
                    "symbol": "NVDA",
                    "broker": "US",
                    "yield_enhancement": {
                        "enabled": True,
                        "variant": variant,
                    },
                }
            ],
        },
    )
    status = {
        "schema_version": "strategy_scan_status.v1",
        "run_id": "run-1",
        "account": "lx",
        "market": "US",
        "symbol": "NVDA",
        "strategy_family": "combo_yield",
        "status": "completed",
        "candidate_count": 1,
        "artifacts": [],
        "source_status_path": "nvda_combo_yield_scan_status.json",
    }
    index = {
        "schema_version": "strategy_scan_status_index.v1",
        "run_id": "run-1",
        "account": "lx",
        "published_at_utc": "2026-08-11T01:00:00Z",
        "expected_count": 1,
        "counts": {
            "completed": 1,
            "unavailable": 0,
            "failed": 0,
            "not_applicable": 0,
        },
        "items": [status],
    }
    (account_dir / "strategy_scan_status_index.v1.json").write_text(
        json.dumps(index),
        encoding="utf-8",
    )
    owner = "sp_lc" if variant == "sp_lc" else "cc_lp"
    schema = "combo_yield_candidate_snapshot.v1" if owner == "sp_lc" else "cc_lp_candidate_snapshot.v1"
    filename = COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE if owner == "sp_lc" else "cc_lp_candidate_snapshot.json"
    payload = {
        "schema_version": schema,
        "run_id": "run-1",
        "account": "lx",
        "market": "us",
        "account_config_sha256": authority.account_config_sha256,
        "strategy_policy_sha256": POLICY_HASH,
        "sealed_at_utc": "2026-08-11T01:00:00Z",
        "opening_status": "candidates_found",
        "ranked_pairs": [
            {
                "candidate_pair_id": f"combo_yield:NVDA:P:C",
                "symbol": "NVDA",
                "put_contract_symbol": "P",
                "call_contract_symbol": "C",
            }
        ],
        "reject_reasons": [],
    }
    payload["content_sha256"] = canonical_sha256(payload)
    (account_dir / "state" / filename).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return owner


def test_classifies_valid_manifest_bundle_as_supported(tmp_path: Path) -> None:
    _publish_empty_modern_bundle(tmp_path)

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED
    assert evidence.classification["strict_replay_authority"] is True
    assert evidence.manifest["completion_reason"] == "no_applicable_scope"


def test_present_invalid_manifest_takes_schema_precedence(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "state" / CANDIDATE_SNAPSHOT_MANIFEST_FILE).write_text(
        "{}",
        encoding="utf-8",
    )
    (account_dir / "legacy_sell_put_candidates.csv").write_text(
        "candidate bytes must not be fallback",
        encoding="utf-8",
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == UNSUPPORTED_SNAPSHOT_SCHEMA
    assert evidence.classification["reason_code"] == "candidate_snapshot_manifest_invalid"


def test_modern_snapshot_without_manifest_is_missing_not_legacy(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "state" / COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE).write_text(
        json.dumps({"schema_version": "combo_yield_candidate_snapshot.v2"}),
        encoding="utf-8",
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == UNSUPPORTED_SNAPSHOT_MISSING
    assert evidence.classification["reason_code"] == "candidate_snapshot_manifest_missing"


def test_valid_v1_snapshot_and_immutable_config_are_limited(tmp_path: Path) -> None:
    owner = _write_legacy_combo_bundle(tmp_path)

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT
    assert evidence.classification["strict_replay_authority"] is False
    assert evidence.classification["owner_snapshots"] == [owner]
    assert evidence.owners[owner]["ranked_pairs"][0]["symbol"] == "NVDA"


def test_legacy_variant_selects_exact_configured_owner(tmp_path: Path) -> None:
    owner = _write_legacy_combo_bundle(tmp_path, variant="cc_lp")

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT
    assert evidence.classification["owner_snapshots"] == [owner]


def test_legacy_config_mismatch_is_unsupported_schema(tmp_path: Path) -> None:
    _write_legacy_combo_bundle(tmp_path)
    compatibility = _account_dir(tmp_path) / "config.override.json"
    compatibility.write_text(
        '{"portfolio":{"account":"lx"},"symbols":[]}\n',
        encoding="utf-8",
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == UNSUPPORTED_SNAPSHOT_SCHEMA
    assert evidence.classification["reason_code"] == "legacy_config_authority_invalid"


def test_csv_only_is_metadata_classified_without_opening_bytes(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    candidate = account_dir / "nvda_sell_put_candidates_labeled.csv"
    candidate.write_text("forbidden candidate bytes", encoding="utf-8")
    os.chmod(candidate, 0)
    try:
        evidence = _classify(tmp_path)
    finally:
        os.chmod(candidate, 0o600)

    assert evidence.classification["status"] == UNSUPPORTED_LEGACY_CSV_ONLY
    assert evidence.classification["legacy_candidate_files"] == [candidate.name]
    assert evidence.owners == {}


def test_trace_only_is_snapshot_missing(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "candidate_filter_trace.jsonl").write_text("{}\n", encoding="utf-8")

    assert _classify(tmp_path).classification["status"] == UNSUPPORTED_SNAPSHOT_MISSING


def test_empty_account_directory_is_not_scanned(tmp_path: Path) -> None:
    _account_dir(tmp_path)

    assert _classify(tmp_path).classification["status"] == NOT_SCANNED


def test_run_summary_requires_every_account_to_be_modern_supported(tmp_path: Path) -> None:
    _publish_empty_modern_bundle(tmp_path)
    other = _account_dir(tmp_path, account="sy")
    (other / "candidate_filter_trace.jsonl").write_text("{}\n", encoding="utf-8")

    summary = summarize_run_candidate_evidence(base=tmp_path, run_id="run-1")

    assert summary["strict_replay_authority"] is False
    assert [row["status"] for row in summary["accounts"]] == [
        SUPPORTED,
        UNSUPPORTED_SNAPSHOT_MISSING,
    ]
