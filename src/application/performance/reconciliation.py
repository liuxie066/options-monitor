from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


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


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


__all__ = ["assess_replay_determinism", "assess_report_coverage"]
