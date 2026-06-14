from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import calendar
from typing import Any


EVIDENCE_BUNDLE_SCHEMA_VERSION = "om-agent-evidence-bundle-v1"
DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION = "om-agent-diagnostic-evidence-v1"
MAX_FACTS_PER_BUNDLE = 500


@dataclass(frozen=True)
class EvidenceFact:
    fact_id: str
    path: str
    value: Any
    unit: str
    currency: str | None = None
    account: str | None = None
    symbol: str | None = None
    as_of: str | None = None
    freshness: str = "not_applicable"
    source_tool: str = ""
    source_label: str = ""
    source_path: str = ""

    def public_payload(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "path": self.path,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "account": self.account,
            "symbol": self.symbol,
            "as_of": self.as_of,
            "freshness": self.freshness,
            "source_tool": self.source_tool,
            "source_label": self.source_label,
            "source_path": self.source_path,
        }


@dataclass(frozen=True)
class EvidenceBundle:
    scope: dict[str, Any]
    facts: tuple[EvidenceFact, ...]
    datasets: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...] = ()
    calculations: tuple[dict[str, Any], ...] = ()
    missing_data: tuple[dict[str, Any], ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    provenance_lines: tuple[str, ...] = ()
    fallback_renderers: tuple[dict[str, Any], ...] = ()
    guard_contracts: tuple[dict[str, Any], ...] = ()
    schema_version: str = EVIDENCE_BUNDLE_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": dict(self.scope),
            "facts": [fact.public_payload() for fact in self.facts],
            "datasets": [dict(item) for item in self.datasets],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "calculations": [dict(item) for item in self.calculations],
            "missing_data": [dict(item) for item in self.missing_data],
            "conflicts": [dict(item) for item in self.conflicts],
            "provenance_lines": list(self.provenance_lines),
            "fallback_renderers": [dict(item) for item in self.fallback_renderers],
            "guard_contracts": [dict(item) for item in self.guard_contracts],
        }

    def trace_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": dict(self.scope),
            "fact_count": len(self.facts),
            "dataset_count": len(self.datasets),
            "diagnostic_count": len(self.diagnostics),
            "missing_data_count": len(self.missing_data),
            "conflict_count": len(self.conflicts),
            "sources": sorted({item.get("source_label") for item in self.datasets if item.get("source_label")}),
            "tools": sorted({item.get("tool_name") for item in self.datasets if item.get("tool_name")}),
            "guard_profiles": sorted({item.get("guard_profile") for item in self.guard_contracts if item.get("guard_profile")}),
            "diagnostic_domains": sorted({item.get("domain") for item in self.diagnostics if item.get("domain")}),
        }


def build_evidence_bundle(
    *,
    question: str,
    plan: dict[str, Any],
    observations: list[dict[str, Any]],
) -> EvidenceBundle:
    datasets: list[dict[str, Any]] = []
    facts: list[EvidenceFact] = []
    diagnostics: list[dict[str, Any]] = []
    missing_data: list[dict[str, Any]] = []
    guard_contracts: list[dict[str, Any]] = []
    scope_accumulator = _ScopeAccumulator(goal=str(plan.get("goal") or question or "").strip())

    for observation_index, observation in enumerate(observations, start=1):
        if not isinstance(observation, dict):
            continue
        data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
        payload = observation.get("payload") if isinstance(observation.get("payload"), dict) else {}
        contract = observation.get("output_contract") if isinstance(observation.get("output_contract"), dict) else {}
        tool_name = str(observation.get("tool_name") or "")
        source_label = str(contract.get("source_label") or "").strip()
        scope_accumulator.add_payload(payload)
        scope_accumulator.add_data(data)

        datasets.append(
            _dataset_payload(
                observation_index=observation_index,
                observation=observation,
                data=data,
                contract=contract,
                source_label=source_label,
            )
        )
        if contract:
            guard_contracts.append(
                {
                    "tool_name": tool_name,
                    "schema_version": contract.get("schema_version"),
                    "canonical_renderer": contract.get("canonical_renderer"),
                    "guard_profile": contract.get("guard_profile"),
                    "fact_fields": list(contract.get("fact_fields") or []),
                }
            )
        diagnostics.extend(
            _diagnostic_records(
                observation=observation,
                data=data,
                contract=contract,
                source_label=source_label,
            )
        )
        missing_data.extend(
            _missing_data_records(
                observation=observation,
                data=data,
                contract=contract,
                source_label=source_label,
            )
        )

        for raw_path in contract.get("fact_fields") or []:
            if len(facts) >= MAX_FACTS_PER_BUNDLE:
                break
            field_path = str(raw_path or "").strip()
            if not field_path:
                continue
            for source_path, value, context in _extract_path_values(data, field_path):
                if len(facts) >= MAX_FACTS_PER_BUNDLE:
                    break
                fact_id = f"fact_{len(facts) + 1:04d}"
                facts.append(
                    EvidenceFact(
                        fact_id=fact_id,
                        path=field_path,
                        value=value,
                        unit=_infer_unit(field_path, value),
                        currency=_inferred_currency(field_path, context),
                        account=_context_str(context, "account"),
                        symbol=_context_str(context, "symbol"),
                        as_of=_context_as_of(context),
                        freshness=_infer_freshness(field_path, value, context),
                        source_tool=tool_name,
                        source_label=source_label,
                        source_path=source_path,
                    )
                )
        if str(contract.get("canonical_renderer") or "") == "analysis_result":
            for fact in _analysis_result_facts(
                data=data,
                tool_name=tool_name,
                source_label=source_label,
                starting_index=len(facts),
            ):
                if len(facts) >= MAX_FACTS_PER_BUNDLE:
                    break
                facts.append(fact)

    scope = scope_accumulator.public_payload()
    deduped_diagnostics = _dedupe_records(diagnostics)
    deduped_missing = _dedupe_records(missing_data)
    calculations = [
        *_reconciliation_calculations(facts=facts, datasets=datasets, missing_data=deduped_missing),
        *_analysis_query_calculations(facts=facts),
    ]
    conflicts = _conflict_records(facts=facts)
    return EvidenceBundle(
        scope=scope,
        facts=tuple(facts),
        datasets=tuple(datasets),
        diagnostics=tuple(deduped_diagnostics),
        calculations=tuple(calculations),
        missing_data=tuple(deduped_missing),
        conflicts=tuple(conflicts),
        guard_contracts=tuple(guard_contracts),
    )


class _ScopeAccumulator:
    def __init__(self, *, goal: str) -> None:
        self.goal = goal
        self.config_keys: set[str] = set()
        self.accounts: set[str] = set()
        self.symbols: set[str] = set()
        self.months: set[str] = set()
        self.actions: set[str] = set()
        self.statuses: set[str] = set()

    def add_payload(self, payload: dict[str, Any]) -> None:
        self._add(self.config_keys, payload.get("config_key"))
        self._add(self.accounts, payload.get("account"))
        self._add(self.symbols, payload.get("symbol"))
        self._add(self.months, payload.get("month"))
        self._add(self.actions, payload.get("action"))
        self._add(self.statuses, payload.get("status"))
        self._add(self.statuses, payload.get("assigned_stock_status"))

    def add_data(self, data: dict[str, Any]) -> None:
        filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
        self._add(self.accounts, filters.get("account"))
        self._add(self.symbols, filters.get("symbol"))
        self._add(self.months, filters.get("month"))
        for coverage in _coverage_payloads(data):
            for value in coverage.get("accounts") or []:
                self._add(self.accounts, value)
            for value in coverage.get("months") or []:
                self._add(self.months, value)
            for value in coverage.get("symbols") or []:
                self._add(self.symbols, value)
        for rows_key in ("rows", "summary", "return_summary", "cashflow_rows", "realized_rows", "premium_rows"):
            rows = data.get(rows_key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                self._add(self.accounts, row.get("account"))
                self._add(self.symbols, row.get("symbol"))
                self._add(self.months, row.get("month"))
                self._add(self.statuses, row.get("status"))

    def public_payload(self) -> dict[str, Any]:
        months = sorted(self.months)
        payload: dict[str, Any] = {
            "goal": self.goal,
            "config_keys": sorted(self.config_keys),
            "accounts": sorted(self.accounts),
            "symbols": sorted(self.symbols),
            "months": months,
            "actions": sorted(self.actions),
            "statuses": sorted(self.statuses),
        }
        time_range = _time_range_for_months(months)
        if time_range:
            payload["time_range"] = time_range
        return payload

    @staticmethod
    def _add(target: set[str], value: Any) -> None:
        text = str(value or "").strip()
        if text and text.lower() not in {"all", "none", "null"}:
            target.add(text)


def _dataset_payload(
    *,
    observation_index: int,
    observation: dict[str, Any],
    data: dict[str, Any],
    contract: dict[str, Any],
    source_label: str,
) -> dict[str, Any]:
    error = observation.get("error") if isinstance(observation.get("error"), dict) else {}
    primary_rows = str(contract.get("primary_rows") or "").strip()
    row_count_field = str(contract.get("row_count_field") or "").strip()
    row_count = data.get(row_count_field) if row_count_field else None
    if row_count is None and primary_rows:
        rows = data.get(primary_rows)
        row_count = len(rows) if isinstance(rows, list) else None
    payload = {
        "dataset_id": f"dataset_{observation_index:02d}",
        "observation_index": observation_index,
        "tool_name": str(observation.get("tool_name") or ""),
        "ok": bool(observation.get("ok", False)),
        "error_code": error.get("code") if error else None,
        "source_label": source_label,
        "schema_version": contract.get("schema_version"),
        "canonical_renderer": contract.get("canonical_renderer"),
        "guard_profile": contract.get("guard_profile"),
        "primary_rows": primary_rows or None,
        "row_count": row_count,
        "payload": dict(observation.get("payload") or {}) if isinstance(observation.get("payload"), dict) else {},
    }
    analysis_evidence = _analysis_evidence_payload(data)
    if analysis_evidence:
        payload["analysis_evidence"] = analysis_evidence
    return payload


def _coverage_payloads(data: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    coverage = data.get("coverage")
    if isinstance(coverage, dict):
        payloads.append(coverage)
    evidence = data.get("evidence")
    if isinstance(evidence, dict) and isinstance(evidence.get("coverage"), dict):
        payloads.append(evidence["coverage"])
    query_explain = data.get("query_explain")
    if isinstance(query_explain, dict) and isinstance(query_explain.get("coverage"), dict):
        payloads.append(query_explain["coverage"])
    return payloads


def _analysis_evidence_payload(data: dict[str, Any]) -> dict[str, Any]:
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    query_explain = data.get("query_explain") if isinstance(data.get("query_explain"), dict) else {}
    out: dict[str, Any] = {}
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), dict) else query_explain.get("coverage")
    if isinstance(coverage, dict):
        out["coverage"] = dict(coverage)
    freshness = evidence.get("freshness")
    if isinstance(freshness, list):
        out["freshness"] = [dict(item) for item in freshness if isinstance(item, dict)]
    aggregation_policy = evidence.get("aggregation_policy")
    if isinstance(aggregation_policy, list):
        out["aggregation_policy"] = [dict(item) for item in aggregation_policy if isinstance(item, dict)]
    diagnostics = evidence.get("diagnostics")
    if isinstance(diagnostics, list):
        out["diagnostics"] = [dict(item) for item in diagnostics if isinstance(item, dict)]
    warnings = query_explain.get("warnings")
    if isinstance(warnings, list):
        out["warnings"] = [str(item) for item in warnings if str(item).strip()]
    return out


def _diagnostic_records(
    *,
    observation: dict[str, Any],
    data: dict[str, Any],
    contract: dict[str, Any],
    source_label: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    records.extend(_analysis_diagnostic_records(observation=observation, data=data, source_label=source_label))
    records.extend(_assigned_stock_quote_diagnostic_records(observation=observation, data=data, contract=contract, source_label=source_label))
    records.extend(_upgrade_operation_diagnostic_records(observation=observation, data=data, source_label=source_label))
    return records


def _analysis_diagnostic_records(*, observation: dict[str, Any], data: dict[str, Any], source_label: str) -> list[dict[str, Any]]:
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    diagnostics = evidence.get("diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = _analysis_diagnostics_from_rows(data)
    elif not diagnostics:
        diagnostics = _analysis_diagnostics_from_rows(data)
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), dict) else {}
    records: list[dict[str, Any]] = []
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        view = str(item.get("view") or "").strip()
        scope = {
            "accounts": _merged_scope_values(coverage.get("accounts"), item.get("accounts"), item.get("account")),
            "symbols": _merged_scope_values(coverage.get("symbols"), item.get("symbols"), item.get("symbol")),
            "months": _merged_scope_values(coverage.get("months"), item.get("months"), item.get("month")),
            "views": [view] if view else [],
        }
        missing = item.get("missing_data")
        missing_data = [dict(entry) for entry in missing if isinstance(entry, dict)] if isinstance(missing, list) else []
        records.append(
            _compact_record(
                {
                    "schema_version": DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION,
                    "domain": _diagnostic_domain_for_view(view),
                    "status": str(item.get("status") or "diagnostic_missing").strip() or "diagnostic_missing",
                    "severity": str(item.get("severity") or "warning").strip() or "warning",
                    "scope": scope,
                    "source": {
                        "tool": str(observation.get("tool_name") or ""),
                        "view": view or None,
                        "source_label": source_label or str(data.get("source_label") or ""),
                    },
                    "observed_reason": str(item.get("summary") or item.get("reason") or item.get("observed_reason") or "").strip() or None,
                    "answer_boundary": str(item.get("answer_boundary") or "observed_diagnostic_evidence_only").strip(),
                    "missing_data": missing_data,
                    "confidence": _diagnostic_confidence(str(item.get("status") or "")),
                }
            )
        )
    return records


def _analysis_diagnostics_from_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    records: list[dict[str, Any]] = []
    for view_name in _analysis_views_used(data):
        if view_name == "candidate_filter_diagnostics":
            records.append(_candidate_analysis_diagnostic_from_rows(rows))
        elif view_name == "runtime_tick_status":
            records.append(_runtime_analysis_diagnostic_from_rows(rows))
        elif view_name == "close_advice_snapshot":
            records.append(_close_advice_analysis_diagnostic_from_rows(rows))
        elif view_name == "quote_freshness":
            records.append(_quote_analysis_diagnostic_from_rows(rows))
        elif view_name == "upgrade_operation_status":
            records.append(_upgrade_analysis_diagnostic_from_rows(rows))
    return [_compact_record(record) for record in records if record]


def _analysis_views_used(data: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    if isinstance(data.get("views_used"), list):
        values.extend(data.get("views_used") or [])
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), dict) else {}
    if isinstance(coverage.get("views"), list):
        values.extend(coverage.get("views") or [])
    query_explain = data.get("query_explain") if isinstance(data.get("query_explain"), dict) else {}
    if isinstance(query_explain.get("views_used"), list):
        values.extend(query_explain.get("views_used") or [])
    return _sorted_unique_text(values)


def _candidate_analysis_diagnostic_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if _rows_represent_no_matches(rows):
        return _analysis_no_matching_rows_diagnostic("candidate_filter_diagnostics")
    statuses = _row_statuses(rows, "diagnostic_status", "status")
    status = "observed_candidate_diagnostic"
    severity = "info"
    if "conflicting_evidence" in statuses:
        status = "conflicting_evidence"
        severity = "warning"
    elif statuses & {"observed_rejection", "reject", "rejected", "filtered", "excluded", "blocked", "skip", "skipped"}:
        status = "observed_rejection"
    rules = _sorted_unique_row_values(rows, "rule")
    return {
        "view": "candidate_filter_diagnostics",
        "status": status,
        "severity": severity,
        "accounts": _sorted_unique_row_values(rows, "account"),
        "symbols": _sorted_unique_row_values(rows, "symbol"),
        "summary": _first_row_text(rows, "summary", "message", "reason") or _candidate_diagnostic_summary(status=status, rules=rules),
        "answer_boundary": "observed_filter_evidence_only"
        if status != "conflicting_evidence"
        else "conflicting_diagnostic_evidence_only",
    }


def _runtime_analysis_diagnostic_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if _rows_represent_no_matches(rows):
        return _analysis_no_matching_rows_diagnostic("runtime_tick_status")
    statuses = _row_statuses(rows, "diagnostic_status", "latest_status", "status")
    freshness = _row_statuses(rows, "freshness_status")
    notification = _row_statuses(rows, "notification_status")
    skip_reason = _first_row_text(rows, "skip_reason")
    status = "observed_runtime_status"
    severity = "info"
    if "conflicting_evidence" in statuses:
        status = "conflicting_evidence"
        severity = "warning"
    elif statuses & {"failed", "error", "failed_run", "exec_failed"}:
        status = "observed_run_failure"
        severity = "warning"
    elif statuses & {"scheduler_skip", "skip", "skipped", "locked", "outside_window"} or skip_reason:
        status = "observed_scheduler_skip"
        severity = "warning"
    elif notification & {"missing", "not_sent", "not_observed", "failed", "error"}:
        status = "observed_notification_missing"
        severity = "warning"
    elif freshness & {"missing", "stale", "unknown", "failed", "error"}:
        status = "observed_runtime_freshness_gap"
        severity = "warning"
    return {
        "view": "runtime_tick_status",
        "status": status,
        "severity": severity,
        "accounts": _sorted_unique_row_values(rows, "account"),
        "summary": _first_row_text(rows, "summary", "message", "reason")
        or _runtime_diagnostic_summary(status=status, skip_reason=skip_reason),
        "answer_boundary": "observed_runtime_status_only"
        if status != "conflicting_evidence"
        else "conflicting_runtime_evidence_only",
    }


def _close_advice_analysis_diagnostic_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if _rows_represent_no_matches(rows):
        return _analysis_no_matching_rows_diagnostic("close_advice_snapshot")
    actions = _sorted_unique_row_values(rows, "close_action")
    return {
        "view": "close_advice_snapshot",
        "status": "observed_close_advice",
        "severity": "info",
        "accounts": _sorted_unique_row_values(rows, "account"),
        "symbols": _sorted_unique_row_values(rows, "symbol"),
        "summary": _first_row_text(rows, "summary", "reason") or _close_advice_diagnostic_summary(actions=actions),
        "answer_boundary": "recorded_close_policy_evidence_only",
    }


def _quote_analysis_diagnostic_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if _rows_represent_no_matches(rows):
        return _analysis_no_matching_rows_diagnostic("quote_freshness")
    quote_statuses = _row_statuses(rows, "quote_status", "freshness_status")
    bad = quote_statuses & {"missing", "missing_quote", "stale", "unknown", "failed", "error"}
    return {
        "view": "quote_freshness",
        "status": "observed_quote_freshness_gap" if bad else "observed_quote_freshness",
        "severity": "warning" if bad else "info",
        "accounts": _sorted_unique_row_values(rows, "account"),
        "symbols": _sorted_unique_row_values(rows, "symbol"),
        "summary": _first_row_text(rows, "summary", "reason")
        or ("quote rows include missing/stale quote status" if bad else "quote freshness rows were observed"),
        "answer_boundary": "quote_dependent_calculations_only",
    }


def _upgrade_analysis_diagnostic_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if _rows_represent_no_matches(rows):
        return _analysis_no_matching_rows_diagnostic("upgrade_operation_status")
    missing: list[dict[str, Any]] = []
    for field, kind, impact in (
        ("current_version", "current_version_missing", "cannot display or verify current version"),
        ("target_version", "target_version_missing", "cannot display or verify target version"),
    ):
        if any(isinstance(row, dict) and field in row and not str(row.get(field) or "").strip() for row in rows):
            missing.append({"kind": kind, "impact": impact, "recoverable_by": "operation_timeline"})
    receipt_statuses = _row_statuses(rows, "receipt_status")
    if receipt_statuses & {"missing", "not_observed", "failed", "error"}:
        missing.append(
            {
                "kind": "receipt_not_observed",
                "impact": "cannot prove final upgrade receipt delivery from analysis evidence",
                "recoverable_by": "operation_timeline",
            }
        )
    if any(
        isinstance(row, dict)
        and str(row.get("release_tag") or "").strip()
        and not any(
            str(row.get(field) or "").strip()
            for field in ("release_status", "release_published_at", "github_release_url")
        )
        for row in rows
    ):
        missing.append(
            {
                "kind": "release_publication_status_missing",
                "impact": "release_tag in operation evidence does not prove GitHub Release publication",
                "recoverable_by": "release_workflow_status",
            }
        )
    conflict_reasons = _upgrade_status_conflict_reasons(rows)
    status = "conflicting_evidence" if conflict_reasons else "observed_operation_status"
    return {
        "view": "upgrade_operation_status",
        "status": status,
        "severity": "warning" if conflict_reasons or missing else "info",
        "summary": _first_row_text(rows, "summary", "reason")
        or (
            "upgrade operation evidence is conflicting: " + "; ".join(conflict_reasons)
            if conflict_reasons
            else "upgrade operation status rows were observed"
        ),
        "answer_boundary": "conflicting_upgrade_operation_evidence_only"
        if conflict_reasons
        else "upgrade_operation_status_evidence_only",
        "missing_data": missing,
    }


def _analysis_no_matching_rows_diagnostic(view_name: str) -> dict[str, Any]:
    return {
        "view": view_name,
        "status": "no_matching_rows",
        "severity": "warning",
        "summary": f"{view_name} returned no matching diagnostic rows",
        "answer_boundary": "cannot infer absence of problem from empty diagnostic result",
    }


def _rows_represent_no_matches(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return True
    if len(rows) != 1:
        return False
    row = rows[0]
    if not isinstance(row, dict):
        return False
    count_fields = [key for key in row if key.lower() in {"count", "row_count", "cnt"} or key.lower().startswith("count_")]
    return bool(count_fields) and all(_safe_float(row.get(key)) == 0 for key in count_fields)


def _row_statuses(rows: list[dict[str, Any]], *fields: str) -> set[str]:
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in fields:
            text = str(row.get(field) or "").strip().lower()
            if text:
                out.add(text)
    return out


def _first_row_text(rows: list[dict[str, Any]], *fields: str) -> str:
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in fields:
            text = str(row.get(field) or "").strip()
            if text:
                return text
    return ""


def _sorted_unique_row_values(rows: list[dict[str, Any]], field: str) -> list[str]:
    return _sorted_unique_text(row.get(field) for row in rows if isinstance(row, dict))


def _sorted_unique_text(values: Any) -> list[str]:
    out: set[str] = set()
    if values is None or isinstance(values, (str, bytes)):
        stack = [values]
    else:
        try:
            stack = list(values)
        except TypeError:
            stack = [values]
    for value in stack:
        if isinstance(value, (list, tuple, set)):
            stack.extend(value)
            continue
        text = str(value or "").strip()
        if text:
            out.add(text)
    return sorted(out)


def _merged_scope_values(*values: Any) -> list[str]:
    merged: list[Any] = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            merged.extend(value)
        else:
            merged.append(value)
    return _sorted_unique_text(merged)


def _candidate_diagnostic_summary(*, status: str, rules: list[str]) -> str:
    if status == "conflicting_evidence":
        return "candidate diagnostic evidence is conflicting"
    if status == "observed_rejection":
        return (
            "candidate diagnostic contains observed rejection/filter evidence by rules: " + ", ".join(rules)
            if rules
            else "candidate diagnostic contains observed rejection/filter evidence"
        )
    return "candidate diagnostic rows were observed"


def _runtime_diagnostic_summary(*, status: str, skip_reason: str = "") -> str:
    if status == "conflicting_evidence":
        return "runtime diagnostic evidence is conflicting"
    if status == "observed_scheduler_skip":
        return f"scheduler skipped because {skip_reason}" if skip_reason else "scheduler skip was observed"
    if status == "observed_run_failure":
        return "runtime latest run failure was observed"
    if status == "observed_notification_missing":
        return "runtime notification was not observed"
    if status == "observed_runtime_freshness_gap":
        return "runtime freshness gap was observed"
    return "runtime status rows were observed"


def _close_advice_diagnostic_summary(*, actions: list[str]) -> str:
    return "close advice actions observed: " + ", ".join(actions) if actions else "close advice rows were observed"


def _assigned_stock_quote_diagnostic_records(
    *,
    observation: dict[str, Any],
    data: dict[str, Any],
    contract: dict[str, Any],
    source_label: str,
) -> list[dict[str, Any]]:
    if str(observation.get("tool_name") or "") != "option_positions_read":
        return []
    if str(contract.get("canonical_renderer") or "") != "assigned_stock_lifecycle":
        return []
    records: list[dict[str, Any]] = []
    quote_refresh = data.get("quote_refresh") if isinstance(data.get("quote_refresh"), dict) else {}
    quote_source = str(quote_refresh.get("quote_source") or quote_refresh.get("source") or "").strip()
    rows = data.get("rows")
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            quote_status = str(row.get("quote_status") or "").strip()
            if quote_status in {"", "fresh", "ok", "not_applicable"}:
                continue
            records.append(
                _compact_record(
                    {
                        "schema_version": DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION,
                        "domain": "quote_freshness",
                        "status": "observed_quote_gap",
                        "severity": "warning",
                        "scope": {
                            "accounts": _list_str(row.get("account")),
                            "symbols": _list_str(row.get("symbol")),
                            "status": _list_str(row.get("status")),
                        },
                        "source": {
                            "tool": str(observation.get("tool_name") or ""),
                            "view": "assigned_stock_lifecycle",
                            "source_label": source_label,
                            "quote_source": quote_source or None,
                            "source_path": f"rows[{index}].quote_status",
                        },
                        "observed_reason": _quote_gap_reason(row=row, quote_status=quote_status),
                        "answer_boundary": "quote_status_only_cannot_infer_upstream_root_cause",
                        "missing_data": [
                            {
                                "kind": quote_status,
                                "impact": "assigned stock realtime floating PnL cannot be fully calculated",
                                "recoverable_by": "refresh_quotes",
                            }
                        ],
                        "confidence": "direct",
                    }
                )
            )
    refresh_status = str(quote_refresh.get("status") or "").strip()
    if refresh_status and refresh_status != "ok" and not records:
        records.append(
            _compact_record(
                {
                    "schema_version": DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION,
                    "domain": "quote_freshness",
                    "status": "observed_quote_gap",
                    "severity": "warning",
                    "scope": {
                        "symbols": [str(value) for value in quote_refresh.get("missing_symbols") or [] if str(value).strip()],
                    },
                    "source": {
                        "tool": str(observation.get("tool_name") or ""),
                        "view": "assigned_stock_lifecycle",
                        "source_label": source_label,
                        "quote_source": quote_source or None,
                        "source_path": "quote_refresh.status",
                    },
                    "observed_reason": f"quote refresh reported {refresh_status}",
                    "answer_boundary": "quote_refresh_status_only_cannot_infer_upstream_root_cause",
                    "missing_data": [
                        {
                            "kind": refresh_status,
                            "impact": "quote dependent facts may be incomplete",
                            "recoverable_by": "refresh_quotes",
                        }
                    ],
                    "confidence": "direct",
                }
            )
        )
    return records


def _upgrade_operation_diagnostic_records(
    *,
    observation: dict[str, Any],
    data: dict[str, Any],
    source_label: str,
) -> list[dict[str, Any]]:
    tool_name = str(observation.get("tool_name") or "")
    if tool_name == "operation_timeline":
        return _operation_timeline_upgrade_diagnostics(observation=observation, data=data, source_label=source_label)
    if tool_name == "inbound.upgrade":
        return _inbound_upgrade_diagnostics(observation=observation, data=data, source_label=source_label)
    return []


def _operation_timeline_upgrade_diagnostics(
    *,
    observation: dict[str, Any],
    data: dict[str, Any],
    source_label: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    warnings = [str(item) for item in data.get("warnings") or [] if str(item).strip()]
    if warnings and int(data.get("timeline_count") or 0) == 0:
        records.append(
            _compact_record(
                {
                    "schema_version": DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION,
                    "domain": "upgrade",
                    "status": "artifact_missing" if any("missing" in warning for warning in warnings) else "diagnostic_missing",
                    "severity": "warning",
                    "scope": {"operation_types": _list_str(_filters_value(data, "operation_types"))},
                    "source": {
                        "tool": str(observation.get("tool_name") or ""),
                        "view": "operation_timeline",
                        "source_label": source_label or "OM inbound operation audit",
                        "source_path": "warnings",
                    },
                    "observed_reason": ", ".join(warnings),
                    "answer_boundary": "operation_timeline_evidence_only",
                    "missing_data": [_upgrade_missing_item(kind=warning, impact=_upgrade_warning_impact(warning)) for warning in warnings],
                    "confidence": "missing",
                }
            )
        )
    timelines = data.get("timelines")
    if not isinstance(timelines, list):
        return records
    for index, item in enumerate(timelines):
        if not isinstance(item, dict):
            continue
        raw_operation = item.get("operation")
        raw_identity = item.get("identity")
        raw_outcome = item.get("outcome")
        raw_receipt = item.get("receipt")
        operation: dict[str, Any] = raw_operation if isinstance(raw_operation, dict) else {}
        operation_type = str(operation.get("operation_type") or "").strip()
        if operation_type and "upgrade" not in operation_type:
            continue
        identity: dict[str, Any] = raw_identity if isinstance(raw_identity, dict) else {}
        outcome: dict[str, Any] = raw_outcome if isinstance(raw_outcome, dict) else {}
        receipt: dict[str, Any] = raw_receipt if isinstance(raw_receipt, dict) else {}
        item_warnings = [str(value) for value in item.get("warnings") or outcome.get("warnings") or [] if str(value).strip()]
        operation_status = str(operation.get("status") or "").strip()
        outcome_status = str(outcome.get("status") or "").strip()
        receipt_status = str(receipt.get("status") or "").strip()
        status = str(outcome_status or operation_status or "unknown").strip()
        conflict_reasons = _upgrade_status_conflict_reasons(
            [
                {
                    "operation_status": operation_status,
                    "outcome_status": outcome_status,
                    "receipt_status": receipt_status,
                }
            ]
        )
        current_version = _first_nonempty(operation.get("current_version"), _nested_value(operation, ("version", "current_version")))
        target_version = _first_nonempty(operation.get("target_version"), _nested_value(operation, ("version", "target_version")))
        missing = [_upgrade_missing_item(kind=warning, impact=_upgrade_warning_impact(warning)) for warning in item_warnings]
        if not current_version:
            missing.append(_upgrade_missing_item(kind="current_version_missing", impact="cannot display or verify current version"))
        if not target_version:
            missing.append(_upgrade_missing_item(kind="target_version_missing", impact="cannot display or verify target version"))
        if str(receipt.get("status") or "") == "not_observed" and not any(entry.get("kind") == "receipt_not_observed" for entry in missing):
            missing.append(
                _upgrade_missing_item(
                    kind="receipt_not_observed",
                    impact="cannot prove the final upgrade receipt was delivered from operation timeline evidence",
                )
            )
        records.append(
            _compact_record(
                {
                    "schema_version": DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION,
                    "domain": "upgrade",
                    "status": "conflicting_evidence" if conflict_reasons else "observed_operation_status",
                    "severity": "warning" if conflict_reasons or missing else "info",
                    "scope": {
                        "operation_types": _list_str(operation_type),
                        "operation_ids": _list_str(identity.get("operation_id") or operation.get("operation_id")),
                        "command_ids": _list_str(identity.get("command_id") or operation.get("command_id")),
                        "statuses": _list_str(status),
                    },
                    "version": {
                        "current_version": current_version,
                        "target_version": target_version,
                    },
                    "source": {
                        "tool": str(observation.get("tool_name") or ""),
                        "view": "operation_timeline",
                        "source_label": source_label or "OM inbound operation audit",
                        "source_path": f"timelines[{index}]",
                    },
                    "observed_reason": "upgrade operation evidence is conflicting: " + "; ".join(conflict_reasons)
                    if conflict_reasons
                    else f"upgrade operation status is {status}",
                    "answer_boundary": "conflicting_operation_timeline_evidence_only"
                    if conflict_reasons
                    else "operation_timeline_status_and_receipt_evidence_only",
                    "missing_data": missing,
                    "confidence": "conflict" if conflict_reasons else ("partial" if missing else "direct"),
                }
            )
        )
    return records


def _inbound_upgrade_diagnostics(
    *,
    observation: dict[str, Any],
    data: dict[str, Any],
    source_label: str,
) -> list[dict[str, Any]]:
    operation_type = str(data.get("operation_type") or _nested_value(data, ("payload", "operation_type")) or "").strip()
    if operation_type and "upgrade" not in operation_type:
        return []
    status = str(data.get("status") or "").strip() or "unknown"
    current_version = _first_nonempty(
        data.get("current_version"),
        _nested_value(data, ("preview", "upgrade", "current_version")),
        _nested_value(data, ("preview", "upgrade", "version_check", "current_version")),
        _nested_value(data, ("result", "current_version")),
        _nested_value(data, ("result", "version_check", "current_version")),
    )
    target_version = _first_nonempty(
        data.get("target_version"),
        _nested_value(data, ("payload", "arguments", "target_version")),
        _nested_value(data, ("preview", "upgrade", "target_version")),
        _nested_value(data, ("preview", "upgrade", "latest_version")),
        _nested_value(data, ("preview", "upgrade", "version_check", "target_version")),
        _nested_value(data, ("preview", "upgrade", "version_check", "latest_version")),
        _nested_value(data, ("result", "target_version")),
        _nested_value(data, ("result", "latest_version")),
    )
    missing: list[dict[str, Any]] = []
    if not current_version:
        missing.append(_upgrade_missing_item(kind="current_version_missing", impact="cannot display or verify current version"))
    if not target_version:
        missing.append(_upgrade_missing_item(kind="target_version_missing", impact="cannot display or verify target version"))
    final_receipt = data.get("final_receipt") if isinstance(data.get("final_receipt"), dict) else {}
    if status in {"applied", "failed"} and not final_receipt:
        missing.append(_upgrade_missing_item(kind="final_receipt_missing", impact="cannot prove final upgrade receipt delivery"))
    return [
        _compact_record(
            {
                "schema_version": DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION,
                "domain": "upgrade",
                "status": "observed_upgrade_operation",
                "severity": "warning" if missing else "info",
                "scope": {
                    "operation_types": _list_str(operation_type),
                    "operation_ids": _list_str(data.get("operation_id")),
                    "statuses": _list_str(status),
                },
                "version": {
                    "current_version": current_version,
                    "target_version": target_version,
                },
                "source": {
                    "tool": str(observation.get("tool_name") or ""),
                    "view": "inbound_upgrade_response",
                    "source_label": source_label or "OM inbound upgrade operation",
                },
                "observed_reason": f"upgrade operation response status is {status}",
                "answer_boundary": "inbound_upgrade_response_evidence_only",
                "missing_data": missing,
                "confidence": "partial" if missing else "direct",
            }
        )
    ]


def _diagnostic_domain_for_view(view: str) -> str:
    normalized = str(view or "").strip()
    if "candidate" in normalized:
        return "candidate_filter"
    if "quote" in normalized:
        return "quote_freshness"
    if "runtime" in normalized or "tick" in normalized:
        return "runtime_tick"
    if "close" in normalized:
        return "close_advice"
    if "upgrade" in normalized or "operation" in normalized:
        return "upgrade"
    return "analysis"


def _diagnostic_confidence(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {
        "observed_rejection",
        "observed_skip",
        "observed_hold",
        "observed_candidate_diagnostic",
        "observed_close_advice",
        "observed_quote_freshness",
        "observed_quote_freshness_gap",
        "observed_runtime_status",
        "observed_scheduler_skip",
        "observed_run_failure",
        "observed_no_candidates",
        "observed_notification_missing",
        "observed_runtime_freshness_gap",
        "observed_operation_status",
        "observed_upgrade_operation",
    }:
        return "direct"
    if normalized in {"diagnostic_missing", "artifact_missing", "empty_artifact", "no_matching_rows", "read_error"}:
        return "missing"
    if normalized in {"conflicting_evidence"}:
        return "conflict"
    return "partial"


def _quote_gap_reason(*, row: dict[str, Any], quote_status: str) -> str:
    symbol = str(row.get("symbol") or "").strip()
    if quote_status == "missing_quote":
        return f"{symbol} has no usable as-of quote" if symbol else "assigned stock lot has no usable as-of quote"
    return f"{symbol} quote status is {quote_status}" if symbol else f"quote status is {quote_status}"


def _filters_value(data: dict[str, Any], key: str) -> Any:
    raw_filters = data.get("filters")
    filters: dict[str, Any] = raw_filters if isinstance(raw_filters, dict) else {}
    return filters.get(key)


def _upgrade_missing_item(*, kind: str, impact: str) -> dict[str, Any]:
    return {
        "kind": str(kind or "").strip() or "missing_upgrade_evidence",
        "impact": str(impact or "").strip() or "upgrade operation evidence is incomplete",
        "recoverable_by": "operation_timeline",
    }


def _upgrade_warning_impact(warning: str) -> str:
    normalized = str(warning or "").strip()
    return {
        "receipt_not_observed": "cannot prove final upgrade receipt delivery from operation timeline evidence",
        "operation_missing": "operation row is missing; only audit evidence is available",
        "operation_store_missing": "pending operation store is missing",
        "operations_table_missing": "pending operation table is missing",
        "audit_table_missing": "inbound audit table is missing",
        "audit_db_missing": "inbound audit database is missing",
        "command_log_missing": "upgrade command log is missing",
        "command_audit_missing": "upgrade command audit is missing",
        "operation_log_missing": "upgrade operation log is missing",
    }.get(normalized, f"upgrade operation warning: {normalized}")


def _upgrade_status_conflict_reasons(rows: list[dict[str, Any]]) -> list[str]:
    reasons: set[str] = set()
    success_statuses = {"applied", "success", "succeeded", "completed", "ok", "observed", "delivered", "sent", "published"}
    failure_statuses = {"failed", "failure", "error", "cancelled", "canceled", "rejected"}
    terminal_statuses = success_statuses | failure_statuses
    for row in rows:
        if not isinstance(row, dict):
            continue
        statuses = {
            "operation_status": str(row.get("operation_status") or "").strip().lower(),
            "outcome_status": str(row.get("outcome_status") or "").strip().lower(),
            "receipt_status": str(row.get("receipt_status") or "").strip().lower(),
            "release_status": str(row.get("release_status") or "").strip().lower(),
        }
        present = {key: value for key, value in statuses.items() if value}
        if not present:
            continue
        has_success = any(value in success_statuses for value in present.values())
        has_failure = any(value in failure_statuses for value in present.values())
        if has_success and has_failure:
            reasons.add(",".join(f"{key}={value}" for key, value in present.items() if value in terminal_statuses))
    return sorted(reasons)


def _nested_value(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_nonempty(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _list_str(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in record.items():
        if value is None:
            continue
        if isinstance(value, dict):
            compact_map = _compact_record(value)
            if compact_map:
                out[key] = compact_map
            continue
        if isinstance(value, list):
            compact_list = [
                _compact_record(item) if isinstance(item, dict) else item
                for item in value
                if item is not None and item != "" and item != [] and item != {}
            ]
            if compact_list:
                out[key] = compact_list
            continue
        if value == "":
            continue
        out[key] = value
    return out


def _missing_data_records(
    *,
    observation: dict[str, Any],
    data: dict[str, Any],
    contract: dict[str, Any],
    source_label: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    tool_name = str(observation.get("tool_name") or "")
    quote_refresh = data.get("quote_refresh") if isinstance(data.get("quote_refresh"), dict) else {}
    quote_status = str(quote_refresh.get("status") or "").strip()
    if quote_status and quote_status != "ok":
        records.append(
            {
                "kind": quote_status,
                "symbols": [str(item) for item in quote_refresh.get("missing_symbols") or [] if str(item).strip()],
                "impact": "realtime quote dependent facts may be incomplete",
                "recoverable_by": "refresh_quotes" if tool_name == "option_positions_read" else None,
                "source_tool": tool_name,
                "source_label": source_label,
            }
        )
    rows = data.get("rows")
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            row_quote_status = str(row.get("quote_status") or "").strip()
            if row_quote_status in {"", "fresh", "ok", "not_applicable"}:
                continue
            records.append(
                {
                    "kind": row_quote_status,
                    "symbol": row.get("symbol"),
                    "account": row.get("account"),
                    "impact": "assigned stock realtime floating PnL cannot be fully calculated"
                    if row_quote_status == "missing_quote"
                    else "quote dependent facts may be incomplete",
                    "recoverable_by": "refresh_quotes",
                    "source_tool": tool_name,
                    "source_label": source_label,
                    "source_path": f"rows[{index}].quote_status",
                }
            )
    for warning_key in ("warnings", "report_warnings", "diagnostics"):
        warnings = data.get(warning_key)
        if not isinstance(warnings, list):
            continue
        for index, warning in enumerate(warnings[:20]):
            if isinstance(warning, dict):
                message = str(warning.get("message") or warning.get("detail") or warning).strip()
            else:
                message = str(warning or "").strip()
            if not message:
                continue
            records.append(
                {
                    "kind": warning_key,
                    "impact": message,
                    "source_tool": tool_name,
                    "source_label": source_label,
                    "source_path": f"{warning_key}[{index}]",
                }
            )
    capability = data.get("capability_status") if isinstance(data.get("capability_status"), dict) else {}
    for gap in capability.get("gaps") or []:
        if str(gap).strip():
            records.append(
                {
                    "kind": "capability_gap",
                    "impact": str(gap),
                    "source_tool": tool_name,
                    "source_label": source_label,
                }
            )
    return records


def _extract_path_values(data: dict[str, Any], field_path: str) -> list[tuple[str, Any, dict[str, Any]]]:
    parts = [part for part in field_path.split(".") if part]
    current: list[tuple[str, Any, dict[str, Any]]] = [("", data, {})]
    for part in parts:
        next_items: list[tuple[str, Any, dict[str, Any]]] = []
        is_array = part.endswith("[]")
        key = part[:-2] if is_array else part
        for prefix, value, context in current:
            if not isinstance(value, dict) or key not in value:
                continue
            child = value.get(key)
            child_prefix = f"{prefix}.{key}" if prefix else key
            if is_array:
                if not isinstance(child, list):
                    continue
                for index, item in enumerate(child):
                    item_prefix = f"{child_prefix}[{index}]"
                    item_context = _merge_context(context, item if isinstance(item, dict) else {})
                    next_items.append((item_prefix, item, item_context))
            else:
                next_items.append((child_prefix, child, _merge_context(context, child if isinstance(child, dict) else {})))
        current = next_items
    return [(path, value, context) for path, value, context in current]


def _merge_context(context: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        return dict(context)
    merged = dict(context)
    for key in (
        "account",
        "symbol",
        "currency",
        "month",
        "status",
        "quote_status",
        "quote_source",
        "quote_as_of",
        "as_of",
        "as_of_ms",
        "updated_at",
        "expiration_ymd",
    ):
        if key in row and row.get(key) is not None:
            merged[key] = row.get(key)
    return merged


def _analysis_result_facts(
    *,
    data: dict[str, Any],
    tool_name: str,
    source_label: str,
    starting_index: int,
) -> list[EvidenceFact]:
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    facts: list[EvidenceFact] = []
    index = int(starting_index)
    for row_index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        context = _merge_context({}, row)
        for column, value in row.items():
            if isinstance(value, (dict, list)):
                continue
            if value is None or value == "":
                continue
            index += 1
            path = f"rows[].{column}"
            facts.append(
                EvidenceFact(
                    fact_id=f"fact_{index:04d}",
                    path=path,
                    value=value,
                    unit=_infer_unit(path, value),
                    currency=_inferred_currency(path, context),
                    account=_context_str(context, "account"),
                    symbol=_context_str(context, "symbol"),
                    as_of=_context_as_of(context),
                    freshness=_infer_freshness(path, value, context),
                    source_tool=tool_name,
                    source_label=source_label,
                    source_path=f"rows[{row_index - 1}].{column}",
                )
            )
    return facts


def _infer_unit(path: str, value: Any) -> str:
    name = path.rsplit(".", 1)[-1].lower()
    if name in {"account"}:
        return "account"
    if name in {"symbol"}:
        return "symbol"
    if name == "currency":
        return "currency_code"
    if name.endswith(("_cny", "_usd", "_hkd")) or name in {"cny", "usd", "hkd"}:
        return "currency"
    if "percent" in name or "rate" in name:
        return "percent"
    if name.endswith("_per_share") and any(token in name for token in ("cost", "price", "spot")):
        return "currency"
    if "contract" in name:
        return "contract"
    if "share" in name or "quantity" in name or name in {"remaining_shares", "sold_shares"}:
        return "share"
    if "date" in name or "expiration" in name or name == "month":
        return "date"
    if "status" in name:
        return "status"
    amount_tokens = (
        "pnl",
        "cashflow",
        "income",
        "premium",
        "basis",
        "cost",
        "price",
        "spot",
        "strike",
        "gross",
        "market_value",
        "amount",
        "diff",
        "delta",
        "difference",
    )
    if any(token in name for token in amount_tokens):
        return "currency"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "text"


def _inferred_currency(path: str, context: dict[str, Any]) -> str | None:
    context_currency = _context_str(context, "currency")
    if context_currency:
        return context_currency.upper()
    name = path.rsplit(".", 1)[-1].lower()
    if name.endswith("_cny") or name == "cny":
        return "CNY"
    if name.endswith("_usd") or name == "usd":
        return "USD"
    if name.endswith("_hkd") or name == "hkd":
        return "HKD"
    return None


def _infer_freshness(path: str, value: Any, context: dict[str, Any]) -> str:
    name = path.rsplit(".", 1)[-1].lower()
    if value is None:
        return "missing"
    quote_status = str(context.get("quote_status") or "").strip()
    if quote_status:
        if quote_status in {"fresh", "ok"}:
            return "fresh"
        if quote_status == "missing_quote":
            return "missing"
        return quote_status
    if "quote" in name or "spot" in name:
        return "missing" if value in {"", None} else "fresh"
    return "not_applicable"


def _context_str(context: dict[str, Any], key: str) -> str | None:
    value = context.get(key)
    text = str(value or "").strip()
    return text or None


def _context_as_of(context: dict[str, Any]) -> str | None:
    for key in ("quote_as_of", "as_of", "updated_at", "as_of_ms", "month", "expiration_ymd"):
        value = context.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return None


def _reconciliation_calculations(
    *,
    facts: list[EvidenceFact],
    datasets: list[dict[str, Any]],
    missing_data: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    views: dict[str, dict[str, Any]] = {}
    has_cashflow_summary = any(fact.path == "summary[].net_cashflow_gross" for fact in facts)
    for fact in facts:
        view = _fact_accounting_view(fact)
        if not view:
            continue
        bucket = views.setdefault(
            view,
            {
                "view": view,
                "fact_ids": [],
                "currencies": sorted({}),
                "sums_by_currency": {},
            },
        )
        bucket["fact_ids"].append(fact.fact_id)
        if fact.currency:
            currencies = set(bucket.get("currencies") or [])
            currencies.add(fact.currency)
            bucket["currencies"] = sorted(currencies)
        amount = _safe_float(fact.value)
        if (
            amount is not None
            and fact.currency
            and fact.unit in {"currency", "number"}
            and _include_fact_in_view_sum(fact, view=view, has_cashflow_summary=has_cashflow_summary)
        ):
            sums = dict(bucket.get("sums_by_currency") or {})
            sums[fact.currency] = round(float(sums.get(fact.currency) or 0.0) + amount, 6)
            bucket["sums_by_currency"] = sums
    calculations: list[dict[str, Any]] = []
    if views:
        calculations.append(
            {
                "kind": "accounting_view_summary",
                "views": [views[key] for key in sorted(views)],
                "missing_data_count": len(missing_data),
            }
        )
    tools = {str(item.get("tool_name") or "") for item in datasets}
    has_income = "monthly_income_report" in tools
    has_assigned_stock = "option_positions_read" in tools and any(
        str(item.get("canonical_renderer") or "") == "assigned_stock_lifecycle" for item in datasets
    )
    if has_income and has_assigned_stock:
        calculations.append(
            {
                "kind": "cross_tool_reconciliation",
                "status": "different_accounting_views",
                "views": [
                    "cashflow",
                    "realized_option_pnl",
                    "option_premium_attribution",
                    "assigned_stock_unrealized_pnl",
                    "assigned_stock_realized_pnl",
                    "assignment_lifecycle_pnl",
                ],
                "note": (
                    "monthly income, realized cashflow, assigned-stock floating PnL, "
                    "and assignment lifecycle PnL are separate accounting views and must not be added blindly"
                ),
            }
        )
    return calculations


def _analysis_query_calculations(*, facts: list[EvidenceFact]) -> list[dict[str, Any]]:
    rows = _analysis_query_fact_rows(facts)
    formulas: list[dict[str, Any]] = []
    for row_key in sorted(rows):
        row = rows[row_key]
        formulas.extend(_analysis_amount_formulas(row_key=row_key, row=row))
        formulas.extend(_analysis_ratio_formulas(row_key=row_key, row=row))
        formulas.extend(_analysis_lifecycle_formulas(row_key=row_key, row=row))
        if len(formulas) >= 80:
            break
    if not formulas:
        return []
    return [
        {
            "kind": "analysis_formula_evidence",
            "tool_name": "analysis_query",
            "formulas": formulas[:80],
        }
    ]


def _analysis_query_fact_rows(facts: list[EvidenceFact]) -> dict[str, dict[str, dict[str, Any]]]:
    rows: dict[str, dict[str, dict[str, Any]]] = {}
    for fact in facts:
        if fact.source_tool != "analysis_query":
            continue
        row_key = _analysis_fact_row_key(fact)
        field_name = _analysis_fact_field_name(fact)
        if not row_key or not field_name:
            continue
        value = _safe_float(fact.value)
        rows.setdefault(row_key, {})[field_name] = {
            "value": value,
            "raw_value": fact.value,
            "currency": fact.currency,
            "unit": fact.unit,
            "fact_id": fact.fact_id,
            "field": field_name,
        }
    return rows


def _analysis_amount_formulas(*, row_key: str, row: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_currency: dict[str, list[dict[str, Any]]] = {}
    for field, item in row.items():
        currency = str(item.get("currency") or "").upper()
        value = item.get("value")
        if not currency or not isinstance(value, (int, float)):
            continue
        if not _analysis_money_formula_source(field):
            continue
        by_currency.setdefault(currency, []).append(item)
    formulas: list[dict[str, Any]] = []
    for currency, items in sorted(by_currency.items()):
        if len(items) < 2:
            continue
        for left_index, left in enumerate(items[:8]):
            for right in items[left_index + 1 : 8]:
                left_value = float(left["value"])
                right_value = float(right["value"])
                diff = round(left_value - right_value, 6)
                if abs(diff) >= 0.000001:
                    formulas.append(
                        {
                            "kind": "amount_difference",
                            "row": row_key,
                            "currency": currency,
                            "operands": [left["field"], right["field"]],
                            "fact_ids": [left["fact_id"], right["fact_id"]],
                            "values": [diff, -diff, abs(diff)],
                            "status": "verified",
                        }
                    )
                total = round(left_value + right_value, 6)
                formulas.append(
                    {
                        "kind": "amount_sum",
                        "row": row_key,
                        "currency": currency,
                        "operands": [left["field"], right["field"]],
                        "fact_ids": [left["fact_id"], right["fact_id"]],
                        "values": [total],
                        "status": "verified",
                    }
                )
        if 2 < len(items) <= 8:
            total = round(sum(float(item["value"]) for item in items), 6)
            formulas.append(
                {
                    "kind": "amount_sum",
                    "row": row_key,
                    "currency": currency,
                    "operands": [str(item["field"]) for item in items],
                    "fact_ids": [str(item["fact_id"]) for item in items],
                    "values": [total],
                    "status": "verified",
                }
            )
    return formulas


def _analysis_ratio_formulas(*, row_key: str, row: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    formulas: list[dict[str, Any]] = []
    cash_secured = _analysis_row_number(row, "cash_secured_cny")
    if cash_secured is not None and abs(cash_secured) >= 0.000001:
        for numerator_name in ("net_income_cny", "premium_income_cny", "realized_pnl_cny"):
            numerator = _analysis_row_number(row, numerator_name)
            if numerator is None:
                continue
            formulas.append(
                {
                    "kind": "ratio",
                    "row": row_key,
                    "output": numerator_name.replace("_cny", "_return_rate"),
                    "numerator": numerator_name,
                    "denominator": "cash_secured_cny",
                    "value": round(numerator / cash_secured, 8),
                    "policy": "weighted_recompute",
                    "status": "verified",
                }
            )
    rate_items = [
        item
        for field, item in row.items()
        if isinstance(item.get("value"), (int, float)) and ("rate" in field or item.get("unit") == "percent")
    ]
    for left_index, left in enumerate(rate_items[:8]):
        for right in rate_items[left_index + 1 : 8]:
            left_value = _normalized_rate_value(float(left["value"]))
            right_value = _normalized_rate_value(float(right["value"]))
            diff = round(left_value - right_value, 8)
            if abs(diff) < 0.000001:
                continue
            formulas.append(
                {
                    "kind": "rate_difference",
                    "row": row_key,
                    "operands": [left["field"], right["field"]],
                    "values": [diff, -diff, abs(diff)],
                    "unit": "rate",
                    "status": "verified",
                }
            )
    for denominator_name in _analysis_denominator_fields(row):
        denominator = _analysis_row_number(row, denominator_name)
        if denominator is None or abs(denominator) < 0.000001:
            continue
        for numerator_name in _analysis_contribution_numerator_fields(row, denominator_name=denominator_name):
            numerator = _analysis_row_number(row, numerator_name)
            if numerator is None:
                continue
            formulas.append(
                {
                    "kind": "contribution_share",
                    "row": row_key,
                    "numerator": numerator_name,
                    "denominator": denominator_name,
                    "value": round(numerator / denominator, 8),
                    "policy": "requires_denominator",
                    "status": "verified",
                }
            )
    return formulas


def _analysis_lifecycle_formulas(*, row_key: str, row: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    component_names = [
        "assigned_stock_unrealized_pnl",
        "assigned_stock_realized_pnl",
        "option_premium_attribution",
    ]
    components: list[dict[str, Any]] = []
    currencies: set[str] = set()
    for name in component_names:
        item = row.get(name)
        if not item or not isinstance(item.get("value"), (int, float)):
            return []
        components.append(item)
        currency = str(item.get("currency") or "").upper()
        if currency:
            currencies.add(currency)
    if len(currencies) != 1:
        return []
    total = round(sum(float(item["value"]) for item in components), 6)
    return [
        {
            "kind": "assigned_stock_lifecycle",
            "row": row_key,
            "currency": next(iter(currencies)),
            "operands": component_names,
            "fact_ids": [str(item["fact_id"]) for item in components],
            "values": [total],
            "status": "verified",
        }
    ]


def _analysis_fact_row_key(fact: EvidenceFact) -> str:
    source_path = str(fact.source_path or "")
    if not source_path.startswith("rows["):
        return ""
    return source_path.split("].", 1)[0] + "]" if "]." in source_path else ""


def _analysis_fact_field_name(fact: EvidenceFact) -> str:
    source_path = str(fact.source_path or "")
    if "." in source_path:
        return source_path.rsplit(".", 1)[-1].lower()
    return str(fact.path or "").rsplit(".", 1)[-1].lower()


def _analysis_money_formula_source(field: str) -> bool:
    name = str(field or "").lower()
    if any(token in name for token in ("diff", "difference", "delta")):
        return False
    if any(token in name for token in ("spot", "strike", "price", "per_share", "cash_secured")):
        return False
    if name.startswith("total_") or name.endswith("_total_cny"):
        return False
    return any(
        token in name
        for token in (
            "income",
            "pnl",
            "cashflow",
            "premium",
            "amount",
            "market_value",
            "cost_basis",
            "basis",
            "gross",
            "attribution",
        )
    )


def _analysis_row_number(row: dict[str, dict[str, Any]], field: str) -> float | None:
    item = row.get(field)
    value = item.get("value") if isinstance(item, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _normalized_rate_value(value: float) -> float:
    return value / 100.0 if abs(value) > 1 and abs(value) <= 100 else value


def _analysis_denominator_fields(row: dict[str, dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for field, item in row.items():
        if not isinstance(item.get("value"), (int, float)):
            continue
        if field.startswith("total_") or field.endswith("_total_cny") or field in {"total_income_cny", "total_amount_cny"}:
            fields.append(field)
    return fields


def _analysis_contribution_numerator_fields(row: dict[str, dict[str, Any]], *, denominator_name: str) -> list[str]:
    fields: list[str] = []
    for field, item in row.items():
        if field == denominator_name:
            continue
        if not isinstance(item.get("value"), (int, float)):
            continue
        if _analysis_money_formula_source(field):
            fields.append(field)
    return fields[:8]


def _fact_accounting_view(fact: EvidenceFact) -> str:
    path = fact.path.lower()
    if "net_cashflow" in path:
        return "cashflow"
    if "realized_gross" in path:
        return "realized_option_pnl"
    if "premium_received" in path or "option_premium_attribution" in path:
        return "option_premium_attribution"
    if "assigned_stock_unrealized_pnl" in path:
        return "assigned_stock_unrealized_pnl"
    if "assigned_stock_realized_pnl" in path:
        return "assigned_stock_realized_pnl"
    if "assignment_lifecycle_pnl" in path:
        return "assignment_lifecycle_pnl"
    if "remaining_market_value" in path:
        return "market_value"
    if "remaining_stock_cost_basis" in path:
        return "cost_basis"
    if "net_income_cny" in path:
        return "net_income_cny"
    return ""


def _include_fact_in_view_sum(fact: EvidenceFact, *, view: str, has_cashflow_summary: bool) -> bool:
    if view == "cashflow":
        if has_cashflow_summary:
            return fact.path == "summary[].net_cashflow_gross"
        return "cashflow_rows[]" in fact.path
    if view == "net_income_cny":
        return fact.path == "return_summary[].net_income_cny"
    return True


def _conflict_records(*, facts: list[EvidenceFact]) -> list[dict[str, Any]]:
    currencies_by_lot: dict[tuple[str, str], set[str]] = {}
    for fact in facts:
        if not fact.account or not fact.symbol or not fact.currency:
            continue
        key = (fact.account, fact.symbol)
        currencies_by_lot.setdefault(key, set()).add(fact.currency)
    conflicts: list[dict[str, Any]] = []
    for (account, symbol), currencies in sorted(currencies_by_lot.items()):
        if len(currencies) <= 1:
            continue
        conflicts.append(
            {
                "kind": "currency_conflict",
                "account": account,
                "symbol": symbol,
                "currencies": sorted(currencies),
                "impact": "facts for one account/symbol carry multiple currencies",
            }
        )
    return conflicts


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _time_range_for_months(months: list[str]) -> dict[str, str] | None:
    parsed: list[date] = []
    for month in months:
        try:
            year_text, month_text = str(month).split("-", 1)
            parsed.append(date(int(year_text), int(month_text), 1))
        except Exception:
            continue
    if not parsed:
        return None
    start = min(parsed)
    end_month = max(parsed)
    end_day = calendar.monthrange(end_month.year, end_month.month)[1]
    end = date(end_month.year, end_month.month, end_day)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for record in records:
        compact = {key: value for key, value in record.items() if value not in (None, "", [], {})}
        marker = tuple(sorted((str(key), str(value)) for key, value in compact.items()))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(compact)
    return out


__all__ = [
    "DIAGNOSTIC_EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_BUNDLE_SCHEMA_VERSION",
    "EvidenceBundle",
    "EvidenceFact",
    "build_evidence_bundle",
]
