"""The formal execution path uses the existing FX owner under its caller budget."""
from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest

from src.application import current_fx_run, tick_account_execution
from src.infrastructure import exchange_rates
from tests.cash_evidence_helpers import cash_config
from tests.test_tick_account_execution_barrier import _request


class _OwnerReached(Exception):
    pass


def test_account_execution_forwards_controls_through_real_fx_seal(monkeypatch, tmp_path):
    calls = []
    stop = threading.Event()
    deadline = time.monotonic() + 30

    def owner(**kwargs):
        calls.append(kwargs)
        raise _OwnerReached

    monkeypatch.setattr(current_fx_run, "current_exchange_rate_snapshot", owner)
    request = _request(tmp_path, accounts=["lx"], workers=1, force=False)
    request = replace(request, base_cfg={**request.base_cfg, **cash_config()},
                      deadline=deadline, cancelled=stop.is_set)
    with pytest.raises(_OwnerReached):
        tick_account_execution.run_tick_account_execution(request)
    assert len(calls) == 1
    assert calls[0]["deadline"] == deadline
    assert calls[0]["cancelled"] == request.cancelled


@pytest.mark.parametrize("stop_kind", ["expired", "cancelled"])
def test_account_execution_does_not_begin_fx_after_stop(monkeypatch, tmp_path, stop_kind):
    monkeypatch.setattr(current_fx_run, "current_exchange_rate_snapshot",
                        lambda **kwargs: pytest.fail("stopped execution reached FX HTTP"))
    request = _request(tmp_path, accounts=["lx"], workers=1, force=False)
    request = replace(request, base_cfg={**request.base_cfg, **cash_config()},
                      deadline=time.monotonic() + (-1 if stop_kind == "expired" else 30),
                      cancelled=lambda: stop_kind == "cancelled")
    with pytest.raises(TimeoutError):
        tick_account_execution.run_tick_account_execution(request)
    assert not (tmp_path / "output_runs" / "run-1" / "state" / current_fx_run.RUN_FX_NAME).exists()


@pytest.mark.parametrize("stop_kind", ["expired", "cancelled"])
def test_real_fx_owner_stops_before_second_http_source_and_sealing(monkeypatch, tmp_path, stop_kind):
    calls = []
    stop = threading.Event()
    deadline = time.monotonic() + (.03 if stop_kind == "expired" else 30)

    def http(url, **kwargs):
        calls.append((url, kwargs["timeout_sec"]))
        if stop_kind == "expired":
            time.sleep(.05)
        else:
            stop.set()
        return "offline invalid quote"

    monkeypatch.setattr(exchange_rates, "_http_get", http)
    with pytest.raises(TimeoutError):
        current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="stopped",
                                           deadline=deadline, cancelled=stop.is_set)
    assert len(calls) == 1
    if stop_kind == "expired":
        assert calls[0][1] <= .03
    assert not (tmp_path / "output_runs" / "stopped" / "state" / current_fx_run.RUN_FX_NAME).exists()
    assert not (tmp_path / "output_shared" / "state" / "rate_cache.json").exists()


def test_stop_during_encoding_prevents_immutable_publication(monkeypatch, tmp_path):
    stop = threading.Event()
    monkeypatch.setattr(current_fx_run, "current_exchange_rate_snapshot", lambda **kwargs: {"pairs": {}})
    encode = current_fx_run.encode_run_fx_snapshot

    def encode_then_cancel(*args, **kwargs):
        raw = encode(*args, **kwargs)
        stop.set()
        return raw

    monkeypatch.setattr(current_fx_run, "encode_run_fx_snapshot", encode_then_cancel)
    with pytest.raises(TimeoutError):
        current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="before-write", cancelled=stop.is_set)
    assert not (tmp_path / "output_runs" / "before-write" / "state" / current_fx_run.RUN_FX_NAME).exists()


def test_stop_after_publication_fails_but_retry_reuses_original_bytes(monkeypatch, tmp_path):
    stop = threading.Event()
    calls = []
    snapshot = {"schema_version": 2, "pairs": {}}
    monkeypatch.setattr(current_fx_run, "current_exchange_rate_snapshot", lambda **kwargs: calls.append(kwargs) or snapshot)
    publish = current_fx_run.write_run_state_bytes_once_safely

    def publish_then_cancel(**kwargs):
        result = publish(**kwargs)
        stop.set()
        return result

    monkeypatch.setattr(current_fx_run, "write_run_state_bytes_once_safely", publish_then_cancel)
    with pytest.raises(TimeoutError):
        current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="published", cancelled=stop.is_set)
    path = Path(tmp_path) / "output_runs" / "published" / "state" / current_fx_run.RUN_FX_NAME
    frozen = path.read_bytes()
    stop.clear()
    actual, _digest = current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="published", cancelled=stop.is_set)
    assert actual == snapshot
    assert path.read_bytes() == frozen
    assert len(calls) == 1
