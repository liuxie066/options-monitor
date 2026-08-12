from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
    StrategyScanStatusError,
    load_strategy_scan_status_index_v2,
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
    validate_strategy_scan_status_index_v2,
)
from src.application.source_receipts import sha256_bytes


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
    loaded = load_strategy_scan_status_index_v2(
        report_dir / STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256="a" * 64,
    )
    assert loaded["content_sha256"] == index["content_sha256"]
    assert "artifacts" not in loaded["items"][0]
    assert loaded["items"][0]["candidate_owner"] == "sp_lc"
    assert list(report_dir.glob("*candidates*.csv")) == []


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
