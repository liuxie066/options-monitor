"""On-demand holdings assessment using the scheduled sealed evaluation pipeline."""
from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime, timezone, date
from io import StringIO
from pathlib import Path
import sys
import sqlite3
from typing import Any
from uuid import uuid4

from domain.domain.close_advice import CloseAdviceInput, evaluate_close_advice
from domain.domain.expiration_dates import expiration_market_date
from domain.domain.option_position_identity import normalize_account
from domain.domain.symbol_identity import symbol_market
from src.application.account_config import build_account_runtime_config, resolve_account_broker, resolve_configured_accounts
from src.application.close_advice_report_manifest import read_close_advice_report_snapshot, normalize_close_advice_report_value
from src.application.config_sections import resolve_watchlist_config, set_watchlist_config
from src.application.market_data_provider import resolve_market_data_provider
from src.application.position_advice_history import find_position_reference, reference_report_paths
from src.application.position_advice_inputs import MAX_ADVICE_POSITIONS, advice_position_key, normalize_manual_positions, select_ledger_positions
from src.application.runtime_paths import runtime_root_scope
from src.infrastructure.io_utils import atomic_write_json

_CLOSED_STATES = frozenset({"REST", "CLOSED", "AUCTION", "WAITING_OPEN", "PRE_MARKET_BEGIN", "PRE_MARKET_END", "AFTER_HOURS_BEGIN", "AFTER_HOURS_END", "NIGHT_OPEN", "NIGHT_END", "HK_CAS", "CLOSE_AUCTION", "AFTERNOON_END", "NIGHT", "OVERNIGHT_BEGIN", "OVERNIGHT_END", "OVERNIGHT", "TRADE_AT_LAST", "TRADE_AUCTION"})


def _request_quote_config(config: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any]:
    """Non-watchlist holdings use only an unambiguous existing effective quote route."""
    cfg = deepcopy(config)
    items = resolve_watchlist_config(cfg)
    provider = resolve_market_data_provider(cfg)
    existing = {item.get("symbol") for item in items}
    for symbol in sorted({str(pos["symbol"]) for pos in positions} - existing):
        route = provider.resolve_quote_route(cfg, market=symbol_market(symbol), symbol=symbol)
        if not route.ok or not route.host or not route.port:
            raise ValueError(f"no unambiguous configured quote route for {symbol}")
        items.append({"symbol": symbol, "broker": symbol_market(symbol), "fetch": {"source": provider.name, "host": route.host, "port": route.port}, "sell_put": {"enabled": False}, "sell_call": {"enabled": False}, "combo_yield": {"enabled": False}})
    set_watchlist_config(cfg, items)
    return cfg


def _observe_sessions(config: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, str]:
    from src.application.opend_utils import normalize_underlier

    provider = resolve_market_data_provider(config)
    result = {str(pos["symbol"]): "UNKNOWN" for pos in positions}
    groups: dict[tuple[str, int], list[str]] = {}
    for symbol in sorted(result):
        route = provider.resolve_quote_route(config, market=symbol_market(symbol), symbol=symbol)
        if route.ok and route.host and route.port:
            groups.setdefault((route.host, route.port), []).append(symbol)
    for (host, port), symbols in groups.items():
        gateway = None
        try:
            gateway = provider.open_quote_gateway(host=host, port=port)
            codes = {symbol: normalize_underlier(symbol).code for symbol in symbols}
            rows = provider.get_market_state(gateway, list(codes.values()))
            if hasattr(rows, "to_dict"):
                rows = rows.to_dict("records")
            if not isinstance(rows, list):
                continue
            for symbol, code in codes.items():
                matches = [row for row in rows if isinstance(row, dict) and row.get("code") == code]
                if len(matches) == 1:
                    result[symbol] = str(matches[0].get("market_state") or "").upper()
        except Exception:
            pass  # Unknown remains distinct from a proven closed session.
        finally:
            if gateway is not None:
                try:
                    gateway.close()
                except Exception:
                    pass

    return result


def _read_holdings(*, config: dict[str, Any], config_path: Path, base_dir: Path, runtime_root: Path, account: str) -> list[dict[str, Any]]:
    from src.application.ledger.api import read_position_query_ledger, attach_event_strategy_metadata
    from src.application.agent_tool_contracts import AgentToolError

    try:
        _repo, snapshots = read_position_query_ledger(base=base_dir, accounts=[account], cfg=config, config_path=config_path, runtime_root=runtime_root)
    except (sqlite3.Error, OSError) as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING", message="Canonical ledger is unavailable; holdings were not treated as empty",
            details={"reason": "ledger_unavailable", "cause": type(exc).__name__},
        ) from exc
    snapshot = snapshots.get(account) or {}
    if snapshot.get("snapshot_status") != "trusted":
        raise ValueError("canonical ledger evidence is unavailable; holdings were not treated as empty")
    return attach_event_strategy_metadata(snapshot["account_position_lots"], snapshot.get("trade_events"))


def _run_current_assessment(
    *, config: dict[str, Any], account: str, positions: list[dict[str, Any]], records: list[dict[str, Any]],
    manual: bool, workspace: Path, repo_root: Path, run_id: str, now_utc: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from src.application.close_advice_required_data import PLAN_FILE_NAME, build_close_advice_required_data_plan, publish_close_advice_required_data_plan, enrich_close_advice_required_data_plan_bounded
    from src.application.required_data_prefetch_planning import merge_close_advice_requirements_into_prefetch_config
    from src.application.multi_tick.required_data_prefetch import prefetch_required_data
    from src.application.required_data_snapshot import seal_required_data_snapshot
    from src.application.close_advice_runner import run_close_advice

    run_dir = workspace / "output_runs" / run_id
    required_root = run_dir / "required_data"
    required_root.mkdir(parents=True, exist_ok=True)
    state = run_dir / "state"
    state.mkdir()
    plan_path = state / PLAN_FILE_NAME
    manifest = state / "required_data_snapshot_manifest.json"
    output_dir = run_dir / "accounts" / account
    markets = sorted({str(symbol_market(pos["symbol"])) for pos in positions})
    plan = build_close_advice_required_data_plan(
        run_id=run_id, run_started_at_utc=now_utc, account_configs={account: config}, base_config=config,
        markets_to_run=markets, position_records_by_account={account: records},
        **({"manual_positions_by_account": {account: positions}} if manual else {}),
    )
    candidate = deepcopy(config)
    set_watchlist_config(candidate, [])
    prefetch_config, plan = merge_close_advice_requirements_into_prefetch_config(candidate_config=candidate, requirements_plan=plan)
    publish_close_advice_required_data_plan(path=plan_path, payload=plan)
    with runtime_root_scope(workspace):
        summary = prefetch_required_data(vpy=Path(sys.executable), base=workspace, repo_root=repo_root, cfg=prefetch_config, shared_required=required_root, force_refresh=True, producer_run_id=run_id, scan_at_utc=now_utc)
        enrichment_error = enrich_close_advice_required_data_plan_bounded(plan_path=plan_path, expected_run_id=run_id, python=Path(sys.executable), repo_root=repo_root)
        seal_required_data_snapshot(manifest_path=manifest, required_data_root=required_root, run_id=run_id, prefetch_summary=summary, close_advice_required_data_plan_path=plan_path)
        result = run_close_advice(config=config, context_path=state / "unused-context.json", required_data_root=required_root, output_dir=output_dir, base_dir=repo_root, markets_to_run=markets, required_data_snapshot_manifest=manifest, required_data_snapshot_run_id=run_id, close_advice_required_data_plan=plan_path, account=account, context_override={"context_status": "available", "filters": {"account": account}, "open_positions_min": positions})
    snapshot = read_close_advice_report_snapshot(csv_path=output_dir / "close_advice.csv", account=account, expected_run_id=run_id, expected_quote_mode="frozen_snapshot")
    rows = list(csv.DictReader(StringIO(snapshot["csv_bytes"].decode("utf-8")))) if snapshot["validation"].get("ok") else []
    return rows, {"report": result, "calendar_enrichment_error": enrichment_error, "report_validation": snapshot["validation"]}


def _unavailable(position: dict[str, Any], *, now_utc: datetime, reason: str) -> dict[str, Any]:
    day = expiration_market_date(now_utc, symbol_market(position["symbol"]))
    expiration = position.get("expiration_ymd") or position.get("expiration")
    try:
        dte = (date.fromisoformat(expiration) - day).days if day else None
    except (TypeError, ValueError, KeyError):
        dte = None
    row = evaluate_close_advice(CloseAdviceInput(
        account=position["account"], position_lot_id=position.get("lot_id") or position.get("record_id"),
        symbol=position["symbol"], option_type=position["option_type"], side="short", expiration=expiration,
        strike=position.get("strike"), contracts_open=position.get("contracts_open"), premium=position.get("premium"),
        multiplier=position.get("multiplier"), currency=position.get("currency"), dte=dte,
        input_source=position.get("input_source", "ledger"), input_id=position.get("input_id"), input_as_of_utc=position.get("input_as_of_utc"),
    ))
    return {**row, "input_source": position.get("input_source", "ledger"), "input_id": position.get("input_id"), "broker": position["broker"], "assessment_mode": "unavailable", "current_actionable": False, "assessment_as_of_utc": None, "reason": reason}


def evaluate_position_advice(
    payload: dict[str, Any], *, config: dict[str, Any], config_path: Path,
    base_dir: Path, runtime_root: Path, now_utc: datetime | None = None,
) -> dict[str, Any]:
    from src.application.position_advice_plugin import position_advice_plugin_status
    from src.application.quality.gate import assert_quality_allows

    instant = now_utc or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("assessment clock must be timezone aware")
    instant = instant.astimezone(timezone.utc)
    account = normalize_account(payload.get("account"))
    if not account:
        raise ValueError("account is required")
    resolve_configured_accounts(config, [account])
    cfg = build_account_runtime_config(base_cfg=config, cfg_path=config_path, account=account, markets_to_run=[])
    state = position_advice_plugin_status(cfg)
    if state != "available":
        return {"status": state, "rows": [], "returned_count": 0, "notify_rows": 0, "reason": "持仓建议未启用" if state == "disabled" else "需要安装 om-position-advice-plugin"}
    manual = "positions" in payload
    if manual and any(payload.get(key) for key in ("symbols", "lot_ids", "contracts")):
        raise ValueError("manual positions and ledger selectors cannot be mixed")
    broker = resolve_account_broker(cfg, account=account)
    records: list[dict[str, Any]] = []
    if manual:
        positions = normalize_manual_positions(payload["positions"], account=account, broker=broker, now_utc=instant)
    else:
        records = _read_holdings(config=cfg, config_path=config_path, base_dir=base_dir, runtime_root=runtime_root, account=account)
        positions, records = select_ledger_positions(payload, records=records, account=account, broker=broker, now_utc=instant)
    if len(positions) > MAX_ADVICE_POSITIONS:
        raise ValueError("too many holdings")
    with runtime_root_scope(runtime_root):
        for market in {symbol_market(pos["symbol"]) for pos in positions}:
            assert_quality_allows("close_advice", account=account, market=str(market).lower())
    cfg = _request_quote_config(cfg, positions)
    sessions = _observe_sessions(cfg, positions)
    active_positions: list[dict[str, Any]] = []
    for pos in positions:
        day = expiration_market_date(instant, symbol_market(pos["symbol"]))
        try:
            active = date.fromisoformat(pos.get("expiration_ymd") or pos.get("expiration")) > day
        except (TypeError, ValueError):
            active = False
        if active and sessions.get(pos["symbol"]) in {"MORNING", "AFTERNOON"}:
            active_positions.append(pos)
    run_id = "advice-" + uuid4().hex
    workspace = runtime_root / "output_shared" / "position_advice_requests" / run_id
    workspace.mkdir(parents=True, exist_ok=False)
    evidence: dict[str, Any] = {}
    current_rows: list[dict[str, Any]] = []
    if active_positions:
        keys = {advice_position_key(pos) for pos in active_positions}
        selected_records = [record for record in records if str(record.get("record_id")) in keys]
        current_rows, evidence = _run_current_assessment(config=cfg, account=account, positions=active_positions, records=selected_records, manual=manual, workspace=workspace, repo_root=base_dir, run_id=run_id, now_utc=instant)
    by_identity = {advice_position_key({**row, "lot_id": row.get("position_lot_id")}): row for row in current_rows}
    provider = resolve_market_data_provider(cfg).name
    references = reference_report_paths(runtime_root, account=account)
    rows: list[dict[str, Any]] = []
    for pos in positions:
        row = by_identity.get(advice_position_key(pos))
        session = sessions.get(pos["symbol"], "UNKNOWN")
        if row is None and session in _CLOSED_STATES:
            day = expiration_market_date(instant, symbol_market(pos["symbol"]))
            try:
                lifecycle_active = date.fromisoformat(pos.get("expiration_ymd") or pos.get("expiration")) > day
            except (ValueError, TypeError):
                lifecycle_active = False
            if lifecycle_active:
                row = find_position_reference(pos, report_paths=references, provider=provider, now_utc=instant)
        if row is None:
            row = _unavailable(pos, now_utc=instant, reason="当前没有兼容的有效行情；休市参考不可用" if session in _CLOSED_STATES else "持仓生命周期、行情状态或封存证据不可用")
        rows.append({key: normalize_close_advice_report_value(key, value) for key, value in {**row, "request_market_state": session}.items()})
    result = {"schema_version": "position_advice.output.v1", "status": "success" if all(row.get("assessment_mode") in {"current", "historical_reference"} for row in rows) else "partial", "request_id": run_id, "account": account, "input_source": "user_provided" if manual else "ledger", "evaluated_at_utc": instant.isoformat(), "rows": rows, "returned_count": len(rows), "notify_rows": 0, "evidence": evidence, "report_path": str(workspace / "position_advice.json"), "interpretation": "hold仅表示本策略未触发平仓条件；历史参考不代表当前建议"}
    atomic_write_json(workspace / "position_advice.json", result)
    return result
