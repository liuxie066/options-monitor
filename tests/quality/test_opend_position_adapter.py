from __future__ import annotations

from datetime import datetime, timezone

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

    def get_snapshot(self, codes):  # noqa: ANN001, ANN201
        batch = list(codes)
        self.snapshot_calls.append(batch)
        return [self.snapshots[code] for code in batch if code in self.snapshots]

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("response", [None, [None], [{"code": "US.NVDA", "qty": 100, "acc_id": "999"}], [{"code": "US.NVDA", "qty": 100, "trd_env": "SIMULATE"}]])
def test_adapter_never_treats_unknown_or_wrong_account_response_as_empty(monkeypatch, response) -> None:
    gateway = _Gateway(positions=[], snapshots={})
    gateway.get_positions = lambda **kwargs: response
    monkeypatch.setattr(adapter_module, "build_ready_futu_broker_gateway", lambda **kwargs: gateway)
    monkeypatch.setattr(adapter_module, "build_ready_futu_quote_gateway", lambda **kwargs: gateway)
    snapshot = OpenDOptionPositionAdapter().fetch(cfg=_config(), account="lx", market="us")
    assert snapshot.complete is False
    assert snapshot.snapshot_input["completeness"] == "unknown"
    assert snapshot.snapshot_input["errors"]


def test_adapter_scopes_contract_term_snapshot_to_requested_market(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[
            {
                "code": "US.NVDA260821P100000",
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            },
            {
                "code": "HK.BAD",
                "qty": -2,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            },
            {
                "code": "HK.TCH260731P440000",
                "qty": 0,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            },
        ],
        snapshots={
            "US.NVDA260821P100000": {
                "code": "US.NVDA260821P100000",
                "stock_owner": "US.NVDA",
                "option_type": "PUT",
                "strike_time": "2026-08-21",
                "option_strike_price": 99.5,
                "option_contract_multiplier": 100,
                "option_valid": True,
            },
        },
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_broker_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(adapter_module, "_MARKET_SNAPSHOT_BATCH_SIZE", 1)

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="us",
    )

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
    gateway = _Gateway(
        positions=[
            {
                "code": "HK.POP260828P145000",
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            }
        ],
        snapshots={},
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_broker_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: gateway,
    )

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="hk",
    )

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
    gateway = _Gateway(
        positions=[
            {
                "code": code,
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            }
        ],
        snapshots={},
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_broker_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: gateway,
    )

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="us",
    )

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
    snapshot_row = {
        "code": "US.NVDA260821P100000",
        "stock_owner": "US.NVDA",
        "option_type": "PUT",
        "strike_time": "2026-08-21",
        "option_strike_price": 99.5,
        "option_contract_size": 100,
    }
    if option_valid is not None:
        snapshot_row["option_valid"] = option_valid
    gateway = _Gateway(
        positions=[
            {
                "code": "US.NVDA260821P100000",
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            }
        ],
        snapshots={"US.NVDA260821P100000": snapshot_row},
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_broker_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: gateway,
    )

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="us",
    )

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_TERMS_EVIDENCE_INCOMPLETE"


def test_adapter_requires_snapshot_stock_owner_without_code_fallback(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[
            {
                "code": "US.NVDA260821P100000",
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            }
        ],
        snapshots={
            "US.NVDA260821P100000": {
                "code": "US.NVDA260821P100000",
                "option_type": "PUT",
                "strike_time": "2026-08-21",
                "option_strike_price": 99.5,
                "option_contract_size": 100,
                "option_valid": True,
            }
        },
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_broker_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: gateway,
    )

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="us",
    )

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_TERMS_EVIDENCE_INCOMPLETE"


def test_adapter_fails_closed_when_current_option_multiplier_is_missing(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[
            {
                "code": "HK.POP260828P145000",
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            }
        ],
        snapshots={
            "HK.POP260828P145000": {
                "code": "HK.POP260828P145000",
                "stock_owner": "HK.09992",
                "option_type": "PUT",
                "strike_time": "2026-08-28",
                "option_strike_price": 144.5,
                "option_valid": True,
            }
        },
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_broker_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: gateway,
    )

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="hk",
    )

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_MULTIPLIER_EVIDENCE_INCOMPLETE"
    assert "1 non-zero option position" in str(snapshot.error_message)
    assert gateway.closed is True


def test_adapter_fails_closed_when_current_option_multiplier_fields_conflict(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[
            {
                "code": "US.NVDA260821P100000",
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            }
        ],
        snapshots={
            "US.NVDA260821P100000": {
                "code": "US.NVDA260821P100000",
                "stock_owner": "US.NVDA",
                "option_type": "PUT",
                "strike_time": "2026-08-21",
                "option_strike_price": 99.5,
                "option_contract_multiplier": 100,
                "option_contract_size": 95,
                "option_valid": True,
            }
        },
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_broker_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: gateway,
    )

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="us",
    )

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_MULTIPLIER_EVIDENCE_INCOMPLETE"
    assert "1 non-zero option position" in str(snapshot.error_message)
    assert gateway.closed is True


@pytest.mark.parametrize(("strike", "multiplier"), [("99.5", "100"), ("100", "101"), ("99.5", "101")])
def test_adapter_snapshot_contract_drift_blocks_dataset_immediately(monkeypatch, strike, multiplier) -> None:
    code = "US.NVDA260717P100000"
    gateway = _Gateway(
        positions=[{"code": code, "qty": -1, "position_side": "SHORT", "sec_type": "DRVT"}],
        snapshots={code: {
            "code": code, "stock_owner": "US.NVDA", "option_type": "PUT",
            "strike_time": "2026-07-17", "option_strike_price": strike,
            "option_contract_multiplier": multiplier, "option_valid": True,
        }},
    )
    monkeypatch.setattr(adapter_module, "build_ready_futu_broker_gateway", lambda **kwargs: gateway)
    monkeypatch.setattr(adapter_module, "build_ready_futu_quote_gateway", lambda **kwargs: gateway)
    snapshot = OpenDOptionPositionAdapter().fetch(cfg=_config(), account="lx", market="us")
    local_lot = {"record_id": "lot-nvda", "fields": {
        "account": "lx", "broker": "富途", "symbol": "NVDA", "option_type": "put",
        "side": "short", "contracts": 1, "contracts_open": 1, "contracts_closed": 0,
        "strike": 100, "multiplier": 100, "expiration_ymd": "2026-07-17", "status": "open",
    }}

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
    position = {"code": code, "qty": -1, "position_side": "SHORT", "sec_type": "DRVT"}
    if deliverable is not None:
        position["deliverable"] = deliverable
    gateway = _Gateway(
        positions=[position],
        snapshots={code: {
            "code": code, "stock_owner": "US.NVDA", "option_type": "PUT",
            "strike_time": "2026-07-17", "option_strike_price": "100",
            "option_contract_multiplier": "100", "option_valid": True,
        }},
    )
    monkeypatch.setattr(adapter_module, "build_ready_futu_broker_gateway", lambda **kwargs: gateway)
    monkeypatch.setattr(adapter_module, "build_ready_futu_quote_gateway", lambda **kwargs: gateway)
    snapshot = OpenDOptionPositionAdapter().fetch(cfg=_config(), account="lx", market="us")
    local = {"record_id": "ordinary-lot", "fields": {
        "account": "lx", "broker": "富途", "symbol": "NVDA", "option_type": "put",
        "side": "short", "contracts": 1, "contracts_open": 1, "strike": 100,
        "multiplier": 100, "expiration_ymd": "2026-07-17", "status": "open",
    }}
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
