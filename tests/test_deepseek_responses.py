from __future__ import annotations

import pytest

from src.infrastructure.deepseek_responses import (
    DEFAULT_DEEPSEEK_RESPONSES_URL,
    DeepSeekResponsesError,
    audit_web_search_calls_by_symbol,
    create_deepseek_response,
    extract_output_text,
    extract_native_url_citations,
    extract_native_web_search_sources,
    extract_usage,
    response_fingerprint,
    resolve_deepseek_responses_url,
    summarize_web_search_calls,
)


def _capture(payload_response: dict):
    seen: dict = {}

    def _post(url, payload, *, headers=None, timeout=60):
        seen["url"] = url
        seen["payload"] = payload
        seen["headers"] = headers
        seen["timeout"] = timeout
        return payload_response

    return seen, _post


def test_create_response_minimal_payload() -> None:
    seen, post = _capture({"status": "completed", "output": []})
    result = create_deepseek_response(
        api_key="k",
        model="deepseek-v4-flash",
        input_items=[{"role": "user", "content": "hi"}],
        instructions="inst",
        http_post_json_fn=post,
    )
    assert result == {"status": "completed", "output": []}
    assert seen["url"] == DEFAULT_DEEPSEEK_RESPONSES_URL
    payload = seen["payload"]
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["instructions"] == "inst"
    assert payload["store"] is False
    assert "tools" not in payload
    assert "text" not in payload
    assert seen["headers"]["Authorization"] == "Bearer k"


def test_create_response_web_search_and_schema() -> None:
    seen, post = _capture({"status": "completed", "output": []})
    schema = {"name": "evidence", "schema": {"type": "object"}}
    create_deepseek_response(
        api_key="k",
        model="m",
        input_items=[],
        instructions="",
        enable_web_search=True,
        json_schema=schema,
        http_post_json_fn=post,
    )
    payload = seen["payload"]
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["name"] == "evidence"
    assert payload["text"]["format"]["strict"] is True


def test_resolve_url_default_and_normalized() -> None:
    assert resolve_deepseek_responses_url(None) == DEFAULT_DEEPSEEK_RESPONSES_URL
    assert resolve_deepseek_responses_url("") == DEFAULT_DEEPSEEK_RESPONSES_URL
    assert (
        resolve_deepseek_responses_url("https://api.deepseek.com")
        == "https://api.deepseek.com/responses"
    )
    assert (
        resolve_deepseek_responses_url("https://api.deepseek.com/responses")
        == "https://api.deepseek.com/responses"
    )


def test_extract_output_text_and_usage() -> None:
    response = {
        "output": [
            {"type": "web_search_call", "id": "ws_1", "status": "completed"},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "{\"a\":"},
                    {"type": "output_text", "text": "1}"},
                ],
            },
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "provider_debug": "must-not-escape",
        },
    }
    assert extract_output_text(response) == "{\"a\":1}"
    assert extract_usage(response) == {"input_tokens": 10, "output_tokens": 5}
    assert summarize_web_search_calls(response) == {
        "count": 1,
        "status_counts": {"completed": 1},
    }
    assert len(response_fingerprint(response)) == 64


def test_extract_helpers_tolerate_missing_fields() -> None:
    assert extract_output_text({}) == ""
    assert extract_usage({}) == {}
    assert summarize_web_search_calls({}) == {"count": 0, "status_counts": {}}


def test_create_response_requires_key_and_model() -> None:
    with pytest.raises(ValueError, match="api_key"):
        create_deepseek_response(api_key="", model="m", input_items=[], instructions="")
    with pytest.raises(ValueError, match="model"):
        create_deepseek_response(api_key="k", model=" ", input_items=[], instructions="")


def test_create_response_rejects_non_completed_provider_status() -> None:
    _seen, post = _capture(
        {
            "status": "incomplete",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"results":[]}'},
                    ],
                }
            ],
        }
    )
    with pytest.raises(DeepSeekResponsesError, match="did not complete") as exc:
        create_deepseek_response(
            api_key="k",
            model="m",
            input_items=[],
            instructions="",
            http_post_json_fn=post,
        )
    assert len(exc.value.response_sha256 or "") == 64


def test_error_type_is_distinct() -> None:
    err = DeepSeekResponsesError("boom", http_status=500, response_sha256="a" * 64)
    assert str(err) == "boom"
    assert err.http_status == 500
    assert err.response_sha256 == "a" * 64


def test_search_calls_are_attributed_without_returning_queries() -> None:
    response = {
        "output": [
            {
                "type": "web_search_call",
                "id": "ws-private",
                "status": "completed",
                "action": {
                    "type": "search",
                    "query": "NVDA NVIDIA regulatory filing",
                    "sources": [
                        {"type": "url", "url": "https://example.com/nvda"}
                    ],
                },
            },
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "queries": ["腾讯控股 0700.HK exchange notice"],
                    "sources": [
                        {"type": "url", "url": "https://example.com/tencent"}
                    ],
                },
            },
        ]
    }
    audit = audit_web_search_calls_by_symbol(
        response,
        identity_rows=[
            {"symbol": "NVDA", "company_name": "NVIDIA", "aliases": []},
            {"symbol": "0700.HK", "company_name": "腾讯控股", "aliases": []},
        ],
    )
    assert audit["count"] == 2
    assert audit["unattributed_count"] == 0
    assert audit["auxiliary_count"] == 0
    assert audit["status_counts"]["completed"] == 2
    assert audit["symbols"]["NVDA"]["completed"] == 1
    assert audit["symbols"]["0700.HK"]["completed"] == 1
    assert "query" not in str(audit)
    assert "ws-private" not in str(audit)
    assert extract_native_web_search_sources(
        response,
        identity_rows=[
            {"symbol": "NVDA", "company_name": "NVIDIA", "aliases": []},
            {"symbol": "0700.HK", "company_name": "腾讯控股", "aliases": []},
        ],
    ) == [
        {"symbol": "NVDA", "url": "https://example.com/nvda"},
        {"symbol": "0700.HK", "url": "https://example.com/tencent"},
    ]


def test_ambiguous_or_missing_search_query_stays_unattributed() -> None:
    response = {
        "output": [
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search"},
            },
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "NVDA and AAPL news"},
            },
        ]
    }
    audit = audit_web_search_calls_by_symbol(
        response,
        identity_rows=[
            {"symbol": "NVDA", "company_name": "NVIDIA"},
            {"symbol": "AAPL", "company_name": "Apple"},
        ],
    )
    assert audit["unattributed_count"] == 2
    assert audit["symbols"]["NVDA"]["completed"] == 0
    assert audit["symbols"]["AAPL"]["completed"] == 0


def test_completed_auxiliary_search_actions_do_not_break_symbol_attribution() -> None:
    response = {
        "output": [
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "NVDA NVIDIA news"},
            },
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "open_page", "url": "https://example.com"},
            },
            {
                "type": "web_search_call",
                "status": "searching",
                "action": {
                    "type": "find_in_page",
                    "url": "https://example.com",
                    "pattern": "NVIDIA",
                },
            },
        ]
    }
    audit = audit_web_search_calls_by_symbol(
        response,
        identity_rows=[{"symbol": "NVDA", "company_name": "NVIDIA"}],
    )
    assert audit == {
        "count": 3,
        "unattributed_count": 0,
        "auxiliary_count": 2,
        "status_counts": {
            "completed": 2,
            "failed": 0,
            "in_progress": 1,
            "unknown": 0,
        },
        "symbols": {
            "NVDA": {
                "completed": 1,
                "failed": 0,
                "in_progress": 0,
                "unknown": 0,
            }
        },
    }


def test_extract_native_citations_accepts_only_url_annotations() -> None:
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "body",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/a",
                                "title": "A",
                            },
                            {
                                "type": "url_citation",
                                "url_citation": {
                                    "url": "https://example.com/b",
                                    "title": "B",
                                    "publisher": "Example",
                                },
                            },
                            {"type": "file_citation", "url": "https://ignore"},
                        ],
                    }
                ],
            }
        ]
    }
    assert extract_native_url_citations(response) == [
        {"url": "https://example.com/a", "title": "A"},
        {
            "url": "https://example.com/b",
            "title": "B",
            "publisher": "Example",
        },
    ]
