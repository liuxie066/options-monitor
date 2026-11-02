"""Assignment baselines require complete facts for the exact account and broker."""

from copy import deepcopy
import socket
import urllib.request

import pytest

from cash_evidence_helpers import cash_portfolio
from domain.domain.portfolio_assignment_scenario import project_assignment_scenario
from domain.domain.short_vol_assessment import ShortVolPortfolioContext, portfolio_concentration_fields
from src.application import portfolio_assignment_scenario as app
from src.infrastructure import exchange_rates
from test_broker_assignment_evidence import broker_inputs
from test_portfolio_assignment_scenario import _evidence, _option, _quote


@pytest.fixture
def inputs(broker_inputs, monkeypatch):
    context, config, fx = broker_inputs

    def blocked(*args, **kwargs):
        pytest.fail("external I/O is forbidden in baseline regressions")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(exchange_rates, "_utc_now", lambda: app.datetime.now())
    context["cash_valuation"] = {
        "status": "available", "amount": 20000, "currency": "HKD",
        "source": "longbridge.account_balance.total_cash",
        "source_observed_at": context["cash_source_observed_at"],
        "scope": "physical_account_aggregate", "additive_with_cash_by_currency": False, "errors": [],
    }
    context["exchange_rates"]["rates"]["HKDCNY"] = 0.9
    fx["rates"]["HKDCNY"] = 0.9
    return context, config, fx


def futu_context(inputs, *, account="lx", empty=False):
    context, _, fx = inputs
    return cash_portfolio({
        "source_observed_at": app.datetime.now().isoformat(),
        "cash_by_currency": {"USD": 1000}, "exchange_rates": fx,
        "position_snapshot_input": {
            "rows": [] if empty else deepcopy(context["position_snapshot_input"]["rows"]),
        },
    }, account=account)


def collect(inputs, *, contexts=None, accounts=None):
    context, config, fx = inputs
    return app.collect_assignment_portfolio_evidence(
        accounts=accounts or ["lx"], runtime_config=config,
        futu_contexts=contexts if contexts is not None else {"lx": context},
        option_positions=[], fx_observation=fx, capacity_fx=fx,
    )


def option(kind="call"):
    return _option("unobserved-futu", option_type=kind, symbol="NVDA", strike=8,
                   currency="USD", expiration="2026-11-20")


def assert_missing_baseline(output, *, kind="call"):
    change = next(row for row in output["position_changes"] if row["broker"] == "富途")
    assert change["opening_shares"] is None
    assert change["ending_shares"] is None
    assert change["opening_market_value_cny"] is None
    assert change["ending_market_value_cny"] is None
    assert change[f"{kind}_assigned_shares"] == "100"
    assignment = output["assignments"][0]
    assert assignment["assigned_shares"] == 100
    assert assignment["principal_native"] == "800.000000"
    assert assignment["cash_delta_native"] == ("800.000000" if kind == "call" else "-800.000000")
    sizing = output["position_sizing"]
    assert sizing["net_assets_cny"] is None
    assert sizing["stock_shares_by_symbol"]["NVDA"] is None
    assert sizing["stock_value_cny_by_symbol"]["NVDA"] is None
    assert sizing["weight_of_net_assets_by_symbol"]["NVDA"] is None
    assert "lx/富途:terminal_baseline_missing" in sizing["unavailable_reasons"]
    assert any("lx/富途/NVDA: terminal baseline unavailable" in warning for warning in output["warnings"])
    assert output["distribution"]["net_assets_cny"] is None
    assert output["status"] in {"partial", "unavailable"}


@pytest.mark.parametrize("kind", ["put", "call"])
def test_public_query_does_not_invent_futu_baseline_after_provider_switch(inputs, monkeypatch, tmp_path, kind):
    context, config, _ = inputs
    fx = {"schema_version": 2, "pairs": {
        pair: {"rate": rate, "source": "tencent_quote", "quote_at_utc": "2026-10-30T07:00:00+00:00",
               "observed_at_utc": "2026-10-30T07:00:00+00:00"}
        for pair, rate in [("USDCNY", 7), ("HKDCNY", 0.9)]
    }}
    monkeypatch.setattr(app, "_load_runtime_and_positions", lambda accounts: (
        [option(kind)], str(tmp_path / "config.us.json"), config,
    ))
    monkeypatch.setattr(app, "current_exchange_rate_snapshot", lambda **kwargs: fx)
    reads = []

    def read_context(**kwargs):
        reads.append(kwargs["portfolio_source"])
        assert kwargs["write_cache"] is False
        assert kwargs["account"] == "lx"
        return context

    monkeypatch.setattr(app, "load_account_portfolio_context", read_context)
    evidence = collect(inputs)
    assert evidence["status"] == "complete"
    assert {row["broker"] for row in evidence["holdings"]} == {"长桥"}
    output = app.query_portfolio_assignment_scenario(["lx"], scenario=f"short_{kind}")
    assert reads == ["longbridge"]
    assert_missing_baseline(output, kind=kind)


def test_other_accounts_futu_binding_cannot_prove_this_accounts_baseline(inputs):
    contexts = {"lx": inputs[0], "sy": futu_context(inputs, account="sy")}
    evidence = collect(inputs, contexts=contexts, accounts=["lx", "sy"])
    assert evidence["status"] == "complete"
    output = project_assignment_scenario(accounts=["lx", "sy"], portfolio_evidence=evidence,
                                         option_positions=[option()], snapshot={})
    assert_missing_baseline(output)


@pytest.mark.parametrize("broker", ["富途", "长桥"])
def test_pm_cash_and_quote_do_not_prove_complete_stock_baseline(broker):
    evidence = _evidence(holdings=[{
        "account": "lx", "broker": broker, "code": "USD-CASH", "asset_type": "cash",
        "currency": "USD", "quantity": 1000, "market_value_cny": 7000,
    }], quotes=[_quote("NVDA", currency="USD", price=10, cny_price=70, fx=7)])
    evidence.pop("direct_broker_bindings", None)
    output = project_assignment_scenario(accounts=["lx"], portfolio_evidence=evidence,
        option_positions=[{**option(), "broker": broker}], snapshot={})
    assert output["position_changes"][0]["opening_shares"] is None
    assert output["position_changes"][0]["ending_shares"] is None
    assert output["position_sizing"]["stock_shares_by_symbol"]["NVDA"] is None
    assert output["position_sizing"]["net_assets_cny"] is None


@pytest.mark.parametrize("status", ["complete", "partial", "unavailable"])
def test_pm_non_futu_baseline_preserves_complete_scope_only(status):
    from domain.domain.portfolio_assignment_scenario import project_non_option_assignment_assets

    evidence = _evidence(status=status, holdings=[
        {"account": "lx", "broker": "IB", "code": "USD-CASH", "asset_type": "cash",
         "currency": "USD", "quantity": 1000, "market_value_cny": 7000},
        {"account": "lx", "broker": "IB", "code": "NVDA", "asset_type": "stock",
         "currency": "USD", "quantity": 100, "market_value_cny": 7000},
    ], quotes=[_quote("NVDA", currency="USD", price=10, cny_price=70, fx=7)])
    result = project_non_option_assignment_assets(accounts=["lx"], portfolio_evidence=evidence,
        option_positions=[{**option(), "broker": "IB"}])
    assert result["net_assets_cny"] == ("12600.00" if status == "complete" else None)
    assert result["stock_shares_by_symbol"]["NVDA"] == ("0" if status == "complete" else None)
    assert result["weight_of_net_assets_by_symbol"]["NVDA"] == ("0.000000" if status == "complete" else None)
    if status == "complete":
        assert result["unavailable_reasons"] == []
    else:
        assert "lx/IB:terminal_baseline_missing" in result["unavailable_reasons"]


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("kind", ["put", "call"])
def test_complete_futu_stock_and_cash_facts_still_support_assignment_and_candidate_sizing(inputs, empty, kind):
    evidence = collect(inputs, contexts={"lx": futu_context(inputs, empty=empty)})
    assert evidence["status"] == "complete"
    assert evidence["direct_broker_bindings"]["lx"]["broker_id"] == "futu"
    position = option(kind)
    output = project_assignment_scenario(accounts=["lx"], portfolio_evidence=evidence,
                                         option_positions=[position], snapshot={})
    opening = 0 if empty else 10
    ending = opening + (100 if kind == "put" else -100)
    net = 7000 + opening * 70 + (1400 if kind == "put" else -1400)
    assert output["position_changes"][0]["opening_shares"] == str(opening)
    assert output["position_changes"][0]["ending_shares"] == str(ending)
    assert output["position_sizing"]["net_assets_cny"] == f"{net}.00"
    assert output["position_sizing"]["stock_shares_by_symbol"]["NVDA"] == str(ending)
    assert output["position_sizing"]["unavailable_reasons"] == []
    assert output["status"] == "partial"
    assert output["fee_summary"]["items"][0]["reason"] == "us_assignment_fee_rule_not_explicit"
    assert output["warnings"] == []
    assert "Longbridge" not in output["cash_coverage"]["operational_note"]
    fields = portfolio_concentration_fields(
        {**position, "net_income_cny": 50}, mode=kind,
        risk_ctx=ShortVolPortfolioContext(nav_cny=None, stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={}, short_put_assignment_total_cny=None,
            account="lx", portfolio_evidence=evidence, assignment_positions=()),
    )
    assert fields["portfolio_nav_after_candidate_assignment_cny"] == net + 50
    assert fields["symbol_concentration_after"] == round(ending * 70 / (net + 50), 6)
    assert fields["concentration_unavailable_reason"] is None


@pytest.mark.parametrize("problem", ["missing_cash", "partial_stock", "wrong_account", "stale_stock"])
def test_incomplete_futu_context_never_seals_a_baseline(inputs, problem):
    context = futu_context(inputs)
    if problem == "missing_cash":
        context.pop("cash_snapshot")
    elif problem == "partial_stock":
        context["position_snapshot_input"]["completeness"] = "partial"
    elif problem == "wrong_account":
        context["position_snapshot_input"]["broker_account_ref"]["account_label"] = "sy"
    else:
        context["position_snapshot_input"]["observed_at_utc"] = "2020-01-01T00:00:00Z"
    evidence = collect(inputs, contexts={"lx": context})
    assert evidence["status"] == "unavailable"
    assert not evidence.get("direct_broker_bindings")
    output = project_assignment_scenario(accounts=["lx"], portfolio_evidence=evidence,
                                         option_positions=[option()], snapshot={})
    assert_missing_baseline(output)


@pytest.mark.parametrize("kind", ["put", "call"])
def test_candidate_sizing_preserves_missing_futu_baseline(inputs, kind):
    evidence = collect(inputs)
    position = option(kind)
    fields = portfolio_concentration_fields(
        {**position, "net_income_cny": 50}, mode=kind,
        risk_ctx=ShortVolPortfolioContext(nav_cny=None, stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={}, short_put_assignment_total_cny=None,
            account="lx", portfolio_evidence=evidence, assignment_positions=(position,)),
    )
    assert fields["symbol_concentration_current"] is not None
    assert fields["symbol_concentration_after_existing_assignments"] is None
    assert fields["symbol_concentration_after"] is None
    assert fields["portfolio_nav_after_candidate_assignment_cny"] is None
    assert "lx/富途:terminal_baseline_missing" in fields["concentration_unavailable_reason"]
