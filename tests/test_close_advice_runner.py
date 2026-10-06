from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from src.application.close_advice_runner import run_close_advice


BUSINESS_DATE = date(2026, 4, 16)
EXPIRATION = "2026-06-15"
OPENED_AT_MS = int(
    datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1000
)


def _freeze_business_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.application.close_advice_runner.expiration_business_today",
        lambda: BUSINESS_DATE,
    )
    monkeypatch.setattr(
        "src.application.close_advice_runner.close_advice_market_date",
        lambda _value, _market: BUSINESS_DATE,
    )


def test_sealed_calendar_counts_market_session_boundary_and_rejects_tamper() -> None:
    from domain.domain.decision_state_fingerprint import canonical_sha256
    from src.application.close_advice_runner import _close_advice_calendar_evidence
    import json

    days = ["2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]
    common = {
        "market": "US", "expiration": "2026-09-10",
        "trading_calendar_market": "US",
        "trading_calendar_as_of_market_date": "2026-09-04",
        "trading_calendar_expiration": "2026-09-10",
        "trading_calendar_request_start": "2026-09-04",
        "trading_calendar_request_end": "2026-09-10",
        "trading_calendar_status": "ok",
        "trading_calendar_dates": json.dumps(days),
        "trading_calendar_input_hash": canonical_sha256({
            "market": "US", "start": "2026-09-04", "end": "2026-09-10", "dates": days,
        }),
        "trading_calendar_receipt": {
            "retcode": 0, "coverage_complete": True, "pagination_complete": True,
            "page_count": 1, "row_count": 4,
        },
    }
    quote = {"market": "US", "snapshot_received_at_utc": "2026-09-04T14:00:00Z"}
    kwargs = {"quote": quote, "market": "US", "market_date": date(2026, 9, 4),
              "expiration": "2026-09-10"}
    unknown, aligned = _close_advice_calendar_evidence(requirement=common, **kwargs)
    assert aligned and (unknown["remaining_trading_sessions_min"], unknown["remaining_trading_sessions_max"]) == (3, 4)
    assert pd.isna(unknown["remaining_trading_sessions"])

    open_state = {**common, "market_state_after_snapshot": "MORNING",
                  "market_state_received_at_utc": "2026-09-04T14:01:00Z"}
    opened, aligned = _close_advice_calendar_evidence(requirement=open_state, **kwargs)
    assert aligned and opened["remaining_trading_sessions"] == 4
    closed_state = {**common, "market_state_after_snapshot": "CLOSED",
                    "market_state_received_at_utc": "2026-09-04T20:01:00Z"}
    closed, aligned = _close_advice_calendar_evidence(requirement=closed_state, **kwargs)
    assert aligned and closed["remaining_trading_sessions"] == 3

    tampered, aligned = _close_advice_calendar_evidence(
        requirement={**common, "trading_calendar_input_hash": "0" * 64}, **kwargs
    )
    assert not aligned and tampered["trading_calendar_status"] == "unavailable"


def _position(
    *,
    lot_id: str = "lot-nvda-1",
    symbol: str = "NVDA",
    option_type: str = "put",
    side: str = "short",
    strike: float = 100.0,
    expiration: str = EXPIRATION,
    strategy_group_id: str | None = None,
    leg_role: str | None = None,
    source_lot_id: str | None = None,
) -> dict:
    return {
        "record_id": lot_id,
        "account": "lx",
        "broker": "富途",
        "symbol": symbol,
        "option_type": option_type,
        "side": side,
        "status": "open",
        "contracts_open": 1,
        "currency": "USD",
        "strike": strike,
        "multiplier": 100,
        "premium": 2.0,
        "expiration": expiration,
        "strategy_group_id": strategy_group_id,
        "leg_role": leg_role,
        "source_stock_lot_id": source_lot_id,
        "opened_at": OPENED_AT_MS,
    }


def _write_context(path: Path, positions: list[dict]) -> None:
    path.write_text(
        json.dumps({"open_positions_min": positions}, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_quotes(root: Path, rows: list[dict]) -> None:
    parsed = root / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(str(row["symbol"]), []).append(row)
    for symbol, symbol_rows in by_symbol.items():
        pd.DataFrame(symbol_rows).to_csv(
            parsed / f"{symbol}_required_data.csv",
            index=False,
        )


def _quote(
    *,
    symbol: str = "NVDA",
    option_type: str = "put",
    strike: float = 100.0,
    bid: float | None = 0.018,
    ask: float | None = 0.02,
    spot: float = 120.0,
) -> dict:
    return {
        "symbol": symbol,
        "option_type": option_type,
        "expiration": EXPIRATION,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "spot": spot,
        "currency": "USD",
        "multiplier": 100,
    }


def _run(
    tmp_path: Path,
    *,
    positions: list[dict],
    quotes: list[dict],
    max_items_per_account: int = 5,
) -> tuple[dict, Path]:
    context_path = tmp_path / "option_positions_context.json"
    required_data_root = tmp_path / "required_data"
    output_dir = tmp_path / "reports"
    _write_context(context_path, positions)
    _write_quotes(required_data_root, quotes)
    result = run_close_advice(
        config={
            "close_advice": {
                "enabled": True,
                "quote_source": "required_data",
                "max_items_per_account": max_items_per_account,
            }
        },
        context_path=context_path,
        required_data_root=required_data_root,
        output_dir=output_dir,
        base_dir=Path.cwd(),
    )
    return result, output_dir


def test_disabled_close_advice_writes_empty_outputs(tmp_path: Path) -> None:
    output_dir = tmp_path / "reports"
    result = run_close_advice(
        config={"close_advice": {"enabled": False}},
        context_path=tmp_path / "missing-context.json",
        required_data_root=tmp_path / "required_data",
        output_dir=output_dir,
        base_dir=Path.cwd(),
    )

    assert result["enabled"] is False
    assert result["status"] == "disabled"
    assert result["report_manifest"]["status"] == "failed"
    assert result["report_manifest"]["reason"] == "close_advice_disabled"
    assert result["rows"] == 0
    assert (output_dir / "close_advice.txt").read_text(encoding="utf-8") == ""
    assert pd.read_csv(output_dir / "close_advice.csv").empty


def test_enabled_close_advice_requires_sealed_run_inputs(tmp_path: Path) -> None:
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    old_csv = b"sentinel-csv\n"
    old_text = "sentinel-text\n"
    (output_dir / "close_advice.csv").write_bytes(old_csv)
    (output_dir / "close_advice.txt").write_text(old_text, encoding="utf-8")

    result = run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=tmp_path / "option_positions_context.json",
        required_data_root=tmp_path / "required_data",
        output_dir=output_dir,
        base_dir=Path.cwd(),
    )

    assert result["status"] == "snapshot_integrity_failed"
    assert result["snapshot_authority"] == "invalid"
    assert result["quote_mode"] == "frozen_snapshot"
    assert result["report_manifest"]["status"] == "failed"
    assert (output_dir / "close_advice.csv").read_bytes() == old_csv
    assert (output_dir / "close_advice.txt").read_text(encoding="utf-8") == old_text

def _publish_sealed_report(
    tmp_path: Path,
    *,
    rows: list[dict],
    manifest_snapshot_sha256: str = "a" * 64,
    manifest_plan_sha256: str = "b" * 64,
) -> Path:
    from src.application.close_advice_report_manifest import (
        publish_close_advice_report_manifest,
    )

    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    csv_path = output_dir / "close_advice.csv"
    text_path = output_dir / "close_advice.txt"
    context_path = output_dir / "option_positions_context.json"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    text_path.write_text("NVDA\n", encoding="utf-8")
    context = {"filters": {"account": "lx"}}
    context_path.write_text(json.dumps(context), encoding="utf-8")
    publish_close_advice_report_manifest(
        csv_path=csv_path,
        text_path=text_path,
        context_path=context_path,
        context=context,
        rows=rows,
        markets_to_run=["US"],
        run_id="run-1",
        quote_mode="frozen_snapshot",
        required_data_snapshot_manifest_sha256=manifest_snapshot_sha256,
        close_advice_required_data_plan_sha256=manifest_plan_sha256,
    )
    return csv_path

def test_report_snapshot_returns_exact_validated_sealed_bytes(tmp_path: Path) -> None:
    from src.application.close_advice_report_manifest import (
        read_close_advice_report_snapshot,
        validate_close_advice_report_manifest,
    )

    rows = [
        {
            "account": "lx",
            "symbol": "NVDA",
            "quote_mode": "frozen_snapshot",
            "required_data_snapshot_manifest_sha256": "a" * 64,
            "close_advice_required_data_plan_sha256": "b" * 64,
        }
    ]
    csv_path = _publish_sealed_report(tmp_path, rows=rows)
    text_path = csv_path.parent / "close_advice.txt"
    snapshot = read_close_advice_report_snapshot(
        csv_path=csv_path,
        desired_market="US",
        account="lx",
        expected_run_id="run-1",
        expected_quote_mode="frozen_snapshot",
    )
    original_csv = csv_path.read_bytes()
    original_text = text_path.read_bytes()
    csv_path.write_text("account,symbol\nlx,TSLA\n", encoding="utf-8")
    text_path.write_text("TSLA\n", encoding="utf-8")

    assert snapshot["validation"]["ok"] is True
    assert snapshot["csv_bytes"] == original_csv
    assert snapshot["text_bytes"] == original_text
    assert (
        validate_close_advice_report_manifest(
            csv_path=csv_path,
            expected_quote_mode="frozen_snapshot",
        )["reason"]
        == "close_advice_report_bytes_mismatch"
    )

def test_sealed_report_rejects_row_to_manifest_hash_mismatch(tmp_path: Path) -> None:
    from src.application.close_advice_report_manifest import (
        validate_close_advice_report_manifest,
    )

    csv_path = _publish_sealed_report(
        tmp_path,
        rows=[
            {
                "account": "lx",
                "symbol": "NVDA",
                "quote_mode": "frozen_snapshot",
                "required_data_snapshot_manifest_sha256": "c" * 64,
                "close_advice_required_data_plan_sha256": "b" * 64,
            }
        ],
    )

    validation = validate_close_advice_report_manifest(
        csv_path=csv_path,
        expected_quote_mode="frozen_snapshot",
    )
    assert validation["ok"] is False
    assert validation["reason"] == "close_advice_report_row_snapshot_hash_mismatch"

def test_empty_sealed_report_is_valid_with_complete_report_binding(tmp_path: Path) -> None:
    from src.application.close_advice_report_manifest import (
        publish_close_advice_report_manifest,
        validate_close_advice_report_manifest,
    )

    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    csv_path = output_dir / "close_advice.csv"
    text_path = output_dir / "close_advice.txt"
    context_path = output_dir / "option_positions_context.json"
    csv_path.write_text(
        "quote_mode,required_data_snapshot_manifest_sha256,close_advice_required_data_plan_sha256\n",
        encoding="utf-8",
    )
    text_path.write_text("", encoding="utf-8")
    context = {"filters": {"account": "lx"}}
    context_path.write_text(json.dumps(context), encoding="utf-8")
    publish_close_advice_report_manifest(
        csv_path=csv_path,
        text_path=text_path,
        context_path=context_path,
        context=context,
        rows=[],
        markets_to_run=["US"],
        run_id="run-empty",
        quote_mode="frozen_snapshot",
        required_data_snapshot_manifest_sha256="a" * 64,
        close_advice_required_data_plan_sha256="b" * 64,
    )

    validation = validate_close_advice_report_manifest(
        csv_path=csv_path,
        expected_run_id="run-empty",
        expected_quote_mode="frozen_snapshot",
    )
    assert validation["ok"] is True
