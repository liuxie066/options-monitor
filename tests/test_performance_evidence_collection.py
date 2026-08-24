from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from domain.domain.performance.models import EvidenceEnvelope, OptionInstrumentKey, OptionValuationPosition
from src.application.performance.evidence_collection import (
    _default_option_snapshot_rows,
    capture_current_performance_evidence,
    collect_current_performance_evidence,
)
from src.application.opend_market_snapshot_fetching import SNAPSHOT_KEEP_COLUMNS, keep_snapshot_record_columns
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository


NOW_MS = 1_768_000_000_000


def _position(
    *,
    account: str = "lx",
    market_code: str | None = None,
    strike: str = "100",
    symbol: str = "NVDA",
) -> OptionValuationPosition:
    return OptionValuationPosition(
        lot_id=f"lot-{account}-{symbol}-{strike}",
        account=account,
        broker="富途",
        instrument=OptionInstrumentKey(
            symbol=symbol,
            option_type="put",
            strike=Decimal(strike),
            expiration_ymd="2026-08-21",
            currency="USD",
            multiplier=Decimal("100"),
        ),
        position_side="short",
        contracts_open=1,
        open_price=Decimal("2"),
        open_fee_remaining=Decimal("0"),
        open_fee_quality="actual",
        opened_at_ms=NOW_MS - 10_000,
        market_code=market_code,
    )


def test_historical_or_disabled_collection_never_calls_live_sources() -> None:
    calls = 0

    def fetch(_positions):
        nonlocal calls
        calls += 1
        return []

    historical = collect_current_performance_evidence(
        period_status="complete_past",
        refresh_quotes=True,
        option_positions=[_position()],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=fetch,
    )
    disabled = collect_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=False,
        option_positions=[_position()],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=fetch,
    )

    assert historical.status == "skipped_historical"
    assert disabled.status == "skipped_refresh_disabled"
    assert calls == 0


def test_cross_account_instrument_reuse_midpoint_and_live_fx_are_collected_once() -> None:
    requested = []

    def fetch(positions):
        requested.append(list(positions))
        return [
            {
                "_requested_instrument_key": positions[0].instrument.instrument_key,
                "code": "US.NVDA260821P100000",
                "bid_price": 2.0,
                "ask_price": 2.4,
                "last_price": 9.0,
                "snapshot_time_ms": NOW_MS - 1_000,
            }
        ]

    result = collect_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=True,
        option_positions=[
            _position(account="lx"),
            _position(account="sy", market_code="US.NVDA260821P100000"),
        ],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=fetch,
        fx_payload_fetcher=lambda: {
            "rates": {"USDCNY": 7.12},
            "timestamp_ms": NOW_MS - 500,
            "source": "test",
        },
    )

    assert len(requested) == 1
    assert len(requested[0]) == 1
    assert requested[0][0].market_code == "US.NVDA260821P100000"
    assert len(result.valuation_marks) == 1
    assert result.valuation_marks[0].price == Decimal("2.2")
    assert result.valuation_marks[0].mark_kind == "midpoint"
    assert result.valuation_marks[0].quality["persistence"] == "live_unpersisted"
    assert len(result.fx_rates) == 1
    assert result.fx_rates[0].rate == Decimal("7.12")


def test_last_fallback_timestamp_fallback_and_exact_code_resolution_fail_closed() -> None:
    position = _position(market_code="EXACT")

    fallback = collect_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=True,
        option_positions=[position],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=lambda _positions: [
            {"code": "EXACT", "bid_price": 0, "ask_price": 0, "last_price": 1.5}
        ],
        fx_payload_fetcher=lambda: {"rates": {"USDCNY": 7.1}, "timestamp_ms": NOW_MS},
    )
    ambiguous = collect_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=True,
        option_positions=[_position()],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=lambda positions: [
            {
                "_requested_instrument_key": positions[0].instrument.instrument_key,
                "code": "A",
                "last_price": 1,
            },
            {
                "_requested_instrument_key": positions[0].instrument.instrument_key,
                "code": "B",
                "last_price": 1,
            },
        ],
        fx_payload_fetcher=lambda: {"rates": {"USDCNY": 7.1}, "timestamp_ms": NOW_MS},
    )

    assert fallback.valuation_marks[0].mark_kind == "last_fallback"
    assert fallback.valuation_marks[0].effective_at_ms == NOW_MS
    assert fallback.valuation_marks[0].quality["timestamp_fallback"] is True
    assert not ambiguous.valuation_marks
    assert ambiguous.diagnostics[0]["code"] == "option_code_resolution_failed"

    naive_or_future = collect_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=True,
        option_positions=[position],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=lambda _positions: [
            {"code": "EXACT", "last_price": 1.5, "update_time": "2026-07-17 15:00:00"}
        ],
        fx_payload_fetcher=lambda: {"rates": {"USDCNY": 7.1}, "timestamp_ms": NOW_MS + 1},
    )
    assert naive_or_future.valuation_marks[0].effective_at_ms == NOW_MS
    assert naive_or_future.valuation_marks[0].quality["timestamp_fallback"] is True
    assert naive_or_future.fx_rates[0].effective_at_ms == NOW_MS
    assert naive_or_future.fx_rates[0].quality["timestamp_fallback"] is True


def test_conflicting_stored_codes_for_same_instrument_fail_closed() -> None:
    first = _position(account="lx", market_code="CODE-A")
    second = _position(account="sy", market_code="CODE-B")
    called = False

    def fetch(_positions):
        nonlocal called
        called = True
        return []

    result = collect_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=True,
        option_positions=[first, second],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=fetch,
        fx_payload_fetcher=lambda: {"rates": {"USDCNY": 7.1}, "timestamp_ms": NOW_MS},
    )

    assert called is False
    assert not result.valuation_marks
    assert any(item["code"] == "option_market_code_conflict" for item in result.diagnostics)


def test_crossed_market_is_missing_and_capture_emits_v1_envelope() -> None:
    result = collect_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=True,
        option_positions=[_position(market_code="EXACT")],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=lambda _positions: [
            {"code": "EXACT", "bid_price": 2.5, "ask_price": 2.0, "last_price": 2.2}
        ],
        fx_payload_fetcher=lambda: {"rates": {"USDCNY": 7.1}, "timestamp_ms": NOW_MS},
    )
    envelope = capture_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=True,
        option_positions=[_position(market_code="EXACT")],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=lambda _positions: [{"code": "EXACT", "last_price": 2.2}],
        fx_payload_fetcher=lambda: {"rates": {"USDCNY": 7.1}, "timestamp_ms": NOW_MS},
    )

    assert not result.valuation_marks
    assert any(item["code"] == "option_mark_missing" for item in result.diagnostics)
    assert envelope.to_dict()["schema_version"] == "option_performance_evidence.v1"
    assert len(envelope.valuation_marks) == 1


def test_option_capture_identity_uses_receipt_time_not_provider_trade_time(tmp_path) -> None:
    position = _position(market_code="US.NVDA260821P100000")

    def capture(*, bid: float, ask: float, requested_ms: int, received_ms: int):
        return collect_current_performance_evidence(
            period_status="partial_current",
            refresh_quotes=True,
            option_positions=[position],
            now_ms=NOW_MS,
            option_snapshot_rows_fetcher=lambda _positions: [
                {
                    "code": position.market_code,
                    "bid_price": bid,
                    "ask_price": ask,
                    "update_time": "2026-01-01T00:00:00+00:00",
                    "_snapshot_requested_at_utc": datetime.fromtimestamp(
                        requested_ms / 1000,
                        tz=timezone.utc,
                    ).isoformat(),
                    "_snapshot_received_at_utc": datetime.fromtimestamp(
                        received_ms / 1000,
                        tz=timezone.utc,
                    ).isoformat(),
                }
            ],
            fx_payload_fetcher=lambda: None,
        ).valuation_marks[0]

    first = capture(
        bid=1.0,
        ask=1.2,
        requested_ms=NOW_MS - 1_500,
        received_ms=NOW_MS - 1_400,
    )
    second = capture(
        bid=1.1,
        ask=1.3,
        requested_ms=NOW_MS - 500,
        received_ms=NOW_MS - 400,
    )
    repo = PerformanceEvidenceSQLiteRepository(tmp_path / "evidence.sqlite3")
    result = repo.import_envelope(
        EvidenceEnvelope(valuation_marks=(first, second)),
        apply=True,
        migrated_at_ms=NOW_MS,
    )

    assert first.source_identity != second.source_identity
    assert first.effective_at_ms == NOW_MS - 1_500
    assert first.observed_at_ms == NOW_MS - 1_400
    assert result.inserted_count == 2
    assert len(repo.read_all().valuation_marks) == 2


def test_default_option_collection_is_one_nonblocking_exact_code_batch(monkeypatch, tmp_path) -> None:
    stored = _position(account="lx", market_code="US.STORED", strike="100")
    unresolved = _position(account="sy", strike="105")
    calls: dict[str, object] = {}

    class Gateway:
        closed = False

        def close(self):
            self.closed = True

    gateway = Gateway()

    def fake_chain(*, gateway, request, retry_call):
        calls["chain_gateway"] = gateway
        calls["chain_request"] = request
        return SimpleNamespace(
            rows=[
                {
                    "code": "US.DISCOVERED",
                    "option_type": "put",
                    "strike_price": 105,
                    "expiration": "2026-08-21",
                    "option_contract_multiplier": 100,
                }
            ]
        )

    def fake_snapshots(*, option_codes, gateway, **kwargs):
        calls["snapshot_gateway"] = gateway
        calls["snapshot_codes"] = list(option_codes)
        calls["snapshot_kwargs"] = kwargs
        return SimpleNamespace(
            snap_map={
                "US.STORED": {"code": "US.STORED", "last_price": 1.1},
                "US.DISCOVERED": {"code": "US.DISCOVERED", "last_price": 1.2},
            },
            requested_at_utc="2026-08-24T08:00:00+00:00",
            received_at_utc="2026-08-24T08:00:00.100000+00:00",
        )

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr("src.application.option_chain_fetching.fetch_option_chains", fake_chain)
    monkeypatch.setattr("src.application.opend_market_snapshot_fetching.fetch_option_snapshots", fake_snapshots)

    rows = _default_option_snapshot_rows([stored, unresolved], cfg={}, base_dir=tmp_path)

    chain_request = calls["chain_request"]
    assert chain_request.expirations == ["2026-08-21"]
    assert chain_request.freshness_policy == "cache_first"
    assert chain_request.chain_cache is True
    assert chain_request.max_wait_sec == 0
    assert chain_request.no_retry is True
    assert calls["snapshot_codes"] == ["US.DISCOVERED", "US.STORED"]
    assert {row["code"] for row in rows} == {"US.DISCOVERED", "US.STORED"}
    assert all(row["_requested_instrument_key"] for row in rows)
    assert rows[0]["_snapshot_received_at_utc"] == "2026-08-24T08:00:00.100000+00:00"
    assert calls["snapshot_kwargs"]["snapshot_limit"].max_wait_sec == 0
    assert calls["snapshot_kwargs"]["snapshot_fallback_max_codes"] == 0
    assert calls["snapshot_kwargs"]["no_retry"] is True
    assert gateway.closed is True


def test_snapshot_adapter_preserves_broker_timestamp_columns() -> None:
    records, kept = keep_snapshot_record_columns(
        pd.DataFrame(
            [
                {
                    "code": "US.NVDA260821P100000",
                    "last_price": 1.5,
                    "option_gamma": 0.03,
                    "option_theta": -0.04,
                    "option_vega": 0.08,
                    "option_rho": -0.01,
                    "update_time": "2026-07-17 15:00:00",
                }
            ]
        ),
        SNAPSHOT_KEEP_COLUMNS,
    )

    assert "update_time" in kept
    assert records[0]["option_gamma"] == 0.03
    assert records[0]["option_theta"] == -0.04
    assert records[0]["option_vega"] == 0.08
    assert records[0]["option_rho"] == -0.01
    assert records[0]["update_time"] == "2026-07-17 15:00:00"


def test_external_snapshot_raw_is_json_safe_and_report_provenance_is_compact() -> None:
    result = collect_current_performance_evidence(
        period_status="partial_current",
        refresh_quotes=True,
        option_positions=[_position(market_code="EXACT")],
        now_ms=NOW_MS,
        option_snapshot_rows_fetcher=lambda _positions: [
            {
                "code": "EXACT",
                "last_price": 1.5,
                "provider_time": pd.Timestamp("2026-07-17T03:00:00Z"),
                "provider_nan": float("nan"),
            }
        ],
        fx_payload_fetcher=lambda: {
            "rates": {"USDCNY": 7.1},
            "timestamp_ms": NOW_MS,
            "provider_nan": float("nan"),
        },
    )

    mark = result.valuation_marks[0]
    assert mark.raw["provider_time"] == "2026-07-17T03:00:00+00:00"
    assert mark.raw["provider_nan"] is None
    assert result.fx_rates[0].raw["provider_nan"] is None
    payload = result.to_dict()
    assert payload["valuation_mark_fact_ids"] == [mark.fact_id]
    assert "evidence" not in payload
    assert "raw" not in payload
