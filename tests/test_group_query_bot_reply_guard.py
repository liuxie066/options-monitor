"""The guard keeps model prose on the fact card and enforces safety clauses."""
from __future__ import annotations

from datetime import datetime, timezone

from om_group_query_bot.guard import (
    MAX_REPLY_CHARS,
    check_result_reply,
    correction_message,
)
from om_group_query_bot.render import fact_card, render_result

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)

QUOTE_DATA = {
    "symbol": "NVDA", "market": "US", "quote_status": "current",
    "effective_parameters": {"contract_requested": False},
    "underlier": {"contract_symbol": "NVDA", "last_price": 185.0,
                  "bid": 184.9, "ask": 185.1,
                  "last_price_update_time": "2026-11-10 01:00:00",
                  "snapshot_received_at_utc": "2026-11-10T06:00:00+00:00"},
    "missing_requirements": [],
}

LAST_KNOWN_DATA = {
    "symbol": "NVDA", "market": "US", "quote_status": "last_known",
    "effective_parameters": {"contract_requested": False},
    "underlier": {"contract_symbol": "NVDA", "last_price": 168.89,
                  "last_price_update_time": "2026-11-10 18:32:10.786",
                  "snapshot_received_at_utc":
                      "2026-11-10T23:32:10.786000+00:00"},
    "missing_requirements": [],
}

UNABLE_CANDIDATES = {
    "symbol": "NVDA", "market": "US", "outcome": "unable",
    "candidates": [], "displayed": 0, "omitted": 0,
    "reason_codes": ["market_closed"],
    "data_quality": {"market_state": "PRE_MARKET_BEGIN",
                     "mode": "unavailable"},
    "effective_filters": {"dte": [7, 60], "dte_source": "public_policy",
                          "limit": 10},
}

CLOSE_DATA = {
    "symbol": "NVDA", "market": "US",
    "position": {"symbol": "NVDA", "side": "short", "contracts": 1,
                 "contract": {"option_type": "put", "expiration": "2026-12-18",
                              "strike": 120.0}},
    "recommendation_state": "hold", "evaluation_status": "priced",
    "quote": {"bid": 1.10, "ask": 1.20},
    "metrics": {"net_capture_ratio": 0.4},
    "fee_model": {"estimated_open_fee": 1.3, "estimated_close_fee": 1.2},
    "reason_codes": [],
}

ROLL_DATA = {
    "symbol": "NVDA", "market": "US", "status": "success",
    "outcome": "found", "analysis_kind": "conditional",
    "position": {"symbol": "NVDA", "side": "short", "contracts": 2,
                 "contract": {"option_type": "call", "expiration": "2026-12-18",
                              "strike": 200.0}},
    "old_quote": {"bid": 1.10, "ask": 1.20},
    "stock_coverage": {"verification_status": "not_verified",
                       "required_shares": 200},
    "unverified_conditions": ["actual_share_count_not_verified"],
    "candidates": [{
        "contract_symbol": "US.NVDA270115C190000",
        "option_contract": {"expiration": "2027-01-15", "strike": 190.0},
        "bid": 2.0, "ask": 2.2, "dte": 66, "delta": 0.3,
        "economic_result": "positive_net_credit", "estimated_sell_fee": 1.25,
        "metrics": {"incremental_annualized_return": 0.25},
    }],
    "displayed": 1, "omitted": 0, "reason_codes": [],
    "scope": {"max_new_expiration": "2027-03-19", "strike_boundary": 200.0},
}


def _check(text, *, method="query_quotes", data=None, user_text="NVDA 现价"):
    data = data if data is not None else QUOTE_DATA
    return check_result_reply(
        text, method=method, data=data,
        fact_card_text=fact_card(method, data), user_text=user_text, now=NOW)


def test_a_short_reply_that_uses_only_card_numbers_passes():
    verdict = _check("NVDA 最新价 185.0000，买一 184.9000 / 卖一 185.1000。")
    assert verdict.ok, verdict.reasons
    assert verdict.reasons == ()


def test_rounding_card_values_to_fewer_digits_is_allowed():
    assert _check("NVDA 大约 185，买 184.9，卖 185.1。").ok
    assert _check("NVDA 最新价 185.0。").ok


def test_an_invented_number_is_rejected_and_named():
    verdict = _check("NVDA 涨到 999.99 了。")
    assert not verdict.ok
    assert "invented_number:999.99" in verdict.reasons
    assert "999.99" in correction_message(verdict)


def test_percent_and_unit_forms_match_the_card():
    data = {**CLOSE_DATA, "metrics": {"net_capture_ratio": 0.4}}
    card = fact_card("evaluate_close", data)
    verdict = check_result_reply(
        "净捕获约 40%，费用按模型估算，不是账户实际扣费。",
        method="evaluate_close", data=data, fact_card_text=card,
        user_text="NVDA 平仓", now=NOW)
    assert verdict.ok, verdict.reasons


def test_thousands_separators_and_wan_units_are_normalized():
    data = {**CLOSE_DATA, "metrics": {"capital_basis": 12000.0}}
    card = fact_card("evaluate_close", data)
    verdict = check_result_reply(
        "保证金基数约 1.2 万，费用按模型估算，不是账户实际扣费。",
        method="evaluate_close", data=data, fact_card_text=card,
        user_text="NVDA 平仓", now=NOW)
    assert verdict.ok, verdict.reasons


def test_contract_symbols_must_come_from_the_card():
    card = fact_card("evaluate_roll_down", ROLL_DATA)
    valid = check_result_reply(
        "候选 US.NVDA270115C190000，卖价 2.2000；股票覆盖未经核实，"
        "不构成正式账户建议；预计卖出手续费 1.25，按富途标准费用模型估算，"
        "不是账户实际扣费。",
        method="evaluate_roll_down", data=ROLL_DATA, fact_card_text=card,
        user_text="NVDA 上移", now=NOW)
    assert valid.ok, valid.reasons
    fabricated = check_result_reply(
        "候选 US.NVDA270116C190000，卖价 2.2000；股票覆盖未经核实，"
        "不构成正式账户建议；费用按模型估算，不是账户实际扣费。",
        method="evaluate_roll_down", data=ROLL_DATA, fact_card_text=card,
        user_text="NVDA 上移", now=NOW)
    assert "invented_symbol:US.NVDA270116C190000" in fabricated.reasons


def test_dates_must_come_from_the_card_in_any_chinese_form():
    card = fact_card("evaluate_roll_down", ROLL_DATA)
    valid = check_result_reply(
        "候选 US.NVDA270115C190000，2027年1月15日到期；股票覆盖未经核实，"
        "不构成正式账户建议；费用按模型估算，不是账户实际扣费。",
        method="evaluate_roll_down", data=ROLL_DATA, fact_card_text=card,
        user_text="NVDA 上移", now=NOW)
    assert valid.ok, valid.reasons
    wrong = check_result_reply(
        "候选 US.NVDA270115C190000，2027年2月15日到期；股票覆盖未经核实，"
        "不构成正式账户建议；费用按模型估算，不是账户实际扣费。",
        method="evaluate_roll_down", data=ROLL_DATA, fact_card_text=card,
        user_text="NVDA 上移", now=NOW)
    assert "invented_date:2027-02-15" in wrong.reasons


def test_internal_jargon_json_and_tables_are_rejected():
    assert "internal_jargon" in _check("行情来自 market_closed。").reasons
    assert "internal_jargon" in _check("候选状态：找到 1 个。").reasons
    assert "internal_jargon" in _check("字段 effective_filters 显示正常。").reasons
    assert "raw_json" in _check('{"last_price": 185.0}').reasons
    assert "markdown_table" in _check("| 项目 | 值 |\n| --- | --- |").reasons


def test_a_result_reply_must_not_carry_the_clarification_note():
    verdict = _check("NVDA 最新价 185.0000。（未执行实时查询，以上为澄清或说明，不含实时数据。）")
    assert "clarification_note" in verdict.reasons


def test_overlong_replies_are_rejected():
    verdict = _check("NVDA 最新价 185.0000。" + "很长的解释。" * 200)
    assert "too_long" in verdict.reasons
    many_lines = "\n".join(["NVDA 最新价 185.0000。"] * 30)
    assert "too_many_lines" in _check(many_lines).reasons


def test_last_known_quotes_must_say_they_are_not_current():
    verdict = _check("NVDA 最新价 168.8900。", data=LAST_KNOWN_DATA)
    assert "missing_last_known_notice" in verdict.reasons
    good = _check("NVDA 最近已知报价 168.8900，不是当前可成交价格。",
                  data=LAST_KNOWN_DATA)
    assert good.ok, good.reasons


def test_fee_replies_must_name_the_estimate_and_the_non_account_boundary():
    card = fact_card("evaluate_close", CLOSE_DATA)
    bare = check_result_reply(
        "建议继续持有，开仓费约 1.30，平仓费约 1.20。",
        method="evaluate_close", data=CLOSE_DATA, fact_card_text=card,
        user_text="NVDA 平仓", now=NOW)
    assert "missing_fee_estimate" in bare.reasons
    assert "missing_non_account_fee" in bare.reasons
    misleading = check_result_reply(
        "建议继续持有；开仓费约 1.30、平仓费约 1.20，按模型估算，实际扣费。",
        method="evaluate_close", data=CLOSE_DATA, fact_card_text=card,
        user_text="NVDA 平仓", now=NOW)
    # Naming the concept is not the disclaimer: a reply that says the fees are
    # the account's actual charge must not satisfy the non-account clause.
    assert "missing_non_account_fee" in misleading.reasons
    good = check_result_reply(
        "建议继续持有；开仓费约 1.30、平仓费约 1.20，按富途模型估算，"
        "不是账户实际扣费。",
        method="evaluate_close", data=CLOSE_DATA, fact_card_text=card,
        user_text="NVDA 平仓", now=NOW)
    assert good.ok, good.reasons


def test_conditional_roll_replies_must_state_the_unverified_coverage():
    card = fact_card("evaluate_roll_down", ROLL_DATA)
    verdict = check_result_reply(
        "候选 US.NVDA270115C190000，卖价 2.2000；费用按模型估算，"
        "不是账户实际扣费。",
        method="evaluate_roll_down", data=ROLL_DATA, fact_card_text=card,
        user_text="NVDA 上移", now=NOW)
    assert "missing_unverified_conditions" in verdict.reasons
    vague = check_result_reply(
        "候选 US.NVDA270115C190000，卖价 2.2000；不构成正式账户建议；"
        "费用按模型估算，不是账户实际扣费。",
        method="evaluate_roll_down", data=ROLL_DATA, fact_card_text=card,
        user_text="NVDA 上移", now=NOW)
    # A generic disclaimer is not the coverage clause: the reply must say
    # *what* is unverified, not merely that the answer is advice-free.
    assert "missing_unverified_conditions" in vague.reasons


def test_unable_replies_must_give_the_reason():
    card = fact_card("query_candidates", UNABLE_CANDIDATES)
    bare = check_result_reply(
        "NVDA 没有候选。", method="query_candidates", data=UNABLE_CANDIDATES,
        fact_card_text=card, user_text="NVDA 候选", now=NOW)
    assert "missing_reason" in bare.reasons
    good = check_result_reply(
        "现在美股盘前，暂时查不了候选。", method="query_candidates",
        data=UNABLE_CANDIDATES, fact_card_text=card, user_text="NVDA 候选",
        now=NOW)
    assert good.ok, good.reasons


def test_empty_replies_are_rejected():
    assert check_result_reply(
        "", method="query_quotes", data=QUOTE_DATA,
        fact_card_text=fact_card("query_quotes", QUOTE_DATA),
        user_text="NVDA", now=NOW).reasons == ("empty",)


def test_fallback_with_many_candidates_stays_within_the_length_cap():
    candidates = [{
        "contract_symbol": f"US.NVDA2611{index:02d}P120000",
        "expiration": f"2026-11-{index:02d}", "strike": 120.0,
        "bid": 1.0 + index / 100, "ask": 1.1 + index / 100,
        "delta": -0.2, "dte": index, "economic_result": "positive_net_credit",
        "metrics": {"incremental_annualized_return": 0.2},
    } for index in range(1, 9)]
    data = {"symbol": "NVDA", "market": "US", "outcome": "found",
            "candidates": candidates, "displayed": len(candidates),
            "omitted": 0, "reason_codes": [],
            "effective_filters": {"dte": [7, 60], "dte_source": "public_policy",
                                  "limit": 10}}
    fallback = render_result("query_candidates", data)
    assert len(fallback) <= MAX_REPLY_CHARS
    assert "这里列出前 5 个" in fallback
    verdict = check_result_reply(
        fallback, method="query_candidates", data=data,
        fact_card_text=fact_card("query_candidates", data),
        user_text="NVDA 候选", now=NOW)
    assert verdict.ok, verdict.reasons


def test_host_fallback_wording_passes_its_own_guard():
    cases = [
        ("query_quotes", QUOTE_DATA),
        ("query_quotes", LAST_KNOWN_DATA),
        ("query_candidates", UNABLE_CANDIDATES),
        ("evaluate_close", CLOSE_DATA),
        ("evaluate_roll_down", ROLL_DATA),
    ]
    for method, data in cases:
        fallback = render_result(method, data)
        verdict = check_result_reply(
            fallback, method=method, data=data,
            fact_card_text=fact_card(method, data),
            user_text="NVDA 查询", now=NOW)
        assert verdict.ok, (method, verdict.reasons, fallback)
