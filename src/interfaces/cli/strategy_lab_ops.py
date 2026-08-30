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
    resolve_strategy_lab_context,
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
    return strategy_lab


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def handle_strategy_lab_command(args: argparse.Namespace) -> dict[str, Any]:
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
