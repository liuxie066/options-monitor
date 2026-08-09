from __future__ import annotations

import json

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.secret_store import LLM_DEEPSEEK_API_KEY
from src.infrastructure.secret_store.memory import InMemorySecretProvider
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.secret_ops import handle_secret_command


def test_secret_cli_set_uses_hidden_prompt_and_never_returns_value() -> None:
    memory = InMemorySecretProvider()
    prompts: list[str] = []

    def prompt(label: str) -> str:
        prompts.append(label)
        return "test-secret-value"

    args = parse_args(["secrets", "set", LLM_DEEPSEEK_API_KEY, "--backend", "keychain"])
    payload = handle_secret_command(
        args,
        provisioner_factory=lambda **_kwargs: memory,
        prompt_fn=prompt,
        input_is_tty=lambda: True,
    )

    assert memory.get(LLM_DEEPSEEK_API_KEY) == "test-secret-value"
    assert len(prompts) == 2
    assert payload["value_exposed"] is False
    assert payload["restart_performed"] is False
    assert "test-secret-value" not in json.dumps(payload)


def test_secret_cli_status_is_redacted() -> None:
    memory = InMemorySecretProvider({LLM_DEEPSEEK_API_KEY: "test-secret-value"})
    args = parse_args(["secrets", "status", LLM_DEEPSEEK_API_KEY])
    payload = handle_secret_command(args, provider_factory=lambda **_kwargs: memory)

    assert payload["summary"]["configured_count"] == 1
    assert payload["summary"]["values_exposed"] is False
    assert "test-secret-value" not in json.dumps(payload)


def test_secret_cli_rejects_mismatched_confirmation() -> None:
    memory = InMemorySecretProvider()
    values = iter(("first", "second"))
    args = parse_args(["secrets", "rotate", LLM_DEEPSEEK_API_KEY])
    with pytest.raises(AgentToolError, match="confirmation does not match"):
        handle_secret_command(
            args,
            provisioner_factory=lambda **_kwargs: memory,
            prompt_fn=lambda _label: next(values),
            input_is_tty=lambda: True,
        )


def test_secret_cli_rejects_noninteractive_secret_input() -> None:
    memory = InMemorySecretProvider()
    args = parse_args(["secrets", "set", LLM_DEEPSEEK_API_KEY])

    with pytest.raises(AgentToolError, match="interactive terminal"):
        handle_secret_command(
            args,
            provisioner_factory=lambda **_kwargs: memory,
            prompt_fn=lambda _label: "must-not-be-read",
            input_is_tty=lambda: False,
        )

    assert memory.get(LLM_DEEPSEEK_API_KEY) is None


def test_secret_cli_delete_requires_explicit_confirmation() -> None:
    memory = InMemorySecretProvider({LLM_DEEPSEEK_API_KEY: "test-secret-value"})
    args = parse_args(["secrets", "delete", LLM_DEEPSEEK_API_KEY])
    with pytest.raises(AgentToolError, match="requires --confirm"):
        handle_secret_command(args, provisioner_factory=lambda **_kwargs: memory)
