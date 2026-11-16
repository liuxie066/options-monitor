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
import re
from queue import Empty, Full, Queue
import threading
import time
import uuid
from typing import Any, Callable
from zoneinfo import ZoneInfo

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
    _classify_delivery_outcome,
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
    render_query_failure,
    result_contract_error,
    strip_clarification_note,
)
from .store import CONVERSATION_DB_NAME, ConversationStore, DeliveryStore, derived_expiry

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「群查询bot」，只服务已授权的个人飞书群。
你可以使用五个业务工具：query_quotes、query_candidates、evaluate_close、
evaluate_roll_down、evaluate_roll_up；另有一个宿主工具 answer_general，只用于提交
通用问题的回答；ask_user_question 用于提出具体问题，可给最多三个选项，用户也可自由回答。
你不具备也不得声称具备任何账户、持仓、
资金、收益、成交或报告访问能力；不要提及或请求账户信息。

规则：
1. 行情、候选、平仓和移仓结论必须来自工具返回的实时数据。业务问题在工具未返回
   数据时，只能澄清问题或说明限制；不得凭记忆给出价格、费用、年化、仓位或建议。
2. 与这五项业务无关的通用问题（例如今天几点开盘、交易所日程、市场规则、合约常识、
   概念解释等）必须调用 answer_general 回答，answer 参数放完整、简短的中文回答；
   不得给出具体价格、费用、年化数值或买卖建议，也不得声称执行了实时查询。涉及
   开盘时间、交易时段、夏令时等信息时，必须基于本轮注入的服务端当前时间与用户时区
   计算；用户未另行指定时，“今天”等相对日期以用户时区为准，不得猜测当前日期或
   夏令时，并提醒以交易所官方说明为准。时间上下文仅用于通用问答中的日历与时区
   推算；业务数字仍必须来自工具，不得把时间上下文当作行情或其他业务事实。
3. 标的直接传用户原话即可（如 NVDA、0700、腾讯、MSFU）；宿主会自动补全并归一化，
   不要要求用户提供市场前缀，也不要把猜测的前缀当成事实。只有工具明确返回无法识别
   标的时，才请用户确认代码。
4. 必填参数只有：query_quotes 的 symbol；query_candidates 的 symbol、strategy；
   evaluate_close 的 position（symbol/contract/side/contracts）和 opening_premium；
   evaluate_roll_down 与 evaluate_roll_up 的 position 和 search_bounds.max_new_expiration。
   其余参数均可省略：用户没给时不要把可选参数当缺失去追问，直接调用工具；
   工具结果里会写明实际采用的期限、Delta、行权价和展示上限。
5. 只有必填参数缺失，或方向、合约、张数、权利金单位等意图本身有歧义时才追问。
   目标不明（例如仅说“分析下 NFLX”且没有有效任务上下文）也须调用 ask_user_question
   确认要查行情还是卖出期权候选。不要用业务工具探测意图。提问与业务调用不可同批。
   参数错误须先修正自己填错的值；需要用户确认时只问具体缺项。不用 answer_general
   绕过缺参、查询失败或有效零结果。用户取消时不再调用业务工具。
6. 业务工具参数中的相对日期要保留用户原话并请用户确认明确日期；最晚新到期日必须由用户明确给出。
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
13. 业务工具的 source_scope 只选择宿主的来源核验路径，不指定来源或保留期限。
    单项补参、编号回答、条件修改依赖历史时填 conversation；用户完整重述全部必要事实，
    或明确独立的新查询时填 current_message。宿主会仅用当前原文独立重建并校验完整请求；
    不能独立重建时会要求重新确认。不能为获得新期限而补造用户没有提供的事实。
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
QUESTION_TOOL: dict[str, Any] = {
    "name": "ask_user_question",
    "description": "确认查询目标或必要的用户输入；提出一个具体中文问题，随后结束本轮。",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 300},
            "options": {"type": "array", "maxItems": 3, "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 60}},
        },
        "required": ["question"], "additionalProperties": False,
    },
}
MODEL_TOOLS: list[dict[str, Any]] = [
    {**tool, "input_schema": {
        **tool["input_schema"], "properties": {
            **tool["input_schema"]["properties"],
            "source_scope": {
                "type": "string", "enum": ["conversation", "current_message"],
                "description": "宿主内部来源核验选择；依赖历史的补参/修改选conversation；"
                               "当前消息完整重述或独立查询选current_message，仍需独立重建校验；"
                               "省略时宿主保守核验。不能提供TTL、source_id或身份。",
            },
        },
    }} for tool in TOOLS
] + [GENERAL_ANSWER_TOOL, QUESTION_TOOL]


class SourceConfirmationRequired(RuntimeError):
    """No business work may follow an uncertain current-message reconstruction."""


def _business_arguments(call: dict) -> tuple[str, dict, str | None]:
    name = str((call.get("function") or {}).get("name") or "")
    arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    arguments = dict(arguments)
    source_scope = arguments.pop("source_scope", None)
    if source_scope is not None and (
            not isinstance(source_scope, str)
            or source_scope not in {"conversation", "current_message"}):
        raise SourceConfirmationRequired("invalid_source_scope")
    return name, arguments, source_scope


def _business_body(name: str, arguments: dict, event: VerifiedGroupEvent,
                   deadline: datetime, now: datetime, *, request_id: str) -> dict:
    body = {
        "schema_version": REQUEST_SCHEMA, "request_id": request_id,
        "request_deadline_utc": deadline.isoformat(),
        "request_context": {"tenant_key": event.tenant_key, "chat_id": event.chat_id,
                            "sender_open_id": event.sender_open_id, "thread_id": event.thread_id},
        "parameters": arguments,
    }
    if name in SCENARIO_METHODS:
        body.update(input_id="provisional", input_as_of_utc=now.isoformat())
    return body


@dataclass(frozen=True)
class UserQuestion:
    text: str


def _question(arguments: Any) -> UserQuestion | None:
    if not isinstance(arguments, dict) or set(arguments) - {"question", "options"}:
        return None
    question = arguments.get("question")
    options = arguments.get("options", [])
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 300:
        return None
    if not isinstance(options, list) or len(options) > 3:
        return None
    if any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 60
           for item in options):
        return None
    options = [item.strip() for item in options]
    if len(set(options)) != len(options):
        return None
    lines = [question.strip(), *[f"{i}. {item}" for i, item in enumerate(options, 1)]]
    if options:
        lines.append("请回复编号、选项文字，或直接描述你的想法。")
    return UserQuestion("\n".join(lines))


_FIELD_LABELS = {
    "symbol": "标的代码", "strategy": "卖出 Put 还是卖出 Call",
    "position": "原合约、空头方向及张数", "contract": "具体期权合约",
    "option_type": "Put 或 Call 方向", "expiration": "明确的到期日期（含年份）",
    "strike": "行权价（精确值与上下限不能同时使用）", "min_strike": "行权价上下限",
    "side": "空头方向", "contracts": "合约张数",
    "opening_premium": "开仓权利金的金额、单位及币种",
    "amount": "开仓权利金金额", "unit": "权利金单位（每股、每张或总额）",
    "currency": "权利金币种", "search_bounds": "最晚新到期日",
    "max_new_expiration": "最晚新到期日（明确日期）",
    "min_new_strike": "新行权价下限（须低于原行权价）",
    "max_new_strike": "新行权价上限（须高于原行权价）",
    "dte": "到期期限范围", "abs_delta_range": "Delta 范围",
    "limit": "展示数量", "opened_at": "明确的开仓日期",
}


def _input_question(fields: tuple[str, ...]) -> str:
    labels = list(dict.fromkeys(_FIELD_LABELS.get(field.split(".")[-1], "该项输入")
                               for field in fields))
    return ("请确认" + "、".join(labels) + "。") if labels else "你想查行情、找卖出期权候选，还是评估平仓或移仓？"


def _new_task(text: str) -> bool:
    # Explicit user boundaries only; ordinary single-field corrections inherit.
    return (_cancelled(text) or re.match(
        r"^(?:新任务|独立(?:的)?新任务|换个问题|改查|重新(?:确认全部|完整重述))", text) is not None)


def _cancelled(text: str) -> bool:
    return re.fullmatch(r"(?:取消(?:查询|当前查询|任务)?|先不查了|不查了)[。！!\s]*", text) is not None


def _context_expiry(history: list[dict], pending: list[dict], now: datetime) -> datetime | None:
    expiries = []
    for item in [*[item for turn in history for item in turn["messages"]], *pending]:
        if not item.get("source_id"):
            return None
        for key in ("source_expires_at_utc", "expires_at_utc"):
            try:
                expiry = datetime.fromisoformat(item[key])
                if expiry.tzinfo is None or expiry <= now:
                    return None
            except (KeyError, ValueError, TypeError):
                return None
            expiries.append(expiry)
    return min(expiries) if expiries else now + timedelta(hours=24)


def _matching_history(history: list[dict], text: str) -> tuple[list[dict], bool]:
    questions = [item for turn in history for item in turn["messages"]
                 if item["role"] == "assistant" and (
                     item.get("clarification") or not item.get("acknowledged")
                     or re.search(r"[？?]|^[1-3]\. ", item["content"], re.MULTILINE))]
    number = re.fullmatch(r"(?:我)?(?:选|选择|选项)?\s*(?:第)?(?P<number>\d+|一|二|三)"
                          r"(?:个选项|个|号|项)?[.。、！!]?", text)
    choice = number is not None
    # Exact option text also refers to a question, even without a number.
    choice = choice or any(text == line.split(". ", 1)[1]
                           for item in questions for line in item["content"].splitlines()
                           if re.match(r"^[1-3]\. ", line))
    ambiguous = choice and (len(questions) != 1 or not questions[0].get("acknowledged"))
    if number and not ambiguous:
        options = re.findall(r"^[1-3]\. ", questions[0]["content"], re.MULTILINE)
        allowed = {str(i) for i in range(1, len(options) + 1)} | set("一二三"[:len(options)])
        if options and number["number"] not in allowed:
            ambiguous = True
    safe = [{**turn, "messages": [item for item in turn["messages"]
                                  if item["role"] != "assistant"
                                  or item.get("acknowledged")]}
            for turn in history]
    return safe, ambiguous


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

    def _messages(self, history: list[dict], pending: list[dict], text: str,
                  now: datetime) -> list[dict]:
        time_context = ["本轮时间上下文（服务端时钟，以下均为同一时刻）："]
        for label, zone in (("服务端 UTC", "UTC"),
                            ("当前时间", self.settings.user_timezone),
                            ("美东", "America/New_York"),
                            ("香港", "Asia/Hong_Kong")):
            local = now.astimezone(ZoneInfo(zone))
            offset = local.strftime("%z")
            time_context.append(
                f"{label}：{local:%Y-%m-%d %H:%M:%S} "
                f"({zone}, {local.tzname()}, UTC{offset[:3]}:{offset[3:]})")
        system = SYSTEM_PROMPT + "\n" + "\n".join(time_context)
        current_query = _last_candidate_query(history)
        if current_query is not None:
            system += ("\n最近一次候选查询的用户参数（只供本轮延续或修改，"
                       "不是本次查询结果；未指定的条件仍用公共策略默认）：\n"
                       + _canonical(current_query))
        if pending:
            system += "\n待补参数（仅当用户本轮明确补充时才使用）：\n" + _canonical([
                {"operation": item["operation"], "parameters": item["parameters"]}
                for item in pending])
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

    def _rebuild_current_request(self, *, text: str, event: VerifiedGroupEvent,
                                 now: datetime, deadline: datetime,
                                 budget: RunBudget, call_settings, expected) -> None:
        """Extract independently; no contextual request, facts or pending enter this call."""
        messages = self._messages([], [], text, now)
        messages[0]["content"] += (
            "\n本次仅做当前原文的独立来源核验，不执行业务查询。仅依据下方唯一用户消息"
            "提取一个完整业务工具请求；禁止使用其他对话、猜测必要参数或把示例当用户事实。"
            "若当前消息仅补充一个字段、依赖上文，或缺少必要事实，请调用ask_user_question，"
            "不要补齐事实。一次只能提交一个工具调用。")
        try:
            budget.begin_model_call()
            budget.reserve_tokens(self._reserve(messages))
            response = self._request(
                settings=call_settings, api_key=self._api_key, messages=messages,
                tools=MODEL_TOOLS,
                timeout=max(0.001, min(float(call_settings.timeout_seconds),
                                       budget.remaining_seconds())))
            message = response.get("message") or {}
            tokens = (response.get("usage") or {}).get("completion_tokens")
            if not isinstance(tokens, int) or isinstance(tokens, bool):
                tokens = self._estimate(message)
            budget.record_output_tokens(max(0, tokens), limit=call_settings.max_output_tokens)
            if response.get("finish_reason") == "length":
                budget.stop("output_token_limit")
            budget.reserve_tokens(0)  # Check deadline and output stop after transport.
            calls = message.get("tool_calls") or []
            if len(calls) != 1:
                raise SourceConfirmationRequired("source_rebuild_incomplete")
            name, arguments, _ = _business_arguments(calls[0])
            rebuilt = normalize_request(name, _business_body(
                name, arguments, event, deadline, now, request_id="source-rebuild"),
                now=self._now())
            if rebuilt.method != expected.method or rebuilt.parameters != expected.parameters:
                raise SourceConfirmationRequired("source_rebuild_mismatch")
        except BudgetExhausted as exc:
            raise SourceConfirmationRequired(exc.reason) from exc
        except SourceConfirmationRequired:
            raise
        except Exception as exc:
            raise SourceConfirmationRequired("source_rebuild_failed") from exc

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
        for index in range(len(history) - 1, -1, -1):
            turn = history[index]
            if (_new_task(turn["messages"][0]["content"])
                    or turn.get("current_message_source")):
                history = history[index:]
                live_sources = {item.get("source_id") for turn in history
                                for item in turn["messages"]}
                pending = [item for item in pending if item.get("source_id") in live_sources]
                break
        if _new_task(text):
            self.conversations.discard_pending_inputs(scope)
            history, pending = [], []
        if _cancelled(text):
            body = "已取消当前查询。"
            self._store_turn(scope=scope, turn_id=turn_id, user_text=text,
                             results=[], reply=body, now=now, source_id=source_id,
                             source_expiry=source_expiry, reply_expiry=source_expiry)
            self.conversations.record_audit(scope_sha256=scope, turn_id=turn_id,
                                            status="cancelled", reason_codes=(), now=now)
            return AgentReply(body, "cancelled", (), source_expiry)
        history, ambiguous_answer = _matching_history(history, text)
        inherited_expiry = _context_expiry(history, pending, now)
        if inherited_expiry is None or ambiguous_answer:
            body = finalize_clarification("请重新确认要查询的目标和必要条件；目前无法唯一确认你回答的是哪个问题。")
            self._store_turn(scope=scope, turn_id=turn_id, user_text=text,
                             results=[], reply=body, now=now, source_id=source_id,
                             source_expiry=source_expiry, reply_expiry=source_expiry)
            self.conversations.record_audit(
                scope_sha256=scope, turn_id=turn_id, status="clarification",
                reason_codes=("clarification_required",), now=now)
            return AgentReply(body, "clarification", ("clarification_required",), source_expiry)
        fresh_task = not history and not pending
        context_source_ids = frozenset(item["source_id"] for item in [
            *[item for turn in history for item in turn["messages"]], *pending])
        inherited_expiry = min(source_expiry, inherited_expiry)
        messages = self._messages(history, pending, text, now)
        stored_results: list[dict] = []
        last_result: tuple[str, dict[str, Any]] | None = None
        last_card: str | None = None
        contract_error: str | None = None
        reply_expiry = inherited_expiry
        input_error_fields: tuple[str, ...] | None = None
        service_error: str | None = None
        asked = False
        explanation = ""
        general_answer = False
        status = "ok"
        reasons: list[str] = []
        stop = False
        guard_retry_used = False
        fallback_used = False
        source_check_used = False
        source_confirmation_failed = False
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
            names = [str((call.get("function") or {}).get("name") or "") for call in calls]
            if QUESTION_TOOL["name"] in names and len(calls) != 1:
                # Preflight the complete batch before any dispatch, regardless of order.
                for call in calls:
                    messages.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                                     "content": _canonical({"ok": False, "error": "tool_batch_conflict",
                                                            "message": "提问必须单独调用；本批未执行任何业务查询。"})})
                input_error_fields = ()
                continue
            try:
                if not fresh_task:
                    for call in calls:
                        name = str((call.get("function") or {}).get("name") or "")
                        if name not in TOOL_NAMES:
                            continue
                        try:
                            name, arguments, source_scope = _business_arguments(call)
                            expected = normalize_request(name, _business_body(
                                name, arguments, event, turn_deadline_utc, now,
                                request_id="source-candidate"), now=self._now())
                        except (ValueError, TypeError):
                            # Ordinary parameter errors retain the canonical
                            # validator's correction/question path below.
                            continue
                        if source_scope == "conversation":
                            continue
                        if source_check_used or len(calls) != 1 or stored_results:
                            raise SourceConfirmationRequired("source_rebuild_ambiguous")
                        source_check_used = True
                        self._rebuild_current_request(
                            text=text, event=event, now=now, deadline=turn_deadline_utc,
                            budget=budget, call_settings=call_settings, expected=expected)
                        # The executed request is independently reproducible
                        # from the new user source. Later narration receives
                        # only this source and the new API facts as well.
                        self.conversations.discard_pending_inputs(scope)
                        reasons.append("current_message_source")
                        inherited_expiry = reply_expiry = source_expiry
                        fresh_task, context_source_ids = True, frozenset()
                        messages = self._messages([], [], text, now)
                        explanation = ""
                        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            except SourceConfirmationRequired as exc:
                explanation = "本次无法独立确认完整输入，请重新确认查询目标及全部必要条件。"
                status, asked = "clarification", True
                source_confirmation_failed = True
                reasons.extend(("clarification_required", str(exc)))
                break
            for call in calls:
                try:
                    payload, result = self._dispatch(
                        call, event=event, scope=scope, budget=budget, now=now,
                        deadline=turn_deadline_utc,
                        source_id=source_id, source_expiry=inherited_expiry,
                        fresh_task=fresh_task, context_source_ids=context_source_ids)
                except BudgetExhausted as exc:
                    status = "limited"
                    reasons.append(exc.reason)
                    stop = True
                    break
                except SourceConfirmationRequired as exc:
                    explanation = "请重新确认查询目标及全部必要条件。"
                    status, asked, stop = "clarification", True, True
                    source_confirmation_failed = True
                    reasons.extend(("clarification_required", str(exc)))
                    break
                if result is not None:
                    if isinstance(result, UserQuestion):
                        if service_error:
                            stop = True
                            break
                        explanation = result.text
                        asked = True
                        status = "clarification"
                        reasons.append("clarification_required")
                        stop = True
                        break
                    if isinstance(result, GeneralAnswer):
                        explanation = result.answer
                        general_answer = True
                        reasons.append("general_answer")
                        stop = True
                        break
                    last_result = (result[0], result[1])
                    input_error_fields = None
                    service_error = None
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
                elif payload.get("error") == "INPUT_ERROR":
                    input_error_fields = tuple(payload.get("fields") or ())
                elif payload.get("error") not in {None, "unsupported_tool"}:
                    service_error = str(payload.get("error"))
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
        if source_confirmation_failed:
            if service_error:
                body = f"{render_query_failure(service_error)}\n\n{prose}"
                status = "query_failed"
                reasons.append(service_error)
            else:
                body = (f"{rendered}\n\n{prose}" if rendered else finalize_clarification(prose))
        elif status == "model_unavailable":
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
            if service_error and not asked:
                body = render_query_failure(service_error)
                status = "query_failed"
                reasons.append(service_error)
            elif input_error_fields is not None and not asked:
                body = finalize_clarification(_input_question(input_error_fields))
                status = "clarification"
                reasons.append("clarification_required")
            elif prose:
                body = prose if general_answer else finalize_clarification(prose)
                if not general_answer:
                    status = "clarification"
                    if "clarification_required" not in reasons:
                        reasons.append("clarification_required")
            else:
                body = finalize_clarification(
                    _input_question(()))
                status = "clarification"
                reasons.append("clarification_required")
        if inherited_expiry <= self._now():
            # A source may expire while the model/provider is running. Do not
            # deliver its facts even if they were live when history was read.
            body = finalize_clarification("此前输入已到期，请重新确认查询目标及完整条件。")
            status = "clarification"
            reasons = ["clarification_required", "source_expired"]
            stored_results = []
            reply_expiry = source_expiry
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
                  source_id: str, source_expiry: datetime, fresh_task: bool = False,
                  context_source_ids: frozenset[str] = frozenset()):
        name = str((call.get("function") or {}).get("name") or "")
        if source_expiry <= self._now():
            return ({"ok": False, "error": "INPUT_ERROR", "fields": [],
                     "message": "上下文来源已到期，请重新确认查询条件。"}, None)
        if name == QUESTION_TOOL["name"]:
            try:
                arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                arguments = None
            question = _question(arguments)
            return ({"ok": question is not None, "error": None if question else "INPUT_ERROR",
                     "message": "问题须为1–300字，选项为0–3个不同的1–60字字符串。"}, question)
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
            name, arguments, _ = _business_arguments(call)
        except (TypeError, ValueError):
            return ({"ok": False, "error": "INPUT_ERROR",
                     "message": "tool arguments must be a JSON object"}, None)
        request_id = uuid.uuid4().hex
        body = _business_body(name, arguments, event, deadline, now, request_id=request_id)
        input_id = None
        input_as_of = None
        input_expiry = source_expiry
        try:
            if name in SCENARIO_METHODS:
                provisional = {**body, "input_id": "provisional",
                               "input_as_of_utc": now.isoformat()}
                normalized = normalize_request(name, provisional, now=now)
                digest = _input_digest(name, normalized.parameters)
                existing = None if fresh_task else self.conversations.resolve_confirmed_input(
                    scope_sha256=scope, operation=name,
                    input_sha256=digest, now=now, source_ids=context_source_ids)
                input_id = existing or uuid.uuid4().hex
                expiry_text = self.conversations.save_confirmed_input(
                    scope_sha256=scope, input_id=input_id, input_sha256=digest,
                    operation=name, parameters=normalized.parameters, now=now,
                    source_id=source_id, source_expiry=source_expiry,
                    inherit_prior=False)
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
                    now=now, source_id=source_id, source_expiry=source_expiry,
                    inherit_prior=False)
            return ({"ok": False, "error": exc.code, "message": exc.message,
                     "fields": list(exc.fields), "category": exc.category}, None)
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
            if source_expiry <= self._now():
                raise RuntimeError("reply_source_expired")
            self.deliveries.mark_sending(row["message_key"])
            started = True

        try:
            response = self.reply_fn(
                message_id=event.message_id, text=reply.text,
                uuid=row["delivery_uuid"], reply_in_thread=bool(event.thread_id),
                before_send=before_send)
            state = _classify_delivery_outcome(started, response=response)
        except Exception as exc:
            state = _classify_delivery_outcome(started, exc=exc)
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
