from __future__ import annotations

import importlib
from types import SimpleNamespace


def _plan(account_messages: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(channel="feishu", target="group://test", account_messages=account_messages)


def test_execute_per_account_delivery_collects_mixed_success_and_unconfirmed(fake_runlog_factory) -> None:
    mod = importlib.import_module("src.application.scheduled_notification")
    normalize = importlib.import_module("domain.domain").normalize_notify_subprocess_output
    events: list[dict] = []
    audit_events: list[dict] = []
    failure_codes: list[str] = []

    def _send_fn(*, message: str, **_kwargs):
        if message == "msg-lx":
            return SimpleNamespace(returncode=0, stdout='{"result":{"messageId":"lx-1"}}', stderr="")
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    out = mod.execute_per_account_delivery(
        delivery_batch=_plan({"lx": "msg-lx", "sy": "msg-sy"}),
        run_id="run-1",
        runlog=fake_runlog_factory(events),
        audit_fn=lambda kind, action, **kwargs: audit_events.append({"kind": kind, "action": action, **kwargs}),
        safe_data_fn=lambda payload: payload,
        send_fn=_send_fn,
        normalize_fn=normalize,
        failure_fields_builder=lambda **_kwargs: {},
        on_failure=lambda error_code: failure_codes.append(error_code),
        base="/tmp/base",
    )

    assert out.sent_accounts == ["lx"]
    assert len(out.notify_failures) == 1
    assert out.notify_failures[0]["account"] == "sy"
    assert out.notify_failures[0]["error_code"] == "SEND_UNCONFIRMED"
    assert out.notify_failures[0]["final_returncode"] == 0
    assert out.notify_failures[0]["message_id"] is None
    assert out.notify_failures[0]["command_ok"] is True
    assert out.notify_failures[0]["delivery_confirmed"] is False
    assert failure_codes == ["SEND_UNCONFIRMED"]
    assert [e["status"] for e in audit_events if e["action"] == "send_openclaw_message"] == ["ok", "unconfirmed"]
    assert [e["status"] for e in events if e["step"] == "notify"] == ["start", "ok", "start", "error"]


def test_execute_per_account_delivery_collects_all_failures(fake_runlog_factory) -> None:
    mod = importlib.import_module("src.application.scheduled_notification")
    normalize = importlib.import_module("domain.domain").normalize_notify_subprocess_output
    failure_codes: list[str] = []

    out = mod.execute_per_account_delivery(
        delivery_batch=_plan({"lx": "msg-lx", "sy": "msg-sy"}),
        run_id="run-2",
        runlog=fake_runlog_factory([]),
        audit_fn=lambda *_args, **_kwargs: None,
        safe_data_fn=lambda payload: payload,
        send_fn=lambda **_kwargs: SimpleNamespace(returncode=2, stdout="", stderr="boom"),
        normalize_fn=normalize,
        failure_fields_builder=lambda **_kwargs: {},
        on_failure=lambda error_code: failure_codes.append(error_code),
        base="/tmp/base",
    )

    assert out.sent_accounts == []
    assert len(out.notify_failures) == 2
    assert [item["account"] for item in out.notify_failures] == ["lx", "sy"]
    assert [item["error_code"] for item in out.notify_failures] == ["SEND_FAILED", "SEND_FAILED"]
    assert [item["final_returncode"] for item in out.notify_failures] == [2, 2]
    assert [item["command_ok"] for item in out.notify_failures] == [False, False]
    assert [item["delivery_confirmed"] for item in out.notify_failures] == [False, False]
    assert failure_codes == ["SEND_FAILED", "SEND_FAILED"]


def test_execute_per_account_delivery_preserves_success_order(fake_runlog_factory) -> None:
    mod = importlib.import_module("src.application.scheduled_notification")
    normalize = importlib.import_module("domain.domain").normalize_notify_subprocess_output

    def _send_fn(*, message: str, **_kwargs):
        suffix = message.split("-")[-1]
        return SimpleNamespace(returncode=0, stdout=f'{{"messageId":"{suffix}"}}', stderr="")

    out = mod.execute_per_account_delivery(
        delivery_batch=_plan({"lx": "msg-lx", "sy": "msg-sy"}),
        run_id="run-3",
        runlog=fake_runlog_factory([]),
        audit_fn=lambda *_args, **_kwargs: None,
        safe_data_fn=lambda payload: payload,
        send_fn=_send_fn,
        normalize_fn=normalize,
        failure_fields_builder=lambda **_kwargs: {},
        on_failure=lambda _error_code: None,
        base="/tmp/base",
    )

    assert out.sent_accounts == ["lx", "sy"]
    assert out.notify_failures == []


def test_execute_per_account_delivery_sends_one_message_per_account_to_same_target(fake_runlog_factory) -> None:
    mod = importlib.import_module("src.application.scheduled_notification")
    seen_targets: list[str] = []
    seen_messages: list[str] = []

    def _send_fn(*, target: str, message: str, **_kwargs):
        seen_targets.append(target)
        seen_messages.append(message)
        suffix = message.split("-")[-1]
        return SimpleNamespace(returncode=0, stdout="", stderr="", raw={"http_status": 200, "response_json": {"code": 0, "data": {"message_id": suffix}}})

    out = mod.execute_per_account_delivery(
        delivery_batch=SimpleNamespace(channel="feishu", target="ou_same", account_messages={"lx": "msg-lx", "sy": "msg-sy"}),
        run_id="run-4",
        runlog=fake_runlog_factory([]),
        audit_fn=lambda *_args, **_kwargs: None,
        safe_data_fn=lambda payload: payload,
        send_fn=_send_fn,
        normalize_fn=lambda *, send_result: {
            "ok": bool(((send_result.get("response_json") or {}).get("data") or {}).get("message_id")),
            "command_ok": True,
            "delivery_confirmed": True,
            "returncode": 0,
            "message_id": ((send_result.get("response_json") or {}).get("data") or {}).get("message_id"),
        },
        failure_fields_builder=lambda **_kwargs: {},
        on_failure=lambda _error_code: None,
        base="/tmp/base",
    )

    assert out.sent_accounts == ["lx", "sy"]
    assert seen_targets == ["ou_same", "ou_same"]
    assert seen_messages == ["msg-lx", "msg-sy"]
