from __future__ import annotations

import json
from pathlib import Path

from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index,
)


def test_completed_zero_candidates_is_available_and_bound_to_artifacts(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "nvda_sell_put_candidates.csv").write_text(
        "symbol\n",
        encoding="utf-8",
    )
    (report_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        "symbol\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="sell_put",
        status="completed",
        candidate_count=0,
        snapshot_id="snapshot-1",
        receipt_relpath="quotes/receipt.json",
    )

    index = publish_strategy_scan_status_index(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        expected=[
            {
                "market": "US",
                "symbol": "NVDA",
                "strategy_family": "sell_put",
            }
        ],
    )

    assert index["counts"] == {
        "completed": 1,
        "unavailable": 0,
        "failed": 0,
        "not_applicable": 0,
    }
    assert index["items"][0]["candidate_count"] == 0


def test_completed_zero_success_empty_preserves_quote_outcome(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "nvda_sell_put_candidates.csv").write_text(
        "symbol\n",
        encoding="utf-8",
    )
    (report_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        "symbol\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="sell_put",
        status="completed",
        candidate_count=0,
        snapshot_id="snapshot-empty",
        receipt_relpath="quotes/empty/receipt.json",
        source_outcome="success_empty",
        reason_code="no_contract_rows",
    )

    index = publish_strategy_scan_status_index(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        expected=[
            {
                "market": "US",
                "symbol": "NVDA",
                "strategy_family": "sell_put",
            }
        ],
    )

    item = index["items"][0]
    assert item["status"] == "completed"
    assert item["candidate_count"] == 0
    assert item["source_outcome"] == "success_empty"
    assert item["reason_code"] == "no_contract_rows"
    assert item["snapshot_id"] == "snapshot-empty"
    assert item["receipt_relpath"] == "quotes/empty/receipt.json"


def test_not_applicable_strategy_is_terminal_and_not_failed(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "0883.hk_sell_call_candidates.csv").write_text(
        "\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=report_dir,
        run_id="run-1",
        account="sy",
        market="HK",
        symbol="0883.HK",
        strategy_family="covered_call",
        status="not_applicable",
        reason="stock_context_missing",
    )

    index = publish_strategy_scan_status_index(
        report_dir=report_dir,
        run_id="run-1",
        account="sy",
        expected=[
            {
                "market": "HK",
                "symbol": "0883.HK",
                "strategy_family": "covered_call",
            }
        ],
    )

    assert index["counts"] == {
        "completed": 0,
        "unavailable": 0,
        "failed": 0,
        "not_applicable": 1,
    }
    assert index["items"][0]["status"] == "not_applicable"


def test_index_keeps_artifact_free_opening_status_and_synthesizes_missing(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "nvda_sell_call_candidates.csv").write_text(
        "symbol\n",
        encoding="utf-8",
    )
    status = publish_strategy_scan_status(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="covered_call",
        status="completed",
        candidate_count=1,
    )
    (report_dir / "nvda_sell_call_candidates.csv").write_text(
        "symbol\nNVDA\n",
        encoding="utf-8",
    )

    index = publish_strategy_scan_status_index(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        expected=[
            {
                "market": "US",
                "symbol": "NVDA",
                "strategy_family": "covered_call",
            },
            {
                "market": "US",
                "symbol": "AMD",
                "strategy_family": "sell_put",
            },
        ],
    )

    reasons = {item.get("reason") for item in index["items"]}
    assert reasons == {None, "strategy_scan_status_missing"}
    assert index["counts"]["completed"] == 1
    assert index["counts"]["failed"] == 1
    payload = json.loads(
        (report_dir / "strategy_scan_status_index.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["expected_count"] == 2
    assert status["status"] == "completed"
