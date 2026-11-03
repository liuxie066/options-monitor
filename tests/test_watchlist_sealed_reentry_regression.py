"""Daily terminal states use real planning/storage with isolated transports."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import socket

import pytest

from src.application.research import watchlist_day as daily
from test_watchlist_research_ancillary import acquisition  # noqa: F401
from test_watchlist_research_day import calendar_job, freeze, job, write  # noqa: F401
from test_watchlist_research_discovery import setup  # noqa: F401
from test_watchlist_research_recurring import recurring  # noqa: F401


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("real network forbidden in daily terminal-state regressions")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def closed_unclaimed(recurring):
    args, schedule, state = recurring
    schedule["schema_version"] = daily.ARCHIVING_SCHEMA
    args["expected_schedule_sha256"] = write(
        args["schedule_file"].parent, args["schedule_file"].name, schedule
    )["sha256"]
    result = daily.run_watchlist_schedule(**args, confirm=True)
    manifest = result["results"][0]["manifest"]
    day = args["archive_root"] / "2026-10-30"
    assert not list(day.rglob("attempt.json"))
    state.now = datetime(2026, 10, 30, 20, 1, tzinfo=timezone.utc)
    options = {
        key: value
        for key, value in args.items()
        if key not in {"schedule_file", "expected_schedule_sha256", "archive_root"}
    }
    options.update(
        request_file=day.parent / "recurring/requests/2026-10-30/request.json",
        expected_request_sha256=manifest["request_sha256"],
        day_dir=day,
        confirm=True,
    )
    return args, state, day, manifest, options


@pytest.mark.parametrize("retain_source", [True, False])
def test_sealed_reentry_is_terminal_and_hot_cold_exports_agree(closed_unclaimed, retain_source):
    args, state, day, manifest, options = closed_unclaimed
    daily.run_watchlist_schedule(**args, confirm=True)
    original = daily._day_tree(day)
    calls = list(state.calls)
    seal = daily._seal_dir(day)
    anchor = (seal / "anchor.json").read_bytes()
    export = dict(
        series_dir=day / "archive",
        expected_plan_sha256=manifest["watchlist_series_sha256"],
        cutoff="2026-10-30T20:01:00Z",
    )
    hot = daily.export_watchlist_series(**export, output_dir=day.parent.parent / "delivery/hot")
    preserved = day.parent.parent / "preserved-source"
    if not retain_source:
        day.rename(preserved)
    for _ in range(2):
        assert daily.run_watchlist_day(**options) == {
            "status": "sealed", "provider_called": False, "date": "2026-10-30"
        }
    assert state.calls == calls and (seal / "anchor.json").read_bytes() == anchor
    assert daily._day_tree(day if retain_source else preserved) == original
    if retain_source:
        day.rename(preserved)
    cold = daily.export_watchlist_series(**export, output_dir=day.parent.parent / "delivery/cold")
    assert hot["watchlist_export_sha256"] == cold["watchlist_export_sha256"]
    assert not day.exists()


@pytest.mark.parametrize("operation", ["collect", "export"])
@pytest.mark.parametrize("damage", ["anchor", "object"])
def test_retained_source_never_bypasses_invalid_sealed_anchor(closed_unclaimed, operation, damage):
    args, state, day, manifest, options = closed_unclaimed
    daily.run_watchlist_schedule(**args, confirm=True)
    if damage == "anchor":
        anchor = daily._seal_dir(day) / "anchor.json"
        value = json.loads(anchor.read_bytes())
        value["day_pack_sha256"] = "0" * 64
        anchor.write_bytes(daily.series._bytes(value))
    else:
        pack, objects = daily._sealed_day(day)
        record = next(r for r in pack["files"] if r["path"] == "prepared/request.json")
        blob = objects / "sha256" / record["sha256"][:2] / (record["sha256"] + ".json.gz")
        blob.rename(blob.with_suffix(".preserved"))
    original, calls = daily._day_tree(day), list(state.calls)
    with pytest.raises((ValueError, OSError)):
        if operation == "collect":
            daily.run_watchlist_day(**options)
        else:
            daily.export_watchlist_series(
                series_dir=day / "archive",
                expected_plan_sha256=manifest["watchlist_series_sha256"],
                cutoff="2026-10-30T20:01:00Z",
                output_dir=day.parent.parent / "delivery/invalid",
            )
    assert daily._day_tree(day) == original and state.calls == calls


def test_anchor_is_rechecked_inside_daily_lock(closed_unclaimed, monkeypatch):
    args, state, day, _, options = closed_unclaimed
    daily.run_watchlist_schedule(**args, confirm=True)
    anchor = daily._seal_dir(day) / "anchor.json"
    preserved = anchor.with_suffix(".preserved")
    anchor.rename(preserved)
    lock = daily.group._lock
    sealed = daily._sealed_day
    held = False
    checked = []

    @contextmanager
    def publishing_lock(root):
        nonlocal held
        with lock(root):
            if root == day:
                preserved.rename(anchor)
                held = True
            try:
                yield
            finally:
                held = False

    def checked_seal(*args, **kwargs):
        assert held
        checked.append(True)
        return sealed(*args, **kwargs)

    monkeypatch.setattr(daily.group, "_lock", publishing_lock)
    monkeypatch.setattr(daily, "_sealed_day", checked_seal)
    original, calls = daily._day_tree(day), list(state.calls)
    assert daily.run_watchlist_day(**options)["status"] == "sealed"
    assert checked and daily._day_tree(day) == original and state.calls == calls


def test_interrupted_anchor_blocks_collection_then_resumes_identical_pack(closed_unclaimed, monkeypatch):
    args, state, day, _, options = closed_unclaimed
    put = daily.series._put
    anchor = daily._seal_dir(day) / "anchor.json"

    def interrupt(path, raw):
        if path == anchor:
            raise KeyboardInterrupt("interrupted anchor publication")
        return put(path, raw)

    original, calls = daily._day_tree(day), list(state.calls)
    with monkeypatch.context() as patch:
        patch.setattr(daily.series, "_put", interrupt)
        with pytest.raises(KeyboardInterrupt):
            daily.run_watchlist_schedule(**args, confirm=True)
    assert not anchor.exists()
    pack = (anchor.parent / "day-pack.json").read_bytes()
    objects = {p: p.read_bytes() for p in (day.parent / "objects").rglob("*.gz")}
    with pytest.raises(ValueError, match="regular input file required"):
        daily.run_watchlist_day(**options)
    assert daily._day_tree(day) == original and state.calls == calls
    assert len(daily.run_watchlist_schedule(**args, confirm=True)["sealed_days"]) == 1
    assert (anchor.parent / "day-pack.json").read_bytes() == pack
    assert objects == {p: p.read_bytes() for p in (day.parent / "objects").rglob("*.gz")}
    assert daily.run_watchlist_day(**options)["status"] == "sealed"
    assert daily.run_watchlist_schedule(**args, confirm=True)["sealed_days"] == []
    assert daily._day_tree(day) == original and state.calls == calls


def test_holiday_ancillary_retains_zero_slot_sources_without_retry(acquisition):
    args, _, state = acquisition
    state.calendar["rows"] = []
    first = daily.run_watchlist_day(**args, confirm=True)
    assert first["status"] == "prepared_and_checked"
    prepared = args["day_dir"] / "prepared"
    assert first["manifest"]["watchlist_series_sha256"]
    plan = json.loads((prepared / "plan/watchlist-series.json").read_bytes())
    assert plan["slots"] == [] and plan["batches"]
    assert json.loads((prepared / "calendar.json").read_bytes())["markets"]["US"]["receipt"] == state.calendar
    assert (prepared / "discovery/calls/000000/response.json").is_file()
    original, calls = daily._day_tree(args["day_dir"]), list(state.calls)
    assert [call[0] for call in calls].count("calendar") == 1
    for hour, minute in ((19, 54), (19, 55), (20, 1)):
        state.now = datetime(2026, 10, 30, hour, minute, 1, tzinfo=timezone.utc)
        result = daily.run_watchlist_day(**args, confirm=True)
        assert result["manifest"] == first["manifest"]
    assert daily._day_tree(args["day_dir"]) == original and state.calls == calls
    assert not state.http and not (args["day_dir"] / "market-ancillary").exists()
    assert not any(call[0] in {"snapshot", "rehab", "preflight"} for call in calls)
    exported = daily.export_watchlist_series(
        series_dir=args["day_dir"] / "archive",
        expected_plan_sha256=first["manifest"]["watchlist_series_sha256"],
        cutoff="2026-10-30T20:01:00Z",
        output_dir=args["day_dir"].parent / "holiday-export",
    )
    assert exported["batches"]


@pytest.mark.parametrize("half_day", [False, True])
def test_trading_day_still_captures_ancillary_and_quotes(acquisition, half_day):
    args, request, state = acquisition
    hour = 16 if half_day else 19
    if half_day:
        state.calendar["rows"][0]["trade_date_type"] = "MORNING"
        root = args["request_file"].parent
        window = json.loads((root / request["observation_window"]["path"]).read_bytes())
        window["intervals"] = [{
            "start_utc": f"2026-10-30T{hour}:54:00Z",
            "end_utc": f"2026-10-30T{hour}:56:00Z",
        }]
        request["observation_window"] = write(root, "half-day-window.json", window)
        freeze(args, request)
    first = daily.run_watchlist_day(**args, confirm=True)
    assert first["manifest"]["watchlist_series_sha256"] and not state.http
    state.now = datetime(2026, 10, 30, hour, 54, 1, tzinfo=timezone.utc)
    assert daily.run_watchlist_day(**args, confirm=True)["ancillary_capture"]["status"] == "captured"
    calls, http = list(state.calls), list(state.http)
    assert len(http) == 2 and [c[0] for c in calls].count("rehab") == 1
    daily.run_watchlist_day(**args, confirm=True)
    assert state.calls == calls and state.http == http
    state.now = datetime(2026, 10, 30, hour, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    assert [c[0] for c in state.calls].count("snapshot") == 2 and state.http == http
