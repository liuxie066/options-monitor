from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.position_advice_authority import (
    portfolio_account_identity_hash,
)
from src.application.position_advice_account_identity_reader import (
    PositionAdviceAccountIdentityError,
    read_current_run_portfolio_identity,
)
from src.application.position_advice_source_producers import (
    publish_portfolio_source_snapshot,
)


NOW = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)
RUN_ID = "account-run-sy"
ACCOUNT = "sy"
IDENTIFIERS = ["futu-sy-123"]
IDENTITY_HASH = portfolio_account_identity_hash(
    normalized_portfolio_source="futu",
    broker_account_identifiers=IDENTIFIERS,
)


def _publish(
    root: Path,
    *,
    run_id: str = RUN_ID,
    account: str = ACCOUNT,
    identifiers: list[str] | None = None,
    observed_at: datetime = NOW,
) -> Path:
    path, _receipt = publish_portfolio_source_snapshot(
        producer_root=root,
        account_run_id=run_id,
        account=account,
        broker="futu",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=portfolio_account_identity_hash(
            normalized_portfolio_source="futu",
            broker_account_identifiers=identifiers or IDENTIFIERS,
        ),
        included_markets=["US"],
        portfolio_context={
            "source_observed_at": observed_at.isoformat(),
            "source_account_identifiers": identifiers or IDENTIFIERS,
            "cash_by_currency": {"USD": 1000},
        },
        completed_at=observed_at + timedelta(seconds=1),
    )
    return path


def _read(root: Path, **overrides: object) -> dict:
    arguments = {
        "account_state_dir": root,
        "account_run_id": RUN_ID,
        "expected_account": ACCOUNT,
        "expected_market": "US",
        "now": NOW + timedelta(seconds=2),
    }
    arguments.update(overrides)
    return read_current_run_portfolio_identity(**arguments)


def test_reads_unique_fresh_current_run_portfolio_identity(tmp_path: Path) -> None:
    receipt_path = _publish(tmp_path)

    result = _read(tmp_path)

    assert result == {
        "status": "available",
        "account": "sy",
        "normalized_portfolio_source": "futu",
        "portfolio_account_identity_hash": IDENTITY_HASH,
        "producer_account_run_id": RUN_ID,
        "included_markets": ["US"],
        "snapshot_id": json.loads(receipt_path.read_text())["snapshot_id"],
        "receipt_hash": result["receipt_hash"],
        "payload_sha256": json.loads(receipt_path.read_text())["payload_sha256"],
        "source_observed_at": "2026-07-30T10:00:00Z",
        "expires_at": "2026-07-30T10:30:00Z",
    }
    assert len(result["receipt_hash"]) == 64


def test_never_falls_back_to_another_account_run(tmp_path: Path) -> None:
    _publish(tmp_path, run_id="older-run")

    with pytest.raises(PositionAdviceAccountIdentityError) as exc_info:
        _read(tmp_path)

    assert exc_info.value.reason_code == "current_run_portfolio_receipt_missing"


def test_rejects_ambiguous_current_run_receipts(tmp_path: Path) -> None:
    first = _publish(tmp_path)
    duplicate = first.parent.parent / ("f" * 64) / "receipt.json"
    duplicate.parent.mkdir()
    duplicate.write_bytes(first.read_bytes())

    with pytest.raises(PositionAdviceAccountIdentityError) as exc_info:
        _read(tmp_path)

    assert exc_info.value.reason_code == "current_run_portfolio_receipt_ambiguous"


def test_rejects_stale_receipt(tmp_path: Path) -> None:
    _publish(tmp_path, observed_at=NOW - timedelta(minutes=31))

    with pytest.raises(PositionAdviceAccountIdentityError) as exc_info:
        _read(tmp_path)

    assert exc_info.value.reason_code == "current_run_portfolio_receipt_stale"


def test_rejects_payload_identity_mismatch(tmp_path: Path) -> None:
    receipt_path = _publish(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    receipt["portfolio_account_identity_hash"] = "f" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(PositionAdviceAccountIdentityError) as exc_info:
        _read(tmp_path)

    assert exc_info.value.reason_code == "current_run_portfolio_identity_mismatch"


def test_rejects_market_and_account_mismatch(tmp_path: Path) -> None:
    _publish(tmp_path)

    with pytest.raises(PositionAdviceAccountIdentityError) as market_error:
        _read(tmp_path, expected_market="HK")
    assert market_error.value.reason_code == "current_run_portfolio_receipt_invalid"

    with pytest.raises(PositionAdviceAccountIdentityError) as account_error:
        _read(tmp_path, expected_account="lx")
    assert account_error.value.reason_code == "current_run_portfolio_receipt_invalid"


@pytest.mark.parametrize(
    "mutation",
    ("bad_json", "path_escape", "payload_hash_mismatch"),
)
def test_rejects_unreadable_or_unbound_receipt_payload(
    tmp_path: Path,
    mutation: str,
) -> None:
    receipt_path = _publish(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    if mutation == "bad_json":
        receipt_path.write_text("{", encoding="utf-8")
    elif mutation == "path_escape":
        receipt["payload_relpath"] = "../payload.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    else:
        Path(receipt_path.parent / "payload.json").write_text(
            '{"tampered":true}\n',
            encoding="utf-8",
        )

    with pytest.raises(PositionAdviceAccountIdentityError) as exc_info:
        _read(tmp_path)

    assert exc_info.value.reason_code == "current_run_portfolio_receipt_invalid"


def test_rejects_symlinked_receipt(tmp_path: Path) -> None:
    receipt_path = _publish(tmp_path)
    real_path = receipt_path.with_name("receipt.real.json")
    receipt_path.rename(real_path)
    receipt_path.symlink_to(real_path.name)

    with pytest.raises(PositionAdviceAccountIdentityError) as exc_info:
        _read(tmp_path)

    assert exc_info.value.reason_code == "current_run_portfolio_receipt_invalid"
