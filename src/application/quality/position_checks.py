from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
)
from domain.domain.symbol_identity import OPTION_CODE_RE, canonical_symbol, symbol_market
from domain.domain.trade_contract_identity import normalize_contract_expiration
from src.application.opend_normalize import normalize_opend_option_type
from src.application.quality.model import (
    check_result,
    dataset_status,
    evidence_ref,
    freshness,
    sha256_json,
    utc_iso,
)
from src.application.trades.lifecycle import PENDING_STATUSES
from src.infrastructure.quality.opend_position_adapter import OpenDOptionSnapshot


def _decimal(value: Any) -> Decimal | None:
    try:
        if value in (None, "", "-"):
            return None
        parsed = Decimal(str(value)).normalize()
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _positive_decimal_from(
    values: dict[str, Any],
    *keys: str,
) -> Decimal | None:
    for key in keys:
        parsed = _decimal(values.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _position_side(value: Any, *, qty: Decimal | None = None) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"long", "buy", "1"}:
        return "long"
    if raw in {"short", "sell", "-1"}:
        return "short"
    if qty is not None and qty < 0:
        return "short"
    if qty is not None and qty > 0:
        return "long"
    return None


def _contract_key(
    *,
    symbol: str,
    option_type: str,
    expiration: str,
    strike: Decimal,
    multiplier: Decimal,
) -> str:
    return "|".join(
        (
            symbol,
            option_type,
            expiration,
            format(strike, "f"),
            format(multiplier, "f"),
        )
    )


def normalize_local_positions(
    rows: list[dict[str, Any]],
    *,
    account: str,
    market: str | None = None,
) -> tuple[dict[str, Decimal], list[str]]:
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    errors: list[str] = []
    for row in rows:
        fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        if str(fields.get("account") or "").strip().lower() != account:
            continue
        contracts = _decimal(effective_contracts_open(fields))
        if contracts is None or contracts == 0:
            continue
        symbol = canonical_symbol(fields.get("symbol"))
        row_market = str(symbol_market(symbol) or "").lower()
        if market and row_market and row_market != market.lower():
            continue
        option_type = str(fields.get("option_type") or "").strip().lower()
        expiration = str(
            effective_expiration_ymd(fields)
            or normalize_contract_expiration(fields.get("expiration_ymd"))
            or ""
        ).strip()
        strike = _decimal(effective_strike(fields))
        multiplier = _decimal(effective_multiplier(fields))
        side = _position_side(fields.get("side"), qty=contracts)
        if not symbol or option_type not in {"put", "call"} or not expiration or strike is None or multiplier is None or side is None:
            errors.append(str(row.get("record_id") or "unknown"))
            continue
        key = _contract_key(
            symbol=symbol,
            option_type=option_type,
            expiration=expiration,
            strike=strike,
            multiplier=multiplier,
        )
        quantities[key] += abs(contracts) if side == "long" else -abs(contracts)
    return {key: value for key, value in quantities.items() if value != 0}, errors


def normalize_opend_positions(
    rows: list[dict[str, Any]],
    *,
    market: str | None = None,
) -> tuple[dict[str, Decimal], list[str]]:
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    errors: list[str] = []
    for index, row in enumerate(rows):
        code = str(row.get("code") or row.get("symbol") or row.get("stock_code") or "").strip().upper()
        row_market = str(
            code.split(".", 1)[0] if "." in code else symbol_market(code) or ""
        ).lower()
        if market and row_market and row_market != market.lower():
            continue
        match = OPTION_CODE_RE.match(code)
        qty = _decimal(row.get("qty") if "qty" in row else row.get("quantity"))
        if qty == 0:
            continue
        multiplier = _positive_decimal_from(
            row,
            "options_per_contract",
            "option_contract_multiplier",
            "option_contract_size",
            "contract_multiplier",
            "lot_size",
            "multiplier",
        )
        side = _position_side(
            row.get("position_side") if "position_side" in row else row.get("side"),
            qty=qty,
        )
        symbol = canonical_symbol(
            row.get("stock_owner")
            or row.get("owner_code")
            or row.get("underlying")
            or code
        )
        option_type = normalize_opend_option_type(row.get("option_type"))
        if option_type not in {"put", "call"} and match is not None:
            option_type = "call" if match.group("cp") == "C" else "put"
        expiration = str(
            normalize_contract_expiration(
                row.get("strike_time")
                or row.get("expiration_ymd")
                or row.get("expiration")
            )
            or ""
        ).strip()
        if not expiration and match is not None:
            expiration = f"20{match.group('yy')}-{match.group('mm')}-{match.group('dd')}"
        strike = _positive_decimal_from(
            row,
            "option_strike_price",
            "strike_price",
        )
        if strike is None and match is not None:
            strike = _decimal(Decimal(match.group("strike")) / Decimal("1000"))
        if (
            qty is None
            or multiplier is None
            or side is None
            or not symbol
            or option_type not in {"put", "call"}
            or not expiration
            or strike is None
        ):
            errors.append(f"row-{index}")
            continue
        key = _contract_key(
            symbol=symbol,
            option_type=option_type,
            expiration=expiration,
            strike=strike,
            multiplier=multiplier,
        )
        quantities[key] += abs(qty) if side == "long" else -abs(qty)
    return {key: value for key, value in quantities.items() if value != 0}, errors


def _contract_terms_drifts(
    *,
    local: dict[str, Decimal],
    broker: dict[str, Decimal],
    broker_rows: list[dict[str, Any]],
    raw_comparison: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    compared_keys = set(raw_comparison)
    proposals: list[tuple[str, str, Decimal, str]] = []
    for row in broker_rows:
        code = str(
            row.get("code") or row.get("symbol") or row.get("stock_code") or ""
        ).strip().upper()
        code_match = OPTION_CODE_RE.match(code)
        if code_match is None:
            continue
        normalized, errors = normalize_opend_positions([row])
        if errors or len(normalized) != 1:
            continue
        current_key, quantity = next(iter(normalized.items()))
        if current_key not in compared_keys or broker.get(current_key) != quantity:
            continue
        current_parts = current_key.split("|")
        if len(current_parts) != 5:
            continue
        locator_option_type = "call" if code_match.group("cp") == "C" else "put"
        locator_expiration = (
            f"20{code_match.group('yy')}-{code_match.group('mm')}-"
            f"{code_match.group('dd')}"
        )
        locator_strike = _decimal(
            Decimal(code_match.group("strike")) / Decimal("1000")
        )
        local_candidates = []
        for local_key, local_quantity in local.items():
            parts = local_key.split("|")
            if (
                local_key in compared_keys
                and len(parts) == 5
                and parts[0] == current_parts[0]
                and parts[1] == locator_option_type
                and parts[2] == locator_expiration
                and _decimal(parts[3]) == locator_strike
                and local_quantity == quantity
            ):
                local_candidates.append(local_key)
        if len(local_candidates) != 1:
            continue
        local_key = local_candidates[0]
        if local_key != current_key:
            proposals.append((local_key, current_key, quantity, code))

    drifts: list[dict[str, str]] = []
    for local_key, current_key, quantity, code in sorted(
        proposals,
        key=lambda item: (item[0], item[1], item[3]),
    ):
        if (
            sum(1 for item in proposals if item[0] == local_key) != 1
            or sum(1 for item in proposals if item[1] == current_key) != 1
        ):
            continue
        local_parts = local_key.split("|")
        current_parts = current_key.split("|")
        drifts.append(
            {
                "symbol": current_parts[0],
                "option_type": current_parts[1],
                "expiration": current_parts[2],
                "quantity": format(quantity, "f"),
                "local_contracts": (
                    f"{local_parts[3]}@{local_parts[4]}:{format(quantity, 'f')}"
                ),
                "opend_contracts": (
                    f"{current_parts[3]}@{current_parts[4]}:{format(quantity, 'f')}"
                ),
                "broker_code": code,
                "mapping": "code_lineage",
            }
        )
    return drifts


def _pending_lifecycle_coverage(
    *,
    cases: list[dict[str, Any]],
    read_models_by_case: dict[str, dict[str, Any]],
    timing_policies_by_case: dict[str, dict[str, Any]],
    account: str,
    market: str,
    now: datetime,
) -> tuple[dict[str, Decimal], dict[str, list[str]]]:
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    case_ids_by_key: dict[str, list[str]] = defaultdict(list)
    now_ms = int(now.astimezone(timezone.utc).timestamp() * 1000)
    for case in cases:
        if str(case.get("account") or "").strip().lower() != account:
            continue
        status = str(case.get("status") or "").strip().lower()
        if status not in PENDING_STATUSES:
            continue
        symbol = canonical_symbol(case.get("symbol"))
        case_market = str(
            case.get("market") or symbol_market(symbol) or ""
        ).strip().lower()
        if case_market != market.strip().lower():
            continue
        case_id = str(case.get("case_id") or "").strip()
        read_model = (
            dict(read_models_by_case.get(case_id) or {})
            if isinstance(read_models_by_case.get(case_id), dict)
            else {}
        )
        if (
            str(read_model.get("lifecycle_state") or "").strip().lower()
            == "conflict"
            or str(read_model.get("reason_state") or "").strip().lower()
            == "conflict"
            or str(
                read_model.get("lifecycle_evidence_status") or ""
            ).strip().lower()
            == "conflict"
        ):
            continue
        timing_policy = (
            dict(timing_policies_by_case.get(case_id) or {})
            if isinstance(timing_policies_by_case.get(case_id), dict)
            else {}
        )
        deadline_ms = (
            read_model.get("pending_until_ms")
            if read_model.get("pending_until_ms") is not None
            else timing_policy.get("settlement_deadline_ms")
        )
        try:
            deadline = int(deadline_ms)
        except (TypeError, ValueError, OverflowError):
            continue
        if deadline <= 0 or now_ms > deadline:
            continue
        remaining_by_lot = read_model.get("remaining_contracts_by_lot")
        if not isinstance(remaining_by_lot, dict) or not remaining_by_lot:
            continue
        remaining_values = [
            _decimal(raw) for raw in remaining_by_lot.values()
        ]
        if any(
            value is None or value < 0 for value in remaining_values
        ):
            continue
        remaining = sum(
            (value for value in remaining_values if value is not None),
            Decimal(0),
        )
        option_type = str(case.get("option_type") or "").strip().lower()
        expiration = str(
            normalize_contract_expiration(case.get("expiration_ymd")) or ""
        ).strip()
        strike = _decimal(case.get("strike"))
        multiplier = _decimal(case.get("multiplier"))
        side = _position_side(case.get("position_side"))
        if (
            not case_id
            or not symbol
            or option_type not in {"put", "call"}
            or not expiration
            or strike is None
            or multiplier is None
            or side is None
            or remaining <= 0
        ):
            continue
        key = _contract_key(
            symbol=symbol,
            option_type=option_type,
            expiration=expiration,
            strike=strike,
            multiplier=multiplier,
        )
        quantities[key] += remaining if side == "long" else -remaining
        case_ids_by_key[key].append(case_id)
    return dict(quantities), {
        key: sorted(set(value)) for key, value in case_ids_by_key.items()
    }


def build_position_dataset(
    *,
    snapshot: OpenDOptionSnapshot,
    local_lots: list[dict[str, Any]],
    account: str,
    market: str,
    observed_at_utc: str,
    now: datetime,
    control_state: dict[str, Any],
    lifecycle_cases: list[dict[str, Any]] | None = None,
    lifecycle_read_models_by_case: dict[str, dict[str, Any]] | None = None,
    lifecycle_timing_policies_by_case: dict[str, dict[str, Any]] | None = None,
    lifecycle_coherent_read_available: bool = True,
    day_end_strict: bool = False,
    persistent_after_seconds: int = 300,
    next_authoritative_refresh_due_utc: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    scope = {"account": account, "market": market}
    source_ok = bool(snapshot.complete and snapshot.refresh_cache and snapshot.environment == "REAL")
    source_check = check_result(
        check_id="OM-POS-001",
        status="pass" if source_ok else "unknown",
        scope=scope,
        observed_at_utc=observed_at_utc,
        reason_code="OPEND_OPTION_SNAPSHOT_COMPLETE" if source_ok else snapshot.error_code or "OPEND_OPTION_SNAPSHOT_INCOMPLETE",
        message="OpenD option snapshot is complete and refreshed." if source_ok else "OpenD option snapshot is unavailable or incomplete.",
        observed={
            "complete": snapshot.complete,
            "refresh_cache": snapshot.refresh_cache,
            "environment": snapshot.environment,
            "row_count": len(snapshot.rows),
        },
        expected={"complete": True, "refresh_cache": True, "environment": "REAL"},
        evidence_refs=[],
    )
    public_snapshot = snapshot.public_source_snapshot()
    mismatches = control_state.setdefault("position_mismatches", {})
    state_key = f"{market}:{account}"
    if not source_ok:
        mismatches.pop(state_key, None)
        convergence = check_result(
            check_id="OM-POS-002",
            status="unknown",
            scope=scope,
            observed_at_utc=observed_at_utc,
            reason_code="POSITION_CONVERGENCE_SOURCE_UNAVAILABLE",
            message="Position convergence cannot run without a complete fresh OpenD snapshot.",
            evidence_refs=[],
        )
        return (
            dataset_status(
                dataset_id="om.option_positions",
                scope=scope,
                status="unavailable",
                as_of_utc=observed_at_utc,
                checks=[source_check, convergence],
                source_snapshots=[public_snapshot],
                blocked_consumers=["option_position_report", "lifecycle", "close_advice"],
                blocked_by=["OM-POS-001", "OM-POS-002"],
                reason_codes=[source_check["reason_code"], convergence["reason_code"]],
            ),
            control_state,
        )

    local, local_errors = normalize_local_positions(
        local_lots,
        account=account,
        market=market,
    )
    broker, broker_errors = normalize_opend_positions(snapshot.rows, market=market)
    normalization_errors = [*local_errors, *broker_errors]
    raw_comparison = {
        key: {
            "local": format(local.get(key, Decimal(0)), "f"),
            "opend": format(broker.get(key, Decimal(0)), "f"),
        }
        for key in sorted(set(local) | set(broker))
        if local.get(key, Decimal(0)) != broker.get(key, Decimal(0))
    }
    contract_terms_drifts = _contract_terms_drifts(
        local=local,
        broker=broker,
        broker_rows=snapshot.rows,
        raw_comparison=raw_comparison,
    )
    lifecycle_coverage, lifecycle_case_ids = _pending_lifecycle_coverage(
        cases=list(lifecycle_cases or []),
        read_models_by_case=dict(lifecycle_read_models_by_case or {}),
        timing_policies_by_case=dict(lifecycle_timing_policies_by_case or {}),
        account=account,
        market=market,
        now=now,
    )
    expected_lifecycle_pending = {
        key: {
            **values,
            "covered_quantity": format(lifecycle_coverage[key], "f"),
            "lifecycle_case_count": len(lifecycle_case_ids.get(key) or []),
        }
        for key, values in raw_comparison.items()
        if lifecycle_coverage.get(key)
        == local.get(key, Decimal(0)) - broker.get(key, Decimal(0))
    }
    comparison = {
        key: values
        for key, values in raw_comparison.items()
        if key not in expected_lifecycle_pending
    }
    local_fingerprint = sha256_json(
        {key: format(value, "f") for key, value in sorted(local.items())}
    )
    opend_fingerprint = sha256_json(
        {key: format(value, "f") for key, value in sorted(broker.items())}
    )
    classified_comparison = raw_comparison if contract_terms_drifts else comparison
    mismatch_fingerprint = sha256_json(classified_comparison)
    evidence = evidence_ref(
        kind="option-position-reconciliation",
        observed_at_utc=observed_at_utc,
        value={
            "account": account,
            "local_contract_groups": len(local),
            "opend_contract_groups": len(broker),
            "observed_mismatch_count": len(raw_comparison),
            "mismatch_count": len(classified_comparison),
            "expected_lifecycle_pending_count": len(expected_lifecycle_pending),
            "mismatch_fingerprint": mismatch_fingerprint,
            "normalization_error_count": len(normalization_errors),
            "local_normalization_error_count": len(local_errors),
            "opend_normalization_error_count": len(broker_errors),
            "contract_terms_drift_count": len(contract_terms_drifts),
        },
        artifact_ref=f"om-evidence:position-reconciliation:{account}",
    )
    if normalization_errors:
        mismatches.pop(state_key, None)
        convergence = check_result(
            check_id="OM-POS-002",
            status="unknown",
            scope=scope,
            observed_at_utc=observed_at_utc,
            reason_code="POSITION_IDENTITY_INCOMPLETE",
            message="Required option identity or multiplier evidence is missing.",
            observed={
                "normalization_error_count": len(normalization_errors),
                "local_normalization_error_count": len(local_errors),
                "opend_normalization_error_count": len(broker_errors),
            },
            expected={
                "normalization_error_count": 0,
                "local_normalization_error_count": 0,
                "opend_normalization_error_count": 0,
            },
            evidence_refs=[evidence],
        )
        verdict = "unavailable"
    elif raw_comparison and not lifecycle_coherent_read_available:
        mismatches.pop(state_key, None)
        convergence = check_result(
            check_id="OM-POS-002",
            status="unknown",
            scope=scope,
            observed_at_utc=observed_at_utc,
            reason_code="POSITION_LIFECYCLE_COHERENT_READ_UNAVAILABLE",
            message=(
                "Position convergence cannot classify a mismatch without "
                "an account-coherent lifecycle read."
            ),
            observed={
                "observed_mismatch_count": len(raw_comparison),
                "lifecycle_coherent_read_available": False,
            },
            expected={"lifecycle_coherent_read_available": True},
            evidence_refs=[evidence],
        )
        verdict = "unavailable"
    elif contract_terms_drifts:
        mismatches[state_key] = {
            "kind": "contract_terms_drift",
            "fingerprint": mismatch_fingerprint,
            "first_seen_at_utc": observed_at_utc,
            "last_seen_at_utc": observed_at_utc,
            "next_recheck_at_utc": None,
            "mismatch_count": len(raw_comparison),
            "contract_terms_drift_count": len(contract_terms_drifts),
        }
        convergence = check_result(
            check_id="OM-POS-002",
            status="fail",
            scope=scope,
            observed_at_utc=observed_at_utc,
            reason_code="POSITION_CONTRACT_TERMS_DRIFT",
            message=(
                "OpenD current option terms differ from the canonical ledger; "
                "reconcile the affected lot before position reports, lifecycle, "
                "or Close Advice consume it."
            ),
            observed={
                "mismatch_count": len(raw_comparison),
                "contract_terms_drift_count": len(contract_terms_drifts),
                "contract_terms_drifts": contract_terms_drifts,
            },
            expected={
                "mismatch_count": 0,
                "contract_terms_drift_count": 0,
            },
            evidence_refs=[evidence],
        )
        verdict = "untrusted"
    elif expected_lifecycle_pending and not comparison:
        mismatches.pop(state_key, None)
        convergence = check_result(
            check_id="OM-POS-002",
            status="warn",
            scope=scope,
            observed_at_utc=observed_at_utc,
            reason_code="POSITIONS_PENDING_LIFECYCLE",
            message=(
                "Canonical-only option quantities are exactly covered by active "
                "lifecycle cases within their immutable deadlines."
            ),
            observed={
                "mismatch_count": 0,
                "observed_mismatch_count": len(raw_comparison),
                "expected_lifecycle_pending_count": len(
                    expected_lifecycle_pending
                ),
            },
            expected={"mismatch_count": 0},
            evidence_refs=[evidence],
        )
        verdict = "partial"
    elif not comparison:
        mismatches.pop(state_key, None)
        convergence = check_result(
            check_id="OM-POS-002",
            status="pass",
            scope=scope,
            observed_at_utc=observed_at_utc,
            reason_code="POSITIONS_RECONCILED",
            message="Canonical ledger and OpenD option positions agree.",
            observed={"normalized_contract_groups": len(local), "mismatch_count": 0},
            expected={"mismatch_count": 0},
            evidence_refs=[evidence],
        )
        verdict = "trusted"
    else:
        prior = mismatches.get(state_key) if isinstance(mismatches.get(state_key), dict) else {}
        first_seen = (
            str(prior.get("first_seen_at_utc") or "")
            if prior.get("fingerprint") == mismatch_fingerprint
            else observed_at_utc
        )
        try:
            first_seen_dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        except ValueError:
            first_seen_dt = now.astimezone(timezone.utc)
            first_seen = utc_iso(first_seen_dt)
        age = max(0.0, (now.astimezone(timezone.utc) - first_seen_dt.astimezone(timezone.utc)).total_seconds())
        persistent = bool(day_end_strict or age >= persistent_after_seconds)
        next_recheck = None if persistent else now.astimezone(timezone.utc) + timedelta(seconds=60 if age < 60 else 300 - age)
        mismatches[state_key] = {
            "fingerprint": mismatch_fingerprint,
            "first_seen_at_utc": first_seen,
            "last_seen_at_utc": observed_at_utc,
            "next_recheck_at_utc": utc_iso(next_recheck) if next_recheck else None,
            "mismatch_count": len(comparison),
        }
        convergence = check_result(
            check_id="OM-POS-002",
            status="fail" if persistent else "warn",
            scope=scope,
            observed_at_utc=observed_at_utc,
            reason_code="POSITION_DIVERGENCE_PERSISTENT" if persistent else "POSITION_DIVERGENCE_TRANSIENT",
            message=(
                "OpenD and canonical option positions still diverge after the convergence window."
                if persistent
                else "OpenD and canonical option positions differ within the read-only convergence window."
            ),
            observed={"mismatch_count": len(comparison), "mismatch_age_seconds": age},
            expected={"mismatch_count": 0},
            thresholds={"persistent_after_seconds": persistent_after_seconds, "day_end_strict": day_end_strict},
            evidence_refs=[evidence],
        )
        verdict = "untrusted" if persistent else "partial"
    blocked = verdict in {"untrusted", "unavailable"}
    return (
        dataset_status(
            dataset_id="om.option_positions",
            scope=scope,
            status=verdict,
            as_of_utc=observed_at_utc,
            checks=[source_check, convergence],
            evidence_refs=[evidence] if source_ok else [],
            source_snapshots=[public_snapshot],
            freshness_value=freshness(
                observed_at_utc=snapshot.observed_at_utc,
                status="fresh" if source_ok else "unknown",
                age_seconds=max(
                    0.0,
                    (
                        now.astimezone(timezone.utc)
                        - datetime.fromisoformat(snapshot.observed_at_utc.replace("Z", "+00:00"))
                    ).total_seconds(),
                ),
                grace_seconds=persistent_after_seconds,
            ),
            usable_for=[] if blocked else ["option_position_report", "lifecycle", "close_advice"],
            blocked_consumers=["option_position_report", "lifecycle", "close_advice"] if blocked else [],
            blocked_by=[item["check_id"] for item in (source_check, convergence) if item["status"] in {"fail", "unknown"}],
            reason_codes=[item["reason_code"] for item in (source_check, convergence) if item["status"] != "pass"],
            extensions={
                "convergence": dict(mismatches.get(state_key) or {}),
                "local_position_fingerprint": local_fingerprint,
                "opend_position_fingerprint": opend_fingerprint,
                **(
                    {
                        "next_authoritative_refresh_due_utc": (
                            next_authoritative_refresh_due_utc
                        )
                    }
                    if next_authoritative_refresh_due_utc
                    else {}
                ),
            },
        ),
        control_state,
    )


def build_opend_runtime_check(
    *,
    snapshot: OpenDOptionSnapshot,
    observed_at_utc: str,
) -> dict[str, Any]:
    ok = snapshot.complete and snapshot.refresh_cache and snapshot.environment == "REAL"
    return check_result(
        check_id="RT-OM-004",
        status="pass" if ok else "unknown",
        scope={"account": snapshot.account, "market": snapshot.market, "source": "futu-opend"},
        observed_at_utc=observed_at_utc,
        reason_code="OPEND_AUTHORITATIVE_QUERY_OK" if ok else snapshot.error_code or "OPEND_AUTHORITATIVE_QUERY_FAILED",
        message="OpenD authoritative read completed." if ok else "OpenD authoritative read failed; cached evidence is not used.",
        observed={
            "complete": snapshot.complete,
            "refresh_cache": snapshot.refresh_cache,
            "environment": snapshot.environment,
        },
        expected={"complete": True, "refresh_cache": True, "environment": "REAL"},
        evidence_refs=[],
    )


__all__ = [
    "build_opend_runtime_check",
    "build_position_dataset",
    "normalize_local_positions",
    "normalize_opend_positions",
]
