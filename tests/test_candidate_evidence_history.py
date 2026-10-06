from __future__ import annotations

import json
import os
from hashlib import sha256
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
    CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
    publish_candidate_snapshot_manifest,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
)
from src.application.strategy_scan_status import (
    publish_strategy_scan_status_index,
)
from src.application.tick_run_workspace import publish_account_run_config


POLICY_HASH = "b" * 64
CONFIG_HASH = "a" * 64


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


def _write_state(base: Path, filename: str, content: str) -> Path:
    """Write ``content`` into the account's ``state`` directory."""
    path = _account_dir(base) / "state" / filename
    path.write_text(content, encoding="utf-8")
    return path


def _publish_empty_modern_bundle(base: Path) -> None:
    account_dir = _account_dir(base)
    publish_strategy_scan_status_index(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256="a" * 64,
        expected=[],
        run_mode={"scan_mode": "standard", "executable": True},
    )
    publish_candidate_snapshot_manifest(
        base=base,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256=POLICY_HASH,
        sealed_at="2026-08-12T01:00:00Z",
    )


def _encoded_json(payload: dict) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _plain_json_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _write_empty_formal_history_bundle(
    base: Path,
    *,
    manifest_version: int,
    include_opening: bool = False,
) -> None:
    account_dir = _account_dir(base)
    manifest_name, manifest_schema, index_name, index_schema = {
        1: (
            CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
            "candidate_snapshot_manifest.v1",
            "strategy_scan_status_index.v2.json",
            "strategy_scan_status_index.v2",
        ),
        3: (
            CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
            "candidate_snapshot_manifest.v3",
            "strategy_scan_status_index.v4.json",
            "strategy_scan_status_index.v4",
        ),
    }[manifest_version]
    expected_scope = {
        "market": "US",
        "symbol": "NVDA",
        "strategy_family": "sell_put",
        "strategy_mode": "put",
        "candidate_owner": "opening",
    }
    source_status = {
        "schema_version": "strategy_scan_status.v1",
        "run_id": "run-1",
        "account": "lx",
        "market": "US",
        "symbol": "NVDA",
        "strategy_family": "sell_put",
        "status": "completed",
        "published_at_utc": "2026-08-12T00:59:59Z",
        "candidate_count": 0,
    }
    source_name = "nvda_sell_put_scan_status.json"
    source_bytes = _encoded_json(source_status)
    index_item = {
        **source_status,
        "strategy_mode": "put",
        "candidate_owner": "opening",
        "account_config_sha256": CONFIG_HASH,
        "source_status_schema": "strategy_scan_status.v1",
        "source_status_path": source_name,
        **(
            {"source_status_sha256": sha256(source_bytes).hexdigest()}
            if manifest_version == 3
            else {}
        ),
    }
    items = [index_item] if include_opening else []
    index = {
        "schema_version": index_schema,
        "run_id": "run-1",
        "account": "lx",
        "account_config_sha256": CONFIG_HASH,
        "published_at_utc": "2026-08-12T01:00:00Z",
        "expected_count": len(items),
        "counts": {
            "completed": len(items),
            "unavailable": 0,
            "failed": 0,
            "not_applicable": 0,
        },
        "items": items,
    }
    index["content_sha256"] = _plain_json_sha256(index)
    index_bytes = _encoded_json(index)
    (account_dir / index_name).write_bytes(index_bytes)
    if include_opening:
        (account_dir / source_name).write_bytes(source_bytes)
    owner_entries = []
    if include_opening:
        owner = {
            "schema_version": "opening_candidate_snapshot.v1",
            "run_id": "run-1",
            "account": "lx",
            "market": "US",
            "account_config_sha256": CONFIG_HASH,
            "strategy_policy_sha256": POLICY_HASH,
            "sealed_at_utc": "2026-08-12T01:00:00Z",
            "opening_status": "no_candidate",
            "dependencies": [],
            "scope_results": [
                {
                    "scope": "strategy",
                    "symbol": "NVDA",
                    "strategy_mode": "put",
                    "status": "completed",
                    "reason_code": None,
                    "quote_snapshot_id": None,
                    "quote_receipt_relpath": None,
                }
            ],
            "ranked_candidates": [],
        }
        owner["content_sha256"] = canonical_sha256(owner)
        owner_bytes = _encoded_json(owner)
        (account_dir / "state" / "opening_candidate_snapshot.json").write_bytes(
            owner_bytes
        )
        owner_entries = [
            {
                "candidate_owner": "opening",
                "schema_version": "opening_candidate_snapshot.v1",
                "relpath": "state/opening_candidate_snapshot.json",
                "sha256": sha256(owner_bytes).hexdigest(),
                "content_sha256": owner["content_sha256"],
                "opening_status": "no_candidate",
                "covered_scopes": [expected_scope],
            }
        ]
    manifest = {
        "schema_version": manifest_schema,
        "run_id": "run-1",
        "account": "lx",
        "account_config_sha256": CONFIG_HASH,
        "strategy_policy_sha256": POLICY_HASH,
        "sealed_at_utc": "2026-08-12T01:00:01Z",
        "markets": ["US"] if include_opening else [],
        "expected_scopes": [expected_scope] if include_opening else [],
        "expected_owners": ["opening"] if include_opening else [],
        "completion_reason": "complete" if include_opening else "no_applicable_scope",
        "status_index": {
            "schema_version": index_schema,
            "relpath": index_name,
            "sha256": sha256(index_bytes).hexdigest(),
            "content_sha256": index["content_sha256"],
        },
        "owner_snapshots": owner_entries,
    }
    manifest["content_sha256"] = canonical_sha256(manifest)
    (account_dir / "state" / manifest_name).write_bytes(_encoded_json(manifest))


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
    assert evidence.manifest["completion_reason"] == "no_applicable_scope"


def test_formal_v1_history_is_read_only_and_classified_as_limited(tmp_path: Path) -> None:
    _write_empty_formal_history_bundle(tmp_path, manifest_version=1)

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT
    assert evidence.classification["reason_code"] == "historical_candidate_snapshot_valid"
    assert evidence.manifest["schema_version"] == "candidate_snapshot_manifest.v1"


def test_nonempty_formal_v1_history_preserves_its_v1_source_contract(
    tmp_path: Path,
) -> None:
    _write_empty_formal_history_bundle(
        tmp_path,
        manifest_version=1,
        include_opening=True,
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT
    assert evidence.status_index["items"][0]["source_status_schema"] == (
        "strategy_scan_status.v1"
    )
    assert evidence.owners["opening"]["opening_status"] == "no_candidate"


def test_formal_v3_history_is_read_only_and_classified_as_limited(tmp_path: Path) -> None:
    _write_empty_formal_history_bundle(tmp_path, manifest_version=3)

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT
    assert evidence.classification["reason_code"] == "historical_candidate_snapshot_valid"
    assert evidence.manifest["schema_version"] == "candidate_snapshot_manifest.v3"


def test_nonempty_formal_v3_history_preserves_non_wheel_v1_source_status(
    tmp_path: Path,
) -> None:
    _write_empty_formal_history_bundle(
        tmp_path,
        manifest_version=3,
        include_opening=True,
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT
    assert evidence.status_index["items"][0]["source_status_schema"] == (
        "strategy_scan_status.v1"
    )
    assert evidence.owners["opening"]["opening_status"] == "no_candidate"


def test_mixed_formal_history_and_current_manifest_is_rejected(tmp_path: Path) -> None:
    _write_empty_formal_history_bundle(tmp_path, manifest_version=1)
    _write_state(tmp_path, CANDIDATE_SNAPSHOT_MANIFEST_FILE, "{}")

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == UNSUPPORTED_SNAPSHOT_SCHEMA
    assert evidence.classification["reason_code"] == "candidate_snapshot_manifest_invalid"


def test_present_invalid_manifest_takes_schema_precedence(tmp_path: Path) -> None:
    _write_state(tmp_path, CANDIDATE_SNAPSHOT_MANIFEST_FILE, "{}")
    (_account_dir(tmp_path) / "legacy_sell_put_candidates.csv").write_text(
        "candidate bytes must not be fallback",
        encoding="utf-8",
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == UNSUPPORTED_SNAPSHOT_SCHEMA
    assert evidence.classification["reason_code"] == "candidate_snapshot_manifest_invalid"


def test_modern_snapshot_without_manifest_is_missing_not_legacy(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
        json.dumps({"schema_version": "combo_yield_candidate_snapshot.v2"}),
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == UNSUPPORTED_SNAPSHOT_MISSING
    assert evidence.classification["reason_code"] == "candidate_snapshot_manifest_missing"


def test_wheel_v2_snapshot_without_manifest_is_missing(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        "wheel_candidate_snapshot.v2.json",
        json.dumps({"schema_version": "wheel_candidate_snapshot.v2"}),
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == UNSUPPORTED_SNAPSHOT_MISSING
    assert evidence.classification["reason_code"] == "candidate_snapshot_manifest_missing"


def test_present_invalid_manifest_v3_takes_schema_precedence(tmp_path: Path) -> None:
    _write_state(tmp_path, CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE, "{}")

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == UNSUPPORTED_SNAPSHOT_SCHEMA
    assert evidence.classification["reason_code"] == "candidate_snapshot_manifest_invalid"


def test_valid_v1_snapshot_and_immutable_config_are_limited(tmp_path: Path) -> None:
    owner = _write_legacy_combo_bundle(tmp_path)

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT
    assert evidence.classification["owner_snapshots"] == [owner]
    assert evidence.owners[owner]["ranked_pairs"][0]["symbol"] == "NVDA"


def test_legacy_variant_selects_exact_configured_owner(tmp_path: Path) -> None:
    owner = _write_legacy_combo_bundle(tmp_path, variant="cc_lp")

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT
    assert evidence.classification["owner_snapshots"] == [owner]


def test_legacy_account_level_config_is_ignored(tmp_path: Path) -> None:
    _write_legacy_combo_bundle(tmp_path)
    compatibility = _account_dir(tmp_path) / "config.override.json"
    compatibility.write_text(
        '{"portfolio":{"account":"lx"},"symbols":[]}\n',
        encoding="utf-8",
    )

    evidence = _classify(tmp_path)

    assert evidence.classification["status"] == SUPPORTED_LIMITED_LEGACY_SNAPSHOT


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
    _write_state(tmp_path, "candidate_filter_trace.jsonl", "{}\n")

    assert _classify(tmp_path).classification["status"] == UNSUPPORTED_SNAPSHOT_MISSING


def test_empty_account_directory_is_not_scanned(tmp_path: Path) -> None:
    _account_dir(tmp_path)

    assert _classify(tmp_path).classification["status"] == NOT_SCANNED


def test_run_summary_requires_every_account_to_be_modern_supported(tmp_path: Path) -> None:
    _publish_empty_modern_bundle(tmp_path)
    other = _account_dir(tmp_path, account="sy")
    (other / "candidate_filter_trace.jsonl").write_text("{}\n", encoding="utf-8")

    summary = summarize_run_candidate_evidence(base=tmp_path, run_id="run-1")

    assert summary["reason_code"] == "candidate_evidence_coverage_incomplete"
    assert [row["status"] for row in summary["accounts"]] == [
        SUPPORTED,
        UNSUPPORTED_SNAPSHOT_MISSING,
    ]
