from __future__ import annotations

import json
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
    scope_for,
)
from domain.domain.position_advice_promotion import (
    PROMOTION_EVIDENCE_SCHEMA,
    SAFETY_METRICS,
    evaluate_promotion_gate,
    unique_decision_opportunity_key,
)
from src.application.position_advice_authority_service import (
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
    covered_strategy_families: Iterable[str],
    safety: Mapping[str, Any],
    critical_replay_fixtures: Mapping[str, Any],
    generated_at: datetime | str,
) -> dict[str, Any]:
    """Aggregate complete immutable v2-shadow plans into promotion evidence."""

    account = normalize_account_label(normalized_account)
    scope_id = scope_for(account)
    identity_hash = _sha256(
        portfolio_account_identity_hash,
        "portfolio_account_identity_hash",
    )
    policy_hash = _sha256(authority_policy_hash, "authority_policy_hash")
    generation = int(authority_generation)
    if generation <= 0:
        raise ValueError("authority_generation must be positive")
    families = sorted(
        {str(item or "").strip() for item in covered_strategy_families if str(item or "").strip()}
    )
    if not families or any(item not in PROMOTABLE_STRATEGY_FAMILIES for item in families):
        raise ValueError("covered strategy families are empty or unsupported")
    safety_payload = _safety_payload(safety)
    fixture_payload = {
        str(key): value is True
        for key, value in sorted(dict(critical_replay_fixtures or {}).items())
    }
    observations: list[dict[str, Any]] = []
    plan_hashes: list[str] = []
    session_ids: set[str] = set()
    opportunity_times: list[str] = []
    pool_rows: list[dict[str, str]] = []
    economic_after = Decimal("0")
    economic_hold = Decimal("0")
    aggregate_horizon = Decimal("0")
    economic_plan_signatures: set[str] = set()

    paths = sorted({Path(item).resolve() for item in plan_paths}, key=str)
    if not paths:
        raise PositionAdvicePromotionError("promotion plan set is empty")
    for path in paths:
        plan, immutable_input = _read_bound_plan(
            path,
            expected_account=account,
            expected_scope_id=scope_id,
            expected_identity_hash=identity_hash,
            expected_generation=generation,
            expected_policy_hash=policy_hash,
        )
        plan_hashes.append(str(plan["artifact_hash"]))
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
        "normalized_portfolio_source": str(
            normalized_portfolio_source or ""
        ).strip().lower(),
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
    covered_strategy_families: Iterable[str],
    safety: Mapping[str, Any],
    critical_replay_fixtures: Mapping[str, Any],
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


def _read_bound_plan(
    path: Path,
    *,
    expected_account: str,
    expected_scope_id: str,
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
    state_dir = path.parent / "state"
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise PositionAdvicePromotionError(
            "position advice input state directory is unsafe"
        )
    input_path = state_dir / "position_advice_input.v2.json"
    immutable_input = _read_json_object(input_path)
    input_hash = immutable_input.pop("input_hash", None)
    if input_hash != canonical_sha256(immutable_input):
        raise PositionAdvicePromotionError("position advice input hash mismatch")
    immutable_input["input_hash"] = input_hash
    if immutable_input.get("schema_version") != POSITION_ADVICE_INPUT_SCHEMA:
        raise PositionAdvicePromotionError("position advice input schema is invalid")
    for field in (
        "account_run_id",
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
) -> list[Path]:
    base_path = Path(base).resolve()
    runs_root = base_path / "output_runs"
    if (
        not runs_root.is_dir()
        or runs_root.is_symlink()
        or runs_root.resolve().parent != base_path
    ):
        raise PositionAdvicePromotionError(
            "promotion output_runs root is unavailable or unsafe"
        )
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
        try:
            relative = resolved.relative_to(runs_root.resolve())
        except ValueError as exc:
            raise PositionAdvicePromotionError(
                "promotion plan path escapes output_runs"
            ) from exc
        if (
            len(relative.parts) != 4
            or relative.parts[1] != "accounts"
            or relative.parts[2] != normalized_account
            or relative.parts[3] != "position_advice.v2.json"
            or relative.parts[0] in {"", ".", ".."}
        ):
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


def _read_json_object(path: Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise PositionAdvicePromotionError(
            f"promotion source is unavailable: {target}"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PositionAdvicePromotionError(
            f"promotion source is unreadable: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise PositionAdvicePromotionError(
            "promotion source must be an object"
        )
    return payload


def _sha256(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
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
    "PositionAdvicePromotionError",
    "build_position_advice_promotion_evidence",
    "publish_position_advice_promotion_evidence",
]
