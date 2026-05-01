from __future__ import annotations

from types import SimpleNamespace


def test_build_scheduler_decision_supports_injected_contracts() -> None:
    from src.application.scheduled_notification import build_scheduler_decision

    seen: dict[str, object] = {}

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            seen.setdefault("snapshots", []).append(payload)
            return SimpleNamespace(payload=dict(payload["payload"]))

    class FakeDecision:
        @classmethod
        def from_payload(cls, payload):
            seen["decision_payload"] = payload
            return payload

    def _resolve_scheduler(payload):
        seen["scheduler_payload"] = payload
        return {"ok": True}, SimpleNamespace(should_run_scan=True, reason="ok")

    def _resolve_notify_window(*, scheduler_decision):
        seen["scheduler_view"] = scheduler_decision
        return False

    out = build_scheduler_decision(
        scheduler_stdout='{"should_run_scan": true, "reason": "ok"}',
        cfg_obj={"portfolio": {"account": "lx"}},
        as_of_utc="2026-04-24T00:00:00Z",
        snapshot_cls=FakeSnapshot,
        decision_cls=FakeDecision,
        scheduler_resolver=_resolve_scheduler,
        notify_window_resolver=_resolve_notify_window,
    )

    assert out["account"] == "lx"
    assert out["should_run"] is True
    assert out["should_notify"] is False
    assert seen["scheduler_payload"] == {"should_run_scan": True, "reason": "ok"}


def test_build_per_account_delivery_batch_supports_single_account_rendering() -> None:
    from src.application.scheduled_notification import build_per_account_delivery_batch

    seen: dict[str, object] = {}

    class FakeDeliveryPlan:
        @classmethod
        def from_payload(cls, payload):
            seen["delivery_payload"] = payload
            return payload

    def _build_decision(**kwargs):
        seen["decision_kwargs"] = kwargs
        return {
            "should_send": True,
            "meaningful": True,
            "config_error": None,
            "effective_target": "user:test",
            "reason": "send",
        }

    decision, batch, target = build_per_account_delivery_batch(
        channel="feishu",
        target="user:test",
        account_messages={"sy": "[sy]\nhello"},
        should_notify_window=True,
        decision_builder=_build_decision,
        delivery_plan_cls=FakeDeliveryPlan,
    )

    assert decision["should_send"] is True
    assert batch is not None
    assert batch.messages_by_account == {"sy": "[sy]\nhello"}
    assert batch.mode == "per_account"
    assert target == "user:test"
    assert seen["decision_kwargs"]["notification_text"] == "[sy]\nhello"


def test_infer_trading_day_guard_markets_supports_injected_resolver() -> None:
    from src.application.scheduled_notification import infer_trading_day_guard_markets

    seen: dict[str, object] = {}

    def _resolver(markets_to_run, cfg, market_config):
        seen["markets_to_run"] = markets_to_run
        seen["cfg"] = cfg
        seen["market_config"] = market_config
        return ["US", "HK"]

    cfg = {"symbols": []}
    out = infer_trading_day_guard_markets(cfg, resolver=_resolver)
    assert out == ["US", "HK"]
    assert seen["markets_to_run"] == []
    assert seen["cfg"] is cfg
    assert seen["market_config"] == "auto"


def test_build_per_account_delivery_batch_supports_skip_paths() -> None:
    from src.application.scheduled_notification import build_per_account_delivery_batch

    def _decision_builder(**kwargs):
        assert kwargs["notification_text"] == "hello\nworld"
        return {
            "should_send": False,
            "meaningful": True,
            "config_error": None,
            "effective_target": None,
            "reason": "no_send",
            "action": "skip",
        }

    decision, batch, target = build_per_account_delivery_batch(
        channel="feishu",
        target="user:test",
        account_messages={"lx": "hello", "sy": "world"},
        no_send=True,
        decision_builder=_decision_builder,
    )

    assert decision["should_send"] is False
    assert batch is None
    assert target is None


def test_build_per_account_delivery_batch_builds_delivery_batch() -> None:
    from src.application.scheduled_notification import build_per_account_delivery_batch

    class FakeDeliveryPlan:
        @classmethod
        def from_payload(cls, payload):
            return payload

    decision, batch, target = build_per_account_delivery_batch(
        channel="feishu",
        target="user:test",
        account_messages={"lx": "hello"},
        delivery_plan_cls=FakeDeliveryPlan,
        decision_builder=lambda **_kwargs: {
            "should_send": True,
            "meaningful": True,
            "config_error": None,
            "effective_target": "user:test",
            "reason": "send",
            "action": "send",
        },
    )

    assert decision["should_send"] is True
    assert target == "user:test"
    assert batch is not None
    assert batch.target == "user:test"
    assert batch.messages_by_account == {"lx": "hello"}


def test_build_single_account_messages_prefixes_account_header() -> None:
    from src.application.scheduled_notification import build_single_account_messages

    assert build_single_account_messages(account="lx", notification_text="hello") == {
        "lx": "[lx]\nhello"
    }
    assert build_single_account_messages(account="", notification_text="") == {
        "default": ""
    }


def test_snapshot_account_messages_normalizes_mapping() -> None:
    from src.application.scheduled_notification import snapshot_account_messages

    seen: dict[str, object] = {}

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            seen["payload"] = payload
            return SimpleNamespace(payload={"account_messages": {"lx": "hello"}})

    out = snapshot_account_messages(
        account_messages={"lx": "hello"},
        as_of_utc="2026-04-25T00:00:00Z",
        snapshot_cls=FakeSnapshot,
    )

    assert out == {"lx": "hello"}
    assert seen["payload"]["snapshot_name"] == "account_messages"


def test_prepare_per_account_messages_keeps_candidate_messages_when_threshold_met() -> None:
    from src.application.scheduled_notification import prepare_per_account_messages

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            return SimpleNamespace(payload=dict(payload["payload"]))

    out = prepare_per_account_messages(
        notify_candidates=["candidate-a"],
        results=["result-a"],
        now_bj="BJ_NOW",
        cash_footer_lines=["cash"],
        cash_footer_for_account_fn=lambda lines, account: [f"{account}:{len(lines)}"],
        build_account_message_fn=lambda *args, **kwargs: "unused",
        build_account_messages_fn=lambda **kwargs: {"lx": "hello"},
        build_no_candidate_account_messages_fn=lambda **kwargs: {"lx": "heartbeat"},
        as_of_utc="2026-04-25T00:00:00Z",
        snapshot_cls=FakeSnapshot,
        engine_entrypoint=lambda **kwargs: {"notify_threshold": {"threshold_met": True}},
    )

    assert out.messages_by_account == {"lx": "hello"}
    assert out.account_messages == {"lx": "hello"}
    assert out.threshold_met is True
    assert out.used_heartbeat is False


def test_prepare_per_account_messages_falls_back_to_heartbeat() -> None:
    from src.application.scheduled_notification import prepare_per_account_messages

    calls = {"n": 0}

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            return SimpleNamespace(payload=dict(payload["payload"]))

    def _engine(**kwargs):
        calls["n"] += 1
        threshold_met = calls["n"] == 2
        return {"notify_threshold": {"threshold_met": threshold_met}}

    out = prepare_per_account_messages(
        notify_candidates=[],
        results=["result-a"],
        now_bj="BJ_NOW",
        cash_footer_lines=["cash"],
        cash_footer_for_account_fn=lambda lines, account: [f"{account}:{len(lines)}"],
        build_account_message_fn=lambda *args, **kwargs: "unused",
        build_account_messages_fn=lambda **kwargs: {},
        build_no_candidate_account_messages_fn=lambda **kwargs: {"lx": "heartbeat"},
        as_of_utc="2026-04-25T00:00:00Z",
        snapshot_cls=FakeSnapshot,
        engine_entrypoint=_engine,
    )

    assert out.messages_by_account == {"lx": "heartbeat"}
    assert out.threshold_met is True
    assert out.used_heartbeat is True


def test_prepare_single_account_delivery_builds_messages_and_delivery() -> None:
    from src.application.scheduled_notification import prepare_single_account_delivery

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            return SimpleNamespace(payload=dict(payload["payload"]))

    class FakeDeliveryPlan:
        @classmethod
        def from_payload(cls, payload):
            return payload

    out = prepare_single_account_delivery(
        account="lx",
        notification_text="hello",
        channel="feishu",
        target="user:test",
        should_notify_window=True,
        as_of_utc="2026-04-25T00:00:00Z",
        snapshot_cls=FakeSnapshot,
        decision_builder=lambda **_kwargs: {
            "should_send": True,
            "meaningful": True,
            "config_error": None,
            "effective_target": "user:test",
            "reason": "send",
        },
        delivery_plan_cls=FakeDeliveryPlan,
    )

    assert out.account_name == "lx"
    assert out.account_messages == {"lx": "[lx]\nhello"}
    assert out.delivery_decision["should_send"] is True
    assert out.delivery_plan["account_messages"] == {"lx": "[lx]\nhello"}
    assert out.effective_target == "user:test"


def test_execute_single_account_pipeline_uses_normalized_returncode_when_present() -> None:
    from src.application.scheduled_notification import execute_single_account_pipeline

    seen: dict[str, object] = {}

    def _run_pipeline(**kwargs):
        seen["run_kwargs"] = kwargs
        return SimpleNamespace(returncode=9, stdout="raw-out", stderr="raw-err")

    def _normalize_pipeline_output(**kwargs):
        seen["normalize_kwargs"] = kwargs
        return {"returncode": 5, "ok": False, "adapter": "pipeline"}

    out = execute_single_account_pipeline(
        run_pipeline=_run_pipeline,
        normalize_pipeline_output=_normalize_pipeline_output,
        vpy="python",
        base="/repo",
        config="config.us.json",
        report_dir="report-dir",
        state_dir="state-dir",
    )

    assert out.returncode == 5
    assert out.payload["adapter"] == "pipeline"
    assert seen["normalize_kwargs"] == {
        "returncode": 9,
        "stdout": "raw-out",
        "stderr": "raw-err",
    }


def test_execute_single_account_pipeline_falls_back_to_process_returncode_when_normalized_missing() -> None:
    from src.application.scheduled_notification import execute_single_account_pipeline

    out = execute_single_account_pipeline(
        run_pipeline=lambda **kwargs: SimpleNamespace(returncode=7, stdout="", stderr=""),
        normalize_pipeline_output=lambda **kwargs: {"ok": False},
        vpy="python",
        base="/repo",
        config="config.us.json",
        report_dir="report-dir",
        state_dir="state-dir",
    )

    assert out.returncode == 7
    assert out.payload == {"ok": False}


def test_execute_single_account_delivery_reports_send_failed_when_command_fails() -> None:
    from src.application.scheduled_notification import execute_single_account_delivery

    delivery_plan = SimpleNamespace(
        channel="feishu",
        target="user:test",
        account_messages={"lx": "[lx]\nhello"},
    )

    out = execute_single_account_delivery(
        delivery_plan=delivery_plan,
        account_name="lx",
        send_message=lambda **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="send failed"),
        normalize_notify_output=lambda **kwargs: {"ok": False, "command_ok": False, "message": "boom", "returncode": 2},
        mark_scheduler_notified=lambda: SimpleNamespace(returncode=0),
        base="/repo",
    )

    assert out.ok is False
    assert out.error_code == "SEND_FAILED"
    assert out.details == "boom"
    assert out.returncode == 2
    assert out.message_id is None


def test_execute_single_account_delivery_reports_unconfirmed_when_message_id_missing() -> None:
    from src.application.scheduled_notification import execute_single_account_delivery

    delivery_plan = SimpleNamespace(
        channel="feishu",
        target="user:test",
        account_messages={"lx": "[lx]\nhello"},
    )

    out = execute_single_account_delivery(
        delivery_plan=delivery_plan,
        account_name="lx",
        send_message=lambda **kwargs: SimpleNamespace(returncode=0, stdout="sent", stderr=""),
        normalize_notify_output=lambda **kwargs: {"ok": False, "command_ok": True, "message": "missing message id", "returncode": 0},
        mark_scheduler_notified=lambda: SimpleNamespace(returncode=0),
        base="/repo",
    )

    assert out.ok is False
    assert out.error_code == "SEND_UNCONFIRMED"
    assert out.details == "missing message id"
    assert out.returncode == 1


def test_execute_single_account_delivery_fails_when_mark_notified_fails() -> None:
    from src.application.scheduled_notification import execute_single_account_delivery

    delivery_plan = SimpleNamespace(
        channel="feishu",
        target="user:test",
        account_messages={"lx": "[lx]\nhello"},
    )

    out = execute_single_account_delivery(
        delivery_plan=delivery_plan,
        account_name="lx",
        send_message=lambda **kwargs: SimpleNamespace(returncode=0, stdout="sent", stderr=""),
        normalize_notify_output=lambda **kwargs: {"ok": True, "message_id": "msg-1", "returncode": 0},
        mark_scheduler_notified=lambda: SimpleNamespace(returncode=9),
        base="/repo",
    )

    assert out.ok is False
    assert out.error_code == "MARK_NOTIFIED_FAILED"
    assert out.details == "send ok but mark-notified failed"
    assert out.returncode == 9
    assert out.message_id == "msg-1"


def test_execute_single_account_delivery_treats_missing_message_id_as_unconfirmed_even_when_send_reports_ok() -> None:
    from src.application.scheduled_notification import execute_single_account_delivery

    delivery_plan = SimpleNamespace(
        channel="feishu",
        target="user:test",
        account_messages={"lx": "[lx]\nhello"},
    )

    out = execute_single_account_delivery(
        delivery_plan=delivery_plan,
        account_name="lx",
        send_message=lambda **kwargs: SimpleNamespace(returncode=0, stdout="sent", stderr=""),
        normalize_notify_output=lambda **kwargs: {"ok": True, "command_ok": True, "message": "message_id is missing", "returncode": 0},
        mark_scheduler_notified=lambda: SimpleNamespace(returncode=0),
        base="/repo",
    )

    assert out.ok is False
    assert out.error_code == "SEND_UNCONFIRMED"
    assert out.details == "message_id is missing"
    assert out.returncode == 1
    assert out.message_id is None


def test_execute_single_account_delivery_supports_raw_sender_normalizer() -> None:
    from src.application.scheduled_notification import execute_single_account_delivery

    delivery_plan = SimpleNamespace(
        channel="feishu",
        target="ou_test",
        account_messages={"lx": "[lx]\nhello"},
    )

    out = execute_single_account_delivery(
        delivery_plan=delivery_plan,
        account_name="lx",
        send_message=lambda **kwargs: SimpleNamespace(returncode=0, stdout="", stderr="", raw={"http_status": 200, "response_json": {"code": 0, "data": {"message_id": "msg-9"}}}),
        normalize_notify_output=lambda *, send_result: {"ok": True, "message_id": "msg-9", "returncode": 0},
        mark_scheduler_notified=lambda: SimpleNamespace(returncode=0),
        base="/repo",
    )

    assert out.ok is True
    assert out.error_code is None
    assert out.message_id == "msg-9"
