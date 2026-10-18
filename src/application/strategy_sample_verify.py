"""Read-only, stage-local parity checks over a manifest-bound opening sample."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any

from domain.domain.engine import CandidateCalculationError
from domain.domain.engine.candidate_engine import STAGE_INPUT_NORMALIZATION
from domain.domain.strategy_core import decision_time_from_input, validate_strategy_identity
from om_strategy_core import (
    CallCapacityInputs,
    CurrencyConverter,
    ExchangeRates,
    PutCapacityInputs,
    calculate_capacity,
    calculate_metrics,
    evaluate_candidates,
    rank_candidates,
)
from src.application.candidate_evidence_history import load_candidate_snapshot_bundle_for_inspection
from src.application.candidate_snapshot_manifest import CandidateSnapshotManifestError
from src.application.opening_candidate_snapshot import candidate_universe_summary
from src.application.prepared_option_positions_context import (
    exchange_rate_scalars_from_option_context,
    load_prepared_option_positions_context,
)
from src.application.prepared_portfolio_context import load_prepared_portfolio_context

MATCHED = "matched"
MISMATCHED = "mismatched"
INSUFFICIENT = "insufficient_evidence"
_MISSING = object()


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isfinite(left) and math.isfinite(right) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _check(checks, category, location, recorded=_MISSING, recomputed=_MISSING, *, reason=None):
    item = {"category": category, "location": location}
    if reason is not None or recorded is _MISSING or recomputed is _MISSING:
        item.update(status=INSUFFICIENT, reason=reason or "recorded_field_missing")
    else:
        item["status"] = MATCHED if _equal(recorded, recomputed) else MISMATCHED
    if recorded is not _MISSING:
        item["recorded"] = recorded
    if recomputed is not _MISSING:
        item["recomputed"] = recomputed
    checks.append(item)


def _finish(report):
    counts = Counter(c["status"] for c in report["checks"])
    report["counts"] = {s: counts[s] for s in (MATCHED, MISMATCHED, INSUFFICIENT)}
    report["status"] = (
        MISMATCHED if counts[MISMATCHED] else INSUFFICIENT if counts[INSUFFICIENT] or not counts[MATCHED] else MATCHED
    )
    return report


def _contexts(base, snapshot):
    """Read bound prepared receipts, never live account or ledger surfaces."""
    contexts, errors = {}, {}
    loaders = {"portfolio": load_prepared_portfolio_context, "ledger": load_prepared_option_positions_context}
    for kind, loader in loaders.items():
        dependency = next((d for d in snapshot["dependencies"] if d["kind"] == kind), {})
        try:
            if not dependency.get("relpath"):
                raise ValueError(f"missing {kind} prepared receipt")
            contexts[kind] = loader(
                manifest_path=base / dependency["relpath"],
                expected_base=base,
                expected_run_id=snapshot["run_id"],
                expected_account=snapshot["account"],
                expected_account_config_sha256=snapshot["account_config_sha256"],
                expected_manifest_sha256=dependency["sha256"],
            )
            if not isinstance(contexts[kind], dict):
                raise ValueError(f"{kind} prepared facts unavailable")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            contexts.pop(kind, None)
            errors[kind] = str(exc)
    return contexts, errors


def _rates(snapshot, contexts):
    ledger = contexts.get("ledger")
    if not ledger:
        raise ValueError("bound prepared ledger FX facts unavailable")
    fx_hash = next(d["sha256"] for d in snapshot["dependencies"] if d["kind"] == "fx")
    if (ledger.get("prepared_authority") or {}).get("run_fx_snapshot_sha256") != fx_hash:
        raise ValueError("prepared FX facts are not bound to sample FX dependency")
    usd, hkd = exchange_rate_scalars_from_option_context(ledger)
    return ExchangeRates(usd_per_cny=usd, cny_per_hkd=hkd)


def _quantity(row, mode, contexts, converter):
    if mode == "call":
        names = ("shares_total", "shares_can_sell", "shares_locked")
        if any(type(row.get(n)) is not int or row[n] < 0 for n in names):
            raise ValueError("require sealed integer shares_total/shares_can_sell/shares_locked")
        inputs = CallCapacityInputs(*(row[n] for n in names))
        return calculate_capacity(row, mode=mode, capacity_inputs=inputs, exchange_rate_converter=converter)[
            "covered_contracts_available"
        ]
    portfolio, ledger = contexts.get("portfolio", {}), contexts.get("ledger", {})
    names = ("cash_by_currency", "_sell_put_fx_status")
    if any(n not in portfolio for n in names) or "cash_secured_total_by_ccy" not in ledger:
        raise ValueError("require bound cash_by_currency/cash_secured_total_by_ccy/_sell_put_fx_status")
    cash, secured = portfolio["cash_by_currency"], ledger["cash_secured_total_by_ccy"]
    if not isinstance(cash, dict) or not isinstance(secured, dict):
        raise ValueError("raw cash and secured amounts must be explicit currency maps")
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for amounts in (cash, secured)
        for v in amounts.values()
    ):
        raise ValueError("raw cash and secured amounts must be finite numbers")
    inputs = PutCapacityInputs(tuple(cash.items()), tuple(secured.items()), portfolio["_sell_put_fx_status"])
    return calculate_capacity(row, mode=mode, capacity_inputs=inputs, exchange_rate_converter=converter)[
        "max_new_contracts"
    ]


def _decision_checks(decision, snapshot, contexts, rates, fx_error, checks):
    cid, mode = decision["candidate_id"], decision["strategy_mode"]
    loc = f"candidate_decisions/{cid}"
    row, opening = decision["normalized_input"], decision["opening_decision"]
    try:
        decision_time_from_input(row)
        strategy = validate_strategy_identity(
            row, mode=mode, account=snapshot["account"], market=snapshot["market"].lower()
        )
        if strategy is None:
            raise ValueError("missing strategy_core identity and effective parameters")
    except (ValueError, TypeError, KeyError) as exc:
        _check(checks, "eligibility", loc, reason=str(exc))
        return False
    _check(checks, "eligibility", loc, True, True)
    raw = {k: v for k, v in row.items() if k not in ("net_income_cny", "option_contract_point_value_cny")}
    rejects = opening["rejects"]
    calculation_reject = bool(rejects) and all(
        r.get("stage") == STAGE_INPUT_NORMALIZATION
        and isinstance(r.get("metric_value"), dict)
        and "reason_code" in r["metric_value"]
        for r in rejects
    )
    try:
        metrics = calculate_metrics(raw, mode=mode, avg_cost=row.get("avg_cost"), exchange_rates=rates)
    except CandidateCalculationError as exc:
        if calculation_reject and len(rejects) == 1:
            detail = exc.to_payload()
            actual = {"reason_code": detail["rule"], "metric_value": detail.get("metric_value")}
            _check(checks, "filter", loc + "/calculation/metric_value", rejects[0]["metric_value"], actual)
            _check(
                checks, "filter", loc + "/calculation/threshold", rejects[0].get("threshold"), detail.get("threshold")
            )
            _check(checks, "filter", loc + "/accepted", opening["accepted"], False)
            _check(
                checks,
                "filter",
                loc + "/calculation/adapter",
                reason="scan adapter rejection classification not reconstructed",
            )
        else:
            _check(checks, "metrics", loc, reason=f"metrics cannot be reconstructed: {exc}")
        return True
    except (ValueError, TypeError, KeyError) as exc:
        _check(checks, "metrics", loc, reason=str(exc))
        return True
    if calculation_reject:
        _check(checks, "filter", loc + "/calculation", "rejected", "calculable")
        return True
    if row.get("insurance_underwriting_mode") == mode:
        try:
            projected = evaluate_candidates([{**row, **metrics}], strategy=strategy)["decisions"][0][
                "opening_decision"
            ]["normalized_input"]
            metrics = {name: projected[name] for name in metrics}
        except (ValueError, TypeError, KeyError) as exc:
            _check(checks, "metrics", loc, reason=f"metric projection unavailable: {exc}")
            return True
    for name, value in metrics.items():
        _check(checks, "metrics", loc + "/normalized_input/" + name, row.get(name, _MISSING), value)
    if (
        fx_error
        or (row.get("currency") == "USD" and rates.usd_per_cny is None)
        or (row.get("currency") == "HKD" and rates.cny_per_hkd is None)
    ):
        _check(checks, "metrics", loc + "/cny", reason=fx_error or "currency FX scalar missing")
    try:
        if row.get("insurance_underwriting_mode") != mode:
            raise ValueError("sealed underwriting input unavailable; prefilter/cash stage not reconstructed")
        missing = sorted(set(metrics).union({"max_new_contracts", "net_income_cny"}) - row.keys())
        if missing:
            raise ValueError("sealed filter inputs missing: " + ", ".join(missing))
        replay = evaluate_candidates([deepcopy(row)], strategy=strategy)["decisions"][0]["opening_decision"]
        for field in ("accepted", "rejects"):
            _check(checks, "filter", loc + "/" + field, opening[field], replay[field])
    except (ValueError, TypeError, KeyError) as exc:
        _check(checks, "filter", loc, reason=str(exc))
    try:
        if mode == "put" and fx_error:
            raise ValueError(fx_error)
        quantity = _quantity(row, mode, contexts, CurrencyConverter(rates))
        _check(checks, "quantity", loc + "/max_new_contracts", row.get("max_new_contracts", _MISSING), quantity)
    except (ValueError, TypeError, KeyError) as exc:
        _check(checks, "quantity", loc, reason=str(exc))
    return True


def _ranking_checks(snapshot, eligible, checks):
    for mode in snapshot["strategy_modes"]:
        decisions = [d for d in snapshot["candidate_decisions"] if d["strategy_mode"] == mode]
        accepted = [d for d in decisions if d["opening_decision"]["accepted"]]
        location = f"ranked_candidates/{mode}"
        if not decisions or not all(d["candidate_id"] in eligible for d in decisions):
            _check(checks, "ranking", location, reason="complete compatible decision universe unavailable")
            continue
        recorded = [r for r in snapshot["ranked_candidates"] if r["strategy_mode"] == mode]
        by_id = {d["candidate_id"]: d["normalized_input"] for d in accepted}
        for r in recorded:
            _check(checks, "ranking_facts", location + "/" + r["candidate_id"], r["facts"], by_id[r["candidate_id"]])
        if not accepted:
            _check(checks, "ranking", location, reason="no accepted candidates to exercise ranking")
            continue
        try:
            rows = [d["normalized_input"] for d in accepted]
            recomputed = rank_candidates(deepcopy(rows), mode=mode)
            expected = [r["facts"]["contract_symbol"] for r in recorded]
            actual = [r["contract_symbol"] for r in recomputed]
            counts = Counter(r["symbol"] for r in rows)
            n = len(counts)
            _check(checks, "ranking", location + "/representatives", expected[:n], actual[:n])
            if sum(v > 1 for v in counts.values()) > 1:
                _check(checks, "ranking", location + "/tail", reason="original symbol-group input order not archived")
            else:
                _check(checks, "ranking", location + "/order", expected, actual)
        except (ValueError, TypeError, KeyError) as exc:
            _check(checks, "ranking", location, reason=str(exc))


def verify_sample(*, base: Path, run_id: str, account: str) -> dict[str, Any]:
    """No providers, live state, current clock, writes, or implicit sample selection."""
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "account": account,
        "checks": [],
        "boundary": "stage-local parity on sealed facts; not provider, portfolio-qualification or return validation",
        "provenance": "source core/template version compatibility; source runtime has no build digest",
        "production_acceptance": "not_certified_by_this_report",
    }
    checks = report["checks"]
    base = Path(base).resolve()
    try:
        if not run_id or not account or account != account.strip().lower():
            raise ValueError("explicit run_id and lowercase account required")
        bundle = load_candidate_snapshot_bundle_for_inspection(base=base, run_id=run_id, account=account)
        snapshot = bundle["owners"].get("opening")
        if not snapshot or snapshot.get("scan_mode") != "standard":
            raise ValueError("formal standard opening sample required")
        if not snapshot.get("candidate_decisions"):
            raise ValueError("no candidate calculations; empty scan is not replay evidence")
        if any(d.get("schema_version") != "opening_candidate_decision.v1" for d in snapshot["candidate_decisions"]):
            raise ValueError("complete candidate decision contract required")
    except (CandidateSnapshotManifestError, OSError, ValueError, KeyError, TypeError) as exc:
        _check(checks, "integrity", "bundle", reason=str(exc))
        return _finish(report)
    report["snapshot_sha256"] = snapshot["content_sha256"]
    report["manifest_sha256"] = bundle["manifest"]["content_sha256"]
    _check(checks, "integrity", "bundle", True, True)
    if any(s.get("strategy_status") not in {"candidates_found", "no_candidate"} for s in snapshot["strategy_results"]):
        _check(checks, "coverage", "strategy_results", reason="scope is not a completed scan")
    for scope in candidate_universe_summary(snapshot)["affected_scopes"]:
        _check(
            checks, "coverage", f"scope_results/{scope['strategy_mode']}/{scope['symbol']}", reason=scope["reason_code"]
        )
    contexts, errors = _contexts(base, snapshot)
    report["dependency_gaps"] = errors
    for kind, error in errors.items():
        _check(checks, "dependency", "dependencies/" + kind, reason=error)
    rates, fx_error = ExchangeRates(), None
    try:
        rates = _rates(snapshot, contexts)
    except (ValueError, KeyError, StopIteration) as exc:
        fx_error = str(exc)
    eligible = set()
    for decision in snapshot["candidate_decisions"]:
        if _decision_checks(decision, snapshot, contexts, rates, fx_error, checks):
            eligible.add(decision["candidate_id"])
    _ranking_checks(snapshot, eligible, checks)
    return _finish(report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    report = verify_sample(base=args.runtime_root, run_id=args.run_id, account=args.account)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return {MATCHED: 0, MISMATCHED: 1, INSUFFICIENT: 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
