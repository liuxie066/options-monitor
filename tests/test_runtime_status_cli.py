from __future__ import annotations

import argparse

from src.application.runtime_status_cli import format_runtime_status_summary
from src.application.agent_tools.runtime_status_impl import (
    _aggregate_trade_intake_summaries,
    _notification_delivery_health,
)
from src.interfaces.cli.observability_ops import handle_observability_command


def test_format_runtime_status_summary_shows_trade_intake_sources() -> None:
    out = format_runtime_status_summary(
        {
            "ok": True,
            "data": {
                "summary": {"ok": True},
                "trade_intake": {
                    "enabled": True,
                    "mode": "apply",
                    "summary": {
                        "listener_status": "listening",
                        "processed_count": 1,
                        "failed_count": 0,
                        "unresolved_count": 0,
                    },
                    "sources": [
                        {
                            "id": "lx",
                            "account": "lx",
                            "host": "127.0.0.1",
                            "port": 11111,
                            "summary": {"listener_status": "listening"},
                        },
                        {
                            "id": "sy",
                            "account": "sy",
                            "host": "127.0.0.1",
                            "port": 11112,
                            "summary": {"listener_status": "listening"},
                        },
                    ],
                },
            },
            "warnings": [],
        }
    )

    assert "trade intake:" in out
    assert "sources=lx:listening@127.0.0.1:11111, sy:listening@127.0.0.1:11112" in out


def test_format_runtime_status_journal_summary_is_bounded_for_large_unicode_warnings() -> None:
    from src.application.runtime_status_cli import format_runtime_status_journal_summary

    out = format_runtime_status_journal_summary(
        {
            "ok": False,
            "data": {
                "summary": {"ok": False, "warning_count": 100},
                "config": {
                    "config_key": "us\nextra",
                    "config_path": "/tmp/config\ninjected-line",
                    "accounts": ["lx", "sy"],
                },
                "ledger_store": {"warnings": ["账本警告\n第二行" * 500]},
            },
            "error": {"code": "FAILED", "message": "错误" * 10000},
            "warnings": [(f"warning-{index}\n" + "警告" * 1000) for index in range(100)],
        }
    )

    assert len(out.splitlines()) <= 20
    assert len(out.encode("utf-8")) <= 16 * 1024
    assert "warnings: count=101" in out
    assert "\n第二行" not in out
    assert "\ninjected-line" not in out


def test_format_runtime_status_journal_summary_keeps_default_summary_unbounded() -> None:
    from src.application.runtime_status_cli import format_runtime_status_journal_summary

    envelope = {
        "ok": True,
        "data": {"summary": {"ok": True}},
        "warnings": ["first", "second"],
    }

    journal = format_runtime_status_journal_summary(envelope)
    default = format_runtime_status_summary(envelope)

    assert "warnings: count=2 first=first" in journal
    assert "- first" in default
    assert "- second" in default


def test_notification_delivery_has_separate_degraded_state_for_unknown_and_receipt() -> None:
    diagnosis = {"status": "unknown", "scheduler_should_notify": True}
    assert _notification_delivery_health(diagnosis, {}) == {
        "status": "degraded",
        "reason_codes": ["NOTIFICATION_EVIDENCE_UNKNOWN"],
        "expected": True,
    }
    confirmed = {"status": "sent", "send_confirmed_count": 1}
    assert _notification_delivery_health(confirmed, {})["status"] == "confirmed"
    trade = {"summary": {"last_receipt_result": {"status": "unresolved"}}}
    assert _notification_delivery_health(confirmed, trade)["reason_codes"] == ["TRADE_RECEIPT_UNCONFIRMED"]


def test_trade_intake_aggregation_follows_latest_source_events_in_either_order() -> None:
    older = {
        "listener_status": "listening", "listener_stage": "ready",
        "last_heartbeat_utc": "2026-01-01T00:00:00Z",
        "last_push_received_utc": "2026-01-01T00:01:00Z", "last_push_deal_id": "old",
        "last_backfill_check_utc": "2026-01-01T00:03:00Z",
        "last_backfill_window_start_utc": "old-start", "last_backfill_error": "old-error",
        "last_backfill_result": {"status": "failed"},
        "last_backfill_deal_count": 4, "last_backfill_applied_count": 4,
        "last_backfill_skipped_duplicate_count": 4, "last_backfill_failed_count": 4,
        "last_backfill_unresolved_count": 4,
        "last_deal_result": {"status": "old"},
        "last_receipt_result": {"status": "unresolved"},
    }
    newer = {
        "listener_status": "listening", "listener_stage": "recovering",
        "last_heartbeat_utc": "2026-01-01T00:04:00+00:00",
        "last_push_received_utc": "2026-01-01T01:02:00+01:00", "last_push_deal_id": "new",
        "last_backfill_check_utc": "2026-01-01T00:05:00Z",
        "last_backfill_window_start_utc": "new-start", "last_backfill_error": None,
        "last_backfill_result": {"status": "applied"},
        "last_backfill_deal_count": 1, "last_backfill_applied_count": 1,
        "last_backfill_skipped_duplicate_count": 1, "last_backfill_failed_count": 1,
        "last_backfill_unresolved_count": 1,
        "last_deal_result": {"status": "new"},
        "last_receipt_result": {"status": "sent"},
    }
    for sources in ([older, newer], [newer, older]):
        trade = {"sources": [{"summary": item} for item in sources]}
        summary = _aggregate_trade_intake_summaries(sources)
        assert summary["last_heartbeat_utc"] == newer["last_heartbeat_utc"]
        assert summary["last_push_deal_id"] == "new"
        assert summary["last_backfill_window_start_utc"] == "new-start"
        assert summary["last_backfill_error"] is None
        assert summary["last_backfill_result"] == {"status": "applied"}
        for key in (
            "last_backfill_deal_count", "last_backfill_applied_count",
            "last_backfill_skipped_duplicate_count", "last_backfill_failed_count",
            "last_backfill_unresolved_count",
        ):
            assert summary[key] == 1
        assert summary["listener_stage"] is None
        assert summary["last_deal_result"] is None
        assert summary["last_receipt_result"] is None
        trade["summary"] = summary
        assert "TRADE_RECEIPT_UNCONFIRMED" in _notification_delivery_health(
            {"status": "sent"}, trade,
        )["reason_codes"]


def test_trade_intake_aggregation_nulls_conflicting_equal_time_events() -> None:
    first = {
        "last_push_received_utc": "2026-01-01T00:01:00Z", "last_push_deal_id": "a",
        "last_backfill_check_utc": "2026-01-01T00:02:00Z",
        "last_backfill_applied_count": 1, "last_backfill_result": {"status": "a"},
        "last_fee_attempted_at_ms": 1000, "last_fee_error": "a",
    }
    second = {
        "last_push_received_utc": "2026-01-01T01:01:00+01:00", "last_push_deal_id": "b",
        "last_backfill_check_utc": "2026-01-01T01:02:00+01:00",
        "last_backfill_applied_count": 4, "last_backfill_result": {"status": "b"},
        "last_fee_attempted_at_ms": 1000, "last_fee_error": "b",
    }
    for sources in ([first, second], [second, first]):
        summary = _aggregate_trade_intake_summaries(sources)
        assert summary["last_push_deal_id"] is None
        assert summary["last_backfill_applied_count"] is None
        assert summary["last_backfill_result"] is None
        assert summary["last_fee_attempted_at_ms"] == 1000
        assert summary["last_fee_error"] is None


def test_notification_delivery_is_visible_alongside_existing_overall_failure() -> None:
    envelope = {
        "ok": True,
        "data": {
            "summary": {"ok": False, "warning_codes": ["NOTIFICATION_ROUTE_MISSING"]},
            "notification_delivery": {
                "status": "degraded", "reason_codes": ["NOTIFICATION_ROUTE_MISSING"],
            },
        },
    }
    text = format_runtime_status_summary(envelope)
    assert "overall: FAIL" in text
    assert "notification delivery: status=degraded reasons=NOTIFICATION_ROUTE_MISSING" in text


def test_system_alert_fallback_and_unconfirmed_state_are_visible_in_status() -> None:
    envelope = {"ok": True, "data": {"summary": {"ok": False},
        "system_alert_delivery": {"status": "degraded", "reason_code": "SYSTEM_ALERT_DELIVERY_UNCONFIRMED",
                                  "provider": "feishu_app", "fallback_used": True, "active_count": 1}}}
    text = format_runtime_status_summary(envelope)
    assert "system alert delivery: status=degraded provider=feishu_app fallback=yes active=1" in text


def test_status_journal_emits_error_meta_signal_only_when_degraded(capsys) -> None:
    args = argparse.Namespace(command="status", json=False, journal_summary=True)
    delivery = {"status": "degraded", "reason_codes": ["NOTIFICATION_EVIDENCE_UNKNOWN"]}
    execute = lambda _name, _payload: {"ok": True, "data": {"summary": {"ok": True}, "notification_delivery": delivery}}
    assert handle_observability_command(
        args,
        execute_tool_fn=execute,
        runtime_status_payload_from_args_fn=lambda _args: {},
    ) == 0
    output = capsys.readouterr()
    assert "notification delivery: status=degraded" in output.out
    assert "<3>NOTIFICATION_DELIVERY_DEGRADED" in output.err

    delivery["status"] = "confirmed"
    assert handle_observability_command(
        args,
        execute_tool_fn=execute,
        runtime_status_payload_from_args_fn=lambda _args: {},
    ) == 0
    assert capsys.readouterr().err == ""


def test_status_journal_marks_system_alert_delivery_degraded(capsys) -> None:
    args = argparse.Namespace(command="status", json=False, journal_summary=True)
    execute = lambda _name, _payload: {"ok": True, "data": {
        "summary": {"ok": False}, "system_alert_delivery": {
            "status": "degraded", "reason_code": "SYSTEM_ALERT_DELIVERY_UNCONFIRMED",
            "fallback_used": False, "provider": None, "active_count": 1,
        },
    }}
    assert handle_observability_command(
        args, execute_tool_fn=execute, runtime_status_payload_from_args_fn=lambda _args: {},
    ) == 0
    output = capsys.readouterr()
    assert "system alert delivery: status=degraded" in output.out
    assert "<3>SYSTEM_ALERT_DELIVERY_DEGRADED" in output.err
