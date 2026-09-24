from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.application.account_config import accounts_from_config_path
from src.application.system_alerts import report_system_failure
from src.application.trades.account_mapping import resolve_trade_intake_config
from src.infrastructure.io_utils import read_json
from src.infrastructure.run_log import create_run_id


def alert_failed_service(*, unit: str, market: str, config_path: str, runtime_root: str | Path) -> int:
    config_file = Path(config_path)
    config = read_json(config_file, {})
    if not isinstance(config, dict) or not config:
        print("<3>SERVICE_FAILURE_ALERT_CONFIG_UNAVAILABLE")
        return 1
    try:
        accounts = accounts_from_config_path(config_file, fallback=()) or ["-"]
    except Exception:
        accounts = ["-"]
    base = Path(runtime_root)
    pending = read_json(base / "output_shared" / "state" / "opend_phone_verify_pending.json", {})
    login_state = "phone_verify_pending" if isinstance(pending, dict) and pending.get("pending") else "unknown"
    run_id = create_run_id()
    now = datetime.now(timezone.utc).isoformat()
    reason_by_account: dict[str, str] = {}
    try:
        intake = resolve_trade_intake_config(config)
        for source in intake.get("sources") or []:
            status_path = Path(source["status_path"])
            if not status_path.is_absolute():
                status_path = base / status_path
            status = read_json(status_path, {})
            if isinstance(status, dict) and status.get("status") == "blocked":
                reason = str(status.get("reason_code") or status.get("error_code") or "").strip()
                if reason:
                    reason_by_account[str(source.get("account") or "")] = reason
    except (KeyError, TypeError, ValueError):
        pass
    results = []
    for account in accounts:
        results.append(report_system_failure(
            base=base, config=config, unit=unit, market=market, account=account,
            failure_code=reason_by_account.get(account, "SERVICE_TERMINAL_FAILURE"), stage="unit_failed", run_id=run_id,
            rc=-1, first_error_at=now, opend_login_state=login_state,
        ))
    if all(item in {"confirmed", "suppressed"} for item in results):
        return 0
    print("<3>SERVICE_FAILURE_ALERT_UNCONFIRMED " + ",".join(results))
    return 1
