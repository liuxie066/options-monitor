"""CC query through the actual OpenD and broker portfolio adapters; transport mocked."""
from copy import deepcopy

import pandas as pd
import pytest

from om_market_opend import OpenDMarketDataProvider
from src.application import futu_portfolio_context as broker_adapter
from src.application import opening_quote_evidence as quote_evidence
from src.application import opend_market_snapshot_fetching as snapshots
from src.application import short_put_roll as service
from test_covered_call_roll_service import FrozenDatetime, covered_service, run  # noqa: F401
from test_short_put_roll_service import NOW, setup_service  # noqa: F401
from test_short_put_roll_provider import OfflineQuotes
from tests.cash_evidence_helpers import cash_config


class CallQuotes(OfflineQuotes):
    def get_option_chain(self, **kwargs):
        rows = super().get_option_chain(**kwargs)
        rows["option_type"] = "CALL"
        return rows


class BrokerTransport:
    def __init__(self, shares):
        self.shares = shares
        self.calls = []
        self.forbidden_calls = 0
        self.closed = 0

    def get_account_balance(self, **kwargs):
        self.calls.append(("balance", kwargs))
        return pd.DataFrame([{"acc_id": "123", "trd_env": "REAL", "cn_cash": 1000}])

    def get_positions(self, **kwargs):
        self.calls.append(("positions", kwargs))
        if not self.shares:
            return pd.DataFrame()
        return pd.DataFrame([{"acc_id": "123", "trd_env": "REAL", "code": "US.NVDA", "stock_type": "STOCK",
            "qty": self.shares, "can_sell_qty": 0, "currency": "USD", "position_side": "long"}])

    def place_order(self, *_args, **_kwargs):
        self.forbidden_calls += 1
        raise RuntimeError("query must not place orders")

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("shares", [0, 100])
def test_real_adapters_prove_stock_before_call_chain_without_business_writes(
    tmp_path, covered_service, monkeypatch, shares,
):
    # Restore the production loader hidden by the unit-service fixture.
    from src.application.portfolio_context_service import load_account_portfolio_context

    provider, _records, cfg, _context, _calls = covered_service
    cfg.update(cash_config(account="sample"))
    cfg["portfolio"].update(broker="富途", account="sample")
    cfg["portfolio"]["futu"] = {"host": "offline.invalid", "port": 11111}
    observed_portfolio, logs = [], []
    def load(**kwargs):
        kwargs["log"] = logs.append
        value = load_account_portfolio_context(**kwargs)
        observed_portfolio.append(value)
        return value
    monkeypatch.setattr(service, "load_account_portfolio_context", load)
    monkeypatch.setattr(broker_adapter, "datetime", FrozenDatetime)
    broker = BrokerTransport(shares)
    monkeypatch.setattr(broker_adapter, "build_ready_futu_broker_gateway", lambda **_: broker)
    quotes = CallQuotes(deepcopy(provider.rows))
    real = OpenDMarketDataProvider()
    monkeypatch.setattr(real, "get_underlier_observation", provider.get_underlier_observation)
    monkeypatch.setattr(real, "resolve_quote_route", provider.resolve_quote_route)
    monkeypatch.setattr(real, "open_quote_gateway", lambda **_: quotes)
    monkeypatch.setattr(service, "resolve_market_data_provider", lambda _: real)
    monkeypatch.setattr(snapshots, "rate_limited_opend_call", lambda **kwargs: kwargs["call"]())
    monkeypatch.setattr(snapshots, "datetime", FrozenDatetime)
    monkeypatch.setattr(quote_evidence, "datetime", FrozenDatetime)
    # forbidden calls are counted before raising, even if a caller catches.
    forbidden = []
    def no_write(*_args, **_kwargs):
        forbidden.append("write")
        raise RuntimeError("unexpected account cache or notification write")
    monkeypatch.setattr("src.application.portfolio_context_service.atomic_write_json", no_write)
    monkeypatch.setattr("src.application.ledger.api.open_position_ledger", no_write)
    result = run(tmp_path, covered_service)
    assert result["outcome"] == ("found" if shares else "not_applicable"), (result["reason_codes"], broker.calls, logs)
    if shares:
        assert result["candidates"][0]["recommendation"] == "credit_roll"
        assert result["candidates"][0]["net_credit"] > 0
        assert result["candidates"][0]["net_debit"] == 0
        chain_calls = [value for name, value in quotes.calls if name == "chain"]
        assert chain_calls
    else:
        assert quotes.calls == []
        assert result["coverage"] is None
    assert broker.forbidden_calls == 0 and forbidden == []
    assert broker.closed == 1
    assert [name for name, _ in broker.calls] == ["balance", "positions"]
    assert all(call["refresh_cache"] is True for _, call in broker.calls)
    assert not (tmp_path / "output_accounts").exists()
