"""An ended day can be compressed, restored and used by the next real research runner."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.research import watchlist_day as daily, watchlist_series as group
from src.interfaces.cli.research import add_research_commands, handle_research_command
from test_watchlist_research_day import job, calendar_job, scheduled, write  # noqa: F401
from test_watchlist_research_discovery import setup  # noqa: F401
from test_watchlist_research_recurring import recurring  # noqa: F401


@pytest.fixture
def archived(recurring):
    args, schedule, state = recurring
    schedule["schema_version"] = daily.ARCHIVING_SCHEMA
    args["expected_schedule_sha256"] = write(args["schedule_file"].parent, args["schedule_file"].name, schedule)[
        "sha256"
    ]
    first = daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    day = args["archive_root"] / "2026-10-30"
    original = daily._day_tree(day)
    calls = list(state.calls)
    state.now = datetime(2026, 10, 30, 20, 1, tzinfo=timezone.utc)
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert len(result["sealed_days"]) == 1 and result["sealed_days"][0]["source_retained"]
    assert daily._day_tree(day) == original and state.calls == calls
    return args, state, day, first["results"][0]["manifest"]


def test_archiving_schedule_is_explicit_and_preview_does_not_write(recurring):
    args, schedule, state = recurring
    schedule["schema_version"] = daily.ARCHIVING_SCHEMA
    args["expected_schedule_sha256"] = write(args["schedule_file"].parent, args["schedule_file"].name, schedule)[
        "sha256"
    ]
    assert daily.run_watchlist_schedule(**args)["status"] == "preview"
    assert not args["archive_root"].exists() and not state.calls
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert result["sealed_days"] == []
    assert not (args["archive_root"] / "objects").exists()


def test_archived_source_supports_export_and_next_day_without_rewriting_requests(archived):
    args, state, day, manifest = archived
    prior = (day / "prepared/scope/tracking.json").read_bytes()
    before_tree = daily._day_tree(day)
    destination = day.parent.parent / "deliveries"
    kwargs = dict(expected_plan_sha256=manifest["watchlist_series_sha256"], cutoff="2026-10-30T20:01:00Z")
    before = daily.export_watchlist_series(series_dir=day / "archive", output_dir=destination / "before", **kwargs)
    objects = {p: p.read_bytes() for p in (day.parent / "objects").rglob("*.gz")}
    assert daily.run_watchlist_schedule(**args, confirm=True)["sealed_days"] == []
    assert objects == {p: p.read_bytes() for p in (day.parent / "objects").rglob("*.gz")}
    # Preserve the synthetic source elsewhere: the runner must use only the compressed source.
    day.rename(day.parent.parent / "preserved-source")
    loaded = daily._load_day(day / "prepared", manifest["request_sha256"])[1]
    assert loaded == manifest
    after = daily.export_watchlist_series(series_dir=day / "archive", output_dir=destination / "after", **kwargs)
    assert after["watchlist_export_sha256"] == before["watchlist_export_sha256"]
    packed = daily.export_watchlist_series(
        series_dir=day / "archive", output_dir=destination / "packed", object_root=destination / "objects", **kwargs
    )
    assert packed["watchlist_export_sha256"] == before["watchlist_export_sha256"]
    assert not list(destination.glob(".sealed-day-*")) and not day.exists()
    calls = list(state.calls)
    repeat = daily.run_watchlist_day(
        **{k: v for k, v in args.items() if k not in {"schedule_file", "expected_schedule_sha256", "archive_root"}},
        request_file=day.parent / "recurring/requests/2026-10-30/request.json",
        expected_request_sha256=manifest["request_sha256"],
        day_dir=day,
        confirm=True,
    )
    assert repeat["status"] == "sealed" and state.calls == calls and not day.exists()
    state.now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    state.calendar["rows"] = [{"time": "2026-11-02", "trade_date_type": "WHOLE"}]
    next_day = daily.run_watchlist_schedule(**args, confirm=True)
    assert next_day["results"][0]["date"] == "2026-11-02"
    assert (day.parent / "2026-11-02/prepared/source/daily-previous-tracking.json").read_bytes() == prior
    request = json.loads((day.parent / "recurring/requests/2026-11-02/request.json").read_bytes())
    assert request["previous_day"] == {"path": str(day / "prepared"), "request_sha256": manifest["request_sha256"]}
    assert before_tree == daily._day_tree(day.parent.parent / "preserved-source")


@pytest.mark.parametrize("change", ["anchor", "missing_anchor", "object", "missing_object"])
def test_broken_cold_predecessor_never_falls_back_or_calls_provider(archived, change):
    args, state, day, _ = archived
    day.rename(day.parent.parent / "preserved-source")
    anchor = daily._seal_dir(day) / "anchor.json"
    if change == "anchor":
        value = json.loads(anchor.read_bytes())
        value["day_pack_sha256"] = "0" * 64
        anchor.write_bytes(daily.series._bytes(value))
    elif change == "missing_anchor":
        anchor.rename(anchor.with_suffix(".preserved"))
    else:
        manifest, objects = daily._sealed_day(day)
        record = next(r for r in manifest["files"] if r["path"] == "prepared/scope/tracking.json")
        blob = objects / "sha256" / record["sha256"][:2] / (record["sha256"] + ".json.gz")
        if change == "object":
            blob.write_bytes(b"invalid")
        else:
            blob.rename(blob.with_suffix(".preserved"))
    before = list(state.calls)
    state.now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    with pytest.raises((ValueError, OSError, EOFError)):
        daily.run_watchlist_schedule(**args, confirm=True)
    assert state.calls == before and not (day.parent / "2026-11-02").exists()


def test_unarchived_schedule_does_not_enable_automatic_source_archiving(recurring):
    args, _, state = recurring
    daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 20, 1, tzinfo=timezone.utc)
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert "sealed_days" not in result
    assert not (args["archive_root"] / "objects").exists()


def test_interrupted_anchor_publication_reuses_verified_pack(archived):
    args, state, day, _ = archived
    anchor = daily._seal_dir(day) / "anchor.json"
    original = anchor.read_bytes()
    anchor.rename(anchor.with_suffix(".preserved"))
    objects = {p: p.read_bytes() for p in (day.parent / "objects").rglob("*.gz")}
    calls = list(state.calls)
    result = daily.run_watchlist_schedule(**args, confirm=True)
    assert len(result["sealed_days"]) == 1 and anchor.read_bytes() == original
    assert objects == {p: p.read_bytes() for p in (day.parent / "objects").rglob("*.gz")}
    assert state.calls == calls


def test_automatic_pack_cannot_report_success_after_consuming_disk_reserve(recurring, monkeypatch):
    args, schedule, state = recurring
    schedule["schema_version"] = daily.ARCHIVING_SCHEMA
    args["expected_schedule_sha256"] = write(args["schedule_file"].parent, args["schedule_file"].name, schedule)[
        "sha256"
    ]
    daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 20, 1, tzinfo=timezone.utc)
    day = args["archive_root"] / "2026-10-30"
    original = daily._day_tree(day)
    calls = list(state.calls)
    disk_usage = group.shutil.disk_usage

    def remaining(path):
        usage = disk_usage(path)
        return usage._replace(free=0) if list((args["archive_root"] / "objects").rglob("*.gz")) else usage

    monkeypatch.setattr(group.shutil, "disk_usage", remaining)
    with pytest.raises(ValueError, match="storage budget exceeded"):
        daily.run_watchlist_schedule(**args, confirm=True)
    assert not (daily._seal_dir(day) / "anchor.json").exists()
    assert daily._day_tree(day) == original and state.calls == calls

    monkeypatch.setattr(group.shutil, "disk_usage", disk_usage)
    objects = {p: p.read_bytes() for p in (args["archive_root"] / "objects").rglob("*.gz")}
    assert len(daily.run_watchlist_schedule(**args, confirm=True)["sealed_days"]) == 1
    assert objects == {p: p.read_bytes() for p in (args["archive_root"] / "objects").rglob("*.gz")}
    assert daily._day_tree(day) == original and state.calls == calls


@pytest.fixture
def closed(job):
    args, request, state = job
    result = daily.run_watchlist_day(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 19, 56, tzinfo=timezone.utc)
    root = args["day_dir"]
    # An empty claim is evidence of interruption, not a directory to silently discard.
    group.series._private_dir(root / "archive/interrupted/claim")
    options = dict(
        day_dir=root,
        expected_request_sha256=args["expected_request_sha256"],
        object_root=root.parent / "objects",
        output_dir=root.parent / "sealed",
        runtime_root=args["runtime_root"],
        prune_roots=args["prune_roots"],
        clock=args["clock"],
    )
    return args, state, result, options


def restore_options(options, digest, out):
    return dict(
        manifest_file=options["output_dir"] / "day-pack.json",
        expected_manifest_sha256=digest,
        object_root=options["object_root"],
        output_dir=out,
        runtime_root=options["runtime_root"],
        prune_roots=options["prune_roots"],
        min_free_bytes=1,
    )


def test_full_day_roundtrip_preserves_runtime_and_export(closed):
    args, state, result, options = closed
    root = args["day_dir"]
    originals = daily._day_tree(root)
    assert any("limiter/" in r["path"] for r in originals[0])
    first = daily.pack_watchlist_day(**options)
    second = daily.pack_watchlist_day(**{**options, "output_dir": root.parent / "sealed-again"})
    assert first["source_retained"] and first["storage"]["new_objects"] > 0
    assert second["storage"]["new_object_bytes"] == second["storage"]["new_objects"] == 0
    assert first["day_pack_sha256"] == second["day_pack_sha256"]
    assert originals == daily._day_tree(root)
    restored = root.parent / "restored"
    daily.restore_watchlist_day(**restore_options(options, first["day_pack_sha256"], restored))
    assert originals == daily._day_tree(restored)
    assert (restored / "archive/interrupted/claim").is_dir()
    kwargs = dict(expected_plan_sha256=result["manifest"]["watchlist_series_sha256"], cutoff="2026-10-30T19:56:00Z")
    before = daily.export_watchlist_series(
        series_dir=root / "archive", output_dir=root.parent / "export-before", **kwargs
    )
    after = daily.export_watchlist_series(
        series_dir=restored / "archive", output_dir=root.parent / "export-after", **kwargs
    )
    assert before["watchlist_export_sha256"] == after["watchlist_export_sha256"]
    assert [name for name, *_ in state.calls].count("snapshot") == 2
    for path in restored.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0


def test_restored_previous_day_continues_tracking_through_owner(scheduled):
    args, state = scheduled
    first = daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    state.now = datetime(2026, 10, 30, 19, 56, tzinfo=timezone.utc)
    root = args["archive_root"] / "2026-10-30"
    options = dict(
        day_dir=root,
        expected_request_sha256=first["results"][0]["manifest"]["request_sha256"],
        object_root=root.parent / "objects",
        output_dir=root.parent / "packed",
        runtime_root=args["runtime_root"],
        prune_roots=args["prune_roots"],
        clock=args["clock"],
    )
    packed = daily.pack_watchlist_day(**options)
    original_tree = daily._day_tree(root)
    root.rename(root.with_name("source-preserved"))
    daily.restore_watchlist_day(**restore_options(options, packed["day_pack_sha256"], root))
    assert original_tree == daily._day_tree(root)
    prior = (root / "prepared/scope/tracking.json").read_bytes()
    state.now = datetime(2026, 11, 2, 15, 0, 1, tzinfo=timezone.utc)
    next_day = daily.run_watchlist_schedule(**args, confirm=True)
    assert next_day["results"][0]["date"] == "2026-11-02"
    assert (args["archive_root"] / "2026-11-02/prepared/source/daily-previous-tracking.json").read_bytes() == prior
    state.now = datetime(2026, 11, 2, 20, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**args, confirm=True)
    assert sum(row[0] == "snapshot" for row in state.calls) == 4


def test_active_day_or_held_lock_cannot_be_sealed(closed):
    _, state, _, options = closed
    state.now = datetime(2026, 10, 30, 19, 55, 59, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="not ended"):
        daily.pack_watchlist_day(**options)
    state.now = datetime(2026, 10, 30, 19, 56, tzinfo=timezone.utc)
    with group._lock(options["day_dir"]), pytest.raises(BlockingIOError):
        daily.pack_watchlist_day(**options)
    assert not options["output_dir"].exists() and not options["object_root"].exists()


@pytest.mark.parametrize(
    "change", ["wrong_sha", "corrupt_object", "missing_object", "path_escape", "directory_loss", "space"]
)
def test_restore_rejects_broken_pack_without_touching_source(closed, change, monkeypatch):
    args, _, _, options = closed
    packed = daily.pack_watchlist_day(**options)
    before = daily._day_tree(args["day_dir"])
    call = restore_options(options, packed["day_pack_sha256"], args["day_dir"].parent / "failed-restore")
    if change == "wrong_sha":
        call["expected_manifest_sha256"] = "0" * 64
    elif change in {"corrupt_object", "missing_object"}:
        blob = next(options["object_root"].rglob("*.gz"))
        if change == "corrupt_object":
            blob.write_bytes(b"invalid gzip")
        else:
            blob.unlink()  # Only this test's newly generated synthetic object.
    elif change in {"path_escape", "directory_loss"}:
        path = call["manifest_file"]
        manifest = json.loads(path.read_bytes())
        if change == "path_escape":
            manifest["files"][0]["path"] = "../escaped"
        else:
            manifest["directories"].remove("archive")
        path.write_bytes(group.series._bytes(manifest))
        call["expected_manifest_sha256"] = group.quote._sha256(path.read_bytes())
    else:
        monkeypatch.setattr(daily.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises((ValueError, OSError, EOFError)):
        daily.restore_watchlist_day(**call)
    assert before == daily._day_tree(args["day_dir"])


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "permissions"])
def test_nonprivate_or_linked_source_is_not_silently_omitted(closed, kind):
    args, _, _, options = closed
    path = args["day_dir"] / "extra"
    if kind == "symlink":
        path.symlink_to(args["day_dir"] / "prepared/request.json")
    elif kind == "hardlink":
        os.link(args["day_dir"] / "prepared/request.json", path)
    else:
        path.write_bytes(b"private source")
        path.chmod(0o644)
    with pytest.raises(ValueError):
        daily.pack_watchlist_day(**options)
    assert not options["output_dir"].exists()


@pytest.mark.parametrize("target", ["runtime", "prune", "objects"])
def test_restore_rejects_protected_or_shared_object_output(closed, target):
    args, _, _, options = closed
    packed = daily.pack_watchlist_day(**options)
    out = {"runtime": args["runtime_root"], "prune": args["prune_roots"][0], "objects": options["object_root"]}[target]
    with pytest.raises(ValueError, match="overlap"):
        daily.restore_watchlist_day(**restore_options(options, packed["day_pack_sha256"], out / "restore"))


def test_actual_allocated_space_reserve_must_still_hold(closed, monkeypatch):
    args, _, _, options = closed
    packed = daily.pack_watchlist_day(**options)
    readings = iter([SimpleNamespace(free=10**12), SimpleNamespace(free=0)])
    monkeypatch.setattr(daily.shutil, "disk_usage", lambda _: next(readings))
    with pytest.raises(ValueError, match="after allocation"):
        daily.restore_watchlist_day(
            **restore_options(options, packed["day_pack_sha256"], args["day_dir"].parent / "partial")
        )


def test_delivery_still_forbids_limiter_paths(closed):
    args, _, _, _ = closed
    with pytest.raises(ValueError, match="unsafe evidence path"):
        daily.series._file(args["day_dir"], "archive/limiter/state.json")


def test_wrong_request_or_changed_preparation_cannot_be_sealed(closed):
    args, _, _, options = closed
    with pytest.raises(ValueError, match="daily request mismatch"):
        daily.pack_watchlist_day(**{**options, "expected_request_sha256": "0" * 64})
    (args["day_dir"] / "prepared/request.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash/size mismatch"):
        daily.pack_watchlist_day(**options)
    assert not options["output_dir"].exists()


def test_cli_source_pack_and_restore_never_enter_production(closed, monkeypatch):
    args, _, _, options = closed
    monkeypatch.setattr(daily.series, "_now", lambda _: datetime(2026, 10, 30, 19, 56, tzinfo=timezone.utc))
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))

    def invoke(command, pairs):
        argv = ["research", "archive", command]
        for key, value in pairs:
            argv += ["--" + key, str(value)]
        return handle_research_command(parser.parse_args(argv), repo_base_fn=lambda: pytest.fail("production"))["data"]

    common = [
        ("object-root", options["object_root"]),
        ("runtime-root", args["runtime_root"]),
        ("prune-root", args["prune_roots"][0]),
    ]
    packed = invoke(
        "watchlist-day-pack",
        common
        + [
            ("day-dir", args["day_dir"]),
            ("expected-request-sha256", args["expected_request_sha256"]),
            ("output-dir", options["output_dir"]),
        ],
    )
    result = invoke(
        "watchlist-day-restore",
        common
        + [
            ("manifest", options["output_dir"] / "day-pack.json"),
            ("expected-manifest-sha256", packed["day_pack_sha256"]),
            ("output-dir", args["day_dir"].parent / "cli-restored"),
            ("min-free-bytes", 1),
        ],
    )
    assert result["status"] == "restored"
    with pytest.raises(FileExistsError):
        daily.pack_watchlist_day(**options)
    with pytest.raises(FileExistsError):
        daily.restore_watchlist_day(**restore_options(options, packed["day_pack_sha256"], args["day_dir"]))
