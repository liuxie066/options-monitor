from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from src.application.opend_symbol_outputs import (
    find_fresh_required_data_quote_receipts,
    publish_required_data_quote_snapshot,
    resolve_exact_fresh_required_data_quote_receipt,
    save_outputs,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    validate_source_receipt,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


def _required_payload(*, mid: float = 1.1) -> dict[str, object]:
    return {
        "symbol": "NVDA",
        "rows": [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-08-21",
                "dte": 25,
                "contract_symbol": "NVDA260821P00100000",
                "strike": 100,
                "spot": 110,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": mid,
                "mid": mid,
                "volume": 50,
                "open_interest": 100,
                "currency": "USD",
                "multiplier": 100,
            }
        ],
        "meta": {"status": "ok", "source": "opend"},
    }


def test_quote_receipt_binds_exact_json_csv_and_fetch_policy(
    tmp_path: Path,
) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    raw_bytes = raw_path.read_bytes()
    csv_bytes = csv_path.read_bytes()

    receipt_path, receipt = publish_required_data_quote_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-1",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan={"symbol": "NVDA", "sides": ["put"]},
        fetch_policy={"source": "opend", "max_wait_sec": 30},
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
    )
    validated = validate_source_receipt(
        receipt,
        producer_root=tmp_path,
        now=NOW + timedelta(seconds=3),
        expected_source_kind="quotes",
    )
    bundle = json.loads(validated["payload_path"].read_text(encoding="utf-8"))

    assert base64.b64decode(bundle["raw_json_base64"]) == raw_bytes
    assert base64.b64decode(bundle["required_data_csv_base64"]) == csv_bytes
    assert bundle["fetch_plan"]["symbol"] == "NVDA"
    assert receipt_path.is_file()


def test_cache_discovery_reuses_receipt_observation_after_shared_files_change(
    tmp_path: Path,
) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    receipt_path, receipt = publish_required_data_quote_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-1",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan={"symbol": "NVDA"},
        fetch_policy={"source": "opend"},
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )

    save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(mid=2.2),
        output_root=tmp_path,
    )
    found = find_fresh_required_data_quote_receipts(
        producer_root=tmp_path,
        symbols=["NVDA"],
        now=NOW + timedelta(minutes=10),
    )

    assert found == {
        "NVDA": receipt_path.resolve().relative_to(tmp_path.resolve()).as_posix()
    }
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["source_observed_at"] == receipt["source_observed_at"]


def test_exact_receipt_resolution_rejects_mutated_scan_bytes(
    tmp_path: Path,
) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    receipt_path, receipt = publish_required_data_quote_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-1",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan={"symbol": "NVDA"},
        fetch_policy={"source": "opend"},
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )

    exact = resolve_exact_fresh_required_data_quote_receipt(
        producer_root=tmp_path,
        symbol="NVDA",
        now=NOW + timedelta(minutes=10),
    )
    assert exact is not None
    assert exact["receipt_relpath"] == (
        receipt_path.resolve().relative_to(tmp_path.resolve()).as_posix()
    )
    assert exact["snapshot_id"] == receipt["snapshot_id"]

    csv_path.write_text(
        csv_path.read_text(encoding="utf-8").replace("1.1", "2.2"),
        encoding="utf-8",
    )

    assert (
        resolve_exact_fresh_required_data_quote_receipt(
            producer_root=tmp_path,
            symbol="NVDA",
            now=NOW + timedelta(minutes=10),
        )
        is None
    )


def test_cache_discovery_does_not_return_stale_receipt(tmp_path: Path) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    publish_required_data_quote_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-1",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan={"symbol": "NVDA"},
        fetch_policy={"source": "opend"},
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )

    assert (
        find_fresh_required_data_quote_receipts(
            producer_root=tmp_path,
            symbols=["NVDA"],
            now=NOW + timedelta(minutes=30),
        )
        == {}
    )


def test_quote_receipt_rejects_symlinked_required_data_file(
    tmp_path: Path,
) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    raw_link = raw_path.with_name("NVDA_required_data_link.json")
    raw_link.symlink_to(raw_path)

    with pytest.raises(
        PositionAdviceSourceError,
        match="source path may not contain symlinks",
    ):
        publish_required_data_quote_snapshot(
            producer_root=tmp_path,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_link,
            csv_path=csv_path,
            fetch_plan={"symbol": "NVDA"},
            fetch_policy={"source": "opend"},
            source_observed_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
        )


def test_cache_discovery_rejects_symlinked_receipt(tmp_path: Path) -> None:
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        _required_payload(),
        output_root=tmp_path,
    )
    receipt_path, _receipt = publish_required_data_quote_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-1",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan={"symbol": "NVDA"},
        fetch_policy={"source": "opend"},
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
    )
    moved_receipt = tmp_path / "moved-receipt.json"
    receipt_path.replace(moved_receipt)
    receipt_path.symlink_to(moved_receipt)

    assert (
        find_fresh_required_data_quote_receipts(
            producer_root=tmp_path,
            symbols=["NVDA"],
            now=NOW + timedelta(minutes=10),
        )
        == {}
    )
