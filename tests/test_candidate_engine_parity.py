from __future__ import annotations

import pandas as pd


def test_candidate_engine_put_rank_matches_option_candidate_strategy() -> None:
    from domain.domain.engine import rank_candidate_rows
    from domain.domain.engine import build_strategy_config, rank_candidates, score_candidates

    rows = [
        {"contract_symbol": "A", "annualized_net_return_on_cash_basis": 0.12, "net_income": 70},
        {"contract_symbol": "B", "annualized_net_return_on_cash_basis": 0.15, "net_income": 60},
        {"contract_symbol": "C", "annualized_net_return_on_cash_basis": 0.12, "net_income": 80},
    ]
    df = pd.DataFrame(rows)
    legacy = rank_candidates(score_candidates(df, build_strategy_config("put")), build_strategy_config("put"))
    engine = rank_candidate_rows(rows, mode="put")

    assert [r["contract_symbol"] for r in engine] == list(legacy["contract_symbol"])


def test_candidate_engine_call_rank_matches_option_candidate_strategy() -> None:
    from domain.domain.engine import rank_candidate_rows
    from domain.domain.engine import build_strategy_config, rank_candidates, score_candidates

    rows = [
        {"contract_symbol": "C1", "annualized_net_premium_return": 0.10, "if_exercised_total_return": 0.20, "net_income": 110},
        {"contract_symbol": "C2", "annualized_net_premium_return": 0.10, "if_exercised_total_return": 0.21, "net_income": 100},
        {"contract_symbol": "C3", "annualized_net_premium_return": 0.09, "if_exercised_total_return": 0.30, "net_income": 130},
    ]
    df = pd.DataFrame(rows)
    legacy = rank_candidates(score_candidates(df, build_strategy_config("call")), build_strategy_config("call"))
    engine = rank_candidate_rows(rows, mode="call")

    assert [r["contract_symbol"] for r in engine] == list(legacy["contract_symbol"])
    assert [r["contract_symbol"] for r in engine] == ["C1", "C2", "C3"]


def test_candidate_engine_put_summary_uses_simple_rank() -> None:
    from domain.domain.engine import rank_candidate_rows
    from scripts.report_summaries import summarize_sell_put

    rows = [
        {
            "symbol": "NVDA",
            "contract_symbol": "P_FAR_DELTA",
            "expiration": "2026-06-18",
            "strike": 140.0,
            "dte": 45,
            "mid": 2.0,
            "net_income": 200.0,
            "annualized_net_return_on_cash_basis": 0.20,
            "delta": -0.10,
        },
        {
            "symbol": "NVDA",
            "contract_symbol": "P_TARGET_DELTA",
            "expiration": "2026-06-18",
            "strike": 130.0,
            "dte": 45,
            "mid": 1.5,
            "net_income": 150.0,
            "annualized_net_return_on_cash_basis": 0.12,
            "delta": -0.22,
        },
    ]
    summary = summarize_sell_put(pd.DataFrame(rows), "NVDA")
    engine_top = rank_candidate_rows(rows, mode="put")[0]

    assert engine_top["contract_symbol"] == "P_FAR_DELTA"
    assert summary["top_contract"] == "2026-06-18 140P"


def test_candidate_engine_call_summary_uses_simple_rank() -> None:
    from domain.domain.engine import rank_candidate_rows
    from scripts.report_summaries import summarize_sell_call

    rows = [
        {
            "symbol": "AAPL",
            "contract_symbol": "C_FAR_DELTA",
            "expiration": "2026-06-18",
            "strike": 220.0,
            "dte": 45,
            "mid": 2.0,
            "net_income": 200.0,
            "annualized_net_premium_return": 0.20,
            "if_exercised_total_return": 0.15,
            "delta": 0.40,
        },
        {
            "symbol": "AAPL",
            "contract_symbol": "C_TARGET_DELTA",
            "expiration": "2026-06-18",
            "strike": 230.0,
            "dte": 45,
            "mid": 1.5,
            "net_income": 150.0,
            "annualized_net_premium_return": 0.12,
            "if_exercised_total_return": 0.10,
            "delta": 0.28,
            "covered_contracts_available": 1,
        },
    ]
    summary = summarize_sell_call(pd.DataFrame(rows), "AAPL")
    engine_top = rank_candidate_rows(rows, mode="call")[0]

    assert engine_top["contract_symbol"] == "C_FAR_DELTA"
    assert summary["top_contract"] == "2026-06-18 220C"


def test_candidate_engine_legacy_put_reject_rule_mapping_matches_filter_reject_log() -> None:
    from domain.domain.engine import normalize_legacy_reject_log_rows
    from domain.domain.engine import build_strategy_config, filter_candidates_with_reject_log

    df = pd.DataFrame(
        [
            {"symbol": "NVDA", "contract_symbol": "PASS", "annualized_net_return_on_cash_basis": 0.12, "net_income": 80, "otm_pct": 0.07, "spread_ratio": 0.20},
            {"symbol": "NVDA", "contract_symbol": "FAIL_RET", "annualized_net_return_on_cash_basis": 0.08, "net_income": 90, "otm_pct": 0.07, "spread_ratio": 0.20},
            {"symbol": "NVDA", "contract_symbol": "FAIL_NET", "annualized_net_return_on_cash_basis": 0.12, "net_income": 40, "otm_pct": 0.07, "spread_ratio": 0.20},
            {"symbol": "NVDA", "contract_symbol": "FAIL_SPREAD", "annualized_net_return_on_cash_basis": 0.12, "net_income": 80, "otm_pct": 0.07, "spread_ratio": 0.40},
        ]
    )
    cfg = build_strategy_config(
        "put",
        min_annualized_return=0.10,
        min_net_income=50,
        max_spread_ratio=0.30,
    )
    out, reject_log = filter_candidates_with_reject_log(df, cfg, reject_stage="step3_risk_gate")

    mapped = normalize_legacy_reject_log_rows(reject_log.to_dict("records"))
    assert list(out["contract_symbol"]) == ["PASS"]
    assert list(reject_log["engine_reject_stage"]) == [
        "stage2_return_floor",
        "stage2_return_floor",
        "stage3_risk_filter",
    ]
    assert list(reject_log["engine_reject_reason"]) == [
        "return_annualized",
        "return_net_income",
        "risk_spread",
    ]
    assert [(m["stage"], m["reason"]) for m in mapped] == [
        ("stage2_return_floor", "return_annualized"),
        ("stage2_return_floor", "return_net_income"),
        ("stage3_risk_filter", "risk_spread"),
    ]
    assert [m["legacy_reject_rule"] for m in mapped] == [
        "min_annualized_return",
        "min_net_income",
        "max_spread_ratio",
    ]
    assert mapped[0]["contract_symbol"] == "FAIL_RET"
    assert mapped[0]["symbol"] == "NVDA"


def test_candidate_engine_legacy_call_reject_rule_mapping_matches_filter_reject_log() -> None:
    from domain.domain.engine import normalize_legacy_reject_log_rows
    from domain.domain.engine import build_strategy_config, filter_candidates_with_reject_log

    df = pd.DataFrame(
        [
            {"symbol": "AAPL", "contract_symbol": "PASS", "annualized_net_premium_return": 0.12, "if_exercised_total_return": 0.08, "net_income": 80, "spread_ratio": 0.20},
            {"symbol": "AAPL", "contract_symbol": "FAIL_RET", "annualized_net_premium_return": 0.08, "if_exercised_total_return": 0.08, "net_income": 90, "spread_ratio": 0.20},
            {"symbol": "AAPL", "contract_symbol": "FAIL_NET", "annualized_net_premium_return": 0.12, "if_exercised_total_return": 0.02, "net_income": 40, "spread_ratio": 0.20},
            {"symbol": "AAPL", "contract_symbol": "FAIL_SPREAD", "annualized_net_premium_return": 0.12, "if_exercised_total_return": 0.08, "net_income": 80, "spread_ratio": 0.40},
        ]
    )
    cfg = build_strategy_config(
        "call",
        min_annualized_return=0.10,
        min_net_income=50,
        max_spread_ratio=0.30,
    )
    out, reject_log = filter_candidates_with_reject_log(df, cfg, reject_stage="step3_risk_gate")

    mapped = normalize_legacy_reject_log_rows(reject_log.to_dict("records"))
    assert list(out["contract_symbol"]) == ["PASS"]
    assert list(reject_log["engine_reject_stage"]) == [
        "stage2_return_floor",
        "stage2_return_floor",
        "stage3_risk_filter",
    ]
    assert list(reject_log["engine_reject_reason"]) == [
        "return_annualized",
        "return_net_income",
        "risk_spread",
    ]
    assert [(m["stage"], m["reason"]) for m in mapped] == [
        ("stage2_return_floor", "return_annualized"),
        ("stage2_return_floor", "return_net_income"),
        ("stage3_risk_filter", "risk_spread"),
    ]
    assert mapped[0]["contract_symbol"] == "FAIL_RET"
    assert mapped[0]["legacy_reject_stage"] == "step3_risk_gate"
