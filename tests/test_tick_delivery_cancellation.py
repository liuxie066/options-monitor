"""Offline regressions for durable delivery facts and cancellation boundaries."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import socket
import threading
from time import monotonic
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

import test_daily_decision_brief_notification_flow as shared
from test_daily_brief_fanout_flow import config as fanout_config
from test_feishu_notification_sender import _wechat_state
from src.application import notification_delivery_adapter as adapters
from src.application import system_alerts
from src.application import tick_notification_flow as flow
from src.application.channels import feishu_notification
from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery_state
from src.application.notification_fanout import list_notification_fanouts
from src.application.secret_store import reset_default_secret_provider
from src.infrastructure import feishu_bitable
from src.infrastructure.fetch_control import check_fetch_control


NOW = "2026-07-21T14:00:40+00:00"
EXPLICIT_FAILURE = {
    "ok": False,
    "command_ok": False,
    "delivery_confirmed": False,
    "returncode": 1,
    "error_code": "SEND_FAILED",
}


def _success(kwargs: dict, message_id: str) -> dict:
    return {
        "ok": True,
        "command_ok": True,
        "delivery_confirmed": True,
        "returncode": 0,
        "message_id": message_id,
        "idempotency_key": kwargs["idempotency_key"],
    }


def _fake_adapter(send):
    return SimpleNamespace(
        send_fn=send,
        normalize_fn=lambda *, send_result: send_result,
        failure_stage="offline_delivery_regression",
    )


def _fixed_delivery(base: Path, account: str = "lx") -> dict:
    observed = read_daily_decision_brief_delivery_state(
        base=base, account=account, market="US",
    )
    return observed["state"]["days"][shared.MARKET_DATE]["fixed_reports"][shared.FIXED_TARGET]


def _run_allowing_cancel(request):
    # Cancellation may propagate after persisting outcomes or produce a failed
    # outcome. Durable facts and the absence of new I/O are decisive.
    try:
        return shared._run_flow(request)
    except TimeoutError:
        return None


@pytest.fixture(autouse=True)
def offline_tick_inputs(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("delivery cancellation tests forbid real network connections")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    monkeypatch.setenv("OM_ENV_FILE", str(tmp_path / "no-user-env"))
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "offline_app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "offline_secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_offline")
    reset_default_secret_provider()
    shared._patch_assembler(monkeypatch)
    monkeypatch.setattr(flow, "utc_now", lambda: NOW)
    monkeypatch.setattr(feishu_bitable, "_token_cache", {})
    monkeypatch.setattr(feishu_bitable, "_token_lock", threading.Lock())

    def check_without_sleep(_seconds, *, deadline=None, cancelled=None):
        # Keep the cancellation boundary that exposed D1 without spending time
        # in the ordinary retry delay.
        check_fetch_control(deadline, cancelled)

    monkeypatch.setattr(flow, "wait_fetch_control", check_without_sleep)
    yield
    reset_default_secret_provider()


def test_d1_confirmed_account_is_durable_before_other_account_cancel_and_not_resent(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(flow, "report_system_meta_signal", lambda **_kwargs: "offline")
    stopped = threading.Event()
    original_calls = []

    def send(**kwargs):
        original_calls.append(kwargs["idempotency_key"])
        if len(original_calls) == 2:
            stopped.set()
            return dict(EXPLICIT_FAILURE)
        return _success(kwargs, "confirmed-account-A")

    monkeypatch.setattr(flow, "select_notification_delivery_adapter", lambda _provider: _fake_adapter(send))
    bundle = shared._request(
        tmp_path,
        run_id="delivery-draft-d1",
        accounts=("lx", "sy"),
        fixed=True,
        delivery_only=False,
        pipeline_ok=True,
        trigger_kind="scheduled",
    )
    request = replace(bundle.request, cancelled=stopped.is_set, deadline=monotonic() + 30)
    _run_allowing_cancel(request)
    assert len(original_calls) == 2
    durable_status_before_resume = _fixed_delivery(tmp_path, "lx")["status"]
    assert request.tick_metrics["scan_outcomes"] == {"lx": True, "sy": True}

    stopped.clear()
    resume_calls = []

    def success(**kwargs):
        resume_calls.append(kwargs["idempotency_key"])
        return _success(kwargs, "confirmed-on-resume")

    monkeypatch.setattr(flow, "select_notification_delivery_adapter", lambda _provider: _fake_adapter(success))
    resume = shared._request(
        tmp_path,
        run_id="delivery-draft-d1-resume",
        accounts=("lx", "sy"),
        fixed=True,
        delivery_only=True,
        pipeline_ok=True,
        trigger_kind="scheduled",
    )
    _run_allowing_cancel(replace(resume.request, cancelled=stopped.is_set, deadline=monotonic() + 30))
    observations = {
        "durable_status_before_resume": durable_status_before_resume,
        "original_calls": original_calls,
        "resume_calls": resume_calls,
    }
    assert durable_status_before_resume == "confirmed", observations
    assert original_calls[0] not in resume_calls, observations


@pytest.mark.parametrize(
    ("cancellation_stage", "expected_status"),
    [
        ("before_io", "pending"),
        ("in_flight_failure", "ambiguous"),
        ("completed_body_cancel", "confirmed"),
        ("completed_response", "confirmed"),
    ],
)
def test_d2_feishu_cancel_preserves_external_outcome_semantics(
    monkeypatch, tmp_path, cancellation_stage, expected_status,
):
    monkeypatch.setattr(flow, "report_system_meta_signal", lambda **_kwargs: "offline")
    stopped = threading.Event()
    provider_posts = []

    class Response:
        status = 200

        def __init__(self, *, auth):
            self.auth = auth

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            if self.auth:
                return json.dumps({
                    "code": 0,
                    "tenant_access_token": "offline-token",
                    "expire": 7200,
                }).encode()
            # Retain an unknown attempt when cancellation accompanies an
            # incomplete response after the POST has started.
            if cancellation_stage == "in_flight_failure":
                stopped.set()
                raise urllib.error.URLError("offline incomplete response")
            # A complete successful body still carries its confirmation when
            # cancellation was signaled during read().
            if cancellation_stage == "completed_body_cancel":
                stopped.set()
            return json.dumps({
                "code": 0,
                "data": {"message_id": "provider-confirmed-message"},
            }).encode()

    def urlopen(request, **_kwargs):
        auth = "/auth/" in request.full_url
        if not auth:
            provider_posts.append(request.full_url)
        return Response(auth=auth)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    if cancellation_stage == "completed_response":
        for name in ("send_message", "send_post_message"):
            original = getattr(feishu_notification, name)

            def stop_after_response(*args, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                stopped.set()
                return result

            monkeypatch.setattr(feishu_notification, name, stop_after_response)

    actual_adapter = flow.select_notification_delivery_adapter("feishu_app")
    send_calls = []

    def send(**kwargs):
        send_calls.append(kwargs["idempotency_key"])
        # Exercise the actual last outer attempt, so durable outcome recording
        # runs rather than escaping from the first retry delay.
        if len(send_calls) == 1:
            return dict(EXPLICIT_FAILURE)
        return actual_adapter.send_fn(**kwargs)

    monkeypatch.setattr(
        flow,
        "select_notification_delivery_adapter",
        lambda _provider: SimpleNamespace(
            send_fn=send,
            normalize_fn=actual_adapter.normalize_fn,
            failure_stage=actual_adapter.failure_stage,
        ),
    )
    bundle = shared._request(
        tmp_path,
        run_id=f"delivery-draft-d2-{cancellation_stage}",
        config=shared._feishu_config(),
        accounts=("lx",),
        fixed=True,
        delivery_only=False,
        pipeline_ok=True,
        trigger_kind="scheduled",
    )
    if cancellation_stage == "before_io":
        shared._prepare_flow(bundle.request)
        stopped.set()
    request = replace(bundle.request, cancelled=stopped.is_set, deadline=monotonic() + 30)
    _run_allowing_cancel(request)
    envelope = _fixed_delivery(tmp_path)
    assert stopped.is_set()
    assert envelope["status"] == expected_status, {
        "stage": cancellation_stage,
        "envelope_status": envelope["status"],
        "provider_posts": provider_posts,
        "notify_failures": request.tick_metrics.get("notify_failures"),
    }
    if cancellation_stage == "before_io":
        assert send_calls == []
        assert provider_posts == []
        assert not envelope.get("last_attempt_at_utc")
    else:
        assert len(provider_posts) == 1


def test_d3_final_attempt_cancel_does_not_send_meta_but_keeps_local_incident(
    monkeypatch, tmp_path,
):
    stopped = threading.Event()
    calls = []
    external_meta_attempts = []

    def send(**kwargs):
        calls.append(kwargs["idempotency_key"])
        if len(calls) == 2:
            stopped.set()
            check_fetch_control(kwargs["deadline"], kwargs["cancelled"])
        return dict(EXPLICIT_FAILURE)

    monkeypatch.setattr(flow, "select_notification_delivery_adapter", lambda _provider: _fake_adapter(send))

    def meta_transport(*_args, **kwargs):
        check_fetch_control(kwargs.get("deadline"), kwargs.get("cancelled"))
        external_meta_attempts.append({
            "after_cancel": stopped.is_set(),
            "control_kwargs": sorted(kwargs),
        })
        return {
            "attempted": True,
            "delivery_confirmed": True,
            "provider": "offline",
            "fallback_used": False,
        }

    monkeypatch.setattr(system_alerts, "_send", meta_transport)
    bundle = shared._request(
        tmp_path,
        run_id="delivery-draft-d3",
        accounts=("lx",),
        fixed=True,
        delivery_only=False,
        pipeline_ok=True,
        trigger_kind="scheduled",
    )
    request = replace(bundle.request, cancelled=stopped.is_set, deadline=monotonic() + 30)
    _run_allowing_cancel(request)
    incident_path = tmp_path / "output_shared/state/system_alerts.json"
    assert stopped.is_set()
    assert len(calls) == 2
    assert incident_path.is_file(), "cancellation must retain local incident evidence"
    incidents = json.loads(incident_path.read_text())
    assert any(
        row.get("account") == "lx"
        and row.get("failure_code") == "NOTIFICATION_DELIVERY_UNCONFIRMED"
        and row.get("status") == "failed"
        and row.get("delivery_confirmed") is False
        for row in incidents.values()
    ), incidents
    assert external_meta_attempts == [], external_meta_attempts


@pytest.mark.parametrize("outcome", ["confirmed", "context_rejected"])
def test_wechat_completed_response_survives_cancel_and_fallback_does_not_start(
    monkeypatch, tmp_path, outcome,
):
    state_dir = _wechat_state(tmp_path)
    stopped = threading.Event()
    posts = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            stopped.set()
            body = ({"ret": 0, "data": {"message_id": "wechat-confirmed"}}
                    if outcome == "confirmed" else {"data": {"ret": -2}})
            return json.dumps(body).encode()

    def urlopen(request, **_kwargs):
        posts.append(request)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    adapter = adapters.select_notification_delivery_adapter("wechat_clawbot")
    arguments = dict(
        base=tmp_path,
        channel="wechat_clawbot",
        target="ops",
        message="completed WeChat message",
        notifications={"wechat_clawbot_state_dir": str(state_dir)},
        idempotency_key="wechat-cancellation-frozen-key",
        deadline=monotonic() + 30,
        cancelled=stopped.is_set,
    )
    if outcome == "context_rejected":
        with pytest.raises(TimeoutError, match="cancelled"):
            adapter.send_fn(**arguments)
    else:
        result = adapters.normalize_notification_delivery_result(
            adapter.send_fn(**arguments), normalize_fn=adapter.normalize_fn,
        )
        assert result["delivery_confirmed"] is True
        assert result["command_ok"] is True
        assert result["message_id"] == "wechat-confirmed"
    assert stopped.is_set()
    assert len(posts) == 1, "cancelled context fallback must not start a second POST"


@pytest.mark.parametrize("route_kind", ["legacy", "fanout"])
@pytest.mark.parametrize("recovery", [False, True], ids=["failure", "recovery"])
def test_health_meta_expired_deadline_keeps_local_transition_without_io(
    monkeypatch, tmp_path, route_kind, recovery,
):
    requests = []

    def forbidden_urlopen(request, **_kwargs):
        requests.append(request.full_url)
        raise AssertionError("expired health delivery must not reach native I/O")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_urlopen)
    config = shared._feishu_config() if route_kind == "legacy" else fanout_config()
    fields = dict(
        base=tmp_path,
        config=config,
        unit="options-monitor-tick-us.service",
        market="us",
        account="lx",
        failure_code="NOTIFICATION_DELIVERY_UNCONFIRMED",
        stage="delivery",
        run_id=f"expired-health-{route_kind}-{recovery}",
        reason="offline expired budget",
    )
    if recovery:
        assert system_alerts.report_system_meta_signal(
            **fields, degraded=True, external=False,
        ) == "signaled"
    result = system_alerts.report_system_meta_signal(
        **fields,
        degraded=not recovery,
        external=True,
        deadline=monotonic() - 1,
        cancelled=lambda: False,
    )
    assert result == ("recovered" if recovery else "signaled")
    incidents = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    incident, = incidents.values()
    assert incident["status"] == ("recovered" if recovery else "failed")
    assert incident["delivery_confirmed"] is False
    assert incident["attempted"] is False
    assert requests == []


def test_health_fanout_cancel_keeps_first_confirmation_and_does_not_start_next_child(
    monkeypatch, tmp_path,
):
    stopped = threading.Event()
    message_posts = []

    class Response:
        status = 200

        def __init__(self, auth):
            self.auth = auth

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            if self.auth:
                return json.dumps({
                    "code": 0, "tenant_access_token": "offline-token", "expire": 7200,
                }).encode()
            stopped.set()
            return json.dumps({
                "code": 0, "data": {"message_id": "confirmed-first-health-recipient"},
            }).encode()

    def urlopen(request, **_kwargs):
        auth = "/auth/" in request.full_url
        if not auth:
            message_posts.append(request.full_url)
        return Response(auth)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert system_alerts.report_system_meta_signal(
        base=tmp_path,
        config=fanout_config(),
        unit="options-monitor-tick-us.service",
        market="us",
        account="lx",
        failure_code="NOTIFICATION_DELIVERY_UNCONFIRMED",
        stage="delivery",
        run_id="health-fanout-cancel-between-children",
        degraded=True,
        external=True,
        deadline=monotonic() + 30,
        cancelled=stopped.is_set,
    ) == "signaled"
    assert stopped.is_set()
    assert len(message_posts) == 1
    fanouts = list_notification_fanouts(
        base=tmp_path, account="lx", logical_key_prefix="system-alert:",
    )
    fanout, = fanouts
    assert len(fanout["children"]) == 2
    assert [child["status"] for child in fanout["children"]] == ["confirmed", "pending"]
    assert [child["attempt_count"] for child in fanout["children"]] == [1, 0]
    incident, = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text()).values()
    assert incident["status"] == "failed"
    assert incident["delivery_confirmed"] is False
