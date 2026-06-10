from __future__ import annotations

from typing import Any

from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


def _mask_path_str(ctx: AgentToolContext, value: Any) -> str:
    return ctx.mask_path(value) or "..."


def _healthcheck_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tool_healthcheck import run_healthcheck_tool

    return run_healthcheck_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        validate_runtime_config=ctx.validate_runtime_config,
        normalize_accounts=ctx.normalize_accounts,
        accounts_from_config=ctx.accounts_from_config,
        resolve_data_config_ref=ctx.resolve_data_config_ref,
        resolve_public_data_config_path=ctx.resolve_public_data_config_path,
        read_json_object_or_empty=ctx.read_json_object_or_empty,
        mask_path=lambda value: _mask_path_str(ctx, value),
        list_account_config_views=ctx.list_account_config_views,
        mask_account_id=ctx.mask_account_id,
        infer_futu_portfolio_settings=ctx.infer_futu_portfolio_settings,
        load_option_positions_repo=ctx.load_option_positions_repo,
        run_futu_doctor=ctx.run_futu_doctor,
        healthcheck_symbols_for_futu=ctx.healthcheck_symbols_for_futu,
        write_tools_enabled=ctx.write_tools_enabled,
    )


def _runtime_status_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tool_runtime_status import runtime_status_tool

    return runtime_status_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        normalize_accounts=ctx.normalize_accounts,
        accounts_from_config=ctx.accounts_from_config,
        read_json_object_or_empty=ctx.read_json_object_or_empty,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )


def _operation_timeline_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    data = ctx.collect_operation_timeline(
        audit_db=payload.get("audit_db") or payload.get("inbound_audit_db"),
        channel=payload.get("channel"),
        sender_id=payload.get("sender_id"),
        conversation_id=payload.get("conversation_id"),
        operation_id=payload.get("operation_id"),
        operation_types=payload.get("operation_types"),
        statuses=payload.get("statuses"),
        limit=int(payload.get("limit") or 10),
        audit_scan_limit=payload.get("audit_scan_limit"),
    )
    warnings = [str(item) for item in data.get("warnings", []) if str(item).strip()]
    return data, warnings, {"audit_db": data.get("audit_db")}


def _openclaw_readiness_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tool_openclaw import openclaw_readiness_tool

    return openclaw_readiness_tool(
        payload,
        runtime_status_tool_fn=lambda inner_payload: _runtime_status_tool(ctx, inner_payload),
        healthcheck_tool_fn=lambda inner_payload: _healthcheck_tool(ctx, inner_payload),
        load_runtime_config=ctx.load_runtime_config,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )


HEALTHCHECK_TOOL = build_agent_tool(
    name="healthcheck",
    description="Validate runtime config and summarize local readiness without sending notifications or writing remote data.",
    requires=("runtime_config", "sqlite_data_config", "opend"),
    capabilities=("diagnostics", "read_only"),
    input_schema={
        "config_key": "us|hk (optional when config_path is set)",
        "config_path": "absolute or relative JSON config path",
        "accounts": "optional list[str]",
        "data_config": "optional explicit data config path",
        "timeout_sec": "optional int",
        "opend_telnet_host": "optional OpenD telnet host; defaults to 127.0.0.1",
        "opend_telnet_port": "optional OpenD telnet port; defaults to 22222",
        "audit_db": "optional inbound audit SQLite path for Feishu inbound diagnostics",
        "profile_path": "optional service.profile.json path for Feishu WS service diagnostics",
        "include_service_status": "optional bool; run local service status checks from profile_path",
        "candidate_report_dir": "optional directory containing candidate/reject/trace evidence for diagnostic readiness checks",
        "candidate_paths": "optional list of candidate evidence files",
        "candidate_reject_log_paths": "optional list of reject-log evidence files",
        "candidate_trace_paths": "optional list of candidate-filter trace files",
        "candidate_evidence_min_sample": "optional int; default 5; minimum candidate rows for readiness checks",
    },
    handler=_healthcheck_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us"}},),
    answer_policy="facts_then_analysis",
)

RUNTIME_STATUS_TOOL = build_agent_tool(
    name="runtime_status",
    description="Summarize existing OpenClaw/runtime output files without running pipelines or sending notifications.",
    requires=("runtime_config",),
    capabilities=("status", "read_only", "openclaw"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "accounts": "optional list[str]",
        "report_dir": "optional report dir; defaults to output_shared/reports",
        "state_dir": "optional shared state dir; defaults to output_shared/state",
        "shared_state_dir": "optional shared state dir; defaults to output_shared/state",
        "accounts_root": "optional accounts output root; defaults to output_accounts",
        "runs_root": "optional run history root; defaults to output_runs",
        "run_id": "optional output_runs run id to inspect instead of the last_run_dir pointer",
        "run_dir": "optional explicit run directory to inspect instead of the last_run_dir pointer",
        "max_notification_chars": "optional int, capped at 20000",
        "max_run_age_minutes": "optional freshness threshold; defaults to 60",
        "profile_path": "optional OpenClaw profile JSON path",
        "include_service_status": "optional bool; inspect configured systemd/launchd service status when a service profile is loaded",
        "trigger_source": "optional outer runner source such as cron or om_direct",
        "trigger_job_id": "optional outer runner job id",
        "delivery": "optional outer runner delivery object, e.g. {'mode':'announce'}",
        "delivery_mode": "optional outer runner delivery mode such as none or announce",
        "timeoutSeconds": "optional outer runner timeout in seconds",
    },
    handler=_runtime_status_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us", "max_notification_chars": 2000}},),
    answer_policy="facts_then_analysis",
)

OPERATION_TIMELINE_TOOL = build_agent_tool(
    name="operation_timeline",
    description=(
        "Read existing inbound audit and pending operation stores to reconstruct recent operator operation "
        "timelines without mutating ledger, channel, or tick state."
    ),
    requires=("inbound_audit_db",),
    capabilities=("operation_timeline", "assistant_diagnostics", "read_only"),
    input_schema={
        "audit_db": "optional inbound audit SQLite path",
        "inbound_audit_db": "optional alias for audit_db",
        "channel": "optional channel filter such as feishu",
        "sender_id": "optional operator sender id filter",
        "conversation_id": "optional conversation filter",
        "operation_id": "optional exact pending operation id",
        "operation_types": "optional list[str] such as manual_open/manual_close",
        "statuses": "optional list[str] of operation statuses; defaults to operator lifecycle statuses",
        "limit": "optional number of recent operation timelines to return; defaults to 10",
        "audit_scan_limit": "optional number of recent audit rows to scan for response/receipt links",
    },
    handler=_operation_timeline_tool,
    pure_read=True,
    safe_default_input={"limit": 10},
    examples=(
        {"input": {"limit": 10}},
        {"input": {"channel": "feishu", "sender_id": "ou_1", "operation_types": ["manual_open"], "limit": 10}},
    ),
)

OPENCLAW_READINESS_TOOL = build_agent_tool(
    name="openclaw_readiness",
    description="OpenClaw-oriented readiness summary combining runtime_status, healthcheck, and local openclaw command availability.",
    requires=("runtime_config",),
    capabilities=("diagnostics", "read_only", "openclaw"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "accounts": "optional list[str]",
        "data_config": "optional explicit data config path for healthcheck",
        "timeout_sec": "optional int for healthcheck OpenD readiness probe",
        "max_notification_chars": "optional int, forwarded to runtime_status",
        "max_run_age_minutes": "optional freshness threshold for runtime_status",
        "profile_path": "optional OpenClaw profile JSON path",
        "cron_jobs": "optional list of OpenClaw cron jobs with id/name/schedule",
        "include_cron_status": "optional bool; run read-only openclaw cron list/runs when true",
        "openclaw_command_timeout_sec": "optional int timeout for read-only openclaw CLI checks",
        "delivery": "optional outer runner delivery object, forwarded to runtime_status",
        "delivery_mode": "optional outer runner delivery mode, forwarded to runtime_status",
        "timeoutSeconds": "optional outer runner timeout in seconds, forwarded to runtime_status",
    },
    handler=_openclaw_readiness_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us", "timeout_sec": 20}},),
)

TOOLS: tuple[AgentTool, ...] = (
    HEALTHCHECK_TOOL,
    RUNTIME_STATUS_TOOL,
    OPERATION_TIMELINE_TOOL,
    OPENCLAW_READINESS_TOOL,
)


__all__ = [
    "HEALTHCHECK_TOOL",
    "OPENCLAW_READINESS_TOOL",
    "OPERATION_TIMELINE_TOOL",
    "RUNTIME_STATUS_TOOL",
    "TOOLS",
]
