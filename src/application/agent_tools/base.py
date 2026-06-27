from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.application.tool_input_schema import build_tool_input_json_schema, validate_tool_input_payload

ToolHandlerResult = tuple[dict[str, Any], list[str], dict[str, Any]]
ToolHandler = Callable[["AgentToolContext", dict[str, Any]], ToolHandlerResult]
InputValidator = Callable[[dict[str, Any]], None]
WriteRequestPredicate = Callable[[dict[str, Any]], bool]
AnswerPolicyResolver = Callable[[dict[str, Any]], str | None]
OutputContractResolver = Callable[[dict[str, Any]], dict[str, Any] | None]
PlannerSemanticsResolver = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class AgentToolContext:
    repo_base: Callable[[], Path]
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]]
    resolve_output_root: Callable[..., Path]
    mask_path: Callable[[Any], str | None]
    validate_runtime_config: Callable[..., list[str]]
    normalize_accounts: Callable[..., list[str]]
    accounts_from_config: Callable[..., list[str]]
    resolve_watchlist_config: Callable[..., list[dict[str, Any]]]
    resolve_data_config_ref: Callable[..., Any]
    resolve_public_data_config_path: Callable[..., Any]
    read_json_object_or_empty: Callable[..., dict[str, Any]]
    list_account_config_views: Callable[..., Any]
    mask_account_id: Callable[[Any], str]
    infer_futu_portfolio_settings: Callable[..., Any]
    refresh_assigned_stock_quotes: Callable[..., Any]
    load_option_positions_repo: Callable[..., Any]
    run_futu_doctor: Callable[..., dict[str, Any]]
    healthcheck_symbols_for_futu: Callable[..., list[str]]
    write_tools_enabled: Callable[[], bool]
    read_scheduler_state: Callable[..., Any]
    scheduler_decide: Callable[..., Any]
    normalize_broker: Callable[..., str]
    normalize_account: Callable[..., str]
    resolve_option_positions_repo: Callable[..., Any]
    list_position_rows: Callable[..., Any]
    build_lot_event_history: Callable[..., Any]
    inspect_projection_state: Callable[..., Any]
    build_monthly_income_report: Callable[..., Any]
    get_exchange_rates: Callable[..., Any]
    build_notification: Callable[..., str]
    collect_operation_timeline: Callable[..., dict[str, Any]]
    collect_assistant_trace: Callable[..., dict[str, Any]]
    check_version_update: Callable[..., dict[str, Any]]
    update_local_version: Callable[..., dict[str, Any]]
    collect_runtime_runs: Callable[..., dict[str, Any]]
    collect_runtime_logs: Callable[..., dict[str, Any]]
    load_runtime_pipeline_config: Callable[..., dict[str, Any]]
    run_watchlist_pipeline_default: Callable[..., Any]
    query_sell_put_cash: Callable[..., dict[str, Any]]
    load_portfolio_context: Callable[..., Any]
    load_option_positions_context: Callable[..., Any]
    symbol_fetch_config_map: Callable[..., dict[str, Any]]
    extract_context_symbols: Callable[..., list[str]]
    resolve_symbol_fetch_source: Callable[..., Any]
    fetch_symbol_opend: Callable[..., Any]
    save_required_data_opend: Callable[..., Any]
    resolve_local_path: Callable[..., Path]
    run_close_advice: Callable[..., dict[str, Any]]
    safe_read_csv: Callable[..., Any]
    as_float: Callable[[Any], float | None]
    deepcopy_value: Callable[[Any], Any]
    apply_symbol_mutation: Callable[..., dict[str, Any]]
    list_symbol_rows: Callable[..., list[dict[str, Any]]]
    write_json_atomic: Callable[..., Any]


@dataclass(frozen=True)
class AgentTool:
    name: str
    read_only: bool
    description: str
    requires: tuple[str, ...]
    capabilities: tuple[str, ...]
    input_schema: dict[str, Any]
    handler: ToolHandler = field(repr=False, compare=False)
    enabled: bool = True
    side_effects: tuple[str, ...] = ()
    risk_level: str | None = None
    requires_confirm: bool = False
    requires_env: tuple[str, ...] = ()
    safe_default_input: dict[str, Any] = field(default_factory=dict)
    examples: tuple[dict[str, Any], ...] = ()
    write_request_predicate: WriteRequestPredicate | None = field(default=None, repr=False, compare=False)
    input_validator: InputValidator | None = field(default=None, repr=False, compare=False)
    answer_policy: str = "default"
    answer_policy_resolver: AnswerPolicyResolver | None = field(default=None, repr=False, compare=False)
    output_contract: dict[str, Any] = field(default_factory=dict)
    output_contract_resolver: OutputContractResolver | None = field(default=None, repr=False, compare=False)
    planner_notes: tuple[str, ...] = ()
    planner_semantics: dict[str, Any] = field(default_factory=dict)
    planner_semantics_resolver: PlannerSemanticsResolver | None = field(default=None, repr=False, compare=False)

    def resolved_risk_level(self) -> str:
        return self.risk_level or ("local_write" if self.side_effects else "read_only")

    def is_pure_read(self) -> bool:
        return (
            bool(self.read_only)
            and self.resolved_risk_level() == "read_only"
            and not self.side_effects
            and not self.requires_confirm
        )

    def is_write_requested(self, payload: dict[str, Any]) -> bool:
        if self.read_only:
            return False
        if self.write_request_predicate is not None:
            return bool(self.write_request_predicate(payload))
        if bool(payload.get("dry_run", False)):
            return False
        return bool(self.side_effects or self.requires_confirm or self.resolved_risk_level() != "read_only")

    def validate_input(self, payload: dict[str, Any]) -> None:
        schema = self.execution_input_json_schema()
        validate_tool_input_payload(
            tool_name=self.name,
            payload=payload,
            schema=schema,
            enforce_required=False,
        )
        if self.input_validator is not None:
            self.input_validator(payload)

    def call(self, ctx: AgentToolContext, payload: dict[str, Any]) -> ToolHandlerResult:
        self.validate_input(payload)
        return self.handler(ctx, payload)

    def input_json_schema(self) -> dict[str, Any]:
        return build_tool_input_json_schema(self.input_schema)

    def execution_input_json_schema(self) -> dict[str, Any]:
        return build_tool_input_json_schema(
            self.input_schema,
            additional_properties=True,
        )

    def resolve_answer_policy(self, payload: dict[str, Any]) -> str:
        if self.answer_policy_resolver is not None:
            resolved = self.answer_policy_resolver(payload)
            if resolved:
                return str(resolved)
        return self.answer_policy

    def resolve_output_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.output_contract_resolver is not None:
            resolved = self.output_contract_resolver(payload)
            if isinstance(resolved, dict) and resolved:
                return deepcopy(resolved)
        return deepcopy(self.output_contract)

    def resolve_planner_semantics(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.planner_semantics_resolver is not None:
            resolved = self.planner_semantics_resolver(dict(context or {}))
            if isinstance(resolved, dict) and resolved:
                return deepcopy(resolved)
        return deepcopy(self.planner_semantics)

    def to_manifest(self) -> dict[str, Any]:
        side_effects = list(self.side_effects)
        output_contract = deepcopy(self.output_contract)
        return {
            "name": self.name,
            "read_only": self.read_only,
            "description": self.description,
            "requires": list(self.requires),
            "capabilities": list(self.capabilities),
            "side_effects": side_effects,
            "annotations": _manifest_annotations(self),
            "input_schema": dict(self.input_schema),
            "input_json_schema": self.input_json_schema(),
            "input_schema_version": "om-tool-input-v1",
            "output_schema": {},
            "risk_level": self.resolved_risk_level(),
            "requires_confirm": bool(self.requires_confirm),
            "requires_env": list(self.requires_env),
            "safe_default_input": dict(self.safe_default_input),
            "examples": deepcopy(list(self.examples)),
            "answer_policy": self.answer_policy,
            "output_contract": output_contract,
            "evidence_contract": _manifest_evidence_contract(output_contract),
            "verifiers": _manifest_verifiers(self, output_contract),
            "planner_notes": list(self.planner_notes),
            "planner_semantics": self.resolve_planner_semantics({}),
        }


def _manifest_annotations(tool: AgentTool) -> dict[str, bool]:
    risk_level = tool.resolved_risk_level()
    return {
        "read_only": bool(tool.read_only),
        "destructive": bool("delete" in tool.side_effects or risk_level in {"destructive", "admin_write"}),
        "idempotent": bool(tool.read_only and not tool.side_effects and not tool.requires_confirm),
        "open_world": bool(tool.requires_env or risk_level in {"preview_admin", "remote_admin"}),
    }


def _manifest_evidence_contract(output_contract: dict[str, Any]) -> dict[str, Any]:
    if not output_contract:
        return {}
    evidence_keys = {
        "schema_version",
        "payload_dependent",
        "source_label",
        "canonical_renderer",
        "guard_profile",
        "result_shape",
        "primary_rows",
        "row_count_field",
        "fact_fields",
        "freshness_fields",
        "missing_data_fields",
        "calculation_fields",
        "model_preview_fields",
    }
    return {key: deepcopy(value) for key, value in output_contract.items() if key in evidence_keys}


def _manifest_verifiers(tool: AgentTool, output_contract: dict[str, Any]) -> list[str]:
    verifiers: list[str] = ["schema"]
    if output_contract:
        verifiers.append("output_contract")
    if output_contract.get("freshness_fields"):
        verifiers.append("freshness")
    if output_contract.get("missing_data_fields"):
        verifiers.append("missing_data")
    if output_contract.get("fact_fields"):
        verifiers.append("numeric")
    if tool.requires_confirm or tool.resolved_risk_level() in {"preview_write", "preview_admin"}:
        verifiers.append("receipt")
    return list(dict.fromkeys(verifiers))


def build_agent_tool(
    *,
    name: str,
    description: str,
    requires: tuple[str, ...],
    capabilities: tuple[str, ...],
    input_schema: dict[str, Any],
    handler: ToolHandler,
    enabled: bool = True,
    pure_read: bool = False,
    read_only: bool = False,
    side_effects: tuple[str, ...] = (),
    risk_level: str | None = "local_write",
    requires_confirm: bool = False,
    requires_env: tuple[str, ...] = (),
    safe_default_input: dict[str, Any] | None = None,
    examples: tuple[dict[str, Any], ...] = (),
    write_request_predicate: WriteRequestPredicate | None = None,
    input_validator: InputValidator | None = None,
    answer_policy: str = "default",
    answer_policy_resolver: AnswerPolicyResolver | None = None,
    output_contract: dict[str, Any] | None = None,
    output_contract_resolver: OutputContractResolver | None = None,
    planner_notes: tuple[str, ...] = (),
    planner_semantics: dict[str, Any] | None = None,
    planner_semantics_resolver: PlannerSemanticsResolver | None = None,
) -> AgentTool:
    if pure_read:
        read_only = True
        side_effects = ()
        risk_level = "read_only"
        requires_confirm = False
    return AgentTool(
        name=name,
        read_only=bool(read_only),
        description=description,
        requires=requires,
        capabilities=capabilities,
        input_schema=input_schema,
        handler=handler,
        enabled=bool(enabled),
        side_effects=side_effects,
        risk_level=risk_level,
        requires_confirm=bool(requires_confirm),
        requires_env=requires_env,
        safe_default_input=dict(safe_default_input or {}),
        examples=examples,
        write_request_predicate=write_request_predicate,
        input_validator=input_validator,
        answer_policy=answer_policy,
        answer_policy_resolver=answer_policy_resolver,
        output_contract=deepcopy(output_contract or {}),
        output_contract_resolver=output_contract_resolver,
        planner_notes=planner_notes,
        planner_semantics=deepcopy(planner_semantics or {}),
        planner_semantics_resolver=planner_semantics_resolver,
    )


def build_default_agent_tool_context() -> AgentToolContext:
    from src.application.account_config import accounts_from_config, list_account_config_views, normalize_accounts
    from src.application.agent_tool_config import load_runtime_config, repo_base, resolve_output_root, write_tools_enabled
    from src.application.agent_tool_contracts import mask_path
    from src.application.agent_tools.runtime_helpers import (
        as_float as _as_float,
        extract_context_symbols as _extract_context_symbols,
        healthcheck_symbols_for_futu as _healthcheck_symbols_for_futu_impl,
        mask_account_id as _mask_account_id_impl,
        normalize_broker as _normalize_broker,
        read_json_object_or_empty as _read_json_object_or_empty_impl,
        resolve_data_config_ref as _resolve_data_config_ref,
        resolve_local_path as _resolve_local_path_impl,
        resolve_public_data_config_path as _resolve_public_data_config_path_impl,
        run_futu_doctor as _run_futu_doctor_impl,
        symbol_fetch_config_map as _symbol_fetch_config_map_impl,
        validate_runtime_config as _validate_runtime_config_impl,
        write_json_atomic as _write_json_atomic,
    )
    from src.application.agent_tools.symbols_impl import (
        apply_symbol_mutation,
        list_symbol_rows,
    )
    from src.application.cash_headroom_query import query_sell_put_cash
    from src.application.close_advice_runner import run_close_advice
    from src.application.config_loader import load_config as load_runtime_pipeline_config
    from src.application.config_loader import resolve_watchlist_config
    from src.application.config_validator import validate_config
    from src.application.futu_portfolio_context import infer_futu_portfolio_settings
    from src.application.ledger.api import (
        list_position_rows as _list_position_rows,
        open_position_ledger,
        open_position_ledger_from_data_config as resolve_option_positions_repo,
    )
    from src.application.notify_symbols import build_notification
    from src.application.pipeline_context import load_option_positions_context, load_portfolio_context
    from src.application.pipeline_watchlist import run_watchlist_pipeline_default
    from src.application.positions.inspection import build_lot_event_history, inspect_projection_state
    from src.application.positions.assigned_stock_quotes import refresh_assigned_stock_quote_snapshots
    from src.application.positions.reporting import build_monthly_income_report
    from src.application.runtime_logs_cli import collect_runtime_logs
    from src.application.runtime_runs_cli import collect_runtime_runs
    from src.application.scan_scheduler import decide as scheduler_decide
    from src.application.scan_scheduler import read_state as read_scheduler_state
    from src.application.assistant.operation_diagnostics import collect_operation_timeline
    from src.application.assistant.session_store import collect_assistant_trace
    from src.application.version_check import check_version_update, update_local_version
    from domain.domain.fetch_source import resolve_symbol_fetch_source
    from domain.domain.ledger.position_fields import normalize_account
    from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest
    from src.infrastructure.io_utils import safe_read_csv

    def _validate_runtime_config(cfg: dict[str, Any], *, allow_empty_symbols: bool = False) -> list[str]:
        return _validate_runtime_config_impl(
            cfg,
            allow_empty_symbols=allow_empty_symbols,
            resolve_watchlist_config=resolve_watchlist_config,
            validate_config=validate_config,
        )

    def _resolve_public_data_config_path(payload: dict[str, Any], portfolio_cfg: dict[str, Any]) -> Any:
        return _resolve_public_data_config_path_impl(payload, portfolio_cfg, repo_base=repo_base)

    def _read_json_object_or_empty(path: Any) -> dict[str, Any]:
        return _read_json_object_or_empty_impl(path)

    def _mask_account_id(value: Any) -> str:
        return _mask_account_id_impl(value)

    def _run_futu_doctor(
        *,
        host: str,
        port: int,
        symbols: list[str],
        timeout_sec: int,
        telnet_host: str = "127.0.0.1",
        telnet_port: int = 22222,
    ) -> dict[str, Any]:
        return _run_futu_doctor_impl(
            host=host,
            port=port,
            symbols=symbols,
            timeout_sec=timeout_sec,
            repo_base=repo_base,
            telnet_host=telnet_host,
            telnet_port=telnet_port,
        )

    def _healthcheck_symbols_for_futu(cfg: dict[str, Any]) -> list[str]:
        return _healthcheck_symbols_for_futu_impl(cfg, resolve_watchlist_config=resolve_watchlist_config)

    def _get_exchange_rates(*, cache_path: Any, log: Any = None) -> Any:
        return get_exchange_rates_or_fetch_latest(cache_path=cache_path, log=log)

    def _symbol_fetch_config_map(cfg: dict[str, Any]) -> dict[str, Any]:
        return _symbol_fetch_config_map_impl(cfg, resolve_watchlist_config=resolve_watchlist_config)

    def _fetch_symbol_opend(*args: Any, **kwargs: Any) -> Any:
        from src.application.opend_symbol_fetching import fetch_symbol

        return fetch_symbol(*args, **kwargs)

    def _save_required_data_opend(*args: Any, **kwargs: Any) -> Any:
        from src.application.opend_symbol_outputs import save_outputs

        return save_outputs(*args, **kwargs)

    def _resolve_local_path(value: Any, *, default: Path) -> Path:
        return _resolve_local_path_impl(value, default=default, repo_base=repo_base)

    return AgentToolContext(
        repo_base=repo_base,
        load_runtime_config=load_runtime_config,
        resolve_output_root=resolve_output_root,
        mask_path=mask_path,
        validate_runtime_config=_validate_runtime_config,
        normalize_accounts=normalize_accounts,
        accounts_from_config=accounts_from_config,
        resolve_watchlist_config=resolve_watchlist_config,
        resolve_data_config_ref=_resolve_data_config_ref,
        resolve_public_data_config_path=_resolve_public_data_config_path,
        read_json_object_or_empty=_read_json_object_or_empty,
        list_account_config_views=list_account_config_views,
        mask_account_id=_mask_account_id,
        infer_futu_portfolio_settings=infer_futu_portfolio_settings,
        refresh_assigned_stock_quotes=refresh_assigned_stock_quote_snapshots,
        load_option_positions_repo=open_position_ledger,
        run_futu_doctor=_run_futu_doctor,
        healthcheck_symbols_for_futu=_healthcheck_symbols_for_futu,
        write_tools_enabled=write_tools_enabled,
        read_scheduler_state=read_scheduler_state,
        scheduler_decide=scheduler_decide,
        normalize_broker=_normalize_broker,
        normalize_account=normalize_account,
        resolve_option_positions_repo=resolve_option_positions_repo,
        list_position_rows=_list_position_rows,
        build_lot_event_history=build_lot_event_history,
        inspect_projection_state=inspect_projection_state,
        build_monthly_income_report=build_monthly_income_report,
        get_exchange_rates=_get_exchange_rates,
        build_notification=build_notification,
        collect_operation_timeline=collect_operation_timeline,
        collect_assistant_trace=collect_assistant_trace,
        check_version_update=check_version_update,
        update_local_version=update_local_version,
        collect_runtime_runs=collect_runtime_runs,
        collect_runtime_logs=collect_runtime_logs,
        load_runtime_pipeline_config=load_runtime_pipeline_config,
        run_watchlist_pipeline_default=run_watchlist_pipeline_default,
        query_sell_put_cash=query_sell_put_cash,
        load_portfolio_context=load_portfolio_context,
        load_option_positions_context=load_option_positions_context,
        symbol_fetch_config_map=_symbol_fetch_config_map,
        extract_context_symbols=_extract_context_symbols,
        resolve_symbol_fetch_source=resolve_symbol_fetch_source,
        fetch_symbol_opend=_fetch_symbol_opend,
        save_required_data_opend=_save_required_data_opend,
        resolve_local_path=_resolve_local_path,
        run_close_advice=run_close_advice,
        safe_read_csv=safe_read_csv,
        as_float=_as_float,
        deepcopy_value=deepcopy,
        apply_symbol_mutation=apply_symbol_mutation,
        list_symbol_rows=list_symbol_rows,
        write_json_atomic=_write_json_atomic,
    )
