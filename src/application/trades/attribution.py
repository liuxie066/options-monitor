from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableMapping, Sequence
from copy import deepcopy
import json
import time
from pathlib import Path

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.strategy_membership import trade_attribution_conflict_execution_keys as _conflict_execution_keys
from src.application.agent_tool_config import DEFAULT_CONFIGS, load_runtime_config, repo_base
from src.application.runtime_config_freshness import RUNTIME_MARKETS, ensure_runtime_config_freshness
from src.application.agent_tool_contracts import AgentToolError
from src.application.futu_portfolio_context import infer_futu_portfolio_settings, resolve_futu_account_ids
from src.application.ledger.api import (
    apply_trade_attribution as apply_trade_attribution,
    build_trade_attribution_view as build_trade_attribution_view,
    ledger_resource_identity, open_trade_reconciliation_evidence_repo,
    resolve_ledger_store, resolve_position_data_config_path,
    ledger_store_write_guard,
    preview_trade_attribution_migration, apply_trade_attribution_migration,
    read_trade_attribution_snapshot, open_trade_attribution_snapshot_reader, trade_attribution_facts_from_events,
    encode_evidence_cursor, decode_evidence_cursor, TradeEventPaginationError,
    combo_attribution_candidates_from_rows, ATTRIBUTION_POLICY_VERSION,
    record_trade_attribution_conflicts, ordinary_attribution_window,
)
from src.application.futu_quote_routing import runtime_config_market
from src.application.write_contract import write_control
from src.application.trades.account_mapping import combo_reconciliation_mode_for_account
from domain.domain.symbol_identity import resolve_symbol_identity, symbol_market
from src.application.wheel.read_model import build_wheel_read_model_from_rows
from src.application.daily_decision_brief_repository import DailyBriefReadScope, read_combo_candidate_exposures


def attribution_result_payload(fact: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: fact.get(key) for key in ("schema_version", "execution_key", "open_event_id", "lot_id", "account",
        "status", "strategy", "wheel_branch_id", "strategy_group_id", "origin", "reason_codes", "candidate_ids",
        "input_hash", "policy_version", "evaluated_at_ms", "ledger_event_ids", "coverage", "direction",
        "rules_enabled", "evidence_complete", "selected_candidate_id")}
    if fact.get("wheel_call_allocations"):
        payload["wheel_call_allocations"] = fact["wheel_call_allocations"]
    return payload


def attribution_focus_open_event_id(rows: Mapping[str, Any], *, account: str, execution_key: str) -> str | None:
    """Resolve only a unique execution; retain all ledger facts for competition."""
    if not execution_key:
        return None
    matches = [row["open_event_id"] for row in trade_attribution_facts_from_events(rows["trade_events"], account=account)
               if row["execution_key"] == execution_key]
    return matches[0] if len(matches) == 1 else None


def read_attribution_combo_evidence(rows: Mapping[str, Any], *, account: str, runtime_root: Path,
                                    now_ms: int, focus_open_event_id: str | None = None) -> dict[str, Any]:
    preview = combo_attribution_candidates_from_rows(rows, account=account, runtime_environment="",
        exposures=[], effective_now_ms=now_ms, include_claimed=True)
    scopes = {(item["market"], item["market_date"]) for item in preview["lot_facts"]
              if focus_open_event_id is None or item["open_event_id"] == focus_open_event_id}
    exposures, reads = {}, []
    read_scopes = {}
    for market, market_date in sorted(scopes):
        if market not in read_scopes:
            read_scopes[market] = DailyBriefReadScope(base=runtime_root, account=account, market=market)
        result = read_combo_candidate_exposures(base=runtime_root, account=account, market=market,
                                                market_trading_date=market_date, read_scope=read_scopes[market])
        reads.append({"market": market, "market_date": market_date, "complete": (
            result.get("available") is True and result.get("complete") is True
            and result.get("delivery_available") is True and result.get("reason") in {None, "ok"}
            and not result.get("invalid_revisions"))})
        for exposure in result.get("exposures") or []:
            key = exposure.get("candidate_exposure_id")
            if key in exposures and exposures[key] != exposure:
                reads[-1]["complete"] = False
            if key:
                exposures[key] = dict(exposure)
    return {"complete": all(item["complete"] for item in reads), "reads": reads,
            "exposures": [exposures[key] for key in sorted(exposures)]}


def read_trade_attribution_context(repo: Any, *, config: Mapping[str, Any], account: str,
                                   runtime_root: Path, execution_key: str = "",
                                   snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from src.application.wheel.capacity import observe_trade_attribution_capacity
    observation = observe_trade_attribution_capacity(config=dict(config), account=account, runtime_root=runtime_root)
    market = runtime_config_market(config).lower()
    rows = snapshot if snapshot is not None else read_trade_attribution_snapshot(repo, account=account, market=market)
    evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=runtime_root,
        now_ms=int(time.time() * 1000),
        focus_open_event_id=attribution_focus_open_event_id(rows, account=account, execution_key=execution_key))
    return {"config": config, "market": market, "combo_evidence": evidence,
        "capacity_observation": observation, "combo_mode": combo_reconciliation_mode_for_account(config, account=account)}


def observe_combo_confirmation_receipt_route(
    *, config: Mapping[str, Any], account: str, runtime_root: Path,
) -> dict[str, Any]:
    from src.application.account_config import accounts_from_config, resolve_account_trade_intake_enabled
    from src.application.trades.account_mapping import resolve_trade_intake_config
    from src.application.trades.lifecycle_batch_dispatcher import resolve_lifecycle_receipt_dispatch_scope
    from src.application.trades.receipt import resolve_trade_lifecycle_notification_batch_route

    if (account not in accounts_from_config(dict(config), fallback=())
            or not resolve_account_trade_intake_enabled(config, account=account)):
        return {"route_available": False, "reason": "account_receipt_disabled"}
    scope = resolve_lifecycle_receipt_dispatch_scope(resolve_trade_intake_config(dict(config)))
    if account not in scope["allowed_accounts"]:
        return {"route_available": False, "reason": "account_receipt_disabled"}
    try:
        route = resolve_trade_lifecycle_notification_batch_route(config=dict(config), base=runtime_root)
        return {key: route.get(key) for key in (
            "route_available", "provider", "channel", "target_fingerprint",
            "route_fingerprint", "attribution_route_key",
        )}
    except Exception as exc:
        return {"route_available": False, "reason": "receipt_route_unavailable:" + type(exc).__name__}


def apply_referenced_trade_attribution(repo: Any, *, account: str, config: Mapping[str, Any],
    runtime_root: Path, expected_input_hash: str, request_id: str, actor: str, apply_changes: bool,
    option_lot_id: str | None = None, wheel_branch_id: str | None = None, stock_lot_id: str | None = None,
    direction: str | None = None, linkage_candidate_id: str | None = None,
    expected_batch_generation_hash: str | None = None, inference_id: str | None = None,
) -> dict[str, Any]:
    """Adapt existing lot/inference references; admission and writing have one owner.

    expected_input_hash comes from trade_attribution_read(prepare_confirmation=True).
    Historical linkage/proposal hashes cannot authorize a new attribution decision.
    """
    context = {"config": config, "market": runtime_config_market(config).lower(),
        "combo_mode": combo_reconciliation_mode_for_account(config, account=account),
        "combo_evidence": {}, "capacity_observation": {}}
    rows = read_trade_attribution_snapshot(repo, account=account, market=context["market"])
    if inference_id:
        pairs = [row for row in rows["account_combo_inferences"] if row["inference_id"] == inference_id]
        if len(pairs) != 1:
            raise ValueError("combo inference is unavailable")
        pair = pairs[0]
        option_lot_id = pair["put_record_id"]
        candidate_id = "combo:" + pair["strategy_group_id"]
        reference = {"kind": "combo", "inference_id": inference_id}
    else:
        if stock_lot_id:
            model = build_wheel_read_model_from_rows(rows, account=account, market=context["market"],
                as_of_ms=int(time.time() * 1000))
            branches = [row for row in model["wheel_branches"] if row.get("stock_lot_id") == stock_lot_id]
            if len(branches) != 1:
                raise ValueError("Wheel stock lot has no unique branch")
            wheel_branch_id = branches[0]["wheel_branch_id"]
        candidate_id = "wheel:" + str(wheel_branch_id or "")
        reference = {"kind": "wheel", "direction": direction, "linkage_candidate_id": linkage_candidate_id,
            "batch_generation_hash": expected_batch_generation_hash}
    facts = [row for row in trade_attribution_facts_from_events(rows["trade_events"], account=account)
             if row["lot_id"] == option_lot_id]
    if len(facts) != 1 or not facts[0]["execution_key"]:
        raise ValueError("attribution source execution identity is unavailable")
    if not any((row.get("raw_payload") or {}).get("attribution_request_id") == request_id for row in rows["trade_events"]):
        context = read_trade_attribution_context(repo, config=config, account=account, runtime_root=runtime_root)
    result = apply_trade_attribution(repo, account=account, execution_key=facts[0]["execution_key"],
        candidate_id=candidate_id, expected_input_hash=expected_input_hash, request_id=request_id,
        actor=actor, apply_changes=apply_changes, manual=True, reference=reference,
        confirmation_receipt_route=(observe_combo_confirmation_receipt_route(
            config=config, account=account, runtime_root=runtime_root) if inference_id else None), **context)
    return {**result, "attribution_status": result["status"], "dry_run": not apply_changes,
        "request_id": request_id, "option_record_id": option_lot_id,
        "event_id": result["proof_event_ids"][0],
        "status": (("dry_run" if not apply_changes else "adopted" if result["write_applied"] else "already_confirmed")
                   if inference_id else ("planned" if not apply_changes else "confirmed" if result["write_applied"] else "idempotent"))}


def reconcile_trade_attribution_account(
    repo: Any, *, config: Mapping[str, Any], account: str, market: str, runtime_root: Path,
    inbox_path: Path, combo_mode: str, cursor: str = "", stop_event: Any = None,
) -> dict[str, Any]:
    with open_trade_attribution_snapshot_reader(repo, account=account, market=market) as read_if_changed:
        return _reconcile_trade_attribution_batch(repo, config=config, account=account, market=market,
            runtime_root=runtime_root, inbox_path=inbox_path, combo_mode=combo_mode, cursor=cursor,
            stop_event=stop_event, read_if_changed=read_if_changed)


def _reconcile_trade_attribution_batch(
    repo: Any, *, config: Mapping[str, Any], account: str, market: str, runtime_root: Path,
    inbox_path: Path, combo_mode: str, cursor: str, stop_event: Any, read_if_changed: Any,
) -> dict[str, Any]:
    from src.application.wheel.capacity import observe_trade_attribution_capacity
    from src.application.trades.inbox import cache_trade_attribution_result

    rows = read_if_changed()
    if rows is None:
        raise RuntimeError("initial trade attribution snapshot is unavailable")
    facts = trade_attribution_facts_from_events(rows["trade_events"], account=account)
    selected = sorted((row for row in facts if row["execution_key"] and row["execution_key"] > cursor
        and str(symbol_market(row["contract_key"]["underlying_symbol"]) or "").lower() == market),
        key=lambda row: row["execution_key"])[:100]
    if not selected or stop_event is not None and stop_event.is_set():
        return {"status": "idle", "checked": 0, "next_cursor": ""}
    result = {"checked": 0, "linked": 0, "ordinary": 0, "conflicts": 0, "cache_updates": 0, "errors": [], "next_cursor": cursor}
    evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=runtime_root, now_ms=int(time.time() * 1000))
    if stop_event is not None and stop_event.is_set():
        return result
    now_ms = int(time.time() * 1000)
    history_starts = [ordinary_attribution_window(fact)[0] for fact in selected
        if fact["position_side"] == "short" and fact["status"] == "pending" and fact["origin"] != "manual"
        and fact["contracts_open"] > 0 and not fact["reason_codes"] and ordinary_attribution_window(fact)[1] <= now_ms]
    observation = observe_trade_attribution_capacity(config=dict(config), account=account, runtime_root=runtime_root,
        stop_event=stop_event, **({"ordinary_history_start_ms": min(history_starts)} if history_starts else {}))
    if stop_event is not None and stop_event.is_set():
        return result
    view = None
    for selected_fact in selected:
        if stop_event is not None and stop_event.is_set():
            break
        # Every committed change (including in-place fees) invalidates the
        # observation. After attempted effects, force a fresh snapshot as well.
        fresh = read_if_changed(force=evidence is None)
        if stop_event is not None and stop_event.is_set():
            break
        if fresh is not None and (fresh != rows or evidence is None):
            rows = fresh
            evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=runtime_root,
                now_ms=int(time.time() * 1000))
            view = None
        if stop_event is not None and stop_event.is_set():
            break
        if view is None:
            view = build_trade_attribution_view(rows, config=config, account=account, market=market,
                now_ms=int(time.time() * 1000), combo_evidence=evidence,
                capacity_observation=observation, combo_mode=combo_mode)
        current = next((row for row in view["rows"] if row["execution_key"] == selected_fact["execution_key"]), None)
        if current is None:
            continue
        current = deepcopy(current)
        if stop_event is not None and stop_event.is_set():
            break
        action_attempted = False
        try:
            if current["selected_candidate_id"]:
                action_attempted = True
                request_id = "trade-attribution:" + canonical_sha256({"policy": ATTRIBUTION_POLICY_VERSION,
                    "execution": current["execution_key"], "candidate": current["selected_candidate_id"]})
                applied = apply_trade_attribution(repo, account=account, market=market, config=config,
                    execution_key=current["execution_key"], candidate_id=current["selected_candidate_id"],
                    expected_input_hash=current["input_hash"], request_id=request_id, actor="trade_intake:attribution_rule",
                    combo_evidence=evidence, capacity_observation=observation, combo_mode=combo_mode, stop_event=stop_event,
                    confirmation_receipt_route=(observe_combo_confirmation_receipt_route(
                        config=config, account=account, runtime_root=runtime_root)
                        if current["selected_candidate_id"].startswith("combo:") else None))
                committed = read_trade_attribution_snapshot(repo, account=account, market=market)
                after_view = build_trade_attribution_view(committed, config=config, account=account, market=market,
                    now_ms=int(time.time() * 1000), combo_evidence=evidence, capacity_observation=observation, combo_mode=combo_mode)
                current = next(row for row in after_view["rows"] if row["execution_key"] == current["execution_key"])
                result["ordinary" if current["status"] == "ordinary" else "linked"] += int(applied["write_applied"])
            elif (current["status"] == "conflict" and current["rules_enabled"]
                    and "unresolved_attribution_conflict" not in current["reason_codes"]):
                action_attempted = True
                events = record_trade_attribution_conflicts(
                    repo, account=account, market=market, config=config,
                    execution_key=current["execution_key"], expected_input_hash=current["input_hash"],
                    combo_evidence=evidence, capacity_observation=observation,
                    combo_mode=combo_mode, stop_event=stop_event,
                )
                current["ledger_event_ids"] = sorted(set(current["ledger_event_ids"]) | set(events))
                result["conflicts"] += 1
            result["cache_updates"] += cache_trade_attribution_result(inbox_path,
                execution_key=current["execution_key"], result=attribution_result_payload(current))
        except Exception as exc:
            result["errors"].append({"execution_key": current["execution_key"], "error": type(exc).__name__})
        finally:
            # Writers recheck snapshot/time/hash in their own transaction. Even a
            # failed attempt may have changed facts; never reuse its old view.
            if action_attempted:
                view = None
                evidence = None
        result["checked"] += 1
        result["next_cursor"] = current["execution_key"]
    if len(selected) < 100 and result["checked"] == len(selected):
        result["next_cursor"] = ""
    return result


def bind_attribution_market_config(
    *, config: Mapping[str, Any], repo: Any, account: str, market: str, runtime_root: Path,
) -> dict[str, Any]:
    """Bind the fill's market to fresh config for the same account and ledger."""
    value = str(market or "").strip().lower()
    source_market = runtime_config_market(config).lower()
    logical_account = str(account or "").strip().lower()
    if logical_account not in (config.get("accounts") or []):
        raise ValueError(f"source runtime config does not configure account {logical_account}")
    if value == source_market:
        return dict(config)
    if value not in RUNTIME_MARKETS:
        raise ValueError(f"attribution market {value or '<empty>'} has no recoverable runtime config")
    path, bound = load_runtime_config(config_path=Path(runtime_root) / DEFAULT_CONFIGS[value],
        expected_market=value)
    ensure_runtime_config_freshness(bound, repo_root=repo_base(), market=value, runtime_config_path=path)
    if logical_account not in (bound.get("accounts") or []):
        raise ValueError(f"{value} runtime config does not configure account {logical_account}")
    source_physical = resolve_futu_account_ids(config, account=logical_account)
    source_environment = str(infer_futu_portfolio_settings(config, account=logical_account).get("trd_env") or "").upper()
    bound_physical = resolve_futu_account_ids(bound, account=logical_account)
    bound_environment = str(infer_futu_portfolio_settings(bound, account=logical_account).get("trd_env") or "").upper()
    if len(source_physical) != 1:
        raise ValueError(f"source physical account for {logical_account} is not exactly one")
    if bound_physical != source_physical or not bound_environment or bound_environment != source_environment:
        raise ValueError(f"{value} runtime config physical account or environment differs from the source")
    data_config = resolve_position_data_config_path(base=repo_base(), cfg=bound, config_path=path)
    store = resolve_ledger_store(data_config, config_path=path)
    try:
        bound_identity = ledger_resource_identity(open_trade_reconciliation_evidence_repo(store.sqlite_path))
    except OSError as exc:
        raise ValueError(f"{value} runtime config ledger is unavailable: {type(exc).__name__}") from exc
    if bound_identity["identity_sha256"] != ledger_resource_identity(repo)["identity_sha256"]:
        raise ValueError(f"{value} runtime config ledger differs from the source ledger")
    return bound


def _attribution_recoverable_markets(repo: Any, *, account: str, market: str) -> set[str]:
    """Only supported markets with an open canonical execution have recovery work."""
    rows = read_trade_attribution_snapshot(repo, account=account, market=market)
    markets: set[str] = set()
    for fact in trade_attribution_facts_from_events(rows["trade_events"], account=account):
        if not fact["execution_key"] or fact["contracts_open"] <= 0:
            continue
        fact_market = str(symbol_market(fact["contract_key"]["underlying_symbol"]) or "").strip().lower()
        if fact_market in RUNTIME_MARKETS:
            markets.add(fact_market)
    return markets


def reconcile_trade_attribution_source(
    repo: Any, *, config: Mapping[str, Any], accounts: Iterable[str], runtime_root: Path,
    inbox_path: Path, combo_mode: str, cursors: MutableMapping[str, str], stop_event: Any = None,
) -> dict[str, Any]:
    """Reuse account reconciliation per market; failures retain only that market's cursor."""
    source_market = runtime_config_market(config).lower()
    recovery: dict[str, dict[str, Any]] = {}
    for account in sorted({str(item or "").strip().lower() for item in accounts} - {""}):
        if stop_event is not None and stop_event.is_set():
            break
        entry = recovery.setdefault(account, {})
        try:
            if account not in (config.get("accounts") or []):
                raise ValueError(f"source runtime config does not configure account {account}")
            discovered = _attribution_recoverable_markets(repo, account=account, market=source_market)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            continue
        for market in [source_market, *sorted(discovered - {source_market})]:
            if stop_event is not None and stop_event.is_set():
                return recovery
            key = f"{account}:{market}"
            if market == source_market:
                bound, mode = dict(config), combo_mode
            else:
                try:
                    bound = bind_attribution_market_config(config=config, repo=repo, account=account,
                        market=market, runtime_root=runtime_root)
                except Exception as exc:
                    entry[market] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                mode = combo_reconciliation_mode_for_account(bound, account=account)
            try:
                result = reconcile_trade_attribution_account(
                    repo, config=bound, account=account, market=market, runtime_root=runtime_root,
                    inbox_path=inbox_path, combo_mode=mode,
                    cursor=str(cursors.get(key) or ""), stop_event=stop_event)
            except Exception as exc:
                entry[market] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            cursors[key] = result["next_cursor"]
            entry[market] = result
    return recovery


def attribution_runtime(*, config_key: str | None, config_path: str | None, account: str):
    if not config_key and not config_path:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="请先指定交易市场。")
    path, config = load_runtime_config(config_key=config_key, config_path=config_path)
    account = str(account or "").strip().lower()
    if not account or account not in config.get("accounts", []):
        raise AgentToolError(code="PERMISSION_DENIED", message="账户不在当前配置范围内。")
    data_config = resolve_position_data_config_path(base=repo_base(), cfg=config, config_path=path)
    store = resolve_ledger_store(data_config, config_path=path)
    repo = open_trade_reconciliation_evidence_repo(store.sqlite_path)
    settings = infer_futu_portfolio_settings(config, account=account)
    mapping = {"account": account, "physical_account_ids": sorted(resolve_futu_account_ids(config, account=account)),
               "environment": str(settings.get("trd_env") or "").upper()}
    authority = {"config_path": str(path.resolve()), "runtime_root": str(store.runtime_root.resolve()),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": canonical_sha256(mapping)}
    return repo, config, authority, mapping


def trade_attribution_read(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    prepare_confirmation = payload.get("prepare_confirmation", False)
    if not isinstance(prepare_confirmation, bool):
        raise AgentToolError(code="INPUT_ERROR", message="prepare_confirmation 必须为布尔值。")
    repo, config, authority, _mapping = attribution_runtime(
        config_key=payload.get("config_key"), config_path=payload.get("config_path"), account=payload.get("account"))
    account = str(payload["account"]).strip().lower()
    market = runtime_config_market(config).lower()
    cursor_key = canonical_sha256({"tool": "trade_attribution_read", "account": account, "market": market,
                                   "runtime_root": authority["runtime_root"], "config_path": authority.get("config_path"),
                                   "prepare_confirmation": prepare_confirmation})
    execution = str(payload.get("execution_key") or "").strip()
    raw_symbol = str(payload.get("symbol") or "").strip()
    identity = resolve_symbol_identity(raw_symbol) if raw_symbol else None
    if raw_symbol and identity is None:
        raise AgentToolError(code="INPUT_ERROR", message="无法识别该标的。")
    if identity and identity.market.lower() != market:
        raise AgentToolError(code="INPUT_ERROR", message="标的与所选市场不一致。")
    status = str(payload.get("status") or "").strip()
    if status and status not in {"linked", "ordinary", "pending", "conflict", "not_applicable"}:
        raise AgentToolError(code="INPUT_ERROR", message="无效的归属状态。")
    filters = {"execution_key": execution, "symbol": identity.canonical if identity else "", "status": status}
    cursor = ""
    if payload.get("cursor"):
        try:
            state = decode_evidence_cursor(str(payload["cursor"]), cursor_key)
            if (state.get("tool") != "trade_attribution_read" or not isinstance(state.get("filters"), dict)
                    or set(state["filters"]) != set(filters)
                    or not all(isinstance(value, str) for value in state["filters"].values())
                    or not isinstance(state.get("last_open_event_id"), str)):
                raise TradeEventPaginationError("invalid attribution cursor")
            if any(value and value != state["filters"].get(key) for key, value in filters.items()):
                raise TradeEventPaginationError("attribution cursor filters changed")
            filters = state["filters"]
            cursor = state["last_open_event_id"]
        except TradeEventPaginationError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=f"归属分页 cursor 无效或已过期，请重新查询：{exc}") from exc
    snapshot = read_trade_attribution_snapshot(repo, account=account, market=market)
    context = (read_trade_attribution_context(repo, config=config, account=account,
        runtime_root=Path(authority["runtime_root"]), execution_key=filters["execution_key"], snapshot=snapshot)
        if prepare_confirmation else None)
    now = int(time.time() * 1000)
    evidence = context["combo_evidence"] if context else read_attribution_combo_evidence(
        snapshot, account=account, runtime_root=Path(authority["runtime_root"]), now_ms=now,
        focus_open_event_id=attribution_focus_open_event_id(snapshot, account=account, execution_key=filters["execution_key"]))
    view = build_trade_attribution_view(snapshot, config=config, account=account, market=market, now_ms=now, combo_evidence=evidence,
        capacity_observation=context["capacity_observation"] if context else None,
        combo_mode=combo_reconciliation_mode_for_account(config, account=account),
        revalidate_expired=prepare_confirmation)
    rows = view["rows"]
    rows = [row for row in rows if (not filters["execution_key"] or row["execution_key"] == filters["execution_key"])
            and (not filters["symbol"] or row["contract_key"]["underlying_symbol"] == filters["symbol"])
            and (not filters["status"] or row["status"] == filters["status"]) and row["open_event_id"] > cursor]
    rows.sort(key=lambda row: row["open_event_id"])
    limit = max(1, min(int(payload.get("limit") or 50), 100))
    page = rows[:limit]
    return {"account": account, "market": market, "rows": page, "returned_count": len(page),
            "next_cursor": encode_evidence_cursor({"tool": "trade_attribution_read", "filters": filters,
                "last_open_event_id": page[-1]["open_event_id"]}, cursor_key) if len(rows) > limit else None,
            "evidence_scope": "canonical_ledger_and_local_candidates", "evidence_complete": all(row["evidence_complete"] for row in page),
            "capacity_observed": bool(context and context["capacity_observation"].get("portfolio")),
            "prepare_confirmation": prepare_confirmation}, [], {}


def run_attribution_admin(args: Any, *, config: dict[str, Any], config_path: Path,
                          runtime_root: Path) -> dict[str, Any]:
    if any(getattr(args, key, None) for key in (
        "mode", "once", "deal_json", "execution_file", "inbox_id", "retry_failed", "reconcile_state",
        "compensate_receipts", "deal_id", "host", "port", "state_path", "audit_path", "status_path")):
        raise ValueError("attribution administration cannot be combined with listener/replay options")
    if args.dry_run and (args.apply or args.confirm or args.yes):
        raise ValueError("dry-run cannot be combined with write flags")
    control = write_control(apply=args.apply, confirm=args.confirm, yes=args.yes, high_risk=True)
    if control["confirmation_required"]:
        raise ValueError("attribution administration requires --apply with --confirm or --yes")
    data_config = resolve_position_data_config_path(base=repo_base(), cfg=config, config_path=config_path,
                                                   data_config=args.data_config)
    store = resolve_ledger_store(data_config, config_path=config_path, runtime_root=runtime_root)
    if args.apply:
        guard = ledger_store_write_guard(data_config, config_path=config_path, runtime_root=runtime_root)
        if not guard.get("ok"):
            raise ValueError("ledger write scope guard failed: " + str(guard.get("errors")))
    if args.action == "attribution-migrate":
        account = str(args.account or "").strip().lower()
        physical = resolve_futu_account_ids(config, account=account) if account in config.get("accounts", []) else []
        settings = infer_futu_portfolio_settings(config, account=account) if physical else {}
        if len(physical) != 1:
            raise ValueError("attribution migration requires one configured account and physical source")
        scope = {"broker": "futu", "physical_account_id": physical[0], "account": account,
                 "environment": str(settings.get("trd_env") or "").upper(),
                 "market": runtime_config_market(config).lower()}
        if not args.apply:
            if args.effective_from_ms is None:
                raise ValueError("attribution migration preview requires --effective-from-ms")
            return preview_trade_attribution_migration(store.sqlite_path, scope=scope,
                effective_from_ms=args.effective_from_ms)
        if not args.manifest or not args.backup_path:
            raise ValueError("migration apply requires --manifest and --backup-path")
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and manifest.get("ok") is True and "result" in manifest:
            manifest = manifest["result"]
        if not isinstance(manifest, dict) or manifest.get("cutover_scope") != scope:
            raise ValueError("migration manifest source differs from current configuration")
        return apply_trade_attribution_migration(store.sqlite_path, manifest=manifest,
            backup_path=args.backup_path, writers_stopped=args.writers_stopped)
    raise ValueError("unknown attribution administration action")


def confirm_wheel_call_linkage(repo: Any, *, account: str, call_lot_id: str, lot_id: str,
    linkage_candidate_id: str, expected_input_hash: str, expected_batch_generation_hash: str,
    request_id: str, actor: str, config: Mapping[str, Any], runtime_root: Path,
    apply_changes: bool = False,
) -> dict[str, Any]:
    return apply_referenced_trade_attribution(repo, account=account, option_lot_id=call_lot_id,
        stock_lot_id=lot_id, direction="call", linkage_candidate_id=linkage_candidate_id,
        expected_input_hash=expected_input_hash, expected_batch_generation_hash=expected_batch_generation_hash,
        request_id=request_id, actor=actor, config=config, runtime_root=runtime_root, apply_changes=apply_changes)


def confirm_wheel_linkage(repo: Any, *, account: str, option_lot_id: str, wheel_branch_id: str,
    direction: str, linkage_candidate_id: str, expected_input_hash: str, expected_batch_generation_hash: str,
    request_id: str, actor: str, config: Mapping[str, Any], runtime_root: Path,
    apply_changes: bool = False,
) -> dict[str, Any]:
    return apply_referenced_trade_attribution(repo, account=account, option_lot_id=option_lot_id,
        wheel_branch_id=wheel_branch_id, direction=direction, linkage_candidate_id=linkage_candidate_id,
        expected_input_hash=expected_input_hash, expected_batch_generation_hash=expected_batch_generation_hash,
        request_id=request_id, actor=actor, config=config, runtime_root=runtime_root, apply_changes=apply_changes)
