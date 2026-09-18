from __future__ import annotations

import asyncio
import contextlib
from dataclasses import FrozenInstanceError, fields

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_error_payload


def _boom() -> AgentToolError:
    return AgentToolError("E_BOOM", "the real failure", hint="run the thing", details={"a": 1})


def _raise_in_its_own_frame() -> None:
    raise _boom()


def _frame_names(err: BaseException) -> list[str]:
    names: list[str] = []
    tb = err.__traceback__
    while tb is not None:
        names.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    return names


@contextlib.contextmanager
def _plain_contextmanager():
    yield
    # `contextlib` rebinds `exc.__traceback__` from Python on the way out.


@contextlib.asynccontextmanager
async def _async_contextmanager():
    yield


def test_agent_tool_error_survives_a_contextmanager_that_reraises() -> None:
    """`contextlib` rebinding `__traceback__` must not replace the error it is unwinding."""
    with pytest.raises(AgentToolError) as excinfo:
        with _plain_contextmanager():
            _raise_in_its_own_frame()

    err = excinfo.value
    assert err.code == "E_BOOM"
    assert err.message == "the real failure"
    assert err.hint == "run the thing"
    assert err.details == {"a": 1}
    assert str(err) == "E_BOOM: the real failure"
    # The traceback `contextlib` restored must be the real chain, still reaching the raise
    # site, not a placeholder.
    assert _frame_names(err)[-1] == "_raise_in_its_own_frame"
    assert build_error_payload(err) == {
        "code": "E_BOOM",
        "message": "the real failure",
        "hint": "run the thing",
        "details": {"a": 1},
    }


def test_agent_tool_error_survives_an_async_contextmanager_that_reraises() -> None:
    async def scenario() -> None:
        async with _async_contextmanager():
            raise _boom()

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(scenario())

    assert excinfo.value.code == "E_BOOM"
    assert excinfo.value.__traceback__ is not None


def test_agent_tool_error_still_chains_and_keeps_its_own_context() -> None:
    cause = ValueError("root cause")
    try:
        try:
            raise cause
        except ValueError as exc:
            raise _boom() from exc
    except AgentToolError as err:
        assert err.__cause__ is cause
        assert err.code == "E_BOOM"

    with pytest.raises(AgentToolError) as excinfo:
        try:
            raise ValueError("inner")
        except ValueError:
            raise _boom()
    assert isinstance(excinfo.value.__context__, ValueError)
    assert excinfo.value.__suppress_context__ is False


def test_agent_tool_error_notes_do_not_replace_the_error() -> None:
    """`BaseException.add_note` assigns `__notes__` from Python, like `__traceback__`."""
    err = _boom()
    err.add_note("operator hint")
    assert err.__notes__ == ["operator hint"]
    assert err.code == "E_BOOM"


def test_agent_tool_error_payload_fields_stay_frozen() -> None:
    err = _boom()
    assert [field.name for field in fields(AgentToolError)] == [
        "code",
        "message",
        "hint",
        "details",
    ]
    for name in ("code", "message", "hint", "details"):
        with pytest.raises(FrozenInstanceError):
            setattr(err, name, "mutated")
    assert err.code == "E_BOOM"
    assert err.details == {"a": 1}
