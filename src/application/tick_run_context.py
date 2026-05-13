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


def build_tick_idempotency_context(
    *,
    cfg_path: Path,
    market_config: str,
    accounts: list[str],
    now_utc: datetime | None = None,
) -> TickIdempotencyContext:
    market_cfg = str(market_config or "auto").strip().lower()
    effective_now = now_utc or datetime.now(timezone.utc)
    bucket = effective_now.strftime("%Y%m%dT%H%M")
    idempotency_accounts = [
        str(a).strip().lower()
        for a in (accounts or [])
        if str(a).strip()
    ]
    key = sha256(
        (
            f"{Path(cfg_path).resolve()}|{market_cfg}|"
            f"{','.join(sorted(idempotency_accounts))}|"
            f"{bucket}"
        ).encode("utf-8")
    ).hexdigest()
    return TickIdempotencyContext(
        bucket=bucket,
        key=key,
        market_config=market_cfg,
        accounts=idempotency_accounts,
    )


def complete_tick_idempotency(
    *,
    base: Path,
    key: str,
    run_id: str,
    market_config: str,
    accounts: list[str],
    status: str = "completed",
    message: str | None = None,
    write_record_fn=state_repo.write_idempotency_record,
) -> None:
    payload: dict[str, Any] = {
        "ok": True,
        "status": status,
        "run_id": run_id,
        "market_config": market_config,
        "accounts": list(accounts),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if message:
        payload["message"] = message
    write_record_fn(
        base,
        scope="tick_execution",
        key=key,
        payload=payload,
    )
