from __future__ import annotations

from dataclasses import dataclass
import io
import logging
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import pandas as pd

from domain.domain.sell_call_config import resolve_effective_sell_call_min_strike
from domain.domain.symbol_identity import symbol_market
from src.application.opend_symbol_outputs import SUCCESS_EMPTY_REASON_CODES
from src.application.strategy_scan_failures import append_strategy_scan_failure
from src.application.opend_fetch_config import opend_discovery_kwargs, opend_fetch_kwargs
from src.application.required_data_planning import build_required_data_fetch_plan
from src.application.required_data_snapshot import (
    FrozenRequiredDataBatch,
    FrozenRequiredDataUnavailable,
)
from src.application.strategy_scan_status import (
    StrategyScanStatusError,
    publish_strategy_scan_status,
)
from src.application.combo_yield_config import (
    COMBO_YIELD_CONFIG_KEY,
    derive_combo_yield_policy,
    resolve_combo_yield_cfg,
)


log = logging.getLogger(__name__)


class SymbolProcessingTimeout(TimeoutError):
    pass


class _PrefilterResultLike(Protocol):
    want_put: bool
    want_call: bool
    sp: dict[str, Any]
    cc: dict[str, Any]
    stock: dict[str, Any] | None
    call_skip_reason: str | None


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
    quote_snapshot_id: str | None = None
    source_producer_run_id: str | None = None
    required_data_snapshot_manifest: Path | None = None
    required_data_snapshot_run_id: str | None = None
    required_data_snapshot_batch: FrozenRequiredDataBatch | None = None
    deadline_monotonic: float | None = None
    final_candidates_sink_fn: (
        Callable[[str, list[dict[str, Any]]], None] | None
    ) = None
    candidate_decisions_sink_fn: (
        Callable[[str, list[dict[str, Any]]], None] | None
    ) = None
    combo_evidence_sink_fn: (
        Callable[[dict[str, Any]], None] | None
    ) = None
    experience: bool = False


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
    run_combo_yield_scan_fn: Callable[..., object]
    empty_combo_yield_summary_fn: Callable[..., object]


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
    if not rows or any(not isinstance(item, dict) for item in rows):
        raise ValueError("strategy result is missing or invalid")
    total = 0
    for item in rows:
        count = item.get("candidate_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("strategy candidate count is missing or invalid")
        total += count
    return total


def run_symbol_monitoring(
    *,
    inputs: SymbolMonitoringInputs,
    deps: SymbolMonitoringDependencies,
) -> list[dict[str, Any]]:
    symbol_cfg = dict(inputs.symbol_cfg or {})
    symbol = str(symbol_cfg["symbol"])
    symbol_lower = symbol.lower()
    deadline = inputs.deadline_monotonic
    if deadline is None and inputs.timeout_sec and inputs.timeout_sec > 0:
        deadline = time.monotonic() + inputs.timeout_sec

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise SymbolProcessingTimeout(f"{symbol} symbol processing exceeded deadline")

    check_deadline()
    limit_expirations = symbol_cfg.get("fetch", {}).get("limit_expirations", 8)

    sp: dict[str, Any] = dict(symbol_cfg.get("sell_put", {}) or {})
    cc: dict[str, Any] = dict(symbol_cfg.get("sell_call", {}) or {})
    combo_yield_cfg = resolve_combo_yield_cfg(symbol_cfg)
    initial_combo_policy = derive_combo_yield_policy(combo_yield_cfg)
    combo_requires_stock = bool(
        initial_combo_policy.enabled
        and str(
            (initial_combo_policy.config or {}).get("variant") or "sp_lc"
        ).strip().lower()
        == "cc_lp"
    )
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
        want_call=bool(want_call or combo_requires_stock),
        portfolio_ctx=inputs.portfolio_ctx,
        demo_capacity=inputs.experience,
    )
    want_put = bool(prefilters.want_put)
    want_call = bool(configured_call and prefilters.want_call)
    sp = dict(prefilters.sp)
    cc = dict(prefilters.cc)
    symbol_cfg["sell_put"] = sp
    symbol_cfg["sell_call"] = cc
    combo_yield_cfg = resolve_combo_yield_cfg(symbol_cfg)
    if combo_yield_cfg:
        symbol_cfg[COMBO_YIELD_CONFIG_KEY] = combo_yield_cfg
    combo_yield_policy = derive_combo_yield_policy(combo_yield_cfg)
    combo_variant = str(
        (combo_yield_policy.config or {}).get("variant") or "sp_lc"
    ).strip().lower()
    stock = prefilters.stock
    call_skip_reason = str(
        getattr(prefilters, "call_skip_reason", None) or ""
    ).strip() or None if configured_call else None
    if want_call and isinstance(stock, dict):
        effective_min_strike = resolve_effective_sell_call_min_strike(
            min_strike=cc.get("min_strike"),
            avg_cost=stock.get("avg_cost"),
            cost_multiplier=cc.get("min_strike_cost_multiplier", 1.02),
        )
        if effective_min_strike is not None:
            cc["min_strike"] = effective_min_strike
            symbol_cfg["sell_call"] = cc
    want_combo_yield = bool(combo_yield_policy.enabled)
    fetch_want_put = bool(want_put or want_combo_yield)
    fetch_want_call = bool(want_call or want_combo_yield)
    fetch_sell_put_cfg = market_sp if want_combo_yield else sp
    runtime_config: dict[str, Any] = (
        inputs.runtime_config
        if isinstance(inputs.runtime_config, dict)
        else symbol_cfg
    )
    frozen_status_enabled = bool(
        inputs.required_data_snapshot_manifest is not None
        and str(inputs.source_producer_run_id or "").strip()
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
        source_outcome: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        check_deadline()
        if not frozen_status_enabled:
            return
        try:
            publish_strategy_scan_status(
                report_dir=inputs.report_dir,
                run_id=str(inputs.source_producer_run_id),
                account=status_account,
                market=status_market,
                symbol=symbol,
                strategy_family=family,
                status=status,
                candidate_count=candidate_count,
                reason=reason,
                snapshot_id=snapshot_id,
                receipt_relpath=receipt_relpath,
                source_outcome=source_outcome,
                reason_code=reason_code,
            )
        except SymbolProcessingTimeout:
            raise
        except Exception as exc:
            raise StrategyScanStatusError(
                f"strategy status publication failed: {symbol}/{family}"
            ) from exc

    call_prefiltered = bool(configured_call and call_skip_reason)
    if call_prefiltered and not inputs.fetch_only:
        _publish_status(
            family="covered_call",
            status="not_applicable",
            reason=call_skip_reason,
            candidate_count=0,
        )

    if call_prefiltered and not fetch_want_put and not fetch_want_call:
        if inputs.fetch_only:
            return []
        summary_rows: list[dict[str, Any]] = []
        _append_summary_result(
            summary_rows,
            deps.empty_sell_call_summary_fn(symbol, symbol_cfg=symbol_cfg),
        )
        return summary_rows

    frozen_required_data = inputs.required_data_snapshot_manifest is not None
    required_data_frame: pd.DataFrame | None = None

    def _capture_required_data_csv_bytes(csv_bytes: bytes) -> None:
        nonlocal required_data_frame
        try:
            required_data_frame = pd.read_csv(io.BytesIO(csv_bytes), converters={field: str for field in ("multiplier", "chain_multiplier", "snapshot_multiplier")})
        except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as exc:
            raise FrozenRequiredDataUnavailable(symbol=symbol, reason="required_data_invalid") from exc

    if not frozen_required_data:
        try:
            deps.apply_multiplier_cache_fn(
                base=inputs.base,
                required_data_dir=inputs.required_data_dir,
                symbol=symbol,
            )
        except SymbolProcessingTimeout:
            raise
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
            combo_yield_cfg=combo_yield_cfg,
            symbol_cfg=symbol_cfg,
            fetch_host=str(fetch_cfg.get("host") or "127.0.0.1"),
            fetch_port=int(fetch_cfg.get("port") or 11111),
            fetch_source=str(fetch_cfg.get("source") or "futu"),
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

    check_deadline()
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
            "source_producer_run_id": (
                inputs.source_producer_run_id
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
                    "required_data_csv_bytes_sink_fn": (
                        _capture_required_data_csv_bytes
                    ),
                }
            )
        if inputs.required_data_snapshot_batch is not None:
            quote_evidence, csv_bytes = inputs.required_data_snapshot_batch.resolve(
                symbol
            )
            _capture_required_data_csv_bytes(csv_bytes)
        else:
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
            ("combo_yield", want_combo_yield),
            ("covered_call", want_call),
        ):
            if enabled:
                append_strategy_scan_failure(
                    report_dir=inputs.report_dir,
                    symbol=symbol,
                    strategy_family=family,
                    error=exc,
                )
        if want_put:
            _unavailable_summary(
                deps.empty_sell_put_summary_fn(symbol, symbol_cfg=symbol_cfg)
            )
        if want_combo_yield:
            _unavailable_summary(
                deps.empty_combo_yield_summary_fn(symbol, symbol_cfg=symbol_cfg)
            )
        if call_prefiltered:
            _append_summary_result(
                summary_rows,
                deps.empty_sell_call_summary_fn(symbol, symbol_cfg=symbol_cfg),
            )
        if want_call:
            _unavailable_summary(
                deps.empty_sell_call_summary_fn(symbol, symbol_cfg=symbol_cfg)
            )
        for family, enabled in (
            ("sell_put", want_put),
            ("combo_yield", want_combo_yield),
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
    quote_source_outcome = str(
        (
            quote_evidence.get("source_outcome")
            if isinstance(quote_evidence, dict)
            else ""
        )
        or ""
    ).strip()
    quote_reason_code = str(
        (
            quote_evidence.get("reason_code")
            if isinstance(quote_evidence, dict)
            else ""
        )
        or ""
    ).strip()

    def _completed_empty_evidence(
        candidate_count: int,
    ) -> dict[str, str | None]:
        if (
            candidate_count == 0
            and quote_source_outcome == "success_empty"
            and quote_reason_code in SUCCESS_EMPTY_REASON_CODES
        ):
            return {
                "source_outcome": quote_source_outcome,
                "reason_code": quote_reason_code or None,
            }
        return {"source_outcome": None, "reason_code": None}

    summary_rows: list[dict[str, Any]] = []
    frozen_frame_kwargs = (
        {"required_data_frame": required_data_frame}
        if required_data_frame is not None else {}
    )

    def run_step(family: str, runner: Callable[..., object], empty: Callable[..., object], **kwargs: Any) -> None:
        check_deadline()
        finals: list[tuple[str, list[dict[str, Any]]]] = []
        decisions: list[tuple[str, list[dict[str, Any]]]] = []
        combos: list[dict[str, Any]] = []
        if family == "combo_yield":
            kwargs["combo_evidence_sink_fn"] = lambda row: combos.append(dict(row))
        else:
            kwargs["final_candidates_sink_fn"] = lambda mode, rows: finals.append((mode, list(rows)))
            kwargs["candidate_decisions_sink_fn"] = lambda mode, rows: decisions.append((mode, list(rows)))
        try:
            result = runner(**kwargs)
            check_deadline()
            count = _summary_candidate_count(result)
            rows = result if isinstance(result, list) else [result]
            terminals = {(row.get("_strategy_status"), row.get("_strategy_reason")) for row in rows}
            if family == "combo_yield" and combo_variant == "cc_lp":
                state = result.get("status")
                status = {"candidates_found": "completed", "no_candidate": "completed",
                          "not_applicable": "not_applicable", "unavailable": "unavailable"}.get(state)
                reason = result.get("reason") or ("no_candidate" if state == "no_candidate" else None)
            elif len(terminals) == 1:
                status, reason = next(iter(terminals))
            else:
                raise ValueError("strategy result has conflicting terminal evidence")
            if status not in {"completed", "unavailable", "not_applicable"}:
                raise ValueError("strategy result has no valid terminal evidence")
            if status != "completed" and (not reason or count):
                raise ValueError("unavailable strategy result has invalid evidence/count")
            if family != "combo_yield":
                mode = "put" if family == "sell_put" else "call"
                if any(key != mode for key, _ in finals + decisions):
                    raise ValueError("candidate sink escapes strategy scope")
                if count != sum(len(items) for _, items in finals):
                    raise ValueError("strategy candidate count does not match captured rows")
            elif (status == "completed" and len(combos) != 1) or len(combos) > 1 or count != sum(len(item.get("ranked_pairs") or []) for item in combos):
                raise ValueError("combo terminal evidence is missing or inconsistent")
        except (SymbolProcessingTimeout, StrategyScanStatusError):
            raise
        except Exception as exc:
            log.exception("symbol_monitoring: %s step failed for %s", family, symbol)
            append_strategy_scan_failure(report_dir=inputs.report_dir, symbol=symbol, strategy_family=family, error=exc)
            _publish_status(family=family, status="failed", reason=f"{family}_scan_failed",
                            snapshot_id=resolved_quote_snapshot_id, receipt_relpath=quote_receipt_relpath)
            _append_summary_result(summary_rows, empty(symbol, symbol_cfg=symbol_cfg))
            return

        # Publication/commit errors must escape; an immutable completed status cannot become failed.
        _publish_status(family=family, status=status, reason=reason, candidate_count=count,
                        snapshot_id=resolved_quote_snapshot_id, receipt_relpath=quote_receipt_relpath,
                        **(_completed_empty_evidence(count) if status == "completed" else {}))
        try:
            for sink, buffered in ((inputs.final_candidates_sink_fn, finals),
                                   (inputs.candidate_decisions_sink_fn, decisions)):
                if sink is not None:
                    for mode, items in buffered:
                        sink(mode, items)
            if inputs.combo_evidence_sink_fn is not None:
                for evidence in combos:
                    inputs.combo_evidence_sink_fn(evidence)
        except Exception as exc:
            raise StrategyScanStatusError(f"candidate commit failed: {symbol}/{family}") from exc
        _append_summary_result(summary_rows, result)

    if want_put:
        run_step("sell_put", deps.run_sell_put_scan_fn, deps.empty_sell_put_summary_fn,
                symbol=symbol,
                symbol_cfg=symbol_cfg,
                sp=sp,
                required_data_dir=inputs.required_data_dir,
                exchange_rate_converter=exchange_rate_converter,
                portfolio_ctx=inputs.portfolio_ctx,
                global_sell_put_liquidity=(symbol_cfg.get("_global_sell_put_liquidity") or {}),
                final_candidates_sink_fn=inputs.final_candidates_sink_fn,
                candidate_decisions_sink_fn=inputs.candidate_decisions_sink_fn,
                demo_capacity=inputs.experience,
                **frozen_frame_kwargs,
        )
    else:
        _append_summary_result(summary_rows, deps.empty_sell_put_summary_fn(symbol, symbol_cfg=symbol_cfg))
    if want_combo_yield:
        run_step("combo_yield", deps.run_combo_yield_scan_fn, deps.empty_combo_yield_summary_fn,
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
                stock=stock,
                global_sell_put_liquidity=(symbol_cfg.get("_global_sell_put_liquidity") or {}),
                combo_evidence_sink_fn=inputs.combo_evidence_sink_fn,
                demo_capacity=inputs.experience,
                **frozen_frame_kwargs,
        )
    if want_call:
        option_ctx = (inputs.portfolio_ctx or {}).get("option_ctx") or {}
        run_step("covered_call", deps.run_sell_call_scan_fn, deps.empty_sell_call_summary_fn,
                symbol=symbol,
                symbol_cfg=symbol_cfg,
                cc=cc,
                required_data_dir=inputs.required_data_dir,
                stock=stock,
                portfolio_ctx=inputs.portfolio_ctx,
                exchange_rate_converter=exchange_rate_converter,
                locked_shares_status=option_ctx.get("locked_shares_status"),
                locked_shares_unavailable_reason=option_ctx.get("locked_shares_unavailable_reason"),
                locked_shares_by_symbol=option_ctx.get("locked_shares_by_symbol"),
                locked_shares_unavailable_by_symbol=option_ctx.get("locked_shares_unavailable_by_symbol"),
                global_sell_call_liquidity=(symbol_cfg.get("_global_sell_call_liquidity") or {}),
                final_candidates_sink_fn=inputs.final_candidates_sink_fn,
                candidate_decisions_sink_fn=inputs.candidate_decisions_sink_fn,
                demo_capacity=inputs.experience,
                **frozen_frame_kwargs,
        )
    else:
        _append_summary_result(summary_rows, deps.empty_sell_call_summary_fn(symbol, symbol_cfg=symbol_cfg))

    return summary_rows
