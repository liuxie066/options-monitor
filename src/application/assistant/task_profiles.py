from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


TASK_PROFILE_SCHEMA_VERSION = "om-agent-task-profile-v1"


@dataclass(frozen=True)
class TaskProfile:
    name: str
    domains: tuple[str, ...]
    task_modes: tuple[str, ...]
    trigger_terms: tuple[str, ...]
    required_evidence: tuple[str, ...]
    required_views: tuple[str, ...]
    required_answer: tuple[str, ...]
    answer_shape: tuple[str, ...]
    completion_answer_keys: tuple[str, ...] = ()
    tool_name: str = "analysis_query"
    schema_version: str = TASK_PROFILE_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "domains": list(self.domains),
            "task_modes": list(self.task_modes),
            "required_evidence": list(self.required_evidence),
            "required_views": list(self.required_views),
            "required_answer": list(self.required_answer),
            "answer_shape": list(self.answer_shape),
            "completion_answer_keys": list(self.completion_answer_keys),
            "tool_name": self.tool_name,
        }


TASK_PROFILES: tuple[TaskProfile, ...] = (
    TaskProfile(
        name="option_operation_review",
        domains=("strategy", "position", "income", "general"),
        task_modes=("analyze", "recommend", "summarize"),
        trigger_terms=("期权操作", "期权交易", "交易记录", "复盘", "不合理", "优化"),
        required_evidence=(
            "monthly_performance",
            "income_components",
            "trade_or_cashflow_rows",
            "open_option_exposure",
            "strategy_premise",
        ),
        required_views=(
            "account_monthly_performance",
            "account_monthly_income_components",
            "monthly_income_cashflow_rows",
            "trade_events",
            "open_option_exposure",
            "strategy_config_by_symbol_account",
            "strategy_replay_read_surface",
        ),
        required_answer=("overall_judgement", "operation_patterns", "optimization_options", "source_and_policy"),
        answer_shape=("judgement", "weak_patterns", "options", "evidence_boundary"),
        completion_answer_keys=("overall_judgement", "operation_patterns", "optimization_options", "source_and_policy"),
    ),
    TaskProfile(
        name="monthly_income_analysis",
        domains=("income",),
        task_modes=("analyze", "compare", "summarize", "explain"),
        trigger_terms=(
            "收益",
            "收入",
            "现金流",
            "净现金流",
            "权利金",
            "已实现",
            "主要来自",
            "来源",
            "组成",
            "构成",
            "账户表现",
            "表现更好",
        ),
        required_evidence=("monthly_performance", "income_components", "source_policy"),
        required_views=(
            "account_monthly_performance",
            "account_monthly_income_components",
            "symbol_income_attribution",
            "monthly_income_cashflow_rows",
            "monthly_income_realized_rows",
            "monthly_income_premium_rows",
        ),
        required_answer=("summary", "main_drivers", "source_and_policy"),
        answer_shape=("conclusion", "drivers", "source_policy"),
        completion_answer_keys=("summary", "main_drivers", "source_and_policy"),
    ),
    TaskProfile(
        name="assigned_stock_review",
        domains=("position", "income"),
        task_modes=("analyze", "diagnose", "recommend", "summarize"),
        trigger_terms=("指派正股", "被指派股票", "被指派正股", "assigned stock", "assigned-stock"),
        required_evidence=("assigned_stock_position_pnl", "source_policy"),
        required_views=("assigned_stock_position_pnl",),
        required_answer=("shares_remaining", "cost_basis", "unrealized_pnl", "lifecycle_pnl", "source_and_policy"),
        answer_shape=("position", "pnl", "options", "evidence_boundary"),
        completion_answer_keys=("shares_remaining", "cost_basis", "unrealized_pnl", "lifecycle_pnl", "source_and_policy"),
        tool_name="option_positions_read",
    ),
    TaskProfile(
        name="position_risk_diagnosis",
        domains=("position",),
        task_modes=("diagnose", "analyze", "recommend", "summarize"),
        trigger_terms=("持仓", "仓位", "风险", "快到期", "到期", "敞口"),
        required_evidence=("open_option_exposure", "expiration_risk", "position_lots", "source_policy"),
        required_views=("open_option_exposure", "expiration_risk_buckets", "position_lots", "quote_freshness"),
        required_answer=("risk_summary", "priority_positions", "source_and_policy"),
        answer_shape=("risk", "priority", "options", "evidence_boundary"),
        completion_answer_keys=("risk_summary", "priority_positions", "source_and_policy"),
    ),
    TaskProfile(
        name="candidate_strategy_diagnosis",
        domains=("candidate", "strategy"),
        task_modes=("diagnose", "analyze", "recommend", "explain", "summarize"),
        trigger_terms=(
            "候选",
            "过滤",
            "没通过",
            "没出",
            "推荐",
            "参数",
            "策略",
            "太严",
            "sell put",
            "sellput",
            "candidate",
            "filter",
            "candidate_filter_explain",
        ),
        required_evidence=("candidate_filter_diagnostics", "strategy_config", "source_policy"),
        required_views=(
            "candidate_filter_diagnostics",
            "strategy_config_by_symbol_account",
            "quote_freshness",
            "strategy_replay_read_surface",
        ),
        required_answer=("summary", "root_cause", "source_and_policy"),
        answer_shape=("observation", "cause_chain", "adjustable_parameters", "evidence_boundary"),
        completion_answer_keys=("summary", "root_cause", "source_and_policy"),
    ),
    TaskProfile(
        name="symbol_config_read",
        domains=("config", "strategy", "general"),
        task_modes=("explain", "summarize", "diagnose", "analyze"),
        trigger_terms=(
            "配置",
            "参数",
            "监控标的",
            "下限",
            "上限",
            "max strike",
            "maxstrike",
            "min strike",
            "minstrike",
            "symbol_config_read",
            "symbol_strategy_config",
            "strategy_config",
        ),
        required_evidence=("strategy_config", "source_policy"),
        required_views=("symbol_strategy_config", "strategy_config_by_symbol_account", "candidate_filter_diagnostics"),
        required_answer=("summary", "config_values", "source_and_policy"),
        answer_shape=("observation", "config_values", "evidence_boundary"),
        completion_answer_keys=("summary", "config_values", "source_and_policy"),
    ),
    TaskProfile(
        name="close_advice_review",
        domains=("strategy", "position", "runtime"),
        task_modes=("diagnose", "analyze", "recommend", "summarize"),
        trigger_terms=("closeadvice", "close advice", "平仓建议", "平仓", "止盈", "健康度"),
        required_evidence=("close_advice_snapshot", "open_option_exposure", "source_policy"),
        required_views=("close_advice_snapshot", "open_option_exposure", "runtime_tick_status"),
        required_answer=("summary", "root_cause", "source_and_policy"),
        answer_shape=("status", "cause", "options", "evidence_boundary"),
        completion_answer_keys=("summary", "root_cause", "source_and_policy"),
    ),
    TaskProfile(
        name="runtime_health_diagnosis",
        domains=("runtime", "operation"),
        task_modes=("diagnose", "summarize", "explain", "analyze"),
        trigger_terms=("健康", "状态", "扫描", "通知", "推送", "线上", "运行", "runtime", "升级", "更新"),
        required_evidence=("runtime_status", "source_policy"),
        required_views=("runtime_tick_status", "quote_freshness"),
        required_answer=("summary", "source_and_policy"),
        answer_shape=("status", "cause", "next_step", "evidence_boundary"),
        completion_answer_keys=("summary", "source_and_policy"),
    ),
)


def select_task_profiles(*, text: str, domain: str, task_mode: str) -> tuple[TaskProfile, ...]:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    selected: list[tuple[int, int, TaskProfile]] = []
    for profile in TASK_PROFILES:
        trigger_score = _trigger_score(compact, profile.trigger_terms)
        trigger_matched = trigger_score > 0
        domain_matched = domain in profile.domains
        mode_matched = task_mode in profile.task_modes
        if trigger_matched and (domain_matched or domain == "general"):
            selected.append((trigger_score + int(domain_matched) + int(mode_matched), -len(selected), profile))
    selected.sort(reverse=True)
    return tuple(profile for _score, _position, profile in selected[:2])


def profile_by_name(name: str) -> TaskProfile | None:
    return next((profile for profile in TASK_PROFILES if profile.name == name), None)


def _trigger_matches(compact: str, trigger_terms: tuple[str, ...]) -> bool:
    return _trigger_score(compact, trigger_terms) > 0


def _trigger_score(compact: str, trigger_terms: tuple[str, ...]) -> int:
    return max((len(term) for term in _matched_triggers(compact, trigger_terms)), default=0)


def _matched_triggers(compact: str, trigger_terms: tuple[str, ...]) -> list[str]:
    return [
        normalized
        for term in trigger_terms
        if (normalized := re.sub(r"\s+", "", term.lower())) and normalized in compact
    ]


__all__ = ["TASK_PROFILE_SCHEMA_VERSION", "TASK_PROFILES", "TaskProfile", "profile_by_name", "select_task_profiles"]
