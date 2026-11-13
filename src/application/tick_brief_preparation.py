"""Compose one account/market observation, qualification and durable Brief source."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from domain.storage.repositories import state_repo
from src.application.daily_decision_brief_repository import (
    daily_brief_delivery_validation_scope,
    persist_daily_decision_brief_failure,
    persist_daily_decision_brief_success,
)
from src.application.daily_decision_brief_service import (
    assemble_daily_decision_briefs,
    daily_brief_scan_is_reliable,
)
from src.application.multi_tick_audit import daily_brief_timing_scope
from src.infrastructure.futu_gateway import check_fetch_control
from src.infrastructure.io_utils import utc_now


@dataclass(frozen=True)
class TickBriefPreparation:
    markets: tuple[str, ...]
    sources: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    scan_outcomes_by_account: dict[str, bool] = field(default_factory=dict)


def _daily_brief_result_value(result: Any, field: str) -> Any:
    return result.get(field) if isinstance(result, dict) else getattr(result, field, None)


def _daily_brief_result_account(result: Any) -> str:
    return str(_daily_brief_result_value(result, "account") or "").strip().lower()


def write_daily_brief_failure_source(
    *, base: Path, run_id: str, account: str, market: str,
    brief: Mapping[str, Any], result: Any,
) -> tuple[str, str]:
    payload = {
        "schema_version": "daily_decision_brief_scan_failure.v1",
        "run_id": run_id,
        "account": account,
        "market": market,
        "market_trading_date": brief.get("market_trading_date"),
        "recorded_at_utc": utc_now(),
        "decision_reason": _daily_brief_result_value(result, "decision_reason"),
        "status": brief.get("status"),
        "actionability": brief.get("actionability"),
        "data_gaps": brief.get("data_gaps") or [],
    }
    path = state_repo.write_account_run_state(
        base, run_id, account, f"daily_decision_brief_failure.{market}.json", payload,
    )
    return path.resolve().relative_to(base.resolve()).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_tick_briefs(
    *, base: Path, run_id: str, config: dict[str, Any], markets: Sequence[str],
    results: Sequence[Any], ran_pipeline_accounts: Sequence[str],
    scheduled_scan_targets_by_account: Mapping[str, str | None] | None,
    scheduler_decisions_by_account: Mapping[str, dict[str, Any]] | None,
    scheduler_decision: Mapping[str, Any] | None, runlog: Any,
    recovery_intent_factory: Callable[[str, str, dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = None,
    deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
    on_account_prepared: Callable[[str, bool], None] | None = None,
) -> TickBriefPreparation:
    """Prepare each scanned account once; callers separately freeze obligations and commit targets.

    The notification policy may supply an intent factory. The repository invokes
    it in the successful-history transaction and freezes absence on same-run replay.
    """
    check_fetch_control(deadline, cancelled)
    markets = tuple(dict.fromkeys(str(value).strip().upper() for value in markets if str(value).strip()))
    if not markets:
        raise ValueError("daily brief requires at least one scheduler market")
    if len(markets) != 1:
        return TickBriefPreparation(markets=markets)
    pipelines = {str(value).strip().lower() for value in ran_pipeline_accounts}
    targets = {str(account).strip().lower(): target for account, target in
               (scheduled_scan_targets_by_account or {}).items() if target and str(target).strip()}
    sources = {}
    outcomes = {}
    observed_accounts = set()
    with daily_brief_delivery_validation_scope():
        for result in results:
            check_fetch_control(deadline, cancelled)
            account = _daily_brief_result_account(result)
            if not account:
                raise ValueError("daily brief account result is missing account")
            if account in observed_accounts:
                raise ValueError(f"daily brief has duplicate account result: {account}")
            observed_accounts.add(account)
            if not (account in targets or account in pipelines or _daily_brief_result_value(result, "ran_scan") is True):
                continue
            raw_scheduler = (scheduler_decisions_by_account or {}).get(account)
            scheduler = dict(raw_scheduler if isinstance(raw_scheduler, dict) else (scheduler_decision or {}))
            with daily_brief_timing_scope(runlog=runlog, account=account, market=markets[0]):
                briefs = assemble_daily_decision_briefs(
                    base=base, run_id=run_id, account=account, markets_to_run=list(markets),
                    scheduler_decision=scheduler, account_result=result,
                    pipeline_succeeded=account in pipelines, config=config,
                )
                check_fetch_control(deadline, cancelled)
                for market in markets:
                    brief = briefs.get(market)
                    if brief is None:
                        raise ValueError(f"daily brief assembler did not return market {market} for {account}")
                    reliable = daily_brief_scan_is_reliable(brief=brief, pipeline_succeeded=account in pipelines)
                    persisted = None
                    failure_source = None
                    if reliable:
                        factory = (lambda: recovery_intent_factory(account, market, scheduler, brief)) if recovery_intent_factory else None
                        persisted = persist_daily_decision_brief_success(
                            base=base, brief=brief, recovery_intent_factory=factory,
                        )
                    else:
                        brief = persist_daily_decision_brief_failure(base=base, brief=brief)
                        failure_source = write_daily_brief_failure_source(
                            base=base, run_id=run_id, account=account, market=market, brief=brief, result=result,
                        )
                    # Necessary history and failure evidence above stay durable if a
                    # cancellation is observed before notification obligations begin.
                    sources[(account, market)] = {
                        "brief": brief, "pipeline_reliable": reliable,
                        "persisted": persisted, "failure_source": failure_source,
                    }
                    outcomes[account] = outcomes.get(account, True) and reliable
                    # Publish only the qualification whose necessary source is durable;
                    # a later account or cancellation must not erase that fact.
                    if on_account_prepared is not None:
                        on_account_prepared(account, outcomes[account])
                    check_fetch_control(deadline, cancelled)
    return TickBriefPreparation(markets=markets, sources=sources, scan_outcomes_by_account=outcomes)
