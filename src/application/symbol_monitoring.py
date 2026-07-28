from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any, Callable, Protocol

from domain.domain.sell_call_config import resolve_effective_sell_call_min_strike
from domain.domain.symbol_identity import symbol_market
from src.application.strategy_scan_failures import append_strategy_scan_failure
from src.application.opend_fetch_config import opend_discovery_kwargs, opend_fetch_kwargs
from src.application.required_data_planning import build_required_data_fetch_plan
from src.application.required_data_snapshot import FrozenRequiredDataUnavailable
from src.application.strategy_scan_status import publish_strategy_scan_status
from src.application.yield_enhancement_config import (
    COMBO_YIELD_CONFIG_KEY,
    derive_yield_enhancement_policy,
    resolve_yield_enhancement_cfg,
)


log = logging.getLogger(__name__)


class _PrefilterResultLike(Protocol):
    want_put: bool
    want_call: bool
    sp: dict[str, Any]
    cc: dict[str, Any]
    stock: dict[str, Any] | None


@dataclass(frozen=True)
class SymbolMonitoringInputs:
    py: str
    base: Path
    symbol_cfg: dict[str, Any]
    top_n: int
    portfolio_ctx: dict[str, Any] | None
    usd_per_cny_exchange_rate: float | None
    cny_per_hkd_exchange_rate: float | None
    timeout_sec: int | None
    required_data_dir: Path
    report_dir: Path
    state_dir: Path | None
    is_scheduled: bool
    runtime_config: dict[str, Any] | None = None
    fetch_only: bool = False
    risk_policy_version: str | None = None
    quote_snapshot_id: str | None = None
    all_decisions_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None
    position_advice_producer_run_id: str | None = None
    required_data_snapshot_manifest: Path | None = None
    required_data_snapshot_run_id: str | None = None
    candidate_capture_status_sink_fn: (
        Callable[[dict[str, Any]], None] | None
    ) = None


@dataclass(frozen=True)
class SymbolMonitoringDependencies:
    build_converter_fn: Callable[..., object]
    apply_prefilters_fn: Callable[..., _PrefilterResultLike]
    apply_multiplier_cache_fn: Callable[..., None]
    ensure_required_data_fn: Callable[..., None]
    run_sell_put_scan_fn: Callable[..., object]
    empty_sell_put_summary_fn: Callable[..., object]
    run_sell_call_scan_fn: Callable[..., object]
    empty_sell_call_summary_fn: Callable[..., object]
    materialize_empty_sell_call_artifacts_fn: Callable[..., None]
    run_combo_yield_scan_fn: Callable[..., object]
    empty_combo_yield_summary_fn: Callable[..., object]
    materialize_empty_sell_put_artifacts_fn: Callable[..., None]
    materialize_empty_combo_yield_artifacts_fn: Callable[..., object]


def _append_summary_result(summary_rows: list[dict[str, Any]], result: object) -> None:
    if result is None:
        return
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                summary_rows.append(item)
        return
    if isinstance(result, dict):
        summary_rows.append(result)


def _summary_candidate_count(result: object) -> int:
    rows = result if isinstance(result, list) else [result]
    return sum(
        max(0, int(item.get("candidate_count") or 0))
        for item in rows
        if isinstance(item, dict)
    )


def run_symbol_monitoring(
    *,
    inputs: SymbolMonitoringInputs,
    deps: SymbolMonitoringDependencies,
) -> list[dict[str, Any]]:
    symbol_cfg = dict(inputs.symbol_cfg or {})
    symbol = str(symbol_cfg["symbol"])
    symbol_lower = symbol.lower()
    limit_expirations = symbol_cfg.get("fetch", {}).get("limit_expirations", 8)

    sp: dict[str, Any] = dict(symbol_cfg.get("sell_put", {}) or {})
    cc: dict[str, Any] = dict(symbol_cfg.get("sell_call", {}) or {})
    yield_enhancement_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    configured_put = bool(sp.get("enabled", False))
    configured_call = bool(cc.get("enabled", False))
    want_put = configured_put
    want_call = configured_call
    market_sp = dict(sp)

    exchange_rate_converter = deps.build_converter_fn(
        usd_per_cny_exchange_rate=inputs.usd_per_cny_exchange_rate,
        cny_per_hkd_exchange_rate=inputs.cny_per_hkd_exchange_rate,
    )

    prefilters = deps.apply_prefilters_fn(
        symbol=symbol,
        sp=sp,
        cc=cc,
        want_put=want_put,
        want_call=want_call,
        portfolio_ctx=inputs.portfolio_ctx,
    )
    want_put = bool(prefilters.want_put)
    want_call = bool(prefilters.want_call)
    sp = dict(prefilters.sp)
    cc = dict(prefilters.cc)
    symbol_cfg["sell_put"] = sp
    symbol_cfg["sell_call"] = cc
    yield_enhancement_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    if yield_enhancement_cfg:
        symbol_cfg.pop("yield_enhancement", None)
        symbol_cfg[COMBO_YIELD_CONFIG_KEY] = yield_enhancement_cfg
    yield_enhancement_policy = derive_yield_enhancement_policy(yield_enhancement_cfg)
    stock = prefilters.stock
    if want_call and isinstance(stock, dict):
        effective_min_strike = resolve_effective_sell_call_min_strike(
            min_strike=cc.get("min_strike"),
            avg_cost=stock.get("avg_cost"),
            cost_multiplier=cc.get("min_strike_cost_multiplier", 1.0),
        )
        if effective_min_strike is not None:
            cc["min_strike"] = effective_min_strike
            symbol_cfg["sell_call"] = cc
    want_yield_enhancement = bool(yield_enhancement_policy.enabled)
    fetch_want_put = bool(want_put or want_yield_enhancement)
    fetch_want_call = bool(want_call or want_yield_enhancement)
    fetch_sell_put_cfg = market_sp if want_yield_enhancement else sp
    runtime_config: dict[str, Any] = (
        inputs.runtime_config
        if isinstance(inputs.runtime_config, dict)
        else symbol_cfg
    )
    frozen_status_enabled = bool(
        inputs.required_data_snapshot_manifest is not None
        and str(inputs.position_advice_producer_run_id or "").strip()
    )
    portfolio_cfg = (
        runtime_config.get("portfolio")
        if isinstance(runtime_config.get("portfolio"), dict)
        else {}
    )
    status_account = str(portfolio_cfg.get("account") or "").strip().lower()
    status_market = str(
        symbol_market(symbol)
        or symbol_cfg.get("broker")
        or ""
    ).strip().upper()

    def _publish_status(
        *,
        family: str,
        status: str,
        candidate_count: int | None = None,
        reason: str | None = None,
        snapshot_id: str | None = None,
        receipt_relpath: str | None = None,
    ) -> None:
        if not frozen_status_enabled:
            return
        try:
            publish_strategy_scan_status(
                report_dir=inputs.report_dir,
                run_id=str(inputs.position_advice_producer_run_id),
                account=status_account,
                market=status_market,
                symbol=symbol,
                strategy_family=family,
                status=status,
                candidate_count=candidate_count,
                reason=reason,
                snapshot_id=snapshot_id,
                receipt_relpath=receipt_relpath,
            )
        except Exception:
            log.exception(
                "symbol_monitoring: strategy status publish failed for %s/%s",
                symbol,
                family,
            )

    frozen_required_data = inputs.required_data_snapshot_manifest is not None
    if not frozen_required_data:
        try:
            deps.apply_multiplier_cache_fn(
                base=inputs.base,
                required_data_dir=inputs.required_data_dir,
                symbol=symbol,
            )
        except Exception:
            pass

    fetch_cfg = dict(symbol_cfg.get("fetch", {}) or {})
    discovery_fetch_kwargs = opend_discovery_kwargs(runtime_config)
    fetch_request_kwargs = opend_fetch_kwargs(runtime_config)
    fetch_plan = None
    if not frozen_required_data:
        fetch_plan = build_required_data_fetch_plan(
            base=inputs.base,
            required_data_dir=inputs.required_data_dir,
            symbol=symbol,
            limit_expirations=int(limit_expirations),
            want_put=fetch_want_put,
            want_call=want_call,
            sell_put_cfg=fetch_sell_put_cfg,
            sell_call_cfg=cc,
            yield_enhancement_cfg=yield_enhancement_cfg,
            symbol_cfg=symbol_cfg,
            fetch_host=str(fetch_cfg.get("host") or "127.0.0.1"),
            fetch_port=int(fetch_cfg.get("port") or 11111),
            snapshot_max_wait_sec=float(discovery_fetch_kwargs["snapshot_max_wait_sec"]),
            snapshot_window_sec=float(discovery_fetch_kwargs["snapshot_window_sec"]),
            snapshot_max_calls=int(discovery_fetch_kwargs["snapshot_max_calls"]),
            expiration_max_wait_sec=float(discovery_fetch_kwargs["expiration_max_wait_sec"]),
            expiration_window_sec=float(discovery_fetch_kwargs["expiration_window_sec"]),
            expiration_max_calls=int(discovery_fetch_kwargs["expiration_max_calls"]),
        )
    fetch_max_strike = fetch_sell_put_cfg.get("max_strike")
    fetch_max_strike_value = (
        float(fetch_max_strike)
        if (fetch_want_put and fetch_max_strike is not None)
        else None
    )

    try:
        required_data_kwargs: dict[str, Any] = {
            "py": inputs.py,
            "base": inputs.base,
            "symbol": symbol,
            "required_data_dir": inputs.required_data_dir,
            "limit_expirations": limit_expirations,
            "want_put": fetch_want_put,
            "want_call": fetch_want_call,
            "timeout_sec": inputs.timeout_sec,
            "is_scheduled": bool(inputs.is_scheduled),
            "state_dir": inputs.state_dir,
            "fetch_source": str(fetch_cfg.get("source") or "opend"),
            "fetch_host": str(fetch_cfg.get("host") or "127.0.0.1"),
            "fetch_port": int(fetch_cfg.get("port") or 11111),
            "max_strike": fetch_max_strike_value,
            "min_dte": None,
            "max_dte": None,
            "fetch_plan": fetch_plan,
            "report_dir": inputs.report_dir,
            "opend_fetch_config": fetch_request_kwargs,
            "position_advice_producer_run_id": (
                inputs.position_advice_producer_run_id
            ),
        }
        if inputs.required_data_snapshot_manifest is not None:
            required_data_kwargs.update(
                {
                    "required_data_snapshot_manifest": (
                        inputs.required_data_snapshot_manifest
                    ),
                    "required_data_snapshot_run_id": (
                        inputs.required_data_snapshot_run_id
                    ),
                }
            )
        quote_evidence = deps.ensure_required_data_fn(
            **required_data_kwargs,
        )
    except FrozenRequiredDataUnavailable as exc:
        summary_rows: list[dict[str, Any]] = []

        def _unavailable_summary(result: object) -> None:
            if not isinstance(result, dict):
                return
            row = dict(result)
            row["candidate_count"] = 0
            row["note"] = "行情快照不可用"
            summary_rows.append(row)

        for family, enabled in (
            ("sell_put", want_put),
            ("combo_yield", want_yield_enhancement),
            ("covered_call", want_call),
        ):
            if enabled:
                append_strategy_scan_failure(
                    report_dir=inputs.report_dir,
                    symbol=symbol,
                    strategy_family=family,
                    error=exc,
                )
        deps.materialize_empty_sell_put_artifacts_fn(
            report_dir=inputs.report_dir,
            symbol_lower=symbol_lower,
        )
        deps.materialize_empty_combo_yield_artifacts_fn(
            report_dir=inputs.report_dir,
            symbol_lower=symbol_lower,
        )
        deps.materialize_empty_sell_call_artifacts_fn(
            report_dir=inputs.report_dir,
            symbol_lower=symbol_lower,
        )
        if want_put:
            _unavailable_summary(
                deps.empty_sell_put_summary_fn(symbol, symbol_cfg=symbol_cfg)
            )
        if want_yield_enhancement:
            _unavailable_summary(
                deps.empty_combo_yield_summary_fn(symbol, symbol_cfg=symbol_cfg)
            )
        if want_call:
            _unavailable_summary(
                deps.empty_sell_call_summary_fn(symbol, symbol_cfg=symbol_cfg)
            )
        if inputs.candidate_capture_status_sink_fn is not None:
            for strategy_mode, enabled in (("put", configured_put), ("call", configured_call)):
                if enabled:
                    inputs.candidate_capture_status_sink_fn(
                        {
                            "symbol": symbol.upper(),
                            "strategy_mode": strategy_mode,
                            "status": "failed",
                            "reason": "required_data_snapshot_unavailable",
                            "quote_snapshot_id": exc.snapshot_id,
                            "quote_receipt_relpath": exc.receipt_relpath,
                        }
                    )
        for family, enabled in (
            ("sell_put", want_put),
            ("combo_yield", want_yield_enhancement),
            ("covered_call", want_call),
        ):
            if enabled:
                _publish_status(
                    family=family,
                    status="unavailable",
                    reason="required_data_snapshot_unavailable",
                    snapshot_id=exc.snapshot_id,
                    receipt_relpath=exc.receipt_relpath,
                )
        return summary_rows

    if bool(inputs.fetch_only):
        return []

    resolved_quote_snapshot_id = str(
        (
            quote_evidence.get("snapshot_id")
            if isinstance(quote_evidence, dict)
            else inputs.quote_snapshot_id
        )
        or ""
    ).strip()
    quote_receipt_relpath = str(
        (
            quote_evidence.get("receipt_relpath")
            if isinstance(quote_evidence, dict)
            else ""
        )
        or ""
    ).strip()
    capture_enabled = bool(
        inputs.all_decisions_sink_fn is not None
        and str(inputs.risk_policy_version or "").strip()
        and resolved_quote_snapshot_id
    )

    def _capture_sink_for(
    ) -> tuple[Callable[[list[dict[str, Any]]], None] | None, dict[str, bool]]:
        state = {"called": False}
        if not capture_enabled:
            return None, state

        def _sink(rows: list[dict[str, Any]]) -> None:
            state["called"] = True
            if inputs.all_decisions_sink_fn is not None:
                inputs.all_decisions_sink_fn(rows)

        return _sink, state

    put_capture_sink, put_capture_state = _capture_sink_for()
    call_capture_sink, call_capture_state = _capture_sink_for()

    def _report_capture(
        *,
        strategy_mode: str,
        status: str,
        reason: str,
    ) -> None:
        if inputs.candidate_capture_status_sink_fn is None:
            return
        inputs.candidate_capture_status_sink_fn(
            {
                "symbol": symbol.upper(),
                "strategy_mode": strategy_mode,
                "status": status,
                "reason": reason,
                "quote_snapshot_id": resolved_quote_snapshot_id or None,
                "quote_receipt_relpath": quote_receipt_relpath or None,
            }
        )

    summary_rows: list[dict[str, Any]] = []

    if want_put:
        try:
            put_result = deps.run_sell_put_scan_fn(
                py=inputs.py,
                base=inputs.base,
                sym=symbol,
                symbol=symbol,
                symbol_lower=symbol_lower,
                symbol_cfg=symbol_cfg,
                sp=sp,
                top_n=inputs.top_n,
                required_data_dir=inputs.required_data_dir,
                report_dir=inputs.report_dir,
                timeout_sec=inputs.timeout_sec,
                is_scheduled=bool(inputs.is_scheduled),
                exchange_rate_converter=exchange_rate_converter,
                portfolio_ctx=inputs.portfolio_ctx,
                global_sell_put_liquidity=(symbol_cfg.get("_global_sell_put_liquidity") or {}),
                global_sell_put_event_risk=(symbol_cfg.get("_global_sell_put_event_risk") or {}),
                run_sell_put=True,
                risk_policy_version=inputs.risk_policy_version,
                quote_snapshot_id=resolved_quote_snapshot_id or None,
                all_decisions_sink_fn=put_capture_sink,
            )
            _append_summary_result(
                summary_rows,
                put_result,
            )
            _publish_status(
                family="sell_put",
                status="completed",
                candidate_count=_summary_candidate_count(put_result),
                snapshot_id=resolved_quote_snapshot_id,
                receipt_relpath=quote_receipt_relpath,
            )
            if configured_put:
                _report_capture(
                    strategy_mode="put",
                    status=(
                        "completed"
                        if put_capture_state["called"]
                        else "incomplete"
                    ),
                    reason=(
                        "all_decisions_captured"
                        if put_capture_state["called"]
                        else "all_decisions_not_captured"
                    ),
                )
        except Exception as exc:
            log.exception("symbol_monitoring: sell_put step failed for %s", symbol)
            append_strategy_scan_failure(
                report_dir=inputs.report_dir,
                symbol=symbol,
                strategy_family="sell_put",
                error=exc,
            )
            deps.materialize_empty_sell_put_artifacts_fn(
                report_dir=inputs.report_dir, symbol_lower=symbol_lower
            )
            _append_summary_result(
                summary_rows, deps.empty_sell_put_summary_fn(symbol, symbol_cfg=symbol_cfg)
            )
            _publish_status(
                family="sell_put",
                status="failed",
                reason="sell_put_scan_failed",
                snapshot_id=resolved_quote_snapshot_id,
                receipt_relpath=quote_receipt_relpath,
            )
            if configured_put:
                _report_capture(
                    strategy_mode="put",
                    status="failed",
                    reason="sell_put_scan_failed",
                )
    else:
        deps.materialize_empty_sell_put_artifacts_fn(
            report_dir=inputs.report_dir, symbol_lower=symbol_lower
        )
        _append_summary_result(summary_rows, deps.empty_sell_put_summary_fn(symbol, symbol_cfg=symbol_cfg))
        if configured_put:
            _report_capture(
                strategy_mode="put",
                status="not_applicable",
                reason="sell_put_prefilter_not_applicable",
            )

    if want_yield_enhancement:
        try:
            combo_result = deps.run_combo_yield_scan_fn(
                base=inputs.base,
                sym=symbol,
                symbol=symbol,
                symbol_lower=symbol_lower,
                symbol_cfg=symbol_cfg,
                sell_put_cfg=market_sp,
                top_n=inputs.top_n,
                required_data_dir=inputs.required_data_dir,
                report_dir=inputs.report_dir,
                is_scheduled=bool(inputs.is_scheduled),
                exchange_rate_converter=exchange_rate_converter,
                portfolio_ctx=inputs.portfolio_ctx,
                global_sell_put_liquidity=(symbol_cfg.get("_global_sell_put_liquidity") or {}),
                global_sell_put_event_risk=(symbol_cfg.get("_global_sell_put_event_risk") or {}),
            )
            _append_summary_result(
                summary_rows,
                combo_result,
            )
            _publish_status(
                family="combo_yield",
                status="completed",
                candidate_count=_summary_candidate_count(combo_result),
                snapshot_id=resolved_quote_snapshot_id,
                receipt_relpath=quote_receipt_relpath,
            )
        except Exception as exc:
            log.exception("symbol_monitoring: combo_yield step failed for %s", symbol)
            append_strategy_scan_failure(
                report_dir=inputs.report_dir,
                symbol=symbol,
                strategy_family="combo_yield",
                error=exc,
            )
            deps.materialize_empty_combo_yield_artifacts_fn(
                report_dir=inputs.report_dir, symbol_lower=symbol_lower
            )
            _append_summary_result(
                summary_rows,
                deps.empty_combo_yield_summary_fn(symbol, symbol_cfg=symbol_cfg),
            )
            _publish_status(
                family="combo_yield",
                status="failed",
                reason="combo_yield_scan_failed",
                snapshot_id=resolved_quote_snapshot_id,
                receipt_relpath=quote_receipt_relpath,
            )
    elif not want_yield_enhancement:
        deps.materialize_empty_combo_yield_artifacts_fn(
            report_dir=inputs.report_dir, symbol_lower=symbol_lower
        )

    if want_call:
        option_ctx = (inputs.portfolio_ctx or {}).get("option_ctx") or {}
        try:
            call_result = deps.run_sell_call_scan_fn(
                py=inputs.py,
                base=inputs.base,
                symbol=symbol,
                symbol_lower=symbol_lower,
                symbol_cfg=symbol_cfg,
                cc=cc,
                top_n=inputs.top_n,
                required_data_dir=inputs.required_data_dir,
                report_dir=inputs.report_dir,
                timeout_sec=inputs.timeout_sec,
                is_scheduled=bool(inputs.is_scheduled),
                stock=stock,
                portfolio_ctx=inputs.portfolio_ctx,
                exchange_rate_converter=exchange_rate_converter,
                locked_shares_status=option_ctx.get("locked_shares_status"),
                locked_shares_unavailable_reason=option_ctx.get("locked_shares_unavailable_reason"),
                locked_shares_by_symbol=option_ctx.get("locked_shares_by_symbol"),
                locked_shares_unavailable_by_symbol=option_ctx.get("locked_shares_unavailable_by_symbol"),
                global_sell_call_liquidity=(symbol_cfg.get("_global_sell_call_liquidity") or {}),
                global_sell_call_event_risk=(symbol_cfg.get("_global_sell_call_event_risk") or {}),
                risk_policy_version=inputs.risk_policy_version,
                quote_snapshot_id=resolved_quote_snapshot_id or None,
                all_decisions_sink_fn=call_capture_sink,
            )
            call_status = "completed"
            call_reason = None
            if isinstance(call_result, dict):
                call_result = dict(call_result)
                call_status = str(call_result.pop("_strategy_status", "completed"))
                call_reason = call_result.pop("_strategy_reason", None)
            _append_summary_result(
                summary_rows,
                call_result,
            )
            _publish_status(
                family="covered_call",
                status=call_status,
                reason=(str(call_reason) if call_reason else None),
                candidate_count=_summary_candidate_count(call_result),
                snapshot_id=resolved_quote_snapshot_id,
                receipt_relpath=quote_receipt_relpath,
            )
            if configured_call:
                _report_capture(
                    strategy_mode="call",
                    status=(
                        call_status
                        if call_status != "completed"
                        else (
                            "completed"
                            if call_capture_state["called"]
                            else "incomplete"
                        )
                    ),
                    reason=(
                        str(call_reason)
                        if call_status != "completed" and call_reason
                        else (
                            "all_decisions_captured"
                            if call_capture_state["called"]
                            else "all_decisions_not_captured"
                        )
                    ),
                )
        except Exception as exc:
            log.exception("symbol_monitoring: sell_call step failed for %s", symbol)
            append_strategy_scan_failure(
                report_dir=inputs.report_dir,
                symbol=symbol,
                strategy_family="covered_call",
                error=exc,
            )
            deps.materialize_empty_sell_call_artifacts_fn(
                report_dir=inputs.report_dir,
                symbol_lower=symbol_lower,
            )
            _append_summary_result(
                summary_rows,
                deps.empty_sell_call_summary_fn(symbol, symbol_cfg=symbol_cfg),
            )
            _publish_status(
                family="covered_call",
                status="failed",
                reason="covered_call_scan_failed",
                snapshot_id=resolved_quote_snapshot_id,
                receipt_relpath=quote_receipt_relpath,
            )
            if configured_call:
                _report_capture(
                    strategy_mode="call",
                    status="failed",
                    reason="covered_call_scan_failed",
                )
    else:
        deps.materialize_empty_sell_call_artifacts_fn(
            report_dir=inputs.report_dir,
            symbol_lower=symbol_lower,
        )
        _append_summary_result(summary_rows, deps.empty_sell_call_summary_fn(symbol, symbol_cfg=symbol_cfg))
        if configured_call:
            _report_capture(
                strategy_mode="call",
                status="not_applicable",
                reason="covered_call_prefilter_not_applicable",
            )

    return summary_rows
