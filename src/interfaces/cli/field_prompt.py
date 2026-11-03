from __future__ import annotations

from typing import Callable, TypeVar

from src.application.agent_tool_contracts import AgentToolError


T = TypeVar("T")


def prompt_field(
    prompt: str, *, validate: Callable[[str], T], default: str = "",
    input_fn: Callable[[str], str] = input,
) -> T:
    """Keep the accepted draft outside this loop; cancellation belongs to the form."""
    while True:
        entered = input_fn(prompt).strip() or default
        try:
            return validate(entered)
        except (AgentToolError, ValueError) as exc:
            if isinstance(exc, AgentToolError) and exc.code != "INPUT_ERROR":
                raise
            message = exc.message if isinstance(exc, AgentToolError) else str(exc)
            print(f"输入无效：{message}；请重新填写此项。")
