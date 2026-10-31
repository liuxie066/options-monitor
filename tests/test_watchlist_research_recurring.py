"""An open-ended schedule reuses the real daily collector, never past quotes."""

from datetime import datetime, timezone
import json

import pytest

from src.application.research import watchlist_day as daily
from test_watchlist_research_day import calendar_job, job, setup, write  # noqa: F401


@pytest.fixture
def recurring(calendar_job):
    args, request, state = calendar_job
    root = args["request_file"].parent
    schedule = {
        "schema_version": "research.watchlist-schedule.v2",
        "start_date": "2026-10-30",
        "timezone": "America/New_York",
        "calendar_markets": ["US"],
        "inputs": {key: request[key] for key in ("scan_input", "policy", "discovery_budget", "observation_budget")},
        "discovery_windows": [{"timezone": "America/New_York", "start_local": "09:00", "end_local": "12:00"}],
        "observation_windows": [{"timezone": "America/New_York", "start_local": "12:55", "end_local": "16:00"}],
    }
    ref = write(root, "recurring.json", schedule)
    options = {
        key: value for key, value in args.items() if key not in {"request_file", "expected_request_sha256", "day_dir"}
    }
    options.update(
        schedule_file=root / "recurring.json",
        expected_schedule_sha256=ref["sha256"],
        archive_root=root.parent / "recurring",
    )
    return options, schedule, state


def test_recurring_has_no_expiry_and_preview_is_offline(recurring):
    args, _, state = recurring
    state.now = datetime(2027, 1, 8, 15, tzinfo=timezone.utc)
    result = daily.run_watchlist_schedule(**args)
    assert result["status"] == "preview" and result["recurring"] is True
    assert result["active_days"] == ["2027-01-08"]
    assert not args["archive_root"].exists() and not state.calls


def test_recurring_calendar_quote_and_tracking_continue_after_dst(recurring):
    args, _, state = recurring
    first = daily.run_watchlist_schedule(**args, confirm=True)
    assert first["status"] == "checked" and len(first["results"]) == 1
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert sum(call[0] == "snapshot" for call in state.calls) == 2
    before = list(state.calls)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert state.calls == before
    state.now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    state.calendar["rows"] = [{"time": "2026-11-02", "trade_date_type": "WHOLE"}]
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert result["results"][0]["date"] == "2026-11-02"
    state.now = datetime(2026, 11, 2, 20, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert sum(call[0] == "snapshot" for call in state.calls) == 4
    root = args["archive_root"]
    friday = json.loads((root / "2026-10-30/prepared/scope/tracking.json").read_bytes())
    monday = json.loads((root / "2026-11-02/prepared/scope/tracking.json").read_bytes())
    assert {row["option_code"] for row in friday["contracts"]} <= {row["option_code"] for row in monday["contracts"]}
    assert (root / "recurring/gaps/2026-10-31.json").is_file()
    assert (root / "recurring/gaps/2026-11-01.json").is_file()
    assert [call[0] for call in state.calls].count("calendar") == 2
    plan = json.loads((root / "2026-11-02/prepared/plan/watchlist-series.json").read_bytes())
    assert [slot["expected_at_utc"] for slot in plan["slots"]] == ["2026-11-02T20:55:00Z"]


def test_recurring_does_not_query_outside_windows_or_replay_a_missed_day(recurring):
    args, _, state = recurring
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert not result["results"] and not state.calls
    assert (args["archive_root"] / "recurring/gaps/2026-10-30.json").is_file()
    state.now = datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert not state.calls


@pytest.mark.parametrize("change", ["external_sha", "input_sha", "window", "markets"])
def test_invalid_recurring_contract_never_calls_provider(recurring, change):
    args, value, state = recurring
    if change == "external_sha":
        args["expected_schedule_sha256"] = "0" * 64
    else:
        if change == "input_sha":
            value["inputs"]["scan_input"]["sha256"] = "0" * 64
        elif change == "window":
            value["discovery_windows"][0]["end_local"] = "08:00"
        else:
            value["calendar_markets"] = []
        args["expected_schedule_sha256"] = write(args["schedule_file"].parent, args["schedule_file"].name, value)[
            "sha256"
        ]
    with pytest.raises(ValueError):
        daily.run_watchlist_schedule(**args, confirm=True)
    assert not state.calls and not args["archive_root"].exists()


def test_recurring_public_cli_uses_same_preview_and_service(recurring):
    import argparse
    from src.interfaces.cli.research import add_research_commands, handle_research_command

    args, _, state = recurring
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    command = ["research", "watchlist-schedule-run"]
    for flag, key in (
        ("schedule", "schedule_file"),
        ("expected-schedule-sha256", "expected_schedule_sha256"),
        ("archive-root", "archive_root"),
        ("runtime-root", "runtime_root"),
        ("opend-host", "host"),
        ("opend-port", "port"),
    ):
        command += ["--" + flag, str(args[key])]
    command += ["--budget-root", str(args["budget_roots"][0]), "--prune-root", str(args["prune_roots"][0])]
    result = handle_research_command(parser.parse_args(command))
    assert result["ok"] and result["data"]["recurring"] is True
    assert not state.calls and not args["archive_root"].exists()


@pytest.mark.parametrize("half_day", [False, True])
def test_recurring_asian_clock_keeps_previous_us_market_date(recurring, half_day):
    args, value, state = recurring
    value["timezone"] = "Asia/Hong_Kong"
    value["start_date"] = "2026-11-02"
    args["expected_schedule_sha256"] = write(args["schedule_file"].parent, args["schedule_file"].name, value)["sha256"]
    state.now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    state.calendar["rows"] = [{"time": "2026-11-02", "trade_date_type": "MORNING" if half_day else "WHOLE"}]
    daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 11, 2, 17 if half_day else 20, 55, 1, tzinfo=timezone.utc)
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert result["results"][0]["date"] == "2026-11-02"
    assert sum(call[0] == "snapshot" for call in state.calls) == 2


def test_recurring_changed_contract_or_derived_request_is_rejected(recurring):
    args, value, state = recurring
    daily.run_watchlist_schedule(**args, confirm=True)
    before = list(state.calls)
    request_path = args["archive_root"] / "recurring/requests/2026-10-30/request.json"
    original = request_path.read_bytes()
    data = json.loads(original)
    data["previous_day"] = {"path": "/unapproved", "request_sha256": "0" * 64}
    request_path.write_bytes(daily.series._bytes(data))
    with pytest.raises(ValueError, match="binding"):
        daily.run_watchlist_schedule(**args, confirm=True)
    request_path.write_bytes(original)
    value["discovery_windows"][0]["start_local"] = "08:00"
    args["expected_schedule_sha256"] = write(args["schedule_file"].parent, args["schedule_file"].name, value)["sha256"]
    with pytest.raises(ValueError, match="immutable"):
        daily.run_watchlist_schedule(**args, confirm=True)
    assert state.calls == before


def test_recurring_storage_limit_prevents_any_request(recurring):
    args, value, state = recurring
    root = args["schedule_file"].parent
    for name in ("discovery_budget", "observation_budget"):
        budget = json.loads((root / value["inputs"][name]["path"]).read_bytes())
        budget["max_bytes"] = 1
        value["inputs"][name] = write(root, name + "-tiny.json", budget)
    args["expected_schedule_sha256"] = write(root, args["schedule_file"].name, value)["sha256"]
    with pytest.raises(ValueError, match="storage budget"):
        daily.run_watchlist_schedule(**args, confirm=True)
    assert not state.calls and not args["archive_root"].exists()


def test_recurring_failed_day_does_not_erase_tracking_or_retry_provider(recurring):
    args, _, state = recurring
    daily.run_watchlist_schedule(**args, confirm=True)
    before = list(state.calls)
    state.now = datetime(2026, 10, 31, 15, tzinfo=timezone.utc)
    state.calendar = TimeoutError("fixture calendar failure")
    with pytest.raises(ValueError, match="calendar unavailable"):
        daily.run_watchlist_schedule(**args, confirm=True)
    after = list(state.calls)
    assert len(after) == len(before) + 1
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert result["results"][0]["status"] == "abandoned" and state.calls == after
    state.now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    state.calendar = {
        "retcode": 0,
        "coverage_complete": True,
        "pagination_complete": True,
        "page_count": 1,
        "rows": [{"time": "2026-11-02", "trade_date_type": "WHOLE"}],
    }
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert result["results"][0]["date"] == "2026-11-02"
    previous = json.loads((args["archive_root"] / "recurring/requests/2026-11-02/request.json").read_bytes())[
        "previous_day"
    ]
    assert previous["path"] == str(args["archive_root"] / "2026-10-30/prepared")
    assert any(gap["date"] == "2026-10-31" for gap in result["continuity_gaps"])


def test_recurring_still_collects_more_than_three_days_after_start(recurring):
    args, _, state = recurring
    daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
    state.calendar["rows"] = [{"time": "2026-11-05", "trade_date_type": "WHOLE"}]
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert result["results"][0]["manifest"]["date"] == "2026-11-05"
    state.now = datetime(2026, 11, 5, 20, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert sum(call[0] == "snapshot" for call in state.calls) == 2
    assert len(list((args["archive_root"] / "recurring/gaps").glob("*.json"))) == 5
