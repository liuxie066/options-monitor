from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.application.multi_tick.misc import AccountResult


def _account_dir(base: Path, run_id: str = "run-1", account: str = "lx") -> Path:
    path = base / "output_runs" / run_id / "accounts" / account
    path.mkdir(parents=True, exist_ok=True)
    state_dir = path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "portfolio_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:00+00:00",
                "cash_by_currency": {"HKD": 480_000, "USD": 18_000},
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "option_positions_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:30+00:00",
                "cash_secured_total_by_ccy": {"HKD": 255_000, "USD": 3_000},
                "cash_secured_unavailable_by_symbol": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_event_snapshot(base: Path, symbols: dict) -> None:
    state_dir = base / "output_runs" / "run-1" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "event_snapshot.json").write_text(
        json.dumps({"schema_version": 1, "symbols": symbols}),
        encoding="utf-8",
    )


def _complete_event_item(*, events: list[dict] | None = None, source_status: str = "ok") -> dict:
    return {
        "symbol": "NVDA",
        "selected_provider": "futu",
        "source_status": source_status,
        "events": list(events or []),
        "coverage": {
            "earnings": {"status": "complete", "error": ""},
            "ex_dividend": {"status": "complete", "error": ""},
            "split": {"status": "complete", "error": ""},
        },
    }


def _config(*, timezone_name: str = "America/New_York") -> dict:
    return {
        "schedule": {
            "timezone": timezone_name,
            "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
        },
        "notifications": {"daily_brief": {"max_candidates_per_strategy": 3}},
    }


def _result(*, ran_scan: bool = True, reason: str = "ok") -> AccountResult:
    return AccountResult("lx", ran_scan, True, reason, "legacy markdown must not be parsed")


def _assemble(
    base: Path,
    *,
    market: str = "US",
    result: AccountResult | None = None,
    pipeline_succeeded: bool = True,
    config: dict | None = None,
):
    from src.application.daily_decision_brief_service import assemble_daily_decision_brief

    return assemble_daily_decision_brief(
        base=base,
        run_id="run-1",
        account="lx",
        market=market,
        scheduler_decision={"in_run_window": True},
        account_result=result or _result(),
        pipeline_succeeded=pipeline_succeeded,
        config=config or _config(timezone_name="Asia/Hong_Kong" if market == "HK" else "America/New_York"),
        now_utc=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
    )


def _put_row(
    *,
    symbol: str = "NVDA",
    contract: str = "NVDA260821P00100000",
    annualized: float = 0.2,
    priority: str | None = None,
) -> dict:
    row = {
        "symbol": symbol,
        "option_type": "put",
        "contract_symbol": contract,
        "expiration": "2026-08-21",
        "strike": 100,
        "spot": 120,
        "dte": 35,
        "delta": -0.2,
        "annualized_net_return_on_cash_basis": annualized,
        "net_income": 200,
        "spread_ratio": 0.1,
        "open_interest": 500,
        "volume": 20,
        "cash_required_cny": 10_000,
        "cash_free_cny": 25_000,
    }
    if priority is not None:
        row["tier"] = priority
    return row


def _call_row(*, symbol: str = "NVDA", contract: str = "NVDA260821C00140000", annualized: float = 0.1) -> dict:
    return {
        "symbol": symbol,
        "option_type": "call",
        "contract_symbol": contract,
        "expiration": "2026-08-21",
        "strike": 140,
        "spot": 120,
        "dte": 35,
        "delta": 0.2,
        "annualized_net_premium_return": annualized,
        "net_income": 100,
        "spread_ratio": 0.1,
        "open_interest": 500,
        "volume": 20,
        "shares_total": 350,
        "shares_locked": 100,
        "shares_available_for_cover": 250,
        "multiplier": 100,
        "call_covered_contracts_available": 2,
    }


def test_assembler_uses_structured_candidates_ranking_and_capacity(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(contract="NVDA_LOW", annualized=0.10),
            _put_row(contract="NVDA_HIGH", annualized=0.25),
            _put_row(contract="NVDA_HIGH", annualized=0.25),
        ]
    ).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)
    pd.DataFrame([_call_row()]).to_csv(account_dir / "nvda_sell_call_candidates.csv", index=False)
    (account_dir / "symbols_notification.txt").write_text("P0 fake action from markdown", encoding="utf-8")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert [item["contract_symbol"] for item in brief["candidates"]["sell_put"]] == ["NVDA_HIGH", "NVDA_LOW"]
    assert brief["capacity"]["sell_put"]["contracts_available"] == 2
    assert brief["capacity"]["covered_call"]["contracts_available"] == 2
    assert len([item for item in brief["actions"] if item["strategy_family"] == "sell_put"]) == 2
    assert all("fake" not in item.get("reason", "") for item in brief["actions"])


def test_assembler_projects_multicurrency_funds_from_run_scoped_context(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )

    brief = _assemble(tmp_path)

    assert brief["funds"] == {
        "as_of_utc": "2026-07-17T13:59:30+00:00",
        "cash_total_by_currency": {"HKD": 480_000.0, "USD": 18_000.0},
        "option_opening_available_by_currency": {"HKD": 225_000.0, "USD": 15_000.0},
        "available": True,
        "reason": "ok",
    }


def test_unreliable_secured_usage_keeps_cash_but_does_not_invent_opening_funds(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    state_dir = account_dir / "state"
    (state_dir / "option_positions_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:30+00:00",
                "cash_secured_total_by_ccy": {"USD": 3_000},
                "cash_secured_unavailable_by_symbol": {"PDD": "basis_missing"},
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["funds"]["cash_total_by_currency"] == {"HKD": 480_000.0, "USD": 18_000.0}
    assert brief["funds"]["option_opening_available_by_currency"] == {}
    assert brief["funds"]["available"] is False
    assert brief["funds"]["reason"] == "option_cash_secured_unavailable"


def test_malformed_secured_reliability_flag_fails_opening_funds_closed(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    (account_dir / "state" / "option_positions_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:30+00:00",
                "cash_secured_total_by_ccy": {},
                "cash_secured_unavailable_by_symbol": ["malformed"],
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    assert brief["funds"]["available"] is False
    assert brief["funds"]["option_opening_available_by_currency"] == {}
    assert brief["status"] == "degraded"


def test_missing_cash_context_blocks_snapshot_without_fabricating_zero(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    (account_dir / "state" / "portfolio_context.json").unlink()

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert brief["funds"]["cash_total_by_currency"] == {}
    assert brief["funds"]["option_opening_available_by_currency"] == {}
    assert "cash_total_unavailable" in brief["actions"][0]["reason"]


def test_candidate_index_uses_all_ranked_candidates_beyond_display_limit(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(symbol="NVDA", contract="NVDA_LOW", annualized=0.10),
            _put_row(symbol="NVDA", contract="NVDA_HIGH", annualized=0.30),
            _put_row(symbol="PDD", contract="PDD_1", annualized=0.25),
            _put_row(symbol="FUTU", contract="FUTU_1", annualized=0.20),
            _put_row(symbol="GOOGL", contract="GOOGL_1", annualized=0.15),
        ]
    ).to_csv(account_dir / "all_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(tmp_path)

    assert len(brief["candidates"]["sell_put"]) == 3
    assert len(brief["candidate_index"]) == 4
    by_symbol = {item["symbol"]: item for item in brief["candidate_index"]}
    assert by_symbol["NVDA"]["contract_count"] == 2
    assert by_symbol["NVDA"]["representative"]["contract_symbol"] == "NVDA_HIGH"
    assert set(by_symbol) == {"NVDA", "PDD", "FUTU", "GOOGL"}


def test_noop_account_result_is_not_a_successful_snapshot(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )

    brief = _assemble(tmp_path, result=_result(ran_scan=False, reason="scheduler noop"))

    assert brief["actionability"] == "blocked"
    assert "scheduler noop" in brief["actions"][0]["reason"]
    assert brief["candidate_index"] == []


def test_candidate_event_projection_binds_snapshot_to_candidate_and_action(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    _write_event_snapshot(
        tmp_path,
        {
            "NVDA": _complete_event_item(
                events=[
                    {
                        "type": "earnings",
                        "date": "2026-08-05",
                        "raw": {"fiscal_year": "2026", "financial_type": "Q2"},
                    }
                ]
            )
        },
    )

    brief = _assemble(tmp_path)
    candidate = brief["candidates"]["sell_put"][0]
    action = next(item for item in brief["actions"] if item["action_type"] == "open_candidate")
    risk = candidate["event_risk"]

    assert action["event_risk"] == risk
    assert risk["user_state"] == "confirmed_event"
    assert risk["days_to_event"] == 19
    assert risk["expiration_relations"]["contract"] == {
        "expiration": "2026-08-21",
        "relation": "before_expiration",
        "days_before_expiration": 16,
    }
    assert risk["in_attention_window"] is True
    assert brief["events"] == [
        {
            **risk["events"][0],
            "symbol": "NVDA",
            "candidate_action_id": action["action_id"],
            "strategy_family": "sell_put",
            "contract_symbol": "NVDA260821P00100000",
            "strategy_group_id": "",
        }
    ]


def test_candidate_event_projection_confirms_complete_primary_absence(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    _write_event_snapshot(tmp_path, {"NVDA": _complete_event_item()})

    brief = _assemble(tmp_path)

    assert brief["candidates"]["sell_put"][0]["event_risk"]["user_state"] == "confirmed_none"
    assert brief["events"] == []


def test_candidate_event_projection_never_falls_back_to_candidate_csv_fields(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    row = _put_row()
    row.update(
        {
            "event_flag": True,
            "event_types": "earnings",
            "event_dates": "2026-08-05",
            "event_source_status": "ok",
        }
    )
    pd.DataFrame([row]).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(tmp_path)

    assert brief["candidates"]["sell_put"][0]["event_risk"]["user_state"] == "unknown"
    assert brief["candidates"]["sell_put"][0]["event_risk"]["reason_code"] == "event_snapshot_missing"
    assert brief["events"] == []


def test_malformed_event_snapshot_degrades_candidate_to_unknown(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    state_dir = tmp_path / "output_runs" / "run-1" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "event_snapshot.json").write_text("{broken", encoding="utf-8")

    brief = _assemble(tmp_path)

    risk = brief["candidates"]["sell_put"][0]["event_risk"]
    assert risk["user_state"] == "unknown"
    assert risk["reason_code"] == "event_snapshot_malformed"
    assert any(item["reason"] == "event_snapshot_malformed" for item in brief["data_gaps"])


def test_event_projection_does_not_change_action_identity_or_candidate_order(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(contract="NVDA_LOW", annualized=0.10),
            _put_row(contract="NVDA_HIGH", annualized=0.25),
        ]
    ).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    without_snapshot = _assemble(tmp_path)
    before = {
        item["contract_symbol"]: item["action_id"]
        for item in without_snapshot["actions"]
        if item["action_type"] == "open_candidate"
    }
    _write_event_snapshot(tmp_path, {"NVDA": _complete_event_item()})
    with_snapshot = _assemble(tmp_path)
    after = {
        item["contract_symbol"]: item["action_id"]
        for item in with_snapshot["actions"]
        if item["action_type"] == "open_candidate"
    }

    assert before == after
    assert [item["contract_symbol"] for item in with_snapshot["candidates"]["sell_put"]] == [
        "NVDA_HIGH",
        "NVDA_LOW",
    ]


def test_candidate_priority_reuses_tier_without_promoting_rank_one(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(contract="NVDA_DEFAULT", annualized=0.30),
            _put_row(contract="NVDA_STRONG", annualized=0.20, priority="strong"),
        ]
    ).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(tmp_path)
    priorities = {item["contract_symbol"]: item["priority"] for item in brief["actions"] if item["action_type"] == "open_candidate"}

    assert priorities["NVDA_DEFAULT"] == "P1"
    assert priorities["NVDA_STRONG"] == "P0"


def test_close_advice_preserves_lot_group_and_leg_identity(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)
    pd.DataFrame(
        [
            {
                "account": "lx",
                "position_lot_id": "lot-put",
                "strategy_group_id": "group-1",
                "leg_role": "funding_put",
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 100,
                "tier": "strong",
                "tier_label": "强提醒",
                "reason": "收益已锁定",
                "close_action": "close_put_keep_call",
                "position_side": "short",
                "close_mid": 0.52,
                "realized_if_close": 474.5,
                "remaining_annualized_return": 0.042,
            }
        ]
    ).to_csv(account_dir / "close_advice.csv", index=False)

    brief = _assemble(tmp_path)
    action = next(item for item in brief["actions"] if item["action_type"] == "close_position")

    assert action["priority"] == "P0"
    assert action["position_lot_id"] == "lot-put"
    assert action["strategy_group_id"] == "group-1"
    assert action["leg_role"] == "funding_put"
    assert action["close_action"] == "close_put_keep_call"
    assert brief["positions"][0]["position_lot_id"] == "lot-put"
    assert brief["positions"][0]["metrics"] == {
        "close_mid": 0.52,
        "realized_if_close": 474.5,
        "remaining_annualized_return": 0.042,
    }


def test_combo_yield_preserves_pipeline_order_and_dedupes_group_legs(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "candidate_pair_id": "pair-b",
                "put_contract_symbol": "NVDA_P95",
                "call_contract_symbol": "NVDA_C130",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-09-18",
                "put_strike": 95,
                "call_strike": 130,
                "annualized_net_credit_yield": 0.08,
            },
            {
                "symbol": "NVDA",
                "candidate_pair_id": "pair-a",
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C125",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-09-18",
                "put_strike": 100,
                "call_strike": 125,
                "annualized_net_credit_yield": 0.20,
            },
            {
                "symbol": "NVDA",
                "candidate_pair_id": "pair-b",
                "put_contract_symbol": "NVDA_P95",
                "call_contract_symbol": "NVDA_C130",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-09-18",
                "put_strike": 95,
                "call_strike": 130,
                "annualized_net_credit_yield": 0.50,
            },
        ]
    ).to_csv(account_dir / "nvda_combo_yield_candidates.csv", index=False)

    brief = _assemble(tmp_path)
    combos = brief["candidates"]["combo_yield"]

    assert [item["strategy_group_id"] for item in combos] == ["pair-b", "pair-a"]
    assert combos[0]["put_leg_role"] == "funding_put"
    assert combos[0]["call_leg_role"] == "participation_call"
    combo_actions = [item for item in brief["actions"] if item["strategy_family"] == "combo_yield"]
    assert [item["strategy_group_id"] for item in combo_actions] == ["pair-b", "pair-a"]


def test_combo_yield_event_projection_relates_to_both_expirations(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "candidate_pair_id": "pair-a",
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C125",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-09-18",
                "put_strike": 100,
                "call_strike": 125,
                "annualized_net_credit_yield": 0.20,
            }
        ]
    ).to_csv(account_dir / "nvda_combo_yield_candidates.csv", index=False)
    _write_event_snapshot(
        tmp_path,
        {"NVDA": _complete_event_item(events=[{"type": "earnings", "date": "2026-08-30"}])},
    )

    brief = _assemble(tmp_path)
    candidate = brief["candidates"]["combo_yield"][0]
    action = next(item for item in brief["actions"] if item["action_type"] == "open_combo_yield")

    assert action["event_risk"] == candidate["event_risk"]
    assert candidate["event_risk"]["expiration_relations"] == {
        "put": {
            "expiration": "2026-08-21",
            "relation": "after_expiration",
            "days_before_expiration": -9,
        },
        "call": {
            "expiration": "2026-09-18",
            "relation": "before_expiration",
            "days_before_expiration": 19,
        },
    }
    assert candidate["event_risk"]["in_attention_window"] is True


def test_partial_symbol_csv_failure_becomes_gap_without_blocking_other_actions(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "aaa_sell_put_candidates_labeled.csv").write_text('symbol,contract_symbol\n"broken', encoding="utf-8")
    pd.DataFrame([_put_row(symbol="PDD", contract="PDD_VALID")]).to_csv(
        account_dir / "pdd_sell_put_candidates_labeled.csv", index=False
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert any(item["contract_symbol"] == "PDD_VALID" for item in brief["actions"])
    assert any(item["reason"] == "csv_unavailable" for item in brief["data_gaps"])


def test_header_only_and_empty_csv_are_readable_empty_decisions(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)
    (account_dir / "close_advice.csv").write_text("", encoding="utf-8")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["candidates"]["sell_put"] == []
    assert not any(item["action_type"] == "resolve_data_blocker" for item in brief["actions"])


def test_all_structured_sources_unavailable_blocks_account(tmp_path: Path) -> None:
    _account_dir(tmp_path)

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    blocker = brief["actions"][0]
    assert blocker["priority"] == "P0"
    assert blocker["state"] == "blocked"
    assert "all_structured_decision_sources_unavailable" in blocker["reason"]


def test_pipeline_failure_blocks_even_when_ran_scan_and_candidate_artifact_exist(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(
        tmp_path,
        result=_result(ran_scan=True, reason="pipeline failed"),
        pipeline_succeeded=False,
    )

    assert brief["actionability"] == "blocked"
    assert "pipeline failed" in brief["actions"][0]["reason"]


def test_missing_capacity_suppresses_only_affected_candidate_and_blocks_when_all_requirements_missing(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    row = _put_row()
    row.pop("cash_required_cny")
    row.pop("cash_free_cny")
    pd.DataFrame([row]).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(tmp_path)

    assert brief["candidates"]["sell_put"][0]["contract_symbol"] == row["contract_symbol"]
    assert not any(item.get("contract_symbol") == row["contract_symbol"] for item in brief["actions"])
    assert brief["actionability"] == "blocked"
    assert any(item["reason"] == "cash_capacity_unavailable" for item in brief["data_gaps"])


def test_market_partition_excludes_other_market_rows_and_uses_market_date(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(symbol="NVDA", contract="US_NVDA"),
            _put_row(symbol="0700.HK", contract="HK_0700"),
        ]
    ).to_csv(account_dir / "mixed_sell_put_candidates_labeled.csv", index=False)

    us = _assemble(tmp_path, market="US")
    hk = _assemble(tmp_path, market="HK")

    assert [item["contract_symbol"] for item in us["candidates"]["sell_put"]] == ["US_NVDA"]
    assert [item["contract_symbol"] for item in hk["candidates"]["sell_put"]] == ["HK_0700"]
    assert us["market_trading_date"] == "2026-07-17"
    assert hk["market_trading_date"] == "2026-07-17"
    assert us["valid_until_utc"] != hk["valid_until_utc"]


def test_prefetch_symbol_failure_is_a_local_gap_not_account_blocker(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(symbol="PDD", contract="PDD_VALID")]).to_csv(
        account_dir / "pdd_sell_put_candidates_labeled.csv", index=False
    )
    state_dir = account_dir / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "required_data_prefetch_summary.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:00+00:00",
                "summary": {"errors": 1},
                "symbols": {"NVDA": {"status": "error", "reason": "quote unavailable"}},
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert any(item.get("symbol") == "NVDA" for item in brief["data_gaps"])
    assert any(item["contract_symbol"] == "PDD_VALID" for item in brief["actions"])


def test_rejection_summary_is_market_qualified(tmp_path: Path) -> None:
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )

    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "0700_sell_put_candidates_labeled.csv", index=False
    )
    append_candidate_filter_trace_rows(
        account_dir / "candidate_filter_trace.jsonl",
        [
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="NVDA",
                function="sell_put",
                status="rejected",
                stage="risk",
                rule="risk_spread",
            ),
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="0700.HK",
                function="sell_put",
                status="rejected",
                stage="risk",
                rule="risk_volume",
            ),
        ],
    )

    brief = _assemble(tmp_path, market="HK")

    assert brief["rejections"]["total_rejected"] == 1
    assert brief["rejections"]["top_categories"][0]["sample_symbols"] == ["0700.HK"]


def test_sell_put_conflict_uses_only_labeled_candidates(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    labeled_rows = [
        _put_row(symbol="0700.HK", contract="0700_P430", annualized=0.18),
        _put_row(symbol="0700.HK", contract="0700_P440", annualized=0.20),
    ]
    raw_rows = [
        *labeled_rows,
        _put_row(symbol="0700.HK", contract="0700_P450_RAW_ONLY", annualized=0.99),
    ]
    pd.DataFrame(labeled_rows).to_csv(account_dir / "0700_sell_put_candidates_labeled.csv", index=False)
    pd.DataFrame(raw_rows).to_csv(account_dir / "0700_sell_put_candidates.csv", index=False)

    brief = _assemble(tmp_path, market="HK")

    assert {item["contract_symbol"] for item in brief["candidates"]["sell_put"]} == {
        "0700_P430",
        "0700_P440",
    }
    assert {item["contract_symbol"] for item in brief["actions"] if item.get("contract_symbol")} == {
        "0700_P430",
        "0700_P440",
    }
    assert "0700_P450_RAW_ONLY" not in json.dumps(brief, sort_keys=True)
    from src.application.daily_decision_brief_renderer import render_full_brief

    assert "0700_P450_RAW_ONLY" not in render_full_brief(brief)
    assert "有效行动 2 条" in brief["strategy_summary"]
    assert "候选证据：Sell Put 2，Covered Call 0，Combo Yield 0" in brief["strategy_summary"]
    assert "数据缺口" in brief["strategy_summary"]
    assert not any(item["path"].endswith("_sell_put_candidates.csv") for item in brief["source_artifacts"])


def test_sell_put_controlled_newline_is_authoritative_empty(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"\n")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["candidates"]["sell_put"] == []
    assert not any(
        item["strategy_family"] == "sell_put" and item["reason"] == "csv_unavailable"
        for item in brief["data_gaps"]
    )


def test_sell_put_controlled_crlf_is_authoritative_empty(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"\r\n")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["candidates"]["sell_put"] == []
    assert not any(
        item["strategy_family"] == "sell_put" and item["reason"] == "csv_unavailable"
        for item in brief["data_gaps"]
    )


def test_sell_put_header_only_requires_minimum_schema(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_text("symbol,annualized\n", encoding="utf-8")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert any(
        item["strategy_family"] == "sell_put"
        and item["reason"] == "csv_unavailable"
        and item["error_type"] == "SchemaError"
        for item in brief["data_gaps"]
    )


def test_sell_put_zero_byte_is_malformed_not_empty(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert any(
        item["strategy_family"] == "sell_put"
        and item["reason"] == "csv_unavailable"
        and item["error_type"] == "EmptyDataError"
        for item in brief["data_gaps"]
    )


def test_sell_put_unrecognized_whitespace_is_malformed_not_empty(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"  ")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert any(
        item["strategy_family"] == "sell_put"
        and item["reason"] == "csv_unavailable"
        and item["error_type"] == "EmptyDataError"
        for item in brief["data_gaps"]
    )


def test_sell_put_raw_only_artifact_reports_canonical_missing_without_fallback(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(contract="RAW_ONLY")]).to_csv(
        account_dir / "nvda_sell_put_candidates.csv", index=False
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert brief["candidates"]["sell_put"] == []
    assert "RAW_ONLY" not in json.dumps(brief, sort_keys=True)
    assert any(
        item["strategy_family"] == "sell_put"
        and item["artifact_key"] == "nvda"
        and item["reason"] == "canonical_labeled_artifact_missing"
        for item in brief["data_gaps"]
    )


def test_sell_put_failure_preserves_covered_call_action_and_degrades_status(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        'symbol,contract_symbol\n"broken', encoding="utf-8"
    )
    pd.DataFrame([_call_row(contract="NVDA_CALL_VALID")]).to_csv(
        account_dir / "nvda_sell_call_candidates.csv", index=False
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["status"] == "degraded"
    assert brief["candidates"]["sell_put"] == []
    assert any(item.get("contract_symbol") == "NVDA_CALL_VALID" for item in brief["actions"])
    assert any(
        item["strategy_family"] == "sell_put" and item["reason"] == "csv_unavailable"
        for item in brief["data_gaps"]
    )


def test_strategy_step_failure_trace_blocks_false_normal_empty_result(tmp_path: Path) -> None:
    from src.application.strategy_scan_failures import append_strategy_scan_failure

    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"\n")
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="NVDA",
        strategy_family="sell_put",
        error=RuntimeError("scanner crashed"),
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert "candidate_strategy_execution_failed" in brief["actions"][0]["reason"]
    assert any(
        item["strategy_family"] == "sell_put"
        and item["reason"] == "strategy_step_failed"
        and item["error_type"] == "RuntimeError"
        for item in brief["data_gaps"]
    )


def test_strategy_step_failure_preserves_other_candidates_and_warns_user(tmp_path: Path) -> None:
    from src.application.strategy_scan_failures import append_strategy_scan_failure
    from src.application.daily_decision_brief_renderer import render_fixed_report

    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(contract="STALE_PUT")]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    pd.DataFrame([_call_row(contract="NVDA_CALL_VALID")]).to_csv(
        account_dir / "nvda_sell_call_candidates.csv",
        index=False,
    )
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="NVDA",
        strategy_family="sell_put",
        error=RuntimeError("scanner crashed"),
    )

    brief = _assemble(tmp_path)
    message = render_fixed_report(brief)

    assert brief["actionability"] == "live_actionable"
    assert brief["status"] == "degraded"
    assert any(item.get("contract_symbol") == "NVDA_CALL_VALID" for item in brief["actions"])
    assert not any(item.get("contract_symbol") == "STALE_PUT" for item in brief["actions"])
    assert brief["candidates"]["sell_put"] == []
    assert "Sell Put 扫描异常，本轮结果不完整" in message
    assert "NVDA_CALL_VALID" not in message
    assert "Covered Call" in message
