from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

import pandas as pd

from domain.domain.engine import (
    STAGE_INPUT_NORMALIZATION,
    build_candidate_decision,
)
from domain.domain.engine.candidate_engine import (
    REJECT_CONTRACT_INELIGIBLE,
    REJECT_EVIDENCE_UNAVAILABLE,
    REJECT_INPUT_INVALID,
    REJECT_INPUT_MISSING,
    REJECT_POLICY_REJECTED,
    REJECT_RISK_EARNINGS_UNAVAILABLE,
)
from src.application.candidate_models import CandidateBaseValues, CandidateContractInput
from src.application.earnings_calendar import annotate_candidates_with_earnings_evidence

_DEFINITIVE_CALCULATION_REASONS = frozenset({"net_premium_non_positive"})


@dataclass(frozen=True)
class CandidateScanConfig:
    """Application inputs for building a calculable opening-candidate universe.

    Strategy thresholds remain in this compatibility-shaped object because the
    human scan CLI still accepts them.  The scanner deliberately does not apply
    them: the sole formal gate and ranking live in Candidate Engine and are
    invoked after earnings, currency and account-capacity facts are attached.
    """

    mode: str
    symbols: list[str]
    input_root: Path
    min_dte: int
    max_dte: int
    min_strike: float | None
    max_strike: float | None
    min_open_interest: float | None
    min_volume: float | None
    max_spread_ratio: float | None
    min_annualized_net_return: float | None
    min_net_income: float
    reject_stage: str = "candidate_calculation"
    strategy_family: str | None = None
    strategy_profile: str | None = None


@dataclass(frozen=True)
class CandidateScanDependencies:
    compute_metrics_fn: Callable[[CandidateContractInput], dict[str, Any] | None]
    build_row_fn: Callable[
        [CandidateContractInput, CandidateBaseValues, dict[str, Any]],
        dict[str, Any] | None,
    ]
    metric_reject_reason_fn: Callable[[CandidateContractInput], dict[str, Any] | None] | None = None


def _load_required_data_rows(*, input_root: Path, symbol: str, mode: str) -> pd.DataFrame:
    path = Path(input_root) / "parsed" / f"{symbol}_required_data.csv"
    try:
        df = pd.read_csv(path)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    if df.empty or "option_type" not in df.columns:
        return pd.DataFrame()
    return cast(pd.DataFrame, df.loc[df["option_type"] == mode].copy())


def _base_values(
    contract: CandidateContractInput,
    metrics: dict[str, Any],
) -> CandidateBaseValues | None:
    if contract.dte is None or contract.strike is None:
        return None
    return CandidateBaseValues(
        dte=int(contract.dte),
        strike=float(contract.strike),
        open_interest=contract.open_interest,
        volume=contract.volume,
        spread=_optional_float(metrics.get("spread")),
        spread_ratio=_optional_float(metrics.get("spread_ratio")),
    )


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _calculation_decision_record(
    *,
    contract: CandidateContractInput,
    config: CandidateScanConfig,
    reason: dict[str, Any] | None,
) -> dict[str, Any]:
    detail = dict(reason or {})
    specific_reason = str(
        detail.get("rule") or "candidate_metrics_unavailable"
    )
    normalized_input = contract.to_gate_payload()
    opening_status = str(
        normalized_input.get("opening_contract_status") or ""
    ).strip().lower()
    if opening_status != "ready":
        reject_reason = specific_reason
        if not opening_status:
            reject_reason = REJECT_INPUT_INVALID
    elif specific_reason in _DEFINITIVE_CALCULATION_REASONS:
        reject_reason = REJECT_POLICY_REJECTED
    else:
        reject_reason = REJECT_INPUT_INVALID
    opening_decision = build_candidate_decision(
        mode=config.mode,
        symbol=contract.symbol,
        contract_symbol=contract.contract_symbol,
        accepted=False,
        rejects=[
            {
                "stage": STAGE_INPUT_NORMALIZATION,
                "reason": reject_reason,
                "message": str(
                    detail.get("message") or "candidate metrics unavailable"
                ),
                "metric_value": {
                    "reason_code": specific_reason,
                    "metric_value": detail.get("metric_value"),
                },
                "threshold": detail.get("threshold"),
            }
        ],
        normalized_input=normalized_input,
    )
    return {
        "normalized_input": normalized_input,
        "opening_decision": opening_decision,
    }


def run_candidate_scan(
    *,
    config: CandidateScanConfig,
    deps: CandidateScanDependencies,
    calculation_decision_sink_fn: (
        Callable[[list[dict[str, Any]]], None] | None
    ) = None,
) -> pd.DataFrame:
    """Build normalized, calculable rows; do not filter or rank strategy policy."""

    rows: list[dict[str, Any]] = []
    calculation_decisions: list[dict[str, Any]] = []
    for symbol in config.symbols:
        data = _load_required_data_rows(
            input_root=config.input_root,
            symbol=symbol,
            mode=config.mode,
        )
        for _, raw_row in data.iterrows():
            contract = CandidateContractInput.from_row(raw_row, mode=config.mode)
            metrics = deps.compute_metrics_fn(contract)
            base_values = _base_values(contract, metrics or {})
            if not metrics or base_values is None:
                detail: dict[str, Any] | None = None
                if deps.metric_reject_reason_fn is not None:
                    try:
                        detail = deps.metric_reject_reason_fn(contract)
                    except Exception:
                        detail = None
                calculation_decisions.append(
                    _calculation_decision_record(
                        contract=contract,
                        config=config,
                        reason=detail,
                    )
                )
                continue
            candidate = deps.build_row_fn(contract, base_values, metrics)
            if candidate is not None:
                rows.append(candidate)

    out = pd.DataFrame(rows)
    if not out.empty:
        out = annotate_candidates_with_earnings_evidence(
            out,
            input_root=config.input_root,
        )

    if calculation_decision_sink_fn is not None:
        calculation_decision_sink_fn(calculation_decisions)
    return out


def evidence_summary_from_decisions(
    *,
    decisions: list[dict[str, Any]],
    accepted_count: int,
) -> dict[str, Any]:
    """Aggregate per-contract opening evidence into scope-level counters.

    Counters let the orchestration layer distinguish a genuine no-candidate
    outcome from evidence that could not even be evaluated, instead of
    projecting both as a normal zero-candidate scan.

    Accepts the raw decision payloads captured by the decision sink (each has
    an ``opening_decision`` mapping), so callers can compute the summary even
    after pandas operations drop DataFrame attrs.
    """

    ineligible = 0
    evidence_unavailable = 0
    diagnostic_gap_count = 0
    policy_rejected = 0
    unavailable_by_reason: dict[str, int] = {}
    diagnostic_gaps_by_reason: dict[str, int] = {}
    accepted_decisions = 0
    unavailable_reasons = {
        REJECT_EVIDENCE_UNAVAILABLE,
        REJECT_INPUT_INVALID,
        REJECT_INPUT_MISSING,
        REJECT_RISK_EARNINGS_UNAVAILABLE,
    }
    for record in decisions:
        if not isinstance(record, dict):
            raise ValueError("candidate evidence decision must be an object")
        decision = (
            record.get("opening_decision")
            if isinstance(record.get("opening_decision"), dict)
            else record
        )
        if not isinstance(decision, dict):
            raise ValueError("candidate opening decision must be an object")
        rejects = (decision or {}).get("rejects") or []
        reasons = [
            str(item.get("reason") or "")
            for item in rejects
            if isinstance(item, dict)
        ]
        if bool((decision or {}).get("accepted")):
            accepted_decisions += 1
            continue
        matched_unavailable_reasons = unavailable_reasons.intersection(reasons)
        definitive_reasons = set(reasons) - unavailable_reasons
        gap_codes: list[str] = []
        if matched_unavailable_reasons:
            diagnostic_gap_count += 1
            for item in rejects:
                if (
                    not isinstance(item, dict)
                    or str(item.get("reason") or "")
                    not in matched_unavailable_reasons
                ):
                    continue
                value = item.get("metric_value")
                code = None
                if isinstance(value, dict):
                    raw_codes = value.get("reason_codes")
                    if isinstance(raw_codes, (list, tuple)) and raw_codes:
                        code = str(raw_codes[0])
                    elif value.get("reason_code"):
                        code = str(value.get("reason_code"))
                gap_codes.append(code or "evidence_unavailable")
            for code in sorted(set(gap_codes)):
                diagnostic_gaps_by_reason[code] = (
                    diagnostic_gaps_by_reason.get(code, 0) + 1
                )
        if matched_unavailable_reasons and not definitive_reasons:
            evidence_unavailable += 1
            for code in sorted(set(gap_codes or ["evidence_unavailable"])):
                unavailable_by_reason[code] = unavailable_by_reason.get(code, 0) + 1
        elif REJECT_CONTRACT_INELIGIBLE in reasons:
            ineligible += 1
        else:
            policy_rejected += 1
    if accepted_decisions != accepted_count:
        raise ValueError("accepted candidate count does not match decision evidence")
    evaluated = len(decisions)
    return {
        "evaluated_contract_count": evaluated,
        "accepted_count": accepted_count,
        "contract_ineligible_count": ineligible,
        "policy_rejected_count": policy_rejected,
        "evidence_unavailable_count": evidence_unavailable,
        "eligibility_unresolved_count": evidence_unavailable,
        "diagnostic_evidence_gap_count": diagnostic_gap_count,
        "unavailable_by_reason": unavailable_by_reason,
        "diagnostic_gaps_by_reason": diagnostic_gaps_by_reason,
    }


def project_evidence_scan_status(
    *,
    evidence: dict[str, Any],
    candidate_count: int,
) -> tuple[str, str | None]:
    """Project unresolved eligibility evidence into the shared scan status."""

    unresolved = int(
        evidence.get(
            "eligibility_unresolved_count",
            evidence.get("evidence_unavailable_count") or 0,
        )
        or 0
    )
    if candidate_count > 0:
        return "completed", "partial_data" if unresolved > 0 else None
    if unresolved == 0:
        return "completed", "no_candidate"
    evaluated = int(evidence.get("evaluated_contract_count") or 0)
    if evaluated > 0 and unresolved < evaluated:
        return "completed", "partial_data"
    return "unavailable", "data_unavailable"
