from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from domain.domain.assignment_pending import match_assignment_pending
from src.application import futu_assignment_pending as ap


def sample(contracts=1, day="2026-11-21"):
    amount = 9745 * contracts
    balance = {"currency": "USD", "us_cash": -amount, "hk_cash": 0, "cn_cash": 0,
               "pending_asset": amount, "fund_assets": 80000}
    common = {"order_status": "FILLED_ALL", "currency": "USD",
              "create_time": day + " 02:14:30"}
    orders = [
        {**common, "order_id": "put", "code": "US.FUTU261120P97450",
         "trd_side": "BUY_BACK", "qty": contracts, "dealt_qty": contracts,
         "price": 0, "dealt_avg_price": 0},
        {**common, "order_id": "stock", "code": "US.FUTU", "trd_side": "BUY",
         "qty": contracts * 100, "dealt_qty": contracts * 100, "price": 97.45,
         "dealt_avg_price": 97.45},
    ]
    terms = [{"symbol": "FUTU", "expiration_ymd": "2026-11-20",
              "strike": 97.45, "multiplier": 100}]
    return balance, orders, terms


def match(balance, orders, terms):
    return match_assignment_pending(balance=balance, orders=orders, terms=terms,
                                    account_id="123", environment="REAL", today=date(2026, 11, 23))


@pytest.mark.parametrize("contracts", [1, 3])
def test_actual_assignment_shapes_match_without_fund_redemption_receipt(contracts):
    result = match(*sample(contracts))
    assert result["status"] == "matched"
    assert result["adjustment"] == contracts * 9745
    assert result["reason"] == "assignment_pending_inferred"


@pytest.mark.parametrize("change", [
    "amount", "cash", "currency", "time", "quantity", "strike",
    "account", "environment", "missing_terms", "multiplier", "old", "call",
    "duplicate_conflict", "partial_fill",
])
def test_mismatch_never_adds_cash(change):
    balance, orders, terms = sample()
    if change == "amount": balance["pending_asset"] += 1
    if change == "cash": balance["us_cash"] += 1
    if change == "currency": orders[1]["currency"] = "HKD"
    if change == "time": orders[1]["create_time"] = "2026-11-21 03:14:30"
    if change == "quantity": orders[1].update(qty=200, dealt_qty=200)
    if change == "strike": orders[1].update(price=97.46, dealt_avg_price=97.46)
    if change == "account": orders[1]["acc_id"] = "456"
    if change == "environment": orders[1]["trd_env"] = "SIMULATE"
    if change == "missing_terms": terms.clear()
    if change == "multiplier": terms[0]["multiplier"] = 10
    if change == "old":
        for row in orders: row["create_time"] = "2026-11-01 02:14:30"
    if change == "call": orders[0]["code"] = "US.FUTU261120C97450"
    if change == "duplicate_conflict": orders.append({**orders[0], "qty": 99})
    if change == "partial_fill": orders[1]["order_status"] = "FILLED_PART"
    assert match(balance, orders, terms)["adjustment"] == 0


def test_order_duplicates_do_not_double_amount_and_ambiguous_pairs_do_not_match():
    balance, orders, terms = sample()
    assert match(balance, orders + deepcopy(orders), terms)["adjustment"] == 9745
    assert match(balance, orders + [{**orders[0], "order_id": "second-put"}], terms)["adjustment"] == 0


def test_partial_arrival_does_not_match_full_assignment_and_zero_clears():
    balance, orders, terms = sample()
    balance.update(pending_asset=4000, us_cash=-4000)
    assert match(balance, orders, terms)["adjustment"] == 0
    balance.update(pending_asset=0, us_cash=0)
    assert match(balance, orders, terms)["status"] == "none"
    balance["us_cash"] = -9745
    assert match(balance, orders, terms)["reason"] == "negative_cash_without_pending"


def test_replay_and_withdrawal_of_adjustment():
    from src.application.futu_portfolio_context import build_futu_portfolio_context
    balance, orders, terms = sample()
    context = build_futu_portfolio_context(balance_rows=[balance], position_rows=[], account="lx")
    base = context["cash_by_currency"]["USD"]
    result = match(balance, orders, terms)
    ap.apply_assignment_pending(context, result)
    ap.apply_assignment_pending(context, result)
    assert context["cash_by_currency"]["USD"] == base + 9745
    assert context["cash_capacity_by_currency"]["USD"]["amount"] == base + 9745
    ap.apply_assignment_pending(context, {**result, "status": "none", "adjustment": 0})
    assert context["cash_by_currency"]["USD"] == base


def test_live_facade_fetches_new_whole_balance_and_reports_inference(monkeypatch):
    from src.application import futu_portfolio_context as fc
    from src.application.daily_decision_brief_renderer import _fund_views
    balance, orders, terms = sample(day=date.today().isoformat())
    monkeypatch.setattr(ap, "_account_terms", lambda *_: terms)
    class Gateway:
        def __init__(self): self.calls = []; self.closed = False
        def get_account_balance(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["currency"] == "CNH":
                return [{**balance, "currency": "CNH", "fund_assets": 560000, "pending_asset": 68215}]
            return [balance]
        def get_history_orders(self, **kwargs):
            assert kwargs["acc_id"] == 123 and kwargs["order_market"] == "US"
            return {"retcode": 0, "coverage_complete": True, "pagination_complete": True, "rows": orders}
        def get_positions(self, **kwargs): return []
        def close(self): self.closed = True
    gateway = Gateway()
    monkeypatch.setattr(fc, "build_ready_futu_broker_gateway", lambda **_: gateway)
    # An old shared-file opt-in must neither gate nor double this new default policy.
    monkeypatch.setenv("FUTU_MMF_FUNDING_DIR", "/nonexistent/old-mmf-file")
    context = fc.fetch_futu_portfolio_context(
        cfg={"portfolio": {"futu": {"host": "test", "port": 1, "trd_env": "REAL"}},
             "account_settings": {"lx": {"futu": {"account_id": "123"}}}},
        account="lx", exchange_rate_observation=None,
    )
    assert gateway.closed
    assert [c["currency"] for c in gateway.calls] == ["CNH", "USD"]
    assert context["cash_by_currency"]["USD"] == 80000
    assert context["cash_by_currency"]["CNY"] == 0
    assert "已补入开仓资金" in "".join(_fund_views({"funds": {"assignment_pending": context["assignment_pending"]}}))


def test_incomplete_history_warns_without_suppressing_original_cash(monkeypatch):
    from src.application.daily_decision_brief_renderer import _fund_views
    balance, _, _ = sample()
    class Gateway:
        def get_history_orders(self, **kwargs):
            return {"retcode": 0, "coverage_complete": False, "rows": []}
    rows, evidence = ap.observe_assignment_pending(
        Gateway(), [balance], query_rows=lambda *_a, **_k: pytest.fail("no refresh after incomplete history"),
        cfg={}, account="lx", account_id="123", environment="REAL",
    )
    assert rows == [balance] and evidence["adjustment"] == 0
    assert "未纳入开仓额度" in "".join(_fund_views({"funds": {"assignment_pending": evidence}}))


def test_history_timeout_is_not_disguised_as_mismatch():
    balance, _, _ = sample()
    class Gateway:
        def get_history_orders(self, **kwargs): raise TimeoutError()
    with pytest.raises(TimeoutError):
        ap.observe_assignment_pending(Gateway(), [balance], query_rows=None, cfg={}, account="lx",
                                      account_id="123", environment="REAL")


def test_brief_service_preserves_pending_warning_without_invalidating_cash(monkeypatch):
    from src.application import daily_decision_brief_service as brief
    from src.application.daily_decision_brief_renderer import _fund_views
    from tests.cash_evidence_helpers import cash_portfolio
    portfolio = cash_portfolio({"cash_by_currency": {"USD": -9745}})
    portfolio["assignment_pending"] = {"status": "unmatched", "currency": "USD",
                                       "pending_amount": 9745, "adjustment": 0}
    monkeypatch.setattr(brief, "cash_snapshot_is_usable", lambda _: True)
    funds, reliable = brief._build_funds(portfolio_context=portfolio, option_positions_context={}, data_gaps=[])
    assert reliable
    assert funds["cash_total_by_currency"] == portfolio["cash_by_currency"]
    assert "未纳入开仓额度" in "".join(_fund_views({"funds": funds}))


def test_confirmed_shared_funding_is_preserved_but_stale_file_does_not_gate_new_policy(tmp_path, monkeypatch):
    import json
    from src.application.futu_portfolio_context import verified_shared_funding
    from hashlib import sha256
    balance, _, _ = sample()
    balance.update(currency="CNH", fund_assets=560000)
    observed = datetime.now(timezone.utc)
    payload = {"schema": "futu.mmf-funding.v1", "account": "lx", "status": "verified",
               "environment": "REAL", "account_fingerprint": "sha256:" + sha256(b"123").hexdigest(),
               "observed_at": observed.isoformat(), "snapshot_id": "fixture",
               "cash": {"CNY": 0, "HKD": 0, "USD": -9745}, "fund_assets_cny": "560000",
               "adjustments": {"USD": "9745"},
               "transfers": [{"receipt_id": "fixture-1", "direction": "redemption", "currency": "USD",
                              "amount": "9745", "source_debit": "9745", "target_credit": "0"}]}
    monkeypatch.setenv("FUTU_MMF_FUNDING_DIR", str(tmp_path))
    target = tmp_path / "lx.json"
    target.write_text(json.dumps(payload))
    kwargs = dict(account="lx", account_id="123", environment="REAL", observed_at=observed.isoformat())
    assert verified_shared_funding([balance], **kwargs) == payload
    payload["observed_at"] = (observed - timedelta(hours=1)).isoformat()
    target.write_text(json.dumps(payload))
    assert verified_shared_funding([balance], **kwargs) is None


def test_zero_pending_with_negative_cash_warns_without_history_queries():
    balance, _, _ = sample()
    balance["pending_asset"] = 0
    _, evidence = ap.observe_assignment_pending(object(), [balance], query_rows=None, cfg={},
                                               account="lx", account_id="123", environment="REAL")
    assert evidence["status"] == "unmatched"
    assert evidence["adjustment"] == 0


@pytest.mark.parametrize("status,text", [("unmatched", "未纳入开仓额度"), ("matched", "已补入开仓资金"), ("verified_mmf", "现有核实凭证")])
def test_pending_survives_canonical_brief_normalization(status, text):
    from domain.domain.daily_decision_brief import normalize_daily_decision_brief
    from src.application.daily_decision_brief_renderer import _fund_views
    brief = normalize_daily_decision_brief({"schema_version": "daily_decision_brief.v1",
        "account": "lx", "market": "US", "market_trading_date": "2026-11-23", "revision": 1,
        "funds": {"assignment_pending": {
        "status": status, "currency": "USD", "pending_amount": 9745, "adjustment": 9745,
    }}})
    assert text in "".join(_fund_views(brief))


def test_pending_ledger_query_facade_preserves_canonical_functions():
    from src.application.ledger import api, query_api
    assert query_api.list_canonical_position_lot_snapshots is api.list_canonical_position_lot_snapshots
    assert query_api.open_position_query_ledger_from_data_config is api.open_position_query_ledger_from_data_config
