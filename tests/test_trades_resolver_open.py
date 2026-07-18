from __future__ import annotations

from pathlib import Path

import src.application.ledger.repository as ledger_repository

from domain.domain.ledger.position_fields import parse_exp_to_ms
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.resolver import resolve_trade_deal


class FakeRepo:
    def __init__(self, records: list[dict] | None = None) -> None:
        self.records = list(records or [])
        self.created: list[dict] = []

    def list_records(self, *, page_size: int = 500) -> list[dict]:
        return list(self.records)

    def list_position_lots(self) -> list[dict]:
        return list(self.records)

    def list_trade_events(self) -> list[dict]:
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


def _position_record(
    record_id: str,
    *,
    symbol: str = "PDD",
    option_type: str = "put",
    side: str = "short",
    strike: float = 80.0,
    expiration_ymd: str = "2026-07-17",
    contracts_open: int = 1,
) -> dict:
    expiration = parse_exp_to_ms(expiration_ymd)
    assert expiration is not None
    return {
        "record_id": record_id,
        "fields": {
            "broker": "富途",
            "account": "lx",
            "symbol": symbol,
            "option_type": option_type,
            "side": side,
            "status": "open",
            "contracts": contracts_open,
            "contracts_open": contracts_open,
            "contracts_closed": 0,
            "strike": strike,
            "currency": "USD",
            "multiplier": 100,
            "expiration": expiration,
            "expiration_ymd": expiration_ymd,
            "opened_at": 100,
        },
    }


def test_resolve_trade_open_dry_run_returns_fields_preview() -> None:
    result = resolve_trade_deal(_deal(), repo=FakeRepo(), state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "open"
    assert result.operations[0].to_payload()["fields"]["account"] == "lx"
    assert result.operations[0].to_payload()["fields"]["side"] == "short"
    assert "multiplier_source=cache" in result.operations[0].to_payload()["fields"]["note"]


def test_resolve_trade_long_open_dry_run_returns_long_fields_preview() -> None:
    result = resolve_trade_deal(_deal(side="buy"), repo=FakeRepo(), state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "open"
    assert result.operations[0].to_payload()["fields"]["account"] == "lx"
    assert result.operations[0].to_payload()["fields"]["side"] == "long"


def test_resolve_unknown_buy_call_with_companion_put_as_combo_yield_long_call() -> None:
    repo = FakeRepo([_position_record("lot_pdd_short_put")])
    deal = _deal(
        deal_id="deal-pdd-long-call",
        symbol="PDD",
        option_type="call",
        side="buy",
        position_effect=None,
        contracts=1,
        price=0.73,
        strike=100.0,
        expiration_ymd="2026-07-17",
        currency="USD",
        raw_payload={"deal_id": "deal-pdd-long-call", "code": "US.PDD260717C100000"},
    )

    result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "open"
    assert result.diagnostics["position_effect_inference"]["decision"] == "open"
    fields = result.operations[0].to_payload()["fields"]
    assert fields["side"] == "long"
    assert fields["strategy"] == "combo_yield"
    assert fields["leg_role"] == "enhancement_call"
    assert fields["strategy_group_id"] == "combo_yield:lx:PDD:2026-07-17"


def test_combo_yield_group_id_canonicalizes_hk_option_alias() -> None:
    repo = FakeRepo(
        [
            _position_record(
                "lot_tch_short_put",
                symbol="0700.HK",
                strike=440.0,
                expiration_ymd="2026-06-05",
            )
        ]
    )
    deal = _deal(
        deal_id="deal-tch-long-call",
        symbol="TCH",
        option_type="call",
        side="buy",
        position_effect=None,
        contracts=1,
        price=0.73,
        strike=520.0,
        expiration_ymd="2026-06-05",
        currency="HKD",
        raw_payload={"deal_id": "deal-tch-long-call", "code": "HK.TCH260605C520000"},
    )

    result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=False)

    assert result.status == "dry_run"
    fields = result.operations[0].to_payload()["fields"]
    assert fields["strategy"] == "combo_yield"
    assert fields["strategy_group_id"] == "combo_yield:lx:0700.HK:2026-06-05"


def test_resolve_unknown_buy_call_without_companion_opens_pending_combo_yield_long_call() -> None:
    deal = _deal(
        deal_id="deal-pdd-long-call",
        symbol="PDD",
        option_type="call",
        side="buy",
        position_effect=None,
        contracts=1,
        price=0.73,
        strike=100.0,
        expiration_ymd="2026-07-17",
        currency="USD",
        raw_payload={"deal_id": "deal-pdd-long-call", "code": "US.PDD260717C100000"},
    )

    result = resolve_trade_deal(deal, repo=FakeRepo(), state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "open"
    assert result.diagnostics["position_effect_inference"]["open_reason"] == "buy_call_without_close_target"
    fields = result.operations[0].to_payload()["fields"]
    assert fields["side"] == "long"
    assert fields["strategy"] == "combo_yield"
    assert fields["leg_role"] == "enhancement_call"
    assert fields["strategy_group_id"] == "combo_yield:lx:PDD:2026-07-17"


def test_resolve_trade_open_apply_creates_record() -> None:
    repo = FakeRepo()
    result = resolve_trade_deal(
        _deal(),
        repo=repo,
        state={},
        apply_changes=True,
        persist_trade_event_fn=lambda repo, deal: {"event_id": deal.deal_id, "created": True},
    )

    assert result.status == "applied"
    assert result.operations[0].to_payload()["event_id"] == "deal-open-1"
    assert repo.created == []


def test_resolve_trade_open_rejects_missing_trade_time_before_write() -> None:
    result = resolve_trade_deal(_deal(trade_time_ms=None), repo=FakeRepo(), state={}, apply_changes=True)

    assert result.status == "unresolved"
    assert result.reason == "missing_required_fields:trade_time_ms"


def test_resolve_trade_open_apply_uses_ledger_preflight_with_sqlite(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    result = resolve_trade_deal(_deal(), repo=repo, state={}, apply_changes=True)

    assert result.status == "applied"
    operation = result.operations[0].to_payload()
    assert operation["action"] == "open"
    assert operation["event_id"] == "futu:lx:REAL_1:deal-open-1"
    assert operation["ledger_preflight"]["status"] == "ok"
    assert operation["ledger_preflight"]["event_type"] == "open"
    assert operation["ledger_preflight"]["target_lot_id"] == operation["result"]["record_id"]
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["record_id"] == operation["ledger_preflight"]["target_lot_id"]
    assert lots[0]["fields"]["contracts_open"] == 2


def test_resolve_unknown_combo_yield_long_call_apply_preserves_strategy_fields(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    put_result = resolve_trade_deal(
        _deal(
            deal_id="deal-pdd-short-put",
            symbol="PDD",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.5,
            strike=80.0,
            expiration_ymd="2026-07-17",
            currency="USD",
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert put_result.status == "applied"

    call_result = resolve_trade_deal(
        _deal(
            deal_id="deal-pdd-long-call",
            symbol="PDD",
            option_type="call",
            side="buy",
            position_effect=None,
            contracts=1,
            price=0.73,
            strike=100.0,
            expiration_ymd="2026-07-17",
            currency="USD",
            raw_payload={"deal_id": "deal-pdd-long-call", "code": "US.PDD260717C100000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert call_result.status == "applied"
    lots = repo.list_position_lots()
    call_lot = next(item for item in lots if item["fields"]["option_type"] == "call")
    assert call_lot["fields"]["side"] == "long"
    assert call_lot["fields"]["strategy"] == "combo_yield"
    assert call_lot["fields"]["leg_role"] == "enhancement_call"
    assert call_lot["fields"]["yield_enhancement_mode"] == "income_upside_enhancement"
    assert call_lot["fields"]["strategy_group_id"] == "combo_yield:lx:PDD:2026-07-17"


def test_resolve_sell_put_open_after_long_call_uses_same_combo_yield_group(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    call_result = resolve_trade_deal(
        _deal(
            deal_id="deal-pdd-long-call",
            symbol="PDD",
            option_type="call",
            side="buy",
            position_effect=None,
            contracts=1,
            price=0.73,
            strike=100.0,
            expiration_ymd="2026-07-17",
            currency="USD",
            raw_payload={"deal_id": "deal-pdd-long-call", "code": "US.PDD260717C100000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert call_result.status == "applied"

    put_result = resolve_trade_deal(
        _deal(
            deal_id="deal-pdd-short-put",
            symbol="PDD",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.5,
            strike=80.0,
            expiration_ymd="2026-07-17",
            currency="USD",
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert put_result.status == "applied"
    lots = repo.list_position_lots()
    call_lot = next(item for item in lots if item["fields"]["option_type"] == "call")
    put_lot = next(item for item in lots if item["fields"]["option_type"] == "put")
    assert call_lot["fields"]["strategy_group_id"] == "combo_yield:lx:PDD:2026-07-17"
    assert put_lot["fields"]["strategy"] == "combo_yield"
    assert put_lot["fields"]["leg_role"] == "sell_put"
    assert put_lot["fields"]["strategy_group_id"] == call_lot["fields"]["strategy_group_id"]


def test_resolve_trade_open_rejects_duplicate_deal_id() -> None:
    result = resolve_trade_deal(
        _deal(),
        repo=FakeRepo(),
        state={"processed_deal_ids": {"deal-open-1": {"status": "applied"}}},
        apply_changes=False,
    )

    assert result.status == "skipped"
    assert result.reason == "duplicate_deal_id"


def test_resolve_trade_skips_non_option_deal() -> None:
    result = resolve_trade_deal(
        _deal(symbol="TIGR", option_type=None, strike=None, expiration_ymd=None, multiplier=None),
        repo=FakeRepo(),
        state={},
        apply_changes=True,
    )

    assert result.status == "skipped"
    assert result.action is None
    assert result.reason == "not_option_deal"
    assert result.operations == []


def test_resolve_trade_skips_non_option_deal_before_account_mapping() -> None:
    result = resolve_trade_deal(
        _deal(
            internal_account=None,
            futu_account_id="REAL_2",
            symbol="TIGR",
            option_type=None,
            strike=None,
            expiration_ymd=None,
            multiplier=None,
        ),
        repo=FakeRepo(),
        state={},
        apply_changes=True,
    )

    assert result.status == "skipped"
    assert result.reason == "not_option_deal"


def test_resolve_trade_open_retries_retryable_unresolved_deal_id() -> None:
    result = resolve_trade_deal(
        _deal(),
        repo=FakeRepo(),
        state={"unresolved_deal_ids": {"deal-open-1": {"status": "unresolved", "retryable": True}}},
        apply_changes=False,
    )

    assert result.status == "dry_run"
    assert result.reason == "preview_open"


def test_resolve_trade_open_rejects_unknown_side() -> None:
    result = resolve_trade_deal(_deal(side="hold"), repo=FakeRepo(), state={}, apply_changes=False)

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
                        {"source": "opend", "status": "error", "error": "multiplier_not_found"},
                    ],
                    "message": "recognized 9992.HK but multiplier could not be resolved",
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


def test_resolve_trade_open_rejects_zero_contracts_as_unresolved() -> None:
    result = resolve_trade_deal(_deal(contracts=0), repo=FakeRepo(), state={}, apply_changes=False)

    assert result.status == "unresolved"
    assert result.reason == "invalid_required_fields:contracts"
    assert result.diagnostics["invalid_fields"] == ["contracts"]


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


def _diagonal_intent(group_id: str | None) -> dict:
    payload = {
        "strategy": "combo_yield",
        "expiry_structure": "diagonal",
        "strategy_snapshot": {
            "strategy": "combo_yield",
            "expiry_structure": "diagonal",
            "combo_pair_fingerprint": "combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP",
        },
    }
    if group_id is not None:
        payload["strategy_group_id"] = group_id
        payload["strategy_snapshot"]["strategy_group_id"] = group_id
    return payload


def test_diagonal_combo_yield_put_first_preserves_explicit_group_through_projection(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    group_id = "combo_yield:lx:combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP"

    put = resolve_trade_deal(
        _deal(
            deal_id="deal-pdd-put-aug",
            symbol="PDD",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            expiration_ymd="2026-08-21",
            strike=80.0,
            currency="USD",
            raw_payload=_diagonal_intent(group_id),
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    call = resolve_trade_deal(
        _deal(
            deal_id="deal-pdd-call-sep",
            symbol="PDD",
            option_type="call",
            side="buy",
            position_effect="open",
            contracts=1,
            expiration_ymd="2026-09-18",
            strike=100.0,
            price=0.73,
            currency="USD",
            raw_payload=_diagonal_intent(group_id),
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert put.status == "applied"
    assert call.status == "applied"
    lots = repo.list_position_lots()
    assert len(lots) == 2
    for lot in lots:
        fields = lot["fields"]
        assert fields["strategy_group_id"] == group_id
        assert fields["strategy"] == "combo_yield"
        assert fields["strategy_snapshot"]["expiry_structure"] == "diagonal"
    assert next(lot for lot in lots if lot["fields"]["option_type"] == "put")["fields"]["leg_role"] == "sell_put"
    assert next(lot for lot in lots if lot["fields"]["option_type"] == "call")["fields"]["leg_role"] == "enhancement_call"


def test_diagonal_combo_yield_call_first_reconstructs_companion_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "option_positions.sqlite3"
    group_id = "combo_yield:lx:combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP"
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    call = resolve_trade_deal(
        _deal(
            deal_id="deal-pdd-call-first",
            symbol="PDD",
            option_type="call",
            side="buy",
            position_effect="open",
            contracts=1,
            expiration_ymd="2026-09-18",
            strike=100.0,
            currency="USD",
            raw_payload=_diagonal_intent(group_id),
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert call.status == "applied"

    restarted_repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    put = resolve_trade_deal(
        _deal(
            deal_id="deal-pdd-put-after-restart",
            symbol="PDD",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            expiration_ymd="2026-08-21",
            strike=80.0,
            currency="USD",
            raw_payload=_diagonal_intent(group_id),
        ),
        repo=restarted_repo,
        state={},
        apply_changes=True,
    )

    assert put.status == "applied"
    assert {lot["fields"]["strategy_group_id"] for lot in restarted_repo.list_position_lots()} == {group_id}


def test_diagonal_combo_yield_missing_or_conflicting_group_metadata_fails_closed() -> None:
    missing = resolve_trade_deal(
        _deal(
            symbol="PDD",
            option_type="call",
            side="buy",
            position_effect="open",
            contracts=1,
            expiration_ymd="2026-09-18",
            raw_payload=_diagonal_intent(None),
        ),
        repo=FakeRepo(),
        state={},
        apply_changes=False,
    )
    conflicting = resolve_trade_deal(
        _deal(
            symbol="PDD",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            expiration_ymd="2026-08-21",
            raw_payload=_diagonal_intent("combo_yield:sy:wrong-account"),
        ),
        repo=FakeRepo(),
        state={},
        apply_changes=False,
    )

    assert missing.status == "unresolved"
    assert missing.reason == "diagonal_combo_yield_missing_group_metadata"
    assert conflicting.status == "unresolved"
    assert conflicting.reason == "diagonal_combo_yield_conflicting_group_metadata"


def test_diagonal_combo_yield_quantity_conflict_fails_closed() -> None:
    group_id = "combo_yield:lx:combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP"
    call_lot = _position_record(
        "call-lot",
        symbol="PDD",
        option_type="call",
        side="long",
        strike=100.0,
        expiration_ymd="2026-09-18",
        contracts_open=2,
    )
    call_lot["fields"].update(
        {
            "strategy": "combo_yield",
            "leg_role": "enhancement_call",
            "strategy_group_id": group_id,
            "strategy_snapshot": {"expiry_structure": "diagonal", "strategy_group_id": group_id},
        }
    )

    result = resolve_trade_deal(
        _deal(
            symbol="PDD",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=3,
            expiration_ymd="2026-08-21",
            raw_payload=_diagonal_intent(group_id),
        ),
        repo=FakeRepo([call_lot]),
        state={},
        apply_changes=False,
    )

    assert result.status == "unresolved"
    assert result.reason == "diagonal_combo_yield_quantity_conflict"


def test_diagonal_combo_yield_partial_fills_accept_aggregate_companion_quantity() -> None:
    group_id = "combo_yield:lx:combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP"
    call_lots = []
    for record_id in ("call-lot-1", "call-lot-2"):
        lot = _position_record(
            record_id,
            symbol="PDD",
            option_type="call",
            side="long",
            strike=100.0,
            expiration_ymd="2026-09-18",
            contracts_open=1,
        )
        lot["fields"].update(
            {
                "strategy": "combo_yield",
                "leg_role": "enhancement_call",
                "strategy_group_id": group_id,
                "strategy_snapshot": {"expiry_structure": "diagonal", "strategy_group_id": group_id},
            }
        )
        call_lots.append(lot)

    result = resolve_trade_deal(
        _deal(
            symbol="PDD",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=2,
            expiration_ymd="2026-08-21",
            raw_payload=_diagonal_intent(group_id),
        ),
        repo=FakeRepo(call_lots),
        state={},
        apply_changes=False,
    )

    assert result.status == "dry_run"
    fields = result.operations[0].to_payload()["fields"]
    assert fields["strategy_group_id"] == group_id
    assert fields.get("paired_long_call_record_id") is None
    companion = result.diagnostics["combo_yield_enrichment"]["companion_long_call"]
    assert companion["contracts_open_total"] == 2
    assert companion["record_ids"] == ["call-lot-1", "call-lot-2"]


def test_diagonal_combo_yield_progressive_partial_fill_does_not_overmatch() -> None:
    group_id = "combo_yield:lx:combo_yield|PDD|PDD_P80_AUG|PDD_C100_SEP"
    put_lot = _position_record(
        "put-lot",
        symbol="PDD",
        option_type="put",
        side="short",
        strike=80.0,
        expiration_ymd="2026-08-21",
        contracts_open=2,
    )
    existing_call = _position_record(
        "call-lot-1",
        symbol="PDD",
        option_type="call",
        side="long",
        strike=100.0,
        expiration_ymd="2026-09-18",
        contracts_open=1,
    )
    for lot, role in ((put_lot, "sell_put"), (existing_call, "enhancement_call")):
        lot["fields"].update(
            {
                "strategy": "combo_yield",
                "leg_role": role,
                "strategy_group_id": group_id,
                "strategy_snapshot": {"expiry_structure": "diagonal", "strategy_group_id": group_id},
            }
        )

    result = resolve_trade_deal(
        _deal(
            symbol="PDD",
            option_type="call",
            side="buy",
            position_effect="open",
            contracts=1,
            expiration_ymd="2026-09-18",
            strike=100.0,
            raw_payload=_diagonal_intent(group_id),
        ),
        repo=FakeRepo([put_lot, existing_call]),
        state={},
        apply_changes=False,
    )

    assert result.status == "dry_run"
    assert result.operations[0].to_payload()["fields"]["strategy_group_id"] == group_id


def test_broker_only_cross_expiry_combo_attempt_fails_closed_without_group_intent() -> None:
    existing_put = _position_record(
        "plain-put",
        symbol="PDD",
        option_type="put",
        side="short",
        strike=80.0,
        expiration_ymd="2026-08-21",
        contracts_open=1,
    )
    call = resolve_trade_deal(
        _deal(
            symbol="PDD",
            option_type="call",
            side="buy",
            position_effect=None,
            contracts=1,
            expiration_ymd="2026-09-18",
            strike=100.0,
            raw_payload={"deal_id": "broker-only-call"},
        ),
        repo=FakeRepo([existing_put]),
        state={},
        apply_changes=False,
    )

    assert call.status == "unresolved"
    assert call.reason == "diagonal_combo_yield_missing_group_metadata"


def test_diagonal_combo_yield_conflicting_snapshot_group_fails_closed() -> None:
    payload = _diagonal_intent("combo_yield:lx:pair-top")
    payload["strategy_snapshot"]["strategy_group_id"] = "combo_yield:lx:pair-snapshot"

    result = resolve_trade_deal(
        _deal(
            symbol="PDD",
            option_type="call",
            side="buy",
            position_effect="open",
            contracts=1,
            expiration_ymd="2026-09-18",
            raw_payload=payload,
        ),
        repo=FakeRepo(),
        state={},
        apply_changes=False,
    )

    assert result.status == "unresolved"
    assert result.reason == "diagonal_combo_yield_conflicting_group_metadata"
