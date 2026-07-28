from __future__ import annotations

from typing import Any

from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.account_config import accounts_from_config
from src.application.agent_tools.runtime_helpers import healthcheck_symbols_for_futu
from src.application.futu_portfolio_context import infer_futu_portfolio_settings
from src.application.account_config import list_account_config_views
from src.application.ledger.api import open_position_ledger as load_option_positions_repo
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tools.runtime_helpers import mask_account_id
from src.application.agent_tool_contracts import mask_path
from src.application.account_config import normalize_accounts
from src.application.agent_tools.runtime_helpers import read_json_object_or_empty
from src.application.agent_tool_config import repo_base
from src.application.agent_tools.runtime_helpers import resolve_data_config_ref
from src.application.agent_tools.runtime_helpers import resolve_public_data_config_path
from src.application.agent_tools.runtime_helpers import run_futu_doctor
from src.application.agent_tools.runtime_helpers import validate_runtime_config
from src.application.agent_tool_config import write_tools_enabled


_OPERATION_TIMELINE_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "operation_timeline.output.v1",
    "source_label": "OM inbound operation and command audit stores",
    "primary_rows": "timelines",
    "row_count_field": "timeline_count",
    "fact_fields": [
        "audit_db_exists",
        "timeline_count",
        "timelines[].identity.operation_id",
        "timelines[].operation.operation_type",
        "timelines[].operation.created_at",
        "timelines[].outcome.status",
        "timelines[].action_lifecycle.phase",
        "timelines[].action_lifecycle.verify_status",
        "timelines[].warnings",
        "warnings[]",
    ],
    "missing_data_fields": ["audit_db_exists", "warnings[]", "timelines[].warnings"],
}


_HEALTHCHECK_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "healthcheck.output.v1",
    "source_label": "OM 本地健康检查",
    "primary_rows": "checks",
    "fact_fields": [
        "summary.ok",
        "summary.critical_count",
        "summary.warning_count",
        "checks[].name",
        "checks[].status",
        "checks[].message",
    ],
}

_RUNTIME_STATUS_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "runtime_status.output.v1",
    "source_label": "OM 本地 runtime_status",
    "result_shape": "scalar",
    "fact_fields": [
        "summary.ok",
        "summary.latest_status",
        "summary.ledger_status",
        "summary.ledger_position_lot_count",
        "summary.ledger_trade_event_count",
        "summary.projection_verify_ok",
        "summary.warning_count",
        "summary.notification_status",
        "summary.notification_reason",
        "notification_diagnosis.status",
        "notification_diagnosis.reason",
        "notification_diagnosis.send_attempted_count",
        "notification_diagnosis.send_confirmed_count",
        "notification_diagnosis.send_failed_count",
        "notification_diagnosis.ambiguous_send_count",
        "notification_diagnosis.duplicate_risk_count",
        "notification_authority.ordinary_scheduled_renderer",
        "notification_authority.compatibility_artifact.authority",
        "notification_authority.compatibility_artifact.delivery_evidence",
        "shared.compatibility_notification.artifact_kind",
        "shared.compatibility_notification.primary_renderer",
        "shared.compatibility_notification.authority",
        "shared.compatibility_notification.delivery_evidence",
        "account_summary.accounts_with_compatibility_notification",
        "latest_run.path",
        "latest_scanned_run.path",
    ],
    "freshness_fields": ["freshness.status", "freshness.latest_run_age_minutes", "freshness.max_run_age_minutes"],
    "missing_data_fields": [
        "summary.latest_status",
        "summary.ledger_status",
        "summary.projection_verify_ok",
        "summary.service_upgrade_error",
    ],
    "model_preview_fields": [
        "summary",
        "notification_diagnosis",
        "notification_authority",
        "freshness",
        "account_summary",
        "shared.compatibility_notification",
        "warnings",
    ],
}

def _mask_path_str(value: Any) -> str:
    return mask_path(value) or "..."


def _healthcheck_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tools.healthcheck_impl import run_healthcheck_tool

    return run_healthcheck_tool(
        payload,
        load_runtime_config=load_runtime_config,
        validate_runtime_config=validate_runtime_config,
        normalize_accounts=normalize_accounts,
        accounts_from_config=accounts_from_config,
        resolve_data_config_ref=resolve_data_config_ref,
        resolve_public_data_config_path=resolve_public_data_config_path,
        read_json_object_or_empty=read_json_object_or_empty,
        mask_path=lambda value: _mask_path_str(value),
        list_account_config_views=list_account_config_views,
        mask_account_id=mask_account_id,
        infer_futu_portfolio_settings=infer_futu_portfolio_settings,
        load_option_positions_repo=load_option_positions_repo,
        run_futu_doctor=run_futu_doctor,
        healthcheck_symbols_for_futu=healthcheck_symbols_for_futu,
        write_tools_enabled=write_tools_enabled,
    )


def _runtime_status_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tools.runtime_status_impl import runtime_status_tool

    return runtime_status_tool(
        payload,
        load_runtime_config=load_runtime_config,
        normalize_accounts=normalize_accounts,
        accounts_from_config=accounts_from_config,
        read_json_object_or_empty=read_json_object_or_empty,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _operation_timeline_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.assistant.operation_diagnostics import collect_operation_timeline

    data = collect_operation_timeline(
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


HEALTHCHECK_TOOL = build_agent_tool(
    name="healthcheck",
    description=(
        "Run dependency and configuration readiness checks. Use for broad readiness diagnosis; use runtime_status "
        "for the latest operational snapshot and runtime_logs only after a failing component is identified."
    ),
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
    output_contract=_HEALTHCHECK_OUTPUT_CONTRACT,
    copilot_input_fields=("config_key", "accounts", "timeout_sec", "include_service_status"),
)

RUNTIME_STATUS_TOOL = build_agent_tool(
    name="runtime_status",
    description=(
        "Summarize current overall runtime health from existing artifacts. Use first for current status questions; "
        "use runtime_runs for historical run selection and runtime_logs for detailed failure text."
    ),
    requires=("runtime_config",),
    capabilities=("status", "read_only"),
    input_schema={
        "config_key": {"type": "string", "enum": ["us", "hk"], "description": "Market config"},
        "config_path": "optional explicit config path",
        "accounts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional account labels",
        },
        "report_dir": "optional report dir; defaults to output_shared/reports",
        "state_dir": "optional shared state dir; defaults to output_shared/state",
        "shared_state_dir": "optional shared state dir; defaults to output_shared/state",
        "accounts_root": "optional accounts output root; defaults to output_accounts",
        "runs_root": "optional run history root; defaults to output_runs",
        "run_id": "optional output_runs run id to inspect instead of the last_run_dir pointer",
        "run_dir": "optional explicit run directory to inspect instead of the last_run_dir pointer",
        "max_notification_chars": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20000,
            "description": "Maximum notification preview characters",
        },
        "max_run_age_minutes": {
            "type": "integer",
            "minimum": 1,
            "description": "Freshness threshold; defaults to 60",
        },
        "profile_path": "optional service.profile.json path",
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
    output_contract=_RUNTIME_STATUS_OUTPUT_CONTRACT,
    copilot_input_fields=(
        "config_key", "accounts", "run_id", "max_notification_chars", "max_run_age_minutes", "include_service_status"
    ),
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
    output_contract=_OPERATION_TIMELINE_OUTPUT_CONTRACT,
    copilot_input_fields=(
        "channel", "sender_id", "conversation_id", "operation_id", "operation_types", "statuses", "limit"
    ),
)

TOOLS: tuple[AgentTool, ...] = (
    HEALTHCHECK_TOOL,
    RUNTIME_STATUS_TOOL,
    OPERATION_TIMELINE_TOOL,
)


__all__ = [
    "HEALTHCHECK_TOOL",
    "OPENCLAW_READINESS_TOOL",
    "OPERATION_TIMELINE_TOOL",
    "RUNTIME_STATUS_TOOL",
    "TOOLS",
]
