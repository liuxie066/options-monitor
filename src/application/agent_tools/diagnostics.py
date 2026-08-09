from __future__ import annotations

from typing import Any

from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.account_config import accounts_from_config
from src.application.futu_portfolio_context import infer_futu_portfolio_settings
from src.application.account_config import list_account_config_views
from src.application.ledger.api import open_position_ledger as load_option_positions_repo
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tools.runtime_helpers import mask_account_id
from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.account_config import normalize_accounts
from src.application.account_config import resolve_account_broker_binding_sets
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.application.agent_tools.runtime_helpers import read_json_object_or_empty
from src.application.agent_tool_config import repo_base
from src.application.agent_tools.runtime_helpers import resolve_data_config_ref
from src.application.agent_tools.runtime_helpers import resolve_public_data_config_path
from src.application.agent_tools.runtime_helpers import run_futu_doctor
from src.application.agent_tools.runtime_helpers import validate_runtime_config
from src.application.agent_tool_config import write_tools_enabled
from src.infrastructure.futu_gateway import (
    build_ready_futu_broker_gateway,
)


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
        "checks[].summary_excluded",
        "checks[].value.capability",
        "checks[].value.capabilities",
        "checks[].value.required_account_id_count",
        "checks[].value.matched_account_id_count",
        "checks[].value.masked_required_account_ids",
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
        "summary.latest_run_id",
        "summary.latest_scanned_run_id",
    ],
    "freshness_fields": ["freshness.status", "freshness.age_seconds", "freshness.max_age_minutes"],
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
        write_tools_enabled=write_tools_enabled,
        resolve_account_broker_binding_sets=resolve_account_broker_binding_sets,
        resolve_futu_quote_route=resolve_futu_quote_route,
        build_ready_futu_broker_gateway=build_ready_futu_broker_gateway,
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


def _private_runtime_status_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Collect full local status for trusted in-process operational checks only."""

    from src.application.agent_tools.runtime_status_impl import private_runtime_status_tool

    return private_runtime_status_tool(
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
    from src.application.assistant.operation_diagnostics import collect_operation_timeline, format_operation_timeline

    channel, sender_id, conversation_id = _diagnostic_identity_scope(payload)

    data = collect_operation_timeline(
        audit_db=payload.get("audit_db") or payload.get("inbound_audit_db"),
        channel=channel,
        sender_id=sender_id,
        conversation_id=conversation_id,
        operation_id=payload.get("operation_id"),
        operation_types=payload.get("operation_types"),
        statuses=payload.get("statuses"),
        limit=int(payload.get("limit") or 10),
        audit_scan_limit=payload.get("audit_scan_limit"),
    )
    data = _status_safe_operation_timeline(
        data,
        channel=channel,
        format_timeline=format_operation_timeline,
    )
    warnings = [str(item) for item in data.get("warnings", []) if str(item).strip()]
    return data, warnings, {"audit_db": data.get("audit_db")}


def _diagnostic_identity_scope(payload: dict[str, Any]) -> tuple[str, str, str]:
    authenticated = {
        "channel": str(payload.get("authenticated_channel") or "").strip().lower(),
        "sender_id": str(payload.get("authenticated_sender_id") or "").strip(),
        "conversation_id": str(payload.get("authenticated_conversation_id") or "").strip(),
    }
    explicit = {
        "channel": str(payload.get("channel") or "").strip().lower(),
        "sender_id": str(payload.get("sender_id") or "").strip(),
        "conversation_id": str(payload.get("conversation_id") or "").strip(),
    }
    if any(authenticated.values()):
        if not all(authenticated.values()):
            raise AgentToolError(
                code="POLICY_ERROR",
                message="authenticated diagnostic scope is incomplete",
            )
        mismatches = [
            key
            for key, value in explicit.items()
            if value and value != authenticated[key]
        ]
        if mismatches:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="diagnostic scope cannot override the authenticated conversation",
            )
        resolved = authenticated
    else:
        resolved = explicit
    if not all(resolved.values()):
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="operation_timeline requires channel, sender_id, and conversation_id scope",
            hint="Pass the complete operator conversation scope; unscoped forensic reads are not available from this tool.",
        )
    return resolved["channel"], resolved["sender_id"], resolved["conversation_id"]


def _status_safe_operation_timeline(
    data: dict[str, Any],
    *,
    channel: str,
    format_timeline: Any,
) -> dict[str, Any]:
    timelines: list[dict[str, Any]] = []
    safe_operation_fields = {
        "operation_id",
        "operation_type",
        "status",
        "current_version",
        "target_version",
        "release_tag",
        "release_status",
        "created_at",
        "expires_at",
        "confirmed_at",
        "applied_at",
        "cancelled_at",
        "source",
    }
    for raw in data.get("timelines") or []:
        if not isinstance(raw, dict):
            continue
        identity = raw.get("identity") if isinstance(raw.get("identity"), dict) else {}
        operation = raw.get("operation") if isinstance(raw.get("operation"), dict) else {}
        audit = raw.get("audit") if isinstance(raw.get("audit"), dict) else {}
        receipt = raw.get("receipt") if isinstance(raw.get("receipt"), dict) else {}
        ledger = raw.get("ledger") if isinstance(raw.get("ledger"), dict) else {}
        outcome = raw.get("outcome") if isinstance(raw.get("outcome"), dict) else {}
        lifecycle = raw.get("action_lifecycle") if isinstance(raw.get("action_lifecycle"), dict) else {}
        timelines.append(
            {
                "schema_version": raw.get("schema_version"),
                "identity": {"operation_id": identity.get("operation_id")},
                "operation": {
                    key: operation.get(key)
                    for key in safe_operation_fields
                    if operation.get(key) is not None
                },
                "action_lifecycle": dict(lifecycle),
                "audit": {
                    "related_count": int(audit.get("related_count") or 0),
                    "apply_count": int(audit.get("apply_count") or 0),
                },
                "ledger": {"present": bool(ledger.get("present"))},
                "receipt": {
                    key: receipt.get(key)
                    for key in ("status", "delivery_confirmed", "error_code")
                    if receipt.get(key) is not None
                },
                "outcome": {
                    "status": outcome.get("status"),
                    "ok": bool(outcome.get("ok")),
                    "warnings": list(outcome.get("warnings") or []),
                },
                "warnings": list(raw.get("warnings") or []),
            }
        )
    warnings = [str(item) for item in data.get("warnings") or [] if str(item).strip()]
    filters = {
        "channel": channel,
        "operation_id": (data.get("filters") or {}).get("operation_id") if isinstance(data.get("filters"), dict) else None,
        "operation_types": (data.get("filters") or {}).get("operation_types") if isinstance(data.get("filters"), dict) else None,
        "statuses": (data.get("filters") or {}).get("statuses") if isinstance(data.get("filters"), dict) else None,
        "limit": (data.get("filters") or {}).get("limit") if isinstance(data.get("filters"), dict) else None,
    }
    filters = {key: value for key, value in filters.items() if value not in (None, "", [])}
    return {
        "schema_version": data.get("schema_version"),
        "audit_db": data.get("audit_db"),
        "audit_db_exists": bool(data.get("audit_db_exists")),
        "scope": {"channel": channel, "authenticated_conversation": True},
        "filters": filters,
        "timeline_count": len(timelines),
        "timelines": timelines,
        "warnings": warnings,
        "response_text": format_timeline(timelines, filters=filters, warnings=warnings),
    }


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
        "use runtime_runs for historical run selection and runtime_logs for bounded, content-free log metadata."
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
        "authenticated_channel": "host-injected authenticated channel scope",
        "authenticated_sender_id": "host-injected authenticated sender scope",
        "authenticated_conversation_id": "host-injected authenticated conversation scope",
        "operation_id": "optional exact pending operation id",
        "operation_types": "optional list[str] such as manual_open/manual_close",
        "statuses": "optional list[str] of operation statuses; defaults to operator lifecycle statuses",
        "limit": "optional number of recent operation timelines to return; defaults to 10",
        "audit_scan_limit": "optional number of recent audit rows to scan for response/receipt links",
    },
    handler=_operation_timeline_tool,
    pure_read=True,
    safe_default_input={},
    examples=(
        {
            "input": {
                "channel": "feishu",
                "sender_id": "operator-reference",
                "conversation_id": "conversation-reference",
                "limit": 10,
            }
        },
    ),
    output_contract=_OPERATION_TIMELINE_OUTPUT_CONTRACT,
    copilot_input_fields=(
        "operation_id", "operation_types", "statuses", "limit"
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
