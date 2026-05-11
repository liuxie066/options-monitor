from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.futu_doctor import run_futu_doctor_checks
from src.application.runtime_config_paths import (
    read_json_object_or_empty,
    resolve_data_config_ref,
    resolve_local_path,
    resolve_public_data_config_path,
    write_json_atomic,
)
from src.application.watchlist_mutations import normalize_symbol_read


def normalize_broker(value: Any) -> str:
    return str(value or "富途").strip() or "富途"


def symbol_fetch_config_map(cfg: dict[str, Any], *, resolve_watchlist_config: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in resolve_watchlist_config(cfg):
        symbol = normalize_symbol_read(item.get("symbol"))
        if symbol and isinstance(item, dict):
            out[symbol] = item
    return out


def extract_context_symbols(ctx: dict[str, Any]) -> list[str]:
    rows = ctx.get("open_positions_min") if isinstance(ctx, dict) else []
    out: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol_read(row.get("symbol"))
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def validate_runtime_config(
    cfg: dict[str, Any],
    *,
    allow_empty_symbols: bool = False,
    resolve_watchlist_config: Callable[[dict[str, Any]], list[dict[str, Any]]],
    validate_config: Callable[[dict[str, Any]], Any],
) -> list[str]:
    warnings: list[str] = []
    try:
        mutated = deepcopy(cfg)
        if allow_empty_symbols and not resolve_watchlist_config(mutated):
            mutated["symbols"] = [{"symbol": "DUMMY"}]
        validate_config(mutated)
    except SystemExit as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc
    return warnings


def as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def mask_account_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return f"...{raw[-4:]}"
    return raw


def run_futu_doctor(*, host: str, port: int, symbols: list[str], timeout_sec: int, repo_base: Callable[[], Path]) -> dict[str, Any]:
    try:
        del repo_base
        return run_futu_doctor_checks(
            host=str(host),
            port=int(port),
            symbols=[str(s) for s in symbols],
            timeout_sec=int(timeout_sec),
        )
    except Exception as exc:
        return {"ok": False, "error_code": "DOCTOR_FAILED", "message": f"{type(exc).__name__}: {exc}"}


def healthcheck_symbols_for_futu(cfg: dict[str, Any], *, resolve_watchlist_config: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> list[str]:
    out: list[str] = []
    for item in resolve_watchlist_config(cfg):
        fetch = item.get("fetch") if isinstance(item.get("fetch"), dict) else {}
        source = str(fetch.get("source") or "futu").strip().lower()
        if source not in {"futu", "opend"}:
            continue
        symbol = normalize_symbol_read(item.get("symbol"))
        if not symbol or symbol in out:
            continue
        out.append(symbol)
        if len(out) >= 1:
            break
    return out


__all__ = [
    "as_float",
    "extract_context_symbols",
    "healthcheck_symbols_for_futu",
    "mask_account_id",
    "normalize_broker",
    "read_json_object_or_empty",
    "resolve_data_config_ref",
    "resolve_local_path",
    "resolve_public_data_config_path",
    "run_futu_doctor",
    "symbol_fetch_config_map",
    "validate_runtime_config",
    "write_json_atomic",
]
