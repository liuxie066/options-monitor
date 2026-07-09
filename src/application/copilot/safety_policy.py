from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyRule:
    name: str
    pattern: re.Pattern[str]


_ADD_ENTITY_WORD = r"(?<!增)加(?=\s|[A-Za-z0-9])"
_CONFIG_MUTATION_WORDS = rf"新增|添加|加入|加到|{_ADD_ENTITY_WORD}|删除|移除|修改|改成|更新|\b(add|delete|remove|update)\b"
_STATE_MUTATION_WORDS = rf"新增|添加|加入|加到|{_ADD_ENTITY_WORD}|删除|移除|修改|改成|\b(add|delete|remove|update)\b"
_READ_LIKE_MUTATION_QUESTION_RE = re.compile(
    r"有没有.{0,12}需要.{0,12}修改.{0,12}地方",
    re.IGNORECASE,
)

SAFETY_RULES: tuple[SafetyRule, ...] = (
    SafetyRule(
        "config_mutation_request",
        re.compile(rf"(?=.*(?:配置|config))(?=.*(?:{_CONFIG_MUTATION_WORDS}))", re.IGNORECASE),
    ),
    SafetyRule(
        "notification_send_request",
        re.compile(r"(发送通知|发通知|(?<!没有)(?<!没)通知我|\b(send|notify)\b)", re.IGNORECASE),
    ),
    SafetyRule(
        "broker_trade_request",
        re.compile(r"(下单|买入|卖出|开仓|平仓|\border\b)", re.IGNORECASE),
    ),
    SafetyRule(
        "release_or_service_change_request",
        re.compile(r"(升级|发布|推送\s*(?:release|版本)|\b(upgrade|release|deploy|push)\b)", re.IGNORECASE),
    ),
    SafetyRule(
        "state_mutation_request",
        re.compile(_STATE_MUTATION_WORDS, re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    hits: tuple[str, ...] = ()
    suppressed_hits: tuple[str, ...] = ()
    refusal_reason: str | None = None

    def trace(self) -> dict[str, object]:
        return {
            "safety_hits": list(self.hits),
            "safety_suppressed_hits": list(self.suppressed_hits),
            "refusal_reason": self.refusal_reason,
        }


def evaluate_safety(message: str) -> SafetyDecision:
    hits: list[str] = []
    suppressed_hits: list[str] = []
    for rule in SAFETY_RULES:
        if not rule.pattern.search(message):
            continue
        if _is_read_like_mutation_question(message, rule.name):
            suppressed_hits.append(rule.name)
            continue
        hits.append(rule.name)
    if hits:
        return SafetyDecision(
            allowed=False,
            hits=tuple(hits),
            suppressed_hits=tuple(suppressed_hits),
            refusal_reason=hits[0],
        )
    return SafetyDecision(allowed=True, suppressed_hits=tuple(suppressed_hits))


def _is_read_like_mutation_question(message: str, rule_name: str) -> bool:
    if rule_name not in {"config_mutation_request", "state_mutation_request"}:
        return False
    return bool(_READ_LIKE_MUTATION_QUESTION_RE.search(message))
