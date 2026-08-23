#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import statistics
import sys
import time
import tracemalloc
from typing import Any, Callable
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application import runtime_portfolio_snapshot as snapshot_owner
import src.application.ledger.api as ledger_api
from src.application.candidate_snapshot_contract import normalize_combo_scope_results
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA,
    validate_candidate_snapshot_manifest,
)
from src.application.cc_lp_candidate_snapshot import (
    CC_LP_CANDIDATE_SNAPSHOT_FILE,
    CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
    validate_cc_lp_candidate_snapshot,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
    validate_combo_yield_candidate_snapshot,
)
from src.application.ledger.api import (
    CURRENT_ASSIGNED_STOCK_SCHEMA,
    CURRENT_COMBO_GROUP_FACT_SCHEMA,
    CURRENT_COMBO_SCHEMA,
    CURRENT_DECISION_POSITION_FIELDS,
    CURRENT_DECISION_READ_SCHEMA,
    POSITION_PROJECTION_SCHEMA,
    build_current_decision_projection_payload,
    build_initial_lifecycle_case_decision_fact,
    build_lifecycle_quality_fact,
    derive_lifecycle_quality_view,
    empty_assigned_stock_fact,
    lifecycle_views_by_lot,
    validate_current_decision_projection_payload,
)
from src.application.prepared_option_positions_context import (
    PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
    PREPARED_OPTION_POSITIONS_MANIFEST_NAME,
    PREPARED_OPTION_POSITIONS_PAYLOAD_NAME,
)
from src.application.prepared_portfolio_context import PREPARED_PORTFOLIO_CONTEXT_SCHEMA
from src.application.required_data_snapshot import REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA
from src.application.runtime_portfolio_snapshot import (
    MAX_CANONICAL_BYTES,
    build_runtime_portfolio_section,
    build_runtime_portfolio_snapshot,
    build_source_status_section,
    canonical_json_bytes,
    compare_runtime_portfolio_snapshot,
    project_broker_cash_facts,
    project_broker_positions_facts,
    project_cash_occupation_facts,
    project_ledger_projection_facts,
    validate_replay_bundle,
)
from src.application.source_receipts import sha256_bytes
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA,
    STRATEGY_SCAN_STATUS_SCHEMA,
    validate_strategy_scan_status_index_v2,
)
from src.application.tick_run_workspace import canonical_account_run_config_bytes


FIXTURE_SCHEMA = "runtime_portfolio_snapshot_benchmark_fixture.v1"
FIXTURE_DESCRIPTOR_PATH = REPO_ROOT / "tests" / "fixtures" / "runtime_portfolio_snapshot_benchmark_metadata.v1.json"
FIXTURE_CONTRACT_SHA256 = "f180e7bbcdd2f9bdaf6edfc540099b5c54156f3c6971ce83ef55c6fea51099c8"
SEED = 20260816
RUN_ID = "fixture-runtime-0001"
ACCOUNT = "acct_fixture"
WARMUPS = 5
REPETITIONS = 30
WALL_LIMIT_MS = 250.0
ALLOCATION_LIMIT_BYTES = 33_554_432
PROFILES = ("current_scale", "current_state_10x")

TIMESTAMPS = {
    "ledger_source_observed_at_utc": "2026-08-16T00:00:00+00:00",
    "prepared_option_received_at_utc": "2026-08-16T00:00:01+00:00",
    "broker_source_observed_at_utc": "2026-08-16T00:00:02+00:00",
    "broker_promoted_at_utc": "2026-08-16T00:00:03+00:00",
    "required_data_sealed_at_utc": "2026-08-16T00:00:04+00:00",
    "candidate_results_sealed_at_utc": "2026-08-16T00:00:05+00:00",
}
FIXED_COUNTS = {
    "accounts": 1,
    "sections": 5,
    "currencies": 3,
    "source_status_receipts": 4,
    "replay_bindings": 5,
    "candidate_owners": 2,
    "quality_markets": 0,
}
CURRENT_SCALE = {
    "position_lots": 55,
    "lifecycle_by_lot": 0,
    "lifecycle_by_case": 0,
    "quality_operational_cases": 0,
    "combo_groups": 3,
    "assigned_stock_entries": 10,
    "broker_stock_symbols": 9,
    "cash_occupation_symbols": 4,
    "candidate_scopes": 16,
}
CURRENT_STATE_10X = {
    "multiplier": 10,
    "position_lots": 550,
    "lifecycle_by_lot": 0,
    "lifecycle_by_case": 0,
    "quality_operational_cases": 0,
    "combo_groups": 30,
    "assigned_stock_entries": 100,
    "broker_stock_symbols": 90,
    "cash_occupation_symbols": 40,
    "candidate_scopes": 160,
}

# Re-pinned only after both profiles pass all real owner validators.
EXPECTED_FIXTURE_PAYLOAD_SHA256 = {
    "current_scale": "344f3d943f56981174974f64cd5965a5345d057d1a7dfe1aabfa33379d17d4f7",
    "current_state_10x": "8e057aa0dc95b4af636ef50bfcd9905f691961cc9c3e08f660a0d36096ec4e09",
}

_H = {
    name: sha256_bytes(f"fixture:{name}".encode())
    for name in ("implementation", "fx", "ledger", "portfolio", "earnings", "policy")
}
_LIFECYCLE_VIEW_KEYS = frozenset(
    "schema_version lifecycle_case_id lifecycle_state lifecycle_evidence_status "
    "lifecycle_reason_codes observation_start_ms pending_until_ms timing_policy_hash "
    "target_contracts_by_lot resolved_contracts_by_lot remaining_contracts_by_lot "
    "resolved_contracts_by_terminal_type reserved_contracts_by_lot closure_fact "
    "reason_state close_reason lifecycle_generation_token actionable".split()
)
_QUALITY_DETAIL_KEYS = frozenset(
    "case_id market status trust_class evidence_count settlement_deadline_ms "
    "reason_state timing_policy_hash dataset_status blocked_consumers".split()
)
_FORBIDDEN_HISTORY_READERS = frozenset(
    {
        "list_trade_events",
        "list_assigned_stock_events",
        "list_assigned_stock_events_for_account",
        "list_trade_lifecycle_cases",
        "list_trade_lifecycle_evidence",
        "list_trade_lifecycle_allocations",
        "list_trade_lifecycle_source_consumptions",
        "preview_current_decision_projection_oracle",
        "rglob",
        "walk",
    }
)


def fixture_descriptor() -> dict[str, Any]:
    return {
        "schema_version": FIXTURE_SCHEMA,
        "seed": SEED,
        "run_id": RUN_ID,
        "account": ACCOUNT,
        "timestamps": dict(TIMESTAMPS),
        "fixed": dict(FIXED_COUNTS),
        "provenance": {
            "baseline_kind": "independent_per_dimension_maxima",
            "source_version": "v1.13.23",
            "source_run_id": "20260814T170014Z-7b61b5",
            "ledger_counts_source": "sqlite_query_only_aggregation",
            "run_counts_source": "validated_owner_manifests_and_receipts",
            "production_payload_copied": False,
            "raw_account_identifier_copied": False,
        },
        "measurement_metadata": {
            "python_version": "unknown_until_formal_execution",
            "sqlite_version": "unknown_until_formal_execution",
            "platform": "unknown_until_formal_execution",
            "source_git_sha": "unknown_until_formal_execution",
            "uncompressed_size_distribution": "independent_dimension_maxima",
            "compression_ratio_distribution": "not_measured",
            "entropy_classes": ["low", "median", "high"],
            "cold_warm_mode": "cold_fixture_then_5_warmups_30_measurements",
        },
        "current_scale": dict(CURRENT_SCALE),
        "current_state_10x": dict(CURRENT_STATE_10X),
    }


def _fixture_descriptor_status() -> dict[str, Any]:
    try:
        raw = FIXTURE_DESCRIPTOR_PATH.read_bytes()
    except FileNotFoundError:
        return {
            "descriptor": None,
            "sha256": None,
            "violations": ["fixture_descriptor_missing"],
        }
    except OSError:
        return {
            "descriptor": None,
            "sha256": None,
            "violations": ["fixture_descriptor_unreadable"],
        }

    violations: list[str] = []
    descriptor: dict[str, Any] | None = None
    digest: str | None = None
    try:
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("fixture descriptor must be an object")
        canonical = canonical_json_bytes(decoded)
        descriptor = decoded
        digest = sha256_bytes(canonical)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        violations.append("fixture_descriptor_drift")
    else:
        if raw != canonical + b"\n" or descriptor != fixture_descriptor():
            violations.append("fixture_descriptor_drift")
        if digest != FIXTURE_CONTRACT_SHA256:
            violations.append("fixture_contract_sha256_mismatch")
    return {
        "descriptor": descriptor,
        "sha256": digest,
        "violations": sorted(set(violations)),
    }


def fixture_contract_sha256() -> str:
    status = _fixture_descriptor_status()
    if status["violations"]:
        raise RuntimeError("invalid checked-in fixture descriptor: " + ",".join(status["violations"]))
    return str(status["sha256"])


def _position_lots(count: int) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        symbol = f"S{index:04d}"
        fields = {key: None for key in CURRENT_DECISION_POSITION_FIELDS}
        fields.update(
            {
                "account": ACCOUNT,
                "broker": "futu",
                "contracts": 1,
                "contracts_closed": 0,
                "contracts_open": 1,
                "currency": "USD",
                "expiration": "2026-12-18",
                "expiration_ymd": "2026-12-18",
                "multiplier": 100,
                "opened_at": 1_760_000_000_000 + index,
                "option_type": "put",
                "position_id": f"position-{index:04d}",
                "premium": "1",
                "side": "short",
                "source_event_id": f"open-{index:04d}",
                "status": "open",
                "strategy": "sell_put",
                "strike": "100",
                "symbol": symbol,
            }
        )
        rows.append({"record_id": f"lot-{index:04d}", "fields": fields})
    return rows


def _assigned_stock(count: int) -> dict[str, Any]:
    empty_chain = canonical_sha256([])
    lots = [
        {
            "stock_lot_id": f"stock-{index:04d}",
            "source_assignment_event_id": f"assignment-{index:04d}",
            "source_option_lot_id": f"lot-{index:04d}",
            "account": ACCOUNT,
            "broker": "futu",
            "symbol": f"S{index:04d}",
            "currency": "USD",
            "assigned_at_ms": 1_760_000_000_000 + index,
            "shares_opened": 100,
            "shares_remaining": 100,
            "assignment_price": "100",
            "remaining_cost_basis": "10000",
            "basis_policy": "strike",
            "strategy": "sell_put",
            "leg_role": "funding_put",
            "strategy_group_id": f"group-{index:04d}",
            "yield_enhancement_mode": "combo_yield",
            "source_option_leg_role": "funding_put",
            "sale_fact_count": 0,
            "sale_fact_chain_sha256": empty_chain,
        }
        for index in range(count)
    ]
    payload = {
        "schema_version": CURRENT_ASSIGNED_STOCK_SCHEMA,
        "account": ACCOUNT,
        "lots": lots,
        "covered_call_allocations": [],
        "review_facts": [],
        "applied_sale_fact_count": 0,
        "applied_sale_fact_chain_sha256": canonical_sha256(
            [
                {
                    "stock_lot_id": row["stock_lot_id"],
                    "sale_fact_count": 0,
                    "sale_fact_chain_sha256": empty_chain,
                }
                for row in lots
            ]
        ),
    }
    payload["current_view_hash"] = canonical_sha256(payload)
    return payload


def _combo_facts(count: int) -> dict[str, Any]:
    groups = []
    for index in range(count):
        row = {
            "schema_version": CURRENT_COMBO_GROUP_FACT_SCHEMA,
            "group_id": f"group-{index:04d}",
            "identity_hash": canonical_sha256({"group": index}),
            "account": ACCOUNT,
            "symbol": f"S{index:04d}",
            "strategy": "combo_yield",
            "original_contracts": 1,
            "expected_roles": ["funding_put", "participation_call"],
            "active_member_bindings": [],
            "assigned_stock_lot_ids": [f"stock-{index:04d}"],
            "status": "assigned_stock_only",
            "reason_codes": [],
        }
        row["fact_sha256"] = canonical_sha256(row)
        groups.append(row)
    payload = {"schema_version": CURRENT_COMBO_SCHEMA, "current_groups": groups}
    payload["current_groups_hash"] = canonical_sha256(payload)
    return payload


def _current_payload(lots: list[dict[str, Any]], counts: dict[str, int]) -> dict[str, Any]:
    lots_hash = canonical_sha256(lots)
    source = {
        "projector_schema": POSITION_PROJECTION_SCHEMA,
        "projector_implementation_fingerprint": _H["implementation"],
        "source_generation": 1,
        "sqlite_schema_cookie": 1,
    }
    head = {
        "projector_schema": POSITION_PROJECTION_SCHEMA,
        "projector_implementation_fingerprint": _H["implementation"],
        "status": "trusted",
        "lots_generation": 1,
        "built_source_generation": 1,
        "built_lots_generation": 1,
        "projection_fingerprint": lots_hash,
        "lot_count": len(lots),
    }
    generation = {
        "account": ACCOUNT,
        "generation": 0,
        "case_generation": 0,
        "evidence_generation": 0,
        "allocation_generation": 0,
        "source_consumption_generation": 0,
        "timing_generation": 0,
        "combo_identity_generation": 0,
        "assigned_stock_generation": 0,
    }
    payload = build_current_decision_projection_payload(
        account=ACCOUNT,
        current_inputs={
            "source": source,
            "head": head,
            "generation": generation,
            "lots": lots,
            "schema_cookie": 1,
            "lots_fingerprint": lots_hash,
            "lot_count": len(lots),
            "identities": [],
        },
        case_facts=[],
        assigned_stock=empty_assigned_stock_fact(ACCOUNT),
        lifecycle_quality=build_lifecycle_quality_fact(
            account=ACCOUNT,
            all_case_facts=[],
            operational_case_facts=[],
        ),
        updated_at_ms=1_760_000_100_000,
        implementation_fingerprint=_H["implementation"],
    )
    payload["assigned_stock"] = _assigned_stock(counts["assigned_stock_entries"])
    payload["combo"] = _combo_facts(counts["combo_groups"])
    payload["decision_state_fingerprint"] = canonical_sha256(
        {key: value for key, value in payload.items() if key not in {"decision_state_fingerprint", "updated_at_ms"}}
    )
    return validate_current_decision_projection_payload(payload)


def owner_valid_schema_probe() -> bool:
    lot = _position_lots(1)[0]
    fact = build_initial_lifecycle_case_decision_fact(
        lifecycle_case={
            "case_id": "probe-case",
            "account": ACCOUNT,
            "market": "US",
            "broker": "futu",
            "symbol": lot["fields"]["symbol"],
            "option_type": "put",
            "position_side": "short",
            "strike": 100,
            "expiration_ymd": "2026-12-18",
            "contract_key": "futu|acct_fixture|S0000|put|short|100|2026-12-18",
            "target_contracts_by_lot": {lot["record_id"]: 1},
            "status": "waiting_settlement_evidence",
            "derived_summary": {"reason_state": "cause_pending"},
        },
        fact_state={"evidence_revision": 0, "evidence_count": 0},
        timing={
            "observation_start_ms": 1_760_000_000_000,
            "pending_until_ms": 1_760_086_400_000,
            "timing_policy_hash": _H["policy"],
        },
    )
    by_lot, by_case = lifecycle_views_by_lot(
        {"operational_cases": [fact]},
        current_position_lots=[lot],
        now_ms=1_760_000_100_000,
    )
    quality = derive_lifecycle_quality_view(
        build_lifecycle_quality_fact(
            account=ACCOUNT,
            all_case_facts=[fact],
            operational_case_facts=[fact],
        ),
        now_ms=1_760_000_100_000,
    )
    return (
        len(by_lot) == len(by_case) == len(quality["operational_cases"]) == 1
        and all(set(row) == _LIFECYCLE_VIEW_KEYS for row in by_lot.values())
        and all(set(row) == _LIFECYCLE_VIEW_KEYS for row in by_case.values())
        and all(set(row) == _QUALITY_DETAIL_KEYS for row in quality["operational_cases"])
    )


def _candidate_bundle(
    count: int,
    *,
    config_hash: str,
    required_raw_hash: str,
) -> tuple[dict[str, Any], dict[str, bytes], list[dict[str, str]]]:
    scopes = sorted(
        [
            {
                "market": "HK" if index % 2 else "US",
                "symbol": f"C{index:04d}",
                "strategy_family": "combo_yield",
                "strategy_mode": "combo_yield",
                "candidate_owner": "cc_lp" if index % 2 else "sp_lc",
            }
            for index in range(count)
        ],
        key=lambda row: (row["market"], row["symbol"], row["strategy_family"]),
    )
    items = [
        {
            "schema_version": STRATEGY_SCAN_STATUS_SCHEMA,
            "run_id": RUN_ID,
            "account": ACCOUNT,
            "market": scope["market"],
            "symbol": scope["symbol"],
            "strategy_family": scope["strategy_family"],
            "status": "completed",
            "published_at_utc": TIMESTAMPS["candidate_results_sealed_at_utc"],
            "candidate_count": 0,
            "strategy_mode": scope["strategy_mode"],
            "candidate_owner": scope["candidate_owner"],
            "account_config_sha256": config_hash,
            "source_status_schema": STRATEGY_SCAN_STATUS_SCHEMA,
            "source_status_path": (f"{scope['symbol'].lower()}_combo_yield_scan_status.json"),
        }
        for scope in scopes
    ]
    counts = {
        status: sum(row["status"] == status for row in items)
        for status in ("completed", "unavailable", "failed", "not_applicable")
    }
    index = {
        "schema_version": STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA,
        "run_id": RUN_ID,
        "account": ACCOUNT,
        "account_config_sha256": config_hash,
        "published_at_utc": TIMESTAMPS["candidate_results_sealed_at_utc"],
        "expected_count": len(items),
        "counts": counts,
        "items": items,
    }
    index["content_sha256"] = sha256_bytes(canonical_json_bytes(index))
    validate_strategy_scan_status_index_v2(
        index,
        expected_run_id=RUN_ID,
        expected_account=ACCOUNT,
        expected_account_config_sha256=config_hash,
    )
    index_raw = canonical_json_bytes(index)
    payloads = {STRATEGY_SCAN_STATUS_INDEX_V2_FILE: index_raw}
    dependencies = [
        {"kind": kind, "relpath": None, "sha256": digest}
        for kind, digest in sorted(
            {
                "earnings_rv": _H["earnings"],
                "fx": _H["fx"],
                "ledger": _H["ledger"],
                "portfolio": _H["portfolio"],
                "required_data": required_raw_hash,
            }.items()
        )
    ]
    owner_entries = []
    for owner, schema, relpath, validator in (
        (
            "cc_lp",
            CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
            f"state/{CC_LP_CANDIDATE_SNAPSHOT_FILE}",
            validate_cc_lp_candidate_snapshot,
        ),
        (
            "sp_lc",
            COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
            f"state/{COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE}",
            validate_combo_yield_candidate_snapshot,
        ),
    ):
        covered = [scope for scope in scopes if scope["candidate_owner"] == owner]
        normalized = normalize_combo_scope_results(
            [
                {
                    "symbol": scope["symbol"],
                    "strategy_mode": "combo_yield",
                    "variant": owner,
                    "status": "completed",
                }
                for scope in covered
            ],
            owner=owner,
        )
        snapshot = {
            "schema_version": schema,
            "run_id": RUN_ID,
            "account": ACCOUNT,
            "market": covered[0]["market"].lower(),
            "candidate_owner": owner,
            "account_config_sha256": config_hash,
            "strategy_policy_sha256": _H["policy"],
            "required_data_manifest_sha256": required_raw_hash,
            "dependencies": dependencies,
            "sealed_at_utc": TIMESTAMPS["candidate_results_sealed_at_utc"],
            "opening_status": "no_candidate",
            "scope_results": normalized,
            "ranked_pairs": [],
        }
        if owner == "sp_lc":
            snapshot.update(
                {
                    "funding_put_decisions": [],
                    "pair_evaluations": [],
                    "rank_records": [],
                }
            )
        snapshot["content_sha256"] = canonical_sha256(snapshot)
        validator(snapshot, expected_run_id=RUN_ID, expected_account=ACCOUNT)
        raw = canonical_json_bytes(snapshot)
        payloads[relpath] = raw
        owner_entries.append(
            {
                "candidate_owner": owner,
                "schema_version": schema,
                "relpath": relpath,
                "sha256": sha256_bytes(raw),
                "content_sha256": snapshot["content_sha256"],
                "opening_status": "no_candidate",
                "covered_scopes": covered,
            }
        )
    manifest = {
        "schema_version": CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA,
        "run_id": RUN_ID,
        "account": ACCOUNT,
        "markets": sorted({scope["market"] for scope in scopes}),
        "account_config_sha256": config_hash,
        "strategy_policy_sha256": _H["policy"],
        "sealed_at_utc": TIMESTAMPS["candidate_results_sealed_at_utc"],
        "completion_reason": "complete",
        "expected_scopes": scopes,
        "expected_owners": ["cc_lp", "sp_lc"],
        "status_index": {
            "schema_version": STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA,
            "relpath": STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
            "sha256": sha256_bytes(index_raw),
            "content_sha256": index["content_sha256"],
        },
        "owner_snapshots": owner_entries,
    }
    manifest["content_sha256"] = canonical_sha256(manifest)
    validate_candidate_snapshot_manifest(manifest, expected_run_id=RUN_ID, expected_account=ACCOUNT)
    chosen = {
        key: manifest[key]
        for key in (
            "completion_reason",
            "expected_scopes",
            "expected_owners",
            "status_index",
            "owner_snapshots",
        )
    }
    return manifest, payloads, scopes


def _replay_bundle(
    candidate_count: int,
    *,
    decision_state_fingerprint: str,
) -> tuple[list[dict[str, Any]], dict[str, bytes], dict[str, Any], list[dict[str, str]]]:
    config_raw = canonical_account_run_config_bytes({"portfolio": {"account": ACCOUNT}, "runtime": {}, "symbols": []})
    config_hash = sha256_bytes(config_raw)
    option_payload_hash = canonical_sha256({"option_positions": ACCOUNT})
    option_manifest = {
        "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
        "run_id": RUN_ID,
        "account": ACCOUNT,
        "status": "ready",
        "account_config_sha256": config_hash,
        "source_observed_at": TIMESTAMPS["ledger_source_observed_at_utc"],
        "application_received_at_utc": TIMESTAMPS["prepared_option_received_at_utc"],
        "fx_status": "ready",
        "fx_observation_sha256": _H["fx"],
        "payload_relpath": PREPARED_OPTION_POSITIONS_PAYLOAD_NAME,
        "payload_sha256": option_payload_hash,
        "ledger_generation_sha256": _H["ledger"],
        "decision_state_fingerprint": decision_state_fingerprint,
    }
    portfolio_payload_hash = canonical_sha256({"portfolio": ACCOUNT})
    portfolio_manifest = {
        "schema_version": PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
        "run_id": RUN_ID,
        "account": ACCOUNT,
        "status": "ready",
        "preparation_started_at_utc": TIMESTAMPS["ledger_source_observed_at_utc"],
        "deadline_at_utc": TIMESTAMPS["broker_promoted_at_utc"],
        "child_finished_at_utc": TIMESTAMPS["broker_source_observed_at_utc"],
        "promoted_at_utc": TIMESTAMPS["broker_promoted_at_utc"],
        "prepared_at_utc": TIMESTAMPS["broker_promoted_at_utc"],
        "deadline_seconds": 3,
        "worker_returncode": 0,
        "account_config_sha256": config_hash,
        "portfolio_context_relpath": f"portfolio_context.{portfolio_payload_hash}.json",
        "payload_sha256": portfolio_payload_hash,
        "portfolio_source_name": "fixture",
        "portfolio_source_account": ACCOUNT,
        "source_as_of_utc": TIMESTAMPS["broker_source_observed_at_utc"],
    }
    symbol_row = {
        "status": "ready",
        "fetch_plan": {},
        "expected_fetch_contract": {},
        "expected_fetch_contract_sha256": _H["policy"],
        "fetch_policy_hash": _H["policy"],
        "receipt_relpath": "required_data/receipts/S0000.json",
        "receipt_hash": _H["portfolio"],
        "snapshot_id": "fixture-required-0001",
        "payload_sha256": _H["ledger"],
        "source_observed_at": TIMESTAMPS["required_data_sealed_at_utc"],
        "expires_at": "2026-08-16T00:30:04+00:00",
        "raw_json_relpath": "required_data/raw/S0000.json",
        "required_data_csv_relpath": "required_data/S0000.csv",
        "source_outcome": "success_rows",
    }
    required_manifest = {
        "schema_version": REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
        "run_id": RUN_ID,
        "status": "complete",
        "plan_id": _H["policy"],
        "sealed_at_utc": TIMESTAMPS["required_data_sealed_at_utc"],
        "required_data_root_relpath": "required_data",
        "symbols": {"S0000": symbol_row},
        "summary": {"symbols_total": 1, "ready": 1, "failed": 0},
    }
    required_manifest["content_sha256"] = canonical_sha256(required_manifest)
    required_raw = canonical_json_bytes(required_manifest)
    candidate_manifest, candidate_payloads, scopes = _candidate_bundle(
        candidate_count,
        config_hash=config_hash,
        required_raw_hash=sha256_bytes(required_raw),
    )
    main = {
        "account_config": (
            "state/config.override.json",
            "account_config.v1",
            config_raw,
            None,
        ),
        "candidate_snapshot_manifest": (
            f"state/{CANDIDATE_SNAPSHOT_MANIFEST_FILE}",
            CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA,
            canonical_json_bytes(candidate_manifest),
            candidate_manifest["content_sha256"],
        ),
        "prepared_option_positions_context": (
            f"state/{PREPARED_OPTION_POSITIONS_MANIFEST_NAME}",
            PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
            canonical_json_bytes(option_manifest),
            option_payload_hash,
        ),
        "prepared_portfolio_context": (
            "state/prepared_portfolio_context.v1.json",
            PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
            canonical_json_bytes(portfolio_manifest),
            portfolio_payload_hash,
        ),
        "required_data_snapshot": (
            "state/required_data_snapshot_manifest.json",
            REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
            required_raw,
            required_manifest["content_sha256"],
        ),
    }
    bindings = []
    payloads = dict(candidate_payloads)
    for role, (relpath, schema, raw, content_hash) in main.items():
        payloads[relpath] = raw
        bindings.append(
            {
                "role": role,
                "schema_version": schema,
                "relpath": relpath,
                "sha256": sha256_bytes(raw),
                "content_sha256": content_hash,
            }
        )
    chosen = {
        key: candidate_manifest[key]
        for key in (
            "completion_reason",
            "expected_scopes",
            "expected_owners",
            "status_index",
            "owner_snapshots",
        )
    }
    return sorted(bindings, key=lambda row: row["role"]), payloads, chosen, scopes


def _owner_receipt(
    binding: dict[str, Any],
    *,
    owner_status: str,
    observed: str,
    received: str,
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "owner_schema_version": binding["schema_version"],
        "owner_status": owner_status,
        "reason_codes": [],
        "manifest_sha256": binding["sha256"],
        "content_sha256": binding["content_sha256"],
        "source_observed_at_utc": observed,
        "application_received_at_utc": received,
        "completeness": {"status": "complete", "reason_codes": []},
        "freshness": freshness or {"authority": "not_applicable", "status": "not_applicable", "reason_codes": []},
    }


def _shape_evidence(
    *,
    counts: dict[str, int],
    current_read: dict[str, Any],
    chosen: dict[str, Any],
    broker_stocks: dict[str, Any],
    occupation: dict[str, Any],
    bindings: list[dict[str, Any]],
    receipts: dict[str, Any],
    reference_payloads: dict[str, bytes],
) -> tuple[bool, bool]:
    payload = validate_current_decision_projection_payload(current_read["payload"])
    owner_payloads = validate_replay_bundle(
        expected_run_id=RUN_ID,
        expected_account=ACCOUNT,
        replay_bindings=bindings,
        chosen_results=chosen,
        reference_payloads=reference_payloads,
    )
    scalable = {
        "position_lots": current_read["position_lots"],
        "lifecycle_by_lot": current_read["lifecycle_by_lot"],
        "lifecycle_by_case": current_read["lifecycle_by_case"],
        "quality_operational_cases": current_read["lifecycle_quality"]["operational_cases"],
        "combo_groups": payload["combo"]["current_groups"],
        "assigned_stock_entries": payload["assigned_stock"]["lots"],
        "broker_stock_symbols": broker_stocks,
        "cash_occupation_symbols": occupation,
        "candidate_scopes": chosen["expected_scopes"],
    }
    nested = (
        all(set(row.get("fields", {})) == CURRENT_DECISION_POSITION_FIELDS for row in current_read["position_lots"])
        and all(set(row) == _LIFECYCLE_VIEW_KEYS for row in current_read["lifecycle_by_lot"].values())
        and all(set(row) == _LIFECYCLE_VIEW_KEYS for row in current_read["lifecycle_by_case"].values())
        and all(set(row) == _QUALITY_DETAIL_KEYS for row in current_read["lifecycle_quality"]["operational_cases"])
    )
    fixed = {
        "accounts": 1,
        "sections": 5,
        "currencies": 3,
        "source_status_receipts": len(receipts),
        "replay_bindings": len(bindings),
        "candidate_owners": len(chosen["owner_snapshots"]),
        "quality_markets": len(current_read["lifecycle_quality"]["aggregate_by_market"]),
    }
    shape_matches = nested and {key: len(value) for key, value in scalable.items()} == counts and fixed == FIXED_COUNTS
    return shape_matches, set(owner_payloads) == {row["role"] for row in bindings}


def generate_fixture(profile: str, *, verify_payload_hash: bool = True) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    contract = _fixture_descriptor_status()
    contract_matches = not contract["violations"]
    declared = dict(CURRENT_SCALE if profile == "current_scale" else CURRENT_STATE_10X)
    counts = dict(declared)
    counts.pop("multiplier", None)
    lots = _position_lots(counts["position_lots"])
    current_payload = _current_payload(lots, counts)
    lifecycle_by_lot: dict[str, Any] = {}
    lifecycle_by_case: dict[str, Any] = {}
    quality = derive_lifecycle_quality_view(
        current_payload["lifecycle_quality"],
        now_ms=1_760_000_100_000,
    )
    current_read = {
        "schema_version": CURRENT_DECISION_READ_SCHEMA,
        "status": "trusted",
        "account": ACCOUNT,
        "reason": None,
        "payload": current_payload,
        "position_lots": lots,
        "lot_count": len(lots),
        "lifecycle_by_lot": lifecycle_by_lot,
        "lifecycle_by_case": lifecycle_by_case,
        "lifecycle_quality": quality,
    }
    decision_fingerprint = canonical_sha256({"fixture": profile, "account": ACCOUNT})
    bindings, reference_payloads, chosen, scopes = _replay_bundle(
        counts["candidate_scopes"],
        decision_state_fingerprint=decision_fingerprint,
    )
    broker_stocks = {
        f"B{index:04d}": {
            "account": ACCOUNT,
            "symbol": f"B{index:04d}",
            "quantity": 100,
            "market_value": 10_000 + index,
        }
        for index in range(counts["broker_stock_symbols"])
    }
    occupation = {f"S{index:04d}": {"USD": 10_000 + index} for index in range(counts["cash_occupation_symbols"])}
    broker_source = {
        "filters": {"market": "us"},
        "source_account_identifiers": [ACCOUNT],
        "capacity_authority": "prepared_portfolio_context",
        "capacity_identity_hash": _H["portfolio"],
        "cash_by_currency": {"CNY": 1_000_000, "HKD": 800_000, "USD": 200_000},
        "cash_components_by_currency": {
            "CNY": {"settled": 1_000_000},
            "HKD": {"settled": 800_000},
            "USD": {"settled": 200_000},
        },
        "cash_capacity_by_currency": {"CNY": 1_000_000, "HKD": 800_000, "USD": 200_000},
        "cash_source": "fixture",
        "cash_power_by_currency": {"CNY": 1_000_000, "HKD": 800_000, "USD": 200_000},
        "cash_power_source": "fixture",
        "exchange_rates": {"CNY": 1, "HKD": 0.92, "USD": 7.2},
        "exchange_rate_status": "ready",
        "stocks_by_symbol": broker_stocks,
        "raw_selected_count": len(broker_stocks),
        "portfolio_source_name": "fixture",
    }
    occupation_source = {
        "filters": {"market": "us"},
        "cash_secured_by_symbol_by_ccy": occupation,
        "cash_secured_total_by_ccy": {"CNY": 0, "HKD": 0, "USD": 640_000},
        "cash_secured_unavailable_by_symbol": {},
        "cash_secured_total_cny": 4_608_000,
        "locked_shares_by_symbol": {},
        "locked_shares_unavailable_by_symbol": {},
        "locked_shares_status": "ready",
        "locked_shares_unavailable_reason": None,
    }
    complete = {"account": ACCOUNT, "completeness_status": "complete", "completeness_reason_codes": []}
    sections = {
        "ledger_projection": build_runtime_portfolio_section(
            "ledger_projection",
            source_observed_at_utc=TIMESTAMPS["ledger_source_observed_at_utc"],
            application_received_at_utc=TIMESTAMPS["prepared_option_received_at_utc"],
            facts=project_ledger_projection_facts(
                current_decision_read=current_read,
                decision_state_fingerprint=decision_fingerprint,
            ),
            **complete,
        ),
        "broker_cash": build_runtime_portfolio_section(
            "broker_cash",
            source_observed_at_utc=TIMESTAMPS["broker_source_observed_at_utc"],
            application_received_at_utc=TIMESTAMPS["broker_promoted_at_utc"],
            facts=project_broker_cash_facts(broker_source),
            **complete,
        ),
        "broker_positions": build_runtime_portfolio_section(
            "broker_positions",
            source_observed_at_utc=TIMESTAMPS["broker_source_observed_at_utc"],
            application_received_at_utc=TIMESTAMPS["broker_promoted_at_utc"],
            facts=project_broker_positions_facts(broker_source),
            **complete,
        ),
        "cash_occupation": build_runtime_portfolio_section(
            "cash_occupation",
            source_observed_at_utc=TIMESTAMPS["ledger_source_observed_at_utc"],
            application_received_at_utc=TIMESTAMPS["prepared_option_received_at_utc"],
            facts=project_cash_occupation_facts(occupation_source),
            freshness_authority="prepared_option_positions_context.fx_status",
            freshness_status="ready",
            freshness_reason_codes=[],
            **complete,
        ),
    }
    by_role = {row["role"]: row for row in bindings}
    receipts = {
        "broker_portfolio": _owner_receipt(
            by_role["prepared_portfolio_context"],
            owner_status="ready",
            observed=TIMESTAMPS["broker_source_observed_at_utc"],
            received=TIMESTAMPS["broker_promoted_at_utc"],
        ),
        "candidate_results": _owner_receipt(
            by_role["candidate_snapshot_manifest"],
            owner_status="complete",
            observed=TIMESTAMPS["candidate_results_sealed_at_utc"],
            received=TIMESTAMPS["candidate_results_sealed_at_utc"],
        ),
        "ledger_projection": _owner_receipt(
            by_role["prepared_option_positions_context"],
            owner_status="ready",
            observed=TIMESTAMPS["ledger_source_observed_at_utc"],
            received=TIMESTAMPS["prepared_option_received_at_utc"],
        ),
        "required_data": _owner_receipt(
            by_role["required_data_snapshot"],
            owner_status="complete",
            observed=TIMESTAMPS["required_data_sealed_at_utc"],
            received=TIMESTAMPS["required_data_sealed_at_utc"],
        ),
    }
    sections["source_status"] = build_source_status_section(account=ACCOUNT, owner_receipts=receipts)
    legacy = {
        name: sections[name]["facts"]
        for name in ("ledger_projection", "broker_cash", "broker_positions", "cash_occupation")
    }
    comparison = compare_runtime_portfolio_snapshot(
        sections=sections,
        chosen_results=chosen,
        legacy_section_facts=legacy,
        legacy_chosen_results=chosen,
        ledger_shadow_status="matched",
    )
    kwargs = {
        "run_id": RUN_ID,
        "account": ACCOUNT,
        "sections": sections,
        "replay_bindings": bindings,
        "chosen_results": chosen,
        "legacy_comparison": comparison,
        "reference_payloads": reference_payloads,
    }
    hash_input = {
        **kwargs,
        "reference_payloads": {path: sha256_bytes(raw) for path, raw in sorted(reference_payloads.items())},
    }
    payload_hash = sha256_bytes(canonical_json_bytes(hash_input))
    payload_matches = payload_hash == EXPECTED_FIXTURE_PAYLOAD_SHA256[profile]
    shape_matches, owner_validators_passed = _shape_evidence(
        counts=counts,
        current_read=current_read,
        chosen=chosen,
        broker_stocks=broker_stocks,
        occupation=occupation,
        bindings=bindings,
        receipts=receipts,
        reference_payloads=reference_payloads,
    )
    if verify_payload_hash and not payload_matches:
        raise RuntimeError(f"{profile} fixture payload hash does not match the frozen value")
    scalable = {
        "position_lots": lots,
        "lifecycle_by_lot": lifecycle_by_lot,
        "lifecycle_by_case": lifecycle_by_case,
        "quality_operational_cases": quality["operational_cases"],
        "combo_groups": current_payload["combo"]["current_groups"],
        "assigned_stock_entries": current_payload["assigned_stock"]["lots"],
        "broker_stock_symbols": broker_stocks,
        "cash_occupation_symbols": occupation,
        "candidate_scopes": scopes,
    }
    return {
        "profile": profile,
        "counts": declared,
        "builder_kwargs": kwargs,
        "payload_sha256": payload_hash,
        "scalable_bytes": len(canonical_json_bytes(scalable)),
        "fixture_contract_sha256": contract["sha256"],
        "fixture_descriptor_violations": contract["violations"],
        "fixture_contract_matches": contract_matches,
        "fixture_payload_hash_matches": payload_matches,
        "fixture_shape_matches": shape_matches,
        "fixture_owner_validators_passed": owner_validators_passed,
        "owner_valid_schema_probe_passed": owner_valid_schema_probe(),
    }


def forbidden_history_structural_reference_count(source: str | None = None) -> int:
    tree = ast.parse(source if source is not None else inspect.getsource(snapshot_owner))
    return sum(
        1
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id in _FORBIDDEN_HISTORY_READERS)
        or (isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_HISTORY_READERS)
        or (isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] in _FORBIDDEN_HISTORY_READERS)
    )


def _run_with_forbidden_spies(
    action: Callable[[], Any],
    injected_probe: Callable[[], Any] | None,
) -> tuple[Any, int, int, Exception | None]:
    history_calls = 0
    artifact_reads = 0
    fixture_read_bytes = Path.read_bytes
    fixture_read_text = Path.read_text

    def history_spy(*_args: Any, **_kwargs: Any) -> list[Any]:
        nonlocal history_calls
        history_calls += 1
        return []

    def artifact_bytes_spy(path: Path, *args: Any, **kwargs: Any) -> bytes:
        nonlocal artifact_reads
        if path == FIXTURE_DESCRIPTOR_PATH:
            return fixture_read_bytes(path, *args, **kwargs)
        artifact_reads += 1
        return b""

    def artifact_text_spy(path: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal artifact_reads
        if path == FIXTURE_DESCRIPTOR_PATH:
            return fixture_read_text(path, *args, **kwargs)
        artifact_reads += 1
        return ""

    history_targets: list[tuple[Any, str]] = [
        (ledger_api, name) for name in _FORBIDDEN_HISTORY_READERS if callable(getattr(ledger_api, name, None))
    ]
    history_targets.extend(
        [
            (Path, "rglob"),
            (os, "walk"),
        ]
    )
    try:
        with ExitStack() as stack:
            for owner, name in history_targets:
                stack.enter_context(patch.object(owner, name, history_spy))
            stack.enter_context(patch.object(Path, "read_bytes", artifact_bytes_spy))
            stack.enter_context(patch.object(Path, "read_text", artifact_text_spy))
            result = action()
            if injected_probe is not None:
                injected_probe()
    except Exception as exc:  # receipt owns fail-closed reporting
        return None, history_calls, artifact_reads, exc
    return result, history_calls, artifact_reads, None


def benchmark_exit_code(receipt: dict[str, Any]) -> int:
    return 1 if receipt["violations"] else 0


def _p95(values: list[int]) -> int:
    return sorted(values)[max(0, (95 * len(values) + 99) // 100 - 1)]


def run_profile(
    profile: str,
    *,
    warmups: int,
    repetitions: int,
    fixture_mutator: Callable[[dict[str, Any]], None] | None = None,
    forbidden_source: str | None = None,
    executable_probe: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    fixture, generation_calls, generation_reads, generation_error = _run_with_forbidden_spies(
        lambda: generate_fixture(profile), None
    )
    if generation_error is not None:
        raise RuntimeError("benchmark fixture generation failed") from generation_error
    if fixture_mutator is not None:
        fixture_mutator(fixture["builder_kwargs"])
    structural = forbidden_history_structural_reference_count(forbidden_source)
    kwargs = fixture["builder_kwargs"]

    def build_and_verify() -> dict[str, Any]:
        snapshot = build_runtime_portfolio_snapshot(**kwargs)
        return snapshot_owner.verify_runtime_portfolio_snapshot(
            snapshot,
            expected_run_id=RUN_ID,
            expected_account=ACCOUNT,
            reference_payloads=kwargs["reference_payloads"],
        )

    snapshot, build_calls, build_reads, preflight_error = _run_with_forbidden_spies(build_and_verify, executable_probe)
    executable = generation_calls + build_calls
    artifact_reads = generation_reads + build_reads
    comparison = fixture["builder_kwargs"]["legacy_comparison"]
    comparison_matches = (
        comparison["status"] == "matched"
        and comparison["mismatch_count"] == 0
        and all(row["mismatch_count"] == 0 for row in comparison["sections"])
    )
    preflight: list[str] = []
    preflight.extend(fixture["fixture_descriptor_violations"])
    for field in (
        "fixture_contract_matches",
        "fixture_payload_hash_matches",
        "fixture_shape_matches",
        "fixture_owner_validators_passed",
        "owner_valid_schema_probe_passed",
    ):
        if not fixture[field]:
            preflight.append(field)
    if preflight_error is not None:
        preflight.append("fixture_owner_validators_passed")
    if structural:
        preflight.append("forbidden_history_structural_reference_count")
    if executable:
        preflight.append("forbidden_history_executable_spy_calls")
    if artifact_reads:
        preflight.append("production_artifact_read_calls")
    if not comparison_matches:
        preflight.append("legacy_comparison_matches")
    elapsed: list[int] = []
    peak = 0
    if not preflight:
        for _ in range(warmups):
            build_runtime_portfolio_snapshot(**fixture["builder_kwargs"])
        for _ in range(repetitions):
            started = time.perf_counter_ns()
            snapshot = build_runtime_portfolio_snapshot(**fixture["builder_kwargs"])
            elapsed.append(time.perf_counter_ns() - started)
        tracemalloc.start()
        build_runtime_portfolio_snapshot(**fixture["builder_kwargs"])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    artifact_bytes = 0 if snapshot is None else len(canonical_json_bytes(snapshot))
    current = generate_fixture("current_scale", verify_payload_hash=False)
    ten_x = generate_fixture("current_state_10x", verify_payload_hash=False)
    ratio = ten_x["scalable_bytes"] / current["scalable_bytes"]
    receipt = {
        "schema_version": "runtime_portfolio_snapshot_benchmark.v1",
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "fixture_contract_sha256": fixture["fixture_contract_sha256"],
        "fixture_payload_sha256": fixture["payload_sha256"],
        "counts": fixture["counts"],
        "fixed_counts": FIXED_COUNTS,
        "scalable_canonical_bytes": fixture["scalable_bytes"],
        "scalable_bytes_ratio_10x": ratio,
        "canonical_json_bytes": artifact_bytes,
        "median_build_wall_ms": None if not elapsed else statistics.median(elapsed) / 1_000_000,
        "p95_build_wall_ms": None if not elapsed else _p95(elapsed) / 1_000_000,
        "python_peak_allocation_bytes": peak,
        "forbidden_history_structural_reference_count": structural,
        "forbidden_history_executable_spy_calls": executable,
        "forbidden_history_reader_calls": structural + executable,
        "production_artifact_read_calls": artifact_reads,
        "fixture_contract_matches": fixture["fixture_contract_matches"],
        "fixture_payload_hash_matches": fixture["fixture_payload_hash_matches"],
        "fixture_shape_matches": fixture["fixture_shape_matches"],
        "fixture_owner_validators_passed": (fixture["fixture_owner_validators_passed"] and preflight_error is None),
        "owner_valid_schema_probe_passed": fixture["owner_valid_schema_probe_passed"],
        "legacy_comparison_status": comparison["status"],
        "legacy_comparison_mismatch_count": comparison["mismatch_count"],
        "legacy_comparison_mismatch_samples": comparison["mismatch_samples"],
        "legacy_comparison_sections": comparison["sections"],
        "legacy_comparison_matches": comparison_matches,
        "preflight_error": (None if preflight_error is None else type(preflight_error).__name__),
    }
    violations = list(preflight)
    if artifact_bytes >= MAX_CANONICAL_BYTES:
        violations.append("canonical_json_bytes")
    if receipt["p95_build_wall_ms"] is not None and receipt["p95_build_wall_ms"] > WALL_LIMIT_MS:
        violations.append("p95_build_wall_ms")
    if peak > ALLOCATION_LIMIT_BYTES:
        violations.append("python_peak_allocation_bytes")
    if not 9.75 <= ratio <= 10.25:
        violations.append("scalable_bytes_ratio_10x")
    receipt["violations"] = sorted(set(violations))
    return receipt


def _write_receipt(path: Path, receipt: dict[str, Any], *, append: bool) -> None:
    if append and path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        rows = existing if isinstance(existing, list) else [existing]
        rows.append(receipt)
        value: Any = rows
    else:
        value = receipt
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the synthetic runtime portfolio snapshot builder")
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--warmups", type=int, default=WARMUPS)
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    output = parser.add_mutually_exclusive_group(required=False)
    output.add_argument("--output", type=Path)
    output.add_argument("--append-output", type=Path)
    args = parser.parse_args(argv)
    if args.warmups < 0 or args.repetitions < 1:
        parser.error("warmups must be non-negative and repetitions must be positive")
    receipt = run_profile(args.profile, warmups=args.warmups, repetitions=args.repetitions)
    if args.output is not None:
        _write_receipt(args.output, receipt, append=False)
    elif args.append_output is not None:
        _write_receipt(args.append_output, receipt, append=True)
    else:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return benchmark_exit_code(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
