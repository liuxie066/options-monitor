from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
from src.application.opend_symbol_chain_fetching import SymbolOptionChainResult
from src.application.short_vol_metrics import RealizedVolatilitySnapshot
from src.application.opening_quote_evidence import OpeningUnderlierObservation


def _chain_bundle(*, rows: list[dict[str, object]], source_outcome: str) -> SymbolOptionChainResult:
    frame = pd.DataFrame(rows)
    return SymbolOptionChainResult(
        rows=rows,
        expirations_all=["2026-06-19"] if rows else [],
        expirations_pick=["2026-06-19"] if rows else [],
        fetch_meta={
            "status": "ok",
            "error_code": None,
            "errors": [],
            "source_outcome": source_outcome,
            "reason_code": "no_contract_rows" if not rows else None,
            "expiration_statuses": (
                {"2026-06-19": "fetched"} if rows else {}
            ),
            "source_observed_at": "2026-08-04T01:00:00+00:00",
            "completed_at_utc": "2026-08-04T01:00:01+00:00",
        },
        frame=frame,
    )


def _install_symbol_dependencies(monkeypatch, *, chain_bundle: SymbolOptionChainResult) -> None:  # type: ignore[no-untyped-def]
    import src.application.opend_symbol_fetching as mod

    monkeypatch.setattr(mod, "get_trading_date", lambda _market: date(2026, 5, 20))
    monkeypatch.setattr(mod, "fetch_symbol_option_chain", lambda **_kwargs: chain_bundle)


@pytest.mark.parametrize(
    ("rv_status", "rv_estimate"),
    [
        ("missing", None),
        ("error", 0.2),
        ("ok", float("nan")),
        ("ok", float("inf")),
        ("ok", 0.0),
        ("ok", -0.1),
    ],
)
def test_required_realized_volatility_failure_is_typed_overall_error(
    monkeypatch,
    tmp_path: Path,
    rv_status: str,
    rv_estimate: float | None,
) -> None:
    import src.application.opend_symbol_fetching as mod

    code = "US.NVDA.2026-06-19.P100"
    chain_bundle = _chain_bundle(
        rows=[
            {
                "code": code,
                "strike_time": "2026-06-19",
                "strike_price": 100.0,
                "option_type": "PUT",
                "lot_size": 100,
            }
        ],
        source_outcome="success_rows",
    )
    _install_symbol_dependencies(monkeypatch, chain_bundle=chain_bundle)
    completion_events: list[str] = []

    def fetch_rv(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        completion_events.append("rv")
        return RealizedVolatilitySnapshot(
            status=rv_status,
            reason=("provider failure" if rv_status == "error" else None),
            rv_estimate=rv_estimate,
        )

    def fetch_snapshots(**_kwargs):  # type: ignore[no-untyped-def]
        completion_events.append("snapshot")
        return MarketSnapshotFetchResult(
            snap_map={code: {"code": code, "last_price": 1.0}},
            errors=[],
            requested_codes=frozenset({code}),
            returned_codes=frozenset({code}),
            missing_codes=frozenset(),
            unexpected_codes=frozenset(),
            complete=True,
        )

    monkeypatch.setattr(
        mod,
        "fetch_realized_volatility_snapshot",
        fetch_rv,
    )
    monkeypatch.setattr(mod, "fetch_option_snapshots", fetch_snapshots)
    monkeypatch.setattr(
        mod,
        "_utc_now_iso",
        lambda: completion_events.append("completed") or "2026-08-04T01:00:02+00:00",
    )

    payload = mod.fetch_symbol_request(
        mod.FetchSymbolRequest(
            symbol="NVDA",
            base_dir=tmp_path,
            gateway=object(),
            spot_override=100.0,
            option_types="put",
            include_realized_volatility=True,
        )
    )

    assert payload["meta"]["status"] == "error"
    assert payload["meta"]["error_code"] == "REQUIRED_REALIZED_VOLATILITY_INCOMPLETE"
    assert payload["meta"]["snapshot_complete"] is True
    assert payload["meta"]["source_observed_at"] == "2026-08-04T01:00:00+00:00"
    assert payload["meta"]["completed_at_utc"] == "2026-08-04T01:00:02+00:00"
    assert completion_events == ["rv", "snapshot", "completed"]
    assert payload["meta"]["option_chain_scope_coverage"] == {
        "schema_version": "option_chain_scope_coverage.v1",
        "scopes": [
            {
                "option_type": "put",
                "expiration": "2026-06-19",
                "chain_status": "fetched",
                "filtered_contract_codes": [code],
                "filtered_contract_count": 1,
            }
        ],
    }
    assert any(
        item["error_code"] == "REQUIRED_REALIZED_VOLATILITY_INCOMPLETE"
        for item in payload["meta"]["errors"]
    )


def test_success_empty_marks_rv_not_applicable_without_provider_call(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_symbol_fetching as mod

    _install_symbol_dependencies(
        monkeypatch,
        chain_bundle=_chain_bundle(rows=[], source_outcome="success_empty"),
    )

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("no-contract result must not call an RV or option-snapshot provider")

    monkeypatch.setattr(mod, "fetch_realized_volatility_snapshot", forbidden)
    monkeypatch.setattr(mod, "fetch_option_snapshots", forbidden)

    payload = mod.fetch_symbol_request(
        mod.FetchSymbolRequest(
            symbol="NVDA",
            base_dir=tmp_path,
            gateway=object(),
            spot_override=100.0,
            include_realized_volatility=True,
        )
    )

    assert payload["meta"]["status"] == "ok"
    assert payload["meta"]["source_outcome"] == "success_empty"
    assert payload["meta"]["realized_volatility"]["status"] == "not_applicable_no_contracts"
    assert payload["meta"]["snapshot_complete"] is True
    assert payload["meta"]["snapshot_requested_code_set"] == []


def test_fully_fetched_chain_filtered_to_empty_is_normalized_to_success_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_symbol_fetching as mod

    _install_symbol_dependencies(
        monkeypatch,
        chain_bundle=_chain_bundle(
            rows=[
                {
                    "code": "US.NVDA.2026-06-19.P90",
                    "strike_time": "2026-06-19",
                    "strike_price": 90.0,
                    "option_type": "PUT",
                    "lot_size": 100,
                }
            ],
            source_outcome="success_rows",
        ),
    )
    monkeypatch.setattr(
        mod,
        "fetch_option_snapshots",
        lambda **_kwargs: MarketSnapshotFetchResult(
            snap_map={},
            errors=[],
            requested_codes=frozenset(),
            returned_codes=frozenset(),
            missing_codes=frozenset(),
            unexpected_codes=frozenset(),
            complete=True,
        ),
    )

    payload = mod.fetch_symbol_request(
        mod.FetchSymbolRequest(
            symbol="NVDA",
            base_dir=tmp_path,
            gateway=object(),
            spot_override=100.0,
            option_types="put",
            explicit_expirations=["2026-06-19"],
            min_strike=100.0,
            max_strike=100.0,
        )
    )

    assert payload["rows"] == []
    assert payload["meta"]["status"] == "ok"
    assert payload["meta"]["source_outcome"] == "success_empty"
    assert payload["meta"]["reason_code"] == "no_contract_rows"
    assert payload["meta"]["option_chain_scope_coverage"]["scopes"] == [
        {
            "option_type": "put",
            "expiration": "2026-06-19",
            "chain_status": "fetched",
            "filtered_contract_codes": [],
            "filtered_contract_count": 0,
        }
    ]


def test_observed_missing_spot_is_not_fetched_again(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_symbol_fetching as mod

    _install_symbol_dependencies(
        monkeypatch,
        chain_bundle=_chain_bundle(rows=[], source_outcome="success_empty"),
    )

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("an observed missing spot must not be fetched again")

    monkeypatch.setattr(mod, "get_underlier_observation_opend", forbidden)
    monkeypatch.setattr(mod, "fetch_realized_volatility_snapshot", forbidden)
    monkeypatch.setattr(mod, "fetch_option_snapshots", forbidden)

    payload = mod.fetch_symbol_request(
        mod.FetchSymbolRequest(
            symbol="NVDA",
            base_dir=tmp_path,
            gateway=object(),
            spot_override=None,
            fetch_spot_if_missing=False,
        )
    )

    assert payload["spot"] is None
    assert payload["meta"]["spot_snapshot_opend_calls"] == 0
    assert payload["meta"]["source_outcome"] == "success_empty"


def test_malformed_chain_filter_input_is_a_provider_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_symbol_fetching as mod

    _install_symbol_dependencies(
        monkeypatch,
        chain_bundle=_chain_bundle(
            rows=[
                {
                    "code": "US.NVDA.2026-06-19.P100",
                    "strike_time": "2026-06-19",
                    "option_type": "PUT",
                    "lot_size": 100,
                }
            ],
            source_outcome="success_rows",
        ),
    )
    monkeypatch.setattr(
        mod,
        "fetch_option_snapshots",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("malformed chain must fail before snapshots")
        ),
    )

    payload = mod.fetch_symbol_request(
        mod.FetchSymbolRequest(
            symbol="NVDA",
            base_dir=tmp_path,
            gateway=object(),
            spot_override=100.0,
            option_types="put",
        )
    )

    assert payload["meta"]["status"] == "error"
    assert "lacks required filter columns" in str(payload["meta"]["error"])


def test_duplicate_snapshot_code_is_a_typed_overall_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_symbol_fetching as mod

    code = "US.NVDA.2026-06-19.P100"
    _install_symbol_dependencies(
        monkeypatch,
        chain_bundle=_chain_bundle(
            rows=[
                {
                    "code": code,
                    "strike_time": "2026-06-19",
                    "strike_price": 100.0,
                    "option_type": "PUT",
                    "lot_size": 100,
                }
            ],
            source_outcome="success_rows",
        ),
    )
    monkeypatch.setattr(
        mod,
        "fetch_realized_volatility_snapshot",
        lambda *_args, **_kwargs: RealizedVolatilitySnapshot(
            rv_20=0.2,
            rv_estimate=0.2,
            status="ok",
        ),
    )
    monkeypatch.setattr(
        mod,
        "fetch_option_snapshots",
        lambda **_kwargs: MarketSnapshotFetchResult(
            snap_map={},
            errors=[
                {
                    "stage": "market_snapshot_completeness",
                    "error_code": "SNAPSHOT_DUPLICATE_CODES",
                    "message": "provider returned duplicate rows",
                    "duplicate_codes": [code],
                }
            ],
            requested_codes=frozenset({code}),
            returned_codes=frozenset({code}),
            missing_codes=frozenset({code}),
            unexpected_codes=frozenset(),
            complete=False,
        ),
    )

    payload = mod.fetch_symbol_request(
        mod.FetchSymbolRequest(
            symbol="NVDA",
            base_dir=tmp_path,
            gateway=object(),
            spot_override=100.0,
            include_realized_volatility=True,
        )
    )

    assert payload["meta"]["status"] == "error"
    assert payload["meta"]["error_code"] == "SNAPSHOT_COVERAGE_INCOMPLETE"
    assert payload["meta"]["snapshot_complete"] is False
    assert any(
        item["error_code"] == "SNAPSHOT_DUPLICATE_CODES"
        for item in payload["meta"]["errors"]
    )


def test_provider_shaped_contract_rows_preserve_ready_and_minimal_failure_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_symbol_fetching as mod

    ready_code = "US.NVDA260821P00170000"
    conflict_code = "US.NVDA260821P00160000"
    chain_rows = [
        {
            "code": code,
            "strike_time": "2026-08-21",
            "strike_price": strike,
            "option_type": "PUT",
            "lot_size": lot_size,
            "stock_type": "DRVT",
            "stock_owner": "US.NVDA",
            "option_standard_type": "STANDARD",
            "suspension": False,
        }
        for code, strike, lot_size in (
            (ready_code, 170.0, 100),
            (conflict_code, 160.0, 50),
        )
    ]
    _install_symbol_dependencies(
        monkeypatch,
        chain_bundle=_chain_bundle(rows=chain_rows, source_outcome="success_rows"),
    )
    observed_now = datetime.now(timezone.utc)
    update_time = observed_now.astimezone(
        ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %H:%M:%S")
    snapshots = {
        code: {
            "code": code,
            "bid_price": 1.0,
            "ask_price": 1.2,
            "last_price": 9.9,
            "update_time": update_time,
            "price_spread": 0.01,
            "option_implied_volatility": 25.0,
            "option_delta": -0.2,
            "option_open_interest": 0,
            "volume": 0,
            "option_contract_size": 100,
            "sec_status": "NORMAL",
            "suspension": False,
        }
        for code in (ready_code, conflict_code)
    }
    monkeypatch.setattr(
        mod,
        "fetch_option_snapshots",
        lambda **_kwargs: MarketSnapshotFetchResult(
            snap_map=snapshots,
            errors=[],
            requested_codes=frozenset(snapshots),
            returned_codes=frozenset(snapshots),
            missing_codes=frozenset(),
            unexpected_codes=frozenset(),
            complete=True,
        ),
    )
    underlier = OpeningUnderlierObservation(
        schema_version="opening_underlier_observation.v1",
        code="US.NVDA",
        market="US",
        last_price=180.0,
        update_time=update_time,
        observed_at_utc=observed_now.isoformat(),
        age_seconds=0.0,
        market_state="MORNING",
        sec_status="NORMAL",
        suspension=False,
        status="ready",
        reason_code=None,
    )

    payload = mod.fetch_symbol_request(
        mod.FetchSymbolRequest(
            symbol="NVDA",
            base_dir=tmp_path,
            gateway=object(),
            spot_override=180.0,
            underlier_observation=underlier.to_dict(),
            option_types="put",
        )
    )
    rows = {row["contract_symbol"]: row for row in payload["rows"]}

    assert payload["meta"]["status"] == "ok"
    assert rows[ready_code]["opening_contract_status"] == "ready"
    assert rows[ready_code]["multiplier"] == 100
    assert rows[ready_code]["mid"] == 1.1
    assert rows[ready_code]["open_interest"] == 0
    assert rows[conflict_code]["opening_contract_status"] == "data_unavailable"
    assert "option_multiplier_conflict" in rows[conflict_code][
        "opening_contract_reason_codes"
    ]
    assert rows[conflict_code]["multiplier"] is None


def test_opening_mid_requires_a_valid_two_sided_quote() -> None:
    import src.application.opend_symbol_fetching as mod

    assert mod.calc_mid(1.0, 1.2, 9.9) == 1.1
    assert mod.calc_mid(None, None, 9.9) is None
    assert mod.calc_mid(1.2, 1.0, 1.1) is None
