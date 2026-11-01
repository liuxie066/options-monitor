from __future__ import annotations

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_primitives import normalize_trd_env
from src.interfaces.cli.field_prompt import prompt_field


def test_field_retry_keeps_default_and_returns_normalized_value(capsys):
    answers = iter(("paper", "  simulate  "))
    assert prompt_field("环境", default="REAL", validate=normalize_trd_env,
                        input_fn=lambda _: next(answers)) == "SIMULATE"
    assert "请重新填写此项" in capsys.readouterr().out
    assert prompt_field("环境", default="REAL", validate=normalize_trd_env,
                        input_fn=lambda _: "  ") == "REAL"


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt(),
                                   AgentToolError(code="CONFIG_ERROR", message="source unreadable")])
def test_field_does_not_hide_cancellation_or_configuration_errors(error):
    def validate(_):
        raise error
    with pytest.raises(type(error)) as caught:
        prompt_field("字段", validate=validate, input_fn=lambda _: "x")
    assert caught.value is error
