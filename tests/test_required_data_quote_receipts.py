from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from src.application.opend_symbol_outputs import (
    find_fresh_required_data_quote_receipts,
    publish_required_data_quote_snapshot,
    resolve_exact_fresh_required_data_quote_receipt,
    save_outputs,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    validate_source_receipt,
)
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
HOST = "127.0.0.1"
PORT = 11111
CONTRACT_CODE = "NVDA260821P00100000"


def _fetch_plan(
    *,
    projection_outcome: str = "success_rows",
    observed_at: datetime = NOW,
    completed_at: datetime = NOW + timedelta(seconds=1),
) -> dict[str, object]:
    if projection_outcome == "success_empty":
        return {
            "symbol": "NVDA",
            "spot_reference": None,
            "side_plans": [],
            "merged_requests": [],
            "require_realized_volatility": False,
            "expiration_discovery_complete": True,
            "expiration_discovery_error": None,
            "expiration_discovery": {
                "outcome": "success_empty",
                "reason_code": "no_expirations",
                "expirations": [],
                "observed_at_utc": observed_at.isoformat(),
                "completed_at_utc": completed_at.isoformat(),
                "request_identity": {
                    "symbol": "NVDA",
                    "underlier": "US.NVDA",
                    "source": "opend",
                    "host": HOST,
                    "port": PORT,
                    "trading_date": "2026-07-27",
                },
                "error": None,
            },
            "projection_outcome": "success_empty",
            "projected_expirations": [],
        }
    side_plan = {
        "option_type": "put",
        "min_dte": 20,
        "max_dte": 30,
        "explicit_expirations": ["2026-08-21"],
        "strike_window": {
            "min_strike": 100.0,
            "max_strike": 100.0,
            "source": "fixture",
            "buffer_applied": False,
            "buffer_pct": 0.0,
            "base_min_strike": 100.0,
            "base_max_strike": 100.0,
        },
        "planning_reason": "fixture",
        "source_fields": ["sell_put"],
        "spot_reference": 110.0,
        "min_strike": 100.0,
        "max_strike": 100.0,
        "expiration_count": 1,
        "required_exact_strikes_by_expiration": {
            "2026-08-21": [100.0],
        },
    }
    return {
        "symbol": "NVDA",
        "spot_reference": 110.0,
        "side_plans": [side_plan],
        "merged_requests": [
            {
                "symbol": "NVDA",
                "limit_expirations": 8,
                "host": HOST,
                "port": PORT,
                "option_types": ["put"],
                "explicit_expirations": ["2026-08-21"],
                "min_dte": 20,
                "max_dte": 30,
                "side_strike_windows": {
                    "put": {"min_strike": 100.0, "max_strike": 100.0}
                },
                "include_realized_volatility": True,
                "trading_date": "2026-07-27",
                "side_plans": [side_plan],
                "planning_reason": "fixture",
            }
        ],
        "require_realized_volatility": True,
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": "success_rows",
            "reason_code": None,
            "expirations": ["2026-08-21"],
            "observed_at_utc": observed_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "request_identity": {
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": HOST,
                "port": PORT,
                "trading_date": "2026-07-27",
            },
            "error": None,
        },
        "projection_outcome": "success_rows",
        "projected_expirations": ["2026-08-21"],
    }


def _contract(fetch_plan: dict[str, object]) -> dict[str, object]:
    return build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=fetch_plan,
        source="opend",
        host=HOST,
        port=PORT,
    )


@pytest.mark.parametrize(
    ("case", "error_match"),
    [
        ("missing_projection", "projection outcome is missing"),
        ("missing_requests", "merged requests are invalid"),
        ("empty_requests", "lacks fetch requests"),
        ("bad_option_types", "option types are invalid"),
        ("bad_expirations", "request expirations are invalid"),
        ("no_expiration_targets", "lacks expiration targets"),
        ("bad_strike_windows", "strike windows are invalid"),
        ("bad_rv_flag", "RV flag is invalid"),
    ],
)
def test_expected_contract_builder_rejects_malformed_exact_fetch_plan(
    case: str,
    error_match: str,
) -> None:
    plan = json.loads(json.dumps(_fetch_plan()))
    if case == "missing_projection":
        plan.pop("projection_outcome")
    elif case == "missing_requests":
        plan.pop("merged_requests")
    elif case == "empty_requests":
        plan["merged_requests"] = []
    else:
        requests = plan["merged_requests"]
        assert isinstance(requests, list) and isinstance(requests[0], dict)
        if case == "bad_option_types":
            requests[0]["option_types"] = "put"
        elif case == "bad_expirations":
            requests[0]["explicit_expirations"] = "2026-08-21"
        elif case == "no_expiration_targets":
            requests[0]["explicit_expirations"] = []
        elif case == "bad_strike_windows":
            requests[0]["side_strike_windows"] = []
        else:
            requests[0]["include_realized_volatility"] = "yes"

    with pytest.raises(ValueError, match=error_match):
        _contract(plan)


def _policy() -> dict[str, object]:
    return {
        "source": "opend",
        "host": HOST,
        "port": PORT,
        "max_wait_sec": 30,
    }


def _required_payload(
    *,
    mid: float = 1.1,
    observed_at: datetime = NOW,
    completed_at: datetime = NOW + timedelta(seconds=1),
) -> dict[str, object]:
    return {
        "symbol": "NVDA",
        "underlier_code": "US.NVDA",
        "rows": [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-08-21",
                "dte": 25,
                "contract_symbol": CONTRACT_CODE,
                "strike": 100,
                "spot": 110,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": mid,
                "mid": mid,
                "volume": 50,
                "open_interest": 100,
                "implied_volatility": 0.25,
                "realized_volatility_20": 0.2,
                "realized_volatility_60": 0.2,
                "realized_volatility_120": 0.2,
                "realized_volatility_estimate": 0.2,
                "currency": "USD",
                "multiplier": 100,
            }
        ],
        "meta": {
            "status": "ok",
            "source": "opend",
            "host": HOST,
            "port": PORT,
            "trading_date": "2026-07-27",
            "source_outcome": "success_rows",
            "source_observed_at": observed_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [CONTRACT_CODE],
            "snapshot_returned_code_set": [CONTRACT_CODE],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": True,
            "snapshot_coverage": {
                "requested": 1,
                "returned": 1,
                "missing": 0,
                "unexpected": 0,
                "complete": True,
            },
            "realized_volatility": {
                "status": "ok",
                "realized_volatility_20": 0.2,
                "realized_volatility_60": 0.2,
                "realized_volatility_120": 0.2,
                "realized_volatility_estimate": 0.2,
            },
        },
    }


def _save_and_publish(
    tmp_path: Path,
    *,
    payload: dict[str, object] | None = None,
    producer_run_id: str = "run-1",
    observed_at: datetime = NOW,
    completed_at: datetime = NOW + timedelta(seconds=1),
) -> tuple[Path, dict[str, object], Path, Path, dict[str, object]]:
    fetch_plan = _fetch_plan(
        observed_at=observed_at,
        completed_at=completed_at,
    )
    expected_contract = _contract(fetch_plan)
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        payload
        or _required_payload(
            observed_at=observed_at,
            completed_at=completed_at,
        ),
        output_root=tmp_path,
    )
    receipt_path, receipt = publish_required_data_quote_snapshot(
        producer_root=tmp_path,
        producer_run_id=producer_run_id,
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=fetch_plan,
        fetch_policy=_policy(),
        expected_fetch_contract=expected_contract,
        source_observed_at=observed_at,
        completed_at=completed_at,
        now=completed_at,
    )
    return receipt_path, receipt, raw_path, csv_path, expected_contract


def test_quote_receipt_binds_exact_json_csv_and_fetch_policy(
    tmp_path: Path,
) -> None:
    receipt_path, receipt, raw_path, csv_path, expected_contract = (
        _save_and_publish(tmp_path)
    )
    validated = validate_source_receipt(
        receipt,
        producer_root=tmp_path,
        now=NOW + timedelta(seconds=3),
        expected_source_kind="quotes",
    )
    bundle = json.loads(validated["payload_path"].read_text(encoding="utf-8"))

    assert base64.b64decode(bundle["raw_json_base64"]) == raw_path.read_bytes()
    assert base64.b64decode(bundle["required_data_csv_base64"]) == csv_path.read_bytes()
    assert bundle["fetch_plan"]["symbol"] == "NVDA"
    assert bundle["expected_fetch_contract"] == expected_contract
    assert bundle["fetch_policy"] == _policy()
    assert receipt_path.is_file()


def test_quote_receipt_rejects_partial_required_data_payload(
    tmp_path: Path,
) -> None:
    payload = _required_payload()
    payload["meta"] = {
        **payload["meta"],
        "status": "partial",
        "errors": [{"expiration": "2026-09-18", "error_code": "RATE_LIMIT"}],
    }
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        payload,
        output_root=tmp_path,
    )
    fetch_plan = _fetch_plan()

    with pytest.raises(
        PositionAdviceSourceError,
        match="incomplete required-data payload",
    ):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    ("source_outcome", "reason_code"),
    (
        ("provider_error", "RATE_LIMIT"),
        ("parse_error", "INVALID_RESPONSE"),
        ("not_attempted", "BUDGET_EXHAUSTED"),
        ("success_rows", "RATE_LIMIT"),
    ),
)
def test_quote_receipt_rejects_rows_with_non_success_source_evidence(
    tmp_path: Path,
    source_outcome: str,
    reason_code: str,
) -> None:
    payload = _required_payload()
    payload["meta"] = {
        **payload["meta"],
        "source_outcome": source_outcome,
        "reason_code": reason_code,
    }
    raw_path, csv_path = save_outputs(tmp_path, "NVDA", payload, output_root=tmp_path)
    fetch_plan = _fetch_plan()

    with pytest.raises(
        PositionAdviceSourceError,
        match="non-success required-data payload contains rows",
    ):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


def test_quote_receipt_rejects_missing_explicit_source_outcome(tmp_path: Path) -> None:
    payload = _required_payload()
    payload["meta"].pop("source_outcome")
    raw_path, csv_path = save_outputs(tmp_path, "NVDA", payload, output_root=tmp_path)
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError, match="explicit success-rows"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


def test_quote_receipt_rejects_missing_top_level_symbol(tmp_path: Path) -> None:
    payload = _required_payload()
    payload.pop("symbol")
    raw_path, csv_path = save_outputs(tmp_path, "NVDA", payload, output_root=tmp_path)
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError, match="payload symbol mismatch"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


def test_quote_receipt_rejects_wrong_physical_binding(tmp_path: Path) -> None:
    payload = _required_payload()
    payload["meta"]["port"] = PORT + 1
    raw_path, csv_path = save_outputs(tmp_path, "NVDA", payload, output_root=tmp_path)
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError, match="payload port mismatch"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


@pytest.mark.parametrize("invalid_port", [True, 11111.0])
def test_quote_receipt_rejects_non_integer_raw_port(
    tmp_path: Path,
    invalid_port: object,
) -> None:
    payload = _required_payload()
    payload["meta"]["port"] = invalid_port
    raw_path, csv_path = save_outputs(tmp_path, "NVDA", payload, output_root=tmp_path)
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError, match="payload port is invalid"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )

    assert list(
        tmp_path.glob("position_advice_sources/quotes/**/receipt.json")
    ) == []


@pytest.mark.parametrize("projection_outcome", ["success_rows", "success_empty"])
def test_quote_receipt_rejects_wrong_raw_underlier_identity(
    tmp_path: Path,
    projection_outcome: str,
) -> None:
    if projection_outcome == "success_rows":
        payload = _required_payload()
        payload["underlier_code"] = "US.AAPL"
    else:
        payload = {
            "symbol": "NVDA",
            "underlier_code": "US.AAPL",
            "expiration_count": 0,
            "expirations": [],
            "rows": [],
            "meta": {
                "status": "ok",
                "source": "opend",
                "host": HOST,
                "port": PORT,
                "trading_date": "2026-07-27",
                "source_outcome": "success_empty",
                "reason_code": "no_expirations",
                "source_observed_at": NOW.isoformat(),
                "completed_at_utc": (NOW + timedelta(seconds=1)).isoformat(),
                "snapshot_requested_codes": 0,
                "snapshot_returned_codes": 0,
                "snapshot_missing_codes": 0,
                "snapshot_unexpected_codes": 0,
                "snapshot_requested_code_set": [],
                "snapshot_returned_code_set": [],
                "snapshot_missing_code_set": [],
                "snapshot_unexpected_code_set": [],
                "snapshot_complete": True,
                "realized_volatility": {
                    "status": "not_applicable_no_contracts"
                },
            },
        }
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        payload,
        output_root=tmp_path,
    )
    fetch_plan = _fetch_plan(projection_outcome=projection_outcome)

    with pytest.raises(PositionAdviceSourceError, match="underlier identity"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )
    assert list(
        tmp_path.glob("position_advice_sources/quotes/**/receipt.json")
    ) == []


def test_quote_receipt_rejects_incomplete_snapshot_evidence(tmp_path: Path) -> None:
    payload = _required_payload()
    payload["meta"].update(
        {
            "snapshot_returned_codes": 0,
            "snapshot_missing_codes": 1,
            "snapshot_returned_code_set": [],
            "snapshot_missing_code_set": [CONTRACT_CODE],
            "snapshot_complete": False,
            "snapshot_coverage": {
                "requested": 1,
                "returned": 0,
                "missing": 1,
                "unexpected": 0,
                "complete": False,
            },
        }
    )
    raw_path, csv_path = save_outputs(tmp_path, "NVDA", payload, output_root=tmp_path)
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError, match=r"^provider_incomplete:"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


def test_quote_receipt_rejects_csv_identity_subset(tmp_path: Path) -> None:
    payload = _required_payload()
    second = dict(payload["rows"][0])
    second.update(
        {
            "contract_symbol": "NVDA260821P00105000",
            "strike": 100,
        }
    )
    payload["rows"].append(second)
    payload["meta"].update(
        {
            "snapshot_requested_codes": 2,
            "snapshot_returned_codes": 2,
            "snapshot_requested_code_set": [
                CONTRACT_CODE,
                "NVDA260821P00105000",
            ],
            "snapshot_returned_code_set": [
                CONTRACT_CODE,
                "NVDA260821P00105000",
            ],
        }
    )
    raw_path, csv_path = save_outputs(tmp_path, "NVDA", payload, output_root=tmp_path)
    pd.read_csv(csv_path).iloc[:1].to_csv(csv_path, index=False)
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError, match="row counts differ"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


def test_quote_receipt_rejects_bad_required_realized_volatility(tmp_path: Path) -> None:
    payload = _required_payload()
    payload["meta"]["realized_volatility"] = {
        "status": "error",
        "realized_volatility_estimate": None,
    }
    raw_path, csv_path = save_outputs(tmp_path, "NVDA", payload, output_root=tmp_path)
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError, match="required realized volatility"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


def test_direct_publisher_cannot_bypass_strict_evidence(tmp_path: Path) -> None:
    weak_payload = {
        "symbol": "NVDA",
        "rows": _required_payload()["rows"],
        "meta": {"status": "ok", "source": "opend"},
    }
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        weak_payload,
        output_root=tmp_path,
    )
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )


def test_direct_publisher_rejects_wrong_plan_before_commit(tmp_path: Path) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    expected_plan = _fetch_plan()
    wrong_plan = {**expected_plan, "projected_expirations": []}

    with pytest.raises(
        PositionAdviceSourceError,
        match="fetch plan contradicts expected contract",
    ):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=wrong_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(expected_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )

    assert list(tmp_path.glob("position_advice_sources/quotes/**/*.json")) == []


def test_stale_quote_is_rejected_before_any_immutable_commit(tmp_path: Path) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    fetch_plan = _fetch_plan()

    with pytest.raises(PositionAdviceSourceError, match="stale"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(minutes=31),
        )

    assert list(tmp_path.glob("position_advice_sources/quotes/**/payload.json")) == []
    assert list(tmp_path.glob("position_advice_sources/quotes/**/receipt.json")) == []


def test_quote_commit_time_resamples_clock_and_leaves_only_orphan_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application import opend_symbol_outputs as outputs

    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    fetch_plan = _fetch_plan()

    class _AdvancingDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            cls.calls += 1
            value = NOW + timedelta(minutes=29 if cls.calls <= 2 else 31)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(outputs, "datetime", _AdvancingDateTime)
    with pytest.raises(PositionAdviceSourceError, match="observation is stale"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-crosses-ttl",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
        )

    assert _AdvancingDateTime.calls == 3
    assert len(
        list(tmp_path.glob("position_advice_sources/quotes/**/payload.json"))
    ) == 1
    assert list(tmp_path.glob("position_advice_sources/quotes/**/receipt.json")) == []


def test_receipt_last_crash_reentry_adopts_exact_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application import opend_symbol_outputs as outputs

    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    fetch_plan = _fetch_plan()
    expected_contract = _contract(fetch_plan)
    real_publish = outputs.publish_source_receipt

    def _publish_then_crash(**kwargs):
        real_publish(**kwargs)
        raise RuntimeError("fault after receipt commit")

    monkeypatch.setattr(outputs, "publish_source_receipt", _publish_then_crash)
    with pytest.raises(RuntimeError, match="after receipt commit"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=expected_contract,
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            now=NOW + timedelta(seconds=1),
        )

    receipt_paths = list(
        tmp_path.glob("position_advice_sources/quotes/**/receipt.json")
    )
    payload_paths = list(
        tmp_path.glob("position_advice_sources/quotes/**/payload.json")
    )
    assert len(receipt_paths) == 1
    assert len(payload_paths) == 1
    committed_receipt_bytes = receipt_paths[0].read_bytes()
    committed_payload_bytes = payload_paths[0].read_bytes()

    monkeypatch.setattr(outputs, "publish_source_receipt", real_publish)
    adopted_path, adopted_receipt = publish_required_data_quote_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-1",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=fetch_plan,
        fetch_policy=_policy(),
        expected_fetch_contract=expected_contract,
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        now=NOW + timedelta(seconds=2),
    )

    assert adopted_path == receipt_paths[0]
    assert adopted_receipt == json.loads(committed_receipt_bytes)
    assert receipt_paths[0].read_bytes() == committed_receipt_bytes
    assert payload_paths[0].read_bytes() == committed_payload_bytes
    assert len(list(tmp_path.glob("position_advice_sources/quotes/**/receipt.json"))) == 1
    assert len(list(tmp_path.glob("position_advice_sources/quotes/**/payload.json"))) == 1


def test_cache_discovery_reuses_receipt_observation_after_shared_files_change(
    tmp_path: Path,
) -> None:
    receipt_path, receipt, _raw_path, _csv_path, _contract_value = (
        _save_and_publish(tmp_path)
    )

    save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(mid=2.2),
        output_root=tmp_path,
    )
    found = find_fresh_required_data_quote_receipts(
        producer_root=tmp_path,
        symbols=["NVDA"],
        now=NOW + timedelta(minutes=10),
    )

    assert found == {
        "NVDA": receipt_path.resolve().relative_to(tmp_path.resolve()).as_posix()
    }
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["source_observed_at"] == receipt["source_observed_at"]


def test_same_run_rejects_second_quote_observation(tmp_path: Path) -> None:
    first_path, first, _raw_path, _csv_path, _contract_value = (
        _save_and_publish(tmp_path)
    )
    second_observed_at = NOW + timedelta(seconds=2)
    second_completed_at = NOW + timedelta(seconds=3)
    fetch_plan = _fetch_plan(
        observed_at=second_observed_at,
        completed_at=second_completed_at,
    )
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(
            observed_at=second_observed_at,
            completed_at=second_completed_at,
        ),
        output_root=tmp_path,
    )

    with pytest.raises(PositionAdviceSourceError, match="conflicts with committed"):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy=_policy(),
            expected_fetch_contract=_contract(fetch_plan),
            source_observed_at=second_observed_at,
            completed_at=second_completed_at,
            now=second_completed_at,
        )

    assert first_path.is_file()
    assert first["source_observed_at"] == "2026-07-27T10:00:00Z"


def test_exact_same_run_reentry_adopts_committed_receipt(tmp_path: Path) -> None:
    first_path, first, raw_path, csv_path, expected_contract = _save_and_publish(
        tmp_path
    )
    fetch_plan = _fetch_plan()

    second_path, second = publish_required_data_quote_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-1",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=fetch_plan,
        fetch_policy=_policy(),
        expected_fetch_contract=expected_contract,
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        now=NOW + timedelta(seconds=1),
    )

    assert second_path == first_path
    assert second == first


def test_exact_receipt_resolution_rejects_mutated_scan_bytes(
    tmp_path: Path,
) -> None:
    receipt_path, receipt, _raw_path, csv_path, expected_contract = (
        _save_and_publish(tmp_path)
    )

    exact = resolve_exact_fresh_required_data_quote_receipt(
        producer_root=tmp_path,
        symbol="NVDA",
        now=NOW + timedelta(minutes=10),
        expected_producer_run_id="run-1",
        expected_fetch_contract=expected_contract,
        expected_source_observed_at=NOW,
        expected_completed_at=NOW + timedelta(seconds=1),
    )
    assert exact is not None
    assert exact["receipt_relpath"] == (
        receipt_path.resolve().relative_to(tmp_path.resolve()).as_posix()
    )
    assert exact["snapshot_id"] == receipt["snapshot_id"]

    csv_path.write_text(
        csv_path.read_text(encoding="utf-8").replace("1.1", "2.2"),
        encoding="utf-8",
    )

    assert (
        resolve_exact_fresh_required_data_quote_receipt(
            producer_root=tmp_path,
            symbol="NVDA",
            now=NOW + timedelta(minutes=10),
            expected_producer_run_id="run-1",
            expected_fetch_contract=expected_contract,
        )
        is None
    )


def test_cache_discovery_does_not_return_stale_receipt(tmp_path: Path) -> None:
    _save_and_publish(tmp_path)

    assert (
        find_fresh_required_data_quote_receipts(
            producer_root=tmp_path,
            symbols=["NVDA"],
            now=NOW + timedelta(minutes=30),
        )
        == {}
    )
