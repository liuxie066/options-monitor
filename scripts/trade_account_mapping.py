from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.account_config import ACCOUNT_TYPE_FUTU, accounts_from_config, normalize_accounts, resolve_account_type


def resolve_trade_intake_config(
    cfg: dict[str, Any] | None,
    *,
    mode_override: str | None = None,
    state_path_override: str | Path | None = None,
    audit_path_override: str | Path | None = None,
) -> dict[str, Any]:
    src = cfg if isinstance(cfg, dict) else {}
    section = src.get("trade_intake")
    ti = dict(section) if isinstance(section, dict) else {}

    mode = str(mode_override or ti.get("mode") or "dry-run").strip().lower()
    if mode not in ("dry-run", "apply"):
        raise ValueError("trade_intake.mode must be dry-run or apply")

    enabled = bool(ti.get("enabled", True))
    reconnect_sec = int(ti.get("reconnect_sec", 5) or 5)
    if reconnect_sec <= 0:
        raise ValueError("trade_intake.reconnect_sec must be > 0")

    state_path = Path(state_path_override or ti.get("state_path") or "output/state/auto_trade_intake_state.json")
    audit_path = Path(audit_path_override or ti.get("audit_path") or "output/state/auto_trade_intake_audit.jsonl")

    return {
        "enabled": enabled,
        "mode": mode,
        "state_path": state_path,
        "audit_path": audit_path,
        "reconnect_sec": reconnect_sec,
        "account_mapping": resolve_futu_account_mapping(src),
    }


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


def resolve_recognized_accounts(cfg: dict[str, Any] | None) -> list[str]:
    return normalize_accounts(accounts_from_config(cfg))
