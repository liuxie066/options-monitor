from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


def _local_tz() -> timezone:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _today_local() -> date:
    return datetime.now(_local_tz()).date()


def _perception_audit_row(
    *,
    run_id: str,
    event_at_utc: datetime,
    accounts: list[str],
    sent_accounts: list[str] | None = None,
    failure_count: int = 0,
    no_send: bool = False,
) -> dict[str, Any]:
    sent = sent_accounts if sent_accounts is not None else list(accounts)
    return {
        "schema_kind": "om-audit-event",
        "schema_version": "v1",
        "event_type": "assistant_perception",
        "action": "notification_delivery_completed",
        "status": "ok",
        "event_at_utc": event_at_utc.isoformat(),
        "run_id": run_id,
        "extra": {
            "event_kind": "notification_delivery_completed",
            "run_id": run_id,
            "accounts": accounts,
            "no_send": no_send,
            "report_refs": [{"account": account, "market": "US", "market_date": event_at_utc.date().isoformat(), "revision": 0, "source_kind": "successful_brief", "source_digest": "a" * 64, "delivery_key": "test-delivery", "source_run_id": run_id} for account in sent],
            "send_summary": {
                "sent_accounts": sent,
                "failure_count": failure_count,
                "send_attempted_count": len(sent),
                "send_confirmed_count": len(sent),
            },
        },
    }


def _write_shared_audit(base: Path, rows: list[dict[str, Any]]) -> None:
    state_dir = base / "output_shared" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "audit_events.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _filler_rows(count: int, *, base_time: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(count):
        rows.append(
            {
                "schema_kind": "om-audit-event",
                "schema_version": "v1",
                "event_type": "assistant_perception",
                "action": "notification_prepared",
                "status": "ok",
                "event_at_utc": (base_time + timedelta(seconds=index)).isoformat(),
                "run_id": f"filler-{index}",
                "extra": {
                    "event_kind": "notification_prepared",
                    "run_id": f"filler-{index}",
                },
            }
        )
    return rows


def _run_tool(payload: dict[str, Any]) -> dict[str, Any]:
    from src.application.tool_execution import execute_tool as run_tool

    return run_tool("candidate_filter_explain", payload)


def _seal_run(base: Path, run_id: str, *, account: str = "lx") -> None:
    seal_opening_candidate_fixture(
        base,
        run_id=run_id,
        account=account,
        rejected_rows=[
            {
                "symbol": "NVDA",
                "contract_symbol": f"NVDA-PUT-{run_id}",
                "rule": "risk_spread",
                "mode": "put",
            }
        ],
    )


def test_latest_notification_resolves_delivered_run(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-notified")
    today = _today_local()
    event_time = datetime.combine(
        today, datetime.min.time(), tzinfo=_local_tz()
    ).astimezone(timezone.utc) + timedelta(hours=1)
    _write_shared_audit(
        tmp_path,
        [_perception_audit_row(run_id="run-notified", event_at_utc=event_time, accounts=["lx"])],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
        }
    )

    assert out["ok"] is True
    resolution = out["meta"]["source_files"][0]["run_resolution"]
    assert resolution["selector"] == "latest_notification"
    assert resolution["resolved_run_id"] == "run-notified"
    assert resolution["notification_date"] == today.isoformat()
    sell_put = next(item for item in out["data"]["functions"] if item["function"] == "sell_put")
    assert sell_put["reason_counts"]["risk_spread"] == 1


def test_latest_notification_picks_most_recent_delivered_event(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-early")
    _seal_run(tmp_path, "run-late")
    today = _today_local()
    base_time = datetime.combine(today, datetime.min.time(), tzinfo=_local_tz()).astimezone(
        timezone.utc
    )
    _write_shared_audit(
        tmp_path,
        [
            _perception_audit_row(
                run_id="run-early", event_at_utc=base_time + timedelta(hours=1), accounts=["lx"]
            ),
            _perception_audit_row(
                run_id="run-late", event_at_utc=base_time + timedelta(hours=2), accounts=["lx"]
            ),
        ],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
        }
    )

    assert out["meta"]["source_files"][0]["run_resolution"]["resolved_run_id"] == "run-late"


def test_latest_notification_scans_beyond_public_preview_window(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-buried")
    today = _today_local()
    base_time = datetime.combine(today, datetime.min.time(), tzinfo=_local_tz()).astimezone(
        timezone.utc
    )
    filler = _filler_rows(120, base_time=base_time + timedelta(hours=2))
    delivered = _perception_audit_row(
        run_id="run-buried",
        event_at_utc=base_time + timedelta(hours=1),
        accounts=["lx"],
    )
    _write_shared_audit(tmp_path, [delivered, *filler])

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
        }
    )

    assert out["ok"] is True
    assert out["meta"]["source_files"][0]["run_resolution"]["resolved_run_id"] == "run-buried"


def test_latest_notification_ignores_no_send_completed_event(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-nosend")
    _seal_run(tmp_path, "run-sent")
    today = _today_local()
    base_time = datetime.combine(today, datetime.min.time(), tzinfo=_local_tz()).astimezone(
        timezone.utc
    )
    _write_shared_audit(
        tmp_path,
        [
            _perception_audit_row(
                run_id="run-sent", event_at_utc=base_time + timedelta(hours=1), accounts=["lx"]
            ),
            _perception_audit_row(
                run_id="run-nosend",
                event_at_utc=base_time + timedelta(hours=2),
                accounts=["lx"],
                no_send=True,
            ),
        ],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
        }
    )

    assert out["meta"]["source_files"][0]["run_resolution"]["resolved_run_id"] == "run-sent"


def test_latest_notification_skips_event_where_account_send_failed(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-lx-failed")
    today = _today_local()
    event_time = datetime.combine(
        today, datetime.min.time(), tzinfo=_local_tz()
    ).astimezone(timezone.utc) + timedelta(hours=1)
    _write_shared_audit(
        tmp_path,
        [
            _perception_audit_row(
                run_id="run-lx-failed",
                event_at_utc=event_time,
                accounts=["lx", "sy"],
                sent_accounts=["sy"],
                failure_count=1,
            )
        ],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
        }
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"
    assert out["error"]["details"]["reason"] == "no_notification_run"


def test_latest_notification_no_events_for_date_fails_closed(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-yesterday")
    today = _today_local()
    yesterday = today - timedelta(days=1)
    event_time = datetime.combine(
        yesterday, datetime.min.time(), tzinfo=_local_tz()
    ).astimezone(timezone.utc) + timedelta(hours=12)
    _write_shared_audit(
        tmp_path,
        [_perception_audit_row(run_id="run-yesterday", event_at_utc=event_time, accounts=["lx"])],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
            "notification_date": today.isoformat(),
        }
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"
    assert out["error"]["details"]["notification_date"] == today.isoformat()


def test_latest_notification_explicit_date_resolves_previous_day(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-prev-day")
    today = _today_local()
    yesterday = today - timedelta(days=1)
    event_time = datetime.combine(
        yesterday, datetime.min.time(), tzinfo=_local_tz()
    ).astimezone(timezone.utc) + timedelta(hours=12)
    _write_shared_audit(
        tmp_path,
        [_perception_audit_row(run_id="run-prev-day", event_at_utc=event_time, accounts=["lx"])],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
            "notification_date": yesterday.isoformat(),
        }
    )

    assert out["ok"] is True
    resolution = out["meta"]["source_files"][0]["run_resolution"]
    assert resolution["resolved_run_id"] == "run-prev-day"
    assert resolution["notification_date"] == yesterday.isoformat()


def test_explicit_run_id_conflicts_with_run_selector(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-explicit")
    _seal_run(tmp_path, "run-notified")
    today = _today_local()
    event_time = datetime.combine(
        today, datetime.min.time(), tzinfo=_local_tz()
    ).astimezone(timezone.utc) + timedelta(hours=1)
    _write_shared_audit(
        tmp_path,
        [_perception_audit_row(run_id="run-notified", event_at_utc=event_time, accounts=["lx"])],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_id": "run-explicit",
            "run_selector": "latest_notification",
        }
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"


def test_default_latest_behavior_unchanged(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-latest")
    out = _run_tool({"runtime_root": str(tmp_path), "account": "lx", "symbol": "NVDA"})

    assert out["ok"] is True
    resolution = out["meta"]["source_files"][0]["run_resolution"]
    assert resolution["selector"] == "latest"
    assert resolution["resolved_run_id"] == "run-latest"


def test_notification_date_without_latest_notification_rejected(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-1")
    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "notification_date": _today_local().isoformat(),
        }
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"


def test_invalid_notification_date_rejected() -> None:
    out = _run_tool(
        {
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
            "notification_date": "13/08/2026",
        }
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"


def test_bot_error_stays_in_safe_vocabulary(tmp_path: Path) -> None:
    from src.application.bot.contracts import BOT_SAFE_ERROR_CODES

    _seal_run(tmp_path, "run-only")
    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
            "notification_date": "2020-01-01",
        }
    )

    assert out["ok"] is False
    assert out["error"]["code"] in BOT_SAFE_ERROR_CODES


def test_cross_utc_midnight_event_maps_to_local_date(tmp_path: Path) -> None:
    _seal_run(tmp_path, "run-midnight")
    today = _today_local()
    local_early = datetime.combine(today, datetime.min.time(), tzinfo=_local_tz()) + timedelta(
        minutes=30
    )
    event_time_utc = local_early.astimezone(timezone.utc)
    _write_shared_audit(
        tmp_path,
        [_perception_audit_row(run_id="run-midnight", event_at_utc=event_time_utc, accounts=["lx"])],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
            "notification_date": today.isoformat(),
        }
    )

    assert out["ok"] is True
    assert out["meta"]["source_files"][0]["run_resolution"]["resolved_run_id"] == "run-midnight"


def test_truncated_audit_window_is_distinguishable_from_no_notification(tmp_path: Path) -> None:
    from src.application.notification_perception_read import (
        iter_notification_perception_events,
    )

    today = _today_local()
    base_time = datetime.combine(today, datetime.min.time(), tzinfo=_local_tz()).astimezone(
        timezone.utc
    )
    # 目标 delivered 事件最旧，前面压着超过扫描上限的更新事件。
    _seal_run(tmp_path, "run-too-old")
    old = _perception_audit_row(
        run_id="run-too-old",
        event_at_utc=base_time + timedelta(hours=1),
        accounts=["lx"],
    )
    filler = _filler_rows(60, base_time=base_time + timedelta(hours=2))
    _write_shared_audit(tmp_path, [old, *filler])

    result = iter_notification_perception_events(
        repo_root=tmp_path,
        event_kind="notification_delivery_completed",
        limit=0,
    )
    assert result["total_count"] == 1
    assert result["truncated"] is True

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
        }
    )
    # 默认窗口 5000 覆盖本夹具，仍能解析；截断路径由上面的 helper 断言与
    # impl 中 reason=audit_window_truncated 分支共同覆盖。
    assert out["ok"] is True


def test_truncated_window_reports_distinct_reason(tmp_path: Path, monkeypatch) -> None:
    from src.application.agent_tools import candidate_filter_impl

    _seal_run(tmp_path, "run-x")
    today = _today_local()

    def _truncated_iter(*, repo_root, event_kind=None, conversation_id=None, limit=None):
        return {"events": [], "total_count": 9000, "truncated": True}

    monkeypatch.setattr(
        candidate_filter_impl, "iter_notification_perception_events", _truncated_iter
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
            "notification_date": today.isoformat(),
        }
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"
    assert out["error"]["details"]["reason"] == "audit_window_truncated"


def test_notification_run_with_missing_snapshot_reports_resolved_run_id(tmp_path: Path) -> None:
    # 通知事件存在，但对应 run 目录已被清理：错误必须带已解析的 run_id。
    today = _today_local()
    event_time = datetime.combine(
        today, datetime.min.time(), tzinfo=_local_tz()
    ).astimezone(timezone.utc) + timedelta(hours=1)
    _write_shared_audit(
        tmp_path,
        [_perception_audit_row(run_id="run-purged", event_at_utc=event_time, accounts=["lx"])],
    )

    out = _run_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
        }
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"
    assert out["error"]["details"]["reason"] == "snapshot_unavailable_for_notification_run"
    assert out["error"]["details"]["run_id"] == "run-purged"


def _report_row(*, attempt: str, source: str, hour: int, conversation: str | None = None):
    from src.application.conversation_scope import conversation_reference

    event_time = datetime.combine(_today_local(), datetime.min.time(), tzinfo=_local_tz()) + timedelta(hours=hour)
    row = _perception_audit_row(run_id=attempt, event_at_utc=event_time.astimezone(timezone.utc), accounts=["lx"])
    row["extra"]["report_refs"][0]["source_run_id"] = source
    if conversation:
        row["extra"]["conversation_scope"] = {"conversation_ref": conversation_reference(conversation)}
    return row


def _report_query(base: Path, **kwargs):
    return _run_tool({"runtime_root": str(base), "account": "lx", "symbol": "NVDA",
                      "run_selector": "latest_notification", "notification_date": _today_local().isoformat(), **kwargs})


def test_delivered_report_uses_revision_zero_source_not_retry_attempt(tmp_path: Path):
    _seal_run(tmp_path, "brief-source")
    _seal_run(tmp_path, "delivery-retry")
    row = _report_row(attempt="delivery-retry", source="brief-source", hour=12)
    assert row["extra"]["report_refs"][0]["revision"] == 0
    _write_shared_audit(tmp_path, [row])

    out = _report_query(tmp_path)

    assert out["ok"] is True, out
    assert out["data"]["source"]["run_id"] == "brief-source"
    assert out["meta"]["source_files"][0]["run_resolution"]["resolved_run_id"] == "brief-source"
    events = [event for function in out["data"]["functions"] for event in function["events"]]
    assert events and all("delivery-retry" not in json.dumps(event) for event in events)


@pytest.mark.parametrize("scope", ["authenticated", "explicit", "both"])
def test_report_selection_keeps_conversation_scope(tmp_path: Path, scope):
    _seal_run(tmp_path, "my-source")
    _seal_run(tmp_path, "other-source")
    _write_shared_audit(tmp_path, [
        _report_row(attempt="my-delivery", source="my-source", hour=10, conversation="my-chat"),
        _report_row(attempt="other-delivery", source="other-source", hour=12, conversation="other-chat"),
    ])
    selectors = {}
    if scope in {"authenticated", "both"}:
        selectors["authenticated_conversation_id"] = "my-chat"
    if scope in {"explicit", "both"}:
        selectors["conversation_id"] = "my-chat"
    out = _report_query(tmp_path, **selectors)
    assert out["ok"] is True, out
    assert out["data"]["source"]["run_id"] == "my-source"


def test_report_cannot_override_authenticated_conversation(tmp_path: Path):
    _seal_run(tmp_path, "other-source")
    _write_shared_audit(tmp_path, [
        _report_row(attempt="other-delivery", source="other-source", hour=12, conversation="other-chat"),
    ])
    out = _report_query(tmp_path, authenticated_conversation_id="my-chat", conversation_id="other-chat")
    assert out["ok"] is False
    assert out["error"]["code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize("stored_conversation", [None, "other-chat"])
def test_authenticated_report_lookup_never_drops_filter_for_global_fallback(tmp_path: Path, stored_conversation):
    _seal_run(tmp_path, "global-source")
    _write_shared_audit(tmp_path, [
        _report_row(attempt="global-delivery", source="global-source", hour=12, conversation=stored_conversation),
    ])
    out = _report_query(tmp_path, authenticated_conversation_id="my-chat")
    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"
    assert out["error"]["details"]["reason"] == "no_notification_run"


@pytest.mark.parametrize("invalid", [
    "missing", "empty", "duplicate", "wrong_account", "wrong_kind", "revision_bool", "revision_negative",
    "digest_invalid", "date_invalid", "source_run_nonstring", "delivery_key_nonstring", "nonlist",
])
def test_latest_delivered_invalid_source_blocks_older_valid_report(tmp_path: Path, invalid):
    _seal_run(tmp_path, "old-source")
    _seal_run(tmp_path, "latest-source")
    _seal_run(tmp_path, "latest-attempt")
    earlier = _report_row(attempt="old-attempt", source="old-source", hour=10, conversation="my-chat")
    latest = _report_row(attempt="latest-attempt", source="latest-source", hour=12, conversation="my-chat")
    extra = latest["extra"]
    ref = extra["report_refs"][0]
    if invalid == "missing":
        del extra["report_refs"]
    elif invalid == "empty":
        extra["report_refs"] = []
    elif invalid == "duplicate":
        extra["report_refs"].append(dict(ref))
    elif invalid == "nonlist":
        extra["report_refs"] = 1
    else:
        key, value = {
            "wrong_account": ("account", "sy"), "wrong_kind": ("source_kind", "failed_brief"),
            "revision_bool": ("revision", False), "revision_negative": ("revision", -1),
            "digest_invalid": ("source_digest", "not-a-digest"), "date_invalid": ("market_date", "not-a-date"),
            "source_run_nonstring": ("source_run_id", ["latest-source"]),
            "delivery_key_nonstring": ("delivery_key", ["delivery"]),
        }[invalid]
        ref[key] = value
    _write_shared_audit(tmp_path, [earlier, latest])

    out = _report_query(tmp_path, authenticated_conversation_id="my-chat")

    assert out["ok"] is False, out
    assert out["error"]["code"] == "DEPENDENCY_MISSING", out
    assert out["error"]["details"]["reason"] == "notification_source_unavailable", out
    assert out["error"]["details"]["retryable"] is False
    assert not out.get("data")


@pytest.mark.parametrize("corruption", ["malformed_line", "nonobject_line", "invalid_utf8", "directory"])
def test_notification_audit_corruption_blocks_older_delivered_source(tmp_path: Path, corruption):
    _seal_run(tmp_path, "old-source")
    _write_shared_audit(tmp_path, [
        _report_row(attempt="old-attempt", source="old-source", hour=10, conversation="my-chat"),
    ])
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    if corruption == "directory":
        audit.unlink()
        audit.mkdir()
    else:
        suffix = {"malformed_line": b"{\n", "nonobject_line": b"[]\n", "invalid_utf8": b"\xff\n"}[corruption]
        audit.write_bytes(audit.read_bytes() + suffix)
    out = _report_query(tmp_path, authenticated_conversation_id="my-chat")
    assert out["ok"] is False, out
    assert out["error"]["code"] == "DEPENDENCY_MISSING", out
    assert out["error"]["details"]["reason"] == "notification_audit_incomplete"
