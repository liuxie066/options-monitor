from __future__ import annotations

from pathlib import Path

import pytest

from cash_evidence_helpers import cash_config, cash_portfolio

from src.application.positions.context_builder import STRATEGY_FAMILY_SOURCE



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


def test_load_option_positions_context_rejects_invalid_cached_contract() -> None:
    import src.application.pipeline_context as pc

    old_is_fresh = pc.is_fresh
    old_load_cached_json = pc.load_cached_json
    try:
        pc.is_fresh = lambda *_a, **_k: True  # type: ignore[assignment]
        pc.load_cached_json = lambda *_a, **_k: {  # type: ignore[assignment]
            "as_of_utc": "2026-04-12T00:00:00+00:00",
            "locked_shares_by_symbol": [],
            "cash_secured_by_symbol_by_ccy": {},
            "strategy_family_source": STRATEGY_FAMILY_SOURCE,
        }
        logs: list[str] = []
        out, refreshed = pc.load_option_positions_context(
            base=Path("."),
            data_config="x.json",
            market="富途",
            account=None,
            ttl_sec=3600,
            state_dir=Path("."),
            shared_state_dir=Path("."),
            log=logs.append,
        )
        assert out is None
        assert refreshed is False
        assert any("option positions context not available" in x for x in logs)
    finally:
        pc.is_fresh = old_is_fresh  # type: ignore[assignment]
        pc.load_cached_json = old_load_cached_json  # type: ignore[assignment]


def test_load_context_persists_source_snapshots_for_valid_cached_contracts(tmp_path, monkeypatch) -> None:
    import src.application.pipeline_context as pc

    monkeypatch.setattr(pc, "fetch_futu_portfolio_context", lambda **_: pytest.fail("fresh cache must avoid provider"))
    old_is_fresh = pc.is_fresh
    old_load_cached_json = pc.load_cached_json
    old_append = pc.state_repo.append_source_snapshot_event
    try:
        pc.is_fresh = lambda *_a, **_k: True  # type: ignore[assignment]
        captured: list[dict] = []

        def _append(_base, payload, run_id=None):  # type: ignore[no-untyped-def]
            captured.append(payload)
            return {}

        pc.state_repo.append_source_snapshot_event = _append  # type: ignore[assignment]

        def _load_cached(path: Path):  # type: ignore[no-untyped-def]
            if path.name == "portfolio_context.json":
                return cash_portfolio({
                    "stocks_by_symbol": {"AAPL": {"shares": 100}},
                    "cash_by_currency": {"USD": 100.0},
                    "portfolio_source_name": "futu",
                })
            return {
                "as_of_utc": "2026-04-12T00:00:00+00:00",
                "locked_shares_by_symbol": {"AAPL": 100},
                "cash_secured_by_symbol_by_ccy": {"AAPL": {"USD": 1000.0}},
                "strategy_family_source": STRATEGY_FAMILY_SOURCE,
            }

        pc.load_cached_json = _load_cached  # type: ignore[assignment]

        logs: list[str] = []
        pctx = pc.load_portfolio_context(
            base=tmp_path,
            data_config="x.json",
            market="富途",
            account="lx",
            runtime_config=cash_config(),
            state_dir=tmp_path,
            shared_state_dir=tmp_path,
            log=logs.append,
        )
        octx, refreshed = pc.load_option_positions_context(
            base=tmp_path,
            data_config="x.json",
            market="富途",
            account=None,
            ttl_sec=3600,
            state_dir=tmp_path,
            shared_state_dir=tmp_path,
            log=logs.append,
        )
        assert pctx is not None
        assert octx is not None
        assert refreshed is False
        assert {str(x.get("source_name")) for x in captured} == {"holdings", "option_positions"}
    finally:
        pc.is_fresh = old_is_fresh  # type: ignore[assignment]
        pc.load_cached_json = old_load_cached_json  # type: ignore[assignment]
        pc.state_repo.append_source_snapshot_event = old_append  # type: ignore[assignment]


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
