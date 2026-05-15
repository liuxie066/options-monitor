from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "source": ("trigger_source", "source"),
    "job_id": ("trigger_job_id", "job_id", "cron_job_id"),
    "job_name": ("trigger_job_name", "job_name", "cron_job_name"),
    "schedule": ("trigger_schedule", "schedule", "cron_schedule"),
    "timezone": ("trigger_timezone", "timezone", "cron_timezone"),
}

_ENV_FIELDS: dict[str, tuple[str, ...]] = {
    "source": ("OM_TRIGGER_SOURCE", "OPENCLAW_TRIGGER_SOURCE"),
    "job_id": ("OM_TRIGGER_JOB_ID", "OPENCLAW_CRON_JOB_ID"),
    "job_name": ("OM_TRIGGER_JOB_NAME", "OPENCLAW_CRON_JOB_NAME"),
    "schedule": ("OM_TRIGGER_SCHEDULE", "OPENCLAW_CRON_SCHEDULE"),
    "timezone": ("OM_TRIGGER_TIMEZONE", "OPENCLAW_CRON_TIMEZONE"),
    "delivery_mode": ("OM_DELIVERY_MODE", "OPENCLAW_DELIVERY_MODE"),
    "timeout_seconds": ("OM_TIMEOUT_SECONDS", "OPENCLAW_TIMEOUT_SECONDS"),
}


def _first_text(source: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return None


def _payload_delivery_mode(payload: Mapping[str, Any]) -> str | None:
    direct = _first_text(payload, ("delivery_mode", "deliveryMode"))
    if direct:
        return direct.lower()
    delivery_raw = payload.get("delivery")
    delivery = delivery_raw if isinstance(delivery_raw, Mapping) else {}
    nested = _first_text(delivery, ("mode",))
    return nested.lower() if nested else None


def _payload_timeout_seconds(payload: Mapping[str, Any]) -> Any:
    for key in ("timeout_seconds", "timeoutSeconds"):
        if key in payload:
            return payload.get(key)
    return None


def _parse_positive_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(float(text))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _add_timeout(ctx: dict[str, Any], value: Any) -> None:
    if value is None:
        return
    parsed = _parse_positive_int(value)
    if parsed is not None:
        ctx["timeout_seconds"] = parsed
        return
    text = str(value or "").strip()
    if text:
        ctx["timeout_seconds_raw"] = text
        ctx["timeout_seconds_parse_error"] = True


def build_trigger_context(
    payload: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return optional outer trigger/delivery metadata for runtime diagnostics."""
    payload_map: Mapping[str, Any] = payload or {}
    env_map: Mapping[str, str] = environ if environ is not None else os.environ
    ctx: dict[str, Any] = {}

    for field, keys in _TEXT_FIELDS.items():
        value = _first_text(payload_map, keys)
        if value is None:
            value = _first_text(env_map, _ENV_FIELDS[field])
        if value is not None:
            ctx[field] = value

    delivery_mode = _payload_delivery_mode(payload_map)
    if delivery_mode is None:
        delivery_mode = _first_text(env_map, _ENV_FIELDS["delivery_mode"])
        delivery_mode = delivery_mode.lower() if delivery_mode else None
    if delivery_mode:
        ctx["delivery_mode"] = delivery_mode
        ctx["announce_expected"] = delivery_mode == "announce"

    timeout_value = _payload_timeout_seconds(payload_map)
    if timeout_value is None:
        timeout_value = _first_text(env_map, _ENV_FIELDS["timeout_seconds"])
    _add_timeout(ctx, timeout_value)

    ctx["observed"] = bool(ctx)
    return ctx


__all__ = ["build_trigger_context"]
