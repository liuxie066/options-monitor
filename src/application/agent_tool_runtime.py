from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
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


def extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            payload = json.loads(raw[idx:])
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def run_futu_doctor(*, host: str, port: int, symbols: list[str], timeout_sec: int, repo_base: Callable[[], Path]) -> dict[str, Any]:
    py = (repo_base() / ".venv" / "bin" / "python").resolve()
    cmd = [str(py if py.exists() else Path(sys.executable)), "scripts/doctor_futu.py", "--host", host, "--port", str(int(port)), "--json"]
    if symbols:
        cmd.extend(["--symbols", *symbols])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_base()),
            capture_output=True,
            text=True,
            timeout=int(timeout_sec),
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error_code": "TIMEOUT", "message": f"doctor_futu timed out after {timeout_sec}s", "raw": str(exc)}
    except Exception as exc:
        return {"ok": False, "error_code": "DOCTOR_FAILED", "message": f"{type(exc).__name__}: {exc}"}

    payload = extract_json_object(proc.stdout or proc.stderr)
    if isinstance(payload, dict):
        watchdog_ok = bool(payload.get("watchdog_ok") or ((payload.get("watchdog") or {}).get("ok") if isinstance(payload.get("watchdog"), dict) else False))
        required_fields_ok = payload.get("required_fields_ok")
        if required_fields_ok is None:
            required_fields_ok = int(payload.get("required_fields_returncode") or 0) == 0
        if watchdog_ok and bool(required_fields_ok):
            payload["ok"] = True
        payload.setdefault("returncode", int(proc.returncode))
        return payload
    raw = (proc.stdout or proc.stderr or "").strip()
    return {
        "ok": False,
        "error_code": "DOCTOR_INVALID_OUTPUT",
        "message": raw or "doctor_futu returned no JSON payload",
        "returncode": int(proc.returncode),
    }


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
    "extract_json_object",
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
