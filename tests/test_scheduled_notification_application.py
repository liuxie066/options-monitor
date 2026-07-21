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
        channel="wechat_clawbot",
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
        channel="wechat_clawbot",
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
        channel="wechat_clawbot",
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
    assert out.heartbeat_accounts == ()


def test_prepare_per_account_messages_adds_heartbeat_for_missing_accounts_when_candidates_exist() -> None:
    from src.application.scheduled_notification import prepare_per_account_messages

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            return SimpleNamespace(payload=dict(payload["payload"]))

    out = prepare_per_account_messages(
        notify_candidates=["candidate-a"],
        results=["result-a", "result-b"],
        now_bj="BJ_NOW",
        cash_footer_lines=["cash"],
        cash_footer_for_account_fn=lambda lines, account: [f"{account}:{len(lines)}"],
        build_account_message_fn=lambda *args, **kwargs: "unused",
        build_account_messages_fn=lambda **kwargs: {"lx": "candidate"},
        build_no_candidate_account_messages_fn=lambda **kwargs: {
            "lx": "lx-heartbeat",
            "sy": "sy-heartbeat",
        },
        as_of_utc="2026-04-25T00:00:00Z",
        snapshot_cls=FakeSnapshot,
        engine_entrypoint=lambda **kwargs: {"notify_threshold": {"threshold_met": True}},
    )

    assert out.messages_by_account == {"lx": "candidate", "sy": "sy-heartbeat"}
    assert out.threshold_met is True
    assert out.used_heartbeat is True
    assert out.heartbeat_accounts == ("sy",)


def test_prepare_per_account_messages_adds_heartbeat_for_lx_when_sy_has_candidates() -> None:
    from src.application.scheduled_notification import prepare_per_account_messages

    class FakeSnapshot:
        @classmethod
        def from_payload(cls, payload):
            return SimpleNamespace(payload=dict(payload["payload"]))

    out = prepare_per_account_messages(
        notify_candidates=["candidate-a"],
        results=["result-a", "result-b"],
        now_bj="BJ_NOW",
        cash_footer_lines=["cash"],
        cash_footer_for_account_fn=lambda lines, account: [f"{account}:{len(lines)}"],
        build_account_message_fn=lambda *args, **kwargs: "unused",
        build_account_messages_fn=lambda **kwargs: {"sy": "candidate"},
        build_no_candidate_account_messages_fn=lambda **kwargs: {
            "lx": "lx-heartbeat",
            "sy": "sy-heartbeat",
        },
        as_of_utc="2026-04-25T00:00:00Z",
        snapshot_cls=FakeSnapshot,
        engine_entrypoint=lambda **kwargs: {"notify_threshold": {"threshold_met": True}},
    )

    assert out.messages_by_account == {"sy": "candidate", "lx": "lx-heartbeat"}
    assert out.threshold_met is True
    assert out.used_heartbeat is True
    assert out.heartbeat_accounts == ("lx",)


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
    assert out.heartbeat_accounts == ("lx",)


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


def test_build_notify_summary_records_run_level_delivery_counts() -> None:
    from src.application.cron_runtime import apply_notify_results_to_tick_metrics, build_notify_summary

    summary = build_notify_summary(
        sent_accounts=["lx"],
        notify_failures=[{"account": "sy", "error_code": "SEND_TIMEOUT", "ambiguous_send": True, "duplicate_risk": True}],
        total_accounts=2,
        send_attempted_count=2,
        send_confirmed_count=1,
        retry_attempt_count=1,
        ambiguous_send_count=1,
        duplicate_risk_count=1,
    )
    tick_metrics: dict[str, object] = {}

    apply_notify_results_to_tick_metrics(
        tick_metrics=tick_metrics,
        no_send=False,
        sent_accounts=["lx"],
        notify_failures=[{"account": "sy", "error_code": "SEND_TIMEOUT", "ambiguous_send": True, "duplicate_risk": True}],
        notify_summary=summary,
    )

    assert summary["account_messages_count"] == 2
    assert summary["send_attempted_count"] == 2
    assert summary["send_confirmed_count"] == 1
    assert summary["retry_attempt_count"] == 1
    assert summary["ambiguous_send_count"] == 1
    assert summary["duplicate_risk_count"] == 1
    assert tick_metrics["account_messages_count"] == 2
    assert tick_metrics["send_attempted_count"] == 2
    assert tick_metrics["send_confirmed_count"] == 1
    assert tick_metrics["retry_attempt_count"] == 1
    assert tick_metrics["ambiguous_send_count"] == 1
    assert tick_metrics["duplicate_risk_count"] == 1
    assert tick_metrics["reason"] == "sent_partial_notify_failure"


def test_shared_last_run_meta_marks_no_send_as_not_sent() -> None:
    from types import SimpleNamespace
    from src.application.cron_runtime import build_shared_last_run_meta

    meta = build_shared_last_run_meta(
        now_utc="2026-05-12T00:00:00Z",
        channel="wechat_clawbot",
        target="group://test",
        results=[SimpleNamespace(account="lx")],
        sent_accounts=["lx"],
        notify_failures=[],
        notify_summary={"success_count": 1},
        no_send=True,
    )

    assert meta["sent"] is False
    assert meta["no_send"] is True
    assert meta["sent_accounts"] == []
    assert meta["would_send_accounts"] == ["lx"]


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


def test_local_feishu_size_failure_is_audited_and_never_retried() -> None:
    from src.application.scheduled_notification import (
        build_notify_failure_summary_message,
        send_account_message_with_retry,
    )

    send_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []
    audit_events: list[dict[str, object]] = []
    runlog_events: list[dict[str, object]] = []
    diagnostics = {
        "local_error_code": "FEISHU_POST_TOO_LARGE",
        "error_code": "FEISHU_POST_TOO_LARGE",
        "request_body_bytes": 28673,
        "request_body_budget_bytes": 28672,
        "normalized_markdown_chars": 12000,
        "normalized_markdown_sha256": "a" * 64,
    }

    class FakeRunlog:
        def safe_event(self, step: str, status: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
            runlog_events.append({"step": step, "status": status, **kwargs})

    def _send_fn(**kwargs):  # type: ignore[no-untyped-def]
        send_calls.append(dict(kwargs))
        return {
            "ok": False,
            "returncode": 1,
            "command_ok": False,
            "delivery_confirmed": False,
            "message_id": None,
            "http_attempts": [],
            "retry_attempt_count": 0,
            "ambiguous_send": False,
            "duplicate_risk": False,
            **diagnostics,
        }

    result = send_account_message_with_retry(
        base="/tmp/base",
        channel="feishu_app",
        target="",
        account="lx",
        message="# 简报",
        run_id="run-size-failure",
        runlog=FakeRunlog(),
        audit_fn=lambda kind, action, **kwargs: audit_events.append(
            {"kind": kind, "action": action, **kwargs}
        ),
        send_fn=_send_fn,
        normalize_fn=lambda **_kwargs: {},
        safe_data_fn=lambda payload: payload,
        failure_fields_builder=lambda **_kwargs: {},
        max_attempts=3,
        sleep_fn=lambda seconds: sleep_calls.append(seconds),
    )

    assert len(send_calls) == 1
    assert sleep_calls == []
    assert result["ok"] is False
    assert result["attempts"] == 1
    assert result["error_code"] == "FEISHU_POST_TOO_LARGE"
    assert result["local_error_code"] == "FEISHU_POST_TOO_LARGE"
    assert result["ambiguous_send"] is False
    assert result["duplicate_risk"] is False
    final = result["final"]
    assert isinstance(final, dict)
    assert final["will_retry"] is False
    assert final["http_attempts"] == []
    for key, value in diagnostics.items():
        assert final[key] == value
        if key != "error_code":
            assert result[key] == value

    send_fail = next(event for event in audit_events if event["action"] == "send_fail")
    assert send_fail["error_code"] == "FEISHU_POST_TOO_LARGE"
    assert isinstance(send_fail["extra"], dict)
    for key, value in diagnostics.items():
        assert send_fail["extra"][key] == value

    error_event = next(event for event in runlog_events if event["status"] == "error")
    assert error_event["error_code"] == "FEISHU_POST_TOO_LARGE"
    assert error_event["data"]["will_retry"] is False
    assert error_event["data"]["request_body_bytes"] == 28673

    summary = build_notify_failure_summary_message(
        run_id="run-size-failure",
        sent_accounts=[],
        notify_failures=[
            {
                "account": "lx",
                "error_code": result["error_code"],
                "attempts": result["attempts"],
                "delivery_confirmed": result["delivery_confirmed"],
            }
        ],
    )
    assert "lx: FEISHU_POST_TOO_LARGE attempts=1 confirmed=False" in summary
