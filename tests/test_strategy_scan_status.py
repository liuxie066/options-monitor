from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.candidate_snapshot_contract import candidate_run_mode_fields
from src.application.source_receipts import sha256_bytes
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V5_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V5_SCHEMA,
    STRATEGY_SCAN_STATUS_V3_SCHEMA,
    StrategyScanStatusError,
    load_strategy_scan_status_index,
    publish_strategy_scan_status,
    publish_strategy_scan_status_index,
    validate_strategy_scan_status_index,
)


def _expected(
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


def _combo_report_dir(tmp_path: Path, name: str = "reports") -> Path:
    report_dir = tmp_path / name
    report_dir.mkdir()
    _publish_combo_status(report_dir)
    return report_dir


def _publish_index(**overrides):
    defaults = {
        "run_id": "run-1",
        "account": "lx",
        "account_config_sha256": "a" * 64,
        "expected": _expected(),
        "run_mode": candidate_run_mode_fields(experience=False),
    }
    return publish_strategy_scan_status_index(**{**defaults, **overrides})


@pytest.mark.parametrize(
    "run_mode",
    [
        candidate_run_mode_fields(experience=False),
        candidate_run_mode_fields(
            experience=True,
            account_display_name="美股模拟期权账户",
        ),
    ],
)
def test_current_index_is_mode_data_and_csv_independent(
    tmp_path: Path,
    run_mode: dict[str, object],
) -> None:
    report_dir = _combo_report_dir(tmp_path)

    index = _publish_index(report_dir=report_dir, run_mode=run_mode)
    loaded = load_strategy_scan_status_index(
        report_dir / STRATEGY_SCAN_STATUS_INDEX_V5_FILE,
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256="a" * 64,
    )

    assert loaded["schema_version"] == STRATEGY_SCAN_STATUS_INDEX_V5_SCHEMA
    assert loaded["scan_mode"] == run_mode["scan_mode"]
    assert loaded["content_sha256"] == index["content_sha256"]
    assert loaded["items"][0]["source_status_schema"] == STRATEGY_SCAN_STATUS_V3_SCHEMA
    assert "artifacts" not in loaded["items"][0]
    assert list(report_dir.glob("*candidates*.csv")) == []


@pytest.mark.parametrize(
    ("owner", "mode"),
    [("opening", "combo_yield"), ("sp_lc", "put"), ("unknown", "combo_yield")],
)
def test_current_index_rejects_owner_mode_mismatch(
    tmp_path: Path,
    owner: str,
    mode: str,
) -> None:
    report_dir = _combo_report_dir(tmp_path, f"reports-{owner}-{mode}")

    with pytest.raises(StrategyScanStatusError, match="owner/mode|unknown"):
        _publish_index(report_dir=report_dir, expected=_expected(owner=owner, mode=mode))


def test_wheel_direction_statuses_use_the_same_current_contract(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports-wheel"
    report_dir.mkdir()
    expected = []
    for direction in ("call", "put"):
        status = publish_strategy_scan_status(
            report_dir=report_dir,
            run_id="run-wheel",
            account="lx",
            market="US",
            symbol="NVDA",
            strategy_family="wheel",
            direction=direction,
            status="completed",
            candidate_count=0,
        )
        assert status["schema_version"] == STRATEGY_SCAN_STATUS_V3_SCHEMA
        assert Path(status["status_path"]).name == (
            f"nvda_wheel_{direction}_scan_status.v3.json"
        )
        expected.append(
            {
                "market": "US",
                "symbol": "NVDA",
                "strategy_family": "wheel",
                "direction": direction,
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "account_config_sha256": "a" * 64,
            }
        )

    index = publish_strategy_scan_status_index(
        report_dir=report_dir,
        run_id="run-wheel",
        account="lx",
        account_config_sha256="a" * 64,
        expected=expected,
        run_mode=candidate_run_mode_fields(experience=False),
    )

    assert index["schema_version"] == STRATEGY_SCAN_STATUS_INDEX_V5_SCHEMA
    assert {row["direction"] for row in index["items"]} == {"call", "put"}
    assert all(row["source_status_sha256"] for row in index["items"])


def test_wheel_is_rejected_in_experience_mode(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports-wheel"
    report_dir.mkdir()
    publish_strategy_scan_status(
        report_dir=report_dir,
        run_id="run-wheel",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="wheel",
        direction="put",
        status="completed",
        candidate_count=0,
    )

    with pytest.raises(StrategyScanStatusError, match="unavailable in experience"):
        publish_strategy_scan_status_index(
            report_dir=report_dir,
            run_id="run-wheel",
            account="lx",
            account_config_sha256="a" * 64,
            expected=[
                {
                    "market": "US",
                    "symbol": "NVDA",
                    "strategy_family": "wheel",
                    "direction": "put",
                    "strategy_mode": "wheel",
                    "candidate_owner": "wheel",
                    "account_config_sha256": "a" * 64,
                }
            ],
            run_mode=candidate_run_mode_fields(
                experience=True,
                account_display_name="美股模拟期权账户",
            ),
        )


def test_current_index_rejects_scope_config_hash_mismatch(tmp_path: Path) -> None:
    report_dir = _combo_report_dir(tmp_path)

    with pytest.raises(StrategyScanStatusError, match="config hash mismatch"):
        _publish_index(report_dir=report_dir, expected=_expected(config_hash="b" * 64))


def test_current_index_does_not_synthesize_missing_status(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    with pytest.raises(StrategyScanStatusError, match="unreadable"):
        _publish_index(report_dir=report_dir)


@pytest.mark.parametrize("candidate_count", [-1, True, "1"])
def test_current_index_rejects_noncanonical_candidate_count(
    tmp_path: Path,
    candidate_count: object,
) -> None:
    report_dir = _combo_report_dir(tmp_path)
    payload = _publish_index(report_dir=report_dir)
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
        validate_strategy_scan_status_index(payload)


def test_current_publication_preserves_conflicting_legacy_bytes(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    legacy = report_dir / "nvda_combo_yield_scan_status.json"
    legacy.write_bytes(b"legacy-bytes")

    with pytest.raises(StrategyScanStatusError, match="artifact_version_mismatch"):
        _publish_combo_status(report_dir)

    assert legacy.read_bytes() == b"legacy-bytes"
    assert not (report_dir / "nvda_combo_yield_scan_status.v3.json").exists()


def test_current_status_rejects_legacy_run_before_writing(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    legacy = report_dir / "strategy_scan_status_index.v4.json"
    legacy.write_bytes(b"legacy-index-bytes")

    with pytest.raises(StrategyScanStatusError, match="artifact_version_mismatch"):
        _publish_combo_status(report_dir)

    assert legacy.read_bytes() == b"legacy-index-bytes"
    assert not (report_dir / "nvda_combo_yield_scan_status.v3.json").exists()


def test_current_status_retry_adopts_the_same_semantic_payload(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    kwargs = {
        "report_dir": report_dir,
        "run_id": "run-1",
        "account": "lx",
        "market": "US",
        "symbol": "NVDA",
        "strategy_family": "combo_yield",
        "status": "completed",
        "candidate_count": 1,
    }
    first = publish_strategy_scan_status(**kwargs)
    second = publish_strategy_scan_status(**kwargs)

    assert second["published_at_utc"] == first["published_at_utc"]
    assert second["content_sha256"] == first["content_sha256"]
