from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.application.opend_symbol_fetching import FetchSymbolRequest, fetch_symbol_request, save_outputs
from src.application.opend_fetch_config import filter_opend_fetch_kwargs
from src.application.expiration_normalization import normalize_expiration_ymd
from src.application.required_data_planning import RequiredDataFetchSpec


@dataclass(frozen=True)
class RequiredDataFetchRequest:
    symbol: str
    limit_expirations: int
    host: str = "127.0.0.1"
    port: int = 11111
    output_root: Path | None = None
    option_types: str = "put,call"
    min_strike: float | None = None
    max_strike: float | None = None
    side_strike_windows: dict[str, dict[str, float | None]] | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    explicit_expirations: list[str] | None = None
    chain_cache: bool = True
    chain_cache_force_refresh: bool = False
    freshness_policy: str = "cache_first"
    max_wait_sec: float = 90.0
    option_chain_window_sec: float = 30.0
    option_chain_max_calls: int = 10
    snapshot_max_wait_sec: float = 30.0
    snapshot_window_sec: float = 30.0
    snapshot_max_calls: int = 60
    expiration_max_wait_sec: float = 30.0
    expiration_window_sec: float = 30.0
    expiration_max_calls: int = 30


def execute_required_data_opend(*, base: Path, request: RequiredDataFetchRequest) -> dict[str, object]:
    explicit_expirations = sorted({
        exp
        for exp in (normalize_expiration_ymd(x) for x in (request.explicit_expirations or []))
        if exp
    }) or None
    return fetch_symbol_request(
        FetchSymbolRequest(
            symbol=request.symbol,
            limit_expirations=int(request.limit_expirations),
            host=str(request.host),
            port=int(request.port),
            base_dir=Path(base),
            chain_cache=bool(request.chain_cache),
            chain_cache_force_refresh=bool(request.chain_cache_force_refresh),
            option_types=str(request.option_types),
            min_strike=request.min_strike,
            max_strike=request.max_strike,
            side_strike_windows=request.side_strike_windows,
            min_dte=request.min_dte,
            max_dte=request.max_dte,
            explicit_expirations=explicit_expirations,
            freshness_policy=str(request.freshness_policy or "cache_first"),
            max_wait_sec=float(request.max_wait_sec),
            option_chain_window_sec=float(request.option_chain_window_sec),
            option_chain_max_calls=int(request.option_chain_max_calls),
            snapshot_max_wait_sec=float(request.snapshot_max_wait_sec),
            snapshot_window_sec=float(request.snapshot_window_sec),
            snapshot_max_calls=int(request.snapshot_max_calls),
            expiration_max_wait_sec=float(request.expiration_max_wait_sec),
            expiration_window_sec=float(request.expiration_window_sec),
            expiration_max_calls=int(request.expiration_max_calls),
        )
    )


def fetch_required_data_opend(*, base: Path, request: RequiredDataFetchRequest) -> tuple[Path, Path]:
    payload = execute_required_data_opend(base=base, request=request)
    return save_outputs(
        Path(base),
        str(request.symbol),
        payload,
        output_root=(Path(request.output_root) if request.output_root is not None else None),
    )


def build_fetch_request_from_spec(
    *,
    spec: RequiredDataFetchSpec,
    output_root: Path | None = None,
    chain_cache: bool = True,
    chain_cache_force_refresh: bool = False,
    opend_fetch_config: dict[str, float | int] | None = None,
) -> RequiredDataFetchRequest:
    kwargs = filter_opend_fetch_kwargs(opend_fetch_config)
    return RequiredDataFetchRequest(
        symbol=spec.symbol,
        limit_expirations=int(spec.limit_expirations),
        host=str(spec.host),
        port=int(spec.port),
        output_root=output_root,
        option_types=",".join(spec.option_types),
        side_strike_windows={k: dict(v) for k, v in spec.side_strike_windows.items()},
        min_dte=(int(spec.min_dte) if spec.min_dte is not None else None),
        max_dte=(int(spec.max_dte) if spec.max_dte is not None else None),
        explicit_expirations=list(spec.explicit_expirations),
        chain_cache=bool(chain_cache),
        chain_cache_force_refresh=bool(chain_cache_force_refresh),
        freshness_policy=("force_refresh" if chain_cache_force_refresh else "cache_first"),
        **kwargs,
    )


def merge_required_data_payloads(*, symbol: str, payloads: list[dict[str, object]]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    meta_items: list[dict[str, object]] = []
    expirations: set[str] = set()
    spot: float | None = None
    underlier_code: str | None = None
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if spot is None:
            try:
                spot = float(payload.get("spot")) if payload.get("spot") is not None else None
            except Exception:
                spot = spot
        if underlier_code is None and payload.get("underlier_code"):
            underlier_code = str(payload.get("underlier_code"))
        for exp in payload.get("expirations") or []:
            if exp:
                expirations.add(str(exp))
        meta = payload.get("meta")
        if isinstance(meta, dict):
            meta_items.append(meta)
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get("contract_symbol") or ""),
                str(row.get("option_type") or ""),
                str(row.get("expiration") or ""),
                str(row.get("strike") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(dict(row))
    return {
        "symbol": symbol,
        "underlier_code": underlier_code,
        "spot": spot,
        "expiration_count": len(expirations),
        "expirations": sorted(expirations),
        "rows": rows,
        "meta": {
            "source": "opend",
            "request_count": len(payloads),
            "requests": meta_items,
        },
    }
