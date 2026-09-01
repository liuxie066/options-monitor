from __future__ import annotations

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades.auto_intake import (
    _attach_combo_reconciliation_after_open,
)
from src.application.trades.combo_reconciliation import (
    reconcile_account_post_trade_combos,
    trade_combo_runtime_environment,
)


BASE_TIME_MS = 1_785_312_000_000


def _event(
    event_id: str,
    lot_id: str,
    *,
    option_type: str,
    side: str,
    strike: int,
    event_time_ms: int,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type=option_type,
            position_side=side,
            strike=strike,
            expiration_ymd="2026-08-21",
        ),
        contracts=1,
        price=1,
        currency="USD",
        source="test",
        lot_id=lot_id,
        raw_payload={
            "_trade_intake_source": {
                "schema_version": "trade_intake_source.v1",
                "transport": "push",
                "source_id": "lx",
                "account": "lx",
                "futu_account_id": "1001",
                "opend_process": "FutuOpenD",
                "opend_host": "127.0.0.1",
                "opend_port": 11111,
                "received_at_utc": "2026-07-31T13:00:00+00:00",
            }
        },
    )


def test_account_reconciler_reads_frozen_exposure_and_auto_adopts_strict_match(
    tmp_path,
    monkeypatch,
) -> None:
    import src.application.trades.combo_reconciliation as module

    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _event(
            "call-open",
            "call-lot",
            option_type="call",
            side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
    ):
        persist_trade_event_object(repo, event)
    reads: list[tuple[str, str]] = []

    def _read_exposures(**kwargs):
        reads.append((kwargs["market"], kwargs["market_trading_date"]))
        return {
            "available": True,
            "reason": "ok",
            "exposures": [
                {
                    "candidate_exposure_id": "exposure-1",
                    "candidate_occurrence_id": "occurrence-1",
                    "account": "lx",
                    "market": "US",
                    "currency": "USD",
                    "multiplier": 100,
                    "put_contract_key": {
                        "underlying_symbol": "NVDA",
                        "option_type": "put",
                        "expiration_ymd": "2026-08-21",
                        "strike": 100,
                    },
                    "call_contract_key": {
                        "underlying_symbol": "NVDA",
                        "option_type": "call",
                        "expiration_ymd": "2026-08-21",
                        "strike": 110,
                    },
                    "generated_at_ms": BASE_TIME_MS,
                    "valid_until_ms": BASE_TIME_MS + 10_000,
                    "delivery_confirmed": True,
                }
            ],
        }

    monkeypatch.setattr(module, "read_combo_candidate_exposures", _read_exposures)
    result = reconcile_account_post_trade_combos(
        repo=repo,
        runtime_root=tmp_path,
        account="lx",
        runtime_environment="opend:127.0.0.1:11111",
        mode="observe",
        effective_now_ms=BASE_TIME_MS + 3_000,
    )

    assert reads and reads[0][0] == "US"
    assert result["proposal_ready_count"] == 1
    assert result["inferences"][0]["evidence_grade"] == "exact_delivered_candidate"
    assert repo.list_combo_pair_inferences(account="lx")[0]["status"] == "proposal_ready"
    assert len(repo.list_trade_events()) == 2
    assert all(
        not item["fields"].get("strategy_group_id")
        for item in repo.list_position_lots()
    )

    adoptions: list[dict] = []

    def _adopt(**kwargs):
        adoptions.append(kwargs)
        return {
            "status": "adopted",
            "inference": result["inferences"][0],
        }

    monkeypatch.setattr(module, "adopt_post_trade_combo_pair", _adopt)
    auto_result = reconcile_account_post_trade_combos(
        repo=repo,
        runtime_root=tmp_path,
        account="lx",
        runtime_environment="opend:127.0.0.1:11111",
        mode="auto",
        effective_now_ms=BASE_TIME_MS + 4_000,
    )

    assert auto_result["auto_adoption_count"] == 1
    assert adoptions[0]["inference_id"] == result["inferences"][0]["inference_id"]
    assert adoptions[0]["actor"] == "trade_intake:auto_combo_reconciliation"
    assert adoptions[0]["apply_changes"] is True


def test_off_mode_does_not_touch_the_repository(tmp_path) -> None:
    result = reconcile_account_post_trade_combos(
        repo=object(),
        runtime_root=tmp_path,
        account="lx",
        runtime_environment="opend:127.0.0.1:11111",
        mode="off",
    )
    assert result["status"] == "off"
    assert result["persisted"] is False


def test_post_commit_failure_is_diagnostic_only_and_preserves_receipt() -> None:
    receipt = {"status": "confirmed", "delivery_key": "receipt-1"}
    result = {
        "status": "applied",
        "action": "open",
        "account": "lx",
        "receipt": receipt,
    }

    def _fail() -> dict:
        raise RuntimeError("injected reconcile failure")

    returned = _attach_combo_reconciliation_after_open(
        result,
        apply_changes=True,
        mode="observe",
        reconcile_fn=_fail,
    )

    assert returned is result
    assert returned["status"] == "applied"
    assert returned["receipt"] is receipt
    assert returned["combo_reconciliation"]["status"] == "failed"
    assert "injected reconcile failure" in returned["combo_reconciliation"]["error"]


def test_runtime_environment_is_stable_per_opend_endpoint() -> None:
    assert (
        trade_combo_runtime_environment(host="127.0.0.1", port=11111)
        == "opend:127.0.0.1:11111"
    )
