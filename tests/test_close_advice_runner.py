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


def _position(
    *,
    lot_id: str = "lot-nvda-1",
    symbol: str = "NVDA",
    option_type: str = "put",
    side: str = "short",
    strike: float = 100.0,
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
        "expiration": EXPIRATION,
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


def test_disabled_close_advice_invalidates_matching_old_empty_report(
    tmp_path: Path,
) -> None:
    from src.application.close_advice_report_manifest import (
        validate_close_advice_report_manifest,
    )

    first, output_dir = _run(tmp_path, positions=[], quotes=[])
    assert first["report_manifest"]["status"] == "success"

    second = run_close_advice(
        config={"close_advice": {"enabled": False}},
        context_path=tmp_path / "option_positions_context.json",
        required_data_root=tmp_path / "required_data",
        output_dir=output_dir,
        base_dir=Path.cwd(),
    )

    assert second["status"] == "disabled"
    validation = validate_close_advice_report_manifest(
        csv_path=output_dir / "close_advice.csv",
    )
    assert validation["ok"] is False
    assert validation["reason"] == "close_advice_manifest_not_success"
    assert validation["status"] == "failed"


def test_disabled_close_advice_invalidates_manifest_before_writing_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application import close_advice_runner as runner
    from src.application.close_advice_report_manifest import (
        validate_close_advice_report_manifest,
    )

    first, output_dir = _run(tmp_path, positions=[], quotes=[])
    assert first["report_manifest"]["status"] == "success"
    original_write_csv = runner._write_csv
    observed_status: list[str] = []

    def _observed_write_csv(path: Path, rows: list[dict]) -> None:
        validation = validate_close_advice_report_manifest(csv_path=path)
        observed_status.append(str(validation.get("status") or ""))
        assert validation["reason"] == "close_advice_manifest_not_success"
        original_write_csv(path, rows)

    monkeypatch.setattr(runner, "_write_csv", _observed_write_csv)

    run_close_advice(
        config={"close_advice": {"enabled": False}},
        context_path=tmp_path / "option_positions_context.json",
        required_data_root=tmp_path / "required_data",
        output_dir=output_dir,
        base_dir=Path.cwd(),
    )

    assert observed_status == ["failed"]


def test_report_snapshot_returns_the_exact_validated_bytes(
    tmp_path: Path,
) -> None:
    from src.application.close_advice_report_manifest import (
        publish_close_advice_report_manifest,
        read_close_advice_report_snapshot,
        validate_close_advice_report_manifest,
    )

    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    csv_path = output_dir / "close_advice.csv"
    text_path = output_dir / "close_advice.txt"
    context_path = output_dir / "option_positions_context.json"
    rows = [{"account": "lx", "symbol": "NVDA"}]
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
    )

    snapshot = read_close_advice_report_snapshot(
        csv_path=csv_path,
        desired_market="US",
        account="lx",
        expected_run_id="run-1",
        expected_quote_mode="frozen_snapshot",
    )
    csv_path.write_text("account,symbol\nlx,TSLA\n", encoding="utf-8")
    text_path.write_text("TSLA\n", encoding="utf-8")

    assert snapshot["validation"]["ok"] is True
    assert b"NVDA" in snapshot["csv_bytes"]
    assert snapshot["text_bytes"] == b"NVDA\n"
    assert (
        validate_close_advice_report_manifest(csv_path=csv_path)["reason"]
        == "close_advice_report_bytes_mismatch"
    )


def test_legacy_run_failure_invalidates_old_success_report(
    tmp_path: Path,
) -> None:
    from src.application.close_advice_report_manifest import (
        validate_close_advice_report_manifest,
    )

    first, output_dir = _run(tmp_path, positions=[], quotes=[])
    assert first["report_manifest"]["status"] == "success"
    context_path = tmp_path / "option_positions_context.json"
    context_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="open_positions_min"):
        run_close_advice(
            config={
                "close_advice": {
                    "enabled": True,
                    "quote_source": "required_data",
                }
            },
            context_path=context_path,
            required_data_root=tmp_path / "required_data",
            output_dir=output_dir,
            base_dir=Path.cwd(),
        )

    validation = validate_close_advice_report_manifest(
        csv_path=output_dir / "close_advice.csv",
    )
    assert validation["ok"] is False
    assert validation["reason"] == "close_advice_manifest_not_success"
    assert validation["status"] == "pending"


def test_context_override_is_the_only_position_snapshot_evaluated(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "option_positions_context.json"
    required_data_root = tmp_path / "required_data"
    output_dir = tmp_path / "reports"
    _write_context(context_path, [_position(symbol="TSLA")])
    _write_quotes(required_data_root, [_quote(symbol="NVDA")])
    validated_context = {
        "open_positions_min": [_position(symbol="NVDA")],
    }

    result = run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=context_path,
        context_override=validated_context,
        required_data_root=required_data_root,
        output_dir=output_dir,
        base_dir=Path.cwd(),
    )

    assert result["rows"] == 1
    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0]
    assert row["symbol"] == "NVDA"
    assert "TSLA" not in result["notification_text"]


def test_strict_close_row_is_the_only_notified_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_business_date(monkeypatch)
    result, output_dir = _run(
        tmp_path,
        positions=[_position()],
        quotes=[_quote()],
    )

    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0]
    assert result["rows"] == 1
    assert result["notify_rows"] == 1
    assert row["policy_version"] == "strict_profit_capture.v1"
    assert row["recommendation_state"] == "close"
    assert row["net_capture_ratio"] >= 0.90
    assert row["close_cost_ratio"] <= 0.001
    assert row["remaining_term_ratio"] >= 0.50
    assert "NVDA Put 2026-06-15" in (
        output_dir / "close_advice.txt"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("quote", "expected_state"),
    [
        (_quote(bid=0.45, ask=0.50), "hold"),
        (_quote(bid=0.02, ask=None), "not_evaluable"),
    ],
)
def test_non_close_states_are_recorded_but_not_notified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    quote: dict,
    expected_state: str,
) -> None:
    _freeze_business_date(monkeypatch)
    result, output_dir = _run(
        tmp_path,
        positions=[_position()],
        quotes=[quote],
    )

    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0]
    assert row["recommendation_state"] == expected_state
    assert result["notify_rows"] == 0
    assert (output_dir / "close_advice.txt").read_text(encoding="utf-8") == ""


def test_fractional_multiplier_fails_closed_instead_of_truncating_fee_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_business_date(monkeypatch)
    position = _position()
    position["multiplier"] = 100.5

    result, output_dir = _run(
        tmp_path,
        positions=[position],
        quotes=[_quote()],
    )

    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0]
    assert row["recommendation_state"] == "not_evaluable"
    assert "fee_evidence_unavailable" in row["data_quality_flags"]
    assert result["notify_rows"] == 0


@pytest.mark.parametrize(
    ("field", "expected_flag"),
    [
        ("multiplier", "missing_multiplier"),
        ("opened_at", "missing_original_dte"),
    ],
)
def test_boolean_position_evidence_fails_closed_before_domain_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    expected_flag: str,
) -> None:
    _freeze_business_date(monkeypatch)
    position = _position()
    position[field] = True

    result, output_dir = _run(
        tmp_path,
        positions=[position],
        quotes=[_quote()],
    )

    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0]
    assert row["recommendation_state"] == "not_evaluable"
    assert expected_flag in row["data_quality_flags"]
    assert result["notify_rows"] == 0


def test_long_options_are_outside_close_advice_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_business_date(monkeypatch)
    result, output_dir = _run(
        tmp_path,
        positions=[_position(side="long")],
        quotes=[_quote()],
    )

    assert result["rows"] == 0
    assert result["notify_rows"] == 0
    assert pd.read_csv(output_dir / "close_advice.csv").empty


def test_notification_limit_does_not_change_policy_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_business_date(monkeypatch)
    positions = [
        _position(lot_id="lot-a", symbol="NVDA", strike=100),
        _position(lot_id="lot-b", symbol="AMD", strike=90),
    ]
    quotes = [
        _quote(symbol="NVDA", strike=100, spot=120),
        _quote(symbol="AMD", strike=90, spot=110),
    ]
    result, output_dir = _run(
        tmp_path,
        positions=positions,
        quotes=quotes,
        max_items_per_account=1,
    )

    rows = pd.read_csv(output_dir / "close_advice.csv")
    assert set(rows["recommendation_state"]) == {"close"}
    assert result["rows"] == 2
    assert result["notify_rows"] == 1


def test_malformed_context_does_not_replace_last_good_report(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "option_positions_context.json"
    context_path.write_text("{not-json", encoding="utf-8")
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    csv_path = output_dir / "close_advice.csv"
    text_path = output_dir / "close_advice.txt"
    csv_path.write_text("last-known-good-csv", encoding="utf-8")
    text_path.write_text("last-known-good-text", encoding="utf-8")

    with pytest.raises(ValueError, match="missing or malformed"):
        run_close_advice(
            config={"close_advice": {"enabled": True}},
            context_path=context_path,
            required_data_root=tmp_path / "required_data",
            output_dir=output_dir,
            base_dir=Path.cwd(),
        )

    assert csv_path.read_text(encoding="utf-8") == "last-known-good-csv"
    assert text_path.read_text(encoding="utf-8") == "last-known-good-text"


def test_auto_quote_refresh_uses_default_futu_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.opend_symbol_fetching as opend_symbol_fetching
    from src.application.close_advice_runner import (
        _fetch_missing_quotes_via_opend,
        _quote_key,
    )

    position = _position()
    key = _quote_key(
        position["symbol"],
        position["option_type"],
        position["expiration"],
        position["strike"],
        base_dir=tmp_path,
    )
    quotes = {key: _quote(bid=None, ask=None)}
    calls: list[dict] = []

    def _fetch_symbol(_symbol: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {"rows": [_quote()]}

    monkeypatch.setattr(opend_symbol_fetching, "fetch_symbol", _fetch_symbol)

    reasons, details = _fetch_missing_quotes_via_opend(
        config={
            "close_advice": {"quote_source": "auto"},
            "symbols": [{"symbol": "NVDA", "fetch": {}}],
        },
        positions=[position],
        quotes=quotes,
        covered_keys={key},
        base_dir=tmp_path,
    )

    assert reasons == {}
    assert details[key]["requested_symbol"] == "NVDA"
    assert quotes[key]["bid"] == 0.018
    assert quotes[key]["ask"] == 0.02
    assert len(calls) == 1
