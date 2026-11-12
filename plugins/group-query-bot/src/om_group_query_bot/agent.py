"""One natural-language turn: admission, context, fixed tools, API, render.

The model only selects one of five business tools and fills business fields.
The host owns the request identity, deadline, sender/group/thread context,
credential, route, input identity and every number shown to the group.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from queue import Empty, Full, Queue
import threading
import time
import uuid
from typing import Any, Callable

from src.application.bot.runtime import request_model, token_estimate
from src.application.secret_store.runtime import resolve_secret

from .api_client import QueryAPIClient, QueryAPIError
from .budget import (
    AdmissionController,
    AdmissionRejected,
    BudgetExhausted,
    RunBudget,
)
from .config import BotSettings
from .contracts import ContractError, REQUEST_SCHEMA, SCENARIO_METHODS, normalize_request
from .feishu import (
    EventRejected,
    VerifiedGroupEvent,
    message_text,
    normalize_sdk_event,
)
from .guard import check_result_reply, correction_message
from .render import (
    fact_card,
    finalize_clarification,
    render_admission_rejected,
    render_budget_exhausted,
    render_result,
    result_contract_error,
    strip_clarification_note,
)
from .store import CONVERSATION_DB_NAME, ConversationStore, DeliveryStore, derived_expiry

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「群查询bot」，只服务已授权的个人飞书群。
你可以使用五个业务工具：query_quotes、query_candidates、evaluate_close、
evaluate_roll_down、evaluate_roll_up；另有一个宿主工具 answer_general，只用于提交
通用问题的回答。你不具备也不得声称具备任何账户、持仓、
资金、收益、成交或报告访问能力；不要提及或请求账户信息。

规则：
1. 行情、候选、平仓和移仓结论必须来自工具返回的实时数据。业务问题在工具未返回
   数据时，只能澄清问题或说明限制；不得凭记忆给出价格、费用、年化、仓位或建议。
2. 与这五项业务无关的通用问题（例如今天几点开盘、交易所日程、市场规则、合约常识、
   概念解释等）必须调用 answer_general 回答，answer 参数放完整、简短的中文回答；
   不得给出具体价格、费用、年化数值或买卖建议，也不得声称执行了实时查询。涉及
   开盘时间、交易时段等时区或夏令时敏感信息时，提醒以交易所官方说明为准。
3. 标的直接传用户原话即可（如 NVDA、0700、腾讯、MSFU）；宿主会自动补全并归一化，
   不要要求用户提供市场前缀，也不要把猜测的前缀当成事实。只有工具明确返回无法识别
   标的时，才请用户确认代码。
4. 必填参数只有：query_quotes 的 symbol；query_candidates 的 symbol、strategy；
   evaluate_close 的 position（symbol/contract/side/contracts）和 opening_premium；
   evaluate_roll_down 与 evaluate_roll_up 的 position 和 search_bounds.max_new_expiration。
   其余参数均可省略：用户没给时不要把可选参数当缺失去追问，直接调用工具；
   工具结果里会写明实际采用的期限、Delta、行权价和展示上限。
5. 只有必填参数缺失，或方向、合约、张数、权利金单位等意图本身有歧义时才追问。
6. 用户给出的相对日期要保留原话并请用户确认明确日期；最晚新到期日必须由用户明确给出。
7. 工具返回的「查询结果」是唯一的事实来源。直接引用里面的数字、日期和合约代码，
   可以保留原精度；不要自己计算（不要算价差、收益率或比例），不要编造或改写数字。
8. 像群里的朋友说话：先给结论，再说关键数字，2-4 句就够；可以分 1-3 行，但不要
   抄“查询结果：”“筛选条件：”这类标题，不要复述整份结果，不要输出 JSON 或
   Markdown 表格。不要出现英文字段名、状态代码或原因代码，把原因换成日常中文。
9. 数据是最近已知或延迟的时候，要明确说“不是现在的实时价”；涉及费用时说明是
   估算、不是账户实际扣费；条件移仓要说明股票覆盖未经核实。
10. 解释历史结果时按上面的风格转述，不要复述历史原文。不要自行添加
   “未执行实时查询”之类的免责声明：只有澄清问题、没有实时数据时，系统会统一追加。
11. 候选行权价只有三种条件：strike 是精确等于；min_strike 是最低价（含端点）；
    max_strike 是最高价（含端点）。min_strike 与 max_strike 可组合，strike 与范围互斥。
    “max strike=380”“不超过380”“最高380”使用 max_strike=380，不能用 strike 代替。
    用户明确要求严格小于或大于、不含端点时，说明目前范围包含端点并确认是否接受；
    不要声称已经执行严格比较，不要丢掉边界后改查行情。
12. 用户补充或纠正候选条件时，沿用最近候选查询的标的、方向及未修改的用户条件，
    只替换本轮明确修改的字段；从精确行权价改为上限或下限时，清除旧 strike。
    用户明确取消某条件时清除该字段、恢复公共策略默认；明确切换到查价等新问题时
    才改用相应工具。历史只供理解参数，每次候选查询仍须重新取数。
"""


def _symbol_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "description": "标的，可传用户原话（如 NVDA、0700、腾讯、MSFU）；"
                       "宿主会自动归一化，不要追问市场前缀",
    }


def _limit_schema() -> dict[str, Any]:
    return {
        "type": "integer", "minimum": 1, "maximum": 50, "default": 10,
        "description": "可选；默认 10，仅限制展示数量",
    }

TOOLS: list[dict[str, Any]] = [
    {
        "name": "query_quotes",
        "description": "查询标的与可选单张期权合约的实时公开行情；不涉及任何账户。",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": _symbol_schema(),
                "contract": {
                    "type": "object",
                    "description": "可选；省略时只查询标的行情",
                    "properties": {
                        "option_type": {"type": "string", "enum": ["put", "call"]},
                        "expiration": {"type": "string", "description": "YYYY-MM-DD"},
                        "strike": {"type": "number"},
                    },
                    "required": ["option_type", "expiration", "strike"],
                    "additionalProperties": False,
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_candidates",
        "description": "按公共策略查询卖出开仓候选；不涉及账户持仓。",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": _symbol_schema(),
                "strategy": {"type": "string", "enum": ["short_put", "short_call"]},
                "dte": {"type": "array", "items": {"type": "integer"},
                        "minItems": 2, "maxItems": 2,
                        "description": "可选；省略时使用公共策略对应方向的期限窗口，"
                                       "不要为此追问用户；结果里会写明实际生效的期限"},
                "strike": {"type": "number", "exclusiveMinimum": 0,
                           "description": "可选；精确行权价（等于）；与 min_strike、"
                                          "max_strike 互斥；三者均省略时使用公共策略范围"},
                "min_strike": {"type": "number", "exclusiveMinimum": 0,
                               "description": "可选；最低行权价（大于等于，包含端点）；"
                                              "可与 max_strike 组合，与 strike 互斥"},
                "max_strike": {"type": "number", "exclusiveMinimum": 0,
                               "description": "可选；最高行权价（小于等于，包含端点）；"
                                              "可与 min_strike 组合，与 strike 互斥"},
                "abs_delta_range": {"type": "array", "items": {"type": "number"},
                                    "minItems": 2, "maxItems": 2,
                                    "description": "可选；省略时使用公共策略 Delta 范围；"
                                                   "结果里会写明实际生效的 Delta 区间"},
                "limit": _limit_schema(),
            },
            "required": ["symbol", "strategy"],
            "additionalProperties": False,
        },
    },
    {
        "name": "evaluate_close",
        "description": "按用户临时提供的空头合约、张数与开仓权利金分析平仓。",
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "object",
                    "properties": {
                        "symbol": _symbol_schema(),
                        "contract": {
                            "type": "object",
                            "properties": {
                                "option_type": {"type": "string", "enum": ["put", "call"]},
                                "expiration": {"type": "string"},
                                "strike": {"type": "number"},
                            },
                            "required": ["option_type", "expiration", "strike"],
                            "additionalProperties": False,
                        },
                        "side": {"type": "string", "enum": ["short"]},
                        "contracts": {"type": "integer", "minimum": 1},
                    },
                    "required": ["symbol", "contract", "side", "contracts"],
                    "additionalProperties": False,
                },
                "opening_premium": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number"},
                        "unit": {"type": "string",
                                 "enum": ["per_share", "per_contract", "total"]},
                        "currency": {"type": "string", "enum": ["USD", "HKD"]},
                    },
                    "required": ["amount", "unit", "currency"],
                    "additionalProperties": False,
                },
                "opened_at": {"type": "string",
                              "description": "可选；省略表示未提供开仓日期，不阻断评估"},
            },
            "required": ["position", "opening_premium"],
            "additionalProperties": False,
        },
    },
    {
        "name": "evaluate_roll_down",
        "description": "分析空头 Put 向下移仓；最晚新到期日必填，行权价下限可选。",
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "object",
                    "properties": {
                        "symbol": _symbol_schema(),
                        "contract": {
                            "type": "object",
                            "properties": {
                                "option_type": {"type": "string", "enum": ["put"]},
                                "expiration": {"type": "string"},
                                "strike": {"type": "number"},
                            },
                            "required": ["option_type", "expiration", "strike"],
                            "additionalProperties": False,
                        },
                        "side": {"type": "string", "enum": ["short"]},
                        "contracts": {"type": "integer", "minimum": 1},
                    },
                    "required": ["symbol", "contract", "side", "contracts"],
                    "additionalProperties": False,
                },
                "search_bounds": {
                    "type": "object",
                    "properties": {
                        "max_new_expiration": {"type": "string"},
                        "min_new_strike": {"type": "number",
                                           "description": "可选；省略时由原合约、现价和"
                                                          "公共资源边界决定搜索范围"},
                    },
                    "required": ["max_new_expiration"],
                    "additionalProperties": False,
                },
                "limit": _limit_schema(),
            },
            "required": ["position", "search_bounds"],
            "additionalProperties": False,
        },
    },
    {
        "name": "evaluate_roll_up",
        "description": "分析空头 Call 向上移仓；最晚新到期日必填，行权价上限可选。",
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "object",
                    "properties": {
                        "symbol": _symbol_schema(),
                        "contract": {
                            "type": "object",
                            "properties": {
                                "option_type": {"type": "string", "enum": ["call"]},
                                "expiration": {"type": "string"},
                                "strike": {"type": "number"},
                            },
                            "required": ["option_type", "expiration", "strike"],
                            "additionalProperties": False,
                        },
                        "side": {"type": "string", "enum": ["short"]},
                        "contracts": {"type": "integer", "minimum": 1},
                    },
                    "required": ["symbol", "contract", "side", "contracts"],
                    "additionalProperties": False,
                },
                "search_bounds": {
                    "type": "object",
                    "properties": {
                        "max_new_expiration": {"type": "string"},
                        "max_new_strike": {"type": "number",
                                           "description": "可选；省略时由原合约、现价和"
                                                          "公共资源边界决定搜索范围"},
                    },
                    "required": ["max_new_expiration"],
                    "additionalProperties": False,
                },
                "limit": _limit_schema(),
            },
            "required": ["position", "search_bounds"],
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)

GENERAL_ANSWER_TOOL: dict[str, Any] = {
    "name": "answer_general",
    "description": "提交与五项业务无关的通用问题回答；宿主原样展示，不附加实时查询声明。",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string",
                       "description": "完整、简短的中文回答，不含免责声明"},
        },
        "required": ["answer"],
        "additionalProperties": False,
    },
}
MODEL_TOOLS: list[dict[str, Any]] = [*TOOLS, GENERAL_ANSWER_TOOL]


@dataclass(frozen=True)
class GeneralAnswer:
    """Host-only tool result: the model's answer to a general question."""
    answer: str


@dataclass(frozen=True)
class AgentReply:
    text: str
    status: str
    reason_codes: tuple[str, ...] = ()
    expires_at_utc: datetime | None = None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _input_digest(method: str, parameters: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical({"operation": method, "parameters": parameters}).encode("utf-8")
    ).hexdigest()


def _tool_summary(method: str, data: dict[str, Any]) -> dict[str, Any]:
    """A bounded result for the model; the renderer keeps the full data."""
    if method == "query_quotes":
        return {"method": method, "symbol": data.get("symbol"),
                "market": data.get("market"),
                "quote_status": data.get("quote_status"),
                "underlier": data.get("underlier"), "option": data.get("option"),
                "missing_requirements": data.get("missing_requirements"),
                "effective_parameters": data.get("effective_parameters")}
    if method == "query_candidates":
        return {"method": method, "symbol": data.get("symbol"),
                "market": data.get("market"), "status": data.get("status"),
                "outcome": data.get("outcome"),
                "candidates": list(data.get("candidates") or [])[:10],
                "displayed": data.get("displayed"), "omitted": data.get("omitted"),
                "reason_codes": data.get("reason_codes"),
                "effective_filters": data.get("effective_filters")}
    if method == "evaluate_close":
        return {"method": method, "symbol": data.get("symbol"),
                "market": data.get("market"),
                "recommendation_state": data.get("recommendation_state"),
                "evaluation_status": data.get("evaluation_status"),
                "quote": data.get("quote"), "metrics": data.get("metrics"),
                "fee_model": data.get("fee_model"), "reason": data.get("reason"),
                "reason_codes": data.get("reason_codes"),
                "evaluated_at_utc": data.get("evaluated_at_utc")}
    return {"method": method, "symbol": data.get("symbol"),
            "market": data.get("market"),
            "status": data.get("status"),
            "outcome": data.get("outcome"), "old_quote": data.get("old_quote"),
            "candidates": list(data.get("candidates") or [])[:10],
            "displayed": data.get("displayed"), "omitted": data.get("omitted"),
            "reason_codes": data.get("reason_codes"),
            "scope": data.get("scope"),
            "evaluated_at_utc": data.get("evaluated_at_utc")}


def _history_tool_messages(content: str, turn_index: int,
                           item_index: int) -> list[dict[str, Any]]:
    """Replay a stored result as a real tool exchange, never as model prose."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        payload = None
    method = payload.get("method") if isinstance(payload, dict) else None
    arguments = payload.get("arguments") if isinstance(payload, dict) else None
    if (isinstance(method, str) and method in TOOL_NAMES
            and isinstance(arguments, dict) and "summary" in payload):
        call_id = f"hist_{turn_index}_{item_index}"
        stored_card = payload.get("fact_card")
        content = (stored_card if isinstance(stored_card, str)
                   and stored_card.strip() else _canonical(payload["summary"]))
        return [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": call_id, "type": "function",
                             "function": {"name": method,
                                          "arguments": _canonical(arguments)}}]},
            {"role": "tool", "tool_call_id": call_id, "content": content},
        ]
    # Legacy rows store a bare summary; keep them as context, not as an
    # assistant message the model would imitate.
    return [{"role": "user",
             "content": "[历史查询结果，仅供指代，不要复述] " + content}]


def _last_candidate_query(history: list[dict]) -> dict[str, Any] | None:
    """Recover only validated arguments, never conditions invented in prior prose."""
    for turn in reversed(history):
        for item in reversed(turn["messages"]):
            if item["role"] != "tool_result":
                continue
            try:
                payload = json.loads(item["content"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("method") not in TOOL_NAMES:
                continue
            arguments = payload.get("arguments")
            if (payload["method"] != "query_candidates"
                    or not isinstance(arguments, dict)
                    or not {"symbol", "strategy"} <= arguments.keys()):
                return None
            return {key: value for key, value in arguments.items()
                    if key in {"symbol", "strategy", "strike", "min_strike", "max_strike",
                               "dte", "abs_delta_range", "limit"}}
    return None


class GroupQueryAgent:
    def __init__(self, *, settings: BotSettings, conversations: ConversationStore,
                 api_client: QueryAPIClient, api_key: str,
                 request: Callable = request_model,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] | None = None,
                 estimate: Callable = token_estimate,
                 admission: AdmissionController | None = None):
        if not api_key or api_key != api_key.strip():
            raise ValueError("a dedicated model API key is required")
        self.settings = settings
        self.conversations = conversations
        self.api_client = api_client
        self._api_key = api_key
        self._request = request
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._estimate = estimate
        self.admission = admission or AdmissionController(
            user_per_minute=settings.user_per_minute,
            group_per_minute=settings.group_per_minute,
            user_concurrency=settings.user_concurrency,
            bot_concurrency=settings.bot_concurrency,
            clock=clock,
        )

    def handle_event(self, event: VerifiedGroupEvent, text: str) -> AgentReply:
        now = self._now()
        scope = event.scope_sha256(self.settings.instance_id)
        clean = (text or "").strip()
        if not clean:
            self.conversations.record_audit(
                scope_sha256=scope, turn_id=None, status="input_error",
                reason_codes=("empty_message",), now=now)
            return AgentReply("请发送要查询的内容。", "input_error",
                              ("empty_message",))
        try:
            permit = self.admission.acquire(sender=event.sender_open_id,
                                            chat=event.chat_id)
        except AdmissionRejected as exc:
            self.conversations.record_audit(
                scope_sha256=scope, turn_id=None, status="rate_limited",
                reason_codes=(exc.reason,), now=now)
            return AgentReply(render_admission_rejected(exc.retry_after_seconds),
                              "rate_limited", (exc.reason,))
        with permit:
            return self._run(event, clean, now, scope)

    def _messages(self, history: list[dict], pending: list[dict], text: str) -> list[dict]:
        system = SYSTEM_PROMPT
        current_query = _last_candidate_query(history)
        if current_query is not None:
            system += ("\n最近一次候选查询的用户参数（只供本轮延续或修改，"
                       "不是本次查询结果；未指定的条件仍用公共策略默认）：\n"
                       + _canonical(current_query))
        if pending:
            system += "\n待补参数（仅当用户本轮明确补充时才使用）：\n" + _canonical(pending)
        messages: list[dict] = [{"role": "system", "content": system}]
        for turn_index, turn in enumerate(history):
            for item_index, item in enumerate(turn["messages"]):
                role = item["role"]
                if role == "tool_result":
                    messages.extend(_history_tool_messages(
                        item["content"], turn_index, item_index))
                elif role == "assistant":
                    content = strip_clarification_note(item["content"])
                    if content:
                        messages.append({"role": "assistant", "content": content})
                else:
                    messages.append({"role": role, "content": item["content"]})
        messages.append({"role": "user", "content": text})
        return messages

    def _reserve(self, messages: list[dict]) -> int:
        return self._estimate({"messages": messages, "tools": MODEL_TOOLS}) + self.settings.output_tokens

    def _run(self, event: VerifiedGroupEvent, text: str, now: datetime,
             scope: str) -> AgentReply:
        self.conversations.cleanup(now)
        turn_id = uuid.uuid4().hex
        source_id = event.message_id
        source_expiry = now + timedelta(hours=24)
        turn_deadline_utc = now + timedelta(seconds=self.settings.turn_seconds)
        budget = RunBudget(
            deadline_monotonic=self._clock() + self.settings.turn_seconds,
            max_model_calls=self.settings.model_calls,
            max_api_calls=self.settings.api_calls,
            max_output_tokens=self.settings.output_tokens,
            max_reserve_tokens=self.settings.reserve_tokens,
            clock=self._clock,
        )
        history = self.conversations.context_turns(
            scope, now, max_turns=self.settings.context_turns,
            max_tokens=self.settings.context_tokens, estimate=self._estimate)
        pending = self.conversations.pending_inputs(scope, now)
        messages = self._messages(history, pending, text)
        stored_results: list[dict] = []
        last_result: tuple[str, dict[str, Any]] | None = None
        last_card: str | None = None
        contract_error: str | None = None
        reply_expiry = source_expiry
        explanation = ""
        general_answer = False
        status = "ok"
        reasons: list[str] = []
        stop = False
        guard_retry_used = False
        fallback_used = False
        output_cap = min(int(self.settings.output_tokens),
                         int(self.settings.model.max_output_tokens
                             or self.settings.output_tokens))
        call_settings = replace(self.settings.model, max_output_tokens=output_cap)
        while not stop:
            try:
                budget.begin_model_call()
                budget.reserve_tokens(self._reserve(messages))
                timeout = max(0.001, min(float(self.settings.model.timeout_seconds),
                                         budget.remaining_seconds()))
                response = self._request(
                    settings=call_settings, api_key=self._api_key,
                    messages=messages, tools=MODEL_TOOLS, timeout=timeout)
            except BudgetExhausted as exc:
                status = "limited"
                reasons.append(exc.reason)
                break
            except Exception:
                status = "model_unavailable"
                reasons.append("model_transport_failed")
                break
            message = response.get("message") or {}
            explanation = str(message.get("content") or "")
            usage = response.get("usage") or {}
            output_tokens = usage.get("completion_tokens")
            if not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
                output_tokens = self._estimate(explanation)
            budget.record_output_tokens(max(0, output_tokens), limit=output_cap)
            if response.get("finish_reason") == "length":
                status = "limited"
                reasons.append("output_token_limit")
                break
            calls = message.get("tool_calls") or []
            if not calls:
                if last_result is None:
                    break
                contract_error = result_contract_error(*last_result)
                if contract_error:
                    break
                verdict = check_result_reply(
                    explanation, method=last_result[0], data=last_result[1],
                    fact_card_text=last_card or "", user_text=text, now=now)
                if verdict.ok:
                    break
                if (not guard_retry_used
                        and budget.model_calls < budget.max_model_calls):
                    guard_retry_used = True
                    messages.append({"role": "assistant", "content": explanation})
                    messages.append({"role": "user",
                                     "content": correction_message(verdict)})
                    continue
                fallback_used = True
                reasons.append("reply_guard_fallback")
                break
            messages.append({"role": "assistant", "content": explanation,
                             "tool_calls": calls})
            for call in calls:
                try:
                    payload, result = self._dispatch(
                        call, event=event, scope=scope, budget=budget, now=now,
                        deadline=turn_deadline_utc,
                        source_id=source_id, source_expiry=source_expiry)
                except BudgetExhausted as exc:
                    status = "limited"
                    reasons.append(exc.reason)
                    stop = True
                    break
                if result is not None:
                    if isinstance(result, GeneralAnswer):
                        explanation = result.answer
                        general_answer = True
                        reasons.append("general_answer")
                        stop = True
                        break
                    last_result = (result[0], result[1])
                    last_card = fact_card(result[0], result[1])
                    contract_error = result_contract_error(result[0], result[1])
                    reply_expiry = min(reply_expiry, result[2])
                    stored_results.append({"method": result[0],
                                           "arguments": result[3],
                                           "summary": _tool_summary(result[0], result[1]),
                                           "fact_card": last_card})
                    # Candidate conditions and quotes are rendered from the API
                    # result; another prose pass could claim unapplied filters.
                    if contract_error or result[0] == "query_candidates":
                        stop = True
                messages.append({"role": "tool",
                                 "tool_call_id": str(call.get("id") or ""),
                                 "content": last_card if result is not None
                                 else _canonical(payload)})
                if stop:
                    break
            if stop:
                break
            if budget.stopped_reason is not None:
                status = "limited"
                reasons.append(budget.stopped_reason)
                break
            if budget.model_calls >= budget.max_model_calls:
                status = "limited"
                reasons.append("model_call_limit")
                break

        if budget.stopped_reason is not None and status == "ok":
            status = "limited"
            reasons.append(budget.stopped_reason)
        if contract_error is None and last_result is not None:
            contract_error = result_contract_error(*last_result)
        rendered = render_result(*last_result) if last_result is not None else None
        prose = strip_clarification_note(explanation)
        if status == "model_unavailable":
            body = "自然语言服务当前不可用，请稍后重试。"
        elif status == "limited":
            note = render_budget_exhausted(reasons[0] if reasons else "limit")
            body = f"{note}\n\n{rendered}" if rendered else note
        elif contract_error:
            body = contract_error
            reasons.append("result_contract_inconsistent")
        elif last_result is not None and last_result[0] == "query_candidates":
            body = rendered
        elif fallback_used:
            body = rendered
        elif rendered is not None:
            body = prose or rendered
            if guard_retry_used:
                reasons.append("reply_guard_retry")
        else:
            if prose:
                body = prose if general_answer else finalize_clarification(prose)
            else:
                body = finalize_clarification(
                    "请补充标的、合约、张数和开仓权利金等信息后再查询。")
                reasons.append("clarification_required")
        self._store_turn(scope=scope, turn_id=turn_id, user_text=text,
                         results=stored_results, reply=body, now=now,
                         source_id=source_id, source_expiry=source_expiry,
                         reply_expiry=reply_expiry)
        self.conversations.record_audit(
            scope_sha256=scope, turn_id=turn_id, status=status,
            reason_codes=tuple(reasons), now=now)
        return AgentReply(body, status, tuple(reasons),
                          min(reply_expiry, now + timedelta(hours=24)))

    def _store_turn(self, *, scope: str, turn_id: str, user_text: str,
                    results: list[dict], reply: str, now: datetime,
                    source_id: str, source_expiry: datetime,
                    reply_expiry: datetime) -> None:
        self.conversations.append_message(
            scope_sha256=scope, turn_id=turn_id, seq=0, role="user",
            content=user_text, now=now, source_id=source_id,
            source_expiry=source_expiry)
        seq = 1
        for result in results:
            payload = {"method": result["method"],
                       "arguments": result.get("arguments") or {},
                       "summary": result["summary"]}
            if result.get("fact_card"):
                payload["fact_card"] = result["fact_card"]
            self.conversations.append_message(
                scope_sha256=scope, turn_id=turn_id, seq=seq, role="tool_result",
                content=_canonical(payload), now=now,
                source_id=source_id, source_expiry=derived_expiry(now, reply_expiry))
            seq += 1
        self.conversations.append_message(
            scope_sha256=scope, turn_id=turn_id, seq=seq, role="assistant",
            content=strip_clarification_note(reply), now=now, source_id=source_id,
            source_expiry=derived_expiry(now, reply_expiry))

    def _dispatch(self, call: dict[str, Any], *, event: VerifiedGroupEvent,
                  scope: str, budget: RunBudget, now: datetime,
                  deadline: datetime,
                  source_id: str, source_expiry: datetime):
        name = str((call.get("function") or {}).get("name") or "")
        if name == GENERAL_ANSWER_TOOL["name"]:
            try:
                arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                return ({"ok": False, "error": "INPUT_ERROR",
                         "message": "tool arguments must be a JSON object"}, None)
            if not isinstance(arguments, dict):
                return ({"ok": False, "error": "INPUT_ERROR",
                         "message": "tool arguments must be a JSON object"}, None)
            answer = arguments.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                return ({"ok": False, "error": "INPUT_ERROR",
                         "message": "answer must be a non-empty string"}, None)
            return ({"ok": True, "mode": "general"}, GeneralAnswer(answer.strip()))
        if name not in TOOL_NAMES:
            return ({"ok": False, "error": "unsupported_tool",
                     "message": "only the five group-query tools exist"}, None)
        try:
            arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            return ({"ok": False, "error": "INPUT_ERROR",
                     "message": "tool arguments must be a JSON object"}, None)
        if not isinstance(arguments, dict):
            return ({"ok": False, "error": "INPUT_ERROR",
                     "message": "tool arguments must be a JSON object"}, None)
        request_id = uuid.uuid4().hex
        body: dict[str, Any] = {
            "schema_version": REQUEST_SCHEMA,
            "request_id": request_id,
            "request_deadline_utc": deadline.isoformat(),
            "request_context": {
                "tenant_key": event.tenant_key,
                "chat_id": event.chat_id,
                "sender_open_id": event.sender_open_id,
                "thread_id": event.thread_id,
            },
            "parameters": arguments,
        }
        input_id = None
        input_as_of = None
        input_expiry = source_expiry
        try:
            if name in SCENARIO_METHODS:
                provisional = {**body, "input_id": "provisional",
                               "input_as_of_utc": now.isoformat()}
                normalized = normalize_request(name, provisional, now=now)
                digest = _input_digest(name, normalized.parameters)
                existing = self.conversations.resolve_confirmed_input(
                    scope_sha256=scope, operation=name,
                    input_sha256=digest, now=now)
                input_id = existing or uuid.uuid4().hex
                expiry_text = self.conversations.save_confirmed_input(
                    scope_sha256=scope, input_id=input_id, input_sha256=digest,
                    operation=name, parameters=normalized.parameters, now=now,
                    source_id=source_id, source_expiry=source_expiry)
                input_expiry = datetime.fromisoformat(expiry_text)
                input_as_of = now
                body["input_id"] = input_id
                body["input_as_of_utc"] = now.isoformat()
            else:
                normalized = normalize_request(name, body, now=now)
        except ContractError as exc:
            if exc.code == "INPUT_ERROR":
                self.conversations.save_pending_input(
                    scope_sha256=scope, operation=name, parameters=arguments,
                    now=now, source_id=source_id, source_expiry=source_expiry)
            return ({"ok": False, "error": exc.code, "message": exc.message}, None)
        try:
            budget.begin_api_call()
            data = self.api_client.call(
                method=name, request_id=request_id, deadline_utc=deadline,
                context=normalized.context.public_dict(),
                parameters=normalized.parameters,
                input_id=input_id, input_as_of_utc=input_as_of,
                timeout_seconds=min(float(self.settings.api_timeout_seconds),
                                    budget.remaining_seconds()))
        except BudgetExhausted:
            raise
        except QueryAPIError as exc:
            return ({"ok": False, "error": exc.code, "message": exc.message}, None)
        payload = {"ok": True, **_tool_summary(name, data)}
        # Persist the normalized parameters, never the model's raw spelling:
        # aliases and old market prefixes must not be replayed from history.
        return payload, (name, data, input_expiry, normalized.parameters)


class BotService:
    """One bot instance: credentials, stores, agent and delivery semantics."""

    def __init__(self, settings: BotSettings, *, secret_provider=None,
                 request: Callable = request_model, reply_fn: Callable | None = None,
                 now: Callable[[], datetime] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._clock = clock
        api_token = resolve_secret(settings.query_api_token_ref, provider=secret_provider)
        if not api_token:
            raise RuntimeError("group-query API credential is unavailable")
        api_key = resolve_secret(settings.model.credential_name, provider=secret_provider)
        if not api_key:
            raise RuntimeError("group-query model credential is unavailable")
        self.feishu_secret = resolve_secret(settings.app_secret_ref,
                                            provider=secret_provider)
        if not self.feishu_secret:
            raise RuntimeError("group-query Feishu credential is unavailable")
        self.conversations = ConversationStore(settings.data_dir)
        self.deliveries = DeliveryStore(settings.data_dir, db_name=CONVERSATION_DB_NAME)
        # Startup retention; a failed cleanup cannot expose expired reads because
        # every read path filters by expiry independently.
        for cleanup in (self.conversations.cleanup, self.deliveries.cleanup):
            try:
                cleanup(self._now())
            except Exception as exc:
                LOGGER.warning("group_query_retention_cleanup_failed error=%s",
                               type(exc).__name__)
        self.api_client = QueryAPIClient(
            base_url=settings.query_api_base_url, token=api_token,
            timeout_seconds=settings.api_timeout_seconds)
        self.agent = GroupQueryAgent(
            settings=settings, conversations=self.conversations,
            api_client=self.api_client, api_key=api_key, request=request,
            now=now, clock=clock)
        self.reply_fn = reply_fn or self._default_reply

    def _default_reply(self, **kwargs):
        from src.infrastructure.feishu_bot import reply_message
        return reply_message(app_id=self.settings.app_id,
                             app_secret=self.feishu_secret, **kwargs)

    def handle_payload(self, payload: object, bot_open_id: str,
                       *, text: str | None = None) -> dict:
        now = self._now()
        try:
            event = normalize_sdk_event(payload, self.settings.app_id, bot_open_id,
                                        self.settings.allowed_chat_ids,
                                        require_mention=False)
            user_text = text if text is not None else message_text(payload)
        except EventRejected as exc:
            LOGGER.info("group_query_event_rejected reason=%s", str(exc))
            return {"status": "rejected", "reason": str(exc)}
        scope = event.scope_sha256(self.settings.instance_id)
        if not event.bot_mentioned and not self.deliveries.has_recent_acknowledgement(
                scope, now, self.settings.followup_window_seconds):
            LOGGER.info("group_query_event_ignored reason=no_active_conversation scope=%s",
                        scope)
            return {"status": "rejected", "reason": "not_mentioned"}
        row, claimed = self.deliveries.claim(event, self.settings.instance_id, now)
        if not claimed:
            return {"status": "duplicate", "delivery_state": row["state"]}
        try:
            reply = self.agent.handle_event(event, user_text)
            scope = event.scope_sha256(self.settings.instance_id)
            source_expiry = reply.expires_at_utc or (now + timedelta(hours=24))
            self.conversations.save_outbox(
                delivery_key=row["message_key"], scope_sha256=scope, body=reply.text,
                now=now, source_id=event.message_id, source_expiry=source_expiry)
        except Exception as exc:
            LOGGER.warning("group_query_turn_failed delivery=%s error=%s",
                           row["message_key"], type(exc).__name__)
            try:
                self.deliveries.finish(row["message_key"], "failed")
            except Exception:
                LOGGER.warning("group_query_delivery_finish_failed delivery=%s",
                               row["message_key"])
            try:
                self.reply_fn(
                    message_id=event.message_id,
                    text="处理这条消息时发生内部错误，请稍后重试。",
                    uuid=row["delivery_uuid"],
                    reply_in_thread=bool(event.thread_id))
            except Exception:
                LOGGER.warning("group_query_failure_reply_failed delivery=%s",
                               row["message_key"])
            return {"status": "failed", "reason": "turn_failed"}
        started = False

        def before_send():
            nonlocal started
            self.deliveries.mark_sending(row["message_key"])
            started = True

        try:
            response = self.reply_fn(
                message_id=event.message_id, text=reply.text,
                uuid=row["delivery_uuid"], reply_in_thread=bool(event.thread_id),
                before_send=before_send)
            if not started:
                state = "outcome_unknown"
            elif (isinstance(response, dict) and type(response.get("code")) is int
                  and response["code"] == 0 and isinstance(response.get("data"), dict)
                  and isinstance(response["data"].get("message_id"), str)
                  and response["data"]["message_id"].strip()):
                state = "acknowledged"
            elif isinstance(response, dict) and type(response.get("code")) is int \
                    and response["code"] != 0:
                state = "failed"
            else:
                state = "outcome_unknown"
        except Exception as exc:
            from src.infrastructure.feishu_bitable import (
                FeishuAuthError, FeishuPermanentError, FeishuPermissionError,
                FeishuRateLimitError,
            )
            explicit_failure = isinstance(
                exc, (FeishuAuthError, FeishuPermissionError, FeishuRateLimitError)) or (
                isinstance(exc, FeishuPermanentError) and (
                    exc.code is not None or
                    (isinstance(exc.response, dict)
                     and isinstance(exc.response.get("http_status"), int)
                     and 400 <= exc.response["http_status"] < 500)))
            state = "failed" if not started or explicit_failure else "outcome_unknown"
        self.deliveries.finish(row["message_key"], state)
        return {"status": state, "scope_sha256": scope,
                "agent_status": reply.status}


class BusinessWorker:
    """Bounded callback queue; the SDK callback itself never runs a turn."""

    def __init__(self, service: BotService, bot_open_id: str, *, queue_size: int = 16):
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.service = service
        self.bot_open_id = bot_open_id
        self.queue: Queue = Queue(maxsize=queue_size)
        self.stopping = threading.Event()
        self.queue_rejections = 0
        self.thread = threading.Thread(target=self._run, name="group-query-bot",
                                       daemon=True)
        self.thread.start()

    def submit(self, payload: object) -> bool:
        if self.stopping.is_set():
            return False
        try:
            self.queue.put_nowait(payload)
            return True
        except Full:
            self.queue_rejections += 1
            return False

    def _run(self):
        maintenance_at = time.monotonic() + 60
        while not self.stopping.is_set():
            if time.monotonic() >= maintenance_at:
                for cleanup in (self.service.conversations.cleanup,
                                self.service.deliveries.cleanup):
                    try:
                        cleanup(self.service._now())
                    except Exception as exc:
                        LOGGER.warning(
                            "group_query_retention_cleanup_failed error=%s",
                            type(exc).__name__)
                maintenance_at = time.monotonic() + 60
            try:
                payload = self.queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                self.service.handle_payload(payload, self.bot_open_id)
            except Exception as exc:
                # Provider text and payload content never enter logs.
                LOGGER.warning("group_query_event_failed error=%s",
                               type(exc).__name__)

    def close(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=5)


__all__ = [
    "AgentReply",
    "BotService",
    "BusinessWorker",
    "GENERAL_ANSWER_TOOL",
    "GroupQueryAgent",
    "MODEL_TOOLS",
    "SYSTEM_PROMPT",
    "TOOLS",
    "TOOL_NAMES",
]
