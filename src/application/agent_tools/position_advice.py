from __future__ import annotations

from typing import Any

from domain.domain.position_advice_authority import (
    normalize_account_label,
    portfolio_account_identity_hash,
)
from src.application.account_config import (
    accounts_from_config,
    build_account_portfolio_source_plan,
    resolve_futu_account_ids,
)
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.config_loader import resolve_data_config_path
from src.application.position_advice_reader import (
    read_position_advice_v2_from_ledger,
)
from src.application.runtime_paths import resolve_runtime_root


_POSITION_ADVICE_READ_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "position_advice_read.output.v2",
    "source_label": "OM Position Advice v2 current manifest",
    "primary_rows": "rows",
    "row_count_field": "row_count",
    "fact_fields": [
        "account",
        "portfolio_scope_id",
        "authority_mode",
        "authority_generation",
        "portfolio_plan_id",
        "account_run_id",
        "economic_model",
        "allocator_version",
        "rows[].position_id",
        "rows[].strategy_family",
        "rows[].strategy_group_id",
        "rows[].leg_role",
        "rows[].lifecycle_state",
        "rows[].group_structure_state",
        "rows[].recommendation",
        "rows[].actionable",
        "rows[].action_scope",
        "rows[].reason_codes",
        "rows[].resource_deltas",
        "rows[].leg_plan",
    ],
    "freshness_fields": [
        "freshness.status",
        "freshness.checked_at",
        "freshness.reason_codes",
        "current_manifest_hash",
    ],
    "missing_data_fields": [
        "availability_status",
        "freshness.reason_codes",
        "rows[].reason_codes",
    ],
    "model_preview_fields": [
        "authority_mode",
        "portfolio_plan_id",
        "economic_model",
        "allocator_version",
        "freshness",
        "rows",
    ],
}


def _position_advice_read_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=str(payload.get("config_key") or "").strip() or None,
        config_path=payload.get("config_path"),
    )
    configured_accounts = accounts_from_config(cfg, fallback=())
    raw_accounts = cfg.get("accounts")
    if isinstance(raw_accounts, str):
        raw_account_items = [raw_accounts]
    elif isinstance(raw_accounts, (list, tuple, set)):
        raw_account_items = list(raw_accounts)
    else:
        raw_account_items = []
    normalized_raw_accounts = [
        str(item or "").strip().lower()
        for item in raw_account_items
        if str(item or "").strip()
    ]
    if len(normalized_raw_accounts) != len(set(normalized_raw_accounts)):
        raise AgentToolError(
            code="FRESHNESS_UNKNOWN",
            message="runtime config contains duplicate normalized account labels",
        )
    requested_account = str(payload.get("account") or "").strip()
    if requested_account:
        account = normalize_account_label(requested_account)
    elif len(configured_accounts) == 1:
        account = normalize_account_label(configured_accounts[0])
    else:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="account is required when runtime config has multiple accounts",
        )
    if account not in configured_accounts:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="account does not belong to the selected runtime config",
        )

    source_plan = build_account_portfolio_source_plan(cfg, account=account)
    source = str(source_plan.primary_source or "").strip().lower()
    if source == "futu":
        identifiers = resolve_futu_account_ids(cfg, account=account)
    else:
        identifiers = [
            str(source_plan.holdings_account or "").strip()
        ]
    try:
        identity_hash = portfolio_account_identity_hash(
            normalized_portfolio_source=source,
            broker_account_identifiers=identifiers,
        )
    except ValueError as exc:
        raise AgentToolError(
            code="DATA_UNAVAILABLE",
            message="portfolio account identity is unavailable",
        ) from exc

    portfolio_cfg = (
        cfg.get("portfolio")
        if isinstance(cfg.get("portfolio"), dict)
        else {}
    )
    data_config_path = resolve_data_config_path(
        base=config_path.parent,
        data_config=portfolio_cfg.get("data_config"),
    )
    base_value = str(payload.get("runtime_base") or "").strip() or None
    base = resolve_runtime_root(
        repo_root=repo_base(),
        runtime_root=base_value,
    ).runtime_root
    result = read_position_advice_v2_from_ledger(
        base=base,
        normalized_account=account,
        normalized_portfolio_source=source,
        portfolio_account_identity_hash=identity_hash,
        data_config_path=data_config_path,
        requested_portfolio_plan_id=(
            str(payload.get("portfolio_plan_id") or "").strip() or None
        ),
        requested_market=str(
            payload.get("config_key") or ""
        ).strip().upper() or None,
    )
    warnings: list[str] = []
    freshness = dict(result.get("freshness") or {})
    if freshness.get("status") != "fresh":
        warnings.append(
            "position advice is not fresh; all returned rows are non-actionable"
        )
    if result.get("authority_mode") == "v2_shadow":
        warnings.append(
            "position advice is in shadow mode; v1 remains notification authority"
        )
    return (
        result,
        warnings,
        {
            "source": "position_advice_current",
            "pure_read": True,
            "runtime_config": config_path.name,
        },
    )


POSITION_ADVICE_READ_TOOL = build_agent_tool(
    name="position_advice_read",
    description=(
        "Read the current immutable Position Advice v2 portfolio plan. "
        "The reader revalidates shared authority, source expiry, artifact hashes, "
        "and the live ledger decision fingerprint without refreshing market data."
    ),
    requires=(
        "position_advice_current_manifest",
        "position_ledger",
        "runtime_config",
    ),
    capabilities=(
        "position_advice",
        "capital_efficiency",
        "read_only",
    ),
    input_schema={
        "config_key": {
            "type": "string",
            "enum": ["us", "hk"],
            "description": "Runtime market config",
        },
        "config_path": "optional explicit runtime config path",
        "account": "account label; required for multi-account configs",
        "portfolio_plan_id": (
            "optional expected plan id; a non-current id is returned as superseded"
        ),
        "runtime_base": (
            "optional runtime root containing output_runs and output_shared"
        ),
    },
    handler=_position_advice_read_tool,
    pure_read=True,
    safe_default_input={},
    examples=(
        {
            "input": {
                "config_key": "us",
                "account": "lx",
            }
        },
    ),
    output_contract=_POSITION_ADVICE_READ_OUTPUT_CONTRACT,
    copilot_input_fields=(
        "config_key",
        "account",
        "portfolio_plan_id",
    ),
)

TOOLS: tuple[AgentTool, ...] = (POSITION_ADVICE_READ_TOOL,)


__all__ = ["POSITION_ADVICE_READ_TOOL", "TOOLS"]
