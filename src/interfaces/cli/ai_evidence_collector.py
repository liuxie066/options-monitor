from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.ai_decision_advice.collector import (
    ModelCallResult,
    compute_cutoffs,
    run_evidence_collector,
)
from src.application.ai_decision_advice.config import (
    MODEL,
    ai_decision_advice_enabled,
    resolve_api_key,
)
from src.application.ai_decision_advice.evidence_store import read_evidence_records
from src.application.ai_decision_advice.identity import (
    RefreshQueue,
    build_observation_set,
    build_symbol_identity_snapshot,
    candidate_symbols_from_snapshot,
    publish_symbol_identity_snapshot,
)
from src.application.ai_decision_advice.prompts import (
    PROMPT_PACK_EVIDENCE,
    compile_prompt_pack,
)
from src.application.runtime_paths import resolve_runtime_root
from src.infrastructure.deepseek_responses import (
    create_deepseek_response,
    extract_output_text,
    extract_usage,
    response_fingerprint,
    summarize_web_search_calls,
)


def _evidence_model_runner(api_key: str):
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
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
            ],
            instructions=instructions,
            enable_web_search=True,
            json_schema={"name": "external_evidence", "schema": schema} if schema else None,
            timeout=max(1, int(timeout)),
        )
        return ModelCallResult(
            output_text=extract_output_text(response),
            usage=extract_usage(response),
            response_sha256=response_fingerprint(response),
            web_search_audit=summarize_web_search_calls(response),
        )

    return runner


def _observation_symbols(configs: list[dict[str, Any]]) -> list[str]:
    symbols: list[str] = []
    for cfg in configs:
        for key in ("symbols", "scan_symbols", "watchlist"):
            rows = cfg.get(key)
            if isinstance(rows, list):
                symbols.extend(str(item) for item in rows if str(item or "").strip())
    return symbols


def run_collector(
    *,
    config_keys: list[str],
    runtime_root: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    configs: list[dict[str, Any]] = []
    for key in config_keys:
        _path, cfg = load_runtime_config(config_key=key, expected_market=key.lower())
        configs.append(dict(cfg))
    if not any(ai_decision_advice_enabled(cfg) for cfg in configs):
        return {"status": "skipped", "reason": "ai_decision_advice_disabled"}

    api_key = resolve_api_key()
    if not api_key and not dry_run:
        return {"status": "failed", "reason": "missing_api_key"}

    observed = build_observation_set(
        scan_symbols=_observation_symbols(configs),
        stock_holding_symbols=[],
        open_option_underlyings=[],
        recent_candidate_symbols=[],
    )
    now = datetime.now(timezone.utc)
    snapshot = build_symbol_identity_snapshot(observed, observed_at=now)
    queue = RefreshQueue.build(observed)

    last_success: dict[str, str | None] = {}
    for record in read_evidence_records(runtime_root):
        if record.get("kind") == "symbol_status" and record.get("last_success_at"):
            symbol = str(record.get("symbol") or "")
            if symbol:
                last_success[symbol] = str(record["last_success_at"])
    cutoffs = compute_cutoffs(
        {symbol: last_success.get(symbol) for symbol in queue.symbols()},
        now=now,
    )

    result: dict[str, Any] = {
        "status": "dry_run",
        "observation_count": len(queue.symbols()),
        "cutoff_count": len(cutoffs),
    }
    if dry_run:
        return result

    publish_symbol_identity_snapshot(base=runtime_root, payload=snapshot)
    summary = run_evidence_collector(
        base=runtime_root,
        queue_symbols=queue.symbols(),
        identity_snapshot=snapshot,
        cutoff_by_symbol=cutoffs,
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_EVIDENCE),
        model_runner=_evidence_model_runner(str(api_key)),
        evidence_run_id=f"ev-{now.strftime('%Y%m%dT%H%M%SZ')}",
        now=now,
    )
    result.update(
        {
            "status": "completed",
            "summary": {
                "budget_seconds": summary.budget_seconds,
                "budget_exhausted": summary.budget_exhausted,
                "completed_count": len(summary.completed_symbols),
                "failed_count": len(summary.failed_symbols),
                "unfinished_count": len(summary.unfinished_symbols),
                "repair_attempts": summary.repair_attempts,
                "records_appended": summary.records_appended,
            },
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="om ai-evidence-collector",
        description="refresh external evidence for AI Decision Advice",
    )
    parser.add_argument(
        "--config-key",
        action="append",
        dest="config_keys",
        default=None,
        help="runtime config key (repeatable); default: us hk",
    )
    parser.add_argument("--dry-run", action="store_true", help="plan only; no model calls")
    args = parser.parse_args(argv)

    repo_root = repo_base()
    runtime_root = resolve_runtime_root(repo_root=repo_root).runtime_root
    config_keys = args.config_keys or ["us", "hk"]
    try:
        result = run_collector(
            config_keys=config_keys,
            runtime_root=runtime_root,
            dry_run=bool(args.dry_run),
        )
    except Exception as exc:  # collector failure must surface but not crash timer
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "collector_error",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"completed", "skipped", "dry_run"} else 1


if __name__ == "__main__":
    sys.exit(main())
