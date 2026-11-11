"""Renderer keeps numeric authority and refuses inconsistent conditional results."""
from __future__ import annotations

from om_group_query_bot.render import render_close, render_roll


def _roll(**overrides):
    data = {
        "status": "success", "outcome": "found", "analysis_kind": "conditional",
        "old_quote": {"bid": 1.10, "ask": 1.20},
        "stock_coverage": {"verification_status": "not_verified",
                           "required_shares": 200},
        "unverified_conditions": ["actual_share_count_not_verified"],
        "candidates": [{
            "contract_symbol": "US.NVDA270115C190000",
            "option_contract": {"expiration": "2027-01-15", "strike": 190.0},
            "bid": 2.0, "ask": 2.2, "dte": 66, "delta": 0.3,
            "economic_result": "positive_net_credit",
            "metrics": {"incremental_annualized_return": 0.25},
        }],
        "displayed": 1, "omitted": 0, "reason_codes": [],
        "scope": {"max_new_expiration": "2027-03-19", "strike_boundary": 200.0},
        "evaluated_at_utc": "2026-11-10T06:00:00+00:00",
    }
    data.update(overrides)
    return data


def test_roll_without_a_not_verified_coverage_contract_is_refused():
    text = render_roll(_roll(stock_coverage={"verification_status": "covered"}))
    assert text.startswith("结果合同不一致")
    assert "条件分析" not in text


def test_roll_without_unverified_conditions_is_refused():
    text = render_roll(_roll(unverified_conditions=[]))
    assert text.startswith("结果合同不一致")


def test_conditional_roll_renders_disclaimers_and_nested_metrics():
    text = render_roll(_roll())
    assert "条件分析：股票覆盖未经核实，不构成正式账户建议。" in text
    assert "年化 25.00%" in text
    assert "按富途标准费用模型估算，非账户实际费用。" in text


def test_close_uses_the_fee_model_id_and_estimate_disclaimer():
    text = render_close({
        "recommendation_state": "hold", "evaluation_status": "priced",
        "quote": {"bid": 1.10, "ask": 1.20},
        "metrics": {"net_capture_ratio": 0.4},
        "fee_model": {"id": "futu_option_fee", "version": "v1",
                      "estimated_open_fee": 1.3,
                      "estimated_close_fee": 1.2,
                      "fee_calc_status": "conservative_estimate"},
        "reason_codes": [],
        "evaluated_at_utc": "2026-11-10T06:00:00+00:00",
    })
    assert "费用模型 futu_option_fee@v1" in text
    assert "非账户实际费用" in text


def test_quote_render_shows_price_and_its_market_time_when_closed():
    from om_group_query_bot.render import render_quotes

    text = render_quotes({
        "quote_status": "last_known",
        "market": "US",
        "effective_parameters": {"contract_requested": False},
        "underlier": {
            # The producer canonicalizes the symbol, so the market prefix comes
            # from the response's market field, not the symbol spelling.
            "contract_symbol": "NVDA", "last_price": 168.89,
            "market_state": "AFTER_HOURS_BEGIN",
            "last_price_update_time": "2026-11-10 18:32:10.786",
            "snapshot_received_at_utc": "2026-11-10T23:32:10.786000+00:00",
        },
        "missing_requirements": [],
    })
    assert "行情状态：最近已知" in text
    assert "最新 168.8900" in text
    assert "最新价时间 2026-11-10 18:32:10.786（纽约时间）" in text
    assert "市场状态 盘后（AFTER_HOURS_BEGIN）" in text
    assert "取数时间 2026-11-10 18:32:10（纽约时间）" in text
    assert "非当前可成交状态" in text


def test_contract_quote_render_keeps_each_instruments_own_time():
    from om_group_query_bot.render import render_quotes

    text = render_quotes({
        "quote_status": "last_known",
        "market": "US",
        "effective_parameters": {"contract_requested": True},
        "underlier": {
            "contract_symbol": "NVDA", "last_price": 168.89,
            "market_state": "AFTER_HOURS_BEGIN",
            "last_price_update_time": "2026-11-10 18:32:10.786",
            "snapshot_received_at_utc": "2026-11-10T23:32:10.786000+00:00",
        },
        "option": {
            "contract_symbol": "US.NVDA261120P168000", "last_price": 1.25,
            "market_state": "AFTER_HOURS_BEGIN",
            "last_price_update_time": "2026-11-10 16:00:00.000",
            "snapshot_received_at_utc": "2026-11-10T21:00:00+00:00",
        },
        "missing_requirements": [],
    })
    underlier_line, option_line = text.split("\n")[1:3]
    assert "最新价时间 2026-11-10 18:32:10.786（纽约时间）" in underlier_line
    assert "最新价时间 2026-11-10 16:00:00.000（纽约时间）" in option_line


def test_quote_render_labels_non_continuous_market_states():
    from om_group_query_bot.render import render_quotes

    def text_for(state):
        return render_quotes({
            "quote_status": "last_known", "market": "US",
            "effective_parameters": {"contract_requested": False},
            "underlier": {"contract_symbol": "NVDA", "last_price": 1.0,
                          "market_state": state},
            "missing_requirements": [],
        })

    assert "市场状态 未开盘/无交易（NONE）" in text_for("NONE")
    assert "市场状态 按最新价交易（TRADE_AT_LAST）" in text_for("TRADE_AT_LAST")
    assert "市场状态 午盘结束（AFTERNOON_END）" in text_for("AFTERNOON_END")
    assert "市场状态 交易竞价（TRADE_AUCTION）" in text_for("TRADE_AUCTION")


def test_candidates_render_closed_reference_and_observation_time():
    from om_group_query_bot.render import render_candidates

    text = render_candidates({
        "outcome": "unable", "market": "US", "candidates": [],
        "displayed": 0, "omitted": 0, "reason_codes": ["market_closed"],
        "data_quality": {
            "mode": "closed_market_reference",
            "market_state": "AFTER_HOURS_BEGIN",
            "underlying_observed_at": "2026-11-10T23:32:10.786000+00:00",
            "reference_date": "2026-11-10",
        },
    })
    assert "数据口径：休市参考" in text
    assert "参考交易日：2026-11-10" in text
    assert "市场状态：盘后（AFTER_HOURS_BEGIN）" in text
    assert "标的观察时间：2026-11-10 18:32:10（纽约时间）" in text


def test_candidates_render_the_producer_notice_when_it_is_present():
    from om_group_query_bot.render import render_candidates

    notice = "休市参考；非实时可成交报价，bid/ask、Delta、IV 无独立源时间。"
    text = render_candidates({
        "outcome": "unable", "market": "US", "candidates": [],
        "displayed": 0, "omitted": 0, "reason_codes": [],
        "data_quality": {"mode": "closed_market_reference", "notice": notice},
    })
    assert f"数据口径：{notice}" in text


def test_candidates_render_reference_time_even_when_unavailable():
    from om_group_query_bot.render import render_candidates

    text = render_candidates({
        "outcome": "unable", "market": "HK", "candidates": [],
        "displayed": 0, "omitted": 0, "reason_codes": ["market_closed"],
        "data_quality": {
            "mode": "unavailable", "market_state": "CLOSED",
            "underlying_observed_at": "2026-11-10T08:00:00+00:00",
        },
    })
    assert "数据口径" not in text
    assert "市场状态：休市（CLOSED）" in text
    assert "标的观察时间：2026-11-10 16:00:00（香港时间）" in text
