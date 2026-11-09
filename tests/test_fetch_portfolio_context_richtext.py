from __future__ import annotations

import copy
import json
import sys
from types import SimpleNamespace

import pytest

from src.application import portfolio_context_builder as builder
from src.application.portfolio_context_builder import build_context
from src.infrastructure import feishu_bitable


def _cell(text: object) -> list[dict[str, object]]:
    return [{"text": text, "type": "text"}]


def _rec(**fields: object) -> dict:
    return {"fields": fields}


def _timed_rec(last_modified_time: str, **fields: object) -> dict:
    return {"last_modified_time": last_modified_time, "fields": fields}


def _run_isolated_holdings_cli(tmp_path, monkeypatch, primary_rows):
    records = primary_rows + [
        _rec(broker="富途", account="other768", asset_type="us_stock", asset_id="NVDA",
             quantity="7", avg_cost="10", currency="USD"),
        _rec(broker="excluded768", account="qa768", asset_type="us_stock", asset_id="NVDA",
             quantity="999", avg_cost="NaN", currency="USD"),
    ]
    original_records = copy.deepcopy(records)
    data_config = tmp_path / "data.json"
    data_config.write_text('{"fixture": "qa768"}', encoding="utf-8")
    state_dir = tmp_path / "state"
    output = state_dir / "portfolio_context.json"
    shared_output = state_dir / "shared.json"
    calls = []

    def resolve_fixture(config):
        calls.append(("config", config))
        return SimpleNamespace(ready=True, missing_fields=(), holdings_ref="fixture-app/fixture-table",
                               app_id="fixture-id", app_secret="fixture-secret")

    def token(app_id, app_secret):
        calls.append(("token", app_id, app_secret))
        return "fixture-token"

    def search(token, app_token, table_id):
        calls.append(("search", token, app_token, table_id))
        return records

    def forbidden(*args, **kwargs):
        calls.append(("forbidden",))
        raise AssertionError("unexpected provider call")

    monkeypatch.setattr(builder, "resolve_feishu_holdings_config", resolve_fixture)
    monkeypatch.setattr(feishu_bitable, "get_tenant_access_token", token)
    monkeypatch.setattr(builder, "bitable_search_records", search)
    monkeypatch.setattr(builder, "bitable_list_records", forbidden)
    monkeypatch.setattr(feishu_bitable, "http_json", forbidden)
    monkeypatch.setattr(sys, "argv", ["portfolio_context_builder", "--data-config", str(data_config),
                                    "--broker", "富途", "--account", "qa768", "--state-dir", str(state_dir),
                                    "--shared-out", str(shared_output), "--quiet"])
    builder.main()
    assert calls == [
        ("config", {"fixture": "qa768"}),
        ("token", "fixture-id", "fixture-secret"),
        ("search", "fixture-token", "fixture-app", "fixture-table"),
    ]
    assert records == original_records
    assert data_config.read_bytes() == b'{"fixture": "qa768"}'
    assert {path for path in tmp_path.rglob("*") if path.is_file()} == {data_config, output, shared_output}

    def reject_constant(value):
        raise ValueError(f"non-standard JSON number: {value}")

    context = json.loads(output.read_text(encoding="utf-8"), parse_constant=reject_constant)
    shared = json.loads(shared_output.read_text(encoding="utf-8"), parse_constant=reject_constant)
    assert context["filters"] == {"broker": "富途", "account": "qa768"}
    assert set(shared["by_account"]) == {"qa768", "other768"}
    assert shared["by_account"]["qa768"]["stocks_by_symbol"] == context["stocks_by_symbol"]
    assert shared["by_account"]["other768"]["stocks_by_symbol"]["NVDA"]["shares"] == 7
    return context, shared


@pytest.mark.parametrize("cost,known,only_average,mixed_average", [
    ("NaN", 0, None, None), ("Infinity", 0, None, None), ("-Infinity", 0, None, None),
    (None, 0, None, None), ("12.5", 10, 12.5, 20.833333333333332),
    ("0", 10, 0.0, 16.666666666666668),
])
@pytest.mark.parametrize("order", ["only", "invalid_first", "known_first"])
def test_holdings_cli_nonfinite_cost_is_unknown_and_outputs_strict_json(
    tmp_path, monkeypatch, cost, known, only_average, mixed_average, order,
):
    row = _rec(broker="富途", account="qa768", asset_type="us_stock", asset_id="NVDA",
               quantity="10", avg_cost=cost, currency="USD")
    clean = _rec(broker="富途", account="qa768", asset_type="us_stock", asset_id="NVDA",
                 quantity="20", avg_cost="25", currency="USD")
    rows = [row] if order == "only" else [row, clean] if order == "invalid_first" else [clean, row]
    context, shared = _run_isolated_holdings_cli(tmp_path, monkeypatch, rows)
    stock = context["stocks_by_symbol"]["NVDA"]
    extra_known = 0 if order == "only" else 20
    assert stock["shares"] == 10 + extra_known
    assert stock["cost_known_shares"] == known + extra_known
    assert stock["cost_unknown_shares"] == 10 - known
    assert stock["cost_basis_complete"] is (known == 10)
    expected_average = only_average if order == "only" else mixed_average
    if expected_average is None:
        assert stock["avg_cost"] is None
    else:
        assert stock["avg_cost"] == pytest.approx(expected_average)
    assert context["raw_selected_count"] == len(rows)
    assert shared["all_accounts"]["stocks_by_symbol"]["NVDA"]["shares"] == stock["shares"] + 7


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-Infinity"])
def test_holdings_cli_skips_nonfinite_stock_quantity_before_integer_conversion(tmp_path, monkeypatch, quantity):
    rows = [
        _rec(broker="富途", account="qa768", asset_type="us_stock", asset_id="NVDA",
             quantity=quantity, avg_cost="12.5", currency="USD"),
        _rec(broker="富途", account="qa768", asset_type="us_stock", asset_id="AAPL",
             quantity="20", avg_cost="25", currency="USD"),
    ]
    context, _shared = _run_isolated_holdings_cli(tmp_path, monkeypatch, rows)
    assert set(context["stocks_by_symbol"]) == {"AAPL"}
    stock = context["stocks_by_symbol"]["AAPL"]
    assert stock["shares"] == stock["cost_known_shares"] == 20
    assert stock["cost_unknown_shares"] == 0
    assert stock["avg_cost"] == 25
    assert stock["cost_basis_complete"] is True


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


def test_feishu_record_update_time_is_not_holdings_observation() -> None:
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

    assert first["source_observed_at"] is None
    assert second["source_observed_at"] is None
    assert first["source_observation_status"] == "unknown"
    assert first["source_observation_basis"] == "record_observation_missing"
    assert first["retrieved_at_utc"]


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
