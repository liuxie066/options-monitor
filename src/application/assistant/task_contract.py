from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from src.application.assistant.capability_catalog import ACCOUNT_VALUES
from src.application.assistant.time_filters import extract_month_filter
from src.application.symbol_calibration import calibrate_symbol


TASK_CONTRACT_SCHEMA_VERSION = "om-agent-task-contract-v1"
_SYMBOL_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"([A-Za-z]{1,8}(?:\.[A-Za-z]{1,4})?|[A-Za-z]{2}\.\d{4,5}|\d{3,5}(?:\.HK)?|[\u4e00-\u9fff]{2,8})"
    r"(?![A-Za-z0-9_.])"
)
_NON_SYMBOL_TOKENS = {
    "ACCOUNT",
    "ACTION",
    "ALL",
    "AND",
    "AS",
    "ASSIGNED",
    "ASC",
    "AVG",
    "BY",
    "CALL",
    "CASE",
    "CANDIDATE",
    "CANDIDATES",
    "CASHFLOW",
    "COUNT",
    "CNY",
    "COVERED",
    "DESC",
    "DIAGNOSE",
    "DIAGNOSTIC",
    "DIAGNOSTICS",
    "ELSE",
    "END",
    "EVIDENCE",
    "FILTER",
    "FILTERED",
    "FROM",
    "GROUP",
    "HK",
    "HKD",
    "IN",
    "IS",
    "JOIN",
    "LEFT",
    "LIKE",
    "LIMIT",
    "LONG",
    "LX",
    "MARKET",
    "MAX",
    "MIN",
    "MONTH",
    "NOT",
    "NULL",
    "ON",
    "OPEN",
    "OR",
    "ORDER",
    "OUTER",
    "P0",
    "P1",
    "P2",
    "PUT",
    "REASON",
    "REJECTED",
    "REFRESH",
    "RIGHT",
    "RISK",
    "RULE",
    "SELECT",
    "SELL",
    "SHORT",
    "SHOW",
    "STATUS",
    "STOCK",
    "STRIKE",
    "SUM",
    "SY",
    "SYMBOL",
    "THEN",
    "TRACE",
    "US",
    "USD",
    "WHEN",
    "WHERE",
    "WHY",
}


@dataclass(frozen=True)
class TaskContract:
    question: str
    goal: str
    intent_families: tuple[str, ...]
    scope: dict[str, Any]
    required_answer: tuple[str, ...]
    optional_answer: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    domain: str = "general"
    task_mode: str = "summarize"
    requested_effect: str = "read"
    required_evidence: tuple[str, ...] = ()
    answer_shape: tuple[str, ...] = ()
    selected_recipe: dict[str, Any] | None = None
    planner_declared: bool = False
    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "question": self.question,
            "goal": self.goal,
            "domain": self.domain,
            "task_mode": self.task_mode,
            "requested_effect": self.requested_effect,
            "intent_families": list(self.intent_families),
            "scope": dict(self.scope),
            "required_answer": list(self.required_answer),
            "optional_answer": list(self.optional_answer),
            "constraints": list(self.constraints),
            "required_evidence": list(self.required_evidence),
            "answer_shape": list(self.answer_shape),
            "planner_declared": bool(self.planner_declared),
        }
        if isinstance(self.selected_recipe, dict) and self.selected_recipe:
            payload["selected_recipe"] = _safe_selected_recipe(self.selected_recipe)
        return payload


def build_task_contract(
    *,
    question: str,
    plan: dict[str, Any],
    request_context: dict[str, Any] | None = None,
    today: date,
) -> TaskContract:
    goal = str(plan.get("goal") or question or "").strip()
    planner_contract = plan.get("task_contract") if isinstance(plan.get("task_contract"), dict) else {}
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    plan_values = list(_plan_values(steps))
    text = "\n".join([str(question or ""), goal, *plan_values])
    user_text = str(question or "")
    question_goal_text = "\n".join([user_text, goal])
    intent_families = _intent_families(question_goal_text, text)
    planned_symbols = _unique([*_extract_symbols(text), *_planned_symbol_values(steps)])
    requested_accounts = _extract_accounts(user_text)
    planned_accounts = _extract_accounts(text)
    requested_symbols = _extract_symbols(user_text, allowed_lowercase_symbols=set(planned_symbols))
    requested_months = _extract_months(user_text, today=today)
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
    scope = _merge_planner_scope(scope, planner_contract.get("scope") if isinstance(planner_contract, dict) else None)
    required, optional = _answer_keys(intent_families=intent_families, text=question_goal_text)
    required = _merge_contract_list(required, planner_contract.get("required_answer") if isinstance(planner_contract, dict) else None)
    optional = _merge_contract_list(optional, planner_contract.get("optional_answer") if isinstance(planner_contract, dict) else None)
    domain = _normalized_contract_value(
        planner_contract.get("domain") if isinstance(planner_contract, dict) else None,
        allowed={
            "income",
            "position",
            "candidate",
            "config",
            "operation",
            "runtime",
            "strategy",
            "general",
        },
        default=_infer_domain(question_goal_text, text),
    )
    task_mode = _normalized_contract_value(
        planner_contract.get("task_mode") if isinstance(planner_contract, dict) else None,
        allowed={
            "summarize",
            "analyze",
            "compare",
            "diagnose",
            "explain",
            "recommend",
            "preview_write",
        },
        default=_infer_task_mode(intent_families=intent_families, text=question_goal_text, full_text=text),
    )
    if domain == "income" and task_mode == "analyze" and "breakdown" in intent_families:
        required = _merge_contract_list(required, ["main_drivers"])
    requested_effect = _normalized_contract_value(
        planner_contract.get("requested_effect") if isinstance(planner_contract, dict) else None,
        allowed={"read", "preview_write", "prohibited"},
        default=_infer_requested_effect(text),
    )
    required_evidence = _merge_contract_list(
        _default_required_evidence(domain=domain, task_mode=task_mode, intent_families=intent_families),
        planner_contract.get("required_evidence") if isinstance(planner_contract, dict) else None,
    )
    answer_shape = _merge_contract_list(
        _default_answer_shape(task_mode=task_mode, required_answer=required),
        planner_contract.get("answer_shape") if isinstance(planner_contract, dict) else None,
    )
    return TaskContract(
        question=str(question or "").strip(),
        goal=goal,
        intent_families=tuple(intent_families),
        scope=scope,
        required_answer=tuple(required),
        optional_answer=tuple(optional),
        constraints=tuple(_constraints(intent_families, required_answer=required)),
        domain=domain,
        task_mode=task_mode,
        requested_effect=requested_effect,
        required_evidence=tuple(required_evidence),
        answer_shape=tuple(answer_shape),
        selected_recipe=_safe_selected_recipe(plan.get("selected_recipe")) if isinstance(plan.get("selected_recipe"), dict) else None,
        planner_declared=bool(planner_contract),
    )


def _safe_selected_recipe(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "name",
        "domains",
        "task_modes",
        "evidence_needs",
        "primary_views",
        "source_tools",
        "external_evidence",
        "followup_tool",
        "answer_shape",
        "match_source",
        "reason",
    }
    return {str(key): value[key] for key in value if str(key) in allowed}


def _intent_families(question_goal_text: str, full_text: str) -> list[str]:
    compact = re.sub(r"\s+", "", question_goal_text.lower())
    full_compact = re.sub(r"\s+", "", full_text.lower())
    accounts = _extract_accounts(question_goal_text)
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
    rate_metric_focused = any(token in compact for token in ("收益率", "现金流率", "已实现率", "权利金率", "returnrate"))
    analysis_focused = any(token in compact for token in ("分析", "复盘", "表现"))
    source_focused = any(token in compact for token in ("主要来自", "来自哪里", "来源", "组成", "构成", "driver", "breakdown")) or (
        analysis_focused and account_metric_focused and not rate_metric_focused
    )
    families: list[str] = []
    is_assigned_stock_pnl = any(token in full_compact for token in ("指派正股", "被指派", "assignedstock")) or (
        "指派" in full_compact and any(token in full_compact for token in ("正股", "浮盈亏", "生命周期", "spot"))
    )
    is_upgrade_status = any(token in compact for token in ("升级", "版本", "回执", "发布", "release", "deploy"))
    is_candidate_filter_diagnostic = _is_candidate_filter_diagnostic(question_goal_text, full_text)
    if (
        (
            any(token in compact for token in ("对比", "比较", "谁更高", "compare"))
            or (any(token in compact for token in ("差异", "不同")) and not source_focused)
        )
        and (len(accounts) >= 2 or "账户" in compact)
        and account_metric_focused
    ):
        families.append("account_comparison")
    if (
        not is_upgrade_status
        and not is_assigned_stock_pnl
        and is_candidate_filter_diagnostic
    ):
        families.append("candidate_filter_diagnostic")
    elif (
        not is_upgrade_status
        and not is_assigned_stock_pnl
        and (
            source_focused
            or any(token in compact for token in ("为什么", "原因", "来源", "组成", "构成", "主要来自", "哪里", "breakdown", "driver"))
        )
    ):
        families.append("breakdown")
    if is_assigned_stock_pnl:
        families.append("assigned_stock_pnl")
    if is_upgrade_status:
        families.append("upgrade_status")
    if not families:
        families.append("general_analysis")
    return families


def _infer_domain(question_goal_text: str, full_text: str) -> str:
    compact = re.sub(r"\s+", "", str(question_goal_text or "").lower())
    full_compact = re.sub(r"\s+", "", str(full_text or "").lower())
    if any(token in compact for token in ("升级", "版本", "回执", "发布", "release", "deploy")):
        return "operation"
    if any(token in full_compact for token in ("runtime", "健康", "状态", "推送", "通知", "scheduler", "notification")):
        return "runtime"
    if any(token in full_compact for token in ("候选", "candidate", "filter", "过滤", "trace")):
        return "candidate"
    if any(token in full_compact for token in ("配置", "min_strike", "max_strike", "coveredcall", "sellput", "sellcall")):
        return "config"
    if any(token in full_compact for token in ("策略", "建议", "太保守", "适合", "sellput", "coveredcall", "yield")):
        return "strategy"
    if any(token in full_compact for token in ("持仓", "指派正股", "被指派", "assignedstock", "平仓", "浮盈亏")):
        return "position"
    if any(token in full_compact for token in ("收益", "收入", "现金流", "权利金", "已实现", "pnl", "income", "return", "cashflow")):
        return "income"
    return "general"


def _infer_task_mode(*, intent_families: list[str], text: str, full_text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    full_compact = re.sub(r"\s+", "", str(full_text or "").lower())
    if any(token in compact for token in ("建议", "应该", "要不要", "是否适合", "是否太", "太保守", "recommend")):
        return "recommend"
    if "account_comparison" in intent_families or any(token in compact for token in ("对比", "比较", "谁更高", "差异", "不同", "compare")):
        return "compare"
    if "candidate_filter_diagnostic" in intent_families or any(
        token in compact for token in ("为什么", "没推送", "没收到", "没出", "异常", "失败", "缺失", "诊断", "why")
    ):
        return "diagnose"
    if any(token in compact for token in ("解释", "口径", "规则", "是什么", "怎么定义", "explain")):
        return "explain"
    if "breakdown" in intent_families or any(token in full_compact for token in ("分析", "复盘", "表现", "来源", "组成", "构成", "主要来自", "driver", "breakdown")):
        return "analyze"
    return "summarize"


def _infer_requested_effect(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if any(token in compact for token in ("记录开仓", "记录平仓", "设置", "修改", "切换模型", "立即升级", "upgrade_now")):
        return "preview_write"
    return "read"


def _default_required_evidence(*, domain: str, task_mode: str, intent_families: list[str]) -> list[str]:
    evidence = ["source_policy"]
    if task_mode == "summarize":
        evidence.insert(0, "summary")
    elif task_mode == "analyze":
        evidence.insert(0, "summary")
        if "breakdown" in intent_families:
            evidence.append("driver_or_breakdown")
    elif task_mode == "compare":
        evidence[:0] = ["same_scope_comparable_data"]
    elif task_mode == "diagnose":
        evidence[:0] = ["observed_status", "diagnostic_evidence"]
    elif task_mode == "explain":
        evidence[:0] = ["rule_or_config_source"]
    elif task_mode == "recommend":
        evidence[:0] = ["current_state", "constraints", "risk_premise", "options"]
    elif task_mode == "preview_write":
        evidence[:0] = ["permission_request", "preview_receipt"]
    if domain == "income" and "breakdown" in intent_families:
        evidence.append("income_components")
    if domain == "position" and "assigned_stock_pnl" in intent_families:
        evidence.append("quote_freshness")
    return _unique(evidence)


def _default_answer_shape(*, task_mode: str, required_answer: list[str]) -> list[str]:
    if task_mode == "analyze":
        shape = ["conclusion", "drivers", "caveat"] if "main_drivers" in required_answer else ["conclusion", "key_facts", "caveat"]
    elif task_mode == "compare":
        shape = ["conclusion", "same_scope_comparison", "difference"]
    elif task_mode == "diagnose":
        shape = ["observation", "cause_chain", "evidence_boundary", "next_step"]
    elif task_mode == "explain":
        shape = ["rule_or_policy", "source", "impact"]
    elif task_mode == "recommend":
        shape = ["judgement", "options", "risk", "premise"]
    elif task_mode == "preview_write":
        shape = ["preview_summary", "risk", "confirmation_handle"]
    else:
        shape = ["direct_answer"]
    if "source_and_policy" in required_answer:
        shape.append("source_policy")
    return _unique(shape)


def _merge_planner_scope(base_scope: dict[str, Any], raw_scope: Any) -> dict[str, Any]:
    scope = {key: value for key, value in base_scope.items()}
    if not isinstance(raw_scope, dict):
        return scope
    key_map = {
        "accounts": "planned_accounts",
        "requested_accounts": "planned_accounts",
        "planned_accounts": "planned_accounts",
        "symbols": "planned_symbols",
        "requested_symbols": "planned_symbols",
        "planned_symbols": "planned_symbols",
        "months": "planned_months",
        "requested_months": "planned_months",
        "planned_months": "planned_months",
        "config_keys": "config_keys",
        "operation_id": "operation_ids",
        "operation_ids": "operation_ids",
        "command_id": "command_ids",
        "command_ids": "command_ids",
    }
    for raw_key, scope_key in key_map.items():
        values = _string_list(raw_scope.get(raw_key))
        if values:
            scope[scope_key] = _unique([*scope.get(scope_key, []), *values])
    return scope


def _normalized_contract_value(value: Any, *, allowed: set[str], default: str) -> str:
    item = str(value or "").strip().lower()
    return item if item in allowed else default


def _merge_contract_list(base: list[str], raw_value: Any) -> list[str]:
    values = list(base)
    for item in _string_list(raw_value):
        values.append(item)
    return _unique(values)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("key") or item.get("name") or item.get("value")
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


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
    if "candidate_filter_diagnostic" in intent_families:
        required.extend(["summary", "source_and_policy"])
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
        release_focused = any(token in compact for token in ("发布", "release", "deploy"))
        operation_focused = any(
            token in compact
            for token in (
                "升级",
                "回执",
                "命令",
                "当前版本",
                "目标版本",
                "版本数据",
                "command",
                "current_version",
                "target_version",
            )
        )
        if release_focused and not operation_focused:
            required.extend(["release_status", "source_and_policy"])
        else:
            required.extend(["command_status", "current_version", "target_version", "source_and_policy"])
        if release_focused:
            required.append("release_status")
    if not required:
        required.extend(["summary", "source_and_policy"])
    return _unique(required), _unique(optional)


def _constraints(intent_families: list[str], *, required_answer: list[str]) -> list[str]:
    constraints = ["must_cite_source_or_policy", "do_not_expose_internal_ids_or_sql"]
    if "account_comparison" in intent_families or "breakdown" in intent_families:
        constraints.extend(["do_not_average_return_rates", "keep_cashflow_realized_pnl_and_premium_separate"])
    if "candidate_filter_diagnostic" in intent_families:
        constraints.append("candidate_filter_root_cause_requires_trace_evidence")
    if "assigned_stock_pnl" in intent_families:
        constraints.extend(
            [
                "assigned_stock_cost_uses_delivery_price",
                "premium_only_affects_lifecycle_pnl",
                "realtime_unrealized_pnl_requires_fresh_spot",
            ]
        )
    if "upgrade_status" in intent_families and {"current_version", "target_version"} <= set(required_answer):
        constraints.append("version_receipt_requires_current_and_target_version")
    return _unique(constraints)


def _is_candidate_filter_diagnostic(question_goal_text: str, full_text: str) -> bool:
    compact = re.sub(r"\s+", "", question_goal_text.lower())
    full_compact = re.sub(r"\s+", "", full_text.lower())
    candidate_context = any(
        token in full_compact
        for token in (
            "候选",
            "candidate",
            "filter",
            "过滤",
            "trace",
        )
    )
    if not candidate_context:
        return False
    return any(
        token in compact
        for token in (
            "没出现在候选",
            "没进候选",
            "为什么没",
            "为什么",
            "被哪个参数过滤",
            "参数过滤",
            "被过滤",
            "过滤了",
            "filter",
            "filtered",
            "rejected",
            "missingcandidate",
            "why",
        )
    )


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


def _planned_symbol_values(value: Any) -> list[str]:
    symbols: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in {"symbol", "symbols"}:
                symbols.extend(_planned_symbol_field_values(item))
            elif isinstance(item, (dict, list, tuple, set)):
                symbols.extend(_planned_symbol_values(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, (dict, list, tuple, set)):
                symbols.extend(_planned_symbol_values(item))
    return _unique(symbols)


def _planned_symbol_field_values(value: Any) -> list[str]:
    symbols: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            symbols.extend(_planned_symbol_field_values(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            symbols.extend(_planned_symbol_field_values(item))
    else:
        symbol = _normalize_planned_symbol_value(value)
        if symbol:
            symbols.append(symbol)
    return _unique(symbols)


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


def _extract_symbols(text: str, *, allowed_lowercase_symbols: set[str] | None = None) -> list[str]:
    symbols: list[str] = []
    for match in _SYMBOL_TEXT_RE.finditer(str(text or "")):
        symbol = _normalize_symbol_token(match.group(1), allowed_lowercase_symbols=allowed_lowercase_symbols)
        if not symbol:
            continue
        symbols.append(symbol)
    return _unique(symbols)


def _normalize_planned_symbol_value(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    normalized = _normalize_symbol_token(text)
    if normalized:
        return normalized
    if re.fullmatch(r"[A-Za-z]{1,6}", text):
        upper = text.upper()
        if upper not in _NON_SYMBOL_TOKENS:
            return _calibrated_symbol(text) or upper
    return ""


def _normalize_symbol_token(raw: Any, *, allowed_lowercase_symbols: set[str] | None = None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if upper in _NON_SYMBOL_TOKENS:
        return ""
    if re.fullmatch(r"20\d{2}", text):
        return ""
    if re.search(r"[\u4e00-\u9fff]", text):
        return _calibrated_symbol(text)
    if re.search(r"\d", text) or "." in text:
        calibrated = _calibrated_symbol(text)
        if calibrated:
            return calibrated
        return upper
    if text == upper:
        return _calibrated_symbol(text) or upper
    if allowed_lowercase_symbols:
        calibrated = _calibrated_symbol(text)
        if calibrated in allowed_lowercase_symbols:
            return calibrated
    return ""


def _calibrated_symbol(text: str) -> str:
    calibrated = calibrate_symbol(text)
    if calibrated.status == "ok" and calibrated.canonical_symbol:
        return str(calibrated.canonical_symbol).strip().upper()
    return ""


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
