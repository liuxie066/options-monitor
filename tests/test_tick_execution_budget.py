"""One absolute budget follows real process waits and the required-data bridge."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from src.application import required_data_fetching, runtime_trigger_context
from src.infrastructure import external_services


def test_required_data_bridge_preserves_deadline_and_cancellation(monkeypatch, tmp_path):
    observed = []
    cancelled = threading.Event().is_set
    monkeypatch.setattr(required_data_fetching, "fetch_symbol_request", lambda request: observed.append(request) or {})
    deadline = time.monotonic() + 1
    required_data_fetching.execute_required_data_opend(
        base=tmp_path,
        request=required_data_fetching.RequiredDataFetchRequest(
            symbol="0700.HK", limit_expirations=1,
            deadline=deadline, cancelled=cancelled,
        ),
    )
    assert observed[0].deadline == deadline
    assert observed[0].cancelled is cancelled


def test_market_facade_builder_passes_controls_to_the_real_fetch_request(monkeypatch, tmp_path):
    from src.application import market_data_provider, opend_symbol_fetching

    observed = []
    deadline = time.monotonic() + 5
    cancelled = threading.Event().is_set
    monkeypatch.setattr(opend_symbol_fetching, "fetch_symbol_request", lambda request: observed.append(request) or {"fixture": True})
    result = market_data_provider.fetch_symbol(
        "0700.HK", base_dir=tmp_path, explicit_expirations=["2026-12-18"],
        deadline=deadline, cancelled=cancelled,
    )
    assert result == {"fixture": True}
    assert len(observed) == 1
    assert observed[0].deadline == deadline and observed[0].cancelled is cancelled


def test_subprocess_inherits_earlier_deadline_instead_of_a_new_timeout(tmp_path):
    deadline = time.monotonic() + 5
    env = runtime_trigger_context.tick_subprocess_env(deadline=deadline, environ=dict(os.environ))
    effective = runtime_trigger_context.resolve_tick_deadline(timeout_seconds=60, environ=env)
    assert effective == deadline
    result = external_services.run_command(
        [sys.executable, "-I", "-c", "import json,os,time; print(json.dumps([float(os.environ['OM_TICK_DEADLINE_MONOTONIC']),time.monotonic()]))"],
        cwd=tmp_path, capture_output=True, text=True,
        env=env, deadline=effective,
    )
    inherited, child_started = json.loads(result.stdout)
    assert inherited == deadline
    assert child_started < inherited
    assert "OM_TICK_DEADLINE_MONOTONIC" not in runtime_trigger_context.build_trigger_context(environ=env)


def test_second_process_wait_consumes_first_waits_elapsed_budget(tmp_path):
    started = time.monotonic()
    deadline = started + .20
    external_services.run_command(
        [sys.executable, "-I", "-c", "import time; time.sleep(.03)"],
        cwd=tmp_path, timeout_sec=2, deadline=deadline,
    )
    with pytest.raises((subprocess.TimeoutExpired, TimeoutError)):
        external_services.run_command(
            [sys.executable, "-I", "-c", "import time; time.sleep(10)"],
            cwd=tmp_path, timeout_sec=2, deadline=deadline,
        )
    assert time.monotonic() - started < 1.5, "the second process received a fresh stage budget"


def test_cancellation_interrupts_and_reaps_a_running_command(tmp_path, monkeypatch):
    stop = threading.Event()
    original = subprocess.Popen
    observed = []

    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        observed.append(process)
        return process

    monkeypatch.setattr(external_services.subprocess, "Popen", observe)
    timer = threading.Timer(.10, stop.set)
    timer.start()
    try:
        with pytest.raises(TimeoutError, match="cancelled"):
            external_services.run_command(
                [sys.executable, "-I", "-c", "import time; time.sleep(10)"],
                cwd=tmp_path, capture_output=True, text=True,
                timeout_sec=2, cancelled=stop.is_set,
            )
        assert len(observed) == 1
        assert observed[0].returncode is not None
    finally:
        timer.cancel()
        timer.join(timeout=1)


def test_expired_budget_does_not_start_another_process(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("an expired business budget started a new process")

    monkeypatch.setattr(external_services.subprocess, "run", forbidden)
    with pytest.raises(TimeoutError, match="deadline"):
        external_services.run_command([sys.executable, "-I", "-c", "pass"], cwd=tmp_path, deadline=0)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "invalid"])
def test_invalid_inherited_control_cannot_reset_the_budget(raw):
    with pytest.raises(ValueError):
        runtime_trigger_context.resolve_tick_deadline(timeout_seconds=60, environ={"OM_TICK_DEADLINE_MONOTONIC": raw})


def test_calendar_cancellation_stops_followup_queries_and_closes_gateway(monkeypatch, tmp_path):
    from src.application import close_advice_required_data as calendar
    from test_close_advice_required_data import _config, _plan, _position
    from test_market_data_provider_consumers import ConsumerProvider, install

    stop = threading.Event()

    class CancellingCalendar(ConsumerProvider):
        def get_trading_days_with_receipt(self, gateway, **kwargs):
            assert kwargs["deadline"] == deadline
            assert kwargs["cancelled"] is cancelled
            result = super().get_trading_days_with_receipt(gateway, **kwargs)
            stop.set()
            return result

    provider = install(monkeypatch, CancellingCalendar())
    config = {**_config(account="lx"), "market_data": {"provider": "fixture"}}
    plan = _plan(account_configs={"lx": config}, base_config=config,
                 position_records_by_account={"lx": [_position(account="lx", lot_id="lot-lx")]})
    path = tmp_path / "plan.json"
    calendar.publish_close_advice_required_data_plan(path=path, payload=plan)
    before = path.read_bytes()
    deadline = time.monotonic() + 5
    cancelled = stop.is_set
    with pytest.raises(TimeoutError, match="cancelled"):
        calendar.enrich_close_advice_required_data_plan(
            plan_path=path, expected_run_id="run-1", deadline=deadline, cancelled=cancelled,
        )
    assert provider.closed == [True]
    assert not any(name in {"rights", "state"} for name, _ in provider.calls)
    assert path.read_bytes() == before, "cancelled enrichment published a new evidence identity"


def test_calendar_worker_uses_parent_deadline_without_mutating_plan(monkeypatch, tmp_path):
    from src.application import close_advice_required_data as calendar

    path = tmp_path / "plan.json"
    path.write_text('{"sealed":true}')
    deadline = time.monotonic() + 5
    cancelled = threading.Event().is_set
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(calendar, "run_command", run)
    assert calendar.enrich_close_advice_required_data_plan_bounded(
        plan_path=path, expected_run_id="run-1", python=Path(sys.executable), repo_root=tmp_path,
        deadline=deadline, cancelled=cancelled,
    ) is None
    assert len(calls) == 1
    controls = calls[0][1]
    assert controls["deadline"] == deadline and controls["cancelled"] is cancelled
    assert controls["timeout_sec"] == 30
    assert float(controls["env"]["OM_TICK_DEADLINE_MONOTONIC"]) == deadline
    assert path.read_text() == '{"sealed":true}'


def test_roll_quote_cancellation_does_not_start_followup_evidence(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.application import roll_quote_refresh as refresh

    stop = threading.Event()
    deadline = time.monotonic() + 5
    cancelled = stop.is_set
    requests = []

    def fetch(*, base, request):
        requests.append(request)
        stop.set()
        return {"rows": [{"expiration": "2026-12-18", "option_type": "put"}]}

    def forbidden(**kwargs):
        pytest.fail("cancelled quote refresh started market-state or quote-rights work")

    monkeypatch.setattr(refresh, "resolve_market_data_provider", lambda _: SimpleNamespace(name="opend", open_quote_gateway=forbidden))
    monkeypatch.setattr(refresh, "execute_required_data_opend", fetch)
    with pytest.raises(TimeoutError, match="cancelled"):
        refresh.refresh_roll_quote_evidence(
            targets=[{"symbol": "0700.HK", "expirations": ["2026-12-18"]},
                     {"symbol": "3690.HK", "expirations": ["2026-12-18"]}],
            config={}, base_dir=tmp_path, deadline=deadline, cancelled=cancelled,
        )
    assert len(requests) == 1
    assert requests[0].deadline == deadline and requests[0].cancelled is cancelled


def test_calendar_worker_nonzero_exit_is_not_reported_as_success(monkeypatch, tmp_path):
    from src.application import close_advice_required_data as calendar

    monkeypatch.setattr(calendar, "run_command", lambda command, **kwargs: subprocess.CompletedProcess(command, 7))
    assert calendar.enrich_close_advice_required_data_plan_bounded(
        plan_path=tmp_path / "plan.json", expected_run_id="run-1", python=Path(sys.executable), repo_root=tmp_path,
    ) == "calendar_enrichment_failed:CalledProcessError"


@pytest.mark.parametrize("preflight_elapsed", [4.0, 11.0])
def test_cron_preflight_consumes_the_inherited_budget(monkeypatch, tmp_path, preflight_elapsed):
    from src.application import tick_cron

    clock = [100.0]
    calls = []
    monkeypatch.setattr(tick_cron.time, "monotonic", lambda: clock[0])

    def preflight(**kwargs):
        clock[0] += preflight_elapsed

    def run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0)

    rc = tick_cron.run_tick_cron(
        market="hk", accounts=["lx"], timeout_seconds=10, no_send=True,
        lock_path=str(tmp_path / "tick.lock"), cwd=tmp_path,
        environ={"OM_RUNTIME_ROOT": str(tmp_path)},
        preflight_config_fn=preflight, run_cmd=run,
    )
    if preflight_elapsed < 10:
        assert rc == 0 and len(calls) == 1
        assert calls[0]["timeout"] == 6.0
        assert float(calls[0]["env"]["OM_TICK_DEADLINE_MONOTONIC"]) == 110.0
    else:
        assert rc == 124 and calls == [], "expired preflight started new business work"
