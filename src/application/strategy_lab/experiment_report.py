from __future__ import annotations

from typing import Any

from src.application.strategy_lab.dataset_contracts import StrategyLabDataset


def build_strategy_lab_experiment_report(result: dict[str, Any], *, dataset: StrategyLabDataset) -> dict[str, Any]:
    summary = {
        "experiment_id": result.get("experiment_id"),
        "dataset_id": dataset.dataset_id,
        "status": result.get("status"),
        "recommendation": (result.get("recommendation") or {}).get("recommendation"),
        "reason": (result.get("recommendation") or {}).get("reason"),
        "sample": (result.get("preflight") or {}).get("sample"),
        "warning_count": len(result.get("warnings") or []),
    }
    return {
        "summary": summary,
        "markdown": render_strategy_lab_experiment_markdown(result, dataset=dataset),
    }


def render_strategy_lab_experiment_markdown(result: dict[str, Any], *, dataset: StrategyLabDataset) -> str:
    scope = dict(dataset.scope)
    recommendation = dict(result.get("recommendation") or {})
    preflight = dict(result.get("preflight") or {})
    sample = dict(preflight.get("sample") or {})
    lines = [
        "# Strategy Lab 实验报告",
        "",
        "## 结论",
        f"- 状态：{_display(result.get('status'))}",
        f"- 建议：{_display(recommendation.get('recommendation'))}",
        f"- 原因：{_display(recommendation.get('reason'))}",
        f"- Dataset：{dataset.dataset_id}",
        f"- 实验：{_display(result.get('experiment_id'))}",
        "",
        "## 范围",
        f"- 市场：{_display(scope.get('market'))}",
        f"- 账号：{_display(scope.get('account'))}",
        f"- 策略：{_display(scope.get('strategy_type'))}",
        f"- 窗口：{_display(scope.get('start_date'))} 至 {_display(scope.get('end_date'))}",
        "",
        "## 样本质量",
        f"- candidate_count：{sample.get('candidate_count', 0)}",
        f"- outcome_count：{sample.get('outcome_count', 0)}",
        f"- reject_count：{sample.get('reject_count', 0)}",
        f"- trace_count：{sample.get('trace_count', 0)}",
        f"- trade_event_count：{sample.get('trade_event_count', 0)}",
        f"- position_lot_count：{sample.get('position_lot_count', 0)}",
    ]
    if result.get("status") == "evaluable":
        lines.extend(
            [
                "",
                "## Baseline",
                *_metric_lines(result.get("baseline_metrics")),
                "",
                "## Candidate",
                *_metric_lines(result.get("candidate_metrics")),
                "",
                "## 对比",
                *_comparison_lines(result.get("comparison")),
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## 缺失项",
                *_list_lines(preflight.get("missing")),
                "",
                "## 补样建议",
                *_list_lines(preflight.get("next_actions")),
            ]
        )
    lines.extend(["", "## 风险提示", *_list_lines(result.get("warnings"))])
    return "\n".join(lines).rstrip() + "\n"


def _metric_lines(metrics: Any) -> list[str]:
    if not isinstance(metrics, dict):
        return ["- 暂无：样本未通过 preflight"]
    returns = dict(metrics.get("returns") or {})
    capital = dict(metrics.get("capital") or {})
    risk = dict(metrics.get("risk") or {})
    execution = dict(metrics.get("execution") or {})
    decision = dict(metrics.get("decision") or {})
    return [
        f"- selected_count：{_number(execution.get('selected_count'))}",
        f"- sample_size：{_number(decision.get('sample_size'))}",
        f"- confidence_level：{_display(decision.get('confidence_level'))}",
        f"- net_cash_inflow：{_money(returns.get('net_cash_inflow'))}",
        f"- realized_pnl：{_money(returns.get('realized_pnl'))}",
        f"- locked_cash_days：{_money(capital.get('locked_cash_days'))}",
        f"- return_per_locked_cash_day：{_ratio(capital.get('return_per_locked_cash_day'))}",
        f"- assignment_rate：{_ratio(risk.get('assignment_rate'))}",
        f"- strike_breach_rate：{_ratio(risk.get('strike_breach_rate'))}",
        f"- worst_trade_pnl：{_money(risk.get('worst_trade_pnl'))}",
        f"- tail_loss_scenario：{_money(risk.get('tail_loss_scenario'))}",
    ]


def _comparison_lines(comparison: Any) -> list[str]:
    if not isinstance(comparison, dict):
        return ["- 暂无"]
    return [
        f"- net_cash_inflow_lift：{_money(comparison.get('net_cash_inflow_lift'))}",
        f"- realized_pnl_lift：{_money(comparison.get('realized_pnl_lift'))}",
        f"- return_per_locked_cash_day_lift：{_ratio(comparison.get('return_per_locked_cash_day_lift'))}",
        f"- tail_loss_scenario_change：{_money(comparison.get('tail_loss_scenario_change'))}",
        f"- risk_worsening：{'yes' if comparison.get('risk_worsening') else 'no'}",
    ]


def _list_lines(values: Any) -> list[str]:
    items = [str(item) for item in (values or []) if str(item).strip()]
    return [f"- {item}" for item in items] or ["- 无"]


def _display(value: Any) -> str:
    text = str(value or "").strip()
    return text or "-"


def _number(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return str(int(value))
    except Exception:
        return str(value)


def _money(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return str(value)


def _ratio(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.6f}"
    except Exception:
        return str(value)

