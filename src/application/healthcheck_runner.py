from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.account_config import accounts_from_config
from src.application.config_loader import normalize_portfolio_broker_config, resolve_data_config_path
from src.application.config_validator import validate_config
from src.application.secret_resolver import resolve_feishu_holdings_config
from src.application.scan_scheduler import run_scheduler
from src.infrastructure.feishu_bitable import bitable_fields, get_tenant_access_token


REQUIRED_HOLDINGS_FIELDS = {"asset_id", "asset_name", "quantity", "account", "currency", "asset_type"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_base() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_config_path(config: str | Path, *, base: Path) -> Path:
    cfg_path = Path(config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
    return cfg_path


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _add_check(
    checks: list[dict[str, Any]],
    *,
    name: str,
    status: str,
    message: str,
    value: Any | None = None,
) -> None:
    item: dict[str, Any] = {
        "name": str(name),
        "status": str(status),
        "message": str(message),
    }
    if value is not None:
        item["value"] = value
    checks.append(item)


def _split_table_ref(value: str) -> tuple[str, str]:
    app_token, table_id = value.split("/", 1)
    return app_token, table_id


def _resolve_accounts(opt_cfg: dict[str, Any], requested: list[str] | None) -> list[str]:
    if requested is None:
        return accounts_from_config(opt_cfg)
    return accounts_from_config({"accounts": requested})


def run_healthcheck_runner(
    *,
    config: str | Path = "config.us.json",
    accounts: list[str] | None = None,
    base: str | Path | None = None,
    cron_path: str | Path | None = None,
) -> dict[str, Any]:
    repo_base = Path(base).resolve() if base is not None else _repo_base()
    cfg_path = _resolve_config_path(config, base=repo_base)
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []

    try:
        raw_cfg = _read_json(cfg_path)
        opt_cfg = normalize_portfolio_broker_config(raw_cfg)
    except Exception as exc:
        raw_cfg = {}
        opt_cfg = {}
        msg = f"config read failed: {exc}"
        errors.append(msg)
        _add_check(checks, name="config_read", status="error", message=msg)

    resolved_accounts = _resolve_accounts(opt_cfg, accounts)

    try:
        validate_config(dict(raw_cfg))
        _add_check(checks, name="config_validation", status="ok", message="config validation passed")
    except Exception as exc:
        msg = f"config validation failed: {exc}"
        errors.append(msg)
        _add_check(checks, name="config_validation", status="error", message=msg)

    try:
        raw_portfolio_cfg = opt_cfg.get("portfolio")
        portfolio_cfg = raw_portfolio_cfg if isinstance(raw_portfolio_cfg, dict) else {}
        data_ref = portfolio_cfg.get("data_config")
        data_path = resolve_data_config_path(base=repo_base, data_config=data_ref)
        pm = _read_json(data_path) if data_path.exists() else {}

        feishu = resolve_feishu_holdings_config(pm)
        if not feishu.ready:
            missing = ", ".join(feishu.missing_fields)
            raise RuntimeError(f"environment missing Feishu holdings config: {missing}")

        token = get_tenant_access_token(feishu.app_id, feishu.app_secret)
        hold_app, hold_tbl = _split_table_ref(feishu.holdings_ref)

        hold_fields = {f.get("field_name") for f in bitable_fields(token, hold_app, hold_tbl)}

        missing_hold = sorted(REQUIRED_HOLDINGS_FIELDS - hold_fields)
        if not (hold_fields & {"broker", "market"}):
            missing_hold.append("broker|market")
        if missing_hold:
            errors.append("holdings table missing fields: " + ",".join(missing_hold))
        if missing_hold:
            _add_check(
                checks,
                name="feishu_schema",
                status="error",
                message="required Feishu fields missing",
                value={"holdings_missing": missing_hold},
            )
        else:
            _add_check(
                checks,
                name="feishu_schema",
                status="ok",
                message="required Feishu fields found",
            )
    except Exception as exc:
        msg = f"feishu schema check failed: {exc}"
        errors.append(msg)
        _add_check(checks, name="feishu_schema", status="error", message=msg)

    try:
        scheduler_outputs: list[dict[str, str]] = []
        for acct in resolved_accounts:
            cfg = dict(raw_cfg)
            raw_scheduler_portfolio_cfg = cfg.get("portfolio")
            scheduler_portfolio_cfg: dict[str, Any] = (
                dict(raw_scheduler_portfolio_cfg) if isinstance(raw_scheduler_portfolio_cfg, dict) else {}
            )
            scheduler_portfolio_cfg["account"] = acct
            cfg["portfolio"] = scheduler_portfolio_cfg
            tmp = repo_base / "output" / "state" / f"healthcheck_config.{acct}.json"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            scheduler_state = repo_base / "output" / "state" / f"healthcheck_scheduler_state.{acct}.json"
            with redirect_stdout(io.StringIO()):
                run_scheduler(config=tmp, state=scheduler_state, jsonl=True, base_dir=repo_base)
            scheduler_outputs.append(
                {
                    "account": acct,
                    "config_path": str(tmp),
                    "state_path": str(scheduler_state),
                }
            )
        _add_check(
            checks,
            name="scheduler_decision",
            status="ok",
            message=f"checked scheduler decision for {len(resolved_accounts)} account(s)",
            value=scheduler_outputs,
        )
    except Exception as exc:
        msg = f"scheduler checks skipped: {exc}"
        warnings.append(msg)
        _add_check(checks, name="scheduler_decision", status="warn", message=msg)

    try:
        cron_state_path = Path(cron_path) if cron_path is not None else Path.home() / ".openclaw" / "cron" / "jobs.json"
        if cron_state_path.exists():
            data = _read_json(cron_state_path)
            job: dict[str, Any] | None = None
            for item in data.get("jobs", []):
                if isinstance(item, dict) and item.get("name") == "options-monitor auto tick":
                    job = item
                    break
            if job:
                raw_state = job.get("state")
                cron_job_state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
                last = cron_job_state.get("lastRunAtMs")
                status = cron_job_state.get("lastRunStatus") or cron_job_state.get("lastStatus")
                if status != "ok":
                    warnings.append(f"cron last status: {status}")
                    _add_check(checks, name="cron_state", status="warn", message=f"cron last status: {status}")
                elif last is None:
                    warnings.append("cron never ran yet")
                    _add_check(checks, name="cron_state", status="warn", message="cron never ran yet")
                else:
                    _add_check(checks, name="cron_state", status="ok", message="cron last run ok")
            else:
                msg = "cron job not found: options-monitor auto tick"
                warnings.append(msg)
                _add_check(checks, name="cron_state", status="warn", message=msg)
        else:
            msg = "cron jobs.json not found"
            warnings.append(msg)
            _add_check(checks, name="cron_state", status="warn", message=msg)
    except Exception as exc:
        msg = f"cron state check failed: {exc}"
        warnings.append(msg)
        _add_check(checks, name="cron_state", status="warn", message=msg)

    return {
        "ok": not errors,
        "utc": now_utc(),
        "config_path": str(cfg_path),
        "accounts": list(resolved_accounts),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "summary": {
            "ok": not errors,
            "critical_count": len(errors),
            "warning_count": len(warnings),
        },
    }


def format_healthcheck_report(result: dict[str, Any]) -> str:
    lines = [
        "# options-monitor healthcheck",
        f"utc: {result.get('utc')}",
    ]
    errors = [str(item) for item in result.get("errors", []) if str(item).strip()]
    warnings = [str(item) for item in result.get("warnings", []) if str(item).strip()]
    if errors:
        lines.append("")
        lines.append("## CRITICAL")
        lines.extend(f"- {item}" for item in errors)
    if warnings:
        lines.append("")
        lines.append("## WARN")
        lines.extend(f"- {item}" for item in warnings)
    if not errors and not warnings:
        lines.append("")
        lines.append("OK")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="options-monitor healthcheck")
    parser.add_argument("--config", default="config.us.json")
    parser.add_argument("--accounts", nargs="*", default=None)
    parser.add_argument("--json", action="store_true", help="print structured JSON instead of the human report")
    args = parser.parse_args(argv)

    result = run_healthcheck_runner(config=args.config, accounts=args.accounts)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_healthcheck_report(result), end="")
    return 0 if result.get("ok") else 2
