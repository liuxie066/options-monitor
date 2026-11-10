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
from .render import (
    compose_reply,
    render_admission_rejected,
    render_budget_exhausted,
    render_result,
)
from .store import CONVERSATION_DB_NAME, ConversationStore, DeliveryStore, derived_expiry

LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「群查询bot」，只服务已授权的个人飞书群。
你可以使用且只能使用五个工具：query_quotes、query_candidates、evaluate_close、
evaluate_roll_down、evaluate_roll_up。你不具备也不得声称具备任何账户、持仓、
资金、收益、成交或报告访问能力；不要提及或请求账户信息。

规则：
1. 行情、候选、平仓和移仓结论必须来自工具返回的实时数据。工具未返回数据时，
   只能澄清问题或说明限制，不得凭记忆给出价格、费用、年化或建议。
2. 必要参数缺失或存在歧义（标的、市场、合约、张数、开仓权利金、最晚新到期日、
   行权价边界等）时，先用简短中文追问；不要猜测。
3. 用户给出的相对日期要保留原话并请用户确认明确日期；最晚新到期日必须由用户明确给出。
4. 工具返回的结构化数值由系统渲染，你不重复、不改写、不重算关键数字；你可以解释
   这些结果的含义和局限。
5. 一次回复保持简短；不要输出 Markdown 表格。
6. 所有工具调用的标的必须带市场前缀：美股用 US.XXX（如 US.NVDA），港股用
   HK.XXXXX（如 HK.00700）。不得传裸代码（如 NVDA）；不确定市场时先追问。
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "query_quotes",
        "description": "查询标的与可选单张期权合约的实时公开行情；不涉及任何账户。",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "必须带市场前缀，如 US.NVDA 或 HK.00700"},
                "contract": {
                    "type": "object",
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
                "symbol": {"type": "string", "description": "必须带市场前缀，如 US.NVDA 或 HK.00700"},
                "strategy": {"type": "string", "enum": ["short_put", "short_call"]},
                "dte": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "strike": {"type": "number"},
                "abs_delta_range": {"type": "array", "items": {"type": "number"},
                                    "minItems": 2, "maxItems": 2},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
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
                        "symbol": {"type": "string", "description": "必须带市场前缀，如 US.NVDA 或 HK.00700"},
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
                "opened_at": {"type": "string", "description": "YYYY-MM-DD"},
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
                        "symbol": {"type": "string", "description": "必须带市场前缀，如 US.NVDA 或 HK.00700"},
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
                        "min_new_strike": {"type": "number"},
                    },
                    "required": ["max_new_expiration"],
                    "additionalProperties": False,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
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
                        "symbol": {"type": "string", "description": "必须带市场前缀，如 US.NVDA 或 HK.00700"},
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
                        "max_new_strike": {"type": "number"},
                    },
                    "required": ["max_new_expiration"],
                    "additionalProperties": False,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["position", "search_bounds"],
            "additionalProperties": False,
        },
    },
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


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
        return {"method": method, "quote_status": data.get("quote_status"),
                "underlier": data.get("underlier"), "option": data.get("option"),
                "missing_requirements": data.get("missing_requirements"),
                "effective_parameters": data.get("effective_parameters")}
    if method == "query_candidates":
        return {"method": method, "status": data.get("status"),
                "outcome": data.get("outcome"),
                "candidates": list(data.get("candidates") or [])[:10],
                "displayed": data.get("displayed"), "omitted": data.get("omitted"),
                "reason_codes": data.get("reason_codes")}
    if method == "evaluate_close":
        return {"method": method,
                "recommendation_state": data.get("recommendation_state"),
                "evaluation_status": data.get("evaluation_status"),
                "quote": data.get("quote"), "metrics": data.get("metrics"),
                "fee_model": data.get("fee_model"), "reason": data.get("reason"),
                "reason_codes": data.get("reason_codes"),
                "evaluated_at_utc": data.get("evaluated_at_utc")}
    return {"method": method, "status": data.get("status"),
            "outcome": data.get("outcome"), "old_quote": data.get("old_quote"),
            "candidates": list(data.get("candidates") or [])[:10],
            "displayed": data.get("displayed"), "omitted": data.get("omitted"),
            "reason_codes": data.get("reason_codes"),
            "scope": data.get("scope"),
            "evaluated_at_utc": data.get("evaluated_at_utc")}


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
        if pending:
            system += "\n待补参数（仅当用户本轮明确补充时才使用）：\n" + _canonical(pending)
        messages: list[dict] = [{"role": "system", "content": system}]
        for turn in history:
            for item in turn["messages"]:
                if item["role"] == "tool_result":
                    messages.append({"role": "assistant",
                                     "content": "[历史查询结果] " + item["content"]})
                else:
                    messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": text})
        return messages

    def _reserve(self, messages: list[dict]) -> int:
        return self._estimate({"messages": messages, "tools": TOOLS}) + self.settings.output_tokens

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
        reply_expiry = source_expiry
        explanation = ""
        status = "ok"
        reasons: list[str] = []
        stop = False
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
                    messages=messages, tools=TOOLS, timeout=timeout)
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
                    last_result = (result[0], result[1])
                    reply_expiry = min(reply_expiry, result[2])
                    stored_results.append({"method": result[0],
                                           "summary": _tool_summary(result[0], result[1])})
                messages.append({"role": "tool",
                                 "tool_call_id": str(call.get("id") or ""),
                                 "content": _canonical(payload)})
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
        rendered = render_result(*last_result) if last_result is not None else None
        if status == "model_unavailable":
            body = "自然语言服务当前不可用，请稍后重试。"
        elif status == "limited":
            note = render_budget_exhausted(reasons[0] if reasons else "limit")
            body = compose_reply(note, rendered)
        elif rendered is not None:
            body = compose_reply(explanation, rendered)
        else:
            prose = explanation.strip()
            if prose:
                body = prose + "\n\n（未执行实时查询，以上为澄清或说明，不含实时数据。）"
            else:
                body = "请补充标的、合约、张数和开仓权利金等信息后再查询。"
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
            self.conversations.append_message(
                scope_sha256=scope, turn_id=turn_id, seq=seq, role="tool_result",
                content=_canonical(result["summary"]), now=now,
                source_id=source_id, source_expiry=derived_expiry(now, reply_expiry))
            seq += 1
        self.conversations.append_message(
            scope_sha256=scope, turn_id=turn_id, seq=seq, role="assistant",
            content=reply, now=now, source_id=source_id,
            source_expiry=derived_expiry(now, reply_expiry))

    def _dispatch(self, call: dict[str, Any], *, event: VerifiedGroupEvent,
                  scope: str, budget: RunBudget, now: datetime,
                  deadline: datetime,
                  source_id: str, source_expiry: datetime):
        name = str((call.get("function") or {}).get("name") or "")
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
        return payload, (name, data, input_expiry)


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
                                        self.settings.allowed_chat_ids)
            user_text = text if text is not None else message_text(payload)
        except EventRejected as exc:
            return {"status": "rejected", "reason": str(exc)}
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
    "GroupQueryAgent",
    "SYSTEM_PROMPT",
    "TOOLS",
    "TOOL_NAMES",
]
