from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
from typing import Any

from src.application.opend_fetch_config import filter_opend_fetch_kwargs
from src.application.opend_symbol_outputs import save_outputs
from src.application.option_chain_fetching import classify_option_chain_error
from src.application.required_data_fetching import RequiredDataFetchRequest, execute_required_data_opend
from src.application.shadow_replay.common import (
    OPTIONAL_CLOSE_DATASET_FILES,
    dataset_dir_from_arg,
    read_jsonl,
    resolve_output_path,
    resolve_path,
    safety_payload,
    text,
    utc_now,
    validate_dataset_integrity,
    write_json,
)
from src.application.shadow_replay.marking import mark_shadow_replay_dataset
from src.application.shadow_replay.settlement import settle_shadow_replay_dataset


def collect_shadow_replay_marks(
    *,
    dataset: str | Path,
    required_data_root: str | Path,
    source: str = "local",
    repo_root: str | Path | None = None,
    opend_base_root: str | Path | None = None,
    opend_fetch_config: dict[str, float | int] | None = None,
    as_of: str | None = None,
    output: str | Path | None = None,
    write: bool = False,
    replace: bool = False,
    settle: bool = False,
    opend_host: str = "127.0.0.1",
    opend_port: int = 11111,
    limit_expirations: int = 8,
    chain_cache: bool = True,
    chain_cache_force_refresh: bool = False,
    include_realized_volatility: bool = False,
    max_symbols: int | None = None,
    fail_fast_on_opend_rate_limit: bool = False,
) -> dict[str, Any]:
    """Collect one replay mark sample from local cache or a fresh OpenD pull."""

    dataset_dir = dataset_dir_from_arg(dataset)
    base = Path(repo_root).expanduser().resolve() if repo_root is not None else dataset_dir
    persistent_fetch_base = (
        Path(opend_base_root).expanduser().resolve()
        if opend_base_root is not None and text(opend_base_root)
        else base
    )
    required_root = resolve_path(required_data_root, base=base)
    source_norm = text(source).lower() or "local"
    if source_norm not in {"local", "opend"}:
        raise ValueError("--source must be local or opend")
    if write:
        validate_dataset_integrity(dataset_dir)

    mark_at = text(as_of) or utc_now()
    mark_time_basis = "operator_asserted_as_of" if text(as_of) else "collection_time"
    candidate_snapshots = read_jsonl(dataset_dir / "candidate_snapshots.jsonl")
    close_episodes = read_jsonl(dataset_dir / OPTIONAL_CLOSE_DATASET_FILES[0])
    combo_decisions = read_jsonl(dataset_dir / "combo_pair_decisions.jsonl")
    quote_subjects = (
        candidate_snapshots
        + _close_quote_subjects(close_episodes)
        + _combo_quote_subjects(combo_decisions)
    )
    with ExitStack() as stack:
        effective_required_root = required_root
        fetch_base = persistent_fetch_base
        if source_norm == "opend" and not write:
            effective_required_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="shadow-replay-required-data-")))
            fetch_base = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="shadow-replay-opend-base-")))

        fetch_summary = _empty_fetch_summary(source=source_norm, candidate_snapshots=quote_subjects)
        if source_norm == "opend":
            fetch_summary = _fetch_required_data_from_opend(
                quote_subjects,
                base=fetch_base,
                required_data_root=effective_required_root,
                host=opend_host,
                port=opend_port,
                limit_expirations=limit_expirations,
                chain_cache=chain_cache,
                chain_cache_force_refresh=chain_cache_force_refresh,
                include_realized_volatility=include_realized_volatility,
                max_symbols=max_symbols,
                fail_fast_on_rate_limit=bool(fail_fast_on_opend_rate_limit),
                opend_fetch_config=opend_fetch_config,
            )

        marking = mark_shadow_replay_dataset(
            dataset=dataset_dir,
            required_data_root=effective_required_root,
            as_of=mark_at,
            repo_root=base,
            write=write,
            replace=replace,
            mark_time_basis=mark_time_basis,
            quote_collection_source=source_norm,
            allowed_required_data_paths=(
                [
                    item["csv_path"]
                    for item in fetch_summary.get("requests") or []
                    if isinstance(item, dict)
                    and text(item.get("status")).lower() == "ok"
                    and text(item.get("csv_path"))
                ]
                if source_norm == "opend"
                else None
            ),
        )
    settlement: dict[str, Any] | None = None
    if settle and write:
        settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True, replace=False)

    persistent_write_targets = _persistent_write_targets(
        source=source_norm,
        write=bool(write),
    )
    safety = safety_payload(writes_local_dataset=bool(write))
    safety["writes_local_dataset_only"] = persistent_write_targets == ["shadow_replay_dataset"]
    safety.update(
        {
            "reads_opend": source_norm == "opend",
            "writes_required_data_cache": source_norm == "opend" and bool(write),
            "writes_persistent_outputs": bool(persistent_write_targets),
            "persistent_write_targets": persistent_write_targets,
        }
    )
    result = {
        "schema_version": "shadow_replay_mark_collection.v1",
        "dataset_dir": str(dataset_dir),
        "required_data_root": str(required_root),
        "generated_at_utc": utc_now(),
        "summary": {
            "status": (
                "deferred"
                if source_norm == "opend"
                and bool(fetch_summary["summary"].get("rate_limit_circuit_open"))
                and int(fetch_summary["summary"].get("non_rate_limit_error_count") or 0) == 0
                else
                "failed"
                if source_norm == "opend"
                and int(fetch_summary["summary"]["ok_count"] or 0) == 0
                and int(fetch_summary["summary"]["error_count"] or 0) > 0
                else "partial_failed"
                if int(fetch_summary["summary"]["error_count"] or 0) > 0
                else "success"
            ),
            "source": source_norm,
            "mark_as_of": mark_at,
            "mark_time_basis": mark_time_basis,
            "candidate_snapshot_count": len(candidate_snapshots),
            "close_decision_episode_count": len(close_episodes),
            "symbol_count": fetch_summary["summary"]["symbol_count"],
            "opend_fetch_attempted": source_norm == "opend",
            "opend_fetch_ok_count": fetch_summary["summary"]["ok_count"],
            "opend_fetch_error_count": fetch_summary["summary"]["error_count"],
            "opend_rate_limit_count": fetch_summary["summary"].get("rate_limit_count", 0),
            "opend_non_rate_limit_error_count": fetch_summary["summary"].get(
                "non_rate_limit_error_count", 0
            ),
            "opend_rate_limit_circuit_open": bool(
                fetch_summary["summary"].get("rate_limit_circuit_open")
            ),
            "opend_fetch_persisted": source_norm == "opend" and bool(write),
            "generated_mark_snapshot_count": marking["summary"]["generated_mark_snapshot_count"],
            "usable_mark_snapshot_count": marking["summary"]["usable_mark_snapshot_count"],
            "missing_quote_count": marking["summary"]["missing_quote_count"],
            "generated_close_mark_count": marking["summary"]["generated_close_mark_count"],
            "usable_close_mark_count": marking["summary"]["usable_close_mark_count"],
            "written": bool(write),
            "settled": bool(settlement is not None),
            "generated_outcome_fact_count": (
                settlement["summary"]["generated_outcome_fact_count"] if settlement is not None else 0
            ),
        },
        "fetch": fetch_summary,
        "marking": marking,
        "settlement": settlement,
        "safety": safety,
    }
    if output:
        write_json(resolve_output_path(output), result)
    return result


def _combo_quote_subjects(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    subjects: list[dict[str, Any]] = []
    for decision in decisions:
        selected = bool(decision.get("baseline_selected")) or any(
            bool(item.get("selected"))
            for item in decision.get("variant_decisions") or []
            if isinstance(item, dict)
        )
        if not selected:
            continue
        common = {
            "symbol": decision.get("symbol"),
            "account": decision.get("account"),
            "multiplier": decision.get("multiplier"),
            "currency": decision.get("currency"),
        }
        subjects.extend(
            [
                {
                    **common,
                    "option_type": "put",
                    "contract_symbol": decision.get("put_contract_symbol"),
                    "expiration": decision.get("put_expiration"),
                    "strike": decision.get("put_strike"),
                },
                {
                    **common,
                    "option_type": "call",
                    "contract_symbol": decision.get("call_contract_symbol"),
                    "expiration": decision.get("call_expiration"),
                    "strike": decision.get("call_strike"),
                },
            ]
        )
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in subjects:
        key = (
            text(row.get("symbol")).upper(),
            text(row.get("contract_symbol")),
        )
        if all(key):
            deduped[key] = row
    return [deduped[key] for key in sorted(deduped)]


def _close_quote_subjects(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for episode in episodes:
        identity = episode.get("position_identity")
        if not isinstance(identity, dict):
            continue
        out.append(
            {
                **identity,
                "account": episode.get("account"),
                "position_lot_id": episode.get("position_lot_id"),
            }
        )
        replacement = episode.get("replacement_evidence")
        if isinstance(replacement, dict) and text(replacement.get("contract_symbol")):
            out.append(
                {
                    "account": episode.get("account"),
                    "symbol": replacement.get("symbol"),
                    "contract_symbol": replacement.get("contract_symbol"),
                    "option_type": replacement.get("option_type") or identity.get("option_type"),
                    "expiration": replacement.get("expiration"),
                    "strike": replacement.get("strike"),
                }
            )
    return out


def _persistent_write_targets(*, source: str, write: bool) -> list[str]:
    if not write:
        return []
    targets = ["shadow_replay_dataset"]
    if source == "opend":
        targets.append("required_data_cache")
        targets.append("opend_rate_limit_state")
        targets.append("opend_cache")
    return targets


def _empty_fetch_summary(*, source: str, candidate_snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    plans = _fetch_plans_from_candidates(candidate_snapshots, host="127.0.0.1", port=11111, limit_expirations=8)
    return {
        "schema_version": "shadow_replay_required_data_fetch.v1",
        "source": source,
        "summary": {
            "candidate_snapshot_count": len(candidate_snapshots),
            "symbol_count": len(plans),
            "requested_symbol_count": 0,
            "ok_count": 0,
            "partial_count": 0,
            "error_count": 0,
            "row_count": 0,
            "skipped_symbol_count": 0,
            "rate_limit_count": 0,
            "non_rate_limit_error_count": 0,
            "rate_limit_circuit_open": False,
        },
        "requests": [],
    }


def _fetch_required_data_from_opend(
    candidate_snapshots: list[dict[str, Any]],
    *,
    base: Path,
    required_data_root: Path,
    host: str,
    port: int,
    limit_expirations: int,
    chain_cache: bool,
    chain_cache_force_refresh: bool,
    include_realized_volatility: bool,
    max_symbols: int | None,
    fail_fast_on_rate_limit: bool,
    opend_fetch_config: dict[str, float | int] | None,
) -> dict[str, Any]:
    plans = _fetch_plans_from_candidates(
        candidate_snapshots,
        host=host,
        port=port,
        limit_expirations=limit_expirations,
    )
    requested = plans
    skipped = []
    if max_symbols is not None and int(max_symbols) >= 0 and len(plans) > int(max_symbols):
        requested = plans[: int(max_symbols)]
        skipped = plans[int(max_symbols) :]

    requests: list[dict[str, Any]] = []
    fetch_kwargs = filter_opend_fetch_kwargs(opend_fetch_config)
    for plan in requested:
        request = RequiredDataFetchRequest(
            symbol=plan["symbol"],
            limit_expirations=plan["limit_expirations"],
            host=host,
            port=int(port),
            output_root=required_data_root,
            option_types=",".join(plan["option_types"]) if plan["option_types"] else "put,call",
            explicit_expirations=plan["explicit_expirations"] or None,
            chain_cache=bool(chain_cache),
            chain_cache_force_refresh=bool(chain_cache_force_refresh),
            freshness_policy=("force_refresh" if chain_cache_force_refresh else "cache_first"),
            include_realized_volatility=bool(include_realized_volatility),
            no_retry=bool(fail_fast_on_rate_limit),
            **fetch_kwargs,
        )
        item = {
            "symbol": plan["symbol"],
            "option_types": plan["option_types"],
            "explicit_expirations": plan["explicit_expirations"],
            "limit_expirations": plan["limit_expirations"],
            "status": "error",
            "rows": 0,
            "expiration_count": 0,
            "raw_path": None,
            "csv_path": None,
            "error": None,
            "error_code": None,
            "source_outcome": None,
            "reason_code": None,
        }
        try:
            payload = execute_required_data_opend(base=base, request=request)
            raw_path, csv_path = save_outputs(base, plan["symbol"], payload, output_root=required_data_root)
            meta = payload.get("meta") if isinstance(payload, dict) else {}
            meta = meta if isinstance(meta, dict) else {}
            status = text(meta.get("status") or "ok").lower() or "ok"
            item.update(
                {
                    "status": status,
                    "rows": len(payload.get("rows") or []) if isinstance(payload, dict) else 0,
                    "expiration_count": int(payload.get("expiration_count") or 0) if isinstance(payload, dict) else 0,
                    "raw_path": str(raw_path),
                    "csv_path": str(csv_path),
                    "error": meta.get("error"),
                    "error_code": meta.get("error_code"),
                    "source_outcome": meta.get("source_outcome"),
                    "reason_code": meta.get("reason_code"),
                }
            )
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            item["error_code"] = classify_option_chain_error(exc)
        requests.append(item)
        if fail_fast_on_rate_limit and text(item.get("error_code")).upper() == "RATE_LIMIT":
            skipped.extend(requested[len(requests) :])
            break

    ok_count = sum(1 for item in requests if item["status"] == "ok")
    partial_count = sum(1 for item in requests if item["status"] == "partial")
    error_count = sum(1 for item in requests if item["status"] not in {"ok", "partial"})
    rate_limit_count = sum(
        1 for item in requests if text(item.get("error_code")).upper() == "RATE_LIMIT"
    )
    non_rate_limit_error_count = sum(
        1
        for item in requests
        if item["status"] not in {"ok", "partial"}
        and text(item.get("error_code")).upper() != "RATE_LIMIT"
    )
    rate_limit_circuit_open = bool(fail_fast_on_rate_limit and rate_limit_count)
    return {
        "schema_version": "shadow_replay_required_data_fetch.v1",
        "source": "opend",
        "opend_fetch_config": fetch_kwargs,
        "summary": {
            "candidate_snapshot_count": len(candidate_snapshots),
            "symbol_count": len(plans),
            "requested_symbol_count": len(requests),
            "ok_count": ok_count,
            "partial_count": partial_count,
            "error_count": error_count,
            "rate_limit_count": rate_limit_count,
            "non_rate_limit_error_count": non_rate_limit_error_count,
            "rate_limit_circuit_open": rate_limit_circuit_open,
            "row_count": sum(int(item.get("rows") or 0) for item in requests),
            "skipped_symbol_count": len(skipped),
        },
        "requests": requests,
        "skipped_symbols": [plan["symbol"] for plan in skipped],
        "stop_reason": "opend_rate_limited" if rate_limit_circuit_open else None,
    }


def _fetch_plans_from_candidates(
    rows: list[dict[str, Any]],
    *,
    host: str,
    port: int,
    limit_expirations: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = text(row.get("symbol") or row.get("underlying_symbol")).upper()
        if not symbol:
            continue
        item = grouped.setdefault(symbol, {"symbol": symbol, "option_types": set(), "explicit_expirations": set()})
        option_type = text(row.get("option_type") or row.get("mode")).lower()
        if option_type in {"put", "call"}:
            item["option_types"].add(option_type)
        expiration = text(row.get("expiration") or row.get("exp"))
        if expiration:
            item["explicit_expirations"].add(expiration[:10])

    plans = []
    default_limit = max(1, int(limit_expirations or 8))
    for symbol in sorted(grouped):
        item = grouped[symbol]
        expirations = sorted(item["explicit_expirations"])
        plans.append(
            {
                "symbol": symbol,
                "host": host,
                "port": int(port),
                "option_types": sorted(item["option_types"]) or ["put", "call"],
                "explicit_expirations": expirations,
                "limit_expirations": max(default_limit, len(expirations) or 0),
            }
        )
    return plans
