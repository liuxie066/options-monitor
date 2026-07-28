from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from domain.services import adapt_holdings_context
from domain.storage.repositories import state_repo
from src.application.account_config import build_account_portfolio_source_plan
from src.application.config_loader import resolve_data_config_path
from src.application.futu_portfolio_context import fetch_futu_portfolio_context
from src.application.portfolio_context_service import (
    load_account_portfolio_context,
    load_holdings_portfolio_shared_context,
    with_context_source,
)
from src.application.strategy_policy import (
    SELL_CALL_FAMILY,
    SELL_PUT_FAMILY,
    strategy_semantics_for_side_config,
)
from src.infrastructure.io_utils import (
    atomic_write_json,
    is_fresh,
    load_cached_json,
)
from src.application.position_advice_source_receipts import sha256_bytes


PREPARED_PORTFOLIO_CONTEXT_SCHEMA = "prepared_portfolio_context.v1"
_RESULT_SCHEMA = "prepared_portfolio_context_worker_result.v1"
DEFAULT_KILL_GRACE_SEC = 0.25


class PreparedPortfolioContextError(RuntimeError):
    pass


def prepare_portfolio_contexts(
    *,
    base: Path,
    repo_root: Path,
    run_id: str,
    account_configs: Mapping[str, Mapping[str, Any]],
    account_state_dirs: Mapping[str, Path],
    shared_state_dir: Path,
    timeout_sec: float,
    python_executable: Path | None = None,
    kill_grace_sec: float = DEFAULT_KILL_GRACE_SEC,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> dict[str, dict[str, Any]]:
    """Prepare all account contexts under one shared absolute deadline."""

    run_id_norm = _required_text(run_id, "run_id")
    configs_by_account = {
        str(account or "").strip().lower(): config
        for account, config in account_configs.items()
        if str(account or "").strip()
    }
    states_by_account = {
        str(account or "").strip().lower(): Path(state_dir)
        for account, state_dir in account_state_dirs.items()
        if str(account or "").strip()
    }
    accounts = sorted(configs_by_account)
    if set(accounts) != set(states_by_account):
        raise PreparedPortfolioContextError("account config/state scopes do not match")
    timeout_value = max(0.001, float(timeout_sec))
    started_at_utc = datetime.now(timezone.utc)
    deadline_at_utc = started_at_utc + timedelta(seconds=timeout_value)
    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + timeout_value
    python = Path(python_executable or sys.executable).resolve()
    run_state_dir = Path(shared_state_dir).resolve()
    run_state_dir.mkdir(parents=True, exist_ok=True)
    processes: dict[str, subprocess.Popen[Any]] = {}
    worker_requests: dict[str, dict[str, Any]] = {}
    accepted: set[str] = set()
    result_payloads: dict[str, dict[str, Any]] = {}
    child_finished_at_utc: dict[str, str] = {}

    with tempfile.TemporaryDirectory(
        prefix="prepared-portfolio-context-",
        dir=str(run_state_dir),
    ) as temp_name:
        temp_root = Path(temp_name).resolve()
        for account in accounts:
            token = uuid4().hex
            account_temp = temp_root / account
            account_temp.mkdir(parents=True, exist_ok=False)
            request_path = account_temp / "request.json"
            result_path = account_temp / "result.json"
            config = dict(configs_by_account[account])
            request_payload = {
                "schema_version": "prepared_portfolio_context_worker_request.v1",
                "token": token,
                "run_id": run_id_norm,
                "account": account,
                "base": str(Path(base).resolve()),
                "state_dir": str(states_by_account[account].resolve()),
                "shared_state_dir": str(run_state_dir),
                "runtime_config": config,
                "result_path": str(result_path),
            }
            atomic_write_json(request_path, request_payload)
            worker_requests[account] = request_payload
            processes[account] = popen_factory(
                [
                    str(python),
                    "-m",
                    "src.application.prepared_portfolio_context",
                    "--worker-request",
                    str(request_path),
                ],
                cwd=str(Path(repo_root).resolve()),
                env=dict(os.environ, PYTHONPATH=str(Path(repo_root).resolve())),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        while len(accepted) < len(accounts):
            now = time.monotonic()
            for account, process in processes.items():
                if account in accepted or process.poll() is None:
                    continue
                accepted.add(account)
                child_finished_at_utc[account] = datetime.now(timezone.utc).isoformat()
                result_path = Path(worker_requests[account]["result_path"])
                result_payloads[account] = _read_worker_result(
                    result_path=result_path,
                    request=worker_requests[account],
                    returncode=process.returncode,
                )
            if len(accepted) == len(accounts) or now >= deadline_monotonic:
                break
            time.sleep(min(0.02, max(0.001, deadline_monotonic - now)))

        timed_out = [
            account
            for account, process in processes.items()
            if account not in accepted and process.poll() is None
        ]
        for account in timed_out:
            processes[account].terminate()
        kill_deadline = time.monotonic() + max(0.0, float(kill_grace_sec))
        for account in timed_out:
            process = processes[account]
            remaining = max(0.0, kill_deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            result_payloads[account] = {
                "status": "unavailable",
                "reason": "portfolio_context_deadline_exceeded",
                "worker_returncode": process.returncode,
            }
            child_finished_at_utc[account] = datetime.now(timezone.utc).isoformat()

        for account, process in processes.items():
            if account in result_payloads:
                continue
            child_finished_at_utc[account] = datetime.now(timezone.utc).isoformat()
            result_path = Path(worker_requests[account]["result_path"])
            result_payloads[account] = _read_worker_result(
                result_path=result_path,
                request=worker_requests[account],
                returncode=process.returncode,
            )

        promoted: dict[str, dict[str, Any]] = {}
        for account in accounts:
            state_dir = states_by_account[account].resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            result = result_payloads[account]
            status = str(result.get("status") or "unavailable").strip().lower()
            promoted_at_utc = datetime.now(timezone.utc).isoformat()
            manifest: dict[str, Any] = {
                "schema_version": PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
                "run_id": run_id_norm,
                "account": account,
                "status": status if status in {"ready", "unavailable"} else "unavailable",
                "preparation_started_at_utc": started_at_utc.isoformat(),
                "deadline_at_utc": deadline_at_utc.isoformat(),
                "child_finished_at_utc": child_finished_at_utc.get(account),
                "promoted_at_utc": promoted_at_utc,
                "prepared_at_utc": promoted_at_utc,
                "deadline_seconds": timeout_value,
                "worker_returncode": result.get("worker_returncode"),
            }
            if status == "ready" and isinstance(result.get("portfolio_context"), dict):
                context = dict(result["portfolio_context"])
                context_path = state_dir / "portfolio_context.json"
                atomic_write_json(context_path, context)
                context_bytes = context_path.read_bytes()
                manifest.update(
                    {
                        "portfolio_context_relpath": context_path.name,
                        "payload_sha256": sha256_bytes(context_bytes),
                        "source_as_of_utc": str(
                            context.get("source_observed_at")
                            or context.get("as_of_utc")
                            or ""
                        ),
                    }
                )
                try:
                    state_repo.append_source_snapshot_event(
                        Path(base),
                        adapt_holdings_context(context),
                    )
                except Exception:
                    pass
            else:
                manifest["status"] = "unavailable"
                manifest["reason"] = str(
                    result.get("reason") or "portfolio_context_worker_failed"
                ).strip()
                if result.get("error_type"):
                    manifest["error_type"] = str(result["error_type"])
            manifest_path = state_dir / "prepared_portfolio_context.v1.json"
            atomic_write_json(manifest_path, manifest)
            manifest["manifest_path"] = str(manifest_path)
            promoted[account] = manifest
        return promoted


def load_prepared_portfolio_context(
    *,
    manifest_path: Path,
    expected_run_id: str,
    expected_account: str,
) -> dict[str, Any] | None:
    path = Path(manifest_path).resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedPortfolioContextError("prepared portfolio manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise PreparedPortfolioContextError("prepared portfolio manifest must be an object")
    if manifest.get("schema_version") != PREPARED_PORTFOLIO_CONTEXT_SCHEMA:
        raise PreparedPortfolioContextError("prepared portfolio manifest schema mismatch")
    run_id = _required_text(manifest.get("run_id"), "manifest run_id")
    account = _required_text(manifest.get("account"), "manifest account").lower()
    if run_id != _required_text(expected_run_id, "expected_run_id"):
        raise PreparedPortfolioContextError("prepared portfolio manifest run mismatch")
    if account != _required_text(expected_account, "expected_account").lower():
        raise PreparedPortfolioContextError("prepared portfolio manifest account mismatch")
    if path.parent.name != "state" or path.parent.parent.name != account:
        raise PreparedPortfolioContextError("prepared portfolio manifest path mismatch")
    if len(path.parents) < 4 or path.parents[3].name != run_id:
        raise PreparedPortfolioContextError("prepared portfolio manifest is outside the current run")
    status = str(manifest.get("status") or "").strip().lower()
    if status == "unavailable":
        return None
    if status != "ready":
        raise PreparedPortfolioContextError("prepared portfolio manifest status is invalid")
    relpath = _required_text(
        manifest.get("portfolio_context_relpath"),
        "portfolio_context_relpath",
    )
    context_path = (path.parent / relpath).resolve()
    try:
        context_path.relative_to(path.parent)
    except ValueError as exc:
        raise PreparedPortfolioContextError("prepared portfolio context escapes state dir") from exc
    if not context_path.is_file() or context_path.is_symlink():
        raise PreparedPortfolioContextError("prepared portfolio context is unavailable")
    payload_bytes = context_path.read_bytes()
    if sha256_bytes(payload_bytes) != _required_text(
        manifest.get("payload_sha256"),
        "payload_sha256",
    ):
        raise PreparedPortfolioContextError("prepared portfolio context hash mismatch")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedPortfolioContextError("prepared portfolio context is unreadable") from exc
    if not isinstance(payload, dict):
        raise PreparedPortfolioContextError("prepared portfolio context must be an object")
    return payload


def run_worker(request_path: Path) -> int:
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    result_path = Path(_required_text(request.get("result_path"), "result_path"))
    token = _required_text(request.get("token"), "token")
    account = _required_text(request.get("account"), "account").lower()
    run_id = _required_text(request.get("run_id"), "run_id")
    cfg = request.get("runtime_config")
    if not isinstance(cfg, dict):
        raise PreparedPortfolioContextError("runtime config is invalid")
    base = Path(_required_text(request.get("base"), "base")).resolve()
    state_dir = Path(_required_text(request.get("state_dir"), "state_dir")).resolve()
    shared_state_dir = Path(
        _required_text(request.get("shared_state_dir"), "shared_state_dir")
    ).resolve()
    logs: list[str] = []
    try:
        portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
        runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
        data_config = resolve_data_config_path(
            base=base,
            data_config=portfolio_cfg.get("data_config"),
        )
        broker = str(portfolio_cfg.get("broker") or "富途")
        source = build_account_portfolio_source_plan(
            cfg,
            account=account,
        ).requested_source
        context = load_account_portfolio_context(
            base=base,
            data_config=str(data_config),
            market=broker,
            account=account,
            ttl_sec=int(runtime.get("portfolio_context_ttl_sec", 900) or 0),
            state_dir=state_dir,
            shared_state_dir=shared_state_dir,
            log=logs.append,
            runtime_config=cfg,
            portfolio_source=str(source),
            fetch_futu_portfolio_context_fn=fetch_futu_portfolio_context,
            is_fresh_fn=is_fresh,
            load_json_fn=load_cached_json,
            write_cache=False,
        )
        if _wants_global_path_risk_context(cfg):
            shared = load_holdings_portfolio_shared_context(
                data_config_path=Path(data_config),
                broker=None,
            )
            all_accounts = shared.get("all_accounts") if isinstance(shared, dict) else None
            if isinstance(all_accounts, dict):
                context = dict(context)
                context["_global_portfolio_ctx"] = with_context_source(
                    dict(all_accounts),
                    "global_prepared",
                )
        result = {
            "schema_version": _RESULT_SCHEMA,
            "token": token,
            "run_id": run_id,
            "account": account,
            "status": "ready",
            "portfolio_context": context,
            "payload_sha256": _canonical_payload_sha256(context),
            "logs": logs[-20:],
        }
    except Exception as exc:
        result = {
            "schema_version": _RESULT_SCHEMA,
            "token": token,
            "run_id": run_id,
            "account": account,
            "status": "unavailable",
            "reason": "portfolio_context_unavailable",
            "error_type": type(exc).__name__,
            "logs": logs[-20:],
        }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(result_path, result)
    return 0


def _wants_global_path_risk_context(cfg: dict[str, Any] | None) -> bool:
    if not isinstance(cfg, dict):
        return False

    def _uses_path_risk(node: object, *, family: str) -> bool:
        return (
            isinstance(node, dict)
            and strategy_semantics_for_side_config(
                family=family,
                side_cfg=node,
            ).scan_uses_path_risk
        )

    templates = cfg.get("templates")
    if isinstance(templates, dict):
        for profile in templates.values():
            if isinstance(profile, dict) and (
                _uses_path_risk(profile.get("sell_put"), family=SELL_PUT_FAMILY)
                or _uses_path_risk(
                    profile.get("sell_call"),
                    family=SELL_CALL_FAMILY,
                )
            ):
                return True
    for item in cfg.get("symbols") or []:
        if isinstance(item, dict) and (
            _uses_path_risk(item.get("sell_put"), family=SELL_PUT_FAMILY)
            or _uses_path_risk(item.get("sell_call"), family=SELL_CALL_FAMILY)
        ):
            return True
    return False


def _read_worker_result(
    *,
    result_path: Path,
    request: Mapping[str, Any],
    returncode: int | None,
) -> dict[str, Any]:
    if returncode != 0:
        return {
            "status": "unavailable",
            "reason": "portfolio_context_worker_failed",
            "worker_returncode": returncode,
        }
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "unavailable",
            "reason": "portfolio_context_worker_result_unavailable",
            "worker_returncode": returncode,
        }
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _RESULT_SCHEMA
        or payload.get("token") != request.get("token")
        or payload.get("run_id") != request.get("run_id")
        or payload.get("account") != request.get("account")
    ):
        return {
            "status": "unavailable",
            "reason": "portfolio_context_worker_result_mismatch",
            "worker_returncode": returncode,
        }
    if payload.get("status") == "ready":
        context = payload.get("portfolio_context")
        if (
            not isinstance(context, dict)
            or payload.get("payload_sha256")
            != _canonical_payload_sha256(context)
        ):
            return {
                "status": "unavailable",
                "reason": "portfolio_context_worker_payload_mismatch",
                "worker_returncode": returncode,
            }
    payload = dict(payload)
    payload["worker_returncode"] = returncode
    return payload


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PreparedPortfolioContextError(f"{field} is required")
    return text


def _canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(raw)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-request", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run_worker(Path(args.worker_request))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_KILL_GRACE_SEC",
    "PREPARED_PORTFOLIO_CONTEXT_SCHEMA",
    "PreparedPortfolioContextError",
    "load_prepared_portfolio_context",
    "prepare_portfolio_contexts",
    "run_worker",
]
