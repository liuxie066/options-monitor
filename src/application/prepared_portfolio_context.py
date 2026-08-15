from __future__ import annotations

import argparse
from contextlib import ExitStack
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
from src.application.account_config import (
    build_account_portfolio_source_plan,
    normalize_account_label,
)
from src.application.config_loader import resolve_data_config_path
from src.application.futu_portfolio_context import fetch_futu_portfolio_context
from src.application.portfolio_context_service import (
    expected_portfolio_context_account,
    load_account_portfolio_context,
    load_holdings_portfolio_shared_context,
    portfolio_context_account_mismatch_reason,
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
from src.application.source_receipts import sha256_bytes
from src.application.tick_run_workspace import (
    AccountRunConfigAuthority,
    AccountRunConfigError,
    ensure_run_state_directory_safely,
    load_account_run_config,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)
from src.application.payload_helpers import required_text
from functools import partial


_required_text = partial(required_text, error=lambda m: PreparedPortfolioContextError(m))


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
    account_config_authorities: Mapping[str, AccountRunConfigAuthority],
    account_state_dirs: Mapping[str, Path],
    shared_state_dir: Path,
    timeout_sec: float,
    python_executable: Path | None = None,
    kill_grace_sec: float = DEFAULT_KILL_GRACE_SEC,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> dict[str, dict[str, Any]]:
    """Prepare all account contexts under one shared absolute deadline."""

    run_id_norm = _required_text(run_id, "run_id")
    try:
        authorities_by_account = {
            normalize_account_label(account): authority
            for account, authority in account_config_authorities.items()
        }
        states_by_account = {
            normalize_account_label(account): Path(state_dir)
            for account, state_dir in account_state_dirs.items()
        }
    except ValueError as exc:
        raise PreparedPortfolioContextError("account scope is invalid") from exc
    accounts = sorted(authorities_by_account)
    if set(accounts) != set(states_by_account):
        raise PreparedPortfolioContextError("account config/state scopes do not match")
    base_path = Path(base).resolve()
    for account, supplied_state_dir in states_by_account.items():
        expected_state_dir = (
            base_path
            / "output_runs"
            / run_id_norm
            / "accounts"
            / account
            / "state"
        )
        supplied_absolute = Path(
            os.path.abspath(str(supplied_state_dir.expanduser()))
        )
        if supplied_absolute != expected_state_dir:
            raise PreparedPortfolioContextError(
                "account prepared state path is outside the current run"
            )
        states_by_account[account] = expected_state_dir
    timeout_value = max(0.001, float(timeout_sec))
    started_at_utc = datetime.now(timezone.utc)
    deadline_at_utc = started_at_utc + timedelta(seconds=timeout_value)
    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + timeout_value
    result_payloads: dict[str, dict[str, Any]] = {}
    child_finished_at_utc: dict[str, str] = {}
    worker_accounts: list[str] = []
    adopted_manifests: dict[str, dict[str, Any]] = {}
    blocked_existing_manifests: dict[str, dict[str, Any]] = {}
    for account in accounts:
        existing_path = (
            base_path
            / "output_runs"
            / run_id_norm
            / "accounts"
            / account
            / "state"
            / "prepared_portfolio_context.v1.json"
        )
        try:
            account_config = load_account_run_config(
                authority=authorities_by_account[account],
                base=base,
                run_id=run_id_norm,
                account=account,
            )
        except AccountRunConfigError as exc:
            result_payloads[account] = {
                "status": "unavailable",
                "reason": exc.reason,
                "error_type": type(exc).__name__,
                "error_code": exc.code,
                "worker_returncode": None,
            }
            child_finished_at_utc[account] = datetime.now(timezone.utc).isoformat()
            try:
                existing_bytes = read_account_run_state_bytes_safely(
                    base=base_path,
                    run_id=run_id_norm,
                    account=account,
                    name=existing_path.name,
                )
            except AccountRunConfigError:
                pass
            else:
                blocked_existing_manifests[account] = {
                    **result_payloads[account],
                    "schema_version": PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
                    "run_id": run_id_norm,
                    "account": account,
                    "account_config_sha256": authorities_by_account[
                        account
                    ].account_config_sha256,
                    "manifest_path": str(existing_path),
                    "manifest_sha256": sha256_bytes(existing_bytes),
                    "publication_status": "existing_immutable_generation",
                }
        else:
            try:
                existing_bytes = read_account_run_state_bytes_safely(
                    base=base_path,
                    run_id=run_id_norm,
                    account=account,
                    name=existing_path.name,
                )
            except AccountRunConfigError:
                worker_accounts.append(account)
            else:
                existing_digest = sha256_bytes(existing_bytes)
                try:
                    existing_manifest = json.loads(existing_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    existing_manifest = {
                        "status": "unavailable",
                        "reason": "prepared_portfolio_context_existing_invalid",
                    }
                if not isinstance(existing_manifest, dict):
                    existing_manifest = {
                        "status": "unavailable",
                        "reason": "prepared_portfolio_context_existing_invalid",
                    }
                try:
                    load_prepared_portfolio_context(
                        manifest_path=existing_path,
                        expected_base=base_path,
                        expected_run_id=run_id_norm,
                        expected_account=account,
                        expected_account_config_sha256=authorities_by_account[
                            account
                        ].account_config_sha256,
                        expected_manifest_sha256=existing_digest,
                        expected_runtime_config=account_config,
                    )
                except PreparedPortfolioContextError as exc:
                    existing_manifest = {
                        **existing_manifest,
                        "status": "unavailable",
                        "reason": "prepared_portfolio_context_existing_invalid",
                        "error_type": type(exc).__name__,
                    }
                adopted_manifests[account] = {
                    **existing_manifest,
                    "manifest_path": str(existing_path),
                    "manifest_sha256": existing_digest,
                }
    python = Path(python_executable or sys.executable).resolve()
    expected_run_state_dir = base_path / "output_runs" / run_id_norm / "state"
    supplied_run_state_dir = Path(
        os.path.abspath(str(Path(shared_state_dir).expanduser()))
    )
    if supplied_run_state_dir != expected_run_state_dir:
        raise PreparedPortfolioContextError(
            "shared prepared state path is outside the current run"
        )
    try:
        run_state_dir = ensure_run_state_directory_safely(
            base=base_path,
            run_id=run_id_norm,
        )
    except AccountRunConfigError as exc:
        raise PreparedPortfolioContextError(
            "shared prepared state path is unavailable or unsafe"
        ) from exc
    processes: dict[str, subprocess.Popen[Any]] = {}
    worker_requests: dict[str, dict[str, Any]] = {}
    accepted: set[str] = set()

    with (
        tempfile.TemporaryDirectory(
            prefix="prepared-portfolio-context-",
        ) as temp_name,
        ExitStack() as cleanup_stack,
    ):
        cleanup_stack.callback(
            _cleanup_worker_processes,
            processes,
            kill_grace_sec,
        )
        temp_root = Path(temp_name).resolve()
        for account in worker_accounts:
            token = uuid4().hex
            account_temp = temp_root / account
            account_temp.mkdir(parents=True, exist_ok=False)
            request_path = account_temp / "request.json"
            result_path = account_temp / "result.json"
            authority = authorities_by_account[account]
            request_payload = {
                "schema_version": "prepared_portfolio_context_worker_request.v1",
                "token": token,
                "run_id": run_id_norm,
                "account": account,
                "base": str(Path(base).resolve()),
                "state_dir": str(states_by_account[account].resolve()),
                "shared_state_dir": str(run_state_dir),
                "account_config_path": str(authority.state_path),
                "account_config_compatibility_path": str(
                    authority.compatibility_path
                ),
                "account_config_sha256": authority.account_config_sha256,
                "account_config_canonical_json": authority.canonical_bytes.decode(
                    "utf-8"
                ),
                "result_path": str(result_path),
            }
            atomic_write_json(request_path, request_payload)
            worker_requests[account] = request_payload
            try:
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
            except Exception as exc:
                result_payloads[account] = {
                    "status": "unavailable",
                    "reason": "portfolio_context_worker_spawn_failed",
                    "error_type": type(exc).__name__,
                    "worker_returncode": None,
                }
                child_finished_at_utc[account] = datetime.now(
                    timezone.utc
                ).isoformat()

        while len(accepted) < len(processes):
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
            if len(accepted) == len(processes) or now >= deadline_monotonic:
                break
            time.sleep(min(0.02, max(0.001, deadline_monotonic - now)))

        timed_out = [
            account
            for account in processes
            if account not in accepted
        ]
        for account in timed_out:
            try:
                processes[account].terminate()
            except Exception:
                # The worker may have exited between poll() and terminate().
                # wait() below still reaps it and records the deadline outcome.
                pass
        kill_deadline = time.monotonic() + max(0.0, float(kill_grace_sec))
        for account in timed_out:
            process = processes[account]
            remaining = max(0.0, kill_deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    process.wait()
                except Exception:
                    pass
            except Exception:
                # ExitStack performs a final best-effort cleanup for a process
                # whose platform-specific wait operation failed.
                pass
            accepted.add(account)
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

        promoted: dict[str, dict[str, Any]] = {
            **blocked_existing_manifests,
            **adopted_manifests,
        }
        for account in accounts:
            if account in promoted:
                continue
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
                "account_config_sha256": authorities_by_account[
                    account
                ].account_config_sha256,
            }
            if status == "ready" and isinstance(result.get("portfolio_context"), dict):
                context = dict(result["portfolio_context"])
                context_bytes = _json_file_bytes(context)
                payload_digest = sha256_bytes(context_bytes)
                context_name = f"portfolio_context.{payload_digest}.json"
                write_account_run_state_bytes_once_safely(
                    base=base_path,
                    run_id=run_id_norm,
                    account=account,
                    name=context_name,
                    payload=context_bytes,
                )
                manifest.update(
                    {
                        "portfolio_context_relpath": context_name,
                        "payload_sha256": payload_digest,
                        "portfolio_source_name": _required_text(
                            result.get("portfolio_source_name"),
                            "portfolio_source_name",
                        ),
                        "portfolio_source_account": _required_text(
                            result.get("portfolio_source_account"),
                            "portfolio_source_account",
                        ),
                        "source_as_of_utc": str(
                            context.get("source_observed_at")
                            or context.get("as_of_utc")
                            or ""
                        ),
                    }
                )
            else:
                manifest["status"] = "unavailable"
                manifest["reason"] = str(
                    result.get("reason") or "portfolio_context_worker_failed"
                ).strip()
                if result.get("error_type"):
                    manifest["error_type"] = str(result["error_type"])
                if result.get("error_code"):
                    manifest["error_code"] = str(result["error_code"])
            manifest_bytes = _json_file_bytes(manifest)
            manifest_path = write_account_run_state_bytes_once_safely(
                base=base_path,
                run_id=run_id_norm,
                account=account,
                name="prepared_portfolio_context.v1.json",
                payload=manifest_bytes,
            )
            promoted_manifest = dict(manifest)
            promoted_manifest["manifest_path"] = str(manifest_path)
            promoted_manifest["manifest_sha256"] = sha256_bytes(manifest_bytes)
            promoted[account] = promoted_manifest
            if status == "ready" and isinstance(result.get("portfolio_context"), dict):
                try:
                    state_repo.append_source_snapshot_event(
                        Path(base),
                        adapt_holdings_context(dict(result["portfolio_context"])),
                    )
                except Exception:
                    pass
        return promoted


def load_prepared_portfolio_context(
    *,
    manifest_path: Path,
    expected_base: Path,
    expected_run_id: str,
    expected_account: str,
    expected_account_config_sha256: str,
    expected_manifest_sha256: str | None = None,
    expected_runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    path = Path(os.path.abspath(str(Path(manifest_path).expanduser())))
    run_id_expected = _required_text(expected_run_id, "expected_run_id")
    account_expected = _required_text(
        expected_account,
        "expected_account",
    ).lower()
    expected_path = (
        Path(expected_base).resolve()
        / "output_runs"
        / run_id_expected
        / "accounts"
        / account_expected
        / "state"
        / "prepared_portfolio_context.v1.json"
    )
    if path != expected_path:
        raise PreparedPortfolioContextError(
            "prepared portfolio manifest path mismatch"
        )
    try:
        manifest_bytes = read_account_run_state_bytes_safely(
            base=expected_base,
            run_id=run_id_expected,
            account=account_expected,
            name=path.name,
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (
        AccountRunConfigError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise PreparedPortfolioContextError("prepared portfolio manifest is unreadable") from exc
    if expected_manifest_sha256 is not None:
        expected_manifest_digest = _required_sha256(
            expected_manifest_sha256,
            "expected_manifest_sha256",
        )
        if sha256_bytes(manifest_bytes) != expected_manifest_digest:
            raise PreparedPortfolioContextError(
                "prepared portfolio manifest generation mismatch"
            )
    if not isinstance(manifest, dict):
        raise PreparedPortfolioContextError("prepared portfolio manifest must be an object")
    if manifest.get("schema_version") != PREPARED_PORTFOLIO_CONTEXT_SCHEMA:
        raise PreparedPortfolioContextError("prepared portfolio manifest schema mismatch")
    run_id = _required_text(manifest.get("run_id"), "manifest run_id")
    account = _required_text(manifest.get("account"), "manifest account").lower()
    if run_id != run_id_expected:
        raise PreparedPortfolioContextError("prepared portfolio manifest run mismatch")
    if account != account_expected:
        raise PreparedPortfolioContextError("prepared portfolio manifest account mismatch")
    expected_digest = _required_text(
        expected_account_config_sha256,
        "expected_account_config_sha256",
    ).lower()
    manifest_digest = _required_text(
        manifest.get("account_config_sha256"),
        "manifest account_config_sha256",
    ).lower()
    if not _is_sha256(expected_digest) or not _is_sha256(manifest_digest):
        raise PreparedPortfolioContextError(
            "prepared portfolio manifest account config hash is invalid"
        )
    if manifest_digest != expected_digest:
        raise PreparedPortfolioContextError(
            "prepared portfolio manifest account config hash mismatch"
        )
    status = str(manifest.get("status") or "").strip().lower()
    if status == "unavailable":
        return None
    if status != "ready":
        raise PreparedPortfolioContextError("prepared portfolio manifest status is invalid")
    relpath = _required_text(
        manifest.get("portfolio_context_relpath"),
        "portfolio_context_relpath",
    )
    if Path(relpath).name != relpath or relpath in {".", ".."}:
        raise PreparedPortfolioContextError(
            "prepared portfolio context escapes state dir"
        )
    try:
        payload_bytes = read_account_run_state_bytes_safely(
            base=expected_base,
            run_id=run_id_expected,
            account=account_expected,
            name=relpath,
        )
    except AccountRunConfigError as exc:
        raise PreparedPortfolioContextError(
            "prepared portfolio context is unavailable"
        ) from exc
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
    _validate_prepared_source_binding(
        manifest=manifest,
        payload=payload,
        expected_account=account,
        expected_runtime_config=expected_runtime_config,
    )
    return payload


def run_worker(request_path: Path) -> int:
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    result_path = Path(_required_text(request.get("result_path"), "result_path"))
    token = _required_text(request.get("token"), "token")
    account = _required_text(request.get("account"), "account").lower()
    run_id = _required_text(request.get("run_id"), "run_id")
    base = Path(_required_text(request.get("base"), "base")).resolve()
    state_dir = Path(_required_text(request.get("state_dir"), "state_dir")).resolve()
    shared_state_dir = Path(
        _required_text(request.get("shared_state_dir"), "shared_state_dir")
    ).resolve()
    logs: list[str] = []
    try:
        account_config_sha256 = _required_text(
            request.get("account_config_sha256"),
            "account_config_sha256",
        ).lower()
        canonical_json = request.get("account_config_canonical_json")
        if not isinstance(canonical_json, str) or not canonical_json:
            raise PreparedPortfolioContextError(
                "account_config_canonical_json is required"
            )
        canonical_bytes = canonical_json.encode("utf-8")
        authority = AccountRunConfigAuthority(
            run_id=run_id,
            account=account,
            state_path=Path(
                _required_text(
                    request.get("account_config_path"),
                    "account_config_path",
                )
            ),
            compatibility_path=Path(
                _required_text(
                    request.get("account_config_compatibility_path"),
                    "account_config_compatibility_path",
                )
            ),
            account_config_sha256=account_config_sha256,
            canonical_bytes=canonical_bytes,
        )
        cfg = load_account_run_config(
            authority=authority,
            base=base,
            run_id=run_id,
            account=account,
        )
        portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
        runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
        data_config = resolve_data_config_path(
            base=base,
            data_config=portfolio_cfg.get("data_config"),
        )
        broker = str(portfolio_cfg.get("broker") or "富途")
        source_plan = build_account_portfolio_source_plan(
            cfg,
            account=account,
        )
        source = source_plan.requested_source
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
        source_name, source_account = _resolve_context_source_binding(
            config=cfg,
            account=account,
            context=context,
        )
        result = {
            "schema_version": _RESULT_SCHEMA,
            "token": token,
            "run_id": run_id,
            "account": account,
            "status": "ready",
            "account_config_sha256": account_config_sha256,
            "portfolio_context": context,
            "payload_sha256": _canonical_payload_sha256(context),
            "portfolio_source_name": source_name,
            "portfolio_source_account": source_account,
            "logs": logs[-20:],
        }
    except AccountRunConfigError as exc:
        result = {
            "schema_version": _RESULT_SCHEMA,
            "token": token,
            "run_id": run_id,
            "account": account,
            "status": "unavailable",
            "account_config_sha256": str(
                request.get("account_config_sha256") or ""
            ).strip().lower(),
            "reason": exc.reason,
            "error_type": type(exc).__name__,
            "error_code": exc.code,
            "logs": logs[-20:],
        }
    except Exception as exc:
        result = {
            "schema_version": _RESULT_SCHEMA,
            "token": token,
            "run_id": run_id,
            "account": account,
            "status": "unavailable",
            "account_config_sha256": str(
                request.get("account_config_sha256") or ""
            ).strip().lower(),
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
        or payload.get("account_config_sha256")
        != request.get("account_config_sha256")
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


def _cleanup_worker_processes(
    processes: Mapping[str, subprocess.Popen[Any]],
    kill_grace_sec: float,
) -> None:
    running: list[subprocess.Popen[Any]] = []
    for process in processes.values():
        try:
            if process.poll() is None:
                process.terminate()
                running.append(process)
        except Exception:
            running.append(process)
    deadline = time.monotonic() + max(0.0, float(kill_grace_sec))
    for process in running:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            finally:
                process.wait()
        except Exception:
            try:
                process.kill()
                process.wait()
            except Exception:
                pass


def _required_sha256(value: Any, field: str) -> str:
    digest = _required_text(value, field).lower()
    if not _is_sha256(digest):
        raise PreparedPortfolioContextError(f"{field} must be a SHA-256 digest")
    return digest


def _resolve_context_source_binding(
    *,
    config: Mapping[str, Any],
    account: str,
    context: Mapping[str, Any],
) -> tuple[str, str]:
    plan = build_account_portfolio_source_plan(dict(config), account=account)
    source_name = str(
        context.get("portfolio_source_name") or plan.primary_source
    ).strip().lower()
    if source_name not in _allowed_context_sources(plan):
        raise PreparedPortfolioContextError(
            "prepared portfolio context source is not allowed by account config"
        )
    source_account = expected_portfolio_context_account(
        source_name=source_name,
        account=account,
        holdings_account=plan.holdings_account,
    )
    if not source_account:
        raise PreparedPortfolioContextError(
            "prepared portfolio context source account is unavailable"
        )
    mismatch = _prepared_context_account_mismatch_reason(
        dict(context),
        requested_account=source_account,
    )
    if mismatch is not None:
        raise PreparedPortfolioContextError(
            f"prepared portfolio context account mismatch: {mismatch}"
        )
    return source_name, source_account


def _allowed_context_sources(plan: Any) -> set[str]:
    if str(plan.account_type).strip().lower() == "external_holdings":
        return {"external_holdings", "holdings"}
    if str(plan.requested_source).strip().lower() == "auto":
        return {"futu", "holdings", "external_holdings"}
    if str(plan.primary_source).strip().lower() == "futu":
        return {"futu"}
    return {"holdings", "external_holdings"}


def _validate_prepared_source_binding(
    *,
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    expected_account: str,
    expected_runtime_config: Mapping[str, Any] | None,
) -> None:
    manifest_source = _required_text(
        manifest.get("portfolio_source_name"),
        "manifest portfolio_source_name",
    ).lower()
    manifest_source_account = _required_text(
        manifest.get("portfolio_source_account"),
        "manifest portfolio_source_account",
    )
    if expected_runtime_config is not None:
        expected_source, expected_source_account = _resolve_context_source_binding(
            config=expected_runtime_config,
            account=expected_account,
            context={**dict(payload), "portfolio_source_name": manifest_source},
        )
        if manifest_source != expected_source:
            raise PreparedPortfolioContextError(
                "prepared portfolio manifest source mismatch"
            )
        if manifest_source_account != expected_source_account:
            raise PreparedPortfolioContextError(
                "prepared portfolio manifest source account mismatch"
            )
    mismatch = _prepared_context_account_mismatch_reason(
        dict(payload),
        requested_account=manifest_source_account,
    )
    if mismatch is not None:
        raise PreparedPortfolioContextError(
            f"prepared portfolio context account mismatch: {mismatch}"
        )


def _prepared_context_account_mismatch_reason(
    context: Mapping[str, Any],
    *,
    requested_account: str,
) -> str | None:
    filters = context.get("filters")
    if not isinstance(filters, Mapping):
        return "filters.account is missing"
    declared_account = str(filters.get("account") or "").strip().lower()
    if not declared_account:
        return "filters.account is missing"
    requested_norm = str(requested_account or "").strip().lower()
    if declared_account != requested_norm:
        return (
            f"filters.account requested={requested_norm} "
            f"cached={declared_account}"
        )
    return portfolio_context_account_mismatch_reason(
        dict(context),
        requested_account=requested_norm,
    )


def _json_file_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


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
