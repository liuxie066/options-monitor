from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from src.application.runtime_status_cli import format_runtime_status_summary
from src.application.agent_tools.runtime_status_impl import (
    _aggregate_trade_intake_summaries,
    _notification_diagnosis,
    _notification_delivery_health,
)
from src.interfaces.cli.observability_ops import handle_observability_command


def test_format_runtime_status_summary_shows_trade_intake_sources() -> None:
    out = format_runtime_status_summary(
        {
            "ok": True,
            "data": {
                "summary": {"ok": True},
                "trade_intake": {
                    "enabled": True,
                    "mode": "apply",
                    "summary": {
                        "listener_status": "listening",
                        "processed_count": 1,
                        "failed_count": 0,
                        "unresolved_count": 0,
                    },
                    "sources": [
                        {
                            "id": "lx",
                            "account": "lx",
                            "host": "127.0.0.1",
                            "port": 11111,
                            "summary": {"listener_status": "listening"},
                        },
                        {
                            "id": "sy",
                            "account": "sy",
                            "host": "127.0.0.1",
                            "port": 11112,
                            "summary": {"listener_status": "listening"},
                        },
                    ],
                },
            },
            "warnings": [],
        }
    )

    assert "trade intake:" in out
    assert "sources=lx:listening@127.0.0.1:11111, sy:listening@127.0.0.1:11112" in out


def test_format_runtime_status_journal_summary_is_bounded_for_large_unicode_warnings() -> None:
    from src.application.runtime_status_cli import format_runtime_status_journal_summary

    out = format_runtime_status_journal_summary(
        {
            "ok": False,
            "data": {
                "summary": {"ok": False, "warning_count": 100},
                "config": {
                    "config_key": "us\nextra",
                    "config_path": "/tmp/config\ninjected-line",
                    "accounts": ["lx", "sy"],
                },
                "ledger_store": {"warnings": ["账本警告\n第二行" * 500]},
            },
            "error": {"code": "FAILED", "message": "错误" * 10000},
            "warnings": [(f"warning-{index}\n" + "警告" * 1000) for index in range(100)],
        }
    )

    assert len(out.splitlines()) <= 20
    assert len(out.encode("utf-8")) <= 16 * 1024
    assert "warnings: count=101" in out
    assert "\n第二行" not in out
    assert "\ninjected-line" not in out


def test_format_runtime_status_journal_summary_keeps_default_summary_unbounded() -> None:
    from src.application.runtime_status_cli import format_runtime_status_journal_summary

    envelope = {
        "ok": True,
        "data": {"summary": {"ok": True}},
        "warnings": ["first", "second"],
    }

    journal = format_runtime_status_journal_summary(envelope)
    default = format_runtime_status_summary(envelope)

    assert "warnings: count=2 first=first" in journal
    assert "- first" in default
    assert "- second" in default


def test_notification_delivery_has_separate_degraded_state_for_unknown_and_receipt() -> None:
    diagnosis = {"status": "unknown", "scheduler_should_notify": True}
    assert _notification_delivery_health(diagnosis, {}) == {
        "status": "degraded",
        "reason_codes": ["NOTIFICATION_EVIDENCE_UNKNOWN"],
        "expected": True,
    }
    confirmed = {"status": "sent", "send_confirmed_count": 1}
    assert _notification_delivery_health(confirmed, {})["status"] == "confirmed"
    trade = {"summary": {"last_receipt_result": {"status": "unresolved"}}}
    assert _notification_delivery_health(confirmed, trade)["reason_codes"] == ["TRADE_RECEIPT_UNCONFIRMED"]


def test_trade_intake_aggregation_follows_latest_source_events_in_either_order() -> None:
    older = {
        "listener_status": "listening", "listener_stage": "ready",
        "last_heartbeat_utc": "2026-01-01T00:00:00Z",
        "last_push_received_utc": "2026-01-01T00:01:00Z", "last_push_deal_id": "old",
        "last_backfill_check_utc": "2026-01-01T00:03:00Z",
        "last_backfill_window_start_utc": "old-start", "last_backfill_error": "old-error",
        "last_backfill_result": {"status": "failed"},
        "last_backfill_deal_count": 4, "last_backfill_applied_count": 4,
        "last_backfill_skipped_duplicate_count": 4, "last_backfill_failed_count": 4,
        "last_backfill_unresolved_count": 4,
        "last_deal_result": {"status": "old"},
        "last_receipt_result": {"status": "unresolved"},
    }
    newer = {
        "listener_status": "listening", "listener_stage": "recovering",
        "last_heartbeat_utc": "2026-01-01T00:04:00+00:00",
        "last_push_received_utc": "2026-01-01T01:02:00+01:00", "last_push_deal_id": "new",
        "last_backfill_check_utc": "2026-01-01T00:05:00Z",
        "last_backfill_window_start_utc": "new-start", "last_backfill_error": None,
        "last_backfill_result": {"status": "applied"},
        "last_backfill_deal_count": 1, "last_backfill_applied_count": 1,
        "last_backfill_skipped_duplicate_count": 1, "last_backfill_failed_count": 1,
        "last_backfill_unresolved_count": 1,
        "last_deal_result": {"status": "new"},
        "last_receipt_result": {"status": "sent"},
    }
    for sources in ([older, newer], [newer, older]):
        trade = {"sources": [{"summary": item} for item in sources]}
        summary = _aggregate_trade_intake_summaries(sources)
        assert summary["last_heartbeat_utc"] == newer["last_heartbeat_utc"]
        assert summary["last_push_deal_id"] == "new"
        assert summary["last_backfill_window_start_utc"] == "new-start"
        assert summary["last_backfill_error"] is None
        assert summary["last_backfill_result"] == {"status": "applied"}
        for key in (
            "last_backfill_deal_count", "last_backfill_applied_count",
            "last_backfill_skipped_duplicate_count", "last_backfill_failed_count",
            "last_backfill_unresolved_count",
        ):
            assert summary[key] == 1
        assert summary["listener_stage"] is None
        assert summary["last_deal_result"] is None
        assert summary["last_receipt_result"] is None
        trade["summary"] = summary
        assert "TRADE_RECEIPT_UNCONFIRMED" in _notification_delivery_health(
            {"status": "sent"}, trade,
        )["reason_codes"]


def test_trade_intake_aggregation_nulls_conflicting_equal_time_events() -> None:
    first = {
        "last_push_received_utc": "2026-01-01T00:01:00Z", "last_push_deal_id": "a",
        "last_backfill_check_utc": "2026-01-01T00:02:00Z",
        "last_backfill_applied_count": 1, "last_backfill_result": {"status": "a"},
        "last_fee_attempted_at_ms": 1000, "last_fee_error": "a",
    }
    second = {
        "last_push_received_utc": "2026-01-01T01:01:00+01:00", "last_push_deal_id": "b",
        "last_backfill_check_utc": "2026-01-01T01:02:00+01:00",
        "last_backfill_applied_count": 4, "last_backfill_result": {"status": "b"},
        "last_fee_attempted_at_ms": 1000, "last_fee_error": "b",
    }
    for sources in ([first, second], [second, first]):
        summary = _aggregate_trade_intake_summaries(sources)
        assert summary["last_push_deal_id"] is None
        assert summary["last_backfill_applied_count"] is None
        assert summary["last_backfill_result"] is None
        assert summary["last_fee_attempted_at_ms"] == 1000
        assert summary["last_fee_error"] is None


def test_notification_delivery_is_visible_alongside_existing_overall_failure() -> None:
    envelope = {
        "ok": True,
        "data": {
            "summary": {"ok": False, "warning_codes": ["NOTIFICATION_ROUTE_MISSING"]},
            "notification_delivery": {
                "status": "degraded", "reason_codes": ["NOTIFICATION_ROUTE_MISSING"],
            },
        },
    }
    text = format_runtime_status_summary(envelope)
    assert "overall: FAIL" in text
    assert "notification delivery: status=degraded reasons=NOTIFICATION_ROUTE_MISSING" in text


def test_system_alert_fallback_and_unconfirmed_state_are_visible_in_status() -> None:
    envelope = {"ok": True, "data": {"summary": {"ok": False},
        "system_alert_delivery": {"status": "degraded", "reason_code": "SYSTEM_ALERT_DELIVERY_UNCONFIRMED",
                                  "provider": "feishu_app", "fallback_used": True, "active_count": 1}}}
    text = format_runtime_status_summary(envelope)
    assert "system alert delivery: status=degraded provider=feishu_app fallback=yes active=1" in text


def test_status_journal_emits_error_meta_signal_only_when_degraded(capsys) -> None:
    args = argparse.Namespace(command="status", json=False, journal_summary=True)
    delivery = {"status": "degraded", "reason_codes": ["NOTIFICATION_EVIDENCE_UNKNOWN"]}
    execute = lambda _name, _payload: {"ok": True, "data": {"summary": {"ok": True}, "notification_delivery": delivery}}
    assert handle_observability_command(
        args,
        execute_tool_fn=execute,
        runtime_status_payload_from_args_fn=lambda _args: {},
    ) == 0
    output = capsys.readouterr()
    assert "notification delivery: status=degraded" in output.out
    assert "<3>NOTIFICATION_DELIVERY_DEGRADED" in output.err

    delivery["status"] = "confirmed"
    assert handle_observability_command(
        args,
        execute_tool_fn=execute,
        runtime_status_payload_from_args_fn=lambda _args: {},
    ) == 0
    assert capsys.readouterr().err == ""


def test_status_journal_marks_system_alert_delivery_degraded(capsys) -> None:
    args = argparse.Namespace(command="status", json=False, journal_summary=True)
    execute = lambda _name, _payload: {"ok": True, "data": {
        "summary": {"ok": False}, "system_alert_delivery": {
            "status": "degraded", "reason_code": "SYSTEM_ALERT_DELIVERY_UNCONFIRMED",
            "fallback_used": False, "provider": None, "active_count": 1,
        },
    }}
    assert handle_observability_command(
        args, execute_tool_fn=execute, runtime_status_payload_from_args_fn=lambda _args: {},
    ) == 0
    output = capsys.readouterr()
    assert "system alert delivery: status=degraded" in output.out
    assert "<3>SYSTEM_ALERT_DELIVERY_DEGRADED" in output.err


@pytest.mark.parametrize("mode", ["none", "announce"])
@pytest.mark.parametrize("counts, overrides, expected", [
    pytest.param((0, 0, 0), {}, "not_sent", id="no-evidence"),
    pytest.param((0, 0, 0), {"no_send": True}, "no_send", id="no-send-zero"),
    pytest.param((0, 0, 0), {"scheduler_decision": {"should_run_scan": False}}, "scheduler_skipped", id="skip-zero"),
    pytest.param((0, 0, 0), {"account_messages_count": 0, "reason": "no_account_notification"}, "no_notification_content", id="no-content-zero"),
    pytest.param((0, 0, 0), {"missing_route": True}, "notification_route_missing", id="missing-route-zero"),
    pytest.param((0, 0, 1), {}, "not_sent", id="failed-only"),
    pytest.param((-1, 0, 1), {}, "not_sent", id="negative-attempts"),
    pytest.param((None, "", 0), {}, "not_sent", id="falsey-counts"),
    pytest.param((1, 1, 0), {"no_send": True}, "no_send", id="no-send-with-receipt"),
    pytest.param((1, 1, 0), {"scheduler_decision": {"should_run_scan": False}}, "scheduler_skipped", id="skip-with-receipt"),
    pytest.param((1, 1, 0), {"account_messages_count": 0, "reason": "no_account_notification"}, "no_notification_content", id="no-content-with-receipt"),
    pytest.param((1, 1, 0), {"no_send": True, "scheduler_decision": {"should_run_scan": False}}, "scheduler_skipped", id="skip-before-no-send"),
    pytest.param(("1", "0", 1), {}, "send_failed_or_unconfirmed", id="numeric-string"),
    pytest.param((0, 1, 0), {}, "sent", id="confirmed-without-attempt-count"),
])
def test_outer_mode_preserves_notification_controls(mode, counts, overrides, expected) -> None:
    attempted, confirmed, failed = counts
    metrics = {
        "reason": "not_sent", "account_messages_count": 1,
        "send_attempted_count": attempted, "send_confirmed_count": confirmed,
        "send_failed_count": failed, **overrides,
    }
    cfg = {} if overrides.get("missing_route") else {
        "notifications": {"enabled": True, "provider": "wechat_clawbot", "channel": "wechat_clawbot", "target": "clawbot:fixture"},
    }
    diagnosis = _notification_diagnosis(
        cfg=cfg, shared_last_run={},
        latest_run_payload={"path": "selected", "state": {"tick_metrics": {"json": metrics}}},
        trigger_context={"delivery_mode": mode, "announce_expected": mode == "announce"},
    )
    if mode == "none" and int(attempted or 0) <= 0 and int(confirmed or 0) <= 0:
        expected = "outer_delivery_disabled"
    assert diagnosis["notification_route"]["configured"] is (not overrides.get("missing_route", False))
    assert diagnosis["status"] == expected
    assert diagnosis["send_attempted_count"] == int(attempted or 0)
    assert diagnosis["send_confirmed_count"] == int(confirmed or 0)
    assert diagnosis["send_failed_count"] == failed
    assert diagnosis["outer_delivery_mode"] == mode
    assert diagnosis["outer_announce_expected"] is (mode == "announce")


@pytest.mark.parametrize("mode", ["none", "announce"])
@pytest.mark.parametrize("key", ["send_attempted_count", "send_confirmed_count", "send_failed_count"])
def test_outer_mode_does_not_change_invalid_count_conversion(mode, key) -> None:
    with pytest.raises(ValueError):
        _notification_diagnosis(
            cfg={}, shared_last_run={}, trigger_context={"delivery_mode": mode},
            latest_run_payload={"path": "selected", "state": {"tick_metrics": {"json": {key: "invalid"}}}},
        )


@pytest.mark.parametrize("mode", ["none", "announce"])
@pytest.mark.parametrize("shared_run, local, expected", [
    pytest.param("selected", {}, "sent", id="matching-fallback"),
    pytest.param("different", {}, "unknown", id="unrelated-shared-ignored"),
    pytest.param("selected", {"send_attempted_count": 0, "send_confirmed_count": 0}, "not_sent", id="explicit-local-zero"),
])
def test_outer_mode_preserves_shared_receipt_authority(mode, shared_run, local, expected) -> None:
    diagnosis = _notification_diagnosis(
        cfg={},
        shared_last_run={"json": {"run_id": shared_run, "send_attempted_count": 2, "send_confirmed_count": 2}},
        latest_run_payload={"path": "output_runs/selected", "state": {"tick_metrics": {"json": local}}},
        trigger_context={"delivery_mode": mode},
    )
    if expected == "sent":
        assert diagnosis["send_confirmed_count"] == 2
    else:
        assert diagnosis["send_attempted_count"] == diagnosis["send_confirmed_count"] == 0
        if mode == "none":
            expected = "outer_delivery_disabled"
    assert diagnosis["status"] == expected


@pytest.mark.parametrize("mode", ["none", "announce"])
@pytest.mark.parametrize("counts, extra, expected", [
    pytest.param((1, 1, 0, 1), {}, "send_failed_or_unconfirmed", id="failed"),
    pytest.param((2, 2, 1, 1), {}, "sent_partial", id="partial"),
    pytest.param((1, 1, 1, 0), {}, "sent", id="confirmed"),
    pytest.param((1, 1, 0, 0), {"ambiguous_send_count": 1}, "send_failed_or_unconfirmed", id="ambiguous"),
    pytest.param((1, 0, 1, 0), {}, "sent", id="confirmed-without-attempt-count"),
    pytest.param((1, 1, 0, 1), {"duplicate_risk_count": 1}, "send_failed_or_unconfirmed", id="failure-and-duplicate"),
    pytest.param((0, 0, 0, 0), {"duplicate_risk_count": 1}, "no_account_sent", id="duplicate-only"),
    pytest.param((0, 0, 0, 0), {}, "no_account_sent", id="explicit-zero"),
    pytest.param((1, 0, 0, 0), {"no_send": True}, "no_send", id="no-send"),
    pytest.param((0, 0, 0, 0), {"should_run_scan": False}, "scheduler_skipped", id="scheduler-skip"),
])
def test_status_reads_native_receipts_before_outer_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, mode, counts, extra, expected,
) -> None:
    from domain.storage.repositories import state_repo
    from src.application.config_yaml import resolve_yaml_runtime_config
    from src.application.cron_runtime import (
        apply_notify_results_to_tick_metrics, build_notify_summary, build_shared_last_run_meta,
    )
    from src.application.research.checks import run_deterministic_checks
    from src.interfaces.cli.main import main

    def write(path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_ENV_FILE", str(env))
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(tmp_path / "absent.sqlite3"))
    root = Path(__file__).resolve().parents[1]
    config, _ = resolve_yaml_runtime_config(
        repo_root=root, market="us", config_path=root / "configs/examples/config.yaml.example",
    )
    config["notifications"] = {"enabled": True, "provider": "wechat_clawbot", "channel": "wechat_clawbot", "target": "clawbot:fixture"}
    config_path = tmp_path / "config.us.json"
    write(config_path, config)
    bot_path = tmp_path / "config.bot.json"
    write(bot_path, {"bot": {"enabled": False}})
    total, attempted, confirmed, failed = counts
    sent = ["lx"] if confirmed else []
    failures = [{"account": "lx", "reason": "synthetic failure"}] * failed
    summary = build_notify_summary(
        sent_accounts=sent, notify_failures=failures, total_accounts=total,
        send_attempted_count=attempted, send_confirmed_count=confirmed,
        ambiguous_send_count=extra.get("ambiguous_send_count"),
        duplicate_risk_count=extra.get("duplicate_risk_count"),
    )
    scan = extra.get("should_run_scan", True)
    metrics = {
        "run_id": "selected", "ran_scan": scan, "no_send": extra.get("no_send", False),
        "scheduler_decision": {"should_run_scan": scan, "is_notify_window_open": scan},
    }
    apply_notify_results_to_tick_metrics(
        tick_metrics=metrics, no_send=metrics["no_send"], sent_accounts=sent,
        notify_failures=failures, notify_summary=summary,
    )
    state_repo.write_tick_metrics(tmp_path, "selected", metrics)
    # Conflicting shared history cannot fill or replace selected-run receipt counts.
    stale_sent = [] if confirmed else ["stale-account"]
    stale_failures = [{"account": "stale-account", "reason": "old failure"}] if confirmed else []
    stale_summary = build_notify_summary(sent_accounts=stale_sent, notify_failures=stale_failures, total_accounts=1)
    stale = build_shared_last_run_meta(
        run_id="different-old-run", now_utc="2020-01-01T00:00:00Z", channel="synthetic", target="fixture",
        results=[], sent_accounts=stale_sent, notify_failures=stale_failures, notify_summary=stale_summary,
    )
    state_repo.write_shared_last_run(tmp_path, stale)
    persisted = [*tmp_path.glob("output_shared/state/**/*.json"), *tmp_path.glob("output_runs/**/*.json")]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in persisted}
    assert len(persisted) == 5
    for path in persisted:
        assert json.loads(path.read_text()) == (stale if "last_run" in path.name else metrics)
    profile_path = tmp_path / "synthetic.profile.json"
    write(profile_path, {
        "runtime_root": str(tmp_path), "config_paths": {"us": str(config_path)}, "accounts": ["lx"],
        "delivery_mode": mode, "env_file": str(env), "bot_config_path": str(bot_path),
        "feishu_ws": {"audit_db": str(tmp_path / "absent.sqlite3")},
        "wechat_clawbot": {"state_dir": str(tmp_path / "absent-wechat")},
        "paths": {key: str(tmp_path / value) for key, value in {
            "report_dir": "output_shared/reports", "state_dir": "output_shared/state",
            "shared_state_dir": "output_shared/state", "accounts_root": "output_accounts", "runs_root": "output_runs",
        }.items()},
    })
    args = ["status", "--config-key", "us", "--config-path", str(config_path), "--profile-path", str(profile_path),
            "--env-file", str(env), "--no-local-env-file", "--run-id", "selected"]
    assert main(args + ["--json"], discover_local=False) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is True
    data = response["data"]
    diagnosis, delivery = data["notification_diagnosis"], data["notification_delivery"]
    if mode == "none" and attempted == confirmed == 0:
        expected = "outer_delivery_disabled"
    assert diagnosis["status"] == expected
    for key in ("send_attempted_count", "send_confirmed_count", "send_failed_count", "ambiguous_send_count", "duplicate_risk_count"):
        assert diagnosis[key] == summary[key]
    assert diagnosis["outer_delivery_mode"] == mode
    assert diagnosis["outer_announce_expected"] is (mode == "announce")
    failure_code = {"sent_partial": "NOTIFICATION_PARTIAL_FAILURE", "send_failed_or_unconfirmed": "NOTIFICATION_DELIVERY_FAILED"}.get(expected)
    codes = ([failure_code] if failure_code else []) + (["NOTIFICATION_DUPLICATE_RISK"] if extra.get("duplicate_risk_count") else [])
    assert delivery["reason_codes"] == codes
    assert delivery["status"] == ("degraded" if codes else "confirmed" if expected == "sent" else "unknown")
    assert all(code in data["summary"]["warning_codes"] for code in codes)
    assert main(args + ["--journal-summary"], discover_local=False) == 0
    journal = capsys.readouterr()
    assert journal.err.count("<3>NOTIFICATION_DELIVERY_DEGRADED") == int(bool(codes))
    research = run_deterministic_checks({"runtime_status": data})
    assert any(f["code"] == "NOTIFICATION_DELIVERY_ISSUE" for f in research["findings"]) is bool(failure_code)
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in persisted}
