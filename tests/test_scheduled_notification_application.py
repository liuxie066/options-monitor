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


def test_build_multi_account_delivery_supports_single_account_rendering() -> None:
    from src.application.scheduled_notification import build_multi_account_delivery

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

    decision, plan, target = build_multi_account_delivery(
        channel="feishu",
        target="user:test",
        account_messages={"sy": "[sy]\nhello"},
        should_notify_window=True,
        decision_builder=_build_decision,
        delivery_plan_cls=FakeDeliveryPlan,
    )

    assert decision["should_send"] is True
    assert plan["account_messages"] == {"sy": "[sy]\nhello"}
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


def test_build_multi_account_delivery_supports_skip_paths() -> None:
    from src.application.scheduled_notification import build_multi_account_delivery

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

    decision, plan, target = build_multi_account_delivery(
        channel="feishu",
        target="user:test",
        account_messages={"lx": "hello", "sy": "world"},
        no_send=True,
        decision_builder=_decision_builder,
    )

    assert decision["should_send"] is False
    assert plan is None
    assert target is None


def test_build_multi_account_delivery_builds_delivery_plan() -> None:
    from src.application.scheduled_notification import build_multi_account_delivery

    class FakeDeliveryPlan:
        @classmethod
        def from_payload(cls, payload):
            return payload

    decision, plan, target = build_multi_account_delivery(
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
    assert plan["target"] == "user:test"
    assert plan["account_messages"] == {"lx": "hello"}


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


def test_prepare_multi_account_messages_keeps_candidate_messages_when_threshold_met() -> None:
    from src.application.scheduled_notification import prepare_multi_account_messages

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            return SimpleNamespace(payload=dict(payload["payload"]))

    out = prepare_multi_account_messages(
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

    assert out.account_messages == {"lx": "hello"}
    assert out.threshold_met is True
    assert out.used_heartbeat is False


def test_prepare_multi_account_messages_falls_back_to_heartbeat() -> None:
    from src.application.scheduled_notification import prepare_multi_account_messages

    calls = {"n": 0}

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            return SimpleNamespace(payload=dict(payload["payload"]))

    def _engine(**kwargs):
        calls["n"] += 1
        threshold_met = calls["n"] == 2
        return {"notify_threshold": {"threshold_met": threshold_met}}

    out = prepare_multi_account_messages(
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

    assert out.account_messages == {"lx": "heartbeat"}
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
