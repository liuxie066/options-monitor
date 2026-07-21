from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from domain.storage.repositories import state_repo


@dataclass(frozen=True)
class TickIdempotencyContext:
    bucket: str
    key: str
    market_config: str
    accounts: list[str]
    trigger_kind: str


def _normalize_trigger_kind(value: str | None) -> str:
    trigger_kind = str(value or "manual").strip().lower()
    if trigger_kind not in {"scheduled", "manual", "force"}:
        raise ValueError(f"unsupported tick trigger kind: {trigger_kind}")
    return trigger_kind


def build_tick_idempotency_context(
    *,
    cfg_path: Path,
    market_config: str,
    accounts: list[str],
    trigger_kind: str = "manual",
    now_utc: datetime | None = None,
) -> TickIdempotencyContext:
    market_cfg = str(market_config or "auto").strip().lower()
    normalized_trigger_kind = _normalize_trigger_kind(trigger_kind)
    effective_now = now_utc or datetime.now(timezone.utc)
    bucket = effective_now.strftime("%Y%m%dT%H%M")
    idempotency_accounts: list[str] = []
    for account in accounts or []:
        account_id = str(account).strip().lower()
        if account_id:
            idempotency_accounts.append(account_id)

    key = sha256(
        (
            f"{Path(cfg_path).resolve()}|{market_cfg}|{normalized_trigger_kind}|"
            f"{','.join(sorted(idempotency_accounts))}|"
            f"{bucket}"
        ).encode("utf-8")
    ).hexdigest()
    return TickIdempotencyContext(
        bucket=bucket,
        key=key,
        market_config=market_cfg,
        accounts=idempotency_accounts,
        trigger_kind=normalized_trigger_kind,
    )


def complete_tick_idempotency(
    *,
    base: Path,
    key: str,
    run_id: str,
    market_config: str,
    accounts: list[str],
    trigger_kind: str = "manual",
    status: str = "completed",
    message: str | None = None,
    ok: bool = True,
    error_code: str | None = None,
    write_record_fn=state_repo.write_idempotency_record,
) -> None:
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "status": status,
        "run_id": run_id,
        "market_config": market_config,
        "accounts": list(accounts),
        "trigger_kind": _normalize_trigger_kind(trigger_kind),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if message:
        payload["message"] = message
    if error_code:
        payload["error_code"] = str(error_code)
    write_record_fn(
        base,
        scope="tick_execution",
        key=key,
        payload=payload,
    )
