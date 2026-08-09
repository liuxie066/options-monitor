from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.application.ai_decision_advice.contexts import (
    build_frozen_inputs,
    freeze_candidates,
    freeze_external_evidence,
    freeze_option_positions,
    freeze_portfolio,
)
from src.application.ai_decision_advice.evidence_store import (
    append_evidence_records,
    content_fingerprint,
    freeze_evidence_index,
)


def _snapshot() -> dict:
    return {
        "content_sha256": "snap123",
        "ranked_candidates": [
            {
                "candidate_id": "sp1",
                "strategy_mode": "put",
                "rank": 1,
                "facts": {
                    "symbol": "NVDA",
                    "strike": 100,
                    "expiry": "2026-09-18",
                    "multiplier": 100,
                    "dte": 40,
                    "period_net_return": 0.03,
                    "annualized_net_return_on_cash_basis": 0.27,
                },
            },
            {
                "candidate_id": "cc1",
                "strategy_mode": "call",
                "rank": 1,
                "facts": {
                    "symbol": "AAPL",
                    "strike": 200,
                    "expiry": "2026-10-16",
                    "multiplier": 100,
                    "annualized_net_premium_return": 0.15,
                },
            },
        ],
    }


def test_freeze_candidates_split_and_fields() -> None:
    out = freeze_candidates(_snapshot(), market="US")
    assert len(out["sell_put"]) == 1
    assert len(out["covered_call"]) == 1
    sp = out["sell_put"][0]
    assert sp["candidate_id"] == "sp1"
    assert sp["period_net_return"] == 0.03
    assert sp["annualized_gate"] == 0.27
    cc = out["covered_call"][0]
    assert cc["annualized_gate"] == 0.15
    assert out["snapshot_content_sha256"] == "snap123"


def test_freeze_portfolio_relative_weights_no_privacy() -> None:
    ctx = {
        "stocks_by_symbol": {
            "NVDA": {"shares": 300, "currency": "USD", "avg_cost": 100, "futu_account_id": "123"},
            "AAPL": {"shares": 100, "currency": "USD"},
        },
        "cash_by_currency": {"USD": 5000, "HKD": 1000},
    }
    out = freeze_portfolio(ctx)
    weights = {row["symbol"]: row["weight"] for row in out["symbol_weights"]}
    assert weights == {"NVDA": 0.75, "AAPL": 0.25}
    assert out["cash_currencies"] == ["HKD", "USD"]
    text = str(out)
    assert "avg_cost" not in text
    assert "futu_account_id" not in text
    assert "5000" not in text


def test_freeze_portfolio_empty() -> None:
    out = freeze_portfolio(None)
    assert out["symbol_weights"] == []
    assert out["cash_currencies"] == []


def test_freeze_option_positions_open_only_and_relations() -> None:
    lots = [
        {
            "status": "open",
            "contracts_open": 1,
            "contract_key": {
                "underlying_symbol": "NVDA",
                "option_type": "put",
                "position_side": "short",
                "strike": 95,
                "expiration_ymd": "2026-09-18",
            },
        },
        {
            "status": "close",
            "contracts_open": 0,
            "contract_key": {"underlying_symbol": "AAPL", "option_type": "call"},
        },
    ]
    out = freeze_option_positions(lots, candidate_symbols=["NVDA"])
    rows = out["open_positions"]
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "NVDA"
    assert row["side"] == "short"
    assert row["option_type"] == "put"
    assert row["same_underlying_as_candidate"] is True


def test_freeze_option_positions_no_identifiers() -> None:
    lots = [
        {
            "status": "open",
            "contracts_open": 2,
            "lot_id": "lot-1",
            "open_event_id": "evt-1",
            "contract_key": {
                "underlying_symbol": "NVDA",
                "option_type": "put",
                "position_side": "short",
                "strike": 95,
                "expiration_ymd": "2026-09-18",
            },
        }
    ]
    out = freeze_option_positions(lots)
    text = str(out)
    assert "lot-1" not in text
    assert "evt-1" not in text


def test_freeze_external_evidence(tmp_path: Path) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    append_evidence_records(
        base=tmp_path,
        records=[
            {
                "kind": "symbol_evidence",
                "symbol": "NVDA",
                "url": "https://x",
                "claim": "c",
                "topic": "regulatory",
                "event_status": "developing",
                "source": {
                    "title": "public title",
                    "publisher": "public publisher",
                    "url": "https://x",
                    "published_at": None,
                    "provider_private": "must-not-leave-store",
                },
                "content_fingerprint": content_fingerprint("https://x", "c"),
                "account_id": "must-not-leave-store",
                "local_path": "/private/must-not-leave-store",
            },
            {
                "kind": "symbol_status",
                "symbol": "NVDA",
                "identity_status": "ok",
                "last_checked_at": checked,
                "last_success_at": checked,
                "search_status": "completed",
            },
        ],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA", "AAPL"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
    )
    out = freeze_external_evidence(index, symbols=["NVDA", "AAPL"])
    by_symbol = {row["symbol"]: row for row in out["symbols"]}
    assert by_symbol["NVDA"]["coverage"] == "completed"
    assert len(by_symbol["NVDA"]["evidence"]) == 1
    assert by_symbol["AAPL"]["coverage"] == "no_evidence"
    assert out["index_hash"]
    serialized = str(out)
    assert "must-not-leave-store" not in serialized
    assert "account_id" not in serialized
    assert "local_path" not in serialized


def test_build_frozen_inputs_hashes(tmp_path: Path) -> None:
    append_evidence_records(
        base=tmp_path,
        records=[
            {
                "kind": "symbol_status",
                "symbol": "NVDA",
                "identity_status": "ok",
                "last_checked_at": "2026-08-09T04:00:00+00:00",
                "last_success_at": "2026-08-09T04:00:00+00:00",
                "search_status": "completed",
            }
        ],
        evidence_run_id="run-1",
        appended_at="2026-08-09T04:00:00+00:00",
    )
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
    )
    inputs = build_frozen_inputs(
        snapshot=_snapshot(),
        portfolio_context={"stocks_by_symbol": {"NVDA": {"shares": 100, "currency": "USD"}}},
        position_lots=[],
        evidence_index=index,
        market="US",
        evidence_run_id="run-1",
    )
    bindings = inputs.input_bindings()
    assert len(bindings["candidate_snapshot_hash"]) == 64
    assert len(bindings["portfolio_context_hash"]) == 64
    assert len(bindings["option_positions_hash"]) == 64
    assert len(bindings["external_evidence_hash"]) == 64
    assert bindings["external_evidence_run_id"] == "run-1"
