from src.infrastructure.openai_chat_completions import resolve_chat_completions_url
from src.infrastructure.openai_responses import resolve_responses_url


def test_responses_url_resolver_keeps_default_base_full_and_trailing_slash_forms() -> None:
    assert resolve_responses_url(None) == "https://api.openai.com/v1/responses"
    assert resolve_responses_url("") == "https://api.openai.com/v1/responses"
    assert resolve_responses_url("https://llm.example/v1") == "https://llm.example/v1/responses"
    assert resolve_responses_url("https://llm.example/v1/") == "https://llm.example/v1/responses"
    assert resolve_responses_url("https://llm.example/v1/responses") == "https://llm.example/v1/responses"
    assert resolve_responses_url("https://llm.example/v1/responses/") == "https://llm.example/v1/responses"


def test_chat_completions_url_resolver_keeps_default_base_full_and_trailing_slash_forms() -> None:
    assert resolve_chat_completions_url(None) == "https://api.deepseek.com/chat/completions"
    assert resolve_chat_completions_url("") == "https://api.deepseek.com/chat/completions"
    assert resolve_chat_completions_url("https://llm.example/v1") == "https://llm.example/v1/chat/completions"
    assert resolve_chat_completions_url("https://llm.example/v1/") == "https://llm.example/v1/chat/completions"
    assert resolve_chat_completions_url("https://llm.example/v1/chat/completions") == "https://llm.example/v1/chat/completions"
    assert resolve_chat_completions_url("https://llm.example/v1/chat/completions/") == "https://llm.example/v1/chat/completions"
