from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from domain.domain.option_position_lots import BUY_TO_CLOSE, EXPIRE_AUTO_CLOSE, parse_exp_to_ms
from domain.domain.fee_calc import FUTU_US_FEE_SCHEDULE_URL, calc_futu_hk_stock_fee
import src.application.ledger.interventions as ledger_interventions
import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository
from src.application.positions.reporting import build_monthly_income_report


def _ms(date: str) -> int:
    out = parse_exp_to_ms(date)
    assert out is not None
    return out


def _assert_contains(row: dict[str, Any], expected: dict[str, Any]) -> None:
    assert {key: row.get(key) for key in expected} == expected


def test_build_monthly_income_report_groups_by_month_account_currency() -> None:
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "0700.HK",
                "side": "short",
                "status": "close",
                "contracts": 2,
                "contracts_closed": 2,
                "currency": "HKD",
                "premium": 3.93,
                "close_price": 1.2,
                "close_type": BUY_TO_CLOSE,
                "opened_at": _ms("2026-04-02"),
                "closed_at": _ms("2026-04-20"),
                "note": "multiplier=100",
            },
        },
        {
            "record_id": "rec_2",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "side": "short",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 2.5,
                "multiplier": 100,
                "close_type": EXPIRE_AUTO_CLOSE,
                "opened_at": _ms("2026-04-15"),
                "closed_at": _ms("2026-05-01"),
            },
        },
    ]

    report = build_monthly_income_report(
        records,
        account="lx",
        broker="富途",
        month="2026-04",
        rates={"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}},
        trade_events=[
            _trade_event(
                "open-hk",
                side="sell",
                position_effect="open",
                price=3.93,
                trade_date="2026-04-02",
                symbol="0700.HK",
                currency="HKD",
                contracts=2,
                strike=510,
            ),
            _trade_event(
                "close-hk",
                side="buy",
                position_effect="close",
                price=1.2,
                trade_date="2026-04-20",
                symbol="0700.HK",
                currency="HKD",
                contracts=2,
                strike=510,
            ),
            _trade_event("open-us", side="sell", position_effect="open", price=2.5, trade_date="2026-04-15"),
            _trade_event(
                "expire-us",
                side="buy",
                position_effect="close",
                price=0.0,
                trade_date="2026-05-01",
                raw_payload={"close_type": EXPIRE_AUTO_CLOSE},
            ),
        ],
    )

    assert report["warnings"] == []
    assert len(report["rows"]) == 1
    assert len(report["premium_rows"]) == 2
    assert len(report["summary"]) == 2
    _assert_contains(
        report["summary"][0],
        {
            "month": "2026-04",
            "account": "lx",
            "currency": "HKD",
            "realized_gross": 546.0,
            "realized_pnl_gross": 546.0,
            "realized_gross_cny": 502.32,
            "closed_contracts": 2,
            "positions": 1,
            "premium_received_gross": 786.0,
            "premium_received_gross_cny": 723.12,
            "cash_out_gross": 240.0,
            "net_cashflow_gross": 546.0,
            "open_basis_lifecycle_pnl_gross": 546.0,
            "premium_contracts": 2,
            "premium_positions": 1,
        },
    )
    _assert_contains(
        report["summary"][1],
        {
            "month": "2026-04",
            "account": "lx",
            "currency": "USD",
            "realized_gross": 0.0,
            "realized_pnl_gross": 0.0,
            "closed_contracts": 0,
            "positions": 0,
            "premium_received_gross": 250.0,
            "premium_received_gross_cny": 1800.0,
            "net_cashflow_gross": 250.0,
            "open_basis_lifecycle_pnl_gross": 250.0,
            "premium_contracts": 1,
            "premium_positions": 1,
        },
    )


def test_build_monthly_income_report_leaves_cny_fields_empty_without_rates() -> None:
    records = [
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "side": "short",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 2.5,
                "close_price": 1.0,
                "multiplier": 100,
                "close_type": BUY_TO_CLOSE,
                "opened_at": _ms("2026-04-02"),
                "closed_at": _ms("2026-04-20"),
            },
        },
    ]

    report = build_monthly_income_report(
        records,
        account="lx",
        broker="富途",
        month="2026-04",
        trade_events=[
            _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-02"),
            _trade_event("close-short", side="buy", position_effect="close", price=1.0, trade_date="2026-04-20"),
        ],
    )

    assert len(report["summary"]) == 1
    _assert_contains(
        report["summary"][0],
        {
            "month": "2026-04",
            "account": "lx",
            "currency": "USD",
            "realized_gross": 150.0,
            "realized_pnl_gross": 150.0,
            "realized_gross_cny": None,
            "closed_contracts": 1,
            "positions": 1,
            "premium_received_gross": 250.0,
            "premium_received_gross_cny": None,
            "cash_out_gross": 100.0,
            "net_cashflow_gross": 150.0,
            "open_basis_lifecycle_pnl_gross": 150.0,
            "premium_contracts": 1,
            "premium_positions": 1,
        },
    )


def test_monthly_income_return_summary_uses_account_cash_secured_and_long_call_cost() -> None:
    records = [
        {
            "record_id": "hk_put",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "0700.HK",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "HKD",
                "premium": 2.0,
                "multiplier": 100,
                "cash_secured_amount": 20000,
                "opened_at": _ms("2026-05-02"),
            },
        },
        {
            "record_id": "us_put",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "USD",
                "premium": 3.0,
                "multiplier": 100,
                "cash_secured_amount": 10000,
                "opened_at": _ms("2026-05-03"),
            },
        },
        {
            "record_id": "long_call",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "call",
                "side": "long",
                "status": "open",
                "contracts": 1,
                "currency": "USD",
                "premium": 1.0,
                "multiplier": 100,
                "opened_at": _ms("2026-05-03"),
            },
        },
    ]

    report = build_monthly_income_report(
        records,
        account="lx",
        broker="富途",
        month="2026-05",
        rates={"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}},
        now_fn=lambda: date(2026, 5, 19),
        trade_events=[
            _trade_event(
                "open-hk-put",
                side="sell",
                position_effect="open",
                price=2.0,
                trade_date="2026-05-02",
                symbol="0700.HK",
                currency="HKD",
            ),
            _trade_event("open-us-put", side="sell", position_effect="open", price=3.0, trade_date="2026-05-03"),
            _trade_event(
                "open-long-call",
                side="buy",
                position_effect="open",
                option_type="call",
                price=1.0,
                trade_date="2026-05-03",
            ),
        ],
    )

    assert report["warnings"] == []
    assert len(report["return_summary"]) == 1
    row = report["return_summary"][0]
    assert row["cash_secured_by_ccy"] == {"HKD": 20000.0, "USD": 10000.0}
    assert row["cash_secured_cny"] == 90400.0
    assert row["net_income_by_ccy"] == {"HKD": 200.0, "USD": 200.0}
    assert row["net_income_cny"] == 1624.0
    assert row["premium_income_by_ccy"] == {"HKD": 200.0, "USD": 300.0}
    assert row["premium_income_cny"] == 2344.0
    assert row["net_return_rate"] == round(1624.0 / 90400.0, 6)
    assert row["premium_return_rate"] == round(2344.0 / 90400.0, 6)
    assert row["annualized_basis_days"] == 19
    assert row["annualized_net_return_rate"] == round(row["net_return_rate"] * 365 / 19, 6)
    assert row["return_basis"] == "current_cash_secured"


def test_monthly_income_return_summary_outputs_each_account_when_account_filter_omitted() -> None:
    records = [
        {
            "record_id": "lx_put",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "USD",
                "premium": 2.0,
                "multiplier": 100,
                "cash_secured_amount": 10000,
                "opened_at": _ms("2026-05-03"),
            },
        },
        {
            "record_id": "sy_put",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "symbol": "0700.HK",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "HKD",
                "premium": 3.0,
                "multiplier": 100,
                "cash_secured_amount": 20000,
                "opened_at": _ms("2026-05-04"),
            },
        },
    ]

    report = build_monthly_income_report(
        records,
        broker="富途",
        month="2026-05",
        rates={"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}},
        now_fn=lambda: date(2026, 5, 19),
        trade_events=[
            _trade_event("open-lx", side="sell", position_effect="open", price=2.0, trade_date="2026-05-03"),
            _trade_event(
                "open-sy",
                side="sell",
                position_effect="open",
                price=3.0,
                trade_date="2026-05-04",
                account="sy",
                symbol="0700.HK",
                currency="HKD",
            ),
        ],
    )

    assert [(row["account"], row["cash_secured_cny"]) for row in report["return_summary"]] == [
        ("lx", 72000.0),
        ("sy", 18400.0),
    ]
    assert len(report["combined_return_summary"]) == 1
    combined = report["combined_return_summary"][0]
    assert combined["account_scope"] == "all"
    assert combined["accounts"] == ["lx", "sy"]
    assert combined["cash_secured_cny"] == 90400.0
    assert combined["net_income_cny"] == 1716.0
    assert combined["premium_income_cny"] == 1716.0
    assert combined["net_return_rate"] == round(1716.0 / 90400.0, 6)
    assert (
        combined["calculation_method"]
        == "sum(income_cashflow_ex_assignment_stock_cny) / sum(current_open_cash_secured_cny)"
    )


def test_monthly_income_return_summary_warns_when_exchange_rate_missing() -> None:
    records = [
        {
            "record_id": "lx_put",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "USD",
                "premium": 2.0,
                "multiplier": 100,
                "cash_secured_amount": 10000,
                "opened_at": _ms("2026-05-03"),
            },
        },
    ]

    report = build_monthly_income_report(
        records,
        account="lx",
        broker="富途",
        month="2026-05",
        trade_events=[
            _trade_event("open-lx", side="sell", position_effect="open", price=2.0, trade_date="2026-05-03"),
        ],
    )

    row = report["return_summary"][0]
    assert row["cash_secured_cny"] is None
    assert row["net_income_cny"] is None
    assert row["net_return_rate"] is None
    assert any("missing CNY exchange rate" in item for item in report["warnings"])


def test_monthly_income_diagnostics_marks_calculable_summary_ok() -> None:
    records = [
        {
            "record_id": "lx_put",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "USD",
                "premium": 2.0,
                "multiplier": 100,
                "cash_secured_amount": 10000,
                "opened_at": _ms("2026-05-03"),
            },
        },
    ]

    report = build_monthly_income_report(
        records,
        account="lx",
        broker="富途",
        month="2026-05",
        rates={"rates": {"USDCNY": 7.2}},
        trade_events=[
            _trade_event("open-lx", side="sell", position_effect="open", price=2.0, trade_date="2026-05-03"),
        ],
    )

    diag = report["diagnostics"][0]
    assert diag["status"] == "ok"
    assert diag["income_record_status"] == "recorded"
    assert diag["income_amount_status"] == "reported"
    assert diag["account"] == "lx"
    assert diag["month_range"] == {"month": "2026-05", "start": "2026-05-01", "end": "2026-05-31"}
    assert diag["position_lot_snapshots_count"] == 1
    assert diag["position_lot_snapshots_count"] == 1
    assert diag["premium_rows_count"] == 1
    assert diag["cash_secured_collateral_status"] == "reported"
    assert "income_rows" not in diag["missing_fields"]
    assert "cash_secured" not in diag["missing_fields"]


def test_monthly_income_diagnostics_explains_open_positions_without_month_income() -> None:
    records = [
        {
            "record_id": "sy_put",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "symbol": "0700.HK",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "HKD",
                "premium": 3.0,
                "multiplier": 100,
                "cash_secured_amount": 20000,
                "opened_at": _ms("2026-04-03"),
            },
        }
    ]

    report = build_monthly_income_report(
        records,
        account="sy",
        broker="富途",
        month="2026-05",
        rates={"rates": {"HKDCNY": 0.92}},
    )

    assert report["summary"] == []
    assert report["return_summary"] == []
    diag = report["diagnostics"][0]
    assert diag["status"] == "empty"
    assert diag["income_record_status"] == "no_recorded_rows"
    assert diag["income_amount_status"] == "not_reported"
    assert diag["position_lot_snapshots_count"] == 1
    assert diag["position_lot_snapshots_count"] == 1
    assert diag["closed_lots_count"] == 0
    assert diag["premium_rows_count"] == 0
    assert diag["cash_secured_collateral_status"] == "reported"
    assert {"income_rows", "closed_lots", "premium"}.issubset(set(diag["missing_fields"]))


def test_monthly_income_diagnostics_exposes_missing_cash_secured_and_conversion() -> None:
    records = [
        {
            "record_id": "sy_put",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "USD",
                "premium": 2.0,
                "multiplier": 100,
                "opened_at": _ms("2026-05-03"),
            },
        }
    ]

    report = build_monthly_income_report(
        records,
        account="sy",
        broker="富途",
        month="2026-05",
        trade_events=[
            _trade_event(
                "open-sy",
                side="sell",
                position_effect="open",
                price=2.0,
                trade_date="2026-05-03",
                account="sy",
            ),
        ],
    )

    diag = report["diagnostics"][0]
    assert diag["status"] == "incomplete"
    assert diag["position_lot_snapshots_count"] == 1
    assert diag["premium_rows_count"] == 1
    assert "cash_secured" not in diag["missing_fields"]
    assert "currency_conversion" in diag["missing_fields"]


def test_monthly_income_diagnostics_distinguishes_cash_secured_conversion_missing() -> None:
    records = [
        {
            "record_id": "lx_hk_put",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "9992.HK",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "HKD",
                "premium": 237.35,
                "multiplier": 100,
                "cash_secured_amount": 377500,
                "opened_at": _ms("2026-05-03"),
            },
        },
        {
            "record_id": "lx_us_put",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "USD",
                "premium": 24.0,
                "multiplier": 100,
                "cash_secured_amount": 29745,
                "opened_at": _ms("2026-05-03"),
            },
        },
    ]

    report = build_monthly_income_report(
        records,
        account="lx",
        broker="富途",
        month="2026-05",
        trade_events=[
            _trade_event(
                "open-hk",
                side="sell",
                position_effect="open",
                price=237.35,
                trade_date="2026-05-03",
                symbol="9992.HK",
                currency="HKD",
            ),
            _trade_event("open-us", side="sell", position_effect="open", price=24.0, trade_date="2026-05-03"),
        ],
    )

    row = report["return_summary"][0]
    assert row["cash_secured_by_ccy"] == {"HKD": 377500.0, "USD": 29745.0}
    assert row["premium_income_by_ccy"] == {"HKD": 23735.0, "USD": 2400.0}
    assert row["cash_secured_cny"] is None
    assert row["premium_income_cny"] is None
    assert row["premium_return_rate_by_ccy"] == {"HKD": 0.062874, "USD": 0.080686}

    diag = report["diagnostics"][0]
    assert diag["status"] == "incomplete"
    assert diag["closed_lots_count"] == 0
    assert diag["premium_rows_count"] == 2
    assert diag["cash_secured_collateral_status"] == "reported"
    assert diag["cash_secured_collateral_conversion_missing"] is True
    assert diag["currency_conversion_missing"] is True
    assert diag["missing_cny_currencies"] == ["HKD", "USD"]
    assert "cash_secured" not in diag["missing_fields"]
    assert "closed_lots" not in diag["missing_fields"]
    assert "currency_conversion" in diag["missing_fields"]


def test_read_model_monthly_income_report_uses_canonical_lot_records() -> None:
    import src.application.ledger.read_model as read_model

    class _Repo:
        def list_trade_events(self) -> list[dict[str, Any]]:
            return [
                _trade_event(
                    "open-hk",
                    side="sell",
                    position_effect="open",
                    price=3.93,
                    trade_date="2026-04-02",
                    symbol="0700.HK",
                    currency="HKD",
                    contracts=2,
                ),
                _trade_event(
                    "close-hk",
                    side="buy",
                    position_effect="close",
                    price=1.2,
                    trade_date="2026-04-20",
                    symbol="0700.HK",
                    currency="HKD",
                    contracts=2,
                ),
            ]

        def list_position_lots(self) -> list[dict[str, Any]]:
            return [
                {
                    "record_id": "rec_1",
                    "fields": {
                        "broker": "富途证券（香港）",
                        "account": "LX",
                        "symbol": "0700.HK",
                        "side": "short",
                        "status": "close",
                        "contracts": 2,
                        "contracts_closed": 2,
                        "currency": "港币",
                        "premium": 3.93,
                        "close_price": 1.2,
                        "close_type": BUY_TO_CLOSE,
                        "opened_at": _ms("2026-04-02"),
                        "closed_at": _ms("2026-04-20"),
                        "note": "multiplier=100",
                    },
                }
            ]

    with TemporaryDirectory() as td:
        base = Path(td)
        original = read_model.get_exchange_rates_or_fetch_latest
        read_model.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {"rates": {"HKDCNY": 0.92}}
        try:
            report = read_model.build_position_monthly_income_report(
                _Repo(),
                base=base,
                broker="富途",
                account="lx",
                month="2026-04",
            )
        finally:
            read_model.get_exchange_rates_or_fetch_latest = original

    assert report["warnings"] == []
    assert report["summary"][0]["account"] == "lx"
    assert report["summary"][0]["currency"] == "HKD"
    assert report["summary"][0]["realized_gross"] == 546.0


def test_build_monthly_income_report_skips_market_only_rows_for_broker_filter() -> None:
    report = build_monthly_income_report(
        [
            {
                "record_id": "rec_market_only",
                "fields": {
                    "market": "富途",
                    "account": "lx",
                    "symbol": "NVDA",
                    "side": "short",
                    "status": "close",
                    "contracts": 1,
                    "contracts_closed": 1,
                    "currency": "USD",
                    "premium": 2.5,
                    "multiplier": 100,
                    "close_price": 1.0,
                    "close_type": BUY_TO_CLOSE,
                    "opened_at": _ms("2026-04-02"),
                    "closed_at": _ms("2026-04-20"),
                },
            }
        ],
        account="lx",
        broker="富途",
        month="2026-04",
    )

    assert report["rows"] == []
    assert report["premium_rows"] == []
    assert report["summary"] == []
    assert report["warnings"] == []


def test_monthly_income_report_excludes_voided_open_event_projection(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    open_result = ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=_ms("2026-04-03"),
        ),
    )
    ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(open_result.event_id),
        void_reason="opened_by_mistake",
        as_of_ms=_ms("2026-04-04"),
    )

    report = build_monthly_income_report(
        repo.list_records(page_size=500),
        account="lx",
        broker="富途",
        month="2026-04",
        trade_events=repo.list_trade_events(),
    )

    assert report["rows"] == []
    assert report["premium_rows"] == []
    assert report["summary"] == []
    assert report["warnings"] == []


def test_monthly_income_report_ignores_invalid_void_when_filtering_active_events() -> None:
    report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        month="2026-04",
        rates={"rates": {"USDCNY": 7.2}},
        trade_events=[
            _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
            _trade_event(
                "invalid-void-open",
                side="",
                position_effect="void",
                price=0.0,
                trade_date="2026-04-04",
                raw_payload={"void_target_event_id": "open-short"},
            ),
        ],
    )

    assert report["warnings"] == []
    assert len(report["premium_rows"]) == 1
    assert report["premium_rows"][0]["event_id"] == "open-short"
    assert len(report["summary"]) == 1
    _assert_contains(
        report["summary"][0],
        {
            "month": "2026-04",
            "account": "lx",
            "currency": "USD",
            "premium_received_gross": 250.0,
            "premium_received_gross_cny": 1800.0,
            "net_cashflow_gross": 250.0,
            "premium_contracts": 1,
            "premium_positions": 1,
        },
    )


def test_monthly_income_report_excludes_voided_close_event_but_keeps_open_premium(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=_ms("2026-04-03"),
        ),
    )
    lot = repo.list_position_lots()[0]
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.0,
        close_reason="manual_buy_to_close",
        as_of_ms=_ms("2026-04-20"),
    )
    ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(close_result.event_id),
        void_reason="close_recorded_by_mistake",
        as_of_ms=_ms("2026-04-21"),
    )

    report = build_monthly_income_report(
        repo.list_records(page_size=500),
        account="lx",
        broker="富途",
        month="2026-04",
        trade_events=repo.list_trade_events(),
    )

    assert report["rows"] == []
    assert len(report["premium_rows"]) == 1
    assert report["premium_rows"][0]["premium_received_gross"] == 250.0
    assert len(report["summary"]) == 1
    _assert_contains(
        report["summary"][0],
        {
            "month": "2026-04",
            "account": "lx",
            "currency": "USD",
            "realized_gross": 0.0,
            "realized_pnl_gross": 0.0,
            "closed_contracts": 0,
            "positions": 0,
            "premium_received_gross": 250.0,
            "premium_received_gross_cny": None,
            "net_cashflow_gross": 250.0,
            "premium_contracts": 1,
            "premium_positions": 1,
        },
    )
    assert any("missing CNY exchange rate" in item for item in report["warnings"])


def test_monthly_income_report_uses_adjusted_premium_and_opened_at(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=_ms("2026-04-03"),
        ),
    )
    lot = repo.list_position_lots()[0]
    ledger_manual_trades.persist_manual_adjust_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        premium_per_share=3.1,
        opened_at_ms=_ms("2026-05-02"),
        as_of_ms=_ms("2026-05-03"),
    )

    april_report = build_monthly_income_report(
        repo.list_records(page_size=500),
        account="lx",
        broker="富途",
        month="2026-04",
        trade_events=repo.list_trade_events(),
    )
    may_report = build_monthly_income_report(
        repo.list_records(page_size=500),
        account="lx",
        broker="富途",
        month="2026-05",
        trade_events=repo.list_trade_events(),
    )

    assert april_report["premium_rows"] == []
    assert may_report["premium_rows"][0]["premium_received_gross"] == 310.0
    assert len(may_report["summary"]) == 1
    _assert_contains(
        may_report["summary"][0],
        {
            "month": "2026-05",
            "account": "lx",
            "currency": "USD",
            "realized_gross": 0.0,
            "realized_pnl_gross": 0.0,
            "closed_contracts": 0,
            "positions": 0,
            "premium_received_gross": 310.0,
            "premium_received_gross_cny": None,
            "net_cashflow_gross": 310.0,
            "premium_contracts": 1,
            "premium_positions": 1,
        },
    )


def test_monthly_income_report_event_long_call_cashflow_and_realized_are_separate() -> None:
    events = [
        _trade_event(
            "open-long-call",
            side="buy",
            position_effect="open",
            option_type="call",
            price=1.2,
            trade_date="2026-04-03",
        ),
        _trade_event(
            "close-long-call",
            side="sell",
            position_effect="close",
            option_type="call",
            price=2.0,
            trade_date="2026-05-01",
        ),
    ]

    april = build_monthly_income_report([], account="lx", broker="富途", month="2026-04", trade_events=events)
    may = build_monthly_income_report([], account="lx", broker="富途", month="2026-05", trade_events=events)

    _assert_contains(
        april["summary"][0],
        {
            "net_cashflow_gross": -120.0,
            "cash_out_gross": 120.0,
            "realized_pnl_gross": 0.0,
            "open_basis_lifecycle_pnl_gross": 80.0,
            "premium_received_gross": 0.0,
        },
    )
    _assert_contains(
        may["summary"][0],
        {
            "net_cashflow_gross": 200.0,
            "cash_in_gross": 200.0,
            "realized_pnl_gross": 80.0,
            "realized_long_pnl_gross": 80.0,
            "premium_received_gross": 0.0,
        },
    )
    assert april["open_basis_rows"][0]["enhancement_call_buy_cost"] == 120.0
    assert april["open_basis_rows"][0]["enhancement_call_sell_proceeds_actual"] == 200.0


def _trade_event(
    event_id: str,
    *,
    side: str,
    position_effect: str,
    price: float,
    trade_date: str,
    account: str = "lx",
    broker: str = "富途",
    symbol: str = "NVDA",
    currency: str = "USD",
    option_type: str = "put",
    contracts: int = 1,
    multiplier: int = 100,
    strike: float = 100.0,
    expiration_ymd: str = "2026-06-19",
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "source_type": "manual_trade_event",
        "source_name": "test",
        "broker": broker,
        "account": account,
        "symbol": symbol,
        "option_type": option_type,
        "side": side,
        "position_effect": position_effect,
        "contracts": contracts,
        "price": price,
        "strike": strike,
        "multiplier": multiplier,
        "expiration_ymd": expiration_ymd,
        "currency": currency,
        "trade_time_ms": _ms(trade_date),
        "order_id": None,
        "multiplier_source": "payload",
        "raw_payload": raw_payload or {},
    }


def test_monthly_income_report_event_cashflow_and_realized_are_separate_across_months() -> None:
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event("close-short", side="buy", position_effect="close", price=1.0, trade_date="2026-05-01"),
    ]

    april = build_monthly_income_report([], account="lx", broker="富途", month="2026-04", trade_events=events)
    may = build_monthly_income_report([], account="lx", broker="富途", month="2026-05", trade_events=events)

    _assert_contains(
        april["summary"][0],
        {
            "net_cashflow_gross": 250.0,
            "realized_pnl_gross": 0.0,
            "open_basis_lifecycle_pnl_gross": 150.0,
            "premium_received_gross": 250.0,
            "realized_gross": 0.0,
        },
    )
    _assert_contains(
        may["summary"][0],
        {
            "net_cashflow_gross": -100.0,
            "realized_pnl_gross": 150.0,
            "realized_short_pnl_gross": 150.0,
            "premium_received_gross": 0.0,
            "realized_gross": 150.0,
        },
    )


def test_monthly_income_report_assignment_includes_stock_settlement_cashflow() -> None:
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-short-put",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0},
            },
        ),
    ]

    april = build_monthly_income_report([], account="lx", broker="富途", month="2026-04", trade_events=events)
    may = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        month="2026-05",
        rates={"USDCNY": 7.2},
        trade_events=events,
    )

    _assert_contains(
        april["summary"][0],
        {
            "net_cashflow_gross": 250.0,
            "realized_pnl_gross": 0.0,
            "open_basis_lifecycle_pnl_gross": 250.0,
            "premium_received_gross": 250.0,
        },
    )
    _assert_contains(
        may["summary"][0],
        {
            "net_cashflow_gross": -10000.0,
            "cash_out_gross": 10000.0,
            "realized_pnl_gross": 250.0,
            "realized_short_pnl_gross": 250.0,
            "assignment_stock_net_cashflow_gross": -10000.0,
            "assignment_stock_cash_out_gross": 10000.0,
            "assignment_stock_shares_bought": 100,
            "assignment_stock_shares_sold": 0,
            "premium_received_gross": 0.0,
        },
    )
    assert may["rows"][0]["close_type"] == "assignment"
    assert may["rows"][0]["realized_gross"] == 250.0
    assert may["return_summary"][0]["net_income_by_ccy"] == {"USD": 0.0}
    assert may["return_summary"][0]["net_income_cny"] == 0.0
    assert may["return_summary"][0]["realized_pnl_by_ccy"] == {"USD": 250.0}
    assert may["return_summary"][0]["realized_pnl_cny"] == 1800.0
    assert may["stock_settlement_rows"] == [
        {
            "event_id": "assign-short-put",
            "event_at": _ms("2026-05-01"),
            "month": "2026-05",
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "position_side": "short",
            "trade_action": "assignment_stock_buy",
            "currency": "USD",
            "contracts": 1,
            "shares": 100,
            "price": 100.0,
            "fees": 0.0,
            "multiplier": 100,
            "strike": 100.0,
            "expiration_ymd": "2026-06-19",
            "cash_in_gross": 0.0,
            "cash_out_gross": 10000.0,
            "net_cashflow_gross": -10000.0,
            "strategy": "",
            "leg_role": "",
            "strategy_group_id": "",
            "close_type": "assignment",
        }
    ]
    assert [row["trade_action"] for row in may["cashflow_rows"]] == [
        "assignment_option_close",
        "assignment_stock_buy",
    ]


def test_monthly_income_report_assignment_lifecycle_marks_open_assigned_stock() -> None:
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-short-put",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0},
            },
        ),
    ]

    report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        month="2026-05",
        trade_events=events,
        quote_snapshots=[
            {
                "symbol": "NVDA",
                "spot": 98.0,
                "quote_time_ms": _ms("2026-05-02"),
                "quote_source": "manual_snapshot",
                "quote_status": "fresh",
            }
        ],
        as_of_ms=_ms("2026-05-02"),
    )

    assert report["assigned_stock_review_rows"] == []
    row = report["assignment_lifecycle_rows"][0]
    _assert_contains(
        row,
        {
            "stock_lot_id": "assigned-stock-assign-short-put",
            "status": "open",
            "review_status": "ready",
            "shares_opened": 100,
            "shares_remaining": 100,
            "assignment_price": 100.0,
            "stock_cost_per_share": 100.0,
            "stock_cost_basis_total": 10000.0,
            "remaining_stock_cost_basis": 10000.0,
            "spot": 98.0,
            "quote_status": "fresh",
            "remaining_market_value": 9800.0,
            "assigned_stock_unrealized_pnl": -200.0,
            "assigned_stock_realized_pnl": 0.0,
            "option_premium_attribution": 250.0,
            "assignment_lifecycle_pnl": 50.0,
        },
    )
    assert "effective_cost_per_share" not in row


def test_monthly_income_report_assignment_lifecycle_marks_sold_assigned_stock() -> None:
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-short-put",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0},
            },
        ),
    ]

    report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        month="2026-06",
        trade_events=events,
        assigned_stock_events=[
            {
                "event_type": "sale",
                "stock_event_id": "sale-1",
                "target_stock_lot_id": "assigned-stock-assign-short-put",
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "side": "sell",
                "shares": 100,
                "price": 105.0,
                "currency": "USD",
                "fees": 0.0,
                "trade_time_ms": _ms("2026-06-01"),
                "source": "manual",
            }
        ],
    )

    assert report["assigned_stock_review_rows"] == []
    row = report["assignment_lifecycle_rows"][0]
    _assert_contains(
        row,
        {
            "stock_lot_id": "assigned-stock-assign-short-put",
            "status": "closed",
            "review_status": "ready",
            "shares_opened": 100,
            "shares_remaining": 0,
            "shares_sold": 100,
            "stock_cost_per_share": 100.0,
            "assigned_stock_realized_pnl": 497.4739,
            "assigned_stock_unrealized_pnl": None,
            "option_premium_attribution": 250.0,
            "assignment_lifecycle_pnl": 747.4739,
            "quote_status": "not_required",
        },
    )
    assert report["assigned_stock_sale_rows"] == [
        {
            "stock_event_id": "sale-1",
            "stock_lot_id": "assigned-stock-assign-short-put",
            "source_assignment_event_id": "assign-short-put",
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "currency": "USD",
            "month": "2026-06",
            "event_at": _ms("2026-06-01"),
            "shares": 100,
            "price": 105.0,
            "fees": 2.5261,
            "fee_basis": "estimated",
            "fee_source": FUTU_US_FEE_SCHEDULE_URL,
            "fee_reason": "standard_fixed_stock_fee_schedule_estimate",
            "cash_in_gross": 10500.0,
            "stock_sale_cash_in_net": 10497.4739,
            "stock_cost_basis_sold": 10000.0,
            "assigned_stock_realized_pnl": 497.4739,
            "source": "manual",
            "source_deal_id": None,
        }
    ]
    stock_sale_fee = next(item for item in row["fee_evidence"] if item["component"] == "stock_sale_fee")
    assert stock_sale_fee == {
        "component": "stock_sale_fee",
        "basis": "estimated",
        "amount": 2.5261,
        "source": FUTU_US_FEE_SCHEDULE_URL,
        "reason": "standard_fixed_stock_fee_schedule_estimate",
    }
    assignment_fee = next(item for item in row["fee_evidence"] if item["component"] == "assignment_stock_fee")
    assert assignment_fee["basis"] == "missing"
    assert assignment_fee["reason"] == "us_assignment_fee_rule_not_explicit"


def test_assignment_lifecycle_reports_dates_complete_fees_and_capital_days() -> None:
    noon_ms = 12 * 60 * 60 * 1000
    open_event = _trade_event(
        "open-short-fees",
        side="sell",
        position_effect="open",
        price=2.5,
        trade_date="2026-04-03",
        raw_payload={"fees": 2.0},
    )
    assignment_event = _trade_event(
        "assign-short-put-fees",
        side="buy",
        position_effect="close",
        price=0.0,
        trade_date="2026-05-01",
        raw_payload={
            "close_type": "assignment",
            "target_lot_id": "lot-open-short-fees",
            "fees": 0.0,
            "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0, "fees": 1.0},
        },
    )
    open_event["trade_time_ms"] += noon_ms
    assignment_event["trade_time_ms"] += noon_ms
    sale_at = _ms("2026-06-01") + noon_ms

    report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        trade_events=[open_event, assignment_event],
        assigned_stock_events=[
            {
                "event_type": "sale",
                "stock_event_id": "sale-fees",
                "target_stock_lot_id": "assigned-stock-assign-short-put-fees",
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "side": "sell",
                "shares": 100,
                "price": 105.0,
                "currency": "USD",
                "fees": 1.5,
                "trade_time_ms": sale_at,
            }
        ],
    )

    row = report["lifecycle_efficiency_rows"][0]
    assert row["assigned_at_ms"] == _ms("2026-05-01") + noon_ms
    assert row["assigned_date"] == "2026-05-01"
    assert row["inventory_end_at_ms"] == sale_at
    assert row["inventory_days"] == 31.0
    assert row["actual_fees"] == 4.5
    assert row["estimated_fees"] == 0.0
    assert row["fees_used"] == 4.5
    assert row["fee_basis"] == "actual"
    assert row["fee_missing_components"] == []
    assert {item["component"] for item in row["fee_evidence"]} == {
        "put_open_option_fee",
        "put_assignment_option_fee",
        "assignment_stock_fee",
        "stock_sale_fee",
    }
    assert row["put_capital_days"] == 280000.0
    assert row["stock_capital_days"] == 310031.0
    assert row["capital_days"] == 590031.0
    assert row["lifecycle_pnl_gross"] == 750.0
    assert row["lifecycle_pnl_net"] == 745.5
    assert row["annualized_capital_efficiency"] == round(745.5 * 365 / 590031, 8)
    assert row["lifecycle_quality"] == "complete_closed"


def test_hk_assignment_estimates_stock_settlement_fee_without_assignment_exercise_fee() -> None:
    events = [
        _trade_event(
            "open-hk-assignment-fee",
            side="sell",
            position_effect="open",
            price=2.5,
            trade_date="2026-04-03",
            symbol="0700.HK",
            currency="HKD",
        ),
        _trade_event(
            "assign-hk-assignment-fee",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            symbol="0700.HK",
            currency="HKD",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0},
            },
        ),
    ]

    row = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        trade_events=events,
    )["assignment_lifecycle_rows"][0]

    assignment_fee = next(item for item in row["fee_evidence"] if item["component"] == "assignment_stock_fee")
    assert assignment_fee["basis"] == "estimated"
    assert assignment_fee["amount"] == calc_futu_hk_stock_fee(100.0, shares=100, is_sell=False)
    assert assignment_fee["reason"] == "hk_assignment_stock_fee_excluding_assignment_exercise_fee"


def test_assignment_lifecycle_integrates_partial_stock_capital_days() -> None:
    events = [
        _trade_event("open-short-partial", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-short-put-partial",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0, "fees": 0.0},
            },
        ),
    ]
    report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        trade_events=events,
        assigned_stock_events=[
            {
                "event_type": "sale",
                "stock_event_id": "sale-partial",
                "target_stock_lot_id": "assigned-stock-assign-short-put-partial",
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "side": "sell",
                "shares": 40,
                "price": 105.0,
                "currency": "USD",
                "fees": 0.0,
                "trade_time_ms": _ms("2026-05-11"),
            }
        ],
        quote_snapshots=[{"symbol": "NVDA", "spot": 102.0, "quote_time_ms": _ms("2026-05-21")}],
        as_of_ms=_ms("2026-05-21"),
    )

    row = report["lifecycle_efficiency_rows"][0]
    assert row["inventory_days"] == 20.0
    assert row["stock_capital_days"] == 160000.0
    assert row["status"] == "partially_sold"
    assert row["lifecycle_quality"] == "open_marked"


def test_assignment_lifecycle_attributes_covered_call_fifo_and_rejects_mixed_inventory() -> None:
    events = [
        _trade_event(
            "open-call-before-assignment",
            side="sell",
            position_effect="open",
            price=1.0,
            trade_date="2026-04-01",
            option_type="call",
            strike=120.0,
        ),
        _trade_event(
            "close-call-before-assignment",
            side="buy",
            position_effect="close",
            price=0.5,
            trade_date="2026-04-02",
            option_type="call",
            strike=120.0,
        ),
        _trade_event("open-put-cc", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-put-cc",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0, "fees": 0.0},
            },
        ),
        _trade_event(
            "open-call-cc",
            side="sell",
            position_effect="open",
            price=2.0,
            trade_date="2026-05-05",
            option_type="call",
            strike=110.0,
        ),
        _trade_event(
            "close-call-cc",
            side="buy",
            position_effect="close",
            price=0.5,
            trade_date="2026-05-20",
            option_type="call",
            strike=110.0,
        ),
    ]
    sale = {
        "event_type": "sale",
        "stock_event_id": "sale-cc",
        "target_stock_lot_id": "assigned-stock-assign-put-cc",
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "side": "sell",
        "shares": 100,
        "price": 105.0,
        "currency": "USD",
        "fees": 0.0,
        "trade_time_ms": _ms("2026-06-01"),
    }

    fifo = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        trade_events=events,
        assigned_stock_events=[sale],
    )
    mixed = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        trade_events=events,
        assigned_stock_events=[sale],
        assigned_stock_holdings=[
            {"account": "lx", "broker": "富途", "symbol": "NVDA", "currency": "USD", "shares": 200}
        ],
    )

    fifo_row = fifo["lifecycle_efficiency_rows"][0]
    assert fifo_row["covered_call_pnl"] == 150.0
    assert fifo_row["covered_call_allocation_status"] == "derived_fifo"
    assert fifo_row["lifecycle_pnl_gross"] == 900.0
    assert not any(
        row.get("event_id") == "open-call-before-assignment"
        for row in fifo["assigned_stock_review_rows"]
    )
    mixed_row = mixed["lifecycle_efficiency_rows"][0]
    assert mixed_row["covered_call_pnl"] == 0.0
    assert any(row["status"] == "covered_call_unallocated" for row in mixed["assigned_stock_review_rows"])


def test_lifecycle_efficiency_summary_uses_weighted_capital_days() -> None:
    from src.application.positions.reporting import _lifecycle_efficiency_summary

    summary = _lifecycle_efficiency_summary(
        [
            {"account": "lx", "currency": "USD", "lifecycle_quality": "complete_closed", "lifecycle_pnl_net": 10.0, "capital_days": 100.0},
            {"account": "lx", "currency": "USD", "lifecycle_quality": "complete_closed", "lifecycle_pnl_net": 30.0, "capital_days": 900.0},
        ]
    )[0]

    assert summary["lifecycle_pnl_net"] == 40.0
    assert summary["capital_days"] == 1000.0
    assert summary["annualized_capital_efficiency"] == 14.6


def test_monthly_income_report_assignment_lifecycle_filters_assigned_stock_sales_by_account() -> None:
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-short-put",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0},
            },
        ),
    ]

    report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        month="2026-05",
        trade_events=events,
        assigned_stock_events=[
            {
                "event_type": "sale",
                "stock_event_id": "sale-other-account",
                "target_stock_lot_id": "assigned-stock-other-account",
                "account": "sy",
                "broker": "富途",
                "symbol": "NVDA",
                "side": "sell",
                "shares": 100,
                "price": 105.0,
                "currency": "USD",
                "trade_time_ms": _ms("2026-05-03"),
            }
        ],
        quote_snapshots=[{"symbol": "NVDA", "spot": 98.0}],
    )

    assert report["assigned_stock_sale_rows"] == []
    assert report["assigned_stock_review_rows"] == []


def test_monthly_income_report_assignment_lifecycle_missing_quote_is_incomplete() -> None:
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-short-put",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0},
            },
        ),
    ]

    report = build_monthly_income_report([], account="lx", broker="富途", month="2026-05", trade_events=events)

    row = report["assignment_lifecycle_rows"][0]
    assert row["review_status"] == "missing_quote"
    assert row["quote_status"] == "missing_quote"
    assert row["assigned_stock_unrealized_pnl"] is None
    assert row["assignment_lifecycle_pnl"] is None
    assert report["assigned_stock_review_rows"][0]["status"] == "missing_quote"


def test_monthly_income_report_assignment_lifecycle_missing_stock_settlement_requires_review() -> None:
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-short-put",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={"close_type": "assignment"},
        ),
    ]

    report = build_monthly_income_report([], account="lx", broker="富途", month="2026-05", trade_events=events)

    assert report["assigned_stock_lots"] == []
    assert report["assignment_lifecycle_rows"] == []
    assert report["assigned_stock_review_rows"][0]["status"] == "missing_stock_settlement"


def test_monthly_income_report_assignment_lifecycle_holdings_gap_does_not_auto_close_lot() -> None:
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event(
            "assign-short-put",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-05-01",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 100.0},
            },
        ),
    ]

    report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        month="2026-05",
        trade_events=events,
        quote_snapshots=[{"symbol": "NVDA", "spot": 98.0}],
        assigned_stock_holdings=[{"account": "lx", "broker": "富途", "symbol": "NVDA", "currency": "USD", "shares": 0}],
    )

    row = report["assignment_lifecycle_rows"][0]
    assert row["status"] == "open"
    assert row["shares_remaining"] == 100
    assert report["assigned_stock_review_rows"][0]["status"] == "missing_stock_sale"


def test_monthly_income_return_summary_handles_realized_month_without_new_open() -> None:
    records = [
        {
            "record_id": "current-open",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "MSFT",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 1,
                "currency": "USD",
                "premium": 1.0,
                "multiplier": 100,
                "cash_secured_amount": 20000,
                "opened_at": _ms("2026-05-02"),
            },
        }
    ]
    events = [
        _trade_event("open-short", side="sell", position_effect="open", price=2.5, trade_date="2026-04-03"),
        _trade_event("close-short", side="buy", position_effect="close", price=1.0, trade_date="2026-05-01"),
    ]

    may = build_monthly_income_report(
        records,
        account="lx",
        broker="富途",
        month="2026-05",
        rates={"rates": {"USDCNY": 7.2}},
        trade_events=events,
        now_fn=lambda: date(2026, 5, 19),
    )

    row = may["return_summary"][0]
    assert row["premium_income_cny"] == 0.0
    assert row["realized_pnl_cny"] == 1080.0
    assert row["net_income_cny"] == -720.0
    assert row["cash_secured_cny"] == 144000.0
    assert row["realized_return_rate"] == round(1080.0 / 144000.0, 6)


def test_monthly_income_report_event_long_call_realized_uses_close_minus_open() -> None:
    events = [
        _trade_event(
            "open-long-call",
            side="buy",
            position_effect="open",
            option_type="call",
            price=1.2,
            trade_date="2026-04-03",
        ),
        _trade_event(
            "close-long-call",
            side="sell",
            position_effect="close",
            option_type="call",
            price=2.0,
            trade_date="2026-05-01",
        ),
    ]

    may = build_monthly_income_report([], account="lx", broker="富途", month="2026-05", trade_events=events)

    assert may["rows"][0]["realized_gross"] == 80.0
    _assert_contains(
        may["summary"][0],
        {
            "net_cashflow_gross": 200.0,
            "realized_pnl_gross": 80.0,
            "realized_long_pnl_gross": 80.0,
            "realized_gross": 80.0,
        },
    )


def test_monthly_income_report_event_yield_enhancement_tracks_call_realized_and_open_basis() -> None:
    group = "ye-1"
    events = [
        _trade_event(
            "open-put",
            side="sell",
            position_effect="open",
            price=3.0,
            trade_date="2026-04-03",
            raw_payload={"strategy": "yield_enhancement", "strategy_group_id": group, "leg_role": "sell_put"},
        ),
        _trade_event(
            "open-call",
            side="buy",
            position_effect="open",
            option_type="call",
            price=1.2,
            trade_date="2026-04-03",
            raw_payload={"strategy": "yield_enhancement", "strategy_group_id": group, "leg_role": "enhancement_call"},
        ),
        _trade_event(
            "close-put",
            side="buy",
            position_effect="close",
            price=0.8,
            trade_date="2026-05-01",
            raw_payload={"strategy": "yield_enhancement", "strategy_group_id": group, "leg_role": "sell_put"},
        ),
        _trade_event(
            "close-call",
            side="sell",
            position_effect="close",
            option_type="call",
            price=2.0,
            trade_date="2026-05-01",
            raw_payload={"strategy": "yield_enhancement", "strategy_group_id": group, "leg_role": "enhancement_call"},
        ),
    ]

    april = build_monthly_income_report([], account="lx", broker="富途", month="2026-04", trade_events=events)
    may = build_monthly_income_report([], account="lx", broker="富途", month="2026-05", trade_events=events)

    _assert_contains(
        april["open_basis_rows"][0],
        {
            "sell_open_premium": 300.0,
            "sell_close_cost_actual": 80.0,
            "enhancement_call_buy_cost": 120.0,
            "enhancement_call_sell_proceeds_actual": 200.0,
            "open_basis_lifecycle_pnl_gross": 300.0,
            "is_final": True,
        },
    )
    _assert_contains(
        may["summary"][0],
        {
            "net_cashflow_gross": 120.0,
            "realized_pnl_gross": 300.0,
            "yield_enhancement_realized_pnl_gross": 80.0,
            "realized_gross": 300.0,
        },
    )
    assert may["enhancement_rows"][0]["realized_pnl_gross"] == 80.0


def _combo_snapshot(group_id: str, leg_role: str) -> dict[str, Any]:
    return {
        "strategy": "combo_yield",
        "leg_role": leg_role,
        "strategy_group_id": group_id,
        "expiry_structure": "diagonal",
        "yield_enhancement_mode": "income_upside",
    }


def test_full_group_lifecycle_reports_partial_assignment_with_residual_call() -> None:
    group_id = "combo_yield:lx:combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP"
    events = [
        _trade_event(
            "put-open",
            side="sell",
            position_effect="open",
            price=1.5,
            trade_date="2026-07-01",
            symbol="PDD",
            contracts=2,
            strike=80.0,
            expiration_ymd="2026-08-21",
            raw_payload={"strategy_snapshot": _combo_snapshot(group_id, "sell_put")},
        ),
        _trade_event(
            "call-open",
            side="buy",
            position_effect="open",
            price=1.0,
            trade_date="2026-07-01",
            symbol="PDD",
            option_type="call",
            contracts=2,
            strike=100.0,
            expiration_ymd="2026-09-18",
            raw_payload={"strategy_snapshot": _combo_snapshot(group_id, "enhancement_call")},
        ),
        _trade_event(
            "put-assign-one",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-08-22",
            symbol="PDD",
            contracts=1,
            strike=80.0,
            expiration_ymd="2026-08-21",
            raw_payload={
                "close_type": "assignment",
                "stock_settlement": {"side": "buy", "shares": 100, "price": 80.0},
                "strategy_snapshot": _combo_snapshot(group_id, "sell_put"),
            },
        ),
    ]

    report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        trade_events=events,
        quote_snapshots=[{"symbol": "PDD", "spot": 82.0, "quote_time_ms": _ms("2026-08-23")}],
        as_of_ms=_ms("2026-08-23"),
    )

    stock_lot = report["assigned_stock_lots"][0]
    assert stock_lot["strategy_group_id"] == group_id
    assert stock_lot["strategy_snapshot"]["expiry_structure"] == "diagonal"
    lifecycle = report["full_group_lifecycle"][0]
    assert lifecycle["summary_classification"] == "assigned_stock_with_residual_call"
    assert lifecycle["put_contracts_open"] == 1
    assert lifecycle["call_contracts_open"] == 2
    assert lifecycle["residual_call_contracts"] == 1
    assert lifecycle["assigned_shares_remaining"] == 100
    assert lifecycle["lifecycle_issues"] == []

    sold_report = build_monthly_income_report(
        [],
        account="lx",
        broker="富途",
        trade_events=events,
        assigned_stock_events=[
            {
                "event_type": "sale",
                "stock_event_id": "stock-sale",
                "target_stock_lot_id": stock_lot["stock_lot_id"],
                "account": "lx",
                "broker": "富途",
                "symbol": "PDD",
                "side": "sell",
                "shares": 100,
                "price": 85.0,
                "currency": "USD",
                "fees": 0.0,
                "trade_time_ms": _ms("2026-08-24"),
            }
        ],
    )
    sold_lifecycle = sold_report["full_group_lifecycle"][0]
    assert sold_lifecycle["summary_classification"] == "review_required"
    assert sold_lifecycle["assigned_shares_sold"] == 100
    assert sold_lifecycle["assignment_event_ids"] == ["put-assign-one"]
    assert sold_report["assigned_stock_sale_rows"][0]["strategy_group_id"] == group_id
    assert sold_report["assigned_stock_sale_rows"][0]["strategy_snapshot"]["expiry_structure"] == "diagonal"


def test_full_group_lifecycle_reports_normal_put_close_as_residual_call() -> None:
    group_id = "combo_yield:lx:normal-close"
    events = [
        _trade_event(
            "put-open-normal",
            side="sell",
            position_effect="open",
            price=1.5,
            trade_date="2026-07-01",
            symbol="PDD",
            strike=80.0,
            expiration_ymd="2026-08-21",
            raw_payload={"strategy_snapshot": _combo_snapshot(group_id, "sell_put")},
        ),
        _trade_event(
            "call-open-normal",
            side="buy",
            position_effect="open",
            price=1.0,
            trade_date="2026-07-01",
            symbol="PDD",
            option_type="call",
            strike=100.0,
            expiration_ymd="2026-09-18",
            raw_payload={"strategy_snapshot": _combo_snapshot(group_id, "enhancement_call")},
        ),
        _trade_event(
            "put-close-normal",
            side="buy",
            position_effect="close",
            price=0.2,
            trade_date="2026-08-01",
            symbol="PDD",
            strike=80.0,
            expiration_ymd="2026-08-21",
            raw_payload={"strategy_snapshot": _combo_snapshot(group_id, "sell_put")},
        ),
    ]

    report = build_monthly_income_report([], account="lx", broker="富途", trade_events=events)

    lifecycle = report["full_group_lifecycle"][0]
    assert lifecycle["summary_classification"] == "residual_call"
    assert lifecycle["assigned_shares_opened"] == 0
    assert lifecycle["residual_call_contracts"] == 1


def test_full_group_lifecycle_reports_put_expiry_as_residual_call() -> None:
    group_id = "combo_yield:lx:expiry-residual"
    events = [
        _trade_event(
            "put-open-expiry",
            side="sell",
            position_effect="open",
            price=1.5,
            trade_date="2026-07-01",
            symbol="PDD",
            strike=80.0,
            expiration_ymd="2026-08-21",
            raw_payload={"strategy_snapshot": _combo_snapshot(group_id, "sell_put")},
        ),
        _trade_event(
            "call-open-expiry",
            side="buy",
            position_effect="open",
            price=1.0,
            trade_date="2026-07-01",
            symbol="PDD",
            option_type="call",
            strike=100.0,
            expiration_ymd="2026-09-18",
            raw_payload={"strategy_snapshot": _combo_snapshot(group_id, "enhancement_call")},
        ),
        _trade_event(
            "put-expire",
            side="buy",
            position_effect="close",
            price=0.0,
            trade_date="2026-08-22",
            symbol="PDD",
            strike=80.0,
            expiration_ymd="2026-08-21",
            raw_payload={
                "close_type": EXPIRE_AUTO_CLOSE,
                "strategy_snapshot": _combo_snapshot(group_id, "sell_put"),
            },
        ),
    ]

    report = build_monthly_income_report([], account="lx", broker="富途", trade_events=events)

    assert report["full_group_lifecycle"][0]["summary_classification"] == "residual_call"
