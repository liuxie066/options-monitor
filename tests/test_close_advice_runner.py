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


@pytest.fixture
def partial_close_quotes(tmp_path: Path) -> tuple[dict, dict]:
    from src.application import close_advice_runner as runner
    from tests.test_required_data_partial_pipeline import partial_candidate, fixture
    from src.application.opend_symbol_outputs import finalize_required_data_quote_candidate

    payload, contract, now = partial_candidate(all_incomplete=True)
    finalized = finalize_required_data_quote_candidate(
        base=tmp_path, producer_root=tmp_path, producer_run_id="run-partial", symbol="NVDA",
        expected_fetch_contract=contract, fetch_policy=fixture._policy(), mode="fresh", payload=payload, now=now,
    )
    provenance = {"NVDA": {"required_data_coverage": finalized["evidence"]["required_data_coverage"]}}
    quotes = runner._load_frozen_required_data_quotes(
        csv_bytes_by_symbol={"NVDA": pd.DataFrame(payload["rows"]).to_csv(index=False).encode("utf-8")},
        base_dir=tmp_path, provenance_by_symbol=provenance,
    )
    return quotes, provenance


def test_partial_range_quotes_retain_own_date_gaps_with_no_complete_dates(partial_close_quotes) -> None:
    quotes, provenance = partial_close_quotes
    assert provenance["NVDA"]["required_data_coverage"]["covered_expirations"] == []
    assert len(quotes) == 4
    for key, quote in quotes.items():
        own = quote["required_data_coverage"]
        assert own["status"] == "partial"
        assert own["covered_expirations"] == []
        assert own["incomplete_expirations"] == [key[2]]
        detail = own["per_expiration"][0]
        assert detail["expiration"] == key[2]
        assert detail["status"] == "incomplete"
        assert detail["missing_ranges"] == [[100.0, 115.0]]
        assert detail["missing_range_inclusive"] == [[True, False]]
        assert quote["required_data_expiration_status"] == "incomplete"
        assert quote["ranking_complete"] is False
        assert quote["ranking_scope"] == "observed_contracts"
        assert quote["partial_candidate_policy"] == "retain_and_mark"


def test_partial_close_provenance_survives_sealed_csv_and_public_reader(
    tmp_path: Path, partial_close_quotes,
) -> None:
    import csv
    from io import StringIO

    from src.application import close_advice_runner as runner
    from src.application.agent_tools import close_advice_read_impl as reader
    from src.application.close_advice_report_manifest import (
        publish_close_advice_report_manifest, read_close_advice_report_snapshot,
    )

    quotes, provenance = partial_close_quotes
    key = next(iter(quotes))
    position = {
        "account": "lx", "lot_id": "lot-partial", "symbol": "NVDA",
        "option_type": "put", "side": "short", "expiration": key[2],
        "strike": float(key[3]), "multiplier": 100, "contracts_open": 1,
        "currency": "USD", "broker": "富途",
    }
    row = runner._apply_required_data_row_provenance(
        {"account": "lx", "symbol": "NVDA", "expiration": key[2], "option_type": "put"},
        position=position, quote_key=key, frozen_manifest={"plan_id": "partial-plan"},
        frozen_manifest_sha256="a" * 64, frozen_plan_sha256="b" * 64,
        requirements_by_lot={}, provenance_by_symbol=provenance,
    )
    coverage = row["required_data_coverage"]
    assert coverage == quotes[key]["required_data_coverage"]
    row["roll_advice"] = {"status": "partial", "coverage": {"required_data_coverage": coverage}}
    csv_path = tmp_path / "reports" / "close_advice.csv"
    text_path = csv_path.with_suffix(".txt")
    runner._write_csv(csv_path, [row])
    text_path.write_text("sealed partial coverage\n", encoding="utf-8")
    raw = next(csv.DictReader(StringIO(csv_path.read_text(encoding="utf-8"))))
    assert json.loads(raw["required_data_coverage"]) == coverage
    assert json.loads(raw["roll_advice"]) == row["roll_advice"]
    publish_close_advice_report_manifest(
        csv_path=csv_path, text_path=text_path, context_path=tmp_path / "unused.json",
        context={"filters": {"account": "lx"}, "open_positions_min": []}, rows=[row],
        markets_to_run=["US"], run_id="run-sealed", quote_mode="frozen_snapshot",
        required_data_snapshot_manifest_sha256="a" * 64,
        close_advice_required_data_plan_sha256="b" * 64,
    )
    snapshot = read_close_advice_report_snapshot(csv_path=csv_path, expected_quote_mode="frozen_snapshot")
    assert snapshot["validation"]["ok"] is True
    source = reader._Source(csv_path, source_type="explicit", account="lx")
    source.csv_bytes = snapshot["csv_bytes"]
    decoded = reader._read_rows(source)[0]
    assert decoded["required_data_coverage"] == coverage
    assert decoded["roll_advice"] == row["roll_advice"]
    public = reader._public_row(decoded)
    assert public["required_data_coverage"] == coverage
    assert public["required_data_expiration_status"] == "incomplete"
    assert public["ranking_complete"] is False
    assert public["ranking_scope"] == "observed_contracts"
    assert public["partial_candidate_policy"] == "retain_and_mark"


@pytest.mark.parametrize("field", ["required_data_coverage", "roll_advice"])
@pytest.mark.parametrize("value", ["{broken", "[]", "null"])
def test_close_reader_rejects_invalid_structured_provenance(tmp_path: Path, field: str, value: str) -> None:
    import csv
    from io import StringIO

    from src.application.agent_tool_contracts import AgentToolError
    from src.application.agent_tools import close_advice_read_impl as reader

    csv_bytes = StringIO()
    writer = csv.DictWriter(csv_bytes, fieldnames=["account", field])
    writer.writeheader()
    writer.writerow({"account": "lx", field: value})
    source = reader._Source(tmp_path / "close_advice.csv", source_type="explicit", account="lx")
    source.csv_bytes = csv_bytes.getvalue().encode("utf-8")
    with pytest.raises(AgentToolError) as error:
        reader._read_rows(source)
    assert error.value.code == "READ_ERROR"
