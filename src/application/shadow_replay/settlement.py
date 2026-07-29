from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.application.shadow_replay.common import (
    CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
    OPTIONAL_CLOSE_DATASET_FILES,
    OUTCOME_FACT_SCHEMA_VERSION,
    bind_legacy_decision_evidence,
    dataset_dir_from_arg,
    dataset_write_lock,
    decision_instance_key,
    first_float,
    freeze_decision_identities,
    instrument_key,
    parse_date,
    read_csv_rows,
    read_jsonl,
    refresh_dataset_manifest,
    resolve_output_path,
    safety_payload,
    text,
    utc_now,
    validate_dataset_integrity,
    write_json,
    write_jsonl,
)


def settle_shadow_replay_dataset(
    *,
    dataset: str | Path,
    output: str | Path | None = None,
    write: bool = False,
    replace: bool = False,
    lifecycle_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
) -> dict[str, Any]:
    """Derive local outcome facts from candidate snapshots and mark paths."""

    dataset_dir = dataset_dir_from_arg(dataset)
    if not write:
        return _settle_shadow_replay_dataset_unlocked(
            dataset=dataset,
            output=output,
            write=False,
            replace=replace,
            lifecycle_paths=lifecycle_paths,
        )
    with dataset_write_lock(dataset_dir):
        validate_dataset_integrity(dataset_dir)
        result = _settle_shadow_replay_dataset_unlocked(
            dataset=dataset,
            output=output,
            write=True,
            replace=replace,
            lifecycle_paths=lifecycle_paths,
        )
        result["dataset_integrity"] = refresh_dataset_manifest(dataset_dir)["integrity"]
        return result


def _settle_shadow_replay_dataset_unlocked(
    *,
    dataset: str | Path,
    output: str | Path | None = None,
    write: bool = False,
    replace: bool = False,
    lifecycle_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    candidate_snapshots = read_jsonl(dataset_dir / "candidate_snapshots.jsonl")
    mark_snapshots = read_jsonl(dataset_dir / "mark_path_snapshots.jsonl")
    existing_outcomes = read_jsonl(dataset_dir / "outcome_facts.jsonl")
    generated = derive_outcome_facts(candidate_snapshots, mark_snapshots, existing_outcomes=existing_outcomes)
    merged = _merge_outcome_facts(existing_outcomes, generated)
    close_episode_path = dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[0]
    close_facet_exists = close_episode_path.is_file()
    close_episodes = read_jsonl(close_episode_path) if close_facet_exists else []
    close_marks = read_jsonl(dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[1]) if close_facet_exists else []
    existing_close_outcomes = (
        read_jsonl(dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[2])
        if close_facet_exists
        else []
    )
    lifecycle_facts = _read_close_lifecycle_facts(lifecycle_paths or [])
    generated_close = derive_close_decision_outcomes(
        close_episodes,
        close_marks,
        lifecycle_facts=lifecycle_facts,
    )
    merged_close = _merge_close_outcomes(
        existing_close_outcomes,
        generated_close,
        replace=replace,
    )
    combo_settlement = None
    if (dataset_dir / "combo_pair_decisions.jsonl").is_file():
        from src.application.shadow_replay.combo_settlement import settle_combo_pair_dataset

        combo_settlement = settle_combo_pair_dataset(
            dataset=dataset_dir,
            write=write,
            replace=replace,
            _lock=False,
        )
    result = {
        "schema_version": "shadow_replay_settlement.v1",
        "dataset_dir": str(dataset_dir),
        "generated_at_utc": utc_now(),
        "summary": {
            "candidate_snapshot_count": len(candidate_snapshots),
            "mark_path_snapshot_count": len(mark_snapshots),
            "existing_outcome_fact_count": len(existing_outcomes),
            "generated_outcome_fact_count": len(generated),
            "written": bool(write),
            "replace": bool(replace),
            "close_decision_episode_count": len(close_episodes),
            "close_mark_count": len(close_marks),
            "close_lifecycle_fact_count": len(lifecycle_facts),
            "generated_close_outcome_count": len(generated_close),
            "usable_close_outcome_count": sum(
                1
                for row in generated_close
                if text(row.get("evidence_status")).lower() == "usable"
            ),
            "inconclusive_close_outcome_count": sum(
                1
                for row in generated_close
                if text(row.get("evidence_status")).lower() == "inconclusive"
            ),
            "generated_combo_pair_outcome_count": (
                combo_settlement["summary"]["generated_outcome_count"]
                if combo_settlement is not None
                else 0
            ),
            "complete_combo_pair_outcome_count": (
                combo_settlement["summary"]["complete_outcome_count"]
                if combo_settlement is not None
                else 0
            ),
        },
        "generated_outcome_facts": generated,
        "generated_close_outcomes": generated_close,
        "combo_pair_settlement": combo_settlement,
        "safety": safety_payload(writes_local_dataset=bool(write)),
    }
    if write:
        write_jsonl(dataset_dir / "outcome_facts.jsonl", merged)
        if close_facet_exists:
            write_jsonl(dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[2], merged_close)
    if output:
        write_json(resolve_output_path(output), result)
    return result


_CLOSE_HORIZONS = ("1d", "3d", "7d", "14d")
_CLOSE_LIFECYCLE_EVENT_TYPES = {"close", "expire_close", "assignment", "exercise"}


def derive_close_decision_outcomes(
    episodes: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    *,
    lifecycle_facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    marks_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mark in marks:
        episode_id = text(mark.get("episode_id"))
        if episode_id:
            marks_by_episode[episode_id].append(mark)
    lifecycle_by_lot: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for fact in lifecycle_facts:
        account = text(fact.get("account") or _nested(fact, "contract_key", "account")).lower()
        lot_id = text(fact.get("target_lot_id") or fact.get("position_lot_id") or fact.get("lot_id"))
        if account and lot_id:
            lifecycle_by_lot[(account, lot_id)].append(fact)

    out: list[dict[str, Any]] = []
    for episode in episodes:
        episode_id = text(episode.get("episode_id"))
        if not episode_id:
            continue
        episode_marks = marks_by_episode.get(episode_id, [])
        for horizon in _CLOSE_HORIZONS:
            horizon_marks = [
                mark
                for mark in episode_marks
                if text(mark.get("horizon")).lower() == horizon
            ]
            usable = [mark for mark in horizon_marks if _usable_horizon_close_mark(mark)]
            mark = min(usable, key=_close_mark_time) if usable else None
            out.append(
                _horizon_close_outcome(
                    episode,
                    horizon=horizon,
                    mark=mark,
                    missing_reason=_missing_close_mark_reason(horizon_marks),
                )
            )
        lifecycle = _eligible_lifecycle_facts(
            episode,
            lifecycle_by_lot.get(
                (text(episode.get("account")).lower(), text(episode.get("position_lot_id"))),
                [],
            ),
        )
        expiry_candidates = [
            mark
            for mark in episode_marks
            if text(mark.get("horizon")).lower() == "expiry"
            and text(mark.get("quote_status")).lower() == "matched"
        ]
        expiry_marks = [mark for mark in expiry_candidates if _usable_expiry_close_mark(mark)]
        expiry_mark = min(expiry_marks, key=_close_mark_time) if expiry_marks else None
        out.append(
            _terminal_close_outcome(
                episode,
                lifecycle_facts=lifecycle,
                expiry_mark=expiry_mark,
                missing_expiry_reason=(
                    _missing_close_mark_reason(expiry_candidates)
                    if expiry_candidates
                    else "lifecycle_or_expiry_fact_missing"
                ),
            )
        )
    return out


def _horizon_close_outcome(
    episode: dict[str, Any],
    *,
    horizon: str,
    mark: dict[str, Any] | None,
    missing_reason: str = "no_usable_mark_in_window",
) -> dict[str, Any]:
    base = _close_outcome_base(episode, outcome_kind=f"horizon_{horizon}")
    if mark is None:
        return _inconclusive_close_outcome(base, missing_reason)
    decision = _decision_close_inputs(episode)
    if decision is None:
        return _inconclusive_close_outcome(base, "decision_close_cost_incomplete", mark=mark)
    ask = first_float(mark, "ask")
    future_fee = first_float(mark, "future_close_fee")
    if ask is None or ask < 0:
        return _inconclusive_close_outcome(base, "future_ask_missing", mark=mark)
    if future_fee is None or future_fee < 0:
        return _inconclusive_close_outcome(base, "future_close_fee_missing", mark=mark)
    close_now_cost, contracts, multiplier = decision
    future_close_cost = ask * multiplier * contracts + future_fee
    hold_incremental = close_now_cost - future_close_cost
    result = {
        **base,
        "evidence_status": "usable",
        "inconclusive_reason": None,
        "outcome": "counterfactual_horizon_mark",
        "marked_at_utc": mark.get("marked_at_utc"),
        "close_now_cost": close_now_cost,
        "future_option_close_cost": future_close_cost,
        "future_close_fee": future_fee,
        "hold_to_horizon_incremental": hold_incremental,
        "close_now_incremental": 0.0,
        "hold_vs_close_regret": hold_incremental,
        "underlying_spot": first_float(mark, "spot"),
        "option_ask": ask,
        "source": "close_decision_mark",
        "mark_time_basis": mark.get("mark_time_basis"),
        "point_in_time_status": mark.get("point_in_time_status"),
    }
    result.update(
        _replacement_horizon_outcome(
            episode,
            mark=mark,
            hold_incremental=hold_incremental,
        )
    )
    return result


def _replacement_horizon_outcome(
    episode: dict[str, Any],
    *,
    mark: dict[str, Any],
    hold_incremental: float,
) -> dict[str, Any]:
    evidence = episode.get("replacement_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    if text(evidence.get("status")).lower() != "review_switch":
        return {
            "replacement_outcome_status": "not_applicable",
            "replacement_inconclusive_reason": "replacement_not_selected_at_decision",
            "replacement_incremental": None,
            "switch_vs_close_incremental": None,
            "switch_vs_hold_incremental": None,
        }
    entry_credit = first_float(evidence, "entry_credit")
    contracts = first_float(evidence, "contracts")
    multiplier = first_float(evidence, "multiplier")
    open_fee = first_float(evidence, "open_fee")
    entry_slippage = first_float(evidence, "entry_slippage")
    future_ask = first_float(mark, "replacement_ask")
    exit_fee = first_float(mark, "replacement_future_close_fee")
    if any(
        value is None
        for value in (
            entry_credit,
            contracts,
            multiplier,
            open_fee,
            entry_slippage,
            future_ask,
            exit_fee,
        )
    ):
        return {
            "replacement_outcome_status": "inconclusive",
            "replacement_inconclusive_reason": "replacement_entry_or_exit_evidence_incomplete",
            "replacement_incremental": None,
            "switch_vs_close_incremental": None,
            "switch_vs_hold_incremental": None,
        }
    assert entry_credit is not None
    assert contracts is not None
    assert multiplier is not None
    assert open_fee is not None
    assert entry_slippage is not None
    assert future_ask is not None
    assert exit_fee is not None
    replacement_incremental = (
        entry_credit
        - future_ask * multiplier * contracts
        - open_fee
        - exit_fee
        - entry_slippage
    )
    return {
        "replacement_outcome_status": "usable",
        "replacement_inconclusive_reason": None,
        "replacement_contract_symbol": evidence.get("contract_symbol"),
        "replacement_future_ask": future_ask,
        "replacement_future_close_fee": exit_fee,
        "replacement_incremental": replacement_incremental,
        "switch_vs_close_incremental": replacement_incremental,
        "switch_vs_hold_incremental": replacement_incremental - hold_incremental,
    }


def _terminal_close_outcome(
    episode: dict[str, Any],
    *,
    lifecycle_facts: list[dict[str, Any]],
    expiry_mark: dict[str, Any] | None,
    missing_expiry_reason: str = "lifecycle_or_expiry_fact_missing",
) -> dict[str, Any]:
    base = _close_outcome_base(episode, outcome_kind="terminal")
    if len(lifecycle_facts) > 1:
        return _inconclusive_close_outcome(base, "multiple_lifecycle_events_require_canonical_allocation")
    if lifecycle_facts:
        return _terminal_from_lifecycle(episode, base=base, event=lifecycle_facts[0])
    if expiry_mark is None:
        return _inconclusive_close_outcome(base, missing_expiry_reason)
    decision = _decision_close_inputs(episode)
    if decision is None:
        return _inconclusive_close_outcome(base, "decision_close_cost_incomplete", mark=expiry_mark)
    identity = episode.get("position_identity")
    identity = identity if isinstance(identity, dict) else {}
    strike = first_float(identity, "strike")
    spot = first_float(expiry_mark, "spot")
    option_type = text(identity.get("option_type")).lower()
    if strike is None or spot is None or option_type not in {"put", "call"}:
        return _inconclusive_close_outcome(base, "expiration_intrinsic_inputs_missing", mark=expiry_mark)
    intrinsic = max(strike - spot, 0.0) if option_type == "put" else max(spot - strike, 0.0)
    if intrinsic > 0:
        return _inconclusive_close_outcome(
            base,
            "itm_expiration_requires_canonical_lifecycle_fact",
            mark=expiry_mark,
        )
    close_now_cost, _contracts, _multiplier = decision
    result = {
        **base,
        "evidence_status": "usable",
        "inconclusive_reason": None,
        "outcome": "expired_worthless",
        "marked_at_utc": expiry_mark.get("marked_at_utc"),
        "close_now_cost": close_now_cost,
        "future_option_close_cost": 0.0,
        "future_close_fee": 0.0,
        "hold_to_horizon_incremental": close_now_cost,
        "close_now_incremental": 0.0,
        "hold_vs_close_regret": close_now_cost,
        "underlying_spot": spot,
        "source": "expiration_mark",
        "mark_time_basis": expiry_mark.get("mark_time_basis"),
        "point_in_time_status": expiry_mark.get("point_in_time_status"),
    }
    result.update(
        _replacement_horizon_outcome(
            episode,
            mark=expiry_mark,
            hold_incremental=close_now_cost,
        )
    )
    return result


def _terminal_from_lifecycle(
    episode: dict[str, Any],
    *,
    base: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    event_type = text(event.get("event_type") or event.get("outcome")).lower()
    event_at = _lifecycle_time(event)
    identity = episode.get("position_identity")
    identity = identity if isinstance(identity, dict) else {}
    option_type = text(identity.get("option_type")).lower()
    outcome = event_type
    if event_type in {"assignment", "exercise"}:
        outcome = "assigned" if option_type == "put" else "called_away" if option_type == "call" else event_type
        incremental = first_float(
            event,
            "lifecycle_pnl_after_decision",
            "incremental_pnl_after_decision",
        )
        incremental_binding_ok = _lifecycle_incremental_matches_episode(event, episode=episode)
        economics = episode.get("decision_economics")
        economics = economics if isinstance(economics, dict) else {}
        required_contracts = first_float(economics, "contracts")
        event_contracts = first_float(event, "contracts")
        if (
            required_contracts is None
            or event_contracts is None
            or abs(event_contracts - required_contracts) > 1e-9
        ):
            return {
                **_inconclusive_close_outcome(base, "lifecycle_contract_quantity_incomplete"),
                "outcome": outcome,
                "lifecycle_at_utc": event_at,
                "willingness_alignment": _willingness_alignment(episode, outcome=outcome),
                "source": "canonical_lifecycle_fact",
            }
        if incremental is None:
            return {
                **_inconclusive_close_outcome(base, "lifecycle_incremental_pnl_missing"),
                "outcome": outcome,
                "lifecycle_at_utc": event_at,
                "willingness_alignment": _willingness_alignment(episode, outcome=outcome),
                "source": "canonical_lifecycle_fact",
            }
        if not incremental_binding_ok:
            return {
                **_inconclusive_close_outcome(base, "lifecycle_incremental_pnl_unbound"),
                "outcome": outcome,
                "lifecycle_at_utc": event_at,
                "willingness_alignment": _willingness_alignment(episode, outcome=outcome),
                "source": "canonical_lifecycle_fact",
            }
        return {
            **base,
            "evidence_status": "usable",
            "inconclusive_reason": None,
            "outcome": outcome,
            "lifecycle_at_utc": event_at,
            "hold_to_horizon_incremental": incremental,
            "close_now_incremental": 0.0,
            "hold_vs_close_regret": incremental,
            "willingness_alignment": _willingness_alignment(episode, outcome=outcome),
            "source": "canonical_lifecycle_fact",
        }

    decision = _decision_close_inputs(episode)
    if decision is None:
        return _inconclusive_close_outcome(base, "decision_close_cost_incomplete")
    close_now_cost, required_contracts, decision_multiplier = decision
    event_contracts = first_float(event, "contracts")
    if event_contracts is None or abs(event_contracts - required_contracts) > 1e-9:
        return _inconclusive_close_outcome(base, "lifecycle_contract_quantity_incomplete")
    price = first_float(event, "price", "close_price")
    fees = first_float(event, "fees", "fee", "close_fee")
    multiplier = first_float(event, "multiplier") or decision_multiplier
    if price is None or price < 0 or fees is None or fees < 0:
        return _inconclusive_close_outcome(base, "lifecycle_price_or_fee_missing")
    future_close_cost = price * multiplier * required_contracts + fees
    hold_incremental = close_now_cost - future_close_cost
    resolved_outcome = "expired_worthless" if event_type == "expire_close" and price == 0 else "closed_later"
    return {
        **base,
        "evidence_status": "usable",
        "inconclusive_reason": None,
        "outcome": resolved_outcome,
        "lifecycle_at_utc": event_at,
        "close_now_cost": close_now_cost,
        "future_option_close_cost": future_close_cost,
        "future_close_fee": fees,
        "hold_to_horizon_incremental": hold_incremental,
        "close_now_incremental": 0.0,
        "hold_vs_close_regret": hold_incremental,
        "source": "canonical_lifecycle_fact",
    }


def _close_outcome_base(episode: dict[str, Any], *, outcome_kind: str) -> dict[str, Any]:
    shadow = episode.get("shadow_policy_results")
    shadow = shadow if isinstance(shadow, dict) else {}
    return {
        "schema_version": CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
        "episode_id": episode.get("episode_id"),
        "outcome_kind": outcome_kind,
        "account": episode.get("account"),
        "position_lot_id": episode.get("position_lot_id"),
        "observed_at_utc": episode.get("observed_at_utc"),
        "policy_recommendations": {
            policy: result.get("recommendation_state")
            for policy, result in shadow.items()
            if isinstance(result, dict)
        },
        "writes_runtime_config": False,
        "writes_trade_state": False,
    }


def _inconclusive_close_outcome(
    base: dict[str, Any],
    reason: str,
    *,
    mark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **base,
        "evidence_status": "inconclusive",
        "inconclusive_reason": reason,
        "outcome": "inconclusive",
        "marked_at_utc": mark.get("marked_at_utc") if isinstance(mark, dict) else None,
        "hold_to_horizon_incremental": None,
        "close_now_incremental": 0.0,
        "hold_vs_close_regret": None,
        "source": "close_decision_mark" if isinstance(mark, dict) else None,
    }


def _decision_close_inputs(episode: dict[str, Any]) -> tuple[float, float, float] | None:
    economics = episode.get("decision_economics")
    economics = economics if isinstance(economics, dict) else {}
    close_now_cost = first_float(economics, "close_now_cost")
    contracts = first_float(economics, "contracts")
    multiplier = first_float(economics, "multiplier")
    if (
        close_now_cost is None
        or close_now_cost < 0
        or contracts is None
        or contracts <= 0
        or multiplier is None
        or multiplier <= 0
    ):
        return None
    return close_now_cost, contracts, multiplier


def _eligible_lifecycle_facts(
    episode: dict[str, Any],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observed = _strict_utc(text(episode.get("observed_at_utc")))
    out: list[dict[str, Any]] = []
    for fact in facts:
        event_type = text(fact.get("event_type") or fact.get("outcome")).lower()
        if event_type not in _CLOSE_LIFECYCLE_EVENT_TYPES:
            continue
        event_at = _strict_utc(_lifecycle_time(fact))
        if event_at > observed:
            out.append(fact)
    return sorted(out, key=_lifecycle_time)


def _willingness_alignment(episode: dict[str, Any], *, outcome: str) -> str:
    if outcome not in {"assigned", "called_away"}:
        return "not_applicable"
    facts = episode.get("normalized_decision_facts")
    facts = facts if isinstance(facts, dict) else {}
    willingness = facts.get("continued_willingness")
    if willingness is True:
        return "aligned"
    if willingness is False:
        return "misaligned"
    return "unknown"


def _lifecycle_incremental_matches_episode(
    event: dict[str, Any],
    *,
    episode: dict[str, Any],
) -> bool:
    event_episode_id = text(event.get("episode_id"))
    if event_episode_id:
        return event_episode_id == text(episode.get("episode_id"))
    event_observed_at = text(
        event.get("decision_observed_at_utc")
        or event.get("decision_time_utc")
    )
    if not event_observed_at:
        return False
    try:
        return _strict_utc(event_observed_at) == _strict_utc(
            text(episode.get("observed_at_utc"))
        )
    except ValueError:
        return False


def _usable_horizon_close_mark(mark: dict[str, Any]) -> bool:
    return (
        text(mark.get("quote_status")).lower() == "matched"
        and first_float(mark, "ask") is not None
        and text(mark.get("point_in_time_status")).lower() == "verified_fresh_collection"
    )


def _usable_expiry_close_mark(mark: dict[str, Any]) -> bool:
    return (
        text(mark.get("point_in_time_status")).lower() == "verified_fresh_collection"
        and (first_float(mark, "spot") is not None or first_float(mark, "ask") is not None)
    )


def _missing_close_mark_reason(marks: list[dict[str, Any]]) -> str:
    if any(
        text(mark.get("point_in_time_status")).lower() != "verified_fresh_collection"
        for mark in marks
    ):
        return "mark_point_in_time_unverified"
    return "no_usable_mark_in_window"


def _close_mark_time(mark: dict[str, Any]) -> str:
    return text(mark.get("marked_at_utc"))


def _lifecycle_time(fact: dict[str, Any]) -> str:
    raw_ms = first_float(fact, "event_time_ms", "trade_time_ms")
    if raw_ms is not None and raw_ms > 0:
        return datetime.fromtimestamp(raw_ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return text(
        fact.get("event_at_utc")
        or fact.get("event_time_utc")
        or fact.get("trade_time_utc")
        or fact.get("closed_at_utc")
    )


def _strict_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid lifecycle timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timezone required for lifecycle timestamp: {value}")
    return parsed.astimezone(timezone.utc)


def _read_close_lifecycle_facts(paths: list[str | Path] | tuple[str | Path, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"close lifecycle source does not exist: {path}")
        if path.suffix.lower() == ".csv":
            rows = read_csv_rows(path)
        elif path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                rows = [item for item in payload if isinstance(item, dict)]
            elif isinstance(payload, dict):
                raw_rows = payload.get("events") or payload.get("rows") or payload.get("facts")
                rows = [item for item in raw_rows if isinstance(item, dict)] if isinstance(raw_rows, list) else [payload]
            else:
                raise ValueError(f"close lifecycle JSON must contain objects: {path}")
        else:
            rows = read_jsonl(path)
        out.extend(dict(row, _source_path=str(path)) for row in rows)
    return out


def _merge_close_outcomes(
    existing: list[dict[str, Any]],
    generated: list[dict[str, Any]],
    *,
    replace: bool,
) -> list[dict[str, Any]]:
    by_key = {
        (text(row.get("episode_id")), text(row.get("outcome_kind"))): row
        for row in existing
        if text(row.get("episode_id")) and text(row.get("outcome_kind"))
    }
    for row in generated:
        key = (text(row.get("episode_id")), text(row.get("outcome_kind")))
        existing_row = by_key.get(key)
        if (
            isinstance(existing_row, dict)
            and text(existing_row.get("evidence_status")).lower() == "usable"
            and text(row.get("inconclusive_reason")).lower()
            in {"lifecycle_or_expiry_fact_missing", "no_usable_mark_in_window"}
        ):
            continue
        by_key[key] = row
    return [by_key[key] for key in sorted(by_key)]


def _merge_outcome_facts(
    existing: list[dict[str, Any]],
    generated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Monotonically merge one current fact per decision occurrence."""

    by_key: dict[str, dict[str, Any]] = {}
    unbound: list[dict[str, Any]] = []
    for row in existing:
        key = decision_instance_key(row)
        if not key:
            unbound.append(row)
            continue
        current = by_key.get(key)
        if current is None or _outcome_can_supersede(current, row):
            by_key[key] = row
    for row in generated:
        key = decision_instance_key(row)
        if not key:
            unbound.append(row)
            continue
        current = by_key.get(key)
        if current is None or _outcome_can_supersede(current, row):
            by_key[key] = row
    return unbound + [by_key[key] for key in sorted(by_key)]


def _outcome_strength(row: dict[str, Any]) -> int:
    if is_complete_closed_outcome(row):
        return 4
    outcome = text(row.get("outcome")).lower()
    if outcome in {
        "expired_worthless",
        "assigned_at_expiry",
        "called_away_at_expiry",
        "expired_in_the_money",
        "assigned",
        "called_away",
        "closed_later",
    }:
        return 3
    if outcome == "counterfactual_mark_to_market":
        return 1
    return 2 if first_float(row, "realized_pnl", "lifecycle_pnl_net") is not None else 0


def _outcome_can_supersede(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    existing_strength = _outcome_strength(existing)
    candidate_strength = _outcome_strength(candidate)
    if candidate_strength != existing_strength:
        return candidate_strength > existing_strength
    return text(candidate.get("final_mark_at") or candidate.get("observed_at_utc")) > text(
        existing.get("final_mark_at") or existing.get("observed_at_utc")
    )


def _mark_outcome_strength(
    candidates: list[dict[str, Any]],
    mark: dict[str, Any],
) -> int:
    if any(is_expiration_mark(candidate, mark) for candidate in candidates):
        return 3
    return 1


def _nested(source: dict[str, Any], *keys: str) -> Any:
    current: Any = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def derive_outcome_facts(
    candidate_snapshots: list[dict[str, Any]],
    mark_snapshots: list[dict[str, Any]],
    *,
    existing_outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidate_snapshots = freeze_decision_identities(candidate_snapshots)
    mark_snapshots = bind_legacy_decision_evidence(candidate_snapshots, mark_snapshots)
    existing_outcomes = bind_legacy_decision_evidence(candidate_snapshots, existing_outcomes)
    marks_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mark in mark_snapshots:
        if not is_usable_mark(mark):
            continue
        key = decision_instance_key(mark)
        if key:
            marks_by_key[key].append(mark)
    existing_by_key = {
        decision_instance_key(row): row
        for row in existing_outcomes
        if decision_instance_key(row)
    }
    out: list[dict[str, Any]] = []
    for candidate in candidate_snapshots:
        key = decision_instance_key(candidate)
        if not key:
            continue
        marks = marks_by_key.get(key) or []
        if not marks:
            continue
        final_mark = _latest_mark(marks)
        pnl_values = [_mark_pnl_value(row) for row in marks]
        pnl_values = [value for value in pnl_values if value is not None]
        realized_pnl, model, quality, outcome = derive_outcome_result(candidate, final_mark)
        if realized_pnl is None:
            continue
        candidate_outcome = {
            "schema_version": OUTCOME_FACT_SCHEMA_VERSION,
            "source": "derived_from_mark_path",
            "decision_instance_id": key,
            "group_occurrence_id": candidate.get("group_occurrence_id"),
            "run_id": candidate.get("run_id"),
            "instrument_key": instrument_key(candidate),
            "account": candidate.get("account"),
            "symbol": candidate.get("symbol"),
            "contract_symbol": candidate.get("contract_symbol"),
            "option_type": candidate.get("option_type") or candidate.get("mode"),
            "expiration": candidate.get("expiration"),
            "strike": candidate.get("strike"),
            "candidate_status": candidate.get("status"),
            "outcome": outcome,
            "realized_pnl": realized_pnl,
            "pnl_model": model,
            "quality": quality,
            "mark_count": len(marks),
            "first_mark_at": mark_time(_earliest_mark(marks)),
            "final_mark_at": mark_time(final_mark),
            "max_adverse_pnl": min(pnl_values) if pnl_values else None,
            "max_favorable_pnl": max(pnl_values) if pnl_values else None,
            **_lifecycle_fact_payload(
                candidate,
                final_mark,
                outcome=outcome,
            ),
            "writes_runtime_config": False,
            "writes_trade_state": False,
        }
        existing = existing_by_key.get(key)
        if existing is not None and not _outcome_can_supersede(existing, candidate_outcome):
            continue
        candidate_outcome["revision"] = int(first_float(existing or {}, "revision") or 0) + 1
        if existing is not None:
            candidate_outcome["supersedes"] = {
                "outcome": existing.get("outcome"),
                "final_mark_at": existing.get("final_mark_at"),
                "revision": existing.get("revision"),
            }
        out.append(candidate_outcome)
    return out


def _lifecycle_fact_payload(
    candidate: dict[str, Any],
    final_mark: dict[str, Any],
    *,
    outcome: str,
) -> dict[str, Any]:
    fields = (
        "lifecycle_pnl_net",
        "capital_days",
        "annualized_capital_efficiency",
        "fee_basis",
        "fee_missing_components",
        "covered_call_allocation_status",
        "allocation_quality",
        "lifecycle_quality",
    )
    payload = {
        field: _first_lifecycle_value(final_mark, candidate, field)
        for field in fields
    }
    if not text(payload.get("lifecycle_quality")):
        payload["lifecycle_quality"] = (
            "transition_only"
            if outcome in {"assigned_at_expiry", "called_away_at_expiry"}
            else "closed_incomplete"
        )
    payload["production_parameter_eligible"] = is_complete_closed_outcome(payload)
    if not payload["production_parameter_eligible"]:
        payload["production_parameter_blocker"] = "fee_complete_closed_lifecycle_required"
    return payload


def _first_lifecycle_value(primary: dict[str, Any], fallback: dict[str, Any], field: str) -> Any:
    for source in (primary, fallback):
        if field in source and source.get(field) is not None:
            return source.get(field)
    return None


def is_complete_closed_outcome(payload: dict[str, Any]) -> bool:
    if text(payload.get("lifecycle_quality")).lower() != "complete_closed":
        return False
    if first_float(payload, "lifecycle_pnl_net") is None:
        return False
    capital_days = first_float(payload, "capital_days")
    if capital_days is None or capital_days <= 0:
        return False
    if text(payload.get("fee_basis")).lower() not in {"actual", "estimated", "mixed"}:
        return False
    missing = payload.get("fee_missing_components")
    if isinstance(missing, (list, tuple, set, dict)) and missing:
        return False
    if not isinstance(missing, (list, tuple, set, dict)) and text(missing).lower() not in {
        "",
        "[]",
        "{}",
        "none",
        "null",
        "nan",
    }:
        return False
    allocation = text(payload.get("covered_call_allocation_status") or payload.get("allocation_quality")).lower()
    return allocation not in {"unallocated", "mixed", "ambiguous", "missing"}


def outcome_gap_summary(
    candidate_snapshots: list[dict[str, Any]],
    mark_snapshots: list[dict[str, Any]],
    outcome_facts: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Classify missing outcomes by the next action that can actually resolve them."""

    candidate_snapshots = freeze_decision_identities(candidate_snapshots)
    mark_snapshots = bind_legacy_decision_evidence(candidate_snapshots, mark_snapshots)
    outcome_facts = bind_legacy_decision_evidence(candidate_snapshots, outcome_facts)
    candidates_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identity_missing_count = 0
    for candidate in candidate_snapshots:
        key = decision_instance_key(candidate)
        if not key:
            identity_missing_count += 1
            continue
        candidates_by_key[key].append(candidate)

    marks_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mark in mark_snapshots:
        key = decision_instance_key(mark)
        if key:
            marks_by_key[key].append(mark)

    outcome_by_key = {
        decision_instance_key(row): row
        for row in outcome_facts
        if decision_instance_key(row)
    }
    ready_to_settle: list[str] = []
    needs_mark: list[str] = []
    blocked: list[tuple[str, str]] = []
    for key, candidates in candidates_by_key.items():
        usable_marks = [row for row in marks_by_key.get(key, []) if is_usable_mark(row)]
        existing_outcome = outcome_by_key.get(key)
        if usable_marks:
            final_mark = _latest_mark(usable_marks)
            if (
                existing_outcome is not None
                and _outcome_strength(existing_outcome)
                >= _mark_outcome_strength(candidates, final_mark)
            ):
                continue
            failures: Counter[str] = Counter()
            for candidate in candidates:
                realized_pnl, _model, quality, _outcome = derive_outcome_result(candidate, final_mark)
                if realized_pnl is not None:
                    ready_to_settle.append(key)
                    break
                failures[quality or "missing_settlement_inputs"] += 1
            else:
                if not any(_candidate_has_entry_premium(candidate) for candidate in candidates):
                    reason = "missing_entry_premium"
                else:
                    reason = failures.most_common(1)[0][0] if failures else "missing_settlement_inputs"
                blocked.append((key, reason))
            continue

        if existing_outcome is not None:
            continue
        if not any(_candidate_has_entry_premium(candidate) for candidate in candidates):
            blocked.append((key, "missing_entry_premium"))
            continue
        expirations = [
            parse_date(text(candidate.get("expiration") or candidate.get("exp")))
            for candidate in candidates
        ]
        expirations = [value for value in expirations if value is not None]
        if not expirations:
            blocked.append((key, "missing_expiration"))
        elif max(expirations) < now.date():
            blocked.append((key, "expired_without_usable_mark"))
        else:
            needs_mark.append(key)

    blocker_counts = Counter(reason for _key, reason in blocked)
    missing_count = len(ready_to_settle) + len(needs_mark) + len(blocked)
    return {
        "schema_version": "shadow_replay_outcome_gaps.v1",
        "missing_outcome_instrument_count": missing_count,
        "ready_to_settle_count": len(ready_to_settle),
        "needs_mark_count": len(needs_mark),
        "blocked_count": len(blocked),
        "identity_missing_candidate_count": identity_missing_count,
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "ready_to_settle_examples": sorted(ready_to_settle)[:10],
        "needs_mark_examples": sorted(needs_mark)[:10],
        "blocked_examples": [
            {"instrument_key": key, "reason": reason}
            for key, reason in sorted(blocked)[:10]
        ],
    }


def _candidate_has_entry_premium(candidate: dict[str, Any]) -> bool:
    side = text(candidate.get("side") or candidate.get("position_side")).lower() or "short"
    return _entry_premium(candidate, side=side) is not None


def derive_outcome_result(candidate: dict[str, Any], final_mark: dict[str, Any]) -> tuple[float | None, str, str, str]:
    expiration_pnl, expiration_model, expiration_quality, expiration_outcome = _derive_expiration_pnl(candidate, final_mark)
    if expiration_pnl is not None:
        return expiration_pnl, expiration_model, expiration_quality, expiration_outcome
    mark_pnl, mark_model, mark_quality = _derive_realized_pnl(candidate, final_mark)
    return mark_pnl, mark_model, mark_quality, "counterfactual_mark_to_market"


def is_usable_mark(row: dict[str, Any]) -> bool:
    point_in_time_status = text(row.get("point_in_time_status")).lower()
    if point_in_time_status != "verified_fresh_collection":
        return False
    if text(row.get("quote_status")).lower() == "missing_quote":
        return False
    quality = text(row.get("mark_quality")).lower()
    if quality == "missing_quote":
        return False
    if quality == "missing_mid" and not (is_expiration_mark(row, row) and expiration_intrinsic_value(row, row) is not None):
        return False
    if _mark_pnl_value(row) is not None:
        return True
    if first_float(row, "option_mid", "mid", "mark", "option_price", "close_price", "last_price") is not None:
        return True
    if is_expiration_mark(row, row) and expiration_intrinsic_value(row, row) is not None:
        return True
    bid = first_float(row, "bid")
    ask = first_float(row, "ask")
    return bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid


def expiration_intrinsic_value(candidate: dict[str, Any], final_mark: dict[str, Any]) -> float | None:
    option_type = text(candidate.get("option_type") or candidate.get("mode") or final_mark.get("option_type") or final_mark.get("mode")).lower()
    strike = first_float(candidate, "strike") or first_float(final_mark, "strike")
    spot = first_float(final_mark, "spot", "underlying_price")
    if option_type not in {"put", "call"} or strike is None or spot is None:
        return None
    if option_type == "put":
        return max(strike - spot, 0.0)
    return max(spot - strike, 0.0)


def mark_time(row: dict[str, Any]) -> str | None:
    return text(row.get("mark_at") or row.get("as_of") or row.get("timestamp") or row.get("time") or row.get("date")) or None


def _derive_expiration_pnl(candidate: dict[str, Any], final_mark: dict[str, Any]) -> tuple[float | None, str, str, str]:
    if not is_expiration_mark(candidate, final_mark):
        return None, "unavailable", "not_expiration_mark", "counterfactual_mark_to_market"
    side = text(candidate.get("side") or candidate.get("position_side")).lower() or "short"
    entry_credit = _entry_premium(candidate, side=side)
    intrinsic = expiration_intrinsic_value(candidate, final_mark)
    if entry_credit is None or intrinsic is None:
        return None, "unavailable", "missing_entry_credit_or_expiration_intrinsic", "expiration_unavailable"
    contracts = first_float(candidate, "contracts", "contract_count") or 1.0
    multiplier = first_float(candidate, "multiplier") or first_float(final_mark, "multiplier") or 100.0
    intrinsic_value = intrinsic * multiplier * contracts
    if side == "long":
        return intrinsic_value - entry_credit, "long_option_expiration_intrinsic_minus_entry_cost", "derived_from_expiration_spot", _expiration_outcome(candidate, intrinsic=intrinsic, side=side)
    return entry_credit - intrinsic_value, "short_option_entry_credit_minus_expiration_intrinsic", "derived_from_expiration_spot", _expiration_outcome(candidate, intrinsic=intrinsic, side=side)


def is_expiration_mark(candidate: dict[str, Any], final_mark: dict[str, Any]) -> bool:
    dte = first_float(final_mark, "dte")
    if dte is not None and dte <= 0:
        return True
    expiration = parse_date(text(candidate.get("expiration") or candidate.get("exp") or final_mark.get("expiration") or final_mark.get("exp")))
    mark_date = parse_date(mark_time(final_mark) or "")
    return bool(expiration and mark_date and mark_date >= expiration)


def _expiration_outcome(candidate: dict[str, Any], *, intrinsic: float, side: str) -> str:
    if intrinsic <= 0:
        return "expired_worthless"
    option_type = text(candidate.get("option_type") or candidate.get("mode")).lower()
    if side == "short" and option_type == "put":
        return "assigned_at_expiry"
    if side == "short" and option_type == "call":
        return "called_away_at_expiry"
    return "expired_in_the_money"


def _derive_realized_pnl(candidate: dict[str, Any], final_mark: dict[str, Any]) -> tuple[float | None, str, str]:
    mark_pnl = _mark_pnl_value(final_mark)
    if mark_pnl is not None:
        return mark_pnl, "mark_pnl", "derived_from_mark_pnl"
    side = text(candidate.get("side") or candidate.get("position_side")).lower() or "short"
    entry_credit = _entry_premium(candidate, side=side)
    exit_price = first_float(final_mark, "option_mid", "mid", "mark", "option_price", "close_price")
    if entry_credit is None or exit_price is None:
        return None, "unavailable", "missing_entry_credit_or_exit_price"
    contracts = first_float(candidate, "contracts", "contract_count") or 1.0
    multiplier = first_float(candidate, "multiplier") or first_float(final_mark, "multiplier") or 100.0
    exit_value = exit_price * multiplier * contracts
    if side == "long":
        return exit_value - entry_credit, "long_option_exit_value_minus_entry_cost", "derived_from_entry_and_exit_price"
    return entry_credit - exit_value, "short_option_entry_credit_minus_exit_value", "derived_from_entry_and_exit_price"


def _entry_premium(candidate: dict[str, Any], *, side: str) -> float | None:
    if side == "long":
        explicit_cost = first_float(candidate, "entry_cost", "call_total_cost")
        if explicit_cost is not None:
            return abs(explicit_cost)
        signed_value = first_float(candidate, "net_income", "net_credit", "entry_credit", "premium")
        return abs(signed_value) if signed_value is not None else None
    return first_float(candidate, "net_income", "net_credit", "entry_credit", "premium")


def _mark_pnl_value(row: dict[str, Any]) -> float | None:
    return first_float(row, "unrealized_pnl", "counterfactual_pnl", "realized_pnl", "pnl", "mark_pnl")


def _latest_mark(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=_mark_sort_key)


def _earliest_mark(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return min(rows, key=_mark_sort_key)


def _mark_sort_key(row: dict[str, Any]) -> str:
    return mark_time(row) or ""
