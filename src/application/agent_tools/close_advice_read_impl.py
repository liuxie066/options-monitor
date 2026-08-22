from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable

from domain.domain.close_advice import (
    DECISION_EVIDENCE_COMPLETE,
    DECISION_EVIDENCE_NOT_EVALUABLE,
    RECOMMENDATION_CLOSE,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_NOT_EVALUABLE,
    STRICT_CLOSE_POLICY_VERSION,
)
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.position_query import PositionExpirationQuery, PositionQuery
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.application.runtime_paths import resolve_runtime_root
from src.application.quality.gate import QualityGateBlocked, assert_quality_allows
from src.application.close_advice_report_manifest import (
    read_close_advice_report_snapshot,
)


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
    try:
        assert_quality_allows(
            "close_advice",
            account=str(query.account or "").strip().lower() or None,
            market=str(payload.get("config_key") or "").strip().lower() or None,
        )
    except QualityGateBlocked as exc:
        raise AgentToolError(
            code="QUALITY_GATE_BLOCKED",
            message=str(exc),
            details={
                "consumer": exc.consumer,
                "reason_code": exc.reason_code,
                "blocked_by": list(exc.blocked_by),
            },
        ) from exc
    config_market = _desired_market(payload, cfg=cfg, config_path=config_path)
    query_market = _query_market(query)
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
    used_sources: list[_Source] = []
    for source in sources:
        source_rows = _read_rows(source)
        if desired_market:
            source_rows = [
                row
                for row in source_rows
                if _normalize_market(symbol_market(row.get("symbol")))
                == desired_market
            ]
        rows.extend(source_rows)
        used_sources.append(source)
    sources = used_sources

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
            "status": "historical",
            "as_of": _sources_as_of(sources),
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
        self.csv_bytes: bytes | None = None
        self.generated_at_utc: str | None = None


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
        requested_run = str(payload.get("run_id") or "").strip() or None
        source = _Source(
            explicit,
            source_type="explicit",
            run_id=requested_run,
        )
        validation = _validate_source_manifest(
            source,
            desired_market=desired_market,
            query_account=query.account,
            expected_run_id=requested_run,
        )
        if not validation.get("ok"):
            raise _invalid_report_error(
                source,
                validation=validation,
                mask_path=mask_path,
            )
        return [source]

    run_sources = _run_sources(
        payload,
        base=base,
        config_path=config_path,
        query=query,
        desired_market=desired_market,
        mask_path=mask_path,
    )
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
        query=query,
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
    mask_path: Callable[[Any], str | None],
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
        sources, failures = _validated_sources_for_run_dir(
            run_dir,
            query=query,
            desired_market=desired_market,
        )
        if requested_run and failures:
            source, validation = failures[0]
            raise _invalid_report_error(
                source,
                validation=validation,
                mask_path=mask_path,
            )
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
        sources, _failures = _validated_sources_for_run_dir(
            run_dir,
            query=query,
            desired_market=None,
        )
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


def _candidate_sources_for_run_dir(
    run_dir: Path,
    *,
    query: PositionQuery,
    desired_market: str | None,
) -> list[_Source]:
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


def _validated_sources_for_run_dir(
    run_dir: Path,
    *,
    query: PositionQuery,
    desired_market: str | None,
) -> tuple[list[_Source], list[tuple[_Source, dict[str, Any]]]]:
    sources = _candidate_sources_for_run_dir(
        run_dir,
        query=query,
        desired_market=desired_market,
    )
    failures: list[tuple[_Source, dict[str, Any]]] = []
    for source in sources:
        validation = _validate_source_manifest(
            source,
            desired_market=desired_market,
            query_account=query.account,
            expected_run_id=run_dir.name,
        )
        if not validation.get("ok"):
            failures.append((source, validation))
    if failures:
        return [], failures
    return sources, []


def _agent_tool_report_sources(
    payload: dict[str, Any],
    *,
    base: Path,
    config_path: Path | None,
    query: PositionQuery,
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
        request_paths = sorted(
            (root / "requests").glob("*/reports/close_advice.csv")
            if (root / "requests").is_dir()
            else [],
            key=lambda item: (item.stat().st_mtime, str(item)),
            reverse=True,
        )
        for path in (
            *request_paths,
            root / "reports" / CLOSE_ADVICE_CSV,
            root / CLOSE_ADVICE_CSV,
        ):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.exists():
                continue
            source = _Source(resolved, source_type="agent_tool")
            manifest = _validate_source_manifest(
                source,
                desired_market=desired_market,
                query_account=query.account,
                expected_run_id=None,
            )
            if manifest.get("ok"):
                out.append(source)
        if out:
            return out[:1]
    return out


def _validate_source_manifest(
    source: _Source,
    *,
    desired_market: str | None,
    query_account: str | None,
    expected_run_id: str | None,
) -> dict[str, Any]:
    snapshot = read_close_advice_report_snapshot(
        csv_path=source.path,
        desired_market=desired_market,
        account=source.account or query_account,
        expected_run_id=expected_run_id,
    )
    validation = snapshot["validation"]
    if validation.get("ok"):
        source.csv_bytes = snapshot["csv_bytes"]
        source.generated_at_utc = str(validation.get("generated_at_utc") or "").strip() or None
    return validation


def _sources_as_of(sources: list[_Source]) -> str:
    observed: list[datetime] = []
    for source in sources:
        raw = str(source.generated_at_utc or "").strip()
        if raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                observed.append(parsed.astimezone(timezone.utc))
                continue
            except ValueError:
                pass
        observed.append(datetime.fromtimestamp(source.path.stat().st_mtime, tz=timezone.utc))
    return min(observed).isoformat()


def _invalid_report_error(
    source: _Source,
    *,
    validation: dict[str, Any],
    mask_path: Callable[[Any], str | None],
) -> AgentToolError:
    return AgentToolError(
        code="DEPENDENCY_INVALID",
        message="平仓建议报告完整性校验失败。",
        hint="请重新生成严格版平仓建议报告。",
        details={
            "csv_path": mask_path(source.path),
            "reason": str(validation.get("reason") or "unknown"),
        },
    )


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
    if source.csv_bytes is None:
        raise AgentToolError(
            code="DEPENDENCY_INVALID",
            message="平仓建议报告缺少已校验的快照。",
            hint="请重新生成严格版平仓建议报告。",
        )
    rows: list[dict[str, Any]] = []
    try:
        reader = csv.DictReader(
            StringIO(source.csv_bytes.decode("utf-8-sig"), newline="")
        )
        for raw in reader:
            if not isinstance(raw, dict):
                continue
            row = {
                str(key): value
                for key, value in raw.items()
                if key is not None
            }
            if source.account and not str(row.get("account") or "").strip():
                row["account"] = source.account
            row["_source_run_id"] = source.run_id
            row["_source_type"] = source.source_type
            rows.append(row)
    except (UnicodeError, csv.Error) as exc:
        raise AgentToolError(
            code="READ_ERROR",
            message=f"读取平仓建议报告失败：{source.path.name}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    return rows


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
    decision_fields = _decision_fields_for_read(row)
    keys = (
        "account",
        "position_lot_id",
        "quote_mode",
        "required_data_snapshot_plan_id",
        "required_data_snapshot_manifest_sha256",
        "close_advice_required_data_plan_sha256",
        "required_data_requirement_id",
        "required_data_binding_id",
        "required_data_snapshot_id",
        "required_data_receipt_hash",
        "required_data_payload_sha256",
        "required_data_source_observed_at",
        "required_data_expires_at",
        "symbol",
        "option_type",
        "side",
        "position_side",
        "expiration",
        "expiration_ymd",
        "strike",
        "contracts_open",
        "multiplier",
        "currency",
        "premium",
        "spot",
        "bid",
        "ask",
        "close_mid",
        "dte",
        "original_dte",
        "remaining_term_ratio",
        "position_lifecycle_state",
        "is_otm",
        "spread_ratio",
        "opening_gross_credit",
        "estimated_open_fee",
        "opening_net_credit",
        "estimated_close_fee",
        "all_in_close_cost",
        "net_capture_ratio",
        "close_cost_ratio",
        "fee_calc_status",
        "fee_calc_basis",
        "estimated_pnl_if_close_net",
        "evaluation_status",
        "quote_status",
        "reason",
        "policy_version",
        "recommendation_state",
        "decision_basis",
        "decision_evidence_status",
        "strategy_family",
        "strategy_profile",
        "data_quality_flags",
    )
    source_row = {**row, **decision_fields}
    out = {
        key: _normalize_public_value(key, source_row.get(key))
        for key in keys
        if source_row.get(key) not in (None, "")
    }
    canonical = _row_symbol(row)
    if canonical:
        out["symbol"] = canonical
    if "expiration" not in out and row.get("expiration_ymd"):
        out["expiration"] = str(row.get("expiration_ymd") or "").strip()
    inferred_side = (
        _canonical_side(row.get("side"))
        or _canonical_side(row.get("position_side"))
    )
    if inferred_side:
        out["side"] = inferred_side
    if row.get("_source_run_id"):
        out["source_run_id"] = row.get("_source_run_id")
    return {key: value for key, value in out.items() if value not in (None, "")}


def _decision_fields_for_read(row: dict[str, Any]) -> dict[str, Any]:
    policy_version = str(row.get("policy_version") or "").strip()
    recommendation = _lower(row.get("recommendation_state"))
    decision_basis = str(row.get("decision_basis") or "").strip()
    evidence_status = _lower(row.get("decision_evidence_status"))
    evaluation_status = _lower(row.get("evaluation_status"))
    expected_evidence_status = (
        DECISION_EVIDENCE_NOT_EVALUABLE
        if recommendation == RECOMMENDATION_NOT_EVALUABLE
        else DECISION_EVIDENCE_COMPLETE
    )
    if (
        policy_version == STRICT_CLOSE_POLICY_VERSION
        and recommendation
        in {
            RECOMMENDATION_CLOSE,
            RECOMMENDATION_HOLD,
            RECOMMENDATION_NOT_EVALUABLE,
        }
        and decision_basis
        and evidence_status == expected_evidence_status
        and (
            (
                recommendation in {RECOMMENDATION_CLOSE, RECOMMENDATION_HOLD}
                and evaluation_status == "priced"
            )
            or (
                recommendation == RECOMMENDATION_NOT_EVALUABLE
                and evaluation_status != "priced"
            )
        )
    ):
        return {
            "policy_version": policy_version,
            "recommendation_state": recommendation,
            "decision_basis": decision_basis,
            "decision_evidence_status": evidence_status,
        }

    if policy_version != STRICT_CLOSE_POLICY_VERSION:
        invalid_basis = "unsupported_or_missing_strict_policy_version"
    elif recommendation not in {
        RECOMMENDATION_CLOSE,
        RECOMMENDATION_HOLD,
        RECOMMENDATION_NOT_EVALUABLE,
    }:
        invalid_basis = "invalid_or_missing_strict_recommendation_state"
    elif not decision_basis:
        invalid_basis = "missing_strict_decision_basis"
    elif (
        recommendation in {RECOMMENDATION_CLOSE, RECOMMENDATION_HOLD}
        and evaluation_status != "priced"
    ):
        invalid_basis = "strict_decision_not_priced"
    elif (
        recommendation == RECOMMENDATION_NOT_EVALUABLE
        and evaluation_status == "priced"
    ):
        invalid_basis = "strict_not_evaluable_marked_priced"
    else:
        invalid_basis = "invalid_strict_decision_evidence_status"
    return {
        "policy_version": policy_version or "unversioned_report",
        "recommendation_state": RECOMMENDATION_NOT_EVALUABLE,
        "decision_basis": invalid_basis,
        "decision_evidence_status": DECISION_EVIDENCE_NOT_EVALUABLE,
        "evaluation_status": "not_evaluable",
        "quote_status": "not_evaluable",
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    recommendation_counts: dict[str, int] = {}
    evaluation_counts: dict[str, int] = {}
    for row in rows:
        recommendation = _lower(row.get("recommendation_state")) or "-"
        evaluation = _lower(row.get("evaluation_status")) or "not_evaluable"
        recommendation_counts[recommendation] = recommendation_counts.get(recommendation, 0) + 1
        evaluation_counts[evaluation] = evaluation_counts.get(evaluation, 0) + 1
    return {
        "recommendation_counts": recommendation_counts,
        "evaluation_counts": evaluation_counts,
        "not_evaluable_count": evaluation_counts.get("not_evaluable", 0),
    }


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


def _sort_key(row: dict[str, Any]) -> tuple[int, float, float, str, str, float]:
    recommendation = _lower(row.get("recommendation_state"))
    capture = _float_or_none(row.get("net_capture_ratio"))
    close_cost = _float_or_none(row.get("all_in_close_cost"))
    return (
        {
            RECOMMENDATION_CLOSE: 0,
            RECOMMENDATION_HOLD: 1,
            RECOMMENDATION_NOT_EVALUABLE: 2,
        }.get(recommendation, 3),
        -(capture if capture is not None else -1.0),
        close_cost if close_cost is not None else float("inf"),
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
        "original_dte",
        "remaining_term_ratio",
        "spot",
        "multiplier",
        "spread_ratio",
        "opening_gross_credit",
        "estimated_open_fee",
        "opening_net_credit",
        "estimated_close_fee",
        "all_in_close_cost",
        "net_capture_ratio",
        "close_cost_ratio",
        "estimated_pnl_if_close_net",
    }
)

_BOOLEAN_PUBLIC_FIELDS = frozenset({"is_otm"})


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
