from __future__ import annotations

import json
import hashlib
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_DEEPSEEK_RESPONSES_URL = "https://api.deepseek.com/responses"
DEEPSEEK_WEB_SEARCH_TOOL = {"type": "web_search"}

HttpPostJsonFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class DeepSeekResponsesError(Exception):
    message: str
    http_status: int | None = None
    response_sha256: str | None = None

    def __str__(self) -> str:
        return self.message


def create_deepseek_response(
    *,
    api_key: str,
    model: str,
    input_items: list[dict[str, Any]],
    instructions: str,
    base_url: str | None = None,
    enable_web_search: bool = False,
    json_schema: dict[str, Any] | None = None,
    timeout: int = 60,
    max_output_tokens: int = 4096,
    http_post_json_fn: HttpPostJsonFn | None = None,
) -> dict[str, Any]:
    """Call the DeepSeek Responses API with optional native web_search.

    Returns the response payload to the immediate caller. Callers must extract
    only the validated output and the minimized audit fields below; provider
    response bodies are never an audit or persistence contract.
    """

    api_key_value = str(api_key or "").strip()
    model_value = str(model or "").strip()
    if not api_key_value:
        raise ValueError("api_key is required")
    if not model_value:
        raise ValueError("model is required")
    payload: dict[str, Any] = {
        "model": model_value,
        "instructions": str(instructions or "").strip(),
        "input": [dict(item) for item in input_items],
        "max_output_tokens": int(max_output_tokens),
        "store": False,
        "temperature": 0.0,
    }
    if enable_web_search:
        payload["tools"] = [dict(DEEPSEEK_WEB_SEARCH_TOOL)]
    if json_schema is not None:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": str(json_schema.get("name") or "structured_output"),
                "schema": dict(json_schema.get("schema") or {}),
                "strict": True,
            }
        }
    response = (http_post_json_fn or _post_json)(
        resolve_deepseek_responses_url(base_url),
        payload,
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
        },
        timeout=int(timeout),
    )
    if str(response.get("status") or "").strip().lower() != "completed":
        raise DeepSeekResponsesError(
            "DeepSeek response did not complete",
            response_sha256=response_fingerprint(response),
        )
    return response


def resolve_deepseek_responses_url(base_url: str | None) -> str:
    value = str(base_url or "").strip()
    if not value:
        return DEFAULT_DEEPSEEK_RESPONSES_URL
    normalized = value.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"


def extract_output_text(response: dict[str, Any]) -> str:
    """Concatenate output_text parts from a Responses payload."""

    parts: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "".join(parts)


def extract_usage(response: dict[str, Any]) -> dict[str, Any]:
    """Return an allowlisted, numeric-only usage summary."""

    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result


def response_fingerprint(response: dict[str, Any]) -> str:
    """Fingerprint a provider response without retaining its contents."""

    encoded = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def summarize_web_search_calls(response: dict[str, Any]) -> dict[str, Any]:
    """Return aggregate web-search telemetry without provider IDs or queries."""

    count = 0
    status_counts: dict[str, int] = {}
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            count += 1
            raw_status = str(item.get("status") or "").strip().lower()
            status = raw_status if raw_status in {"completed", "failed", "in_progress"} else "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "count": count,
        "status_counts": dict(sorted(status_counts.items())),
    }


def audit_web_search_calls_by_symbol(
    response: dict[str, Any],
    *,
    identity_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attribute native search calls to one frozen symbol identity.

    Provider query text is inspected in memory but never returned. Search
    actions must be attributable from their query text. Completed auxiliary
    actions (open_page/find_in_page) are counted but do not need query-based
    attribution; any non-completed provider action still fails coverage at the
    collector boundary.
    """

    symbols = [str(row.get("symbol") or "") for row in identity_rows]
    by_symbol = {
        symbol: {
            "completed": 0,
            "failed": 0,
            "in_progress": 0,
            "unknown": 0,
        }
        for symbol in symbols
        if symbol
    }
    call_count = 0
    unattributed_count = 0
    auxiliary_count = 0
    status_counts = {
        "completed": 0,
        "failed": 0,
        "in_progress": 0,
        "unknown": 0,
    }
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        call_count += 1
        status = _web_search_status(item)
        status_counts[status] += 1
        action = item.get("action")
        action_type = (
            str(action.get("type") or "").strip().lower()
            if isinstance(action, dict)
            else ""
        )
        if action_type in {"open_page", "find_in_page"}:
            auxiliary_count += 1
            continue
        if action_type != "search":
            unattributed_count += 1
            continue
        queries = _web_search_queries(item)
        matched, attributable = _symbols_for_queries(
            queries,
            identity_rows=identity_rows,
        )
        if not attributable:
            unattributed_count += 1
            continue
        for symbol in matched:
            by_symbol[symbol][status] += 1
    return {
        "count": call_count,
        "unattributed_count": unattributed_count,
        "auxiliary_count": auxiliary_count,
        "status_counts": status_counts,
        "symbols": by_symbol,
    }


def extract_native_url_citations(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract allowlisted native URL citations from output annotations."""

    citations: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            for annotation in content.get("annotations") or []:
                if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                    continue
                nested = annotation.get("url_citation")
                source = nested if isinstance(nested, dict) else annotation
                url = str(source.get("url") or "").strip()
                if not url:
                    continue
                row: dict[str, Any] = {"url": url}
                for key in ("title", "publisher"):
                    value = str(source.get(key) or "").strip()
                    if value:
                        row[key] = value
                citations.append(row)
    return citations


def extract_native_web_search_sources(
    response: dict[str, Any],
    *,
    identity_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Bind provider-native search sources to one frozen symbol identity.

    ``action.sources`` belongs to the provider search action, unlike a URL
    repeated by the model in structured output. A multi-symbol search can
    prove coverage for each symbol, but its shared source list is deliberately
    not assigned to either symbol because that would permit cross-symbol
    citation binding.
    """

    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        if not isinstance(action, dict) or action.get("type") != "search":
            continue
        matched, attributable = _symbols_for_queries(
            _web_search_queries(item),
            identity_rows=identity_rows,
        )
        if not attributable or len(matched) != 1:
            continue
        symbol = next(iter(matched))
        for source in action.get("sources") or []:
            if not isinstance(source, dict) or source.get("type") != "url":
                continue
            url = str(source.get("url") or "").strip()
            key = (symbol, url)
            if not url or key in seen:
                continue
            seen.add(key)
            rows.append({"symbol": symbol, "url": url})
    return rows


def _web_search_queries(item: dict[str, Any]) -> list[str]:
    action = item.get("action")
    source = action if isinstance(action, dict) else item
    queries: list[str] = []
    raw_query = source.get("query")
    if isinstance(raw_query, str) and raw_query.strip():
        queries.append(raw_query.strip())
    raw_queries = source.get("queries")
    if isinstance(raw_queries, list):
        queries.extend(
            value.strip()
            for value in raw_queries
            if isinstance(value, str) and value.strip()
        )
    return queries


def _symbols_for_queries(
    queries: list[str],
    *,
    identity_rows: list[dict[str, Any]],
) -> tuple[set[str], bool]:
    if not queries:
        return set(), False
    matched_symbols: set[str] = set()
    for query in queries:
        query_matches = {
            str(row.get("symbol") or "")
            for row in identity_rows
            if str(row.get("symbol") or "")
            and _query_matches_identity(query, row)
        }
        if len(query_matches) != 1:
            return set(), False
        matched_symbols.update(query_matches)
    return matched_symbols, bool(matched_symbols)


def _web_search_status(item: dict[str, Any]) -> str:
    raw_status = str(item.get("status") or "").strip().lower()
    if raw_status == "completed":
        return "completed"
    if raw_status == "failed":
        return "failed"
    if raw_status in {"in_progress", "searching"}:
        return "in_progress"
    return "unknown"


def _query_matches_identity(query: str, row: dict[str, Any]) -> bool:
    text = " ".join(str(query or "").casefold().split())
    if not text:
        return False
    symbol = str(row.get("symbol") or "").strip().casefold()
    names = [
        str(row.get("company_name") or row.get("name") or "").strip().casefold(),
        *[
            str(alias or "").strip().casefold()
            for alias in row.get("aliases") or []
        ],
    ]
    if symbol and re.search(rf"(?<![\w.]){re.escape(symbol)}(?![\w.])", text):
        return True
    return any(name and name in text for name in names)


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers=dict(headers or {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body_text = _decode_body(resp.read())
            parsed = _try_parse_json(body_text)
            if not isinstance(parsed, dict):
                raise DeepSeekResponsesError(
                    "invalid DeepSeek JSON response",
                    http_status=getattr(resp, "status", None),
                    response_sha256=hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                )
            return parsed
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = _decode_body(exc.read())
        except Exception:
            body_text = ""
        raise DeepSeekResponsesError(
            f"DeepSeek API HTTP error {getattr(exc, 'code', None)}",
            http_status=getattr(exc, "code", None),
            response_sha256=hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        ) from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise DeepSeekResponsesError(
            f"DeepSeek API network error: {type(exc).__name__}",
            http_status=None,
        ) from exc


def _decode_body(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None
