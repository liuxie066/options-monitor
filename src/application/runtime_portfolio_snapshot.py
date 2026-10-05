from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.account_config import normalize_account_label
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA,
    CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA,
    CandidateSnapshotManifestError,
    validate_candidate_snapshot_manifest,
)
from src.application.candidate_evidence_history import (
    validate_historical_candidate_snapshot_bundle_bytes,
)
from src.application.cc_lp_candidate_snapshot import (
    CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
    CcLpCandidateSnapshotError,
    validate_cc_lp_candidate_snapshot,
)
from src.application.ledger.api import (
    CURRENT_DECISION_READ_SCHEMA,
    CurrentDecisionProjectionError,
    validate_current_decision_projection_payload,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
    ComboYieldCandidateSnapshotError,
    validate_combo_yield_candidate_snapshot,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
    OpeningCandidateSnapshotError,
    validate_opening_candidate_snapshot,
)
from src.application.prepared_option_positions_context import (
    PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
    PREPARED_OPTION_POSITIONS_MANIFEST_NAME,
    PREPARED_OPTION_POSITIONS_PAYLOAD_NAME,
)
from src.application.prepared_portfolio_context import (
    PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
)
from src.application.required_data_snapshot import (
    REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
)
from src.application.source_receipts import sha256_bytes
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V5_SCHEMA,
    StrategyScanStatusError,
    validate_strategy_scan_status_index,
)
from src.application.wheel.candidate_snapshot import (
    WHEEL_CANDIDATE_SNAPSHOT_SCHEMA,
    WheelCandidateSnapshotError,
    validate_wheel_candidate_snapshot,
)
from src.application.experience_candidate_snapshot import (
    EXPERIENCE_CANDIDATE_MANIFEST_FILE,
    EXPERIENCE_CANDIDATE_MANIFEST_SCHEMA,
)
from src.application.tick_run_workspace import (
    ACCOUNT_RUN_CONFIG_NAME,
    AccountRunConfigError,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)


SCHEMA_VERSION = "runtime_portfolio_snapshot.v2"
LEGACY_SCHEMA_VERSION = "runtime_portfolio_snapshot.v1"
SHADOW_SCHEMA_VERSION = "runtime_portfolio_snapshot_shadow.v1"
LEDGER_SHADOW_SCHEMA_VERSION = "runtime_portfolio_snapshot.ledger_shadow.v1"
ARTIFACT_NAME = f"{SCHEMA_VERSION}.json"
LEGACY_ARTIFACT_NAME = f"{LEGACY_SCHEMA_VERSION}.json"
CANONICALIZATION = "json.sort_keys.compact.utf8.v1"
MAX_CANONICAL_BYTES = 1_048_576

SECTION_NAMES = tuple("ledger_projection broker_cash broker_positions cash_occupation source_status".split())
SECTION_SCHEMA_VERSIONS = {name: f"runtime_portfolio_snapshot.{name}.v1" for name in SECTION_NAMES}
BROKER_CASH_SCHEMA_V1 = SECTION_SCHEMA_VERSIONS["broker_cash"]
SECTION_SCHEMA_VERSIONS["broker_cash"] = "runtime_portfolio_snapshot.broker_cash.v2"
SOURCE_OWNERS = tuple("broker_portfolio candidate_results ledger_projection required_data".split())
REPLAY_BINDING_ROLES = tuple(
    "account_config candidate_snapshot_manifest prepared_option_positions_context "
    "prepared_portfolio_context required_data_snapshot".split()
)
_OWNER_BINDING_ROLES = {
    "ledger_projection": "prepared_option_positions_context",
    "broker_portfolio": "prepared_portfolio_context",
    "required_data": "required_data_snapshot",
    "candidate_results": "candidate_snapshot_manifest",
}
_ROLE_RELPATHS = {
    "account_config": f"state/{ACCOUNT_RUN_CONFIG_NAME}",
    "prepared_option_positions_context": (f"state/{PREPARED_OPTION_POSITIONS_MANIFEST_NAME}"),
    "prepared_portfolio_context": "state/prepared_portfolio_context.v1.json",
    "required_data_snapshot": "state/required_data_snapshot_manifest.json",
}
_ROLE_SCHEMAS = {
    "account_config": "account_config.v1",
    "prepared_option_positions_context": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
    "prepared_portfolio_context": PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
    "required_data_snapshot": REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
}


def _candidate_manifest_binding(schema_version: str) -> tuple[str, str]:
    if schema_version == CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA:
        return schema_version, f"state/{CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE}"
    if schema_version == CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA:
        return schema_version, f"state/{CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE}"
    if schema_version == EXPERIENCE_CANDIDATE_MANIFEST_SCHEMA:
        return schema_version, f"state/{EXPERIENCE_CANDIDATE_MANIFEST_FILE}"
    if schema_version == CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA:
        return schema_version, f"state/{CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE}"
    _fail("REFERENCE_SCHEMA_INVALID", "candidate manifest schema is unsupported")


def _prepared_option_binding(schema_version: str) -> tuple[str, str]:
    if schema_version == PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA:
        return schema_version, f"state/{PREPARED_OPTION_POSITIONS_MANIFEST_NAME}"
    _fail("REFERENCE_SCHEMA_INVALID", "prepared option context schema is unsupported")
CURRENT_DECISION_READ_KEYS = tuple(
    "account status reason payload lot_count lifecycle_by_lot lifecycle_by_case lifecycle_quality".split()
)
SECTION_FACT_KEYS = {
    "ledger_projection": tuple("read_schema_version position_lots current_decision decision_state_fingerprint".split()),
    "broker_cash": tuple(
        "filters source_account_identifiers capacity_authority capacity_identity_hash "
        "cash_by_currency cash_components_by_currency cash_capacity_by_currency "
        "cash_source cash_power_by_currency cash_power_source exchange_rates "
        "exchange_rate_status".split()
    ),
    "broker_positions": tuple(
        "filters source_account_identifiers capacity_authority capacity_identity_hash "
        "stocks_by_symbol raw_selected_count portfolio_source_name".split()
    ),
    "cash_occupation": tuple(
        "filters cash_secured_by_symbol_by_ccy cash_secured_total_by_ccy "
        "cash_secured_unavailable_by_symbol cash_secured_total_cny locked_shares_by_symbol "
        "locked_shares_unavailable_by_symbol locked_shares_status "
        "locked_shares_unavailable_reason".split()
    ),
}

BROKER_CASH_V1_FACT_KEYS = SECTION_FACT_KEYS["broker_cash"]
SECTION_FACT_KEYS["broker_cash"] += tuple(
    "portfolio_source_name cash_source_observed_at cash_snapshot cash_balance_reliable "
    "cash_balance_unavailable_by_row cash_source_observation_status source_observation_status".split()
)


def _positions_completeness(context: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = context.get("position_snapshot_input")
    complete = (isinstance(snapshot, Mapping) and snapshot.get("completeness") == "complete"
                and (snapshot.get("quality") or {}).get("status") == "ready" and not snapshot.get("errors"))
    return {"status": "complete" if complete else "unavailable",
            "reason_codes": [] if complete else ["broker_positions_unavailable"]}


_TOP_LEVEL_KEYS = set(
    "schema_version run_id account status reason_codes sections observed_time_range "
    "replay_bindings chosen_results ledger_shadow seal".split()
)
_LEGACY_TOP_LEVEL_KEYS = (_TOP_LEVEL_KEYS - {"ledger_shadow"}) | {"legacy_comparison"}
_SECTION_KEYS = set(
    "account schema_version source_observed_at_utc application_received_at_utc "
    "content_sha256 completeness freshness facts".split()
)
_OWNER_RECEIPT_KEYS = set(
    "owner_schema_version owner_status reason_codes manifest_sha256 content_sha256 "
    "source_observed_at_utc application_received_at_utc completeness freshness".split()
)
_REFERENCE_KEYS = set("schema_version relpath sha256 content_sha256".split())
_BINDING_KEYS = {*_REFERENCE_KEYS, "role"}
_OWNER_SNAPSHOT_KEYS = {*_REFERENCE_KEYS, "candidate_owner", "opening_status", "covered_scopes"}
_OWNER_SNAPSHOT_MODE_KEYS = {"scan_mode", "executable"}
_CHOSEN_KEYS = set("completion_reason expected_scopes expected_owners status_index owner_snapshots".split())
_SCOPE_KEYS = set("market symbol strategy_family strategy_mode candidate_owner".split())
_COMPARISON_NAMES = tuple("broker_cash broker_positions cash_occupation chosen_results ledger_projection".split())
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RuntimePortfolioSnapshotError(RuntimeError):
    """Raised when a compact runtime snapshot fails closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json_bytes(value: Any) -> bytes:
    """Encode exact JSON facts without dropping operational fields."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_JSON_INVALID",
            "runtime portfolio snapshot contains a non-canonical JSON value",
        ) from exc


def project_ledger_projection_facts(
    *,
    current_decision_read: Mapping[str, Any],
    decision_state_fingerprint: str,
) -> dict[str, Any]:
    read = _mapping(current_decision_read, "current_decision_read")
    keys = set(read)
    full_keys = {*CURRENT_DECISION_READ_KEYS, "schema_version", "position_lots"}
    if keys == full_keys:
        current_decision = _project_required(read, CURRENT_DECISION_READ_KEYS)
    elif keys == {"schema_version", "status", "account", "reason", "payload", "position_lots"}:
        current_decision = {key: read.get(key) for key in CURRENT_DECISION_READ_KEYS}
    elif keys == {"status", "reason"}:
        current_decision = {key: read.get(key) for key in CURRENT_DECISION_READ_KEYS}
    else:
        _fail("FIELD_INVALID", "current decision read fields do not match a known shape")
    return {
        "read_schema_version": read.get("schema_version"),
        "position_lots": read.get("position_lots"),
        "current_decision": current_decision,
        "decision_state_fingerprint": _sha256(decision_state_fingerprint, "decision_state_fingerprint"),
    }


def project_broker_cash_facts(source: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(source, "broker portfolio")
    return {**_project_required(source, BROKER_CASH_V1_FACT_KEYS),
            **{key: source.get(key) for key in SECTION_FACT_KEYS["broker_cash"] if key not in BROKER_CASH_V1_FACT_KEYS}}


def project_broker_positions_facts(source: Mapping[str, Any]) -> dict[str, Any]:
    return _project_required(_mapping(source, "broker portfolio"), SECTION_FACT_KEYS["broker_positions"])


def project_cash_occupation_facts(source: Mapping[str, Any]) -> dict[str, Any]:
    return _project_required(_mapping(source, "prepared option context"), SECTION_FACT_KEYS["cash_occupation"])


def build_runtime_portfolio_section(
    section_name: str,
    *,
    account: str,
    source_observed_at_utc: str,
    application_received_at_utc: str,
    facts: Mapping[str, Any],
    completeness_status: str,
    completeness_reason_codes: Sequence[str] = (),
    freshness_authority: str = "not_applicable",
    freshness_status: str = "not_applicable",
    freshness_reason_codes: Sequence[str] = (),
) -> dict[str, Any]:
    """Build one exact section from already-owned, already-read facts."""

    name = _one_of(section_name, set(SECTION_NAMES), "section_name")
    if name == "source_status":
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_SECTION_INVALID",
            "source_status must be built from owner receipts",
        )
    projected = _validated_section_facts(name, facts)
    freshness = _freshness(
        freshness_authority,
        freshness_status,
        freshness_reason_codes,
        path=f"sections.{name}.freshness",
    )
    if name != "cash_occupation" and freshness != _not_applicable_freshness():
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_FRESHNESS_INVALID",
            f"{name} freshness must be not_applicable",
        )
    if name == "cash_occupation" and freshness["authority"] != ("prepared_option_positions_context.fx_status"):
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_FRESHNESS_INVALID",
            "cash_occupation freshness authority is invalid",
        )
    return {
        "account": normalize_account_label(account),
        "schema_version": SECTION_SCHEMA_VERSIONS[name],
        "source_observed_at_utc": _utc_timestamp(source_observed_at_utc, f"sections.{name}.source_observed_at_utc"),
        "application_received_at_utc": _utc_timestamp(
            application_received_at_utc,
            f"sections.{name}.application_received_at_utc",
        ),
        "content_sha256": sha256_bytes(canonical_json_bytes(projected)),
        "completeness": _completeness(
            completeness_status,
            completeness_reason_codes,
            path=f"sections.{name}.completeness",
        ),
        "freshness": freshness,
        "facts": projected,
    }


def build_source_status_section(*, account: str, owner_receipts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    owners = _mapping(owner_receipts, "source_status owner receipts", set(SOURCE_OWNERS))
    facts = {owner: _owner_receipt(owners[owner], owner) for owner in SOURCE_OWNERS}
    observed = [_parse_utc_timestamp(row["source_observed_at_utc"], "source observation") for row in facts.values()]
    received = [
        _parse_utc_timestamp(row["application_received_at_utc"], "source application receipt") for row in facts.values()
    ]
    worst_status = max(
        (row["completeness"]["status"] for row in facts.values()),
        key={"complete": 0, "partial": 1, "unavailable": 2}.__getitem__,
    )
    reason_codes = _sorted_codes(
        [
            code
            for row in facts.values()
            for code in (
                *row["reason_codes"],
                *row["completeness"]["reason_codes"],
            )
        ]
    )
    section = {
        "account": normalize_account_label(account),
        "schema_version": SECTION_SCHEMA_VERSIONS["source_status"],
        "source_observed_at_utc": _format_utc(min(observed)),
        "application_received_at_utc": _format_utc(max(received)),
        "content_sha256": sha256_bytes(canonical_json_bytes(facts)),
        "completeness": {
            "status": worst_status,
            "reason_codes": reason_codes,
        },
        "freshness": _not_applicable_freshness(),
        "facts": facts,
    }
    return section


def assemble_runtime_portfolio_snapshot(
    *,
    run_id: str,
    account: str,
    account_config_bytes: bytes,
    prepared_option_manifest_bytes: bytes,
    prepared_option_payload_bytes: bytes,
    prepared_portfolio_manifest_bytes: bytes,
    prepared_portfolio_payload_bytes: bytes,
    required_data_manifest_bytes: bytes,
    candidate_manifest_bytes: bytes,
    candidate_status_index_bytes: bytes,
    candidate_owner_snapshot_bytes: Mapping[str, bytes],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Assemble one compact snapshot from already-read current-run owners."""

    run_id_norm = _identity(run_id, "run_id")
    account_norm = normalize_account_label(account)
    option_manifest = _json_object(
        prepared_option_manifest_bytes,
        "prepared option manifest",
    )
    option_context = _json_object(
        prepared_option_payload_bytes,
        "prepared option payload",
    )
    portfolio_manifest = _json_object(
        prepared_portfolio_manifest_bytes,
        "prepared portfolio manifest",
    )
    portfolio_context = _json_object(
        prepared_portfolio_payload_bytes,
        "prepared portfolio payload",
    )
    required_manifest = _json_object(
        required_data_manifest_bytes,
        "required-data manifest",
    )
    candidate_manifest = _json_object(
        candidate_manifest_bytes,
        "candidate manifest",
    )
    if candidate_manifest.get("schema_version") != CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA:
        _fail(
            "REFERENCE_SCHEMA_INVALID",
            "new runtime snapshots require the current candidate manifest",
        )
    if sha256_bytes(prepared_option_payload_bytes) != option_manifest.get("payload_sha256") or sha256_bytes(
        prepared_portfolio_payload_bytes
    ) != portfolio_manifest.get("payload_sha256"):
        _fail("REFERENCE_HASH_INVALID", "prepared owner payload hash mismatch")

    option_authority = _mapping(
        option_context.get("prepared_authority"),
        "prepared option authority",
    )
    for field in (
        "run_id",
        "account",
        "account_config_sha256",
        "ledger_generation_sha256",
        "fx_observation_sha256",
        "source_observed_at",
        "application_received_at_utc",
    ):
        if option_authority.get(field) != option_manifest.get(field):
            _fail(
                "SOURCE_BINDING_INVALID",
                f"prepared option payload authority mismatch: {field}",
            )

    option_schema, option_relpath = _prepared_option_binding(
        str(option_manifest.get("schema_version") or "")
    )
    legacy_decision = option_context.get("decision_state_snapshot")
    decision = legacy_decision if isinstance(legacy_decision, Mapping) else option_context
    current_read = _mapping(decision.get("current_decision_read"), "current decision read")
    decision_fingerprint = _sha256(
        decision.get("decision_state_fingerprint"),
        "decision_state_fingerprint",
    )
    if (
        option_context.get("decision_state_fingerprint") != decision_fingerprint
        or option_manifest.get("decision_state_fingerprint") != decision_fingerprint
    ):
        _fail(
            "SOURCE_BINDING_INVALID",
            "prepared option decision fingerprint mismatch",
        )

    candidate_schema, candidate_relpath = _candidate_manifest_binding(
        str(candidate_manifest.get("schema_version") or "")
    )

    bindings = [
        {
            "role": "account_config",
            "schema_version": _ROLE_SCHEMAS["account_config"],
            "relpath": _ROLE_RELPATHS["account_config"],
            "sha256": sha256_bytes(account_config_bytes),
            "content_sha256": None,
        },
        {
            "role": "candidate_snapshot_manifest",
            "schema_version": candidate_schema,
            "relpath": candidate_relpath,
            "sha256": sha256_bytes(candidate_manifest_bytes),
            "content_sha256": candidate_manifest.get("content_sha256"),
        },
        {
            "role": "prepared_option_positions_context",
            "schema_version": option_schema,
            "relpath": option_relpath,
            "sha256": sha256_bytes(prepared_option_manifest_bytes),
            "content_sha256": option_manifest.get("payload_sha256"),
        },
        {
            "role": "prepared_portfolio_context",
            "schema_version": PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
            "relpath": _ROLE_RELPATHS["prepared_portfolio_context"],
            "sha256": sha256_bytes(prepared_portfolio_manifest_bytes),
            "content_sha256": portfolio_manifest.get("payload_sha256"),
        },
        {
            "role": "required_data_snapshot",
            "schema_version": REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
            "relpath": _ROLE_RELPATHS["required_data_snapshot"],
            "sha256": sha256_bytes(required_data_manifest_bytes),
            "content_sha256": required_manifest.get("content_sha256"),
        },
    ]
    chosen = {
        field: candidate_manifest.get(field)
        for field in (
            "completion_reason",
            "expected_scopes",
            "expected_owners",
            "status_index",
            "owner_snapshots",
        )
    }
    option_reasons = _manifest_reason_codes(option_manifest)
    read_trusted = current_read.get("status") == "trusted"
    reference_payloads = {
        _ROLE_RELPATHS["account_config"]: account_config_bytes,
        candidate_relpath: candidate_manifest_bytes,
        option_relpath: prepared_option_manifest_bytes,
        _ROLE_RELPATHS["prepared_portfolio_context"]: prepared_portfolio_manifest_bytes,
        _ROLE_RELPATHS["required_data_snapshot"]: required_data_manifest_bytes,
        str(candidate_manifest["status_index"]["relpath"]): candidate_status_index_bytes,
    }
    reference_payloads["state/" + str(portfolio_manifest["portfolio_context_relpath"])] = prepared_portfolio_payload_bytes
    for raw in candidate_manifest.get("owner_snapshots") or []:
        owner = str(raw.get("candidate_owner") or "")
        relpath = str(raw.get("relpath") or "")
        payload = candidate_owner_snapshot_bytes.get(owner)
        if not isinstance(payload, bytes):
            _fail(
                "REFERENCE_PAYLOAD_INVALID",
                f"candidate owner payload is missing: {owner}",
            )
        reference_payloads[relpath] = payload

    option_observed = str(option_manifest.get("source_observed_at") or "")
    option_received = str(option_manifest.get("application_received_at_utc") or "")
    portfolio_observed = str(portfolio_manifest.get("source_as_of_utc") or "")
    portfolio_received = str(portfolio_manifest.get("promoted_at_utc") or "")
    sections = {
        "ledger_projection": build_runtime_portfolio_section(
            "ledger_projection",
            account=account_norm,
            source_observed_at_utc=option_observed,
            application_received_at_utc=option_received,
            facts=project_ledger_projection_facts(
                current_decision_read=current_read,
                decision_state_fingerprint=decision_fingerprint,
            ),
            completeness_status=("complete" if option_manifest.get("status") == "ready" and read_trusted else "unavailable"),
            completeness_reason_codes=(
                [] if option_manifest.get("status") == "ready" and read_trusted
                else [*option_reasons, *([] if read_trusted else [str(current_read.get("reason") or f"current_decision:{current_read.get('status')}")])]
            ),
        ),
        "broker_cash": build_runtime_portfolio_section(
            "broker_cash",
            account=account_norm,
            source_observed_at_utc=portfolio_observed,
            application_received_at_utc=portfolio_received,
            facts=project_broker_cash_facts(portfolio_context),
            completeness_status="complete",
        ),
        "broker_positions": build_runtime_portfolio_section(
            "broker_positions",
            account=account_norm,
            source_observed_at_utc=portfolio_observed,
            application_received_at_utc=portfolio_received,
            facts=project_broker_positions_facts(portfolio_context),
            completeness_status=_positions_completeness(portfolio_context)["status"],
            completeness_reason_codes=_positions_completeness(portfolio_context)["reason_codes"],
        ),
        "cash_occupation": build_runtime_portfolio_section(
            "cash_occupation",
            account=account_norm,
            source_observed_at_utc=option_observed,
            application_received_at_utc=option_received,
            facts=project_cash_occupation_facts(option_context),
            completeness_status=("complete" if option_manifest.get("status") == "ready" and read_trusted else "unavailable"),
            completeness_reason_codes=(
                [] if option_manifest.get("status") == "ready" and read_trusted else option_reasons
            ),
            freshness_authority=("prepared_option_positions_context.fx_status"),
            freshness_status=str(option_manifest.get("fx_status") or ""),
            freshness_reason_codes=_option_freshness(option_manifest)["reason_codes"],
        ),
    }
    snapshot_status = str(
        decision.get("snapshot_status")
        or option_context.get("decision_snapshot_status")
        or ""
    )
    shadow = _mapping(
        option_context.get("current_decision_shadow"),
        "prepared option current decision shadow",
    )
    if legacy_decision is not None and shadow != decision.get("current_decision_shadow"):
        _fail(
            "SOURCE_BINDING_INVALID",
            "prepared option current decision shadow mismatch",
        )
    shadow_status = str(shadow.get("status") or "")
    ledger_shadow_status = (
        "matched"
        if snapshot_status == "trusted"
        and (
            decision.get("actionable") is True
            or option_context.get("decision_snapshot_actionable") is True
        )
        and shadow_status == "matched"
        else "mismatched"
        if shadow_status == "mismatch"
        else "unavailable"
    )
    ledger_shadow = {
        "schema_version": LEDGER_SHADOW_SCHEMA_VERSION,
        "status": ledger_shadow_status,
    }
    if ledger_shadow_status != "matched":
        ledger_completeness = sections["ledger_projection"]["completeness"]
        sections["ledger_projection"]["completeness"] = {
            "status": "unavailable",
            "reason_codes": _sorted_codes([
                *ledger_completeness["reason_codes"],
                f"ledger_shadow:{ledger_shadow_status}",
            ]),
        }

    by_role = {row["role"]: row for row in bindings}
    required_ready = [
        str(row.get("source_observed_at") or "")
        for row in required_manifest.get("symbols", {}).values()
        if row.get("status") == "ready"
    ]
    required_received = str(required_manifest.get("sealed_at_utc") or "")
    required_observed = max(
        required_ready,
        key=lambda value: _parse_utc_timestamp(value, value),
        default=required_received,
    )
    required_status = str(required_manifest.get("status") or "")
    required_completeness = {
        "complete": "complete",
        "partial": "partial",
        "failed": "unavailable",
    }.get(required_status, "unavailable")
    required_reasons = sorted(
        {
            *(
                str(row["reason"])
                for row in required_manifest.get("symbols", {}).values()
                if row.get("status") == "failed"
            ),
            *([] if required_ready else ["source_observation_unavailable"]),
        }
    )
    option_completeness = dict(sections["ledger_projection"]["completeness"])
    receipts = {
        "broker_portfolio": _receipt_from_binding(
            by_role["prepared_portfolio_context"],
            status=str(portfolio_manifest.get("status") or ""),
            reason_codes=_manifest_reason_codes(portfolio_manifest),
            observed=portfolio_observed,
            received=portfolio_received,
            completeness={"status": "complete", "reason_codes": []},
        ),
        "candidate_results": _receipt_from_binding(
            by_role["candidate_snapshot_manifest"],
            status=str(candidate_manifest.get("completion_reason") or ""),
            reason_codes=[],
            observed=str(candidate_manifest.get("sealed_at_utc") or ""),
            received=str(candidate_manifest.get("sealed_at_utc") or ""),
            completeness={"status": "complete", "reason_codes": []},
        ),
        "ledger_projection": _receipt_from_binding(
            by_role["prepared_option_positions_context"],
            status=str(option_manifest.get("status") or ""),
            reason_codes=_manifest_reason_codes(option_manifest),
            observed=option_observed,
            received=option_received,
            completeness=option_completeness,
        ),
        "required_data": _receipt_from_binding(
            by_role["required_data_snapshot"],
            status=required_status,
            reason_codes=required_reasons,
            observed=required_observed,
            received=required_received,
            completeness={
                "status": required_completeness,
                "reason_codes": ([] if required_completeness == "complete" else required_reasons),
            },
        ),
    }
    sections["source_status"] = build_source_status_section(
        account=account_norm,
        owner_receipts=receipts,
    )
    snapshot = build_runtime_portfolio_snapshot(
        run_id=run_id_norm,
        account=account_norm,
        sections=sections,
        replay_bindings=bindings,
        chosen_results=chosen,
        ledger_shadow=ledger_shadow,
        reference_payloads=reference_payloads,
    )
    return snapshot, reference_payloads


def _receipt_from_binding(
    binding: Mapping[str, Any],
    *,
    status: str,
    reason_codes: Sequence[str],
    observed: str,
    received: str,
    completeness: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "owner_schema_version": binding["schema_version"],
        "owner_status": status,
        "reason_codes": sorted(set(reason_codes)),
        "manifest_sha256": binding["sha256"],
        "content_sha256": binding["content_sha256"],
        "source_observed_at_utc": observed,
        "application_received_at_utc": received,
        "completeness": dict(completeness),
        "freshness": _not_applicable_freshness(),
    }


def build_runtime_portfolio_snapshot(
    *,
    run_id: str,
    account: str,
    sections: Mapping[str, Mapping[str, Any]],
    replay_bindings: Sequence[Mapping[str, Any]],
    chosen_results: Mapping[str, Any],
    ledger_shadow: Mapping[str, Any],
    reference_payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    """Build and seal one run/account snapshot from bounded supplied inputs."""

    return _build_runtime_portfolio_snapshot(
        run_id=run_id,
        account=account,
        sections=sections,
        replay_bindings=replay_bindings,
        chosen_results=chosen_results,
        receipt=_ledger_shadow(ledger_shadow),
        receipt_key="ledger_shadow",
        schema_version=SCHEMA_VERSION,
        reference_payloads=reference_payloads,
    )


def _build_runtime_portfolio_snapshot(
    *,
    run_id: str,
    account: str,
    sections: Mapping[str, Mapping[str, Any]],
    replay_bindings: Sequence[Mapping[str, Any]],
    chosen_results: Mapping[str, Any],
    receipt: Mapping[str, Any],
    receipt_key: str,
    schema_version: str,
    reference_payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    """Shared canonical validation for new v2 writes and historical v1 reads."""

    run_id_norm = _identity(run_id, "run_id")
    account_norm = normalize_account_label(account)
    normalized_sections = _sections(sections, account_norm)
    bindings = _replay_bindings(replay_bindings)
    chosen = _chosen_results(chosen_results)
    owner_payloads = validate_replay_bundle(
        expected_run_id=run_id_norm,
        expected_account=account_norm,
        replay_bindings=bindings,
        chosen_results=chosen,
        reference_payloads=reference_payloads,
        require_portfolio_payload=normalized_sections["broker_cash"]["schema_version"] != BROKER_CASH_SCHEMA_V1,
    )
    _validate_source_bindings(
        normalized_sections,
        bindings,
        owner_payloads,
        chosen,
        receipt,
        legacy=schema_version == LEGACY_SCHEMA_VERSION,
    )
    body = {
        "schema_version": schema_version,
        "run_id": run_id_norm,
        "account": account_norm,
        "status": "trusted",
        "reason_codes": [],
        "sections": normalized_sections,
        "observed_time_range": _observed_time_range(normalized_sections),
        "replay_bindings": bindings,
        "chosen_results": chosen,
        receipt_key: dict(receipt),
    }
    body["reason_codes"] = _snapshot_reason_codes(body)
    body["status"] = "trusted" if not body["reason_codes"] else "data_unavailable"
    snapshot = {
        **body,
        "seal": {
            "algorithm": "sha256",
            "canonicalization": CANONICALIZATION,
            "content_sha256": sha256_bytes(canonical_json_bytes(body)),
        },
    }
    _enforce_size(snapshot)
    return snapshot


def verify_runtime_portfolio_snapshot(
    snapshot: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
    reference_payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    """Verify new v2 snapshots or immutable historical v1 snapshots."""

    payload = _mapping(snapshot, "runtime portfolio snapshot")
    schema = payload.get("schema_version")
    if schema not in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION}:
        _fail("SCHEMA_INVALID", "runtime portfolio snapshot schema is invalid")
    _keys(payload, _TOP_LEVEL_KEYS if schema == SCHEMA_VERSION else _LEGACY_TOP_LEVEL_KEYS, "runtime portfolio snapshot")
    run_id = _identity(payload.get("run_id"), "run_id")
    account = normalize_account_label(payload.get("account"))
    if run_id != _identity(expected_run_id, "expected_run_id"):
        _fail("RUN_MISMATCH", "runtime portfolio snapshot run_id mismatch")
    if account != normalize_account_label(expected_account):
        _fail("ACCOUNT_MISMATCH", "runtime portfolio snapshot account mismatch")
    common = {
        "run_id": run_id,
        "account": account,
        "sections": payload.get("sections"),
        "replay_bindings": payload.get("replay_bindings"),
        "chosen_results": payload.get("chosen_results"),
        "reference_payloads": reference_payloads,
    }
    if schema == SCHEMA_VERSION:
        rebuilt = build_runtime_portfolio_snapshot(**common, ledger_shadow=payload.get("ledger_shadow"))
    else:
        rebuilt = _build_runtime_portfolio_snapshot(
            **common,
            receipt=_legacy_comparison(payload.get("legacy_comparison")),
            receipt_key="legacy_comparison",
            schema_version=LEGACY_SCHEMA_VERSION,
        )
    if payload != rebuilt:
        _fail(
            "VERIFICATION_FAILED",
            "runtime portfolio snapshot does not match its canonical rebuilt form",
        )
    return rebuilt


def publish_runtime_portfolio_snapshot(
    *,
    base: Path,
    snapshot: Mapping[str, Any],
    reference_payloads: Mapping[str, bytes],
) -> Path:
    """Publish once or adopt identical bytes, then verify the readback."""

    payload = _mapping(snapshot, "runtime portfolio snapshot")
    verified = verify_runtime_portfolio_snapshot(
        payload,
        expected_run_id=payload.get("run_id"),
        expected_account=payload.get("account"),
        reference_payloads=reference_payloads,
    )
    if verified["schema_version"] != SCHEMA_VERSION:
        _fail("SCHEMA_INVALID", "historical v1 snapshots are read-only")
    encoded = canonical_json_bytes(verified)
    path = write_account_run_state_bytes_once_safely(
        base=base,
        run_id=verified["run_id"],
        account=verified["account"],
        name=ARTIFACT_NAME,
        payload=encoded,
    )
    load_runtime_portfolio_snapshot(
        base=base,
        run_id=verified["run_id"],
        account=verified["account"],
        reference_payloads=reference_payloads,
    )
    return path


def load_runtime_portfolio_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
    reference_payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    """Load v2 when present, or a canonical historical v1 artifact."""

    try:
        raw = read_account_run_state_bytes_safely(
            base=base,
            run_id=run_id,
            account=account,
            name=ARTIFACT_NAME,
        )
    except AccountRunConfigError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
        raw = read_account_run_state_bytes_safely(
            base=base,
            run_id=run_id,
            account=account,
            name=LEGACY_ARTIFACT_NAME,
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_DECODE_FAILED",
            "runtime portfolio snapshot is not valid UTF-8 JSON",
        ) from exc
    verified = verify_runtime_portfolio_snapshot(
        decoded,
        expected_run_id=run_id,
        expected_account=account,
        reference_payloads=reference_payloads,
    )
    if raw != canonical_json_bytes(verified):
        _fail("CANONICAL_BYTES_INVALID", "runtime portfolio snapshot bytes are not canonical")
    return verified


def _sections(value: Any, account: str) -> dict[str, dict[str, Any]]:
    rows = _mapping(value, "sections", set(SECTION_NAMES))
    sections = {name: _section(rows[name], name, account) for name in SECTION_NAMES}
    for field in ("source_observed_at_utc", "application_received_at_utc"):
        if sections["ledger_projection"][field] != sections["cash_occupation"][field]:
            _fail("SECTION_TIME_INVALID", f"prepared option {field} receipts differ")
        if sections["broker_cash"][field] != sections["broker_positions"][field]:
            _fail("SECTION_TIME_INVALID", f"prepared portfolio {field} receipts differ")
    return sections


def _section(value: Any, name: str, account: str) -> dict[str, Any]:
    row = _mapping(value, f"sections.{name}", _SECTION_KEYS)
    if normalize_account_label(row.get("account")) != account:
        _fail("SECTION_ACCOUNT_MISMATCH", f"{name} account mismatch")
    version = row.get("schema_version")
    if version != SECTION_SCHEMA_VERSIONS[name] and not (name == "broker_cash" and version == BROKER_CASH_SCHEMA_V1):
        _fail("SECTION_SCHEMA_INVALID", f"{name} schema is invalid")
    facts = (
        _source_status_facts(row.get("facts"))
        if name == "source_status"
        else _validated_section_facts(name, row.get("facts"), version=version)
    )
    if name == "ledger_projection":
        _mapping(
            facts["current_decision"],
            "sections.ledger_projection.facts.current_decision",
            set(CURRENT_DECISION_READ_KEYS),
        )
        _sha256(facts["decision_state_fingerprint"], "decision_state_fingerprint")
        _validate_current_decision_truth({"facts": facts}, expected_account=account)
    observed = _utc_timestamp(row.get("source_observed_at_utc"), f"sections.{name}.source_observed_at_utc")
    received = _utc_timestamp(
        row.get("application_received_at_utc"),
        f"sections.{name}.application_received_at_utc",
    )
    if _parse_utc_timestamp(received, "application receipt") < _parse_utc_timestamp(observed, "source observation"):
        _fail("TIMESTAMP_INVALID", f"{name} receive time precedes observation")
    completeness = _completeness(row.get("completeness"), path=f"sections.{name}")
    freshness = _freshness(row.get("freshness"), path=f"sections.{name}")
    expected_freshness = _not_applicable_freshness()
    if name == "cash_occupation":
        if freshness["authority"] != "prepared_option_positions_context.fx_status":
            _fail("FRESHNESS_INVALID", "cash_occupation freshness authority is invalid")
    elif freshness != expected_freshness:
        _fail("FRESHNESS_INVALID", f"{name} freshness must be not_applicable")
    if name == "source_status":
        expected = build_source_status_section(account=account, owner_receipts=facts)
        for key in (
            "source_observed_at_utc",
            "application_received_at_utc",
            "completeness",
            "freshness",
        ):
            if row.get(key) != expected[key]:
                _fail("SOURCE_STATUS_INVALID", f"source_status {key} mismatch")
    expected_hash = sha256_bytes(canonical_json_bytes(facts))
    if _sha256(row.get("content_sha256"), f"sections.{name}.content_sha256") != expected_hash:
        _fail("SECTION_HASH_INVALID", f"{name} content hash mismatch")
    return {
        "account": account,
        "schema_version": version,
        "source_observed_at_utc": observed,
        "application_received_at_utc": received,
        "content_sha256": expected_hash,
        "completeness": completeness,
        "freshness": freshness,
        "facts": facts,
    }


def _validated_section_facts(name: str, value: Any, *, version: str | None = None) -> dict[str, Any]:
    keys = BROKER_CASH_V1_FACT_KEYS if name == "broker_cash" and version == BROKER_CASH_SCHEMA_V1 else SECTION_FACT_KEYS.get(name)
    if keys is None:
        _fail("SECTION_INVALID", f"unsupported section {name}")
    facts = _mapping(value, f"sections.{name}.facts", set(keys))
    canonical_json_bytes(facts)
    return {key: facts[key] for key in keys}


def _source_status_facts(value: Any) -> dict[str, Any]:
    facts = _mapping(value, "sections.source_status.facts", set(SOURCE_OWNERS))
    return {owner: _owner_receipt(facts[owner], owner) for owner in SOURCE_OWNERS}


def _owner_receipt(value: Any, owner: str) -> dict[str, Any]:
    row = _mapping(value, f"source_status.{owner}", _OWNER_RECEIPT_KEYS)
    completeness = _completeness(row.get("completeness"), path=f"source_status.{owner}")
    content_hash = row.get("content_sha256")
    if content_hash is not None:
        content_hash = _sha256(content_hash, f"source_status.{owner}.content_sha256")
    elif completeness["status"] != "unavailable":
        _fail("OWNER_HASH_INVALID", f"{owner} content hash may be null only when unavailable")
    observed = _utc_timestamp(
        row.get("source_observed_at_utc"),
        f"source_status.{owner}.source_observed_at_utc",
    )
    received = _utc_timestamp(
        row.get("application_received_at_utc"),
        f"source_status.{owner}.application_received_at_utc",
    )
    if _parse_utc_timestamp(received, owner) < _parse_utc_timestamp(observed, owner):
        _fail("TIMESTAMP_INVALID", f"{owner} receive time precedes observation")
    return {
        "owner_schema_version": _text(row.get("owner_schema_version"), f"source_status.{owner}.owner_schema_version"),
        "owner_status": _text(row.get("owner_status"), f"source_status.{owner}.owner_status"),
        "reason_codes": _sorted_codes(row.get("reason_codes")),
        "manifest_sha256": _sha256(row.get("manifest_sha256"), f"source_status.{owner}.manifest_sha256"),
        "content_sha256": content_hash,
        "source_observed_at_utc": observed,
        "application_received_at_utc": received,
        "completeness": completeness,
        "freshness": _freshness(row.get("freshness"), path=f"source_status.{owner}"),
    }


def _replay_bindings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("BINDING_INVALID", "replay_bindings must be a sequence")
    rows = []
    for index, raw in enumerate(value):
        row = _mapping(raw, f"replay_bindings[{index}]", _BINDING_KEYS)
        rows.append({"role": _text(row.get("role"), "binding role"), **_reference(row)})
    rows.sort(key=lambda row: row["role"])
    if [row["role"] for row in rows] != list(REPLAY_BINDING_ROLES):
        _fail("BINDING_INVALID", "replay bindings must contain sorted required roles")
    return rows


def _chosen_results(value: Any) -> dict[str, Any]:
    row = _mapping(value, "chosen_results", _CHOSEN_KEYS)
    status_index = _reference(_mapping(row.get("status_index"), "chosen_results.status_index"))
    scopes = _scopes(row.get("expected_scopes"), "expected_scopes")
    snapshots_raw = row.get("owner_snapshots")
    if not isinstance(snapshots_raw, Sequence) or isinstance(snapshots_raw, (str, bytes, bytearray)):
        _fail("CHOSEN_RESULTS_INVALID", "owner_snapshots must be a sequence")
    snapshots = []
    for index, raw in enumerate(snapshots_raw):
        item = _mapping(raw, f"owner_snapshots[{index}]")
        if frozenset(item) not in {
            frozenset(_OWNER_SNAPSHOT_KEYS),
            frozenset(_OWNER_SNAPSHOT_KEYS | _OWNER_SNAPSHOT_MODE_KEYS),
        }:
            _fail(
                "FIELD_INVALID",
                f"owner_snapshots[{index}] fields do not match schema",
            )
        mode_fields: dict[str, Any] = {}
        if "scan_mode" in item:
            scan_mode = _text(item.get("scan_mode"), "owner scan_mode").lower()
            executable = item.get("executable")
            if scan_mode not in {"standard", "experience"} or type(executable) is not bool:
                _fail("CHOSEN_RESULTS_INVALID", "owner run mode is invalid")
            mode_fields = {"scan_mode": scan_mode, "executable": executable}
        snapshots.append(
            {
                "candidate_owner": _text(item.get("candidate_owner"), "candidate_owner").lower(),
                **_reference(item),
                "opening_status": _text(item.get("opening_status"), "opening_status"),
                "covered_scopes": _scopes(item.get("covered_scopes"), "covered_scopes"),
                **mode_fields,
            }
        )
    snapshots.sort(key=lambda item: item["candidate_owner"])
    owners = _sorted_texts(row.get("expected_owners"), "expected_owners")
    if owners != sorted({scope["candidate_owner"] for scope in scopes}):
        _fail("CHOSEN_RESULTS_INVALID", "expected owners do not match scopes")
    if [item["candidate_owner"] for item in snapshots] != owners:
        _fail("CHOSEN_RESULTS_INVALID", "owner snapshots must match sorted expected owners")
    for item in snapshots:
        expected = [scope for scope in scopes if scope["candidate_owner"] == item["candidate_owner"]]
        if item["covered_scopes"] != expected:
            _fail("CHOSEN_RESULTS_INVALID", "owner covered scopes mismatch")
    completion = _text(row.get("completion_reason"), "completion_reason")
    if completion != ("complete" if scopes else "no_applicable_scope"):
        _fail("CHOSEN_RESULTS_INVALID", "completion reason does not match scopes")
    chosen = {
        "completion_reason": completion,
        "expected_scopes": scopes,
        "expected_owners": owners,
        "status_index": status_index,
        "owner_snapshots": snapshots,
    }
    return chosen


def _reference(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(value, "reference")
    if "role" not in row and "candidate_owner" not in row:
        _keys(row, _REFERENCE_KEYS, "reference")
    content_hash = row.get("content_sha256")
    return {
        "schema_version": _text(row.get("schema_version"), "reference.schema_version"),
        "relpath": _relpath(row.get("relpath")),
        "sha256": _sha256(row.get("sha256"), "reference.sha256"),
        "content_sha256": None if content_hash is None else _sha256(content_hash, "reference.content_sha256"),
    }


def validate_replay_bundle(
    *,
    expected_run_id: str,
    expected_account: str,
    replay_bindings: Sequence[Mapping[str, Any]],
    chosen_results: Mapping[str, Any],
    reference_payloads: Mapping[str, bytes],
    require_portfolio_payload: bool = False,
) -> dict[str, dict[str, Any]]:
    bindings = _replay_bindings(replay_bindings)
    chosen = _chosen_results(chosen_results)
    supplied = _mapping(reference_payloads, "reference_payloads")
    references = [*bindings, chosen["status_index"], *chosen["owner_snapshots"]]
    rooted_paths = [
        (
            "run" if row.get("role") == "required_data_snapshot" else "account",
            row["relpath"],
        )
        for row in references
    ]
    if len(rooted_paths) != len(set(rooted_paths)):
        _fail("REFERENCE_PATH_INVALID", "replay references repeat a resolved path")
    expected: dict[str, str] = {}
    for row in references:
        relpath = row["relpath"]
        digest = row["sha256"]
        if relpath in expected and expected[relpath] != digest:
            _fail("REFERENCE_HASH_INVALID", f"conflicting hashes for {relpath}")
        expected[relpath] = digest
    portfolio_payload_path = None
    if require_portfolio_payload:
        manifest_path = _ROLE_RELPATHS["prepared_portfolio_context"]
        manifest = _json_object(supplied.get(manifest_path, b""), "prepared portfolio manifest")
        if manifest.get("status") == "ready":
            name = _relpath(manifest.get("portfolio_context_relpath"))
            if "/" in name:
                _fail("REFERENCE_PATH_INVALID", "prepared portfolio payload must be a state file")
            portfolio_payload_path = "state/" + name
            expected[portfolio_payload_path] = _sha256(manifest.get("payload_sha256"), "portfolio payload hash")
    _keys(supplied, set(expected), "reference_payloads")
    for relpath, digest in expected.items():
        raw = supplied[relpath]
        if not isinstance(raw, bytes) or sha256_bytes(raw) != digest:
            _fail("REFERENCE_HASH_INVALID", f"reference payload hash mismatch for {relpath}")

    by_role = {row["role"]: row for row in bindings}
    for role, binding in by_role.items():
        if role == "prepared_option_positions_context":
            expected_schema, expected_relpath = _prepared_option_binding(
                binding["schema_version"]
            )
            if binding["schema_version"] != expected_schema:
                _fail("REFERENCE_SCHEMA_INVALID", f"{role} reference schema mismatch")
            if binding["relpath"] != expected_relpath:
                _fail("REFERENCE_PATH_INVALID", f"{role} reference path mismatch")
            continue
        if role == "candidate_snapshot_manifest":
            _, expected_relpath = _candidate_manifest_binding(binding["schema_version"])
            if binding["relpath"] != expected_relpath:
                _fail("REFERENCE_PATH_INVALID", f"{role} reference path mismatch")
            continue
        if binding["schema_version"] != _ROLE_SCHEMAS[role]:
            _fail("REFERENCE_SCHEMA_INVALID", f"{role} reference schema mismatch")
        if binding["relpath"] != _ROLE_RELPATHS[role]:
            _fail("REFERENCE_PATH_INVALID", f"{role} reference path mismatch")
    decoded = {role: _json_object(supplied[binding["relpath"]], role) for role, binding in by_role.items()}
    config_hash = by_role["account_config"]["sha256"]
    _validate_account_config_reference(
        decoded["account_config"],
        binding=by_role["account_config"],
        expected_account=expected_account,
    )
    _validate_prepared_option_reference(
        decoded["prepared_option_positions_context"],
        binding=by_role["prepared_option_positions_context"],
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=config_hash,
    )
    _validate_prepared_portfolio_reference(
        decoded["prepared_portfolio_context"],
        binding=by_role["prepared_portfolio_context"],
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=config_hash,
    )
    _validate_required_data_reference(
        decoded["required_data_snapshot"],
        binding=by_role["required_data_snapshot"],
        expected_run_id=expected_run_id,
    )
    _validate_candidate_reference(
        decoded["candidate_snapshot_manifest"],
        binding=by_role["candidate_snapshot_manifest"],
        chosen=chosen,
        supplied=supplied,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=config_hash,
        expected_required_data_sha256=by_role["required_data_snapshot"]["sha256"],
    )
    if portfolio_payload_path is not None:
        decoded["broker_portfolio_payload"] = _json_object(supplied[portfolio_payload_path], "portfolio payload")
    return decoded


def _validate_source_bindings(
    sections: Mapping[str, Mapping[str, Any]],
    bindings: Sequence[Mapping[str, Any]],
    owner_payloads: Mapping[str, Mapping[str, Any]],
    chosen: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    legacy: bool,
) -> None:
    by_role = {row["role"]: row for row in bindings}
    owners = sections["source_status"]["facts"]
    if sections["source_status"] != build_source_status_section(
        account=sections["source_status"]["account"],
        owner_receipts=owners,
    ):
        _fail("SOURCE_STATUS_INVALID", "source status aggregate contradicts its owners")
    for owner, role in _OWNER_BINDING_ROLES.items():
        owner_receipt = owners[owner]
        binding = by_role[role]
        if (
            owner_receipt["owner_schema_version"] != binding["schema_version"]
            or owner_receipt["manifest_sha256"] != binding["sha256"]
            or owner_receipt["content_sha256"] != binding["content_sha256"]
        ):
            _fail("SOURCE_BINDING_INVALID", f"{owner} replay binding mismatch")

    option_manifest = owner_payloads["prepared_option_positions_context"]
    portfolio_manifest = owner_payloads["prepared_portfolio_context"]
    required_manifest = owner_payloads["required_data_snapshot"]
    candidate_manifest = owner_payloads["candidate_snapshot_manifest"]

    ledger = sections["ledger_projection"]
    occupation = sections["cash_occupation"]
    if ledger["freshness"] != _not_applicable_freshness():
        _fail("SOURCE_BINDING_INVALID", "ledger freshness must be not_applicable")
    _validate_current_decision_truth(ledger, expected_account=ledger["account"])
    option_status = option_manifest["status"]
    ledger_shadow_failed = (
        next(row for row in receipt["sections"] if row["section"] == "ledger_projection")["mismatch_count"] != 0
        if legacy else receipt["status"] != "matched"
    )
    shadow_reason = f"{'legacy_comparison' if legacy else 'ledger_shadow'}:{receipt['status']}"
    ledger_status = (
        "complete"
        if option_status == "ready"
        and ledger["facts"]["current_decision"]["status"] == "trusted"
        and not ledger_shadow_failed
        else "unavailable"
    )
    option_status_expected = (
        "complete"
        if option_status == "ready" and ledger["facts"]["current_decision"]["status"] == "trusted"
        else "unavailable"
    )
    option_reasons = _manifest_reason_codes(option_manifest)
    ledger_reasons = list(option_reasons)
    current = ledger["facts"]["current_decision"]
    if current["status"] != "trusted":
        ledger_reasons.append(str(current.get("reason") or f"current_decision:{current['status']}"))
    if ledger_shadow_failed:
        ledger_reasons.append(shadow_reason)
    ledger_completeness = _completeness(
        ledger_status,
        ledger_reasons if ledger_status != "complete" else [],
        path="ledger_projection",
    )
    occupation_completeness = _completeness(
        option_status_expected,
        option_reasons if option_status_expected != "complete" else [],
        path="cash_occupation",
    )
    _require_completeness(ledger, ledger_completeness, "ledger_projection")
    _require_completeness(occupation, occupation_completeness, "cash_occupation")
    option_observed = _utc_timestamp(
        option_manifest.get("source_observed_at"),
        "prepared option source_observed_at",
    )
    option_received = _utc_timestamp(
        option_manifest.get("application_received_at_utc"),
        "prepared option application_received_at_utc",
    )
    for name in ("ledger_projection", "cash_occupation"):
        _require_section_times(sections[name], option_observed, option_received, name)
    if option_manifest.get("decision_state_fingerprint") != ledger["facts"]["decision_state_fingerprint"]:
        _fail("SOURCE_BINDING_INVALID", "prepared option decision fingerprint mismatch")
    option_freshness = _option_freshness(option_manifest)
    if occupation["freshness"] != option_freshness:
        _fail("SOURCE_BINDING_INVALID", "cash occupation freshness mismatch")
    option_receipt = owners["ledger_projection"]
    _require_owner_receipt(
        option_receipt,
        owner_schema_version=str(option_manifest["schema_version"]),
        owner_status=option_status,
        reason_codes=_manifest_reason_codes(option_manifest),
        observed=option_observed,
        received=option_received,
        completeness=ledger_completeness,
        freshness=_not_applicable_freshness(),
        owner="ledger_projection",
    )

    portfolio_status = portfolio_manifest["status"]
    portfolio_status_expected = "complete" if portfolio_status == "ready" else "unavailable"
    portfolio_reasons = _manifest_reason_codes(portfolio_manifest)
    portfolio_completeness = _completeness(
        portfolio_status_expected,
        portfolio_reasons if portfolio_status_expected != "complete" else [],
        path="broker_portfolio",
    )
    portfolio_observed = _utc_timestamp(
        portfolio_manifest.get("source_as_of_utc", portfolio_manifest.get("promoted_at_utc")),
        "prepared portfolio source_as_of_utc",
    )
    portfolio_received = _utc_timestamp(
        portfolio_manifest.get("promoted_at_utc"),
        "prepared portfolio promoted_at_utc",
    )
    current_cash_schema = sections["broker_cash"]["schema_version"] != BROKER_CASH_SCHEMA_V1
    portfolio_payload = owner_payloads.get("broker_portfolio_payload")
    if current_cash_schema and portfolio_status == "ready":
        if not isinstance(portfolio_payload, Mapping):
            _fail("SOURCE_BINDING_INVALID", "prepared portfolio payload missing")
        if sections["broker_cash"]["facts"] != project_broker_cash_facts(portfolio_payload) or sections["broker_positions"]["facts"] != project_broker_positions_facts(portfolio_payload):
            _fail("SOURCE_BINDING_INVALID", "broker projection differs from original payload")
    for name in ("broker_cash", "broker_positions"):
        expected_completeness = (_positions_completeness(portfolio_payload or {})
                                 if current_cash_schema and name == "broker_positions" else portfolio_completeness)
        _require_completeness(sections[name], expected_completeness, name)
        if sections[name]["freshness"] != _not_applicable_freshness():
            _fail("SOURCE_BINDING_INVALID", f"{name} freshness must be not_applicable")
        _require_section_times(sections[name], portfolio_observed, portfolio_received, name)
    _require_owner_receipt(
        owners["broker_portfolio"],
        owner_schema_version=PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
        owner_status=portfolio_status,
        reason_codes=_manifest_reason_codes(portfolio_manifest),
        observed=portfolio_observed,
        received=portfolio_received,
        completeness=portfolio_completeness,
        freshness=_not_applicable_freshness(),
        owner="broker_portfolio",
    )

    required_status = required_manifest["status"]
    required_expected = {
        "complete": "complete",
        "partial": "partial",
        "failed": "unavailable",
    }[required_status]
    ready_observations = [
        _utc_timestamp(row.get("source_observed_at"), "required symbol observation")
        for row in required_manifest["symbols"].values()
        if row.get("status") == "ready"
    ]
    required_received = _utc_timestamp(required_manifest.get("sealed_at_utc"), "required data sealed_at_utc")
    required_observed = (
        max(ready_observations, key=lambda value: _parse_utc_timestamp(value, value))
        if ready_observations
        else required_received
    )
    required_reasons = sorted(
        {
            *(str(row["reason"]) for row in required_manifest["symbols"].values() if row.get("status") == "failed"),
            *([] if ready_observations else ["source_observation_unavailable"]),
        }
    )
    required_completeness = _completeness(
        required_expected,
        required_reasons if required_expected != "complete" else [],
        path="required_data",
    )
    _require_owner_receipt(
        owners["required_data"],
        owner_schema_version=REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
        owner_status=required_status,
        reason_codes=required_reasons,
        observed=required_observed,
        received=required_received,
        completeness=required_completeness,
        freshness=_not_applicable_freshness(),
        owner="required_data",
    )

    candidate_status = candidate_manifest["completion_reason"]
    candidate_time = _utc_timestamp(candidate_manifest.get("sealed_at_utc"), "candidate sealed_at_utc")
    if candidate_status != chosen["completion_reason"]:
        _fail("SOURCE_BINDING_INVALID", "candidate terminal status mismatch")
    _require_owner_receipt(
        owners["candidate_results"],
        owner_schema_version=by_role["candidate_snapshot_manifest"]["schema_version"],
        owner_status=candidate_status,
        reason_codes=[],
        observed=candidate_time,
        received=candidate_time,
        completeness={"status": "complete", "reason_codes": []},
        freshness=_not_applicable_freshness(),
        owner="candidate_results",
    )


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_REFERENCE_PAYLOAD_INVALID",
            f"{label} reference is not valid UTF-8 JSON",
        ) from exc
    return _mapping(value, f"{label} reference")


def _validate_account_config_reference(
    payload: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    expected_account: str,
) -> None:
    portfolio = payload.get("portfolio")
    if not isinstance(portfolio, Mapping) or normalize_account_label(portfolio.get("account")) != expected_account:
        _fail("REFERENCE_ACCOUNT_MISMATCH", "account config account mismatch")
    if binding["content_sha256"] is not None:
        _fail("REFERENCE_HASH_INVALID", "account config logical hash must be null")


def _validate_prepared_option_reference(
    payload: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    expected_run_id: str,
    expected_account: str,
    expected_account_config_sha256: str,
) -> None:
    schema = str(payload.get("schema_version") or "")
    expected_schema, expected_relpath = _prepared_option_binding(schema)
    if binding.get("schema_version") != expected_schema or binding.get(
        "relpath"
    ) != expected_relpath:
        _fail(
            "REFERENCE_SCHEMA_INVALID",
            "prepared option reference schema mismatch",
        )
    status = _one_of(payload.get("status"), {"ready", "unavailable"}, "prepared option status")
    common = {
        "schema_version",
        "run_id",
        "account",
        "status",
        "account_config_sha256",
        "source_observed_at",
        "application_received_at_utc",
        "fx_status",
        "fx_observation_sha256",
    }
    specific = (
        {
            "payload_relpath",
            "payload_sha256",
            "ledger_generation_sha256",
            "decision_state_fingerprint",
        }
        if status == "ready"
        else {"reason"}
    )
    optional = {"fx_error_type"} if "fx_error_type" in payload else set()
    _keys(payload, common | specific | optional, "prepared option manifest")
    _require_manifest_identity(
        payload,
        schema=str(binding["schema_version"]),
        run_id=expected_run_id,
        account=expected_account,
        config_hash=expected_account_config_sha256,
        label="prepared option",
    )
    observed = _utc_timestamp(payload.get("source_observed_at"), "prepared option observed")
    received = _utc_timestamp(payload.get("application_received_at_utc"), "prepared option received")
    if _parse_utc_timestamp(received, received) < _parse_utc_timestamp(observed, observed):
        _fail("REFERENCE_PAYLOAD_INVALID", "prepared option receive time precedes observation")
    _one_of(
        payload.get("fx_status"),
        {"ready", "unavailable_stale", "unavailable"},
        "prepared option fx_status",
    )
    _sha256(payload.get("fx_observation_sha256"), "prepared option fx hash")
    if "fx_error_type" in payload:
        _text(payload.get("fx_error_type"), "prepared option fx_error_type")
    if status == "ready":
        if payload.get("payload_relpath") != PREPARED_OPTION_POSITIONS_PAYLOAD_NAME:
            _fail("REFERENCE_PATH_INVALID", "prepared option payload path mismatch")
        for field in (
            "payload_sha256",
            "ledger_generation_sha256",
            "decision_state_fingerprint",
        ):
            _sha256(payload.get(field), f"prepared option {field}")
        if binding["content_sha256"] != payload["payload_sha256"]:
            _fail("REFERENCE_HASH_INVALID", "prepared option logical hash mismatch")
    else:
        _text(payload.get("reason"), "prepared option reason")
        if binding["content_sha256"] is not None:
            _fail("REFERENCE_HASH_INVALID", "unavailable prepared option has content hash")


def _validate_prepared_portfolio_reference(
    payload: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    expected_run_id: str,
    expected_account: str,
    expected_account_config_sha256: str,
) -> None:
    status = _one_of(payload.get("status"), {"ready", "unavailable"}, "prepared portfolio status")
    common = {
        "schema_version",
        "run_id",
        "account",
        "status",
        "preparation_started_at_utc",
        "deadline_at_utc",
        "child_finished_at_utc",
        "promoted_at_utc",
        "prepared_at_utc",
        "deadline_seconds",
        "worker_returncode",
        "account_config_sha256",
    }
    specific = (
        {
            "portfolio_context_relpath",
            "payload_sha256",
            "portfolio_source_name",
            "portfolio_source_account",
            "source_as_of_utc",
        }
        if status == "ready"
        else {"reason"}
    )
    optional = {field for field in ("error_type", "error_code") if field in payload}
    _keys(payload, common | specific | optional, "prepared portfolio manifest")
    _require_manifest_identity(
        payload,
        schema=PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
        run_id=expected_run_id,
        account=expected_account,
        config_hash=expected_account_config_sha256,
        label="prepared portfolio",
    )
    for field in (
        "preparation_started_at_utc",
        "deadline_at_utc",
        "promoted_at_utc",
        "prepared_at_utc",
    ):
        _utc_timestamp(payload.get(field), f"prepared portfolio {field}")
    if payload.get("child_finished_at_utc") is not None:
        _utc_timestamp(payload.get("child_finished_at_utc"), "prepared portfolio child finished")
    if payload.get("prepared_at_utc") != payload.get("promoted_at_utc"):
        _fail("REFERENCE_PAYLOAD_INVALID", "prepared portfolio receipt alias mismatch")
    deadline = payload.get("deadline_seconds")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or deadline < 0:
        _fail("REFERENCE_PAYLOAD_INVALID", "prepared portfolio deadline is invalid")
    returncode = payload.get("worker_returncode")
    if returncode is not None and (isinstance(returncode, bool) or not isinstance(returncode, int)):
        _fail("REFERENCE_PAYLOAD_INVALID", "prepared portfolio returncode is invalid")
    if status == "ready":
        relpath = _relpath(payload.get("portfolio_context_relpath"))
        expected_relpath = f"portfolio_context.{payload['payload_sha256']}.json"
        if relpath != expected_relpath:
            _fail("REFERENCE_PATH_INVALID", "prepared portfolio payload path mismatch")
        _sha256(payload.get("payload_sha256"), "prepared portfolio payload hash")
        _text(payload.get("portfolio_source_name"), "portfolio source name")
        _text(payload.get("portfolio_source_account"), "portfolio source account")
        _utc_timestamp(payload.get("source_as_of_utc"), "portfolio source observation")
        if binding["content_sha256"] != payload["payload_sha256"]:
            _fail("REFERENCE_HASH_INVALID", "prepared portfolio logical hash mismatch")
    else:
        _text(payload.get("reason"), "prepared portfolio reason")
        for field in optional:
            _text(payload.get(field), f"prepared portfolio {field}")
        if binding["content_sha256"] is not None:
            _fail("REFERENCE_HASH_INVALID", "unavailable prepared portfolio has content hash")


def _validate_required_data_reference(
    payload: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    expected_run_id: str,
) -> None:
    required = {
        "schema_version",
        "run_id",
        "status",
        "plan_id",
        "sealed_at_utc",
        "required_data_root_relpath",
        "symbols",
        "summary",
        "content_sha256",
    }
    close_pair = {
        "close_advice_required_data_plan_relpath",
        "close_advice_required_data_plan_sha256",
    }
    if frozenset(payload) not in {
        frozenset(required),
        frozenset(required | close_pair),
    }:
        _fail("REFERENCE_PAYLOAD_INVALID", "required-data manifest fields do not match schema")
    if (
        payload.get("schema_version") != REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA
        or payload.get("run_id") != expected_run_id
    ):
        _fail("REFERENCE_PAYLOAD_INVALID", "required-data manifest identity mismatch")
    status = _one_of(payload.get("status"), {"complete", "partial", "failed"}, "required-data status")
    _sha256(payload.get("plan_id"), "required-data plan_id")
    _utc_timestamp(payload.get("sealed_at_utc"), "required-data sealed_at_utc")
    _relpath(payload.get("required_data_root_relpath"))
    if close_pair <= set(payload):
        _relpath(payload.get("close_advice_required_data_plan_relpath"))
        _sha256(
            payload.get("close_advice_required_data_plan_sha256"),
            "required-data close plan hash",
        )
    content_hash = _sha256(payload.get("content_sha256"), "required-data content hash")
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    if canonical_sha256(content) != content_hash or binding["content_sha256"] != content_hash:
        _fail("REFERENCE_HASH_INVALID", "required-data content hash mismatch")
    symbols = _mapping(payload.get("symbols"), "required-data symbols")
    ready_count = 0
    for symbol, raw in symbols.items():
        if not isinstance(symbol, str) or symbol != symbol.strip().upper():
            _fail("REFERENCE_PAYLOAD_INVALID", "required-data symbol is not canonical")
        row = _mapping(raw, f"required-data symbols.{symbol}")
        item_status = _one_of(row.get("status"), {"ready", "failed"}, f"required-data {symbol} status")
        if item_status == "ready":
            ready_count += 1
            ready_keys = {
                "status",
                "fetch_plan",
                "expected_fetch_contract",
                "expected_fetch_contract_sha256",
                "fetch_policy_hash",
                "receipt_relpath",
                "receipt_hash",
                "snapshot_id",
                "payload_sha256",
                "source_observed_at",
                "expires_at",
                "raw_json_relpath",
                "required_data_csv_relpath",
                "source_outcome",
            }
            if "reason_code" in row:
                ready_keys.add("reason_code")
            _keys(row, ready_keys, f"required-data symbols.{symbol}")
            for field in (
                "expected_fetch_contract_sha256",
                "fetch_policy_hash",
                "receipt_hash",
                "payload_sha256",
            ):
                _sha256(row.get(field), f"required-data {symbol} {field}")
            _utc_timestamp(row.get("source_observed_at"), f"required-data {symbol} observed")
            _utc_timestamp(row.get("expires_at"), f"required-data {symbol} expires")
        else:
            failed_keys = {"status", "reason", "error_type"}
            if "detail" in row:
                failed_keys.add("detail")
            _keys(row, failed_keys, f"required-data symbols.{symbol}")
            _text(row.get("reason"), f"required-data {symbol} reason")
            _text(row.get("error_type"), f"required-data {symbol} error_type")
            if "detail" in row and not isinstance(row["detail"], str):
                _fail("REFERENCE_PAYLOAD_INVALID", "required-data failure detail is invalid")
    summary = _mapping(
        payload.get("summary"),
        "required-data summary",
        {"symbols_total", "ready", "failed"},
    )
    expected_summary = {
        "symbols_total": len(symbols),
        "ready": ready_count,
        "failed": len(symbols) - ready_count,
    }
    if summary != expected_summary:
        _fail("REFERENCE_PAYLOAD_INVALID", "required-data summary mismatch")
    expected_status = "complete" if ready_count == len(symbols) and symbols else "partial" if ready_count else "failed"
    if status != expected_status:
        _fail("REFERENCE_PAYLOAD_INVALID", "required-data status mismatch")


def _validate_candidate_reference(
    payload: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    chosen: Mapping[str, Any],
    supplied: Mapping[str, bytes],
    expected_run_id: str,
    expected_account: str,
    expected_account_config_sha256: str,
    expected_required_data_sha256: str,
) -> None:
    if payload.get("schema_version") != binding["schema_version"]:
        _fail("REFERENCE_SCHEMA_INVALID", "candidate manifest binding schema mismatch")
    if binding["schema_version"] != CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA:
        projection = {
            key: payload.get(key)
            for key in (
                "completion_reason",
                "expected_scopes",
                "expected_owners",
                "status_index",
                "owner_snapshots",
            )
        }
        if projection != chosen:
            _fail(
                "CHOSEN_RESULTS_INVALID",
                "chosen results differ from historical candidate manifest",
            )
        manifest_name = PurePosixPath(str(binding["relpath"])).name
        candidate_files = {
            str(binding["relpath"]): supplied[str(binding["relpath"])],
            str(chosen["status_index"]["relpath"]): supplied[
                str(chosen["status_index"]["relpath"])
            ],
            **{
                str(row["relpath"]): supplied[str(row["relpath"])]
                for row in chosen["owner_snapshots"]
            },
        }
        try:
            validate_historical_candidate_snapshot_bundle_bytes(
                manifest_name=manifest_name,
                files=candidate_files,
                account_names=[
                    PurePosixPath(name).name
                    for name in candidate_files
                    if not name.startswith("state/")
                ],
                state_names=[
                    PurePosixPath(name).name
                    for name in candidate_files
                    if name.startswith("state/")
                ],
                run_id=expected_run_id,
                account=expected_account,
                dependencies={},
                require_external_bindings=False,
            )
        except CandidateSnapshotManifestError as exc:
            raise RuntimePortfolioSnapshotError(
                "RUNTIME_PORTFOLIO_SNAPSHOT_REFERENCE_PAYLOAD_INVALID",
                "historical candidate snapshot bundle is invalid",
            ) from exc
        if payload.get("account_config_sha256") != expected_account_config_sha256:
            _fail(
                "REFERENCE_ACCOUNT_MISMATCH",
                "historical candidate account config mismatch",
            )
        if binding["content_sha256"] != payload.get("content_sha256"):
            _fail(
                "REFERENCE_HASH_INVALID",
                "historical candidate manifest content hash mismatch",
            )
        for reference in chosen["owner_snapshots"]:
            owner_payload = _json_object(
                supplied[reference["relpath"]],
                f"historical candidate owner {reference['candidate_owner']}",
            )
            required_data_hash = owner_payload.get(
                "required_data_manifest_sha256"
            )
            if (
                required_data_hash is not None
                and required_data_hash != expected_required_data_sha256
            ):
                _fail(
                    "REFERENCE_PAYLOAD_INVALID",
                    "historical candidate owner required-data binding mismatch",
                )
        return
    try:
        validate_candidate_snapshot_manifest(
            payload,
            expected_run_id=expected_run_id,
            expected_account=expected_account,
        )
    except CandidateSnapshotManifestError as exc:
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_REFERENCE_PAYLOAD_INVALID",
            "candidate snapshot manifest is invalid",
        ) from exc
    if payload.get("account_config_sha256") != expected_account_config_sha256:
        _fail("REFERENCE_ACCOUNT_MISMATCH", "candidate account config mismatch")
    if binding["content_sha256"] != payload.get("content_sha256"):
        _fail("REFERENCE_HASH_INVALID", "candidate manifest content hash mismatch")
    projection = {
        key: payload.get(key)
        for key in (
            "completion_reason",
            "expected_scopes",
            "expected_owners",
            "status_index",
            "owner_snapshots",
        )
    }
    if projection != chosen:
        _fail("CHOSEN_RESULTS_INVALID", "chosen results differ from candidate manifest")
    status_payload = _json_object(supplied[chosen["status_index"]["relpath"]], "strategy status index")
    status_schema = chosen["status_index"]["schema_version"]
    if (
        status_schema != STRATEGY_SCAN_STATUS_INDEX_V5_SCHEMA
        or status_payload.get("schema_version") != status_schema
    ):
        _fail("REFERENCE_SCHEMA_INVALID", "strategy status index schema mismatch")
    try:
        validate_strategy_scan_status_index(
            status_payload,
            expected_run_id=expected_run_id,
            expected_account=expected_account,
            expected_account_config_sha256=expected_account_config_sha256,
        )
    except StrategyScanStatusError as exc:
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_REFERENCE_PAYLOAD_INVALID",
            "strategy status index is invalid",
        ) from exc
    if status_payload.get("content_sha256") != chosen["status_index"]["content_sha256"]:
        _fail("REFERENCE_HASH_INVALID", "strategy status index content hash mismatch")
    status_scopes = _scopes(
        [
            {
                **{key: row.get(key) for key in _SCOPE_KEYS},
                **({"direction": row["direction"]} if row.get("direction") else {}),
            }
            for row in status_payload.get("items", [])
        ],
        "strategy status index scopes",
    )
    if status_scopes != chosen["expected_scopes"]:
        _fail("CHOSEN_RESULTS_INVALID", "strategy status scopes differ from chosen results")
    status_by_owner_scope = {
        (
            scope["candidate_owner"],
            scope["symbol"],
            scope["strategy_mode"],
            scope.get("direction", ""),
        ): row.get("status")
        for scope, row in zip(status_scopes, status_payload["items"], strict=True)
    }
    for reference in chosen["owner_snapshots"]:
        owner_payload = _json_object(
            supplied[reference["relpath"]],
            f"candidate owner {reference['candidate_owner']}",
        )
        if (
            owner_payload.get("schema_version") != reference["schema_version"]
            or owner_payload.get("run_id") != expected_run_id
            or normalize_account_label(owner_payload.get("account")) != expected_account
            or owner_payload.get("content_sha256") != reference["content_sha256"]
            or owner_payload.get("account_config_sha256") != expected_account_config_sha256
            or owner_payload.get("strategy_policy_sha256") != payload.get("strategy_policy_sha256")
            or owner_payload.get("required_data_manifest_sha256") != expected_required_data_sha256
            or owner_payload.get("opening_status") != reference["opening_status"]
        ):
            _fail("REFERENCE_PAYLOAD_INVALID", "candidate owner snapshot identity mismatch")
        owner_content = {key: value for key, value in owner_payload.items() if key != "content_sha256"}
        if canonical_sha256(owner_content) != reference["content_sha256"]:
            _fail("REFERENCE_HASH_INVALID", "candidate owner content hash mismatch")
        try:
            if reference["candidate_owner"] == "opening":
                if reference["schema_version"] != OPENING_CANDIDATE_SNAPSHOT_SCHEMA:
                    raise OpeningCandidateSnapshotError("opening schema mismatch")
                validate_opening_candidate_snapshot(
                    owner_payload,
                    expected_run_id=expected_run_id,
                    expected_account=expected_account,
                    require_current_contract=True,
                )
            elif reference["candidate_owner"] == "sp_lc":
                if reference["schema_version"] != COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA:
                    raise ComboYieldCandidateSnapshotError("combo schema mismatch")
                validate_combo_yield_candidate_snapshot(
                    owner_payload,
                    expected_run_id=expected_run_id,
                    expected_account=expected_account,
                )
            elif reference["candidate_owner"] == "cc_lp":
                if reference["schema_version"] != CC_LP_CANDIDATE_SNAPSHOT_SCHEMA:
                    raise CcLpCandidateSnapshotError("cc_lp schema mismatch")
                validate_cc_lp_candidate_snapshot(
                    owner_payload,
                    expected_run_id=expected_run_id,
                    expected_account=expected_account,
                )
            elif reference["candidate_owner"] == "wheel":
                if reference["schema_version"] != WHEEL_CANDIDATE_SNAPSHOT_SCHEMA:
                    raise WheelCandidateSnapshotError("wheel schema mismatch")
                validate_wheel_candidate_snapshot(
                    owner_payload,
                    expected_run_id=expected_run_id,
                    expected_account=expected_account,
                )
            else:
                _fail("REFERENCE_PAYLOAD_INVALID", "unsupported candidate owner")
        except (
            OpeningCandidateSnapshotError,
            ComboYieldCandidateSnapshotError,
            CcLpCandidateSnapshotError,
            WheelCandidateSnapshotError,
        ) as exc:
            raise RuntimePortfolioSnapshotError(
                "RUNTIME_PORTFOLIO_SNAPSHOT_REFERENCE_PAYLOAD_INVALID",
                "candidate owner snapshot is invalid",
            ) from exc
        expected_scopes = reference["covered_scopes"]
        owner = reference["candidate_owner"]
        owner_scope_rows = [
            row
            for row in owner_payload.get("scope_results", [])
            if isinstance(row, Mapping) and row.get("scope") == "strategy"
        ]
        expected_by_key = {
            (row["symbol"], row["strategy_mode"], row.get("direction", "")): row
            for row in expected_scopes
        }
        actual_by_key = {
            (
                str(row.get("symbol") or "").strip().upper(),
                str(row.get("strategy_mode") or "").strip().lower(),
                str(row.get("direction") or "").strip().lower(),
            ): row
            for row in owner_scope_rows
        }
        if len(actual_by_key) != len(owner_scope_rows) or set(actual_by_key) != set(expected_by_key):
            _fail("CHOSEN_RESULTS_INVALID", f"{owner} owner scopes differ from chosen results")
        if any(
            str(owner_payload.get("market") or "").strip().upper() != scope["market"]
            or actual_by_key[key].get("candidate_owner") != owner
            or actual_by_key[key].get("status") != status_by_owner_scope[(owner, *key)]
            for key, scope in expected_by_key.items()
        ):
            _fail("CHOSEN_RESULTS_INVALID", f"{owner} owner scope facts differ from status index")


def _require_manifest_identity(
    payload: Mapping[str, Any],
    *,
    schema: str,
    run_id: str,
    account: str,
    config_hash: str,
    label: str,
) -> None:
    if (
        payload.get("schema_version") != schema
        or payload.get("run_id") != run_id
        or normalize_account_label(payload.get("account")) != account
        or _sha256(payload.get("account_config_sha256"), f"{label} config hash") != config_hash
    ):
        _fail("REFERENCE_PAYLOAD_INVALID", f"{label} manifest identity mismatch")


def _validate_current_decision_truth(section: Mapping[str, Any], *, expected_account: str) -> None:
    facts = section["facts"]
    current = facts["current_decision"]
    status = _one_of(
        current.get("status"),
        {"trusted", "absent", "data_unavailable"},
        "current decision status",
    )
    reason = current.get("reason")
    schema = facts["read_schema_version"]
    positions = facts["position_lots"]
    missing_views = all(current.get(key) is None for key in (
        "lot_count", "lifecycle_by_lot", "lifecycle_by_case", "lifecycle_quality"
    ))
    if schema is None:
        if (status != "data_unavailable" or current.get("account") is not None
                or current.get("payload") is not None or positions is not None
                or not missing_views or not isinstance(reason, str) or not reason.strip()):
            _fail("SOURCE_STATUS_INVALID", "fallback current decision read is invalid")
        return
    if schema != CURRENT_DECISION_READ_SCHEMA:
        _fail("SOURCE_STATUS_INVALID", "current decision read schema mismatch")
    if normalize_account_label(current.get("account")) != expected_account:
        _fail("SOURCE_STATUS_INVALID", "current decision account mismatch")
    if missing_views:
        if (status == "trusted" or current.get("payload") is not None
                or positions != [] or not isinstance(reason, str) or not reason.strip()):
            _fail("SOURCE_STATUS_INVALID", "sparse current decision read is invalid")
        return
    if not isinstance(positions, list) or _nonnegative_int(current.get("lot_count"), "lot_count") != len(positions):
        _fail("SOURCE_STATUS_INVALID", "current decision lot count mismatch")
    if (
        not isinstance(current.get("lifecycle_by_lot"), Mapping)
        or not isinstance(current.get("lifecycle_by_case"), Mapping)
        or not isinstance(current.get("lifecycle_quality"), Mapping)
    ):
        _fail("SOURCE_STATUS_INVALID", "current decision derived views are invalid")
    if status == "trusted":
        if current.get("reason") is not None or not isinstance(current.get("payload"), Mapping):
            _fail("SOURCE_STATUS_INVALID", "trusted current decision receipt is invalid")
        try:
            payload = validate_current_decision_projection_payload(current["payload"])
        except CurrentDecisionProjectionError as exc:
            raise RuntimePortfolioSnapshotError(
                "RUNTIME_PORTFOLIO_SNAPSHOT_SOURCE_STATUS_INVALID",
                "current decision payload is invalid",
            ) from exc
        if payload["normalized_account"] != expected_account:
            _fail("SOURCE_STATUS_INVALID", "current decision payload account mismatch")
        if payload["position_binding"]["lot_count"] != current["lot_count"]:
            _fail("SOURCE_STATUS_INVALID", "current decision payload lot count mismatch")
    elif current.get("payload") is not None or not isinstance(reason, str) or not reason.strip():
        _fail("SOURCE_STATUS_INVALID", "unavailable current decision receipt is invalid")


def _require_completeness(section: Mapping[str, Any], expected: Mapping[str, Any], name: str) -> None:
    if section["completeness"] != expected:
        _fail("SOURCE_STATUS_INVALID", f"{name} completeness contradicts its owner")


def _require_section_times(section: Mapping[str, Any], observed: str, received: str, name: str) -> None:
    if section["source_observed_at_utc"] != observed or section["application_received_at_utc"] != received:
        _fail("SOURCE_STATUS_INVALID", f"{name} timing contradicts its owner")


def _manifest_reason_codes(manifest: Mapping[str, Any]) -> list[str]:
    return sorted({str(manifest[field]) for field in ("reason", "error_type", "error_code") if manifest.get(field)})


def _option_freshness(manifest: Mapping[str, Any]) -> dict[str, Any]:
    status = str(manifest["fx_status"])
    codes = [] if status == "ready" else [f"fx_status:{status}"]
    if status != "ready" and manifest.get("fx_error_type"):
        codes.append(f"fx_error_type:{manifest['fx_error_type']}")
    return {
        "authority": "prepared_option_positions_context.fx_status",
        "status": status,
        "reason_codes": sorted(codes),
    }


def _require_owner_receipt(
    receipt: Mapping[str, Any],
    *,
    owner_schema_version: str,
    owner_status: str,
    reason_codes: Sequence[str],
    observed: str,
    received: str,
    completeness: Mapping[str, Any],
    freshness: Mapping[str, Any],
    owner: str,
) -> None:
    expected = {
        "owner_schema_version": owner_schema_version,
        "owner_status": owner_status,
        "reason_codes": sorted(set(reason_codes)),
        "source_observed_at_utc": observed,
        "application_received_at_utc": received,
        "completeness": dict(completeness),
        "freshness": dict(freshness),
    }
    actual = {
        "owner_schema_version": receipt["owner_schema_version"],
        "owner_status": receipt["owner_status"],
        "reason_codes": receipt["reason_codes"],
        "source_observed_at_utc": receipt["source_observed_at_utc"],
        "application_received_at_utc": receipt["application_received_at_utc"],
        "completeness": receipt["completeness"],
        "freshness": receipt["freshness"],
    }
    if actual != expected:
        _fail("SOURCE_STATUS_INVALID", f"{owner} receipt contradicts its owner")


def _scopes(value: Any, path: str) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("CHOSEN_RESULTS_INVALID", f"{path} must be a sequence")
    scopes = []
    for index, raw in enumerate(value):
        row = _mapping(raw, f"{path}[{index}]")
        if set(row) != _SCOPE_KEYS and set(row) != _SCOPE_KEYS | {"direction"}:
            _fail("CHOSEN_RESULTS_INVALID", f"{path}[{index}] fields do not match schema")
        scope = {
            "market": _text(row.get("market"), "scope market").upper(),
            "symbol": _text(row.get("symbol"), "scope symbol").upper(),
            "strategy_family": _text(row.get("strategy_family"), "scope strategy_family").lower(),
            "strategy_mode": _text(row.get("strategy_mode"), "scope strategy_mode").lower(),
            "candidate_owner": _text(row.get("candidate_owner"), "scope candidate_owner").lower(),
        }
        if "direction" in row:
            direction = _text(row["direction"], "scope direction").lower()
            if scope["strategy_family"] != "wheel" or direction not in {"call", "put"}:
                _fail("CHOSEN_RESULTS_INVALID", "scope direction is invalid")
            scope["direction"] = direction
        scopes.append(scope)
    ordered = sorted(
        scopes,
        key=lambda item: (
            item["market"], item["symbol"], item["strategy_family"], item.get("direction", ""),
        ),
    )
    identities = {
        tuple(item[key] for key in sorted(_SCOPE_KEYS)) + (item.get("direction", ""),)
        for item in ordered
    }
    if scopes != ordered or len(identities) != len(ordered):
        _fail("CHOSEN_RESULTS_INVALID", f"{path} is not canonical")
    return ordered


def _legacy_comparison(value: Any) -> dict[str, Any]:
    row = _mapping(
        value,
        "legacy_comparison",
        {"schema_version", "status", "mismatch_count", "mismatch_samples", "sections"},
    )
    if row.get("schema_version") != SHADOW_SCHEMA_VERSION:
        _fail("COMPARISON_INVALID", "legacy comparison schema is invalid")
    status = _one_of(row.get("status"), {"matched", "mismatched", "unavailable"}, "comparison status")
    count = _nonnegative_int(row.get("mismatch_count"), "mismatch_count")
    rows_raw = row.get("sections")
    if not isinstance(rows_raw, Sequence) or isinstance(rows_raw, (str, bytes, bytearray)):
        _fail("COMPARISON_INVALID", "comparison sections must be a sequence")
    rows = []
    for index, raw in enumerate(rows_raw):
        item = _mapping(
            raw,
            f"comparison.sections[{index}]",
            {"section", "legacy_sha256", "compact_sha256", "mismatch_count"},
        )
        rows.append(
            {
                "section": _text(item.get("section"), "comparison section"),
                "legacy_sha256": _sha256(item.get("legacy_sha256"), "legacy_sha256"),
                "compact_sha256": _sha256(item.get("compact_sha256"), "compact_sha256"),
                "mismatch_count": _nonnegative_int(item.get("mismatch_count"), "mismatch_count"),
            }
        )
    rows.sort(key=lambda item: item["section"])
    if [item["section"] for item in rows] != list(_COMPARISON_NAMES):
        _fail("COMPARISON_INVALID", "comparison sections are not canonical")
    if sum(item["mismatch_count"] for item in rows) != count:
        _fail("COMPARISON_INVALID", "comparison mismatch count is inconsistent")
    samples_raw = row.get("mismatch_samples")
    if not isinstance(samples_raw, Sequence) or isinstance(samples_raw, (str, bytes, bytearray)):
        _fail("COMPARISON_INVALID", "mismatch_samples must be a sequence")
    if len(samples_raw) > 10:
        _fail("COMPARISON_INVALID", "mismatch_samples exceeds the bound")
    samples = [_mismatch_sample(item, index) for index, item in enumerate(samples_raw)]
    if status == "matched" and (count or samples):
        _fail("COMPARISON_INVALID", "matched comparison contains mismatches")
    if status != "matched" and count == 0:
        _fail("COMPARISON_INVALID", "untrusted comparison lacks a mismatch")
    return {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "status": status,
        "mismatch_count": count,
        "mismatch_samples": samples,
        "sections": rows,
    }


def _ledger_shadow(value: Any) -> dict[str, str]:
    row = _mapping(value, "ledger_shadow", {"schema_version", "status"})
    if row.get("schema_version") != LEDGER_SHADOW_SCHEMA_VERSION:
        _fail("SHADOW_INVALID", "ledger shadow schema is invalid")
    return {
        "schema_version": LEDGER_SHADOW_SCHEMA_VERSION,
        "status": _one_of(row.get("status"), {"matched", "mismatched", "unavailable"}, "ledger shadow status"),
    }


def _mismatch_sample(value: Any, index: int) -> dict[str, Any]:
    keys = {"section", "key", "reason", "legacy_sha256", "compact_sha256"}
    row = _mapping(value, f"mismatch_samples[{index}]", keys)
    return {
        "section": _text(row.get("section"), "mismatch sample section"),
        "key": _text(row.get("key"), "mismatch sample key"),
        "reason": _text(row.get("reason"), "mismatch sample reason"),
        "legacy_sha256": None
        if row.get("legacy_sha256") is None
        else _sha256(row.get("legacy_sha256"), "legacy_sha256"),
        "compact_sha256": None
        if row.get("compact_sha256") is None
        else _sha256(row.get("compact_sha256"), "compact_sha256"),
    }


def _snapshot_reason_codes(body: Mapping[str, Any]) -> list[str]:
    reasons = []
    for name, section in body["sections"].items():
        completeness = section["completeness"]
        if completeness["status"] != "complete":
            reasons.append(f"section_completeness:{name}:{completeness['status']}")
            reasons.extend(completeness["reason_codes"])
        freshness = section["freshness"]
        if freshness["status"] not in {"ready", "not_applicable"}:
            reasons.append(f"section_freshness:{name}:{freshness['status']}")
            reasons.extend(freshness["reason_codes"])
    receipt_key = "legacy_comparison" if body["schema_version"] == LEGACY_SCHEMA_VERSION else "ledger_shadow"
    receipt = body[receipt_key]
    if receipt["status"] != "matched":
        reasons.append(f"{receipt_key}:{receipt['status']}")
    for owner, receipt in body["sections"]["source_status"]["facts"].items():
        freshness = receipt["freshness"]
        if freshness["status"] not in {"ready", "not_applicable"}:
            reasons.append(f"source_freshness:{owner}:{freshness['status']}")
            reasons.extend(freshness["reason_codes"])
    return _sorted_codes(reasons)


def _observed_time_range(sections: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    values = [_parse_utc_timestamp(sections[name]["source_observed_at_utc"], name) for name in SECTION_NAMES]
    minimum = min(values)
    maximum = max(values)
    delta = maximum - minimum
    skew_ms = (delta.days * 86_400_000) + (delta.seconds * 1_000) + (delta.microseconds // 1_000)
    return {
        "minimum_utc": _format_utc(minimum),
        "maximum_utc": _format_utc(maximum),
        "skew_ms": skew_ms,
        "section_names": list(SECTION_NAMES),
    }


def _completeness(
    status: str | Mapping[str, Any],
    codes: Sequence[str] | None = None,
    *,
    path: str,
) -> dict[str, Any]:
    if isinstance(status, Mapping):
        row = _mapping(status, f"{path}.completeness", {"status", "reason_codes"})
        status, codes = row.get("status"), row.get("reason_codes")
    return {
        "status": _one_of(status, {"complete", "partial", "unavailable"}, f"{path}.status"),
        "reason_codes": _sorted_codes(codes or []),
    }


def _freshness(
    authority: str | Mapping[str, Any],
    status: str | None = None,
    codes: Sequence[str] | None = None,
    *,
    path: str,
) -> dict[str, Any]:
    if isinstance(authority, Mapping):
        row = _mapping(
            authority,
            f"{path}.freshness",
            {"authority", "status", "reason_codes"},
        )
        authority, status, codes = (row.get("authority"), row.get("status"), row.get("reason_codes"))
    authority_norm = _text(authority, f"{path}.authority")
    status_norm = _one_of(
        status,
        {"ready", "unavailable_stale", "unavailable", "not_applicable"},
        f"{path}.status",
    )
    reason_codes = _sorted_codes(codes or [])
    if status_norm == "not_applicable" and (authority_norm != "not_applicable" or reason_codes):
        _fail("FRESHNESS_INVALID", f"{path} not_applicable receipt is invalid")
    if status_norm == "ready" and reason_codes:
        _fail("FRESHNESS_INVALID", f"{path} ready receipt has reason codes")
    return {"authority": authority_norm, "status": status_norm, "reason_codes": reason_codes}


def _not_applicable_freshness() -> dict[str, Any]:
    return {"authority": "not_applicable", "status": "not_applicable", "reason_codes": []}


def _project_required(source: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    missing = [key for key in keys if key not in source]
    if missing:
        _fail("REQUIRED_FIELD_MISSING", f"required fields are missing: {','.join(missing)}")
    projected = {key: source[key] for key in keys}
    canonical_json_bytes(projected)
    return projected


def _sorted_codes(value: Any) -> list[str]:
    return _sorted_texts(value, "reason_codes")


def _sorted_texts(value: Any, path: str) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("FIELD_INVALID", f"{path} must be a sequence")
    items = [_text(item, path) for item in value]
    return sorted(set(items))


def _relpath(value: Any) -> str:
    text = _text(value, "reference.relpath")
    if "\\" in text:
        _fail("REFERENCE_PATH_INVALID", "reference path contains a backslash")
    path = PurePosixPath(text)
    parts = path.parts
    if path.is_absolute() or path.as_posix() != text or not parts:
        _fail("REFERENCE_PATH_INVALID", "reference path is not normalized and relative")
    if any(part in {"", ".", ".."} or part.lower() == "latest" for part in parts):
        _fail("REFERENCE_PATH_INVALID", "reference path contains a forbidden component")
    return text


def _utc_timestamp(value: Any, path: str) -> str:
    return _format_utc(_parse_utc_timestamp(value, path))


def _parse_utc_timestamp(value: Any, path: str) -> datetime:
    text = _text(value, path)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimePortfolioSnapshotError(
            "RUNTIME_PORTFOLIO_SNAPSHOT_TIMESTAMP_INVALID", f"{path} is not RFC3339"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _fail("TIMESTAMP_INVALID", f"{path} must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _mapping(value: Any, path: str, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("FIELD_INVALID", f"{path} must be an object")
    row = dict(value)
    if keys is not None:
        _keys(row, keys, path)
    return row


def _keys(value: Mapping[str, Any], keys: set[str], path: str) -> None:
    if set(value) != keys:
        _fail("FIELD_INVALID", f"{path} fields do not match schema")


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail("FIELD_INVALID", f"{path} must be non-empty normalized text")
    return value


def _identity(value: Any, path: str) -> str:
    text = _text(value, path)
    if text in {".", ".."} or "/" in text or "\\" in text:
        _fail("IDENTITY_INVALID", f"{path} is not path-safe")
    return text


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail("HASH_INVALID", f"{path} must be a lowercase SHA-256")
    return value


def _one_of(value: Any, choices: set[str], path: str) -> str:
    text = _text(value, path)
    if text not in choices:
        _fail("FIELD_INVALID", f"{path} is invalid")
    return text


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("FIELD_INVALID", f"{path} must be a non-negative integer")
    return value


def _enforce_size(snapshot: Mapping[str, Any]) -> None:
    if len(canonical_json_bytes(snapshot)) >= MAX_CANONICAL_BYTES:
        _fail("SIZE_LIMIT_EXCEEDED", "runtime portfolio snapshot exceeds the byte limit")


def _fail(suffix: str, message: str):
    raise RuntimePortfolioSnapshotError(f"RUNTIME_PORTFOLIO_SNAPSHOT_{suffix}", message)
