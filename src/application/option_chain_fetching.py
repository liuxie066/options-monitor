from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
import json
import os
import time

try:
    import fcntl
except Exception:  # pragma: no cover - non-Unix fallback
    fcntl = None  # type: ignore[assignment]

from src.application.expiration_normalization import normalize_expiration_ymd


FreshnessPolicy = Literal["cache_first", "refresh_missing", "force_refresh"]

DEFAULT_OPTION_CHAIN_WINDOW_SEC = 30.0
DEFAULT_OPTION_CHAIN_MAX_CALLS = 10
DEFAULT_OPTION_CHAIN_MAX_WAIT_SEC = 90.0


class OptionChainRateLimitExceeded(RuntimeError):
    pass


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


@dataclass(frozen=True)
class OptionChainFetchResult:
    rows: list[dict[str, Any]]
    from_cache_expirations: list[str]
    fetched_expirations: list[str]
    opend_call_count: int
    status: str
    error_code: str | None
    errors: list[dict[str, Any]]
    expiration_statuses: dict[str, str]

    def to_meta(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error_code": self.error_code,
            "from_cache_expirations": list(self.from_cache_expirations),
            "fetched_expirations": list(self.fetched_expirations),
            "opend_call_count": int(self.opend_call_count),
            "expiration_statuses": dict(self.expiration_statuses),
            "errors": list(self.errors),
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

    def acquire(self) -> float:
        start = self.clock()
        slept = 0.0
        deadline = start + self.max_wait_sec

        while True:
            wait_s = 0.0
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+", encoding="utf-8") as lock_fp:
                if fcntl is not None:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
                try:
                    now = self.clock()
                    timestamps = self._read_timestamps(now)
                    if len(timestamps) < self.max_calls:
                        timestamps.append(now)
                        self._write_timestamps(timestamps)
                        return slept

                    oldest = min(timestamps)
                    wait_s = max(0.0, self.window_sec - (now - oldest) + 0.05)
                    if now + wait_s > deadline:
                        raise OptionChainRateLimitExceeded(
                            f"{self.label} rate limit wait budget exceeded: "
                            f"max_calls={self.max_calls} window_sec={self.window_sec:g} "
                            f"max_wait_sec={self.max_wait_sec:g}"
                        )
                    self._write_timestamps(timestamps)
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

            if wait_s > 0:
                self.sleep(wait_s)
                slept += wait_s

    def _read_timestamps(self, now: float) -> list[float]:
        try:
            obj = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw = obj.get("timestamps") if isinstance(obj, dict) else []
        except Exception:
            raw = []
        timestamps: list[float] = []
        for item in raw or []:
            try:
                value = float(item)
            except Exception:
                continue
            if now - value < self.window_sec:
                timestamps.append(value)
        return timestamps

    def _write_timestamps(self, timestamps: list[float]) -> None:
        payload = {
            "updated_at": self.clock(),
            "window_sec": self.window_sec,
            "max_calls": self.max_calls,
            "timestamps": timestamps,
        }
        _atomic_write_json(self.state_path, payload)


def option_chain_limiter_state_path(base_dir: Path) -> Path:
    return Path(base_dir) / "output_shared" / "state" / "opend_option_chain_limiter.json"


def option_chain_cache_root(base_dir: Path) -> Path:
    return Path(base_dir) / "cache" / "opend_option_chain"


def option_chain_shard_cache_path(base_dir: Path, underlier_code: str, expiration: str | None) -> Path:
    safe_underlier = str(underlier_code or "").replace(".", "_")
    safe_exp = _cache_expiration_key(expiration)
    return option_chain_cache_root(base_dir) / safe_underlier / f"{safe_exp}.json"


def option_chain_diagnostic_path(base_dir: Path, underlier_code: str, expiration: str | None) -> Path:
    safe_underlier = str(underlier_code or "").replace(".", "_")
    safe_exp = _cache_expiration_key(expiration)
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
    _atomic_write_json(
        Path(path),
        {
            "asof_date": asof_date,
            "underlier_code": underlier_code,
            "expiration": expiration,
            "status": "ok",
            "rows": rows,
        },
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
    _atomic_write_json(
        Path(path),
        {
            "asof_date": asof_date,
            "underlier_code": underlier_code,
            "expiration": expiration,
            "status": status,
            "error_code": error_code,
            "error": message,
        },
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


def fetch_option_chains(
    *,
    gateway: Any,
    request: OptionChainFetchRequest,
    retry_call: Callable[..., Any],
) -> OptionChainFetchResult:
    base_dir = Path(request.base_dir) if request.base_dir is not None else Path.cwd()
    asof_date = str(request.asof_date or "")
    freshness_policy = str(request.freshness_policy or "cache_first")
    force_refresh = request.is_force_refresh or freshness_policy == "force_refresh"
    targets: list[str | None] = list(request.expirations or [])
    if not targets:
        targets = [None]

    limiter = FileRateLimiter(
        state_path=option_chain_limiter_state_path(base_dir),
        max_calls=int(request.max_calls),
        window_sec=float(request.window_sec),
        max_wait_sec=float(request.max_wait_sec),
    )

    rows: list[dict[str, Any]] = []
    from_cache: list[str] = []
    fetched: list[str] = []
    errors: list[dict[str, Any]] = []
    statuses: dict[str, str] = {}
    opend_calls = 0

    for exp in targets:
        exp_norm = normalize_expiration_ymd(exp) if exp else None
        exp_key = _cache_expiration_key(exp_norm)
        cache_path = option_chain_shard_cache_path(base_dir, request.underlier_code, exp_norm)
        if request.chain_cache and (not force_refresh) and asof_date:
            cached_rows = load_option_chain_shard(cache_path, asof_date=asof_date)
            if cached_rows:
                rows.extend(cached_rows)
                from_cache.append(exp_key)
                statuses[exp_key] = "cache"
                continue

        def _call_chain(exp0: str | None = exp_norm) -> Any:
            def _mark_opend_call() -> None:
                nonlocal opend_calls
                opend_calls += 1

            return _fetch_one_chain(gateway, request, limiter, exp0, on_opend_call=_mark_opend_call)

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
            )
        except Exception as exc:
            code = classify_option_chain_error(exc)
            msg = str(exc)
            statuses[exp_key] = "error"
            errors.append({"expiration": exp_norm, "error_code": code, "message": msg})
            if request.chain_cache and asof_date:
                save_option_chain_diagnostic(
                    option_chain_diagnostic_path(base_dir, request.underlier_code, exp_norm),
                    asof_date=asof_date,
                    underlier_code=request.underlier_code,
                    expiration=exp_norm,
                    status="error",
                    error_code=code,
                    message=msg,
                )
            continue

        chain_rows = _dataframe_to_records(chain)
        if not chain_rows:
            statuses[exp_key] = "empty"
            errors.append({"expiration": exp_norm, "error_code": "EMPTY_CHAIN", "message": "empty_chain"})
            if request.chain_cache and asof_date:
                save_option_chain_diagnostic(
                    option_chain_diagnostic_path(base_dir, request.underlier_code, exp_norm),
                    asof_date=asof_date,
                    underlier_code=request.underlier_code,
                    expiration=exp_norm,
                    status="empty",
                    error_code="EMPTY_CHAIN",
                    message="empty_chain",
                )
            continue

        rows.extend(chain_rows)
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

    status = _result_status(rows=rows, errors=errors, target_count=len(targets))
    return OptionChainFetchResult(
        rows=rows,
        from_cache_expirations=from_cache,
        fetched_expirations=fetched,
        opend_call_count=opend_calls,
        status=status,
        error_code=_primary_error_code(errors),
        errors=errors,
        expiration_statuses=statuses,
    )


def classify_option_chain_error(exc: Any) -> str:
    msg = str(exc or "")
    low = msg.lower()
    if "rate limit" in low or "too frequent" in low or "频率太高" in msg or "最多10次" in msg:
        return "RATE_LIMIT"
    if "timeout" in low or "disconnected" in low or "connection reset" in low or "broken pipe" in low:
        return "TRANSIENT"
    if "empty_chain" in low or "empty" in low:
        return "EMPTY_CHAIN"
    return "UNKNOWN"


def _fetch_one_chain(
    gateway: Any,
    request: OptionChainFetchRequest,
    limiter: FileRateLimiter,
    expiration: str | None,
    *,
    on_opend_call: Callable[[], None] | None = None,
) -> Any:
    limiter.acquire()
    if on_opend_call is not None:
        on_opend_call()
    kwargs = {"code": request.underlier_code, "is_force_refresh": bool(request.is_force_refresh)}
    if expiration:
        kwargs["start"] = str(expiration)
        kwargs["end"] = str(expiration)
    return gateway.get_option_chain(**kwargs)


def _dataframe_to_records(value: Any) -> list[dict[str, Any]]:
    try:
        if value is None or value.empty:
            return []
    except Exception:
        if value is None:
            return []
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    try:
        return [dict(row) for row in value.to_dict(orient="records")]
    except Exception:
        return []


def _primary_error_code(errors: list[dict[str, Any]]) -> str | None:
    if not errors:
        return None
    codes = [str(item.get("error_code") or "UNKNOWN") for item in errors if isinstance(item, dict)]
    for code in ("RATE_LIMIT", "TRANSIENT", "EMPTY_CHAIN"):
        if code in codes:
            return code
    return codes[0] if codes else "UNKNOWN"


def _result_status(*, rows: list[dict[str, Any]], errors: list[dict[str, Any]], target_count: int) -> str:
    if rows and not errors:
        return "ok"
    if rows:
        return "partial"
    return "error" if errors or target_count > 0 else "ok"


def _cache_expiration_key(expiration: str | None) -> str:
    exp = normalize_expiration_ymd(expiration) if expiration else None
    return exp or "__all__"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)
