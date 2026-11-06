"""Same-batch Tick roll planning, sealing and persisted position advice.

Only provider transport is replaced. Planning, immutable quote publication,
coverage validation, context building and the scheduled evaluator are real.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import MethodType

import pandas as pd
import pytest

from src.application import short_put_roll
from src.application.close_advice_required_data import (
    PLAN_FILE_NAME,
    build_close_advice_required_data_plan,
    close_advice_market_date,
    enrich_close_advice_required_data_plan,
    publish_close_advice_required_data_plan,
)
from src.application.close_advice_runner import run_close_advice
from src.application.ledger.api import build_position_risk_context
from src.application.multi_tick.required_data_prefetch import prefetch_required_data
from src.application.opening_quote_evidence import OPENING_QUOTE_MAX_AGE_SECONDS, normalize_underlier_observation
from src.application.required_data_prefetch_planning import merge_close_advice_requirements_into_prefetch_config
from src.application.required_data_snapshot import seal_required_data_snapshot
from src.application.runtime_paths import runtime_root_scope
from test_close_advice_required_data import _position
from test_position_advice_pipeline import pipeline  # noqa: F401
from test_required_data_prefetch_inprocess import _strict_success_rows_payload
from test_short_put_roll_service import quote


def _stock_context(now, *, shares=100):
    from src.application.futu_portfolio_context import build_futu_portfolio_context, build_futu_position_snapshot

    rows = [] if not shares else [{"code": "US.NVDA", "stock_type": "STOCK", "qty": shares,
        "can_sell_qty": 0, "currency": "USD", "position_side": "long"}]
    ref = {"broker_id": "futu", "external_account_id": "123", "environment": "REAL",
        "account_label": "sample", "broker_account_id": "futu:REAL:123"}
    snapshot = build_futu_position_snapshot(rows=rows, broker_account_ref=ref, markets=["US"],
        asset_types=["stock"], observed_at_utc=now.isoformat(), completeness="complete")
    return build_futu_portfolio_context(balance_rows=[], position_rows=rows, account="sample",
        futu_account_id="123", broker_account_identifiers=["123"], trd_env="REAL", capacity_market="us",
        position_snapshot_input=snapshot)


def _scheduled(tmp_path, monkeypatch, pipeline, *, option_type="put", shares=100, target_bid=6.5,
               premium=2, max_dte=60, stale_target=False, stock_age=0):
    from test_position_advice_service import config

    _, provider, _ = pipeline
    cfg = config()
    cfg["market_data"] = {"provider": provider.name}
    now = datetime.now(timezone.utc)
    old_expiry = provider.payload["expirations"][0]
    new_expiry = (now + timedelta(days=35)).date().isoformat()
    spot = 102 if option_type == "call" else 98
    provider.payload["expirations"] = [old_expiry, new_expiry]
    observation = normalize_underlier_observation(code="US.NVDA", market="US", snapshot_row={
        "code": "US.NVDA", "last_price": spot, "update_time": now.isoformat(),
        "sec_status": "NORMAL", "suspension": False,
    }, market_state_row={"code": "US.NVDA", "market_state": "MORNING"}, now_utc=now)
    provider.payload["meta"]["underlier_observation"] = observation.to_dict()
    cfg["symbols"][0]["sell_call" if option_type == "call" else "sell_put"] = {
        "enabled": False, "max_dte": max_dte,
    }
    record = _position(account="sample", lot_id="lot-1", expiration=old_expiry)
    record["fields"]["contract_key"]["option_type"] = option_type
    record["fields"]["premium_open"] = premium
    records = [record]
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append(True)
        raise AssertionError("scheduled evaluation attempted another broker/ledger fetch")

    monkeypatch.setattr(short_put_roll, "_read_holdings", forbidden)
    monkeypatch.setattr(short_put_roll, "load_account_portfolio_context", forbidden)
    monkeypatch.setattr(short_put_roll, "resolve_market_data_provider", forbidden)

    def fetch(self, request, **kwargs):
        self.calls.append(("fetch", request))
        received = datetime.now(timezone.utc)
        rows = []
        for expiry in request.explicit_expirations:
            for strike in (100, 105 if option_type == "call" else 95):
                row = quote(strike, expiry, bid=4.9 if strike == 100 else target_bid,
                    ask=5 if strike == 100 else target_bid + 0.1,
                    option_type=option_type, spot=spot,
                    contract_symbol=f"US.NVDA{expiry.replace('-', '')[2:]}{'C' if option_type == 'call' else 'P'}{strike * 1000:08d}",
                    dte=(datetime.fromisoformat(expiry).date() - datetime.fromisoformat(request.trading_date).date()).days,
                    spot_observed_at_utc=now.isoformat(),
                    snapshot_requested_at_utc=(received - timedelta(seconds=120 if stale_target and expiry == new_expiry else 0)).isoformat(),
                    snapshot_received_at_utc=(received - timedelta(seconds=120 if stale_target and expiry == new_expiry else 0)).isoformat())
                rows.append(row)
        return _strict_success_rows_payload(request.symbol, rows, trading_date=request.trading_date,
            host=request.host, port=request.port, meta={"source": self.name,
                "underlier_observation": request.underlier_observation})

    provider.fetch_symbol_request = MethodType(fetch, provider)
    run_id = "scheduled-roll"
    run_dir = tmp_path / "output_runs" / run_id
    required_root = run_dir / "required_data"
    required_root.mkdir(parents=True)
    plan_path = run_dir / "state" / PLAN_FILE_NAME
    manifest_path = run_dir / "state" / "required_data_snapshot_manifest.json"
    monkeypatch.setattr("src.application.opend_utils.get_trading_date", lambda _market: close_advice_market_date(now, "US"))
    plan = build_close_advice_required_data_plan(run_id=run_id, run_started_at_utc=now,
        account_configs={"sample": cfg}, base_config=cfg, markets_to_run=["US"],
        position_records_by_account={"sample": records}, include_roll_candidates=True)
    candidate = deepcopy(cfg)
    candidate["symbols"] = []
    prefetch_config, plan = merge_close_advice_requirements_into_prefetch_config(
        candidate_config=candidate, requirements_plan=plan)
    publish_close_advice_required_data_plan(path=plan_path, payload=plan)
    repo_root = Path(__file__).resolve().parents[1]
    with runtime_root_scope(tmp_path):
        summary = prefetch_required_data(vpy=Path(sys.executable), base=tmp_path, repo_root=repo_root,
            cfg=prefetch_config, shared_required=required_root, force_refresh=True,
            producer_run_id=run_id, scan_at_utc=now)
        enrich_close_advice_required_data_plan(plan_path=plan_path, expected_run_id=run_id)
        seal_required_data_snapshot(manifest_path=manifest_path, required_data_root=required_root,
            run_id=run_id, prefetch_summary=summary, close_advice_required_data_plan_path=plan_path)
        context = build_position_risk_context(records, broker="富途", account="sample", observed_at=now)
        output_dir = run_dir / "accounts" / "sample"
        result = run_close_advice(config=cfg, context_path=output_dir / "state" / "option_positions_context.json",
            context_override=context, portfolio_context_override=_stock_context(now - timedelta(seconds=stock_age), shares=shares),
            required_data_root=required_root, output_dir=output_dir, base_dir=repo_root,
            required_data_snapshot_manifest=manifest_path, required_data_snapshot_run_id=run_id,
            close_advice_required_data_plan=plan_path, account="sample", markets_to_run=["US"])
    assert forbidden_calls == []
    assert result["snapshot_authority"] == "valid", result
    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0].to_dict()
    roll = json.loads(row["roll_advice"])
    return result, row, roll, provider, plan


@pytest.mark.parametrize("option_type", ["put", "call"])
def test_tick_uses_same_sealed_batch_for_concrete_roll_target(tmp_path, monkeypatch, pipeline, option_type):
    _, row, roll, provider, plan = _scheduled(tmp_path, monkeypatch, pipeline, option_type=option_type)
    assert roll["status"] == "success", roll
    assert roll["outcome"] == "found", roll
    target = roll["candidates"][0]
    assert target["strike"] == (105 if option_type == "call" else 95)
    assert target["evidence"]["old_ask"] == 5
    assert target["evidence"]["new_bid"] == 6.5
    assert target["net_credit"] > 0
    assert target["per_contract"]["net_credit"] == pytest.approx(target["net_credit"])
    assert target["per_contract"]["incremental_annualized_return"] == pytest.approx(
        target["incremental_annualized_return"])
    assert roll["coverage"]["enumeration_complete"] is True
    assert roll["source"]["expected_fetch_contract_sha256"]
    assert roll["reason_codes"] == []
    assert datetime.fromisoformat(roll["actionable_until_utc"]) > datetime.fromisoformat(roll["evaluated_at_utc"])
    assert (datetime.fromisoformat(roll["actionable_until_utc"]) - datetime.fromisoformat(roll["evaluated_at_utc"])).total_seconds() <= OPENING_QUOTE_MAX_AGE_SECONDS
    assert row["position_lot_id"] == roll["position"]["lot_id"] == "lot-1"
    assert row["required_data_snapshot_manifest_sha256"] == roll["source"]["manifest_sha256"]
    assert plan["accounts"]["sample"]["requirements"][0]["roll_scope"]["max_dte"] == 60
    requests = [value for name, value in provider.calls if name == "fetch"]
    assert len(requests) == 1
    window = requests[0].side_strike_windows[option_type]
    assert window["max_strike" if option_type == "call" else "min_strike"] is None
    assert {target["expiration"], roll["position"]["expiration"]} <= set(requests[0].explicit_expirations)


def test_cc_stock_absence_and_debit_are_not_roll_notifications(tmp_path, monkeypatch, pipeline):
    _, _, roll, _, _ = _scheduled(tmp_path, monkeypatch, pipeline, option_type="call", shares=0)
    assert (roll["status"], roll["outcome"]) == ("success", "not_applicable")
    assert roll["stock_coverage"]["status"] == "insufficient"
    assert roll["candidates"] == []


def test_cc_net_debit_is_excluded_even_with_strike_uplift(tmp_path, monkeypatch, pipeline):
    _, _, roll, _, _ = _scheduled(tmp_path, monkeypatch, pipeline, option_type="call", target_bid=4.5)
    assert (roll["status"], roll["outcome"]) == ("success", "none"), roll
    assert roll["candidates"] == []
    assert {item["code"] for item in roll["exclusion_summary"]} == {"non_positive_net_credit"}


def test_missing_opening_premium_does_not_block_roll_cash_estimate(tmp_path, monkeypatch, pipeline):
    _, row, roll, _, _ = _scheduled(tmp_path, monkeypatch, pipeline, premium=None)
    assert row["recommendation_state"] == "not_evaluable"
    assert roll["outcome"] == "found", roll


def test_symbol_max_dte_bounds_roll_search(tmp_path, monkeypatch, pipeline):
    _, _, roll, provider, _ = _scheduled(tmp_path, monkeypatch, pipeline, max_dte=25)
    assert roll["scope"]["max_new_expiration"] < provider.payload["expirations"][1]
    assert {target["expiration"] for target in roll["candidates"]} == {provider.payload["expirations"][0]}


def test_cc_stale_stock_proof_is_unavailable_not_zero_holdings(tmp_path, monkeypatch, pipeline):
    _, _, roll, _, _ = _scheduled(tmp_path, monkeypatch, pipeline, option_type="call", stock_age=301)
    assert (roll["status"], roll["outcome"]) == ("unavailable", "unable"), roll
    assert roll["stock_coverage"]["status"] == "unavailable"
    assert roll["candidates"] == []


def test_stale_replacement_quote_preserves_partial_coverage(tmp_path, monkeypatch, pipeline):
    _, _, roll, _, _ = _scheduled(tmp_path, monkeypatch, pipeline, stale_target=True)
    assert (roll["status"], roll["outcome"]) == ("partial", "found"), roll
    assert roll["coverage"]["status"] == "partial"
    assert roll["coverage"]["unassessed"] == 1
    assert roll["candidates"]


def test_unplanned_wider_scope_cannot_claim_a_complete_empty_result():
    position = {"option_type": "call", "expiration": "2026-12-01", "strike": 100}
    requirement = {"roll_scope": {"max_new_expiration": "2027-01-01"}}
    plan = {"expiration_discovery_complete": True,
        "expiration_discovery": {"outcome": "success_rows", "expirations": ["2026-12-01", "2026-12-18"]},
        "side_plans": [{"option_type": "call", "source_fields": ["close_advice.position_requirements"],
            "explicit_expirations": ["2026-12-01"], "strike_window": {"min_strike": 100, "max_strike": 100}}]}
    complete, expirations = short_put_roll._frozen_roll_scope_coverage(
        plan=plan, requirement=requirement, position=position)
    assert complete is False
    assert expirations == ["2026-12-01", "2026-12-18"]


@pytest.mark.parametrize("option_type", ["put", "call"])
def test_real_tick_report_flows_unchanged_into_agent_advice(tmp_path, monkeypatch, pipeline, option_type):
    from src.application.agent_tools.daily_brief import read_daily_brief_view
    from src.application.agent_tools.position_advice_read_impl import read_position_advice_view
    from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success
    from src.application.daily_decision_brief_service import _load_close_advice, _position_view
    from src.application.daily_decision_brief_renderer import render_fixed_report
    from test_daily_decision_brief_notification_flow import _brief

    _, _, roll, _, _ = _scheduled(tmp_path, monkeypatch, pipeline, option_type=option_type)
    account_dir = tmp_path / "output_runs" / "scheduled-roll" / "accounts" / "sample"
    source_artifacts, data_gaps = [], []
    rows, loaded = _load_close_advice(path=account_dir / "close_advice.csv", run_account_dir=account_dir,
        market="US", account="sample", run_id="scheduled-roll", source_artifacts=source_artifacts, data_gaps=data_gaps)
    assert loaded is True
    assert data_gaps == []
    assert rows[0]["roll_advice"] == roll
    brief = _brief(base=tmp_path, run_id="scheduled-roll", account="sample", candidate=False)
    now = datetime.now(timezone.utc)
    brief.update(generated_at_utc=now.isoformat(), data_as_of_utc=roll["evaluated_at_utc"],
        market_trading_date=close_advice_market_date(now, "US").isoformat(),
        valid_until_utc=(now + timedelta(minutes=5)).isoformat(),
        positions=[_position_view(rows[0], notification_eligible=False, close_actionable=False)])
    persist_daily_decision_brief_success(base=tmp_path, brief=brief)
    daily = read_daily_brief_view(base=tmp_path, account="sample", market="US", now_utc=now)
    output = read_position_advice_view(daily, action="roll_up" if option_type == "call" else "roll_down", now_utc=now)
    assert output["returned_count"] == output["actionable_count"] == 1
    assert output["rows"][0]["roll_advice"] == roll
    rendered = render_fixed_report(daily["brief"], context={"query_time_utc": now.isoformat()})
    assert "持仓" in rendered
    assert roll["candidates"][0]["contract"] in rendered
    assert "平仓 ask USD 5" in rendered and "开仓 bid USD 6.5" in rendered
