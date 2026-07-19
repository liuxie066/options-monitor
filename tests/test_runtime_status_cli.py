from __future__ import annotations

from src.application.runtime_status_cli import format_runtime_status_summary


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
                "config": {"config_key": "us", "accounts": ["lx", "sy"]},
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
