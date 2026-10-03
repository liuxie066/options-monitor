from __future__ import annotations

import pytest

from cash_evidence_helpers import cash_config, cash_portfolio

from src.application.ledger.api import decision_state_snapshot
from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions.context_builder import build_context


@pytest.fixture
def context_inputs(tmp_path, monkeypatch):
    import src.application.pipeline_context as pc

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    run_position_projection_forced_full(repo, [])
    snapshot = decision_state_snapshot(repo, account="lx", portfolio_scope_id="test-lx")
    assert snapshot["snapshot_status"] == "trusted"
    option_context = build_context([], broker="富途", account="lx", decision_snapshot=snapshot)
    monkeypatch.setattr(pc, "open_position_ledger", lambda *_a, **_k: repo)
    monkeypatch.setattr(pc, "is_fresh", lambda *_a, **_k: True)
    logs = []
    return dict(
        base=tmp_path, data_config=str(tmp_path / "fixture.json"), market="富途", account="lx",
        ttl_sec=3600, state_dir=tmp_path / "state", shared_state_dir=tmp_path / "shared",
        log=logs.append,
    ), option_context, logs


def test_load_portfolio_context_rejects_invalid_cached_contract(tmp_path, monkeypatch) -> None:
    import src.application.pipeline_context as pc

    monkeypatch.setattr(pc, "load_cached_json", lambda *_a, **_k: {
        "as_of_utc": "2026-04-12T00:00:00+00:00",
        "stocks_by_symbol": [], "cash_by_currency": {},
    })
    calls = []
    captured = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("fixture provider unavailable")

    monkeypatch.setattr(pc, "fetch_futu_portfolio_context", unavailable)
    monkeypatch.setattr(pc.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: captured.append(args))
    out = pc.load_portfolio_context(
        base=tmp_path, data_config="x.json", market="富途", account="lx",
        state_dir=tmp_path, shared_state_dir=tmp_path, log=lambda _: None,
        runtime_config=cash_config(),
    )
    assert out["cash_snapshot"]["status"] == "unknown"
    assert "CASH_PROVIDER_UNAVAILABLE" in out["cash_snapshot"]["reason_codes"]
    assert len(calls) == 1
    assert captured == []
    assert list(tmp_path.iterdir()) == []


def test_load_option_positions_context_rejects_invalid_cached_contract(context_inputs, monkeypatch):
    import src.application.pipeline_context as pc

    args, context, logs = context_inputs
    monkeypatch.setattr(pc, "load_cached_json", lambda *_a, **_k: {
        **context, "locked_shares_by_symbol": [],
    })
    out, refreshed = pc.load_option_positions_context(**args)
    assert out is None
    assert refreshed is False
    assert any("option positions context not available" in x for x in logs)


def test_load_context_persists_source_snapshots_for_valid_cached_contracts(context_inputs, monkeypatch):
    import src.application.pipeline_context as pc

    args, option_context, _ = context_inputs
    captured = []
    monkeypatch.setattr(pc.state_repo, "append_source_snapshot_event", lambda _base, payload, **_k: captured.append(payload))

    def load_cached(path):
        if path.name == "portfolio_context.json":
            return cash_portfolio({
                "stocks_by_symbol": {"AAPL": {"shares": 100}},
                "cash_by_currency": {"USD": 100.0},
                "portfolio_source_name": "futu", "filters": {"account": "lx"},
            })
        return option_context

    monkeypatch.setattr(pc, "load_cached_json", load_cached)
    monkeypatch.setattr(pc, "fetch_futu_portfolio_context", lambda **_: pytest.fail("fresh cache must avoid provider"))
    pctx = pc.load_portfolio_context(**{k: v for k, v in args.items() if k != "ttl_sec"}, runtime_config=cash_config())
    octx, refreshed = pc.load_option_positions_context(**args)
    assert pctx is not None
    assert octx is not None
    assert refreshed is False
    assert {str(x.get("source_name")) for x in captured} == {"holdings", "option_positions"}


@pytest.mark.parametrize("stock_projection", ["missing", "list", "empty", "populated"])
def test_cached_stock_projection_controls_nav_without_invalidating_cash(
    tmp_path, monkeypatch, stock_projection,
) -> None:
    import json

    from domain.domain.short_vol_assessment import portfolio_concentration_fields
    from src.application import pipeline_context as pc
    from src.application.futu_portfolio_context import build_futu_position_snapshot
    from src.application.short_vol_risk_context import build_portfolio_risk_context
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    context = cash_portfolio({"cash_by_currency": {"CNY": 100000}})
    context["position_snapshot_input"] = build_futu_position_snapshot(
        rows=[] if stock_projection == "empty" else [
            {"code": "US.NVDA", "sec_type": "STOCK", "qty": 100,
             "market_val": 10000, "currency": "USD"},
        ],
        broker_account_ref={"broker_id": "futu", "external_account_id": "123",
            "environment": "REAL", "account_label": "lx", "broker_account_id": "futu:REAL:123"},
        markets=["US", "HK"], asset_types=["stock", "option"],
        observed_at_utc=context["cash_source_observed_at"], completeness="complete",
    )
    if stock_projection == "missing":
        context.pop("stocks_by_symbol")
    elif stock_projection == "list":
        context["stocks_by_symbol"] = []
    elif stock_projection == "populated":
        context["stocks_by_symbol"] = {"NVDA": {"shares": 100, "market_value_cny": 70000}}
    context["option_ctx"] = {
        "decision_snapshot_status": "trusted", "cash_secured_total_by_ccy": {},
        "cash_secured_by_symbol_by_ccy": {}, "cash_secured_unavailable_by_symbol": {},
    }
    cache = tmp_path / "portfolio_context.json"
    cache.write_text(json.dumps(context))
    original_bytes = cache.read_bytes()
    monkeypatch.setattr(pc, "fetch_futu_portfolio_context", lambda **_: pytest.fail("unexpected provider call"))
    monkeypatch.setattr(pc.state_repo, "append_source_snapshot_event", lambda *_a, **_k: {})
    loaded = pc.load_portfolio_context(
        base=tmp_path, data_config="fixture.json", market="富途", account="lx",
        state_dir=tmp_path, shared_state_dir=None, log=lambda _: None, runtime_config=cash_config(),
    )
    assert loaded["context_source"] == "account_cache"
    assert loaded["cash_snapshot"]["status"] == "fresh"
    assert loaded["cash_by_currency"] == {"CNY": 100000}
    assert cache.read_bytes() == original_bytes
    risk = build_portfolio_risk_context(
        portfolio_ctx=loaded, exchange_rate_converter=CurrencyConverter(ExchangeRates()),
    )
    fields = portfolio_concentration_fields(
        {"symbol": "NVDA", "cash_required_cny": 10000}, mode="put", risk_ctx=risk,
    )
    if stock_projection in {"missing", "list"}:
        assert loaded["position_snapshot_input"]["rows"][0]["quantity"] == "100"
        assert risk.nav_cny is None
        assert risk.unavailable_reasons == ("broker_positions_unavailable",)
        assert fields["concentration_evaluable"] is False
        assert fields["symbol_concentration_after"] is None
        assert fields["concentration_score"] is None
    else:
        assert risk.nav_cny == (170000 if stock_projection == "populated" else 100000)
        assert not risk.unavailable_reasons
        assert fields["concentration_evaluable"] is True
        assert fields["symbol_concentration_after"] == pytest.approx(
            80000 / 170000 if stock_projection == "populated" else 0.1, abs=1e-6,
        )
