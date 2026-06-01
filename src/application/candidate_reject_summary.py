from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from src.application.candidate_filter_trace import read_candidate_filter_trace


SCHEMA_VERSION = "candidate_reject_summary.v1"

REJECT_STATUSES = {"rejected", "post_filtered"}
SCAN_FUNCTIONS = {
    "sell_put",
    "sell_call",
    "yield_enhancement",
    "cash_reserve",
    "share_coverage",
}

RISK_ALERT_RULES = {"event_source_unavailable"}

FUNCTION_LABELS = {
    "sell_put": "Sell Put",
    "sell_call": "Covered Call",
    "yield_enhancement": "收益增强",
    "cash_reserve": "现金过滤",
    "share_coverage": "覆盖能力",
}

CATEGORY_LABELS = {
    "data_missing": "数据缺失",
    "vol_edge": "波动率边际不足",
    "liquidity": "流动性不足",
    "risk_budget": "风险预算超限",
    "event_risk": "事件风险",
    "return_floor": "收益门槛不足",
    "cash_or_coverage": "资金或覆盖不足",
    "yield_enhancement": "收益增强组合不成立",
    "hard_constraints": "基础条件不符",
    "other": "其他",
}

RULE_LABELS = {
    "volatility_estimate_missing": "RV 缺失",
    "implied_volatility_missing": "IV 缺失",
    "delta_missing": "Delta 缺失",
    "event_source_unavailable": "事件风险数据源不可用",
    "vol_edge_ratio_below_min": "IV/RV 不足",
    "vol_edge_spread_below_min": "IV-RV 不足",
    "risk_open_interest": "OI 不足",
    "risk_volume": "成交量不足",
    "risk_spread": "价差不合格",
    "single_trade_concentration_exceeded": "单笔集中度超限",
    "symbol_concentration_exceeded": "单标的集中度超限",
    "total_short_put_concentration_exceeded": "总 short put 集中度超限",
    "put_sigma_stress_loss_exceeded": "2σ 压力亏损超限",
    "put_gap_down_stress_loss_exceeded": "gap-down 压力亏损超限",
    "call_gap_up_opportunity_cost_nav_exceeded": "右尾机会成本超限",
    "call_gap_up_opportunity_cost_premium_exceeded": "右尾成本/权利金过高",
    "path_stress_inputs_missing": "路径压力数据缺失",
    "concentration_not_evaluable": "集中度不可评估",
    "event_risk_within_expiry": "到期前存在事件",
    "risk_event_reject": "事件风险拒绝",
    "return_annualized": "年化收益不足",
    "return_net_income": "净收入不足",
    "hard_dte": "DTE 不符合",
    "hard_strike": "行权价不符合",
    "input_missing": "基础字段缺失",
    "candidate_metrics_unavailable": "候选指标不可用",
    "metrics_mid_non_positive": "mid 不可用",
    "usd_cash_insufficient": "USD 现金不足",
    "cny_cash_insufficient": "CNY 现金不足",
    "total_cny_cash_insufficient": "总 CNY 现金不足",
    "cash_secured_unavailable": "担保现金不可评估",
    "hard_capacity_put": "Put 资金容量不足",
    "hard_capacity_call": "Call 覆盖能力不足",
    "yield_enhancement_put_universe_empty": "Put 候选为空",
    "yield_enhancement_no_pair": "没有可配对 Call",
    "yield_enhancement_no_recommended_pair": "没有推荐组合",
}


def build_candidate_reject_summary(
    *,
    trace_path: Path | str | None = None,
    reject_log_paths: list[Path | str] | tuple[Path | str, ...] | None = None,
    account: str | None = None,
    run_id: str | None = None,
    max_categories: int = 6,
    max_rules_per_category: int = 5,
) -> dict[str, Any]:
    trace_rows = _read_trace_rows(trace_path)
    reject_log_rows = _read_reject_log_rows(reject_log_paths or [])
    source = "trace" if trace_rows else ("reject_log" if reject_log_rows else "none")
    raw_rows = trace_rows if trace_rows else reject_log_rows

    account_norm = _clean(account).lower()
    run_id_norm = _clean(run_id)
    rows = [
        row
        for row in raw_rows
        if _matches_account(row, account_norm)
        and _matches_run_id(row, run_id_norm)
        and _clean(row.get("function")).lower() in SCAN_FUNCTIONS
    ]
    accepted_rows = [row for row in rows if _clean(row.get("status")).lower() == "accepted"]
    rejected_rows = [row for row in rows if _row_is_rejection(row)]

    function_counts = Counter(_clean(row.get("function")).lower() for row in rejected_rows)
    accepted_function_counts = Counter(_clean(row.get("function")).lower() for row in accepted_rows)
    status_counts = Counter(_clean(row.get("status")).lower() or "rejected" for row in rows)

    category_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rejected_rows:
        category_rows[_category_for_row(row)].append(row)

    top_categories: list[dict[str, Any]] = []
    for category, grouped_rows in sorted(
        category_rows.items(),
        key=lambda item: (-len(item[1]), _category_sort_key(item[0])),
    )[: max(0, int(max_categories or 0))]:
        rule_counts = Counter(_rule_for_row(row) for row in grouped_rows)
        sample_symbols = _sample_symbols(grouped_rows)
        top_categories.append(
            {
                "category": category,
                "label": CATEGORY_LABELS.get(category, category),
                "count": len(grouped_rows),
                "rule_counts": dict(rule_counts.most_common(max(1, int(max_rules_per_category or 1)))),
                "rule_labels": {
                    rule: _rule_label(rule)
                    for rule, _count in rule_counts.most_common(max(1, int(max_rules_per_category or 1)))
                },
                "function_counts": dict(Counter(_clean(row.get("function")).lower() for row in grouped_rows).most_common()),
                "sample_symbols": sample_symbols,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "available": source != "none",
        "source": source,
        "trace_path": str(trace_path) if trace_path is not None else None,
        "reject_log_paths": [str(path) for path in (reject_log_paths or [])],
        "account": account_norm or None,
        "run_id": run_id_norm or None,
        "accepted_count": len(accepted_rows),
        "accepted_function_counts": dict(accepted_function_counts.most_common()),
        "total_rejected": len(rejected_rows),
        "status_counts": dict(status_counts.most_common()),
        "function_counts": dict(function_counts.most_common()),
        "risk_alerts": _risk_alerts(rejected_rows),
        "top_categories": top_categories,
    }


def render_candidate_reject_summary(summary: dict[str, Any], *, max_categories: int = 3) -> str:
    if not isinstance(summary, dict):
        return ""
    if not bool(summary.get("available")):
        return "### 拒绝摘要\n- 拒绝摘要不可用：未找到 candidate_filter_trace.jsonl 或 reject_log.csv\n"

    total_rejected = _int(summary.get("total_rejected"))
    accepted_count = _int(summary.get("accepted_count"))
    lines = ["### 拒绝摘要"]
    lines.append(f"- 通过 {accepted_count} 条；拒绝/后过滤 {total_rejected} 条")
    function_line = _format_function_counts(summary.get("function_counts"))
    if function_line:
        lines.append(f"- 涉及模块：{function_line}")
    risk_alerts = _format_risk_alerts(summary.get("risk_alerts"))
    if risk_alerts:
        lines.extend(f"- 风控注意：{item}" for item in risk_alerts)

    top_categories = summary.get("top_categories")
    if not isinstance(top_categories, list) or not top_categories:
        lines.append("- 未记录主要拒绝原因")
        return "\n".join(lines).strip() + "\n"

    display_categories = [
        item
        for item in top_categories
        if not (isinstance(item, dict) and risk_alerts and _category_only_has_risk_alerts(item))
    ]
    for item in display_categories[: max(0, int(max_categories or 0))]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("category") or "其他")
        count = _int(item.get("count"))
        rules = _format_rule_counts(item.get("rule_counts"), item.get("rule_labels"))
        samples = _format_samples(item.get("sample_symbols"))
        detail = f"{label} {count}"
        if rules:
            detail += f"：{rules}"
        if samples:
            detail += f"；样例 {samples}"
        lines.append(f"- {detail}")
    return "\n".join(lines).strip() + "\n"


def append_candidate_reject_summary_to_text(
    text: str,
    *,
    trace_path: Path | str | None,
    reject_log_paths: list[Path | str] | tuple[Path | str, ...] | None = None,
    account: str | None = None,
    run_id: str | None = None,
) -> str:
    summary = build_candidate_reject_summary(
        trace_path=trace_path,
        reject_log_paths=reject_log_paths,
        account=account,
        run_id=run_id,
    )
    rendered = render_candidate_reject_summary(summary)
    if not rendered.strip():
        return str(text or "").strip()
    base = str(text or "").strip()
    if not base:
        return rendered.strip()
    return (base + "\n\n" + rendered.strip()).strip()


def _read_trace_rows(path: Path | str | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    source = Path(path)
    if not source.exists() or not source.is_file():
        return []
    return read_candidate_filter_trace(source)


def _read_reject_log_rows(paths: list[Path | str] | tuple[Path | str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists() or not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                for row in csv.DictReader(fh):
                    if isinstance(row, dict):
                        rows.append(_reject_log_row_to_trace_like(row))
        except Exception:
            continue
    return rows


def _reject_log_row_to_trace_like(row: dict[str, Any]) -> dict[str, Any]:
    mode = _clean(row.get("mode")).lower()
    function = "sell_call" if mode == "call" else "sell_put"
    return {
        **row,
        "function": row.get("function") or function,
        "status": row.get("status") or "rejected",
        "stage": row.get("engine_reject_stage") or row.get("reject_stage"),
        "rule": row.get("engine_reject_reason") or row.get("reject_rule") or row.get("reject_reason"),
    }


def _row_is_rejection(row: dict[str, Any]) -> bool:
    status = _clean(row.get("status")).lower()
    if status:
        return status in REJECT_STATUSES
    return bool(_rule_for_row(row))


def _category_for_row(row: dict[str, Any]) -> str:
    function = _clean(row.get("function")).lower()
    rule = _rule_for_row(row)
    rule_l = rule.lower()
    message_l = _clean(row.get("message")).lower()
    combined = f"{rule_l} {message_l}"

    if function == "yield_enhancement":
        return "yield_enhancement"
    if any(
        token in combined
        for token in (
            "event_risk_within",
            "event_source_unavailable",
            "risk_event",
            "event risk exists",
        )
    ):
        return "event_risk"
    if any(token in combined for token in ("missing", "unavailable", "required_data", "not_evaluable", "metrics_")):
        return "data_missing"
    if any(token in combined for token in ("vol_edge", "iv/rv", "iv-rv")):
        return "vol_edge"
    if any(token in combined for token in ("open_interest", "volume", "spread", "liquidity")):
        return "liquidity"
    if any(token in combined for token in ("concentration", "stress", "gap_down", "gap_up", "opportunity_cost")):
        return "risk_budget"
    if any(token in combined for token in ("cash", "capacity", "cover", "coverage", "shares")):
        return "cash_or_coverage"
    if any(token in combined for token in ("return_", "annualized", "net_income", "net_credit")):
        return "return_floor"
    if any(token in combined for token in ("hard_", "dte", "strike", "otm", "input_")):
        return "hard_constraints"
    return "other"


def _rule_for_row(row: dict[str, Any]) -> str:
    return _clean(
        row.get("rule")
        or row.get("engine_reject_reason")
        or row.get("reject_rule")
        or row.get("reject_reason")
        or "unknown"
    )


def _rule_label(rule: str) -> str:
    raw = _clean(rule)
    if raw in RULE_LABELS:
        return RULE_LABELS[raw]
    lower = raw.lower()
    if lower.startswith("required_data_missing_"):
        return "required data 缺失"
    if "cash_insufficient" in lower:
        return "现金不足"
    if "spread" in lower:
        return "价差不合格"
    if "volume" in lower:
        return "成交量不足"
    if "open_interest" in lower:
        return "OI 不足"
    return raw


def _risk_alerts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rule = _rule_for_row(row)
        if rule in RISK_ALERT_RULES:
            grouped[rule].append(row)
    alerts: list[dict[str, Any]] = []
    for rule, grouped_rows in sorted(grouped.items()):
        alerts.append(
            {
                "rule": rule,
                "label": _rule_label(rule),
                "count": len(grouped_rows),
                "sample_symbols": _sample_symbols(grouped_rows),
            }
        )
    return alerts


def _format_risk_alerts(raw_alerts: Any) -> list[str]:
    if not isinstance(raw_alerts, list):
        return []
    out: list[str] = []
    for item in raw_alerts:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("rule") or "").strip()
        count = _int(item.get("count"))
        if not label or count <= 0:
            continue
        detail = f"{label} {count}"
        samples = _format_samples(item.get("sample_symbols"))
        if samples:
            detail += f"；样例 {samples}"
        out.append(detail)
    return out


def _category_only_has_risk_alerts(item: dict[str, Any]) -> bool:
    raw_rules = item.get("rule_counts")
    if not isinstance(raw_rules, dict) or not raw_rules:
        return False
    rules = {str(rule) for rule in raw_rules}
    return bool(rules) and rules.issubset(RISK_ALERT_RULES)


def _format_rule_counts(raw_counts: Any, raw_labels: Any) -> str:
    if not isinstance(raw_counts, dict):
        return ""
    labels = raw_labels if isinstance(raw_labels, dict) else {}
    parts: list[str] = []
    for rule, count in list(raw_counts.items())[:3]:
        label = str(labels.get(rule) or _rule_label(str(rule)))
        parts.append(f"{label} {int(count or 0)}")
    return "，".join(parts)


def _format_function_counts(raw_counts: Any) -> str:
    if not isinstance(raw_counts, dict):
        return ""
    parts: list[str] = []
    for function, count in list(raw_counts.items())[:4]:
        label = FUNCTION_LABELS.get(str(function), str(function))
        parts.append(f"{label} {int(count or 0)}")
    return " / ".join(parts)


def _sample_symbols(rows: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        symbol = _clean(row.get("symbol") or row.get("underlying_symbol")).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
        if len(out) >= limit:
            break
    return out


def _format_samples(raw_samples: Any) -> str:
    if not isinstance(raw_samples, list):
        return ""
    samples = [str(item) for item in raw_samples if str(item).strip()]
    return "、".join(samples[:3])


def _matches_account(row: dict[str, Any], account: str) -> bool:
    if not account:
        return True
    row_account = _clean(row.get("account")).lower()
    return (not row_account) or row_account == account


def _matches_run_id(row: dict[str, Any], run_id: str) -> bool:
    if not run_id:
        return True
    row_run_id = _clean(row.get("run_id"))
    return (not row_run_id) or row_run_id == run_id


def _category_sort_key(category: str) -> int:
    order = {
        "event_risk": 0,
        "data_missing": 1,
        "vol_edge": 2,
        "liquidity": 3,
        "risk_budget": 4,
        "return_floor": 5,
        "cash_or_coverage": 6,
        "yield_enhancement": 7,
        "hard_constraints": 8,
        "other": 9,
    }
    return order.get(category, 99)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
