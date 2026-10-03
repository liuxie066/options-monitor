"""Explicit, evaluated Futu cash fixtures shared by consumer regression tests."""
from copy import deepcopy
from datetime import datetime, timezone

from src.application.futu_portfolio_context import build_futu_portfolio_context, build_futu_position_snapshot
from src.application.portfolio_context_service import evaluate_account_cash_snapshot


def cash_config(account="lx", account_id="123"):
    return {"_resolved": {"market": "us"}, "portfolio": {"source": "futu", "base_currency": "CNY"},
            "account_settings": {account: {"futu": {"account_id": account_id, "trd_env": "REAL"}}}}


def cash_portfolio(value, *, account="lx", account_id="123", evaluated_at=None):
    value = deepcopy(value)
    observed = value.get("cash_source_observed_at", value.get("source_observed_at", value.get("as_of_utc")))
    observed = observed or datetime.now(timezone.utc).isoformat()
    config = cash_config(account, account_id)
    result = build_futu_portfolio_context(balance_rows=[{"cn_cash": 0}], position_rows=[],
        account=account, source_observed_at=observed, cash_source_observed_at=observed,
        broker_account_identifiers={account_id}, futu_account_id=account_id, trd_env="REAL", capacity_market="us")
    result["position_snapshot_input"] = build_futu_position_snapshot(
        rows=[], broker_account_ref={"broker_id": "futu", "external_account_id": account_id, "environment": "REAL",
            "account_label": account, "broker_account_id": f"futu:REAL:{account_id}"},
        markets=["US", "HK"], asset_types=["stock", "option"], observed_at_utc=observed, completeness="complete")
    authority = {**result["capacity_authority"], **value.get("capacity_authority", {})}
    if isinstance(value.get("position_snapshot_input"), dict):
        value["position_snapshot_input"] = {**result["position_snapshot_input"], **value["position_snapshot_input"]}
    result.update(value)
    result["capacity_authority"] = authority
    result["cash_snapshot"] = evaluate_account_cash_snapshot(result, config=config, account=account,
        evaluated_at=evaluated_at or datetime.fromisoformat(observed.replace("Z", "+00:00")))
    return result
