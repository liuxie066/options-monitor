"""Daily composition exercises the real discovery, planner, limiter and exporter."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shlex
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from domain.domain.option_lifecycle import INSTRUMENT_POLICY_REGISTRY
from src.application import opend_market_snapshot_fetching as snapshots
from src.application.research import watchlist_day as daily, watchlist_series as group
from src.interfaces.cli.research import add_research_commands, handle_research_command
from test_watchlist_research_discovery import setup  # noqa: F401


def write(root, name, value):
    path = root / name
    path.write_bytes(daily.series._bytes(value))
    return {"path": name, "sha256": daily.quote._sha256(path.read_bytes())}


@pytest.fixture
def job(setup, monkeypatch):
    args, state = setup
    root = args["input_file"].parent
    request = {
        "schema_version": daily.REQUEST_SCHEMA,
        "date": "2026-10-30",
        "previous_day": None,
        "scan_input": {"path": "input.json", "sha256": args["expected_input_sha256"]},
        "discovery_window": {"path": "safe.json", "sha256": args["expected_safe_window_sha256"]},
        "discovery_budget": {"path": "budget.json", "sha256": args["expected_budget_sha256"]},
    }
    request["calendar"] = write(
        root,
        "calendar.json",
        {
            "markets": {
                "US": {
                    "start": "2026-10-30",
                    "end": "2026-11-02",
                    "source_observed_at_utc": "2026-10-30T14:00:00Z",
                    "receipt": {
                        "retcode": 0,
                        "coverage_complete": True,
                        "pagination_complete": True,
                        "page_count": 1,
                        "rows": [{"time": day, "trade_date_type": "WHOLE"} for day in ("2026-10-30", "2026-11-02")],
                    },
                }
            }
        },
    )
    request["policy"] = write(root, "policy.json", {"US": INSTRUMENT_POLICY_REGISTRY["US:standard_equity_option"]})
    request["observation_budget"] = write(
        root,
        "quote-budget.json",
        {"schema_version": group.BUDGET_SCHEMA, "max_calls_per_slot": 6, "max_bytes": 100_000_000, "min_free_bytes": 1},
    )
    request["observation_window"] = write(
        root,
        "quote-window.json",
        {
            "schema_version": "research.safe-window.v1",
            "series_plan_sha256": args["expected_input_sha256"],
            "intervals": [{"start_utc": "2026-10-30T19:55:00Z", "end_utc": "2026-10-30T19:56:00Z"}],
        },
    )
    request_ref = write(root, "request.json", request)
    base_factory = args["gateway_factory"]

    class Gateway:
        def __init__(self):
            self.base = base_factory()

        def __getattr__(self, name):
            return getattr(self.base, name)

        def get_snapshot(self, codes):
            state.calls.append(("snapshot", list(codes)))
            return pd.DataFrame(
                [
                    {
                        "code": code,
                        "bid_price": 1,
                        "ask_price": 1.1,
                        "bid_vol": 10,
                        "ask_vol": 11,
                        "update_time": state.now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
                        "last_price": 1.05,
                        "option_contract_size": 100,
                        "option_contract_multiplier": 100,
                        "option_implied_volatility": 30,
                        "option_delta": -0.25,
                        "option_gamma": 0.01,
                        "option_theta": -0.02,
                        "option_vega": 0.1,
                    }
                    for code in codes
                ]
            )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now if tz else state.now.replace(tzinfo=None)

    monkeypatch.setattr(snapshots, "datetime", Clock)
    options = {
        "request_file": root / "request.json",
        "expected_request_sha256": request_ref["sha256"],
        "day_dir": root.parent / "day-friday",
        "budget_roots": [root.parent],
        "runtime_root": args["runtime_root"],
        "prune_roots": args["prune_roots"],
        "host": "fixture.invalid",
        "port": 11111,
        "clock": args["clock"],
        "gateway_factory": lambda **_: Gateway(),
    }
    return options, request, state


def freeze(options, request):
    options["expected_request_sha256"] = write(options["request_file"].parent, options["request_file"].name, request)[
        "sha256"
    ]


@pytest.fixture
def calendar_job(job):
    args, request, state = job
    request["schema_version"] = "research.watchlist-day-request.v2"
    request.pop("calendar")
    request["calendar_query"] = write(
        args["request_file"].parent,
        "calendar-query.json",
        {
            "schema_version": "research.watchlist-calendar-query.v1",
            "markets": ["US"],
            "start": "2026-10-30",
            "end": "2026-11-02",
        },
    )
    freeze(args, request)
    state.calendar = {
        "retcode": 0,
        "coverage_complete": True,
        "pagination_complete": True,
        "page_count": 1,
        "rows": [{"time": "2026-10-30", "trade_date_type": "WHOLE"}],
    }
    base_factory = args["gateway_factory"]

    class Gateway:
        def __init__(self):
            self.base = base_factory()

        def __getattr__(self, name):
            return getattr(self.base, name)

        def get_trading_days_with_receipt(self, **kwargs):
            state.calls.append(("calendar", kwargs))
            if isinstance(state.calendar, Exception):
                raise state.calendar
            return state.calendar

    args["gateway_factory"] = lambda **_: Gateway()
    return args, request, state


def test_calendar_query_is_previewed_offline_then_frozen_once(calendar_job):
    args, _, state = calendar_job
    assert daily.run_watchlist_day(**args)["status"] == "preview"
    assert not state.calls and not args["day_dir"].exists()
    first = daily.run_watchlist_day(**args, confirm=True)
    prepared = args["day_dir"] / "prepared"
    frozen = (prepared / "calendar.json").read_bytes()
    calendar = json.loads(frozen)["markets"]["US"]
    assert calendar["receipt"] == state.calendar
    assert daily.series._utc(calendar["source_observed_at_utc"]) == state.now
    calls = list(state.calls)
    assert calls[0] == ("calendar", {"market": "US", "start": "2026-10-30", "end": "2026-11-02"})
    discovery = json.loads((prepared / "discovery/discovery-manifest.json").read_bytes())
    assert discovery["provider_call_count"] == 5
    assert daily.run_watchlist_day(**args, confirm=True)["manifest"] == first["manifest"]
    assert state.calls == calls and (prepared / "calendar.json").read_bytes() == frozen
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    exported = group.export_watchlist_series(
        series_dir=args["day_dir"] / "archive",
        expected_plan_sha256=first["manifest"]["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=args["day_dir"].parent / "export-calendar-query",
    )
    assert exported["batches"]
    assert json.loads((prepared / "plan/calendar.json").read_bytes())["markets"]["US"] == calendar


@pytest.mark.parametrize("kind", ["provider_failure", "partial", "invalid_session"])
def test_calendar_failure_preserves_response_and_never_retries_or_discovers_chains(calendar_job, kind):
    args, _, state = calendar_job
    if kind == "provider_failure":
        state.calendar = TimeoutError("synthetic calendar failure")
    elif kind == "partial":
        state.calendar["coverage_complete"] = False
    else:
        state.calendar["rows"][0]["trade_date_type"] = "UNKNOWN"
    with pytest.raises(ValueError, match="calendar"):
        daily.run_watchlist_day(**args, confirm=True)
    assert [call[0] for call in state.calls] == ["calendar"] and state.closed
    prepared = args["day_dir"] / "prepared"
    assert (prepared / "discovery/calls/000000/response.json").is_file()
    assert not (prepared / "plan").exists()
    assert daily.run_watchlist_day(**args, confirm=True)["status"] == "abandoned"
    assert len(state.calls) == 1


@pytest.mark.parametrize("change", ["market", "date", "hash"])
def test_calendar_query_scope_and_anchor_rejected_before_network(calendar_job, change):
    args, request, state = calendar_job
    if change == "hash":
        request["calendar_query"]["sha256"] = "0" * 64
    else:
        path = args["request_file"].parent / "calendar-query.json"
        query = json.loads(path.read_bytes())
        query["markets" if change == "market" else "start"] = ["HK"] if change == "market" else "2026-11-01"
        request["calendar_query"] = write(path.parent, path.name, query)
    freeze(args, request)
    with pytest.raises(ValueError, match="calendar|SHA"):
        daily.run_watchlist_day(**args, confirm=True)
    assert not state.calls and not args["day_dir"].exists()


def test_calendar_uses_discovery_budget_and_does_not_refund_failures(calendar_job):
    args, request, state = calendar_job
    root = args["request_file"].parent
    budget = json.loads((root / request["discovery_budget"]["path"]).read_bytes())
    budget["max_calls"] = 1
    request["discovery_budget"] = write(root, "calendar-budget.json", budget)
    freeze(args, request)
    daily.run_watchlist_day(**args, confirm=True)
    manifest = json.loads((args["day_dir"] / "prepared/discovery/discovery-manifest.json").read_bytes())
    assert manifest["request_reservations"] == manifest["provider_call_count"] == 1
    assert [call[0] for call in state.calls] == ["calendar"]


@pytest.mark.parametrize("kind", ["MORNING", "HOLIDAY"])
def test_calendar_session_owner_preserves_half_days_and_explicit_holidays(calendar_job, kind):
    args, request, state = calendar_job
    state.calendar["rows"] = [] if kind == "HOLIDAY" else [{"time": "2026-10-30", "trade_date_type": kind}]
    if kind == "MORNING":
        root = args["request_file"].parent
        window = json.loads((root / request["observation_window"]["path"]).read_bytes())
        window["intervals"] = [{"start_utc": "2026-10-30T16:55:00Z", "end_utc": "2026-10-30T16:56:00Z"}]
        request["observation_window"] = write(root, "half-day-window.json", window)
        freeze(args, request)
    result = daily.run_watchlist_day(**args, confirm=True)
    path = args["day_dir"] / "prepared/plan/watchlist-series.json"
    slots = json.loads(path.read_bytes())["slots"]
    assert slots == [] if kind == "HOLIDAY" else slots[0]["expected_at_utc"] == "2026-10-30T16:55:00Z"
    assert result["manifest"]["watchlist_series_sha256"]
    assert [call[0] for call in state.calls].count("calendar") == 1


def test_calendar_limiter_wait_rechecks_safe_window_before_provider(calendar_job, monkeypatch):
    args, _, state = calendar_job

    def delayed(**kwargs):
        assert kwargs["endpoint"] == "trading_days"
        state.now += timedelta(hours=1)
        return kwargs["call"]()

    monkeypatch.setattr(daily.discovery, "rate_limited_opend_call", delayed)
    with pytest.raises(ValueError, match="calendar unavailable"):
        daily.run_watchlist_day(**args, confirm=True)
    assert not state.calls and state.closed


def test_calendar_queries_explicit_markets_for_carried_tracking_and_records_actual_receive_time(calendar_job):
    args, request, state = calendar_job
    root = args["request_file"].parent
    query = json.loads((root / "calendar-query.json").read_bytes())
    query["markets"] = ["HK", "US"]
    request["calendar_query"] = write(root, "calendar-query.json", query)
    freeze(args, request)
    factory = args["gateway_factory"]

    class Gateway:
        def __init__(self):
            self.base = factory()

        def __getattr__(self, name):
            return getattr(self.base, name)

        def get_trading_days_with_receipt(self, **kwargs):
            state.now += timedelta(seconds=2)
            return self.base.get_trading_days_with_receipt(**kwargs)

    args["gateway_factory"] = lambda **_: Gateway()
    started = state.now
    daily.run_watchlist_day(**args, confirm=True)
    calendar = json.loads((args["day_dir"] / "prepared/calendar.json").read_bytes())["markets"]
    assert sorted(calendar) == ["HK", "US"]
    assert daily.series._utc(calendar["HK"]["source_observed_at_utc"]) == started + timedelta(seconds=2)
    assert daily.series._utc(calendar["US"]["source_observed_at_utc"]) == started + timedelta(seconds=4)
    assert [entry[1]["market"] for entry in state.calls if entry[0] == "calendar"] == ["HK", "US"]


def test_calendar_request_v2_public_cli_preview_is_offline(calendar_job):
    args, _, state = calendar_job
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    cli = ["research", "watchlist-day-run"]
    for flag, value in (
        ("request", args["request_file"]),
        ("expected-request-sha256", args["expected_request_sha256"]),
        ("day-dir", args["day_dir"]),
        ("budget-root", args["budget_roots"][0]),
        ("runtime-root", args["runtime_root"]),
        ("prune-root", args["prune_roots"][0]),
        ("opend-host", args["host"]),
        ("opend-port", args["port"]),
    ):
        cli.extend(["--" + flag, str(value)])
    result = handle_research_command(parser.parse_args(cli))
    assert result["ok"] and result["data"]["status"] == "preview"
    assert not state.calls and not args["day_dir"].exists()


def test_daily_freezes_explicit_ancillary_and_exports_original_bytes(job):
    args, request, state = job
    root = args["request_file"].parent
    evidence = root / "evidence"
    evidence.mkdir()
    write(
        evidence,
        "actions.json",
        {
            "kind": "corporate_actions",
            "request": {"code": "US.ACME", "start": "2026-10-30", "end": "2026-10-30"},
            "started_at_utc": "2026-10-30T14:00:00Z",
            "received_at_utc": "2026-10-30T14:00:01Z",
            "status": "received_unverified_coverage",
            "responses": [{"rows": []}],
        },
    )
    (evidence / "actions.json").chmod(0o600)
    record = daily.series._record(evidence, evidence / "actions.json")
    anchor = write(evidence, "manifest.json", {"schema_version": group.ANCILLARY_SCHEMA, "files": [record]})
    request["ancillary"] = {"path": "evidence/manifest.json", "sha256": anchor["sha256"]}
    freeze(args, request)
    assert not daily.run_watchlist_day(**args)["provider_called"]
    assert not args["day_dir"].exists()
    first = daily.run_watchlist_day(**args, confirm=True)
    frozen = args["day_dir"] / "prepared/ancillary/actions.json"
    assert frozen.read_bytes() == (evidence / "actions.json").read_bytes()
    assert daily.run_watchlist_day(**args, confirm=True)["manifest"] == first["manifest"]
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    output = root.parent / "ancillary-export"
    exported = group.export_watchlist_series(
        series_dir=args["day_dir"] / "archive",
        expected_plan_sha256=first["manifest"]["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=output,
    )
    for batch in exported["batches"]:
        assert (
            output / "exports" / batch["batch_id"] / "inputs/ancillary/supplemental/actions.json"
        ).read_bytes() == frozen.read_bytes()
    (evidence / "actions.json").write_text("{}")
    with pytest.raises(ValueError):
        daily.run_watchlist_day(**args, confirm=True)


def test_daily_ancillary_rejects_mutation_before_discovery(job):
    args, request, state = job
    root = args["request_file"].parent
    value = write(root, "ancillary-source.json", {"kind": "corporate_actions"})
    manifest = {
        "schema_version": group.ANCILLARY_SCHEMA,
        "files": [{"path": value["path"], "sha256": value["sha256"], "size": 1}],
    }
    request["ancillary"] = write(root, "ancillary-manifest.json", manifest)
    freeze(args, request)
    with pytest.raises(ValueError):
        daily.run_watchlist_day(**args, confirm=True)
    assert not state.calls and not args["day_dir"].exists()


@pytest.fixture
def scheduled(calendar_job):
    args, request, state = calendar_job
    root = args["request_file"].parent
    archive = root.parent / "scheduled-days"
    state.calendar["rows"] = [{"time": day, "trade_date_type": "WHOLE"} for day in ("2026-10-30", "2026-11-02")]
    first = {"path": "request.json", "sha256": args["expected_request_sha256"]}
    second = {
        **request,
        "date": "2026-11-02",
        "previous_day": {
            "path": str(archive / request["date"] / "prepared"),
            "request_sha256": first["sha256"],
        },
    }
    for key in ("discovery_window", "observation_window"):
        value = json.loads((root / request[key]["path"]).read_bytes())
        value["intervals"] = [
            {k: v.replace("2026-10-30", "2026-11-02") for k, v in row.items()} for row in value["intervals"]
        ]
        if key == "observation_window":
            value["intervals"] = [{"start_utc": "2026-11-02T20:55:00Z", "end_utc": "2026-11-02T20:56:00Z"}]
        second[key] = write(root, "monday-" + key + ".json", value)
    second_ref = write(root, "monday-request.json", second)
    schedule = {"schema_version": daily.SCHEDULE_SCHEMA, "requests": [first, second_ref]}
    ref = write(root, "schedule.json", schedule)
    options = {k: v for k, v in args.items() if k not in {"request_file", "expected_request_sha256", "day_dir"}}
    options.update(schedule_file=root / "schedule.json", expected_schedule_sha256=ref["sha256"], archive_root=archive)
    return options, state


@pytest.mark.parametrize("session,hour", [("WHOLE", 20), ("MORNING", 17)])
def test_continuous_schedule_prepares_next_day_and_carries_tracking(scheduled, session, hour):
    args, state = scheduled
    path = args["schedule_file"]
    schedule = json.loads(path.read_bytes())
    ref = schedule["requests"][1]
    request = json.loads((path.parent / ref["path"]).read_bytes())
    window = json.loads((path.parent / request["observation_window"]["path"]).read_bytes())
    window["intervals"] = [{"start_utc": f"2026-11-02T{hour}:55:00Z", "end_utc": f"2026-11-02T{hour}:56:00Z"}]
    request["observation_window"] = write(path.parent, "monday-observation_window.json", window)
    schedule["requests"][1] = write(path.parent, ref["path"], request)
    args["expected_schedule_sha256"] = write(path.parent, path.name, schedule)["sha256"]
    state.calendar["rows"][1]["trade_date_type"] = session
    assert daily.run_watchlist_schedule(**args)["status"] == "preview"
    assert not state.calls and not args["archive_root"].exists()
    first = daily.run_watchlist_schedule(**args, confirm=True)
    assert len(first["results"]) == 1
    calls = list(state.calls)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert state.calls == calls
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert sum(call[0] == "snapshot" for call in state.calls) == 2
    friday = group.export_watchlist_series(
        series_dir=args["archive_root"] / "2026-10-30/archive",
        expected_plan_sha256=first["results"][0]["manifest"]["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=args["archive_root"].parent / "export-schedule-friday",
    )
    state.now = datetime(2026, 11, 2, 15, 0, 1, tzinfo=timezone.utc)
    second = daily.run_watchlist_schedule(**args, confirm=True)
    assert second["results"][0]["date"] == "2026-11-02"
    assert [entry[0] for entry in state.calls].count("calendar") == 2
    first_tracking = json.loads((args["archive_root"] / "2026-10-30/prepared/scope/tracking.json").read_bytes())
    next_tracking = json.loads((args["archive_root"] / "2026-11-02/prepared/scope/tracking.json").read_bytes())
    assert {item["option_code"] for item in first_tracking["contracts"]} <= {
        item["option_code"] for item in next_tracking["contracts"]
    }
    calls = list(state.calls)
    state.now = datetime(2026, 11, 2, hour, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert sum(call[0] == "snapshot" for call in state.calls) == 4
    calls = list(state.calls)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert state.calls == calls
    monday = group.export_watchlist_series(
        series_dir=args["archive_root"] / "2026-11-02/archive",
        expected_plan_sha256=second["results"][0]["manifest"]["watchlist_series_sha256"],
        cutoff=f"2026-11-02T{hour}:56:00Z",
        output_dir=args["archive_root"].parent / "export-schedule-monday",
    )
    assert friday["batches"] and monday["batches"]
    for day, expected in (("2026-10-30", "2026-10-30T19:55:00Z"), ("2026-11-02", f"2026-11-02T{hour}:55:00Z")):
        plan = json.loads((args["archive_root"] / day / "prepared/plan/watchlist-series.json").read_bytes())
        assert [slot["expected_at_utc"] for slot in plan["slots"]] == [expected]


@pytest.mark.parametrize(
    "intervals",
    [
        [{"start_utc": "2026-11-02T19:55:00Z", "end_utc": "2026-11-02T19:56:00Z"}],
        [{"start_utc": "2026-11-02T20:55:00Z", "end_utc": "2026-11-02T20:55:59Z"}],
        [
            {"start_utc": "2026-11-02T20:55:00Z", "end_utc": "2026-11-02T20:55:30Z"},
            {"start_utc": "2026-11-02T20:55:30Z", "end_utc": "2026-11-02T20:56:00Z"},
        ],
    ],
)
def test_schedule_rejects_window_that_misses_owner_dst_slot(scheduled, intervals):
    args, state = scheduled
    path = args["schedule_file"]
    schedule = json.loads(path.read_bytes())
    ref = schedule["requests"][1]
    request = json.loads((path.parent / ref["path"]).read_bytes())
    window = json.loads((path.parent / request["observation_window"]["path"]).read_bytes())
    window["intervals"] = intervals
    request["observation_window"] = write(path.parent, "monday-observation_window.json", window)
    schedule["requests"][1] = write(path.parent, ref["path"], request)
    args["expected_schedule_sha256"] = write(path.parent, path.name, schedule)["sha256"]
    daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 11, 2, 15, 0, 1, tzinfo=timezone.utc)
    state.calendar["rows"] = [{"time": "2026-11-02", "trade_date_type": "WHOLE"}]
    with pytest.raises(ValueError, match="observation window does not cover planned slot"):
        daily.run_watchlist_schedule(**args, confirm=True)
    assert not any(call[0] == "snapshot" for call in state.calls)
    retained = args["archive_root"] / "2026-11-02/prepared/plan/watchlist-series.json"
    assert json.loads(retained.read_bytes())["slots"][0]["expected_at_utc"] == "2026-11-02T20:55:00Z"
    calls = list(state.calls)
    assert daily.run_watchlist_schedule(**args, confirm=True)["results"][0]["status"] == "abandoned"
    assert state.calls == calls


def test_schedule_outside_windows_and_after_end_never_contacts_provider(scheduled):
    args, state = scheduled
    for when, status in (
        (datetime(2026, 10, 30, 16, tzinfo=timezone.utc), "checked"),
        (datetime(2026, 11, 3, 1, tzinfo=timezone.utc), "expired"),
    ):
        state.now = when
        result = daily.run_watchlist_schedule(**args, confirm=True)
        assert result["status"] == status and not result["results"]
        assert not state.calls and not args["archive_root"].exists()


@pytest.mark.parametrize("change", ["schedule_hash", "request_hash", "tracking"])
def test_schedule_rejects_changed_scope_or_lost_tracking_before_any_provider(scheduled, change):
    args, state = scheduled
    if change == "schedule_hash":
        args["expected_schedule_sha256"] = "0" * 64
    else:
        path = args["schedule_file"]
        schedule = json.loads(path.read_bytes())
        if change == "request_hash":
            schedule["requests"][1]["sha256"] = "0" * 64
        else:
            ref = schedule["requests"][1]
            request = json.loads((path.parent / ref["path"]).read_bytes())
            request["previous_day"] = None
            schedule["requests"][1] = write(path.parent, ref["path"], request)
        args["expected_schedule_sha256"] = write(path.parent, path.name, schedule)["sha256"]
    with pytest.raises(ValueError, match="SHA|tracking"):
        daily.run_watchlist_schedule(**args, confirm=True)
    assert not state.calls and not args["archive_root"].exists()


def test_schedule_public_cli_and_persistent_timer_are_limited_to_research(scheduled):
    args, state = scheduled
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    cli = ["research", "watchlist-schedule-run"]
    for flag, value in (
        ("schedule", args["schedule_file"]),
        ("expected-schedule-sha256", args["expected_schedule_sha256"]),
        ("archive-root", args["archive_root"]),
        ("budget-root", args["budget_roots"][0]),
        ("runtime-root", args["runtime_root"]),
        ("prune-root", args["prune_roots"][0]),
        ("opend-host", args["host"]),
        ("opend-port", args["port"]),
    ):
        cli.extend(["--" + flag, str(value)])
    report = handle_research_command(parser.parse_args(cli))
    assert report["ok"] and report["data"]["status"] == "preview"
    assert len(report["data"]["days"]) == 2 and not state.calls
    root = Path(__file__).resolve().parents[1] / "services/systemd"
    service = (root / "options-monitor-research-watchlist-schedule.service.in").read_text()
    timer = (root / "options-monitor-research-watchlist-schedule.timer.in").read_text()
    command = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert " research watchlist-schedule-run " in command and "--confirm" in command
    assert "--expected-schedule-sha256 {{SCHEDULE_SHA256}}" in command
    assert "Restart=no" in service and "NoNewPrivileges=yes" in service
    assert "ReadWritePaths={{ARCHIVE_ROOT}} {{SDK_HOME}}" in service
    assert "OnCalendar=*-*-* *:*:00 UTC" in timer and "Persistent=false" in timer
    assert not any(word in command for word in (" scan", " tick", " trade", " notify"))


def test_preview_does_not_write_or_call(job):
    args, _, state = job
    assert daily.run_watchlist_day(**args)["status"] == "preview"
    assert state.calls == [] and not args["day_dir"].exists()


def test_actual_discovery_then_collector_repeat_is_not_a_second_discovery(job):
    args, _, state = job
    first = daily.run_watchlist_day(**args, confirm=True)
    assert first["manifest"]["discovery_status"] == "complete"
    calls = list(state.calls)
    assert len(calls) == 4
    assert daily.run_watchlist_day(**args, confirm=True)["manifest"] == first["manifest"]
    assert state.calls == calls
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    assert len(state.calls) == 6
    daily.run_watchlist_day(**args, confirm=True)
    assert len(state.calls) == 6
    exported = group.export_watchlist_series(
        series_dir=args["day_dir"] / "archive",
        expected_plan_sha256=first["manifest"]["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=args["day_dir"].parent / "export-friday",
    )
    assert len(exported["batches"]) == 1


@pytest.mark.parametrize("ref", sorted(daily.REFERENCES))
def test_changed_input_rejected_before_any_call(job, ref):
    args, request, state = job
    request[ref]["sha256"] = "0" * 64
    freeze(args, request)
    with pytest.raises(ValueError, match="SHA"):
        daily.run_watchlist_day(**args, confirm=True)
    assert state.calls == [] and not args["day_dir"].exists()


def test_external_request_anchor_and_window_overlap_rejected(job):
    args, request, state = job
    with pytest.raises(ValueError, match="SHA"):
        daily.run_watchlist_day(**{**args, "expected_request_sha256": "0" * 64}, confirm=True)
    request["observation_window"] = request["discovery_window"]
    freeze(args, request)
    with pytest.raises(ValueError, match="overlaps"):
        daily.run_watchlist_day(**args, confirm=True)
    assert state.calls == []


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("utc_date_matches", [False, True])
def test_discovery_window_on_wrong_market_date_does_not_query_provider(job, confirm, utc_date_matches):
    args, request, state = job
    request["date"] = "2026-11-02"
    if utc_date_matches:
        request["date"] = "2026-10-31"
        state.now = datetime(2026, 10, 31, 0, 1, tzinfo=timezone.utc)
        for key, hour, minute in (("discovery_window", 0, 0), ("observation_window", 19, 55)):
            value = json.loads((args["request_file"].parent / request[key]["path"]).read_bytes())
            start = state.now.replace(hour=hour, minute=minute)
            value["intervals"] = [
                {"start_utc": start.isoformat(), "end_utc": (start + timedelta(minutes=1)).isoformat()}
            ]
            if key == "discovery_window":
                value["intervals"][0]["end_utc"] = (start + timedelta(minutes=10)).isoformat()
            request[key] = write(args["request_file"].parent, key + "-wrong-day.json", value)
    freeze(args, request)
    with pytest.raises(ValueError, match="market date differs"):
        daily.run_watchlist_day(**args, confirm=confirm)
    assert state.calls == [] and not args["day_dir"].exists()


def test_missed_and_interrupted_preparation_do_not_retry_network(job):
    args, _, state = job
    state.now += timedelta(hours=1)
    assert daily.run_watchlist_day(**args, confirm=True)["status"] == "missed"
    assert (args["day_dir"] / "missed.json").is_file()
    state.now -= timedelta(hours=1)
    assert daily.run_watchlist_day(**args, confirm=True)["status"] == "missed"
    args["day_dir"] = args["day_dir"].parent / "interrupted"
    args["day_dir"].mkdir(mode=0o700)
    (args["day_dir"] / "prepared").mkdir(mode=0o700)
    assert daily.run_watchlist_day(**args, confirm=True)["status"] == "abandoned"
    assert state.calls == []


def test_prepared_original_change_or_different_request_never_restarts_discovery(job):
    args, request, state = job
    daily.run_watchlist_day(**args, confirm=True)
    calls = list(state.calls)
    original = args["day_dir"] / "prepared/calendar.json"
    raw = original.read_bytes()
    original.write_bytes(b"{}")
    with pytest.raises(ValueError):
        daily.run_watchlist_day(**args, confirm=True)
    original.write_bytes(raw)
    request["date"] = "2026-10-31"
    freeze(args, request)
    with pytest.raises(ValueError, match="request mismatch"):
        daily.run_watchlist_day(**args, confirm=True)
    assert state.calls == calls


def test_next_day_carries_previous_tracking_and_exports_holiday_without_new_quote(job):
    args, request, state = job
    state.expiries = ["2026-11-20"]
    first = daily.run_watchlist_day(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    friday = group.export_watchlist_series(
        series_dir=args["day_dir"] / "archive",
        expected_plan_sha256=first["manifest"]["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=args["day_dir"].parent / "export-friday",
    )
    previous = args["day_dir"] / "prepared"
    previous_tracking = (previous / "scope/tracking.json").read_bytes()
    request["previous_day"] = {"path": str(previous), "request_sha256": args["expected_request_sha256"]}
    request["date"] = "2026-10-31"
    root = args["request_file"].parent
    for key in ("discovery_window", "observation_window"):
        value = json.loads((root / request[key]["path"]).read_bytes())
        for window in value["intervals"]:
            for field in window:
                window[field] = window[field].replace("2026-10-30", "2026-10-31")
        request[key] = write(root, "next-" + key + ".json", value)
    args["request_file"] = root / "saturday-request.json"
    args["day_dir"] = root.parent / "day-saturday"
    freeze(args, request)
    state.now = datetime(2026, 10, 31, 15, tzinfo=timezone.utc)
    next_day = daily.run_watchlist_day(**args, confirm=True)
    assert (args["day_dir"] / "prepared/source/daily-previous-tracking.json").read_bytes() == previous_tracking
    tracking = json.loads((args["day_dir"] / "prepared/scope/tracking.json").read_bytes())
    assert len(tracking["contracts"]) >= len(json.loads(previous_tracking)["contracts"])
    assert next_day["manifest"]["date"] == "2026-10-31"
    assert sum(call[0] == "snapshot" for call in state.calls) == 2
    saturday = group.export_watchlist_series(
        series_dir=args["day_dir"] / "archive",
        expected_plan_sha256=next_day["manifest"]["watchlist_series_sha256"],
        cutoff="2026-10-31T20:00:00Z",
        output_dir=args["day_dir"].parent / "export-saturday",
    )
    packages = [
        {
            "package_id": name,
            "path": "export-" + name,
            "expected_plan_sha256": item["watchlist_series_sha256"],
            "expected_export_sha256": item["watchlist_export_sha256"],
            "expected_implementation_sha256": item["producer_implementation_sha256"],
        }
        for name, item in (("friday", friday), ("saturday", saturday))
    ]
    write(
        args["day_dir"].parent,
        "collection.json",
        {
            "schema_version": "research.watchlist-collection.v1",
            "start_date": "2026-10-30",
            "end_date": "2026-10-31",
            "cutoff_utc": "2026-10-31T20:00:00Z",
            "packages": packages,
            "days": [
                {"date": day, "package_id": name, "valuation_at_utc": point}
                for day, name, point in (
                    ("2026-10-30", "friday", "2026-10-30T19:56:00Z"),
                    ("2026-10-31", "saturday", "2026-10-31T20:00:00Z"),
                )
            ],
        },
    )


def test_empty_discovery_keeps_visible_scope_result_without_retry(job):
    args, _, state = job
    state.expiries = []
    first = daily.run_watchlist_day(**args, confirm=True)
    assert first["status"] == "no_observable_scope"
    assert first["manifest"]["watchlist_series_sha256"] is None
    assert daily.run_watchlist_day(**args, confirm=True) == first
    assert len(state.calls) == 1


def test_cli_forwards_only_explicit_daily_arguments(monkeypatch):
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(
        [
            "research",
            "watchlist-day-run",
            "--request",
            "/inbox/request.json",
            "--expected-request-sha256",
            "a" * 64,
            "--day-dir",
            "/research/day",
            "--runtime-root",
            "/runtime",
            "--budget-root",
            "/research",
            "--prune-root",
            "/prune",
            "--opend-host",
            "fixture.invalid",
            "--opend-port",
            "11111",
        ]
    )
    received = {}

    def run(**kwargs):
        received.update(kwargs)
        return {"status": "preview"}

    monkeypatch.setattr(daily, "run_watchlist_day", run)
    assert handle_research_command(args)["ok"] is True
    assert received["expected_request_sha256"] == "a" * 64 and received["confirm"] is False


def test_finite_systemd_template_uses_daily_cli_and_restricts_writes(monkeypatch):
    root = Path(__file__).resolve().parents[1] / "services/systemd"
    values = {
        "USER": "research-fixture",
        "SOURCE_ROOT": "/opt/research/source",
        "SDK_HOME": "/var/lib/research/sdk-home",
        "PYTHON": "/opt/research/venv/bin/python",
        "REQUEST_FILE": "/var/lib/research/plans/friday/request.json",
        "REQUEST_SHA256": "a" * 64,
        "DAY_DIR": "/var/lib/research/days/friday",
        "DATA_BUDGET_ROOT": "/var/lib/research",
        "INSTALL_BUDGET_ROOT": "/opt/research",
        "OM_RUNTIME_ROOT": "/var/lib/production-om",
        "OM_CONFIG_ROOT": "/etc/production-om",
        "OM_PRUNE_ROOT": "/var/cache/production-om",
        "OPEND_HOST": "fixture.invalid",
        "OPEND_PORT": "11111",
        "TIMEOUT_SECONDS": "600",
        "DISCOVERY_AT_UTC": "2026-10-30 06:00:00 UTC",
        "HK_OBSERVATION_AT_UTC": "2026-10-30 07:55:00 UTC",
        "US_OBSERVATION_AT_UTC": "2026-10-30 19:55:00 UTC",
        "SERVICE_UNIT": "research-fixture-friday.service",
    }
    service, timer = [
        re.sub(
            r"\{\{([A-Z0-9_]+)\}\}",
            lambda m: values[m[1]],
            (root / ("options-monitor-research-watchlist-day." + kind + ".in")).read_text(),
        )
        for kind in ("service", "timer")
    ]
    command = shlex.split(next(line.split("=", 1)[1] for line in service.splitlines() if line.startswith("ExecStart=")))
    assert command[:4] == [values["PYTHON"], "-B", "-m", "src.interfaces.cli.main"]
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(command[4:])
    received = {}
    monkeypatch.setattr(daily, "run_watchlist_day", lambda **kw: received.update(kw) or {"status": "preview"})
    assert handle_research_command(args)["ok"] is True
    assert received["request_file"] == Path(values["REQUEST_FILE"])
    assert received["expected_request_sha256"] == values["REQUEST_SHA256"]
    assert received["budget_roots"] == [Path(values[k]) for k in ("DATA_BUDGET_ROOT", "INSTALL_BUDGET_ROOT")]
    assert received["confirm"] is True
    assert "ReadWritePaths=" + values["DAY_DIR"] + " " + values["SDK_HOME"] in service
    for setting in ("ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "NoNewPrivileges=yes", "Restart=no"):
        assert setting in service.splitlines()
    assert [line for line in timer.splitlines() if line.startswith("OnCalendar=")] == [
        "OnCalendar=" + values[k] for k in ("DISCOVERY_AT_UTC", "HK_OBSERVATION_AT_UTC", "US_OBSERVATION_AT_UTC")
    ]
    assert "Persistent=false" in timer.splitlines() and "RandomizedDelaySec=0" in timer.splitlines()
    assert "{{" not in service + timer and "*" not in timer
