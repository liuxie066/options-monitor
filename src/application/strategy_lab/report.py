from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from src.application.strategy_lab.contracts import BacktestResult, MetricSet


@dataclass(frozen=True)
class StrategyLabReport:
    summary: Mapping[str, Any]
    markdown: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", MappingProxyType(dict(self.summary)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": dict(self.summary),
            "markdown": self.markdown,
        }


def build_strategy_lab_report(result: BacktestResult) -> StrategyLabReport:
    summary = _summary(result)
    lines = [
        "# Strategy Lab 回测报告",
        "",
        "## 结论",
        f"- 判断：{summary['conclusion_text']}",
        f"- 实验：{summary['experiment_id']}",
        f"- 策略：{summary['strategy_type']}",
        f"- 账号：{_display(summary.get('account'))}",
        "",
        "## 实验范围",
        f"- Baseline policy：{result.experiment.baseline_policy.name}",
        f"- Candidate policy：{result.experiment.candidate_policy.name}",
        f"- 开始日期：{_display(result.experiment.start_date)}",
        f"- 结束日期：{_display(result.experiment.end_date)}",
        f"- 证据文件数：{result.evidence.summary().get('artifact_count', 0)}",
        f"- 候选行数：{result.evidence.summary().get('candidate_count', 0)}",
        f"- 拒绝日志行数：{result.evidence.summary().get('reject_log_count', 0)}",
        "",
        "## Baseline 指标",
        *_metric_lines(result.baseline_metrics),
        "",
        "## Candidate 指标",
        *_metric_lines(result.candidate_metrics),
        "",
        "## 收益差异",
        f"- net_cash_inflow_lift：{_money(result.comparison.get('net_cash_inflow_lift'))}",
        f"- realized_pnl_lift：{_money(result.comparison.get('realized_pnl_lift'))}",
        f"- selected_count_lift：{_number(result.comparison.get('selected_count_lift'))}",
        "",
        "## 风险差异",
        f"- risk_worsening：{_yes_no(result.comparison.get('risk_worsening'))}",
        f"- tail_loss_scenario_change：{_money(result.comparison.get('tail_loss_scenario_change'))}",
        "",
        "## 资金效率差异",
        f"- return_per_locked_cash_day_lift：{_ratio(result.comparison.get('return_per_locked_cash_day_lift'))}",
        "",
        "## 数据质量 warning",
        *_warning_lines(result.warnings),
        "",
        "## 下一步建议",
        *_next_step_lines(result),
    ]
    return StrategyLabReport(summary=summary, markdown="\n".join(lines).rstrip() + "\n")


def render_strategy_lab_markdown(result: BacktestResult) -> str:
    return build_strategy_lab_report(result).markdown


def _summary(result: BacktestResult) -> dict[str, Any]:
    baseline_selected = _int_metric(result.baseline_metrics.execution.get("selected_count"))
    candidate_selected = _int_metric(result.candidate_metrics.execution.get("selected_count"))
    baseline_sample = _int_metric(result.baseline_metrics.decision.get("sample_size"))
    candidate_sample = _int_metric(result.candidate_metrics.decision.get("sample_size"))
    return {
        "experiment_id": result.experiment.experiment_id,
        "strategy_type": result.experiment.strategy_type,
        "account": result.experiment.account,
        "conclusion": result.conclusion,
        "conclusion_text": _conclusion_text(result.conclusion),
        "baseline_selected_count": baseline_selected,
        "candidate_selected_count": candidate_selected,
        "baseline_sample_size": baseline_sample,
        "candidate_sample_size": candidate_sample,
        "net_cash_inflow_lift": result.comparison.get("net_cash_inflow_lift"),
        "realized_pnl_lift": result.comparison.get("realized_pnl_lift"),
        "return_per_locked_cash_day_lift": result.comparison.get("return_per_locked_cash_day_lift"),
        "risk_worsening": _bool_metric(result.comparison.get("risk_worsening")),
        "warning_count": len(result.warnings),
        "warnings": list(result.warnings),
    }


def _metric_lines(metrics: MetricSet) -> list[str]:
    return [
        f"- candidate_count：{_number(metrics.execution.get('candidate_count'))}",
        f"- selected_count：{_number(metrics.execution.get('selected_count'))}",
        f"- sample_size：{_number(metrics.decision.get('sample_size'))}",
        f"- confidence_level：{_display(metrics.decision.get('confidence_level'))}",
        f"- net_cash_inflow：{_money(metrics.returns.get('net_cash_inflow'))}",
        f"- net_premium：{_money(metrics.returns.get('net_premium'))}",
        f"- realized_pnl：{_money(metrics.returns.get('realized_pnl'))}",
        f"- annualized_return_on_locked_cash：{_ratio(metrics.returns.get('annualized_return_on_locked_cash'))}",
        f"- locked_cash_days：{_money(metrics.capital.get('locked_cash_days'))}",
        f"- return_per_locked_cash_day：{_ratio(metrics.capital.get('return_per_locked_cash_day'))}",
        f"- assignment_rate：{_ratio(metrics.risk.get('assignment_rate'))}",
        f"- strike_breach_rate：{_ratio(metrics.risk.get('strike_breach_rate'))}",
        f"- worst_trade_pnl：{_money(metrics.risk.get('worst_trade_pnl'))}",
        f"- tail_loss_scenario：{_money(metrics.risk.get('tail_loss_scenario'))}",
    ]


def _warning_lines(warnings: tuple[str, ...]) -> list[str]:
    if not warnings:
        return ["- 无"]
    return [f"- {item}" for item in warnings]


def _next_step_lines(result: BacktestResult) -> list[str]:
    lines: list[str] = []
    if result.comparison.get("risk_worsening"):
        lines.append("- 风险变差，先缩小样本或收紧筛选条件，不进入影子观察。")
    if result.conclusion == "reject":
        lines.append("- 当前证据不支持继续推进，保留实验记录即可。")
    elif result.conclusion == "watch":
        lines.append("- 先补充样本和数据质量，不改线上策略参数。")
    elif result.conclusion == "shadow":
        lines.append("- 可以进入影子观察，但只记录结果，不自动改线上策略。")
    elif result.conclusion == "candidate":
        lines.append("- 可以进入人工评审，确认风险预算和回撤口径后再考虑上线。")
    if result.warnings:
        lines.append("- 先处理数据质量 warning，再比较策略优劣。")
    return lines or ["- 暂无额外动作。"]


def _conclusion_text(conclusion: str) -> str:
    if conclusion == "reject":
        return "不建议进入下一步"
    if conclusion == "watch":
        return "先观察，不进入影子运行"
    if conclusion == "shadow":
        return "可以进入影子观察"
    if conclusion == "candidate":
        return "可以作为候选策略继续评审"
    return conclusion


def _display(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"


def _money(value: Any) -> str:
    number = _float_metric(value)
    if number is None:
        return "-"
    return f"{number:,.2f}"


def _ratio(value: Any) -> str:
    number = _float_metric(value)
    if number is None:
        return "-"
    return f"{number:.6f}"


def _number(value: Any) -> str:
    number = _float_metric(value)
    if number is None:
        return "-"
    if number.is_integer():
        return str(int(number))
    return f"{number:.6f}"


def _yes_no(value: Any) -> str:
    return "是" if _bool_metric(value) else "否"


def _int_metric(value: Any) -> int | None:
    number = _float_metric(value)
    if number is None:
        return None
    return int(number)


def _float_metric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _bool_metric(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否"}:
        return False
    return False
