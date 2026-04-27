from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from domain.domain.close_advice import CloseAdviceConfig
from domain.storage.repositories import state_repo
from scripts.account_config import (
    accounts_from_config,
    list_account_config_views,
)
from scripts.infra.service import send_openclaw_message
from scripts.validate_config import SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS as VALIDATOR_SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS
from src.application.account_management import add_account, edit_account, remove_account
from src.application.tool_execution import build_tool_manifest, execute_tool
from src.application.webui_config_service import (
    backup as _backup_impl,
    load_config as _load_config_impl,
    load_data_config_for_runtime as _load_data_config_for_runtime_impl,
    recommended_runtime_config_path as _recommended_runtime_config_path_impl,
    require_token_for_write as _require_token_for_write_impl,
    resolve_config_path as _resolve_config_path_impl,
    runtime_config_path as _runtime_config_path_impl,
    try_load_config as _try_load_config_impl,
    uses_runtime_config_override as _uses_runtime_config_override_impl,
    validate_config as _validate_config_impl,
    write_validated_config as _write_validated_config,
)
from src.application.webui_patchers import (
    clean_symbol_level_strategy_fields as _clean_symbol_level_strategy_fields_impl,
    ensure_symbols_list as _ensure_symbols_list_impl,
    find_symbol as _find_symbol_impl,
    patch_close_advice as _patch_close_advice_impl,
    patch_entry as _patch_entry_impl,
    patch_global_strategy as _patch_global_strategy_impl,
    patch_notifications as _patch_notifications_impl,
)
from src.application.webui_editor_adapter import (
    account_payload_defaults as _account_payload_defaults,
    apply_market_data_patch as _apply_market_data_patch,
    build_editor_summary as _build_editor_summary,
    write_notification_credentials as _write_notification_credentials,
)
from src.application.webui_presenters import (
    AccountRow,
    SymbolRow,
    append_webui_tool_execution_audit as _append_webui_tool_execution_audit_impl,
    history_snapshot as _history_snapshot_impl,
    list_rows as _list_rows_impl,
    repair_hint_from_error as _repair_hint_from_error_impl,
    to_row as _to_row_impl,
    tool_result_snapshot as _tool_result_snapshot_impl,
    global_summary as _global_summary_impl,
    account_rows as _account_rows_impl,
)
from src.application.version_check import check_version_update as _check_version_update_impl
from src.application.runtime_config_paths import read_json_file as _read_json_file_impl


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_CONFIG_DIR = Path("../options-monitor-config")


def _runtime_config_path(config_key: str, filename: str) -> Path:
    return _runtime_config_path_impl(config_key, filename, default_runtime_config_dir=DEFAULT_RUNTIME_CONFIG_DIR)


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


app = FastAPI(title="options-monitor webui", version="0.1.0-beta.6")

static_dir = (Path(__file__).resolve().parent / "static").resolve()
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/__debug/z")
def debug_z() -> dict[str, Any]:
    return {
        "static_dir": str(static_dir),
        "config_files": {k: str(v) for k, v in CONFIG_FILES.items()},
        "resolved_config_files": {k: str(_resolve_config_path(v)) for k, v in CONFIG_FILES.items()},
        "ts": 0,
    }


def _resolve_config_path(path: Path) -> Path:
    return _resolve_config_path_impl(path, base_dir=BASE_DIR)


def _recommended_runtime_config_path(config_key: str) -> Path:
    return _recommended_runtime_config_path_impl(config_key, base_dir=BASE_DIR)


def _uses_runtime_config_override(config_key: str) -> bool:
    return _uses_runtime_config_override_impl(config_key)


def _load_config(config_key: str) -> dict:
    return _load_config_impl(config_key, config_files=CONFIG_FILES, base_dir=BASE_DIR)


def _try_load_config(config_key: str) -> tuple[dict | None, str | None]:
    return _try_load_config_impl(config_key, config_files=CONFIG_FILES, base_dir=BASE_DIR)


def _to_row(config_key: str, item: dict, cfg: dict | None = None) -> SymbolRow:
    return _to_row_impl(config_key, item, cfg)


def _list_rows() -> list[dict[str, Any]]:
    return _list_rows_impl(config_keys=("us", "hk"), try_load_config=_try_load_config, to_row_fn=_to_row)


def _global_summary(config_key: str) -> dict[str, Any]:
    return _global_summary_impl(
        config_key,
        config_files=CONFIG_FILES,
        resolve_config_path=_resolve_config_path,
        recommended_runtime_config_path=_recommended_runtime_config_path,
        uses_runtime_config_override=_uses_runtime_config_override,
        try_load_config=_try_load_config,
        accounts_from_config=accounts_from_config,
        close_advice_config_cls=CloseAdviceConfig,
        schedule_summary_fields=SCHEDULE_SUMMARY_FIELDS,
        global_strategy_fields=GLOBAL_STRATEGY_FIELDS,
    )


def _patch_close_advice(cfg: dict, payload: dict) -> None:
    return _patch_close_advice_impl(cfg, payload)


def _patch_global_strategy(cfg: dict, payload: dict):
    return _patch_global_strategy_impl(cfg, payload, global_strategy_fields=GLOBAL_STRATEGY_FIELDS)


def _patch_notifications(cfg: dict, payload: dict) -> None:
    return _patch_notifications_impl(cfg, payload, notification_numeric_fields=NOTIFICATION_NUMERIC_FIELDS)


def _apply_market_data(cfg: dict, payload: dict) -> None:
    return _apply_market_data_patch(cfg, payload)


def _clean_symbol_level_strategy_fields(cfg: dict) -> None:
    return _clean_symbol_level_strategy_fields_impl(cfg, forbidden_fields=SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS)


def _account_rows(config_key: str) -> list[dict[str, Any]]:
    return _account_rows_impl(
        config_key,
        load_config=_load_config,
        resolve_config_path=_resolve_config_path,
        config_files=CONFIG_FILES,
        load_data_config_for_runtime=lambda cfg, *, config_path: _load_data_config_for_runtime_impl(cfg, config_path=config_path),
        list_account_config_views=list_account_config_views,
    )


def _tool_result_snapshot(config_key: str) -> dict[str, Any]:
    return _tool_result_snapshot_impl(config_key, base_dir=BASE_DIR)


def _repair_hint_from_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    return _repair_hint_from_error_impl(error)


def _patch_entry(entry: dict, payload: dict):
    return _patch_entry_impl(entry, payload, forbidden_fields=SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS)


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(
        str(static_dir / "index.html"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/watchlist")
def api_list_watchlist():
    return {"rows": _list_rows()}


@app.get("/api/version/check")
def api_version_check():
    return _check_version_update_impl(base_dir=BASE_DIR)


@app.get("/api/accounts")
def api_list_accounts(configKey: str):
    key = str(configKey or "").strip().lower()
    if key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    return {"rows": _account_rows(key)}


@app.get("/api/configs/summary")
def api_configs_summary():
    return {"configs": {k: _global_summary(k) for k in ("hk", "us")}}


@app.get("/api/configs/editor")
def api_configs_editor(configKey: str):
    key = str(configKey or "").strip().lower()
    if key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    cfg = _load_config(key)
    config_path = _resolve_config_path(CONFIG_FILES[key])
    summary = _global_summary(key)
    return {"editor": _build_editor_summary(cfg, config_key=key, config_path=config_path, summary=summary)}


@app.post("/api/configs/global/update")
async def api_update_global_config(req: Request):
    _require_token_for_write_impl(req)
    payload = await req.json()

    config_key = str(payload.get("configKey") or "").strip().lower()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")

    cfg = _load_config(config_key)
    market_data_payload = payload.get("marketData")
    if market_data_payload is not None:
        _apply_market_data(cfg, market_data_payload)
    _patch_global_strategy(cfg, payload)
    _patch_notifications(cfg, payload)
    _patch_close_advice(cfg, payload)
    notifications_payload = payload.get("notifications") if isinstance(payload.get("notifications"), dict) else None
    if notifications_payload is not None and ("appId" in notifications_payload or "appSecret" in notifications_payload):
        _write_notification_credentials(
            cfg,
            config_path=_resolve_config_path(CONFIG_FILES[config_key]),
            app_id=str(notifications_payload.get("appId") or "").strip(),
            app_secret=str(notifications_payload.get("appSecret") or "").strip(),
            secrets_file=str(notifications_payload.get("secretsFile") or "").strip() or None,
        )
    _clean_symbol_level_strategy_fields(cfg)

    path = _resolve_config_path(CONFIG_FILES[config_key])
    _write_validated_config(path, cfg, base_dir=BASE_DIR)

    return {"ok": True, "configs": {k: _global_summary(k) for k in ("hk", "us")}}


@app.post("/api/accounts/upsert")
async def api_upsert_account(req: Request):
    _require_token_for_write_impl(req)
    payload = await req.json()

    config_key = str(payload.get("configKey") or "").strip().lower()
    normalized = _account_payload_defaults(payload, config_key=config_key)
    account_label = normalized["accountLabel"]
    account_type = normalized["accountType"]
    futu_acc_id = normalized["futuAccId"]
    holdings_account = normalized["holdingsAccount"]
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
                market_label=normalized["market"],
                enabled=normalized["enabled"],
                trade_intake_enabled=normalized["tradeIntakeEnabled"],
                futu_host=str((normalized["futu"] or {}).get("host") or "").strip() or None,
                futu_port=(normalized["futu"] or {}).get("port"),
                bitable_app_token=str((normalized["bitable"] or {}).get("app_token") or "").strip() or None,
                bitable_table_id=str((normalized["bitable"] or {}).get("table_id") or "").strip() or None,
                bitable_view_name=str((normalized["bitable"] or {}).get("view_name") or "").strip() or None,
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
                market_label=normalized["market"],
                enabled=normalized["enabled"],
                trade_intake_enabled=normalized["tradeIntakeEnabled"],
                futu_host=str((normalized["futu"] or {}).get("host") or "").strip() or None,
                futu_port=(normalized["futu"] or {}).get("port"),
                bitable_app_token=str((normalized["bitable"] or {}).get("app_token") or "").strip() or None,
                bitable_table_id=str((normalized["bitable"] or {}).get("table_id") or "").strip() or None,
                bitable_view_name=str((normalized["bitable"] or {}).get("view_name") or "").strip() or None,
            )
    except Exception as exc:
        detail = getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)
        raise HTTPException(status_code=400, detail=detail)

    return {"ok": True, "result": result, "rows": _account_rows(config_key), "configs": {k: _global_summary(k) for k in ("hk", "us")}}


@app.post("/api/accounts/delete")
async def api_delete_account(req: Request):
    _require_token_for_write_impl(req)
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
    _append_webui_tool_execution_audit_impl(config_key=str(input_payload.get("config_key") or config_key or "us"), tool_name=tool_name, result=result, base_dir=BASE_DIR, state_repo=state_repo)
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
    return _history_snapshot_impl(config_key=key, limit=max(1, min(int(limit), 100)), base_dir=BASE_DIR, state_repo=state_repo, read_json_file=_read_json_file_impl)


@app.post("/api/notifications/check")
async def api_notifications_check(req: Request):
    payload = await req.json()
    config_key = str(payload.get("configKey") or "").strip().lower()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    cfg = _load_config(config_key)
    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    from src.application.webui_presenters import output_root
    output_root = output_root(BASE_DIR)
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
    from src.application.webui_presenters import output_root

    reports_dir = (output_root(BASE_DIR) / "reports").resolve()
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
    _require_token_for_write_impl(req)
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
    _require_token_for_write_impl(req)
    payload = await req.json()

    config_key = str(payload.get("configKey") or "").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    cfg = _load_config(config_key)
    symbols = _ensure_symbols_list_impl(cfg)

    idx, entry = _find_symbol_impl(cfg, symbol)
    if entry is None:
        entry = {"symbol": symbol, "sell_put": {"enabled": False}, "sell_call": {"enabled": False}}
        symbols.append(entry)
    else:
        # idx should exist
        pass

    _patch_entry(entry, payload)
    _clean_symbol_level_strategy_fields(cfg)

    path = _resolve_config_path(CONFIG_FILES[config_key])
    _write_validated_config(path, cfg, base_dir=BASE_DIR)

    return {"ok": True, "rows": _list_rows()}


@app.post("/api/watchlist/delete")
async def api_delete(req: Request):
    _require_token_for_write_impl(req)
    payload = await req.json()

    config_key = str(payload.get("configKey") or "").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().upper()
    if config_key not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="configKey must be us|hk")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    cfg = _load_config(config_key)
    symbols = _ensure_symbols_list_impl(cfg)

    idx, _entry = _find_symbol_impl(cfg, symbol)
    if idx is None:
        return {"ok": True, "rows": _list_rows()}

    symbols.pop(idx)

    path = _resolve_config_path(CONFIG_FILES[config_key])
    _write_validated_config(path, cfg, base_dir=BASE_DIR)

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
