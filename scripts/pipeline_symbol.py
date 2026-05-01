"""Per-symbol pipeline orchestration.

Stage 3 refactor target: keep run_pipeline as a thin top-level CLI orchestrator.

This module intentionally contains the (large) process_symbol() function extracted
from run_pipeline.py with minimal/no behavioral changes.
"""

from __future__ import annotations

from pathlib import Path

from scripts.exchange_rate_loader import build_converter
from scripts.prefilters import apply_prefilters
from scripts.multiplier_steps import apply_multiplier_cache_to_required_data_csv
from scripts.required_data_steps import ensure_required_data
from scripts.sell_call_steps import empty_sell_call_summary, run_sell_call_scan_and_summarize
from scripts.sell_put_steps import empty_sell_put_summary, run_sell_put_scan_and_summarize
from src.application.symbol_monitoring import (
    SymbolMonitoringDependencies,
    SymbolMonitoringInputs,
    run_symbol_monitoring,
)


def process_symbol(
    py: str,
    base: Path,
    symbol_cfg: dict,
    top_n: int,
    portfolio_ctx: dict | None = None,
    usd_per_cny_exchange_rate: float | None = None,
    cny_per_hkd_exchange_rate: float | None = None,
    timeout_sec: int | None = 120,
    *,
    required_data_dir: Path | None = None,
    report_dir: Path | None = None,
    state_dir: Path | None = None,
    is_scheduled: bool = False,
    runtime_config: dict | None = None,
) -> list[dict]:
    """Thin wrapper around the canonical symbol monitoring use case."""
    if report_dir is None:
        report_dir = base / 'output' / 'reports'
    if required_data_dir is None:
        required_data_dir = base / 'output'
    return run_symbol_monitoring(
        inputs=SymbolMonitoringInputs(
            py=py,
            base=base,
            symbol_cfg=symbol_cfg,
            top_n=top_n,
            portfolio_ctx=portfolio_ctx,
            usd_per_cny_exchange_rate=usd_per_cny_exchange_rate,
            cny_per_hkd_exchange_rate=cny_per_hkd_exchange_rate,
            timeout_sec=timeout_sec,
            required_data_dir=required_data_dir,
            report_dir=report_dir,
            state_dir=state_dir,
            is_scheduled=bool(is_scheduled),
            runtime_config=runtime_config,
        ),
        deps=SymbolMonitoringDependencies(
            build_converter_fn=build_converter,
            apply_prefilters_fn=apply_prefilters,
            apply_multiplier_cache_fn=apply_multiplier_cache_to_required_data_csv,
            ensure_required_data_fn=ensure_required_data,
            run_sell_put_scan_fn=run_sell_put_scan_and_summarize,
            empty_sell_put_summary_fn=empty_sell_put_summary,
            run_sell_call_scan_fn=run_sell_call_scan_and_summarize,
            empty_sell_call_summary_fn=empty_sell_call_summary,
        ),
    )
