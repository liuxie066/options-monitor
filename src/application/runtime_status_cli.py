from __future__ import annotations

from typing import Any


PAYLOAD_KEYS = (
    "config_key",
    "config_path",
    "accounts",
    "profile_path",
    "run_id",
    "run_dir",
    "report_dir",
    "state_dir",
    "shared_state_dir",
    "accounts_root",
    "runs_root",
    "max_run_age_minutes",
    "max_notification_chars",
)


def runtime_status_payload_from_args(args: Any) -> dict[str, Any]:
    payload = {key: getattr(args, key) for key in PAYLOAD_KEYS if hasattr(args, key)}
    return {key: value for key, value in payload.items() if value not in (None, [])}


def format_runtime_status_summary(envelope: dict[str, Any]) -> str:
    data = _dict(envelope.get("data"))
    summary = _dict(data.get("summary"))
    freshness = _dict(data.get("freshness"))
    config = _dict(data.get("config"))
    latest_run = _dict(data.get("latest_run_selection"))
    latest_scanned = _dict(data.get("latest_scanned_run_selection"))
    notification = _dict(data.get("notification_diagnosis"))
    ledger = _dict(data.get("ledger_store"))
    projection = _dict(data.get("projection_verify"))
    trade = _dict(data.get("trade_intake"))
    prefetch = _dict(data.get("required_data_prefetch"))
    scanned_prefetch = _dict(data.get("latest_scanned_run_required_data_prefetch"))
    service = _dict(data.get("service_upgrade"))
    service_drift = _dict(data.get("service_drift"))
    warnings = _list(envelope.get("warnings"))
    ledger_warnings = _list(ledger.get("warnings"))

    lines = [
        "options-monitor status",
        _overall_line(envelope=envelope, summary=summary, freshness=freshness, warnings=warnings),
        _config_line(config),
        "",
        "runs:",
        _run_line("latest", latest_run, summary.get("latest_status")),
        _run_line("latest scanned", latest_scanned, None),
        _freshness_line(freshness),
        "",
        _notification_line(notification),
        _ledger_line(summary=summary, ledger=ledger),
        _projection_line(projection),
        _trade_intake_line(trade),
        _prefetch_line("prefetch", prefetch),
        _prefetch_line("prefetch scanned", scanned_prefetch),
        _service_line(service),
        _service_drift_line(service_drift),
    ]

    error = _dict(envelope.get("error"))
    if error:
        lines.extend(["", _error_line(error)])
    if warnings or ledger_warnings:
        lines.append("")
        lines.append("warnings:")
        lines.extend(f"- {item}" for item in [*warnings, *ledger_warnings] if item)
    return "\n".join(lines).rstrip() + "\n"


def _overall_line(
    *,
    envelope: dict[str, Any],
    summary: dict[str, Any],
    freshness: dict[str, Any],
    warnings: list[Any],
) -> str:
    status = _overall_status(envelope=envelope, summary=summary, freshness=freshness, warnings=warnings)
    parts = [f"overall: {status}"]
    freshness_status = freshness.get("status") or summary.get("freshness_status")
    if freshness_status is not None:
        parts.append(f"freshness={freshness_status}")
    warning_count = summary.get("warning_count")
    if warning_count is not None:
        parts.append(f"warnings={warning_count}")
    latest_status = summary.get("latest_status")
    if latest_status is not None:
        parts.append(f"latest_status={latest_status}")
    return " ".join(parts)


def _overall_status(
    *,
    envelope: dict[str, Any],
    summary: dict[str, Any],
    freshness: dict[str, Any],
    warnings: list[Any],
) -> str:
    if envelope.get("ok") is False:
        return "FAIL"
    if summary.get("ok") is False:
        return "FAIL"
    if str(freshness.get("status") or summary.get("freshness_status") or "").lower() in {
        "stale",
        "missing",
        "error",
    }:
        return "WARN"
    if warnings or _as_int(summary.get("warning_count")) > 0:
        return "WARN"
    return "OK"


def _config_line(config: dict[str, Any]) -> str:
    accounts = _csv(config.get("accounts"))
    return (
        f"config: key={_value(config.get('config_key'))} "
        f"path={_value(config.get('config_path'))} accounts={accounts}"
    )


def _run_line(label: str, selection: dict[str, Any], latest_status: Any) -> str:
    parts = [
        f"{label}:",
        f"found={_yes_no(selection.get('found'))}",
        f"path={_value(selection.get('path'))}",
    ]
    if latest_status is not None:
        parts.append(f"status={latest_status}")
    source = selection.get("source")
    if source:
        parts.append(f"source={source}")
    return " ".join(parts)


def _freshness_line(freshness: dict[str, Any]) -> str:
    parts = [
        "freshness:",
        f"status={_value(freshness.get('status'))}",
        f"age={_duration_seconds(freshness.get('age_seconds'))}",
        f"max={_max_age(freshness.get('max_age_minutes'))}",
    ]
    if freshness.get("latest_source"):
        parts.append(f"source={freshness.get('latest_source')}")
    return " ".join(parts)


def _notification_line(notification: dict[str, Any]) -> str:
    route = _dict(notification.get("notification_route"))
    route_text = "/".join(
        item
        for item in (
            str(route.get("provider") or "").strip(),
            str(route.get("channel") or "").strip(),
        )
        if item
    )
    return (
        "notifications: "
        f"status={_value(notification.get('status'))} "
        f"reason={_value(notification.get('final_reason') or notification.get('reason'))} "
        f"route={_value(route_text)} "
        f"target={_yes_no(route.get('target_configured'))} "
        f"sent={_int_value(notification.get('send_attempted_count'))} "
        f"confirmed={_int_value(notification.get('send_confirmed_count'))} "
        f"failed={_int_value(notification.get('send_failed_count'))}"
    )


def _ledger_line(*, summary: dict[str, Any], ledger: dict[str, Any]) -> str:
    trade_events = (
        summary.get("ledger_trade_event_count")
        if summary.get("ledger_trade_event_count") is not None
        else ledger.get("trade_event_count")
    )
    lots = (
        summary.get("ledger_position_lot_count")
        if summary.get("ledger_position_lot_count") is not None
        else ledger.get("position_lot_count")
    )
    return (
        "ledger: "
        f"status={_value(summary.get('ledger_status'))} "
        f"fail_closed={_yes_no(summary.get('ledger_fail_closed'))} "
        f"events={_value(trade_events)} "
        f"lots={_value(lots)} "
        f"sqlite={_value(summary.get('ledger_sqlite_path') or ledger.get('sqlite_path'))}"
    )


def _projection_line(projection: dict[str, Any]) -> str:
    latest = _dict(projection.get("latest") or projection.get("json"))
    ok = latest.get("ok") if latest else projection.get("ok")
    mode = latest.get("mode") if latest else projection.get("mode")
    return (
        "projection: "
        f"exists={_yes_no(projection.get('exists'))} "
        f"ok={_yes_no(ok)} "
        f"mode={_value(mode)} "
        f"path={_value(projection.get('path'))}"
    )


def _trade_intake_line(trade: dict[str, Any]) -> str:
    summary = _dict(trade.get("summary"))
    return (
        "trade intake: "
        f"enabled={_yes_no(trade.get('enabled'))} "
        f"mode={_value(trade.get('mode'))} "
        f"listener={_value(summary.get('listener_status'))} "
        f"processed={_int_value(summary.get('processed_count'))} "
        f"failed={_int_value(summary.get('failed_count'))} "
        f"unresolved={_int_value(summary.get('unresolved_count'))} "
        f"receipts={_int_value(summary.get('receipt_count'))} "
        f"confirmed={_int_value(summary.get('receipt_confirmed_count'))} "
        f"receipt_failed={_int_value(summary.get('receipt_failed_count'))}"
    )


def _prefetch_line(label: str, prefetch: dict[str, Any]) -> str:
    return (
        f"{label}: "
        f"available={_yes_no(prefetch.get('available'))} "
        f"accounts={_int_value(prefetch.get('available_account_count'))}/{_int_value(prefetch.get('account_count'))} "
        f"calls={_int_value(prefetch.get('total_opend_calls'))} "
        f"wait={_seconds_value(prefetch.get('total_rate_gate_wait_sec'))} "
        f"errors={_int_value(prefetch.get('total_errors'))} "
        f"bottleneck={_value(prefetch.get('primary_bottleneck'))}"
    )


def _service_line(service: dict[str, Any]) -> str:
    latest = _dict(service.get("latest") or service.get("json"))
    status = latest.get("status") if latest else service.get("status")
    target = latest.get("target_version") if latest else service.get("target_version")
    return f"service: upgrade={_value(status)} target={_value(target)}"


def _service_drift_line(drift: dict[str, Any]) -> str:
    summary = _dict(drift.get("summary"))
    return (
        "service drift: "
        f"status={_value(summary.get('status'))} "
        f"missing={_int_value(summary.get('missing_installed_count'))} "
        f"required_missing={_csv(summary.get('missing_required_units'))}"
    )


def _error_line(error: dict[str, Any]) -> str:
    code = error.get("code")
    message = error.get("message")
    if code and message:
        return f"error: {code} {message}"
    return f"error: {_value(message or code or error)}"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _csv(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    if value is None:
        return "-"
    return str(value)


def _value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "-"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_value(value: Any) -> str:
    if value is None:
        return "-"
    return str(_as_int(value))


def _seconds_value(value: Any) -> str:
    try:
        return f"{float(value):.1f}s"
    except (TypeError, ValueError):
        return "-"


def _duration_seconds(value: Any) -> str:
    if value is None:
        return "-"
    seconds = _as_int(value)
    return f"{seconds}s"


def _max_age(value: Any) -> str:
    if value is None:
        return "-"
    return f"{_as_int(value)}m"
