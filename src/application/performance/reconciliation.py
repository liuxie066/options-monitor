from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

_MONEY_QUANTUM = Decimal("0.000001")
_LEGACY_REFERENCE_TERMS = (
    "monthly_income" + "_report",
    "net_income" + "_cny",
    "realized_return" + "_rate",
)

LEGACY_REFERENCE_ALLOWLIST: Mapping[str, str] = MappingProxyType(
    {
        "application/agent_tools/analysis.py": "deprecated_compatibility_projection",
        "application/agent_tools/candidate_rank_impl.py": "candidate_strategy_domain",
        "application/agent_tools/materialization_impl.py": "deprecated_compatibility_projection",
        "application/agent_tools/operations_impl.py": "deprecated_adapter_rollback",
        "application/agent_tools/portfolio.py": "deprecated_adapter_rollback",
        "application/agent_tools/positions.py": "deprecated_adapter_rollback",
        "application/assistant/inbound_control.py": "deprecated_compatibility_projection",
        "application/assistant/renderer.py": "deprecated_compatibility_projection",
        "application/assistant/tool_bindings.py": "deprecated_compatibility_projection",
        "application/covered_call_strategy_risk.py": "candidate_strategy_domain",
        "application/ledger/api.py": "deprecated_adapter_rollback",
        "application/ledger/queries.py": "deprecated_adapter_rollback",
        "application/ledger/read_model.py": "deprecated_adapter_rollback",
        "application/portfolio_capital_bridge.py": "deprecated_adapter_rollback",
        "application/positions/reporting.py": "deprecated_adapter_rollback",
        "application/positions/workflows.py": "deprecated_adapter_rollback",
        "application/sell_call_steps.py": "candidate_strategy_domain",
        "application/sell_put_steps.py": "candidate_strategy_domain",
        "application/sell_put_strategy_risk.py": "candidate_strategy_domain",
        "application/shadow_replay/analysis.py": "candidate_strategy_domain",
        "application/shadow_replay/candidate_impact.py": "candidate_strategy_domain",
        "application/shadow_replay/capture.py": "candidate_strategy_domain",
        "application/short_vol_risk_context.py": "candidate_strategy_domain",
        "application/strategy_lab/experiment.py": "candidate_strategy_domain",
        "interfaces/cli/option_positions_report.py": "deprecated_adapter_rollback",
    }
)


def reconcile_legacy_monthly_report(
    legacy_report: Mapping[str, Any],
    v1_report: Mapping[str, Any],
    *,
    scope_proven: bool = False,
) -> dict[str, Any]:
    """Compare current legacy monthly output with the v1 report without mixing semantics."""

    legacy_summary = _mapping_rows(legacy_report.get("summary"))
    v1_rows = _mapping_rows(v1_report.get("rows"))

    legacy_premium = _sum_legacy_summary(legacy_summary, "premium_received_gross")
    legacy_option_cash = _legacy_option_cash(legacy_summary)
    v1_premium = _metric_native(v1_report, "activity", "premium_collected_gross")
    v1_option_cash = _metric_native(v1_report, "cash", "option_trade_cash_gross")

    exact_checks = [
        _native_check("premium_collected_gross.native", legacy_premium, v1_premium),
        _native_check("option_trade_cash_gross.native", legacy_option_cash, v1_option_cash),
        _identity_realized_check(legacy_report, v1_report),
    ]
    quantity_checks = _quantity_checks(legacy_report, v1_report)
    expected_deltas = _expected_deltas(
        legacy_report=legacy_report,
        legacy_summary=legacy_summary,
        v1_report=v1_report,
        v1_rows=v1_rows,
    )
    coverage = assess_report_coverage(v1_report, scope_proven=scope_proven)

    failures = [item["name"] for item in [*exact_checks, *quantity_checks] if item.get("status") == "fail"]
    if coverage["status"] == "fail":
        failures.append("coverage")
    return {
        "schema_version": "option_performance_reconciliation.v1",
        "status": "pass" if not failures else "fail",
        "exact_checks": exact_checks,
        "quantity_checks": quantity_checks,
        "expected_deltas": expected_deltas,
        "coverage": coverage,
        "failures": failures,
    }


def assess_replay_determinism(first_report: Mapping[str, Any], second_report: Mapping[str, Any]) -> dict[str, Any]:
    first_json = _canonical_json(first_report)
    second_json = _canonical_json(second_report)
    first_hash = hashlib.sha256(first_json.encode("utf-8")).hexdigest()
    second_hash = hashlib.sha256(second_json.encode("utf-8")).hexdigest()
    equal = first_json == second_json
    return {
        "schema_version": "option_performance_replay_determinism.v1",
        "status": "pass" if equal else "fail",
        "equal": equal,
        "first_sha256": first_hash,
        "second_sha256": second_hash,
    }


def assess_report_coverage(report: Mapping[str, Any], *, scope_proven: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    for path, envelope in _amount_envelopes(report):
        status = str(envelope.get("status") or "")
        native = envelope.get("by_currency")
        cny = envelope.get("cny")
        missing = [str(item) for item in envelope.get("missing") or []]
        fx_fact_ids = envelope.get("fx_fact_ids")
        reasons: list[str] = []
        if status not in {"observed", "partial", "not_observed", "not_applicable"}:
            reasons.append("invalid_status")
        if not isinstance(native, Mapping):
            reasons.append("native_amounts_not_mapping")
        if status == "not_observed":
            if native:
                reasons.append("not_observed_has_native_amount")
            if cny is not None:
                reasons.append("not_observed_has_cny_amount")
            if scope_proven:
                reasons.append("proven_scope_must_report_observed_zero")
        if status == "observed" and missing:
            reasons.append("observed_metric_has_missing_evidence")
        if status == "observed" and cny is None:
            reasons.append("observed_metric_missing_cny_value")
        if (
            status == "observed"
            and isinstance(native, Mapping)
            and any(str(currency).upper() != "CNY" for currency in native)
            and not (isinstance(fx_fact_ids, (list, tuple)) and fx_fact_ids)
        ):
            reasons.append("observed_non_cny_metric_missing_fx_evidence")
        if status == "partial" and not missing:
            reasons.append("partial_metric_missing_reason_absent")
        if status == "partial" and cny is not None and any(item.startswith("fx:") for item in missing):
            reasons.append("missing_fx_became_cny_value")
        if status == "partial" and not native and cny == 0:
            reasons.append("missing_evidence_became_zero")
        check_status = "fail" if reasons else "pass"
        checks.append({"name": path, "status": check_status, "reasons": reasons})
        if reasons:
            failures.append(path)

    pair_checks = _gross_net_coverage_checks(report)
    failures.extend(item["name"] for item in pair_checks if item["status"] == "fail")
    return {
        "schema_version": "option_performance_coverage_gate.v1",
        "status": "pass" if not failures else "fail",
        "scope_proven": bool(scope_proven),
        "checks": checks,
        "gross_net_checks": pair_checks,
        "failures": failures,
    }


def scan_legacy_references(
    source_root: str | Path,
    *,
    allowlist: Mapping[str, str] = LEGACY_REFERENCE_ALLOWLIST,
) -> dict[str, Any]:
    """Inventory legacy semantic references and fail when ownership drifts."""

    root = Path(source_root)
    matches: dict[str, dict[str, Any]] = {}
    scanner_path = Path(__file__).resolve()
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == scanner_path:
            continue
        text = path.read_text(encoding="utf-8")
        terms = sorted(term for term in _LEGACY_REFERENCE_TERMS if term in text)
        if not terms:
            continue
        relative = path.relative_to(root).as_posix()
        matches[relative] = {
            "category": allowlist.get(relative),
            "terms": terms,
        }
    unowned = sorted(path for path, item in matches.items() if not item["category"])
    stale_allowlist = sorted(path for path in allowlist if path not in matches)
    return {
        "status": "pass" if not unowned and not stale_allowlist else "fail",
        "matches": matches,
        "unowned": unowned,
        "stale_allowlist": stale_allowlist,
    }


def _native_check(name: str, legacy: Mapping[str, Decimal] | None, v1: Mapping[str, Decimal] | None) -> dict[str, Any]:
    if legacy is None or v1 is None:
        return {
            "name": name,
            "status": "fail",
            "legacy": _float_map(legacy),
            "v1": _float_map(v1),
            "reason": "required native metric is unavailable",
        }
    delta = _map_delta(v1, legacy)
    return {
        "name": name,
        "status": "pass" if all(value == 0 for value in delta.values()) else "fail",
        "legacy": _float_map(legacy),
        "v1": _float_map(v1),
        "delta": _float_map(delta),
    }


def _identity_realized_check(legacy_report: Mapping[str, Any], v1_report: Mapping[str, Any]) -> dict[str, Any]:
    legacy_rows = _required_mapping_rows(legacy_report, "rows")
    v1_rows = _required_mapping_rows(v1_report, "rows")
    if legacy_rows is None or v1_rows is None:
        missing = []
        if legacy_rows is None:
            missing.append("legacy.rows")
        if v1_rows is None:
            missing.append("v1.rows")
        return {
            "name": "option_realized_gross.by_close_event",
            "status": "fail",
            "reason": "detail rows are required for identity reconciliation",
            "missing": missing,
        }
    legacy = _amounts_by_identity(
        legacy_rows,
        amount_key="realized_pnl_gross",
        identity_key="event_id",
        require_allocation=False,
    )
    v1 = _amounts_by_identity(
        [row for row in v1_rows if row.get("fact_kind") == "realized_gross"],
        amount_key="amount",
        identity_key="source_event_id",
        require_allocation=True,
    )
    delta = _map_delta(v1, legacy)
    return {
        "name": "option_realized_gross.by_close_event",
        "status": "pass" if all(value == 0 for value in delta.values()) else "fail",
        "legacy": _float_map(legacy),
        "v1": _float_map(v1),
        "delta": _float_map(delta),
    }


def _quantity_checks(legacy_report: Mapping[str, Any], v1_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _mapping_rows(legacy_report.get("cashflow_rows"))
    activity = v1_report.get("activity") if isinstance(v1_report.get("activity"), Mapping) else {}
    if not rows:
        return [
            {
                "name": "contracts_opened_and_closed",
                "status": "not_applicable",
                "reason": "legacy cashflow_rows are required for quantity reconciliation",
            }
        ]
    opened = sum(
        _integer(row.get("contracts"))
        for row in rows
        if str(row.get("trade_action") or "") in {"sell_open", "buy_open"}
    )
    closed = sum(
        _integer(row.get("contracts"))
        for row in rows
        if str(row.get("trade_action") or "")
        in {"buy_close", "sell_close", "assignment_option_close", "exercise_option_close"}
    )
    actual_opened = _integer(activity.get("contracts_opened"))
    actual_closed = _integer(activity.get("contracts_closed"))
    return [
        {
            "name": "contracts_opened",
            "status": "pass" if opened == actual_opened else "fail",
            "legacy": opened,
            "v1": actual_opened,
        },
        {
            "name": "contracts_closed",
            "status": "pass" if closed == actual_closed else "fail",
            "legacy": closed,
            "v1": actual_closed,
        },
    ]


def _expected_deltas(
    *,
    legacy_report: Mapping[str, Any],
    legacy_summary: list[Mapping[str, Any]],
    v1_report: Mapping[str, Any],
    v1_rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    gross = _metric_native(v1_report, "pnl", "realized_gross")
    net = _metric_native(v1_report, "pnl", "realized_net")
    net_envelope = _metric_envelope(v1_report, "pnl", "realized_net") or {}
    net_missing = [str(item) for item in net_envelope.get("missing") or []]
    fee_coverage_incomplete = (
        gross is None
        or net is None
        or set(net) != set(gross)
        or any(not item.startswith("fx:") for item in net_missing)
    )
    if fee_coverage_incomplete:
        fee_status = "classified"
        fee_code = "fee_coverage_incomplete"
        fee_delta = None
    else:
        fee_delta = _map_delta(net, gross)
        fee_status = "classified" if any(value != 0 for value in fee_delta.values()) else "no_delta"
        fee_code = "actual_fee_delta"
    deltas.append(
        {
            "name": "realized_net_vs_legacy_gross",
            "status": fee_status,
            "code": fee_code,
            "delta_by_currency": _float_map(fee_delta),
            "v1_net_status": net_envelope.get("status"),
        }
    )

    for name, legacy_field, namespace, metric in (
        ("premium_cny", "premium_received_gross_cny", "activity", "premium_collected_gross"),
        ("option_cash_cny", "net_cashflow_gross_cny", "cash", "option_trade_cash_gross"),
        ("realized_cny", "realized_pnl_gross_cny", "pnl", "realized_gross"),
    ):
        legacy_cny = _sum_optional_legacy_cny(
            legacy_summary, legacy_field, subtract_assignment=name == "option_cash_cny"
        )
        v1_envelope = _metric_envelope(v1_report, namespace, metric) or {}
        v1_cny = _decimal_or_none(v1_envelope.get("cny"))
        delta = None if legacy_cny is None or v1_cny is None else _money(v1_cny - legacy_cny)
        deltas.append(
            {
                "name": name,
                "status": "unavailable" if delta is None else "classified" if delta != 0 else "no_delta",
                "code": "effective_time_fx_vs_legacy_static_fx",
                "legacy_cny": _float(legacy_cny),
                "v1_cny": _float(v1_cny),
                "delta_cny": _float(delta),
            }
        )

    option_realized = _option_realized_native(v1_rows)
    period_total = _metric_native(v1_report, "pnl", "period_total_gross")
    period_delta = (
        None if option_realized is None or period_total is None else _map_delta(period_total, option_realized)
    )
    deltas.append(
        {
            "name": "period_total_gross_vs_legacy_option_realized",
            "status": (
                "unavailable"
                if period_delta is None
                else "classified"
                if any(value != 0 for value in period_delta.values())
                else "no_delta"
            ),
            "code": "opening_ending_valuation_and_assigned_stock_lifecycle",
            "delta_by_currency": _float_map(period_delta),
        }
    )

    period = v1_report.get("period") if isinstance(v1_report.get("period"), Mapping) else {}
    legacy_months = sorted({str(row.get("month") or "") for row in legacy_summary if str(row.get("month") or "")})
    deltas.append(
        {
            "name": "period_attribution_timezone",
            "status": "classified",
            "code": "asia_shanghai_vs_legacy_month_attribution",
            "legacy_months": legacy_months,
            "v1_reporting_timezone": period.get("reporting_timezone"),
        }
    )

    return_rows = _mapping_rows(legacy_report.get("return_summary"))
    legacy_rates_present = any(
        row.get(key) is not None
        for row in return_rows
        for key in ("realized_return_rate", "net_return_rate", "premium_return_rate")
    )
    deltas.append(
        {
            "name": "generic_return_rates",
            "status": "classified" if legacy_rates_present else "no_delta",
            "code": "intentional_removal_use_explicit_capital_efficiency",
            "legacy_rates_present": legacy_rates_present,
        }
    )
    return deltas


def _gross_net_coverage_checks(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    pairs = (
        ("pnl.realized", "realized_gross", "realized_net"),
        ("pnl.opening_unrealized", "opening_unrealized_gross", "opening_unrealized_net"),
        ("pnl.ending_unrealized", "ending_unrealized_gross", "ending_unrealized_net"),
        ("pnl.period_total", "period_total_gross", "period_total_net"),
    )
    pnl = report.get("pnl") if isinstance(report.get("pnl"), Mapping) else {}
    checks: list[dict[str, Any]] = []
    for name, gross_key, net_key in pairs:
        gross = pnl.get(gross_key) if isinstance(pnl.get(gross_key), Mapping) else None
        net = pnl.get(net_key) if isinstance(pnl.get(net_key), Mapping) else None
        if gross is None or net is None:
            checks.append({"name": name, "status": "fail", "reason": "gross/net metric pair is missing"})
            continue
        gross_native = gross.get("by_currency") if isinstance(gross.get("by_currency"), Mapping) else {}
        net_status = str(net.get("status") or "")
        reason = None
        if gross_native and net_status == "not_observed":
            reason = "gross evidence exists but net metric erased the missing-fee state"
        checks.append({"name": name, "status": "fail" if reason else "pass", "reason": reason})
    return checks


def _amount_envelopes(value: Any, path: str = "") -> Iterable[tuple[str, Mapping[str, Any]]]:
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{path}[{index}]" if path else f"[{index}]"
            yield from _amount_envelopes(item, child)
        return
    if not isinstance(value, Mapping):
        return
    if {"by_currency", "cny", "status", "missing"}.issubset(value.keys()):
        yield path or "$", value
        return
    for key, item in value.items():
        child = f"{path}.{key}" if path else str(key)
        yield from _amount_envelopes(item, child)


def _legacy_option_cash(rows: list[Mapping[str, Any]]) -> dict[str, Decimal] | None:
    out: dict[str, Decimal] = {}
    for row in rows:
        currency = str(row.get("currency") or "").strip().upper()
        cash = _decimal_or_none(row.get("net_cashflow_gross"))
        assignment = _decimal_or_none(row.get("assignment_stock_net_cashflow_gross"))
        if not currency or cash is None or assignment is None:
            return None
        out[currency] = _money(out.get(currency, Decimal(0)) + cash - assignment)
    return out


def _sum_legacy_summary(rows: list[Mapping[str, Any]], field: str) -> dict[str, Decimal] | None:
    out: dict[str, Decimal] = {}
    for row in rows:
        currency = str(row.get("currency") or "").strip().upper()
        amount = _decimal_or_none(row.get(field))
        if not currency or amount is None:
            return None
        out[currency] = _money(out.get(currency, Decimal(0)) + amount)
    return out


def _sum_optional_legacy_cny(
    rows: list[Mapping[str, Any]], field: str, *, subtract_assignment: bool = False
) -> Decimal | None:
    total = Decimal(0)
    for row in rows:
        amount = _decimal_or_none(row.get(field))
        if amount is None:
            return None
        if subtract_assignment:
            assignment = _decimal_or_none(row.get("assignment_stock_net_cashflow_gross_cny"))
            if assignment is None:
                return None
            amount -= assignment
        total += amount
    return _money(total)


def _metric_native(report: Mapping[str, Any], namespace: str, metric: str) -> dict[str, Decimal] | None:
    envelope = _metric_envelope(report, namespace, metric)
    if envelope is None or not isinstance(envelope.get("by_currency"), Mapping):
        return None
    out: dict[str, Decimal] = {}
    for currency, amount in envelope["by_currency"].items():
        parsed = _decimal_or_none(amount)
        if parsed is None:
            return None
        out[str(currency).upper()] = _money(parsed)
    return out


def _metric_envelope(report: Mapping[str, Any], namespace: str, metric: str) -> Mapping[str, Any] | None:
    section = report.get(namespace)
    if not isinstance(section, Mapping):
        return None
    envelope = section.get(metric)
    return envelope if isinstance(envelope, Mapping) else None


def _amounts_by_identity(
    rows: Iterable[Mapping[str, Any]],
    *,
    amount_key: str,
    identity_key: str,
    require_allocation: bool,
) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in rows:
        if require_allocation and not str(row.get("allocation_id") or "").strip():
            continue
        identity = str(row.get(identity_key) or "").strip()
        currency = str(row.get("currency") or "").strip().upper()
        amount = _decimal_or_none(row.get(amount_key))
        if not identity or not currency or amount is None:
            continue
        key = f"{identity}|{currency}"
        out[key] = _money(out.get(key, Decimal(0)) + amount)
    return out


def _option_realized_native(rows: list[Mapping[str, Any]]) -> dict[str, Decimal] | None:
    if not rows:
        return None
    out: dict[str, Decimal] = {}
    found = False
    for row in rows:
        if row.get("fact_kind") != "realized_gross" or not str(row.get("allocation_id") or "").strip():
            continue
        currency = str(row.get("currency") or "").strip().upper()
        amount = _decimal_or_none(row.get("amount"))
        if not currency or amount is None:
            continue
        found = True
        out[currency] = _money(out.get(currency, Decimal(0)) + amount)
    return out if found else {}


def _required_mapping_rows(report: Mapping[str, Any], key: str) -> list[Mapping[str, Any]] | None:
    value = report.get(key)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        return None
    return list(value)


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _map_delta(left: Mapping[str, Decimal], right: Mapping[str, Decimal]) -> dict[str, Decimal]:
    return {
        key: _money(left.get(key, Decimal(0)) - right.get(key, Decimal(0))) for key in sorted(set(left) | set(right))
    }


def _float_map(value: Mapping[str, Decimal] | None) -> dict[str, float] | None:
    if value is None:
        return None
    return {key: float(amount) for key, amount in sorted(value.items())}


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return _money(out) if out.is_finite() else None


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


__all__ = [
    "LEGACY_REFERENCE_ALLOWLIST",
    "assess_replay_determinism",
    "assess_report_coverage",
    "reconcile_legacy_monthly_report",
    "scan_legacy_references",
]
