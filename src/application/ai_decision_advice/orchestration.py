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
from src.application.ai_decision_advice.contexts import (
    build_frozen_inputs,
    verified_relevant_symbols,
)
from src.application.ai_decision_advice.evidence_store import (
    EvidenceIndex,
    freeze_evidence_index,
)
from src.application.ai_decision_advice.identity import (
    identity_semantic_hash_by_symbol,
    load_symbol_identity_snapshot,
)
from src.application.ai_decision_advice.prompts import (
    PROMPT_PACK_ADVICE,
    compile_prompt_pack,
)
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    validate_opening_candidate_snapshot,
)
from src.application.prepared_portfolio_distribution import (
    PreparedPortfolioDistribution,
)
from src.infrastructure.deepseek_responses import (
    create_deepseek_response,
    extract_output_text,
    extract_usage,
    response_fingerprint,
)


def unavailable_brief_view(reason: str) -> dict[str, Any]:
    """Deterministic unavailable view; never blocks receipts (docs 13.1)."""

    return AdviceRunResult(
        status="unavailable",
        unavailable_reason=reason,
    ).to_brief_view()


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
            output_text=extract_output_text(response),
            usage=extract_usage(response),
            response_sha256=response_fingerprint(response),
        )

    return runner


def run_or_reuse_ai_decision_advice(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    config: Mapping[str, Any] | None,
    candidate_snapshot: Mapping[str, Any] | None,
    portfolio_distribution: (
        PreparedPortfolioDistribution | Mapping[str, Any] | None
    ),
    option_positions_context: Mapping[str, Any] | None,
    candidate_unavailable_reason: str = "candidate_snapshot_missing",
    portfolio_unavailable_reason: str = "portfolio_unavailable",
    option_positions_unavailable_reason: str = (
        "option_positions_unavailable"
    ),
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

    effective_now = now or datetime.now(timezone.utc)
    snapshot = dict(candidate_snapshot or {})
    if not snapshot:
        return unavailable_brief_view(candidate_unavailable_reason)
    try:
        validate_opening_candidate_snapshot(
            snapshot,
            expected_run_id=str(run_id).strip(),
            expected_account=str(account).strip().lower(),
        )
        if str(snapshot.get("market") or "").strip().upper() != str(
            market
        ).strip().upper():
            raise OpeningCandidateSnapshotError(
                "opening candidate snapshot market mismatch"
            )
        _require_advice_evaluable_candidate_snapshot(snapshot)
        relevant_symbols = verified_relevant_symbols(
            snapshot=snapshot,
            portfolio_distribution=portfolio_distribution,
            option_positions_context=option_positions_context,
            market=market,
        )
        identity_hashes = identity_semantic_hash_by_symbol(
            load_symbol_identity_snapshot(Path(base))
        )
        # A symbol without a frozen identity must not accidentally consume
        # evidence produced for an unknown or previous identity generation.
        identity_bindings = {
            symbol: identity_hashes.get(symbol, "0" * 64)
            for symbol in relevant_symbols
        }
        evidence_index = (
            freeze_evidence_index(
                Path(base),
                symbols=relevant_symbols,
                now=effective_now,
                identity_hash_by_symbol=identity_bindings,
            )
            if relevant_symbols
            else EvidenceIndex(
                frozen_at=effective_now.astimezone(timezone.utc).isoformat(),
            )
        )
        frozen = build_frozen_inputs(
            snapshot=snapshot,
            portfolio_distribution=portfolio_distribution,
            option_positions_context=option_positions_context,
            evidence_index=evidence_index,
            market=market,
            evidence_run_id=str(evidence_index.frozen_at or "") or None,
            portfolio_unavailable_reason=portfolio_unavailable_reason,
            option_positions_unavailable_reason=(
                option_positions_unavailable_reason
            ),
        )
    except Exception:
        return unavailable_brief_view("advice_input_unavailable")

    try:
        runner = model_runner
        has_candidates = bool(
            frozen.candidates.get("sell_put")
            or frozen.candidates.get("covered_call")
        )
        if runner is None and has_candidates:
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
            now=effective_now,
            monotonic=monotonic,
            compiled_prompt=compile_prompt_pack(PROMPT_PACK_ADVICE),
        )
    except Exception:
        return unavailable_brief_view("advice_execution_failed")
    view = result.to_brief_view()
    evidence_index_view = result.to_evidence_index_view(frozen)
    if evidence_index_view is not None:
        view["evidence_index"] = evidence_index_view
    return view


def _require_advice_evaluable_candidate_snapshot(
    snapshot: Mapping[str, Any],
) -> None:
    """Accept only complete Candidate Engine outcomes for Advice.

    An empty family is a legal zero candidate only when Candidate Engine
    explicitly sealed it as ``no_candidate``. Market closure, unavailable or
    partial strategy data must not collapse into a synthetic zero-candidate
    Advice result.
    """

    ranked = snapshot.get("ranked_candidates")
    results = snapshot.get("strategy_results")
    if not isinstance(ranked, list) or not isinstance(results, list):
        raise OpeningCandidateSnapshotError(
            "opening candidate snapshot collections are invalid"
        )

    counts = {"put": 0, "call": 0}
    for item in ranked:
        if not isinstance(item, Mapping):
            raise OpeningCandidateSnapshotError(
                "opening candidate snapshot row is invalid"
            )
        mode = str(item.get("strategy_mode") or "").strip().lower()
        if mode not in counts:
            raise OpeningCandidateSnapshotError(
                "opening candidate snapshot strategy mode is unsupported"
            )
        counts[mode] += 1

    statuses = {
        str(item.get("strategy_mode") or "").strip().lower(): str(
            item.get("strategy_status") or ""
        ).strip().lower()
        for item in results
        if isinstance(item, Mapping)
    }
    if set(statuses) != set(counts):
        raise OpeningCandidateSnapshotError(
            "opening candidate snapshot advice strategies are incomplete"
        )
    for mode, count in counts.items():
        expected = "candidates_found" if count else "no_candidate"
        if statuses[mode] != expected:
            raise OpeningCandidateSnapshotError(
                "opening candidate snapshot is not advice-evaluable"
            )

    expected_opening = (
        "candidates_found" if any(counts.values()) else "no_candidate"
    )
    if str(snapshot.get("opening_status") or "").strip().lower() != (
        expected_opening
    ):
        raise OpeningCandidateSnapshotError(
            "opening candidate snapshot is not advice-evaluable"
        )
