"""One Python model/tool loop. No evidence registry or answer submission protocol."""
from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import Counter
from typing import Any, Callable

from src.infrastructure.openai_chat_completions import create_chat_completion
from src.infrastructure.openai_responses import create_response


class RunStopped(Exception):
    def __init__(self, code: str, reason: str):
        self.code, self.reason = code, reason
        super().__init__(reason)


def check_run(deadline: float, cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise RunStopped("CANCELLED", "cancelled")
    if time.monotonic() >= deadline:
        raise RunStopped("BUDGET_EXHAUSTED", "time_deadline")


def bounded_call(fn: Callable, *, deadline: float, cancelled: Callable[[], bool], **kwargs):
    """Discard late read/model results; workers cannot persist a reply or invoke tools."""
    check_run(deadline, cancelled)
    result: queue.Queue = queue.Queue(maxsize=1)

    def work():
        try:
            result.put((True, fn(**kwargs)))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(target=work, daemon=True, name="bot-read").start()
    while True:
        check_run(deadline, cancelled)
        try:
            ok, value = result.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
        except queue.Empty:
            continue
        check_run(deadline, cancelled)
        if not ok:
            raise value
        return value


def token_estimate(value: Any) -> int:
    # Conservative UTF-8 bound; no provider usage from a different input is reused.
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) // 2 + 1


def compact_messages(messages: list[dict], capacity: int, tools: list[dict]) -> list[dict]:
    """Compress older complete groups, including this turn, preserving tool pairings."""
    if token_estimate([messages, tools]) <= capacity:
        return messages
    prefix, rest = [], list(messages)
    while rest and rest[0]["role"] == "system":
        item = rest.pop(0)
        if not item.get("content", "").startswith("Untrusted partial history excerpts."):
            prefix.append(item)
    groups: list[list[dict]] = []
    for message in rest:
        if message["role"] != "tool" or not groups:
            groups.append([])
        groups[-1].append(message)
    # Preserve the latest user request and the latest tool group, not an old first question.
    current_user = next((g for g in reversed(groups) if g[0]["role"] == "user"), None)
    latest_tool = next((g for g in reversed(groups) if g[0].get("tool_calls")), None)
    removed: list[dict] = []
    for group in list(groups):
        if group is current_user or group is latest_tool or group is groups[-1]:
            continue
        groups.remove(group)
        removed.extend(group)
        snippets = [{"role": m["role"], "excerpt": str(m.get("content") or "")[:400], "partial": True} for m in removed[-4:]]
        summary = {"role": "system", "content": "Untrusted partial history excerpts. Exact facts require rereading. " + json.dumps(snippets, ensure_ascii=False)}
        candidate = prefix + [summary] + [m for g in groups for m in g]
        if token_estimate([candidate, tools]) <= capacity:
            return candidate
    raise RunStopped("BUDGET_EXHAUSTED", "context_capacity")


def _reported_input_tokens(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    for name in ("prompt_tokens", "input_tokens"):
        value = usage.get(name)
        if type(value) is int and value > 0:
            return value
    return None


def request_model(*, settings, api_key: str, messages: list[dict], tools: list[dict], timeout: float, require_tool: bool = False) -> dict:
    common = dict(api_key=api_key, base_url=settings.base_url, model=settings.model,
                  timeout=timeout, max_output_tokens=settings.max_output_tokens, temperature=None,
                  tool_choice="required" if require_tool else "auto")
    if settings.api_kind == "openai-completions":
        schemas = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                    "parameters": t["input_schema"]}} for t in tools]
        raw = create_chat_completion(**common, messages=messages, tools=schemas,
                                     thinking={"type": "disabled"} if settings.provider == "deepseek" else None)
        choices = raw.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise RunStopped("MODEL_ERROR", "invalid_response")
        return {"message": choices[0]["message"], "finish_reason": choices[0].get("finish_reason"), "usage": raw.get("usage", {})}
    items = []
    for message in messages:
        if message["role"] == "system":
            continue
        if message["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
        elif message.get("tool_calls"):
            for call in message["tool_calls"]:
                items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
        else:
            items.append({"role": message["role"], "content": message.get("content") or ""})
    schemas = [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["input_schema"]} for t in tools]
    raw = create_response(**common, input_items=items, instructions="\n\n".join(m["content"] for m in messages if m["role"] == "system"), tools=schemas)
    calls, texts = [], []
    for item in raw.get("output", []):
        if item.get("type") == "function_call":
            calls.append({"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}})
        elif item.get("type") == "message":
            texts.extend(c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text")
    if raw.get("status") == "incomplete" and (raw.get("incomplete_details") or {}).get("reason") != "max_output_tokens":
        raise RunStopped("MODEL_ERROR", "incomplete_response")
    reason = "length" if raw.get("status") == "incomplete" else "tool_calls" if calls else "stop"
    if raw.get("status") not in {"completed", "incomplete"}:
        raise RunStopped("MODEL_ERROR", "invalid_response")
    return {"message": {"role": "assistant", "content": "".join(texts), "tool_calls": calls}, "finish_reason": reason, "usage": raw.get("usage", {})}


def run_agent(*, settings, api_key: str, messages: list[dict], tools: list[dict], call_tool: Callable,
              deadline: float, cancelled: Callable[[], bool], event: Callable,
              model_limit: int = 16, tool_limit: int = 12, reserve_seconds: float = 45,
              request: Callable = request_model) -> str:
    names = {tool["name"] for tool in tools}
    calls_seen: set[str] = set()
    repeated: Counter = Counter()
    tool_count = failures = model_failures = 0
    final = False
    continued = False
    parts: list[str] = []
    capacity = int((settings.context_window_tokens - settings.output_reservation_tokens) * 0.9)
    estimate_scale: float | None = None
    require_initial_tool = not any(message.get("role") == "assistant" for message in messages)
    for turn in range(model_limit):
        check_run(deadline, cancelled)
        final = final or turn == model_limit - 1 or tool_count >= tool_limit or failures >= 3 or deadline - time.monotonic() <= reserve_seconds
        # Providers report tokens for the exact previous request. Calibrate the
        # conservative local estimate before deciding that current evidence no
        # longer fits; without usage, fall back to the safe early answer.
        if estimate_scale is None and tool_count and token_estimate([messages, tools]) > capacity:
            final = True
        active_tools = [] if final else tools
        current = list(messages)
        if final:
            current.append({"role": "system", "content": "Stop investigating. Answer the user's question from the available results. State missing evidence plainly. No tools are available."})
        original = current
        local_capacity = capacity if estimate_scale is None else max(1, math.floor(capacity / (estimate_scale * 1.10)))
        current = compact_messages(current, local_capacity, active_tools)
        if current != original:
            event("context_compacted", {"estimated_tokens": token_estimate([current, active_tools]),
                                        "provider_calibrated": estimate_scale is not None})
        messages = current
        event("model_turn_started", {"turn": turn + 1})
        try:
            reply = bounded_call(request, deadline=deadline, cancelled=cancelled, settings=settings, api_key=api_key,
                                 messages=current, tools=active_tools,
                                 require_tool=bool(active_tools) and turn == 0 and require_initial_tool,
                                 timeout=min(settings.timeout_seconds, deadline - time.monotonic()))
        except RunStopped:
            raise
        except Exception:
            model_failures += 1
            event("model_turn_failed", {"turn": turn + 1, "reason": "provider_error"})
            if not final and (tool_count or model_failures < settings.max_attempts):
                final = bool(tool_count)
                continue
            raise RunStopped("MODEL_ERROR", "provider_error")
        model_failures = 0
        reported_input = _reported_input_tokens(reply.get("usage"))
        local_input = token_estimate([current, active_tools])
        if reported_input is not None and local_input > 0:
            estimate_scale = max(0.25, min(2.0, reported_input / local_input))
        event("model_turn_completed", {"usage": reply.get("usage", {}), "turn": turn + 1, "stop_reason": reply.get("finish_reason")})
        message = reply.get("message")
        if not isinstance(message, dict):
            raise RunStopped("MODEL_ERROR", "invalid_response")
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise RunStopped("MODEL_ERROR", "invalid_content")
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            raise RunStopped("MODEL_ERROR", "invalid_tool_calls")
        if calls:
            if final:
                raise RunStopped("MODEL_ERROR", "tools_during_final_answer")
            clean_calls = []
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise RunStopped("MODEL_ERROR", "invalid_tool_call")
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in calls_seen:
                    raise RunStopped("MODEL_ERROR", "duplicate_tool_id")
                calls_seen.add(call_id)
                clean_calls.append({"id": call_id, "type": "function", "function": call["function"]})
            messages.append({"role": "assistant", "content": content, "tool_calls": clean_calls})
            for call in clean_calls:
                check_run(deadline, cancelled)
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                    if not isinstance(args, dict):
                        raise ValueError()
                except (TypeError, ValueError):
                    args = None
                key = json.dumps([fn.get("name"), args], sort_keys=True, ensure_ascii=False)
                repeated[key] += 1
                if fn.get("name") not in names or args is None:
                    result = {"ok": False, "error": {"code": "INPUT_ERROR", "message": "Use an available tool and a JSON object matching its schema."}}
                elif tool_count >= tool_limit or repeated[key] > 2:
                    final = True
                    result = {"ok": False, "error": {"code": "BUDGET_EXHAUSTED", "message": "Stop reading; answer using existing results and state gaps."}}
                else:
                    tool_count += 1
                    result = call_tool(fn["name"], args)
                failures = 0 if result.get("ok") else failures + 1
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False, allow_nan=False)})
            continue
        if reply.get("finish_reason") == "length":
            if continued or turn + 1 >= model_limit:
                raise RunStopped("BUDGET_EXHAUSTED", "output_length")
            continued, final = True, True
            parts.append(content)
            messages += [{"role": "assistant", "content": content}, {"role": "user", "content": "Continue exactly where the answer was cut off; do not repeat earlier text."}]
            continue
        if reply.get("finish_reason") != "stop" or not content.strip():
            raise RunStopped("MODEL_ERROR", "empty_or_incomplete_answer")
        return "".join(parts + [content]).strip()
    raise RunStopped("BUDGET_EXHAUSTED", "model_turn_limit")
