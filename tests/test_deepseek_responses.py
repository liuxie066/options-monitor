from __future__ import annotations

import pytest

from src.infrastructure.deepseek_responses import (
    DEFAULT_DEEPSEEK_RESPONSES_URL,
    DeepSeekResponsesError,
    create_deepseek_response,
    extract_output_text,
    extract_usage,
    extract_web_search_calls,
    resolve_deepseek_responses_url,
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
    seen, post = _capture({"output": []})
    result = create_deepseek_response(
        api_key="k",
        model="deepseek-v4-flash",
        input_items=[{"role": "user", "content": "hi"}],
        instructions="inst",
        http_post_json_fn=post,
    )
    assert result == {"output": []}
    assert seen["url"] == DEFAULT_DEEPSEEK_RESPONSES_URL
    payload = seen["payload"]
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["instructions"] == "inst"
    assert payload["store"] is False
    assert "tools" not in payload
    assert "text" not in payload
    assert seen["headers"]["Authorization"] == "Bearer k"


def test_create_response_web_search_and_schema() -> None:
    seen, post = _capture({"output": []})
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
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    assert extract_output_text(response) == "{\"a\":1}"
    assert extract_usage(response) == {"input_tokens": 10, "output_tokens": 5}
    calls = extract_web_search_calls(response)
    assert calls == [{"type": "web_search_call", "id": "ws_1", "status": "completed"}]


def test_extract_helpers_tolerate_missing_fields() -> None:
    assert extract_output_text({}) == ""
    assert extract_usage({}) == {}
    assert extract_web_search_calls({}) == []


def test_create_response_requires_key_and_model() -> None:
    with pytest.raises(ValueError, match="api_key"):
        create_deepseek_response(api_key="", model="m", input_items=[], instructions="")
    with pytest.raises(ValueError, match="model"):
        create_deepseek_response(api_key="k", model=" ", input_items=[], instructions="")


def test_error_type_is_distinct() -> None:
    err = DeepSeekResponsesError("boom", http_status=500, response={"error": {"message": "boom"}})
    assert str(err) == "boom"
    assert err.http_status == 500
