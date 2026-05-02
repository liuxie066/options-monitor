from __future__ import annotations

from types import SimpleNamespace


def test_build_per_account_delivery_batch_supports_one_account_message() -> None:
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


def test_prepare_multi_account_notification_collects_candidates_and_cash_footer() -> None:
    from src.application.scheduled_notification import prepare_multi_account_notification

    seen: dict[str, object] = {}

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            return SimpleNamespace(payload=dict(payload["payload"]))

    def _filter(results):
        seen["filter_results"] = results
        return ["candidate-b", "candidate-a"]

    def _rank(candidates):
        seen["rank_candidates"] = candidates
        return ["candidate-a", "candidate-b"]

    def _query_cash_footer(base, *, config_path, market, accounts, timeout_sec, snapshot_max_age_sec):
        seen["cash_footer"] = {
            "base": base,
            "config_path": config_path,
            "market": market,
            "accounts": accounts,
            "timeout_sec": timeout_sec,
            "snapshot_max_age_sec": snapshot_max_age_sec,
        }
        return ["cash-line"]

    def _build_account_messages(**kwargs):
        seen["message_kwargs"] = kwargs
        return {"lx": "candidate-message"}

    out = prepare_multi_account_notification(
        results=["raw-a", "raw-b"],
        base="/repo",
        config_path="/repo/config.us.json",
        config={
            "accounts": ["lx"],
            "portfolio": {"broker": "富途"},
            "notifications": {
                "cash_footer_timeout_sec": 12,
                "cash_snapshot_max_age_sec": 34,
            },
        },
        now_bj="BJ_NOW",
        as_of_utc="2026-04-25T00:00:00Z",
        filter_notify_candidates_fn=_filter,
        rank_notify_candidates_fn=_rank,
        query_cash_footer_fn=_query_cash_footer,
        cash_footer_accounts_from_config_fn=lambda cfg: list(cfg["accounts"]),
        cash_footer_for_account_fn=lambda lines, account: [f"{account}:{len(lines)}"],
        build_account_message_fn=lambda *args, **kwargs: "unused",
        build_account_messages_fn=_build_account_messages,
        build_no_candidate_account_messages_fn=lambda **kwargs: {"lx": "heartbeat"},
        snapshot_cls=FakeSnapshot,
        engine_entrypoint=lambda **kwargs: {"notify_threshold": {"threshold_met": True}},
    )

    assert out.results_count == 2
    assert out.notify_candidates == ["candidate-a", "candidate-b"]
    assert out.cash_footer_lines == ["cash-line"]
    assert out.messages_by_account == {"lx": "candidate-message"}
    assert out.threshold_met is True
    assert out.used_heartbeat is False
    assert seen["filter_results"] == ["raw-a", "raw-b"]
    assert seen["rank_candidates"] == ["candidate-b", "candidate-a"]
    assert seen["cash_footer"] == {
        "base": "/repo",
        "config_path": "/repo/config.us.json",
        "market": "富途",
        "accounts": ["lx"],
        "timeout_sec": 12,
        "snapshot_max_age_sec": 34,
    }
    assert seen["message_kwargs"]["notify_candidates"] == ["candidate-a", "candidate-b"]
    assert seen["message_kwargs"]["cash_footer_lines"] == ["cash-line"]


def test_mark_no_candidate_notification_metrics_updates_matching_accounts_only() -> None:
    from src.application.scheduled_notification import mark_no_candidate_notification_metrics

    tick_metrics = {
        "accounts": [
            {"account": "lx", "meaningful": False},
            {"account": "sy", "meaningful": False},
            {"account": "other", "meaningful": False},
            "invalid",
        ]
    }

    mark_no_candidate_notification_metrics(
        tick_metrics=tick_metrics,
        account_messages={"LX": "heartbeat", "sy": "heartbeat"},
    )

    assert tick_metrics["accounts"][0]["meaningful"] is True
    assert tick_metrics["accounts"][0]["notification_type"] == "no_candidate"
    assert tick_metrics["accounts"][1]["meaningful"] is True
    assert tick_metrics["accounts"][1]["notification_type"] == "no_candidate"
    assert tick_metrics["accounts"][2] == {"account": "other", "meaningful": False}
