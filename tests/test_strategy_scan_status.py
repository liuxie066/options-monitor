from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
    StrategyScanStatusError,
    load_strategy_scan_status_index_v2,
    publish_strategy_scan_status,
    publish_strategy_scan_status_index,
    publish_strategy_scan_status_index_v2,
    validate_strategy_scan_status_index_v2,
)
from src.application.source_receipts import sha256_bytes


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


def _v2_expected(
    *,
    owner: str = "sp_lc",
    mode: str = "combo_yield",
    config_hash: str = "a" * 64,
) -> list[dict[str, str]]:
    return [
        {
            "market": "US",
            "symbol": "NVDA",
            "strategy_family": "combo_yield",
            "strategy_mode": mode,
            "candidate_owner": owner,
            "account_config_sha256": config_hash,
        }
    ]


def _publish_combo_status(report_dir: Path) -> None:
    (report_dir / "nvda_combo_yield_candidates.csv").write_text(
        "symbol\nNVDA\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="combo_yield",
        status="completed",
        candidate_count=1,
        snapshot_id="quote-1",
        receipt_relpath="quotes/quote-1/receipt.json",
    )


def test_v2_index_is_csv_independent_after_status_publication(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    _publish_combo_status(report_dir)

    index = publish_strategy_scan_status_index_v2(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256="a" * 64,
        expected=_v2_expected(),
    )
    (report_dir / "nvda_combo_yield_candidates.csv").unlink()

    loaded = load_strategy_scan_status_index_v2(
        report_dir / STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256="a" * 64,
    )
    assert loaded["content_sha256"] == index["content_sha256"]
    assert "artifacts" not in loaded["items"][0]
    assert loaded["items"][0]["candidate_owner"] == "sp_lc"


@pytest.mark.parametrize(
    ("owner", "mode"),
    [("opening", "combo_yield"), ("sp_lc", "put"), ("unknown", "combo_yield")],
)
def test_v2_index_rejects_owner_mode_mismatch(
    tmp_path: Path,
    owner: str,
    mode: str,
) -> None:
    report_dir = tmp_path / f"reports-{owner}-{mode}"
    report_dir.mkdir()
    _publish_combo_status(report_dir)

    with pytest.raises(StrategyScanStatusError, match="owner/mode|unknown"):
        publish_strategy_scan_status_index_v2(
            report_dir=report_dir,
            run_id="run-1",
            account="lx",
            account_config_sha256="a" * 64,
            expected=_v2_expected(owner=owner, mode=mode),
        )


def test_v2_index_rejects_scope_config_hash_mismatch(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    _publish_combo_status(report_dir)

    with pytest.raises(StrategyScanStatusError, match="config hash mismatch"):
        publish_strategy_scan_status_index_v2(
            report_dir=report_dir,
            run_id="run-1",
            account="lx",
            account_config_sha256="a" * 64,
            expected=_v2_expected(config_hash="b" * 64),
        )


def test_v2_index_does_not_synthesize_missing_status(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    with pytest.raises(StrategyScanStatusError, match="unreadable"):
        publish_strategy_scan_status_index_v2(
            report_dir=report_dir,
            run_id="run-1",
            account="lx",
            account_config_sha256="a" * 64,
            expected=_v2_expected(),
        )
    assert not (report_dir / "nvda_combo_yield_candidates.csv").exists()


@pytest.mark.parametrize("candidate_count", [-1, True, "1"])
def test_v2_index_rejects_noncanonical_candidate_count(
    tmp_path: Path,
    candidate_count: object,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    _publish_combo_status(report_dir)
    payload = publish_strategy_scan_status_index_v2(
        report_dir=report_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256="a" * 64,
        expected=_v2_expected(),
    )
    payload.pop("index_path")
    payload["items"][0]["candidate_count"] = candidate_count
    payload["content_sha256"] = sha256_bytes(
        json.dumps(
            {key: value for key, value in payload.items() if key != "content_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )

    with pytest.raises(StrategyScanStatusError, match="non-negative integer"):
        validate_strategy_scan_status_index_v2(
            payload,
            expected_run_id="run-1",
            expected_account="lx",
            expected_account_config_sha256="a" * 64,
        )


def test_v2_index_rejects_incomplete_quote_binding(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    _publish_combo_status(report_dir)
    status_path = report_dir / "nvda_combo_yield_scan_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status.pop("receipt_relpath")
    status_path.write_text(json.dumps(status), encoding="utf-8")

    with pytest.raises(StrategyScanStatusError, match="quote binding"):
        publish_strategy_scan_status_index_v2(
            report_dir=report_dir,
            run_id="run-1",
            account="lx",
            account_config_sha256="a" * 64,
            expected=_v2_expected(),
        )
