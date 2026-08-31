from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn

from domain.domain.decision_state_fingerprint import canonical_sha256 as _canonical_sha256


RECIPE_ID = "sell_put_option_position_concentration"
RESEARCH_SESSIONS = 20
VALIDATION_SESSIONS = 10
HIDDEN_SNAPSHOT_BATCH_CEILING = 200
ADVANCE_HARD_TIMEOUT_SECONDS = 10
VALIDATION_WAKE_TOLERANCE_SECONDS = 20
TICK_PROTECTION_SECONDS = 20
HIDDEN_SNAPSHOT_LOW_PRIORITY_CALLS_PER_WINDOW = 1
STRATEGY_LAB_ADVANCE_SERVICE = "options-monitor-strategy-lab-advance.service"
STRATEGY_LAB_ADVANCE_TIMER = "options-monitor-strategy-lab-advance.timer"
STRATEGY_LAB_ADVANCE_CALENDARS = (
    "Mon..Fri *-*-* 09..15:*:00 Asia/Hong_Kong",
    "Mon..Fri *-*-* 16..23:00/10:00 Asia/Hong_Kong",
    "Tue..Sat *-*-* 00..08:00/10:00 Asia/Hong_Kong",
)
NEAR_RETURN_THRESHOLDS = (0.002, 0.004, 0.006)
MARKET = "hk"
ACCOUNT = "lx"
STRATEGY = "sell_put"
_LOWER_HEX = frozenset("0123456789abcdef")

EXPERIMENT_STATES = frozenset(
    {
        "research_running",
        "research_complete",
        "awaiting_validation_confirmation",
        "validation_collecting",
        "waiting_outcome",
        "completed",
    }
)
TERMINAL_STATES = frozenset({"completed"})
EXPERIMENT_TRANSITIONS = frozenset(
    {
        ("research_running", "research_complete"),
        ("research_complete", "awaiting_validation_confirmation"),
        ("research_complete", "completed"),
        ("awaiting_validation_confirmation", "validation_collecting"),
        ("validation_collecting", "waiting_outcome"),
        ("waiting_outcome", "completed"),
    }
)
OBSERVATION_KINDS = frozenset(
    {
        "history_k_query",
        "research_fill",
        "expiry_close_query",
        "single_result",
        "validation_point",
        "hidden_batch",
        "validation_fill",
    }
)
OBSERVATION_STATUSES = frozenset(
    {
        "available",
        "simulated_fill",
        "no_fill",
        "not_evaluable",
        "started",
        "complete",
        "observed_fill",
        "pending_outcome",
    }
)

EVALUATOR_OWNER_PATHS = (
    "src/application/strategy_lab/contracts.py",
    "src/application/strategy_lab/recipe.py",
    "src/application/strategy_lab/comparison.py",
    "src/application/strategy_lab/evidence.py",
    "src/application/strategy_lab/readiness.py",
    "src/application/strategy_lab/service.py",
    "src/application/strategy_lab/receipts.py",
    "src/infrastructure/strategy_lab/experiment_store.py",
    "domain/domain/engine/candidate_engine.py",
    "domain/domain/short_vol_assessment.py",
    "domain/domain/option_lifecycle.py",
    "domain/domain/fee_calc.py",
    "domain/domain/performance/models.py",
    "src/application/performance/account_fee_plan.py",
    "src/infrastructure/performance_evidence_sqlite.py",
    "src/application/opend_market_snapshot_fetching.py",
    "src/infrastructure/futu_gateway.py",
    "src/application/opening_candidate_snapshot.py",
    "src/application/prepared_option_positions_context.py",
    "src/application/recommendation_point.py",
    "src/application/research/formal_corpus.py",
)


class StrategyLabContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise StrategyLabContractError(reason_code, message)


def canonical_sha256(value: Any) -> str:
    return _canonical_sha256(value)


def build_strategy_lab_timer_binding() -> dict[str, Any]:
    return {
        "service_name": STRATEGY_LAB_ADVANCE_SERVICE,
        "timer_name": STRATEGY_LAB_ADVANCE_TIMER,
        "calendars": list(STRATEGY_LAB_ADVANCE_CALENDARS),
        "accuracy_sec": "1s",
        "randomized_delay_sec": 0,
        "persistent": False,
        "timeout_start_sec": ADVANCE_HARD_TIMEOUT_SECONDS,
    }


def strict_json_bytes(value: Any) -> bytes:
    """Encode durable JSON without changing JSON number types."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def build_evaluator_behavior_manifest(repo_root: str | Path) -> list[dict[str, str]]:
    root = Path(repo_root).resolve()
    owners: list[dict[str, str]] = []
    for relative in EVALUATOR_OWNER_PATHS:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            _fail(
                "evaluator_owner_unavailable",
                f"evaluator owner is unavailable: {relative}",
            )
        owners.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return owners


def evaluator_behavior_sha256(manifest: object) -> str:
    if (
        not isinstance(manifest, list)
        or [item.get("path") for item in manifest if isinstance(item, dict)]
        != list(EVALUATOR_OWNER_PATHS)
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
            or bool(set(item["sha256"]) - _LOWER_HEX)
            for item in manifest
        )
    ):
        _fail("evaluator_manifest_invalid", "evaluator manifest is invalid")
    return canonical_sha256(manifest)


__all__ = [
    "ACCOUNT",
    "ADVANCE_HARD_TIMEOUT_SECONDS",
    "EVALUATOR_OWNER_PATHS",
    "EXPERIMENT_STATES",
    "EXPERIMENT_TRANSITIONS",
    "HIDDEN_SNAPSHOT_BATCH_CEILING",
    "HIDDEN_SNAPSHOT_LOW_PRIORITY_CALLS_PER_WINDOW",
    "MARKET",
    "NEAR_RETURN_THRESHOLDS",
    "OBSERVATION_KINDS",
    "OBSERVATION_STATUSES",
    "RECIPE_ID",
    "RESEARCH_SESSIONS",
    "STRATEGY_LAB_ADVANCE_CALENDARS",
    "STRATEGY_LAB_ADVANCE_SERVICE",
    "STRATEGY_LAB_ADVANCE_TIMER",
    "STRATEGY",
    "StrategyLabContractError",
    "TERMINAL_STATES",
    "TICK_PROTECTION_SECONDS",
    "VALIDATION_SESSIONS",
    "VALIDATION_WAKE_TOLERANCE_SECONDS",
    "build_evaluator_behavior_manifest",
    "build_strategy_lab_timer_binding",
    "canonical_sha256",
    "evaluator_behavior_sha256",
    "strict_json_bytes",
]
