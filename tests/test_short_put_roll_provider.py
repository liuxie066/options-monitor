"""Roll service through real OpenD owners, replacing only final quote transport."""
from copy import deepcopy
from datetime import datetime, timedelta
import socket

import pandas as pd
import pytest

from om_market_opend import OpenDMarketDataProvider
from src.application import opend_market_snapshot_fetching as snapshots
from src.application import opening_quote_evidence as quote_evidence
from src.application import short_put_roll as service
import test_short_put_roll_service as fixtures


class OfflineQuotes:
    def __init__(self, rows, *, multiplier="N/A", state="NORMAL", suspension=False,
                 missing_snapshot=False, old_untradable=False, stale=False):
        self.rows = {row["contract_symbol"]: deepcopy(row) for row in rows}
        self.multiplier = multiplier
        self.state = state
        self.suspension = suspension
        self.missing_snapshot = missing_snapshot
        self.old_untradable = old_untradable
        self.stale = stale
        self.calls = []
        self.closed = 0
        self.bundle = None

    def get_option_expiration_dates(self, code):
        self.calls.append(("expirations", code))
        return pd.DataFrame({"strike_time": [fixtures.OLD_EXPIRY, fixtures.NEW_EXPIRY]})

    def get_option_chain(self, *, code, start=None, end=None, **kwargs):
        self.calls.append(("chain", start))
        return pd.DataFrame([{
            "code": row["contract_symbol"], "strike_time": row["expiration"],
            "strike_price": row["strike"], "option_type": "PUT", "lot_size": 100,
            "option_standard_type": "STANDARD", "stock_owner": code, "stock_type": "DRVT",
            "suspension": False,
        } for row in self.rows.values() if row["expiration"] == start])

    def get_snapshot(self, codes):
        self.calls.append(("snapshot", list(codes)))
        output = []
        for code in codes:
            row = self.rows[code]
            target = row["strike"] == (100 if self.old_untradable else 95)
            if self.missing_snapshot and target:
                continue
            output.append({
                "code": code, "bid_price": row["bid"], "ask_price": row["ask"],
                "last_price": row["bid"], "option_contract_size": 100,
                "option_contract_multiplier": self.multiplier, "price_spread": 0.01,
                "suspension": self.suspension if target else False,
                "sec_status": self.state if target else "NORMAL",
                "update_time": "2026-11-04 09:59:59", "option_delta": -0.3,
                "option_implied_volatility": 30.0,
            })
        return pd.DataFrame(output)

    def get_market_state(self, codes):
        self.calls.append(("state", list(codes)))
        return pd.DataFrame([{"code": code, "market_state": "MORNING"} for code in codes])

    def get_quote_rights(self, *, market):
        self.calls.append(("rights", market))
        return {"status": "ok", "market": market, "underlier_right": "LV1", "option_right": "LV1"}

    def close(self):
        self.closed += 1


@pytest.fixture
def real_provider(monkeypatch):
    setup = fixtures.setup_service.__wrapped__(monkeypatch)
    provider, _, _ = setup

    def no_network(*_args, **_kwargs):
        pytest.fail("unexpected network in offline provider regression")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    def install(**kwargs):
        transport = OfflineQuotes(provider.rows, **kwargs)
        real = OpenDMarketDataProvider()
        real.resolve_quote_route = provider.resolve_quote_route
        real.get_underlier_observation = provider.get_underlier_observation
        real.open_quote_gateway = lambda **_: transport
        fetch = real.fetch_symbol_request

        def capture(request):
            bundle = fetch(request)
            if transport.stale:
                for row in bundle["rows"]:
                    if row["strike"] == 95:
                        row["snapshot_received_at_utc"] = (fixtures.NOW - timedelta(seconds=301)).isoformat()
            transport.bundle = bundle
            return bundle

        real.fetch_symbol_request = capture

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixtures.NOW if tz is not None else fixtures.NOW.replace(tzinfo=None)

        monkeypatch.setattr(snapshots, "datetime", FrozenDateTime)
        monkeypatch.setattr(quote_evidence, "datetime", FrozenDateTime)
        monkeypatch.setattr(service, "resolve_market_data_provider", lambda _: real)
        return transport

    return setup, install


@pytest.mark.parametrize("multiplier", [100, "N/A"])
def test_sdk_stock_option_shape_retains_both_legs_and_finds_candidate(tmp_path, real_provider, multiplier):
    setup, install = real_provider
    transport = install(multiplier=multiplier)
    result = fixtures.run(tmp_path, setup)
    assert result["outcome"] == "found", result["reason_codes"]
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["matched"] == 1
    assert len(transport.bundle["rows"]) == 2
    for row in transport.bundle["rows"]:
        assert row["chain_multiplier"] == row["snapshot_multiplier"] == row["multiplier"] == 100
        assert row["opening_contract_status"] == "ready"
    assert transport.closed == 1


@pytest.mark.parametrize("lower", [97, 98, 99])
def test_strict_otm_minimum_reaches_provider_only_for_nonempty_range(tmp_path, real_provider, lower):
    setup, install = real_provider
    transport = install()
    if lower >= 98:
        with pytest.raises(ValueError, match="below the current spot"):
            fixtures.run(tmp_path, setup, {"min_new_strike": lower})
        assert transport.bundle is None
        assert not transport.calls
        assert transport.closed == 0
    else:
        result = fixtures.run(tmp_path, setup, {"min_new_strike": lower})
        assert result["outcome"] == "none"
        assert result["coverage"]["status"] == "complete"
        assert not result["reason_codes"]
        assert transport.closed == 1


@pytest.mark.parametrize("changes, reason", [
    ({"suspension": True}, "option_suspended"),
    ({"state": "DELISTED"}, "option_security_not_normal"),
])
@pytest.mark.parametrize("another_candidate", [False, True])
def test_explicitly_ineligible_new_leg_counts_as_excluded_with_complete_coverage(
    tmp_path, real_provider, changes, reason, another_candidate,
):
    setup, install = real_provider
    if another_candidate:
        setup[0].rows.append(fixtures.quote(94, fixtures.NEW_EXPIRY))
    transport = install(**changes)
    result = fixtures.run(tmp_path, setup)
    new = next(row for row in transport.bundle["rows"] if row["strike"] == 95)
    assert new["opening_contract_status"] == "ineligible"
    assert reason in new["opening_contract_reason_codes"]
    assert result["outcome"] == ("found" if another_candidate else "none"), result
    assert result["status"] == "success"
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["evaluated"] == 1 + int(another_candidate)
    assert result["coverage"]["excluded"] == 1
    assert result["coverage"]["unassessed"] == 0
    assert result["exclusion_summary"] == [{"code": "contract_ineligible", "count": 1}]
    assert transport.closed == 1


@pytest.mark.parametrize("changes", [
    {"state": None}, {"suspension": None}, {"missing_snapshot": True},
    {"suspension": True, "stale": True}, {"suspension": True, "multiplier": 50},
    {"suspension": True, "state": None},
])
def test_missing_or_stale_evidence_never_becomes_complete_exclusion(tmp_path, real_provider, changes):
    setup, install = real_provider
    transport = install(**changes)
    result = fixtures.run(tmp_path, setup)
    assert result["outcome"] == "unable", result
    assert result["coverage"] is None or result["coverage"]["unassessed"] == 1
    assert not result["candidates"]
    assert transport.closed == 1


def test_explicitly_ineligible_old_leg_still_blocks_entire_query(tmp_path, real_provider):
    setup, install = real_provider
    transport = install(suspension=True, old_untradable=True)
    result = fixtures.run(tmp_path, setup)
    assert result["outcome"] == "unable"
    assert result["coverage"] is None
    assert "option_security_not_tradable" in result["reason_codes"]
    assert not result["candidates"]
    assert transport.closed == 1
