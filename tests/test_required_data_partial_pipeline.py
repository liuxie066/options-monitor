from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path

import pandas as pd
import pytest

from src.application.opend_symbol_outputs import finalize_required_data_quote_candidate
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
    required_data_plan_id,
)
from src.application.required_data_snapshot import (
    resolve_frozen_required_data_csv_bytes_batch,
    seal_required_data_snapshot,
)
from tests import test_required_data_output_integrity as fixture
from src.application.symbol_monitoring import SymbolMonitoringDependencies
from tests.test_symbol_monitoring_fetch_spec_merge import _inputs, _prefilters_fn


def partial_candidate(*, all_incomplete: bool = False):
    """Real provider envelope with legacy global proof and date-local edge gaps."""
    plan = fixture._fetch_plan()
    expirations = ["2026-08-21", "2026-08-28"]
    plan["require_realized_volatility"] = False
    plan["projected_expirations"] = expirations
    plan["expiration_discovery"]["expirations"] = expirations
    for side in [*plan["side_plans"], *plan["merged_requests"][0]["side_plans"]]:
        side.update({
            "explicit_expirations": expirations, "min_dte": 17, "max_dte": 24,
            "expiration_count": 2, "min_strike": 100.0, "max_strike": 120.0,
            "required_exact_strikes_by_expiration": {},
        })
        side["strike_window"].update({
            "min_strike": 100.0, "max_strike": 120.0,
            "base_min_strike": 100.0, "base_max_strike": 120.0,
        })
    request = plan["merged_requests"][0]
    request.update({
        "explicit_expirations": expirations, "min_dte": 17, "max_dte": 24,
        "include_realized_volatility": False,
        "side_strike_windows": {"put": {"min_strike": 100.0, "max_strike": 120.0}},
    })
    now = datetime.now(timezone.utc)
    payload = fixture._payload()
    template = payload["rows"][0]
    payload["rows"] = []
    for index, expiration in enumerate(expirations):
        strikes = (115.0, 120.0) if index or all_incomplete else (100.0, 105.0, 110.0, 115.0, 120.0)
        for strike in strikes:
            row = deepcopy(template)
            row.update({
                "expiration": expiration, "dte": 17 + 7 * index, "strike": strike,
                "contract_symbol": f"NVDA{expiration.replace('-', '')[2:]}P{int(strike * 1000):08d}",
            })
            payload["rows"].append(row)
    codes = [row["contract_symbol"] for row in payload["rows"]]
    payload.update({"expirations": expirations, "expiration_count": 2})
    payload["meta"].update({
        "source_observed_at": now.isoformat(), "completed_at_utc": now.isoformat(),
        "snapshot_requested_codes": len(codes), "snapshot_returned_codes": len(codes),
        "snapshot_requested_code_set": codes, "snapshot_returned_code_set": codes,
        "realized_volatility": {"status": "skipped", "reason": "not_requested"},
    })
    contract = build_required_data_expected_fetch_contract(
        symbol="NVDA", fetch_plan=plan, source="opend", host=fixture.HOST, port=fixture.PORT,
    )
    return payload, contract, now


def sealed_partial(tmp_path: Path):
    payload, contract, now = partial_candidate()
    run_root = tmp_path / "output_runs" / "run-partial"
    root = run_root / "required_data"
    finalized = finalize_required_data_quote_candidate(
        base=tmp_path, producer_root=root, producer_run_id="run-partial", symbol="NVDA",
        expected_fetch_contract=contract, fetch_policy=fixture._policy(), mode="fresh",
        payload=payload, now=now,
    )
    item = {
        "symbol": "NVDA", "source": "opend", "fetch_binding": contract["fetch_binding"],
        "fetch_plan": contract["fetch_plan"], "expected_fetch_contract": contract,
        "projection_outcome": "success_rows", "discovery_status": "complete",
    }
    summary = {"global_required_data_plan": {
        "plan_id": required_data_plan_id([item]), "symbols": [item],
        "symbols_count": 1, "discovery_complete": True,
    }}
    path = run_root / "state" / "required_data_snapshot_manifest.json"
    manifest = seal_required_data_snapshot(
        manifest_path=path, required_data_root=root, run_id="run-partial",
        prefetch_summary=summary, sealed_at=now,
    )
    batch = resolve_frozen_required_data_csv_bytes_batch(
        manifest_path=path, expected_run_id="run-partial", required_data_root=root,
        require_fresh=False,
    )
    return root, path, manifest, batch, finalized


def test_partial_receipt_seals_without_rewriting_or_fabricating_rows(tmp_path: Path) -> None:
    root, _path, manifest, batch, finalized = sealed_partial(tmp_path)
    assert manifest["status"] == "partial"
    assert manifest["summary"] == {"symbols_total": 1, "ready": 1, "failed": 0}
    coverage = manifest["symbols"]["NVDA"]["required_data_coverage"]
    assert coverage["covered_expirations"] == ["2026-08-21"]
    assert coverage["incomplete_expirations"] == ["2026-08-28"]
    evidence, csv_bytes = batch.resolve("NVDA")
    assert evidence["required_data_coverage"] == coverage
    raw = json.loads((root / "raw" / "NVDA_required_data.json").read_bytes())
    assert len(raw["rows"]) == len(pd.read_csv(BytesIO(csv_bytes))) == 7
    assert finalized["evidence"]["required_data_coverage"] == coverage


def test_runtime_snapshot_reader_preserves_partial_required_data_manifest(tmp_path: Path) -> None:
    from src.application.runtime_portfolio_snapshot import _validate_required_data_reference

    _root, _path, manifest, _batch, _finalized = sealed_partial(tmp_path)
    _validate_required_data_reference(
        manifest, binding={"content_sha256": manifest["content_sha256"]},
        expected_run_id="run-partial",
    )


def test_all_incomplete_dates_never_publish_receipt(tmp_path: Path) -> None:
    payload, contract, now = partial_candidate(all_incomplete=True)
    from src.application.source_receipts import SourceReceiptError

    with pytest.raises(SourceReceiptError, match="invalid_row_identity.*covered_expirations"):
        finalize_required_data_quote_candidate(
            base=tmp_path, producer_root=tmp_path, producer_run_id="run-partial", symbol="NVDA",
            expected_fetch_contract=contract, fetch_policy=fixture._policy(), mode="fresh",
            payload=payload, now=now,
        )
    assert not list(tmp_path.glob("source_receipts/**/receipt.json"))


def test_frozen_partial_dates_reach_candidate_runner_and_publish_partial_status(tmp_path: Path) -> None:
    from src.application.symbol_monitoring import run_symbol_monitoring

    root, path, _manifest, batch, _finalized = sealed_partial(tmp_path)
    seen = []

    def scan(**kwargs):
        frame = kwargs["required_data_frame"]
        seen.extend(frame.to_dict("records"))
        kwargs["final_candidates_sink_fn"]("put", [{"expiration": frame.iloc[0]["expiration"]}])
        return {"symbol": "NVDA", "candidate_count": 1,
                "_strategy_status": "completed", "_strategy_reason": None}

    deps = SymbolMonitoringDependencies(
        build_converter_fn=lambda **_kwargs: object(), apply_prefilters_fn=_prefilters_fn,
        apply_multiplier_cache_fn=lambda **_kwargs: pytest.fail("frozen bytes mutated"),
        ensure_required_data_fn=lambda **_kwargs: pytest.fail("frozen data refetched"),
        run_sell_put_scan_fn=scan, empty_sell_put_summary_fn=lambda *_args, **_kwargs: {},
        run_sell_call_scan_fn=lambda **_kwargs: None,
        empty_sell_call_summary_fn=lambda *_args, **_kwargs: {},
        run_combo_yield_scan_fn=lambda **_kwargs: None,
        empty_combo_yield_summary_fn=lambda *_args, **_kwargs: {},
    )
    report = tmp_path / "reports"
    rows = run_symbol_monitoring(
        inputs=_inputs(
            tmp_path, symbol_cfg={"symbol": "NVDA", "sell_put": {"enabled": True},
                                  "sell_call": {"enabled": False}},
            required_data_dir=root, report_dir=report, is_scheduled=True,
            source_producer_run_id="run-partial", required_data_snapshot_run_id="run-partial",
            required_data_snapshot_manifest=path, required_data_snapshot_batch=batch,
            runtime_config={"portfolio": {"account": "lx"}},
        ), deps=deps,
    )
    assert len(seen) == 5 and {row["expiration"] for row in seen} == {"2026-08-21"}
    assert rows[0]["candidate_count"] == 1
    assert rows[0]["required_data_status"] == "incomplete"
    status = json.loads(next(report.glob("*_scan_status.v3.json")).read_text())
    assert status["status"] == "completed" and status["reason"] == "partial_data"
    assert status["required_data_coverage"]["incomplete_expirations"] == ["2026-08-28"]


def test_close_and_roll_quotes_exclude_rejected_dates(tmp_path: Path) -> None:
    from src.application.close_advice_runner import _load_frozen_required_data_quotes

    _root, _path, _manifest, batch, _finalized = sealed_partial(tmp_path)
    evidence, csv_bytes = batch.resolve("NVDA")
    quotes = _load_frozen_required_data_quotes(
        csv_bytes_by_symbol={"NVDA": csv_bytes}, base_dir=tmp_path,
        provenance_by_symbol={"NVDA": evidence},
    )
    assert len(quotes) == 5
    assert {key[2] for key in quotes} == {"2026-08-21"}
