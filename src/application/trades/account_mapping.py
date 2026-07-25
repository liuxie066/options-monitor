from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.account_config import (
    ACCOUNT_TYPE_FUTU,
    account_settings_from_config,
    accounts_from_config,
    build_account_runtime_plan,
    normalize_accounts,
    resolve_account_trade_intake_enabled,
    resolve_account_type,
)


def resolve_trade_intake_config(
    cfg: dict[str, Any] | None,
    *,
    mode_override: str | None = None,
    state_path_override: str | Path | None = None,
    audit_path_override: str | Path | None = None,
    status_path_override: str | Path | None = None,
) -> dict[str, Any]:
    src = cfg if isinstance(cfg, dict) else {}
    section = src.get("trade_intake")
    ti = dict(section) if isinstance(section, dict) else {}

    mode = str(mode_override or ti.get("mode") or "dry-run").strip().lower()
    if mode not in ("dry-run", "apply"):
        raise ValueError("trade_intake.mode must be dry-run or apply")

    enabled = _bool_from_config(ti, "enabled", default=True, section="")
    reconnect_sec = int(ti.get("reconnect_sec", 5) or 5)
    if reconnect_sec <= 0:
        raise ValueError("trade_intake.reconnect_sec must be > 0")

    state_path = Path(state_path_override or ti.get("state_path") or "output_shared/state/auto_trade_intake_state.json")
    audit_path = Path(audit_path_override or ti.get("audit_path") or "output_shared/state/auto_trade_intake_audit.jsonl")
    status_path = Path(status_path_override or ti.get("status_path") or "output_shared/state/auto_trade_intake_status.json")
    receipt_cfg = resolve_trade_intake_receipt_config(ti.get("receipt"))
    backfill_cfg = resolve_trade_intake_backfill_config(ti.get("backfill"))
    holdings_sync_cfg = resolve_trade_intake_holdings_sync_config(ti.get("holdings_sync"))
    account_mapping = resolve_futu_account_mapping(src)
    futu_lookup_account_ids = resolve_futu_lookup_account_ids(src, account_mapping=account_mapping)
    sources = resolve_trade_intake_sources(
        src,
        mode=mode,
        enabled=enabled,
        receipt=receipt_cfg,
        backfill=backfill_cfg,
        reconnect_sec=reconnect_sec,
        fallback_state_path=state_path,
        fallback_audit_path=audit_path,
        fallback_status_path=status_path,
        fallback_account_mapping=account_mapping,
        fallback_futu_account_ids=futu_lookup_account_ids,
    )

    return {
        "enabled": enabled,
        "mode": mode,
        "state_path": state_path,
        "audit_path": audit_path,
        "status_path": status_path,
        "reconnect_sec": reconnect_sec,
        "receipt": receipt_cfg,
        "backfill": backfill_cfg,
        "holdings_sync": holdings_sync_cfg,
        "account_mapping": account_mapping,
        "futu_account_ids": futu_lookup_account_ids,
        "sources": sources,
    }


def resolve_trade_intake_receipt_config(value: Any) -> dict[str, bool]:
    if value is None:
        src: dict[str, Any] = {}
    elif isinstance(value, dict):
        src = value
    else:
        raise ValueError("trade_intake.receipt must be an object")
    return {
        "enabled": _bool_from_config(src, "enabled", default=True, section="receipt"),
        "notify_applied": _bool_from_config(src, "notify_applied", default=True, section="receipt"),
        "notify_unresolved": _bool_from_config(src, "notify_unresolved", default=True, section="receipt"),
        "notify_failed": _bool_from_config(src, "notify_failed", default=True, section="receipt"),
        "notify_duplicate": _bool_from_config(src, "notify_duplicate", default=False, section="receipt"),
        "retry_unconfirmed_duplicate": _bool_from_config(src, "retry_unconfirmed_duplicate", default=True, section="receipt"),
    }


def resolve_trade_intake_backfill_config(value: Any) -> dict[str, Any]:
    if value is None:
        src: dict[str, Any] = {}
    elif isinstance(value, dict):
        src = value
    else:
        raise ValueError("trade_intake.backfill must be an object")

    enabled = _bool_from_config(src, "enabled", default=True, section="backfill")
    startup_check = _bool_from_config(src, "startup_check", default=True, section="backfill")
    interval_sec = int(src.get("interval_sec", 300) or 300)
    lookback_hours = float(src.get("lookback_hours", 6) or 6)
    if interval_sec <= 0:
        raise ValueError("trade_intake.backfill.interval_sec must be > 0")
    if lookback_hours <= 0:
        raise ValueError("trade_intake.backfill.lookback_hours must be > 0")
    return {
        "enabled": enabled,
        "startup_check": startup_check,
        "interval_sec": interval_sec,
        "lookback_hours": lookback_hours,
    }


def resolve_trade_intake_holdings_sync_config(value: Any) -> dict[str, Any]:
    if value is None:
        src: dict[str, Any] = {}
    elif isinstance(value, dict):
        src = value
    else:
        raise ValueError("trade_intake.holdings_sync must be an object")

    enabled = _bool_from_config(
        src,
        "enabled",
        default=False,
        section="holdings_sync",
    )
    debounce_sec = float(src.get("debounce_sec", 2.0))
    request_timeout_sec = float(src.get("request_timeout_sec", 120.0))
    max_attempts = int(src.get("max_attempts", 3))
    retry_backoff_sec = float(src.get("retry_backoff_sec", 2.0))
    queue_capacity = int(src.get("queue_capacity", 100))
    recent_deal_limit = int(src.get("recent_deal_limit", 2000))
    state_dir = Path(
        src.get("state_dir")
        or "output_shared/state/trade_intake/stock_holdings_sync"
    )
    if debounce_sec < 0:
        raise ValueError("trade_intake.holdings_sync.debounce_sec must be >= 0")
    if request_timeout_sec <= 0:
        raise ValueError(
            "trade_intake.holdings_sync.request_timeout_sec must be > 0"
        )
    if max_attempts <= 0:
        raise ValueError("trade_intake.holdings_sync.max_attempts must be > 0")
    if retry_backoff_sec < 0:
        raise ValueError(
            "trade_intake.holdings_sync.retry_backoff_sec must be >= 0"
        )
    if queue_capacity <= 0:
        raise ValueError(
            "trade_intake.holdings_sync.queue_capacity must be > 0"
        )
    if recent_deal_limit <= 0:
        raise ValueError(
            "trade_intake.holdings_sync.recent_deal_limit must be > 0"
        )
    return {
        "enabled": enabled,
        "debounce_sec": debounce_sec,
        "request_timeout_sec": request_timeout_sec,
        "max_attempts": max_attempts,
        "retry_backoff_sec": retry_backoff_sec,
        "queue_capacity": queue_capacity,
        "recent_deal_limit": recent_deal_limit,
        "state_dir": state_dir,
    }


def _bool_from_config(src: dict[str, Any], key: str, *, default: bool, section: str) -> bool:
    value = src.get(key, default)
    if not isinstance(value, bool):
        path = f"trade_intake.{section}.{key}" if section else f"trade_intake.{key}"
        raise ValueError(f"{path} must be a boolean")
    return bool(value)


def resolve_futu_account_mapping(cfg: dict[str, Any] | None) -> dict[str, str]:
    src = cfg if isinstance(cfg, dict) else {}
    ti = src.get("trade_intake")
    ti = ti if isinstance(ti, dict) else {}
    mapping_root = ti.get("account_mapping")
    mapping_root = mapping_root if isinstance(mapping_root, dict) else {}
    futu_mapping = mapping_root.get("futu")
    futu_mapping = futu_mapping if isinstance(futu_mapping, dict) else {}

    allowed_accounts = {
        account
        for account in accounts_from_config(src)
        if resolve_account_type(src, account=account) == ACCOUNT_TYPE_FUTU
    }
    out: dict[str, str] = {}
    for raw_key, raw_value in futu_mapping.items():
        key = str(raw_key or "").strip()
        value = str(raw_value or "").strip().lower()
        if not key:
            raise ValueError("trade_intake.account_mapping.futu contains empty account id")
        if not value:
            raise ValueError(f"trade_intake.account_mapping.futu[{key}] must be a non-empty account label")
        if value not in allowed_accounts:
            raise ValueError(
                f"trade_intake.account_mapping.futu[{key}]={value} is not a futu account in top-level accounts/account_settings"
            )
        out[key] = value

    settings = account_settings_from_config(src)
    for account in accounts_from_config(src):
        if resolve_account_type(src, account=account) != ACCOUNT_TYPE_FUTU:
            continue
        if not resolve_account_trade_intake_enabled(src, account=account):
            continue
        futu_cfg = settings.get(account, {}).get("futu") if isinstance(settings.get(account), dict) else None
        if not isinstance(futu_cfg, dict):
            continue
        account_id = str(futu_cfg.get("account_id") or "").strip()
        if account_id:
            out.setdefault(account_id, account)
    return out


def resolve_internal_account(
    futu_account_id: str | None,
    mapping: dict[str, str] | None,
) -> str | None:
    key = str(futu_account_id or "").strip()
    if not key:
        return None
    table = mapping if isinstance(mapping, dict) else {}
    value = table.get(key)
    if value is None:
        return None
    return str(value).strip().lower() or None


def resolve_futu_lookup_account_ids(
    cfg: dict[str, Any] | None,
    *,
    account_mapping: dict[str, str] | None = None,
) -> list[str]:
    src = cfg if isinstance(cfg, dict) else {}
    mapping = account_mapping if isinstance(account_mapping, dict) else resolve_futu_account_mapping(src)
    seen: set[str] = set()
    out: list[str] = []

    for raw_key in mapping.keys():
        key = str(raw_key or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)

    settings = account_settings_from_config(src)
    for account in accounts_from_config(src):
        if resolve_account_type(src, account=account) != ACCOUNT_TYPE_FUTU:
            continue
        if not resolve_account_trade_intake_enabled(src, account=account):
            continue
        futu_cfg = settings.get(account, {}).get("futu") if isinstance(settings.get(account), dict) else None
        if not isinstance(futu_cfg, dict):
            continue
        account_id = str(futu_cfg.get("account_id") or "").strip()
        if account_id and account_id not in seen:
            seen.add(account_id)
            out.append(account_id)
    return out


def resolve_trade_intake_sources(
    cfg: dict[str, Any] | None,
    *,
    mode: str | None = None,
    enabled: bool | None = None,
    receipt: dict[str, Any] | None = None,
    backfill: dict[str, Any] | None = None,
    reconnect_sec: int | None = None,
    fallback_state_path: str | Path | None = None,
    fallback_audit_path: str | Path | None = None,
    fallback_status_path: str | Path | None = None,
    fallback_account_mapping: dict[str, str] | None = None,
    fallback_futu_account_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    src = cfg if isinstance(cfg, dict) else {}
    base_mode = str(mode or "dry-run")
    base_enabled = True if enabled is None else bool(enabled)
    base_receipt = dict(receipt or {})
    base_backfill = dict(backfill or {})
    base_reconnect_sec = int(reconnect_sec or 5)
    base_state_path = Path(fallback_state_path or "output_shared/state/auto_trade_intake_state.json")
    base_audit_path = Path(fallback_audit_path or "output_shared/state/auto_trade_intake_audit.jsonl")
    base_status_path = Path(fallback_status_path or "output_shared/state/auto_trade_intake_status.json")
    base_mapping = dict(fallback_account_mapping or resolve_futu_account_mapping(src))
    base_account_ids = list(fallback_futu_account_ids or resolve_futu_lookup_account_ids(src, account_mapping=base_mapping))

    account_sources: list[dict[str, Any]] = []
    for account in accounts_from_config(src):
        plan = build_account_runtime_plan(src, account=account)
        if plan.account_type != ACCOUNT_TYPE_FUTU or not plan.trade_intake_enabled:
            continue
        if not plan.futu_account_id or not plan.futu_host or not plan.futu_port:
            continue
        account_sources.append(
            {
                "id": account,
                "account": account,
                "enabled": base_enabled,
                "mode": base_mode,
                "host": plan.futu_host,
                "port": int(plan.futu_port),
                "state_path": Path(f"output_shared/state/trade_intake/{account}/state.json"),
                "audit_path": Path(f"output_shared/state/trade_intake/{account}/audit.jsonl"),
                "status_path": Path(f"output_shared/state/trade_intake/{account}/status.json"),
                "reconnect_sec": base_reconnect_sec,
                "receipt": base_receipt,
                "backfill": base_backfill,
                "account_mapping": {plan.futu_account_id: account},
                "futu_account_ids": [plan.futu_account_id],
            }
        )

    if len(account_sources) == 1:
        source = dict(account_sources[0])
        source["state_path"] = base_state_path
        source["audit_path"] = base_audit_path
        source["status_path"] = base_status_path
        return [source]
    if account_sources:
        return account_sources

    return [
        {
            "id": "legacy",
            "account": None,
            "enabled": base_enabled,
            "mode": base_mode,
            "host": "127.0.0.1",
            "port": 11111,
            "state_path": base_state_path,
            "audit_path": base_audit_path,
            "status_path": base_status_path,
            "reconnect_sec": base_reconnect_sec,
            "receipt": base_receipt,
            "backfill": base_backfill,
            "account_mapping": base_mapping,
            "futu_account_ids": base_account_ids,
        }
    ]


def resolve_recognized_accounts(cfg: dict[str, Any] | None) -> list[str]:
    return normalize_accounts(accounts_from_config(cfg))
