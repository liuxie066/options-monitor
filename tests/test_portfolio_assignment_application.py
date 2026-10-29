from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from src.application import portfolio_assignment_scenario as application
from cash_evidence_helpers import cash_portfolio, cash_config


@pytest.fixture(autouse=True)
def cash_runtime(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 24, 1, 0, 0, tzinfo=timezone.utc)
    from src.application import portfolio_context_service
    monkeypatch.setattr(portfolio_context_service, "datetime", Clock)
    monkeypatch.setattr(application, "datetime", Clock)
    monkeypatch.setattr(application, "repo_base", lambda: tmp_path)



class _Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {"X-PM-API-Version": "portfolio.api.v1"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def _valuation_response(*, accounts=None):
    resolved_accounts = list(accounts or ["lx"])
    return {
        "schema_version": "portfolio.valuation_evidence.v1",
        "success": True,
        "status": "complete",
        "freshness": {
            "status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": "2026-07-24T01:00:00Z",
            "dataset_ids": [
                "pm.holdings_quantity",
                "pm.prices",
                "pm.fx",
            ],
            "reason_codes": [],
        },
        "retrieved_at_utc": "2026-07-24T01:00:01Z",
        "scope": {
            "accounts": resolved_accounts,
            "reporting_currency": "CNY",
        },
        "snapshot": {
            "snapshot_id": "valuation-1",
            "observed_at": "2026-07-24T01:00:00Z",
        },
        "holdings": [],
        "quotes": [],
        "account_status": [{"account": account, "status": "complete"} for account in resolved_accounts],
        "warnings": [],
    }


def _patch_positions(monkeypatch, positions, *, holdings_enabled=False, approved=None, futu_context=None, futu_quotes=None):
    observation = {
        "source": "tencent_quote",
        "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setattr(
        application,
        "current_exchange_rate_snapshot",
        lambda **_kwargs: observation,
    )
    monkeypatch.setattr(application, "project_exchange_rate_snapshot", lambda *_args, **_kwargs: observation)
    monkeypatch.setattr(
        application,
        "_load_runtime_and_positions",
        lambda accounts: (
            positions,
            "config.us.json",
            {**cash_config(), "portfolio_management": {"enabled": True}, "portfolio": {"holdings": {"enabled": holdings_enabled, **({"approved_non_futu_brokers": {"lx": approved}} if approved is not None else {})}}},
        ),
    )
    monkeypatch.setattr(
        application,
        "fetch_futu_portfolio_context",
        lambda *, cfg, account, exchange_rate_observation, **_kwargs: (
            futu_context
            or cash_portfolio({
                "source_observed_at": "2026-07-24T01:00:00Z",
                "cash_by_currency": {"CNY": 0},
                "cash_balance_reliable": True,
                "stocks_by_symbol": {},
                "position_snapshot_input": {"rows": [], "errors": []},
                "exchange_rates": None,
                "exchange_rate_status": "unavailable",
            })
        ),
    )
    monkeypatch.setattr(application, "_read_futu_quotes", lambda *_args, **_kwargs: (futu_quotes or [], []))


def _futu_quote(code, price_native, *, rate=7.2):
    return {
        "code": code,
        "currency": "USD",
        "price_native": price_native,
        "price_cny": price_native * rate,
        "exchange_rate_to_cny": rate,
        "source": "futu_opend_market_snapshot",
        "observed_at": "2026-07-24T01:00:00Z",
    }


def test_normalize_assignment_accounts_trims_lowercases_and_deduplicates():
    assert application.normalize_assignment_accounts([" LX ", "sy", "lx"]) == ["lx", "sy"]

    with pytest.raises(application.AssignmentScenarioInputError, match="at least one"):
        application.normalize_assignment_accounts([])
    with pytest.raises(application.AssignmentScenarioInputError, match="invalid account"):
        application.normalize_assignment_accounts(["bad account"])


def test_valuation_evidence_client_posts_to_fixed_loopback_endpoint(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response(_valuation_response(accounts=["lx", "sy"]))

    monkeypatch.delenv(application.SERVICE_URL_ENV, raising=False)
    monkeypatch.setattr(application.urllib.request, "urlopen", fake_urlopen)

    result = application.read_portfolio_valuation_evidence(
        accounts=["lx", "sy"],
        supplemental_codes=["NVDA"],
        price_timeout=7,
        runtime_config={"portfolio_management": {"enabled": True}},
    )

    request = seen["request"]
    assert urlsplit(request.full_url).scheme == "http"
    assert urlsplit(request.full_url).netloc == "127.0.0.1:8765"
    assert urlsplit(request.full_url).path == "/api/v1/analysis/valuation-evidence"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "accounts": ["lx", "sy"],
        "supplemental_codes": ["NVDA"],
        "price_timeout": 7,
    }
    assert seen["timeout"] == 17
    assert result["status"] == "complete"


def test_valuation_evidence_client_rejects_non_loopback_url(monkeypatch):
    monkeypatch.setenv(
        application.SERVICE_URL_ENV,
        "https://portfolio.example.com",
    )

    with pytest.raises(application.PortfolioEvidenceReadError, match="loopback"):
        application.read_portfolio_valuation_evidence(
            accounts=["lx"],
            supplemental_codes=[],
            runtime_config={"portfolio_management": {"enabled": True}},
        )


def test_valuation_evidence_disabled_never_opens_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(
        application.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: calls.append(True),
    )

    with pytest.raises(application.PortfolioEvidenceReadError) as raised:
        application.read_portfolio_valuation_evidence(
            accounts=["lx"],
            supplemental_codes=[],
            runtime_config={"portfolio_management": {"enabled": False}},
        )

    assert raised.value.code == "PORTFOLIO_MANAGEMENT_DISABLED"
    assert calls == []


def test_query_assignment_scenario_reads_only_open_short_underlyings(monkeypatch):
    positions = [
        {
            "record_id": "p1",
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "status": "open",
            "contracts_open": 1,
            "multiplier": 100,
            "strike": 100,
            "currency": "USD",
            "expiration_ymd": "2026-08-28",
        }
    ]
    seen = {}
    _patch_positions(monkeypatch, positions)

    def read_quotes(codes, **_kwargs):
        seen["codes"] = list(codes)
        return [_futu_quote("NVDA", 120)], []

    monkeypatch.setattr(
        application,
        "_read_futu_quotes",
        read_quotes,
    )
    monkeypatch.setattr(
        application,
        "read_portfolio_valuation_evidence",
        lambda **_kwargs: pytest.fail("PM must not be read when Holdings is off"),
    )

    result = application.query_portfolio_assignment_scenario([" LX ", "lx"])

    assert seen == {"codes": ["NVDA"]}
    assert result["scope"]["accounts"] == ["lx"]
    assert result["scope"]["include_long_options"] is False
    assert result["summary"]["assignment_count"] == 1
    assert result["snapshot"]["portfolio_snapshot_id"].startswith("futu-")
    assert result["snapshot"]["quote_observed_at_by_code"] == {"NVDA": "2026-07-24T01:00:00Z"}
    assert result["snapshot"]["runtime_config"] == "config.us.json"


@pytest.mark.parametrize("scenario,expected_codes,expected_types", [
    ("short_put", ["NVDA"], ["put"]),
    ("short_call", ["AAPL"], ["call"]),
    ("all_short", ["AAPL", "NVDA"], ["put", "call"]),
])
def test_directional_query_collects_only_selected_option_quotes(monkeypatch, scenario, expected_codes, expected_types):
    from test_assignment_position_sizing import option

    _patch_positions(monkeypatch, [
        {**option("put"), "currency": "USD"},
        {**option("call"), "symbol": "AAPL", "currency": "USD"},
    ])
    seen = []

    def read_quotes(codes, **kwargs):
        seen.append(list(codes))
        return [_futu_quote(code, 10) for code in codes], []

    monkeypatch.setattr(application, "_read_futu_quotes", read_quotes)
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", lambda **kwargs: pytest.fail("PM disabled"))
    out = application.query_portfolio_assignment_scenario(["lx"], scenario=scenario)
    assert seen == [expected_codes]
    assert [row["option_type"] for row in out["assignments"]] == expected_types
    assert set(out["snapshot"]["quote_observed_at_by_code"]) == set(expected_codes)
    assert out["snapshot"]["scenario"] == scenario


def test_directional_query_keeps_stock_valuation_for_unassigned_call(monkeypatch):
    from test_assignment_position_sizing import option

    context = cash_portfolio({
        "source_observed_at": "2026-07-24T01:00:00Z",
        "cash_by_currency": {"CNY": 10000},
        "cash_balance_reliable": True,
        "position_snapshot_input": {"rows": [{
            "instrument_ref": {"asset_type": "stock", "market": "US", "symbol": "AAPL", "currency": "USD"},
            "position_side": "long", "quantity": "100",
        }], "errors": []},
    })
    _patch_positions(monkeypatch, [
        {**option("put"), "currency": "USD"},
        {**option("call"), "symbol": "AAPL", "currency": "USD"},
    ], futu_context=context)

    def read_quotes(codes, **kwargs):
        assert codes == ["AAPL", "NVDA"]
        return [_futu_quote(code, 10) for code in codes], []

    monkeypatch.setattr(application, "_read_futu_quotes", read_quotes)
    out = application.query_portfolio_assignment_scenario(["lx"], scenario="short_put")
    assert [row["symbol"] for row in out["assignments"]] == ["NVDA"]
    assert out["position_sizing"]["stock_shares_by_symbol"] == {"AAPL": "100", "NVDA": "100"}


@pytest.mark.parametrize("scenario", ["put", "", None, ["short_put"]])
def test_invalid_assignment_scenario_fails_before_reads(monkeypatch, scenario):
    monkeypatch.setattr(application, "_load_runtime_and_positions", lambda *args: pytest.fail("unexpected ledger read"))
    with pytest.raises(application.AssignmentScenarioInputError, match="scenario must be"):
        application.query_portfolio_assignment_scenario(["lx"], scenario=scenario)


def test_snapshot_identifies_empty_direction_and_ledger_failure_keeps_assumption(monkeypatch):
    _patch_positions(monkeypatch, [])
    put = application.query_portfolio_assignment_scenario(["lx"], scenario="short_put")
    call = application.query_portfolio_assignment_scenario(["lx"], scenario="short_call")
    assert put["snapshot"]["snapshot_id"] != call["snapshot"]["snapshot_id"]
    monkeypatch.setattr(application, "_load_runtime_and_positions", lambda *args: (_ for _ in ()).throw(RuntimeError("ledger down")))
    failed = application.query_portfolio_assignment_scenario(["lx"], scenario="short_call")
    assert failed["status"] == "unavailable"
    assert failed["scope"]["scenario"] == "all_open_short_call_assigned"
    assert failed["snapshot"]["scenario"] == "short_call"


def test_query_uses_one_fx_observation_for_all_requested_futu_accounts(monkeypatch):
    observation = {
        "source": "tencent_quote",
        "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    reads = []
    monkeypatch.setattr(application, "_load_runtime_and_positions", lambda _accounts: (
        [], "config.us.json", {**cash_config(), "account_settings": {**cash_config()["account_settings"], **cash_config("sy")["account_settings"]}, "portfolio": {"holdings": {"enabled": False}}},
    ))
    monkeypatch.setattr(application, "current_exchange_rate_snapshot", lambda **_kwargs: reads.append("fx") or observation)
    monkeypatch.setattr(application, "project_exchange_rate_snapshot", lambda *_args, **_kwargs: observation)

    def read_context(*, cfg, account, exchange_rate_observation, **_kwargs):
        assert exchange_rate_observation is observation
        reads.append(account)
        return cash_portfolio({
            "source_observed_at": "2026-07-24T01:00:00Z",
            "cash_by_currency": {"CNY": 1},
            "cash_balance_reliable": True,
            "position_snapshot_input": {"rows": [], "errors": []},
            "exchange_rates": observation,
            "exchange_rate_status": "ready",
            "filters": {"account": account},
        }, account=account)

    monkeypatch.setattr(application, "fetch_futu_portfolio_context", read_context)
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", lambda **_kwargs: pytest.fail("PM read while off"))

    result = application.query_portfolio_assignment_scenario(["lx", "sy"])

    assert reads == ["fx", "lx", "sy"]
    assert result["snapshot"]["fx_observation"]["source"] == "tencent_quote"
    assert result["cash_coverage"]["available_cash_and_mmf_cny"] == "2.00"


@pytest.mark.parametrize("existing_cache", [False, True])
def test_query_keeps_shared_fx_cache_read_only(monkeypatch, tmp_path, existing_cache):
    from src.infrastructure import exchange_rates as fx

    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    from src.application import portfolio_context_service
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(portfolio_context_service, "datetime", Clock)
    monkeypatch.setattr(application, "datetime", SimpleNamespace(now=lambda _tz: now))
    monkeypatch.setattr(fx, "_utc_now", lambda: now)
    monkeypatch.setattr(application, "repo_base", lambda: tmp_path)
    cache = tmp_path / "output_shared" / "state" / "rate_cache.json"

    def pair(rate, quoted):
        return {
            "rate": rate, "source": "tencent_quote",
            "quote_at_utc": quoted, "observed_at_utc": quoted,
        }

    if existing_cache:
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"schema_version": 2, "pairs": {
            "USDCNY": pair(7.1, "2026-09-30T10:00:00+00:00"),
            "HKDCNY": pair(0.91, "2026-09-30T10:00:00+00:00"),
        }}), encoding="utf-8")
    original = cache.read_bytes() if existing_cache else None
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: {"pairs": {
        "USDCNY": pair(7.2, "2026-09-30T11:00:00+00:00"),
        "HKDCNY": pair(0.92, "2026-09-30T11:00:00+00:00"),
    }})
    monkeypatch.setattr(application, "_load_runtime_and_positions", lambda _accounts: (
        [], "config.us.json", {**cash_config(), "account_settings": {**cash_config()["account_settings"], **cash_config("sy")["account_settings"]}, "portfolio": {"holdings": {"enabled": False}}},
    ))
    monkeypatch.setattr(application, "fetch_futu_portfolio_context", lambda **_kwargs: cash_portfolio({
        "source_observed_at": now.isoformat(),
        "cash_by_currency": {"CNY": 1},
        "cash_balance_reliable": True,
        "position_snapshot_input": {"rows": [], "errors": []},
    }))

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["snapshot"]["fx_observation"]["pairs"]["USDCNY"]["rate"] == 7.2
    assert (cache.read_bytes() if cache.exists() else None) == original
    assert not cache.with_suffix(".json.lock").exists()


def test_query_holiday_fx_supports_cash_valuation_and_coverage(monkeypatch):
    from src.infrastructure import exchange_rates as fx

    monkeypatch.setattr(fx, "_utc_now", lambda: datetime(2026, 10, 2, 1, 43, tzinfo=timezone.utc))
    snapshot = {"schema_version": 2, "pairs": {
        "HKDCNY": {
            "rate": 0.92, "source": "tencent_quote",
            "quote_at_utc": "2026-09-30T07:00:00+00:00",
            "observed_at_utc": "2026-09-30T07:01:00+00:00",
        },
    }}
    monkeypatch.setattr(application, "_load_runtime_and_positions", lambda _accounts: (
        [], "config.hk.json", {**cash_config(), "account_settings": {**cash_config()["account_settings"], **cash_config("sy")["account_settings"]}, "portfolio": {"holdings": {"enabled": False}}},
    ))
    monkeypatch.setattr(application, "current_exchange_rate_snapshot", lambda **_kwargs: snapshot)

    def read_context(*, cfg, account, exchange_rate_observation, **_kwargs):
        return cash_portfolio({
            "source_observed_at": "2026-07-24T01:00:00Z",
            "cash_by_currency": {"HKD": 100},
            "cash_balance_reliable": True,
            "position_snapshot_input": {"rows": [], "errors": []},
            "exchange_rates": exchange_rate_observation,
            "exchange_rate_status": "unavailable_stale",
            "filters": {"account": account},
        }, account=account)

    monkeypatch.setattr(application, "fetch_futu_portfolio_context", read_context)
    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["cash_coverage"]["available_cash_and_mmf_cny"] == "92.00"
    assert result["distribution"]["by_code"][0]["value_cny"] == "92.00"
    assert result["fx_facts"][0]["quality"] == "holiday_carried"
    assert result["snapshot"]["fx_observation"]["pairs"]["HKDCNY"]["quote_at_utc"] == "2026-09-30T07:00:00+00:00"
    assert result["snapshot"]["fx_observation"]["snapshot_sha256"]
    assert "资金能力汇率：不可用" not in application.render_assignment_scenario_text(result)


def test_futu_quote_adapter_uses_opend_snapshot_and_scenario_fx(monkeypatch):
    calls = []
    monkeypatch.setattr(application, "exchange_rate_observation_status", lambda *_args, **_kwargs: "ready")
    gateway = SimpleNamespace(close=lambda: calls.append("closed"))
    monkeypatch.setattr(
        application,
        "resolve_futu_quote_route",
        lambda _cfg: SimpleNamespace(ok=True, status="ok", host="127.0.0.1", port=11111),
    )
    monkeypatch.setattr(application, "build_ready_futu_quote_gateway", lambda **_kwargs: gateway)

    def fetch_observations(used_gateway, codes, **kwargs):
        assert used_gateway is gateway
        assert codes == ["US.NVDA", "US.TSLA", "US.AAPL", "US.MSFT"]
        assert kwargs["market"] == "US"
        return {
            "US.NVDA": SimpleNamespace(
                code="US.NVDA", market="US", sec_status="NORMAL", suspension=False,
                status="ready",
                reason_code=None,
                last_price=120,
                observed_at_utc="2026-07-24T01:00:00Z",
                age_seconds=10,
            ),
            "US.TSLA": SimpleNamespace(
                code="US.TSLA", market="US", sec_status="NORMAL", suspension=False,
                status="market_closed",
                reason_code="market_closed",
                last_price=50,
                observed_at_utc="2026-07-23T20:00:00Z",
                age_seconds=18000,
            ),
            "US.AAPL": SimpleNamespace(
                code="US.AAPL", market="US", sec_status="HALT", suspension=False,
                status="market_closed", reason_code="market_closed", last_price=200,
                observed_at_utc="2026-07-23T20:00:00Z", age_seconds=18000,
            ),
            "US.MSFT": SimpleNamespace(
                code="US.MSFT", market="US", sec_status="NORMAL", suspension=False,
                status="market_closed", reason_code="market_closed", last_price=300,
                observed_at_utc="2026-07-14T20:00:00Z", age_seconds=10 * 86400,
            ),
        }

    monkeypatch.setattr(application, "get_underlier_observations_opend", fetch_observations)
    quotes, warnings = application._read_futu_quotes(
        ["NVDA", "TSLA", "AAPL", "MSFT"],
        runtime_config={},
        contexts={"lx": {}},
        fx_observation={
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    assert calls == ["closed"]
    assert [(item["code"], item["price_native"], item["price_cny"]) for item in quotes] == [
        ("NVDA", "120", "864.0"),
        ("TSLA", "50", "360.0"),
    ]
    assert all(item["source"] == "futu_opend_market_snapshot" for item in quotes)
    assert quotes[1]["is_stale"] is True
    assert any("quote is dated" in warning for warning in warnings)
    assert any("AAPL: Futu quote unavailable" in warning for warning in warnings)
    assert any("MSFT: Futu quote unavailable" in warning for warning in warnings)


def test_query_missing_futu_quote_is_partial_without_pm_fallback(monkeypatch):
    _patch_positions(
        monkeypatch,
        [],
        futu_context=cash_portfolio({
            "source_observed_at": "2026-07-24T01:00:00Z",
            "cash_by_currency": {"CNY": 100},
            "cash_balance_reliable": True,
            "position_snapshot_input": {
                "rows": [
                    {
                        "instrument_ref": {"asset_type": "stock", "market": "US", "symbol": "NVDA", "currency": "USD"},
                        "position_side": "long",
                        "quantity": "1",
                    }
                ],
                "errors": [],
            },
            "exchange_rates": None,
            "exchange_rate_status": "unavailable",
        }),
    )
    monkeypatch.setattr(
        application,
        "read_portfolio_valuation_evidence",
        lambda **_kwargs: pytest.fail("PM quote fallback is forbidden"),
    )

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["status"] == "partial"
    assert result["distribution"]["net_assets_cny"] is None
    assert any("CNY market value missing" in warning for warning in result["warnings"])


def test_query_preserves_futu_baseline_when_portfolio_source_is_down(monkeypatch):
    _patch_positions(monkeypatch, [], holdings_enabled=True)
    monkeypatch.setattr(
        application,
        "read_portfolio_valuation_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(
            application.PortfolioEvidenceReadError(
                "service down",
                code="PORTFOLIO_MANAGEMENT_UNAVAILABLE",
            )
        ),
    )

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["status"] == "partial"
    assert "service down" in result["warnings"]


def test_pm_read_failure_survives_client_preparation_and_position_sizing(monkeypatch, tmp_path):
    from domain.domain.short_vol_assessment import portfolio_concentration_fields
    from src.application.short_vol_risk_context import build_portfolio_risk_context
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
    from src.infrastructure.portfolio_management_client import PortfolioManagementClient

    _patch_positions(monkeypatch, [], holdings_enabled=True)
    receipt = _valuation_response()
    source_error = "lx: Feishu holdings read timed out"
    receipt.update(status="unavailable", warnings=[source_error])
    receipt["freshness"].update(status="unavailable", trust_status="unavailable")
    receipt["scope"].update(holdings_scope="non_futu", broker_inventory={}, holding_counts={})
    receipt["account_status"][0].update(status="unavailable", warnings=[source_error])
    client = PortfolioManagementClient(urlopen_fn=lambda *_args, **_kwargs: _Response(receipt))
    monkeypatch.setattr(application, "resolve_portfolio_management_client", lambda *_args, **_kwargs: client)
    context = cash_portfolio({
        "cash_by_currency": {"CNY": 10000}, "source_observed_at": "2026-07-24T01:00:00Z",
    }, evaluated_at=application.datetime.now(timezone.utc))
    _, _, config = application._load_runtime_and_positions(["lx"])
    prepared = application.prepare_position_sizing_evidence(
        context=context, runtime_config=config, account="lx",
        fx_observation=application.current_exchange_rate_snapshot(),
    )
    evidence = prepared["position_sizing_evidence"]
    assert evidence["status"] == "partial"
    assert evidence["pm_supplement"]["status"] == "missing"
    assert any(source_error in warning for warning in evidence["warnings"])
    assert any(source_error in reason for reason in evidence["freshness"]["reason_codes"])
    assert prepared["cash_by_currency"] == context["cash_by_currency"]
    prepared["option_ctx"] = {
        "context_status": "available", "decision_snapshot_status": "trusted", "assignment_positions": [],
    }
    fields = portfolio_concentration_fields(
        {"symbol": "NVDA", "strike": 8, "multiplier": 100, "currency": "CNY", "net_income_cny": 50},
        mode="put", risk_ctx=build_portfolio_risk_context(
            portfolio_ctx=prepared, exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        ),
    )
    assert source_error in fields["concentration_unavailable_reason"]
    assert fields["concentration_evaluable"] is False
    assert all(fields[key] is None for key in (
        "symbol_concentration_current", "symbol_concentration_after_existing_assignments", "symbol_concentration_after",
    ))
    assert list(tmp_path.iterdir()) == []


def test_scoped_old_pm_input_error_is_source_failure() -> None:
    from src.infrastructure.portfolio_management_client import PortfolioManagementHTTPError

    class OldClient:
        def read_valuation_evidence(self, **_kwargs):
            raise PortfolioManagementHTTPError("unknown holdings_scope", status=422, error_code="INPUT_ERROR")

    with pytest.raises(application.PortfolioEvidenceReadError, match="holdings_scope"):
        application.read_portfolio_valuation_evidence(
            accounts=["lx"], supplemental_codes=[], client=OldClient(), holdings_scope="non_futu"
        )


def test_query_enabled_without_broker_approval_accepts_empty_pm_scope(monkeypatch):
    _patch_positions(monkeypatch, [], holdings_enabled=True)
    evidence = _valuation_response()
    evidence["scope"].update({"holdings_scope": "non_futu", "broker_inventory": {"lx": {"brokers": []}},
                              "holding_counts": {"lx": {"source_rows": 0, "included": 0, "unsupported": 0}}})
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", lambda **_kwargs: evidence)
    result = application.query_portfolio_assignment_scenario(["lx"])
    assert result["status"] == "complete"
    assert result["snapshot"]["pm_supplement"]["status"] == "complete"
    assert result["snapshot"]["holdings_sources"] == ["futu", "pm_non_futu"]


@pytest.mark.parametrize("status,freshness,trust", [
    ("partial", "fresh", "partial"),
    ("complete", "stale", "trusted"),
    ("complete", "fresh", "untrusted"),
])
def test_query_without_broker_approval_still_marks_pm_quality_partial(monkeypatch, status, freshness, trust):
    _patch_positions(monkeypatch, [], holdings_enabled=True)
    evidence = _valuation_response()
    evidence["status"] = status
    evidence["freshness"].update({"status": freshness, "trust_status": trust})
    evidence["scope"].update({"holdings_scope": "non_futu", "broker_inventory": {"lx": {"brokers": []}},
                              "holding_counts": {"lx": {"source_rows": 0, "included": 0, "unsupported": 0}}})
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", lambda **_kwargs: evidence)
    result = application.query_portfolio_assignment_scenario(["lx"])
    assert result["status"] == "partial"
    assert "PM non-Futu valuation evidence is partial" in result["warnings"]


@pytest.mark.parametrize("approved", [None, ["银行"]])
def test_query_new_broker_is_included_without_approval(monkeypatch, approved):
    _patch_positions(monkeypatch, [], holdings_enabled=True, approved=approved)
    evidence = _valuation_response()
    evidence["scope"].update({
        "holdings_scope": "non_futu",
        "broker_inventory": {"lx": {"brokers": [
            {"broker": "银行", "classification": "non_futu", "row_count": 1},
            {"broker": "新券商", "classification": "non_futu", "row_count": 1},
        ]}},
        "holding_counts": {"lx": {"source_rows": 2, "included": 2, "zero_quantity": 0,
                                  "excluded_futu": 0, "excluded_unknown_broker": 0, "unsupported": 0}},
    })
    evidence["holdings"] = [
        {"account": "lx", "broker": broker, "code": f"{index}-CASH", "asset_type": "cash",
         "quantity": "100", "market_value_cny": "100"}
        for index, broker in enumerate(("银行", "新券商"))
    ]
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", lambda **_kwargs: evidence)
    result = application.query_portfolio_assignment_scenario(["lx"])
    assert result["status"] == "complete"
    assert result["snapshot"]["holdings_sources"] == ["futu", "pm_non_futu"]
    assert result["snapshot"]["pm_supplement"]["included_rows"] == 2
    assert result["distribution"]["net_assets_cny"] == "200.00"
    assert result["cash_coverage"]["available_cash_and_mmf_cny"] == "0.00"


@pytest.mark.parametrize(
    "enabled,expected_cash,expected_codes",
    [
        (False, "125000.00", {"NVDA", "TSLA"}),
        (True, "125000.00", {"NVDA", "TSLA", "BANK"}),
    ],
)
def test_query_uses_futu_baseline_and_only_optional_non_futu_pm_rows(
    monkeypatch,
    enabled,
    expected_cash,
    expected_codes,
):
    context = cash_portfolio({
        "source_observed_at": "2026-07-24T01:00:00Z",
        "cash_by_currency": {"CNY": 125000},  # Futu cash plus its fund_assets/MMF
        "cash_balance_reliable": True,
        "cash_components_by_currency": {"CNY": {"cn_cash": 100000, "fund_assets": 25000}},
        "stocks_by_symbol": {"NVDA": {"shares": 10, "currency": "USD", "name": "Nvidia"}},
        "position_snapshot_input": {
            "rows": [
                {
                    "instrument_ref": {"asset_type": "stock", "market": "US", "symbol": "NVDA", "currency": "USD"},
                    "position_side": "long",
                    "quantity": "10",
                    "source_row": {"stock_name": "Nvidia"},
                },
                {
                    "instrument_ref": {"asset_type": "stock", "market": "US", "symbol": "TSLA", "currency": "USD"},
                    "position_side": "short",
                    "quantity": "10",
                    "source_row": {"stock_name": "Tesla"},
                },
            ]
        },
        "exchange_rates": None,
        "exchange_rate_status": "unavailable",
    })
    _patch_positions(
        monkeypatch,
        [],
        holdings_enabled=enabled,
        futu_context=context,
        futu_quotes=[_futu_quote("NVDA", 100), _futu_quote("TSLA", 50)],
    )
    evidence = _valuation_response()
    evidence["quotes"] = [_futu_quote("NVDA", 9999)]  # PM quote must not value Futu stock.
    evidence["scope"].update({"holdings_scope": "non_futu", "broker_inventory": {"lx": {"brokers": [{"broker": "银行", "classification": "non_futu", "row_count": 2}]}}, "holding_counts": {"lx": {"source_rows": 2, "included": 2, "zero_quantity": 0, "excluded_futu": 0, "excluded_unknown_broker": 0, "unsupported": 0}}})
    evidence["holdings"] = [
        {
            "account": "lx",
            "broker": "银行",
            "code": "CNY-CASH",
            "asset_type": "cash",
            "currency": "CNY",
            "quantity": 2000,
            "market_value_cny": 2000,
        },
        {
            "account": "lx",
            "broker": "银行",
            "code": "BANK",
            "asset_type": "stock",
            "quantity": 1,
            "market_value_cny": 500,
        },
    ]
    seen = {}

    def reader(**kwargs):
        seen["supplemental_codes"] = kwargs["supplemental_codes"]
        seen["holdings_scope"] = kwargs["holdings_scope"]
        return evidence

    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", reader)
    result = application.query_portfolio_assignment_scenario(["lx"])

    assert seen == ({"supplemental_codes": [], "holdings_scope": "non_futu"} if enabled else {})
    assert result["snapshot"]["pm_snapshot_id"] == ("valuation-1" if enabled else None)
    assert result["snapshot"]["holdings_sources"] == (["futu", "pm_non_futu"] if enabled else ["futu"])
    assert result["cash_coverage"]["available_cash_and_mmf_cny"] == expected_cash
    assert {row["code"] for row in result["distribution"]["by_code"]} == expected_codes | {"CASH+MMF"}
    assert result["distribution"]["net_assets_cny"] == ("131100.00" if enabled else "128600.00")
    assert result["distribution"]["liabilities_cny"] == "3600.00"


def test_query_fails_closed_when_futu_baseline_is_unavailable(monkeypatch):
    _patch_positions(monkeypatch, [])
    monkeypatch.setattr(
        application, "fetch_futu_portfolio_context", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("OpenD down"))
    )
    monkeypatch.setattr(
        application, "read_portfolio_valuation_evidence", lambda **_kwargs: pytest.fail("PM should not replace Futu")
    )

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["status"] == "unavailable"
    assert result["cash_coverage"]["available_cash_and_mmf_cny"] is None
    assert "CASH_PROVIDER_UNAVAILABLE" in result["snapshot"]["cash_snapshots"]["lx"]["reason_codes"]


def test_query_marks_incomplete_futu_cash_and_unknown_pm_broker_partial(monkeypatch):
    _patch_positions(
        monkeypatch,
        [],
        holdings_enabled=True,
        futu_context=cash_portfolio({
            "source_observed_at": "2026-07-24T01:00:00Z",
            "cash_by_currency": {"CNY": 100},
            "cash_balance_reliable": False,
            "stocks_by_symbol": {},
            "position_snapshot_input": {"rows": [], "errors": []},
            "exchange_rates": None,
            "exchange_rate_status": "unavailable",
        }),
    )
    evidence = _valuation_response()
    evidence["scope"].update({"holdings_scope": "non_futu", "broker_inventory": {"lx": {"brokers": [{"broker": "Futu", "classification": "futu", "row_count": 1}]}}})
    evidence["holdings"] = [
        {"account": "lx", "code": "UNKNOWN", "asset_type": "stock", "quantity": 1, "market_value_cny": 900},
    ]
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", lambda **_kwargs: pytest.fail("PM read before Futu baseline validation"))

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["status"] == "unavailable"
    assert result["cash_coverage"]["available_cash_and_mmf_cny"] is None
    assert "UNKNOWN" not in {row["code"] for row in result["distribution"]["by_code"]}
    assert any("Futu cash snapshot is incomplete" in warning for warning in result["warnings"])


def test_futu_cash_uses_observed_currency_rate():
    rows, warnings = application._futu_holdings(
        "lx",
        cash_portfolio({
            "source_observed_at": application.datetime.now(timezone.utc).isoformat(),
            "cash_by_currency": {"HKD": 100},
            "cash_balance_reliable": True,
            "stocks_by_symbol": {},
            "position_snapshot_input": {"rows": [], "errors": []},
            "exchange_rates": {"rates": {"HKDCNY": 0.92}},
            "exchange_rate_status": "ready",
        }),
        {},
    )

    assert warnings == []
    assert rows[0]["market_value_cny"] == "92.00"


def test_futu_holdings_requires_full_position_snapshot():
    with pytest.raises(ValueError, match="stock snapshot is missing"):
        application._futu_holdings("lx", cash_portfolio({"cash_by_currency": {"CNY": 0}, "stocks_by_symbol": {}, "position_snapshot_input": None}), {})


def test_call_assignment_does_not_count_pm_futu_stock_copy(monkeypatch):
    _patch_positions(
        monkeypatch,
        [
            {
                "record_id": "call-1",
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "option_type": "call",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "multiplier": 100,
                "strike": 100,
                "currency": "USD",
                "expiration_ymd": "2026-08-28",
            }
        ],
        holdings_enabled=True,
        futu_context=cash_portfolio({
            "source_observed_at": "2026-07-24T01:00:00Z",
            "cash_by_currency": {"CNY": 0},
            "cash_balance_reliable": True,
            "stocks_by_symbol": {"NVDA": {"shares": 100, "currency": "USD"}},
            "position_snapshot_input": {
                "rows": [
                    {
                        "instrument_ref": {"asset_type": "stock", "market": "US", "symbol": "NVDA", "currency": "USD"},
                        "position_side": "long",
                        "quantity": "100",
                    }
                ]
            },
            "exchange_rates": None,
            "exchange_rate_status": "unavailable",
        }),
    )
    evidence = _valuation_response()
    monkeypatch.setattr(application, "_read_futu_quotes", lambda *_args, **_kwargs: ([_futu_quote("NVDA", 120)], []))
    evidence["holdings"] = []
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", lambda **_kwargs: evidence)

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["summary"]["assignment_count"] == 1
    assert result["position_changes"][0]["opening_shares"] == "100"
    assert result["position_changes"][0]["ending_shares"] == "0"


def test_query_returns_business_unavailable_when_option_ledger_is_down(monkeypatch):
    monkeypatch.setattr(
        application,
        "_load_runtime_and_positions",
        lambda accounts: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["status"] == "unavailable"
    assert result["summary"]["assignment_count"] == 0
    assert any("ledger down" in warning for warning in result["warnings"])


@pytest.mark.parametrize("future", [False, True])
def test_assignment_evaluates_cash_after_provider_returns(tmp_path, monkeypatch, future):
    from datetime import timedelta
    from src.application import portfolio_context_service
    start = datetime(2026, 7, 24, 1, tzinfo=timezone.utc)
    clock = [start]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    for module in (application, portfolio_context_service):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(application, "_load_runtime_and_positions",
        lambda _: ([], str(tmp_path / "config.us.json"), cash_config()))
    monkeypatch.setattr(application, "current_exchange_rate_snapshot", lambda **_: {})
    monkeypatch.setattr(application, "project_exchange_rate_snapshot", lambda *a, **kw: {})
    monkeypatch.setattr(application, "_read_futu_quotes", lambda *a, **kw: ([], []))
    def fetch(**kwargs):
        clock[0] += timedelta(milliseconds=1)
        observed = clock[0] + (timedelta(seconds=1) if future else timedelta())
        return cash_portfolio({"cash_source_observed_at": observed.isoformat(),
            "source_observed_at": observed.isoformat(), "cash_by_currency": {"CNY": 100}})
    monkeypatch.setattr(application, "fetch_futu_portfolio_context", fetch)
    result = application.query_portfolio_assignment_scenario(["lx"])
    snapshot = result["snapshot"]["cash_snapshots"]["lx"]
    assert snapshot["status"] == ("unknown" if future else "fresh")
    assert snapshot["reason_codes"] == (["CASH_OBSERVATION_IN_FUTURE"] if future else [])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("observed_at,calendar_rows,complete,expected_stale", [
    ("2026-07-23T20:00:00Z", ["2026-07-23"], True, False),
    ("2026-07-22T20:00:00Z", ["2026-07-22", "2026-07-23"], True, True),
    ("2026-07-22T20:00:00Z", ["2026-07-22"], True, False),
    ("2026-07-23T20:00:00Z", ["2026-07-23"], False, True),
    ("2026-07-23T20:00:00Z", [], True, True),
])
def test_closed_market_valuation_requires_latest_complete_calendar(
    monkeypatch, observed_at, calendar_rows, complete, expected_stale,
):
    calls = []
    class Gateway:
        def close(self):
            calls.append("closed")
        def get_trading_days_with_receipt(self, **kwargs):
            calls.append(kwargs)
            return {"retcode": 0, "coverage_complete": complete, "pagination_complete": True, "page_count": 1,
                    "rows": [{"time": day, "trade_date_type": "WHOLE"} for day in calendar_rows]}
    monkeypatch.setattr(application, "build_ready_futu_quote_gateway", lambda **kwargs: Gateway())
    monkeypatch.setattr(application, "resolve_futu_quote_route", lambda config: SimpleNamespace(ok=True, status="ok", host="127.0.0.1", port=11111))
    observation = SimpleNamespace(code="US.TSLA", market="US", sec_status="NORMAL", suspension=False,
        status="market_closed", reason_code="market_closed", last_price=50, observed_at_utc=observed_at,
        age_seconds=(application.datetime.now(timezone.utc) - datetime.fromisoformat(observed_at.replace("Z", "+00:00"))).total_seconds())
    monkeypatch.setattr(application, "get_underlier_observations_opend", lambda *args, **kwargs: {"US.TSLA": observation})
    context = cash_portfolio({"cash_by_currency": {"CNY": 10000}, "source_observed_at": "2026-07-24T01:00:00Z", "exchange_rates": {"rates": {"USDCNY": 7.2}}}, evaluated_at=application.datetime.now(timezone.utc))
    from src.infrastructure import exchange_rates as fx
    monkeypatch.setattr(fx, "_utc_now", lambda: application.datetime.now(timezone.utc))
    quoted = "2026-07-23T16:00:00Z"
    fx_snapshot = {"calendar": fx.CALENDAR_EVIDENCE, "pairs": {
        pair: {"rate": rate, "source": "tencent_quote", "quote_at_utc": quoted, "observed_at_utc": quoted}
        for pair, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))}}
    evidence = application.prepare_position_sizing_evidence(
        context=context, runtime_config={**cash_config(), "symbols": {"TSLA": {}}}, account="lx",
        fx_observation=fx_snapshot,
    )["position_sizing_evidence"]
    assert evidence["quotes"], evidence.get("warnings")
    quote = evidence["quotes"][0]
    assert quote["is_stale"] is expected_stale
    assert evidence["status"] == ("partial" if expected_stale else "complete"), evidence.get("warnings")
    assert evidence["freshness"]["trust_status"] == ("partial" if expected_stale else "trusted")
    if not expected_stale:
        assert quote["valuation_quality"] == "closed_market_carried"
        assert quote["observed_at"] == observed_at
        assert quote["trading_calendar"]["latest_trading_date"] == calendar_rows[-1]
        from src.application.short_vol_risk_context import build_portfolio_risk_context
        from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
        from domain.domain.short_vol_assessment import portfolio_concentration_fields
        risk = build_portfolio_risk_context(
            portfolio_ctx={**context, "position_sizing_evidence": evidence, "option_ctx": {
                "context_status": "available", "decision_snapshot_status": "trusted", "assignment_positions": []}},
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=1 / 7.2)))
        fields = portfolio_concentration_fields(
            {"symbol": "TSLA", "strike": 45, "multiplier": 100, "currency": "USD", "net_income_cny": 50},
            mode="put", risk_ctx=risk)
        assert fields["symbol_concentration_current"] == 0
        assert fields["symbol_concentration_after"] is not None
        assert f"closed_market_quote:US:{observed_at}" in fields["portfolio_risk_warnings"]
    assert calls[-1] == "closed"
    assert calls[0] == {"market": "US", "start": "2026-07-16", "end": "2026-07-23"}


def test_closed_market_recent_quote_does_not_need_calendar(monkeypatch):
    class Gateway:
        def close(self):
            pass
        def get_trading_days_with_receipt(self, **kwargs):
            pytest.fail("recent quote does not need historical calendar")
    monkeypatch.setattr(application, "build_ready_futu_quote_gateway", lambda **kwargs: Gateway())
    monkeypatch.setattr(application, "resolve_futu_quote_route", lambda config: SimpleNamespace(ok=True, status="ok", host="127.0.0.1", port=11111))
    monkeypatch.setattr(application, "get_underlier_observations_opend", lambda *args, **kwargs: {
        "US.TSLA": SimpleNamespace(code="US.TSLA", market="US", sec_status="NORMAL", suspension=False,
            status="market_closed", reason_code="market_closed", last_price=50,
            observed_at_utc="2026-07-24T00:59:59Z", age_seconds=1)})
    quotes, warnings = application._read_futu_quotes(
        ["TSLA"], runtime_config={}, contexts={"lx": {}}, fx_observation={"rates": {"USDCNY": 7.2}})
    assert not quotes[0]["is_stale"] and not warnings


def _consumer_stock_evidence(monkeypatch, entry, context, *, quote_reader=None):
    from copy import deepcopy
    _patch_positions(monkeypatch, [], futu_context=context)
    if quote_reader is not None:
        monkeypatch.setattr(application, "_read_futu_quotes", quote_reader)
    monkeypatch.setattr(application, "load_account_portfolio_context", lambda **kw: deepcopy(context))
    if entry == "prepared":
        return application.prepare_position_sizing_evidence(
            context=context, runtime_config=cash_config(), account="lx", fx_observation=None,
        )["position_sizing_evidence"]
    observed = []
    project = application.project_assignment_scenario
    def capture(**kwargs):
        observed.append(kwargs["portfolio_evidence"])
        return project(**kwargs)
    monkeypatch.setattr(application, "project_assignment_scenario", capture)
    application.query_portfolio_assignment_scenario(["lx"])
    return observed[0]


@pytest.mark.parametrize("entry", ["query", "prepared"])
@pytest.mark.parametrize("change,reason", [
    ("physical", "snapshot_physical_account_mismatch"),
    ("logical", "snapshot_account_mismatch"),
    ("environment", "snapshot_environment_mismatch"),
    ("market", "snapshot_scope_mismatch"),
    ("asset", "snapshot_scope_mismatch"),
    ("source", "snapshot_source_as_of_utc_stale_or_future"),
    ("refresh", "position_snapshot_refresh_unavailable"),
    ("partial", "snapshot_scope_incomplete"),
    ("future", "snapshot_observed_at_utc_stale_or_future"),
])
def test_assignment_consumers_reject_canonical_stock_scope(monkeypatch, entry, change, reason):
    from datetime import timedelta
    from src.application.portfolio_context_service import portfolio_position_snapshot_errors
    now = application.datetime.now(timezone.utc)
    context = cash_portfolio({"source_observed_at": now.isoformat(), "cash_by_currency": {"CNY": 0}}, evaluated_at=now)
    snapshot = context["position_snapshot_input"]
    if change == "physical":
        snapshot["broker_account_ref"]["external_account_id"] = "456"
    elif change == "logical":
        snapshot["broker_account_ref"]["account_label"] = "sy"
    elif change == "environment":
        snapshot["broker_account_ref"]["environment"] = "SIMULATE"
    elif change == "market":
        snapshot["scope"]["markets"] = ["HK"]
    elif change == "asset":
        snapshot["scope"]["asset_types"] = ["option"]
    elif change == "source":
        snapshot["source_as_of_utc"] = (now - timedelta(seconds=301)).isoformat()
    elif change == "refresh":
        context["position_refresh_error"] = "provider timeout"
    elif change == "partial":
        snapshot["completeness"] = "partial"
    else:
        snapshot["observed_at_utc"] = (now + timedelta(seconds=1)).isoformat()
    assert reason in portfolio_position_snapshot_errors(context, account="lx", now_utc=now)
    evidence = _consumer_stock_evidence(monkeypatch, entry, context,
        quote_reader=lambda *a, **kw: pytest.fail("invalid stock evidence reached valuation"))
    assert evidence["status"] == "unavailable", evidence
    assert evidence["holdings"] == []
    assert any(reason in warning for warning in evidence["warnings"])
    diagnostic = application._futu_evidence(["lx"], {"lx": context}, now_utc=now)
    assert reason in diagnostic["freshness"]["reason_codes"]
    assert diagnostic["freshness"]["status"] == "unknown"


@pytest.mark.parametrize("entry", ["query", "prepared"])
@pytest.mark.parametrize("elapsed", [0, 300])
def test_assignment_consumers_accept_verified_empty_stock_scope(monkeypatch, entry, elapsed):
    from datetime import timedelta
    now = application.datetime.now(timezone.utc)
    context = cash_portfolio({"source_observed_at": now.isoformat(), "cash_by_currency": {"CNY": 0}}, evaluated_at=now)
    snapshot = context["position_snapshot_input"]
    snapshot["observed_at_utc"] = snapshot["source_as_of_utc"] = (now - timedelta(seconds=elapsed)).isoformat()
    evidence = _consumer_stock_evidence(monkeypatch, entry, context)
    assert evidence["status"] == "complete", evidence
    assert evidence["freshness"]["status"] == "fresh"
    assert evidence["holdings"] == [{"account": "lx", "broker": "富途", "code": "CNY-CASH", "asset_type": "cash",
        "currency": "CNY", "quantity": "0", "market_value_cny": "0", "source": "futu_cash_including_mmf"}]


@pytest.mark.parametrize("entry", ["query", "prepared"])
def test_assignment_consumers_revalidate_stocks_after_quote_io(monkeypatch, entry):
    from datetime import timedelta
    now = application.datetime.now(timezone.utc)
    clock = [now]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(application, "datetime", Clock)
    context = cash_portfolio({"source_observed_at": now.isoformat(), "cash_by_currency": {"CNY": 0}}, evaluated_at=now)
    context["position_snapshot_input"]["observed_at_utc"] = context["position_snapshot_input"]["source_as_of_utc"] = (now - timedelta(seconds=300)).isoformat()
    calls = []
    def delayed(*a, **kw):
        calls.append("quote")
        clock[0] += timedelta(seconds=1)
        return [], []
    evidence = _consumer_stock_evidence(monkeypatch, entry, context, quote_reader=delayed)
    assert calls == ["quote"]
    assert evidence["status"] == "unavailable", evidence
    assert any("snapshot_source_as_of_utc_stale_or_future" in warning for warning in evidence["warnings"])


@pytest.fixture
def sizing_recovery_inputs(monkeypatch):
    from datetime import timedelta
    from src.application.required_data_snapshot import FrozenRequiredDataBatch

    now = application.datetime.now(timezone.utc)
    cfg = cash_config()
    cfg["portfolio"].update(account="lx", holdings={"enabled": False})
    cfg["symbols"] = [
        {
            "symbol": "TIGR",
            "fetch": {"source": "opend", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True},
        }
    ]
    fx = {"rates": {"USDCNY": 7.2}, "timestamp": now.isoformat(), "source": "tencent_quote"}
    context = cash_portfolio(
        {
            "source_observed_at": now.isoformat(),
            "cash_by_currency": {"USD": 1000},
            "exchange_rates": fx,
            "position_snapshot_input": {
                "rows": [
                    {
                        "instrument_ref": {"asset_type": "stock", "symbol": "TIGR", "currency": "USD", "market": "US"},
                        "quantity": "100",
                        "position_side": "long",
                    }
                ]
            },
        },
        evaluated_at=now,
    )
    old = {
        **_futu_quote("TIGR", 4.261),
        "is_stale": True,
        "valuation_quality": "stale",
        "observed_at": (now - timedelta(seconds=332)).isoformat(),
        "market": "US",
        "trading_calendar": None,
    }
    warning = f"TIGR: Futu quote is dated ({old['observed_at']})"
    monkeypatch.setattr(application, "_read_futu_quotes", lambda *a, **kw: ([old], [warning]))
    context["position_sizing_evidence"] = application.collect_assignment_portfolio_evidence(
        accounts=["lx"], runtime_config=cfg, futu_contexts={"lx": context}, option_positions=[], fx_observation=fx
    )
    assert context["position_sizing_evidence"]["status"] == "partial", context["position_sizing_evidence"]["warnings"]
    context["option_ctx"] = {
        "context_status": "available",
        "decision_snapshot_status": "trusted",
        "assignment_positions": [],
        "cash_secured_total_by_ccy": {},
    }
    raw = {
        "schema_version": "opening_underlier_observation.v1",
        "code": "US.TIGR",
        "market": "US",
        "last_price": 4.2606,
        "update_time": (now - timedelta(seconds=9)).isoformat(),
        "observed_at_utc": (now - timedelta(seconds=9)).isoformat(),
        "age_seconds": 9,
        "market_state": "AFTERNOON",
        "sec_status": "NORMAL",
        "suspension": False,
        "status": "ready",
        "reason_code": None,
    }
    entry = {
        "source_observed_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=1800)).isoformat(),
        "snapshot_id": "quote-1",
        "receipt_hash": "a" * 64,
        "payload_sha256": "b" * 64,
        "receipt_relpath": "source_receipts/tigr.json",
        "expected_fetch_contract": {
            "fetch_binding": {"source": "opend", "host": "127.0.0.1", "port": 11111},
            "fetch_plan": {"underlier_observation": raw},
        },
    }
    batch = FrozenRequiredDataBatch(
        manifest={"run_id": "run-1"}, manifest_bytes=b"", entries={"TIGR": (entry, b"")}, unavailable={}
    )
    return context, cfg, batch


def test_same_run_quote_recovery_revalues_sizing_without_mutating_inputs(sizing_recovery_inputs):
    from copy import deepcopy
    from src.application.short_vol_risk_context import build_portfolio_risk_context
    from domain.domain.short_vol_assessment import portfolio_concentration_fields
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    context, cfg, batch = sizing_recovery_inputs
    before = deepcopy(context)
    updated = application.recover_position_sizing_quotes(
        context=context, runtime_config=cfg, account="lx", run_id="run-1", quotes=batch
    )
    assert context == before
    assert updated is not context
    evidence = updated["position_sizing_evidence"]
    assert evidence["status"] == "complete"
    assert evidence["warnings"] == []
    assert evidence["quotes"][0]["price_native"] == "4.2606"
    assert float(evidence["holdings"][1]["market_value_cny"]) == pytest.approx(100 * 4.2606 * 7.2)
    converter = CurrencyConverter(ExchangeRates(usd_per_cny=1 / 7.2))
    row = {"symbol": "TIGR", "currency": "USD", "multiplier": 100, "strike": 4, "net_income_cny": 15.18 * 7.2}
    original = portfolio_concentration_fields(
        row, mode="put", risk_ctx=build_portfolio_risk_context(portfolio_ctx=context, exchange_rate_converter=converter)
    )
    assert original["symbol_concentration_current"] is None
    result = portfolio_concentration_fields(
        row, mode="put", risk_ctx=build_portfolio_risk_context(portfolio_ctx=updated, exchange_rate_converter=converter)
    )
    assert result["symbol_concentration_current"] == round(426.06 / 1426.06, 6)
    assert result["symbol_concentration_after_existing_assignments"] == result["symbol_concentration_current"]
    assert result["symbol_concentration_after"] == round(852.12 / (1426.06 + 426.06 - 400 + 15.18), 6)
    assert result["position_sizing_quote_recovery"]["source_receipts"]["TIGR"]["snapshot_id"] == "quote-1"


@pytest.mark.parametrize(
    "failure",
    [
        "stale",
        "future",
        "missing",
        "wrong_code",
        "wrong_market",
        "halt",
        "suspended",
        "bad_price",
        "wrong_route",
        "wrong_run",
        "wrong_account",
        "pm",
        "missing_fx",
        "stock_stale",
        "cash",
        "receipt_stale",
        "unknown_reason",
        "second_stale",
        "other_quote_aged",
    ],
)
def test_quote_recovery_keeps_incomplete_or_untrusted_inputs_closed(sizing_recovery_inputs, failure):
    from datetime import timedelta

    context, cfg, batch = sizing_recovery_inputs
    entry = batch.entries["TIGR"][0]
    raw = entry["expected_fetch_contract"]["fetch_plan"]["underlier_observation"]
    evidence = context["position_sizing_evidence"]
    now = application.datetime.now(timezone.utc)
    account, run_id = "lx", "run-1"
    if failure in {"stale", "future"}:
        raw["update_time"] = (now + timedelta(seconds=-301 if failure == "stale" else 1)).isoformat()
    elif failure == "missing":
        batch.entries.clear()
    elif failure == "wrong_code":
        raw["code"] = "US.NVDA"
    elif failure == "wrong_market":
        raw["market"] = "HK"
    elif failure == "halt":
        raw["sec_status"] = "HALT"
    elif failure == "suspended":
        raw["suspension"] = True
    elif failure == "bad_price":
        raw["last_price"] = float("nan")
    elif failure == "wrong_route":
        entry["expected_fetch_contract"]["fetch_binding"]["port"] = 22222
    elif failure == "wrong_run":
        run_id = "run-2"
    elif failure == "wrong_account":
        account = "sy"
    elif failure == "pm":
        evidence["warnings"].append("PM non-Futu valuation evidence is partial")
    elif failure == "missing_fx":
        evidence["fx_rates_to_cny"] = {}
    elif failure == "stock_stale":
        context["position_snapshot_input"]["source_as_of_utc"] = (now - timedelta(seconds=301)).isoformat()
    elif failure == "cash":
        context["cash_snapshot"]["status"] = "unavailable"
    elif failure == "receipt_stale":
        entry["expires_at"] = now.isoformat()
    elif failure == "unknown_reason":
        evidence["freshness"]["reason_codes"].append("UNKNOWN")
    elif failure == "second_stale":
        other = {**evidence["quotes"][0], "code": "NVDA"}
        warning = f"NVDA: Futu quote is dated ({other['observed_at']})"
        evidence["quotes"].append(other)
        evidence["warnings"].append(warning)
        evidence["freshness"]["reason_codes"].append(warning)
    elif failure == "other_quote_aged":
        evidence["quotes"].append(
            {**evidence["quotes"][0], "code": "NVDA", "is_stale": False, "valuation_quality": "fresh"}
        )
    result = application.recover_position_sizing_quotes(
        context=context, runtime_config=cfg, account=account, run_id=run_id, quotes=batch
    )
    assert result is context
    assert result["position_sizing_evidence"]["status"] == "partial"


def test_watchlist_recovers_before_capture_and_strategy(sizing_recovery_inputs, tmp_path):
    from tests.test_pipeline_watchlist_whitelist import _pipeline

    context, cfg, batch = sizing_recovery_inputs
    seen = []

    def scan(*args, **kwargs):
        seen.append(kwargs["portfolio_ctx"]["position_sizing_evidence"])
        return []

    _pipeline(
        cfg,
        process_symbol_fn=scan,
        base=tmp_path,
        report_dir=tmp_path,
        build_pipeline_context_fn=lambda **kw: (context, None, 1 / 7.2, None),
        required_data_snapshot_batch=batch,
        source_producer_run_id="run-1",
        opening_runtime_context_sink_fn=lambda p, o: seen.append(p["position_sizing_evidence"]),
    )
    assert len(seen) == 2
    assert all(e["status"] == "complete" for e in seen)
    assert seen[0] == seen[1]


@pytest.mark.parametrize("age, recovered", [(0, True), (300, True), (301, False)])
def test_quote_recovery_preserves_300_second_boundary(sizing_recovery_inputs, age, recovered):
    from datetime import timedelta

    context, cfg, batch = sizing_recovery_inputs
    raw = batch.entries["TIGR"][0]["expected_fetch_contract"]["fetch_plan"]["underlier_observation"]
    raw["update_time"] = (application.datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    result = application.recover_position_sizing_quotes(
        context=context, runtime_config=cfg, account="lx", run_id="run-1", quotes=batch
    )
    assert (result is not context) is recovered


def test_quote_recovery_preserves_non_futu_holdings_and_closed_market_quotes(sizing_recovery_inputs):
    from copy import deepcopy

    context, cfg, batch = sizing_recovery_inputs
    evidence = context["position_sizing_evidence"]
    holding = {
        "account": "lx",
        "broker": "other-broker",
        "code": "CNY-CASH",
        "asset_type": "cash",
        "currency": "CNY",
        "quantity": "50",
        "market_value_cny": "50",
    }
    quote = {
        "code": "0700.HK",
        "source": "futu_opend_market_snapshot",
        "currency": "HKD",
        "is_stale": False,
        "valuation_quality": "closed_market_carried",
        "observed_at": "2026-07-23T08:08:00Z",
        "trading_calendar": {"latest_trading_date": "2026-07-23"},
    }
    evidence["holdings"].append(holding)
    evidence["quotes"].append(quote)
    before = deepcopy(evidence)
    updated = application.recover_position_sizing_quotes(
        context=context, runtime_config=cfg, account="lx", run_id="run-1", quotes=batch
    )
    assert updated is not context
    result = updated["position_sizing_evidence"]
    assert result["holdings"][-1] == holding
    assert result["quotes"][-1] == quote
    assert evidence == before


@pytest.mark.parametrize("entry", ["query", "agent_bot"])
def test_ledger_read_failure_keeps_account_cash_unknown_through_public_consumers(monkeypatch, entry):
    import sqlite3
    from src.application.bot.tools import model_observation
    from src.application.tool_execution import execute_tool

    def failed_ledger(_accounts):
        raise sqlite3.OperationalError("synthetic assignment ledger unavailable")

    def forbidden(*_args, **_kwargs):
        pytest.fail("ledger failure must not fetch portfolio/provider evidence")

    monkeypatch.setattr(application, "_load_runtime_and_positions", failed_ledger)
    monkeypatch.setattr(application, "fetch_futu_portfolio_context", forbidden)
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence", forbidden)
    monkeypatch.setattr(application, "_read_futu_quotes", forbidden)
    if entry == "query":
        result = application.query_portfolio_assignment_scenario(["lx", "sy"])
    else:
        response = execute_tool("portfolio_assignment_scenario", {"accounts": ["lx", "sy"]})
        assert response["ok"] is True  # Transport success is not business availability.
        observation = model_observation("portfolio_assignment_scenario", response)
        assert observation["ok"] is True
        result = observation["data"]
    assert result["status"] == "unavailable"
    assert result["cash_coverage"]["ending_cash_net_estimated_cny"] is None
    assert [row["account"] for row in result["account_breakdown"]] == ["lx", "sy"]
    for account in result["account_breakdown"]:
        assert account["opening_cash_mmf_cny"] is None
        assert account["ending_cash_gross_cny"] is None
        assert account["ending_cash_net_estimated_cny"] is None
        assert account["funding_gap_cny"] is None
        assert account["fees_complete"] is False
    assert result["fee_summary"]["status"] == "partial"
    assert result["fee_summary"]["total_fees_cny"] is None
    assert any("synthetic assignment ledger unavailable" in warning for warning in result["warnings"])
