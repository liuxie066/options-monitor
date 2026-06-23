from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from src.application.assistant.capability_catalog import ACCOUNT_VALUES, spec_by_intent
from src.application.symbol_calibration import calibrate_symbol


ACTION_SAFETY_SCHEMA_VERSION = "om-agent-action-safety-v1"
_COMMAND_SPECS_BY_INTENT = spec_by_intent()
_SYMBOL_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"([A-Za-z]{1,8}(?:\.[A-Za-z]{1,4})?|[A-Za-z]{2}\.\d{4,5}|\d{3,5}(?:\.HK)?|[\u4e00-\u9fff]{2,8})"
    r"(?![A-Za-z0-9_.])"
)
_SQL_SINGLE_QUOTED_LITERAL_RE = re.compile(r"'((?:''|[^'])*)'")
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
_PAYLOAD_SCOPE_TEXT_SKIP_KEYS = {
    "artifact_path",
    "audit_db",
    "config_path",
    "db_path",
    "file_path",
    "output_path",
    "path",
    "paths",
    "raw_text",
}


@dataclass(frozen=True)
class ActionSafetyDecision:
    status: str
    code: str
    user_intent: str
    requested_effect: str
    proposed_tool: str
    proposed_effect: str
    route: str
    reason: str
    source: str = "agent_loop"
    scope_delta: dict[str, Any] = field(default_factory=dict)
    injection_evidence: tuple[str, ...] = ()
    schema_version: str = ACTION_SAFETY_SCHEMA_VERSION

    @property
    def allows_execution(self) -> bool:
        return self.status in {"allow", "allow_followup", "allow_preview"}

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "code": self.code,
            "user_intent": self.user_intent,
            "requested_effect": self.requested_effect,
            "proposed_tool": self.proposed_tool,
            "proposed_effect": self.proposed_effect,
            "scope_delta": dict(self.scope_delta),
            "injection_evidence": list(self.injection_evidence),
            "route": self.route,
            "reason": self.reason,
            "source": self.source,
        }


def assess_action_safety(
    *,
    question: str | None = None,
    task_contract: dict[str, Any] | None = None,
    tool_name: str,
    payload: dict[str, Any] | None,
    action_policy: dict[str, Any] | None = None,
    source: str = "agent_loop",
    untrusted_texts: list[str] | tuple[str, ...] | None = None,
) -> ActionSafetyDecision:
    """Rule-based second check for whether a tool call still matches the task.

    ActionPolicy remains the authority for manifest/risk permission. This
    classifier can only keep an already-allowed call allowed or make it more
    conservative when the proposed effect or scope no longer matches the
    original user request.
    """
    source_text = str(source or "").strip() or "agent_loop"
    contract = _contract_payload(task_contract)
    text = str(question if question is not None else contract.get("question") or "")
    requested_effect = _requested_effect(text=text, contract=contract)
    user_intent = _user_intent(contract=contract, requested_effect=requested_effect)
    policy = action_policy if isinstance(action_policy, dict) else {}
    proposed_effect = _proposed_effect(tool_name=tool_name, action_policy=policy)
    proposed_family = _effect_family(proposed_effect)
    scope_delta = _scope_delta(contract=contract, payload=payload or {}, text=text)
    injection_evidence = tuple(_prompt_injection_evidence(untrusted_texts or ()))

    if policy and not bool(policy.get("allowed")):
        return _decision(
            status="deny",
            code="action_policy_denied",
            user_intent=user_intent,
            requested_effect=requested_effect,
            tool_name=tool_name,
            proposed_effect=proposed_effect,
            scope_delta=scope_delta,
            injection_evidence=injection_evidence,
            route="deny",
            reason="ActionPolicy did not allow this tool call.",
            source=source_text,
        )

    if _is_apply_or_confirm(tool_name=tool_name, action_policy=policy):
        return _decision(
            status="deny",
            code="planner_apply_denied",
            user_intent=user_intent,
            requested_effect=requested_effect,
            tool_name=tool_name,
            proposed_effect=proposed_effect,
            scope_delta=scope_delta,
            injection_evidence=injection_evidence,
            route="deny",
            reason="Planner may not confirm, cancel, apply, or otherwise execute pending operations.",
            source=source_text,
        )

    if injection_evidence and proposed_family != "read":
        return _decision(
            status="deny",
            code="prompt_injection_chain",
            user_intent=user_intent,
            requested_effect=requested_effect,
            tool_name=tool_name,
            proposed_effect=proposed_effect,
            scope_delta=scope_delta,
            injection_evidence=injection_evidence,
            route="deny",
            reason="Untrusted tool output contains instruction-like text and cannot authorize a side-effect action.",
            source=source_text,
        )

    if requested_effect == "read" and proposed_family != "read":
        return _decision(
            status="deny",
            code="effect_mismatch",
            user_intent=user_intent,
            requested_effect=requested_effect,
            tool_name=tool_name,
            proposed_effect=proposed_effect,
            scope_delta=scope_delta,
            injection_evidence=injection_evidence,
            route="deny",
            reason="User asked for read-only information, but the proposed tool creates a preview or side-effect action.",
            source=source_text,
        )

    if requested_effect in {"preview", "confirm"} and proposed_family == "read":
        return _decision(
            status="ask",
            code="effect_mismatch",
            user_intent=user_intent,
            requested_effect=requested_effect,
            tool_name=tool_name,
            proposed_effect=proposed_effect,
            scope_delta=scope_delta,
            injection_evidence=injection_evidence,
            route="ask",
            reason="User request appears to require an operation preview, but the proposed tool is read-only.",
            source=source_text,
        )

    scope_code = _scope_decision_code(tool_name=tool_name, proposed_family=proposed_family, scope_delta=scope_delta)
    if scope_code:
        status = "ask" if scope_code.startswith("missing_") or proposed_family == "read" else "deny"
        return _decision(
            status=status,
            code=scope_code,
            user_intent=user_intent,
            requested_effect=requested_effect,
            tool_name=tool_name,
            proposed_effect=proposed_effect,
            scope_delta=scope_delta,
            injection_evidence=injection_evidence,
            route=status,
            reason=_scope_reason(scope_code),
            source=source_text,
        )

    if proposed_family == "preview":
        return _decision(
            status="allow_preview",
            code="ok",
            user_intent=user_intent,
            requested_effect=requested_effect,
            tool_name=tool_name,
            proposed_effect=proposed_effect,
            scope_delta=scope_delta,
            injection_evidence=injection_evidence,
            route="preview",
            reason="The proposed preview operation matches the user's explicit operation request.",
            source=source_text,
        )

    return _decision(
        status="allow",
        code="ok",
        user_intent=user_intent,
        requested_effect=requested_effect,
        tool_name=tool_name,
        proposed_effect=proposed_effect,
        scope_delta=scope_delta,
        injection_evidence=injection_evidence,
        route="execute",
        reason="The proposed read-only tool call matches the task effect and scope.",
        source=source_text,
    )


def _decision(
    *,
    status: str,
    code: str,
    user_intent: str,
    requested_effect: str,
    tool_name: str,
    proposed_effect: str,
    scope_delta: dict[str, Any],
    injection_evidence: tuple[str, ...],
    route: str,
    reason: str,
    source: str,
) -> ActionSafetyDecision:
    return ActionSafetyDecision(
        status=status,
        code=code,
        user_intent=user_intent,
        requested_effect=requested_effect,
        proposed_tool=str(tool_name or ""),
        proposed_effect=proposed_effect,
        scope_delta=scope_delta,
        injection_evidence=injection_evidence,
        route=route,
        reason=reason,
        source=source,
    )


def _contract_payload(task_contract: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(task_contract, dict):
        return dict(task_contract)
    if hasattr(task_contract, "public_payload"):
        payload = task_contract.public_payload()
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _user_intent(*, contract: dict[str, Any], requested_effect: str) -> str:
    families = [str(item) for item in contract.get("intent_families") or [] if str(item).strip()]
    if families:
        return "+".join(families)
    return f"{requested_effect}_request"


def _requested_effect(*, text: str, contract: dict[str, Any]) -> str:
    explicit = str(contract.get("requested_effect") or "").strip().lower()
    if explicit in {"read", "preview", "confirm"}:
        return explicit
    if explicit == "preview_write":
        return "preview"
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if not compact:
        return "read"
    if any(token in compact for token in ("为什么", "原因", "回执", "状态", "查询", "查看", "对比", "比较", "收益", "持仓", "盈亏")):
        if not any(
            token in compact
            for token in (
                "立即升级",
                "记录开仓",
                "记录平仓",
                "写入交易",
                "成交提醒",
                "期权被指派通知",
                "已被指派",
                "期权到期失效通知",
                "已到期失效",
            )
        ):
            return "read"
    if any(token in compact for token in ("确认", "/confirm", "cancel", "/cancel", "取消")):
        return "confirm"
    if _looks_like_preview_request(compact):
        return "preview"
    return "read"


def _looks_like_preview_request(compact: str) -> bool:
    high_confidence = (
        "记录开仓",
        "记录平仓",
        "记录交易",
        "写入交易",
        "补录",
        "成交提醒",
        "委托已全部成交",
        "成功卖出",
        "成功买入",
        "期权被指派通知",
        "已被指派",
        "期权到期失效通知",
        "已到期失效",
        "recordopen",
        "recordclose",
        "立即升级",
        "切换模型",
        "使用模型",
        "跑一次港股监控",
        "跑一次美股监控",
    )
    if any(token in compact for token in high_confidence):
        return True
    if _looks_like_monitor_run_preview(compact):
        return True
    setting_tokens = ("coveredcall", "sellcall", "sellput", "minstrike", "maxstrike", "min_strike", "max_strike")
    if ("设置" in compact or "修改监控" in compact or "配置标的" in compact) and any(token in compact for token in setting_tokens):
        return True
    return False


def _looks_like_monitor_run_preview(compact: str) -> bool:
    market = any(token in compact for token in ("港股", "香港", "hk", "美股", "美国", "us"))
    monitor = any(token in compact for token in ("监控", "monitor", "tick", "扫描", "scan"))
    run_once = any(token in compact for token in ("跑一次", "执行一次", "运行一次", "触发一次", "跑一遍", "runonce"))
    run_verb = any(token in compact for token in ("运行", "执行", "触发", "启动", "run", "start"))
    return bool(market and monitor and (run_once or run_verb))


def _proposed_effect(*, tool_name: str, action_policy: dict[str, Any]) -> str:
    risk = str(action_policy.get("risk_level") or "").strip()
    allowed_effect = str(action_policy.get("allowed_effect") or "").strip()
    spec = _COMMAND_SPECS_BY_INTENT.get(str(tool_name or ""))
    operation_action = str(getattr(spec, "operation_action", "") or "").strip()
    if operation_action in {"confirm", "cancel"} or risk == "confirm_write":
        return "confirm"
    if operation_action == "preview" or allowed_effect == "preview":
        return risk if risk in {"preview_write", "preview_admin"} else "preview"
    if allowed_effect in {"read", "none"}:
        return allowed_effect
    if risk:
        return risk
    return "read"


def _effect_family(proposed_effect: str) -> str:
    effect = str(proposed_effect or "").strip()
    if effect in {"read", "none"}:
        return "read"
    if effect.startswith("preview"):
        return "preview"
    if effect in {"confirm", "cancel", "apply"} or effect.startswith("confirm"):
        return "confirm"
    return "side_effect"


def _is_apply_or_confirm(*, tool_name: str, action_policy: dict[str, Any]) -> bool:
    name = str(tool_name or "").lower()
    if any(token in name for token in ("confirm", "cancel", "apply")):
        return True
    risk = str(action_policy.get("risk_level") or "").strip()
    effect = str(action_policy.get("allowed_effect") or "").strip()
    return risk == "confirm_write" or effect in {"confirm", "cancel", "apply"}


def _scope_delta(*, contract: dict[str, Any], payload: dict[str, Any], text: str = "") -> dict[str, Any]:
    scope = contract.get("scope") if isinstance(contract.get("scope"), dict) else {}
    requested_accounts = _scope_values_or_text(
        scope=scope,
        key="requested_accounts",
        text_values=_accounts_from_text(text),
        lower=True,
    )
    requested_symbols = _scope_symbols_or_text(
        scope=scope,
        key="requested_symbols",
        text_values=_symbols_from_text(text),
    )
    requested_months = _scope_values_or_text(
        scope=scope,
        key="requested_months",
        text_values=_months_from_text(text),
    )
    payload_text = "\n".join(_payload_scope_texts(payload))
    provided_accounts = _normal_values(
        [*_payload_values(payload, keys=("account", "accounts"), lower=True), *_accounts_from_text(payload_text)],
        lower=True,
    )
    provided_symbols = _normal_symbol_values([*_payload_raw_values(payload, keys=("symbol", "symbols")), *_symbols_from_text(payload_text)])
    provided_months = _normal_values(
        [*_payload_values(payload, keys=("month", "months")), *_months_from_text(payload_text)]
    )
    return {
        "accounts": _scope_field_delta(requested=requested_accounts, provided=provided_accounts),
        "symbols": _scope_field_delta(requested=requested_symbols, provided=provided_symbols),
        "period": _scope_field_delta(requested=requested_months, provided=provided_months),
    }


def _scope_values_or_text(
    *,
    scope: dict[str, Any],
    key: str,
    text_values: list[str],
    lower: bool = False,
) -> list[str]:
    if key not in scope:
        return text_values
    values = _normal_values(scope.get(key), lower=lower)
    return values or text_values


def _scope_symbols_or_text(*, scope: dict[str, Any], key: str, text_values: list[str]) -> list[str]:
    if key not in scope:
        return text_values
    values = _normal_symbol_values(scope.get(key))
    return values or text_values


def _accounts_from_text(text: str) -> list[str]:
    compact = str(text or "").lower()
    out: list[str] = []
    for raw in ACCOUNT_VALUES:
        account = str(raw or "").strip().lower()
        if account and re.search(rf"(?<![a-z0-9_]){re.escape(account)}(?![a-z0-9_])", compact):
            out.append(account)
    return _normal_values(out, lower=True)


def _symbols_from_text(text: str) -> list[str]:
    symbols: list[str] = []
    for match in _SYMBOL_TEXT_RE.finditer(str(text or "")):
        symbol = _normalize_symbol_token(match.group(1), free_text=True)
        if not symbol:
            continue
        symbols.append(symbol)
    return _normal_values(symbols)


def _normal_symbol_values(value: Any) -> list[str]:
    raw_values: list[Any]
    if value is None:
        raw_values = []
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]
    symbols = [_normalize_symbol_token(raw, free_text=False) for raw in raw_values]
    return _normal_values(symbols)


def _normalize_symbol_token(raw: Any, *, free_text: bool) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    upper = text.upper()
    if upper in _NON_SYMBOL_TOKENS:
        return ""
    if re.fullmatch(r"20\d{2}", text):
        return ""
    if free_text:
        calibrated = _calibrated_symbol(text)
        if calibrated:
            return calibrated
        if re.search(r"[\u4e00-\u9fff]", text):
            return ""
        if re.search(r"\d", text) or "." in text:
            return upper
        if text == upper:
            return upper
        return ""
    calibrated = _calibrated_symbol(text)
    if calibrated:
        return calibrated
    if re.search(r"\d", text) or "." in text:
        return upper
    if text == upper:
        return upper
    return ""


def _calibrated_symbol(text: str) -> str:
    calibrated = calibrate_symbol(text)
    if calibrated.status == "ok" and calibrated.canonical_symbol:
        return str(calibrated.canonical_symbol).strip().upper()
    return ""


def _months_from_text(text: str) -> list[str]:
    months: list[str] = []
    for year, month in re.findall(r"(?<!\d)(20\d{2})[-/.年](0?[1-9]|1[0-2])(?:月)?(?!\d)", str(text or "")):
        months.append(f"{year}-{int(month):02d}")
    return _normal_values(months)


def _payload_scope_texts(value: Any, *, key: str = "") -> list[str]:
    if key and key.lower() in _PAYLOAD_SCOPE_TEXT_SKIP_KEYS:
        return []
    if isinstance(value, str):
        if key.lower() == "sql":
            return _sql_scope_texts(value)
        return [value]
    if isinstance(value, dict):
        texts: list[str] = []
        for item_key, item in value.items():
            texts.extend(_payload_scope_texts(item, key=str(item_key)))
        return texts
    if isinstance(value, (list, tuple, set)):
        texts = []
        for item in value:
            texts.extend(_payload_scope_texts(item, key=key))
        return texts
    return []


def _sql_scope_texts(value: str) -> list[str]:
    return [match.group(1).replace("''", "'") for match in _SQL_SINGLE_QUOTED_LITERAL_RE.finditer(str(value or ""))]


def _scope_field_delta(*, requested: list[str], provided: list[str]) -> dict[str, Any]:
    out_of_scope = sorted(item for item in provided if requested and item not in set(requested))
    return {
        "requested": requested,
        "provided": provided,
        "status": "expanded" if out_of_scope else "same_or_unspecified",
        "out_of_scope": out_of_scope,
    }


def _scope_decision_code(*, tool_name: str, proposed_family: str, scope_delta: dict[str, Any]) -> str:
    for field in ("accounts", "symbols", "period"):
        delta = scope_delta.get(field)
        if isinstance(delta, dict) and delta.get("out_of_scope"):
            return f"{field[:-1] if field.endswith('s') else field}_scope_expansion"
    if proposed_family != "preview":
        return ""
    name = str(tool_name or "")
    accounts = scope_delta.get("accounts") if isinstance(scope_delta.get("accounts"), dict) else {}
    symbols = scope_delta.get("symbols") if isinstance(scope_delta.get("symbols"), dict) else {}
    if name in {"manual_trade_open", "manual_trade_close", "manual_assignment", "manual_expiry"} and not accounts.get("requested"):
        return "missing_account_scope"
    if name == "symbol_edit" and not symbols.get("requested"):
        return "missing_symbol_scope"
    return ""


def _scope_reason(code: str) -> str:
    return {
        "account_scope_expansion": "The proposed account scope is outside the user-requested account scope.",
        "symbol_scope_expansion": "The proposed symbol scope is outside the user-requested symbol scope.",
        "period_scope_expansion": "The proposed period scope is outside the user-requested period scope.",
        "missing_account_scope": "A write preview for manual trade records requires an explicit account in the user request.",
        "missing_symbol_scope": "A symbol configuration preview requires an explicit symbol in the user request.",
    }.get(code, "The proposed scope does not match the current task.")


def _normal_values(value: Any, *, lower: bool = False, upper: bool = False) -> list[str]:
    raw_values: list[Any]
    if value is None:
        raw_values = []
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        item = str(raw or "").strip()
        if not item:
            continue
        if lower:
            item = item.lower()
        if upper:
            item = item.upper()
        if item in seen:
            continue
        out.append(item)
        seen.add(item)
    return out


def _payload_values(payload: dict[str, Any], *, keys: tuple[str, ...], lower: bool = False, upper: bool = False) -> list[str]:
    return _normal_values(_payload_raw_values(payload, keys=keys), lower=lower, upper=upper)


def _payload_raw_values(payload: dict[str, Any], *, keys: tuple[str, ...]) -> list[Any]:
    values: list[Any] = []
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(value)
        else:
            values.append(value)
    return values


def _prompt_injection_evidence(texts: list[str] | tuple[str, ...]) -> list[str]:
    triggers = (
        "忽略上文",
        "覆盖规则",
        "确认写入",
        "立即修改配置",
        "发送通知",
        "重启服务",
        "读取token",
        "打印env",
        "ignore previous",
        "override instructions",
        "confirm write",
        "restart service",
    )
    hits: list[str] = []
    for text in texts:
        compact = re.sub(r"\s+", "", str(text or "").lower())
        if not compact:
            continue
        for trigger in triggers:
            normalized = re.sub(r"\s+", "", trigger.lower())
            if normalized in compact:
                hits.append(trigger)
                break
    return hits[:4]


__all__ = [
    "ACTION_SAFETY_SCHEMA_VERSION",
    "ActionSafetyDecision",
    "assess_action_safety",
]
