from __future__ import annotations

import argparse

from src.application.runtime_status_cli import format_runtime_status_summary
from src.application.agent_tools.runtime_status_impl import _notification_delivery_health
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


def test_notification_delivery_is_visible_without_changing_overall_health() -> None:
    envelope = {
        "ok": True,
        "data": {
            "summary": {"ok": True},
            "notification_delivery": {
                "status": "degraded", "reason_codes": ["NOTIFICATION_ROUTE_MISSING"],
            },
        },
    }
    text = format_runtime_status_summary(envelope)
    assert "overall: OK" in text
    assert "notification delivery: status=degraded reasons=NOTIFICATION_ROUTE_MISSING" in text


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
