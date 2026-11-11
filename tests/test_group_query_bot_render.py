"""Renderer keeps numeric authority and refuses inconsistent conditional results."""
from __future__ import annotations

from om_group_query_bot.render import (
    CLARIFICATION_NOTE,
    effective_filter_line,
    finalize_clarification,
    render_candidates,
    render_close,
    render_roll,
    strip_clarification_note,
)


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


def test_quote_render_never_labels_a_placeholder_or_price_without_time():
    from om_group_query_bot.render import render_quotes

    text = render_quotes({
        "quote_status": "unavailable", "market": "US",
        "effective_parameters": {"contract_requested": True},
        "underlier": None,
        "option": {
            "last_price": 3.25, "last_price_update_time": "N/A",
            "bid": 3.1, "ask": 3.4, "market_state": "N/A",
            "snapshot_received_at_utc": "2026-11-10T23:32:10.786000+00:00",
        },
        "missing_requirements": ["option_quote_unavailable"],
    })
    assert "最新" not in text
    assert "最新价时间" not in text
    assert "N/A" not in text
    assert "买 3.1000" in text and "卖 3.4000" in text


def test_quote_render_converts_an_offset_aware_update_time():
    from om_group_query_bot.render import render_quotes

    text = render_quotes({
        "quote_status": "last_known", "market": "US",
        "effective_parameters": {"contract_requested": False},
        "underlier": {
            "contract_symbol": "NVDA", "last_price": 168.89,
            "last_price_update_time": "2026-11-10T23:32:10+00:00",
            "snapshot_received_at_utc": "2026-11-10T23:32:10+00:00",
        },
        "missing_requirements": [],
    })
    assert "最新价时间 2026-11-10 18:32:10（纽约时间）" in text


def test_end_of_session_states_keep_their_meaning():
    from om_group_query_bot.render import render_quotes

    for state, label in (("AFTER_HOURS_END", "盘后结束"),
                         ("PRE_MARKET_END", "盘前结束"),
                         ("NIGHT_END", "夜盘结束")):
        text = render_quotes({
            "quote_status": "last_known", "market": "US",
            "effective_parameters": {"contract_requested": False},
            "underlier": {"contract_symbol": "NVDA", "last_price": 1.0,
                          "last_price_update_time": "2026-11-10 18:32:10",
                          "snapshot_received_at_utc": "2026-11-10T23:32:10+00:00",
                          "market_state": state},
            "missing_requirements": [],
        })
        assert f"市场状态 {label}（{state}）" in text


def test_close_and_roll_render_the_evaluation_time_in_the_market_zone():
    zone_text = "评估时间：2026-11-10 01:00:00（纽约时间）"
    close = render_close({
        "recommendation_state": "hold", "evaluation_status": "priced",
        "market": "US", "quote": {"bid": 1.10, "ask": 1.20},
        "metrics": {}, "reason_codes": [],
        "fee_model": {"id": "futu_option_fee", "version": "v1",
                      "fee_calc_status": "conservative_estimate"},
        "evaluated_at_utc": "2026-11-10T06:00:00+00:00",
    })
    assert zone_text in close
    roll = render_roll(_roll(market="US"))
    assert zone_text in roll


def test_clarification_note_is_appended_exactly_once():
    text = finalize_clarification(f"请补充张数。\n\n{CLARIFICATION_NOTE}")
    assert text == f"请补充张数。\n\n{CLARIFICATION_NOTE}"
    assert text.count(CLARIFICATION_NOTE) == 1


def test_strip_clarification_note_removes_every_occurrence_and_trailing_blanks():
    messy = f"A{CLARIFICATION_NOTE}\nB\n\n{CLARIFICATION_NOTE}\n\n  "
    assert strip_clarification_note(messy) == "A\nB"
    assert strip_clarification_note(None) == ""


def test_candidate_render_echoes_effective_filters_with_their_source():
    data = {
        "outcome": "found", "status": "success",
        "effective_filters": {
            "dte": [20, 60], "dte_source": "request",
            "min_abs_delta": 0.1, "max_abs_delta": 0.25,
            "max_abs_delta_source": "public_policy",
            "strike": 180.0, "strike_source": "public_policy",
            "limit": 10,
        },
        "candidates": [], "displayed": 0, "omitted": 0, "reason_codes": [],
    }
    line = effective_filter_line(data)
    assert line == ("筛选：期限 20–60 天（用户指定）；Delta 绝对值 0.10–0.25"
                    "（公共策略默认）；行权价 180.00（公共策略默认）；展示上限 10")
    assert line in render_candidates(data)
    assert effective_filter_line({"effective_filters": None}) is None


def test_delta_filter_echoes_the_applied_range_not_the_requested_range():
    # The owner keeps the requested range verbatim in abs_delta_range while
    # the applied range is the intersection with the public cap.
    filters = {
        "abs_delta_range": [0.2, 0.4], "min_abs_delta": 0.2,
        "max_abs_delta": 0.25, "max_abs_delta_source": "request",
    }
    line = effective_filter_line({"effective_filters": filters})
    assert line == ("筛选：Delta 绝对值 0.20–0.25"
                    "（用户指定，受公共策略上限约束）")
    uncapped = {**filters, "abs_delta_range": [0.2, 0.25]}
    assert effective_filter_line({"effective_filters": uncapped}) == (
        "筛选：Delta 绝对值 0.20–0.25（用户指定）")
