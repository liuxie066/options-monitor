from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


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


def normalize_symbol_for_name_lookup(value: Any) -> str:
    return str(value or "").strip().upper()


def symbol_name_from_aliases(symbol: str, cfg: dict | None) -> str | None:
    if not isinstance(cfg, dict):
        return None
    intake = cfg.get("intake") if isinstance(cfg.get("intake"), dict) else {}
    aliases = intake.get("symbol_aliases") if isinstance(intake.get("symbol_aliases"), dict) else {}
    target = normalize_symbol_for_name_lookup(symbol)
    for alias, alias_symbol in aliases.items():
        if normalize_symbol_for_name_lookup(alias_symbol) == target:
            name = str(alias).strip()
            if name:
                return name
    return None


def to_row(config_key: str, item: dict, cfg: dict | None = None) -> SymbolRow:
    fetch = item.get("fetch") or {}
    sp = item.get("sell_put") or {}
    sc = item.get("sell_call") or {}
    symbol = str(item.get("symbol") or "")
    name = item.get("name") or item.get("display_name") or item.get("symbol_name")
    if name is None or not str(name).strip():
        name = symbol_name_from_aliases(symbol, cfg)
    return SymbolRow(
        configKey=config_key,  # type: ignore[arg-type]
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


def list_rows(*, config_keys: tuple[str, ...], try_load_config, to_row_fn) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in config_keys:
        cfg, _err = try_load_config(key)
        if cfg is None:
            continue
        symbols = cfg.get("symbols") or []
        if not isinstance(symbols, list):
            continue
        for item in symbols:
            if not isinstance(item, dict):
                continue
            rows.append(to_row_fn(key, item, cfg).__dict__)
    rows.sort(key=lambda row: (row.get("configKey") or "", row.get("market") or "", row.get("symbol") or ""))
    return rows


def global_summary(
    config_key: str,
    *,
    config_files: dict[str, Path],
    resolve_config_path,
    recommended_runtime_config_path,
    uses_runtime_config_override,
    try_load_config,
    accounts_from_config,
    close_advice_config_cls,
    schedule_summary_fields,
    global_strategy_fields,
) -> dict[str, Any]:
    path = resolve_config_path(config_files[config_key])
    recommended_path = recommended_runtime_config_path(config_key)
    path_warning = recommended_path.exists() and path != recommended_path and not uses_runtime_config_override(config_key)
    cfg, err = try_load_config(config_key)
    if cfg is None:
        return {
            "configKey": config_key,
            "path": str(config_files[config_key]),
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
    schedule = {key: raw_schedule.get(key) for key in schedule_summary_fields if key in raw_schedule}
    templates = cfg.get("templates") if isinstance(cfg.get("templates"), dict) else {}
    close_advice = cfg.get("close_advice") if isinstance(cfg.get("close_advice"), dict) else {}
    resolved_close_advice = close_advice_config_cls.from_mapping(close_advice)
    put_template = templates.get("put_base") if isinstance(templates.get("put_base"), dict) else {}
    call_template = templates.get("call_base") if isinstance(templates.get("call_base"), dict) else {}
    put_strategy = put_template.get("sell_put") if isinstance(put_template.get("sell_put"), dict) else {}
    call_strategy = call_template.get("sell_call") if isinstance(call_template.get("sell_call"), dict) else {}
    return {
        "configKey": config_key,
        "path": str(config_files[config_key]),
        "resolvedPath": str(path),
        "recommendedPath": str(recommended_path),
        "recommendedPathExists": recommended_path.exists(),
        "canonicalPathWarning": path_warning,
        "exists": True,
        "accounts": accounts_from_config(cfg),
        "symbolCount": len(symbols),
        "enabledSymbolCount": sum(1 for item in symbols if isinstance(item, dict) and (bool((item.get("sell_put") or {}).get("enabled")) or bool((item.get("sell_call") or {}).get("enabled")))),
        "sections": {
            "schedule": schedule,
            "notifications": {k: v for k, v in {
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
            }.items() if v is not None},
            "templates": sorted(templates.keys()),
            "outputs": cfg.get("outputs") if isinstance(cfg.get("outputs"), dict) else {},
            "runtime": cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {},
            "alert_policy": cfg.get("alert_policy") if isinstance(cfg.get("alert_policy"), dict) else {},
            "fetch_policy": cfg.get("fetch_policy") if isinstance(cfg.get("fetch_policy"), dict) else {},
            "portfolio": cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {},
            "close_advice": {k: v for k, v in {
                "enabled": close_advice.get("enabled"),
                "quote_source": close_advice.get("quote_source"),
                "notify_levels": close_advice.get("notify_levels"),
                "max_items_per_account": close_advice.get("max_items_per_account"),
                "max_spread_ratio": resolved_close_advice.max_spread_ratio,
                "strong_remaining_annualized_max": resolved_close_advice.strong_remaining_annualized_max,
                "medium_remaining_annualized_max": resolved_close_advice.medium_remaining_annualized_max,
            }.items() if v is not None},
        },
        "globalStrategy": {
            "sell_put": {k: put_strategy.get(k) for k in global_strategy_fields},
            "sell_call": {k: call_strategy.get(k) for k in global_strategy_fields},
        },
    }


def mask_acc_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return f"...{raw[-4:]}"
    return raw


def account_rows(config_key: str, *, load_config, resolve_config_path, config_files, load_data_config_for_runtime, list_account_config_views, account_type_external_holdings: str) -> list[dict[str, Any]]:
    cfg = load_config(config_key)
    config_path = resolve_config_path(config_files[config_key])
    data_cfg = load_data_config_for_runtime(cfg, config_path=config_path)
    feishu_cfg = data_cfg.get("feishu") if isinstance(data_cfg.get("feishu"), dict) else {}
    feishu_tables = feishu_cfg.get("tables") if isinstance(feishu_cfg.get("tables"), dict) else {}
    feishu_ready = bool(str(feishu_cfg.get("app_id") or "").strip()) and bool(str(feishu_cfg.get("app_secret") or "").strip())
    holdings_table_ready = bool(str(feishu_tables.get("holdings") or "").strip())
    holdings_ready = feishu_ready and holdings_table_ready
    rows: list[dict[str, Any]] = []
    for account_view in list_account_config_views(cfg):
        source_plan = account_view.portfolio_source_plan
        account_type = account_view.account_type
        futu_acc_ids = [mask_acc_id(acc_id) for acc_id in account_view.futu_acc_ids]
        fallback_enabled = bool(source_plan.fallback_source) or account_type == account_type_external_holdings
        primary_ready = bool(futu_acc_ids) if source_plan.primary_source == "futu" else bool(holdings_ready)
        fallback_ready = bool(holdings_ready) if fallback_enabled else None
        rows.append(
            AccountRow(
                configKey=config_key,  # type: ignore[arg-type]
                account_label=account_view.account,
                account_type=account_type,
                futu_acc_ids=futu_acc_ids,
                holdings_account=(str(source_plan.holdings_account or "").strip() or None),
                portfolio_source=source_plan.requested_source,
                primary_source=source_plan.primary_source,
                primary_ready=primary_ready,
                fallback_enabled=fallback_enabled,
                fallback_source=(source_plan.fallback_source or ("holdings" if fallback_enabled else None)),
                fallback_ready=fallback_ready,
            ).__dict__
        )
    return rows


def output_root(base_dir: Path) -> Path:
    return (base_dir / "output" / "agent_plugin").resolve()


def report_text(path: Path, *, max_chars: int = 12000) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if len(text) > max_chars:
        return text[:max_chars] + "\n...[truncated]"
    return text


def json_report(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def tool_result_snapshot(config_key: str, *, base_dir: Path) -> dict[str, Any]:
    root = output_root(base_dir)
    reports_dir = (root / "reports").resolve()
    state_dir = (root / "state").resolve()
    return {
        "configKey": config_key,
        "paths": {"outputRoot": str(root), "reports": str(reports_dir), "state": str(state_dir)},
        "artifacts": {
            "close_advice_text": report_text(reports_dir / "close_advice.txt"),
            "symbols_notification_text": report_text(reports_dir / "symbols_notification.txt"),
            "option_positions_context": json_report(state_dir / "option_positions_context.json"),
        },
    }


def read_jsonl_tail(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
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
    return rows[-int(limit):] if limit > 0 else rows


def tool_execution_status(result: dict[str, Any]) -> tuple[str, bool, str]:
    ok = bool(result.get("ok"))
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    if ok:
        return "fetched", True, "completed"
    return "error", False, str(error.get("message") or "tool failed")


def append_webui_tool_execution_audit(*, config_key: str, tool_name: str, result: dict[str, Any], base_dir: Path, state_repo) -> None:
    started = datetime.now(timezone.utc).isoformat()
    finished = datetime.now(timezone.utc).isoformat()
    status, ok, message = tool_execution_status(result)
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
    root = output_root(base_dir)
    state_repo.append_tool_execution_audit(root, payload)
    state_repo.append_audit_event(
        root,
        {
            "event_type": "webui_tool_run",
            "action": str(tool_name),
            "status": ("ok" if ok else "error"),
            "message": message,
            "tool_name": str(tool_name),
            "extra": {"config_key": str(config_key or "").strip().lower()},
        },
    )


def repair_hint_from_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(error, dict) or not error:
        return None
    code = str(error.get("code") or "").strip().upper()
    message = str(error.get("message") or "").strip()
    hint = str(error.get("hint") or "").strip()
    actions: list[str] = []
    if code == "CONFIG_ERROR":
        actions = ["先运行 healthcheck，确认 runtime config 和 SQLite data config 都存在。", "检查 config 内的 broker/account/data_config 路径是否填对。"]
        if "runtime config not found" in message.lower():
            actions.insert(0, "先运行 om-agent init 生成本地配置，或在 WebUI 指向正确的 runtime config。")
    elif code == "DEPENDENCY_MISSING":
        actions = ["检查 OpenD 是否已启动并登录。", "确认本地依赖和 secrets 文件已经按安装步骤准备完成。"]
    elif code == "PERMISSION_DENIED":
        actions = ["这是受保护动作，默认不会直接执行。", "如果确实需要写操作，先显式开启写工具或改用 dry-run。"]
    elif code == "CONFIRMATION_REQUIRED":
        actions = ["当前动作需要 confirm=true 才会真实执行。", "先做 dry-run，确认输出后再重试。"]
    elif "opend" in message.lower():
        actions = ["确认 OpenD 进程在线，并且当前机器能连到配置里的 host/port。", "确认富途客户端或 OpenD 登录状态正常，再重试 healthcheck。"]
    elif "portfolio.data_config" in message.lower() or "sqlite" in message.lower():
        actions = ["确认 portfolio.data_config 指向本地 SQLite data config。", "检查 position lots 的 SQLite 文件和 secrets 配置是否存在。"]
    if not hint and not actions:
        return None
    return {"code": code or None, "summary": hint or message or "请先修复配置或依赖问题后重试。", "actions": actions}


def history_snapshot(*, config_key: str, limit: int, base_dir: Path, state_repo, read_json_file) -> dict[str, Any]:
    base = output_root(base_dir)
    shared_dir = state_repo.shared_state_dir(base)
    current_dir = state_repo.shared_current_read_model_dir(base)
    tool_rows = state_repo.query_tool_execution_audit(base, limit=limit)
    filtered_tool_rows = [row for row in tool_rows if not row.get("config_key") or str(row.get("config_key")).strip().lower() == str(config_key).strip().lower()]
    audit_rows = read_jsonl_tail(shared_dir / "audit_events.jsonl", limit=limit)
    filtered_audit_rows = [
        row
        for row in audit_rows
        if not ((row.get("extra") if isinstance(row.get("extra"), dict) else {}).get("config_key"))
        or str((row.get("extra") if isinstance(row.get("extra"), dict) else {}).get("config_key")).strip().lower() == str(config_key).strip().lower()
    ]
    tick_metrics = read_json_file(shared_dir / "tick_metrics_history.json")
    if not isinstance(tick_metrics, list):
        tick_metrics = []
    last_run = read_json_file(current_dir / "last_run.current.json")
    latest_audit = read_json_file(current_dir / "audit_event_latest.current.json")
    return {
        "toolExecutions": filtered_tool_rows,
        "auditEvents": filtered_audit_rows,
        "tickMetrics": tick_metrics[-int(limit):],
        "lastRun": last_run if isinstance(last_run, dict) else None,
        "latestAudit": latest_audit if isinstance(latest_audit, dict) else None,
    }
