from __future__ import annotations


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
