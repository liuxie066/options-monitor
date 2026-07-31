from __future__ import annotations

import gzip
import json
import shutil
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from domain.domain.config_contract import RUNTIME_SCHEDULE_TIMEZONE_BY_MARKET
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice import decimal_value
from domain.domain.position_advice_authority import (
    PROMOTABLE_STRATEGY_FAMILIES,
    normalize_account_label,
    normalize_portfolio_source,
    scope_for,
    validate_authority_policy,
)
from domain.domain.position_advice_promotion import (
    PROMOTION_CHECKS_SCHEMA,
    PROMOTION_EVIDENCE_SCHEMA,
    SAFETY_METRICS,
    evaluate_promotion_gate,
    unique_decision_opportunity_key,
)
from src.application.ledger.api import (
    validate_position_fact_snapshot_contract,
)
from src.application.position_advice_authority_service import (
    authority_policy_path,
    plan_authority_change,
    read_authority_resolution,
    read_authority_resolution_under_lock,
)
from src.application.position_advice_input_builder import (
    POSITION_ADVICE_INPUT_SCHEMA,
)
from src.application.position_advice_plan_builder import (
    POSITION_ADVICE_PLAN_SCHEMA,
)
from src.infrastructure.io_utils import atomic_write_json
from src.infrastructure.position_advice_manifest_lock import (
    portfolio_scope_state_dir,
    position_advice_manifest_locks,
)


PROMOTION_BUILD_SCHEMA = "position_advice_promotion_build.v1"
PROMOTION_REFRESH_SCHEMA = "position_advice_promotion_refresh.v1"
PROMOTION_STATUS_SCHEMA = "position_advice_promotion_status.v1"
PROMOTION_SOURCE_PLAN_FILENAME = "position_advice.v2.json.gz"
PROMOTION_SOURCE_INPUT_FILENAME = "position_advice_input.v2.json.gz"


class PositionAdvicePromotionError(RuntimeError):
    """Raised when replay evidence cannot be proven from immutable v2 plans."""


def build_position_advice_promotion_evidence(
    *,
    plan_paths: Iterable[Path],
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    authority_generation: int,
    authority_policy_hash: str,
    covered_strategy_families: Iterable[str] | None,
    safety: Mapping[str, Any] | None,
    critical_replay_fixtures: Mapping[str, Any] | None,
    generated_at: datetime | str,
) -> dict[str, Any]:
    """Aggregate complete immutable v2-shadow plans into promotion evidence."""

    account = normalize_account_label(normalized_account)
    portfolio_source = normalize_portfolio_source(
        normalized_portfolio_source
    )
    scope_id = scope_for(account)
    identity_hash = _sha256(
        portfolio_account_identity_hash,
        "portfolio_account_identity_hash",
    )
    policy_hash = _sha256(authority_policy_hash, "authority_policy_hash")
    generation = int(authority_generation)
    if generation <= 0:
        raise ValueError("authority_generation must be positive")
    observations: list[dict[str, Any]] = []
    plan_hashes: list[str] = []
    session_ids: set[str] = set()
    opportunity_times: list[str] = []
    pool_rows: list[dict[str, str]] = []
    economic_after = Decimal("0")
    economic_hold = Decimal("0")
    aggregate_horizon = Decimal("0")
    economic_plan_signatures: set[str] = set()
    seen_plan_hashes: set[str] = set()
    inferred_families: set[str] = set()
    automatic_safety_counts = {metric: 0 for metric in SAFETY_METRICS}
    automatic_safety_violations: list[dict[str, str]] = []
    safety_evaluator: Any = None
    if safety is None:
        from src.application.position_advice_promotion_checks import (
            evaluate_position_advice_plan_safety,
        )

        safety_evaluator = evaluate_position_advice_plan_safety

    paths = sorted({Path(item).resolve() for item in plan_paths}, key=str)
    if not paths:
        raise PositionAdvicePromotionError("promotion plan set is empty")
    for path in paths:
        plan, immutable_input = _read_bound_plan(
            path,
            expected_account=account,
            expected_scope_id=scope_id,
            expected_portfolio_source=portfolio_source,
            expected_identity_hash=identity_hash,
            expected_generation=generation,
            expected_policy_hash=policy_hash,
        )
        plan_hash = str(plan["artifact_hash"])
        if plan_hash in seen_plan_hashes:
            continue
        seen_plan_hashes.add(plan_hash)
        plan_hashes.append(plan_hash)
        inferred_families.update(
            _covered_strategy_families(((plan, immutable_input),))
        )
        if safety_evaluator is not None:
            safety_report = safety_evaluator(
                ((plan, immutable_input),)
            )
            for metric in SAFETY_METRICS:
                automatic_safety_counts[metric] += int(
                    safety_report["safety"][metric]
                )
            automatic_safety_violations.extend(
                dict(item)
                for item in safety_report.get("violations") or []
            )
        checked_at = _timestamp(plan.get("advice_checked_at"))
        opportunity_times.append(checked_at)
        markets = list(plan.get("included_markets") or [])
        if not markets:
            raise PositionAdvicePromotionError("promotion plan market is missing")
        session_ids.update(
            _market_session_id(market, checked_at) for market in markets
        )
        source_complete = _source_manifest_complete(plan)
        plan_observations = _plan_opportunities(
            plan=plan,
            immutable_input=immutable_input,
            source_complete=source_complete,
        )
        observations.extend(plan_observations)
        economic_signature = canonical_sha256(
            sorted(
                (_opportunity_material(item) for item in plan_observations),
                key=lambda item: str(item.get("opportunity_key") or ""),
            )
        )
        if economic_signature not in economic_plan_signatures:
            economic_plan_signatures.add(economic_signature)
            plan_pool_rows, after, hold, horizon = _plan_economics(plan)
            pool_rows.extend(plan_pool_rows)
            economic_after += after
            economic_hold += hold
            aggregate_horizon += horizon

    families = sorted(
        {
            str(item or "").strip()
            for item in (
                covered_strategy_families
                if covered_strategy_families is not None
                else inferred_families
            )
            if str(item or "").strip()
        }
    )
    if (
        not families
        or any(
            item not in PROMOTABLE_STRATEGY_FAMILIES
            for item in families
        )
    ):
        raise ValueError(
            "covered strategy families are empty or unsupported"
    )
    automatic_safety: dict[str, Any] | None = None
    if safety is None:
        automatic_safety_payload = {
            "schema_version": PROMOTION_CHECKS_SCHEMA,
            "evaluator_version": PROMOTION_CHECKS_SCHEMA,
            "source_plan_hashes": sorted(set(plan_hashes)),
            "safety": automatic_safety_counts,
            "violations": sorted(
                automatic_safety_violations,
                key=lambda item: (
                    item["metric"],
                    item["plan_hash"],
                    item["code"],
                ),
            ),
        }
        automatic_safety = {
            **automatic_safety_payload,
            "artifact_hash": canonical_sha256(
                automatic_safety_payload
            ),
        }
        safety_payload = _safety_payload(automatic_safety["safety"])
    else:
        safety_payload = _safety_payload(safety)
    automatic_replay: dict[str, Any] | None = None
    if critical_replay_fixtures is None:
        from src.application.position_advice_promotion_checks import (
            run_critical_promotion_replay,
        )

        automatic_replay = run_critical_promotion_replay()
        fixture_payload = dict(automatic_replay["fixture_results"])
    else:
        fixture_payload = {
            str(key): value is True
            for key, value in sorted(
                dict(critical_replay_fixtures).items()
            )
        }

    unique: dict[str, dict[str, Any]] = {}
    for item in observations:
        key = str(item["opportunity_key"])
        existing = unique.get(key)
        if (
            existing is not None
            and _opportunity_material(existing)
            != _opportunity_material(item)
        ):
            raise PositionAdvicePromotionError(
                "duplicate promotion opportunity conflicts"
            )
        unique.setdefault(key, item)
    opportunities = [unique[key] for key in sorted(unique)]
    reason_distribution = Counter(
        str(item.get("outcome_reason") or "unknown") for item in opportunities
    )
    payload = {
        "schema_version": PROMOTION_EVIDENCE_SCHEMA,
        "normalized_account": account,
        "portfolio_scope_id": scope_id,
        "normalized_portfolio_source": portfolio_source,
        "portfolio_account_identity_hash": identity_hash,
        "authority_mode": "v2_shadow",
        "authority_generation": generation,
        "authority_policy_hash": policy_hash,
        "generated_at": _timestamp(generated_at),
        "source_plan_hashes": sorted(set(plan_hashes)),
        "market_session_ids": sorted(session_ids),
        "first_opportunity_at": min(opportunity_times),
        "last_opportunity_at": max(opportunity_times),
        "covered_strategy_families": families,
        "safety": safety_payload,
        "critical_replay_fixtures": fixture_payload,
        "automatic_safety_evaluation": automatic_safety,
        "automatic_critical_replay": automatic_replay,
        "economic": {
            "modeled_portfolio_daily_carry_after_friction_base_cny": _decimal_text(
                economic_after
            ),
            "modeled_hold_daily_carry_base_cny": _decimal_text(economic_hold),
            "modeled_daily_carry_uplift_base_cny": _decimal_text(
                economic_after - economic_hold
            ),
            "aggregate_net_carry_improvement_H_base_cny": _decimal_text(
                aggregate_horizon
            ),
            "pool_efficiencies": pool_rows,
        },
        "reason_distribution": dict(sorted(reason_distribution.items())),
        "realized_outcome": {
            "status": "unknown",
            "reason": "canonical_executed_event_binding_not_evaluated",
        },
        "opportunities": opportunities,
    }
    return payload


def publish_position_advice_promotion_evidence(
    *,
    base: Path,
    plan_paths: Iterable[Path],
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    covered_strategy_families: Iterable[str] | None,
    safety: Mapping[str, Any] | None,
    critical_replay_fixtures: Mapping[str, Any] | None,
    generated_at: datetime | str,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Resolve the current shadow authority, build evidence, and write it once."""

    account = normalize_account_label(normalized_account)
    canonical_plan_paths = _canonical_promotion_plan_paths(
        base=base,
        plan_paths=plan_paths,
        normalized_account=account,
    )
    resolution = read_authority_resolution(
        base=base,
        normalized_account=account,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        timeout_seconds=timeout_seconds,
    )
    if (
        resolution.resolution_status != "resolved"
        or resolution.mode != "v2_shadow"
        or resolution.generation is None
        or not resolution.policy_hash
    ):
        raise PositionAdvicePromotionError(
            "promotion evidence requires resolved v2_shadow authority"
        )
    evidence = build_position_advice_promotion_evidence(
        plan_paths=canonical_plan_paths,
        normalized_account=account,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        authority_generation=resolution.generation,
        authority_policy_hash=resolution.policy_hash,
        covered_strategy_families=covered_strategy_families,
        safety=safety,
        critical_replay_fixtures=critical_replay_fixtures,
        generated_at=generated_at,
    )
    gate = evaluate_promotion_gate(evidence)
    evidence_hash = canonical_sha256(evidence)
    gate_payload = {
        **gate,
        "promotion_evidence_hash": evidence_hash,
    }
    gate_payload["artifact_hash"] = canonical_sha256(gate_payload)
    scope_dir = portfolio_scope_state_dir(base, resolution.portfolio_scope_id)
    evidence_path = scope_dir / "promotion_evidence" / f"{evidence_hash}.json"
    gate_path = scope_dir / "promotion_gates" / f"{gate_payload['artifact_hash']}.json"
    with position_advice_manifest_locks(
        base=base,
        portfolio_scope_id=resolution.portfolio_scope_id,
        global_mode="shared",
        scope_mode="exclusive",
        timeout_seconds=timeout_seconds,
    ):
        current = read_authority_resolution_under_lock(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=normalized_portfolio_source,
            portfolio_account_identity_hash=portfolio_account_identity_hash,
        )
        if (
            current.resolution_status != "resolved"
            or current.mode != "v2_shadow"
            or current.generation != resolution.generation
            or current.policy_hash != resolution.policy_hash
        ):
            raise PositionAdvicePromotionError(
                "authority changed while promotion evidence was built"
            )
        _write_once_or_verify(evidence_path, evidence)
        _write_once_or_verify(gate_path, gate_payload)
    return {
        "schema_version": PROMOTION_BUILD_SCHEMA,
        "status": gate["status"],
        "portfolio_scope_id": resolution.portfolio_scope_id,
        "promotion_evidence_hash": evidence_hash,
        "promotion_gate_hash": gate["gate_hash"],
        "evidence_path": str(evidence_path),
        "gate_path": str(gate_path),
        "evidence": evidence,
        "gate": gate_payload,
    }


def refresh_position_advice_promotion(
    *,
    base: Path,
    normalized_account: str,
    confirm: bool = False,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Build current-shadow evidence and optionally publish immutable artifacts."""

    account = normalize_account_label(normalized_account)
    policy = _read_current_policy(base=base, normalized_account=account)
    if policy is None:
        return _inactive_refresh(
            account=account,
            status="not_applicable",
            reason_code="authority_policy_absent",
            confirm=confirm,
        )
    if policy.get("mode") != "v2_shadow":
        return _inactive_refresh(
            account=account,
            status="not_applicable",
            reason_code=f"authority_mode_{policy.get('mode')}",
            confirm=confirm,
        )
    live_plan_paths = discover_current_shadow_plan_paths(
        base=base,
        normalized_account=account,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
    )
    if confirm and live_plan_paths:
        archive_current_shadow_plans(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=str(
                policy["normalized_portfolio_source"]
            ),
            portfolio_account_identity_hash=str(
                policy["portfolio_account_identity_hash"]
            ),
            authority_generation=int(policy["generation"]),
            authority_policy_hash=str(policy["policy_hash"]),
            plan_paths=live_plan_paths,
            timeout_seconds=timeout_seconds,
        )
    archived_plan_paths = discover_archived_shadow_plan_paths(
        base=base,
        normalized_account=account,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
    )
    plan_paths = sorted(
        {
            *archived_plan_paths,
            *([] if confirm else live_plan_paths),
        },
        key=str,
    )
    if not plan_paths:
        return _inactive_refresh(
            account=account,
            status="waiting_for_shadow_plans",
            reason_code="current_shadow_plan_set_empty",
            confirm=confirm,
            policy=policy,
        )
    generated_at = max(
        _timestamp(_read_json_object(path).get("advice_checked_at"))
        for path in plan_paths
    )
    kwargs = {
        "plan_paths": plan_paths,
        "normalized_account": account,
        "normalized_portfolio_source": str(
            policy["normalized_portfolio_source"]
        ),
        "portfolio_account_identity_hash": str(
            policy["portfolio_account_identity_hash"]
        ),
        "covered_strategy_families": None,
        "safety": None,
        "critical_replay_fixtures": None,
        "generated_at": generated_at,
    }
    if confirm:
        result = publish_position_advice_promotion_evidence(
            base=base,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )
        return {
            **result,
            "schema_version": PROMOTION_REFRESH_SCHEMA,
            "normalized_account": account,
            "source_plan_count": len(plan_paths),
            "dry_run": False,
            "published": True,
        }
    evidence = build_position_advice_promotion_evidence(
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
        **kwargs,
    )
    gate = evaluate_promotion_gate(evidence)
    return {
        "schema_version": PROMOTION_REFRESH_SCHEMA,
        "status": gate["status"],
        "normalized_account": account,
        "portfolio_scope_id": policy["portfolio_scope_id"],
        "source_plan_count": len(plan_paths),
        "promotion_evidence_hash": canonical_sha256(evidence),
        "promotion_gate_hash": gate["gate_hash"],
        "evidence": evidence,
        "gate": gate,
        "dry_run": True,
        "published": False,
    }


def position_advice_promotion_status(
    *,
    base: Path,
    normalized_account: str,
) -> dict[str, Any]:
    """Resolve the newest valid published gate for the current authority."""

    account = normalize_account_label(normalized_account)
    policy = _read_current_policy(base=base, normalized_account=account)
    if policy is None:
        return _promotion_status_payload(
            account=account,
            status="not_applicable",
            reason_codes=["authority_policy_absent"],
        )
    if policy.get("mode") != "v2_shadow":
        return _promotion_status_payload(
            account=account,
            status="not_applicable",
            reason_codes=[f"authority_mode_{policy.get('mode')}"],
            policy=policy,
        )
    scope_dir = portfolio_scope_state_dir(
        base, str(policy["portfolio_scope_id"])
    )
    with position_advice_manifest_locks(
        base=base,
        portfolio_scope_id=str(policy["portfolio_scope_id"]),
        global_mode="shared",
        scope_mode="shared",
    ):
        current = read_authority_resolution_under_lock(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=str(
                policy["normalized_portfolio_source"]
            ),
            portfolio_account_identity_hash=str(
                policy["portfolio_account_identity_hash"]
            ),
        )
        if (
            current.resolution_status != "resolved"
            or current.mode != "v2_shadow"
            or current.generation != policy["generation"]
            or current.policy_hash != policy["policy_hash"]
        ):
            raise PositionAdvicePromotionError(
                "authority changed while promotion status was read"
            )
        candidates = _published_promotion_candidates(
            scope_dir=scope_dir,
            policy=policy,
        )
    if not candidates:
        return _promotion_status_payload(
            account=account,
            status="waiting_for_promotion_evidence",
            reason_codes=["current_policy_promotion_evidence_absent"],
            policy=policy,
        )
    _, evidence, evidence_path, gate, gate_path = max(
        candidates, key=lambda item: item[0]
    )
    authority_plan = plan_authority_change(
        base=base,
        normalized_account=account,
        normalized_portfolio_source=str(
            policy["normalized_portfolio_source"]
        ),
        portfolio_account_identity_hash=str(
            policy["portfolio_account_identity_hash"]
        ),
        target_mode="v2",
        expected_policy_hash=str(policy["policy_hash"]),
        actor="position-advice-promotion-status",
        requested_at=datetime.now(timezone.utc),
        promotion_evidence=evidence,
    )
    ready = (
        gate["status"] == "pass"
        and authority_plan["status"] == "ready"
    )
    status = (
        str(gate["status"])
        if gate["status"] != "pass" or ready
        else "blocked"
    )
    reason_codes = (
        list(gate["reason_codes"])
        if gate["status"] != "pass"
        else list(authority_plan["reason_codes"])
    )
    return {
        **_promotion_status_payload(
            account=account,
            status=status,
            reason_codes=reason_codes,
            policy=policy,
        ),
        "ready_for_final_cas": ready,
        "promotion_gate_status": gate["status"],
        "authority_transition_status": authority_plan["status"],
        "authority_plan_hash": authority_plan["plan_hash"],
        "outstanding_notification_receipt_ids": list(
            authority_plan["outstanding_notification_receipt_ids"]
        ),
        "promotion_evidence_hash": canonical_sha256(evidence),
        "promotion_gate_hash": gate["gate_hash"],
        "evidence_path": str(evidence_path),
        "gate_path": str(gate_path),
        "source_plan_count": len(evidence.get("source_plan_hashes") or []),
        "first_opportunity_at": evidence.get("first_opportunity_at"),
        "last_opportunity_at": evidence.get("last_opportunity_at"),
        "distinct_market_session_count": gate[
            "distinct_market_session_count"
        ],
        "eligible_evaluation_count": gate["eligible_evaluation_count"],
        "selected_proposal_count": gate["selected_proposal_count"],
        "covered_strategy_families": list(
            evidence.get("covered_strategy_families") or []
        ),
        "safety": dict(evidence.get("safety") or {}),
        "critical_replay_fixtures": dict(
            evidence.get("critical_replay_fixtures") or {}
        ),
        "reason_distribution": dict(
            evidence.get("reason_distribution") or {}
        ),
        "economic": dict(evidence.get("economic") or {}),
        "final_cas": {
            "target_mode": "v2",
            "expected_policy_hash": policy["policy_hash"],
            "evidence_path": str(evidence_path),
        }
        if ready
        else None,
    }


def _published_promotion_candidates(
    *,
    scope_dir: Path,
    policy: Mapping[str, Any],
) -> list[
    tuple[
        tuple[datetime, int, datetime, str],
        dict[str, Any],
        Path,
        dict[str, Any],
        Path,
    ]
]:
    evidence_dir = scope_dir / "promotion_evidence"
    candidates: list[
        tuple[
            tuple[datetime, int, datetime, str],
            dict[str, Any],
            Path,
            dict[str, Any],
            Path,
        ]
    ] = []
    if not evidence_dir.exists():
        return candidates
    if not evidence_dir.is_dir() or evidence_dir.is_symlink():
        raise PositionAdvicePromotionError(
            "promotion evidence directory is unsafe"
        )
    for evidence_path in sorted(evidence_dir.glob("*.json")):
        evidence = _read_json_object(evidence_path)
        evidence_hash = canonical_sha256(evidence)
        if evidence_path.name != f"{evidence_hash}.json":
            raise PositionAdvicePromotionError(
                "promotion evidence filename hash mismatch"
            )
        if not _evidence_matches_policy(evidence, policy):
            continue
        gate = evaluate_promotion_gate(evidence)
        gate_payload = {
            **gate,
            "promotion_evidence_hash": evidence_hash,
        }
        gate_payload["artifact_hash"] = canonical_sha256(gate_payload)
        gate_path = (
            scope_dir
            / "promotion_gates"
            / f"{gate_payload['artifact_hash']}.json"
        )
        if (
            not gate_path.is_file()
            or gate_path.is_symlink()
            or _read_json_object(gate_path) != gate_payload
        ):
            raise PositionAdvicePromotionError(
                "published promotion gate is missing or conflicted"
            )
        order_key = (
            _datetime(evidence.get("last_opportunity_at")),
            len(evidence.get("source_plan_hashes") or []),
            _datetime(evidence.get("generated_at")),
            evidence_hash,
        )
        candidates.append(
            (order_key, evidence, evidence_path, gate_payload, gate_path)
        )
    return candidates


def discover_current_shadow_plan_paths(
    *,
    base: Path,
    normalized_account: str,
    authority_generation: int,
    authority_policy_hash: str,
) -> list[Path]:
    """Find canonical plans bound to one exact current shadow generation."""

    account = normalize_account_label(normalized_account)
    runs_root = Path(base).resolve() / "output_runs"
    if not runs_root.exists():
        return []
    if not runs_root.is_dir() or runs_root.is_symlink():
        raise PositionAdvicePromotionError(
            "promotion output_runs root is unavailable or unsafe"
        )
    paths: list[Path] = []
    for run_root in sorted(runs_root.iterdir(), key=lambda item: item.name):
        if run_root.is_symlink():
            raise PositionAdvicePromotionError(
                "promotion run root may not be a symlink"
            )
        if not run_root.is_dir():
            continue
        path = run_root / "accounts" / account / "position_advice.v2.json"
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise PositionAdvicePromotionError(
                "promotion plan path is unavailable or unsafe"
            )
        plan = _read_json_object(path)
        artifact_hash = plan.pop("artifact_hash", None)
        if artifact_hash != canonical_sha256(plan):
            raise PositionAdvicePromotionError(
                "position advice artifact hash mismatch during discovery"
            )
        plan["artifact_hash"] = artifact_hash
        if plan.get("normalized_account") != account:
            raise PositionAdvicePromotionError(
                "position advice account path binding mismatch"
            )
        if (
            plan.get("authority_mode") == "v2_shadow"
            and plan.get("authority_generation") == authority_generation
            and plan.get("authority_policy_hash") == authority_policy_hash
        ):
            paths.append(path.resolve())
    return paths


def archive_current_shadow_plans(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    authority_generation: int,
    authority_policy_hash: str,
    plan_paths: Iterable[Path],
    timeout_seconds: float = 5.0,
) -> list[Path]:
    """Copy exact live shadow inputs into compressed immutable control-plane sources."""

    account = normalize_account_label(normalized_account)
    portfolio_source = normalize_portfolio_source(
        normalized_portfolio_source
    )
    identity_hash = _sha256(
        portfolio_account_identity_hash,
        "portfolio_account_identity_hash",
    )
    policy_hash = _sha256(authority_policy_hash, "authority_policy_hash")
    generation = int(authority_generation)
    if generation <= 0:
        raise ValueError("authority_generation must be positive")
    canonical_paths = _canonical_promotion_plan_paths(
        base=base,
        plan_paths=plan_paths,
        normalized_account=account,
        allow_archived=False,
    )
    scope_id = scope_for(account)
    archived: list[Path] = []
    with position_advice_manifest_locks(
        base=base,
        portfolio_scope_id=scope_id,
        global_mode="shared",
        scope_mode="exclusive",
        timeout_seconds=timeout_seconds,
    ):
        current = read_authority_resolution_under_lock(
            base=base,
            normalized_account=account,
            normalized_portfolio_source=portfolio_source,
            portfolio_account_identity_hash=identity_hash,
        )
        if (
            current.resolution_status != "resolved"
            or current.mode != "v2_shadow"
            or current.generation != generation
            or current.policy_hash != policy_hash
        ):
            raise PositionAdvicePromotionError(
                "authority changed while promotion sources were archived"
            )
        archive_root = _promotion_source_archive_root(
            base=base,
            portfolio_scope_id=scope_id,
        )
        _ensure_safe_archive_directory(archive_root)
        for path in canonical_paths:
            plan, immutable_input = _read_bound_plan(
                path,
                expected_account=account,
                expected_scope_id=scope_id,
                expected_portfolio_source=portfolio_source,
                expected_identity_hash=identity_hash,
                expected_generation=generation,
                expected_policy_hash=policy_hash,
            )
            plan_hash = str(plan["artifact_hash"])
            source_dir = archive_root / plan_hash
            archived_plan = _archive_source_pair(
                source_dir=source_dir,
                plan=plan,
                immutable_input=immutable_input,
            )
            archived.append(archived_plan.resolve())
    return sorted(set(archived), key=str)


def discover_archived_shadow_plan_paths(
    *,
    base: Path,
    normalized_account: str,
    authority_generation: int,
    authority_policy_hash: str,
) -> list[Path]:
    """Find compressed exact sources for one current shadow generation."""

    account = normalize_account_label(normalized_account)
    policy_hash = _sha256(authority_policy_hash, "authority_policy_hash")
    generation = int(authority_generation)
    if generation <= 0:
        raise ValueError("authority_generation must be positive")
    archive_root = _promotion_source_archive_root(
        base=base,
        portfolio_scope_id=scope_for(account),
    )
    if not archive_root.exists():
        return []
    if not archive_root.is_dir() or archive_root.is_symlink():
        raise PositionAdvicePromotionError(
            "promotion source archive is unsafe"
        )
    paths: list[Path] = []
    for source_dir in sorted(
        archive_root.iterdir(), key=lambda item: item.name
    ):
        if source_dir.name.startswith(".tmp."):
            continue
        if (
            source_dir.is_symlink()
            or not source_dir.is_dir()
            or not _is_sha256_text(source_dir.name)
        ):
            raise PositionAdvicePromotionError(
                "promotion source archive entry is unsafe"
            )
        path = source_dir / PROMOTION_SOURCE_PLAN_FILENAME
        input_path = (
            source_dir / "state" / PROMOTION_SOURCE_INPUT_FILENAME
        )
        if (
            not path.is_file()
            or path.is_symlink()
            or not input_path.is_file()
            or input_path.is_symlink()
        ):
            raise PositionAdvicePromotionError(
                "promotion source archive is incomplete"
            )
        plan = _read_json_object(path)
        if (
            plan.get("artifact_hash") != source_dir.name
            or plan.get("artifact_hash")
            != canonical_sha256(
                {
                    key: value
                    for key, value in plan.items()
                    if key != "artifact_hash"
                }
            )
        ):
            raise PositionAdvicePromotionError(
                "promotion source archive hash mismatch"
            )
        if plan.get("normalized_account") != account:
            raise PositionAdvicePromotionError(
                "promotion source archive account mismatch"
            )
        if (
            plan.get("authority_mode") == "v2_shadow"
            and plan.get("authority_generation") == generation
            and plan.get("authority_policy_hash") == policy_hash
        ):
            paths.append(path.resolve())
    return paths


def _read_current_policy(
    *,
    base: Path,
    normalized_account: str,
) -> dict[str, Any] | None:
    account = normalize_account_label(normalized_account)
    path = authority_policy_path(base, scope_for(account))
    if not path.exists():
        return None
    policy = _read_json_object(path)
    reasons = validate_authority_policy(
        policy,
        expected_scope_id=scope_for(account),
    )
    if reasons or policy.get("normalized_account") != account:
        raise PositionAdvicePromotionError(
            "authority policy is invalid for promotion: "
            + ",".join(reasons or ("authority_account_mismatch",))
        )
    return policy


def _inactive_refresh(
    *,
    account: str,
    status: str,
    reason_code: str,
    confirm: bool,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": PROMOTION_REFRESH_SCHEMA,
        "status": status,
        "reason_codes": [reason_code],
        "normalized_account": account,
        "portfolio_scope_id": (
            dict(policy or {}).get("portfolio_scope_id")
            or scope_for(account)
        ),
        "source_plan_count": 0,
        "dry_run": not confirm,
        "published": False,
    }


def _promotion_status_payload(
    *,
    account: str,
    status: str,
    reason_codes: list[str],
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy_payload = dict(policy or {})
    return {
        "schema_version": PROMOTION_STATUS_SCHEMA,
        "status": status,
        "reason_codes": sorted(set(reason_codes)),
        "normalized_account": account,
        "portfolio_scope_id": (
            policy_payload.get("portfolio_scope_id") or scope_for(account)
        ),
        "authority_mode": policy_payload.get("mode"),
        "authority_generation": policy_payload.get("generation"),
        "authority_policy_hash": policy_payload.get("policy_hash"),
        "ready_for_final_cas": False,
    }


def _evidence_matches_policy(
    evidence: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> bool:
    return all(
        evidence.get(field) == expected
        for field, expected in {
            "normalized_account": policy.get("normalized_account"),
            "portfolio_scope_id": policy.get("portfolio_scope_id"),
            "normalized_portfolio_source": policy.get(
                "normalized_portfolio_source"
            ),
            "portfolio_account_identity_hash": policy.get(
                "portfolio_account_identity_hash"
            ),
            "authority_mode": "v2_shadow",
            "authority_generation": policy.get("generation"),
            "authority_policy_hash": policy.get("policy_hash"),
        }.items()
    )


def _covered_strategy_families(
    bound_plans: Iterable[
        tuple[Mapping[str, Any], Mapping[str, Any]]
    ],
) -> list[str]:
    families: set[str] = set()
    for plan, _immutable_input in bound_plans:
        for raw in plan.get("rows") or []:
            if not isinstance(raw, Mapping):
                continue
            family = str(raw.get("strategy_family") or "")
            if family in {"short_put", "funding_put"}:
                families.add("short_put")
            elif family == "covered_call":
                families.add("covered_call")
    return sorted(families)


def _read_bound_plan(
    path: Path,
    *,
    expected_account: str,
    expected_scope_id: str,
    expected_portfolio_source: str,
    expected_identity_hash: str,
    expected_generation: int,
    expected_policy_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = _read_json_object(path)
    artifact_hash = plan.pop("artifact_hash", None)
    if artifact_hash != canonical_sha256(plan):
        raise PositionAdvicePromotionError("position advice artifact hash mismatch")
    plan["artifact_hash"] = artifact_hash
    if plan.get("schema_version") != POSITION_ADVICE_PLAN_SCHEMA:
        raise PositionAdvicePromotionError("position advice schema is invalid")
    if (
        plan.get("normalized_account") != expected_account
        or plan.get("portfolio_scope_id") != expected_scope_id
        or plan.get("normalized_portfolio_source")
        != expected_portfolio_source
        or plan.get("portfolio_account_identity_hash") != expected_identity_hash
    ):
        raise PositionAdvicePromotionError("position advice identity mismatch")
    if (
        plan.get("authority_mode") != "v2_shadow"
        or plan.get("authority_generation") != expected_generation
        or plan.get("authority_policy_hash") != expected_policy_hash
    ):
        raise PositionAdvicePromotionError("position advice shadow authority mismatch")
    if (
        dict(plan.get("freshness") or {}).get("status") != "fresh"
        or plan.get("decision_snapshot_status") != "trusted"
    ):
        raise PositionAdvicePromotionError("position advice plan is not fresh and trusted")
    archived = path.name == PROMOTION_SOURCE_PLAN_FILENAME
    state_dir = path.parent / "state"
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise PositionAdvicePromotionError(
            "position advice input state directory is unsafe"
        )
    input_path = state_dir / (
        PROMOTION_SOURCE_INPUT_FILENAME
        if archived
        else "position_advice_input.v2.json"
    )
    immutable_input = _read_json_object(input_path)
    input_hash = immutable_input.pop("input_hash", None)
    if input_hash != canonical_sha256(immutable_input):
        raise PositionAdvicePromotionError("position advice input hash mismatch")
    immutable_input["input_hash"] = input_hash
    if immutable_input.get("schema_version") != POSITION_ADVICE_INPUT_SCHEMA:
        raise PositionAdvicePromotionError("position advice input schema is invalid")
    position_fact_reasons = validate_position_fact_snapshot_contract(
        dict(immutable_input.get("decision_state_snapshot") or {})
    )
    if position_fact_reasons:
        raise PositionAdvicePromotionError(
            "position advice input decision facts are invalid: "
            + ",".join(position_fact_reasons)
        )
    for field in (
        "account_run_id",
        "normalized_account",
        "normalized_portfolio_source",
        "portfolio_scope_id",
        "portfolio_account_identity_hash",
        "authority_mode",
        "authority_generation",
        "authority_policy_hash",
        "decision_state_fingerprint",
        "source_manifest_hash",
    ):
        if plan.get(field) != immutable_input.get(field):
            raise PositionAdvicePromotionError(
                f"position advice input binding mismatch: {field}"
            )
    if plan.get("input_hash") != input_hash:
        raise PositionAdvicePromotionError("position advice plan input hash mismatch")
    return plan, immutable_input


def _canonical_promotion_plan_paths(
    *,
    base: Path,
    plan_paths: Iterable[Path],
    normalized_account: str,
    allow_archived: bool = True,
) -> list[Path]:
    base_path = Path(base).resolve()
    runs_root = base_path / "output_runs"
    resolved_runs_root: Path | None = None
    if runs_root.exists() and (
        not runs_root.is_dir()
        or runs_root.is_symlink()
        or runs_root.resolve().parent != base_path
    ):
        raise PositionAdvicePromotionError(
            "promotion output_runs root is unavailable or unsafe"
        )
    if runs_root.is_dir():
        resolved_runs_root = runs_root.resolve()
    output: set[Path] = set()
    for raw in plan_paths:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base_path / candidate
        try:
            relative_candidate = candidate.relative_to(base_path)
        except ValueError as exc:
            raise PositionAdvicePromotionError(
                "promotion plan path escapes runtime root"
            ) from exc
        current = base_path
        for part in relative_candidate.parts:
            current = current / part
            if current.is_symlink():
                raise PositionAdvicePromotionError(
                    "promotion plan path may not contain symlinks"
                )
        if not candidate.is_file():
            raise PositionAdvicePromotionError(
                "promotion plan path is unavailable"
            )
        resolved = candidate.resolve()
        live_path = False
        relative: Path | None = None
        if resolved_runs_root is not None:
            try:
                relative = resolved.relative_to(resolved_runs_root)
            except ValueError:
                pass
        if relative is not None:
            live_path = (
                len(relative.parts) == 4
                and relative.parts[1] == "accounts"
                and relative.parts[2] == normalized_account
                and relative.parts[3] == "position_advice.v2.json"
                and relative.parts[0] not in {"", ".", ".."}
            )
        archived_path = False
        if allow_archived:
            archive_root = _promotion_source_archive_root(
                base=base_path,
                portfolio_scope_id=scope_for(normalized_account),
            )
            try:
                archive_relative = resolved.relative_to(
                    archive_root.resolve()
                )
            except ValueError:
                archive_relative = None
            if archive_relative is not None:
                archived_path = (
                    len(archive_relative.parts) == 2
                    and _is_sha256_text(archive_relative.parts[0])
                    and archive_relative.parts[1]
                    == PROMOTION_SOURCE_PLAN_FILENAME
                )
        if not live_path and not archived_path:
            raise PositionAdvicePromotionError(
                "promotion plan path is noncanonical"
            )
        output.add(resolved)
    if not output:
        raise PositionAdvicePromotionError("promotion plan set is empty")
    return sorted(output, key=str)


def _plan_opportunities(
    *,
    plan: Mapping[str, Any],
    immutable_input: Mapping[str, Any],
    source_complete: bool,
) -> list[dict[str, Any]]:
    rows = {
        str(item.get("position_id") or ""): dict(item)
        for item in plan.get("rows") or []
        if isinstance(item, Mapping) and str(item.get("position_id") or "")
    }
    proposals = [
        ({**dict(item), "selected": True, "outcome_reason": "selected"})
        for item in plan.get("selected_proposals") or []
        if isinstance(item, Mapping)
    ]
    proposals.extend(
        {
            **dict(item),
            "selected": False,
            "outcome_reason": str(
                item.get("allocator_reason") or "allocator_rejected"
            ),
        }
        for item in plan.get("alternative_proposals") or []
        if isinstance(item, Mapping)
    )
    output: list[dict[str, Any]] = []
    represented_positions: set[str] = set()
    source_fact_manifest_hash = _source_fact_manifest_hash(plan)
    for proposal in proposals:
        source_ids = sorted(
            {
                str(item or "").strip()
                for item in proposal.get("source_position_ids") or []
                if str(item or "").strip()
            }
        )
        if not source_ids:
            raise PositionAdvicePromotionError("promotion proposal identity is incomplete")
        represented_positions.update(source_ids)
        candidate_id = str(proposal.get("candidate_id") or "").strip()
        if not candidate_id:
            raise PositionAdvicePromotionError("promotion candidate identity is incomplete")
        strategy_family = _strategy_family(source_ids, rows)
        economic_hash = canonical_sha256(
            {
                "proposal_id": proposal.get("proposal_id"),
                "resource_deltas": proposal.get("resource_deltas"),
                "current_daily_carry_base_cny": proposal.get(
                    "current_daily_carry_base_cny"
                ),
                "candidate_daily_carry_base_cny": proposal.get(
                    "candidate_daily_carry_base_cny"
                ),
                "friction_base_cny": proposal.get("friction_base_cny"),
                "comparison_horizon_days": proposal.get(
                    "comparison_horizon_days"
                ),
                "net_carry_improvement_H_base_cny": proposal.get(
                    "net_carry_improvement_H_base_cny"
                ),
            }
        )
        opportunity_key = unique_decision_opportunity_key(
            portfolio_scope_id=str(plan["portfolio_scope_id"]),
            source_position_ids=source_ids,
            candidate_id=candidate_id,
            decision_state_fingerprint=str(
                plan["decision_state_fingerprint"]
            ),
            source_manifest_hash=source_fact_manifest_hash,
            economic_inputs_hash=economic_hash,
        )
        output.append(
            {
                "opportunity_key": opportunity_key,
                "account_run_id": plan.get("account_run_id"),
                "portfolio_plan_id": plan.get("portfolio_plan_id"),
                "source_position_ids": source_ids,
                "candidate_id": candidate_id,
                "eligible": proposal.get("risk_eligibility_status") == "accepted",
                "replacement_opportunity": True,
                "selected": proposal.get("selected") is True,
                "replacement_eligibility": proposal.get(
                    "replacement_eligibility"
                ),
                "strategy_family": strategy_family,
                "receipt_complete": source_complete,
                "fresh": True,
                "authority_mode": "v2_shadow",
                "pool_efficiency_improvement": proposal.get(
                    "pool_efficiency_improvement"
                ),
                "outcome_reason": proposal.get("outcome_reason"),
            }
        )
    for position_id, row in rows.items():
        if position_id in represented_positions:
            continue
        strategy_family = str(row.get("strategy_family") or "")
        reasons = [str(item) for item in row.get("reason_codes") or []]
        reason = _row_outcome_reason(reasons)
        economic_hash = canonical_sha256(
            {
                "row": row,
                "economic_inputs": immutable_input.get("economic_inputs"),
            }
        )
        candidate_id = f"no_candidate:{position_id}"
        output.append(
            {
                "opportunity_key": unique_decision_opportunity_key(
                    portfolio_scope_id=str(plan["portfolio_scope_id"]),
                    source_position_ids=[position_id],
                    candidate_id=candidate_id,
                    decision_state_fingerprint=str(
                        plan["decision_state_fingerprint"]
                    ),
                    source_manifest_hash=source_fact_manifest_hash,
                    economic_inputs_hash=economic_hash,
                ),
                "account_run_id": plan.get("account_run_id"),
                "portfolio_plan_id": plan.get("portfolio_plan_id"),
                "source_position_ids": [position_id],
                "candidate_id": candidate_id,
                "eligible": (
                    row.get("lifecycle_state") == "open"
                    and strategy_family in PROMOTABLE_STRATEGY_FAMILIES
                ),
                "replacement_opportunity": False,
                "selected": False,
                "replacement_eligibility": None,
                "strategy_family": strategy_family,
                "receipt_complete": source_complete,
                "fresh": True,
                "authority_mode": "v2_shadow",
                "pool_efficiency_improvement": None,
                "outcome_reason": reason,
            }
        )
    return output


def _source_fact_manifest_hash(plan: Mapping[str, Any]) -> str:
    """Hash source facts without run-local paths or consumer run identity."""

    entries = []
    for raw in plan.get("source_manifest") or []:
        item = dict(raw)
        entries.append(
            {
                "source_kind": item.get("source_kind"),
                "snapshot_id": item.get("snapshot_id"),
                "payload_sha256": item.get("payload_sha256"),
                "source_observed_at": item.get("source_observed_at"),
                "expires_at": item.get("expires_at"),
                "dependencies": _source_fact_dependencies(
                    item.get("dependencies")
                ),
                "capacity_pool_authority_id": item.get(
                    "capacity_pool_authority_id"
                ),
            }
        )
    return canonical_sha256(
        sorted(
            entries,
            key=lambda item: (
                str(item.get("source_kind") or ""),
                str(item.get("snapshot_id") or ""),
            ),
        )
    )


def _source_fact_dependencies(raw: Any) -> list[dict[str, Any]]:
    dependencies = []
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        dependency = dict(item)
        dependencies.append(
            {
                "source_kind": dependency.get("source_kind"),
                "snapshot_id": dependency.get("snapshot_id"),
                "payload_sha256": dependency.get("payload_sha256"),
                "expires_at": dependency.get("expires_at"),
            }
        )
    return sorted(
        dependencies,
        key=lambda item: (
            str(item.get("source_kind") or ""),
            str(item.get("snapshot_id") or ""),
        ),
    )


def _opportunity_material(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in {"account_run_id", "portfolio_plan_id"}
    }


def _plan_economics(
    plan: Mapping[str, Any],
) -> tuple[list[dict[str, str]], Decimal, Decimal, Decimal]:
    grouped: dict[str, dict[str, Decimal]] = {}
    after = Decimal("0")
    hold = Decimal("0")
    horizon_total = Decimal("0")
    for item in plan.get("selected_proposals") or []:
        proposal = dict(item)
        candidate_daily = decimal_value(
            proposal.get("candidate_daily_carry_base_cny"),
            field="candidate_daily_carry_base_cny",
        )
        current_daily = decimal_value(
            proposal.get("current_daily_carry_base_cny"),
            field="current_daily_carry_base_cny",
        )
        friction = decimal_value(
            proposal.get("friction_base_cny"),
            field="friction_base_cny",
            nonnegative=True,
        )
        horizon = decimal_value(
            proposal.get("comparison_horizon_days"),
            field="comparison_horizon_days",
            positive=True,
        )
        net_h = decimal_value(
            proposal.get("net_carry_improvement_H_base_cny"),
            field="net_carry_improvement_H_base_cny",
        )
        after += candidate_daily - (friction / horizon)
        hold += current_daily
        horizon_total += net_h
        for raw_delta in proposal.get("resource_deltas") or []:
            delta = dict(raw_delta)
            pool_key = str(delta.get("pool_key") or "").strip()
            if not pool_key:
                raise PositionAdvicePromotionError("typed pool key is missing")
            bucket = grouped.setdefault(
                pool_key,
                {
                    "current": Decimal("0"),
                    "candidate_after_friction": Decimal("0"),
                    "before_units": Decimal("0"),
                    "after_units": Decimal("0"),
                },
            )
            bucket["current"] += current_daily
            bucket["candidate_after_friction"] += candidate_daily - (
                friction / horizon
            )
            bucket["before_units"] += decimal_value(
                delta.get("released"),
                field="released resource units",
                positive=True,
            )
            bucket["after_units"] += decimal_value(
                delta.get("required"),
                field="required resource units",
                positive=True,
            )
    rows: list[dict[str, str]] = []
    for pool_key, bucket in sorted(grouped.items()):
        before = bucket["current"] / bucket["before_units"]
        after_efficiency = (
            bucket["candidate_after_friction"] / bucket["after_units"]
        )
        rows.append(
            {
                "portfolio_plan_id": str(plan.get("portfolio_plan_id") or ""),
                "pool_key": pool_key,
                "before": _decimal_text(before),
                "after": _decimal_text(after_efficiency),
                "resource_units_before": _decimal_text(
                    bucket["before_units"]
                ),
                "resource_units_after": _decimal_text(
                    bucket["after_units"]
                ),
            }
        )
    return rows, after, hold, horizon_total


def _source_manifest_complete(plan: Mapping[str, Any]) -> bool:
    entries = plan.get("source_manifest")
    if not isinstance(entries, list) or not entries:
        return False
    required = {
        "quotes",
        "candidate_decisions",
        "portfolio",
        "ledger_decision_state",
        "cash_capacity",
        "share_coverage",
        "fx",
    }
    kinds = {str(dict(item).get("source_kind") or "") for item in entries}
    if not required.issubset(kinds):
        return False
    for raw in entries:
        item = dict(raw)
        for field in ("receipt_hash", "snapshot_id", "payload_sha256"):
            value = str(item.get(field) or "")
            if len(value) != 64:
                return False
    return True


def _strategy_family(
    source_ids: list[str],
    rows: Mapping[str, Mapping[str, Any]],
) -> str:
    families = {
        str(rows.get(source_id, {}).get("strategy_family") or "")
        for source_id in source_ids
    }
    families.discard("")
    if len(families) != 1:
        raise PositionAdvicePromotionError(
            "promotion proposal strategy family is ambiguous"
        )
    return next(iter(families))


def _row_outcome_reason(reasons: list[str]) -> str:
    if any("capacity" in item for item in reasons):
        return "capacity_conflict"
    if any("invariant" in item for item in reasons):
        return "rejected_invariant"
    if any("candidate" in item for item in reasons):
        return "no_candidate"
    return reasons[0] if reasons else "eligible_no_candidate"


def _safety_payload(raw: Mapping[str, Any]) -> dict[str, int | None]:
    payload: dict[str, int | None] = {}
    for metric in SAFETY_METRICS:
        value = dict(raw or {}).get(metric)
        if value is None:
            payload[metric] = None
        elif isinstance(value, bool):
            raise ValueError(f"safety metric is invalid: {metric}")
        else:
            parsed = int(value)
            if parsed != value or parsed < 0:
                raise ValueError(f"safety metric is invalid: {metric}")
            payload[metric] = parsed
    return payload


def _write_once_or_verify(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        if path.is_symlink() or _read_json_object(path) != dict(payload):
            raise PositionAdvicePromotionError(
                "immutable promotion artifact conflicts"
            )
        return
    atomic_write_json(path, dict(payload), sort_keys=True)


def _archive_source_pair(
    *,
    source_dir: Path,
    plan: Mapping[str, Any],
    immutable_input: Mapping[str, Any],
) -> Path:
    plan_path = source_dir / PROMOTION_SOURCE_PLAN_FILENAME
    input_path = (
        source_dir / "state" / PROMOTION_SOURCE_INPUT_FILENAME
    )
    if source_dir.exists():
        _ensure_safe_archive_directory(source_dir)
        state_dir = source_dir / "state"
        if not state_dir.is_dir() or state_dir.is_symlink():
            raise PositionAdvicePromotionError(
                "promotion source archive is incomplete"
            )
        _write_once_or_verify_compressed(plan_path, plan)
        _write_once_or_verify_compressed(input_path, immutable_input)
        return plan_path

    temporary = source_dir.parent / (
        f".tmp.{source_dir.name}.{uuid.uuid4().hex[:12]}"
    )
    try:
        temporary.mkdir()
        (temporary / "state").mkdir()
        _write_once_or_verify_compressed(
            temporary / PROMOTION_SOURCE_PLAN_FILENAME,
            plan,
        )
        _write_once_or_verify_compressed(
            temporary / "state" / PROMOTION_SOURCE_INPUT_FILENAME,
            immutable_input,
        )
        temporary.replace(source_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return plan_path


def _write_once_or_verify_compressed(
    path: Path,
    payload: Mapping[str, Any],
) -> None:
    if path.exists():
        if path.is_symlink() or _read_json_object(path) != dict(payload):
            raise PositionAdvicePromotionError(
                "immutable promotion source conflicts"
            )
        return
    raw = (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    temporary = path.with_suffix(
        path.suffix + f".tmp.{uuid.uuid4().hex[:12]}"
    )
    try:
        temporary.write_bytes(compressed)
        temporary.replace(path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json_object(path: Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise PositionAdvicePromotionError(
            f"promotion source is unavailable: {target}"
        )
    try:
        if target.name.endswith(".json.gz"):
            raw = gzip.decompress(target.read_bytes()).decode("utf-8")
        else:
            raw = target.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (
        EOFError,
        OSError,
        UnicodeDecodeError,
        gzip.BadGzipFile,
        json.JSONDecodeError,
    ) as exc:
        raise PositionAdvicePromotionError(
            f"promotion source is unreadable: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise PositionAdvicePromotionError(
            "promotion source must be an object"
        )
    return payload


def _promotion_source_archive_root(
    *,
    base: Path,
    portfolio_scope_id: str,
) -> Path:
    return (
        portfolio_scope_state_dir(base, portfolio_scope_id)
        / "promotion_sources"
    )


def _ensure_safe_archive_directory(path: Path) -> None:
    target = Path(path)
    if target.exists() and (
        not target.is_dir() or target.is_symlink()
    ):
        raise PositionAdvicePromotionError(
            "promotion source archive directory is unsafe"
        )
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir() or target.is_symlink():
        raise PositionAdvicePromotionError(
            "promotion source archive directory is unsafe"
        )


def _is_sha256_text(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(
        char in "0123456789abcdef" for char in text
    )


def _sha256(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _is_sha256_text(text):
        raise ValueError(f"{field} must be SHA-256")
    return text


def _timestamp(value: datetime | str | Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime(value: datetime | str | Any) -> datetime:
    return datetime.fromisoformat(
        _timestamp(value).replace("Z", "+00:00")
    )


def _market_session_id(market: Any, checked_at: datetime | str) -> str:
    market_code = str(market or "").strip().upper()
    timezone_name = RUNTIME_SCHEDULE_TIMEZONE_BY_MARKET.get(
        market_code.lower()
    )
    if not timezone_name:
        raise PositionAdvicePromotionError(
            "promotion plan market timezone is unsupported"
        )
    normalized = _timestamp(checked_at)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    session_date = parsed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    return f"{market_code}:{session_date}"


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


__all__ = [
    "POSITION_ADVICE_INPUT_SCHEMA",
    "POSITION_ADVICE_PLAN_SCHEMA",
    "PROMOTION_BUILD_SCHEMA",
    "PROMOTION_REFRESH_SCHEMA",
    "PROMOTION_SOURCE_INPUT_FILENAME",
    "PROMOTION_SOURCE_PLAN_FILENAME",
    "PROMOTION_STATUS_SCHEMA",
    "PositionAdvicePromotionError",
    "archive_current_shadow_plans",
    "build_position_advice_promotion_evidence",
    "discover_archived_shadow_plan_paths",
    "discover_current_shadow_plan_paths",
    "position_advice_promotion_status",
    "publish_position_advice_promotion_evidence",
    "refresh_position_advice_promotion",
]
