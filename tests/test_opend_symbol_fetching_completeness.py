from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
from src.application.opend_symbol_chain_fetching import SymbolOptionChainResult
from src.application.short_vol_metrics import (
    RealizedVolatilitySnapshot,
    TermMatchedRVObservation,
)
from src.application.opening_quote_evidence import OpeningUnderlierObservation
from src.application.opend_normalize import normalize_opend_option_type


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("PUT", "put"),
        ("CALL", "call"),
        ("OptionType.PUT", "put"),
        ("OptionType.CALL", "call"),
    ],
)
def test_opend_option_type_normalization_accepts_exact_provider_values(
    value: object,
    expected: str,
) -> None:
    assert normalize_opend_option_type(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, "", 1, "OUTPUT", "RECALL", "putative", "callback"],
)
def test_opend_option_type_normalization_rejects_non_exact_values(
    value: object,
) -> None:
    assert normalize_opend_option_type(value) not in {"put", "call"}


def _chain_row(code: str, strike_price: float = 100.0, lot_size: int = 100) -> dict:
    return {
        "code": code,
        "strike_time": "2026-06-19",
        "strike_price": strike_price,
        "option_type": "PUT",
        "lot_size": lot_size,
    }


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


class _DteSelectionGateway:
    def __init__(self, expirations: list[str], *, chain_expiration: str = "2026-12-18", empty_chain: bool = False, discovery_mode: str = "ok"):
        self.expirations = expirations
        self.chain_expiration = chain_expiration
        self.empty_chain = empty_chain
        self.discovery_mode = discovery_mode
        self.expiration_calls: list[str] = []
        self.chain_calls: list[dict] = []
        self.snapshot_calls: list[list[str]] = []
        self.close_calls = 0

    def get_option_expiration_dates(self, code):
        self.expiration_calls.append(code)
        if self.discovery_mode == "error":
            raise RuntimeError("synthetic expiration discovery failure")
        if self.discovery_mode == "malformed":
            return pd.DataFrame({"unexpected_column": ["2026-12-18"]})
        return pd.DataFrame({"strike_time": self.expirations})

    def get_option_chain(self, **kwargs):
        self.chain_calls.append(dict(kwargs))
        if self.empty_chain:
            return pd.DataFrame()
        code = f"US.NVDA{date.fromisoformat(self.chain_expiration).strftime('%y%m%d')}P100000"
        return pd.DataFrame([{
            **_chain_row(code), "strike_time": self.chain_expiration,
            "option_standard_type": "STANDARD", "stock_owner": "US.NVDA",
            "stock_type": "DRVT", "suspension": False,
        }])

    def get_snapshot(self, codes):
        codes = list(codes)
        self.snapshot_calls.append(codes)
        return pd.DataFrame([
            {"code": code, "last_price": 1.0, "bid_price": 0.9, "ask_price": 1.1,
             "bid_vol": 10, "ask_vol": 12, "price_spread": 0.01, "sec_status": "NORMAL",
             "suspension": False, "option_contract_multiplier": 100}
            for code in codes
        ])

    def close(self):
        self.close_calls += 1


@pytest.mark.parametrize("case", [
    "discovery_empty", "dte_filtered_empty", "dte_filtered_empty_chain_empty",
    "selected_date", "explicit_date", "discovery_error", "discovery_malformed",
])
def test_fetch_symbol_preserves_completed_empty_expiration_selection(monkeypatch, tmp_path: Path, case: str) -> None:
    import socket

    import src.application.opend_symbol_fetching as mod

    today = date(2026, 7, 28)
    in_scope = "2026-08-21"
    positive = case in {"selected_date", "explicit_date"}
    gateway = _DteSelectionGateway(
        [] if case == "discovery_empty" else [in_scope if case == "selected_date" else "2026-12-18"],
        chain_expiration=in_scope if positive else "2026-12-18",
        empty_chain=case == "dte_filtered_empty_chain_empty",
        discovery_mode={"discovery_error": "error", "discovery_malformed": "malformed"}.get(case, "ok"),
    )
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError("controlled fetch must not construct a real gateway or connect a socket")

    monkeypatch.setattr(mod, "get_trading_date", lambda _market: today)
    monkeypatch.setattr(mod, "build_ready_futu_quote_gateway", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("src.application.opend_utils.load_runtime_symbol_aliases", lambda *_: {})
    payload = mod.fetch_symbol(
        "NVDA", gateway=gateway, spot_override=100.0, base_dir=tmp_path,
        option_types="put", min_dte=7, max_dte=45, no_retry=True,
        explicit_expirations=[in_scope] if case == "explicit_date" else None,
    )
    meta = payload["meta"]
    summary = {
        "expiration_calls": gateway.expiration_calls, "chain_calls": gateway.chain_calls,
        "snapshot_calls": gateway.snapshot_calls,
        "rows": [{key: row.get(key) for key in ("expiration", "dte")} for row in payload["rows"]],
        "meta": {key: meta.get(key) for key in ("status", "source_outcome", "reason_code", "error_code", "error")},
    }
    assert forbidden_calls == []
    assert gateway.close_calls == 0
    assert gateway.expiration_calls == ([] if case == "explicit_date" else ["US.NVDA"])
    assert payload["symbol"] == "NVDA" and payload["underlier_code"] == "US.NVDA"
    if case in {"discovery_error", "discovery_malformed"}:
        assert meta["status"] == "error", summary
        assert meta.get("source_outcome") != "success_empty", summary
        assert meta["snapshot_complete"] is False
        assert gateway.chain_calls == gateway.snapshot_calls == []
    elif positive:
        assert meta["status"] == "ok" and meta["source_outcome"] == "success_rows", summary
        assert len(payload["rows"]) == 1 and payload["expirations"] == [in_scope], summary
        assert len(gateway.chain_calls) == 1
        assert gateway.chain_calls[0]["start"] == gateway.chain_calls[0]["end"] == in_scope
        expected_code = f"US.NVDA{date.fromisoformat(in_scope).strftime('%y%m%d')}P100000"
        assert gateway.snapshot_calls == [[expected_code]]
    else:
        assert payload["rows"] == [] and payload["expirations"] == [], summary
        assert meta["status"] == "ok" and meta["source_outcome"] == "success_empty", summary
        assert meta["reason_code"] == "no_expirations"
        assert meta["snapshot_complete"] is True and meta["snapshot_requested_code_set"] == []
        assert meta["query_scope_validated"] is True
        assert meta["expiration_opend_calls"] == 1 and meta["expiration_cache_hits"] == 0
        assert meta["opend_call_count"] == 0
        assert meta["errors"] == [] and meta["expiration_statuses"] == {}
        assert OpeningUnderlierObservation.from_mapping(meta["underlier_observation"]).code == "US.NVDA"
        assert datetime.fromisoformat(meta["source_observed_at"]).tzinfo is not None
        assert datetime.fromisoformat(meta["completed_at_utc"]).tzinfo is not None
        assert gateway.chain_calls == gateway.snapshot_calls == []


def test_raw_chain_request_retains_unscoped_full_chain_contract(tmp_path: Path) -> None:
    from src.application.option_chain_fetching import OptionChainFetchRequest, fetch_option_chains
    from src.infrastructure.futu_gateway import retry_futu_gateway_call

    gateway = _DteSelectionGateway([])
    result = fetch_option_chains(
        gateway=gateway,
        retry_call=retry_futu_gateway_call,
        request=OptionChainFetchRequest(
            symbol="NVDA", underlier_code="US.NVDA", expirations=[], option_types="put",
            chain_cache=False, base_dir=tmp_path, asof_date="2026-07-28", no_retry=True,
        ),
    )
    assert result.status == "ok" and result.source_outcome == "success_rows"
    assert len(result.rows) == 1 and result.rows[0]["strike_time"] == "2026-12-18"
    assert len(gateway.chain_calls) == 1
    assert "start" not in gateway.chain_calls[0] and "end" not in gateway.chain_calls[0]
    assert gateway.expiration_calls == gateway.snapshot_calls == []
    assert gateway.close_calls == 0


def _install_symbol_dependencies(monkeypatch, *, chain_bundle: SymbolOptionChainResult) -> None:  # type: ignore[no-untyped-def]
    import src.application.opend_symbol_fetching as mod

    monkeypatch.setattr(mod, "get_trading_date", lambda _market: date(2026, 5, 20))
    monkeypatch.setattr(mod, "fetch_symbol_option_chain", lambda **_kwargs: chain_bundle)
    monkeypatch.setattr("src.application.opend_utils.load_runtime_symbol_aliases", lambda *_: {})


def _fetch_symbol_request(tmp_path: Path, **overrides):
    import src.application.opend_symbol_fetching as mod

    kwargs = {
        "symbol": "NVDA",
        "base_dir": tmp_path,
        "gateway": object(),
        "spot_override": 100.0,
    }
    kwargs.update(overrides)
    return mod.fetch_symbol_request(mod.FetchSymbolRequest(**kwargs))


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
        rows=[_chain_row(code)],
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

    payload = _fetch_symbol_request(
        tmp_path,
        option_types="put",
        include_realized_volatility=True,
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


def test_partial_realized_volatility_only_blocks_dependent_expiration() -> None:
    import src.application.opend_symbol_fetching as mod

    ready = TermMatchedRVObservation(
        schema_version="term_matched_rv.v1",
        expiration="2026-06-19",
        status="ok",
        reason=None,
        term_matched_rv=0.22,
        remaining_sessions=20,
        lookback_sessions=20,
        input_start="2026-04-20",
        input_end="2026-05-19",
        input_close_session_count=21,
        input_return_count=20,
        input_hash="a" * 64,
    )
    unavailable = TermMatchedRVObservation(
        schema_version="term_matched_rv.v1",
        expiration="2026-07-17",
        status="data_unavailable",
        reason="qfq_history_session_gap",
        term_matched_rv=None,
        remaining_sessions=40,
        lookback_sessions=40,
        input_start=None,
        input_end=None,
        input_close_session_count=0,
        input_return_count=0,
        input_hash=None,
    )
    snapshot = RealizedVolatilitySnapshot(
        status="partial",
        reason="term_matched_rv_incomplete",
        term_matched={
            ready.expiration: ready,
            unavailable.expiration: unavailable,
        },
    )

    assert (
        mod._required_realized_volatility_error(
            snapshot,
            expirations=[ready.expiration, unavailable.expiration],
        )
        is None
    )
    assert (
        mod._required_realized_volatility_error(
            snapshot,
            expirations=[unavailable.expiration],
        )
        is None
    )
    assert mod._required_realized_volatility_error(
        snapshot,
        expirations=["2026-09-18"],
    )["missing_expirations"] == ["2026-09-18"]


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

    payload = _fetch_symbol_request(tmp_path, include_realized_volatility=True)

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
            rows=[_chain_row("US.NVDA.2026-06-19.P90", 90.0)],
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

    payload = _fetch_symbol_request(
        tmp_path,
        option_types="put",
        explicit_expirations=["2026-06-19"],
        min_strike=100.0,
        max_strike=100.0,
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

    payload = _fetch_symbol_request(tmp_path, spot_override=None, fetch_spot_if_missing=False)

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

    payload = _fetch_symbol_request(tmp_path, option_types="put")

    assert payload["meta"]["status"] == "error"
    assert "lacks required filter columns" in str(payload["meta"]["error"])


@pytest.mark.parametrize("option_type", ["OUTPUT", "RECALL"])
def test_option_chain_filter_rejects_non_exact_option_type(
    option_type: str,
) -> None:
    import src.application.opend_symbol_fetching as mod

    chain = pd.DataFrame(
        [
            {
                "code": "US.NVDA.2026-06-19.P100",
                "expiration": "2026-06-19",
                "strike_price": 100.0,
                "option_type": option_type,
            }
        ]
    )

    with pytest.raises(ValueError, match="invalid option type"):
        mod._filter_option_chain_for_request(
            chain=chain,
            option_types="put",
            min_strike=None,
            max_strike=None,
            side_strike_windows=None,
        )


def test_duplicate_snapshot_code_is_a_typed_overall_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.opend_symbol_fetching as mod

    code = "US.NVDA.2026-06-19.P100"
    _install_symbol_dependencies(
        monkeypatch,
        chain_bundle=_chain_bundle(
            rows=[_chain_row(code)],
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

    payload = _fetch_symbol_request(tmp_path, include_realized_volatility=True)

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
    for snapshot in snapshots.values():
        snapshot["snapshot_requested_at_utc"] = observed_now.isoformat()
        snapshot["snapshot_received_at_utc"] = observed_now.isoformat()
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
            requested_at_utc=observed_now.isoformat(),
            received_at_utc=observed_now.isoformat(),
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

    payload = _fetch_symbol_request(
        tmp_path,
        spot_override=180.0,
        underlier_observation=underlier.to_dict(),
        option_types="put",
    )
    rows = {row["contract_symbol"]: row for row in payload["rows"]}

    assert payload["meta"]["status"] == "ok"
    assert rows[ready_code]["opening_contract_status"] == "ready"
    assert rows[ready_code]["multiplier"] == 100
    assert rows[ready_code]["mid"] == 1.1
    assert rows[ready_code]["open_interest"] == 0
    assert rows[ready_code]["delta"] == -0.2
    assert rows[ready_code]["delta_source"] == "opend_market_snapshot.option_delta"
    assert rows[ready_code]["delta_unit"] == "fraction"
    assert rows[ready_code]["delta_reason_code"] is None
    assert rows[ready_code]["snapshot_received_at_utc"] == observed_now.isoformat()
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
