from __future__ import annotations

import multiprocessing
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.application.wheel import capacity
from src.infrastructure import futu_gateway as gateway


class WorkerSDK:
    def __init__(self, scenario):
        self.scenario = scenario
        self.closed = threading.Event()
        threading.Thread(target=self.closed.wait, daemon=False).start()
        if scenario == 'construct':
            threading.Event().wait()

    def query(self):
        if self.scenario == 'query':
            threading.Event().wait()
        return {'pid': os.getpid(), 'children': len(multiprocessing.active_children())}

    def close(self):
        if self.scenario == 'close':
            threading.Event().wait()
        self.closed.set()


def attribution_child(connection, config, account, runtime_root, *args):
    # Install the fake SDK in the actual spawn child, not just its parent.
    gateway._create_futu_sdk_context = lambda kind, kwargs: WorkerSDK(config.get('scenario'))
    capacity.current_exchange_rate_snapshot = lambda **kwargs: None
    capacity.project_exchange_rate_snapshot = lambda *args, **kwargs: None

    def portfolio(**kwargs):
        def query():
            context = gateway._start_futu_query_context('trade', {})
            try:
                return context.query()
            finally:
                context.close()
        if config.get('threaded'):
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(query).result()
        return query()

    capacity.load_account_portfolio_context = portfolio
    capacity._attribution_capacity_worker(connection, config, account, runtime_root, *args)


@pytest.mark.parametrize('threaded', [False, True])
def test_real_attribution_daemon_queries_sdk_without_grandchildren(monkeypatch, tmp_path, threaded):
    before = {child.pid for child in multiprocessing.active_children()}
    monkeypatch.setattr(capacity, '_attribution_capacity_worker', attribution_child)
    result = capacity.observe_trade_attribution_capacity(config={'threaded': threaded}, account='qa', runtime_root=tmp_path)
    assert 'portfolio' in result, result
    assert result['portfolio']['pid'] != os.getpid()
    assert result['portfolio']['children'] == 0
    assert {child.pid for child in multiprocessing.active_children()} <= before


@pytest.mark.parametrize('scenario', ['construct', 'query', 'close'])
@pytest.mark.parametrize('cancel', [False, True])
def test_owned_sdk_hang_is_reaped_by_observer(monkeypatch, tmp_path, scenario, cancel):
    before = {child.pid for child in multiprocessing.active_children()}
    monkeypatch.setattr(capacity, '_attribution_capacity_worker', attribution_child)
    stop = threading.Event()
    timer = threading.Timer(2, stop.set) if cancel else None
    if timer:
        timer.start()
    start = time.monotonic()
    result = capacity.observe_trade_attribution_capacity(
        config={'scenario': scenario}, account='qa', runtime_root=tmp_path, stop_event=stop)
    assert result['error'] == ('cancelled' if cancel else 'provider_timeout')
    assert time.monotonic() - start < (4 if cancel else 12)
    assert {child.pid for child in multiprocessing.active_children()} <= before
    if timer:
        timer.join()


def test_main_process_cannot_opt_out_of_sdk_isolation():
    with pytest.raises(RuntimeError, match='supervised daemon'):
        gateway.own_futu_sdk_in_current_worker(deadline=time.monotonic() + 10)


def prefetch_child(connection, kwargs):
    from src.application.multi_tick import required_data_prefetch as prefetch
    from concurrent.futures import ThreadPoolExecutor
    gateway._create_futu_sdk_context = lambda kind, kwargs: WorkerSDK(None)

    def warm(**kwargs):
        def query():
            proxy = gateway._start_futu_query_context('quote', {})
            try:
                return proxy.query()
            finally:
                proxy.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return {'status': 'ready', **pool.submit(query).result()}
    prefetch._warm_required_data_chain_cache_inprocess = warm
    prefetch._warm_required_data_chain_cache_child(connection, kwargs)


def test_real_prefetch_spawn_threads_share_sdk_owner_and_release_lock(monkeypatch, tmp_path):
    from src.application.multi_tick import required_data_prefetch as prefetch
    from src.infrastructure.private_storage import exclusive_private_file_lock
    before = {child.pid for child in multiprocessing.active_children()}
    monkeypatch.setattr(prefetch, '_warm_required_data_chain_cache_child', prefetch_child)
    later = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
    result = prefetch.warm_required_data_chain_cache(
        base=tmp_path, base_cfg={}, cfg_path=tmp_path / 'config.json', accounts=['qa'], markets_to_run=['us'],
        symbols_arg=None, next_formal_target_utc=later, worker_stop_at_utc=later, lock_release_by_utc=later)
    assert result['status'] == 'ready', result
    assert result['pid'] != os.getpid() and result['children'] == 0
    assert {child.pid for child in multiprocessing.active_children()} <= before
    with exclusive_private_file_lock(tmp_path / 'output_shared/state/required_data_prefetch.lock'):
        pass


def unregistered_child(connection):
    def forbidden(*args, **kwargs):
        raise AssertionError('must reject before allocating an SDK process')
    gateway._FutuContextProcess = forbidden
    try:
        gateway._start_futu_query_context('quote', {})
    except RuntimeError as exc:
        connection.send(str(exc))
    finally:
        connection.close()


def test_unregistered_daemon_rejected_before_sdk_process_creation():
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=unregistered_child, args=(sender,), daemon=True)
    try:
        process.start()
        sender.close()
        assert receiver.poll(5)
        assert receiver.recv() == 'SDK query requires explicit supervised worker ownership'
        process.join(2)
        assert process.exitcode == 0
    finally:
        sender.close()
        receiver.close()
        if process.is_alive():
            process.kill()
            process.join(2)
        process.close()


@pytest.mark.parametrize('method', ['query', 'close'])
def test_worker_deadline_cannot_be_extended_by_context_controls(monkeypatch, method):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(gateway, '_SDK_WORKER_OWNER', (os.getpid(), time.monotonic() - 1))
    sdk = SimpleNamespace(query=lambda: calls.append('query'), close=lambda: calls.append('close'))
    proxy = gateway._FutuQueryContext(sdk, deadline=None, cancelled=None)
    token = gateway._FETCH_CONTROL.set((time.monotonic() + 100, None))
    try:
        with pytest.raises(TimeoutError):
            getattr(proxy, method)()
        assert calls == []
    finally:
        gateway._FETCH_CONTROL.reset(token)


def test_expired_worker_deadline_rejects_before_sdk_factory(monkeypatch):
    monkeypatch.setattr(gateway, '_SDK_WORKER_OWNER', (os.getpid(), time.monotonic() - 1))
    monkeypatch.setattr(gateway, '_create_futu_sdk_context', lambda *args: pytest.fail('SDK was created'))
    with pytest.raises(TimeoutError):
        gateway._start_futu_query_context('quote', {}, deadline=time.monotonic() + 100)
