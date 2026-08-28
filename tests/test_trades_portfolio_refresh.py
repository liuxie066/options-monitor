from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.application.trades.auto_intake import (
    _dispatch_portfolio_refresh_intent,
)
from src.application.trades.intake import _build_portfolio_refresh_intent


def _stock_deal(**overrides):
    values = {
        "broker": "富途",
        "internal_account": "lx",
        "futu_account_id": "REAL_123",
        "deal_id": "deal-secret-1",
        "symbol": "0700.HK",
        "option_type": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_stock_refresh_intent_is_first_seen_and_redacted() -> None:
    deal = _stock_deal()
    intent = _build_portfolio_refresh_intent(
        deal,
        state={},
        apply_changes=True,
        source="push",
        enabled=True,
    )

    assert intent is not None
    assert intent["account"] == "lx"
    assert intent["request_id"].startswith("stock-refresh:")
    assert len(intent["request_id"]) == len("stock-refresh:") + 64
    assert "REAL_123" not in intent["request_id"]
    assert "deal-secret-1" not in intent["request_id"]

    deal_key = "futu:lx:REAL_123:deal-secret-1"
    duplicate_state = {"failed_deal_ids": {deal_key: {"status": "failed"}}}
    assert (
        _build_portfolio_refresh_intent(
            deal,
            state=duplicate_state,
            apply_changes=True,
            source="backfill",
            enabled=True,
        )
        is None
    )
    assert (
        _build_portfolio_refresh_intent(
            _stock_deal(option_type="call"),
            state={},
            apply_changes=True,
            source="push",
            enabled=True,
        )
        is None
    )


def test_refresh_dispatch_uses_fixed_timeout_and_audits_acceptance(
    tmp_path: Path,
) -> None:
    calls = []
    audits = []

    class Client:
        def request_holdings_refresh(self, **kwargs):
            calls.append(kwargs)
            return {
                "success": True,
                "status": "accepted",
                "account": kwargs["account"],
                "request_id": kwargs["request_id"],
            }

    intent = {"account": "lx", "request_id": "stock-refresh:abc"}
    _dispatch_portfolio_refresh_intent(
        intent,
        config={"portfolio_management": {"enabled": True}},
        audit_path=tmp_path / "audit.jsonl",
        client=Client(),
        append_audit_fn=lambda _path, event: audits.append(event),
    )

    assert calls == [{**intent, "timeout": 2.0}]
    assert audits[0]["phase"] == "portfolio_refresh_hint_accepted"
    assert set(audits[0]) == {
        "phase",
        "account",
        "request_id",
        "observed_at_utc",
    }


def test_refresh_failure_and_audit_failure_never_escape(tmp_path: Path) -> None:
    logs = []

    class Client:
        def request_holdings_refresh(self, **_kwargs):
            raise TimeoutError("contains-sensitive-upstream-text")

    _dispatch_portfolio_refresh_intent(
        {"account": "lx", "request_id": "stock-refresh:abc"},
        config={"portfolio_management": {"enabled": True}},
        audit_path=tmp_path / "audit.jsonl",
        client=Client(),
        append_audit_fn=lambda *_args: (_ for _ in ()).throw(OSError("disk")),
        log_fn=logs.append,
    )

    assert logs == [
        "[WARN] portfolio refresh hint audit failed "
        "phase=portfolio_refresh_hint_failed error_type=OSError"
    ]
    assert "sensitive" not in logs[0]
