from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from src.application.assistant.capability_catalog import ACCOUNT_VALUES
from src.application.assistant.time_filters import extract_month_filter


TASK_CONTRACT_SCHEMA_VERSION = "om-agent-task-contract-v1"


@dataclass(frozen=True)
class TaskContract:
    question: str
    goal: str
    intent_families: tuple[str, ...]
    scope: dict[str, Any]
    required_answer: tuple[str, ...]
    optional_answer: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "question": self.question,
            "goal": self.goal,
            "intent_families": list(self.intent_families),
            "scope": dict(self.scope),
            "required_answer": list(self.required_answer),
            "optional_answer": list(self.optional_answer),
            "constraints": list(self.constraints),
        }


def build_task_contract(
    *,
    question: str,
    plan: dict[str, Any],
    request_context: dict[str, Any] | None = None,
    today: date,
) -> TaskContract:
    goal = str(plan.get("goal") or question or "").strip()
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    plan_values = list(_plan_values(steps))
    text = "\n".join([str(question or ""), goal, *plan_values])
    question_goal_text = "\n".join([str(question or ""), goal])
    intent_families = _intent_families(question_goal_text, text)
    requested_accounts = _extract_accounts(question_goal_text)
    planned_accounts = _extract_accounts(text)
    requested_symbols = _extract_symbols(question_goal_text)
    planned_symbols = _extract_symbols(text)
    requested_months = _extract_months(question_goal_text, today=today)
    planned_months = _extract_months(text, today=today)
    config_keys = _extract_config_keys(plan_values, request_context=request_context)
    scope = {
        "requested_accounts": requested_accounts,
        "planned_accounts": planned_accounts,
        "requested_symbols": requested_symbols,
        "planned_symbols": planned_symbols,
        "requested_months": requested_months,
        "planned_months": planned_months,
        "config_keys": config_keys,
    }
    required, optional = _answer_keys(intent_families=intent_families, text=question_goal_text)
    return TaskContract(
        question=str(question or "").strip(),
        goal=goal,
        intent_families=tuple(intent_families),
        scope=scope,
        required_answer=tuple(required),
        optional_answer=tuple(optional),
        constraints=tuple(_constraints(intent_families)),
    )


def _intent_families(question_goal_text: str, full_text: str) -> list[str]:
    compact = re.sub(r"\s+", "", question_goal_text.lower())
    full_compact = re.sub(r"\s+", "", full_text.lower())
    accounts = _extract_accounts(question_goal_text)
    source_focused = any(token in compact for token in ("主要来自", "来自哪里", "来源", "组成", "构成", "driver", "breakdown"))
    account_metric_focused = any(
        token in compact
        for token in (
            "账户收益",
            "收益",
            "收入",
            "净现金流",
            "现金流",
            "回报",
            "收益率",
            "权利金",
            "已实现",
            "pnl",
            "income",
            "return",
            "cashflow",
        )
    )
    families: list[str] = []
    if (
        (
            any(token in compact for token in ("对比", "比较", "谁更高", "compare"))
            or (any(token in compact for token in ("差异", "不同")) and not source_focused)
        )
        and (len(accounts) >= 2 or "账户" in compact)
        and account_metric_focused
    ):
        families.append("account_comparison")
    if any(token in compact for token in ("为什么", "原因", "来源", "组成", "构成", "主要来自", "哪里", "breakdown", "driver")):
        families.append("breakdown")
    if any(token in full_compact for token in ("指派正股", "被指派", "assignedstock")) or (
        "指派" in full_compact and any(token in full_compact for token in ("正股", "浮盈亏", "生命周期", "spot"))
    ):
        families.append("assigned_stock_pnl")
    if any(token in compact for token in ("升级", "版本", "回执", "发布", "release", "deploy")):
        families.append("upgrade_status")
    if not families:
        families.append("general_analysis")
    return families


def _answer_keys(*, intent_families: list[str], text: str) -> tuple[list[str], list[str]]:
    required: list[str] = []
    optional: list[str] = []
    compact = re.sub(r"\s+", "", text.lower())
    if "account_comparison" in intent_families:
        source_focused = any(token in compact for token in ("主要来自", "来自哪里", "来源", "组成", "构成", "driver", "breakdown"))
        required.extend(["comparison_winner", "source_and_policy"])
        if source_focused:
            optional.append("amount_difference")
        else:
            required.append("amount_difference")
        if "率" in compact or "rate" in compact or "return" in compact:
            required.append("rate_difference")
        else:
            optional.append("rate_difference")
        optional.append("main_drivers")
    if "breakdown" in intent_families:
        required.extend(["summary", "main_drivers", "source_and_policy"])
    if "assigned_stock_pnl" in intent_families:
        required.extend(
            [
                "shares_remaining",
                "cost_basis",
                "spot_freshness",
                "unrealized_pnl",
                "lifecycle_pnl",
                "source_and_policy",
            ]
        )
    if "upgrade_status" in intent_families:
        required.extend(["command_status", "current_version", "target_version", "source_and_policy"])
        if any(token in compact for token in ("发布", "release", "deploy")):
            required.append("release_status")
    if not required:
        required.extend(["summary", "source_and_policy"])
    return _unique(required), _unique(optional)


def _constraints(intent_families: list[str]) -> list[str]:
    constraints = ["must_cite_source_or_policy", "do_not_expose_internal_ids_or_sql"]
    if "account_comparison" in intent_families or "breakdown" in intent_families:
        constraints.extend(["do_not_average_return_rates", "keep_cashflow_realized_pnl_and_premium_separate"])
    if "assigned_stock_pnl" in intent_families:
        constraints.extend(
            [
                "assigned_stock_cost_uses_delivery_price",
                "premium_only_affects_lifecycle_pnl",
                "realtime_unrealized_pnl_requires_fresh_spot",
            ]
        )
    if "upgrade_status" in intent_families:
        constraints.append("version_receipt_requires_current_and_target_version")
    return _unique(constraints)


def _plan_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in {"account", "accounts", "symbol", "symbols", "month", "config_key", "sql", "query"}:
                values.append(str(item))
            values.extend(_plan_values(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            values.extend(_plan_values(item))
    return values


def _extract_accounts(text: str) -> list[str]:
    compact = str(text or "").lower()
    accounts: list[str] = []
    for raw in ACCOUNT_VALUES:
        account = str(raw or "").strip().lower()
        if not account:
            continue
        if re.search(rf"(?<![a-z0-9_]){re.escape(account)}(?![a-z0-9_])", compact):
            accounts.append(account)
    return _unique(accounts)


def _extract_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    for match in re.finditer(r"(?<![A-Za-z0-9_.])([A-Z0-9]{1,5}(?:\.HK)?)(?![A-Za-z0-9_.])", str(text or "")):
        symbol = match.group(1).upper()
        if symbol in {"HKD", "USD", "CNY", "P0", "P1", "P2"}:
            continue
        symbols.append(symbol)
    return _unique(symbols)


def _extract_months(text: str, *, today: date) -> list[str]:
    months: list[str] = []
    explicit = re.findall(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])(?!\d)", str(text or ""))
    months.extend(f"{year}-{month}" for year, month in explicit)
    normalized = extract_month_filter(text, today=today)
    if normalized:
        months.append(normalized)
    return _unique(months)


def _extract_config_keys(values: list[str], *, request_context: dict[str, Any] | None) -> list[str]:
    config_keys: list[str] = []
    request_key = (request_context or {}).get("config_key") if isinstance(request_context, dict) else None
    if str(request_key or "").strip():
        config_keys.append(str(request_key).strip())
    for value in values:
        text = str(value or "").strip().lower()
        if text in {"us", "hk"}:
            config_keys.append(text)
    return _unique(config_keys)


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


__all__ = [
    "TASK_CONTRACT_SCHEMA_VERSION",
    "TaskContract",
    "build_task_contract",
]
