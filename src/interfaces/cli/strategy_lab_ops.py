from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.opend_fetch_config import resolve_opend_fetch_limits
from src.application.service_deploy import load_service_profile
from src.application.strategy_lab.contracts import STRATEGY_LAB_ADVANCE_SERVICE
from src.application.strategy_lab.readiness import (
    HistoryKReadinessError,
    preview_history_k_readiness,
    refresh_history_k_readiness,
)
from src.application.strategy_lab.service import (
    StrategyLabContextError,
    StrategyLabServiceError,
    advance_experiment,
    advance_scheduled,
    confirm_research,
    confirm_validation,
    execute_research,
    get_experiment_status,
    list_recipes,
    preview_engineering_canary,
    preview_experiment,
    preview_validation,
    read_receipt,
    resolve_strategy_lab_context,
    resolve_strategy_lab_runtime_context,
)
from src.infrastructure.futu_gateway import (
    FutuGatewayError,
    build_futu_gateway,
)
from src.interfaces.cli.strategy_lab_parser import add_strategy_lab_commands


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_bounded_systemd_invocation(
    environ: Mapping[str, str] | None = None,
    pid: int | None = None,
    cgroup_text: str | None = None,
) -> bool:
    values = os.environ if environ is None else environ
    actual_pid = os.getpid() if pid is None else pid
    invocation = str(values.get("INVOCATION_ID") or "")
    systemd_pid = str(values.get("SYSTEMD_EXEC_PID") or "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", invocation) is None or not systemd_pid.isdigit():
        return False
    if int(systemd_pid) != actual_pid:
        return False
    if cgroup_text is None:
        try:
            cgroup_text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        except OSError:
            return False
    return any(STRATEGY_LAB_ADVANCE_SERVICE in line.strip().split("/") for line in cgroup_text.splitlines())


def _experiment_request(args: argparse.Namespace) -> dict[str, str]:
    return {
        "hypothesis": args.hypothesis,
        "recipe_id": args.recipe_id,
        "market": "hk",
        "account": "lx",
        "maturity_cutoff_utc": args.maturity_cutoff_utc,
        "fee_plan_receipt_path": args.fee_plan_receipt_path,
    }


def handle_strategy_lab_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.strategy_lab_command in {
        "recipes",
        "canary",
        "preview",
        "confirm-research",
        "preview-validation",
        "confirm-validation",
        "advance",
        "status",
        "research",
        "receipt",
    }:
        try:
            profile = load_service_profile(Path(args.profile_path).expanduser())
            context = (
                resolve_strategy_lab_runtime_context(profile, market="hk")
                if args.strategy_lab_command in {"canary", "status", "receipt"}
                else resolve_strategy_lab_context(profile)
            )
            if args.strategy_lab_command == "canary":
                data = preview_engineering_canary(
                    context,
                    occurred_at_utc=_now_utc(),
                )
            elif args.strategy_lab_command == "recipes":
                data = list_recipes(
                    context,
                    fee_plan_receipt_path=args.fee_plan_receipt_path,
                    maturity_cutoff_utc=args.maturity_cutoff_utc,
                    occurred_at_utc=_now_utc(),
                )
            elif args.strategy_lab_command == "preview":
                data = preview_experiment(
                    context,
                    _experiment_request(args),
                    occurred_at_utc=_now_utc(),
                )
            elif args.strategy_lab_command == "confirm-research":
                data = confirm_research(
                    context,
                    _experiment_request(args),
                    confirmed_preview_sha256=args.confirmed_preview_sha256,
                    actor=args.actor,
                    idempotency_key=args.idempotency_key,
                    occurred_at_utc=_now_utc(),
                )
            elif args.strategy_lab_command == "preview-validation":
                data = preview_validation(
                    context,
                    args.experiment_id,
                    args.requested_start,
                    occurred_at_utc=_now_utc(),
                )
            elif args.strategy_lab_command == "confirm-validation":
                data = confirm_validation(
                    context,
                    args.experiment_id,
                    args.requested_start,
                    confirmed_preview_sha256=args.confirmed_preview_sha256,
                    actor=args.actor,
                    idempotency_key=args.idempotency_key,
                    occurred_at_utc=_now_utc(),
                )
            elif args.strategy_lab_command == "advance":
                occurred_at_utc = _now_utc()
                if args.scheduled:
                    data = advance_scheduled(
                        context,
                        occurred_at_utc=occurred_at_utc,
                        provider_capable=_is_bounded_systemd_invocation(),
                    )
                else:
                    data = advance_experiment(
                        context,
                        args.experiment_id,
                        occurred_at_utc=occurred_at_utc,
                        provider_capable=False,
                    )
            elif args.strategy_lab_command == "status":
                data = get_experiment_status(context, args.experiment_id)
            elif args.strategy_lab_command == "receipt":
                data = read_receipt(context, args.experiment_id, kind=args.kind)
            elif args.strategy_lab_research_command == "execute":
                data = execute_research(
                    context,
                    args.experiment_id,
                    actor=args.actor,
                    occurred_at_utc=_now_utc(),
                )
            else:
                raise AgentToolError(code="INPUT_ERROR", message="unsupported research command")
        except (OSError, ValueError, StrategyLabServiceError) as exc:
            raise AgentToolError(
                code=str(getattr(exc, "reason_code", "CONFIG_ERROR")),
                message=str(exc),
            ) from exc
        return build_response(
            tool_name=(
                "strategy-lab.research.execute"
                if args.strategy_lab_command == "research"
                else f"strategy-lab.{args.strategy_lab_command}"
            ),
            ok=True,
            data=data,
        )

    if args.strategy_lab_command != "readiness" or args.strategy_lab_readiness_command != "refresh-history-k":
        raise AgentToolError(code="INPUT_ERROR", message="unsupported Strategy Lab command")
    try:
        profile = load_service_profile(Path(args.profile_path).expanduser())
        context = resolve_strategy_lab_context(profile)
        occurred_at_utc = _now_utc()
        preview = preview_history_k_readiness(
            market="HK",
            account=context["account"],
            opend_binding=context["opend_binding"],
            contract_symbol=args.contract_symbol,
            underlier_code=args.underlier_code,
            sample_date=args.sample_date,
            as_of_utc=occurred_at_utc,
        )
    except (OSError, ValueError, StrategyLabContextError, HistoryKReadinessError) as exc:
        raise AgentToolError(
            code=str(getattr(exc, "reason_code", "CONFIG_ERROR")),
            message=str(exc),
        ) from exc
    tool_name = "strategy-lab.readiness.refresh-history-k"
    if not args.write:
        return build_response(tool_name=tool_name, ok=True, data=preview)
    if not args.confirmed_probe_sha256 or not args.actor:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="history-K readiness refresh requires confirmed probe hash, actor, and --write",
        )
    try:
        _config_path, config = load_runtime_config(
            config_path=context["config_hk"],
            expected_market="hk",
        )
        limit = resolve_opend_fetch_limits(config).history_kline
        binding = context["opend_binding"]
        receipt = refresh_history_k_readiness(
            context["artifact_root"],
            gateway_factory=lambda: build_futu_gateway(
                host=str(binding["host"]),
                port=int(binding["port"]),
                is_option_chain_cache_enabled=False,
            ),
            request=preview["probe_request"],
            confirmed_probe_sha256=args.confirmed_probe_sha256,
            actor=args.actor,
            occurred_at_utc=occurred_at_utc,
            limiter_root=context["opend_limiter_root"],
            tick_lock_path=context["tick_lock_path"],
            window_sec=limit.window_sec,
            max_calls=limit.max_calls,
        )
    except (HistoryKReadinessError, FutuGatewayError) as exc:
        raise AgentToolError(
            code=str(getattr(exc, "reason_code", getattr(exc, "code", "ERROR"))),
            message=str(exc),
        ) from exc
    observation = receipt["provider_observation"]
    return build_response(
        tool_name=tool_name,
        ok=True,
        data={
            "status": observation["readiness_status"],
            "blockers": observation["blockers"],
            "probe_sha256": receipt["probe_sha256"],
            "observed_at_utc": receipt["observed_at_utc"],
            "expires_at_utc": receipt["expires_at_utc"],
            "receipt_ref": receipt["receipt_ref"],
            "receipt_content_sha256": receipt["content_sha256"],
            "receipt_file_sha256": receipt["receipt_file_sha256"],
            "provider_observation": observation,
        },
    )


__all__ = ["add_strategy_lab_commands", "handle_strategy_lab_command"]
