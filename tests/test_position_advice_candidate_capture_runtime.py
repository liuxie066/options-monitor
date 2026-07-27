from __future__ import annotations

import json
from pathlib import Path

from src.application import pipeline_context, pipeline_symbol, report_builders
from src.application.pipeline_watchlist import run_watchlist_pipeline_default


def _config() -> dict:
    return {
        "portfolio": {"account": "acct-a"},
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_annualized_net_return": 0.1,
                },
                "sell_call": {"enabled": False},
            }
        ],
        "templates": {},
        "runtime": {},
    }


def _patch_pipeline_dependencies(monkeypatch, process_symbol_fn) -> None:
    monkeypatch.setattr(pipeline_symbol, "process_symbol", process_symbol_fn)
    monkeypatch.setattr(
        pipeline_context,
        "build_pipeline_context",
        lambda **_kwargs: ({}, None, None, None),
    )
    monkeypatch.setattr(
        report_builders,
        "build_symbols_summary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        report_builders,
        "build_symbols_digest",
        lambda *_args, **_kwargs: None,
    )


def test_account_pipeline_writes_complete_candidate_capture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def _process_symbol(*args, **kwargs):
        sink = kwargs["all_decisions_sink_fn"]
        status_sink = kwargs["candidate_capture_status_sink_fn"]
        sink(
            [
                {
                    "schema_version": "candidate_all_decisions.v1",
                    "candidate_id": "candidate-1",
                    "strategy_mode": "put",
                    "quote_snapshot_id": "a" * 64,
                }
            ]
        )
        status_sink(
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
                "reason": "all_decisions_captured",
                "quote_snapshot_id": "a" * 64,
                "quote_receipt_relpath": "quotes/nvda/receipt.json",
            }
        )
        return [
            {
                "symbol": str(args[2]["symbol"]),
                "strategy": "sell_put",
                "candidate_count": 1,
            }
        ]

    _patch_pipeline_dependencies(monkeypatch, _process_symbol)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    run_watchlist_pipeline_default(
        py="python",
        base=tmp_path,
        cfg=_config(),
        report_dir=tmp_path / "reports",
        state_dir=state_dir,
        shared_state_dir=None,
        required_data_dir=tmp_path / "required_data",
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=False,
        symbols_arg=None,
        log=lambda _message: None,
        want_fn=lambda _step: True,
        position_advice_account_run_id="run-1",
    )

    capture = json.loads(
        (state_dir / "position_advice_candidate_all_decisions.raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert capture["complete"] is True
    assert capture["account_run_id"] == "run-1"
    assert capture["expected_scan_scopes"] == ["NVDA:put"]
    assert capture["candidate_count"] == 1
    assert capture["missing_scan_scopes"] == []


def test_account_pipeline_marks_missing_scan_completion_incomplete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def _process_symbol(*args, **_kwargs):
        return [
            {
                "symbol": str(args[2]["symbol"]),
                "strategy": "sell_put",
                "candidate_count": 0,
            }
        ]

    _patch_pipeline_dependencies(monkeypatch, _process_symbol)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    run_watchlist_pipeline_default(
        py="python",
        base=tmp_path,
        cfg=_config(),
        report_dir=tmp_path / "reports",
        state_dir=state_dir,
        shared_state_dir=None,
        required_data_dir=tmp_path / "required_data",
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=False,
        symbols_arg=None,
        log=lambda _message: None,
        want_fn=lambda _step: True,
        position_advice_account_run_id="run-1",
    )

    capture = json.loads(
        (state_dir / "position_advice_candidate_all_decisions.raw.json").read_text(
            encoding="utf-8"
        )
    )
    assert capture["complete"] is False
    assert capture["missing_scan_scopes"] == ["NVDA:put"]
