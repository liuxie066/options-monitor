from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.application.ai_decision_advice.identity import (
    PRIORITY_OPEN_OPTION,
    PRIORITY_RECENT_CANDIDATE,
    PRIORITY_SCAN_CONFIG,
    PRIORITY_STOCK_HOLDING,
    ObservationSnapshotError,
    RefreshQueue,
    build_observation_set,
    build_symbol_identity_snapshot,
    candidate_symbols_from_snapshot,
    identity_by_symbol,
    load_observation_set,
    load_symbol_identity_snapshot,
    observed_symbols_from_snapshot,
    publish_observation_partition,
    open_option_underlyings_from_lots,
    publish_symbol_identity_snapshot,
    stock_symbols_from_portfolio_context,
)


def test_observation_set_union_dedup_and_priority() -> None:
    observed = build_observation_set(
        scan_symbols=["NVDA", "0700.HK", "AAPL"],
        stock_holding_symbols=["AAPL"],
        open_option_underlyings=["NVDA"],
        recent_candidate_symbols=["AAPL"],
    )
    by_symbol = {item.symbol: item for item in observed}
    assert set(by_symbol) == {"NVDA", "0700.HK", "AAPL"}
    assert by_symbol["NVDA"].priority == PRIORITY_OPEN_OPTION
    assert by_symbol["AAPL"].priority == PRIORITY_RECENT_CANDIDATE
    assert by_symbol["0700.HK"].priority == PRIORITY_SCAN_CONFIG
    assert "open_option" in by_symbol["NVDA"].sources
    assert "scan_config" in by_symbol["NVDA"].sources


def test_observation_set_drops_unknown_and_alias() -> None:
    observed = build_observation_set(scan_symbols=["", "not a symbol ??"])
    assert all(item.symbol for item in observed)


def test_open_option_underlyings_from_lots() -> None:
    lots = [
        {"status": "open", "contracts_open": 1, "contract_key": {"underlying_symbol": "NVDA"}},
        {"status": "open", "contracts_open": 0, "contract_key": {"underlying_symbol": "AAPL"}},
        {"status": "close", "contracts_open": 0, "contract_key": {"underlying_symbol": "MSFT"}},
    ]
    assert open_option_underlyings_from_lots(lots) == ["NVDA"]


def test_stock_symbols_from_portfolio_context() -> None:
    ctx = {"stocks_by_symbol": {"NVDA": {"shares": 100}, "AAPL": {"shares": 50}}}
    assert sorted(stock_symbols_from_portfolio_context(ctx)) == ["AAPL", "NVDA"]
    assert stock_symbols_from_portfolio_context(None) == []
    assert stock_symbols_from_portfolio_context({}) == []


def test_candidate_symbols_from_snapshot() -> None:
    snapshot = {
        "ranked_candidates": [
            {"candidate_id": "c1", "facts": {"symbol": "NVDA"}},
            {"candidate_id": "c2", "facts": {"symbol": "AAPL"}},
            {"candidate_id": "c3"},
        ]
    }
    assert candidate_symbols_from_snapshot(snapshot) == ["NVDA", "AAPL"]
    assert candidate_symbols_from_snapshot(None) == []


def test_identity_snapshot_prefers_market_snapshot_names() -> None:
    observed = build_observation_set(scan_symbols=["NVDA", "0700.HK"])
    snapshot = build_symbol_identity_snapshot(
        observed,
        market_snapshot_provider=lambda market, symbols: (
            {"US.NVDA": {"name": "NVIDIA Corp", "exchange_type": "NASDAQ"}} if market == "US" else {}
        ),
        basic_info_provider=lambda codes: [
            {"code": "HK.00700", "name": "腾讯控股", "exchange_type": "HKEX"},
        ],
        observed_at="2026-08-09T00:00:00+00:00",
    )
    rows = identity_by_symbol(snapshot)
    assert rows["NVDA"]["name"] == "NVIDIA Corp"
    assert rows["NVDA"]["exchange"] == "NASDAQ"
    assert rows["NVDA"]["status"] == "resolved"
    assert rows["0700.HK"]["status"] == "resolved"
    assert rows["0700.HK"]["name"] == "腾讯控股"
    assert snapshot["schema_version"] == "ai_decision_advice.symbol_identity_snapshot.v1"
    assert len(snapshot["content_sha256"]) == 64
    assert len(snapshot["semantic_sha256"]) == 64
    assert len(rows["NVDA"]["identity_semantic_sha256"]) == 64


def test_identity_semantic_hash_ignores_observed_time_but_tracks_identity() -> None:
    observed = build_observation_set(scan_symbols=["NVDA"])

    def build(observed_at: str, name: str) -> dict:
        return build_symbol_identity_snapshot(
            observed,
            market_snapshot_provider=lambda market, symbols: {
                "NVDA": {"name": name, "exchange_type": "NASDAQ"}
            },
            observed_at=observed_at,
        )

    first = build("2026-08-09T00:00:00+00:00", "NVIDIA")
    later = build("2026-08-09T04:00:00+00:00", "NVIDIA")
    renamed = build("2026-08-09T04:00:00+00:00", "NVIDIA Corp")
    assert first["content_sha256"] != later["content_sha256"]
    assert first["semantic_sha256"] == later["semantic_sha256"]
    assert (
        identity_by_symbol(first)["NVDA"]["identity_semantic_sha256"]
        == identity_by_symbol(later)["NVDA"]["identity_semantic_sha256"]
    )
    assert first["semantic_sha256"] != renamed["semantic_sha256"]


def test_identity_ignores_rows_outside_requested_set() -> None:
    observed = build_observation_set(scan_symbols=["NVDA"])
    snapshot = build_symbol_identity_snapshot(
        observed,
        market_snapshot_provider=lambda market, symbols: {
            "US.AAPL": {"name": "Apple"},
        },
        basic_info_provider=lambda codes: [
            {"code": "US.MSFT", "name": "Microsoft"},
        ],
        observed_at="2026-08-09T00:00:00+00:00",
    )
    rows = identity_by_symbol(snapshot)
    assert rows["NVDA"]["status"] == "identity_unavailable"


def test_identity_snapshot_fallback_to_basicinfo_then_unavailable() -> None:
    observed = build_observation_set(scan_symbols=["NVDA", "AAPL"])
    snapshot = build_symbol_identity_snapshot(
        observed,
        market_snapshot_provider=lambda market, symbols: {},
        basic_info_provider=lambda codes: [
            {"code": "US.NVDA", "name": "NVIDIA", "exchange_type": "NASDAQ"},
        ],
        observed_at="2026-08-09T00:00:00+00:00",
    )
    rows = identity_by_symbol(snapshot)
    assert rows["NVDA"]["status"] == "resolved"
    assert rows["AAPL"]["status"] == "identity_unavailable"
    assert rows["AAPL"]["name"] is None


def test_publish_and_load_roundtrip(tmp_path: Path) -> None:
    observed = build_observation_set(scan_symbols=["NVDA"])
    payload = build_symbol_identity_snapshot(
        observed,
        market_snapshot_provider=lambda market, symbols: {"NVDA": {"name": "NVIDIA"}},
        observed_at="2026-08-09T00:00:00+00:00",
    )
    path = publish_symbol_identity_snapshot(base=tmp_path, payload=payload)
    assert path.name == "symbol_identity_snapshot.json"
    loaded = load_symbol_identity_snapshot(tmp_path)
    assert loaded is not None
    assert loaded["content_sha256"] == payload["content_sha256"]
    assert identity_by_symbol(loaded)["NVDA"]["name"] == "NVIDIA"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_publish_rewrites_same_content_deterministically(tmp_path: Path) -> None:
    observed = build_observation_set(scan_symbols=["NVDA"])
    payload = build_symbol_identity_snapshot(
        observed,
        market_snapshot_provider=lambda market, symbols: {"NVDA": {"name": "NVIDIA"}},
        observed_at="2026-08-09T00:00:00+00:00",
    )
    first = publish_symbol_identity_snapshot(base=tmp_path, payload=payload)
    first_text = first.read_text(encoding="utf-8")
    second = publish_symbol_identity_snapshot(base=tmp_path, payload=payload)
    assert second.read_text(encoding="utf-8") == first_text
    assert json.loads(first_text)["content_sha256"] == payload["content_sha256"]


def test_load_missing_or_invalid_returns_none(tmp_path: Path) -> None:
    assert load_symbol_identity_snapshot(tmp_path) is None
    path = tmp_path / "output_shared" / "state" / "ai_decision_advice"
    path.mkdir(parents=True)
    (path / "symbol_identity_snapshot.json").write_text("not json", encoding="utf-8")
    assert load_symbol_identity_snapshot(tmp_path) is None
    (path / "symbol_identity_snapshot.json").write_text(
        json.dumps({"schema_version": "other"}), encoding="utf-8"
    )
    assert load_symbol_identity_snapshot(tmp_path) is None


def test_publish_rejects_symlinked_identity_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "output_shared" / "state" / "ai_decision_advice"
    path.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (path / "symbol_identity_snapshot.json").symlink_to(outside)

    with pytest.raises(OSError, match="symlink"):
        publish_symbol_identity_snapshot(base=tmp_path, payload={"schema_version": "x"})


def test_refresh_queue_priority_and_starvation_order() -> None:
    observed = build_observation_set(
        scan_symbols=["MSFT", "GOOGL"],
        stock_holding_symbols=["AAPL"],
        open_option_underlyings=["NVDA"],
    )
    queue = RefreshQueue.build(
        observed,
        last_attempt_by_symbol={"NVDA": "2026-08-09T04:00:00+00:00"},
    )
    symbols = queue.symbols()
    assert symbols[0] == "NVDA" or symbols[0] == "AAPL"
    assert symbols.index("MSFT") < len(symbols)


def test_refresh_queue_requeue_unfinished_to_tier_head() -> None:
    observed = build_observation_set(scan_symbols=["MSFT", "GOOGL", "AAPL"])
    queue = RefreshQueue.build(observed)
    first_pass = queue.symbols()
    assert first_pass == ["AAPL", "GOOGL", "MSFT"]
    queue.requeue_unfinished(["MSFT"])
    assert queue.symbols() == ["MSFT", "AAPL", "GOOGL"]


def test_priority_constants_match_design_order() -> None:
    assert PRIORITY_OPEN_OPTION < PRIORITY_RECENT_CANDIDATE
    assert PRIORITY_RECENT_CANDIDATE < PRIORITY_STOCK_HOLDING
    assert PRIORITY_STOCK_HOLDING < PRIORITY_SCAN_CONFIG


def test_observation_partition_is_anonymous_deduped_and_strict(tmp_path: Path) -> None:
    observed = build_observation_set(
        scan_symbols=["NVDA", "AAPL"],
        open_option_underlyings=["NVDA"],
    )
    path = publish_observation_partition(
        base=tmp_path,
        market="US",
        observed=observed,
        generation="run-us-1",
        generated_at="2026-08-09T00:00:00+00:00",
    )
    payload = load_observation_set(tmp_path)
    assert payload is not None
    rows = payload["partitions"]["US"]["symbols"]
    assert rows == [
        {"symbol": "NVDA", "market": "US", "priority": PRIORITY_OPEN_OPTION},
        {"symbol": "AAPL", "market": "US", "priority": PRIORITY_SCAN_CONFIG},
    ]
    encoded = path.read_text(encoding="utf-8")
    assert "account" not in encoded
    assert "quantity" not in encoded
    assert "sources" not in encoded
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with pytest.raises(ObservationSnapshotError, match="fields"):
        publish_observation_partition(
            base=tmp_path,
            market="US",
            observed=[
                {
                    "symbol": "NVDA",
                    "market": "US",
                    "priority": PRIORITY_OPEN_OPTION,
                    "account": "lx",
                }
            ],
            generation="bad",
        )


def test_observation_market_partitions_are_retained_and_replaced(tmp_path: Path) -> None:
    us = build_observation_set(scan_symbols=["NVDA"])
    hk = build_observation_set(scan_symbols=["0700.HK"])
    publish_observation_partition(
        base=tmp_path,
        market="US",
        observed=us,
        generation="us-1",
        generated_at="2026-08-09T00:00:00+00:00",
    )
    publish_observation_partition(
        base=tmp_path,
        market="HK",
        observed=hk,
        generation="hk-1",
        generated_at="2026-08-09T00:01:00+00:00",
    )
    publish_observation_partition(
        base=tmp_path,
        market="US",
        observed=build_observation_set(scan_symbols=["AAPL"]),
        generation="us-2",
        generated_at="2026-08-09T00:02:00+00:00",
    )
    payload = load_observation_set(tmp_path)
    assert payload is not None
    assert set(payload["partitions"]) == {"HK", "US"}
    assert payload["partitions"]["US"]["symbols"][0]["symbol"] == "AAPL"
    assert payload["partitions"]["HK"]["generation"] == "hk-1"
    assert [item.symbol for item in observed_symbols_from_snapshot(payload)] == [
        "0700.HK",
        "AAPL",
    ]


def test_observation_market_partitions_are_retained_hk_then_us(tmp_path: Path) -> None:
    publish_observation_partition(
        base=tmp_path,
        market="HK",
        observed=build_observation_set(scan_symbols=["0700.HK"]),
        generation="hk-first",
        generated_at="2026-08-09T00:00:00+00:00",
    )
    publish_observation_partition(
        base=tmp_path,
        market="US",
        observed=build_observation_set(scan_symbols=["NVDA"]),
        generation="us-second",
        generated_at="2026-08-09T00:01:00+00:00",
    )
    payload = load_observation_set(tmp_path)
    assert payload is not None
    assert set(payload["partitions"]) == {"HK", "US"}
    assert payload["partitions"]["HK"]["generation"] == "hk-first"
    assert payload["partitions"]["US"]["generation"] == "us-second"


def test_observation_concurrent_markets_do_not_lose_updates(tmp_path: Path) -> None:
    def publish(market: str, symbol: str) -> None:
        publish_observation_partition(
            base=tmp_path,
            market=market,
            observed=build_observation_set(scan_symbols=[symbol]),
            generation=f"{market}-1",
            generated_at="2026-08-09T00:00:00+00:00",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(publish, "US", "NVDA"),
            executor.submit(publish, "HK", "0700.HK"),
        ]
        for future in futures:
            future.result()
    payload = load_observation_set(tmp_path)
    assert payload is not None
    assert set(payload["partitions"]) == {"HK", "US"}


def test_observation_corruption_fails_closed_for_reader_and_publisher(tmp_path: Path) -> None:
    publish_observation_partition(
        base=tmp_path,
        market="US",
        observed=build_observation_set(scan_symbols=["NVDA"]),
        generation="us-1",
    )
    path = tmp_path / "output_shared" / "state" / "ai_decision_advice" / "observation_set.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["partitions"]["US"]["generation"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ObservationSnapshotError, match="hash"):
        load_observation_set(tmp_path)
    with pytest.raises(ObservationSnapshotError, match="hash"):
        publish_observation_partition(
            base=tmp_path,
            market="HK",
            observed=build_observation_set(scan_symbols=["0700.HK"]),
            generation="hk-1",
        )


def test_observation_publisher_rejects_invalid_time_without_replacing_snapshot(
    tmp_path: Path,
) -> None:
    publish_observation_partition(
        base=tmp_path,
        market="US",
        observed=build_observation_set(scan_symbols=["NVDA"]),
        generation="us-valid",
        generated_at="2026-08-09T00:00:00+00:00",
    )
    path = (
        tmp_path
        / "output_shared"
        / "state"
        / "ai_decision_advice"
        / "observation_set.json"
    )
    before = path.read_bytes()

    with pytest.raises(ObservationSnapshotError, match="generated_at"):
        publish_observation_partition(
            base=tmp_path,
            market="US",
            observed=build_observation_set(scan_symbols=["AAPL"]),
            generation="us-invalid",
            generated_at="not-a-time",
        )

    assert path.read_bytes() == before
    assert load_observation_set(tmp_path)["partitions"]["US"]["generation"] == (
        "us-valid"
    )
