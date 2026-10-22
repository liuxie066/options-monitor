"""Order creation-time evidence through native receipts and ordinary qualification."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domain.domain.trade_execution import execution_identity_from_input, normalize_execution_input
from src.application.ledger.ordinary_attribution import ordinary_attribution_basis, ordinary_attribution_window
from src.infrastructure import futu_history_deals
from src.infrastructure.futu_gateway import _FutuAPIClient


FILL = 1785510000000
NOW = FILL + 172800000
REF = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
SCOPE = {**REF, "account": "synthetic"}
ORDER = {"order_id": "synthetic-order", "order_status": "FILLED_ALL", "dealt_qty": "1"}
COMPETING_TIMES = [
    ("occurred_at_utc", "2026-07-30T15:00:00Z"),
    ("trade_time_ms", 1785423600000),
    ("create_timestamp", 1785423600),
    ("createTimestamp", 1785423600),
    ("updated_time", "2026-07-30T15:00:00Z"),
]
VALID_TIMES = [
    ({"create_time": "2026-01-15 11:00:00", "code": "US.NVDA"}, 1768492800000),
    ({"create_time": "2026-07-31 11:00:00.123456789", "code": "US.NVDA"}, FILL + 123),
    ({"create_time": "2026-07-31 11:00:00", "code": "HK.00700260821P100000"}, 1785466800000),
    ({"create_time": "2026-07-31 11:00:00", "market": "US"}, FILL),
    ({"create_time": "2026-07-31 11:00:00", "source_timezone": "America/New_York"}, FILL),
    ({"create_time": "2026-07-31 11:00:00", "source_timezone": "Asia/Hong_Kong", "code": "US.NVDA"}, 1785466800000),
    ({"create_time": "2026-07-31T11:00:00-04:00"}, FILL),
    ({"create_time": "2026-07-31T15:00:00Z"}, FILL),
] + [
    ({"create_time": "2026-07-31 11:00:00", key: "US.NVDA"}, FILL)
    for key in ("symbol", "underlying_symbol", "owner_symbol", "owner_stock_code", "owner_stock_code_full",
                "underlying_stock_code", "owner_code", "underlying_code", "stock_code", "code")
]
INVALID_TIMES = [
    {"create_time": "2026-07-31 11:00:00"},
    {"create_time": "2026-07-31 11:00:00", "market": "HK", "code": "US.NVDA"},
    {"create_time": "2026-07-31 11:00:00", "source_timezone": "Invalid/Synthetic"},
    {"create_time": "2026-11-01 01:30:00", "code": "US.NVDA"},
    {"create_time": "2026-03-08 02:30:00", "code": "US.NVDA"},
    {"create_time": "not-a-time", "code": "US.NVDA"},
    {"code": "US.NVDA"},
]


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _adapt(monkeypatch, current, historical, *, current_response=None, history_response=None):
    """Only SDK methods, execution-history fetch and the external clock are fixtures."""
    calls = []
    originals = deepcopy((current, historical))

    def query(kind, rows, response, **kwargs):
        calls.append((kind, kwargs))
        return response if response is not None else (0, rows)

    native = _FutuAPIClient(SimpleNamespace(), is_option_chain_cache_enabled=False)
    monkeypatch.setattr(native, "_trade", lambda: SimpleNamespace(
        order_list_query=lambda **kw: query("current", current, current_response, **kw),
        history_order_list_query=lambda **kw: query("history", historical, history_response, **kw)))
    client = futu_history_deals.OpenDHistoryDealClient(host="synthetic.invalid", port=0)
    client._gateway = native
    monkeypatch.setattr(client, "fetch", lambda **kw: ([], {"start_date": "2026-07-31", "end_date": "2026-08-02"}))

    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(NOW / 1000, tz or timezone.utc)

    monkeypatch.setattr(futu_history_deals, "datetime", FixtureClock)
    result = client.fetch_attribution_evidence(futu_account_id="1001", start_ms=FILL - 86400000)
    assert (current, historical) == originals
    assert calls == [
        ("current", {"trd_env": "REAL", "acc_id": 1001, "refresh_cache": True}),
        ("history", {"trd_env": "REAL", "acc_id": 1001, "start": "2026-07-31", "end": "2026-08-02"}),
    ]
    for name in ("current_orders", "history_orders"):
        assert {key: value for key, value in result[name].items() if key != "rows"} == {
            "retcode": 0, "coverage_complete": True, "pagination_complete": True, "truncated": None, "error": None}
    return result


def _qualify(receipts, *, market="US", fill_ms=FILL):
    """Complete in-memory source/event facts; no database, view/apply or cash-policy stand-in."""
    symbol, currency = ("0700.HK", "HKD") if market == "HK" else ("NVDA", "USD")
    execution = normalize_execution_input({"schema_version": "trade_execution.v1",
        "broker_account_ref": {**REF, "broker_account_id": "futu:REAL:1001", "account_label": "synthetic"},
        "instrument_ref": {"asset_type": "option", "symbol": symbol, "market": market, "currency": currency,
                           "option_type": "put", "strike": "100", "expiration_ymd": "2026-08-21", "multiplier": "100"},
        "external_id_namespace": "futu.deal", "external_execution_id": "synthetic-fill",
        "external_order_namespace": "futu.order", "external_order_id": "synthetic-order", "side": "sell",
        "position_effect": "open", "quantity": "1", "price": "2", "currency": currency, "occurred_at_utc": _iso(fill_ms)})
    assert not execution["errors"]
    contract = {"broker": "futu", "account": "synthetic", "underlying_symbol": symbol,
                "option_type": "put", "strike": "100", "expiration_ymd": "2026-08-21"}
    fact = {"position_side": "short", "reason_codes": [], "status": "pending", "origin": "unknown",
            "contracts_open": 1, "contract_key": contract, "event_time_ms": fill_ms,
            "broker_account_ref": REF, "account": "synthetic", "execution_key": execution_identity_from_input(execution)}
    event = {"event_type": "open", "event_time_ms": fill_ms, "contract_key": contract,
             "raw_payload": {"side": "sell", "execution_input": execution,
                             "execution_id": fact["execution_key"], "order_id": "synthetic-order"}}
    start, _ = ordinary_attribution_window(fact)
    history = {"dataset": "executions", "trd_env": "REAL", "request_id": "synthetic-request",
        "window_start_utc": _iso(start), "window_end_utc": _iso(NOW),
        "account_results": [{"futu_account_id": "1001", "trd_env": "REAL", "coverage_status": "complete",
            "coverage_complete": True, "pagination_complete": True, "row_count": 1, "accepted_row_count": 1,
            "covered_start_utc": _iso(start), "covered_end_utc": _iso(NOW)}]}
    evidence = {"scope": SCOPE, "window_start_ms": start, "window_end_ms": NOW, "observed_at_ms": NOW,
                "history": history, "executions": [execution]}
    for name in ("current_orders", "history_orders"):
        evidence[name] = {**receipts[name], "scope": SCOPE}
    return ordinary_attribution_basis(fact=fact, facts=[fact], events=[event], proposals=[],
        exposures=[], exposure_ids=[], now_ms=NOW, observation={"ordinary_evidence": evidence,
            "portfolio": {"position_snapshot_input": {"observed_at_utc": _iso(NOW)}}})


@pytest.mark.parametrize("source,expected", VALID_TIMES)
def test_order_creation_context_reaches_both_receipts_and_basis(monkeypatch, source, expected):
    row = {**ORDER, **source}
    result = _adapt(monkeypatch, [row], [deepcopy(row)])
    for name in ("current_orders", "history_orders"):
        assert result[name]["rows"] == [{"order_id": "synthetic-order", "status": "terminal_with_fill",
                                         "dealt_qty": "1.000000", "created_at_ms": expected}]
    market = "HK" if str(source.get("code", "")).startswith("HK.") else "US"
    assert _qualify(result, market=market, fill_ms=expected) is not None


@pytest.mark.parametrize("source", INVALID_TIMES)
@pytest.mark.parametrize("bad_receipt", ["current", "history"])
def test_invalid_order_time_in_either_receipt_still_blocks_basis(monkeypatch, source, bad_receipt):
    good = {**ORDER, "create_time": "2026-07-31T15:00:00Z"}
    bad = {**ORDER, **source}
    result = _adapt(monkeypatch, [bad if bad_receipt == "current" else good],
                    [bad if bad_receipt == "history" else good])
    assert result[bad_receipt + "_orders"]["rows"][0]["created_at_ms"] is None
    assert _qualify(result) is None


@pytest.mark.parametrize("key,value", COMPETING_TIMES)
@pytest.mark.parametrize("created,expected", [
    ("2026-07-31 11:00:00", FILL), ("2026-07-31T15:00:00Z", FILL), (None, None), ("not-a-time", None),
])
def test_other_timestamps_cannot_replace_order_creation(monkeypatch, key, value, created, expected):
    row = {**ORDER, "code": "US.NVDA", key: value}
    if created is not None:
        row["create_time"] = created
    result = _adapt(monkeypatch, [row], [deepcopy(row)])
    assert [result[name]["rows"][0]["created_at_ms"] for name in ("current_orders", "history_orders")] == [expected, expected]
    assert (_qualify(result) is not None) == (expected is not None)


@pytest.mark.parametrize("bad_receipt", ["current", "history"])
@pytest.mark.parametrize("change", [{"order_status": "FILLED_PART"}, {"order_status": "unrecognized"},
                                    {"dealt_qty": "2"}, {"order_id": ""}])
def test_order_status_quantity_and_identity_guards_remain(monkeypatch, bad_receipt, change):
    good = {**ORDER, "create_time": "2026-07-31 11:00:00", "code": "US.NVDA"}
    bad = {**good, **change}
    result = _adapt(monkeypatch, [bad if bad_receipt == "current" else good],
                    [bad if bad_receipt == "history" else good])
    assert _qualify(result) is None


@pytest.mark.parametrize("bad_receipt", ["current", "history"])
@pytest.mark.parametrize("change", [{"acc_id": "other"}, {"trd_env": "SIMULATE"}])
def test_order_account_and_environment_guards_remain(monkeypatch, bad_receipt, change):
    good = {**ORDER, "create_time": "2026-07-31 11:00:00", "code": "US.NVDA"}
    bad = {**good, **change}
    with pytest.raises(ValueError, match="account differs"):
        _adapt(monkeypatch, [bad if bad_receipt == "current" else good],
               [bad if bad_receipt == "history" else good])


@pytest.mark.parametrize("response", [(0, ["malformed"]), (None, []), (0, [], b"unconsumed")])
def test_native_incomplete_current_receipts_still_reject(monkeypatch, response):
    with pytest.raises((ValueError, RuntimeError)):
        _adapt(monkeypatch, [], [], current_response=response)


@pytest.mark.parametrize("change", [{"coverage_complete": False}, {"pagination_complete": False},
                                    {"truncated": True}, {"error": "synthetic-error"}, {"retcode": 1}])
def test_receipt_completeness_still_blocks_basis(monkeypatch, change):
    row = {**ORDER, "create_time": "2026-07-31 11:00:00", "code": "US.NVDA"}
    result = _adapt(monkeypatch, [row], [deepcopy(row)])
    result["history_orders"].update(change)
    assert _qualify(result) is None


@pytest.mark.parametrize("empty", ["current", "history", "both"])
def test_empty_receipts_preserve_existing_evidence_rules(monkeypatch, empty):
    row = {**ORDER, "create_time": "2026-07-31 11:00:00", "code": "US.NVDA"}
    result = _adapt(monkeypatch, [] if empty in ("current", "both") else [row],
                    [] if empty in ("history", "both") else [deepcopy(row)])
    assert (_qualify(result) is not None) == (empty != "both")
