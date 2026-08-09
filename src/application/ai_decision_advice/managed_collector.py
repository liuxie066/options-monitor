from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.symbol_identity import futu_underlier_code, symbol_market
from src.application.agent_tool_config import load_runtime_config
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.application.ai_decision_advice.collector import (
    ModelCallResult,
    compute_cutoffs,
    run_evidence_collector,
)
from src.application.ai_decision_advice.config import (
    EVIDENCE_FULL_RECHECK_SECONDS,
    EVIDENCE_LOOKBACK_DAYS,
    MODEL,
    ai_decision_advice_enabled,
    resolve_api_key,
)
from src.application.ai_decision_advice.evidence_store import (
    read_evidence_records,
    resolve_latest_success_snapshot,
)
from src.application.ai_decision_advice.identity import (
    RefreshQueue,
    build_observation_set,
    build_symbol_identity_snapshot,
    identity_by_symbol,
    load_observation_set,
    observed_symbols_from_snapshot,
    publish_symbol_identity_snapshot,
)
from src.application.ai_decision_advice.prompts import (
    PROMPT_PACK_EVIDENCE,
    compile_prompt_pack,
)
from src.application.opend_fetch_config import DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT
from src.infrastructure.deepseek_responses import (
    audit_web_search_calls_by_symbol,
    create_deepseek_response,
    extract_native_url_citations,
    extract_native_web_search_sources,
    extract_output_text,
    extract_usage,
    response_fingerprint,
)
from src.infrastructure.futu_gateway import build_futu_gateway


ModelRunnerFactory = Callable[[str], Callable[..., ModelCallResult]]


def build_deepseek_evidence_runner(api_key: str):
    """Create the provider adapter while keeping raw responses in memory only."""

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
            json_schema={"name": "external_evidence", "schema": schema}
            if schema
            else None,
            timeout=max(1, int(timeout)),
        )
        identity_rows = [
            dict(row)
            for row in payload.get("symbols") or []
            if isinstance(row, Mapping)
        ]
        return ModelCallResult(
            output_text=extract_output_text(response),
            usage=extract_usage(response),
            response_sha256=response_fingerprint(response),
            web_search_audit=audit_web_search_calls_by_symbol(
                response,
                identity_rows=identity_rows,
            ),
            native_citations=tuple(extract_native_url_citations(response)),
            native_search_sources=tuple(
                extract_native_web_search_sources(
                    response,
                    identity_rows=identity_rows,
                )
            ),
        )

    return runner


def config_scan_symbols(configs: list[Mapping[str, Any]]) -> list[str]:
    symbols: list[str] = []
    for cfg in configs:
        for item in cfg.get("symbols") or []:
            if isinstance(item, Mapping):
                value = item.get("symbol")
            else:
                value = item
            if str(value or "").strip():
                symbols.append(str(value))
    return symbols


def run_managed_collector(
    *,
    config_keys: list[str],
    runtime_root: Path,
    dry_run: bool = False,
    now: datetime | None = None,
    model_runner_factory: ModelRunnerFactory = build_deepseek_evidence_runner,
    market_snapshot_provider: Callable[..., Mapping[str, Mapping[str, Any]]] | None = None,
    basic_info_provider: Callable[[list[str]], list[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Application owner for one timer-driven evidence refresh."""

    configs_by_market: dict[str, dict[str, Any]] = {}
    for key in config_keys:
        _path, cfg = load_runtime_config(
            config_key=key,
            expected_market=key.lower(),
        )
        configs_by_market[key.upper()] = dict(cfg)
    configs = list(configs_by_market.values())
    if not any(ai_decision_advice_enabled(cfg) for cfg in configs):
        return {"status": "skipped", "reason": "ai_decision_advice_disabled"}

    api_key = resolve_api_key()
    if not api_key and not dry_run:
        return {"status": "failed", "reason": "missing_api_key"}

    observation_snapshot = load_observation_set(runtime_root)
    if observation_snapshot is None:
        observed = build_observation_set(scan_symbols=config_scan_symbols(configs))
        observation_source = "config_fallback"
    else:
        observed = observed_symbols_from_snapshot(observation_snapshot)
        observation_source = "anonymous_snapshot"
    effective_now = now or datetime.now(timezone.utc)
    records = read_evidence_records(runtime_root)
    queue = RefreshQueue.build(
        observed,
        last_attempt_by_symbol=_last_attempt_by_symbol(records),
    )
    result: dict[str, Any] = {
        "status": "dry_run",
        "observation_source": observation_source,
        "observation_count": len(queue.symbols()),
    }
    if dry_run:
        return result

    if market_snapshot_provider is None:
        identity_provider = _opend_market_snapshot_provider(configs_by_market)
        identity_basic_info_provider = (
            basic_info_provider or _opend_basic_info_provider(configs_by_market)
        )
    else:
        identity_provider = market_snapshot_provider
        identity_basic_info_provider = basic_info_provider
    identity_snapshot = build_symbol_identity_snapshot(
        observed,
        market_snapshot_provider=identity_provider,
        basic_info_provider=identity_basic_info_provider,
        observed_at=effective_now,
    )
    publish_symbol_identity_snapshot(base=runtime_root, payload=identity_snapshot)

    identities = identity_by_symbol(identity_snapshot)
    last_success: dict[str, str | None] = {}
    search_modes: dict[str, str] = {}
    for symbol in queue.symbols():
        identity_hash = str(
            identities.get(symbol, {}).get("identity_semantic_sha256") or ""
        )
        status, _rows, error = resolve_latest_success_snapshot(
            records,
            symbol=symbol,
            identity_semantic_sha256=identity_hash or None,
        )
        if error or status is None:
            last_success[symbol] = None
            search_modes[symbol] = "full"
            continue
        last_success[symbol] = str(status.get("last_success_at") or "") or None
        latest_full = _latest_full_success_at(
            records,
            symbol=symbol,
            identity_semantic_sha256=identity_hash,
        )
        search_modes[symbol] = (
            "full"
            if latest_full is None
            or (effective_now - latest_full).total_seconds()
            >= EVIDENCE_FULL_RECHECK_SECONDS
            else "incremental"
        )
    cutoffs = compute_cutoffs(last_success, now=effective_now)
    first_cutoff = (
        effective_now - timedelta(days=EVIDENCE_LOOKBACK_DAYS)
    ).isoformat()
    for symbol, mode in search_modes.items():
        if mode == "full":
            cutoffs[symbol] = first_cutoff

    summary = run_evidence_collector(
        base=runtime_root,
        queue_symbols=queue.symbols(),
        identity_snapshot=identity_snapshot,
        cutoff_by_symbol=cutoffs,
        search_mode_by_symbol=search_modes,
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_EVIDENCE),
        model_runner=model_runner_factory(str(api_key)),
        evidence_run_id=f"ev-{effective_now.strftime('%Y%m%dT%H%M%SZ')}",
        now=effective_now,
    )
    result_status = _collector_result_status(
        summary,
        observation_count=len(queue.symbols()),
    )
    result.update(
        {
            "status": result_status,
            "cutoff_count": len(cutoffs),
            "summary": {
                "budget_seconds": summary.budget_seconds,
                "budget_exhausted": summary.budget_exhausted,
                "completed_count": len(summary.completed_symbols),
                "identity_unavailable_count": len(
                    summary.identity_unavailable_symbols
                ),
                "failed_count": len(summary.failed_symbols),
                "unfinished_count": len(summary.unfinished_symbols),
                "repair_attempts": summary.repair_attempts,
                "records_appended": summary.records_appended,
            },
        }
    )
    if result_status == "failed":
        result["reason"] = "no_evidence_refresh_completed"
    return result


def _last_attempt_by_symbol(
    records: list[dict[str, Any]],
) -> dict[str, str]:
    latest: dict[str, datetime] = {}
    for row in records:
        if row.get("kind") != "symbol_status":
            continue
        symbol = str(row.get("symbol") or "")
        checked_at = _parse_utc(row.get("last_checked_at"))
        if not symbol or checked_at is None:
            continue
        if symbol not in latest or checked_at > latest[symbol]:
            latest[symbol] = checked_at
    return {symbol: checked_at.isoformat() for symbol, checked_at in latest.items()}


def _collector_result_status(
    summary,
    *,
    observation_count: int,
) -> str:
    gaps = (
        len(summary.identity_unavailable_symbols)
        + len(summary.failed_symbols)
        + len(summary.unfinished_symbols)
    )
    if observation_count > 0 and not summary.completed_symbols and gaps:
        return "failed"
    if gaps:
        return "partial"
    return "completed"


def _latest_full_success_at(
    records: list[dict[str, Any]],
    *,
    symbol: str,
    identity_semantic_sha256: str,
) -> datetime | None:
    timestamps: list[datetime] = []
    for row in records:
        if (
            row.get("kind") != "symbol_status"
            or row.get("symbol") != symbol
            or row.get("search_status") != "completed"
            or row.get("search_mode") != "full"
            or str(row.get("identity_semantic_sha256") or "")
            != identity_semantic_sha256
        ):
            continue
        parsed = _parse_utc(row.get("last_success_at"))
        if parsed is not None:
            timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _opend_market_snapshot_provider(
    configs_by_market: Mapping[str, Mapping[str, Any]],
    *,
    gateway_factory: Callable[..., Any] = build_futu_gateway,
) -> Callable[[str, list[str]], Mapping[str, Mapping[str, Any]]]:
    endpoints = _opend_endpoints(configs_by_market)

    def provider(market: str, symbols: list[str]) -> Mapping[str, Mapping[str, Any]]:
        endpoint = endpoints.get(str(market or "").upper())
        if endpoint is None:
            return {}
        codes = [futu_underlier_code(symbol) for symbol in symbols]
        requested = list(dict.fromkeys(code for code in codes if code))
        if not requested:
            return {}
        try:
            gateway = gateway_factory(host=endpoint[0], port=endpoint[1])
        except Exception:
            return {}
        by_code: dict[str, dict[str, Any]] = {}
        try:
            for start in range(
                0,
                len(requested),
                DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT,
            ):
                batch = requested[
                    start : start + DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT
                ]
                try:
                    data = gateway.get_snapshot(batch)
                except Exception:
                    continue
                rows = _opend_rows(data)
                for row in rows:
                    if isinstance(row, Mapping) and row.get("code"):
                        by_code[str(row["code"])] = dict(row)
            return by_code
        finally:
            gateway.close()

    return provider


def _opend_basic_info_provider(
    configs_by_market: Mapping[str, Mapping[str, Any]],
    *,
    gateway_factory: Callable[..., Any] = build_futu_gateway,
) -> Callable[[list[str]], list[Mapping[str, Any]]]:
    endpoints = _opend_endpoints(configs_by_market)

    def provider(symbols: list[str]) -> list[Mapping[str, Any]]:
        by_market: dict[str, list[str]] = {}
        for symbol in symbols:
            market = str(symbol_market(symbol) or "").upper()
            code = futu_underlier_code(symbol)
            if market and code:
                by_market.setdefault(market, []).append(code)

        rows: list[Mapping[str, Any]] = []
        for market, raw_codes in sorted(by_market.items()):
            endpoint = endpoints.get(market)
            if endpoint is None:
                continue
            requested = list(dict.fromkeys(raw_codes))
            try:
                gateway = gateway_factory(host=endpoint[0], port=endpoint[1])
            except Exception:
                continue
            try:
                for start in range(
                    0,
                    len(requested),
                    DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT,
                ):
                    batch = requested[
                        start : start + DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT
                    ]
                    try:
                        data = gateway.get_stock_basicinfo(
                            market=market,
                            codes=batch,
                        )
                    except Exception:
                        continue
                    rows.extend(
                        dict(row)
                        for row in _opend_rows(data)
                        if isinstance(row, Mapping) and row.get("code")
                    )
            finally:
                gateway.close()
        return rows

    return provider


def _opend_rows(data: Any) -> list[Mapping[str, Any]]:
    if hasattr(data, "to_dict"):
        try:
            rows = data.to_dict("records")
        except TypeError:
            rows = data.to_dict(orient="records")
    elif isinstance(data, list):
        rows = data
    else:
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _opend_endpoints(
    configs_by_market: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[str, int]]:
    endpoints: dict[str, tuple[str, int]] = {}
    for raw_market, cfg in configs_by_market.items():
        market = str(raw_market or "").upper()
        route = resolve_futu_quote_route(
            cfg,
            config_key=market.lower(),
            market=market,
        )
        if route.ok and route.host is not None and route.port is not None:
            endpoints[market] = (route.host, route.port)
    return endpoints


__all__ = [
    "build_deepseek_evidence_runner",
    "config_scan_symbols",
    "run_managed_collector",
]
