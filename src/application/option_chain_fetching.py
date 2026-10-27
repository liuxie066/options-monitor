from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import importlib
from pathlib import Path
from typing import Any, Callable, Literal
import json
import threading
import time

import pandas as pd

from .expiration_normalization import normalize_expiration_ymd
from src.application.opend_normalize import normalize_opend_option_type
from src.infrastructure.io_utils import atomic_write_json
from src.infrastructure.futu_gateway import check_fetch_control


FreshnessPolicy = Literal["cache_first", "refresh_missing", "force_refresh"]
OptionChainSourceOutcome = Literal[
    "success_rows",
    "success_empty",
    "provider_error",
    "parse_error",
    "not_attempted",
]

DEFAULT_OPTION_CHAIN_WINDOW_SEC = 30.0
DEFAULT_OPTION_CHAIN_MAX_CALLS = 10
DEFAULT_OPTION_CHAIN_MAX_WAIT_SEC = 90.0
DEFAULT_STALE_OPTION_CHAIN_CACHE_MAX_AGE_DAYS = 7

_RATE_GATE_CACHE_LOCK = threading.Lock()
_RATE_GATE_CACHE: dict[tuple[str, int, float, float], Any] = {}


class OptionChainRateLimitExceeded(RuntimeError):
    pass


class FetchRangeExceeded(ValueError):
    """The request's unique chain or quote work exceeds its fixed limit."""


@dataclass
class QueryFetchCache:
    """Facts and unique work for ONE serial request; never share across requests."""

    underliers: dict[tuple, Any] = field(default_factory=dict)
    expirations: dict[tuple, list[str]] = field(default_factory=dict)
    chains: dict[tuple, tuple[list[dict[str, Any]], str]] = field(default_factory=dict)
    snapshots: dict[tuple, dict[str, Any]] = field(default_factory=dict)
    history: dict[tuple, dict[str, Any]] = field(default_factory=dict)
    chain_units: set[tuple] = field(default_factory=set)
    quote_units: set[tuple] = field(default_factory=set)
    reuse_hits: int = 0
    diagnostics: dict[str, Any] = field(default_factory=lambda: {"phase_ms": {}, "opend_calls": {}, "retry_count": 0, "reuse_hits": 0})

    def record_phase(self, name: str, started: float) -> None:
        phases = self.diagnostics["phase_ms"]
        phases[name] = phases.get(name, 0.0) + (time.monotonic() - started) * 1000

    def record_call(self, endpoint: str) -> None:
        calls = self.diagnostics["opend_calls"]
        calls[endpoint] = calls.get(endpoint, 0) + 1

    def reserve_chain(self, key: tuple) -> None:
        if key not in self.chain_units and len(self.chain_units) >= 50:
            raise FetchRangeExceeded("query_range_exceeded: more than 50 unique chain work units")
        self.chain_units.add(key)

    def reserve_quotes(self, identity: tuple, codes: list[str]) -> None:
        units = {(*identity, code) for code in codes}
        if len(self.quote_units | units) > 1000:
            raise FetchRangeExceeded("query_range_exceeded: more than 1000 unique quote contracts")
        self.quote_units.update(units)


@dataclass(frozen=True)
class OptionChainFetchRequest:
    symbol: str
    underlier_code: str
    expirations: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 11111
    option_types: str = "put,call"
    strike_windows: dict[str, dict[str, float | None]] | None = None
    base_dir: Path | None = None
    asof_date: str | None = None
    freshness_policy: FreshnessPolicy = "cache_first"
    chain_cache: bool = True
    max_wait_sec: float = DEFAULT_OPTION_CHAIN_MAX_WAIT_SEC
    window_sec: float = DEFAULT_OPTION_CHAIN_WINDOW_SEC
    max_calls: int = DEFAULT_OPTION_CHAIN_MAX_CALLS
    is_force_refresh: bool = False
    no_retry: bool = False
    retry_max_attempts: int = 4
    retry_time_budget_sec: float = 8.0
    retry_base_delay_sec: float = 0.8
    retry_max_delay_sec: float = 6.0
    deadline: float | None = None
    cancelled: Callable[[], bool] | None = None
    query_cache: QueryFetchCache | None = None


@dataclass(frozen=True)
class OptionChainFetchResult:
    rows: list[dict[str, Any]]
    from_cache_expirations: list[str]
    fetched_expirations: list[str]
    opend_call_count: int
    rate_gate_wait_sec: float
    status: str
    error_code: str | None
    errors: list[dict[str, Any]]
    expiration_statuses: dict[str, str]
    source_outcome: OptionChainSourceOutcome
    reason_code: str | None = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    stale_cache_expirations: list[str] = field(default_factory=list)
    stale_cache_asof_dates: dict[str, str] = field(default_factory=dict)
    frame: pd.DataFrame | None = None
    source_observed_at: str | None = None

    def to_meta(self) -> dict[str, Any]:
        return {
            "source_observed_at": self.source_observed_at,
            "status": self.status,
            "error_code": self.error_code,
            "from_cache_expirations": list(self.from_cache_expirations),
            "fetched_expirations": list(self.fetched_expirations),
            "opend_call_count": int(self.opend_call_count),
            "rate_gate_wait_sec": float(self.rate_gate_wait_sec),
            "expiration_statuses": dict(self.expiration_statuses),
            "errors": list(self.errors),
            "source_outcome": self.source_outcome,
            "reason_code": self.reason_code,
            "diagnostics": list(self.diagnostics),
            "stale_cache_expirations": list(self.stale_cache_expirations),
            "stale_cache_asof_dates": dict(self.stale_cache_asof_dates),
        }


class FileRateLimiter:
    def __init__(
        self,
        *,
        state_path: Path,
        max_calls: int = DEFAULT_OPTION_CHAIN_MAX_CALLS,
        window_sec: float = DEFAULT_OPTION_CHAIN_WINDOW_SEC,
        max_wait_sec: float = DEFAULT_OPTION_CHAIN_MAX_WAIT_SEC,
        label: str = "option_chain",
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self.max_calls = max(1, int(max_calls))
        self.window_sec = max(0.001, float(window_sec))
        self.max_wait_sec = max(0.0, float(max_wait_sec))
        self.label = str(label or "opend")
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self._gate = _get_or_create_gate(
            state_path=self.state_path,
            max_calls=self.max_calls,
            window_sec=self.window_sec,
            max_wait_sec=self.max_wait_sec,
            label=self.label,
            clock=self.clock,
            sleep=self.sleep,
        )

    def acquire(self, *, deadline: float | None = None, cancelled: Callable[[], bool] | None = None) -> float:
        check_fetch_control(deadline, cancelled)
        try:
            controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
            result = self._gate.acquire(**controls)
        except TimeoutError as exc:
            check_fetch_control(deadline, cancelled)
            raise OptionChainRateLimitExceeded(str(exc)) from exc
        check_fetch_control(deadline, cancelled)
        return result

    def record_rate_limit(self, *, cooldown_sec: float | None = None, deadline: float | None = None, cancelled: Callable[[], bool] | None = None) -> None:
        check_fetch_control(deadline, cancelled)
        recorder = getattr(self._gate, "record_rate_limit", None)
        if callable(recorder):
            controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
            recorder(cooldown_sec=cooldown_sec, **controls)


def _get_or_create_gate(
    *,
    state_path: Path,
    max_calls: int,
    window_sec: float,
    max_wait_sec: float,
    label: str,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    gate_cls = _load_opend_rate_gate_class()
    path = Path(state_path)
    key = (str(path), int(max_calls), float(window_sec), float(max_wait_sec))
    with _RATE_GATE_CACHE_LOCK:
        gate = _RATE_GATE_CACHE.get(key)
        if gate is None:
            gate = gate_cls(
                state_path=path,
                max_calls=max_calls,
                window_sec=window_sec,
                max_wait_sec=max_wait_sec,
                label=label,
                clock=clock,
                sleep=sleep,
            )
            _RATE_GATE_CACHE[key] = gate
        return gate


def option_chain_limiter_state_path(base_dir: Path) -> Path:
    return Path(base_dir) / "output_shared" / "state" / "opend_option_chain_limiter.json"


def option_chain_cache_root(base_dir: Path) -> Path:
    return Path(base_dir) / "cache" / "opend_option_chain"


def option_chain_shard_cache_path(
    base_dir: Path,
    underlier_code: str,
    expiration: str | None,
    *,
    option_type_scope: str | None = None,
) -> Path:
    safe_underlier = str(underlier_code or "").replace(".", "_")
    safe_exp = _cache_expiration_key(expiration)
    scope = _option_type_cache_scope(option_type_scope)
    if scope:
        safe_exp = f"{safe_exp}.{scope}"
    return option_chain_cache_root(base_dir) / safe_underlier / f"{safe_exp}.json"


def option_chain_diagnostic_path(
    base_dir: Path,
    underlier_code: str,
    expiration: str | None,
    *,
    option_type_scope: str | None = None,
) -> Path:
    safe_underlier = str(underlier_code or "").replace(".", "_")
    safe_exp = _cache_expiration_key(expiration)
    scope = _option_type_cache_scope(option_type_scope)
    if scope:
        safe_exp = f"{safe_exp}.{scope}"
    return option_chain_cache_root(base_dir) / safe_underlier / f"{safe_exp}.error.json"


def load_option_chain_shard(path: Path, *, asof_date: str) -> list[dict[str, Any]] | None:
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if str(obj.get("asof_date") or "") != str(asof_date):
        return None
    if str(obj.get("status") or "").lower() != "ok":
        return None
    if obj.get("error") or obj.get("error_code"):
        return None
    rows = obj.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    return [dict(row) for row in rows if isinstance(row, dict)]


def _parse_cache_asof_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if len(raw) >= 10:
        raw = raw[:10]
    try:
        return date.fromisoformat(raw)
    except Exception:
        return None


def _is_stale_cache_age_allowed(stale_asof: str, *, asof_date: str, max_age_days: int) -> bool:
    stale_date = _parse_cache_asof_date(stale_asof)
    current_date = _parse_cache_asof_date(asof_date)
    if stale_date is None or current_date is None:
        return False
    age_days = (current_date - stale_date).days
    return 0 < age_days <= max(0, int(max_age_days))


def load_stale_option_chain_shard(
    path: Path,
    *,
    asof_date: str,
    max_age_days: int = DEFAULT_STALE_OPTION_CHAIN_CACHE_MAX_AGE_DAYS,
) -> tuple[list[dict[str, Any]], str] | None:
    if not str(asof_date or "").strip():
        return None
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    stale_asof = str(obj.get("asof_date") or "").strip()
    if not stale_asof or not _is_stale_cache_age_allowed(
        stale_asof,
        asof_date=asof_date,
        max_age_days=max_age_days,
    ):
        return None
    if str(obj.get("status") or "").lower() != "ok":
        return None
    if obj.get("error") or obj.get("error_code"):
        return None
    rows = obj.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    clean_rows = [dict(row) for row in rows if isinstance(row, dict)]
    return (clean_rows, stale_asof) if clean_rows else None


def save_option_chain_shard(
    path: Path,
    *,
    asof_date: str,
    underlier_code: str,
    expiration: str | None,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    atomic_write_json(
        Path(path),
        {
            "asof_date": asof_date,
            "underlier_code": underlier_code,
            "expiration": expiration,
            "status": "ok",
            "rows": rows,
        },
        default=str,
    )


def save_option_chain_diagnostic(
    path: Path,
    *,
    asof_date: str,
    underlier_code: str,
    expiration: str | None,
    status: str,
    error_code: str,
    message: str,
) -> None:
    atomic_write_json(
        Path(path),
        {
            "asof_date": asof_date,
            "underlier_code": underlier_code,
            "expiration": expiration,
            "status": status,
            "error_code": error_code,
            "error": message,
        },
        default=str,
    )


def prune_option_chain_cache(base_dir: Path, keep_days: int) -> None:
    if int(keep_days) <= 0:
        return
    root = option_chain_cache_root(base_dir)
    if not root.exists():
        return
    cutoff = time.time() - int(keep_days) * 86400
    for path in root.rglob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except Exception:
            pass


def _validate_query_chain_rows(rows: list[dict[str, Any]], expiration: str | None) -> None:
    """A malformed expiry shard cannot attest an empty query subrange."""
    import math

    for row in rows:
        if not isinstance(row.get("code"), str) or not row["code"].strip():
            raise ValueError("option chain contains an invalid code")
        if normalize_opend_option_type(row.get("option_type")) not in {"put", "call"}:
            raise ValueError("option chain contains an invalid option type")
        actual_expiration = normalize_expiration_ymd(row.get("strike_time"))
        if actual_expiration is None or (expiration and actual_expiration != expiration):
            raise ValueError("option chain contains an invalid expiration")
        strike = float(row.get("strike_price"))
        if not math.isfinite(strike) or strike <= 0:
            raise ValueError("option chain contains an invalid strike")


def fetch_option_chains(
    *,
    gateway: Any,
    request: OptionChainFetchRequest,
    retry_call: Callable[..., Any],
) -> OptionChainFetchResult:
    check_fetch_control(request.deadline, request.cancelled)
    controls = {"deadline": request.deadline, "cancelled": request.cancelled} if request.deadline is not None or request.cancelled is not None else {}
    base_dir = Path(request.base_dir) if request.base_dir is not None else Path.cwd()
    asof_date = str(request.asof_date or "")
    freshness_policy = str(request.freshness_policy or "cache_first")
    force_refresh = request.is_force_refresh or freshness_policy == "force_refresh"
    targets: list[str | None] = list(request.expirations or [])
    explicit_target_scope = bool(targets)
    if not targets:
        targets = [None]

    limiter = FileRateLimiter(
        state_path=option_chain_limiter_state_path(base_dir),
        max_calls=int(request.max_calls),
        window_sec=float(request.window_sec),
        max_wait_sec=float(request.max_wait_sec),
    )

    rows: list[dict[str, Any]] = []
    frame_parts: list[pd.DataFrame] = []
    from_cache: list[str] = []
    fetched: list[str] = []
    errors: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    statuses: dict[str, str] = {}
    stale_cache: list[str] = []
    stale_cache_asof_dates: dict[str, str] = {}
    opend_calls = 0
    rate_gate_wait_sec = 0.0
    option_type_scope = _single_option_type(request.option_types)

    observed_times: list[str] = []
    local_cache = request.query_cache
    identity = (str(base_dir.resolve()), request.host, int(request.port), request.underlier_code, asof_date)
    for exp in targets:
        check_fetch_control(request.deadline, request.cancelled)
        exp_norm = normalize_expiration_ymd(exp) if exp else None
        exp_key = _cache_expiration_key(exp_norm)
        local_key = (*identity, exp_key, option_type_scope or "both")
        full_key = (*identity, exp_key, "both")
        if local_cache is not None:
            hit_key = full_key if full_key in local_cache.chains else local_key
            if hit_key in local_cache.chains:
                local_cache.reserve_chain(hit_key)
                cached_rows, observed_at = local_cache.chains[hit_key]
                rows.extend(dict(row) for row in cached_rows)
                cached_frame = _records_to_frame(cached_rows)
                if cached_frame is not None:
                    frame_parts.append(cached_frame)
                observed_times.append(observed_at)
                from_cache.append(exp_key)
                statuses[exp_key] = "cache" if cached_rows else "empty"
                local_cache.reuse_hits += 1
                continue
        cache_path = option_chain_shard_cache_path(
            base_dir,
            request.underlier_code,
            exp_norm,
            option_type_scope=option_type_scope,
        )
        fallback_cache_path = option_chain_shard_cache_path(base_dir, request.underlier_code, exp_norm)
        if request.chain_cache and (not force_refresh) and asof_date:
            loaded_path = cache_path
            cached_rows = load_option_chain_shard(cache_path, asof_date=asof_date)
            if not cached_rows and option_type_scope:
                loaded_path = fallback_cache_path
                cached_rows = load_option_chain_shard(fallback_cache_path, asof_date=asof_date)
            check_fetch_control(request.deadline, request.cancelled)
            if cached_rows and local_cache is not None:
                try:
                    _validate_query_chain_rows(cached_rows, exp_norm)
                except (TypeError, ValueError):
                    cached_rows = None
            if cached_rows:
                if local_cache is not None:
                    cache_key = full_key if loaded_path == fallback_cache_path else local_key
                    local_cache.reserve_chain(cache_key)
                    observed_at = datetime.fromtimestamp(loaded_path.stat().st_mtime, timezone.utc).isoformat()
                    local_cache.diagnostics["persistent_cache_hits"] = local_cache.diagnostics.get("persistent_cache_hits", 0) + 1
                    local_cache.chains[cache_key] = ([dict(row) for row in cached_rows], observed_at)
                    observed_times.append(observed_at)
                rows.extend(cached_rows)
                cached_frame = _records_to_frame(cached_rows)
                if cached_frame is not None and not cached_frame.empty:
                    frame_parts.append(cached_frame)
                from_cache.append(exp_key)
                statuses[exp_key] = "cache"
                continue

        if local_cache is not None:
            local_cache.reserve_chain(local_key)

        def _call_chain(exp0: str | None = exp_norm) -> Any:
            def _mark_opend_call() -> None:
                nonlocal opend_calls
                opend_calls += 1
                if local_cache is not None:
                    local_cache.record_call("option_chain")

            def _mark_rate_gate_wait(wait_sec: float) -> None:
                nonlocal rate_gate_wait_sec
                rate_gate_wait_sec += max(0.0, float(wait_sec))
                if local_cache is not None:
                    local_cache.diagnostics["rate_gate_wait_sec"] = local_cache.diagnostics.get("rate_gate_wait_sec", 0.0) + max(0.0, float(wait_sec))

            return _fetch_one_chain(
                gateway,
                request,
                limiter,
                exp0,
                on_opend_call=_mark_opend_call,
                on_rate_gate_wait=_mark_rate_gate_wait,
            )

        try:
            chain = retry_call(
                f"get_option_chain({exp_key})",
                _call_chain,
                no_retry=bool(request.no_retry),
                retry_max_attempts=int(request.retry_max_attempts),
                retry_time_budget_sec=float(request.retry_time_budget_sec),
                retry_base_delay_sec=float(request.retry_base_delay_sec),
                retry_max_delay_sec=float(request.retry_max_delay_sec),
                quiet=True,
                **controls,
                **({"diagnostics": local_cache.diagnostics} if local_cache is not None else {}),
            )
            check_fetch_control(request.deadline, request.cancelled)
        except Exception as exc:
            check_fetch_control(request.deadline, request.cancelled)
            code = classify_option_chain_error(exc)
            msg = str(exc)
            errors.append({"expiration": exp_norm, "error_code": code, "message": msg})
            if request.chain_cache and asof_date:
                save_option_chain_diagnostic(
                    option_chain_diagnostic_path(
                        base_dir,
                        request.underlier_code,
                        exp_norm,
                        option_type_scope=option_type_scope,
                    ),
                    asof_date=asof_date,
                    underlier_code=request.underlier_code,
                    expiration=exp_norm,
                    status="error",
                    error_code=code,
                    message=msg,
                )
            stale_loaded: tuple[list[dict[str, Any]], str] | None = None
            if code == "RATE_LIMIT" and request.chain_cache and asof_date and not force_refresh:
                stale_loaded = load_stale_option_chain_shard(cache_path, asof_date=asof_date)
                if stale_loaded is None and fallback_cache_path != cache_path:
                    stale_loaded = load_stale_option_chain_shard(fallback_cache_path, asof_date=asof_date)
            if stale_loaded is not None:
                stale_rows, stale_asof = stale_loaded
                rows.extend(stale_rows)
                stale_frame = _records_to_frame(stale_rows)
                if stale_frame is not None and not stale_frame.empty:
                    frame_parts.append(stale_frame)
                stale_cache.append(exp_key)
                stale_cache_asof_dates[exp_key] = stale_asof
                statuses[exp_key] = "stale_cache"
                continue
            statuses[exp_key] = "error"
            continue

        try:
            chain_frame = _chain_value_to_frame(chain)
            chain_rows = _chain_value_to_records(chain, frame=chain_frame)
            if local_cache is not None:
                _validate_query_chain_rows(chain_rows, exp_norm)
        except (TypeError, ValueError) as exc:
            errors.append(
                {
                    "expiration": exp_norm,
                    "error_code": "PARSE_ERROR",
                    "message": str(exc),
                }
            )
            statuses[exp_key] = "error"
            continue
        check_fetch_control(request.deadline, request.cancelled)
        if local_cache is not None:
            observed_at = datetime.now(timezone.utc).isoformat()
            local_cache.chains[local_key] = ([dict(row) for row in chain_rows], observed_at)
            observed_times.append(observed_at)
        if not chain_rows:
            statuses[exp_key] = "empty"
            empty_diagnostic = {
                "expiration": exp_norm,
                "diagnostic_code": "EMPTY_CHAIN",
                "message": "empty_chain",
            }
            diagnostics.append(empty_diagnostic)
            if not explicit_target_scope:
                errors.append(
                    {
                        "expiration": exp_norm,
                        "error_code": "EMPTY_CHAIN",
                        "message": "empty_chain",
                    }
                )
            if request.chain_cache and asof_date:
                save_option_chain_diagnostic(
                    option_chain_diagnostic_path(
                        base_dir,
                        request.underlier_code,
                        exp_norm,
                        option_type_scope=option_type_scope,
                    ),
                    asof_date=asof_date,
                    underlier_code=request.underlier_code,
                    expiration=exp_norm,
                    status="empty",
                    error_code="EMPTY_CHAIN",
                    message="empty_chain",
                )
            continue

        rows.extend(chain_rows)
        if chain_frame is not None and not chain_frame.empty:
            frame_parts.append(chain_frame)
        fetched.append(exp_key)
        statuses[exp_key] = "fetched"
        if request.chain_cache and asof_date:
            save_option_chain_shard(
                cache_path,
                asof_date=asof_date,
                underlier_code=request.underlier_code,
                expiration=exp_norm,
                rows=chain_rows,
            )

    check_fetch_control(request.deadline, request.cancelled)
    source_outcome, reason_code = _source_outcome(
        rows=rows,
        errors=errors,
        statuses=statuses,
        explicit_target_scope=explicit_target_scope,
    )
    status = (
        "ok"
        if source_outcome in {"success_rows", "success_empty"}
        else _result_status(
            rows=rows,
            errors=errors,
            target_count=len(targets),
        )
    )
    return OptionChainFetchResult(
        rows=rows,
        from_cache_expirations=from_cache,
        fetched_expirations=fetched,
        opend_call_count=opend_calls,
        rate_gate_wait_sec=rate_gate_wait_sec,
        status=status,
        error_code=_primary_error_code(errors),
        errors=errors,
        expiration_statuses=statuses,
        source_outcome=source_outcome,
        reason_code=reason_code,
        diagnostics=diagnostics,
        stale_cache_expirations=stale_cache,
        stale_cache_asof_dates=stale_cache_asof_dates,
        frame=_concat_frames(frame_parts),
        source_observed_at=min(observed_times) if observed_times else None,
    )


def classify_option_chain_error(exc: Any) -> str:
    return _classify_opend_error(exc).value


def _classify_opend_error(exc: Any) -> Any:
    module = importlib.import_module("src.infrastructure.opend_retcodes")
    return module.classify_opend_error(exc)


def _load_opend_rate_gate_class() -> Any:
    module = importlib.import_module("src.application.opend_rate_gate")
    return module.OpenDRateGate


def _fetch_one_chain(
    gateway: Any,
    request: OptionChainFetchRequest,
    limiter: FileRateLimiter,
    expiration: str | None,
    *,
    on_opend_call: Callable[[], None] | None = None,
    on_rate_gate_wait: Callable[[float], None] | None = None,
) -> Any:
    controls = {"deadline": request.deadline, "cancelled": request.cancelled} if request.deadline is not None or request.cancelled is not None else {}
    check_fetch_control(request.deadline, request.cancelled)
    wait_sec = limiter.acquire(**controls)
    if on_rate_gate_wait is not None:
        on_rate_gate_wait(wait_sec)
    if on_opend_call is not None:
        on_opend_call()
    kwargs = {"code": request.underlier_code, "is_force_refresh": bool(request.is_force_refresh)}
    if expiration:
        kwargs["start"] = str(expiration)
        kwargs["end"] = str(expiration)
    option_type = _single_option_type(request.option_types)
    if option_type:
        kwargs["option_type"] = option_type

    def _call_gateway(call_kwargs: dict[str, Any]) -> Any:
        check_fetch_control(request.deadline, request.cancelled)
        try:
            try:
                result = gateway.get_option_chain(**call_kwargs)
            finally:
                check_fetch_control(request.deadline, request.cancelled)
            return result
        except Exception as exc:
            check_fetch_control(request.deadline, request.cancelled)
            if classify_option_chain_error(exc) == "RATE_LIMIT":
                limiter.record_rate_limit(**controls)
            raise

    try:
        return _call_gateway(kwargs)
    except TypeError as exc:
        if "option_type" not in str(exc):
            raise
        kwargs.pop("option_type", None)
        wait_sec = limiter.acquire(**controls)
        if on_rate_gate_wait is not None:
            on_rate_gate_wait(wait_sec)
        if on_opend_call is not None:
            on_opend_call()
        if request.query_cache is not None:
            request.query_cache.reserve_chain((str(Path(request.base_dir or Path.cwd()).resolve()), request.host, int(request.port), request.underlier_code, str(request.asof_date or ""), _cache_expiration_key(expiration), "both"))
        return _call_gateway(kwargs)


def _chain_value_to_frame(value: Any) -> pd.DataFrame | None:
    if value is None:
        raise TypeError("option chain response is None")
    if isinstance(value, pd.DataFrame):
        return value
    if isinstance(value, list):
        if any(not isinstance(row, dict) for row in value):
            raise TypeError("option chain list contains non-object rows")
        return pd.DataFrame(value)
    if hasattr(value, "to_dict"):
        try:
            frame = pd.DataFrame(value.to_dict(orient="records"))
        except Exception as exc:
            raise ValueError("option chain response conversion failed") from exc
        return frame
    raise TypeError(
        f"unsupported option chain response type: {type(value).__name__}"
    )


def _chain_value_to_records(value: Any, *, frame: pd.DataFrame | None = None) -> list[dict[str, Any]]:
    if frame is not None:
        return [dict(row) for row in frame.to_dict(orient="records")]
    if isinstance(value, list):
        if any(not isinstance(row, dict) for row in value):
            raise TypeError("option chain list contains non-object rows")
        return [dict(row) for row in value]
    try:
        return [dict(row) for row in value.to_dict(orient="records")]
    except Exception as exc:
        raise ValueError("option chain response conversion failed") from exc


def _records_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame | None:
    try:
        if not rows:
            return None
        frame = pd.DataFrame([dict(row) for row in rows if isinstance(row, dict)])
        return frame if not frame.empty else None
    except Exception:
        return None


def _concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame | None:
    usable = [frame for frame in frames if frame is not None and not frame.empty]
    if not usable:
        return None
    try:
        if len(usable) == 1:
            return usable[0]
        return pd.concat(usable, ignore_index=True)
    except Exception:
        return None


def _primary_error_code(errors: list[dict[str, Any]]) -> str | None:
    if not errors:
        return None
    codes = [str(item.get("error_code") or "UNKNOWN") for item in errors if isinstance(item, dict)]
    for code in ("RATE_LIMIT", "TRANSIENT", "PARSE_ERROR", "EMPTY_CHAIN"):
        if code in codes:
            return code
    return codes[0] if codes else "UNKNOWN"


def _result_status(*, rows: list[dict[str, Any]], errors: list[dict[str, Any]], target_count: int) -> str:
    if rows and not errors:
        return "ok"
    if rows:
        return "partial"
    return "error" if errors or target_count > 0 else "ok"


def _source_outcome(
    *,
    rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    statuses: dict[str, str],
    explicit_target_scope: bool,
) -> tuple[OptionChainSourceOutcome, str | None]:
    if errors:
        codes = {
            str(item.get("error_code") or "").strip().upper()
            for item in errors
            if isinstance(item, dict)
        }
        if "PARSE_ERROR" in codes:
            return "parse_error", "chain_response_invalid"
        return "provider_error", _primary_error_code(errors)
    if rows:
        return "success_rows", None
    if (
        explicit_target_scope
        and statuses
        and set(statuses.values()) == {"empty"}
    ):
        return "success_empty", "no_contract_rows"
    return "provider_error", "EMPTY_CHAIN"


def _cache_expiration_key(expiration: str | None) -> str:
    exp = normalize_expiration_ymd(expiration) if expiration else None
    return exp or "__all__"


def _single_option_type(option_types: str | None) -> str | None:
    values = {
        normalize_opend_option_type(value)
        for value in str(option_types or "").split(",")
        if str(value or "").strip()
    }
    values.discard("")
    if values == {"put"}:
        return "PUT"
    if values == {"call"}:
        return "CALL"
    return None


def _option_type_cache_scope(option_types: str | None) -> str | None:
    single = _single_option_type(option_types)
    if single == "PUT":
        return "put"
    if single == "CALL":
        return "call"
    return None
