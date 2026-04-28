from __future__ import annotations

from pathlib import Path

from scripts.option_positions_core.domain import EXPIRE_AUTO_CLOSE, parse_exp_to_ms
from scripts.option_positions_core.service import auto_close_expired_positions, build_expired_close_decisions


def test_build_expired_close_decisions_marks_expired_position() -> None:
    as_of_ms = parse_exp_to_ms("2026-04-20")
    assert as_of_ms is not None

    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_1",
                "position_id": "NVDA_20260417_100P_short",
                "status": "open",
                "contracts": 1,
                "contracts_open": 1,
                "expiration": parse_exp_to_ms("2026-04-17"),
                "note": "",
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert len(decisions) == 1
    assert decisions[0]["should_close"] is True
    assert decisions[0]["record_id"] == "rec_1"
    patch = decisions[0]["patch"]
    assert isinstance(patch, dict)
    assert patch["contracts_open"] == 0
    assert patch["status"] == "close"
    assert patch["close_type"] == EXPIRE_AUTO_CLOSE
    assert patch["close_reason"] == "expired"


def test_build_expired_close_decisions_skips_missing_record_id() -> None:
    as_of_ms = parse_exp_to_ms("2026-04-20")
    assert as_of_ms is not None

    decisions = build_expired_close_decisions(
        [
            {
                "position_id": "missing_rid",
                "contracts": 1,
                "contracts_open": 1,
                "note": "exp=2026-04-17",
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert decisions[0]["should_close"] is False
    assert decisions[0]["reason"] == "missing record_id"
    assert decisions[0]["patch"] is None


def test_auto_close_expired_positions_uses_effective_contracts_open_fallback(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    inserted = repo.replace_position_lots(
        [
            {
                "record_id": "rec_nvda",
                "fields": {
                    "record_id": "rec_nvda",
                    "position_id": "NVDA_20260417_100P_short",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": None,
                    "contracts_closed": 0,
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "currency": "USD",
                    "strike": 100,
                    "multiplier": 100,
                    "expiration": parse_exp_to_ms("2026-04-17"),
                    "note": "",
                },
            }
        ]
    )
    assert inserted == 1

    as_of_ms = parse_exp_to_ms("2026-04-20")
    assert as_of_ms is not None

    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]

    decisions, applied, errors = auto_close_expired_positions(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert len(decisions) == 1
    assert decisions[0]["should_close"] is True
    assert len(applied) == 1
    assert errors == []
    assert repo.list_position_lots() == []
