from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.application.close_advice_quote_cache import (
    publish_quote_cache_metadata,
    validate_quote_cache_metadata,
)


def test_quote_cache_metadata_binds_source_time_market_and_csv_bytes(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "NVDA_required_data.csv"
    csv_path.write_text(
        "symbol,option_type,expiration,strike,bid,ask\n"
        "NVDA,put,2026-06-19,100,1.0,1.2\n",
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc)
    publish_quote_cache_metadata(
        csv_path=csv_path,
        symbol="NVDA",
        source="opend",
        source_run_id="run-current",
        observed_at=now,
    )

    valid = validate_quote_cache_metadata(
        csv_path=csv_path,
        symbol="NVDA",
        max_age_sec=900,
        now=now + timedelta(seconds=10),
    )
    assert valid["ok"] is True
    assert valid["source"] == "opend"
    assert valid["market"] == "US"

    csv_path.write_text(
        csv_path.read_text(encoding="utf-8") + "NVDA,put,2026-06-19,105,1,1\n",
        encoding="utf-8",
    )
    tampered = validate_quote_cache_metadata(
        csv_path=csv_path,
        symbol="NVDA",
        max_age_sec=900,
        now=now + timedelta(seconds=10),
    )
    assert tampered["ok"] is False
    assert tampered["reason"] == "quote_provenance_bytes_mismatch"


def test_quote_cache_metadata_rejects_cross_day_stale_observation(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "0700.HK_required_data.csv"
    csv_path.write_text(
        "symbol,option_type,expiration,strike,bid,ask\n"
        "0700.HK,call,2026-06-29,500,1.0,1.2\n",
        encoding="utf-8",
    )
    observed = datetime.now(timezone.utc) - timedelta(days=1)
    publish_quote_cache_metadata(
        csv_path=csv_path,
        symbol="0700.HK",
        source="opend",
        source_run_id="run-yesterday",
        observed_at=observed,
    )

    stale = validate_quote_cache_metadata(
        csv_path=csv_path,
        symbol="0700.HK",
        max_age_sec=900,
        now=datetime.now(timezone.utc),
    )
    assert stale["ok"] is False
    assert stale["reason"] == "quote_cache_stale"
