from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from src.application.trades.auto_intake import (
    _dispatch_portfolio_refresh_intent,
)
from src.application.trades.deal_identity import broker_deal_key
from src.application.trades.intake import _build_portfolio_refresh_intent
from src.application.trades.normalizer import normalize_trade_deal


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


def _refresh_intent(deal, *, state=None, source="push", enabled=True):
    """Portfolio refresh intent for a deal; the push/first-seen case by default."""
    return _build_portfolio_refresh_intent(
        deal, state={} if state is None else state, apply_changes=True, source=source, enabled=enabled
    )


def test_stock_refresh_intent_is_first_seen_and_redacted() -> None:
    deal = _stock_deal()
    intent = _refresh_intent(deal)

    assert intent is not None
    assert intent["account"] == "lx"
    assert intent["request_id"].startswith("stock-refresh:")
    assert len(intent["request_id"]) == len("stock-refresh:") + 64
    assert "REAL_123" not in intent["request_id"]
    assert "deal-secret-1" not in intent["request_id"]

    deal_key = "futu:lx:REAL_123:deal-secret-1"
    duplicate_state = {"failed_deal_ids": {deal_key: {"status": "failed"}}}
    assert _refresh_intent(deal, state=duplicate_state, source="backfill") is None
    assert _refresh_intent(_stock_deal(option_type="call")) is None


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


def _futu_stock_payload(**overrides) -> dict:
    payload = {
        "broker_account_id": "futu:REAL:900000000000000001",
        "futu_account_id": "900000000000000001",
        "acc_id": "900000000000000001",
        "trd_env": "REAL",
        "external_id_namespace": "futu.deal",
        "external_order_namespace": "futu.order",
        "deal_id": "4583632043475634176",
        "order_id": "FH1D244146DA2E8000",
        "code": "US.VOO",
        "stock_name": "标普500ETF-Vanguard",
        "trd_market": "US",
        "trd_side": "BUY",
        "qty": 2.0,
        "price": 695.450589,
        "create_time": "2026-09-15 10:56:52.838",
    }
    payload.update(overrides)
    return payload


def test_real_futu_stock_payload_produces_refresh_intent() -> None:
    deal = normalize_trade_deal(
        _futu_stock_payload(),
        futu_account_mapping={"900000000000000001": "sy"},
        allow_opend_refresh=False,
    )

    assert deal.execution_input["errors"] == []
    assert deal.currency == "USD"
    intent = _refresh_intent(deal)
    assert intent == {
        "account": "sy",
        "request_id": "stock-refresh:"
        + hashlib.sha256(broker_deal_key(deal).encode()).hexdigest(),
    }


def test_option_and_unresolved_asset_payloads_never_produce_refresh_intent() -> None:
    mapping = {"900000000000000001": "sy"}
    option = normalize_trade_deal(
        _futu_stock_payload(
            code="US.PDD261009P77000", deal_id="d-opt", trd_side="SELL_SHORT", qty=1
        ),
        futu_account_mapping=mapping,
        allow_opend_refresh=False,
    )
    assert option.option_type == "put"
    assert _refresh_intent(option) is None

    unresolved = normalize_trade_deal(
        _futu_stock_payload(code="600519.SH", deal_id="d-cn"),
        futu_account_mapping=mapping,
        allow_opend_refresh=False,
    )
    assert _refresh_intent(unresolved) is None
