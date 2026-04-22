from __future__ import annotations

from importlib import import_module


_EXPORTS: dict[str, str] = {
    'apply_scan_run_decision': '.multi_tick',
    'cash_footer_for_account': '.multi_tick',
    'decide_notify_dispatch': '.multi_tick',
    'decide_should_notify': '.multi_tick',
    'evaluate_dnd_quiet_hours': '.multi_tick',
    'filter_notify_candidates': '.multi_tick',
    'is_in_quiet_hours_window': '.multi_tick',
    'markets_for_trading_day_guard': '.multi_tick',
    'reduce_trading_day_guard': '.multi_tick',
    'resolve_notification_channel_target': '.multi_tick',
    'resolve_notification_route_from_config': '.multi_tick',
    'resolve_scheduler_state_path': '.multi_tick',
    'select_markets_to_run': '.multi_tick',
    'select_scheduler_state_filename': '.multi_tick',
    'build_account_messages': '.multi_tick_result',
    'build_no_candidate_account_messages': '.multi_tick_result',
    'build_no_account_notification_payloads': '.multi_tick_result',
    'build_shared_last_run_payload': '.multi_tick_result',
    'SCHEMA_KIND_SUBPROCESS_ADAPTER': '.tool_boundary',
    'SCHEMA_KIND_SCHEDULER_DECISION': '.tool_boundary',
    'SCHEMA_KIND_TOOL_EXECUTION': '.tool_boundary',
    'SCHEMA_VERSION_V1': '.tool_boundary',
    'build_tool_idempotency_key': '.tool_boundary',
    'normalize_notify_subprocess_output': '.tool_boundary',
    'normalize_pipeline_subprocess_output': '.tool_boundary',
    'normalize_scheduler_decision_payload': '.tool_boundary',
    'normalize_subprocess_adapter_payload': '.tool_boundary',
    'normalize_tool_execution_payload': '.tool_boundary',
    'normalize_watchdog_subprocess_output': '.tool_boundary',
    'validate_schema_payload': '.tool_boundary',
    'CANONICAL_SCHEMA_VERSION_V1': '.canonical_schema',
    'SCHEMA_KIND_PROCESSOR_OUTPUT': '.canonical_schema',
    'SCHEMA_KIND_SOURCE_SNAPSHOT': '.canonical_schema',
    'normalize_processor_row': '.canonical_schema',
    'normalize_processor_rows': '.canonical_schema',
    'normalize_source_snapshot': '.canonical_schema',
    'validate_canonical_payload': '.canonical_schema',
    'ERR_2FA_REQUIRED': '.error_policy',
    'ERR_CONFIG': '.error_policy',
    'ERR_TIMEOUT': '.error_policy',
    'ERR_UNEXPECTED': '.error_policy',
    'ERR_UPSTREAM_UNAVAILABLE': '.error_policy',
    'classify_failure': '.error_policy',
    'CANONICAL_CONFIGS': '.config_contract',
    'DERIVED_CONFIGS': '.config_contract',
    'ensure_runtime_canonical_config': '.config_contract',
    'resolve_config_contract': '.config_contract',
    'SCHEMA_KIND_DECISION': '.intermediate_objects',
    'SCHEMA_KIND_DELIVERY_PLAN': '.intermediate_objects',
    'SCHEMA_KIND_SNAPSHOT_DTO': '.intermediate_objects',
    'Decision': '.intermediate_objects',
    'DeliveryPlan': '.intermediate_objects',
    'SchemaValidationError': '.intermediate_objects',
    'SnapshotDTO': '.intermediate_objects',
    'build_failure_audit_fields': '.engine',
}

_MODULE_EXPORTS = {
    'multi_tick',
    'multi_tick_result',
    'tool_boundary',
    'canonical_schema',
    'error_policy',
    'config_contract',
    'intermediate_objects',
    'engine',
    'fetch_source',
    'close_advice',
}

__all__ = [*_EXPORTS.keys(), *_MODULE_EXPORTS]


def __getattr__(name: str):
    if name in _MODULE_EXPORTS:
        return import_module(f'.{name}', __name__)
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(module_name, __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
