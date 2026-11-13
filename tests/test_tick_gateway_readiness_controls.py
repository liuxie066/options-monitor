"""Tick controls cover native gateway readiness and remain fresh on pool reuse."""
from __future__ import annotations

from functools import partial
import importlib.util
import json
from multiprocessing.connection import wait
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from src.application import plugin_runtime, roll_quote_refresh
from src.application.multi_tick import required_data_prefetch as prefetch
from src.infrastructure import futu_gateway as gateway
from src.infrastructure.futu_gateway_pool import ThreadLocalFutuGatewayPool


class _ReadinessSDK:
    """Only the SDK transport is replaced; the native process/proxy still run."""

    def __init__(self, proof: str, blocking: str):
        self.proof = Path(proof)
        self.blocking = Path(blocking)

    def _record(self, operation):
        with self.proof.open("a") as stream:
            stream.write(json.dumps({"operation": operation}) + "\n")
        if self.blocking.read_text() == operation:
            threading.Event().wait()

    def get_sync_conn_id(self):
        self._record("health")
        return 1

    def get_global_state(self):
        self._record("readiness")
        return 0, {"program_status_type": "READY", "qot_logined": True}

    def get_market_state(self, code_list):
        self._record("state")
        return 0, [{"code": code, "market_state": "MORNING"} for code in code_list]

    def get_user_info(self):
        self._record("rights")
        return 0, {"hk_qot_right": "LV1", "hk_option_qot_right": "LV1"}

    def close(self):
        pass


def _sdk_factory(_kind, kwargs, *, proof, blocking):
    assert "deadline" not in kwargs and "cancelled" not in kwargs
    sdk = _ReadinessSDK(proof, blocking)
    sdk._record("connect")
    return sdk


def _operations(proof):
    if not proof.exists():
        return []
    return [json.loads(line)["operation"] for line in proof.read_text().splitlines() if line]


@pytest.fixture
def readiness(monkeypatch, tmp_path):
    root = next(parent for parent in Path(__file__).resolve().parents
                if (parent / "src/infrastructure/futu_gateway.py").is_file())
    provider_path = root / "plugins/market-opend/src/om_market_opend/__init__.py"
    spec = importlib.util.spec_from_file_location("_readiness_market_opend_source", provider_path)
    provider_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider_module)
    provider = provider_module.OpenDMarketDataProvider()
    proof = tmp_path / "sdk-calls.jsonl"
    blocking = tmp_path / "blocking"
    blocking.write_text("")
    monkeypatch.setenv("OM_TICK_HANG_TRACEBACK_PATH", str(tmp_path / "sdk-hang.log"))
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    monkeypatch.setenv("OM_ENV_FILE", str(tmp_path / "missing.env"))
    original_connect = socket.socket.connect

    def offline(sock, address):
        if isinstance(address, tuple):
            raise AssertionError("readiness regressions forbid network/provider access")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(gateway, "port_open", lambda *args, **kwargs: True)
    monkeypatch.setattr(gateway, "_ensure_futu_api_importable", lambda: None)
    monkeypatch.setattr(plugin_runtime, "require_plugin", lambda _name: provider_module)
    monkeypatch.setattr(gateway, "_create_futu_sdk_context",
                        partial(_sdk_factory, proof=str(proof), blocking=str(blocking)))
    # Bound the deliberately broken baseline by the existing SDK timeout owner.
    monkeypatch.setattr(gateway, "FUTU_QUERY_CONNECT_TIMEOUT_SEC", 3.0)
    monkeypatch.setattr(gateway, "FUTU_QUERY_TIMEOUT_SEC", 3.0)
    remotes = []
    original_process_init = gateway._FutuContextProcess.__init__

    def remember_process(remote, *args, **kwargs):
        original_process_init(remote, *args, **kwargs)
        remotes.append(remote)

    monkeypatch.setattr(gateway._FutuContextProcess, "__init__", remember_process)
    builds = []
    original_ready = gateway.build_ready_futu_quote_gateway

    def remember_ready(**kwargs):
        builds.append(dict(kwargs))
        return original_ready(**kwargs)

    monkeypatch.setattr(gateway, "build_ready_futu_quote_gateway", remember_ready)
    pool = ThreadLocalFutuGatewayPool()
    monkeypatch.setattr(prefetch, "_gateway_pool", pool)
    monkeypatch.setattr(prefetch, "provider_by_name", lambda _name: provider)
    monkeypatch.setattr(roll_quote_refresh, "resolve_market_data_provider", lambda _config: provider)
    monkeypatch.setattr(prefetch.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)
    fetches = []

    def after_readiness(_symbol, **kwargs):
        fetches.append(kwargs)
        kwargs["gateway"].get_market_state(
            ["HK.TEST"], deadline=kwargs.get("deadline"), cancelled=kwargs.get("cancelled"),
        )
        raise gateway.FutuGatewayError("readiness probe completed")

    monkeypatch.setattr(prefetch, "fetch_symbol", after_readiness)
    quote_requests = []

    def quote_rows(*, base, request):
        quote_requests.append(request)
        return {"rows": [{"expiration": "2026-12-18", "option_type": "put"}]}

    monkeypatch.setattr(roll_quote_refresh, "execute_required_data_opend", quote_rows)

    def invoke(entry, controls):
        if entry == "prefetch":
            payload = prefetch._fetch_one_inprocess(
                {"symbol": "0700.HK", "fetch": {"source": "opend", "host": "offline.invalid", "port": 1}},
                base=tmp_path, shared_required=tmp_path / "required_data",
                opend_fetch_cfg=prefetch._resolve_opend_fetch_cfg({}),
                batch_cfg=SimpleNamespace(market_snapshot=0), **controls,
            )
            return payload["message"]
        try:
            roll_quote_refresh.refresh_roll_quote_evidence(
                targets=[{"symbol": "0700.HK", "expirations": ["2026-12-18"],
                          "host": "offline.invalid", "port": 1}],
                config={}, base_dir=tmp_path, **controls,
            )
        except TimeoutError as exc:
            return str(exc)
        return "refreshed"

    yield SimpleNamespace(
        invoke=invoke, builds=builds, fetches=fetches, quote_requests=quote_requests,
        proof=proof, blocking=blocking, pool=pool,
    )
    pool.close_registered()
    for remote in remotes:
        remote.abort()
        assert wait([remote._process.sentinel], timeout=3), "readiness SDK child remained alive"


@pytest.mark.parametrize("entry", ["prefetch", "roll"])
def test_readiness_receives_exact_caller_controls(readiness, entry):
    cancelled = threading.Event().is_set
    controls = {"deadline": time.monotonic() + 10, "cancelled": cancelled}
    readiness.invoke(entry, controls)
    assert len(readiness.builds) == 1
    assert readiness.builds[0].get("deadline") == controls["deadline"]
    assert readiness.builds[0].get("cancelled") is cancelled
    assert "readiness" in _operations(readiness.proof)


@pytest.mark.parametrize("entry", ["prefetch", "roll"])
@pytest.mark.parametrize("operation", ["connect", "readiness"])
@pytest.mark.parametrize("control", ["deadline", "cancelled"])
def test_tick_control_interrupts_native_readiness(readiness, entry, operation, control):
    readiness.blocking.write_text(operation)
    started = time.monotonic()
    controls = ({"deadline": started + 1.5} if control == "deadline" else
                {"cancelled": lambda: operation in _operations(readiness.proof)})
    message = readiness.invoke(entry, controls)
    elapsed = time.monotonic() - started
    assert operation in _operations(readiness.proof)
    assert control in message
    assert elapsed < 2.5, f"{entry} ignored Tick {control} for {elapsed:.3f}s"
    assert not readiness.fetches
    assert "state" not in _operations(readiness.proof)
    assert "rights" not in _operations(readiness.proof)


def test_prefetch_pool_reuse_uses_current_cancellation(readiness):
    previous_cancelled = threading.Event()
    first = {"deadline": time.monotonic() + 10, "cancelled": previous_cancelled.is_set}
    assert readiness.invoke("prefetch", first) == "readiness probe completed"
    previous_gateway = readiness.fetches[0]["gateway"]
    previous_cancelled.set()
    current = {"deadline": time.monotonic() + 10, "cancelled": threading.Event().is_set}
    assert readiness.invoke("prefetch", current) == "readiness probe completed"
    assert len(readiness.builds) == 1, "old initialization controls discarded a reusable gateway"
    assert readiness.fetches[-1]["gateway"] is previous_gateway
    assert readiness.fetches[-1]["cancelled"] is current["cancelled"]
    readiness.proof.write_text("")
    readiness.blocking.write_text("state")
    message = readiness.invoke("prefetch", {
        "deadline": time.monotonic() + 10,
        "cancelled": lambda: "state" in _operations(readiness.proof),
    })
    assert "cancelled" in message
    assert len(readiness.builds) == 1
    assert "state" in _operations(readiness.proof)


@pytest.mark.parametrize("earlier", ["worker", "caller"])
def test_warmup_cold_gateway_inherits_earlier_stop_and_original_cancel(readiness, monkeypatch, tmp_path, earlier):
    from datetime import datetime, timedelta, timezone
    from src.application.required_data_fetching import RequiredDataFetchRequest

    symbol = {"symbol": "0700.HK"}
    monkeypatch.setattr(prefetch, "build_account_runtime_config", lambda **kwargs: {})
    monkeypatch.setattr(prefetch, "build_cross_account_prefetch_config", lambda **kwargs: {"symbols": [symbol]})
    monkeypatch.setattr(prefetch, "resolve_templates_config", lambda config: {})
    monkeypatch.setattr(prefetch, "resolve_watchlist_config", lambda config: config["symbols"])
    monkeypatch.setattr(prefetch, "apply_profiles", lambda item, profiles: item)
    monkeypatch.setattr(prefetch, "build_prefetch_symbol_plan", lambda items: SimpleNamespace(symbol_cfgs=items))
    stops = []

    def prefill(**kwargs):
        stops.append(kwargs["deadline"])
        return {}, set()

    monkeypatch.setattr(prefetch, "_prefill_spot_observation_cache", prefill)
    monkeypatch.setattr(prefetch, "_build_prefetch_fetch_plan", lambda *args, **kwargs: SimpleNamespace(
        symbol=symbol["symbol"], projection_outcome="success_rows", merged_specs=[object()],
        spot_reference=100.0, underlier_observation=None,
    ))
    request = RequiredDataFetchRequest(
        symbol=symbol["symbol"], limit_expirations=0, host="offline.invalid", port=1,
        explicit_expirations=["2026-12-18"], trading_date="2026-11-13",
    )
    monkeypatch.setattr(prefetch, "build_fetch_request_from_spec", lambda **kwargs: request)
    chain_requests = []

    def cached_chain(**kwargs):
        chain_requests.append(kwargs["request"])
        return SimpleNamespace(opend_call_count=0, rate_gate_wait_sec=0,
                               expiration_statuses={"2026-12-18": "cache"})

    monkeypatch.setattr(prefetch, "fetch_option_chains", cached_chain)
    cancelled = threading.Event().is_set
    caller_deadline = time.monotonic() + (30 if earlier == "worker" else 10)
    stop = (datetime.now(timezone.utc) + timedelta(seconds=10 if earlier == "worker" else 30)).isoformat()
    result = prefetch._warm_required_data_chain_cache_inprocess(
        base=tmp_path, base_cfg={}, cfg_path=tmp_path / "unused.json", accounts=["lx"],
        markets_to_run=["HK"], symbols_arg=None, next_formal_target_utc=stop,
        worker_stop_at_utc=stop, lock_release_by_utc=stop,
        deadline=caller_deadline, cancelled=cancelled,
    )
    assert result["status"] == "ready" and result["cache_hit_shards"] == 1
    assert len(readiness.builds) == len(stops) == len(chain_requests) == 1
    assert readiness.builds[0].get("deadline") == stops[0] == chain_requests[0].deadline
    assert readiness.builds[0].get("cancelled") is chain_requests[0].cancelled is cancelled
    assert stops[0] < caller_deadline if earlier == "worker" else stops[0] == caller_deadline
    assert "connect" in _operations(readiness.proof) and "readiness" in _operations(readiness.proof)
