from __future__ import annotations

import pytest


def test_build_trigger_context_prefers_payload_delivery_and_timeout() -> None:
    from src.application.runtime_trigger_context import build_trigger_context

    out = build_trigger_context(
        {
            "trigger_source": "om_direct",
            "trigger_job_id": "hk-direct-11",
            "delivery": {"mode": "none"},
            "timeoutSeconds": 700,
        },
        environ={
            "OM_DELIVERY_MODE": "announce",
            "OM_TIMEOUT_SECONDS": "60",
        },
    )

    assert out == {
        "source": "om_direct",
        "job_id": "hk-direct-11",
        "delivery_mode": "none",
        "announce_expected": False,
        "timeout_seconds": 700,
        "observed": True,
    }


def test_build_trigger_context_reads_environment_when_payload_absent() -> None:
    from src.application.runtime_trigger_context import build_trigger_context

    out = build_trigger_context(
        environ={
            "OM_TRIGGER_SOURCE": "cron",
            "OM_TRIGGER_JOB_NAME": "OM direct HK tick",
            "OM_DELIVERY_MODE": "announce",
            "OM_TIMEOUT_SECONDS": "700",
        }
    )

    assert out["source"] == "cron"
    assert out["job_name"] == "OM direct HK tick"
    assert out["delivery_mode"] == "announce"
    assert out["announce_expected"] is True
    assert out["timeout_seconds"] == 700
    assert out["observed"] is True


@pytest.mark.parametrize(
    "source",
    ("timeout_seconds", "timeoutSeconds", "OM_TIMEOUT_SECONDS", "OPENCLAW_TIMEOUT_SECONDS"),
)
@pytest.mark.parametrize("raw", ("inf", "-inf", "1e309", "Infinity", "-1e309", "nan", "abc", "0", "-1"))
def test_invalid_timeout_metadata_retains_diagnostic(source: str, raw: str) -> None:
    from src.application.runtime_trigger_context import build_trigger_context

    payload = {source: raw} if source.startswith("timeout") else {}
    environ = {} if payload else {source: raw}

    assert build_trigger_context(payload, environ=environ) == {
        "timeout_seconds_raw": raw,
        "timeout_seconds_parse_error": True,
        "observed": True,
    }


@pytest.mark.parametrize("key", ("timeout_seconds", "timeoutSeconds"))
@pytest.mark.parametrize("value, raw", ((float("inf"), "inf"), (float("-inf"), "-inf"), (float("nan"), "nan")))
def test_numeric_nonfinite_payload_retains_diagnostic(key: str, value: float, raw: str) -> None:
    from src.application.runtime_trigger_context import build_trigger_context

    assert build_trigger_context({key: value}, environ={}) == {
        "timeout_seconds_raw": raw,
        "timeout_seconds_parse_error": True,
        "observed": True,
    }


@pytest.mark.parametrize(
    "source",
    ("timeout_seconds", "timeoutSeconds", "OM_TIMEOUT_SECONDS", "OPENCLAW_TIMEOUT_SECONDS"),
)
@pytest.mark.parametrize("raw", ("700", " 7e2 ", "700.9"))
def test_finite_timeout_metadata_keeps_conversion(source: str, raw: str) -> None:
    from src.application.runtime_trigger_context import build_trigger_context

    payload = {source: raw} if source.startswith("timeout") else {}
    environ = {} if payload else {source: raw}

    assert build_trigger_context(payload, environ=environ) == {
        "timeout_seconds": 700,
        "observed": True,
    }


@pytest.mark.parametrize("key", ("timeout_seconds", "timeoutSeconds"))
@pytest.mark.parametrize(
    "value, expected",
    (
        (" inf ", {"timeout_seconds_raw": "inf", "timeout_seconds_parse_error": True, "observed": True}),
        (30, {"timeout_seconds": 30, "observed": True}),
        (None, {"timeout_seconds": 700, "observed": True}),
        ("", {"observed": False}),
        (" ", {"observed": False}),
        (0, {"observed": False}),
        (False, {"observed": False}),
    ),
)
def test_payload_timeout_precedence_and_empty_values(key: str, value, expected: dict) -> None:
    from src.application.runtime_trigger_context import build_trigger_context

    assert build_trigger_context(
        {key: value}, environ={"OM_TIMEOUT_SECONDS": "700"}
    ) == expected


def test_timeout_alias_precedence_and_absence() -> None:
    from src.application.runtime_trigger_context import build_trigger_context

    assert build_trigger_context(environ={}) == {"observed": False}
    assert build_trigger_context(
        {"timeout_seconds": "inf", "timeoutSeconds": 700}, environ={}
    ) == {"timeout_seconds_raw": "inf", "timeout_seconds_parse_error": True, "observed": True}
    assert build_trigger_context(
        environ={"OM_TIMEOUT_SECONDS": "inf", "OPENCLAW_TIMEOUT_SECONDS": "700"}
    ) == {"timeout_seconds_raw": "inf", "timeout_seconds_parse_error": True, "observed": True}
    assert build_trigger_context(
        environ={"OM_TIMEOUT_SECONDS": " ", "OPENCLAW_TIMEOUT_SECONDS": "700"}
    ) == {"timeout_seconds": 700, "observed": True}
