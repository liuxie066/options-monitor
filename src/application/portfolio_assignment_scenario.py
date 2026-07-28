"""Application boundary for the portfolio assignment stress scenario."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from domain.domain.portfolio_assignment_scenario import (
    PORTFOLIO_EVIDENCE_VERSION,
    project_assignment_scenario,
)
from domain.domain.symbol_identity import canonical_symbol
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.api import (
    list_open_short_assignment_rows,
    open_position_ledger_from_runtime_config,
)
from src.infrastructure.portfolio_management_client import (
    SERVICE_URL_ENV,
    PortfolioManagementClient,
    PortfolioManagementConfigError,
    PortfolioManagementError,
    PortfolioManagementHTTPError,
)


MAX_ACCOUNTS = 20
MAX_SUPPLEMENTAL_CODES = 500


class AssignmentScenarioInputError(ValueError):
    """Raised when the public business input violates the scenario contract."""


class PortfolioEvidenceReadError(RuntimeError):
    """Raised when the portfolio-management evidence boundary is unavailable."""


def normalize_assignment_accounts(accounts: Sequence[str]) -> list[str]:
    if isinstance(accounts, (str, bytes)) or not isinstance(accounts, Sequence):
        raise AssignmentScenarioInputError("accounts must be an array of account labels")
    normalized = list(
        dict.fromkeys(str(item or "").strip().lower() for item in accounts if str(item or "").strip())
    )
    if not normalized:
        raise AssignmentScenarioInputError("accounts must contain at least one account")
    if len(normalized) > MAX_ACCOUNTS:
        raise AssignmentScenarioInputError(f"accounts must contain at most {MAX_ACCOUNTS} accounts")
    invalid = [
        account
        for account in normalized
        if len(account) > 32
        or not account[0].isalnum()
        or any(not (char.islower() or char.isdigit() or char in {"_", "-"}) for char in account)
    ]
    if invalid:
        raise AssignmentScenarioInputError(
            f"invalid account labels: {', '.join(invalid)}"
        )
    return normalized


def read_portfolio_valuation_evidence(
    *,
    accounts: Sequence[str],
    supplemental_codes: Sequence[str],
    price_timeout: int = 30,
    client: PortfolioManagementClient | None = None,
) -> dict[str, Any]:
    try:
        resolved_client = client or PortfolioManagementClient(
            urlopen_fn=urllib.request.urlopen
        )
        return resolved_client.read_valuation_evidence(
            accounts=list(accounts),
            supplemental_codes=list(supplemental_codes),
            price_timeout=int(price_timeout),
        )
    except PortfolioManagementHTTPError as exc:
        if exc.error_code == "INPUT_ERROR":
            raise AssignmentScenarioInputError(str(exc)) from exc
        raise PortfolioEvidenceReadError(str(exc)) from exc
    except PortfolioManagementError as exc:
        raise PortfolioEvidenceReadError(str(exc)) from exc


def _load_runtime_and_positions(
    accounts: Sequence[str],
) -> tuple[list[dict[str, Any]], str]:
    config_path, cfg = load_runtime_config(config_key="us")
    configured_accounts = {
        str(item or "").strip().lower()
        for item in (cfg.get("accounts") or [])
        if str(item or "").strip()
    }
    unknown = [account for account in accounts if account not in configured_accounts]
    if unknown:
        raise AssignmentScenarioInputError(
            f"unknown accounts: {', '.join(unknown)}"
        )
    _data_config, repo = open_position_ledger_from_runtime_config(
        base=repo_base(),
        cfg=cfg,
        config_path=config_path,
    )
    return (
        list_open_short_assignment_rows(repo, accounts=list(accounts)),
        str(config_path.name),
    )


def _iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _snapshot_payload(
    *,
    accounts: Sequence[str],
    option_positions: Sequence[Mapping[str, Any]],
    portfolio_evidence: Mapping[str, Any],
    options_observed_at: str,
    runtime_config_name: str,
) -> dict[str, Any]:
    portfolio_snapshot = (
        portfolio_evidence.get("snapshot")
        if isinstance(portfolio_evidence.get("snapshot"), Mapping)
        else {}
    )
    portfolio_observed_at = str(
        portfolio_snapshot.get("observed_at")
        or portfolio_evidence.get("observed_at")
        or ""
    ).strip() or None
    portfolio_time = _iso_datetime(portfolio_observed_at)
    options_time = _iso_datetime(options_observed_at)
    max_skew = (
        abs((portfolio_time - options_time).total_seconds())
        if portfolio_time is not None and options_time is not None
        else None
    )
    option_identity = [
        {
            "record_id": row.get("record_id"),
            "account": row.get("account"),
            "broker": row.get("broker"),
            "symbol": row.get("symbol"),
            "option_type": row.get("option_type"),
            "strike": row.get("strike"),
            "multiplier": row.get("multiplier"),
            "expiration_ymd": row.get("expiration_ymd"),
            "currency": row.get("currency"),
            "contracts_open": row.get("contracts_open"),
            "status": row.get("status"),
        }
        for row in option_positions
    ]
    digest = hashlib.sha256(
        json.dumps(
            {
                "accounts": list(accounts),
                "portfolio_snapshot_id": portfolio_snapshot.get("snapshot_id"),
                "options_observed_at": options_observed_at,
                "option_identity": option_identity,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "snapshot_id": f"assignment-{digest}",
        "observed_at": options_observed_at,
        "portfolio_snapshot_id": portfolio_snapshot.get("snapshot_id"),
        "portfolio_observed_at": portfolio_observed_at,
        "options_observed_at": options_observed_at,
        "max_source_skew_seconds": (
            format(max_skew, ".6f") if max_skew is not None else None
        ),
        "runtime_config": runtime_config_name,
    }


def _unavailable_evidence(
    message: str,
    *,
    accounts: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": PORTFOLIO_EVIDENCE_VERSION,
        "success": True,
        "status": "unavailable",
        "freshness": {
            "status": "unavailable",
            "trust_status": "unavailable",
            "observed_at_utc": None,
            "dataset_ids": [],
            "reason_codes": ["PORTFOLIO_EVIDENCE_UNAVAILABLE"],
        },
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "accounts": list(accounts),
            "reporting_currency": "CNY",
        },
        "snapshot": {},
        "holdings": [],
        "quotes": [],
        "account_status": [],
        "warnings": [message],
    }


def query_portfolio_assignment_scenario(
    accounts: Sequence[str],
) -> dict[str, Any]:
    """Read one current snapshot and return the assignment scenario.

    This public application function is read-only. It never applies assignment
    events or mutates portfolio holdings.
    """

    normalized_accounts = normalize_assignment_accounts(accounts)
    options_observed_at = datetime.now(timezone.utc).isoformat()
    try:
        option_positions, runtime_config_name = _load_runtime_and_positions(
            normalized_accounts
        )
    except (AssignmentScenarioInputError, AgentToolError):
        raise
    except Exception as exc:
        evidence = _unavailable_evidence(
            f"option position ledger read failed: {exc}",
            accounts=normalized_accounts,
        )
        snapshot = _snapshot_payload(
            accounts=normalized_accounts,
            option_positions=[],
            portfolio_evidence=evidence,
            options_observed_at=options_observed_at,
            runtime_config_name="unknown",
        )
        return project_assignment_scenario(
            accounts=normalized_accounts,
            portfolio_evidence=evidence,
            option_positions=[],
            snapshot=snapshot,
        )
    supplemental_codes = sorted(
        {
            symbol
            for symbol in (
                canonical_symbol(row.get("symbol")) for row in option_positions
            )
            if symbol
        }
    )
    if len(supplemental_codes) > MAX_SUPPLEMENTAL_CODES:
        raise AssignmentScenarioInputError(
            f"open short positions reference more than {MAX_SUPPLEMENTAL_CODES} underlyings"
        )
    try:
        evidence = read_portfolio_valuation_evidence(
            accounts=normalized_accounts,
            supplemental_codes=supplemental_codes,
        )
    except AssignmentScenarioInputError:
        raise
    except PortfolioEvidenceReadError as exc:
        evidence = _unavailable_evidence(
            str(exc),
            accounts=normalized_accounts,
        )

    snapshot = _snapshot_payload(
        accounts=normalized_accounts,
        option_positions=option_positions,
        portfolio_evidence=evidence,
        options_observed_at=options_observed_at,
        runtime_config_name=runtime_config_name,
    )
    return project_assignment_scenario(
        accounts=normalized_accounts,
        portfolio_evidence=evidence,
        option_positions=option_positions,
        snapshot=snapshot,
    )


def render_assignment_scenario_text(result: Mapping[str, Any]) -> str:
    scope = result.get("scope") if isinstance(result.get("scope"), Mapping) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    cash = result.get("cash_coverage") if isinstance(result.get("cash_coverage"), Mapping) else {}
    distribution = result.get("distribution") if isinstance(result.get("distribution"), Mapping) else {}
    lines = [
        "# 指派后资产分布（不含 Long Option）",
        "",
        f"- 状态：{result.get('status')}",
        f"- 账户：{', '.join(scope.get('accounts') or [])}",
        (
            f"- 指派：{summary.get('assignment_count', 0)} 笔"
            f"（Sell Put {summary.get('short_put_count', 0)}；"
            f"Sell Call {summary.get('short_call_count', 0)}）"
        ),
        "",
        "## CNY 资金覆盖",
        "",
        f"- 现金 + MMF：{cash.get('available_cash_and_mmf_cny') or '-'}",
        f"- Sell Put 总需求：{cash.get('gross_put_requirement_cny') or '-'}",
        f"- Sell Call 回款：{cash.get('call_assignment_inflow_cny') or '-'}",
        f"- 指派后净现金（估算）：{cash.get('ending_cash_net_estimated_cny') or '-'}",
        f"- 终局资金缺口：{cash.get('terminal_funding_gap_cny') or '-'}",
        "",
        "## 指派后分布",
        "",
        "| 类别 | CNY 市值/负债 | 正资产权重 |",
        "|---|---:|---:|",
    ]
    for row in distribution.get("by_category") or []:
        lines.append(
            f"| {row.get('category')} | {row.get('value_cny') or '-'} | "
            f"{row.get('weight_of_gross_assets') or '-'} |"
        )
    lines.extend(
        [
            "",
            f"- 正资产：{distribution.get('gross_assets_cny') or '-'}",
            f"- 负债：{distribution.get('liabilities_cny') or '-'}",
            f"- 净资产：{distribution.get('net_assets_cny') or '-'}",
        ]
    )
    warnings = list(result.get("warnings") or [])
    if warnings:
        lines.extend(["", "## 告警", ""])
        lines.extend(f"- {item}" for item in warnings)
    return "\n".join(lines) + "\n"


__all__ = [
    "AssignmentScenarioInputError",
    "PortfolioEvidenceReadError",
    "normalize_assignment_accounts",
    "query_portfolio_assignment_scenario",
    "read_portfolio_valuation_evidence",
    "render_assignment_scenario_text",
]
