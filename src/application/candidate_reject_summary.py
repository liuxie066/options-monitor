from __future__ import annotations

"""Human labels for Candidate Engine rejection reason codes."""


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
    "metrics_net_income_non_positive": "净收入非正",
    "usd_cash_insufficient": "USD 现金不足",
    "cny_cash_insufficient": "CNY 现金不足",
    "total_cny_cash_insufficient": "总 CNY 现金不足",
    "cash_secured_unavailable": "担保现金不可评估",
    "hard_capacity_put": "Put 资金容量不足",
    "hard_capacity_call": "Call 覆盖能力不足",
    "combo_yield_put_universe_empty": "Put 候选为空",
    "combo_yield_put_cash_filtered": "Put 现金不足",
    "combo_yield_no_pair": "没有可配对 Call",
    "combo_yield_no_recommended_pair": "没有推荐组合",
    "yield_enhancement_put_universe_empty": "Put 候选为空",
    "yield_enhancement_no_pair": "没有可配对 Call",
    "yield_enhancement_no_recommended_pair": "没有推荐组合",
}


def candidate_rule_label(rule: str) -> str:
    raw = str(rule or "").strip()
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
