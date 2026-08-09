from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.application.ai_decision_advice.advice import (
    AdviceRunResult,
    run_decision_advice,
)
from src.application.ai_decision_advice.collector import ModelCallResult
from src.application.ai_decision_advice.config import (
    ADVICE_ACCOUNT_BUDGET_SECONDS,
    MODEL,
    ai_decision_advice_enabled,
    resolve_api_key,
)
from src.application.ai_decision_advice.contexts import build_frozen_inputs
from src.application.ai_decision_advice.evidence_store import freeze_evidence_index
from src.application.ai_decision_advice.prompts import (
    PROMPT_PACK_ADVICE,
    compile_prompt_pack,
)
from src.infrastructure.deepseek_responses import (
    create_deepseek_response,
    extract_output_text,
    extract_usage,
)


def unavailable_brief_view(reason: str) -> dict[str, Any]:
    """Deterministic unavailable view; never blocks receipts (docs 13.1)."""

    return AdviceRunResult(
        status="unavailable",
        unavailable_reason=reason,
    ).to_brief_view()


def load_json_artifact(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _portfolio_from_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt the prepared portfolio context to the advice freeze input."""

    stocks = context.get("stocks_by_symbol")
    if isinstance(stocks, Mapping):
        return dict(context)
    rows = context.get("stocks") or context.get("holdings")
    if isinstance(rows, list):
        by_symbol: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or row.get("code") or "").strip()
            if not symbol:
                continue
            shares = row.get("shares")
            if shares is None:
                shares = row.get("quantity", row.get("qty"))
            by_symbol[symbol] = {
                "shares": shares,
                "currency": row.get("currency"),
            }
        return {
            "stocks_by_symbol": by_symbol,
            "cash_by_currency": context.get("cash_by_currency"),
        }
    return {}


def _option_lots_from_context(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("position_lots", "lots", "open_positions"):
        rows = context.get(key)
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _build_model_runner(api_key: str) -> Callable[[str, dict, dict | None, int], ModelCallResult]:
    def runner(
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any] | None,
        timeout: int,
    ) -> ModelCallResult:
        response = create_deepseek_response(
            api_key=api_key,
            model=MODEL,
            input_items=[
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            ],
            instructions=instructions,
            enable_web_search=False,
            json_schema={"name": "ai_decision_advice", "schema": schema} if schema else None,
            timeout=max(1, int(timeout)),
        )
        return ModelCallResult(
            raw_response=response,
            output_text=extract_output_text(response),
            usage=extract_usage(response),
        )

    return runner


def run_or_reuse_ai_decision_advice(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    config: Mapping[str, Any] | None,
    state_dir: Path,
    budget_seconds: int = ADVICE_ACCOUNT_BUDGET_SECONDS,
    model_runner: Callable[[str, dict, dict | None, int], ModelCallResult] | None = None,
    now: datetime | None = None,
    monotonic: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run or reuse AI Decision Advice for one account/market (docs 13.1).

    Failures degrade to ``unavailable`` brief views without raising; the tick
    receipt path is never blocked by this node.
    """

    cfg = dict(config or {})
    if not ai_decision_advice_enabled(cfg):
        return AdviceRunResult(status="not_applicable").to_brief_view()

    state_dir = Path(state_dir)
    snapshot = load_json_artifact(state_dir / "opening_candidate_snapshot.json")
    if not snapshot:
        return unavailable_brief_view("candidate_snapshot_missing")

    portfolio_raw = load_json_artifact(state_dir / "portfolio_context.json")
    option_raw = load_json_artifact(state_dir / "option_positions_context.json")
    context_complete = bool(portfolio_raw) and bool(option_raw)

    effective_now = now or datetime.now(timezone.utc)
    evidence_index = freeze_evidence_index(base, now=effective_now)
    frozen = build_frozen_inputs(
        snapshot=snapshot,
        portfolio_context=_portfolio_from_context(portfolio_raw),
        position_lots=_option_lots_from_context(option_raw),
        evidence_index=evidence_index,
        market=market,
        evidence_run_id=str(evidence_index.frozen_at or "") or None,
    )

    runner = model_runner
    if runner is None:
        api_key = resolve_api_key()
        if api_key:
            runner = _build_model_runner(api_key)

    result = run_decision_advice(
        output_root=Path(base),
        run_id=run_id,
        account=account,
        market=market,
        frozen=frozen,
        model_runner=runner,
        budget_seconds=budget_seconds,
        context_complete=context_complete,
        now=effective_now,
        monotonic=monotonic,
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_ADVICE),
    )
    return result.to_brief_view()
