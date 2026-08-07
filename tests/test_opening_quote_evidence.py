from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.application.opening_quote_evidence import (
    normalize_option_observation,
    normalize_underlier_observation,
)

_RECEIPT = {
    "snapshot_requested_at_utc": "2026-08-06T14:59:58+00:00",
    "snapshot_received_at_utc": "2026-08-06T14:59:59+00:00",
}

_RECEIPT_HK = {
    "snapshot_requested_at_utc": "2026-08-06T01:59:58+00:00",
    "snapshot_received_at_utc": "2026-08-06T01:59:59+00:00",
}


@pytest.mark.parametrize(
    ("market", "code", "update_time"),
    [
        ("US", "US.NVDA", "2026-08-06 10:56:00"),
        ("HK", "HK.00700", "2026-08-06 22:56:00+00:00"),
    ],
)
def test_underlier_observation_uses_opend_state_and_five_minute_freshness(
    market: str,
    code: str,
    update_time: str,
) -> None:
    now = (
        datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
        if market == "US"
        else datetime(2026, 8, 6, 23, 0, tzinfo=timezone.utc)
    )
    observation = normalize_underlier_observation(
        code=code,
        market=market,
        snapshot_row={
            "code": code,
            "last_price": 100.0,
            "update_time": update_time,
            "sec_status": "NORMAL",
            "suspension": False,
        },
        market_state_row={"code": code, "market_state": "MORNING"},
        now_utc=now,
    )

    assert observation.status == "ready"
    assert observation.reason_code is None
    assert observation.age_seconds == 240


def test_underlier_observation_projects_closed_market_without_local_clock_guess() -> None:
    observation = normalize_underlier_observation(
        code="US.NVDA",
        market="US",
        snapshot_row={
            "code": "US.NVDA",
            "last_price": 100.0,
            "update_time": None,
            "sec_status": "NORMAL",
            "suspension": False,
        },
        market_state_row={"code": "US.NVDA", "market_state": "CLOSED"},
        now_utc=datetime(2026, 8, 6, 23, 0, tzinfo=timezone.utc),
    )

    assert observation.status == "market_closed"
    assert observation.reason_code == "market_closed"


def test_underlier_observation_missing_state_or_stale_spot_is_unavailable() -> None:
    missing = normalize_underlier_observation(
        code="US.NVDA",
        market="US",
        snapshot_row={
            "code": "US.NVDA",
            "last_price": 100.0,
            "update_time": "2026-08-06 10:59:00",
            "sec_status": "NORMAL",
            "suspension": False,
        },
        market_state_row={"code": "US.NVDA", "market_state": "N/A"},
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )
    stale = normalize_underlier_observation(
        code="US.NVDA",
        market="US",
        snapshot_row={
            "code": "US.NVDA",
            "last_price": 100.0,
            "update_time": "2026-08-06 10:54:59",
            "sec_status": "NORMAL",
            "suspension": False,
        },
        market_state_row={"code": "US.NVDA", "market_state": "MORNING"},
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert (missing.status, missing.reason_code) == (
        "data_unavailable",
        "market_state_missing_or_invalid",
    )
    assert (stale.status, stale.reason_code) == (
        "data_unavailable",
        "underlier_quote_stale",
    )


def _ready_underlier():  # type: ignore[no-untyped-def]
    return normalize_underlier_observation(
        code="US.NVDA",
        market="US",
        snapshot_row={
            "code": "US.NVDA",
            "last_price": 180.0,
            "update_time": "2026-08-06 10:59:00",
            "sec_status": "NORMAL",
            "suspension": False,
        },
        market_state_row={"code": "US.NVDA", "market_state": "MORNING"},
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )


def _chain(**overrides):  # type: ignore[no-untyped-def]
    return {
        "code": "US.NVDA260821P00170000",
        "lot_size": 100,
        "stock_type": "DRVT",
        "stock_owner": "US.NVDA",
        "option_standard_type": "STANDARD",
        "suspension": False,
        **overrides,
    }


def _snapshot(**overrides):  # type: ignore[no-untyped-def]
    return {
        "code": "US.NVDA260821P00170000",
        "bid_price": 1.0,
        "ask_price": 1.2,
        "last_price": 1.1,
        "update_time": "2026-08-06 10:59:00",
        "price_spread": 0.01,
        "option_implied_volatility": 25.0,
        "option_delta": -0.2,
        "option_open_interest": 0,
        "volume": 0,
        "option_contract_size": 100,
        "sec_status": "NORMAL",
        "suspension": False,
        **overrides,
    }


def test_option_observation_preserves_zero_optional_values_and_normalizes_iv_only() -> None:
    observation = normalize_option_observation(
        expected_owner="US.NVDA",
        market="US",
        currency="USD",
        chain_row=_chain(),
        snapshot_row=_snapshot(),
        underlier_observation=_ready_underlier(),
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
        **_RECEIPT,
    )

    assert observation.status == "ready"
    assert observation.reason_codes == ()
    assert observation.last_price_activity_status == "recent"
    assert observation.snapshot_age_seconds == 1.0
    assert observation.implied_volatility == 0.25
    assert observation.delta == -0.2
    assert observation.open_interest == 0
    assert observation.volume == 0
    assert observation.price_tick == 0.01
    assert observation.multiplier == 100


@pytest.mark.parametrize(
    ("chain_overrides", "snapshot_overrides", "expected_reason"),
    [
        ({"option_standard_type": "NON_STANDARD"}, {}, "option_non_standard"),
        ({"stock_owner": "US.AMD"}, {}, "option_stock_owner_mismatch"),
        ({"suspension": True}, {}, "option_suspended"),
        ({"lot_size": 50}, {}, "option_multiplier_conflict"),
        ({}, {"price_spread": None}, "option_price_tick_missing_or_invalid"),
    ],
)
def test_option_observation_scopes_contract_failures(
    chain_overrides: dict[str, object],
    snapshot_overrides: dict[str, object],
    expected_reason: str,
) -> None:
    observation = normalize_option_observation(
        expected_owner="US.NVDA",
        market="US",
        currency="USD",
        chain_row=_chain(**chain_overrides),
        snapshot_row=_snapshot(**snapshot_overrides),
        underlier_observation=_ready_underlier(),
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
        **_RECEIPT,
    )

    assert observation.status in {"ineligible", "data_unavailable"}
    assert expected_reason in observation.reason_codes
    if expected_reason == "option_multiplier_conflict":
        assert observation.multiplier is None


def test_option_observation_never_uses_last_as_bid_or_ask() -> None:
    observation = normalize_option_observation(
        expected_owner="US.NVDA",
        market="US",
        currency="USD",
        chain_row=_chain(),
        snapshot_row=_snapshot(bid_price=None, ask_price=None, last_price=9.9),
        underlier_observation=_ready_underlier(),
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
        **_RECEIPT,
    )

    assert observation.status == "data_unavailable"
    assert observation.bid is None
    assert observation.ask is None
    assert observation.last_price == 9.9


def test_hk_provider_shape_uses_drvt_and_preserves_zero_values() -> None:
    now = datetime(2026, 8, 6, 2, 0, tzinfo=timezone.utc)
    underlier = normalize_underlier_observation(
        code="HK.00700",
        market="HK",
        snapshot_row={
            "code": "HK.00700",
            "last_price": 550.0,
            "update_time": "2026-08-06 09:59:00",
            "sec_status": "NORMAL",
            "suspension": False,
        },
        market_state_row={"code": "HK.00700", "market_state": "MORNING"},
        now_utc=now,
    )
    option = normalize_option_observation(
        expected_owner="HK.00700",
        market="HK",
        currency="HKD",
        chain_row={
            "code": "HK.TCH260827P00500000",
            "lot_size": 100,
            "stock_type": "DRVT",
            "stock_owner": "HK.00700",
            "option_standard_type": "STANDARD",
            "suspension": False,
        },
        snapshot_row={
            "code": "HK.TCH260827P00500000",
            "bid_price": 4.0,
            "ask_price": 4.2,
            "last_price": 4.1,
            "update_time": "2026-08-06 09:59:00",
            "price_spread": 0.2,
            "option_implied_volatility": 20.0,
            "option_delta": -0.1,
            "option_open_interest": 0,
            "volume": 0,
            "option_contract_size": 100,
            "sec_status": "NORMAL",
            "suspension": False,
        },
        underlier_observation=underlier,
        now_utc=now,
        **_RECEIPT_HK,
    )

    assert option.status == "ready"
    assert option.stock_type == "DRVT"
    assert option.multiplier == 100
    assert option.implied_volatility == 0.2
    assert option.delta == -0.1
    assert option.open_interest == 0
    assert option.volume == 0


def test_option_observation_quiet_latest_price_stays_ready() -> None:
    """A quiet HK option (last trade 09:30, decision 15:00) must not be gated
    by latest-price age when bid/ask evidence is valid."""
    now = datetime(2026, 8, 6, 7, 0, tzinfo=timezone.utc)  # 15:00 HK
    underlier = normalize_underlier_observation(
        code="HK.00700",
        market="HK",
        snapshot_row={
            "code": "HK.00700",
            "last_price": 550.0,
            "update_time": "2026-08-06 14:59:00",
            "sec_status": "NORMAL",
            "suspension": False,
        },
        market_state_row={"code": "HK.00700", "market_state": "AFTERNOON"},
        now_utc=now,
    )
    option = normalize_option_observation(
        expected_owner="HK.00700",
        market="HK",
        currency="HKD",
        chain_row={
            "code": "HK.TCH260827P00500000",
            "lot_size": 100,
            "stock_type": "DRVT",
            "stock_owner": "HK.00700",
            "option_standard_type": "STANDARD",
            "suspension": False,
        },
        snapshot_row={
            "code": "HK.TCH260827P00500000",
            "bid_price": 4.0,
            "ask_price": 4.2,
            "last_price": 4.1,
            "update_time": "2026-08-06 09:30:00",  # 5.5h old latest price
            "price_spread": 0.2,
            "option_implied_volatility": 20.0,
            "option_delta": -0.1,
            "option_open_interest": 0,
            "volume": 0,
            "option_contract_size": 100,
            "sec_status": "NORMAL",
            "suspension": False,
        },
        underlier_observation=underlier,
        snapshot_requested_at_utc="2026-08-06T06:59:58+00:00",
        snapshot_received_at_utc="2026-08-06T06:59:59+00:00",
        now_utc=now,
    )

    assert option.status == "ready"
    assert option.last_price_activity_status == "quiet"
    assert option.snapshot_age_seconds == 1.0


def test_option_observation_missing_update_time_is_unknown_activity_not_unavailable() -> None:
    observation = normalize_option_observation(
        expected_owner="US.NVDA",
        market="US",
        currency="USD",
        chain_row=_chain(),
        snapshot_row=_snapshot(update_time=None),
        underlier_observation=_ready_underlier(),
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
        **_RECEIPT,
    )

    assert observation.status == "ready"
    assert observation.last_price_activity_status == "unknown"


def test_option_observation_future_update_time_is_anomalous_activity() -> None:
    observation = normalize_option_observation(
        expected_owner="US.NVDA",
        market="US",
        currency="USD",
        chain_row=_chain(),
        snapshot_row=_snapshot(update_time="2026-08-06 15:30:00"),
        underlier_observation=_ready_underlier(),
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
        **_RECEIPT,
    )

    assert observation.status == "ready"
    assert observation.last_price_activity_status == "anomalous"


def test_option_observation_stale_snapshot_receipt_is_unavailable() -> None:
    observation = normalize_option_observation(
        expected_owner="US.NVDA",
        market="US",
        currency="USD",
        chain_row=_chain(),
        snapshot_row=_snapshot(),
        underlier_observation=_ready_underlier(),
        snapshot_requested_at_utc="2026-08-06T14:50:00+00:00",
        snapshot_received_at_utc="2026-08-06T14:50:01+00:00",  # 599s before decision
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert observation.status == "data_unavailable"
    assert "option_snapshot_stale" in observation.reason_codes


def test_option_observation_missing_snapshot_receipt_is_unavailable() -> None:
    observation = normalize_option_observation(
        expected_owner="US.NVDA",
        market="US",
        currency="USD",
        chain_row=_chain(),
        snapshot_row=_snapshot(),
        underlier_observation=_ready_underlier(),
        now_utc=datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert observation.status == "data_unavailable"
    assert "option_snapshot_receipt_missing" in observation.reason_codes
