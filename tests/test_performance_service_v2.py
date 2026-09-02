from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import src.application.performance.service as performance_service
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.performance.period import PeriodRequest, normalize_performance_period
from src.application.performance.service import (
    OptionPerformanceReadError,
    build_option_period_performance,
)


_TZ = ZoneInfo("Asia/Shanghai")
_ROW_FIELDS = {
    "fact_id",
    "open_lot_id",
    "open_event_id",
    "terminal_event_id",
    "account",
    "broker",
    "symbol",
    "currency",
    "leg_type",
    "attribution_strategy",
    "strategy_group_id",
    "source_stock_lot_id",
    "opened_at_ms",
    "terminal_at_ms",
    "expiration_ymd",
    "terminal_kind",
    "state",
    "strike",
    "multiplier",
    "contracts",
    "opening_option_cash",
    "opening_actual_fee",
    "terminal_option_cash",
    "terminal_actual_fee",
    "option_net_cashflow",
    "occupied_capital",
    "capital_days",
    "win_eligible",
    "win",
    "status",
    "missing",
}


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=_TZ).timestamp() * 1000)


def _event(
    event_id: str,
    event_type: str,
    at: str,
    *,
    target_lot_id: str | None = None,
    target_event_id: str | None = None,
) -> TradeEvent:
    key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-09-30",
    )
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=_ms(at),
        contract_key=key,
        contracts=1 if event_type in {"open", "close"} else 0,
        price=2 if event_type == "open" else 1,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0,
        lot_id="lot-1" if event_type == "open" else None,
        target_lot_id=target_lot_id,
        target_event_id=target_event_id,
        raw_payload={
            "close_type": "buy_to_close" if event_type == "close" else None,
            "fee_provenance": {"basis": "actual", "amount": 0, "source": "test"},
        },
    )


class _Repo:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.read_count = 0

    def list_trade_events(self) -> list[dict]:
        self.read_count += 1
        return list(self.rows)

    def list_position_lots(self) -> list[dict]:
        return []


class _InvalidRepo(_Repo):
    def list_trade_events(self):
        self.read_count += 1
        return tuple(self.rows)


def _period():
    return normalize_performance_period(
        PeriodRequest(period="mtd", as_of_date="2026-09-02"),
        report_now_ms=_ms("2026-09-03T12:00:00"),
    )


def _contains_not_observed(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_not_observed(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_not_observed(item) for item in value)
    return value == "not_observed"


def test_service_reads_one_tuple_and_serializes_only_the_canonical_contract() -> None:
    repo = _Repo(
        [
            _event("open", "open", "2026-09-01T10:00:00").to_dict(),
            _event(
                "close",
                "close",
                "2026-09-02T10:00:00",
                target_lot_id="lot-1",
            ).to_dict(),
        ]
    )

    report = build_option_period_performance(
        repo,
        period=_period(),
        config_key="us",
        configured_accounts=("lx", "sy"),
        include_rows=True,
    )

    assert repo.read_count == 1
    assert set(report) == {
        "period",
        "scope",
        "option_net_cashflow",
        "sell_option_win_rate",
        "buy_option_win_rate",
        "option_return",
        "breakdowns",
        "quality",
        "rows",
    }
    assert report["scope"] == {
        "config_key": "us",
        "accounts": ["lx", "sy"],
        "brokers": ["富途"],
    }
    assert report["period"]["freshness_status"] == "historical"
    assert len(report["quality"]["ledger_input_hash"]) == 64
    assert set(report["rows"][0]) == _ROW_FIELDS
    assert report["option_net_cashflow"]["by_currency"]["USD"]["total"]["amount"] == 100.0
    assert not _contains_not_observed(report)


def test_service_fails_before_read_when_scope_is_unproved() -> None:
    repo = _Repo([])
    with pytest.raises(OptionPerformanceReadError) as caught:
        build_option_period_performance(
            repo,
            period=_period(),
            config_key="us",
            configured_accounts=("lx",),
            account="ghost",
        )
    assert caught.value.reason_codes == ("scope_unproven",)
    assert repo.read_count == 0


def test_service_reports_observed_empty_for_a_canonical_broker_with_no_matching_events() -> None:
    repo = _Repo([_event("open", "open", "2026-09-01T10:00:00").to_dict()])

    report = build_option_period_performance(
        repo,
        period=_period(),
        config_key="us",
        configured_accounts=("lx",),
        broker="ibkr",
    )

    assert report["scope"]["brokers"] == ["ibkr"]
    assert report["quality"]["status"] == "observed"
    assert report["option_net_cashflow"]["by_currency"] == {}


def test_service_canonicalizes_futu_broker_aliases_before_filtering() -> None:
    repo = _Repo([_event("open", "open", "2026-09-01T10:00:00").to_dict()])

    report = build_option_period_performance(
        repo,
        period=_period(),
        config_key="us",
        configured_accounts=("lx",),
        broker="FUTU",
    )

    assert report["scope"]["brokers"] == ["富途"]
    assert report["option_net_cashflow"]["by_currency"]["USD"]["total"]["amount"] == 200.0


def test_service_rejects_an_invalid_repository_result_instead_of_reporting_empty() -> None:
    repo = _InvalidRepo([])
    with pytest.raises(OptionPerformanceReadError) as caught:
        build_option_period_performance(
            repo,
            period=_period(),
            config_key="us",
            configured_accounts=("lx",),
        )
    assert caught.value.reason_codes == ("ledger_read_failed",)
    assert repo.read_count == 1


def test_service_does_not_publish_internal_failed_fact_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_reduce = performance_service.reduce_option_performance

    def _reduce(*args, **kwargs):
        reduction = real_reduce(*args, **kwargs)
        failed = replace(reduction.facts[0], state="failed")
        return replace(
            reduction,
            facts=(failed,),
            bundle={
                **reduction.bundle,
                "status": "partial",
                "missing": ("economic_adjust_invalid",),
            },
        )

    monkeypatch.setattr(performance_service, "reduce_option_performance", _reduce)
    report = build_option_period_performance(
        _Repo([_event("open", "open", "2026-09-01T10:00:00").to_dict()]),
        period=_period(),
        config_key="us",
        configured_accounts=("lx",),
        include_rows=True,
    )

    assert report["rows"] == []
    assert report["quality"]["status"] == "partial"
    assert report["quality"]["missing"] == ["economic_adjust_invalid"]


def test_service_classifies_tuple_and_control_graph_failures() -> None:
    duplicate = _event("same", "open", "2026-09-01T10:00:00").to_dict()
    with pytest.raises(OptionPerformanceReadError) as tuple_error:
        build_option_period_performance(
            _Repo([duplicate, duplicate]),
            period=_period(),
            config_key="us",
            configured_accounts=("lx",),
        )
    assert tuple_error.value.reason_codes == ("ledger_tuple_invalid",)

    missing_target = _event(
        "void",
        "void",
        "2026-09-02T11:00:00",
        target_event_id="missing",
    ).to_dict()
    with pytest.raises(OptionPerformanceReadError) as graph_error:
        build_option_period_performance(
            _Repo([missing_target]),
            period=_period(),
            config_key="us",
            configured_accounts=("lx",),
        )
    assert graph_error.value.reason_codes == ("ledger_control_graph_invalid",)
