"""Offline warmup regressions through real planning, disk cache and supervisor."""
from __future__ import annotations

from datetime import timedelta
import json
import multiprocessing
from pathlib import Path
import time

import pytest

from scripts.benchmark_opening_chain_warmup import (
    EXPIRATIONS,
    SYMBOLS,
    TRADING_DATE,
    offline_runtime,
    representative_config,
    run_comparison,
    warmup_kwargs,
)
from src.application.multi_tick import required_data_prefetch as mod
from src.application.option_chain_fetching import (
    OptionChainFetchRequest,
    fetch_option_chains,
    option_chain_shard_cache_path,
)
from src.infrastructure.private_storage import exclusive_private_file_lock


@pytest.mark.parametrize("market", ["US", "HK"])
def test_warmup_broad_shards_cover_formal_sides_without_extra_chain_calls(tmp_path, market):
    before = {process.pid for process in multiprocessing.active_children()}
    cold = run_comparison(tmp_path / "cold", warmup=False, market=market)
    warm = run_comparison(tmp_path / "warm", warmup=True, market=market)

    assert cold["formal_open_at"] == warm["formal_open_at"] == 1120.0
    assert cold["limits"] == warm["limits"]
    assert cold["forbidden_network_calls"] == warm["forbidden_network_calls"] == 0
    assert cold["output_rows"] == warm["output_rows"]
    formal_scopes = lambda result: [
        (row["symbol"], row["expirations"], row["option_types"])
        for row in result["records"] if row["endpoint"] == "formal_request"
    ]
    assert formal_scopes(cold) == formal_scopes(warm)
    assert {scope[2] for scope in formal_scopes(warm)} == {"put", "call", "put,call"}
    for rows in warm["output_rows"]:
        assert rows
        assert all(row["strike_price"] == (90.0 if row["option_type"] == "PUT" else 110.0)
                   for row in rows)

    summary = warm["warmup"]
    assert summary["status"] == "ready"
    assert summary["child_terminated"] is False
    assert summary["planned_shards"] == summary["fetched_shards"] == 8
    assert summary["option_contract_snapshot_requested_codes"] == 0
    assert summary["reason_codes"] == []
    assert {process.pid for process in multiprocessing.active_children()} == before

    warm_requests = [row for row in warm["records"] if row["endpoint"] == "chain_request"]
    assert {row["symbol"] for row in warm_requests} == {
        symbol for index, symbol in enumerate(SYMBOLS[market]) if index % 3 != 1
    }
    assert len(warm_requests) == 8
    assert all(row["option_types"] == "put,call" for row in warm_requests)
    assert {expiration for row in warm_requests for expiration in row["expirations"]} == set(EXPIRATIONS)
    assert all("." not in code.split(".", 1)[1] for row in warm["records"]
               if row["phase"] == "warmup" and row["endpoint"] == "snapshot" for code in row["codes"])
    assert warm["opening_cache_hits"] == 8
    assert cold["opening_chain_calls"] == 12
    assert warm["opening_chain_calls"] == 4
    assert cold["total_chain_calls"] == warm["total_chain_calls"] == 12
    assert cold["total_provider_data_calls"] == 26
    assert warm["total_provider_data_calls"] == 28  # additional underlier snapshot + market-state
    assert cold["opening_rate_gate_wait_seconds"] > 0
    assert warm["opening_rate_gate_wait_seconds"] == 0
    assert warm["opening_seconds"] < cold["opening_seconds"]
    assert warm["chain_cache_bytes"] > cold["chain_cache_bytes"]
    for result in (cold, warm):
        calls = [row["at"] for row in result["records"] if row["endpoint"] == "chain"]
        assert max(sum(-1e-6 <= now - other < 30.0 - 1e-6 for other in calls)
                   for now in calls) <= 10


def test_final_with_lingering_provider_thread_is_reaped_without_deadline_failure(tmp_path):
    before = {process.pid for process in multiprocessing.active_children()}
    with offline_runtime(tmp_path, linger=True):
        started = time.monotonic()
        summary = mod.warm_required_data_chain_cache(**warmup_kwargs(tmp_path, representative_config()))
    assert time.monotonic() - started < 2.0
    assert summary["status"] == "ready"
    assert summary["planned_shards"] == summary["fetched_shards"] == 8
    assert summary["failed_shards"] == summary["deadline_skipped_shards"] == 0
    assert summary["child_terminated"] is True
    assert "worker_deadline_reached" not in summary["reason_codes"]
    assert {process.pid for process in multiprocessing.active_children()} == before
    with exclusive_private_file_lock(tmp_path / "output_shared/state/required_data_prefetch.lock"):
        pass


@pytest.mark.parametrize("status", ["ready", "degraded", "deadline_reached"])
def test_final_preserves_worker_result_and_shard_accounting(tmp_path, monkeypatch, status):
    def worker(**kwargs):
        summary = mod._opening_chain_warmup_summary(**{
            key: kwargs[key] for key in ("next_formal_target_utc", "worker_stop_at_utc", "lock_release_by_utc")
        })
        summary.update(status=status, planned_shards=3, fetched_shards=1,
                       cache_hit_shards=2 if status == "ready" else 1,
                       failed_shards=int(status == "degraded"),
                       deadline_skipped_shards=int(status == "deadline_reached"),
                       reason_codes=[] if status == "ready" else ["provider_error" if status == "degraded" else "worker_deadline_reached"])
        return summary

    monkeypatch.setattr(mod, "_warm_required_data_chain_cache_inprocess", worker)
    with offline_runtime(tmp_path):
        summary = mod.warm_required_data_chain_cache(**warmup_kwargs(tmp_path, representative_config()))
    assert summary["status"] == status
    assert summary["fetched_shards"] == 1
    assert summary["failed_shards"] == int(status == "degraded")
    assert summary["deadline_skipped_shards"] == int(status == "deadline_reached")
    assert "child_failed" not in summary["reason_codes"]


def test_eof_without_final_marks_only_unfinished_shards_failed(tmp_path, monkeypatch):
    def child(send_conn, kwargs):
        summary = mod._opening_chain_warmup_summary(**{
            key: kwargs[key] for key in ("next_formal_target_utc", "worker_stop_at_utc", "lock_release_by_utc")
        })
        summary.update(status="degraded", planned_shards=4, fetched_shards=1,
                       cache_hit_shards=1, failed_shards=1, reason_codes=["provider_error"])
        send_conn.send({"summary": summary, "final": False})
        send_conn.close()

    monkeypatch.setattr(mod, "_warm_required_data_chain_cache_child", child)
    with offline_runtime(tmp_path):
        summary = mod.warm_required_data_chain_cache(**warmup_kwargs(tmp_path, representative_config()))
    assert summary["status"] == "degraded"
    assert summary["fetched_shards"] == summary["cache_hit_shards"] == 1
    assert summary["failed_shards"] == 2
    assert summary["deadline_skipped_shards"] == 0
    assert set(summary["reason_codes"]) == {"provider_error", "child_failed"}


def test_complete_progress_without_final_still_obeys_real_worker_deadline(tmp_path, monkeypatch):
    def child(send_conn, kwargs):
        summary = mod._opening_chain_warmup_summary(**{
            key: kwargs[key] for key in ("next_formal_target_utc", "worker_stop_at_utc", "lock_release_by_utc")
        })
        summary.update(status="ready", planned_shards=2, fetched_shards=2)
        send_conn.send({"summary": summary, "final": False})
        while True:
            time.sleep(1)

    monkeypatch.setattr(mod, "_warm_required_data_chain_cache_child", child)
    with offline_runtime(tmp_path):
        summary = mod.warm_required_data_chain_cache(**warmup_kwargs(tmp_path, representative_config(), seconds=0.3))
    assert summary["status"] == "deadline_reached"
    assert summary["child_terminated"] is True
    assert summary["fetched_shards"] == 2
    assert summary["failed_shards"] == summary["deadline_skipped_shards"] == 0
    assert "worker_deadline_reached" in summary["reason_codes"]


def test_broad_cache_remains_day_scoped_and_preserves_all_strikes(tmp_path):
    config = representative_config()
    with offline_runtime(tmp_path) as provider:
        summary = mod.warm_required_data_chain_cache(**warmup_kwargs(tmp_path, config))
        assert summary["status"] == "ready"
        path = option_chain_shard_cache_path(tmp_path, "US.ALFA", EXPIRATIONS[0])
        shard = json.loads(path.read_text())
        assert shard["asof_date"] == TRADING_DATE.isoformat()
        assert {row["option_type"] for row in shard["rows"]} == {"PUT", "CALL"}
        assert {row["strike_price"] for row in shard["rows"]} == {90.0, 100.0, 110.0, 130.0}
        provider.phase = "next_day"
        gateway = provider.open_quote_gateway()
        try:
            result = fetch_option_chains(
                gateway=gateway, retry_call=mod.retry_futu_gateway_call,
                request=OptionChainFetchRequest(
                    symbol="ALFA", underlier_code="US.ALFA", expirations=[EXPIRATIONS[0]],
                    base_dir=tmp_path, asof_date=(TRADING_DATE + timedelta(days=1)).isoformat(),
                    option_types="put,call", chain_cache=True, no_retry=True,
                ),
            )
        finally:
            gateway.close()
        assert result.status == "ok"
        assert result.opend_call_count == 1
        assert result.from_cache_expirations == []
