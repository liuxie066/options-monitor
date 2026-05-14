from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import time
from typing import Any, Callable

from domain.domain.fetch_source import is_futu_fetch_source, resolve_symbol_fetch_source
from domain.domain.symbol_identity import symbol_market
from domain.domain.tool_boundary import normalize_tool_execution_payload
from src.infrastructure.opend_retcodes import classify_opend_error


DispatchFn = Callable[[dict[str, Any]], dict[str, Any]]
CleanupFn = Callable[[], None]


def _symbol_class(symbol: str) -> str:
    return symbol_market(symbol) or "US"


def _fetch_cfg(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
    return (symbol_cfg.get("fetch") or {}) if isinstance(symbol_cfg, dict) else {}


def _limit_exp(symbol_cfg: dict[str, Any]) -> int:
    fetch_cfg = _fetch_cfg(symbol_cfg)
    return int(fetch_cfg.get("limit_expirations") or 8)


def _is_prefetch_error(payload: dict[str, Any]) -> bool:
    if not bool(payload.get("ok")):
        return True
    status = str(payload.get("status") or "").strip().lower()
    return status in {"error", "fail", "failed"}


def _is_opend_rate_limit_payload(payload: dict[str, Any]) -> bool:
    return bool(_extract_opend_rate_limit_items(payload))


def _iter_nested_opend_error_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    candidates: list[dict[str, Any]] = []

    def add_meta(raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else raw
        if isinstance(meta, dict):
            candidates.append(meta)
            for key in ("errors", "snapshot_errors", "spot_errors"):
                items = meta.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict):
                        candidates.append(item)
                    elif str(item or "").strip():
                        candidates.append({"message": str(item)})

    add_meta(payload)
    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict):
        add_meta(nested_payload)
    return candidates


def _extract_opend_rate_limit_items(payload: dict[str, Any], *, symbol: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    payload_symbol = str(symbol or payload.get("symbol") or "").strip()
    payload_market = _symbol_class(payload_symbol) if payload_symbol else ""
    nested_candidates = _iter_nested_opend_error_payloads(payload)
    has_nested_detail = any(_is_rate_limit_detail_candidate(candidate) for candidate in nested_candidates)
    candidates = [payload, *nested_candidates]
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not classify_opend_error(candidate).is_rate_limit:
            continue
        if candidate is payload and has_nested_detail:
            continue
        if _has_nested_rate_limit_items(candidate):
            continue
        expiration = str(candidate.get("expiration") or "").strip()
        endpoint = _rate_limit_endpoint(candidate)
        message = str(candidate.get("message") or candidate.get("error") or payload.get("message") or "").strip()
        key = (payload_symbol, payload_market, expiration, endpoint, message)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "symbol": payload_symbol,
                "market": payload_market,
                "expiration": expiration,
                "endpoint": endpoint,
                "error_code": str(candidate.get("error_code") or "RATE_LIMIT"),
                "message": message,
            }
        )
    return out


def _is_rate_limit_detail_candidate(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not classify_opend_error(payload).is_rate_limit:
        return False
    if str(payload.get("expiration") or "").strip():
        return True
    stage = str(payload.get("stage") or payload.get("endpoint") or "").strip()
    return bool(stage)


def _has_nested_rate_limit_items(payload: dict[str, Any]) -> bool:
    for key in ("errors", "snapshot_errors", "spot_errors"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and classify_opend_error(item).is_rate_limit:
                return True
            if not isinstance(item, dict) and classify_opend_error({"message": str(item or "")}).is_rate_limit:
                return True
    return False


def _rate_limit_endpoint(payload: dict[str, Any]) -> str:
    raw_stage = str(payload.get("stage") or payload.get("endpoint") or "").strip().lower()
    if raw_stage:
        if raw_stage in {"market_snapshot", "snapshot"}:
            return "market_snapshot"
        if raw_stage in {"spot_snapshot", "spot"}:
            return "spot_snapshot"
        if raw_stage in {"option_expiration", "expiration"}:
            return "option_expiration"
        if raw_stage in {"option_chain", "chain"}:
            return "option_chain"
        return raw_stage
    if str(payload.get("expiration") or "").strip():
        return "option_chain"
    return "opend"


def _annotate_prefetch_payload(
    payload: dict[str, Any],
    *,
    execution_mode: str,
    duration_sec: float | None,
) -> dict[str, Any]:
    payload["execution_mode"] = execution_mode
    if duration_sec is not None:
        payload["duration_sec"] = float(duration_sec)
    return payload


def _safe_duration_sec(started_at: float | None) -> float | None:
    if started_at is None:
        return None
    try:
        return max(0.0, float(time.monotonic() - started_at))
    except Exception:
        return None


@dataclass
class PrefetchCoordinatorResult:
    fetched_ok: int = 0
    errors: int = 0
    skipped: int = 0
    submitted_count: int = 0
    completed_count: int = 0
    fail_budget_consecutive: int = 0
    fail_budget_total: int = 0
    budget_triggered: bool = False
    opend_rate_limit_classes: set[str] = field(default_factory=set)
    opend_rate_limit_items: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    audit_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def symbol_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in self.audit_items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "")
            if not symbol.strip() or symbol == "*":
                continue
            duration_sec = item.get("duration_sec")
            row: dict[str, Any] = {
                "symbol": symbol,
                "status": str(item.get("status") or ""),
                "execution_mode": str(item.get("execution_mode") or ""),
            }
            if duration_sec is not None:
                row["duration_sec"] = float(duration_sec)
            items.append(row)
        return items


class PrefetchCoordinator:
    def __init__(
        self,
        *,
        symbol_cfgs: list[dict[str, Any]],
        max_workers: int,
        execution_mode: str,
        fail_budget_consecutive: int,
        fail_budget_total: int,
        dispatch_fn: DispatchFn,
        cleanup_worker_fn: CleanupFn | None = None,
        short_circuit_rate_limits: bool = True,
        stop_on_failure_budget: bool = True,
    ) -> None:
        self._symbol_cfgs = list(symbol_cfgs)
        self._max_workers = max(1, int(max_workers))
        self._execution_mode = str(execution_mode)
        self._fail_budget_consecutive = max(1, int(fail_budget_consecutive))
        self._fail_budget_total = max(1, int(fail_budget_total))
        self._dispatch_fn = dispatch_fn
        self._cleanup_worker_fn = cleanup_worker_fn
        self._short_circuit_rate_limits = bool(short_circuit_rate_limits)
        self._stop_on_failure_budget = bool(stop_on_failure_budget)

    def _make_short_circuit_payload(self, symbol_cfg: dict[str, Any], sym_class: str) -> dict[str, Any]:
        symbol = str((symbol_cfg or {}).get("symbol") or "").strip()
        fetch_cfg = _fetch_cfg(symbol_cfg)
        source, _decision = resolve_symbol_fetch_source(fetch_cfg)
        return normalize_tool_execution_payload(
            tool_name="required_data_prefetch",
            symbol=symbol,
            source=source,
            limit_exp=_limit_exp(symbol_cfg),
            status="skipped",
            ok=True,
            message=f"opend_rate_limit_short_circuit class={sym_class}",
            returncode=None,
        )

    def _make_budget_skip_payload(self, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        symbol = str((symbol_cfg or {}).get("symbol") or "").strip()
        fetch_cfg = _fetch_cfg(symbol_cfg)
        return normalize_tool_execution_payload(
            tool_name="required_data_prefetch",
            symbol=symbol,
            source=str(fetch_cfg.get("source") or "unknown"),
            limit_exp=_limit_exp(symbol_cfg),
            status="skipped",
            ok=True,
            message="prefetch_stopped_by_failure_budget",
            returncode=None,
        )

    def _make_budget_summary(self, fail_consecutive: int, fail_total: int) -> dict[str, Any]:
        return normalize_tool_execution_payload(
            tool_name="required_data_prefetch",
            symbol="*",
            source="budget",
            limit_exp=0,
            status="error",
            ok=False,
            message=(
                f"prefetch_failure_budget_exceeded consecutive={fail_consecutive}/{self._fail_budget_consecutive} "
                f"total={fail_total}/{self._fail_budget_total}; stopped_early"
            ),
            returncode=None,
        )

    def _dispatch_timed(self, symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        try:
            started_at: float | None = time.monotonic()
        except Exception:
            started_at = None
        payload = self._dispatch_fn(symbol_cfg)
        if isinstance(payload, dict):
            return _annotate_prefetch_payload(
                payload,
                execution_mode=self._execution_mode,
                duration_sec=_safe_duration_sec(started_at),
            )
        return payload

    def run(self) -> PrefetchCoordinatorResult:
        result = PrefetchCoordinatorResult(
            fail_budget_consecutive=self._fail_budget_consecutive,
            fail_budget_total=self._fail_budget_total,
        )
        q = list(self._symbol_cfgs)
        q_idx = 0
        fail_consecutive = 0
        fail_total = 0
        budget_summary_emitted = False

        def budget_exceeded() -> bool:
            return (fail_consecutive >= self._fail_budget_consecutive) or (fail_total >= self._fail_budget_total)

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures: dict[Any, dict[str, Any]] = {}

            def submit_next() -> bool:
                nonlocal q_idx
                while q_idx < len(q):
                    symbol_cfg = q[q_idx]
                    q_idx += 1
                    symbol = str((symbol_cfg or {}).get("symbol") or "").strip()
                    fetch_cfg = _fetch_cfg(symbol_cfg)
                    source, _decision = resolve_symbol_fetch_source(fetch_cfg)
                    sym_class = _symbol_class(symbol)
                    if (
                        self._short_circuit_rate_limits
                        and is_futu_fetch_source(source)
                        and sym_class in result.opend_rate_limit_classes
                    ):
                        payload = self._make_short_circuit_payload(symbol_cfg, sym_class)
                        result.audit_items.append(
                            _annotate_prefetch_payload(payload, execution_mode=self._execution_mode, duration_sec=0.0)
                        )
                        if symbol:
                            result.results[symbol] = str(payload.get("message") or "")
                        result.skipped += 1
                        continue
                    future = executor.submit(self._dispatch_timed, symbol_cfg)
                    futures[future] = symbol_cfg
                    result.submitted_count += 1
                    return True
                return False

            while len(futures) < self._max_workers and submit_next():
                pass

            while futures:
                future = next(as_completed(futures))
                symbol_cfg = futures.pop(future)
                payload = future.result()
                result.audit_items.append(payload)
                result.completed_count += 1

                symbol = str(payload.get("symbol") or "").strip()
                message = str(payload.get("message") or "")
                if symbol:
                    result.results[symbol] = message
                rate_limit_items = (
                    _extract_opend_rate_limit_items(payload, symbol=symbol)
                    if is_futu_fetch_source(payload.get("source"))
                    else []
                )
                if rate_limit_items:
                    result.opend_rate_limit_classes.add(_symbol_class(symbol))
                    result.opend_rate_limit_items.extend(rate_limit_items)

                status = str(payload.get("status") or "").strip().lower()
                if status == "skipped":
                    result.skipped += 1
                elif _is_prefetch_error(payload):
                    result.errors += 1
                    fail_consecutive += 1
                    fail_total += 1
                    if self._stop_on_failure_budget and (not result.budget_triggered) and budget_exceeded():
                        result.budget_triggered = True
                        if not budget_summary_emitted:
                            budget_summary_emitted = True
                            result.audit_items.append(self._make_budget_summary(fail_consecutive, fail_total))
                else:
                    result.fetched_ok += 1
                    fail_consecutive = 0

                if result.budget_triggered:
                    for pending in list(futures.keys()):
                        if pending.cancel():
                            cfg0 = futures.pop(pending)
                            payload0 = self._make_budget_skip_payload(cfg0)
                            result.audit_items.append(
                                _annotate_prefetch_payload(payload0, execution_mode=self._execution_mode, duration_sec=0.0)
                            )
                            symbol0 = str((cfg0 or {}).get("symbol") or "").strip()
                            if symbol0:
                                result.results[symbol0] = str(payload0.get("message") or "")
                            result.skipped += 1
                    while q_idx < len(q):
                        cfg1 = q[q_idx]
                        q_idx += 1
                        payload1 = self._make_budget_skip_payload(cfg1)
                        result.audit_items.append(
                            _annotate_prefetch_payload(payload1, execution_mode=self._execution_mode, duration_sec=0.0)
                        )
                        symbol1 = str((cfg1 or {}).get("symbol") or "").strip()
                        if symbol1:
                            result.results[symbol1] = str(payload1.get("message") or "")
                        result.skipped += 1
                    continue

                while len(futures) < self._max_workers and submit_next():
                    pass

            cleanup_worker_fn = self._cleanup_worker_fn
            if cleanup_worker_fn is not None:
                list(executor.map(lambda _idx: cleanup_worker_fn(), range(self._max_workers)))

        return result
