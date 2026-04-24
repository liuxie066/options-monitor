from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from domain.domain.close_advice import CloseAdviceConfig
from domain.storage.repositories import state_repo
from scripts.account_config import (
    ACCOUNT_TYPE_EXTERNAL_HOLDINGS,
    accounts_from_config,
    build_account_portfolio_source_plan,
)
from scripts.infra.service import send_openclaw_message
from scripts.validate_config import SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS as VALIDATOR_SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS
from src.application.account_management import add_account, edit_account, remove_account
from src.application.tool_execution import build_tool_manifest, execute_tool


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_CONFIG_DIR = Path("../options-monitor-config")


def _runtime_config_path(config_key: str, filename: str) -> Path:
    env_key = f"OM_WEBUI_CONFIG_{config_key.upper()}"
    explicit = (os.environ.get(env_key) or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    env_dir = (os.environ.get("OM_WEBUI_CONFIG_DIR") or "").strip()
    if env_dir:
        return Path(env_dir).expanduser() / filename

    return DEFAULT_RUNTIME_CONFIG_DIR / filename


CONFIG_FILES: dict[str, Path] = {
    "us": _runtime_config_path("us", "config.us.json"),
    "hk": _runtime_config_path("hk", "config.hk.json"),
}

GLOBAL_STRATEGY_FIELDS: dict[str, type] = {
    "min_annualized_net_return": float,
    "min_net_income": float,
    "min_open_interest": int,
    "min_volume": int,
    "max_spread_ratio": float,
}

NOTIFICATION_NUMERIC_FIELDS: dict[str, type] = {
    "cash_footer_timeout_sec": int,
    "cash_snapshot_max_age_sec": int,
    "opend_alert_cooldown_sec": int,
    "opend_alert_burst_window_sec": int,
    "opend_alert_burst_max": int,
}

SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS = VALIDATOR_SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS


SCHEDULE_SUMMARY_FIELDS = {
    "enabled",
    "market_timezone",
    "beijing_timezone",
    "market_open",
    "market_close",
    "market_break_start",
    "market_break_end",
    "first_notify_after_open_min",
    "notify_interval_min",
    "final_notify_before_close_min",
}


@dataclass(frozen=True)
class SymbolRow:
    configKey: Literal["us", "hk"]
    symbol: str
    name: str | None
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


@dataclass(frozen=True)
class AccountRow:
    configKey: Literal["us", "hk"]
    account_label: str
    account_type: str
    futu_acc_ids: list[str]
    holdings_account: str | None
    portfolio_source: str | None
    primary_source: str | None
    primary_ready: bool
    fallback_enabled: bool
    fallback_source: str | None
    fallback_ready: bool | None


app = FastAPI(title="options-monitor webui", version="0.1.0")

static_dir = (Path(__file__).resolve().parent / "static").resolve()
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/__debug/z")
def debug_z() -> dict[str, Any]:
    return {
        "static_dir": str(static_dir),
        "config_files": {k: str(v) for k, v in CONFIG_FILES.items()},
        "resolved_config_files": {k: str(_resolve_config_path(v)) for k, v in CONFIG_FILES.items()},
        "ts": int(time.time()),
    }


def _resolve_config_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return (BASE_DIR / path).resolve()


def _recommended_runtime_config_path(config_key: str) -> Path:
    filename = "config.hk.json" if str(config_key).strip().lower() == "hk" else "config.us.json"
    return (BASE_DIR.parent / "options-monitor-config" / filename).resolve()


def _uses_runtime_config_override(config_key: str) -> bool:
    env_key = f"OM_WEBUI_CONFIG_{str(config_key).strip().upper()}"
    explicit = (os.environ.get(env_key) or "").strip()
    if explicit:
        return True
    env_dir = (os.environ.get("OM_WEBUI_CONFIG_DIR") or "").strip()
    return bool(env_dir)


def _load_config(config_key: str) -> dict:
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail=f"invalid configKey: {config_key}")

    path = _resolve_config_path(CONFIG_FILES[config_key])
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

    cmd = [str(py), "scripts/validate_config.py", "--config", str(path)]
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


def _normalize_symbol_for_name_lookup(value: Any) -> str:
    return str(value or "").strip().upper()


def _symbol_name_from_aliases(symbol: str, cfg: dict | None) -> str | None:
    if not isinstance(cfg, dict):
        return None
    intake = cfg.get("intake") if isinstance(cfg.get("intake"), dict) else {}
    aliases = intake.get("symbol_aliases") if isinstance(intake.get("symbol_aliases"), dict) else {}
    target = _normalize_symbol_for_name_lookup(symbol)
    for alias, alias_symbol in aliases.items():
        if _normalize_symbol_for_name_lookup(alias_symbol) == target:
            name = str(alias).strip()
            if name:
                return name
    return None


def _to_row(config_key: str, item: dict, cfg: dict | None = None) -> SymbolRow:
    fetch = item.get("fetch") or {}
    sp = item.get("sell_put") or {}
    sc = item.get("sell_call") or {}
    symbol = str(item.get("symbol") or "")
    name = item.get("name") or item.get("display_name") or item.get("symbol_name")
    if name is None or not str(name).strip():
        name = _symbol_name_from_aliases(symbol, cfg)

    return SymbolRow(
        configKey=config_key,  # type: ignore
        symbol=symbol,
        name=str(name).strip() if name is not None and str(name).strip() else None,
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
        symbols = cfg.get("symbols") or []
        if not isinstance(symbols, list):
            continue
        for it in symbols:
            if not isinstance(it, dict):
                continue
            row = _to_row(k, it, cfg)
            rows.append(row.__dict__)

    # stable sort: configKey, market, symbol
    def _key(r: dict):
        return (r.get("configKey") or "", r.get("market") or "", r.get("symbol") or "")

    rows.sort(key=_key)
    return rows


def _global_summary(config_key: str) -> dict[str, Any]:
    path = _resolve_config_path(CONFIG_FILES[config_key])
    recommended_path = _recommended_runtime_config_path(config_key)
    path_warning = recommended_path.exists() and path != recommended_path and not _uses_runtime_config_override(config_key)
    cfg, err = _try_load_config(config_key)
    if cfg is None:
        return {
            "configKey": config_key,
            "path": str(CONFIG_FILES[config_key]),
            "resolvedPath": str(path),
            "recommendedPath": str(recommended_path),
            "recommendedPathExists": recommended_path.exists(),
            "canonicalPathWarning": path_warning,
            "exists": path.exists(),
            "error": err,
        }

    symbols = cfg.get("symbols") or []
    if not isinstance(symbols, list):
        symbols = []

    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    raw_schedule = cfg.get("schedule") if isinstance(cfg.get("schedule"), dict) else {}
    schedule = {k: raw_schedule.get(k) for k in SCHEDULE_SUMMARY_FIELDS if k in raw_schedule}
    templates = cfg.get("templates") if isinstance(cfg.get("templates"), dict) else {}
    close_advice = cfg.get("close_advice") if isinstance(cfg.get("close_advice"), dict) else {}
    resolved_close_advice = CloseAdviceConfig.from_mapping(close_advice)
    put_template = templates.get("put_base") if isinstance(templates.get("put_base"), dict) else {}
    call_template = templates.get("call_base") if isinstance(templates.get("call_base"), dict) else {}
    put_strategy = put_template.get("sell_put") if isinstance(put_template.get("sell_put"), dict) else {}
    call_strategy = call_template.get("sell_call") if isinstance(call_template.get("sell_call"), dict) else {}

    return {
        "configKey": config_key,
        "path": str(CONFIG_FILES[config_key]),
        "resolvedPath": str(path),
        "recommendedPath": str(recommended_path),
        "recommendedPathExists": recommended_path.exists(),
        "canonicalPathWarning": path_warning,
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
                k: v
                for k, v in {
                    "enabled": notifications.get("enabled"),
                    "channel": notifications.get("channel"),
                    "target": notifications.get("target"),
                    "include_cash_footer": notifications.get("include_cash_footer"),
                    "cash_footer_accounts": notifications.get("cash_footer_accounts"),
                    "cash_footer_timeout_sec": notifications.get("cash_footer_timeout_sec"),
                    "cash_snapshot_max_age_sec": notifications.get("cash_snapshot_max_age_sec"),
                    "quiet_hours_beijing": notifications.get("quiet_hours_beijing"),
                    "opend_alert_cooldown_sec": notifications.get("opend_alert_cooldown_sec"),
                    "opend_alert_burst_window_sec": notifications.get("opend_alert_burst_window_sec"),
                    "opend_alert_burst_max": notifications.get("opend_alert_burst_max"),
                }.items()
                if v is not None
            },
            "templates": sorted(templates.keys()),
            "outputs": cfg.get("outputs") if isinstance(cfg.get("outputs"), dict) else {},
            "runtime": cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {},
            "alert_policy": cfg.get("alert_policy") if isinstance(cfg.get("alert_policy"), dict) else {},
            "fetch_policy": cfg.get("fetch_policy") if isinstance(cfg.get("fetch_policy"), dict) else {},
            "portfolio": cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {},
            "close_advice": {
                k: v
                for k, v in {
                    "enabled": close_advice.get("enabled"),
                    "quote_source": close_advice.get("quote_source"),
                    "notify_levels": close_advice.get("notify_levels"),
                    "max_items_per_account": close_advice.get("max_items_per_account"),
                    "max_spread_ratio": resolved_close_advice.max_spread_ratio,
                    "strong_remaining_annualized_max": resolved_close_advice.strong_remaining_annualized_max,
                    "medium_remaining_annualized_max": resolved_close_advice.medium_remaining_annualized_max,
                }.items()
                if v is not None
            },
        },
        "globalStrategy": {
            "sell_put": {k: put_strategy.get(k) for k in GLOBAL_STRATEGY_FIELDS},
            "sell_call": {k: call_strategy.get(k) for k in GLOBAL_STRATEGY_FIELDS},
        },
    }


def _patch_close_advice(cfg: dict, payload: dict) -> None:
    close_advice = payload.get("closeAdvice")
    if close_advice is None:
        return
    if not isinstance(close_advice, dict):
        raise HTTPException(status_code=400, detail="closeAdvice must be an object")

    close_cfg = cfg.get("close_advice")
    if close_cfg is None:
        close_cfg = {}
        cfg["close_advice"] = close_cfg
    if not isinstance(close_cfg, dict):
        raise HTTPException(status_code=400, detail="config close_advice must be an object")

    if "enabled" in close_advice:
        close_cfg["enabled"] = bool(close_advice.get("enabled"))
    if "quote_source" in close_advice:
        value = str(close_advice.get("quote_source") or "").strip()
        if value:
            close_cfg["quote_source"] = value
        else:
            close_cfg.pop("quote_source", None)
    if "notify_levels" in close_advice:
        raw_levels = close_advice.get("notify_levels")
        if raw_levels is None:
            close_cfg.pop("notify_levels", None)
        elif isinstance(raw_levels, list):
            levels = [str(x).strip().lower() for x in raw_levels if str(x).strip()]
            if levels:
                close_cfg["notify_levels"] = levels
            else:
                close_cfg.pop("notify_levels", None)
        else:
            raise HTTPException(status_code=400, detail="closeAdvice.notify_levels must be a list or null")

    numeric_fields = (
        "max_items_per_account",
        "max_spread_ratio",
        "strong_remaining_annualized_max",
        "medium_remaining_annualized_max",
    )
    for field in numeric_fields:
        if field not in close_advice:
            continue
        raw = close_advice.get(field)
        if raw is None or raw == "":
            close_cfg.pop(field, None)
            continue
        caster = int if field == "max_items_per_account" else float
        try:
            value = caster(raw)
        except Exception:
            raise HTTPException(status_code=400, detail=f"closeAdvice.{field} must be a number")
        if value < 0:
            raise HTTPException(status_code=400, detail=f"closeAdvice.{field} must be >= 0")
        close_cfg[field] = value


def _patch_global_strategy(cfg: dict, payload: dict):
    strategies = payload.get("strategies")
    if not isinstance(strategies, dict):
        raise HTTPException(status_code=400, detail="strategies must be an object")

    templates = cfg.get("templates")
    if templates is None:
        templates = {}
        cfg["templates"] = templates
    if not isinstance(templates, dict):
        raise HTTPException(status_code=400, detail="templates must be an object")
    cfg["templates"] = templates

    targets = {
        "sell_put": ("put_base", "sell_put"),
        "sell_call": ("call_base", "sell_call"),
    }
    for side, (template_name, side_key) in targets.items():
        side_payload = strategies.get(side)
        if side_payload is None:
            continue
        if not isinstance(side_payload, dict):
            raise HTTPException(status_code=400, detail=f"strategies.{side} must be an object")

        template = templates.get(template_name)
        if template is None:
            template = {}
            templates[template_name] = template
        if not isinstance(template, dict):
            raise HTTPException(status_code=400, detail=f"templates.{template_name} must be an object")

        side_cfg = template.get(side_key)
        if side_cfg is None:
            side_cfg = {}
            template[side_key] = side_cfg
        if not isinstance(side_cfg, dict):
            raise HTTPException(status_code=400, detail=f"templates.{template_name}.{side_key} must be an object")

        for field, caster in GLOBAL_STRATEGY_FIELDS.items():
            if field not in side_payload:
                continue
            raw = side_payload.get(field)
            if raw is None or raw == "":
                raise HTTPException(status_code=400, detail=f"{side}.{field} is required")
            try:
                value = caster(raw)
            except Exception:
                raise HTTPException(status_code=400, detail=f"{side}.{field} must be a number")
            if field in {"min_annualized_net_return", "min_net_income", "min_open_interest", "min_volume"} and value < 0:
                raise HTTPException(status_code=400, detail=f"{side}.{field} must be >= 0")
            if field == "max_spread_ratio" and value < 0:
                raise HTTPException(status_code=400, detail=f"{side}.{field} must be >= 0")
            side_cfg[field] = value


def _patch_notifications(cfg: dict, payload: dict) -> None:
    notifications = payload.get("notifications")
    if notifications is None:
        return
    if not isinstance(notifications, dict):
        raise HTTPException(status_code=400, detail="notifications must be an object")

    notif_cfg = cfg.get("notifications")
    if notif_cfg is None:
        notif_cfg = {}
        cfg["notifications"] = notif_cfg
    if not isinstance(notif_cfg, dict):
        raise HTTPException(status_code=400, detail="config notifications must be an object")

    for key in ("enabled", "include_cash_footer"):
        if key in notifications:
            value = notifications.get(key)
            if value is None:
                notif_cfg.pop(key, None)
            else:
                notif_cfg[key] = bool(value)

    for key in ("channel", "target"):
        if key in notifications:
            raw = notifications.get(key)
            value = str(raw or "").strip()
            if value:
                notif_cfg[key] = value
            else:
                notif_cfg.pop(key, None)

    if "cash_footer_accounts" in notifications:
        raw_accounts = notifications.get("cash_footer_accounts")
        if raw_accounts is None:
            notif_cfg.pop("cash_footer_accounts", None)
        elif isinstance(raw_accounts, list):
            values = [str(x).strip().lower() for x in raw_accounts if str(x).strip()]
            if values:
                notif_cfg["cash_footer_accounts"] = values
            else:
                notif_cfg.pop("cash_footer_accounts", None)
        else:
            raise HTTPException(status_code=400, detail="notifications.cash_footer_accounts must be a list or null")

    if "quiet_hours_beijing" in notifications:
        raw_quiet = notifications.get("quiet_hours_beijing")
        if raw_quiet is None:
            notif_cfg.pop("quiet_hours_beijing", None)
        elif not isinstance(raw_quiet, dict):
            raise HTTPException(status_code=400, detail="notifications.quiet_hours_beijing must be an object or null")
        else:
            start = str(raw_quiet.get("start") or "").strip()
            end = str(raw_quiet.get("end") or "").strip()
            if start or end:
                if not start or not end:
                    raise HTTPException(status_code=400, detail="notifications.quiet_hours_beijing.start/end must be set together")
                notif_cfg["quiet_hours_beijing"] = {"start": start, "end": end}
            else:
                notif_cfg.pop("quiet_hours_beijing", None)

    for field, caster in NOTIFICATION_NUMERIC_FIELDS.items():
        if field not in notifications:
            continue
        raw = notifications.get(field)
        if raw is None or raw == "":
            notif_cfg.pop(field, None)
            continue
        try:
            value = caster(raw)
        except Exception:
            raise HTTPException(status_code=400, detail=f"notifications.{field} must be a number")
        if value < 0:
            raise HTTPException(status_code=400, detail=f"notifications.{field} must be >= 0")
        notif_cfg[field] = value


def _find_symbol(cfg: dict, symbol: str) -> tuple[int | None, dict | None]:
    symbols = cfg.get("symbols")
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
    if cfg.get("symbols") is None:
        cfg["symbols"] = []
    if not isinstance(cfg.get("symbols"), list):
        raise HTTPException(status_code=400, detail="config symbols must be a list")
    return cfg["symbols"]


def _clean_symbol_level_strategy_fields(cfg: dict) -> None:
    symbols = cfg.get("symbols")
    if not isinstance(symbols, list):
        return
    for item in symbols:
        if not isinstance(item, dict):
            continue
        for side in ("sell_put", "sell_call"):
            side_cfg = item.get(side)
            if not isinstance(side_cfg, dict):
                continue
            for field in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS:
                side_cfg.pop(field, None)


def _mask_acc_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return f"...{raw[-4:]}"
    return raw


def _account_rows(config_key: str) -> list[dict[str, Any]]:
    cfg = _load_config(config_key)
    config_path = _resolve_config_path(CONFIG_FILES[config_key])
    data_cfg = _load_data_config_for_runtime(cfg, config_path=config_path)
    feishu_cfg = data_cfg.get("feishu") if isinstance(data_cfg.get("feishu"), dict) else {}
    feishu_tables = feishu_cfg.get("tables") if isinstance(feishu_cfg.get("tables"), dict) else {}
    feishu_ready = bool(str(feishu_cfg.get("app_id") or "").strip()) and bool(str(feishu_cfg.get("app_secret") or "").strip())
    holdings_table_ready = bool(str(feishu_tables.get("holdings") or "").strip())
    holdings_ready = feishu_ready and holdings_table_ready
    accounts = accounts_from_config(cfg)
    account_settings = cfg.get("account_settings") if isinstance(cfg.get("account_settings"), dict) else {}
    trade_intake = cfg.get("trade_intake") if isinstance(cfg.get("trade_intake"), dict) else {}
    account_mapping = trade_intake.get("account_mapping") if isinstance(trade_intake.get("account_mapping"), dict) else {}
    futu_mapping = account_mapping.get("futu") if isinstance(account_mapping.get("futu"), dict) else {}
    rows: list[dict[str, Any]] = []
    for account in accounts:
        setting = account_settings.get(account) if isinstance(account_settings.get(account), dict) else {}
        source_plan = build_account_portfolio_source_plan(cfg, account=account)
        account_type = source_plan.account_type
        futu_acc_ids = [_mask_acc_id(acc_id) for acc_id, label in futu_mapping.items() if str(label or "").strip().lower() == account]
        fallback_enabled = bool(source_plan.fallback_source) or account_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS
        primary_ready = bool(futu_acc_ids) if source_plan.primary_source == "futu" else bool(holdings_ready)
        fallback_ready = (bool(holdings_ready) if fallback_enabled else None)
        row = AccountRow(
            configKey=config_key,  # type: ignore[arg-type]
            account_label=account,
            account_type=str(setting.get("type") or account_type or "futu"),
            futu_acc_ids=futu_acc_ids,
            holdings_account=(str(source_plan.holdings_account or "").strip() or None),
            portfolio_source=source_plan.requested_source,
            primary_source=source_plan.primary_source,
            primary_ready=primary_ready,
            fallback_enabled=fallback_enabled,
            fallback_source=(source_plan.fallback_source or ("holdings" if fallback_enabled else None)),
            fallback_ready=fallback_ready,
        )
        rows.append(row.__dict__)
    return rows


def _output_root() -> Path:
    return (BASE_DIR / "output" / "agent_plugin").resolve()


def _report_text(path: Path, *, max_chars: int = 12000) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if len(text) > max_chars:
        return text[:max_chars] + "\n...[truncated]"
    return text


def _json_report(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _tool_result_snapshot(config_key: str) -> dict[str, Any]:
    output_root = _output_root()
    reports_dir = (output_root / "reports").resolve()
    state_dir = (output_root / "state").resolve()
    return {
        "configKey": config_key,
        "paths": {
            "outputRoot": str(output_root),
            "reports": str(reports_dir),
            "state": str(state_dir),
        },
        "artifacts": {
            "close_advice_text": _report_text(reports_dir / "close_advice.txt"),
            "symbols_notification_text": _report_text(reports_dir / "symbols_notification.txt"),
            "option_positions_context": _json_report(state_dir / "option_positions_context.json"),
        },
    }


def _read_json_file(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl_tail(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
    except Exception:
        return []
    if limit > 0:
        rows = rows[-int(limit):]
    return rows


def _resolve_portfolio_data_config_path(cfg: dict[str, Any], *, config_path: Path) -> Path | None:
    portfolio = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    raw = portfolio.get("data_config")
    if raw is None or not str(raw).strip():
        return None
    path = Path(str(raw).strip()).expanduser()
    if not path.is_absolute():
        path = (config_path.parent / path).resolve()
    return path


def _load_data_config_for_runtime(cfg: dict[str, Any], *, config_path: Path) -> dict[str, Any]:
    data_path = _resolve_portfolio_data_config_path(cfg, config_path=config_path)
    if data_path is None or not data_path.exists():
        return {}
    payload = _read_json_file(data_path)
    return payload if isinstance(payload, dict) else {}


def _tool_execution_status(result: dict[str, Any]) -> tuple[str, bool, str]:
    ok = bool(result.get("ok"))
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    if ok:
        return "fetched", True, "completed"
    message = str(error.get("message") or "tool failed")
    return "error", False, message


def _append_webui_tool_execution_audit(*, config_key: str, tool_name: str, result: dict[str, Any]) -> None:
    started = datetime.now(timezone.utc).isoformat()
    finished = datetime.now(timezone.utc).isoformat()
    status, ok, message = _tool_execution_status(result)
    payload = {
        "schema_kind": "tool_execution",
        "schema_version": "1.0",
        "tool_name": str(tool_name),
        "symbol": "",
        "source": "webui",
        "limit_exp": 0,
        "status": status,
        "ok": ok,
        "message": message,
        "returncode": 0 if ok else 1,
        "started_at_utc": started,
        "finished_at_utc": finished,
        "config_key": str(config_key or "").strip().lower(),
    }
    state_repo.append_tool_execution_audit(_output_root(), payload)
    state_repo.append_audit_event(
        _output_root(),
        {
            "event_type": "webui_tool_run",
            "action": str(tool_name),
            "status": ("ok" if ok else "error"),
            "message": message,
            "tool_name": str(tool_name),
            "extra": {"config_key": str(config_key or "").strip().lower()},
        },
    )


def _repair_hint_from_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(error, dict) or not error:
        return None
    code = str(error.get("code") or "").strip().upper()
    message = str(error.get("message") or "").strip()
    hint = str(error.get("hint") or "").strip()
    actions: list[str] = []

    if code == "CONFIG_ERROR":
        actions = [
            "先运行 healthcheck，确认 runtime config 和 SQLite data config 都存在。",
            "检查 config 内的 broker/account/data_config 路径是否填对。",
        ]
        if "runtime config not found" in message.lower():
            actions.insert(0, "先运行 om-agent init 生成本地配置，或在 WebUI 指向正确的 runtime config。")
    elif code == "DEPENDENCY_MISSING":
        actions = [
            "检查 OpenD 是否已启动并登录。",
            "确认本地依赖和 secrets 文件已经按安装步骤准备完成。",
        ]
    elif code == "PERMISSION_DENIED":
        actions = [
            "这是受保护动作，默认不会直接执行。",
            "如果确实需要写操作，先显式开启写工具或改用 dry-run。",
        ]
    elif code == "CONFIRMATION_REQUIRED":
        actions = [
            "当前动作需要 confirm=true 才会真实执行。",
            "先做 dry-run，确认输出后再重试。",
        ]
    elif "opend" in message.lower():
        actions = [
            "确认 OpenD 进程在线，并且当前机器能连到配置里的 host/port。",
            "确认富途客户端或 OpenD 登录状态正常，再重试 healthcheck。",
        ]
    elif "portfolio.data_config" in message.lower() or "sqlite" in message.lower():
        actions = [
            "确认 portfolio.data_config 指向本地 SQLite data config。",
            "检查 position lots 的 SQLite 文件和 secrets 配置是否存在。",
        ]

    if not hint and not actions:
        return None
    return {
        "code": code or None,
        "summary": hint or message or "请先修复配置或依赖问题后重试。",
        "actions": actions,
    }


def _history_snapshot(*, config_key: str, limit: int = 20) -> dict[str, Any]:
    base = _output_root()
    shared_dir = state_repo.shared_state_dir(base)
    current_dir = state_repo.shared_current_read_model_dir(base)
    tool_rows = state_repo.query_tool_execution_audit(base, limit=limit)
    filtered_tool_rows = [
        row for row in tool_rows
        if not row.get("config_key") or str(row.get("config_key")).strip().lower() == str(config_key).strip().lower()
    ]
    audit_rows = _read_jsonl_tail(shared_dir / "audit_events.jsonl", limit=limit)
    filtered_audit_rows = [
        row for row in audit_rows
        if not ((row.get("extra") if isinstance(row.get("extra"), dict) else {}).get("config_key"))
        or str((row.get("extra") if isinstance(row.get("extra"), dict) else {}).get("config_key")).strip().lower() == str(config_key).strip().lower()
    ]
    tick_metrics = _read_json_file(shared_dir / "tick_metrics_history.json")
    if not isinstance(tick_metrics, list):
        tick_metrics = []
    last_run = _read_json_file(current_dir / "last_run.current.json")
    latest_audit = _read_json_file(current_dir / "audit_event_latest.current.json")
    return {
        "toolExecutions": filtered_tool_rows,
        "auditEvents": filtered_audit_rows,
        "tickMetrics": tick_metrics[-int(limit):],
        "lastRun": (last_run if isinstance(last_run, dict) else None),
        "latestAudit": (latest_audit if isinstance(latest_audit, dict) else None),
    }


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
    for field in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS:
        sp.pop(field, None)
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
    for field in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS:
        sc.pop(field, None)
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
    return FileResponse(
        str(static_dir / "index.html"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/watchlist")
def api_list_watchlist():
    return {"rows": _list_rows()}


@app.get("/api/accounts")
def api_list_accounts(configKey: str):
    key = str(configKey or "").strip().lower()
    if key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    return {"rows": _account_rows(key)}


@app.get("/api/configs/summary")
def api_configs_summary():
    return {"configs": {k: _global_summary(k) for k in ("hk", "us")}}


@app.post("/api/configs/global/update")
async def api_update_global_config(req: Request):
    _require_token_for_write(req)
    payload = await req.json()

    config_key = str(payload.get("configKey") or "").strip().lower()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")

    cfg = _load_config(config_key)
    _patch_global_strategy(cfg, payload)
    _patch_notifications(cfg, payload)
    _patch_close_advice(cfg, payload)
    _clean_symbol_level_strategy_fields(cfg)

    path = _resolve_config_path(CONFIG_FILES[config_key])
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

    return {"ok": True, "configs": {k: _global_summary(k) for k in ("hk", "us")}}


@app.post("/api/accounts/upsert")
async def api_upsert_account(req: Request):
    _require_token_for_write(req)
    payload = await req.json()

    config_key = str(payload.get("configKey") or "").strip().lower()
    account_label = str(payload.get("accountLabel") or "").strip()
    account_type = str(payload.get("accountType") or "").strip()
    futu_acc_id = payload.get("futuAccId")
    holdings_account = payload.get("holdingsAccount")
    mode = str(payload.get("mode") or "").strip().lower()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    if not account_label:
        raise HTTPException(status_code=400, detail="accountLabel is required")
    if mode not in {"add", "edit"}:
        raise HTTPException(status_code=400, detail="mode must be add|edit")

    config_path = _resolve_config_path(CONFIG_FILES[config_key])
    try:
        if mode == "add":
            result = add_account(
                market=config_key,
                account_label=account_label,
                account_type=account_type,
                config_path=config_path,
                futu_acc_id=futu_acc_id,
                holdings_account=holdings_account,
            )
        else:
            result = edit_account(
                market=config_key,
                account_label=account_label,
                config_path=config_path,
                account_type=(account_type or None),
                futu_acc_id=futu_acc_id,
                holdings_account=holdings_account,
                clear_holdings_account=bool(payload.get("clearHoldingsAccount", False)),
            )
    except Exception as exc:
        detail = getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)
        raise HTTPException(status_code=400, detail=detail)

    return {"ok": True, "result": result, "rows": _account_rows(config_key), "configs": {k: _global_summary(k) for k in ("hk", "us")}}


@app.post("/api/accounts/delete")
async def api_delete_account(req: Request):
    _require_token_for_write(req)
    payload = await req.json()
    config_key = str(payload.get("configKey") or "").strip().lower()
    account_label = str(payload.get("accountLabel") or "").strip()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    if not account_label:
        raise HTTPException(status_code=400, detail="accountLabel is required")

    config_path = _resolve_config_path(CONFIG_FILES[config_key])
    try:
        result = remove_account(
            market=config_key,
            account_label=account_label,
            config_path=config_path,
        )
    except Exception as exc:
        detail = getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)
        raise HTTPException(status_code=400, detail=detail)

    return {"ok": True, "result": result, "rows": _account_rows(config_key), "configs": {k: _global_summary(k) for k in ("hk", "us")}}


@app.get("/api/spec")
def api_spec():
    return build_tool_manifest()


@app.post("/api/tools/run")
async def api_tools_run(req: Request):
    payload = await req.json()
    tool_name = str(payload.get("toolName") or "").strip()
    if tool_name not in {"healthcheck", "scan_opportunities", "get_close_advice"}:
        raise HTTPException(status_code=400, detail="unsupported tool for WebUI")

    input_payload = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    config_key = str(input_payload.get("config_key") or payload.get("configKey") or "").strip().lower()
    if config_key in {"us", "hk"} and "config_key" not in input_payload:
        input_payload = {**input_payload, "config_key": config_key}
    result = execute_tool(tool_name, input_payload)
    _append_webui_tool_execution_audit(config_key=str(input_payload.get("config_key") or config_key or "us"), tool_name=tool_name, result=result)
    return {
        "result": result,
        "snapshot": _tool_result_snapshot(str(input_payload.get("config_key") or config_key or "us")),
        "repairHint": _repair_hint_from_error(result.get("error") if isinstance(result, dict) else None),
    }


@app.get("/api/history")
def api_history(configKey: str, limit: int = 20):
    key = str(configKey or "").strip().lower()
    if key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    return _history_snapshot(config_key=key, limit=max(1, min(int(limit), 100)))


@app.post("/api/notifications/check")
async def api_notifications_check(req: Request):
    payload = await req.json()
    config_key = str(payload.get("configKey") or "").strip().lower()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    cfg = _load_config(config_key)
    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    output_root = _output_root()
    reports_dir = (output_root / "reports").resolve()
    checks = [
        {"name": "notifications_enabled", "ok": bool(notifications.get("enabled", False)), "message": ("enabled" if notifications.get("enabled", False) else "disabled")},
        {"name": "target", "ok": bool(str(notifications.get("target") or "").strip()), "message": (str(notifications.get("target") or "").strip() or "notifications.target missing")},
        {"name": "channel", "ok": bool(str(notifications.get("channel") or "").strip()), "message": (str(notifications.get("channel") or "").strip() or "notifications.channel missing")},
        {"name": "preview_source", "ok": (reports_dir / "symbols_notification.txt").exists(), "message": ("symbols_notification.txt found" if (reports_dir / "symbols_notification.txt").exists() else "run scan first to generate symbols_notification.txt")},
        {"name": "sender_binary", "ok": shutil.which("openclaw") is not None, "message": ("openclaw found" if shutil.which("openclaw") is not None else "openclaw command not found")},
    ]
    return {"checks": checks, "ok": all(bool(item["ok"]) for item in checks if item["name"] != "notifications_enabled")}


@app.post("/api/notifications/preview")
async def api_notifications_preview(req: Request):
    payload = await req.json()
    config_key = str(payload.get("configKey") or "").strip().lower()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    reports_dir = (_output_root() / "reports").resolve()
    alerts_path = reports_dir / "symbols_alerts.txt"
    changes_path = reports_dir / "symbols_changes.txt"
    if not alerts_path.exists() and not changes_path.exists():
        raise HTTPException(status_code=404, detail="preview source files not found; run scan first")
    result = execute_tool(
        "preview_notification",
        {
            "alerts_path": str(alerts_path),
            "changes_path": str(changes_path),
            "account_label": str(payload.get("accountLabel") or "user1"),
        },
    )
    return {"result": result}


@app.post("/api/notifications/test-send")
async def api_notifications_test_send(req: Request):
    _require_token_for_write(req)
    payload = await req.json()
    config_key = str(payload.get("configKey") or "").strip().lower()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    cfg = _load_config(config_key)
    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    channel = str(notifications.get("channel") or "").strip() or "feishu"
    target = str(notifications.get("target") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="notifications.target missing")

    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    if not bool(payload.get("confirm", False)):
        return {
            "ok": True,
            "mode": "dry_run",
            "channel": channel,
            "target": target,
            "message": message,
        }

    send = send_openclaw_message(base=BASE_DIR, channel=channel, target=target, message=message)
    return {
        "ok": send.returncode == 0,
        "mode": "send",
        "channel": channel,
        "target": target,
        "stdout": str(send.stdout or ""),
        "stderr": str(send.stderr or ""),
    }


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
    _clean_symbol_level_strategy_fields(cfg)

    path = _resolve_config_path(CONFIG_FILES[config_key])
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

    path = _resolve_config_path(CONFIG_FILES[config_key])
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
        "recommendedFlow": ["healthcheck", "scan_opportunities", "get_close_advice"],
        "historyEnabled": True,
    }
