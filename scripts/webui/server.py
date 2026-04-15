from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from scripts.account_config import accounts_from_config


BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_FILES: dict[str, Path] = {
    "us": (BASE_DIR / "config.us.json").resolve(),
    "hk": (BASE_DIR / "config.hk.json").resolve(),
}


@dataclass(frozen=True)
class SymbolRow:
    configKey: Literal["us", "hk"]
    symbol: str
    market: str | None
    accounts: list[str] | None
    limit_expirations: int | None
    sell_put_enabled: bool
    sell_call_enabled: bool
    sell_put_min_dte: int | None
    sell_put_max_dte: int | None
    sell_put_min_strike: float | int | None
    sell_put_max_strike: float | int | None
    sell_call_min_dte: int | None
    sell_call_max_dte: int | None
    sell_call_min_strike: float | int | None
    sell_call_max_strike: float | int | None


app = FastAPI(title="options-monitor webui", version="0.1.0")

static_dir = (Path(__file__).resolve().parent / "static").resolve()
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/__debug/z")
def debug_z() -> dict[str, Any]:
    return {"static_dir": str(static_dir), "ts": int(time.time())}


def _load_config(config_key: str) -> dict:
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail=f"invalid configKey: {config_key}")

    path = CONFIG_FILES[config_key]
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"config not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to parse config: {e}")


def _try_load_config(config_key: str) -> tuple[dict | None, str | None]:
    try:
        return _load_config(config_key), None
    except HTTPException as e:
        return None, str(e.detail)


def _write_config_atomic(path: Path, cfg: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _backup(path: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, bak)
    return bak


def _validate_config(path: Path):
    py = (BASE_DIR / ".venv" / "bin" / "python").resolve()
    if not py.exists():
        raise HTTPException(status_code=500, detail="python venv not found; run ./run_webui.sh once")

    cmd = [str(py), "scripts/validate_config.py", "--config", str(path.name)]
    try:
        r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"validate failed to run: {e}")

    if r.returncode != 0:
        raise HTTPException(status_code=400, detail=(r.stderr.strip() or r.stdout.strip() or "validate failed"))


def _require_token_for_write(req: Request):
    token = (os.environ.get("OM_WEBUI_TOKEN") or "").strip()
    if not token:
        return
    got = (req.headers.get("x-om-token") or "").strip()
    if got != token:
        raise HTTPException(status_code=401, detail="missing/invalid X-OM-Token")


def _to_row(config_key: str, item: dict) -> SymbolRow:
    fetch = item.get("fetch") or {}
    sp = item.get("sell_put") or {}
    sc = item.get("sell_call") or {}

    return SymbolRow(
        configKey=config_key,  # type: ignore
        symbol=str(item.get("symbol") or ""),
        market=item.get("market"),
        accounts=item.get("accounts"),
        limit_expirations=(fetch.get("limit_expirations") if isinstance(fetch, dict) else None),
        sell_put_enabled=bool(sp.get("enabled", False)),
        sell_call_enabled=bool(sc.get("enabled", False)),
        sell_put_min_dte=sp.get("min_dte"),
        sell_put_max_dte=sp.get("max_dte"),
        sell_put_min_strike=sp.get("min_strike"),
        sell_put_max_strike=sp.get("max_strike"),
        sell_call_min_dte=sc.get("min_dte"),
        sell_call_max_dte=sc.get("max_dte"),
        sell_call_min_strike=sc.get("min_strike"),
        sell_call_max_strike=sc.get("max_strike"),
    )


def _list_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for k in ("us", "hk"):
        cfg, _err = _try_load_config(k)
        if cfg is None:
            continue
        symbols = cfg.get("symbols") or cfg.get("watchlist") or []
        if not isinstance(symbols, list):
            continue
        for it in symbols:
            if not isinstance(it, dict):
                continue
            row = _to_row(k, it)
            rows.append(row.__dict__)

    # stable sort: configKey, market, symbol
    def _key(r: dict):
        return (r.get("configKey") or "", r.get("market") or "", r.get("symbol") or "")

    rows.sort(key=_key)
    return rows


def _global_summary(config_key: str) -> dict[str, Any]:
    path = CONFIG_FILES[config_key]
    cfg, err = _try_load_config(config_key)
    if cfg is None:
        return {
            "configKey": config_key,
            "path": str(path),
            "exists": path.exists(),
            "error": err,
        }

    symbols = cfg.get("symbols") or cfg.get("watchlist") or []
    if not isinstance(symbols, list):
        symbols = []

    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    schedule = cfg.get("schedule") if isinstance(cfg.get("schedule"), dict) else {}
    templates = cfg.get("templates") if isinstance(cfg.get("templates"), dict) else {}

    return {
        "configKey": config_key,
        "path": str(path),
        "exists": True,
        "accounts": accounts_from_config(cfg),
        "symbolCount": len(symbols),
        "enabledSymbolCount": sum(
            1
            for it in symbols
            if isinstance(it, dict)
            and (
                bool((it.get("sell_put") or {}).get("enabled"))
                or bool((it.get("sell_call") or {}).get("enabled"))
            )
        ),
        "sections": {
            "schedule": schedule,
            "notifications": {
                "enabled": notifications.get("enabled"),
                "channel": notifications.get("channel"),
                "mode": notifications.get("mode"),
                "include_cash_footer": notifications.get("include_cash_footer"),
                "quiet_hours_beijing": notifications.get("quiet_hours_beijing"),
            },
            "templates": sorted(templates.keys()),
            "outputs": cfg.get("outputs") if isinstance(cfg.get("outputs"), dict) else {},
            "runtime": cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {},
            "alert_policy": cfg.get("alert_policy") if isinstance(cfg.get("alert_policy"), dict) else {},
            "fetch_policy": cfg.get("fetch_policy") if isinstance(cfg.get("fetch_policy"), dict) else {},
            "portfolio": cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {},
        },
    }


def _find_symbol(cfg: dict, symbol: str) -> tuple[int | None, dict | None]:
    symbols = cfg.get("symbols")
    if symbols is None:
        symbols = cfg.get("watchlist")
    if not isinstance(symbols, list):
        return None, None

    s = symbol.strip().upper()
    for i, it in enumerate(symbols):
        if not isinstance(it, dict):
            continue
        if str(it.get("symbol") or "").strip().upper() == s:
            return i, it
    return None, None


def _ensure_symbols_list(cfg: dict) -> list:
    if cfg.get("symbols") is None and isinstance(cfg.get("watchlist"), list):
        cfg["symbols"] = cfg.get("watchlist")
    if cfg.get("symbols") is None:
        cfg["symbols"] = []
    if not isinstance(cfg.get("symbols"), list):
        raise HTTPException(status_code=400, detail="config symbols must be a list")
    return cfg["symbols"]


def _patch_entry(entry: dict, payload: dict):
    # only patch known editable fields; keep other fields untouched
    if "market" in payload:
        entry["market"] = payload.get("market")

    if "accounts" in payload:
        accounts = payload.get("accounts")
        if accounts is None:
            entry.pop("accounts", None)
        elif isinstance(accounts, list):
            entry["accounts"] = [str(a).strip().lower() for a in accounts if str(a).strip()]
        else:
            raise HTTPException(status_code=400, detail="accounts must be list or null")

    if "limit_expirations" in payload:
        le = payload.get("limit_expirations")
        entry.setdefault("fetch", {})
        if not isinstance(entry.get("fetch"), dict):
            entry["fetch"] = {}
        if le is None:
            entry["fetch"].pop("limit_expirations", None)
        else:
            entry["fetch"]["limit_expirations"] = int(le)

    # sell_put
    sp = entry.get("sell_put")
    if not isinstance(sp, dict):
        sp = {}
        entry["sell_put"] = sp
    mapping_sp = {
        "sell_put_enabled": ("enabled", bool),
        "sell_put_min_dte": ("min_dte", int),
        "sell_put_max_dte": ("max_dte", int),
        # allow empty => 0 (write 0 instead of removing the field)
        "sell_put_min_strike": ("min_strike", float),
        "sell_put_max_strike": ("max_strike", float),
    }
    for k, (field, caster) in mapping_sp.items():
        if k in payload:
            v = payload.get(k)
            if v is None:
                sp.pop(field, None)
            else:
                sp[field] = caster(v)

    # sell_call
    sc = entry.get("sell_call")
    if not isinstance(sc, dict):
        sc = {}
        entry["sell_call"] = sc
    mapping_sc = {
        "sell_call_enabled": ("enabled", bool),
        "sell_call_min_dte": ("min_dte", int),
        "sell_call_max_dte": ("max_dte", int),
        "sell_call_min_strike": ("min_strike", float),
        "sell_call_max_strike": ("max_strike", float),
    }
    for k, (field, caster) in mapping_sc.items():
        if k in payload:
            v = payload.get(k)
            if v is None:
                sc.pop(field, None)
            else:
                sc[field] = caster(v)


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(static_dir / "index.html"))


@app.get("/api/watchlist")
def api_list_watchlist():
    return {"rows": _list_rows()}


@app.get("/api/configs/summary")
def api_configs_summary():
    return {"configs": {k: _global_summary(k) for k in ("hk", "us")}}


@app.post("/api/watchlist/upsert")
async def api_upsert(req: Request):
    _require_token_for_write(req)
    payload = await req.json()

    config_key = str(payload.get("configKey") or "").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    cfg = _load_config(config_key)
    symbols = _ensure_symbols_list(cfg)

    idx, entry = _find_symbol(cfg, symbol)
    if entry is None:
        entry = {"symbol": symbol, "sell_put": {"enabled": False}, "sell_call": {"enabled": False}}
        symbols.append(entry)
    else:
        # idx should exist
        pass

    _patch_entry(entry, payload)

    path = CONFIG_FILES[config_key]
    bak = _backup(path)
    try:
        _write_config_atomic(path, cfg)
        _validate_config(path)
    except HTTPException:
        shutil.copy2(bak, path)
        raise
    except Exception as e:
        shutil.copy2(bak, path)
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "rows": _list_rows()}


@app.post("/api/watchlist/delete")
async def api_delete(req: Request):
    _require_token_for_write(req)
    payload = await req.json()

    config_key = str(payload.get("configKey") or "").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    cfg = _load_config(config_key)
    symbols = _ensure_symbols_list(cfg)

    idx, _entry = _find_symbol(cfg, symbol)
    if idx is None:
        return {"ok": True, "rows": _list_rows()}

    symbols.pop(idx)

    path = CONFIG_FILES[config_key]
    bak = _backup(path)
    try:
        _write_config_atomic(path, cfg)
        _validate_config(path)
    except HTTPException:
        shutil.copy2(bak, path)
        raise
    except Exception as e:
        shutil.copy2(bak, path)
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "rows": _list_rows()}


@app.get("/api/meta")
def api_meta():
    accounts: set[str] = set()
    for key in CONFIG_FILES:
        try:
            accounts.update(accounts_from_config(_load_config(key)))
        except HTTPException:
            continue
    return {
        "configs": {k: str(v) for k, v in CONFIG_FILES.items()},
        "accounts": sorted(accounts),
        "tokenRequired": bool((os.environ.get("OM_WEBUI_TOKEN") or "").strip()),
    }
