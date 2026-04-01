"""Pipeline stage runner.

Why:
- Keep run_pipeline orchestration-only (Stage 3).
- Centralize stage selection semantics for --stage / --stage-only.

Design:
- No implicit globals; all runtime flags are explicit args.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class StagePlan:
    stage: str
    stage_only: str | None

    def want(self, name: str) -> bool:
        if self.stage_only is not None:
            return name == self.stage_only
        if self.stage == 'all':
            return True
        order = ['fetch', 'scan', 'alert', 'notify']
        try:
            return order.index(name) <= order.index(self.stage)
        except ValueError:
            return True


def build_stage_plan(*, stage: str, stage_only: str | None) -> StagePlan:
    return StagePlan(stage=str(stage), stage_only=(str(stage_only) if stage_only else None))


def run_pipeline(
    *,
    py: str,
    base: Path,
    cfg: dict,
    report_dir: Path,
    is_scheduled: bool,
    stage: str,
    stage_only: str | None,
    top_n: int,
    symbol_timeout_sec: int,
    portfolio_timeout_sec: int,
    no_context: bool,
    symbols_arg: str | None,
    log: Callable[[str], None],
    build_pipeline_context_fn,
    run_watchlist_pipeline_fn,
    run_stage_only_alert_notify_fn,
    build_symbols_summary_fn,
    build_symbols_digest_fn,
    apply_profiles_fn,
    process_symbol_fn,
) -> list[dict]:
    plan = build_stage_plan(stage=stage, stage_only=stage_only)

    if plan.stage_only is not None:
        run_stage_only_alert_notify_fn(
            py=py,
            base=base,
            report_dir=report_dir,
            is_scheduled=is_scheduled,
            stage_only=plan.stage_only,
            want=plan.want,
            log=log,
        )
        return []

    return run_watchlist_pipeline_fn(
        py=py,
        base=base,
        cfg=cfg,
        report_dir=report_dir,
        is_scheduled=is_scheduled,
        top_n=top_n,
        symbol_timeout_sec=symbol_timeout_sec,
        portfolio_timeout_sec=portfolio_timeout_sec,
        want_scan=plan.want('scan'),
        no_context=bool(no_context),
        symbols_arg=symbols_arg,
        log=log,
        want_fn=plan.want,
        apply_profiles_fn=apply_profiles_fn,
        process_symbol_fn=process_symbol_fn,
        build_pipeline_context_fn=build_pipeline_context_fn,
        build_symbols_summary_fn=build_symbols_summary_fn,
        build_symbols_digest_fn=build_symbols_digest_fn,
    )
