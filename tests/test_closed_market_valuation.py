"""A closed session can carry the last close observation, never a missing open day."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json

import pytest

from om_strategy_core.evaluation import legacy_market
from domain.domain.decision_state_fingerprint import canonical_sha256


def ms(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def market():
    observed = ms("2026-10-23T07:56:00Z")
    basis = dict(
        source="synthetic-original",
        time_basis="received_snapshot",
        effective_at_ms=None,
        available_at_ms=observed,
        valid_until_ms=observed + 300_000,
    )
    return dict(
        stocks=[dict(symbol="0700.HK", currency="HKD", price="400", **basis)],
        options=[dict(contract_id="HK.TCH261230P390000", currency="HKD", bid="10", ask="12", **basis)],
        fx=[],
        closed_market_carry=dict(
            schema_version="om.closed-market-carry.v1",
            observation_at_ms=observed,
            calendars={
                "HK": dict(
                    start="2026-10-23",
                    end="2026-10-27",
                    source_observed_at_utc="2026-10-22T00:00:00Z",
                    receipt=dict(
                        retcode=0,
                        coverage_complete=True,
                        pagination_complete=True,
                        page_count=1,
                        rows=[
                            dict(time=day, trade_date_type="WHOLE")
                            for day in ("2026-10-23", "2026-10-26", "2026-10-27")
                        ],
                    ),
                )
            },
        ),
    )


def test_closed_market_keeps_original_marks_and_times():
    raw = market()
    original = deepcopy(raw)
    result = legacy_market(market=raw, base_currency="HKD", at_ms=ms("2026-10-24T07:56:00Z"))
    assert result["stock_marks"][0]["price"] == 400
    assert result["option_marks"][0]["price"] == 11
    assert raw == original


def legacy_calendar(calendar, *, market_name="HK"):
    snapshot = dict(
        schema_version="sell_put_top1_market_calendar_snapshot.v2",
        market=market_name,
        market_calendar_version="synthetic.opend.v1",
        coverage_start=calendar["start"],
        coverage_end=calendar["end"],
        trading_sessions=[
            dict(trading_date=r["time"], trade_date_type=r["trade_date_type"]) for r in calendar["receipt"]["rows"]
        ],
        observed_at_utc=calendar["source_observed_at_utc"],
    )
    snapshot["source_receipt_sha256"] = canonical_sha256(
        dict(
            schema_version="sell_put_top1_market_calendar_source_receipt.v1",
            source="futu.request_trading_days",
            market=market_name,
            coverage_start=snapshot["coverage_start"],
            coverage_end=snapshot["coverage_end"],
            trading_sessions=snapshot["trading_sessions"],
        )
    )
    snapshot["content_sha256"] = canonical_sha256(snapshot)
    original = json.dumps(snapshot, indent=2) + "\n"
    return dict(
        schema_version="om.legacy-normalized-calendar.v1",
        snapshot_original=dict(raw_json=original, sha256=hashlib.sha256(original.encode()).hexdigest()),
        source_observed_at_utc=snapshot["observed_at_utc"],
        native_receipt_status="not_retained",
    )


def test_explicit_legacy_calendar_keeps_original_bytes_and_marks():
    raw = market()
    carry = raw["closed_market_carry"]
    carry["calendars"]["HK"] = legacy_calendar(carry["calendars"]["HK"])
    before = deepcopy(raw)
    result = legacy_market(market=raw, base_currency="HKD", at_ms=ms("2026-10-24T07:56:00Z"))
    assert result["stock_marks"][0]["price"] == 400
    assert raw == before


@pytest.mark.parametrize(
    "case",
    [
        "raw_hash",
        "content_hash",
        "normalized_hash",
        "wrapper_time",
        "provenance",
        "wrong_market",
        "bare_snapshot",
        "future",
        "open_day",
    ],
)
def test_legacy_calendar_does_not_waive_source_or_session_checks(case):
    raw = market()
    carry = raw["closed_market_carry"]
    native = carry["calendars"]["HK"]
    if case == "future":
        native["source_observed_at_utc"] = "2026-10-25T00:00:00Z"
    if case == "open_day":
        native["receipt"]["rows"].insert(1, dict(time="2026-10-24", trade_date_type="WHOLE"))
    calendar = legacy_calendar(native, market_name="US" if case == "wrong_market" else "HK")
    original = calendar["snapshot_original"]
    if case == "raw_hash":
        original["raw_json"] += " "
    elif case in {"content_hash", "normalized_hash", "bare_snapshot"}:
        snapshot = json.loads(original["raw_json"])
        if case == "bare_snapshot":
            calendar = snapshot
        else:
            snapshot["content_sha256" if case == "content_hash" else "source_receipt_sha256"] = "0" * 64
            if case == "normalized_hash":
                snapshot["content_sha256"] = canonical_sha256(
                    {k: v for k, v in snapshot.items() if k != "content_sha256"}
                )
            original["raw_json"] = json.dumps(snapshot)
            original["sha256"] = hashlib.sha256(original["raw_json"].encode()).hexdigest()
    elif case == "wrapper_time":
        calendar["source_observed_at_utc"] = "2026-10-21T00:00:00Z"
    elif case == "provenance":
        calendar["native_receipt_status"] = "complete"
    carry["calendars"]["HK"] = calendar
    with pytest.raises(ValueError):
        legacy_market(market=raw, base_currency="HKD", at_ms=ms("2026-10-24T07:56:00Z"))


def test_calendar_known_after_quote_before_valuation_is_usable():
    raw = market()
    raw["closed_market_carry"]["calendars"]["HK"]["source_observed_at_utc"] = "2026-10-24T00:00:00Z"
    original = deepcopy(raw)
    result = legacy_market(market=raw, base_currency="HKD", at_ms=ms("2026-10-24T07:56:00Z"))
    assert result["option_marks"][0]["price"] == 11
    assert raw == original


@pytest.mark.parametrize(
    "case",
    [
        "open_day",
        "missed_open_day",
        "stale_original",
        "future_calendar",
        "coverage",
        "bad_receipt",
        "morning_price",
        "wrong_market",
        "duplicate_date",
    ],
)
def test_unproved_closed_market_cannot_extend_validity(case):
    raw = market()
    at = ms("2026-10-24T07:56:00Z")
    carry = raw["closed_market_carry"]
    calendar = carry["calendars"]["HK"]
    if case == "open_day":
        at = ms("2026-10-26T07:56:00Z")
    elif case == "missed_open_day":
        at = ms("2026-10-25T07:56:00Z")
        calendar["receipt"]["rows"].append(dict(time="2026-10-24", trade_date_type="MORNING"))
    elif case == "stale_original":
        raw["options"][0]["available_at_ms"] -= 301_000
        raw["options"][0]["valid_until_ms"] -= 301_000
    elif case == "future_calendar":
        calendar["source_observed_at_utc"] = "2026-10-24T07:56:01Z"
    elif case == "coverage":
        calendar["end"] = "2026-10-23"
    elif case == "bad_receipt":
        calendar["receipt"]["coverage_complete"] = False
    elif case == "morning_price":
        carry["observation_at_ms"] = ms("2026-10-23T02:00:00Z")
    elif case == "wrong_market":
        raw["stocks"][0]["symbol"] = "US.ACME"
    else:
        calendar["receipt"]["rows"].append(deepcopy(calendar["receipt"]["rows"][0]))
    with pytest.raises(ValueError):
        legacy_market(market=raw, base_currency="HKD", at_ms=at)


def test_ordinary_market_keeps_original_300s_expiry():
    raw = market()
    del raw["closed_market_carry"]
    assert legacy_market(market=raw, base_currency="HKD", at_ms=ms("2026-10-24T07:56:00Z"))["stock_marks"] == []


def test_half_day_uses_owner_close_and_us_dst_preserves_original_time():
    raw = market()
    carry = raw["closed_market_carry"]
    observed = ms("2026-10-23T03:56:00Z")
    carry["observation_at_ms"] = observed
    carry["calendars"]["HK"]["receipt"]["rows"][0]["trade_date_type"] = "MORNING"
    for kind in ("stocks", "options"):
        for row in raw[kind]:
            row.update(available_at_ms=observed, valid_until_ms=observed + 300_000)
    assert legacy_market(market=raw, base_currency="HKD", at_ms=ms("2026-10-24T07:56:00Z"))["stock_marks"]
    observed = ms("2026-10-30T19:56:00Z")
    raw["stocks"][0].update(
        symbol="US.ACME", currency="USD", available_at_ms=observed, valid_until_ms=observed + 300_000
    )
    raw["options"] = []
    carry.update(
        observation_at_ms=observed,
        calendars={
            "US": dict(
                start="2026-10-30",
                end="2026-11-01",
                source_observed_at_utc="2026-10-29T00:00:00Z",
                receipt=dict(
                    retcode=0,
                    coverage_complete=True,
                    pagination_complete=True,
                    page_count=1,
                    rows=[dict(time="2026-10-30", trade_date_type="WHOLE")],
                ),
            )
        },
    )
    assert legacy_market(market=raw, base_currency="USD", at_ms=ms("2026-11-01T20:56:00Z"))["stock_marks"]
