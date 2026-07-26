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
from domain.domain.symbol_identity import OPTION_CODE_RE, canonical_symbol
from domain.domain.trade_contract_identity import normalize_contract_expiration
from src.application.quality.model import (
    check_result,
    dataset_status,
    evidence_ref,
    freshness,
    sha256_json,
    utc_iso,
)
from src.infrastructure.quality.opend_position_adapter import OpenDOptionSnapshot


def _decimal(value: Any) -> Decimal | None:
    try:
        if value in (None, "", "-"):
            return None
        return Decimal(str(value)).normalize()
    except (InvalidOperation, TypeError, ValueError):
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


def normalize_local_positions(rows: list[dict[str, Any]], *, account: str) -> tuple[dict[str, Decimal], list[str]]:
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


def normalize_opend_positions(rows: list[dict[str, Any]]) -> tuple[dict[str, Decimal], list[str]]:
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    errors: list[str] = []
    for index, row in enumerate(rows):
        code = str(row.get("code") or row.get("symbol") or row.get("stock_code") or "").strip().upper()
        match = OPTION_CODE_RE.match(code)
        qty = _decimal(row.get("qty") if "qty" in row else row.get("quantity"))
        multiplier = _decimal(
            row.get("options_per_contract")
            if "options_per_contract" in row
            else row.get("contract_multiplier")
            if "contract_multiplier" in row
            else row.get("lot_size")
            if "lot_size" in row
            else row.get("multiplier")
        )
        side = _position_side(
            row.get("position_side") if "position_side" in row else row.get("side"),
            qty=qty,
        )
        if match is None or qty is None or multiplier is None or side is None:
            errors.append(f"row-{index}")
            continue
        symbol = canonical_symbol(code)
        strike = _decimal(Decimal(match.group("strike")) / Decimal("1000"))
        expiration = f"20{match.group('yy')}-{match.group('mm')}-{match.group('dd')}"
        if not symbol or strike is None:
            errors.append(f"row-{index}")
            continue
        key = _contract_key(
            symbol=symbol,
            option_type="call" if match.group("cp") == "C" else "put",
            expiration=expiration,
            strike=strike,
            multiplier=multiplier,
        )
        quantities[key] += abs(qty) if side == "long" else -abs(qty)
    return {key: value for key, value in quantities.items() if value != 0}, errors


def build_position_dataset(
    *,
    snapshot: OpenDOptionSnapshot,
    local_lots: list[dict[str, Any]],
    account: str,
    market: str,
    observed_at_utc: str,
    now: datetime,
    control_state: dict[str, Any],
    day_end_strict: bool = False,
    persistent_after_seconds: int = 300,
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

    local, local_errors = normalize_local_positions(local_lots, account=account)
    broker, broker_errors = normalize_opend_positions(snapshot.rows)
    normalization_errors = [*local_errors, *broker_errors]
    comparison = {
        key: {
            "local": format(local.get(key, Decimal(0)), "f"),
            "opend": format(broker.get(key, Decimal(0)), "f"),
        }
        for key in sorted(set(local) | set(broker))
        if local.get(key, Decimal(0)) != broker.get(key, Decimal(0))
    }
    mismatch_fingerprint = sha256_json(comparison)
    evidence = evidence_ref(
        kind="option-position-reconciliation",
        observed_at_utc=observed_at_utc,
        value={
            "account": account,
            "local_contract_groups": len(local),
            "opend_contract_groups": len(broker),
            "mismatch_count": len(comparison),
            "mismatch_fingerprint": mismatch_fingerprint,
            "normalization_error_count": len(normalization_errors),
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
            observed={"normalization_error_count": len(normalization_errors)},
            expected={"normalization_error_count": 0},
            evidence_refs=[evidence],
        )
        verdict = "unavailable"
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
