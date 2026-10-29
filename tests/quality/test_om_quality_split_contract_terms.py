from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.application.quality import opend_position_adapter as adapter
from src.application.quality.gate import QualityGateBlocked, assert_quality_allows
from src.application.quality.position_checks import (
    _contract_terms_drifts,
    build_position_dataset,
    normalize_opend_positions,
)
from src.infrastructure.futu_gateway import FutuGateway, _FutuAPIBackend, _FutuAPIClient


NOW = datetime(2026, 7, 13, 10, tzinfo=timezone.utc)
ACCOUNT = "jade"
ACCOUNT_ID = "776001"
CODE = "US.NVDA260717P100000"
CONSUMERS = {"option_position_report", "lifecycle", "close_advice"}
OLD_KEY = "NVDA|put|2026-07-17|100|100"


def _row(qty, *, side="SHORT", code=CODE, **extra):
    return {
        "code": code,
        "qty": qty if side == "LONG" else -qty,
        "position_side": side,
        "sec_type": "DRVT",
        "acc_id": int(ACCOUNT_ID),
        "trd_env": "REAL",
        **extra,
    }


def _terms(*, code=CODE, strike=99.5, multiplier=101):
    return {
        "code": code,
        "stock_owner": "US.NVDA",
        "option_type": "PUT",
        "strike_time": "2026-07-17",
        "option_strike_price": strike,
        "option_contract_multiplier": multiplier,
        "option_valid": True,
    }


def _lot(qty, *, side="SHORT"):
    return {
        "record_id": "synthetic-lot",
        "fields": {
            "account": ACCOUNT,
            "broker": "futu",
            "symbol": "NVDA",
            "option_type": "put",
            "side": side.lower(),
            "contracts": qty,
            "contracts_open": qty,
            "contracts_closed": 0,
            "strike": 100,
            "multiplier": 100,
            "expiration_ymd": "2026-07-17",
            "status": "open",
        },
    }


def _dataset(
    monkeypatch, rows, quotes, lots, *, context="ordinary", market="us", symbol="NVDA", calendar_complete=True,
):
    originals = deepcopy((rows, quotes, lots))
    calls = []

    class Broker:
        def get_positions(self, **kwargs):
            assert kwargs == {"acc_id": int(ACCOUNT_ID), "trd_env": "REAL", "refresh_cache": True}
            calls.append("positions")
            return deepcopy(rows)

        def close(self):
            calls.append("broker.close")

    class SDKContext:
        def request_trading_days(self, **kwargs):
            assert kwargs == {"market": market.upper(), "start": "2026-07-01", "end": "2026-07-31"}
            calls.append("calendar")
            return 0, ([{"time": "2026-07-13", "trade_date_type": "WHOLE"}] if calendar_complete else None)

        def get_market_snapshot(self, **kwargs):
            assert kwargs == {"code_list": sorted({row["code"] for row in rows})}
            calls.append("terms")
            return 0, deepcopy(quotes)

        def close(self):
            calls.append("quote.close")

    def broker_factory(**kwargs):
        assert kwargs == {
            "host": "fixture.invalid",
            "port": 1776,
            "expected_account_ids": [ACCOUNT_ID],
            "trd_env": "REAL",
            "is_option_chain_cache_enabled": False,
        }
        return Broker()

    def quote_factory(**kwargs):
        assert kwargs == {"host": "fixture.invalid", "port": 1776, "is_option_chain_cache_enabled": False}
        backend = _FutuAPIBackend(host="fixture.invalid", port=1776)
        backend._quote_client = SDKContext()
        return FutuGateway(
            client=_FutuAPIClient(backend, is_option_chain_cache_enabled=False),
            backend=backend, host="fixture.invalid", port=1776,
        )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(adapter, "datetime", Clock)
    monkeypatch.setattr(adapter, "build_ready_futu_broker_gateway", broker_factory)
    monkeypatch.setattr(adapter, "build_ready_futu_quote_gateway", quote_factory)
    cfg = {
        "accounts": [ACCOUNT],
        "_generated": {"market": market},
        "account_settings": {
            ACCOUNT: {
                "type": "futu",
                "futu": {"host": "fixture.invalid", "port": 1776, "account_id": ACCOUNT_ID, "trd_env": "REAL"},
            }
        },
        "symbols": [{"symbol": symbol, "fetch": {"source": "futu", "host": "fixture.invalid", "port": 1776}}],
    }
    snapshot = adapter.OpenDOptionPositionAdapter().fetch(
        cfg=cfg, account=ACCOUNT, market=market, calendar_start=date(2026, 7, 1), calendar_end=date(2026, 7, 31)
    )
    extra = {}
    if context == "lifecycle":
        extra = {
            "lifecycle_cases": [
                {
                    "case_id": "pending",
                    "account": ACCOUNT,
                    "market": market.upper(),
                    "status": "waiting_settlement_evidence",
                    "symbol": symbol,
                    "option_type": "put",
                    "position_side": lots[0]["fields"]["side"],
                    "strike": 100,
                    "multiplier": 100,
                    "expiration_ymd": "2026-07-17",
                }
            ],
            "lifecycle_read_models_by_case": {
                "pending": {
                    "remaining_contracts_by_lot": {"synthetic-lot": lots[0]["fields"]["contracts"]},
                    "pending_until_ms": int((NOW + timedelta(hours=1)).timestamp() * 1000),
                    "timing_policy_hash": "7" * 64,
                }
            },
        }
    dataset, state = build_position_dataset(
        snapshot=snapshot,
        local_lots=lots,
        account=ACCOUNT,
        market=market,
        observed_at_utc=NOW.isoformat(),
        now=NOW,
        control_state={"position_mismatches": {}},
        day_end_strict=context == "day-end",
        **extra,
    )
    assert (rows, quotes, lots) == originals
    assert calls.count("broker.close") == calls.count("quote.close") == 1
    assert calls.count("positions") == 1
    if not calendar_complete:
        assert calls.count("calendar") == 1
        assert "terms" not in calls
    if snapshot.complete:
        assert calls.count("calendar") == calls.count("terms") == 1
        assert len(snapshot.snapshot_input["rows"]) == len(rows)
        assert snapshot.snapshot_input["errors"] == []
    return snapshot, dataset, state


def _gate_results(monkeypatch, dataset, *, market="us"):
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "1")
    reads = []

    class Reader:
        def read_published(self, **kwargs):
            assert kwargs["consumer"] in CONSUMERS
            assert {key: value for key, value in kwargs.items() if key != "consumer"} == {
                "account": ACCOUNT,
                "market": market,
                "lifecycle_rows_requested": False,
            }
            reads.append(kwargs["consumer"])
            return {"observed_at_utc": NOW.isoformat(), "datasets": [deepcopy(dataset)]}

    results = {}
    for consumer in sorted(CONSUMERS):
        try:
            assert_quality_allows(consumer, account=ACCOUNT, market=market, service=Reader(), now=NOW)
            results[consumer] = True
        except QualityGateBlocked:
            results[consumer] = False
    assert set(reads) == CONSUMERS
    return results


@pytest.mark.parametrize("side", ["SHORT", "LONG"])
@pytest.mark.parametrize("quantities,total", [((2,), 2), ((1, 1), 2), ((1, 2), 3), ((2, 1), 3)])
@pytest.mark.parametrize("context", ["ordinary", "lifecycle", "day-end"])
def test_split_terms_drift_matches_merged_and_blocks_all_consumers(monkeypatch, side, quantities, total, context):
    snapshot, dataset, state = _dataset(
        monkeypatch, [_row(q, side=side) for q in quantities], [_terms()], [_lot(total, side=side)], context=context
    )
    quantity = str(total) if side == "LONG" else "-" + str(total)
    assert snapshot.complete
    assert dataset["status"] == "untrusted"
    assert dataset["checks"][1]["reason_code"] == "POSITION_CONTRACT_TERMS_DRIFT"
    assert dataset["checks"][1]["observed"]["contract_terms_drifts"] == [
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-07-17",
            "quantity": quantity,
            "local_contracts": "100@100:" + quantity,
            "opend_contracts": "99.5@101:" + quantity,
            "broker_code": CODE,
            "mapping": "code_lineage",
        }
    ]
    assert state["position_mismatches"]["us:jade"]["kind"] == "contract_terms_drift"
    assert set(dataset["blocked_consumers"]) == CONSUMERS
    assert _gate_results(monkeypatch, dataset) == dict.fromkeys(CONSUMERS, False)


@pytest.mark.parametrize("side", ["SHORT", "LONG"])
def test_unchanged_split_terms_remain_trusted(monkeypatch, side):
    snapshot, dataset, _ = _dataset(
        monkeypatch,
        [_row(1, side=side), _row(2, side=side)],
        [_terms(strike=100, multiplier=100)],
        [_lot(3, side=side)],
    )
    assert snapshot.complete
    assert dataset["status"] == "trusted"
    assert dataset["checks"][1]["reason_code"] == "POSITIONS_RECONCILED"
    assert _gate_results(monkeypatch, dataset) == dict.fromkeys(CONSUMERS, True)


@pytest.mark.parametrize("calendar_complete", [True, False])
@pytest.mark.parametrize("quantities", [(2,), (1, 1)])
@pytest.mark.parametrize("context", ["ordinary", "lifecycle", "day-end"])
@pytest.mark.parametrize("terms_drift", [True, False])
def test_calendar_completeness_precedes_split_terms_and_context_gates(
    monkeypatch, calendar_complete, quantities, context, terms_drift,
):
    snapshot, dataset, state = _dataset(
        monkeypatch,
        [_row(q) for q in quantities],
        [_terms() if terms_drift else _terms(strike=100, multiplier=100)],
        [_lot(sum(quantities))],
        context=context,
        calendar_complete=calendar_complete,
    )
    assert snapshot.complete is calendar_complete
    assert dataset["source_snapshots"][0]["complete"] is calendar_complete
    if not calendar_complete:
        assert snapshot.rows == snapshot.trading_days == []
        assert snapshot.snapshot_input["source_payload"]["rows"] == [_row(q) for q in quantities]
        assert dataset["status"] == "unavailable"
        assert dataset["checks"][0]["status"] == "unknown"
        assert dataset["checks"][1]["reason_code"] == "POSITION_CONVERGENCE_SOURCE_UNAVAILABLE"
        assert not dataset.get("extensions", {}).get("next_authoritative_refresh_due_utc")
        assert state["position_mismatches"] == {}
    else:
        assert snapshot.trading_days == [date(2026, 7, 13)]
        assert dataset["status"] == ("untrusted" if terms_drift else "trusted")
        assert dataset["checks"][1]["reason_code"] == (
            "POSITION_CONTRACT_TERMS_DRIFT" if terms_drift else "POSITIONS_RECONCILED"
        )
    assert _gate_results(monkeypatch, dataset) == dict.fromkeys(CONSUMERS, calendar_complete and not terms_drift)


@pytest.mark.parametrize("failure", ["terms-conflict", "missing-multiplier", "account", "environment"])
def test_split_source_errors_stay_unavailable(monkeypatch, failure):
    rows, quotes = [_row(1), _row(1)], [_terms()]
    if failure == "terms-conflict":
        quotes.append(_terms(strike=98))
    elif failure == "missing-multiplier":
        quotes[0].pop("option_contract_multiplier")
    elif failure == "account":
        rows[1]["acc_id"] = 776002
    else:
        rows[1]["trd_env"] = "SIMULATE"
    snapshot, dataset, _ = _dataset(monkeypatch, rows, quotes, [_lot(2)])
    assert not snapshot.complete
    assert dataset["status"] == "unavailable"
    assert dataset["checks"][1]["reason_code"] == "POSITION_CONVERGENCE_SOURCE_UNAVAILABLE"
    assert _gate_results(monkeypatch, dataset) == dict.fromkeys(CONSUMERS, False)


def _owner(rows, *, local=None):
    before = deepcopy(rows)
    local = {OLD_KEY: Decimal("-2")} if local is None else local
    broker, errors = normalize_opend_positions(rows)
    comparison = {key: {} for key in local.keys() | broker.keys() if local.get(key) != broker.get(key)}
    result = _contract_terms_drifts(local=local, broker=broker, broker_rows=rows, raw_comparison=comparison)
    assert rows == before
    return result, errors


def _enriched(contracts=2, **extra):
    return {**_row(contracts), **_terms(), **extra}


@pytest.mark.parametrize("invalid", ["quantity", "multiplier-missing", "multiplier-negative"])
@pytest.mark.parametrize("reverse", [False, True])
def test_invalid_same_code_row_disqualifies_entire_drift_group(invalid, reverse):
    bad = _enriched(1)
    if invalid == "quantity":
        bad["qty"] = None
    elif invalid == "multiplier-missing":
        bad.pop("option_contract_multiplier")
    else:
        bad["option_contract_multiplier"] = -1
    rows = [_enriched(2), bad]
    if reverse:
        rows.reverse()
    drifts, errors = _owner(rows)
    assert errors
    assert drifts == []


@pytest.mark.parametrize("shape", ["mixed-net", "mixed-zero", "multi-key", "distinct-code"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ambiguous_groups_do_not_manufacture_lineage(shape, reverse):
    rows = [_enriched(3), _enriched(1, position_side="LONG", qty=1)]
    if shape == "mixed-zero":
        rows = [_enriched(2), _enriched(2, position_side="LONG", qty=2)]
    elif shape == "multi-key":
        rows = [_enriched(2), _enriched(1, option_strike_price=98.5)]
    elif shape == "distinct-code":
        rows = [_enriched(1), _enriched(1, code="US.NVDA260717P110000")]
    if reverse:
        rows.reverse()
    drifts, errors = _owner(rows)
    assert not errors
    assert drifts == []


@pytest.mark.parametrize("zero_first", [False, True])
def test_zero_row_keeps_original_ignore_semantics(zero_first):
    rows = [_enriched(2), {"code": CODE, "qty": 0}]
    if zero_first:
        rows.reverse()
    drifts, errors = _owner(rows)
    assert not errors
    assert len(drifts) == 1
    assert drifts[0]["quantity"] == "-2"
    assert drifts[0]["broker_code"] == CODE
    assert _owner([{"code": CODE, "qty": 0}]) == ([], [])


def test_code_group_preserves_local_candidate_uniqueness():
    rows = [_enriched(1), _enriched(1)]
    local = {OLD_KEY: Decimal("-2"), "NVDA|put|2026-07-17|100|99": Decimal("-2")}
    assert _owner(rows, local=local) == ([], [])


def test_different_codes_preserve_current_mapping_uniqueness():
    rows = [
        _enriched(2),
        _enriched(2, code="US.NVDA260717P110000"),
        _enriched(2, code="US.NVDA260717P090000", position_side="LONG", qty=2),
    ]
    local = {OLD_KEY: Decimal("-2"), "NVDA|put|2026-07-17|110|100": Decimal("-2")}
    assert _owner(rows, local=local) == ([], [])


def test_code_normalization_preserves_canonical_output():
    rows = [_enriched(1), _enriched(1, code=" us.nvda260717p100000 ")]
    drifts, errors = _owner(rows)
    assert not errors
    assert len(drifts) == 1
    assert drifts[0]["broker_code"] == CODE
    assert drifts[0]["quantity"] == "-2"


@pytest.mark.parametrize("side", ["SHORT", "LONG"])
@pytest.mark.parametrize("quantities", [(2,), (1, 1)])
def test_hk_split_drift_reaches_all_consumer_gates(monkeypatch, side, quantities):
    code = "HK.TCH260717P100000"
    lot = _lot(sum(quantities), side=side)
    lot["fields"]["symbol"] = "0700.HK"
    terms = {**_terms(code=code), "stock_owner": "HK.00700"}
    snapshot, dataset, _ = _dataset(
        monkeypatch,
        [_row(q, side=side, code=code) for q in quantities],
        [terms],
        [lot],
        market="hk",
        symbol="0700.HK",
    )
    assert snapshot.complete
    assert dataset["status"] == "untrusted"
    assert dataset["checks"][1]["reason_code"] == "POSITION_CONTRACT_TERMS_DRIFT"
    drifts = dataset["checks"][1]["observed"]["contract_terms_drifts"]
    assert len(drifts) == 1
    assert drifts[0]["symbol"] == "0700.HK"
    assert drifts[0]["quantity"] == ("2" if side == "LONG" else "-2")
    assert drifts[0]["broker_code"] == code
    assert _gate_results(monkeypatch, dataset, market="hk") == dict.fromkeys(CONSUMERS, False)


@pytest.mark.parametrize(
    "age,strict,reason,status",
    [
        (299, False, "POSITION_DIVERGENCE_TRANSIENT", "partial"),
        (300, False, "POSITION_DIVERGENCE_PERSISTENT", "untrusted"),
        (0, True, "POSITION_DIVERGENCE_PERSISTENT", "untrusted"),
    ],
)
def test_ordinary_quantity_mismatch_keeps_convergence_window(monkeypatch, age, strict, reason, status):
    lots = [_lot(2)]
    snapshot, initial, state = _dataset(
        monkeypatch,
        [_row(1)],
        [_terms(strike=100, multiplier=100)],
        lots,
    )
    assert initial["checks"][1]["reason_code"] == "POSITION_DIVERGENCE_TRANSIENT"
    state["position_mismatches"]["us:jade"]["first_seen_at_utc"] = (NOW - timedelta(seconds=age)).isoformat()
    dataset, _ = build_position_dataset(
        snapshot=snapshot,
        local_lots=lots,
        account=ACCOUNT,
        market="us",
        observed_at_utc=NOW.isoformat(),
        now=NOW,
        control_state=state,
        day_end_strict=strict,
    )
    assert dataset["status"] == status
    assert dataset["checks"][1]["reason_code"] == reason
    assert _gate_results(monkeypatch, dataset) == dict.fromkeys(CONSUMERS, not (strict or age >= 300))
