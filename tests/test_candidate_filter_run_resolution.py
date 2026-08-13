from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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


def test_explicit_run_id_wins_over_run_selector(tmp_path: Path) -> None:
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

    assert out["ok"] is True
    resolution = out["meta"]["source_files"][0]["run_resolution"]
    assert resolution["selector"] == "explicit_run_id"
    assert resolution["resolved_run_id"] == "run-explicit"


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


def test_copilot_error_stays_in_safe_vocabulary(tmp_path: Path) -> None:
    from src.application.copilot.contracts import COPILOT_SAFE_ERROR_CODES

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
    assert out["error"]["code"] in COPILOT_SAFE_ERROR_CODES


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

    def _truncated_iter(*, repo_root, event_kind=None, limit=None):
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
