from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.secret_store import LLM_DEEPSEEK_API_KEY
from src.infrastructure.secret_store.memory import InMemorySecretProvider
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.secret_ops import run_store_command


def test_secret_cli_set_uses_hidden_prompt_and_never_returns_value() -> None:
    memory = InMemorySecretProvider()
    prompts: list[str] = []

    def prompt(label: str) -> str:
        prompts.append(label)
        return "test-secret-value"

    args = parse_args(["secrets", "set", LLM_DEEPSEEK_API_KEY, "--backend", "keychain"])
    payload = run_store_command(
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
    payload = run_store_command(args, provider_factory=lambda **_kwargs: memory)

    assert payload["summary"]["configured_count"] == 1
    assert payload["summary"]["values_exposed"] is False
    assert "test-secret-value" not in json.dumps(payload)


def test_secret_cli_rejects_mismatched_confirmation() -> None:
    memory = InMemorySecretProvider()
    values = iter(("first", "second"))
    args = parse_args(["secrets", "rotate", LLM_DEEPSEEK_API_KEY])
    with pytest.raises(AgentToolError, match="confirmation does not match"):
        run_store_command(
            args,
            provisioner_factory=lambda **_kwargs: memory,
            prompt_fn=lambda _label: next(values),
            input_is_tty=lambda: True,
        )


def test_secret_cli_rejects_noninteractive_secret_input() -> None:
    memory = InMemorySecretProvider()
    args = parse_args(["secrets", "set", LLM_DEEPSEEK_API_KEY])

    with pytest.raises(AgentToolError, match="interactive terminal"):
        run_store_command(
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
        run_store_command(args, provisioner_factory=lambda **_kwargs: memory)


@pytest.mark.parametrize("backend", ["systemd", "auto"])
@pytest.mark.parametrize("output_format", ["json", "text"])
@pytest.mark.parametrize("state", ["denied", "io", "missing", "valid"])
def test_secret_cli_real_systemd_status_classifies_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    backend: str, output_format: str, state: str,
) -> None:
    import src.infrastructure.secret_store.factory as factory
    from src.interfaces.cli.main import main

    monkeypatch.setattr(factory, "_platform_name", lambda value=None: "linux")
    monkeypatch.setenv("OM_SECRET_BACKEND", "auto")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    target = tmp_path / "om-llm-deepseek-api-key"
    if state == "valid":
        target.write_text("synthetic-value-must-not-be-read")
    original_lstat = Path.lstat

    def injected_lstat(path, *args, **kwargs):
        if path == target and state in {"denied", "io"}:
            raise OSError(errno.EACCES if state == "denied" else errno.EIO, "private-path-and-value")
        return original_lstat(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("status must not read/decrypt credentials")

    from src.infrastructure.secret_store.systemd_credentials import SystemdCredentialProvider
    monkeypatch.setattr(SystemdCredentialProvider, "get", forbidden)
    monkeypatch.setattr(Path, "lstat", injected_lstat)
    argv = ["secrets", "status", LLM_DEEPSEEK_API_KEY, "--backend", backend,
            "--store-root", str(tmp_path), "--format", output_format]
    if state in {"denied", "io"}:
        with pytest.raises(AgentToolError) as caught:
            run_store_command(parse_args(argv))
        assert caught.value.code == "CONFIG_ERROR"
    assert main(argv) == (2 if state in {"denied", "io"} else 0)
    output = capsys.readouterr()
    assert "private-path-and-value" not in output.out + output.err
    assert "synthetic-value-must-not-be-read" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    if state in {"denied", "io"}:
        payload = json.loads(output.out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "CONFIG_ERROR"
        assert str(tmp_path) not in output.out
    elif output_format == "json":
        payload = json.loads(output.out)
        assert payload["summary"]["configured_count"] == (1 if state == "valid" else 0)
        assert payload["summary"]["values_exposed"] is False
