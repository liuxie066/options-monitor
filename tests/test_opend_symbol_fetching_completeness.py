from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
from src.application.opend_symbol_chain_fetching import SymbolOptionChainResult
from src.application.short_vol_metrics import RealizedVolatilitySnapshot


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
            include_realized_volatility=True,
        )
    )

    assert payload["meta"]["status"] == "error"
    assert payload["meta"]["error_code"] == "REQUIRED_REALIZED_VOLATILITY_INCOMPLETE"
    assert payload["meta"]["snapshot_complete"] is True
    assert payload["meta"]["source_observed_at"] == "2026-08-04T01:00:00+00:00"
    assert payload["meta"]["completed_at_utc"] == "2026-08-04T01:00:02+00:00"
    assert completion_events == ["rv", "snapshot", "completed"]
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
