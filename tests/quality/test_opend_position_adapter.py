from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import src.application.quality.opend_position_adapter as adapter_module
from src.application.quality.opend_position_adapter import (
    OpenDOptionPositionAdapter,
)
from src.application.quality.position_checks import build_position_dataset


def _config() -> dict:
    return {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "123456",
                    "trd_env": "REAL",
                },
            }
        },
        "symbols": [
            {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}}
        ],
    }


def _position(code: str, **overrides: object) -> dict:
    row = {"code": code, "qty": -1, "position_side": "SHORT", "sec_type": "DRVT"}
    row.update(overrides)
    return row


def _snapshot_row(code: str, **overrides: object) -> dict:
    row = {
        "code": code,
        "stock_owner": "US.NVDA",
        "option_type": "PUT",
        "strike_time": "2026-08-21",
        "option_strike_price": 99.5,
    }
    row.update(overrides)
    return row


def _local_lot(record_id: str, **field_overrides: object) -> dict:
    fields = {
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "contracts": 1,
        "contracts_open": 1,
        "strike": 100,
        "multiplier": 100,
        "expiration_ymd": "2026-07-17",
        "status": "open",
    }
    fields.update(field_overrides)
    return {"record_id": record_id, "fields": fields}


class _Gateway:
    def __init__(
        self,
        *,
        positions: list[dict],
        snapshots: dict[str, dict],
    ) -> None:
        self.positions = positions
        self.snapshots = snapshots
        self.snapshot_calls: list[list[str]] = []
        self.closed = False

    def get_positions(self, **_kwargs):  # noqa: ANN003, ANN201
        return list(self.positions)

    def get_trading_days(self, **_kwargs):  # noqa: ANN003, ANN201
        return [{"time": "2026-07-31", "trade_date_type": "WHOLE"}]

    def get_trading_days_with_receipt(self, **kwargs):
        return _calendar_receipt(rows=self.get_trading_days(**kwargs))

    def get_snapshot(self, codes):  # noqa: ANN001, ANN201
        batch = list(codes)
        self.snapshot_calls.append(batch)
        return [self.snapshots[code] for code in batch if code in self.snapshots]

    def close(self) -> None:
        self.closed = True


def _wire(monkeypatch: pytest.MonkeyPatch, gateway: _Gateway) -> None:
    monkeypatch.setattr(adapter_module, "build_ready_futu_broker_gateway", lambda **_kwargs: gateway)
    monkeypatch.setattr(adapter_module, "build_ready_futu_quote_gateway", lambda **_kwargs: gateway)


def _fetch(*, market: str = "us"):
    return OpenDOptionPositionAdapter().fetch(cfg=_config(), account="lx", market=market)


def _calendar_receipt(**overrides):
    return {
        "retcode": 0,
        "coverage_complete": True,
        "pagination_complete": True,
        "page_count": 1,
        "rows": [],
        **overrides,
    }


@pytest.mark.parametrize("receipt", [
    None, [], {},
    _calendar_receipt(retcode=1),
    _calendar_receipt(coverage_complete=None),
    _calendar_receipt(coverage_complete=False),
    _calendar_receipt(pagination_complete=None),
    _calendar_receipt(pagination_complete=False),
    _calendar_receipt(page_count=None),
    _calendar_receipt(page_count=2),
    _calendar_receipt(rows=None),
    _calendar_receipt(rows=[None]),
])
def test_calendar_requires_complete_receipt_and_dictionary_rows(monkeypatch, receipt) -> None:
    gateway = _Gateway(positions=[], snapshots={})
    monkeypatch.setattr(gateway, "get_trading_days_with_receipt", lambda **_kwargs: receipt)
    _wire(monkeypatch, gateway)

    snapshot = _fetch()

    assert snapshot.complete is False
    assert snapshot.rows == snapshot.trading_days == []
    assert snapshot.snapshot_input["completeness"] == "unknown"
    assert snapshot.error_code == "VALUEERROR"
    assert snapshot.error_message in {
        "OpenD trading calendar receipt is incomplete",
        "OpenD trading calendar rows are invalid",
    }
    assert gateway.closed is True


@pytest.mark.parametrize("row, message", [
    ({}, "date is invalid"),
    ({"time": "not-a-date"}, "date is invalid"),
    ({"time": "2026-07-32"}, "date is invalid"),
    ({"time": "bad", "trade_date_type": "NON_TRADING"}, "date is invalid"),
    ({"time": "2026-07-31", "trade_date_type": "UNKNOWN"}, "session kind is invalid"),
])
def test_calendar_rejects_invalid_dates_and_unknown_sessions(monkeypatch, row, message) -> None:
    gateway = _Gateway(positions=[], snapshots={})
    monkeypatch.setattr(gateway, "get_trading_days", lambda **_kwargs: [row])
    _wire(monkeypatch, gateway)

    snapshot = _fetch()

    assert snapshot.complete is False
    assert snapshot.error_code == "VALUEERROR"
    assert snapshot.error_message == f"OpenD trading calendar {message}"
    assert snapshot.snapshot_input["completeness"] == "unknown"
    assert snapshot.trading_days == []
    assert gateway.closed is True


@pytest.mark.parametrize("field", ["time", "date", "trade_date"])
@pytest.mark.parametrize("kind", [None, "", "WHOLE", "MORNING", "AFTERNOON", "TRADING"])
def test_calendar_preserves_date_aliases_and_supported_sessions(monkeypatch, field, kind) -> None:
    gateway = _Gateway(positions=[], snapshots={})
    row = {field: "2026-07-31 09:30:00", "trade_date_type": kind}
    monkeypatch.setattr(gateway, "get_trading_days", lambda **_kwargs: [row])
    _wire(monkeypatch, gateway)

    snapshot = _fetch()

    assert snapshot.complete is True
    assert snapshot.trading_days == [date(2026, 7, 31)]
    assert gateway.closed is True


@pytest.mark.parametrize("rows, expected", [
    ([], []),
    ([{"time": "2026-07-31", "trade_date_type": "NON_TRADING"}], []),
    ([{"time": "2026-07-30", "trade_date_type": "NON_TRADING"},
      {"time": "2026-07-31", "trade_date_type": "WHOLE"}], [date(2026, 7, 31)]),
])
def test_complete_empty_and_nontrading_calendars_remain_valid(monkeypatch, rows, expected) -> None:
    gateway = _Gateway(positions=[], snapshots={})
    monkeypatch.setattr(gateway, "get_trading_days", lambda **_kwargs: rows)
    _wire(monkeypatch, gateway)

    snapshot = _fetch()

    assert snapshot.complete is True
    assert snapshot.snapshot_input["completeness"] == "complete"
    assert snapshot.trading_days == expected
    assert gateway.closed is True


@pytest.mark.parametrize("response", [None, [None], [{"code": "US.NVDA", "qty": 100, "acc_id": "999"}], [{"code": "US.NVDA", "qty": 100, "trd_env": "SIMULATE"}]])
def test_adapter_never_treats_unknown_or_wrong_account_response_as_empty(monkeypatch, response) -> None:
    gateway = _Gateway(positions=[], snapshots={})
    gateway.get_positions = lambda **kwargs: response
    _wire(monkeypatch, gateway)
    snapshot = _fetch(market="us")
    assert snapshot.complete is False
    assert snapshot.snapshot_input["completeness"] == "unknown"
    assert snapshot.snapshot_input["errors"]


def test_adapter_scopes_contract_term_snapshot_to_requested_market(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[
            _position("US.NVDA260821P100000"),
            _position("HK.BAD", qty=-2),
            _position("HK.TCH260731P440000", qty=0),
        ],
        snapshots={
            "US.NVDA260821P100000": _snapshot_row(
                "US.NVDA260821P100000",
                option_contract_multiplier=100,
                option_valid=True,
            ),
        },
    )
    _wire(monkeypatch, gateway)
    monkeypatch.setattr("src.application.futu_option_terms._MARKET_SNAPSHOT_BATCH_SIZE", 1)

    snapshot = _fetch(market="us")

    assert snapshot.complete is True
    assert [row.get("options_per_contract") for row in snapshot.rows] == [
        100.0,
    ]
    assert [row.get("option_strike_price") for row in snapshot.rows] == [
        99.5,
    ]
    assert [row.get("option_terms_source") for row in snapshot.rows] == [
        "market_snapshot",
    ]
    assert gateway.snapshot_calls == [
        ["US.NVDA260821P100000"],
    ]
    assert gateway.closed is True


def test_adapter_fails_closed_when_current_option_terms_are_missing(
    monkeypatch,
) -> None:
    gateway = _Gateway(positions=[_position("HK.POP260828P145000")], snapshots={})
    _wire(monkeypatch, gateway)

    snapshot = _fetch(market="hk")

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_TERMS_EVIDENCE_INCOMPLETE"
    assert "1 non-zero option position" in str(snapshot.error_message)
    assert gateway.closed is True


@pytest.mark.parametrize("code", ["NVDA_OPTION", "NVDA.OPTION"])
def test_adapter_fails_closed_when_nonzero_option_market_is_ambiguous(
    monkeypatch,
    code,
) -> None:
    gateway = _Gateway(positions=[_position(code)], snapshots={})
    _wire(monkeypatch, gateway)

    snapshot = _fetch(market="us")

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_TERMS_EVIDENCE_INCOMPLETE"
    assert "market identity" in str(snapshot.error_message)
    assert gateway.snapshot_calls == []


@pytest.mark.parametrize("option_valid", [None, False, "true", 1])
def test_adapter_requires_explicit_boolean_option_valid(
    monkeypatch,
    option_valid,
) -> None:
    snapshot_row = _snapshot_row("US.NVDA260821P100000", option_contract_size=100)
    if option_valid is not None:
        snapshot_row["option_valid"] = option_valid
    gateway = _Gateway(
        positions=[_position("US.NVDA260821P100000")],
        snapshots={"US.NVDA260821P100000": snapshot_row},
    )
    _wire(monkeypatch, gateway)

    snapshot = _fetch(market="us")

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_TERMS_EVIDENCE_INCOMPLETE"


def test_adapter_requires_snapshot_stock_owner_without_code_fallback(
    monkeypatch,
) -> None:
    code = "US.NVDA260821P100000"
    missing_owner = _snapshot_row(code, option_contract_size=100, option_valid=True)
    missing_owner.pop("stock_owner")
    gateway = _Gateway(
        positions=[_position(code)],
        snapshots={code: missing_owner},
    )
    _wire(monkeypatch, gateway)

    snapshot = _fetch(market="us")

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_TERMS_EVIDENCE_INCOMPLETE"


def test_adapter_fails_closed_when_current_option_multiplier_is_missing(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[_position("HK.POP260828P145000")],
        snapshots={
            "HK.POP260828P145000": _snapshot_row(
                "HK.POP260828P145000",
                stock_owner="HK.09992",
                strike_time="2026-08-28",
                option_strike_price=144.5,
                option_valid=True,
            )
        },
    )
    _wire(monkeypatch, gateway)

    snapshot = _fetch(market="hk")

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_MULTIPLIER_EVIDENCE_INCOMPLETE"
    assert "1 non-zero option position" in str(snapshot.error_message)
    assert gateway.closed is True


def test_adapter_fails_closed_when_current_option_multiplier_fields_conflict(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[_position("US.NVDA260821P100000")],
        snapshots={
            "US.NVDA260821P100000": _snapshot_row(
                "US.NVDA260821P100000",
                option_contract_multiplier=100,
                option_contract_size=95,
                option_valid=True,
            )
        },
    )
    _wire(monkeypatch, gateway)

    snapshot = _fetch(market="us")

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_MULTIPLIER_EVIDENCE_INCOMPLETE"
    assert "1 non-zero option position" in str(snapshot.error_message)
    assert gateway.closed is True


@pytest.mark.parametrize(("strike", "multiplier"), [("99.5", "100"), ("100", "101"), ("99.5", "101")])
def test_adapter_snapshot_contract_drift_blocks_dataset_immediately(monkeypatch, strike, multiplier) -> None:
    code = "US.NVDA260717P100000"
    gateway = _Gateway(
        positions=[_position(code)],
        snapshots={code: _snapshot_row(
            code, strike_time="2026-07-17", option_strike_price=strike,
            option_contract_multiplier=multiplier, option_valid=True)},
    )
    _wire(monkeypatch, gateway)
    snapshot = _fetch(market="us")
    local_lot = _local_lot("lot-nvda", contracts_closed=0)

    dataset, _ = build_position_dataset(
        snapshot=snapshot, local_lots=[local_lot], account="lx", market="us",
        observed_at_utc=snapshot.observed_at_utc, now=datetime.now(timezone.utc), control_state={},
    )

    assert snapshot.complete is True
    assert snapshot.snapshot_input["rows"][0]["instrument_ref"]["source_code"] == code
    assert dataset["status"] == "untrusted"
    assert dataset["usable_for"] == []
    assert set(dataset["blocked_consumers"]) == {"option_position_report", "lifecycle", "close_advice"}
    convergence = next(check for check in dataset["checks"] if check["check_id"] == "OM-POS-002")
    assert convergence["reason_code"] == "POSITION_CONTRACT_TERMS_DRIFT"
    assert convergence["observed"]["contract_terms_drifts"] == [{
        "symbol": "NVDA", "option_type": "put", "expiration": "2026-07-17", "quantity": "-1",
        "local_contracts": "100@100:-1", "opend_contracts": f"{strike}@{multiplier}:-1",
        "broker_code": code, "mapping": "code_lineage",
    }]


@pytest.mark.parametrize("deliverable", [None, {}, {"symbol": "NVDA", "quantity": "10", "cash": "9000"}])
def test_adapter_snapshot_rejects_unsupported_deliverable_before_dataset_comparison(monkeypatch, deliverable) -> None:
    code = "US.NVDA260717P100000"
    position = _position(code)
    if deliverable is not None:
        position["deliverable"] = deliverable
    gateway = _Gateway(
        positions=[position],
        snapshots={code: _snapshot_row(
            code, strike_time="2026-07-17", option_strike_price="100",
            option_contract_multiplier="100", option_valid=True)},
    )
    _wire(monkeypatch, gateway)
    snapshot = _fetch(market="us")
    local = _local_lot("ordinary-lot")
    result, _ = build_position_dataset(
        snapshot=snapshot, local_lots=[local], account="lx", market="us",
        observed_at_utc=snapshot.observed_at_utc, now=datetime.now(timezone.utc), control_state={},
    )
    assert snapshot.snapshot_input["rows"][0]["instrument_ref"].get("deliverable") == deliverable
    assert gateway.closed is True
    if deliverable:
        reason = "unsupported:rows.0.instrument_ref.deliverable"
        assert snapshot.complete is False
        assert snapshot.error_code == "OPEND_POSITION_INPUT_INVALID"
        assert reason in snapshot.snapshot_input["errors"]
        assert snapshot.snapshot_input["source_payload"]["rows"][0]["deliverable"] == deliverable
        assert result["status"] == "unavailable"
        assert result["usable_for"] == []
        assert set(result["blocked_consumers"]) == {"option_position_report", "lifecycle", "close_advice"}
        assert reason in result["checks"][0]["observed"]["snapshot_errors"]
    else:
        assert snapshot.complete is True
        assert snapshot.snapshot_input["errors"] == []
        assert result["status"] == "trusted"
        assert set(result["usable_for"]) == {"option_position_report", "lifecycle", "close_advice"}
