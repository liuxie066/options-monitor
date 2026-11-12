from __future__ import annotations

from pathlib import Path


def test_registry_covers_expected_reports_and_migrated_runners() -> None:
    from src.application.reports.registry import REPORT_REGISTRY
    from src.application.reports.runner import RUNNERS

    ids = [item.report_id for item in REPORT_REGISTRY]
    assert ids == ["daily_brief", "close_advice", "code_health", "code_entropy", "config_review"]
    assert len(ids) == len(set(ids))
    assert set(RUNNERS) == {item.report_id for item in REPORT_REGISTRY if item.migrated}
    assert {item.report_id for item in REPORT_REGISTRY if item.migrated} == {"config_review"}


def test_legacy_reports_declare_implementation_and_probe() -> None:
    from src.application.reports.registry import REPORT_REGISTRY

    for definition in REPORT_REGISTRY:
        assert definition.run_entry
        assert definition.render_entries
        assert definition.state_dir
        if not definition.migrated:
            assert definition.legacy_probe is not None
            assert definition.legacy_probe.kind in {"json_object", "jsonl_tail", "daily_brief", "close_advice"}


def test_schedule_payload_exports_config_review_and_legacy_launchd_labels(tmp_path: Path) -> None:
    from src.application.reports.registry import schedule_payload

    payload = schedule_payload(generated_at_utc="2026-11-12T00:00:00Z", home=tmp_path)
    assert payload["schema_version"] == "report_schedule.v1"
    assert payload["timezone"] == "Asia/Shanghai"
    reports = {item["report_id"]: item for item in payload["reports"]}
    config_review = reports["config_review"]["entries"][0]
    assert config_review["frequency"] == "weekly"
    assert config_review["trigger"] == "systemd"
    assert config_review["weekday"] == 7
    assert config_review["hour"] == 20
    assert config_review["minute"] == 0
    assert config_review["timezone"] == "Asia/Shanghai"
    assert "launchd" not in config_review
    assert "options-monitor-report-config-review.timer" in config_review["note"]
    assert config_review["command"] == ["om", "report", "run", "config_review", "--trigger", "schedule"]

    labels = {
        entry["launchd"]["label"]
        for report in payload["reports"]
        for entry in report["entries"]
        if entry.get("launchd")
    }
    assert "local.options-monitor.code-health-review" in labels
    assert "com.options-monitor.code-entropy.daily" in labels
    assert "com.options-monitor.code-entropy.weekly" in labels


def test_schedule_payload_detects_installed_plist(tmp_path: Path) -> None:
    from src.application.reports.registry import schedule_payload

    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "local.options-monitor.code-health-review.plist").write_text("<plist/>", encoding="utf-8")
    payload = schedule_payload(generated_at_utc="2026-11-12T00:00:00Z", home=tmp_path)
    reports = {item["report_id"]: item for item in payload["reports"]}
    entry = reports["code_health"]["entries"][0]
    assert entry["launchd"]["plist_present"] is True


def test_retention_policy_is_separate_from_output_runs() -> None:
    from src.application.reports.registry import report_definition

    definition = report_definition("config_review")
    assert definition is not None
    assert definition.retention.keep_count is None
    assert definition.retention.keep_days is None
    assert "不自动删除" in definition.retention.note
