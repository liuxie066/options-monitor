from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.engine import rank_candidate_rows
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.performance.models import select_fx_rate
from domain.domain.short_vol_assessment import calculate_option_market_concentration_after
from domain.domain.symbol_identity import OPTION_CODE_RE, resolve_symbol_identity
from src.application.opening_candidate_snapshot import ranked_opening_candidate_decisions
from src.application.performance.account_fee_plan import load_account_fee_plan_receipt
from src.application.research.formal_corpus import (
    FormalCorpusError,
    load_formal_expectation,
    load_formal_point,
    read_expectation_bound_market_calendar_snapshot,
    read_market_calendar_binding,
)
from src.application.strategy_lab.contracts import (
    ACCOUNT,
    MARKET,
    NEAR_RETURN_THRESHOLDS,
    RECIPE_ID,
    RESEARCH_SESSIONS,
    canonical_sha256,
)
from src.application.strategy_lab.readiness import (
    HistoryKReadinessError,
    build_history_k_probe_request,
    read_history_k_readiness_receipt,
)
from src.infrastructure.performance_evidence_sqlite import (
    PerformanceEvidenceSQLiteRepository,
)


_HK_TZ = ZoneInfo("Asia/Hong_Kong")


class StrategyLabRecipeError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _blocked(reason_code: str, message: str, **facts: Any) -> dict[str, Any]:
    return {
        "status": "blocked",
        "blockers": [{"reason_code": reason_code, "message": message}],
        **facts,
    }


def describe_recipe(recipe_id: str) -> dict[str, Any]:
    if recipe_id != RECIPE_ID:
        raise StrategyLabRecipeError("recipe_unsupported", "Recipe is not supported")
    return {
        "recipe_id": RECIPE_ID,
        "name": "Sell Put option-position concentration",
        "question": "降低近收益候选的期权持仓市值集中度，能否改善单推荐结果？",
        "market": MARKET,
        "account": ACCOUNT,
        "strategy": "sell_put",
        "research_sessions": RESEARCH_SESSIONS,
        "near_return_thresholds": list(NEAR_RETURN_THRESHOLDS),
        "evidence": [
            "formal_corpus",
            "sealed_opening_candidate_snapshot",
            "prepared_option_position_evidence",
            "targeted_history_k_readiness",
            "terminal_fx",
            "account_fee_plan",
        ],
    }


def _canonical_hk_put_candidate(candidate: Mapping[str, Any]) -> None:
    contract_value = candidate.get("contract_symbol")
    if not isinstance(contract_value, str):
        raise ValueError("contract_symbol is unavailable")
    contract = contract_value.strip().upper()
    match = OPTION_CODE_RE.fullmatch(contract)
    contract_identity = resolve_symbol_identity(contract)
    symbol_identity = resolve_symbol_identity(candidate.get("symbol"))
    if (
        contract_value != contract
        or match is None
        or match.group("market") != "HK"
        or match.group("cp") != "P"
        or contract_identity is None
        or contract_identity.market != "HK"
        or contract_identity.currency != "HKD"
        or symbol_identity is None
        or symbol_identity.canonical != contract_identity.canonical
        or candidate.get("symbol") != contract_identity.canonical
        or candidate.get("option_type") != "put"
        or candidate.get("currency") != "HKD"
    ):
        raise ValueError("candidate is not one canonical HK Put contract")
    try:
        code_expiration = date(
            2000 + int(match.group("yy")),
            int(match.group("mm")),
            int(match.group("dd")),
        ).isoformat()
        code_strike = Decimal(match.group("strike")) / Decimal("1000")
        candidate_strike = Decimal(str(candidate["strike"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("candidate contract identity is incomplete") from exc
    if candidate.get("expiration") != code_expiration or candidate_strike != code_strike:
        raise ValueError("candidate contract fields do not match contract_symbol")


def _candidate_rows(formal_point: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recommendation = formal_point.get("recommendation_point")
    opening = formal_point.get("opening_snapshot")
    evidence = formal_point.get("option_position_evidence_binding")
    if not all(isinstance(value, Mapping) for value in (recommendation, opening, evidence)):
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "formal point owners are incomplete")
    assert isinstance(recommendation, Mapping)
    assert isinstance(opening, Mapping)
    assert isinstance(evidence, Mapping)
    if recommendation.get("opening_snapshot_sha256") != opening.get("content_sha256"):
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "formal point opening snapshot binding changed")
    accepted_ids = recommendation.get("producer_accepted_candidate_ids")
    if not isinstance(accepted_ids, list) or not accepted_ids:
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "formal point has no accepted Sell Put candidate")
    decisions = [
        item
        for item in ranked_opening_candidate_decisions(opening)
        if item.get("strategy_mode") == "put" and (item.get("opening_decision") or {}).get("accepted") is True
    ]
    decision_ids = [item.get("candidate_id") for item in decisions]
    if len(decision_ids) != len(set(decision_ids)) or set(decision_ids) != set(accepted_ids):
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "sealed accepted candidate set changed")
    rows: list[dict[str, Any]] = []
    try:
        for decision in decisions:
            normalized = decision.get("normalized_input")
            if not isinstance(normalized, Mapping):
                raise ValueError("candidate normalized input is unavailable")
            candidate = {"candidate_id": decision["candidate_id"], **dict(normalized)}
            _canonical_hk_put_candidate(candidate)
            metric = calculate_option_market_concentration_after(
                candidate=candidate,
                open_option_positions=[dict(row) for row in evidence["open_option_positions"]],
                valuation_mark_facts=[dict(row) for row in evidence["valuation_mark_facts"]],
                fx_rate_facts=[dict(row) for row in evidence["fx_rate_facts"]],
            )
            rows.append(
                {
                    **candidate,
                    "opening_snapshot_rank": decision["opening_snapshot_rank"],
                    "option_market_concentration_after": metric["option_market_concentration_after"],
                    "option_market_value_cny": metric["option_market_value_cny"],
                    "option_market_concentration_metric_version": metric["metric_version"],
                    "option_market_evidence_refs": {
                        "prepared_context_manifest_ref": recommendation["prepared_context_manifest_ref"],
                        "prepared_context_manifest_sha256": recommendation["prepared_context_manifest_sha256"],
                        "prepared_context_payload_sha256": recommendation["prepared_context_payload_sha256"],
                        **metric["evidence_refs"],
                    },
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyLabRecipeError("recipe_evidence_incomplete", f"candidate evidence is incomplete: {exc}") from exc
    baseline = [row for row in rows if row["opening_snapshot_rank"] == 1]
    if len(baseline) != 1 or baseline[0]["candidate_id"] not in accepted_ids:
        raise StrategyLabRecipeError("recipe_evidence_incomplete", "sealed production rank-1 candidate is unavailable")
    return baseline[0], rows


def _arm(kind: str, candidate: Mapping[str, Any], threshold: float | None) -> dict[str, Any]:
    return {
        "arm_id": kind if threshold is None else f"{kind}_{threshold:.3f}",
        "kind": kind,
        "near_return_threshold": threshold,
        "candidate_id": candidate["candidate_id"],
        "candidate": dict(candidate),
    }


def _opening_fx_binding(formal_point: Mapping[str, Any]) -> dict[str, Any]:
    evidence = formal_point.get("option_position_evidence_binding")
    rows = evidence.get("fx_rate_facts") if isinstance(evidence, Mapping) else None
    matches = [
        dict(row)
        for row in rows or []
        if isinstance(row, Mapping)
        and row.get("base_currency") == "HKD"
        and row.get("quote_currency") == "CNY"
    ]
    if len(matches) != 1:
        raise StrategyLabRecipeError(
            "recipe_evidence_incomplete",
            "formal point must bind exactly one HKD/CNY opening FX fact",
        )
    fact = matches[0]
    fact_id = fact.get("fact_id")
    source_hash = fact.get("source_fact_sha256")
    if (
        not isinstance(fact_id, str)
        or not fact_id
        or not isinstance(source_hash, str)
        or len(source_hash) != 64
    ):
        raise StrategyLabRecipeError(
            "recipe_evidence_incomplete",
            "formal point opening FX identity is incomplete",
        )
    return {
        "fact": fact,
        "fact_ref": {"kind": "formal_point_fx_rate", "fact_id": fact_id},
        "fact_sha256": canonical_sha256(fact),
        "source_fact_sha256": source_hash,
    }


def _recommendation_available_at_utc(formal_point: Mapping[str, Any]) -> str:
    try:
        recommendation = formal_point["recommendation_point"]
        opening = formal_point["opening_snapshot"]
        coherence = recommendation["formal_point_time_coherence"]
        values = (
            formal_point["captured_at_utc"],
            formal_point["source_binding"]["scheduled_scan_target_market"],
            recommendation["scheduled_scan_target_market"],
            recommendation["decision_at_utc"],
            opening["sealed_at_utc"],
            coherence["maximum_observed_at_utc"],
        )
        parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StrategyLabRecipeError(
            "recipe_evidence_incomplete", "formal point availability time is incomplete"
        ) from exc
    if any(value.tzinfo is None for value in parsed):
        raise StrategyLabRecipeError(
            "recipe_evidence_incomplete", "formal point availability time lacks a timezone"
        )
    return max(value.astimezone(timezone.utc) for value in parsed).isoformat().replace(
        "+00:00", "Z"
    )


def build_concentration_arms(
    formal_point: Mapping[str, Any],
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    parameters = dict(parameters or {})
    baseline, candidates = _candidate_rows(formal_point)
    arms = [_arm("baseline", baseline, None)]
    candidate_ids = {str(row["candidate_id"]) for row in candidates}
    for threshold in NEAR_RETURN_THRESHOLDS:
        ranked = rank_candidate_rows(
            candidates,
            mode="put",
            sell_put_ranking_profile="option_market_concentration",
            near_return_threshold=threshold,
        )
        if {str(row["candidate_id"]) for row in ranked} != candidate_ids:
            raise StrategyLabRecipeError("recipe_evidence_incomplete", "Recipe reranking changed the accepted set")
        arms.append(_arm("challenger", ranked[0], threshold))
    return {
        "recommendation_point_id": formal_point["recommendation_point_id"],
        "scheduled_scan_target_market": formal_point["recommendation_point"][
            "scheduled_scan_target_market"
        ],
        "recommendation_available_at_utc": _recommendation_available_at_utc(formal_point),
        "formal_point_ref": parameters.get("formal_point_ref"),
        "formal_point_content_sha256": formal_point["content_sha256"],
        "formal_point_file_sha256": parameters.get("formal_point_file_sha256"),
        "source_commit_sha": formal_point["recommendation_point"]["source_commit_sha"],
        "opening_fx_binding": _opening_fx_binding(formal_point),
        "accepted_candidate_ids": sorted(candidate_ids),
        "arms": arms,
    }


def _expiration_is_mature(expiration: object, cutoff_ms: int) -> bool:
    observed = expiration_observation_start_ms(str(expiration or ""), "HK")
    return observed is not None and observed < cutoff_ms


def _after_cutoff(value: object, cutoff: datetime) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed.tzinfo is None or parsed.astimezone(timezone.utc) > cutoff


def _calendar_session(calendar: Mapping[str, Any], trading_date: str) -> dict[str, Any] | None:
    sessions = calendar.get("trading_sessions")
    if not isinstance(sessions, list):
        return None
    found = [
        dict(item)
        for item in sessions
        if isinstance(item, Mapping) and item.get("trading_date") == trading_date
    ]
    return found[0] if len(found) == 1 else None


def _load_window_day(
    context: Mapping[str, Any],
    trading_date: str,
    cutoff: datetime,
    cutoff_ms: int,
    calendar: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    runtime_root = context["runtime_root"]
    try:
        expectation = load_formal_expectation(
            runtime_root,
            market="HK",
            account=ACCOUNT,
            trading_date=trading_date,
        )
    except FormalCorpusError as exc:
        return None, exc.reason_code
    if expectation.get("status") != "available":
        return None, str(expectation.get("reason_code") or "formal_expectation_missing")
    expectation_payload = expectation["expectation"]
    if _after_cutoff(expectation_payload.get("sealed_at_utc"), cutoff):
        return None, "research_point_post_cutoff"
    try:
        bound_calendar = read_expectation_bound_market_calendar_snapshot(
            context["artifact_root"],
            market="HK",
            market_calendar_version=expectation_payload["market_calendar_version"],
            market_calendar_sha256=expectation_payload["market_calendar_sha256"],
        )
    except (FormalCorpusError, KeyError) as exc:
        return None, str(getattr(exc, "reason_code", "market_calendar_binding_unavailable"))
    bound_session = _calendar_session(bound_calendar, trading_date)
    current_session = _calendar_session(calendar, trading_date)
    if bound_session is None or current_session is None:
        return None, "market_calendar_binding_changed"
    if bound_session["trade_date_type"] != current_session["trade_date_type"]:
        return None, "market_calendar_session_changed"
    targets = expectation_payload["scheduled_scan_targets_market"]
    expected = expectation_payload["expected_recommendation_point_ids"]
    points: list[dict[str, Any]] = []
    for target, point_id in zip(targets, expected, strict=True):
        if _after_cutoff(target, cutoff):
            return None, "research_point_post_cutoff"
        try:
            loaded = load_formal_point(
                runtime_root,
                market="HK",
                account=ACCOUNT,
                trading_date=trading_date,
                recommendation_point_id=point_id,
            )
        except FormalCorpusError as exc:
            return None, exc.reason_code
        if loaded.get("status") != "available":
            return None, str(loaded.get("reason_code") or "formal_point_evidence_missing")
        point = loaded["point"]
        recommendation = point["recommendation_point"]
        opening = point["opening_snapshot"]
        coherence = recommendation["formal_point_time_coherence"]
        authoritative_times = (
            point["captured_at_utc"],
            point["source_binding"]["scheduled_scan_target_market"],
            recommendation["scheduled_scan_target_market"],
            recommendation["decision_at_utc"],
            opening["sealed_at_utc"],
            coherence["maximum_observed_at_utc"],
        )
        if any(_after_cutoff(value, cutoff) for value in authoritative_times):
            return None, "research_point_post_cutoff"
        try:
            arms = build_concentration_arms(
                point,
                {
                    "formal_point_ref": loaded["artifact_ref"],
                    "formal_point_file_sha256": loaded["artifact_file_sha256"],
                },
            )
        except StrategyLabRecipeError as exc:
            return None, exc.reason_code
        if any(not _expiration_is_mature(arm["candidate"].get("expiration"), cutoff_ms) for arm in arms["arms"]):
            return None, "research_outcome_immature"
        points.append(arms)
    return (
        {
            "trading_date": trading_date,
            "expectation_ref": expectation["artifact_ref"],
            "expectation_content_sha256": expectation["artifact_content_sha256"],
            "expectation_file_sha256": expectation["artifact_file_sha256"],
            "market_calendar_binding": {
                "market_calendar_version": bound_calendar["market_calendar_version"],
                "snapshot_ref": bound_calendar["snapshot_ref"],
                "snapshot_content_sha256": bound_calendar["snapshot_content_sha256"],
                "snapshot_file_sha256": bound_calendar["snapshot_file_sha256"],
                "session": bound_session,
            },
            "points": points,
        },
        None,
    )


def select_research_window(
    context: Mapping[str, Any],
    maturity_cutoff_utc: str,
) -> dict[str, Any]:
    try:
        cutoff = datetime.fromisoformat(maturity_cutoff_utc.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
        calendar = read_market_calendar_binding(context["artifact_root"], market="HK")
    except Exception as exc:
        return _blocked("market_calendar_binding_unavailable", str(exc), sessions=[])
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cutoff_date = cutoff.astimezone(_HK_TZ).date().isoformat()
    dates = [value for value in calendar["trading_dates"] if value <= cutoff_date]
    if len(dates) < RESEARCH_SESSIONS:
        return _blocked("research_corpus_warming", "fewer than 20 trading sessions", sessions=[])
    skipped_newer_suffix = 0
    skipped_suffix_reason = "research_outcome_immature"
    for end in range(len(dates) - 1, RESEARCH_SESSIONS - 2, -1):
        selected_dates = dates[end - RESEARCH_SESSIONS + 1 : end + 1]
        sessions: list[dict[str, Any]] = []
        invalid: list[tuple[int, str]] = []
        for index, trading_date in enumerate(selected_dates):
            day, reason = _load_window_day(
                context,
                trading_date,
                cutoff,
                cutoff_ms,
                calendar,
            )
            if day is not None:
                sessions.append(day)
            else:
                invalid.append((index, reason or "research_window_coverage_missing"))
        if invalid:
            first_invalid = invalid[0][0]
            if (
                [index for index, _reason in invalid]
                == list(range(first_invalid, RESEARCH_SESSIONS))
                and all(
                    reason in {"research_outcome_immature", "research_point_post_cutoff"}
                    for _index, reason in invalid
                )
            ):
                skipped_newer_suffix += 1
                if any(reason == "research_point_post_cutoff" for _index, reason in invalid):
                    skipped_suffix_reason = "research_point_post_cutoff"
                continue
            index, reason = invalid[0]
            return _blocked(
                reason,
                f"research session is incomplete: {selected_dates[index]}",
                sessions=sessions,
                selected_trading_dates=selected_dates,
            )
        return {
            "status": "available",
            "blockers": [],
            "selected_trading_dates": selected_dates,
            "ignored_immature_window_count": skipped_newer_suffix,
            "market_calendar": {
                "market_calendar_version": calendar["market_calendar_version"],
                "snapshot_ref": calendar["snapshot_ref"],
                "snapshot_content_sha256": calendar["snapshot_content_sha256"],
                "snapshot_file_sha256": calendar["snapshot_file_sha256"],
            },
            "sessions": sessions,
        }
    return _blocked(
        skipped_suffix_reason,
        "no mature 20-session research window is available",
        sessions=[],
    )


def _all_arms(window: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        arm
        for session in window.get("sessions", [])
        for point in session.get("points", [])
        for arm in point.get("arms", [])
    ]


def _terminal_fx_bindings(
    context: Mapping[str, Any],
    arms: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    bundle = PerformanceEvidenceSQLiteRepository(context["ledger_path"]).read_all()
    if bundle.schema_state != "initialized_v1":
        return [], [{"reason_code": "terminal_fx_unavailable", "message": bundle.schema_state}]
    bindings: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    identities = sorted({(str(arm["candidate"]["expiration"]), str(arm["candidate"]["currency"])) for arm in arms})
    for expiration, currency in identities:
        observation_ms = expiration_observation_start_ms(expiration, "HK")
        if observation_ms is None:
            blockers.append({"reason_code": "terminal_fx_unavailable", "message": f"invalid expiry: {expiration}"})
            continue
        selection = select_fx_rate(
            bundle.fx_rates,
            base_currency=currency,
            quote_currency="CNY",
            at_ms=observation_ms,
        )
        if selection.status != "selected" or selection.fact is None:
            blockers.append(
                {
                    "reason_code": "terminal_fx_unavailable",
                    "message": f"{currency} FX is {selection.status} at {expiration}",
                }
            )
            continue
        fact = selection.fact
        payload = fact.normalized_payload(include_fact_id=True)
        bindings.append(
            {
                "expiration": expiration,
                "currency": currency,
                "observation_start_ms": observation_ms,
                "fact_ref": {"kind": "fx_rate", "fact_id": fact.fact_id},
                "fact_sha256": canonical_sha256(payload),
                "fact": payload,
            }
        )
    return bindings, blockers


def _history_k_authority(
    context: Mapping[str, Any],
    arms: list[dict[str, Any]],
    *,
    maturity_cutoff_utc: str,
    occurred_at_utc: str,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    tuples: set[tuple[str, str, str]] = set()
    for arm in arms:
        candidate = arm["candidate"]
        contract = str(candidate.get("contract_symbol") or "").upper()
        identity = resolve_symbol_identity(contract)
        sample_date = str(candidate.get("sample_trading_date") or arm.get("trading_date") or "")
        if identity is None or identity.market != "HK" or not sample_date:
            return None, [{"reason_code": "history_k_projection_incomplete", "message": contract or "missing contract"}]
        tuples.add((identity.futu_code, contract, sample_date))
    ordered = sorted(tuples)
    if not ordered:
        return None, [{"reason_code": "history_k_projection_incomplete", "message": "no arms"}]
    representative = ordered[0]
    try:
        probe_request = build_history_k_probe_request(
            market="HK",
            account=ACCOUNT,
            opend_binding=context["opend_binding"],
            contract_symbol=representative[1],
            underlier_code=representative[0],
            sample_date=representative[2],
            as_of_utc=maturity_cutoff_utc,
        )
    except HistoryKReadinessError as exc:
        return None, [{"reason_code": exc.reason_code, "message": str(exc)}]
    probe_sha256 = canonical_sha256(probe_request)
    authority: dict[str, Any] = {
        "queries": [
            {"security_quota_identity": item[0], "contract_symbol": item[1], "sample_date": item[2]} for item in ordered
        ],
        "representative": {
            "security_quota_identity": representative[0],
            "contract_symbol": representative[1],
            "sample_date": representative[2],
        },
        "probe_request": probe_request,
        "probe_sha256": probe_sha256,
        "required_unique_security_identity_count": len({item[0] for item in ordered}),
        "quota_rule": "required_unique_security_identity_count <= provider_observation.quota.remain_quota",
    }
    try:
        receipt = read_history_k_readiness_receipt(
            context["artifact_root"],
            probe_sha256=probe_sha256,
            expected_opend_binding=context["opend_binding"],
            as_of_utc=occurred_at_utc,
        )
    except HistoryKReadinessError as exc:
        return authority, [{"reason_code": exc.reason_code, "message": str(exc)}]
    observation = receipt.get("provider_observation")
    quota = observation.get("quota") if isinstance(observation, Mapping) else None
    remaining = quota.get("remain_quota") if isinstance(quota, Mapping) else None
    ready = (
        isinstance(observation, Mapping)
        and observation.get("readiness_status") == "ready"
        and observation.get("pagination_complete") is True
        and observation.get("no_trade_bar_semantics_observed") is True
        and isinstance(quota, Mapping)
        and quota.get("sample_quota_code_counted") is True
        and quota.get("sample_quota_code") == representative[0]
        and type(remaining) is int
        and authority["required_unique_security_identity_count"] <= remaining
    )
    authority["receipt"] = {
        "receipt_ref": receipt.get("receipt_ref"),
        "content_sha256": receipt.get("content_sha256"),
        "receipt_file_sha256": receipt.get("receipt_file_sha256"),
        "observed_at_utc": receipt.get("observed_at_utc"),
        "expires_at_utc": receipt.get("expires_at_utc"),
        "observed_remaining_quota": remaining,
    }
    if not ready:
        return authority, [
            {"reason_code": "history_k_readiness_insufficient", "message": "targeted readiness proof is incomplete"}
        ]
    return authority, []


def check_recipe_readiness(
    context: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    occurred_at_utc: str,
) -> dict[str, Any]:
    window = select_research_window(context, str(request["maturity_cutoff_utc"]))
    blockers = list(window.get("blockers", []))
    try:
        fee_plan = load_account_fee_plan_receipt(Path(str(request["fee_plan_receipt_path"])))
        if (fee_plan["market"], fee_plan["account"]) != ("HK", ACCOUNT):
            raise ValueError("fee-plan identity changed")
    except Exception as exc:
        fee_plan = None
        blockers.append({"reason_code": "account_fee_plan_unavailable", "message": str(exc)})
    arms = _all_arms(window)
    for session in window.get("sessions", []):
        for point in session.get("points", []):
            for arm in point.get("arms", []):
                arm["trading_date"] = session["trading_date"]
                arm["candidate"]["sample_trading_date"] = session["trading_date"]
    terminal_fx, fx_blockers = _terminal_fx_bindings(context, arms) if arms else ([], [])
    blockers.extend(fx_blockers)
    history_k, history_blockers = (
        _history_k_authority(
            context,
            arms,
            maturity_cutoff_utc=str(request["maturity_cutoff_utc"]),
            occurred_at_utc=occurred_at_utc,
        )
        if arms
        else (None, [])
    )
    blockers.extend(history_blockers)
    return {
        "status": "available" if not blockers else "blocked",
        "blockers": blockers,
        "window": window,
        "fee_plan": fee_plan,
        "terminal_fx_bindings": terminal_fx,
        "history_k_authority": history_k,
    }


RECIPES = {RECIPE_ID: build_concentration_arms}


__all__ = [
    "RECIPES",
    "StrategyLabRecipeError",
    "build_concentration_arms",
    "check_recipe_readiness",
    "describe_recipe",
    "select_research_window",
]
