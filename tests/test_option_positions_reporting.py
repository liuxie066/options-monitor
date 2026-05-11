from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from domain.domain.option_position_lots import BUY_TO_CLOSE, EXPIRE_AUTO_CLOSE, parse_exp_to_ms
from src.application.option_positions_reporting import (
    build_income_row,
    build_monthly_income_report,
    build_premium_income_row,
)


def _ms(date: str) -> int:
    out = parse_exp_to_ms(date)
    assert out is not None
    return out


def _assert_contains(row: dict, expected: dict) -> None:
    assert {key: row.get(key) for key in expected} == expected


def test_build_income_row_for_buy_to_close() -> None:
    row, warning = build_income_row(
        {
            "record_id": "rec_1",
            "fields": {
                "broker": "富途证券（香港）",
                "account": "LX",
                "symbol": "0700.HK",
                "status": "close",
                "contracts": 2,
                "contracts_closed": 2,
                "currency": "港币",
                "premium": 3.93,
                "close_price": 1.2,
                "close_type": BUY_TO_CLOSE,
                "closed_at": _ms("2026-04-20"),
                "note": "multiplier=100",
            },
        }
    )

    assert warning is None
    assert row is not None
    assert row.month == "2026-04"
    assert row.account == "lx"
    assert row.broker == "富途"
    assert row.currency == "HKD"
    assert row.realized_gross == 546.0


def test_build_income_row_for_expire_auto_close_uses_zero_close_price() -> None:
    row, warning = build_income_row(
        {
            "record_id": "rec_2",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "symbol": "NVDA",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 2.5,
                "multiplier": 100,
                "close_type": EXPIRE_AUTO_CLOSE,
                "closed_at": _ms("2026-05-01"),
            },
        }
    )

    assert warning is None
    assert row is not None
    assert row.month == "2026-05"
    assert row.close_price == 0.0
    assert row.multiplier == 100
    assert row.realized_gross == 250.0


def test_build_income_row_does_not_use_market_fallback_for_broker_label() -> None:
    row, warning = build_income_row(
        {
            "record_id": "rec_market_only",
            "fields": {
                "market": "富途证券（香港）",
                "account": "lx",
                "symbol": "NVDA",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 2.5,
                "multiplier": 100,
                "close_price": 1.0,
                "close_type": BUY_TO_CLOSE,
                "closed_at": _ms("2026-05-01"),
            },
        }
    )

    assert warning is None
    assert row is not None
    assert row.broker == "-"


def test_build_income_row_warns_when_buy_close_missing_close_price() -> None:
    row, warning = build_income_row(
        {
            "record_id": "rec_3",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 2.5,
                "multiplier": 100,
                "close_type": BUY_TO_CLOSE,
                "closed_at": _ms("2026-05-01"),
            },
        }
    )

    assert row is None
    assert warning == "rec_3: missing close_price"


def test_build_premium_income_row_for_short_position_uses_opened_at_month() -> None:
    row, warning = build_premium_income_row(
        {
            "record_id": "rec_4",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "side": "short",
                "contracts": 2,
                "currency": "USD",
                "premium": 2.5,
                "multiplier": 100,
                "opened_at": _ms("2026-04-03"),
            },
        }
    )

    assert warning is None
    assert row is not None
    assert row.month == "2026-04"
    assert row.currency == "USD"
    assert row.premium_received_gross == 500.0


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

    report = build_monthly_income_report(records, account="lx", broker="富途", month="2026-04")

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


def test_facade_monthly_income_report_uses_canonical_compat_records() -> None:
    import src.application.option_positions_facade as facade

    class _Repo:
        def list_position_lots(self) -> list[dict]:
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
        original = facade.get_exchange_rates_or_fetch_latest
        facade.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {"rates": {"HKDCNY": 0.92}}
        try:
            report = facade.build_option_positions_monthly_income_report(
                _Repo(),
                base=base,
                broker="富途",
                account="lx",
                month="2026-04",
            )
        finally:
            facade.get_exchange_rates_or_fetch_latest = original

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
    import src.application.option_positions_service as svc
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    open_result = svc.persist_manual_open_event(
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
    svc.persist_manual_void_event(
        repo,
        target_event_id=str(open_result["event_id"]),
        void_reason="opened_by_mistake",
        as_of_ms=_ms("2026-04-04"),
    )

    report = build_monthly_income_report(
        repo.list_records(page_size=500),
        account="lx",
        broker="富途",
        month="2026-04",
    )

    assert report["rows"] == []
    assert report["premium_rows"] == []
    assert report["summary"] == []
    assert report["warnings"] == []


def test_monthly_income_report_excludes_voided_close_event_but_keeps_open_premium(tmp_path) -> None:
    import src.application.option_positions_service as svc
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    svc.persist_manual_open_event(
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
    close_result = svc.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.0,
        close_reason="manual_buy_to_close",
        as_of_ms=_ms("2026-04-20"),
    )
    svc.persist_manual_void_event(
        repo,
        target_event_id=str(close_result["event_id"]),
        void_reason="close_recorded_by_mistake",
        as_of_ms=_ms("2026-04-21"),
    )

    report = build_monthly_income_report(
        repo.list_records(page_size=500),
        account="lx",
        broker="富途",
        month="2026-04",
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
    assert report["warnings"] == []


def test_monthly_income_report_uses_adjusted_premium_and_opened_at(tmp_path) -> None:
    import src.application.option_positions_service as svc
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    svc.persist_manual_open_event(
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
    svc.persist_manual_adjust_event(
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
    )
    may_report = build_monthly_income_report(
        repo.list_records(page_size=500),
        account="lx",
        broker="富途",
        month="2026-05",
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


def test_monthly_income_report_legacy_long_call_cashflow_and_realized_are_separate() -> None:
    records = [
        {
            "record_id": "rec_long_call",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "call",
                "side": "long",
                "status": "close",
                "contracts": 1,
                "contracts_closed": 1,
                "currency": "USD",
                "premium": 1.2,
                "close_price": 2.0,
                "multiplier": 100,
                "opened_at": _ms("2026-04-03"),
                "closed_at": _ms("2026-05-01"),
            },
        }
    ]

    april = build_monthly_income_report(records, account="lx", broker="富途", month="2026-04")
    may = build_monthly_income_report(records, account="lx", broker="富途", month="2026-05")

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
    option_type: str = "put",
    contracts: int = 1,
    multiplier: int = 100,
    strike: float = 100.0,
    expiration_ymd: str = "2026-06-19",
    raw_payload: dict | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "source_type": "manual_trade_event",
        "source_name": "test",
        "broker": "富途",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": option_type,
        "side": side,
        "position_effect": position_effect,
        "contracts": contracts,
        "price": price,
        "strike": strike,
        "multiplier": multiplier,
        "expiration_ymd": expiration_ymd,
        "currency": "USD",
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
