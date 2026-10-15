from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from domain.storage.repositories import state_repo
from domain.storage.json_io import atomic_write_private_json


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
    symbols: str | None = None,
    no_send: bool = False,
    experience: bool = False,
    trigger_job_id: str | None = None,
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
    work_scope = "|".join(
        (
            str(symbols or "").strip().upper(),
            "no_send" if no_send else "send",
            "experience" if experience else "standard",
            str(trigger_job_id or "").strip(),
        )
    )

    key = sha256(
        (
            f"{Path(cfg_path).resolve()}|{market_cfg}|{normalized_trigger_kind}|"
            f"{','.join(sorted(idempotency_accounts))}|"
            f"{work_scope}|"
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
    account_outcomes: list[dict[str, Any]] | None = None,
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
    if account_outcomes:
        payload["account_outcomes"] = account_outcomes
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


def tick_account_outcomes(tick_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    notify_failed = {item.get("account") for item in tick_metrics.get("notify_failures") or []}
    return [{
        "account": account, "scan_ok": reliable,
        "notify_failed": account in notify_failed,
        "failure_notification_confirmed": account in (tick_metrics.get("scan_failure_notified_accounts") or []),
    } for account, reliable in sorted((tick_metrics.get("scan_outcomes") or {}).items())]


def write_child_tick_completion(*, base: Path, wrapper_run_id: str, inner_run_id: str,
                                market: str, accounts: list[str], account_outcomes: list[dict[str, Any]], rc: int) -> None:
    atomic_write_private_json(
        state_repo.run_state_dir(base, wrapper_run_id) / "child_tick_completion.json",
        {"status": "ok" if rc == 0 else "failed", "wrapper_run_id": wrapper_run_id, "inner_run_id": inner_run_id,
         "market": market, "accounts": sorted(set(accounts)), "rc": rc, "account_outcomes": account_outcomes,
         "completed_at_utc": datetime.now(timezone.utc).isoformat()},
    )
