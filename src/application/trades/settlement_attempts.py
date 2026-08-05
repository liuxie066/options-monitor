from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from typing import Any, Mapping


SETTLEMENT_COLLECTOR_NAME = "settlement_observation"
SETTLEMENT_COLLECTOR_CONTRACT_VERSION = "settlement_collector.v1"
SETTLEMENT_GATEWAY_ADAPTER_VERSION = "futu_settlement_adapter.v1"
SETTLEMENT_OBSERVATION_CONTEXT_KEY = "_settlement_observation_context"


_DEFAULT_REQUIREMENTS = {
    "broker.history_deals": ("broker", "get_history_deals"),
    "broker.history_orders": ("broker", "get_history_orders"),
    "broker.fresh_positions": (
        "broker",
        "get_positions_with_receipt",
    ),
    "quote.trading_calendar": (
        "quote",
        "get_trading_days_with_receipt",
    ),
}

# Safe initial boundary: no provider code is treated as a permanent account
# capability result until authoritative provider evidence is added here.
EXPLICIT_ACCOUNT_BLOCK_PROVIDER_CODES: frozenset[str] = frozenset()

_PROVIDER_ERROR_CLASS_BY_CODE = {
    "TRANSIENT": "transient",
    "RATE_LIMIT": "rate_limit",
    "AUTH_EXPIRED": "auth_expired",
    "NEED_2FA": "need_2fa",
    "TIMEOUT": "timeout",
    "PROVIDER_UNAVAILABLE": "provider_unavailable",
}


@dataclass(frozen=True)
class SettlementCollectorContract:
    name: str = SETTLEMENT_COLLECTOR_NAME
    contract_version: str = SETTLEMENT_COLLECTOR_CONTRACT_VERSION
    required_capability_keys: tuple[str, ...] = tuple(
        _DEFAULT_REQUIREMENTS
    )


@dataclass(frozen=True)
class SettlementCapabilitySnapshot:
    contract_version: str
    gateway_adapter_version: str
    provider_sdk_version: str
    capability_fingerprint: str
    capabilities: dict[str, str]

    @property
    def supported(self) -> bool:
        return all(
            state == "supported"
            for state in self.capabilities.values()
        )

    @property
    def missing_keys(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                key
                for key, state in self.capabilities.items()
                if state != "supported"
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "gateway_adapter_version": self.gateway_adapter_version,
            "provider_sdk_version": self.provider_sdk_version,
            "capability_fingerprint": self.capability_fingerprint,
            "capabilities": dict(self.capabilities),
            "supported": self.supported,
            "missing_keys": list(self.missing_keys),
        }


@dataclass(frozen=True)
class SettlementAttemptOutcome:
    kind: str
    source_id: str
    account: str
    case_id: str
    contract_version: str
    capability_fingerprint: str
    reason_code: str | None = None
    provider_code: str | None = None
    error_class: str | None = None
    retry_after_ms: int | None = None
    observation: dict[str, Any] | None = None

    def to_dict(self, *, include_observation: bool = True) -> dict[str, Any]:
        output = {
            "kind": self.kind,
            "source_id": self.source_id,
            "account": self.account,
            "case_id": self.case_id,
            "contract_version": self.contract_version,
            "capability_fingerprint": self.capability_fingerprint,
            "reason_code": self.reason_code,
            "provider_code": self.provider_code,
            "error_class": self.error_class,
            "retry_after_ms": self.retry_after_ms,
        }
        if include_observation:
            output["observation"] = (
                dict(self.observation)
                if isinstance(self.observation, dict)
                else None
            )
        return output


def inspect_settlement_capabilities(
    *,
    broker_gateway: Any | None,
    quote_gateway: Any | None,
    contract: SettlementCollectorContract,
    additional_requirements: Mapping[str, tuple[str, str]] | None = None,
) -> SettlementCapabilitySnapshot:
    requirements = {
        **_DEFAULT_REQUIREMENTS,
        **dict(additional_requirements or {}),
    }
    gateways = {"broker": broker_gateway, "quote": quote_gateway}
    capabilities: dict[str, str] = {}
    for key in contract.required_capability_keys:
        requirement = requirements.get(key)
        if requirement is None:
            capabilities[key] = "missing_static"
            continue
        gateway_name, method_name = requirement
        gateway = gateways.get(gateway_name)
        capabilities[key] = (
            "supported"
            if gateway is not None
            and callable(getattr(gateway, method_name, None))
            else "missing_static"
        )
    try:
        sdk_version = importlib.metadata.version("futu-api")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = "unavailable"
    preimage = {
        "contract_version": contract.contract_version,
        "required_capability_keys": list(
            contract.required_capability_keys
        ),
        "gateway_adapter_version": SETTLEMENT_GATEWAY_ADAPTER_VERSION,
        "provider_sdk_version": sdk_version,
        "capabilities": capabilities,
    }
    return SettlementCapabilitySnapshot(
        contract_version=contract.contract_version,
        gateway_adapter_version=SETTLEMENT_GATEWAY_ADAPTER_VERSION,
        provider_sdk_version=sdk_version,
        capability_fingerprint=_canonical_hash(preimage),
        capabilities=capabilities,
    )


def classify_observation_outcome(
    observation: Mapping[str, Any],
    *,
    source_id: str,
    account: str,
    case_id: str,
    contract: SettlementCollectorContract,
    capability: SettlementCapabilitySnapshot,
) -> SettlementAttemptOutcome:
    receipts = observation.get("source_receipts")
    receipt_rows = (
        [item for item in receipts.values() if isinstance(item, Mapping)]
        if isinstance(receipts, Mapping)
        else []
    )
    failures = [
        row
        for row in receipt_rows
        if str(row.get("status") or "").strip().lower()
        != "complete"
        and (
            str(row.get("error_class") or "").strip()
            or str(row.get("provider_code") or "").strip()
        )
    ]
    if failures:
        provider_codes = {
            str(row.get("provider_code") or "").strip().upper()
            for row in failures
            if str(row.get("provider_code") or "").strip()
        }
        explicit = sorted(
            provider_codes & EXPLICIT_ACCOUNT_BLOCK_PROVIDER_CODES
        )
        retry_after_ms = max(
            (
                _nonnegative_int(row.get("retry_after_ms")) or 0
                for row in failures
            ),
            default=0,
        )
        error_classes = {
            str(row.get("error_class") or "").strip().lower()
            for row in failures
            if str(row.get("error_class") or "").strip()
        }
        if explicit:
            kind = "blocked_account_explicit"
            reason = "provider_account_capability_blocked"
        elif error_classes and error_classes <= {
            "transient",
            "rate_limit",
            "auth_expired",
            "need_2fa",
            "timeout",
            "provider_unavailable",
        }:
            kind = "retryable_error"
            reason = "provider_query_retryable"
        else:
            kind = "unknown_error"
            reason = "provider_query_unknown"
        return SettlementAttemptOutcome(
            kind=kind,
            source_id=source_id,
            account=account,
            case_id=case_id,
            contract_version=contract.contract_version,
            capability_fingerprint=(
                capability.capability_fingerprint
            ),
            reason_code=reason,
            provider_code=(
                explicit[0]
                if explicit
                else sorted(provider_codes)[0]
                if provider_codes
                else None
            ),
            error_class=(
                sorted(error_classes)[0]
                if error_classes
                else "unknown"
            ),
            retry_after_ms=retry_after_ms or None,
        )
    kind = (
        "observed_complete"
        if bool(observation.get("complete"))
        else "observed_incomplete"
    )
    return SettlementAttemptOutcome(
        kind=kind,
        source_id=source_id,
        account=account,
        case_id=case_id,
        contract_version=contract.contract_version,
        capability_fingerprint=capability.capability_fingerprint,
        reason_code=(
            "settlement_observation_complete"
            if kind == "observed_complete"
            else "settlement_observation_incomplete"
        ),
        observation=dict(observation),
    )


def classify_exception_outcome(
    exc: Exception,
    *,
    source_id: str,
    account: str,
    case_id: str,
    contract: SettlementCollectorContract,
    capability: SettlementCapabilitySnapshot,
) -> SettlementAttemptOutcome:
    provider_code = str(getattr(exc, "code", "") or "").strip().upper()
    error_class = _PROVIDER_ERROR_CLASS_BY_CODE.get(
        provider_code,
        "unknown",
    )
    if provider_code in EXPLICIT_ACCOUNT_BLOCK_PROVIDER_CODES:
        kind = "blocked_account_explicit"
        reason = "provider_account_capability_blocked"
    elif error_class != "unknown" or isinstance(exc, TimeoutError):
        kind = "retryable_error"
        reason = "provider_query_retryable"
        if isinstance(exc, TimeoutError):
            error_class = "timeout"
    else:
        kind = "unknown_error"
        reason = "provider_query_unknown"
    retry_after = _nonnegative_int(
        getattr(exc, "retry_after_ms", None)
    )
    return SettlementAttemptOutcome(
        kind=kind,
        source_id=source_id,
        account=account,
        case_id=case_id,
        contract_version=contract.contract_version,
        capability_fingerprint=capability.capability_fingerprint,
        reason_code=reason,
        provider_code=provider_code or None,
        error_class=error_class,
        retry_after_ms=retry_after,
    )


def backoff_delay_ms(
    outcome_kind: str,
    *,
    attempt_count: int,
    no_progress_count: int,
    retry_after_ms: int | None = None,
) -> int | None:
    kind = str(outcome_kind or "").strip().lower()
    if kind in {"blocked_static", "legacy_semantic_unavailable"}:
        return None
    if kind == "blocked_account_explicit":
        delay = 24 * 60 * 60 * 1000
    elif kind == "retryable_error":
        delay = _schedule((1, 5, 15, 60), attempt_count) * 60_000
    elif kind == "unknown_error":
        delay = _schedule((5, 15, 60, 360), attempt_count) * 60_000
    elif kind == "observed_incomplete":
        delay = _schedule((5, 15, 60, 360), no_progress_count) * 60_000
    elif kind == "observed_complete":
        delay = 6 * 60 * 60 * 1000
    elif kind == "stale_generation":
        return 0
    else:
        delay = 5 * 60 * 1000
    return max(delay, int(retry_after_ms or 0))


def case_scope_fingerprint(candidate: Mapping[str, Any]) -> str:
    lifecycle_case = candidate.get("lifecycle_case")
    timing_policy = candidate.get("timing_policy")
    case = dict(lifecycle_case) if isinstance(lifecycle_case, Mapping) else {}
    timing = dict(timing_policy) if isinstance(timing_policy, Mapping) else {}
    return _canonical_hash(
        {
            "schema_version": "case_scope_fingerprint.v2",
            "case": {
                "case_id": case.get("case_id"),
                "account": case.get("account"),
                "status": case.get("status"),
                "decision_type": case.get("decision_type"),
                "target_contracts_by_lot": case.get(
                    "target_contracts_by_lot"
                ),
                "observation_start_ms": case.get(
                    "observation_start_ms"
                ),
                "pending_until_ms": case.get("pending_until_ms"),
                "derived_reason_state": (
                    dict(case.get("derived_summary") or {}).get(
                        "reason_state"
                    )
                    if isinstance(case.get("derived_summary"), Mapping)
                    else None
                ),
            },
            "case_updated_at_ms": candidate.get(
                "case_updated_at_ms"
            ),
            "timing": {
                "policy_schema": timing.get("policy_schema"),
                "settlement_deadline_ms": timing.get(
                    "settlement_deadline_ms"
                ),
                "calendar_hash": timing.get("calendar_hash"),
            },
            "evidence_revision": int(
                candidate.get("evidence_revision") or 0
            ),
        }
    )


def provider_input_scope_fingerprint(
    *,
    lifecycle_case: Mapping[str, Any],
    read_model: Mapping[str, Any],
) -> str:
    return _canonical_hash(
        {
            "schema_version": "provider_input_scope_fingerprint.v1",
            "case_id": lifecycle_case.get("case_id"),
            "account": lifecycle_case.get("account"),
            "futu_account_id": lifecycle_case.get("futu_account_id"),
            "contract_key": lifecycle_case.get("contract_key"),
            "target_contracts_by_lot": lifecycle_case.get(
                "target_contracts_by_lot"
            ),
            "observation_start_ms": lifecycle_case.get(
                "observation_start_ms"
            ),
            "pending_until_ms": read_model.get("pending_until_ms"),
            "pairing_until_ms": read_model.get("pairing_until_ms"),
            "first_option_close_received_at_ms": read_model.get(
                "first_option_close_received_at_ms"
            ),
            "remaining_contracts_by_lot": read_model.get(
                "remaining_contracts_by_lot"
            ),
            "reserved_contracts_by_lot": read_model.get(
                "reserved_contracts_by_lot"
            ),
            "terminal_event_ids": read_model.get(
                "terminal_event_ids"
            ),
            "reservation_evidence_ids": sorted(
                str(item or "").strip()
                for item in read_model.get(
                    "reservation_evidence_ids"
                )
                or ()
                if str(item or "").strip()
            ),
            "timing_policy_hash": read_model.get("timing_policy_hash"),
        }
    )


def prepare_provider_required_state(
    current: Mapping[str, Any] | None,
    *,
    source_id: str,
    account: str,
    case_id: str,
    case_scope_fingerprint_value: str,
    provider_input_scope_fingerprint_value: str,
    contract_version: str,
    capability_fingerprint: str,
    now_ms: int,
) -> dict[str, Any]:
    prior = dict(current or {})
    legacy_evidence_scope_changed = (
        str(prior.get("outcome_kind") or "")
        == "legacy_semantic_unavailable"
        and str(prior.get("case_scope_fingerprint") or "")
        != str(case_scope_fingerprint_value or "")
    )
    same_attempt_scope = (
        not legacy_evidence_scope_changed
        and str(prior.get("provider_input_scope_fingerprint") or "")
        == str(provider_input_scope_fingerprint_value or "")
        and str(prior.get("collector_contract_version") or "")
        == str(contract_version or "")
        and str(prior.get("capability_fingerprint") or "")
        == str(capability_fingerprint or "")
    )
    preserved = prior if same_attempt_scope else {}
    return {
        "source_id": str(source_id or "").strip(),
        "account": str(account or "").strip().lower(),
        "case_id": str(case_id or "").strip(),
        "case_scope_fingerprint": str(
            case_scope_fingerprint_value or ""
        ).strip(),
        "provider_input_scope_fingerprint": str(
            provider_input_scope_fingerprint_value or ""
        ).strip(),
        "collector_contract_version": str(
            contract_version or ""
        ).strip(),
        "capability_fingerprint": str(
            capability_fingerprint or ""
        ).strip(),
        "classification": "provider_required",
        "outcome_kind": preserved.get("outcome_kind"),
        "reason_code": preserved.get("reason_code"),
        "provider_code": preserved.get("provider_code"),
        "error_class": preserved.get("error_class"),
        "attempt_count": int(
            preserved.get("attempt_count") or 0
        ),
        "no_progress_count": int(
            preserved.get("no_progress_count") or 0
        ),
        "next_attempt_at_ms": preserved.get(
            "next_attempt_at_ms"
        ),
        "last_attempt_at_ms": preserved.get(
            "last_attempt_at_ms"
        ),
        "last_semantic_fingerprint": preserved.get(
            "last_semantic_fingerprint"
        ),
        "claim_id": preserved.get("claim_id"),
        "claim_until_ms": preserved.get("claim_until_ms"),
        "updated_at_ms": int(now_ms),
    }


def settlement_attempt_updates_after_outcome(
    current: Mapping[str, Any],
    *,
    outcome: SettlementAttemptOutcome,
    now_ms: int,
    case_scope_fingerprint_value: str,
    provider_input_scope_fingerprint_value: str,
    semantic_fingerprint: str | None = None,
    provider_attempted: bool | None = None,
) -> dict[str, Any]:
    previous_attempts = _nonnegative_int(
        current.get("attempt_count")
    ) or 0
    previous_no_progress = _nonnegative_int(
        current.get("no_progress_count")
    ) or 0
    semantic_value = str(semantic_fingerprint or "").strip() or None
    previous_semantic = str(
        current.get("last_semantic_fingerprint") or ""
    ).strip() or None
    same_semantic = bool(
        semantic_value and semantic_value == previous_semantic
    )
    delay_ms = backoff_delay_ms(
        outcome.kind,
        attempt_count=previous_attempts,
        no_progress_count=(
            previous_no_progress if same_semantic else 0
        ),
        retry_after_ms=outcome.retry_after_ms,
    )
    if outcome.kind == "observed_incomplete":
        no_progress_count = (
            previous_no_progress + 1 if same_semantic else 1
        )
    elif outcome.kind == "observed_complete":
        no_progress_count = 0
    else:
        no_progress_count = previous_no_progress
    attempted = (
        bool(provider_attempted)
        if provider_attempted is not None
        else outcome.kind not in {"blocked_static", "stale_generation"}
    )
    return {
        "case_scope_fingerprint": str(
            case_scope_fingerprint_value or ""
        ).strip(),
        "provider_input_scope_fingerprint": str(
            provider_input_scope_fingerprint_value or ""
        ).strip(),
        "collector_contract_version": outcome.contract_version,
        "capability_fingerprint": outcome.capability_fingerprint,
        "classification": (
            "unclassified"
            if outcome.kind == "stale_generation"
            else "provider_required"
        ),
        "outcome_kind": outcome.kind,
        "reason_code": outcome.reason_code,
        "provider_code": outcome.provider_code,
        "error_class": outcome.error_class,
        "attempt_count": previous_attempts + (1 if attempted else 0),
        "no_progress_count": no_progress_count,
        "next_attempt_at_ms": (
            int(now_ms) + int(delay_ms)
            if delay_ms is not None
            else None
        ),
        "last_attempt_at_ms": (
            int(now_ms)
            if attempted
            else current.get("last_attempt_at_ms")
        ),
        "last_semantic_fingerprint": (
            semantic_value or previous_semantic
        ),
        "updated_at_ms": int(now_ms),
    }


def _schedule(values: tuple[int, ...], count: int) -> int:
    index = max(0, min(int(count or 0), len(values) - 1))
    return values[index]


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "EXPLICIT_ACCOUNT_BLOCK_PROVIDER_CODES",
    "SETTLEMENT_COLLECTOR_CONTRACT_VERSION",
    "SETTLEMENT_COLLECTOR_NAME",
    "SETTLEMENT_OBSERVATION_CONTEXT_KEY",
    "SettlementAttemptOutcome",
    "SettlementCapabilitySnapshot",
    "SettlementCollectorContract",
    "backoff_delay_ms",
    "case_scope_fingerprint",
    "classify_exception_outcome",
    "classify_observation_outcome",
    "inspect_settlement_capabilities",
    "prepare_provider_required_state",
    "provider_input_scope_fingerprint",
    "settlement_attempt_updates_after_outcome",
]
