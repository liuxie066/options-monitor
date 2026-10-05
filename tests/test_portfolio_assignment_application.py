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
            {**cash_config(), "portfolio_management": {"enabled": True}, "portfolio": {"holdings": {"enabled": holdings_enabled, **({"approved_non_futu_brokers": {"lx": approved if approved is not None else ["银行"]}} if holdings_enabled else {})}}},
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
                        "instrument_ref": {"asset_type": "stock", "symbol": "NVDA", "currency": "USD"},
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


def test_scoped_old_pm_input_error_is_source_failure() -> None:
    from src.infrastructure.portfolio_management_client import PortfolioManagementHTTPError

    class OldClient:
        def read_valuation_evidence(self, **_kwargs):
            raise PortfolioManagementHTTPError("unknown holdings_scope", status=422, error_code="INPUT_ERROR")

    with pytest.raises(application.PortfolioEvidenceReadError, match="holdings_scope"):
        application.read_portfolio_valuation_evidence(
            accounts=["lx"], supplemental_codes=[], client=OldClient(), holdings_scope="non_futu"
        )


def test_query_old_enabled_config_without_broker_approval_skips_pm(monkeypatch):
    _patch_positions(monkeypatch, [], holdings_enabled=True)
    monkeypatch.setattr(application, "_load_runtime_and_positions", lambda _accounts: (
        [], "config.us.json", {**cash_config(), "portfolio": {"holdings": {"enabled": True}}},
    ))
    monkeypatch.setattr(application, "read_portfolio_valuation_evidence",
                        lambda **_kwargs: pytest.fail("PM must not be read before broker approval"))
    result = application.query_portfolio_assignment_scenario(["lx"])
    assert result["status"] == "partial"
    assert result["snapshot"]["pm_supplement"]["status"] == "missing"
    assert any("re-preview" in warning for warning in result["warnings"])


def test_query_new_broker_pauses_entire_pm_supplement(monkeypatch):
    _patch_positions(monkeypatch, [], holdings_enabled=True, approved=["银行"])
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
    assert result["status"] == "partial"
    assert result["snapshot"]["holdings_sources"] == ["futu"]
    assert result["snapshot"]["pm_supplement"]["included_rows"] == 0
    assert any("新券商" in warning and "re-preview" in warning for warning in result["warnings"])


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
                    "instrument_ref": {"asset_type": "stock", "symbol": "NVDA", "currency": "USD"},
                    "position_side": "long",
                    "quantity": "10",
                    "source_row": {"stock_name": "Nvidia"},
                },
                {
                    "instrument_ref": {"asset_type": "stock", "symbol": "TSLA", "currency": "USD"},
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
                        "instrument_ref": {"asset_type": "stock", "symbol": "NVDA", "currency": "USD"},
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
