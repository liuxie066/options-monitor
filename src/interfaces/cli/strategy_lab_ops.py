from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.opend_fetch_config import resolve_opend_fetch_limits
from src.application.service_deploy import load_service_profile
from src.application.strategy_lab.readiness import (
    HistoryKReadinessError,
    preview_history_k_readiness,
    refresh_history_k_readiness,
)
from src.application.strategy_lab.service import (
    StrategyLabContextError,
    StrategyLabServiceError,
    confirm_research,
    confirm_validation,
    execute_research,
    get_experiment_status,
    list_recipes,
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


def add_strategy_lab_commands(subparsers: Any) -> argparse.ArgumentParser:
    strategy_lab = subparsers.add_parser("strategy-lab", help="operate the Strategy Lab")
    commands = strategy_lab.add_subparsers(dest="strategy_lab_command", required=True)
    readiness = commands.add_parser("readiness", help="manage targeted readiness evidence")
    readiness_commands = readiness.add_subparsers(dest="strategy_lab_readiness_command", required=True)
    refresh = readiness_commands.add_parser(
        "refresh-history-k",
        help="preview or publish one targeted history-K readiness PoC",
    )
    refresh.add_argument("--profile-path", required=True)
    refresh.add_argument("--contract-symbol", required=True)
    refresh.add_argument("--underlier-code", required=True)
    refresh.add_argument("--sample-date", required=True)
    refresh.add_argument("--confirmed-probe-sha256", default=None)
    refresh.add_argument("--actor", default=None)
    refresh.add_argument("--write", action="store_true")

    recipes = commands.add_parser("recipes", help="list currently supported experiment recipes")
    recipes.add_argument("--profile-path", required=True)
    recipes.add_argument("--fee-plan-receipt-path", required=True)
    recipes.add_argument("--maturity-cutoff-utc", required=True)

    preview = commands.add_parser("preview", help="preview a read-only experiment")
    preview.add_argument("--profile-path", required=True)
    preview.add_argument("--hypothesis", required=True)
    preview.add_argument("--recipe-id", required=True)
    preview.add_argument("--fee-plan-receipt-path", required=True)
    preview.add_argument("--maturity-cutoff-utc", required=True)

    confirm = commands.add_parser("confirm-research", help="confirm one current experiment preview")
    confirm.add_argument("--profile-path", required=True)
    confirm.add_argument("--hypothesis", required=True)
    confirm.add_argument("--recipe-id", required=True)
    confirm.add_argument("--fee-plan-receipt-path", required=True)
    confirm.add_argument("--maturity-cutoff-utc", required=True)
    confirm.add_argument("--confirmed-preview-sha256", required=True)
    confirm.add_argument("--actor", required=True)
    confirm.add_argument("--idempotency-key", required=True)

    validation_preview = commands.add_parser(
        "preview-validation", help="preview the next 10 validation sessions"
    )
    validation_preview.add_argument("--profile-path", required=True)
    validation_preview.add_argument("--experiment-id", required=True)
    validation_preview.add_argument("--requested-start", required=True)

    validation_confirm = commands.add_parser(
        "confirm-validation", help="confirm one current validation preview"
    )
    validation_confirm.add_argument("--profile-path", required=True)
    validation_confirm.add_argument("--experiment-id", required=True)
    validation_confirm.add_argument("--requested-start", required=True)
    validation_confirm.add_argument("--confirmed-preview-sha256", required=True)
    validation_confirm.add_argument("--actor", required=True)
    validation_confirm.add_argument("--idempotency-key", required=True)

    status = commands.add_parser("status", help="read one experiment status")
    status.add_argument("--profile-path", required=True)
    status.add_argument("--experiment-id", required=True)

    research = commands.add_parser("research", help="resume bounded local research")
    research_commands = research.add_subparsers(dest="strategy_lab_research_command", required=True)
    execute = research_commands.add_parser("execute", help="execute at most one provider unit")
    execute.add_argument("--profile-path", required=True)
    execute.add_argument("--experiment-id", required=True)
    execute.add_argument("--actor", required=True)

    receipt = commands.add_parser("receipt", help="read one Research Receipt")
    receipt.add_argument("--profile-path", required=True)
    receipt.add_argument("--experiment-id", required=True)
    return strategy_lab


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        "preview",
        "confirm-research",
        "preview-validation",
        "confirm-validation",
        "status",
        "research",
        "receipt",
    }:
        try:
            profile = load_service_profile(Path(args.profile_path).expanduser())
            context = (
                resolve_strategy_lab_runtime_context(profile, market="hk")
                if args.strategy_lab_command in {"status", "receipt"}
                else resolve_strategy_lab_context(profile)
            )
            if args.strategy_lab_command == "recipes":
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
            elif args.strategy_lab_command == "status":
                data = get_experiment_status(context, args.experiment_id)
            elif args.strategy_lab_command == "receipt":
                data = read_receipt(context, args.experiment_id)
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

    if (
        args.strategy_lab_command != "readiness"
        or args.strategy_lab_readiness_command != "refresh-history-k"
    ):
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
