from __future__ import annotations



from src.application.portfolio_context_builder import build_context


def _cell(text: object) -> list[dict[str, object]]:
    return [{"text": text, "type": "text"}]


def _rec(**fields: object) -> dict:
    return {"fields": fields}


def _timed_rec(last_modified_time: str, **fields: object) -> dict:
    return {"last_modified_time": last_modified_time, "fields": fields}


def test_build_context_requires_broker_field_and_normalizes_hk_symbol() -> None:
    records = [
        _rec(
            broker=_cell("富途"), account=_cell(" LX "), asset_type="hk_stock",
            asset_id=_cell("00700"), asset_name=_cell("腾讯控股"), currency="港币",
            quantity=500, avg_cost=503.916, asset_class="港股资产",
        ),
        _rec(
            broker=_cell("富途"), account=_cell("lx"), asset_type="cash",
            asset_id=_cell("CNY-CASH"), asset_name=_cell("账户余额"), currency="rmb",
            quantity=406.24, asset_class="现金",
        ),
        _rec(
            broker=_cell("富途"), account=_cell("lx"), asset_type="us_stock",
            asset_id=_cell("NVDA"), asset_name=_cell("英伟达"), currency="USD",
            quantity=160, avg_cost=164.959, asset_class="美国资产",
        ),
    ]

    ctx = build_context(records, broker="富途", account="lx")

    assert ctx["raw_selected_count"] == 3
    assert ctx["cash_by_currency"]["CNY"] == 406.24

    stocks = ctx["stocks_by_symbol"]
    assert "0700.HK" in stocks
    assert stocks["0700.HK"]["shares"] == 500
    assert stocks["0700.HK"]["currency"] == "HKD"
    assert stocks["0700.HK"]["account"] == "lx"

    assert "NVDA" in stocks
    assert stocks["NVDA"]["shares"] == 160


def test_build_context_accepts_legacy_market_only_holdings_rows() -> None:
    records = [
        _rec(
            market=_cell("富途"), account=_cell("lx"), asset_type="cash",
            asset_id=_cell("USD-CASH"), currency="USD", quantity=100,
        )
    ]

    ctx = build_context(records, broker="富途", account="lx")

    assert ctx["raw_selected_count"] == 1
    assert ctx["cash_by_currency"] == {"USD": 100.0}
    assert ctx["stocks_by_symbol"] == {}


def test_build_context_accepts_broker_field_without_market() -> None:
    records = [
        _rec(broker="富途", account="lx", asset_type="cash", asset_id="USD-CASH", currency="USD", quantity="123.45"),
        _rec(
            broker="富途", account="lx", asset_type="us_stock", asset_id="AAPL",
            asset_name="Apple", currency="USD", quantity="20", avg_cost="150",
        ),
        _rec(broker="其他券商", account="lx", asset_type="cash", asset_id="USD-CASH", currency="USD", quantity="999"),
    ]

    ctx = build_context(records, broker="富途", account="lx")

    assert ctx["filters"]["broker"] == "富途"
    assert "market" not in ctx["filters"]
    assert ctx["raw_selected_count"] == 2
    assert ctx["cash_by_currency"]["USD"] == 123.45
    assert ctx["stocks_by_symbol"]["AAPL"]["broker"] == "富途"


def test_build_context_aggregates_duplicate_stock_rows_for_same_account() -> None:
    records = [
        _rec(
            broker="富途", account="lx", asset_type="us_stock", asset_id="AAPL",
            asset_name="Apple", currency="USD", quantity="50", avg_cost="100",
        ),
        _rec(
            broker="富途", account="lx", asset_type="us_stock", asset_id="AAPL",
            asset_name="Apple", currency="USD", quantity="150", avg_cost="120",
        ),
    ]

    ctx = build_context(records, broker="富途", account="lx")

    stock = ctx["stocks_by_symbol"]["AAPL"]
    assert stock["shares"] == 200
    assert stock["avg_cost"] == 115.0
    assert stock["cost_basis_complete"] is True
    assert stock["cost_known_shares"] == 200
    assert stock["cost_unknown_shares"] == 0


def test_build_context_does_not_apply_partial_cost_basis_to_all_shares() -> None:
    records = [
        _rec(
            broker="富途", account="lx", asset_type="us_stock", asset_id="AAPL",
            quantity="50", avg_cost="100",
        ),
        _rec(
            broker="富途", account="lx", asset_type="us_stock", asset_id="AAPL",
            quantity="50", avg_cost=None,
        ),
    ]

    stock = build_context(records, broker="富途", account="lx")["stocks_by_symbol"]["AAPL"]

    assert stock["shares"] == 100
    assert stock["avg_cost"] is None
    assert stock["cost_basis_complete"] is False
    assert stock["cost_known_shares"] == 50
    assert stock["cost_unknown_shares"] == 50


def test_feishu_record_update_time_is_business_observation_not_read_time() -> None:
    records = [
        _timed_rec(
            "1785110400000", broker="富途", account="lx", asset_type="cash",
            asset_id="USD-CASH", currency="USD", quantity="100",
        ),
        _timed_rec(
            "1785114000000", broker="富途", account="lx", asset_type="us_stock",
            asset_id="NVDA", currency="USD", quantity="10", avg_cost="100",
        ),
    ]

    first = build_context(records, broker="富途", account="lx")
    second = build_context(records, broker="富途", account="lx")

    assert first["source_observed_at"] == "2026-07-27T00:00:00Z"
    assert second["source_observed_at"] == first["source_observed_at"]
    assert first["retrieved_at_utc"] != first["source_observed_at"]
    assert first["source_observation_status"] == "trusted"
    assert (
        first["source_observation_basis"]
        == "feishu_record:last_modified_time"
    )


def test_missing_or_invalid_record_observation_is_unknown() -> None:
    base = _rec(
        broker="富途", account="lx", asset_type="cash",
        asset_id="USD-CASH", currency="USD", quantity="100",
    )

    missing = build_context([base], broker="富途", account="lx")
    invalid = build_context(
        [{**base, "last_modified_time": "not-a-time"}],
        broker="富途",
        account="lx",
    )

    assert missing["source_observed_at"] is None
    assert missing["source_observation_status"] == "unknown"
    assert invalid["source_observed_at"] is None
    assert invalid["source_observation_status"] == "unknown"


def test_owner_snapshot_time_takes_precedence_over_feishu_read_metadata() -> None:
    context = build_context(
        [
            _timed_rec(
                "1785114000000", snapshot_observed_at="2026-07-26T20:00:00Z",
                broker="富途", account="lx", asset_type="cash",
                asset_id="USD-CASH", currency="USD", quantity="100",
            )
        ],
        broker="富途",
        account="lx",
    )

    assert context["source_observed_at"] == "2026-07-26T20:00:00Z"
    assert (
        context["source_observation_basis"]
        == "holdings_field:snapshot_observed_at"
    )
