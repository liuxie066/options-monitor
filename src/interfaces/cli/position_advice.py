"""Human projection of the existing assessment contract; no policy or I/O."""
from __future__ import annotations

import json
from typing import Any


def _value(value: Any, *, percent: bool = False) -> str:
    if value is None or value == "":
        return "不可用"
    if percent and isinstance(value, (int, float)):
        return f"{value:.2%}"
    return str(value)


def render_position_advice(result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        error = result["error"]
        lines = [f"持仓评估执行失败 [{error['code']}]：{error['message']}"]
        if error.get("hint"):
            lines.append(error["hint"])
        return "\n".join(lines) + "\n"
    status = result.get("status")
    label = {"success": "成功", "partial": "部分结果（含不可评估持仓）",
             "disabled": "持仓建议未启用", "unavailable": "持仓建议插件不可用"}.get(status, _value(status))
    rows = result.get("rows", [])
    lines = [f"持仓评估：{label}"]
    if result.get("reason"):
        lines.append(str(result["reason"]))
    if result.get("account"):
        lines.append(f"账户：{result['account']}；请求时间：{_value(result.get('evaluated_at_utc'))}")
    if status == "success" and not rows:
        lines.append("无匹配持仓（合法空结果）")
    for row in rows:
        mode = row.get("assessment_mode")
        mode_label = {"current": "当前评估", "historical_reference": "历史参考（不代表当前建议）",
                      "unavailable": "不可评估"}.get(mode, "不可评估")
        lines.extend([
            "",
            f"合约：{_value(row.get('symbol'))} {_value(row.get('option_type'))} "
            f"{_value(row.get('expiration') or row.get('expiration_ymd'))} "
            f"行权价 {_value(row.get('strike'))}；张数 {_value(row.get('contracts_open'))}",
            f"评估模式：{mode_label}；数据时间：{_value(row.get('assessment_as_of_utc'))}",
            f"现价：{_value(row.get('spot'))}；买/卖价：{_value(row.get('bid'))}/{_value(row.get('ask'))}；DTE：{_value(row.get('dte'))}",
            f"净兑现比例：{_value(row.get('net_capture_ratio'), percent=True)}；"
            f"剩余最高年化：{_value(row.get('remaining_max_annualized_return'), percent=True)}；"
            f"资金分母：{_value(row.get('capital_basis'))} {_value(row.get('currency'))}",
        ])
        decision = row.get("recommendation_state")
        labels = {"hold": "本策略未触发平仓条件（hold）", "close": "可考虑买回平仓（close）"}
        if mode == "historical_reference":
            reference = row.get("reference_recommendation_state")
            lines.append(f"历史结论：{labels.get(reference, '不可评估')}；当前不可据此平仓")
        elif mode == "current" and row.get("current_actionable") is True:
            lines.append(f"结论：{labels.get(decision, '不可评估')}")
        else:
            lines.append("结论：不可评估")
        lines.append(f"原因：{_value(row.get('reason'))}")
    if result.get("report_path"):
        lines.append(f"\n独立报告：{result['report_path']}")
    lines.append("hold 仅表示现有策略未触发平仓条件；休市历史参考不代表当前平仓建议。")
    return "\n".join(lines) + "\n"


def _roll_money(value: Any, currency: Any) -> str:
    amount = f"{value:.2f}" if isinstance(value, (int, float)) else _value(value)
    return f"{amount} {currency}" if currency else amount


def _roll_diagnostic(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _value(value)


def render_short_put_roll(result: dict[str, Any]) -> str:
    """Project the roll service's values without recomputing policy or ranking."""
    if result.get("ok") is False:
        error = result["error"]
        lines = [f"Short Put 向下移仓执行失败 [{error['code']}]：{error['message']}"]
        if error.get("hint"):
            lines.append(error["hint"])
        return "\n".join(lines) + "\n"

    status = result.get("status")
    status_label = {
        "success": "请求完成（success）",
        "partial": "部分结果（partial）",
        "disabled": "持仓建议插件未启用（disabled）",
        "unavailable": "查询不可用（unavailable）",
    }.get(status, _value(status))
    reasons = result.get("reason_codes") or []
    if status == "unavailable" and any(
        reason in {"position_advice_unavailable", "position_advice_roll_unavailable"} for reason in reasons
    ):
        status_label = "持仓建议插件不可用（unavailable）"
    outcome = result.get("outcome")
    outcome_label = {
        "found": "找到候选（found）",
        "none": "无合格候选（none，覆盖完整的合法空结果）",
        "unable": "不可评估（unable，缺失数据或证据不足）",
        "not_applicable": "范围外（not_applicable）：旧仓价外幅度超过 5%，未搜索移仓候选",
    }.get(outcome, _value(outcome))
    lines = ["Short Put 向下移仓", f"执行状态：{status_label}", f"筛选结果：{outcome_label}"]
    lines.append(f"账户：{_value(result.get('account'))}；评估时间：{_value(result.get('evaluated_at_utc'))}")
    position = result.get("position") or {}
    currency = position.get("currency")
    lines.extend([
        f"旧仓：{_value(position.get('symbol'))} put {_value(position.get('expiration'))}；"
        f"strike {_value(position.get('strike'))}；张数 {_value(position.get('contracts_open'))}；"
        f"乘数 {_value(position.get('multiplier'))}；币种 {_value(currency)}",
        f"现价：{_value(position.get('spot'))}；旧仓价外幅度：{_value(position.get('old_otm_pct'), percent=True)}（负值为价内）；"
        f"买回总支出：{_roll_money(position.get('old_close_cost'), currency)}",
        f"旧仓继续持有到期归零最高年化：{_value(position.get('old_remaining_max_annualized_return'), percent=True)}",
        "范围：单账户、单 lot、全部剩余张数；旧仓价内/平值或价外幅度 ≤5%；新 put 同标的、降 strike 且严格价外。",
    ])
    if position.get("lot_id"):
        lines.append(f"旧 lot：{position['lot_id']}（本次仅处理这一笔）")
    scope = result.get("scope")
    if scope:
        minimum = scope.get("min_new_strike")
        minimum_label = "未限制" if minimum is None else _value(minimum)
        lines.append(
            f"请求范围：最大新到期日 {_value(scope.get('max_new_expiration'))}（无默认）；"
            f"最低新 strike {minimum_label}；新到期日不得早于旧到期日。"
        )
        if "enumerated_expirations" in scope:
            expirations = scope["enumerated_expirations"]
            lines.append(f"实际已请求到期日：{_roll_diagnostic(expirations) if expirations else '不可用（未提供或未枚举）'}")
    else:
        lines.append("请求到期/strike 范围：未提供诊断，请核对原请求 JSON；最大新到期日无默认值。")
    coverage = result.get("coverage")
    if coverage:
        lines.append(
            f"覆盖：{_value(coverage.get('status'))}；枚举完整：{_value(coverage.get('enumeration_complete'))}；"
            f"发现 {_value(coverage.get('discovered'))} / 已评价 {_value(coverage.get('evaluated'))} / "
            f"匹配 {_value(coverage.get('matched'))} / 排除 {_value(coverage.get('excluded'))} / "
            f"不可评估 {_value(coverage.get('unassessed'))}"
        )
        if coverage.get("reason_codes"):
            lines.append(f"覆盖原因：{_roll_diagnostic(coverage['reason_codes'])}")
    else:
        lines.append("覆盖：未搜索（范围外）" if outcome == "not_applicable" else "覆盖：不可用")
    if status == "partial":
        lines.append("覆盖存在缺口；候选及排名仅涵盖已评价集合，未评价范围可能仍有机会。")
    if result.get("reason_codes"):
        lines.append(f"原因：{_roll_diagnostic(result['reason_codes'])}")
    if result.get("reason"):
        lines.append(str(result["reason"]))

    candidates = result.get("candidates") or []
    if candidates:
        lines.append("\n候选比较（净增现金年化优先，仅覆盖已评价集合）：")
    for index, candidate in enumerate(candidates, 1):
        change = candidate.get("remaining_annualized_change")
        change_label = f"{change * 100:+.2f} pp" if isinstance(change, (int, float)) else _value(change)
        lines.extend([
            f"{index}. {_value(candidate.get('contract'))} | strike {_value(candidate.get('strike'))} | "
            f"降幅 {_value(candidate.get('strike_drop_pct'), percent=True)} | "
            f"新仓价外 {_value(candidate.get('new_otm_pct'), percent=True)} | "
            f"到期 {_value(candidate.get('expiration'))} | 延后 {_value(candidate.get('extension_days'))} 天",
            f"   净增现金 {_roll_money(candidate.get('net_credit'), currency)} | "
            f"净增现金年化 {_value(candidate.get('incremental_annualized_return'), percent=True)} | "
            f"新仓到期归零最高年化 {_value(candidate.get('new_remaining_max_annualized_return'), percent=True)} | "
            f"较旧仓变化 {change_label}",
            f"   张数 {_value(candidate.get('contracts'))}；买回总支出 {_roll_money(candidate.get('old_close_cost'), currency)}；"
            f"新卖出净收入 {_roll_money(candidate.get('new_net_premium'), currency)}；"
            f"资金分母（旧/新）{_roll_money(candidate.get('old_capital_basis'), currency)} / "
            f"{_roll_money(candidate.get('new_capital_basis'), currency)}；"
            f"DTE（旧/新）{_value(candidate.get('old_dte'))}/{_value(candidate.get('new_dte'))}；"
            f"净增现金期间回报 {_value(candidate.get('incremental_period_return'), percent=True)}",
            f"   买回费用：{_roll_diagnostic(candidate.get('old_buy_fee'))}",
            f"   新卖出费用：{_roll_diagnostic(candidate.get('new_sell_fee'))}",
        ])
        if candidate.get("evidence"):
            lines.append(f"   报价与证据：{_roll_diagnostic(candidate['evidence'])}")
    lines.append(f"展示 {_value(result.get('displayed'))}；省略 {_value(result.get('omitted'))}（limit 仅限制展示）。")
    lines.append("单腿 bid/ask 现金估算：旧腿按 ask 买回、新腿按 bid 卖出并分别扣费；不是保证可成交的组合报价。")
    lines.append("净增现金是移仓现金流，不等于交易盈利；到期归零最高年化以期权到期归零为假设，不是期望收益。")
    if result.get("interpretation"):
        lines.append(f"解读：{_roll_diagnostic(result['interpretation'])}")
    if result.get("report_path"):
        lines.append(f"独立报告：{result['report_path']}")
    return "\n".join(lines) + "\n"
