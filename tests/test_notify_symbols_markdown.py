"""Notification markdown rendering regression tests."""

from __future__ import annotations

import json
import warnings

import pandas as pd

from tests.notification_format_assertions import assert_mobile_flat_markdown


def _render_via_alert_engine(summary_row: dict, *, render_style: str = "legacy") -> str:
    from domain.domain import normalize_processor_row
    from src.application.alert_engine import build_alert_text
    from src.application.notify_symbols import build_notification

    normalized = normalize_processor_row(summary_row)
    df = pd.DataFrame([normalized])
    alerts = build_alert_text(df)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return build_notification("", alerts, account_label="SY", render_style=render_style)


def _render_legacy(changes: str, alerts: str, *, account_label: str) -> str:
    from src.application.notify_symbols import build_notification

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return build_notification(changes, alerts, account_label=account_label, render_style="legacy")


def _staggered_combo_summary() -> dict:
    return {
        "symbol": "NVDA",
        "strategy": "combo_yield",
        "candidate_count": 1,
        "top_contract": "2026-08-21 100P + 2026-10-16 120C",
        "structure_mode": "staggered_expiry_pair",
        "put_expiration": "2026-08-21",
        "put_dte": 35,
        "call_expiration": "2026-10-16",
        "call_dte": 91,
        "expiry_gap_days": 56,
        "put_strike": 100.0,
        "call_strike": 120.0,
        "put_bid": 2.35,
        "call_ask": 2.10,
        "call_delta": 0.31,
        "put_net_credit": 228.0,
        "call_total_cost": 218.0,
        "combo_net_credit": 10.0,
        "net_credit": 10.0,
        "call_cost_to_put_credit": 218.0 / 228.0,
        "funding_ratio": 228.0 / 218.0,
        "funding_accepted": True,
        "strike_safety_margin_pct": 0.18,
        "cash_required_usd": 10000.0,
        "option_ccy": "USD",
        "annualized_return": None,
        "net_income": 10.0,
    }


def test_notify_symbols_markdown_put_layout() -> None:
    from src.application.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- [腾讯](0700.HK) | sell_put | 2026-04-29 460P | 年化 17.21% | 净收入 557.00 | DTE 26 | Strike 460 | 中性 | ccy HKD | ask 5.860 | bid 5.580 | mid 5.720 | delta -0.23 | cash_req_cny ¥110,720 | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = _render_legacy("", alerts, account_label="LX")

    expected = """Put

**[lx] 腾讯｜卖Put｜2026-04-29 460P**
收益｜权利金=5.720 (HKD) | 年化 17.21% | 净收 557
合约｜行权价=460 | 数量=1张(默认) | DTE=26
风控｜风险=中性 | delta=-0.23 | IV=缺失(告警未提供iv)
资金｜保证金占用=¥110,720 (CNY)
操作｜建议挂单=5.720
备注｜通过准入后，收益/风险组合较强，值得优先看。
"""
    assert out == expected
    assert_mobile_flat_markdown(out, require_title=False)


def test_notify_symbols_no_candidate_message_is_heartbeat() -> None:
    from src.application.notify_symbols import build_notification

    out = build_notification('', '', account_label='LX')

    assert out == '当前没有通过筛选的候选。\n'
    assert '今日无需要主动提醒的内容。' not in out
    assert_mobile_flat_markdown(out, require_title=False)


def test_notify_symbols_markdown_put_layout_missing_fields_have_reasons() -> None:
    from src.application.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- NVDA | sell_put | 2026-06-18 156P | 年化 - | 净收入 524.99 | DTE 76 | Strike nan | nan | ccy USD | ask 5.450 | bid 5.100 | mid 5.275 | delta nan | iv nan | cash_req - | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = _render_legacy("", alerts, account_label="SY")

    assert "nan" not in out.lower()
    assert "行权价=156" in out
    assert "年化 缺失(告警未提供年化)" in out
    assert "保证金占用=缺失(告警未提供cash_req_cny/cash_req)" in out
    assert "同标的Sell Put占用" not in out
    assert "delta=缺失(告警未提供delta)" in out
    assert "IV=缺失(告警未提供iv)" in out


def test_notify_symbols_markdown_call_layout_ignores_changes_input() -> None:
    from src.application.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- [英伟达](NVDA) | sell_call | 2026-06-18 180C | 年化 12.30% | 净收入 240.40 | DTE 44 | Strike 180 | 保守 | ccy USD | ask 2.500 | bid 2.300 | mid 2.400 | delta 0.16 | cover 2 | shares 200(-0) | 已通过准入，可作为 Covered Call 备选。
"""
    changes = """# Symbols Changes

- NVDA sell_call: Top pick 由 2026-06-18 175C 变为 2026-06-18 180C。
"""
    out = _render_legacy(changes, alerts, account_label="SY")

    assert "**[sy] 英伟达｜Covered Call｜2026-06-18 180C**" in out
    assert "数量=2张(可覆盖)" in out
    assert "持仓｜总股数=200 | 已占用=0 | 可用=200 | 可覆盖=2张" in out
    assert "变化" not in out
    assert "Top pick" not in out


def test_notify_symbols_markdown_call_layout_missing_fields_have_reasons() -> None:
    from src.application.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- NVDA | sell_call | 2026-06-18 180C | 年化 - | 净收入 240.40 | DTE 44 | Strike nan | 保守 | ccy USD | ask 2.500 | bid 2.300 | mid 2.400 | delta nan | cover nan | shares nan | 已通过准入，可作为 Covered Call 备选。
"""
    out = _render_legacy("", alerts, account_label="SY")

    assert "nan" not in out.lower()
    assert "行权价=180" in out
    assert "年化 缺失(告警未提供年化)" in out
    assert "delta=缺失(告警未提供delta)" in out
    assert "IV=缺失(告警未提供iv)" in out
    assert "持仓｜总股数=缺失(告警未提供shares) | 已占用=缺失(告警未提供shares) | 可用=缺失(告警未提供shares) | 可覆盖=缺失(告警未提供cover)张" in out


def test_notify_symbols_markdown_put_chain_uses_upstream_fields_when_available() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "0700.HK",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-04-29 460P",
            "annualized_return": 0.1721,
            "net_income": 557.00,
            "dte": 26,
            "strike": 460.0,
            "risk_label": "中性",
            "delta": -0.23,
            "iv": 0.41,
            "cash_required_cny": 110720.0,
            "mid": 5.72,
            "bid": 5.58,
            "ask": 5.86,
            "option_ccy": "HKD",
        }
    )

    assert "保证金占用=¥110,720 (CNY)" in out
    assert "同标的Sell Put占用" not in out
    assert "delta=-0.23" in out
    assert "IV=41.00%" in out
    assert "告警未提供cash_req_cny/cash_req" not in out
    assert "告警未提供delta" not in out
    assert "告警未提供iv" not in out


def test_notify_symbols_markdown_put_chain_shows_event_risk() -> None:
    from domain.domain import normalize_processor_row
    from src.application.alert_engine import build_alert_text
    from src.application.notify_symbols import build_notification

    summary_row = normalize_processor_row(
        {
            "symbol": "AAPL",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-19 180P",
            "annualized_return": 0.18,
            "net_income": 210.0,
            "dte": 24,
            "strike": 180.0,
            "risk_label": "中性",
            "delta": -0.22,
            "iv": 0.38,
            "cash_required_usd": 18000.0,
            "mid": 2.1,
            "option_ccy": "USD",
            "event_flag": True,
            "event_types": "earnings",
            "event_dates": "2026-06-10",
        }
    )

    alerts = build_alert_text(pd.DataFrame([summary_row]))
    out = _render_legacy("", alerts, account_label="SY")

    assert "event earnings@2026-06-10" in alerts
    assert "事件｜earnings@2026-06-10" in out


def test_notify_symbols_markdown_put_chain_shows_same_symbol_usage_from_summary_fields() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "3690.HK",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-05-28 75P",
            "annualized_return": 0.128,
            "net_income": 468.0,
            "dte": 36,
            "strike": 75.0,
            "risk_label": "保守",
            "delta": -0.16,
            "iv": 0.4138,
            "cash_required_cny": 32715.0,
            "cash_secured_used_cny_total": 200000.0,
            "cash_secured_used_cny_symbol": 45000.0,
            "mid": 0.965,
            "option_ccy": "HKD",
        }
    )

    assert "保证金占用=¥32,715 (CNY)" in out
    assert "同标的Sell Put占用=¥45,000" in out


def test_notify_symbols_markdown_put_chain_uses_total_cny_cash_guard_for_alert_engine() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "0700.HK",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-29 450P",
            "annualized_return": 0.1977,
            "net_income": 1416.5,
            "dte": 60,
            "strike": 450.0,
            "risk_label": "中性",
            "delta": -0.35,
            "cash_required_cny": 39280.0,
            "cash_free_total_cny": 11666.0,
            "mid": 14.375,
            "option_ccy": "HKD",
        }
    )

    assert "备注｜所需担保现金约 ¥39,280，但当前现金类资产扣担保后余量约 ¥11,666" in out


def test_notify_symbols_markdown_put_chain_uses_usd_cash_guard_for_alert_engine() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "AAPL",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-29 180P",
            "annualized_return": 0.18,
            "net_income": 210.0,
            "dte": 60,
            "strike": 180.0,
            "risk_label": "中性",
            "delta": -0.21,
            "cash_required_usd": 18000.0,
            "cash_free_usd": 15000.0,
            "mid": 2.15,
            "option_ccy": "USD",
        }
    )

    assert "备注｜所需担保现金约 $18,000，但当前账户可用担保现金约 $15,000" in out


def test_notify_symbols_markdown_put_falls_back_to_usd_margin_when_cny_margin_missing() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "0700.HK",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-04-29 460P",
            "annualized_return": 0.1721,
            "net_income": 557.00,
            "dte": 26,
            "strike": 460.0,
            "risk_label": "中性",
            "delta": -0.23,
            "cash_required_usd": 58880.0,
            "cash_free_cny": 200000.0,
            "mid": 5.72,
            "option_ccy": "HKD",
        }
    )

    assert "保证金占用=$58,880 (USD)" in out
    assert "告警未提供cash_req_cny/cash_req" not in out


def test_notify_symbols_markdown_put_chain_missing_fields_keep_reasons() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "NVDA",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-18 156P",
            "annualized_return": 0.1,
            "net_income": 524.99,
            "dte": 76,
            "strike": 156.0,
            "risk_label": "中性",
        }
    )

    assert "保证金占用=缺失(告警未提供cash_req_cny/cash_req)" in out
    assert "delta=缺失(告警未提供delta)" in out
    assert "IV=缺失(告警未提供iv)" in out


def test_notify_symbols_markdown_put_shows_same_symbol_position_usage() -> None:
    from src.application.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- [腾讯](0700.HK) | sell_put | 2026-04-29 460P | 年化 17.21% | 净收入 557.00 | DTE 26 | Strike 460 | 中性 | ccy HKD | mid 5.720 | cash_req_cny ¥110,720 | cash_used_total_cny ¥200,000 | cash_used_sym_cny ¥45,000 | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = _render_legacy("", alerts, account_label="LX")

    assert "同标的Sell Put占用=¥45,000" in out


def test_notify_symbols_markdown_put_chain_shows_linked_call_hint() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "NVDA",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-19 95P",
            "annualized_return": 0.273,
            "net_income": 307.65,
            "dte": 44,
            "strike": 95.0,
            "risk_label": "中性",
            "delta": -0.25,
            "iv": 0.42,
            "cash_required_usd": 9500.0,
            "mid": 3.1,
            "bid": 3.0,
            "ask": 3.2,
            "option_ccy": "USD",
            "linked_call_contract": "2026-06-19 110C",
            "linked_call_count": 2,
            "linked_call_ask": 1.5,
            "linked_call_delta": 0.32,
            "linked_call_net_credit": 145.33,
            "linked_call_scenario_score": 0.0458,
        }
    )

    assert "组合收益｜推荐Call=2026-06-19 110C" in out
    assert "候选Call=2个" in out
    assert "参考买价=1.500" in out
    assert "净权利金=145.33" in out
    assert "场景评分=4.58%" in out
    assert "目标收益" not in out
    assert "全账户Sell Put占用" not in out


def test_notify_symbols_markdown_yield_enhancement_layout() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "NVDA",
            "strategy": "yield_enhancement",
            "candidate_count": 1,
            "top_contract": "2026-06-19 95P+110C",
            "annualized_return": 1.0142,
            "net_income": 145.33,
            "dte": 44,
            "strike": 95.0,
            "risk_label": "中性",
            "option_ccy": "USD",
            "mid": 1.453,
            "put_bid": 3.0,
            "put_strike": 95.0,
            "call_strike": 110.0,
            "call_ask": 1.5,
            "call_delta": 0.32,
            "call_candidate_count": 2,
            "net_credit": 145.33,
            "scenario_score": 0.0458,
            "expected_move": 14.24,
            "expected_move_iv": 0.41,
            "combo_spread_ratio": 0.18,
        }
    )

    assert "Combo Yield" in out
    assert "**[sy] NVDA｜组合收益｜2026-06-19 95P+110C**" in out
    assert "组合净权利金=145.33" in out
    assert "场景评分=4.58%" in out
    assert "Put=95" in out
    assert "Call=110" in out
    assert "备选Call=2个" in out
    assert "Call delta=0.32" in out
    assert "建议挂单=卖3.000/买1.500" in out
    assert "卖1.453/买1.500" not in out
    assert "目标价" not in out


def test_yield_enhancement_notification_ignores_option_contract_display_name(tmp_path) -> None:
    from domain.domain import normalize_processor_row
    from src.application.alert_engine import _load_symbol_display_map, build_alert_text
    from src.application.notify_symbols import build_notification

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "portfolio_context.json").write_text(
        json.dumps(
            {
                "stocks_by_symbol": {
                    "PDD": {"symbol": "PDD", "name": "PDD 260626 91.00C"},
                    "0700.HK": {"symbol": "0700.HK", "name": "腾讯"},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config.us.json").write_text(
        json.dumps(
            {
                "intake": {
                    "symbol_aliases": {
                        "PDD 260626 91.00C": "PDD",
                        "拼多多": "PDD",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    display_map = _load_symbol_display_map(tmp_path, state_dir=state_dir)
    assert display_map == {"0700.HK": "腾讯", "PDD": "拼多多"}

    row = normalize_processor_row(
        {
            "symbol": "PDD",
            "strategy": "yield_enhancement",
            "candidate_count": 1,
            "top_contract": "2026-06-26 78P+88C",
            "annualized_return": 0.41,
            "net_income": 128.34,
            "dte": 15,
            "strike": 78.0,
            "risk_label": "激进",
            "option_ccy": "USD",
            "mid": 1.283,
            "put_bid": 1.68,
            "put_strike": 78.0,
            "call_strike": 88.0,
            "call_ask": 0.35,
            "call_delta": 0.12,
            "call_candidate_count": 1,
            "net_credit": 128.34,
            "scenario_score": 1.78,
            "expected_move": 6.27,
            "expected_move_iv": 0.3892,
        }
    )
    alerts = build_alert_text(
        pd.DataFrame([row]),
        symbol_display_map={"PDD": "PDD 260626 91.00C"},
    )
    out = build_notification("", alerts, account_label="LX", render_style="compact")

    assert "PDD 260626 91.00C" not in alerts
    assert "PDD 260626 91.00C" not in out
    assert "💎 组合·同期 PDD 78P+88C · 06-26" in out


def test_alert_engine_high_priority_orders_by_strategy_then_strength() -> None:
    from src.application.alert_engine import build_alert_text
    from src.application.notify_symbols import extract_section

    rows = [
        {
            "symbol": "AAPL",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-19 180P",
            "annualized_return": 0.12,
            "net_income": 120.0,
            "dte": 30,
            "strike": 180.0,
            "risk_label": "中性",
        },
        {
            "symbol": "NVDA",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-19 150P",
            "annualized_return": 0.25,
            "net_income": 250.0,
            "dte": 30,
            "strike": 150.0,
            "risk_label": "中性",
        },
        {
            "symbol": "MSFT",
            "strategy": "sell_call",
            "candidate_count": 1,
            "top_contract": "2026-06-19 430C",
            "annualized_return": 0.11,
            "net_income": 110.0,
            "dte": 30,
            "strike": 430.0,
            "risk_label": "保守",
            "cover_avail": 1,
            "shares_total": 100,
            "shares_locked": 0,
        },
        {
            "symbol": "BABA",
            "strategy": "yield_enhancement",
            "candidate_count": 1,
            "top_contract": "2026-06-19 80P+95C",
            "annualized_return": 0.30,
            "net_income": 90.0,
            "dte": 30,
            "strike": 80.0,
            "risk_label": "保守",
        },
    ]

    alerts = build_alert_text(pd.DataFrame(rows))
    high_lines = extract_section(alerts, "## 高优先级")

    assert "NVDA | sell_put" in high_lines[0]
    assert "AAPL | sell_put" in high_lines[1]
    assert "MSFT | sell_call" in high_lines[2]
    assert "BABA | yield_enhancement" in high_lines[3]


def test_alert_engine_missing_numeric_fields_do_not_abort_alert_build() -> None:
    from src.application.alert_engine import build_alert_text
    from src.application.notify_symbols import extract_section

    rows = [
        {
            "symbol": "NVDA",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-19 100P",
            "annualized_return": pd.NA,
            "net_income": pd.NA,
            "dte": pd.NA,
            "strike": pd.NA,
            "risk_label": pd.NA,
        },
        {
            "symbol": "AAPL",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-19 180P",
            "annualized_return": "",
            "net_income": "",
            "dte": "",
            "strike": "",
            "risk_label": "",
        },
    ]

    alerts = build_alert_text(pd.DataFrame(rows))
    low_lines = extract_section(alerts, "## 低优先级")

    assert len(low_lines) == 2
    assert all("年化 -" in line for line in low_lines)
    assert all("净收入 -" in line for line in low_lines)
    assert "DTE -" in low_lines[0]
    assert "Strike -" in low_lines[0]


def test_build_notification_keeps_per_strategy_capacity() -> None:
    from src.application.notify_symbols import build_notification

    put_lines = [
        (
            f"- PUT{i} | sell_put | 2026-06-19 10{i}P | 年化 {20 - i:.2f}% | 净收入 {100 + i:.2f} | "
            f"DTE 30 | Strike 10{i} | 中性 | ccy USD | mid 1.000 | 通过准入后，收益/风险组合较强，值得优先看。"
        )
        for i in range(1, 7)
    ]
    call_line = (
        "- CALL1 | sell_call | 2026-06-19 180C | 年化 9.00% | 净收入 90.00 | "
        "DTE 30 | Strike 180 | 保守 | ccy USD | mid 1.000 | cover 1 | shares 100(-0) | 已通过准入，可作为 Covered Call 备选。"
    )
    alerts = "# Symbols Alerts\n\n## 高优先级\n" + "\n".join(put_lines + [call_line]) + "\n"

    out = _render_legacy("", alerts, account_label="LX")

    assert out.count("｜卖Put｜") == 5
    assert "PUT5" in out
    assert "PUT6" not in out
    assert "CALL1" in out
    assert out.count("｜Covered Call｜") == 1


def test_build_notification_keeps_medium_strategy_when_high_exists() -> None:
    from src.application.notify_symbols import build_notification

    high_put = (
        "- NVDA | sell_put | 2026-06-19 150P | 年化 20.00% | 净收入 200.00 | "
        "DTE 30 | Strike 150 | 中性 | ccy USD | mid 2.000 | 通过准入后，收益/风险组合较强，值得优先看。"
    )
    medium_call = (
        "- MSFT | sell_call | 2026-06-19 430C | 年化 6.50% | 净收入 80.00 | "
        "DTE 30 | Strike 430 | 保守 | ccy USD | mid 0.800 | cover 1 | shares 100(-0) | 已通过准入，可作为 Covered Call 备选。"
    )
    alerts = (
        "# Symbols Alerts\n\n"
        "## 高优先级\n"
        f"{high_put}\n\n"
        "## 中优先级\n"
        f"{medium_call}\n"
    )

    out = build_notification("", alerts, account_label="LX")

    assert "NVDA" in out
    assert "MSFT" in out
    assert out.index("Put") < out.index("Call")


def test_notify_symbols_staggered_combo_is_high_priority_and_uses_separate_leg_copy() -> None:
    out = _render_via_alert_engine(_staggered_combo_summary())

    assert "**[sy] NVDA｜组合收益**" in out
    assert "Put｜卖 100P · 2026-08-21/35天 · bid=2.35 · 估算净收=228 USD" in out
    assert "Call｜买 120C · 2026-10-16/91天 · ask=2.1 · delta=0.31 · 估算成本=218 USD" in out
    assert "覆盖率=104.59%" in out
    assert "净现金流=10 USD" in out
    assert "风险｜Put安全边界=18% · 现金=$10,000 · Call晚56天" in out
    assert "备注｜" not in out
    assert "资金利用率" not in out
    assert "两腿各1张" not in out
    assert "当前组合收益推荐未通过优先级阈值" not in out
    assert "场景评分" not in out
    assert "预期波动" not in out


def test_notify_symbols_staggered_combo_compact_copy_is_concise() -> None:
    out = _render_via_alert_engine(_staggered_combo_summary(), render_style="compact")

    assert "🧩 组合·跨期 NVDA 100P+120C" in out
    assert "Put｜卖 100P · 08-21/35天 · bid 2.35 · 估算净收 228 USD" in out
    assert "Call｜买 120C · 10-16/91天 · ask 2.1 · Δ 0.31 · 估算成本 218 USD" in out
    assert "组合｜覆盖 104.59% · 净现金流 10 USD" in out
    assert "风险｜安全边界 18% · 现金 $10,000 · Call晚56天" in out
    assert "备注｜" not in out
    assert "资金利用率" not in out
    assert "两腿各1张" not in out
