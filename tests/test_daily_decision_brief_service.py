from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.application.multi_tick.misc import AccountResult


@pytest.fixture(autouse=True)
def _default_successful_v1_position_advice_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application import daily_decision_brief_service as service

    monkeypatch.setattr(
        service,
        "read_position_advice_v2_from_ledger",
        lambda **_kwargs: {
            "availability_status": "unavailable",
            "freshness": {"status": "fresh", "reason_codes": []},
            "authority_mode": "v1",
            "authority_generation": 0,
            "authority_policy_hash": None,
            "portfolio_plan_id": None,
            "account_run_id": "run-1",
            "row_count": 0,
            "actionable_count": 0,
            "model_actionable_count": 0,
            "model_trade_actionable_count": 0,
            "human_review_required_count": 0,
            "rows": [],
        },
    )


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
                "cash_secured_total_cny": 250_500.0,
                "exchange_rates": {
                    "rates": {"USDCNY": 7.0, "HKDCNY": 0.9},
                    "timestamp": "2026-07-17T13:59:30+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "position_advice_sources.v2.json").write_text(
        json.dumps(
            {
                "account": account,
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": "a" * 64,
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


def test_missing_success_summary_blocks_normal_delivery_but_current_run_identity_allows_failure(
    tmp_path: Path,
) -> None:
    from domain.domain.position_advice_authority import (
        portfolio_account_identity_hash,
    )
    from src.application.position_advice_source_producers import (
        publish_portfolio_source_snapshot,
    )

    account_dir = _account_dir(tmp_path)
    state_dir = account_dir / "state"
    (state_dir / "position_advice_sources.v2.json").unlink()
    identifiers = ["futu-lx-current"]
    identity_hash = portfolio_account_identity_hash(
        normalized_portfolio_source="futu",
        broker_account_identifiers=identifiers,
    )
    publish_portfolio_source_snapshot(
        producer_root=state_dir,
        account_run_id="run-1",
        account="lx",
        broker="futu",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity_hash,
        included_markets=["US"],
        portfolio_context={
            "source_observed_at": "2026-07-17T14:00:00+00:00",
            "source_account_identifiers": identifiers,
            "cash_by_currency": {"USD": 1000},
        },
        completed_at="2026-07-17T14:00:01+00:00",
    )

    brief = _assemble(tmp_path)
    authority = brief["notification_authority"]

    assert brief["actionability"] == "blocked"
    assert authority["normal_delivery_allowed"] is False
    assert authority["notification_allowed"] is False
    assert authority["blocker"] == "position_advice_source_summary_missing"
    assert authority["normal_delivery_token"] is None
    assert authority["fixed_failure_delivery_allowed"] is True
    assert authority["fixed_failure_delivery_token"][
        "schema_version"
    ] == "position_advice_notification_authority_token.v2"
    assert authority["fixed_failure_delivery_token"][
        "authorized_delivery_kinds"
    ] == ["fixed_failure"]
    assert authority["identity_evidence"]["status"] == "available"
    assert authority["authority_identity_source"] == (
        "current_run_portfolio_receipt"
    )
    assert len(authority["identity_snapshot_id"]) == 64
    assert len(authority["identity_receipt_hash"]) == 64
    assert (
        authority["identity_evidence"][
            "portfolio_account_identity_hash"
        ]
        == identity_hash
    )


def test_missing_success_summary_without_current_run_identity_blocks_all_delivery(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    (
        account_dir
        / "state"
        / "position_advice_sources.v2.json"
    ).unlink()

    brief = _assemble(tmp_path)
    authority = brief["notification_authority"]

    assert authority["normal_delivery_allowed"] is False
    assert authority["fixed_failure_delivery_allowed"] is False
    assert authority["normal_delivery_token"] is None
    assert authority["fixed_failure_delivery_token"] is None
    assert authority["identity_evidence"]["reason"] == (
        "current_run_portfolio_receipt_missing"
    )


@pytest.mark.parametrize(
    ("authority_allowed", "builder_fails", "expected_blocker"),
    (
        (
            False,
            False,
            "position_advice_authority_conflict",
        ),
        (
            True,
            True,
            "position_advice_failure_token_build_failed",
        ),
    ),
)
def test_failure_authority_reason_codes_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authority_allowed: bool,
    builder_fails: bool,
    expected_blocker: str,
) -> None:
    from src.application import daily_decision_brief_service as service

    monkeypatch.setattr(
        service,
        "read_authority_resolution",
        lambda **_kwargs: SimpleNamespace(
            notifications_allowed=authority_allowed,
            resolution_status=(
                "resolved" if authority_allowed else "authority_conflict"
            ),
            mode="v1" if authority_allowed else None,
            generation=0 if authority_allowed else None,
            policy_hash=None,
        ),
    )
    if builder_fails:
        monkeypatch.setattr(
            service,
            "build_fixed_failure_notification_authority_token",
            lambda **_kwargs: (_ for _ in ()).throw(
                ValueError("invalid token")
            ),
        )

    result = service._daily_brief_notification_authority(
        {
            "mode": "authority_conflict",
            "available": False,
            "blocker": "position_advice_source_summary_missing",
        },
        base=tmp_path,
        account="sy",
        account_run_id="run-sy",
        current_run_identity={
            "status": "available",
            "normalized_portfolio_source": "futu",
            "portfolio_account_identity_hash": "a" * 64,
            "snapshot_id": "b" * 64,
            "receipt_hash": "c" * 64,
        },
    )

    assert result["fixed_failure_delivery_allowed"] is False
    assert result["fixed_failure_delivery_token"] is None
    assert result["fixed_failure_blocker"] == expected_blocker


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
    assert [item["contract_symbol"] for item in brief["candidates"]["sell_put"]] == ["NVDA_HIGH"]
    assert brief["capacity"]["sell_put"]["contracts_available"] == 2
    assert brief["capacity"]["covered_call"]["contracts_available"] == 2
    assert len([item for item in brief["actions"] if item["strategy_family"] == "sell_put"]) == 1
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
        "cash_total_cny": 558_000.0,
        "cash_secured_total_cny": 250_500.0,
        "option_opening_available_cny": 307_500.0,
        "available": True,
        "reason": "ok",
    }


def test_funds_cny_totals_cover_secured_currency_without_cash(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    state_dir = account_dir / "state"
    (state_dir / "portfolio_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:00+00:00",
                "cash_by_currency": {"HKD": 1_104_060.32},
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "option_positions_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:30+00:00",
                "cash_secured_total_by_ccy": {"HKD": 171_000, "USD": 8_500},
                "cash_secured_unavailable_by_symbol": {},
                "cash_secured_total_cny": 213_400.0,
                "exchange_rates": {
                    "rates": {"USDCNY": 7.0, "HKDCNY": 0.9},
                    "timestamp": "2026-07-17T13:59:30+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    funds = brief["funds"]
    assert funds["cash_total_by_currency"] == {"HKD": 1_104_060.32}
    assert funds["option_opening_available_by_currency"]["HKD"] == pytest.approx(933_060.32)
    assert funds["cash_total_cny"] == pytest.approx(1_104_060.32 * 0.9)
    assert funds["cash_secured_total_cny"] == 213_400.0
    assert funds["option_opening_available_cny"] == pytest.approx(1_104_060.32 * 0.9 - 213_400.0)
    assert funds["available"] is True
    assert funds["reason"] == "ok"
    assert not any(item.get("scope") == "funds" for item in brief["data_gaps"])


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


def test_candidate_index_uses_one_ranked_candidate_per_symbol_beyond_display_limit(tmp_path: Path) -> None:
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
    assert by_symbol["NVDA"]["contract_count"] == 1
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
    ]


def test_candidate_priority_does_not_promote_lower_return_same_symbol_contract(tmp_path: Path) -> None:
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
    assert "NVDA_STRONG" not in priorities


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


def test_close_advice_daily_brief_honors_notify_levels(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    pd.DataFrame(
        [
            {
                "account": "lx",
                "position_lot_id": f"lot-{tier}",
                "symbol": symbol,
                "strategy_family": "sell_put",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": strike,
                "tier": tier,
                "tier_label": tier,
                "reason": "test",
                "close_action": "close",
                "evaluation_status": "priced",
                "quote_status": "priced",
            }
            for tier, symbol, strike in (
                ("strong", "STRONG", 100),
                ("medium", "MEDIUM", 101),
                ("optional", "OPTIONAL", 102),
                ("weak", "WEAK", 103),
            )
        ]
    ).to_csv(account_dir / "close_advice.csv", index=False)
    config = _config()
    config["close_advice"] = {
        "enabled": True,
        "notify_levels": ["strong", "medium"],
    }

    brief = _assemble(tmp_path, config=config)
    close_actions = [
        item
        for item in brief["actions"]
        if item["action_type"] == "close_position"
    ]
    eligibility = {
        item["symbol"]: item["notification_eligible"]
        for item in brief["positions"]
    }
    message = render_full_brief(
        brief,
        limits={"max_actions_per_priority": 10},
    )

    assert {item["symbol"] for item in close_actions} == {"STRONG", "MEDIUM"}
    assert eligibility == {
        "STRONG": True,
        "MEDIUM": True,
        "OPTIONAL": False,
        "WEAK": False,
    }
    assert "STRONG｜Sell Put｜08-21 $100 Put｜强烈建议平仓" in message
    assert "MEDIUM｜Sell Put｜08-21 $101 Put｜建议平仓" in message
    assert "OPTIONAL" not in message
    assert "WEAK" not in message
    assert "汇总｜共 4 条，需处理 2 条。" in message

    config["close_advice"]["notify_levels"] = ["optional"]
    optional_brief = _assemble(tmp_path, config=config)
    optional_actions = [
        item
        for item in optional_brief["actions"]
        if item["action_type"] == "close_position"
    ]
    optional_message = render_full_brief(
        optional_brief,
        limits={"max_actions_per_priority": 10},
    )

    assert {item["symbol"] for item in optional_actions} == {"OPTIONAL"}
    assert "OPTIONAL｜Sell Put｜08-21 $102 Put｜低价买回可选" in optional_message
    assert "STRONG" not in optional_message
    assert "MEDIUM" not in optional_message
    assert "WEAK" not in optional_message
    assert "汇总｜共 4 条，需处理 1 条。" in optional_message


def test_close_advice_daily_brief_honors_ranked_account_limit(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    pd.DataFrame(
        [
            {
                "account": "lx",
                "position_lot_id": f"lot-{symbol.lower()}",
                "symbol": symbol,
                "strategy_family": "sell_put",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": strike,
                "tier": "strong",
                "tier_label": "strong",
                "reason": "test",
                "close_action": "close",
                "evaluation_status": "priced",
                "quote_status": "priced",
                "capture_ratio": capture_ratio,
                "remaining_premium": remaining_premium,
            }
            for symbol, strike, capture_ratio, remaining_premium in (
                ("SECOND", 101, 0.80, 10),
                ("FIRST", 100, 0.95, 5),
                ("THIRD", 102, 0.70, 20),
            )
        ]
    ).to_csv(account_dir / "close_advice.csv", index=False)
    config = _config()
    config["close_advice"] = {
        "enabled": True,
        "notify_levels": ["strong", "medium"],
        "max_items_per_account": 1,
    }

    brief = _assemble(tmp_path, config=config)
    close_actions = [
        item
        for item in brief["actions"]
        if item["action_type"] == "close_position"
    ]
    eligibility = {
        item["symbol"]: item["notification_eligible"]
        for item in brief["positions"]
    }
    message = render_full_brief(
        brief,
        limits={"max_actions_per_priority": 10},
    )

    assert [item["symbol"] for item in close_actions] == ["FIRST"]
    assert eligibility == {
        "SECOND": False,
        "FIRST": True,
        "THIRD": False,
    }
    assert "FIRST｜Sell Put｜08-21 $100 Put｜强烈建议平仓" in message
    assert "SECOND" not in message
    assert "THIRD" not in message
    assert "汇总｜共 3 条，需处理 1 条。" in message


def test_daily_brief_uses_only_v2_position_authority_when_promoted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application import daily_decision_brief_service as service

    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    (account_dir / "state" / "position_advice_sources.v2.json").write_text(
        json.dumps(
            {
                "account": "lx",
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "account": "lx",
                "position_lot_id": "legacy-lot",
                "symbol": "NVDA",
                "option_type": "put",
                "tier": "strong",
                "close_action": "close",
            }
        ]
    ).to_csv(account_dir / "close_advice.csv", index=False)
    monkeypatch.setattr(
        service,
        "read_position_advice_v2_from_ledger",
        lambda **_kwargs: {
            "availability_status": "available",
            "freshness": {"status": "fresh", "reason_codes": []},
            "authority_mode": "v2",
            "portfolio_plan_id": "plan-v2",
            "account_run_id": "run-1",
            "row_count": 1,
            "actionable_count": 1,
            "model_actionable_count": 1,
            "rows": [
                {
                    "position_id": "v2-lot",
                    "strategy_family": "short_put",
                    "strategy_group_id": None,
                    "leg_role": None,
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "expiration": "2026-08-21",
                    "strike": 100,
                    "contract_symbol": "NVDA260821P00100000",
                    "lifecycle_state": "open",
                    "group_structure_state": "standalone",
                    "recommendation": "roll",
                    "actionable": True,
                    "action_scope": "position",
                    "reason_codes": ["positive_carry_improvement"],
                    "portfolio_plan_id": "plan-v2",
                    "execution_order": 1,
                    "depends_on": [],
                    "quote_as_of": "2026-07-17T13:59:30Z",
                    "net_carry_improvement_H_base_cny": "120",
                    "payback_days": "2",
                }
            ],
        },
    )

    brief = _assemble(tmp_path)

    position_actions = [
        item
        for item in brief["actions"]
        if item["action_type"].startswith("position_")
    ]
    assert [item["position_lot_id"] for item in position_actions] == [
        "v2-lot"
    ]
    assert not any(
        item.get("position_lot_id") == "legacy-lot"
        for item in brief["actions"]
    )
    assert brief["positions"][0]["recommendation"] == "roll"
    assert brief["position_advice_preview"]["authority_mode"] == "v2"


@pytest.mark.parametrize("authority_mode", ("v1", "v2_shadow", "v2"))
def test_daily_brief_keeps_lifecycle_human_review_visible_in_every_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authority_mode: str,
) -> None:
    from src.application import daily_decision_brief_service as service

    account_dir = _account_dir(tmp_path)
    (account_dir / "state" / "position_advice_sources.v2.json").write_text(
        json.dumps(
            {
                "account": "lx",
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        service,
        "read_position_advice_v2_from_ledger",
        lambda **_kwargs: {
            "availability_status": "available",
            "freshness": {"status": "fresh", "reason_codes": []},
            "authority_mode": authority_mode,
            "portfolio_plan_id": "plan-review",
            "account_run_id": "run-review",
            "row_count": 1,
            "actionable_count": 0,
            "model_actionable_count": 0,
            "model_trade_actionable_count": 0,
            "human_review_required_count": 1,
            "rows": [
                {
                    "position_id": "review-lot",
                    "strategy_family": "short_put",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "expiration": "2026-07-01",
                    "strike": 100,
                    "contract_symbol": "NVDA260701P00100000",
                    "lifecycle_state": "needs_review",
                    "group_structure_state": "standalone",
                    "recommendation": "review",
                    "model_trade_actionable": False,
                    "model_actionable": False,
                    "human_review_required": True,
                    "actionable": False,
                    "action_scope": "lifecycle_fact_review",
                    "reason_codes": [
                        "lifecycle_needs_review",
                        "lifecycle_read_model_missing",
                    ],
                    "portfolio_plan_id": "plan-review",
                    "depends_on": [],
                }
            ],
        },
    )

    brief = _assemble(tmp_path)
    review = next(
        item
        for item in brief["actions"]
        if item.get("position_lot_id") == "review-lot"
    )

    assert review["priority"] == "P0"
    assert review["action_type"] == "position_review"
    assert review["human_review_required"] is True
    assert review["model_trade_actionable"] is False
    assert review["requires_user_confirmation"] is True
    assert any(
        item.get("position_lot_id") == "review-lot"
        for item in brief["positions"]
    )


def test_combo_yield_selects_one_pair_per_symbol_and_ranks_before_truncation(tmp_path: Path) -> None:
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
                "structure_mode": "staggered_expiry_pair",
                "funding_accepted": True,
                "put_only_annualized_net_return": 0.08,
                "call_delta": 0.20,
                "net_credit_retention": 0.70,
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
                "structure_mode": "staggered_expiry_pair",
                "funding_accepted": True,
                "put_only_annualized_net_return": 0.20,
                "call_delta": 0.15,
                "net_credit_retention": 0.75,
            },
            {
                "symbol": "AAPL",
                "candidate_pair_id": "pair-c",
                "put_contract_symbol": "AAPL_P180",
                "call_contract_symbol": "AAPL_C220",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-09-18",
                "put_strike": 180,
                "call_strike": 220,
                "bid": 4.25,
                "linked_call_ask": 0.55,
                "cash_required_usd": 18_000,
                "cash_free_usd": 36_000,
                "structure_mode": "staggered_expiry_pair",
                "funding_accepted": True,
                "put_only_annualized_net_return": 0.30,
                "call_delta": 0.10,
                "net_credit_retention": 0.80,
            },
        ]
    ).to_csv(account_dir / "nvda_combo_yield_candidates.csv", index=False)

    brief = _assemble(tmp_path)
    combos = brief["candidates"]["combo_yield"]

    assert [item["strategy_group_id"] for item in combos] == ["pair-c", "pair-a"]
    assert combos[0]["put_leg_role"] == "funding_put"
    assert combos[0]["call_leg_role"] == "participation_call"
    assert combos[0]["put_sell_reference"] == 4.25
    assert combos[0]["call_buy_reference"] == 0.55
    combo_index = {
        item["symbol"]: item["representative"]
        for item in brief["candidate_index"]
        if item["strategy_family"] == "combo_yield"
    }
    assert combo_index["AAPL"]["put_sell_reference"] == 4.25
    assert combo_index["AAPL"]["call_buy_reference"] == 0.55
    combo_actions = [item for item in brief["actions"] if item["strategy_family"] == "combo_yield"]
    assert [item["strategy_group_id"] for item in combo_actions] == ["pair-c", "pair-a"]


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


def test_canonical_prefetch_shape_projects_one_symbol_gap_without_duplicate_aggregate(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(symbol="PDD", contract="PDD_VALID")]).to_csv(
        account_dir / "pdd_sell_put_candidates_labeled.csv",
        index=False,
    )
    state_dir = account_dir / "state"
    (state_dir / "required_data_prefetch_summary.json").write_text(
        json.dumps(
            {
                "errors": 1,
                "symbols": [
                    {"symbol": "NVDA", "status": "error"},
                    {"symbol": "PDD", "status": "ok"},
                ],
                "results": {"NVDA": "empty_chain", "PDD": "ok"},
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)
    matching = [
        item
        for item in brief["data_gaps"]
        if item.get("symbol") == "NVDA"
        and item.get("reason") == "empty_chain"
    ]

    assert len(matching) == 1
    assert not any(
        item.get("reason") == "required_data_prefetch_errors"
        for item in brief["data_gaps"]
    )


def test_status_index_treats_completed_zero_as_available_with_partial_failure(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=["symbol", "contract_symbol"]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    (account_dir / "nvda_sell_call_candidates.csv").write_bytes(b"\n")
    (account_dir / "strategy_scan_status_index.v1.json").write_text(
        json.dumps(
            {
                "schema_version": "strategy_scan_status_index.v1",
                "run_id": "run-1",
                "account": "lx",
                "items": [
                    {
                        "market": "US",
                        "symbol": "NVDA",
                        "strategy_family": "sell_put",
                        "status": "completed",
                        "candidate_count": 0,
                        "source_status_path": "nvda_sell_put_scan_status.json",
                    },
                    {
                        "market": "US",
                        "symbol": "NVDA",
                        "strategy_family": "covered_call",
                        "status": "unavailable",
                        "reason": "empty_chain",
                        "source_status_path": "nvda_covered_call_scan_status.json",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["status"] == "degraded"
    assert not any(
        "candidate_strategy_execution_failed" in str(item.get("reason"))
        for item in brief["actions"]
    )
    assert any(
        item.get("strategy_family") == "covered_call"
        and item.get("reason") == "empty_chain"
        for item in brief["data_gaps"]
    )


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
        "0700_P440",
    }
    assert {item["contract_symbol"] for item in brief["actions"] if item.get("contract_symbol")} == {
        "0700_P440",
    }
    assert "0700_P450_RAW_ONLY" not in json.dumps(brief, sort_keys=True)
    from src.application.daily_decision_brief_renderer import render_full_brief

    assert "0700_P450_RAW_ONLY" not in render_full_brief(brief)
    assert "有效行动 1 条" in brief["strategy_summary"]
    assert "候选证据：Sell Put 1，Covered Call 0，Combo Yield 0" in brief["strategy_summary"]
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
