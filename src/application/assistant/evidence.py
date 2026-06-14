from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import calendar
from typing import Any


EVIDENCE_BUNDLE_SCHEMA_VERSION = "om-agent-evidence-bundle-v1"
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
            "missing_data_count": len(self.missing_data),
            "conflict_count": len(self.conflicts),
            "sources": sorted({item.get("source_label") for item in self.datasets if item.get("source_label")}),
            "tools": sorted({item.get("tool_name") for item in self.datasets if item.get("tool_name")}),
            "guard_profiles": sorted({item.get("guard_profile") for item in self.guard_contracts if item.get("guard_profile")}),
        }


def build_evidence_bundle(
    *,
    question: str,
    plan: dict[str, Any],
    observations: list[dict[str, Any]],
) -> EvidenceBundle:
    datasets: list[dict[str, Any]] = []
    facts: list[EvidenceFact] = []
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
    "EVIDENCE_BUNDLE_SCHEMA_VERSION",
    "EvidenceBundle",
    "EvidenceFact",
    "build_evidence_bundle",
]
