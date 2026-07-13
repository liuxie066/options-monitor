from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from domain.domain.close_advice import TIER_PRIORITY
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.position_query import PositionExpirationQuery, PositionQuery
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.application.runtime_paths import resolve_runtime_root


CLOSE_ADVICE_CSV = "close_advice.csv"


def close_advice_read_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]],
    resolve_output_root: Callable[[Any], Path],
    repo_base: Callable[[], Path],
    mask_path: Callable[[Any], str | None],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Read existing close-advice reports without refreshing market data."""

    base = repo_base().resolve()
    config_path: Path | None = None
    cfg: dict[str, Any] | None = None
    if payload.get("config_key") or payload.get("config_path"):
        config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))

    query = _query_from_payload(payload)
    config_market = _desired_market(payload, cfg=cfg, config_path=config_path)
    query_market = None if _has_explicit_source_scope(payload) else _query_market(query)
    payload_market_scope = _payload_market_scope(payload)
    desired_market = query_market or (None if payload_market_scope == "all" else config_market)
    sources = _resolve_sources(
        payload,
        base=base,
        config_path=config_path,
        query=query,
        desired_market=desired_market,
        resolve_output_root=resolve_output_root,
        mask_path=mask_path,
    )
    rows: list[dict[str, Any]] = []
    for source in sources:
        rows.extend(_read_rows(source))

    matched = [_public_row(row) for row in rows if _matches(row, query)]
    matched.sort(key=_sort_key)
    limit = max(1, min(int(query.limit or 50), 500))
    returned = matched[:limit]

    data = {
        "query": query.to_payload(),
        "source": _source_payload(sources, mask_path=mask_path),
        "scope": {
            "market": desired_market or "all",
            "query": query.to_payload(),
        },
        "row_count": len(rows),
        "matched_count": len(matched),
        "returned_count": len(returned),
        "rows": returned,
        "summary": _summary(returned),
        "coverage": {
            "source_count": len(sources),
            "source_row_count": len(rows),
            "matched_count": len(matched),
            "returned_count": len(returned),
            "truncated": len(returned) < len(matched),
        },
        "freshness": {
            "kind": "report_snapshot",
            "run_ids": sorted({str(source.run_id) for source in sources if source.run_id}),
        },
    }
    meta: dict[str, Any] = {
        "source_count": len(sources),
        "source_paths": [mask_path(source.path) for source in sources],
    }
    if desired_market:
        meta["market_filter"] = desired_market
    if query_market:
        meta["market_filter_source"] = "query_symbol"
        if config_market and config_market != query_market:
            meta["config_market_filter"] = config_market
    elif config_market:
        meta["market_filter_source"] = "payload" if payload_market_scope in {"us", "hk"} else "config"
    if payload_market_scope:
        meta["market_scope"] = payload_market_scope
    if config_path is not None:
        meta["config_path"] = mask_path(config_path)
    return data, [], meta


class _Source:
    def __init__(
        self,
        path: Path,
        *,
        source_type: str,
        run_id: str | None = None,
        account: str | None = None,
    ) -> None:
        self.path = path
        self.source_type = source_type
        self.run_id = run_id
        self.account = account


def _query_from_payload(payload: dict[str, Any]) -> PositionQuery:
    query_payload: dict[str, Any] = {}
    raw_query = payload.get("query")
    if isinstance(raw_query, dict):
        query_payload.update(raw_query)
    for key in ("account", "status", "symbol", "option_type", "side", "strike", "expiration", "limit"):
        if payload.get(key) not in (None, ""):
            query_payload[key] = payload[key]
    return PositionQuery.from_payload(query_payload)


def _resolve_sources(
    payload: dict[str, Any],
    *,
    base: Path,
    config_path: Path | None,
    query: PositionQuery,
    desired_market: str | None,
    resolve_output_root: Callable[[Any], Path],
    mask_path: Callable[[Any], str | None],
) -> list[_Source]:
    explicit = _explicit_csv_path(payload, base=base)
    if explicit is not None:
        if not explicit.exists():
            raise AgentToolError(
                code="DEPENDENCY_MISSING",
                message="没有找到指定的平仓建议报告。",
                details={"csv_path": mask_path(explicit)},
            )
        return [_Source(explicit, source_type="explicit")]

    run_sources = _run_sources(payload, base=base, config_path=config_path, query=query, desired_market=desired_market)
    if run_sources:
        return run_sources
    if str(payload.get("run_id") or "").strip():
        raise AgentToolError(
            code="DEPENDENCY_MISSING",
            message="没有找到指定 run_id 的平仓建议报告。",
            details={"run_id": str(payload.get("run_id") or "").strip()},
        )

    report_sources = _agent_tool_report_sources(
        payload,
        base=base,
        config_path=config_path,
        desired_market=desired_market,
        resolve_output_root=resolve_output_root,
    )
    if report_sources:
        return report_sources

    raise AgentToolError(
        code="DEPENDENCY_MISSING",
        message="没有找到最近的平仓建议报告。",
        hint="先运行扫描/平仓建议生成后再查询，或指定 run_id/report_path。",
    )


def _explicit_csv_path(payload: dict[str, Any], *, base: Path) -> Path | None:
    raw = payload.get("report_path") or payload.get("csv_path")
    if raw is None or not str(raw).strip():
        return None
    path = _resolve_path(raw, base=base)
    return (path / CLOSE_ADVICE_CSV).resolve() if path.is_dir() else path


def _run_sources(
    payload: dict[str, Any],
    *,
    base: Path,
    config_path: Path | None,
    query: PositionQuery,
    desired_market: str | None,
) -> list[_Source]:
    root = _runs_root(payload, base=base, config_path=config_path)
    if not root.exists() or not root.is_dir():
        return []
    requested_run = str(payload.get("run_id") or "").strip()
    run_dirs = [(root / requested_run).resolve()] if _is_direct_child_name(requested_run) else ([] if requested_run else _latest_run_dirs(root))
    if not requested_run and desired_market is None:
        return _latest_run_sources_across_markets(run_dirs, query=query)
    for run_dir in run_dirs:
        if not run_dir.exists() or not run_dir.is_dir():
            continue
        sources = _sources_for_run_dir(run_dir, query=query, desired_market=desired_market)
        if sources:
            return sources
    return []


def _latest_run_sources_across_markets(run_dirs: list[Path], *, query: PositionQuery) -> list[_Source]:
    sources_out: list[_Source] = []
    unknown_sources: list[_Source] = []
    seen_markets: set[str] = set()
    for run_dir in run_dirs:
        if not run_dir.exists() or not run_dir.is_dir():
            continue
        sources = _sources_for_run_dir(run_dir, query=query, desired_market=None)
        if not sources:
            continue
        run_markets: set[str] = set()
        for source in sources:
            run_markets.update(_source_market_values(source))
        if not run_markets:
            if not sources_out and not unknown_sources:
                unknown_sources.extend(sources)
            continue
        if run_markets.issubset(seen_markets):
            continue
        seen_markets.update(run_markets)
        sources_out.extend(sources)
        if {"US", "HK"}.issubset(seen_markets):
            break
    return sources_out or unknown_sources


def _runtime_root(*, base: Path, config_path: Path | None) -> Path:
    runtime_root = resolve_runtime_root(repo_root=base).runtime_root
    if runtime_root.resolve() != base.resolve():
        return runtime_root
    if config_path is not None:
        return config_path.expanduser().resolve().parent
    return runtime_root


def _runs_root(payload: dict[str, Any], *, base: Path, config_path: Path | None) -> Path:
    raw = payload.get("runs_root")
    if raw is not None and str(raw).strip():
        return _resolve_path(raw, base=base)
    return (_runtime_root(base=base, config_path=config_path) / "output_runs").resolve()


def _latest_run_dirs(root: Path) -> list[Path]:
    return sorted(
        [item for item in root.iterdir() if item.is_dir()],
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )


def _sources_for_run_dir(run_dir: Path, *, query: PositionQuery, desired_market: str | None) -> list[_Source]:
    accounts_root = run_dir / "accounts"
    if not accounts_root.exists() or not accounts_root.is_dir():
        path = run_dir / CLOSE_ADVICE_CSV
        if path.exists() and _matches_market(run_dir=run_dir, account_dir=None, desired_market=desired_market):
            return [_Source(path, source_type="run", run_id=run_dir.name)]
        return []

    account_dirs: list[Path]
    if query.account:
        account_dirs = [accounts_root / query.account]
    else:
        account_dirs = sorted([item for item in accounts_root.iterdir() if item.is_dir()], key=lambda item: item.name)

    sources: list[_Source] = []
    for account_dir in account_dirs:
        path = account_dir / CLOSE_ADVICE_CSV
        if path.exists() and _matches_market(run_dir=run_dir, account_dir=account_dir, desired_market=desired_market):
            sources.append(_Source(path, source_type="run", run_id=run_dir.name, account=account_dir.name))
    return sources


def _agent_tool_report_sources(
    payload: dict[str, Any],
    *,
    base: Path,
    config_path: Path | None,
    desired_market: str | None,
    resolve_output_root: Callable[[Any], Path],
) -> list[_Source]:
    roots: list[Path] = []
    if payload.get("output_dir"):
        roots.append(resolve_output_root(payload.get("output_dir")).resolve())
    else:
        runtime_root = _runtime_root(base=base, config_path=config_path)
        roots.append((runtime_root / "output_shared" / "agent_tools").resolve())
        default_output_root = resolve_output_root(None).resolve()
        if not _is_repo_default_agent_output(default_output_root, base=base, runtime_root=runtime_root):
            roots.append(default_output_root)
        elif runtime_root.resolve() == base.resolve():
            roots.append(default_output_root)
        if desired_market is None:
            roots.append((base / "output_shared" / "agent_tools").resolve())

    out: list[_Source] = []
    seen: set[Path] = set()
    for root in roots:
        for path in (root / "reports" / CLOSE_ADVICE_CSV, root / CLOSE_ADVICE_CSV):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.exists():
                out.append(_Source(resolved, source_type="agent_tool"))
        if out:
            return out
    return out


def _is_direct_child_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    path = Path(text)
    return not path.is_absolute() and path.name == text


def _is_repo_default_agent_output(path: Path, *, base: Path, runtime_root: Path) -> bool:
    default = (base / "output_shared" / "agent_tools").resolve()
    if path.resolve() != default:
        return False
    return runtime_root.resolve() != base.resolve()


def _resolve_path(raw: Any, *, base: Path) -> Path:
    path = Path(str(raw)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()


def _read_rows(source: _Source) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    context_side_index = _context_side_index(source)
    try:
        with source.path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                if not isinstance(raw, dict):
                    continue
                row = {str(key): value for key, value in raw.items() if key is not None}
                if source.account and not str(row.get("account") or "").strip():
                    row["account"] = source.account
                if not row.get("side") and not row.get("position_side"):
                    key = _position_side_index_key(
                        symbol=row.get("symbol"),
                        option_type=row.get("option_type"),
                        expiration=row.get("expiration") or row.get("expiration_ymd"),
                        strike=row.get("strike"),
                    )
                    side = context_side_index.get(key) if key else None
                    if side:
                        row["position_side"] = side
                row["_source_run_id"] = source.run_id
                row["_source_type"] = source.source_type
                rows.append(row)
    except OSError as exc:
        raise AgentToolError(
            code="READ_ERROR",
            message=f"读取平仓建议报告失败：{source.path.name}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    return rows


def _context_side_index(source: _Source) -> dict[tuple[str, str, str, str], str]:
    context_path = source.path.parent / "state" / "option_positions_context.json"
    obj = _read_json(context_path)
    positions = obj.get("open_positions_min") if isinstance(obj, dict) else []
    if not isinstance(positions, list):
        return {}
    out: dict[tuple[str, str, str, str], str] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        side = _canonical_side(pos.get("side") or pos.get("position_side"))
        if not side:
            continue
        key = _position_side_index_key(
            symbol=pos.get("symbol"),
            option_type=pos.get("option_type"),
            expiration=pos.get("expiration") or pos.get("expiration_ymd") or pos.get("exp"),
            strike=pos.get("strike"),
        )
        if key:
            out[key] = side
    return out


def _position_side_index_key(
    *,
    symbol: Any,
    option_type: Any,
    expiration: Any,
    strike: Any,
) -> tuple[str, str, str, str] | None:
    canonical = canonical_symbol(symbol)
    opt = _lower(option_type)
    exp = _normalize_expiration_for_index(expiration)
    strike_key = _normalize_strike_for_index(strike)
    if not (canonical and opt and exp and strike_key):
        return None
    return canonical, opt, exp, strike_key


def _normalize_expiration_for_index(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return _date_from_epoch_like(float(value))
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        if text.isdigit():
            return _date_from_epoch_like(float(text))
    except Exception:
        pass
    parsed = _parse_date(text)
    return parsed.isoformat() if parsed else text[:10]


def _date_from_epoch_like(value: float) -> str:
    try:
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    except Exception:
        return ""


def _normalize_strike_for_index(value: Any) -> str:
    parsed = _float_or_none(value)
    if parsed is None:
        return str(value or "").strip()
    return f"{parsed:.8f}".rstrip("0").rstrip(".")


def _desired_market(payload: dict[str, Any], *, cfg: dict[str, Any] | None, config_path: Path | None) -> str | None:
    payload_scope = _payload_market_scope(payload)
    if payload_scope == "all":
        return None
    if payload_scope in {"us", "hk"}:
        return payload_scope.upper()
    market = infer_runtime_config_market(
        config_key=str(payload.get("config_key") or "").strip() or None,
        config_path=config_path,
        config=cfg,
    )
    return _normalize_market(market)


def _payload_market_scope(payload: dict[str, Any]) -> str | None:
    text = str(payload.get("market_scope") or payload.get("market_filter") or "").strip().lower()
    if text in {"us", "hk", "all"}:
        return text
    return None


def _query_market(query: PositionQuery) -> str | None:
    if not query.symbol:
        return None
    return _normalize_market(symbol_market(query.symbol))


def _has_explicit_source_scope(payload: dict[str, Any]) -> bool:
    for key in ("run_id", "report_path", "csv_path", "output_dir"):
        if str(payload.get(key) or "").strip():
            return True
    return False


def _source_market_values(source: _Source) -> set[str]:
    account_dir = source.path.parent if source.account else None
    run_dir = _source_run_dir(source)
    if run_dir is None:
        return set()
    return _run_market_values(run_dir=run_dir, account_dir=account_dir)


def _source_run_dir(source: _Source) -> Path | None:
    current = source.path.parent
    while current.name:
        if current.name == "accounts":
            return current.parent
        current = current.parent
    return None


def _matches_market(*, run_dir: Path, account_dir: Path | None, desired_market: str | None) -> bool:
    if desired_market is None:
        return True
    observed = _run_market_values(run_dir=run_dir, account_dir=account_dir)
    return bool(observed) and desired_market in observed


def _run_market_values(*, run_dir: Path, account_dir: Path | None) -> set[str]:
    payloads: list[Any] = [
        _read_json(run_dir / "state" / "tick_metrics.json"),
        _read_json(run_dir / "state" / "last_run.json"),
    ]
    if account_dir is not None:
        payloads.extend(
            [
                _read_json(account_dir / "config.override.json"),
                _read_json(account_dir / "state" / "account_metrics.json"),
                _read_json(account_dir / "state" / "last_run.json"),
            ]
        )
    out: set[str] = set()
    for payload in payloads:
        out.update(_market_values(payload))
    return out


def _read_json(path: Path) -> Any:
    try:
        if not path.exists() or not path.is_file():
            return None
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _market_values(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for key in (
            "market",
            "markets",
            "market_key",
            "config_key",
            "markets_to_run",
            "scheduler_markets",
            "scheduler_market",
        ):
            if key in value:
                out.update(_market_values(value.get(key)))
        out.update(_market_values(value.get("_generated")))
        out.update(_market_values(value.get("_resolved")))
        if not out:
            out.update(_market_values(value.get("symbols")))
        return out
    if isinstance(value, (list, tuple, set)):
        for item in value:
            out.update(_market_values(item))
        return out
    market = _normalize_market(value)
    if market:
        out.add(market)
    return out


def _normalize_market(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"US", "USA"}:
        return "US"
    if text in {"HK", "HKG", "HKEX"}:
        return "HK"
    return None


def _matches(row: dict[str, Any], query: PositionQuery) -> bool:
    if query.status == "close":
        return False
    if query.account and normalize_account(row.get("account")) != query.account:
        return False
    if query.symbol and _row_symbol(row) != query.symbol:
        return False
    if query.option_type and _lower(row.get("option_type")) != query.option_type:
        return False
    if query.side and not _side_matches(row, query.side):
        return False
    if query.strike is not None and not _float_equal(row.get("strike"), query.strike):
        return False
    if not _expiration_matches(_row_expiration(row), query.expiration):
        return False
    row_status = _lower(row.get("status"))
    if query.status == "open" and row_status and row_status != "open":
        return False
    return True


def _row_symbol(row: dict[str, Any]) -> str | None:
    raw = str(row.get("symbol") or "").strip()
    return canonical_symbol(raw) if raw else None


def _side_matches(row: dict[str, Any], side: str) -> bool:
    observed_sides: list[str] = []
    for key in ("side", "position_side"):
        value = _canonical_side(row.get(key))
        if value:
            observed_sides.append(value)
    if side in observed_sides:
        return True
    if observed_sides:
        return False

    leg_role = _lower(row.get("leg_role"))
    if side == "long" and leg_role in {"enhancement_call", "long_call", "upside_call", "convexity_call", "buy_call"}:
        return True
    if side == "short" and leg_role in {"sell_put", "short_put", "sell_call", "short_call"}:
        return True
    return False


def _row_expiration(row: dict[str, Any]) -> str:
    return str(row.get("expiration") or row.get("expiration_ymd") or "").strip()


def _expiration_matches(value: str, query: PositionExpirationQuery) -> bool:
    if query.exact and value != query.exact:
        return False
    if query.month and not value.startswith(query.month):
        return False
    value_date = _parse_date(value)
    if query.before:
        before = _parse_date(query.before)
        if value_date is not None and before is not None and value_date >= before:
            return False
    if query.after:
        after = _parse_date(query.after)
        if value_date is not None and after is not None and value_date <= after:
            return False
    if query.within_days is not None and value_date is not None:
        days = (value_date - date.today()).days
        if days < 0 or days > int(query.within_days):
            return False
    return True


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "account",
        "position_lot_id",
        "symbol",
        "option_type",
        "side",
        "position_side",
        "expiration",
        "expiration_ymd",
        "strike",
        "contracts_open",
        "premium",
        "close_mid",
        "bid",
        "ask",
        "dte",
        "capture_ratio",
        "remaining_premium",
        "realized_if_close",
        "buy_to_close_fee",
        "buy_to_close_cost",
        "close_fee_to_remaining_premium",
        "remaining_risk_status",
        "remaining_risk_unavailable_reason",
        "remaining_stress_scenario",
        "remaining_stress_loss",
        "remaining_reward_to_stress_loss",
        "replacement_annualized_return",
        "replacement_annualized_advantage",
        "replacement_source",
        "continued_willingness",
        "continued_willingness_source",
        "close_calibration_status",
        "close_calibration_missing",
        "put_leg_realized_if_close",
        "combo_call_cost",
        "combo_call_value_if_close",
        "combo_net_locked_if_close_put_keep_call",
        "combo_net_if_close_both",
        "combo_cost_basis_status",
        "paired_leg_status",
        "long_call_value_ratio",
        "long_call_cost_basis",
        "long_call_current_value",
        "remaining_annualized_return",
        "evaluation_status",
        "quote_status",
        "tier",
        "tier_label",
        "reason",
        "exit_state",
        "exit_reason_type",
        "hold_reason_type",
        "close_action",
        "optional_combo_action",
        "strategy_exit_mode",
        "strategy",
        "leg_role",
        "yield_enhancement_mode",
        "strategy_family",
        "strategy_profile",
        "risk_model",
        "short_vol_thesis_status",
        "short_vol_reason",
        "event_risk_flag",
        "event_risk_types",
        "event_risk_dates",
        "event_source_status",
        "path_stress_status",
        "implied_volatility",
        "realized_volatility_estimate",
        "iv_rv_ratio",
        "iv_minus_rv",
        "abs_delta",
        "delta",
        "gamma",
        "data_quality_flags",
    )
    out = {key: _normalize_public_value(key, row.get(key)) for key in keys if row.get(key) not in (None, "")}
    canonical = _row_symbol(row)
    if canonical:
        out["symbol"] = canonical
    if "expiration" not in out and row.get("expiration_ymd"):
        out["expiration"] = str(row.get("expiration_ymd") or "").strip()
    inferred_side = (
        _canonical_side(row.get("side"))
        or _canonical_side(row.get("position_side"))
        or _side_from_leg_role(row.get("leg_role"))
    )
    if inferred_side:
        out["side"] = inferred_side
    if row.get("_source_run_id"):
        out["source_run_id"] = row.get("_source_run_id")
    return {key: value for key, value in out.items() if value not in (None, "")}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tier_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    evaluation_counts: dict[str, int] = {}
    for row in rows:
        tier = _lower(row.get("tier")) or "none"
        action = _lower(row.get("close_action")) or "-"
        evaluation = _lower(row.get("evaluation_status")) or "priced"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        action_counts[action] = action_counts.get(action, 0) + 1
        evaluation_counts[evaluation] = evaluation_counts.get(evaluation, 0) + 1
    return {
        "tier_counts": tier_counts,
        "action_counts": action_counts,
        "evaluation_counts": evaluation_counts,
        "not_evaluable_count": evaluation_counts.get("not_evaluable", 0),
    }


def _side_from_leg_role(value: Any) -> str | None:
    leg_role = _lower(value)
    if leg_role in {"enhancement_call", "long_call", "upside_call", "convexity_call", "buy_call"}:
        return "long"
    if leg_role in {"sell_put", "short_put", "sell_call", "short_call"}:
        return "short"
    return None


def _canonical_side(value: Any) -> str | None:
    normalized = _lower(value)
    if normalized in {"short", "sell", "sold", "write", "written"}:
        return "short"
    if normalized in {"long", "buy", "bought"}:
        return "long"
    return None


def _source_payload(sources: list[_Source], *, mask_path: Callable[[Any], str | None]) -> dict[str, Any]:
    run_ids = sorted({str(source.run_id) for source in sources if source.run_id})
    accounts = sorted({str(source.account) for source in sources if source.account})
    return {
        "type": sources[0].source_type if sources else None,
        "run_id": run_ids[0] if len(run_ids) == 1 else None,
        "run_ids": run_ids,
        "accounts": accounts,
        "paths": [mask_path(source.path) for source in sources],
    }


def _sort_key(row: dict[str, Any]) -> tuple[int, float, str, str, float]:
    tier = _lower(row.get("tier")) or "none"
    realized = _float_or_none(row.get("realized_if_close"))
    return (
        TIER_PRIORITY.get(tier, 9),
        -(realized if realized is not None else -10**12),
        str(row.get("account") or ""),
        str(row.get("symbol") or ""),
        _float_or_none(row.get("strike")) or 0.0,
    )


_NUMERIC_PUBLIC_FIELDS = frozenset(
    {
        "strike",
        "contracts_open",
        "premium",
        "close_mid",
        "bid",
        "ask",
        "dte",
        "capture_ratio",
        "remaining_premium",
        "realized_if_close",
        "buy_to_close_fee",
        "buy_to_close_cost",
        "close_fee_to_remaining_premium",
        "remaining_stress_loss",
        "remaining_reward_to_stress_loss",
        "replacement_annualized_return",
        "replacement_annualized_advantage",
        "put_leg_realized_if_close",
        "combo_call_cost",
        "combo_call_value_if_close",
        "combo_net_locked_if_close_put_keep_call",
        "combo_net_if_close_both",
        "long_call_value_ratio",
        "long_call_cost_basis",
        "long_call_current_value",
        "remaining_annualized_return",
        "implied_volatility",
        "realized_volatility_estimate",
        "iv_rv_ratio",
        "iv_minus_rv",
        "abs_delta",
        "delta",
        "gamma",
    }
)

_BOOLEAN_PUBLIC_FIELDS = frozenset({"continued_willingness"})


def _normalize_public_value(key: str, value: Any) -> Any:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if text.lower() in {"nan", "none", "null"}:
        return None
    if key in _BOOLEAN_PUBLIC_FIELDS:
        if text.lower() in {"true", "1", "yes"}:
            return True
        if text.lower() in {"false", "0", "no"}:
            return False
        return text
    if key not in _NUMERIC_PUBLIC_FIELDS:
        return text
    number = _float_or_none(text)
    return number if number is not None else text


def _float_equal(left: Any, right: float) -> bool:
    value = _float_or_none(left)
    return value is not None and abs(value - float(right)) < 1e-6


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


__all__ = ["close_advice_read_tool"]
