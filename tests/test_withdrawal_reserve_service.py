from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.application import withdrawal_reserve as service
from src.application.withdrawal_correction import correction
from test_withdrawal_reserve import SOURCE, FLOW

NOW = datetime(2026, 11, 26, 3, tzinfo=timezone.utc)
CFG = {"_resolved": {"market": "us"}, "account_settings": {
    "lx": {"futu": {"account_id": "123", "host": "offline", "port": 11111, "trd_env": "REAL"}}}}


def pm(rows):
    return SimpleNamespace(read_cash_flows=lambda **kwargs: {
        "schema_version": "portfolio.cash_flows.v1", "account": "lx", "source_identity": "pm.cash_flow",
        "complete": True, "records": rows, "row_errors": [],
        "freshness": {"status": "fresh", "trust_status": "trusted", "observed_at_utc": NOW.isoformat()},
    })


class Gateway:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def get_cash_flows(self, **kwargs):
        self.calls.append(kwargs)
        return [row for row in self.rows if row["clearing_date"] == kwargs["clearing_date"]]

    def get_account_balance(self, **kwargs):
        return [{"hk_cash": 90000, "us_cash": 0, "cn_cash": 0, "pending_asset": 0,
                 "currency": "CNH", "fund_assets": 0}]


def observe(path, *, rows=None, gateway=None, **kwargs):
    return service.observe(cfg={}, account="lx", account_id="123", environment="REAL",
                           gateway=gateway or Gateway(), client=pm(rows if rows is not None else [SOURCE]),
                           path=path, now=NOW, **kwargs)


def test_persistence_restart_and_actual_flow_scope(tmp_path):
    path = tmp_path / "reserve.db"
    assert observe(path)["reserved_by_currency"] == {"HKD": 10000}
    gateway = Gateway([FLOW])
    assert observe(path, gateway=gateway)["reserved_by_currency"] == {}
    assert all(call["acc_id"] == "123" and call["trd_env"] == "REAL" for call in gateway.calls)
    assert observe(path)["reserved_by_currency"] == {}


def test_save_failure_new_or_increased_blocks_release_failure_remains_reserved(tmp_path, monkeypatch):
    path = tmp_path / "reserve.db"
    observe(path)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(service, "save_state", fail)
    assert observe(path, rows=[{**SOURCE, "amount": "-20000"}])["status"] == "unavailable"
    assert observe(path, gateway=Gateway([FLOW]))["reserved_by_currency"] == {"HKD": 10000}
    assert observe(tmp_path / "new.db")["status"] == "unavailable"


def test_pm_failure_never_treated_as_no_withdrawals(tmp_path):
    broken = SimpleNamespace(read_cash_flows=lambda **kwargs: {})
    result = service.observe(cfg={}, account="lx", account_id="123", environment="REAL", gateway=Gateway(),
                             client=broken, path=tmp_path / "reserve.db", now=NOW)
    assert result["status"] == "unavailable"
    assert result["reason"] == "withdrawal_pm_unavailable"


def test_manual_preview_confirm_idempotency_and_restore(tmp_path):
    path = tmp_path / "reserve.db"
    observe(path)
    kwargs = dict(cfg=CFG, account="lx", account_id="123", environment="REAL", gateway=Gateway([FLOW]),
                  record_id="pm1", action="settle", flow_ids=["flow1"], reason="bank receipt", actor="operator",
                  path=path, client=pm([SOURCE]), now=NOW)
    preview = correction(**kwargs)
    assert not preview["applied"]
    applied = correction(**kwargs, expected_hash=preview["preview_hash"], operation_id="request1")
    assert applied["applied"]
    assert correction(**kwargs, expected_hash=preview["preview_hash"], operation_id="request1") == applied
    with pytest.raises(ValueError, match="preview changed"):
        correction(**kwargs, expected_hash=preview["preview_hash"], operation_id="request2")


def test_apply_gross_and_net_are_distinct(tmp_path):
    context = {"cash_by_currency": {"HKD": 100000}, "cash_components_by_currency": {"HKD": {}},
               "cash_capacity_by_currency": {"HKD": {"amount": 100000}}}
    service.apply(context, observe(tmp_path / "reserve.db"), [{"currency": "HKD", "pending_asset": 5000}])
    assert context["cash_gross_by_currency"] == {"HKD": 105000}
    assert context["cash_by_currency"] == {"HKD": 95000}


def test_manual_source_change_invalidates_preview(tmp_path):
    path = tmp_path / "reserve.db"
    observe(path)
    kwargs = dict(cfg={}, account="lx", account_id="123", environment="REAL", gateway=Gateway([FLOW]),
                  record_id="pm1", action="cancel", flow_ids=[], reason="cancelled", actor="operator",
                  path=path, client=pm([SOURCE]), now=NOW)
    preview = correction(**kwargs)
    kwargs["client"] = pm([{**SOURCE, "amount": "-12000"}])
    with pytest.raises(ValueError, match="preview changed"):
        correction(**kwargs, expected_hash=preview["preview_hash"], operation_id="cancel1")


def test_real_futu_adapter_reads_scoped_flows_and_never_writes(tmp_path):
    from src.infrastructure.futu_gateway import _FutuAPIClient

    class Trade:
        def __init__(self):
            self.calls, self.forbidden = [], []

        def get_acc_cash_flow(self, **kwargs):
            self.calls.append(kwargs)
            return 0, [FLOW] if kwargs["clearing_date"] == FLOW["clearing_date"] else []

        def place_order(self, **kwargs):
            self.forbidden.append(kwargs)
            raise AssertionError("broker writes forbidden")

    trade = Trade()
    gateway = _FutuAPIClient(SimpleNamespace(_ensure_trade_client=lambda: trade),
                             is_option_chain_cache_enabled=False)
    result = observe(tmp_path / "adapter.db", gateway=gateway)
    assert result["reserved_by_currency"] == {}
    assert trade.calls == [dict(acc_id=123, trd_env="REAL", clearing_date=day)
                           for day in ("2026-11-25", "2026-11-26")]
    assert trade.forbidden == []


def test_cancellation_propagates_and_does_not_save(tmp_path):
    from concurrent.futures import CancelledError

    def cancelled(**kwargs):
        raise CancelledError()
    with pytest.raises(CancelledError):
        observe(tmp_path / "cancel.db", gateway=SimpleNamespace(get_cash_flows=cancelled))


def test_invalid_refreshed_funds_keeps_matched_reservation(tmp_path):
    result = observe(tmp_path / "invalid.db", gateway=Gateway([FLOW]), refresh_funds=lambda: False)
    assert result["reserved_by_currency"] == {"HKD": 10000}
    assert result["statuses_by_currency"] == {"HKD": ["matched"]}


def test_public_correction_preview_and_confirmation_gate(monkeypatch):
    from src.application.agent_tools.withdrawals import TOOLS
    from src.application.agent_tools import permissions

    tool = TOOLS[0]
    payload = dict(account="lx", record_id="pm1", action="restore", reason="wrong match", actor="operator")
    tool.validate_input(payload)
    assert not tool.is_write_requested(payload)
    assert permissions.write_gate_error(tool, payload) is None
    monkeypatch.setattr(permissions, "write_tools_enabled_from_env", lambda: False)
    assert permissions.write_gate_error(tool, {**payload, "confirm": True}).code == "PERMISSION_DENIED"
    monkeypatch.setattr(permissions, "write_tools_enabled_from_env", lambda: True)
    assert permissions.write_gate_error(tool, {**payload, "confirm": True}) is None
    with pytest.raises(Exception):
        tool.validate_input({**payload, "confirm": "false"})


def test_pm_observation_after_request_start_is_accepted(tmp_path):
    def read(**kwargs):
        payload = pm([]).read_cash_flows()
        payload["freshness"]["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
        return payload
    result = service.observe(cfg={}, account="lx", account_id="123", environment="REAL", gateway=Gateway(),
                             client=SimpleNamespace(read_cash_flows=read), path=tmp_path / "clock.db")
    assert result["status"] == "available"


def test_market_value_and_unchanged_scan_do_not_invalidate_manual_preview(tmp_path):
    path = tmp_path / "reserve.db"
    observe(path)
    gateway = Gateway([FLOW])
    kwargs = dict(cfg=CFG, account="lx", account_id="123", environment="REAL", gateway=gateway,
                  record_id="pm1", action="settle", flow_ids=["flow1"], reason="bank receipt", actor="operator",
                  path=path, client=pm([SOURCE]), now=NOW)
    preview = correction(**kwargs)
    observe(path)
    old = gateway.get_account_balance
    gateway.get_account_balance = lambda **args: [{**old(**args)[0], "market_val": 123456}]
    applied = correction(**kwargs, expected_hash=preview["preview_hash"], operation_id="stable")
    assert applied["applied"]


@pytest.mark.parametrize("status,complete,expected", [
    ("pending", True, "出金待确认｜HK$10,000.00 已预扣"),
    ("manual", True, "出金待核对｜HK$10,000.00 继续预扣"),
    ("matched", True, "出金已扣款｜HK$10,000.00 暂保留预扣"),
    ("pending", False, "出金核对失败｜HK$10,000.00 继续预扣"),
])
def test_approved_short_customer_copy(status, complete, expected):
    from src.application.daily_decision_brief_renderer import _fund_views
    reserve = dict(status="available", reserved_by_currency={"HKD": 10000},
                   statuses_by_currency={"HKD": [status]}, flow_complete=complete)
    assert expected in _fund_views({"funds": {"withdrawal_reserve": reserve}})


def test_unavailable_reserve_blocks_all_capacity_consumers():
    from src.application.sell_put_cash import sell_put_cash_context_unavailable_reason
    from src.application.ledger.wheel_capacity import build_shared_cash_capacity_fact
    from src.application.daily_decision_brief_service import _build_funds
    from src.application.futu_portfolio_context import build_futu_portfolio_context
    from src.application.portfolio_context_service import evaluate_account_cash_snapshot

    context = build_futu_portfolio_context(balance_rows=Gateway().get_account_balance(), position_rows=[],
                account="lx", cash_source_observed_at=NOW.isoformat(), futu_account_id="123",
                broker_account_identifiers={"123"}, trd_env="REAL", capacity_market="us")
    context["cash_snapshot"] = evaluate_account_cash_snapshot(context, config=CFG, account="lx", evaluated_at=NOW)
    context["withdrawal_reserve"] = {"status": "unavailable", "reason": "withdrawal_pm_unavailable"}
    assert sell_put_cash_context_unavailable_reason({}, context) == "withdrawal_reserve_unavailable"
    fact = build_shared_cash_capacity_fact(account="lx", portfolio_context=context, option_context={},
                                          wheel_read_model={}, fx_snapshot={})
    assert fact["status"] == "unavailable"
    funds, _ = _build_funds(portfolio_context=context, option_positions_context={}, data_gaps=[])
    assert funds["option_opening_available_cny"] is None
    assert not funds["option_opening_reliable"]


def test_read_only_query_never_writes_reservations(tmp_path):
    path = tmp_path / "readonly.db"
    assert observe(path, persist=False)["status"] == "unavailable"
    assert not path.exists()
    assert observe(path, rows=[], persist=False)["status"] == "available"
    assert not path.exists()
    observe(path)
    before = path.read_bytes()
    assert observe(path, gateway=Gateway([FLOW]), persist=False)["reserved_by_currency"] == {"HKD": 10000}
    assert observe(path, rows=[{**SOURCE, "amount": "-20000"}], persist=False)["status"] == "unavailable"
    assert path.read_bytes() == before
