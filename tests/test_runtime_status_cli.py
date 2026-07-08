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
