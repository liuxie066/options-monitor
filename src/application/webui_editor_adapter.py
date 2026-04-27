from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from scripts.account_config import (
    account_settings_from_config,
    accounts_from_config,
    cash_footer_accounts_from_config,
)
from src.application.runtime_config_paths import read_json_file, write_json_atomic


def _market_label_from_config_key(config_key: str) -> str:
    return "hk" if str(config_key or "").strip().lower() == "hk" else "us"


def _default_notifications_secrets_file(cfg: dict[str, Any]) -> str:
    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    return str(notifications.get("secrets_file") or "secrets/notifications.feishu.app.json").strip()


def _resolve_relative_to_config(config_path: Path, ref: str) -> Path:
    path = Path(str(ref).strip()).expanduser()
    if not path.is_absolute():
        path = (config_path.parent / path).resolve()
    return path


def _allowed_secrets_root(config_path: Path) -> Path:
    return (config_path.parent / "secrets").resolve()


def _ensure_safe_secrets_ref(config_path: Path, ref: str) -> tuple[str, Path]:
    raw = str(ref or "").strip() or "secrets/notifications.feishu.app.json"
    candidate = Path(raw)
    if candidate.is_absolute():
        raise HTTPException(status_code=400, detail="notifications.secretsFile must stay under repo secrets/")
    path = _resolve_relative_to_config(config_path, raw)
    secrets_root = _allowed_secrets_root(config_path)
    try:
        path.relative_to(secrets_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="notifications.secretsFile must stay under repo secrets/") from exc
    normalized_ref = str(path.relative_to(config_path.parent))
    return normalized_ref, path


def load_notification_credentials(cfg: dict[str, Any], *, config_path: Path) -> dict[str, str]:
    ref = _default_notifications_secrets_file(cfg)
    try:
        safe_ref, path = _ensure_safe_secrets_ref(config_path, ref)
    except HTTPException:
        safe_ref = "secrets/notifications.feishu.app.json"
        path = _resolve_relative_to_config(config_path, safe_ref)
    payload = read_json_file(path)
    if not isinstance(payload, dict):
        return {"app_id": "", "app_secret": "", "secrets_file": safe_ref}
    feishu = payload.get("feishu") if isinstance(payload.get("feishu"), dict) else {}
    return {
        "app_id": str(feishu.get("app_id") or "").strip(),
        "app_secret": str(feishu.get("app_secret") or "").strip(),
        "secrets_file": safe_ref,
    }


def write_notification_credentials(
    cfg: dict[str, Any],
    *,
    config_path: Path,
    app_id: str,
    app_secret: str,
    secrets_file: str | None = None,
) -> str:
    ref, path = _ensure_safe_secrets_ref(
        config_path,
        str(secrets_file or _default_notifications_secrets_file(cfg)).strip() or "secrets/notifications.feishu.app.json",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        path,
        {
            "feishu": {
                "app_id": str(app_id or "").strip(),
                "app_secret": str(app_secret or "").strip(),
            }
        },
    )
    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    notifications["secrets_file"] = ref
    cfg["notifications"] = notifications
    return ref


def _shared_fetch_host_port(symbols: list[dict[str, Any]]) -> tuple[str, int | None]:
    for item in symbols:
        if not isinstance(item, dict):
            continue
        fetch = item.get("fetch") if isinstance(item.get("fetch"), dict) else {}
        host = str(fetch.get("host") or "").strip()
        port = fetch.get("port")
        if host or port not in (None, ""):
            try:
                parsed_port = int(port) if port not in (None, "") else None
            except Exception:
                parsed_port = None
            return host, parsed_port
    return "", None


def build_editor_summary(
    cfg: dict[str, Any],
    *,
    config_key: str,
    config_path: Path,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbols = cfg.get("symbols") if isinstance(cfg.get("symbols"), list) else []
    account_settings = account_settings_from_config(cfg)
    host, port = _shared_fetch_host_port(symbols)
    credentials = load_notification_credentials(cfg, config_path=config_path)
    notifications = cfg.get("notifications") if isinstance(cfg.get("notifications"), dict) else {}
    quiet_hours = notifications.get("quiet_hours_beijing") if isinstance(notifications.get("quiet_hours_beijing"), dict) else {}
    return {
        "configKey": config_key,
        "marketData": {
            "source": "OpenD",
            "host": host,
            "port": port,
            "mode": "compat_global",
        },
        "accounts": [
            {
                "accountLabel": account,
                "market": str(item.get("market") or _market_label_from_config_key(config_key)).upper(),
                "enabled": item.get("enabled", True) is not False,
                "accountType": str(item.get("type") or "futu"),
                "tradeIntakeEnabled": item.get("trade_intake_enabled", str(item.get("type") or "futu") == "futu") is not False,
                "holdingsAccount": str(item.get("holdings_account") or ""),
                "futu": {
                    key: value
                    for key, value in (dict(item.get("futu") or {}) if isinstance(item.get("futu"), dict) else {}).items()
                    if key in {"host", "port", "account_id"}
                },
                "bitable": {
                    "table_id": str((item.get("bitable") or {}).get("table_id") or "") if isinstance(item.get("bitable"), dict) else "",
                    "view_name": str((item.get("bitable") or {}).get("view_name") or "") if isinstance(item.get("bitable"), dict) else "",
                    "hasAppToken": bool(str((item.get("bitable") or {}).get("app_token") or "")) if isinstance(item.get("bitable"), dict) else False,
                },
            }
            for account, item in account_settings.items()
        ],
        "marketAccounts": accounts_from_config(cfg),
        "notifications": {
            "channel": str(notifications.get("channel") or "feishu"),
            "target": str(notifications.get("target") or ""),
            "appId": "",
            "appSecret": "",
            "secretsFile": credentials["secrets_file"],
            "hasCredentials": bool(credentials["app_id"] and credentials["app_secret"]),
            "includeCashFooter": notifications.get("include_cash_footer", True) is not False,
            "cashFooterAccounts": cash_footer_accounts_from_config(cfg),
            "quietHoursStart": str(quiet_hours.get("start") or ""),
            "quietHoursEnd": str(quiet_hours.get("end") or ""),
        },
        "summary": summary or {},
    }


def apply_market_data_patch(cfg: dict[str, Any], payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="marketData must be an object")
    host = str(payload.get("host") or "").strip()
    port = payload.get("port")
    source = str(payload.get("source") or "OpenD").strip() or "OpenD"
    legacy_source = "futu"
    market_data = cfg.get("market_data") if isinstance(cfg.get("market_data"), dict) else {}
    market_data["source"] = source
    if host:
        market_data["host"] = host
    else:
        market_data.pop("host", None)
    if port in (None, ""):
        market_data.pop("port", None)
        parsed_port = None
    else:
        try:
            parsed_port = int(port)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="marketData.port must be an integer") from exc
        market_data["port"] = parsed_port
    cfg["market_data"] = market_data

    symbols = cfg.get("symbols") if isinstance(cfg.get("symbols"), list) else []
    for item in symbols:
        if not isinstance(item, dict):
            continue
        fetch = item.get("fetch") if isinstance(item.get("fetch"), dict) else {}
        fetch["source"] = legacy_source
        if host:
            fetch["host"] = host
        elif "host" in fetch:
            fetch.pop("host", None)
        if parsed_port is not None:
            fetch["port"] = parsed_port
        elif "port" in fetch:
            fetch.pop("port", None)
        item["fetch"] = fetch


def account_payload_defaults(payload: dict[str, Any], *, config_key: str) -> dict[str, Any]:
    account_type = str(payload.get("accountType") or "futu").strip().lower() or "futu"
    market = str(payload.get("market") or _market_label_from_config_key(config_key)).strip().lower()
    return {
        "accountLabel": str(payload.get("accountLabel") or "").strip().lower(),
        "accountType": account_type,
        "market": market if market in {"us", "hk"} else _market_label_from_config_key(config_key),
        "enabled": bool(payload.get("enabled", True)),
        "tradeIntakeEnabled": bool(
            payload.get("tradeIntakeEnabled", account_type == "futu")
        ),
        "holdingsAccount": str(payload.get("holdingsAccount") or "").strip() or None,
        "futu": payload.get("futu") if isinstance(payload.get("futu"), dict) else {},
        "bitable": payload.get("bitable") if isinstance(payload.get("bitable"), dict) else {},
        "futuAccId": str(payload.get("futuAccId") or "").strip() or None,
    }
