from __future__ import annotations

from scripts.trade_event_normalizer import NormalizedTradeDeal
from scripts.trade_intake_resolver import resolve_trade_deal


class FakeRepo:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def list_records(self, *, page_size: int = 500) -> list[dict]:
        return []

    def get_record_fields(self, record_id: str) -> dict:
        raise KeyError(record_id)

    def create_record(self, fields: dict) -> dict:
        self.created.append(fields)
        return {"record": {"record_id": "rec_open_1"}}


def _deal(**overrides: object) -> NormalizedTradeDeal:
    base = {
        "broker": "富途",
        "futu_account_id": "REAL_1",
        "internal_account": "lx",
        "deal_id": "deal-open-1",
        "order_id": "order-1",
        "symbol": "0700.HK",
        "option_type": "put",
        "side": "sell",
        "position_effect": "open",
        "contracts": 2,
        "price": 3.93,
        "strike": 480.0,
        "multiplier": 100,
        "multiplier_source": "cache",
        "expiration_ymd": "2026-04-29",
        "currency": "HKD",
        "trade_time_ms": 1000,
        "raw_payload": {},
    }
    base.update(overrides)
    return NormalizedTradeDeal(**base)


def test_resolve_trade_open_dry_run_returns_fields_preview() -> None:
    result = resolve_trade_deal(_deal(), repo=FakeRepo(), state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "open"
    assert result.operations[0]["fields"]["account"] == "lx"
    assert "multiplier_source=cache" in result.operations[0]["fields"]["note"]


def test_resolve_trade_open_apply_creates_record() -> None:
    repo = FakeRepo()
    import scripts.trade_intake_resolver as tir

    old_persist = tir.persist_trade_event
    try:
        tir.persist_trade_event = lambda repo, deal: {"event_id": deal.deal_id, "created": True}  # type: ignore[assignment]
        result = resolve_trade_deal(_deal(), repo=repo, state={}, apply_changes=True)
    finally:
        tir.persist_trade_event = old_persist  # type: ignore[assignment]

    assert result.status == "applied"
    assert result.operations[0]["event_id"] == "deal-open-1"
    assert repo.created == []


def test_resolve_trade_open_rejects_duplicate_deal_id() -> None:
    result = resolve_trade_deal(
        _deal(),
        repo=FakeRepo(),
        state={"processed_deal_ids": {"deal-open-1": {"status": "applied"}}},
        apply_changes=False,
    )

    assert result.status == "skipped"
    assert result.reason == "duplicate_deal_id"


def test_resolve_trade_open_retries_retryable_unresolved_deal_id() -> None:
    result = resolve_trade_deal(
        _deal(),
        repo=FakeRepo(),
        state={"unresolved_deal_ids": {"deal-open-1": {"status": "unresolved", "retryable": True}}},
        apply_changes=False,
    )

    assert result.status == "dry_run"
    assert result.reason == "preview_open"


def test_resolve_trade_open_rejects_non_sell_side() -> None:
    result = resolve_trade_deal(_deal(side="buy"), repo=FakeRepo(), state={}, apply_changes=False)

    assert result.status == "unresolved"
    assert result.reason == "unsupported_open_side"


def test_resolve_trade_open_missing_multiplier_is_retryable_with_diagnostics() -> None:
    result = resolve_trade_deal(
        _deal(
            multiplier=None,
            multiplier_source=None,
            normalization_diagnostics={
                "symbol": {"canonical": "9992.HK", "raw_fields": {"code": "HK.POP260528P150000"}},
                "multiplier_resolution": {
                    "canonical_symbol": "9992.HK",
                    "selected_source": None,
                    "attempted_sources": [
                        {"source": "payload", "status": "missing"},
                        {"source": "cache", "status": "miss"},
                        {"source": "config:intake.multiplier_by_symbol", "status": "miss"},
                    ],
                },
            },
        ),
        repo=FakeRepo(),
        state={},
        apply_changes=False,
    )

    assert result.status == "unresolved"
    assert result.reason == "missing_required_fields:multiplier"
    assert result.diagnostics["retryable"] is True
    assert result.diagnostics["missing_fields"] == ["multiplier"]
    assert result.diagnostics["multiplier_resolution"]["canonical_symbol"] == "9992.HK"
    assert result.diagnostics["raw_symbol_fields"] == {"code": "HK.POP260528P150000"}


def test_resolve_trade_open_missing_account_mapping_exposes_diagnostics() -> None:
    result = resolve_trade_deal(
        _deal(
            internal_account=None,
            futu_account_id="281756479859383816",
            raw_payload={"deal_id": "deal-open-1", "trade_acc_id": "281756479859383816"},
            visible_account_fields={"trade_acc_id": "281756479859383816"},
            account_mapping_keys=["999999999999999999"],
        ),
        repo=FakeRepo(),
        state={},
        apply_changes=False,
    )

    assert result.status == "unresolved"
    assert result.reason == "missing_account_mapping:futu_account_id=281756479859383816"
    assert result.diagnostics["futu_account_id"] == "281756479859383816"
    assert result.diagnostics["visible_account_fields"] == {"trade_acc_id": "281756479859383816"}
    assert result.diagnostics["account_mapping_keys"] == ["999999999999999999"]
