from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import src.application.performance.service as performance_service
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.performance.period import PeriodRequest, normalize_performance_period
from src.application.cash_conversion import attach_trade_event_cash_conversions
from src.application.performance.service import (
    OptionPerformanceReadError,
    build_option_period_performance,
)
from src.application.copilot import tools as copilot_tools
from src.application.copilot.result_admission import admit_submit_answer


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
    fx_rate: float | None = 7.0,
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
    event = TradeEvent(
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
    if fx_rate is None:
        return event
    return attach_trade_event_cash_conversions(
        event,
        fx_payload={
            "rates": {"USDCNY": fx_rate},
            "timestamp": datetime.fromtimestamp(
                event.event_time_ms / 1000,
                tz=timezone.utc,
            ).isoformat(),
        },
        observed_at_ms=event.event_time_ms + 1,
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


def _admission_evidence(observation: dict) -> dict:
    return {
        "ok": True,
        "authorized_read": True,
        "observation_status": observation["status"],
        "coverage": observation["coverage"],
        "freshness": observation["freshness"],
    }


def _submit(*, kind: str, status: str = "complete", text: str = "期权收益已确认") -> dict:
    return {
        "mode": "evidence",
        "status": status,
        "answer_markdown": text,
        "claims": [
            {
                "text": text,
                "kind": kind,
                "observation_ids": ["obv_report"],
                "required_scope": "full_query",
            }
        ],
    }


def test_service_reads_one_tuple_and_serializes_only_the_canonical_contract() -> None:
    repo = _Repo(
        [
            _event("open", "open", "2026-09-01T10:00:00").to_dict(),
            _event(
                "close",
                "close",
                "2026-09-02T10:00:00",
                target_lot_id="lot-1",
                fx_rate=7.2,
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
        "coverage",
        "freshness",
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
    assert report["coverage"] == {
        "status": "complete",
        "complete_for": "full_query",
        "included_count": 1,
        "total_count": 1,
        "omitted_count": 0,
    }
    assert report["freshness"] == {
        "status": "historical",
        "as_of": "2026-09-02T23:59:59.999+08:00",
    }
    assert len(report["quality"]["ledger_input_hash"]) == 64
    assert set(report["rows"][0]) == _ROW_FIELDS
    assert report["option_net_cashflow"]["by_currency"]["USD"]["total"]["amount"] == 100.0
    cny_total = report["option_net_cashflow"]["cny_total"]
    assert cny_total["currency"] == "CNY"
    assert cny_total["amount"] == 680.0
    assert cny_total["status"] == "observed"
    assert cny_total["missing"] == []
    assert report["quality"]["status"] == "observed"
    assert not _contains_not_observed(report)


def test_service_keeps_native_cash_when_cny_conversion_is_missing() -> None:
    report = build_option_period_performance(
        _Repo(
            [
                _event(
                    "open",
                    "open",
                    "2026-09-01T10:00:00",
                    fx_rate=None,
                ).to_dict()
            ]
        ),
        period=_period(),
        config_key="us",
        configured_accounts=("lx",),
    )

    assert report["option_net_cashflow"]["by_currency"]["USD"]["total"]["amount"] == 200.0
    assert report["option_net_cashflow"]["cny_total"]["amount"] is None
    assert report["option_net_cashflow"]["cny_total"]["status"] == "partial"
    assert report["quality"]["status"] == "partial"
    assert "cash_conversion_missing" in report["quality"]["missing"]


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("native_currency", "?", "identity_contract_mismatch"),
        ("native_amount", "1e999999", "invalid_numeric_contract"),
    ],
)
def test_service_keeps_native_cash_when_cny_conversion_is_malformed(
    field: str,
    value: str,
    issue: str,
) -> None:
    event = _event("open", "open", "2026-09-01T10:00:00")
    raw_payload = dict(event.raw_payload)
    conversions = dict(raw_payload["cash_conversions"])
    gross = dict(conversions["option_trade_cash_gross"])
    gross[field] = value
    conversions["option_trade_cash_gross"] = gross
    raw_payload["cash_conversions"] = conversions

    report = build_option_period_performance(
        _Repo([replace(event, raw_payload=raw_payload).to_dict()]),
        period=_period(),
        config_key="us",
        configured_accounts=("lx",),
    )

    assert report["option_net_cashflow"]["by_currency"]["USD"]["total"]["amount"] == 200.0
    assert report["option_net_cashflow"]["cny_total"]["amount"] is None
    assert report["option_net_cashflow"]["cny_total"]["status"] == "partial"
    assert f"cash_conversion_corrupt:{issue}" in report["quality"]["missing"]


def test_real_report_evidence_is_admitted_only_for_supported_time_claims() -> None:
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
        configured_accounts=("lx",),
    )
    observation = copilot_tools.compact_observation(
        "option_performance_report",
        {"ok": True, "data": report},
        {"period": "mtd", "as_of_date": "2026-09-02"},
    )
    registry = {"obv_report": _admission_evidence(observation)}

    assert observation["coverage"]["complete_for"] == "full_query"
    assert observation["freshness"]["status"] == "historical"
    assert admit_submit_answer(
        _submit(kind="historical_fact"),
        registry,
    )["observation"]["ok"] is True
    assert admit_submit_answer(
        _submit(kind="current_fact"),
        registry,
    )["observation"]["reason"] == "claim_freshness_not_supported"


def test_current_real_report_supports_current_claim_and_empty_is_not_zero_profit() -> None:
    current_period = normalize_performance_period(
        {"period": "month", "month": "2026-09"},
        report_now_ms=_ms("2026-09-02T12:00:00"),
    )
    report = build_option_period_performance(
        _Repo([]),
        period=current_period,
        config_key="us",
        configured_accounts=("lx",),
    )
    observation = copilot_tools.compact_observation(
        "option_performance_report",
        {"ok": True, "data": report},
        {"period": "month", "month": "2026-09"},
    )
    registry = {"obv_report": _admission_evidence(observation)}

    assert observation["freshness"]["status"] == "current"
    assert observation["value"]["sell_option_win_rate"]["status"] == "not_applicable"
    assert observation["value"]["sell_option_win_rate"]["rate"] is None
    assert admit_submit_answer(
        _submit(kind="current_fact", text="当前期间没有可计入卖方胜率的合约"),
        registry,
    )["observation"]["ok"] is True


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
    assert report["option_net_cashflow"]["cny_total"]["amount"] == 0.0


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
    assert report["coverage"]["status"] == "complete"
    assert report["coverage"]["complete_for"] == "full_query"
    observation = copilot_tools.compact_observation(
        "option_performance_report",
        {"ok": True, "data": report},
        {"period": "mtd", "as_of_date": "2026-09-02"},
    )
    registry = {"obv_report": _admission_evidence(observation)}
    assert observation["status"] == "partial"
    assert admit_submit_answer(
        _submit(kind="historical_fact"),
        registry,
    )["observation"]["reason"] == "answer_status_overstates_evidence"
    assert admit_submit_answer(
        _submit(kind="historical_fact", status="partial"),
        registry,
    )["observation"]["ok"] is True


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
