"""Prepare one explicitly approved research day, then reuse the shard collector."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import shutil
import stat
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from src.application.research import observation_series as series
from src.application.research import option_quote_capture as quote
from src.application.research import watchlist_discovery as discovery
from src.application.research import watchlist_scope as scope
from src.application.research import watchlist_series as group

REQUEST_SCHEMA = "research.watchlist-day-request.v1"
CALENDAR_REQUEST_SCHEMA = "research.watchlist-day-request.v2"
DAY_SCHEMA = "research.watchlist-day.v1"
SCHEDULE_SCHEMA = "research.watchlist-schedule.v1"
RECURRING_SCHEMA = "research.watchlist-schedule.v2"
ARCHIVING_SCHEMA = "research.watchlist-schedule.v3"
DAY_PACK_SCHEMA = "research.watchlist-day-pack.v1"
REFERENCES = {
    "scan_input",
    "calendar",
    "policy",
    "discovery_budget",
    "observation_budget",
    "discovery_window",
    "observation_window",
}


def _reference(root, ref):
    series._keys(ref, {"path", "sha256"}, "daily input reference")
    path = series._file(root, ref["path"])
    raw, value = series._read(path)
    if quote._sha256(raw) != ref["sha256"]:
        raise ValueError("daily input SHA mismatch")
    return path, raw, value


def _load_day(root, expected_request_sha256):
    if not root.exists():
        manifest, objects = _sealed_day(root.parent, expected_request_sha256)
        contents = {
            record["path"].removeprefix("prepared/"): group._object_bytes(objects, record)
            for record in manifest["files"]
            if record["path"].startswith("prepared/") and "limiter" not in Path(record["path"]).parts
        }
        raw = contents.pop("day.json")
        day = json.loads(raw)
        actual = [
            {"path": name, "size": len(content), "sha256": quote._sha256(content)}
            for name, content in sorted(contents.items())
        ]
        if (
            day.get("schema_version") != DAY_SCHEMA
            or day.get("request_sha256") != expected_request_sha256
            or day.get("files") != actual
            or quote._sha256(contents["request.json"]) != expected_request_sha256
            or day.get("date") != manifest["date"]
        ):
            raise ValueError("sealed daily preparation originals changed")
        return raw, day
    raw, manifest = series._read(root / "day.json")
    if manifest.get("schema_version") != DAY_SCHEMA or manifest.get("request_sha256") != expected_request_sha256:
        raise ValueError("previous/current daily request mismatch")
    records = manifest["files"]
    for record in records:
        series._verify_record(root, record)
    actual = [record for record in series._inventory(root, root) if record["path"] != "day.json"]
    if records != actual or quote._sha256((root / "request.json").read_bytes()) != expected_request_sha256:
        raise ValueError("daily preparation originals changed")
    return raw, manifest


def _seal_dir(day):
    return day.parent / "recurring/sealed" / day.name


def _sealed_day(day, expected_request_sha256=None):
    _, anchor = series._read(_seal_dir(day) / "anchor.json")
    series._keys(anchor, {"day_pack_sha256", "request_sha256"}, "sealed day anchor")
    manifest = _read_day_pack(_seal_dir(day) / "day-pack.json", anchor["day_pack_sha256"], day)
    if (
        manifest.get("date") != day.name
        or manifest.get("request_sha256") != anchor["request_sha256"]
        or (expected_request_sha256 is not None and anchor["request_sha256"] != expected_request_sha256)
    ):
        raise ValueError("sealed day anchor mismatch")
    return manifest, series._path(day.parent / "objects")


def _prepared_read(root, name):
    if root.exists():
        return series._file(root, name).read_bytes()
    manifest, objects = _sealed_day(root.parent)
    record = next((r for r in manifest["files"] if r["path"] == "prepared/" + name), None)
    if record is None:
        raise ValueError("sealed prepared original missing: " + name)
    return group._object_bytes(objects, record)


def _seal_closed_days(archive_root, runtime_root, prune_roots, clock, budget_roots, budgets):
    results = []
    for request_path in sorted((archive_root / "recurring/requests").glob("*/request.json")):
        day = archive_root / request_path.parent.name
        prepared = day / "prepared"
        if (_seal_dir(day) / "anchor.json").exists():
            _sealed_day(day, quote._sha256(series._read(request_path)[0]))
            continue
        if not (prepared / "day.json").exists():
            continue
        request_raw, request = series._read(prepared / "request.json")
        digest = quote._sha256(request_raw)
        _, intervals = series._safe_window(
            prepared / "observation_window.json",
            request["observation_window"]["sha256"],
            request["scan_input"]["sha256"],
        )
        if series._now(clock) < max(end for _, end in intervals):
            continue
        for budget in budgets:
            group._storage(archive_root, budget_roots, budget)
        destination = _seal_dir(day)
        if not destination.exists():
            pack_watchlist_day(
                day_dir=day,
                expected_request_sha256=digest,
                object_root=archive_root / "objects",
                output_dir=destination,
                runtime_root=runtime_root,
                prune_roots=prune_roots,
                clock=clock,
            )
        # Compression retains the source, so its extra allocation must fit both budgets.
        for budget in budgets:
            group._storage(archive_root, budget_roots, budget)
        # A publication interrupted before anchor creation can only reuse an identical pack.
        raw, manifest = series._read(destination / "day-pack.json")
        with group._lock(day):
            if (manifest["files"], manifest["directories"]) != _day_tree(day):
                raise ValueError("sealed daily source inventory changed")
            for record in manifest["files"]:
                group._object_bytes(archive_root / "objects", record)
            anchor = {"day_pack_sha256": quote._sha256(raw), "request_sha256": digest}
            series._put(destination / "anchor.json", series._bytes(anchor))
        _sealed_day(day, digest)
        results.append({"date": day.name, **anchor, "source_retained": True})
    return results


def export_watchlist_series(*, series_dir, expected_plan_sha256, cutoff, output_dir, object_root=None):
    """Dispatch sealed-day restoration above the ordinary shard exporter."""
    root = series._path(series_dir)
    if not root.exists() and root.name == "archive" and _seal_dir(root.parent).exists():
        return export_sealed_watchlist_day(
            day_dir=root.parent,
            expected_plan_sha256=expected_plan_sha256,
            cutoff=cutoff,
            output_dir=output_dir,
            object_root=object_root,
        )
    return group.export_watchlist_series(
        series_dir=series_dir,
        expected_plan_sha256=expected_plan_sha256,
        cutoff=cutoff,
        output_dir=output_dir,
        object_root=object_root,
    )


def export_sealed_watchlist_day(*, day_dir, expected_plan_sha256, cutoff, output_dir, object_root):
    """Temporarily restore an explicitly sealed source, then reuse the ordinary exporter."""
    day, out = series._path(day_dir), series._path(output_dir)
    manifest, objects = _sealed_day(day)
    records = {r["path"]: r for r in manifest["files"]}
    isolation = json.loads(group._object_bytes(objects, records["archive/local-isolation.json"]))
    budget = json.loads(group._object_bytes(objects, records["prepared/observation_budget.json"]))
    for target in (out, out.parent):
        _day_boundaries(target, isolation["runtime_root"], isolation["prune_roots"], [day.parent])
    if out.exists():
        raise FileExistsError(out)
    series._private_dir(out.parent)
    with tempfile.TemporaryDirectory(prefix=".sealed-day-", dir=out.parent) as temporary:
        restored = Path(temporary) / "day"
        restore_watchlist_day(
            manifest_file=_seal_dir(day) / "day-pack.json",
            expected_manifest_sha256=quote._sha256(series._read(_seal_dir(day) / "day-pack.json")[0]),
            object_root=objects,
            output_dir=restored,
            runtime_root=Path(isolation["runtime_root"]),
            prune_roots=[Path(p) for p in isolation["prune_roots"]],
            min_free_bytes=budget["min_free_bytes"],
        )
        return group.export_watchlist_series(
            series_dir=restored / "archive",
            expected_plan_sha256=expected_plan_sha256,
            cutoff=cutoff,
            output_dir=out,
            object_root=object_root,
        )


def _day_tree(root):
    """Keep runtime files and empty claim directories, unlike a research delivery export."""
    files, directories = [], []
    for path in (root, *sorted(root.rglob("*"))):
        series._path(path)
        info = path.stat()
        rel = path.relative_to(root)
        native_limiter = "limiter" in rel.parts
        # Native limiter children use the owner's modes, within a private limiter root.
        if info.st_mode & 0o077 and (not native_limiter or path.name == "limiter"):
            raise ValueError("private daily archive required")
        if stat.S_ISDIR(info.st_mode):
            if path != root:
                directories.append(rel.as_posix())
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            raw = path.read_bytes()
            files.append({"path": rel.as_posix(), "size": len(raw), "sha256": quote._sha256(raw)})
        else:
            raise ValueError("regular single-link daily originals required")
    return files, directories


def _day_boundaries(target, runtime_root, prune_roots, others=()):
    if not prune_roots:
        raise ValueError("explicit prune roots required")
    series._outside_checkout(target)
    series._disjoint(target, [runtime_root, *prune_roots, *others])


def pack_watchlist_day(
    *,
    day_dir: Path,
    expected_request_sha256: str,
    object_root: Path,
    output_dir: Path,
    runtime_root: Path,
    prune_roots: list[Path],
    clock=None,
):
    """Seal an ended day without removing or modifying any source original."""
    root, objects, out = (series._path(p) for p in (day_dir, object_root, output_dir))
    for target in (root, objects, out):
        _day_boundaries(target, runtime_root, prune_roots, [p for p in (root, objects, out) if p != target])
    if len({root, objects, out}) != 3:
        raise ValueError("source, objects and output must differ")
    if out.exists():
        raise FileExistsError(out)
    with group._lock(root):
        _, day = _load_day(root / "prepared", expected_request_sha256)
        _, request = series._read(root / "prepared/request.json")
        _, intervals = series._safe_window(
            root / "prepared/observation_window.json",
            request["observation_window"]["sha256"],
            request["scan_input"]["sha256"],
        )
        end = max(b for _, b in intervals)
        if series._now(clock) < end:
            raise ValueError("daily observation window has not ended")
        files, directories = _day_tree(root)
        series._private_dir(objects)
        storage = group._store_objects(root, files, objects, allow_limiter=True)
        if (files, directories) != _day_tree(root):
            raise ValueError("daily originals changed during sealing")
        manifest = {
            "schema_version": DAY_PACK_SCHEMA,
            "date": day["date"],
            "request_sha256": expected_request_sha256,
            "observation_window_end_utc": series._stamp(end),
            "files": files,
            "directories": directories,
        }
        series._private_dir(out, fresh=True)
        digest = quote._write_once(out / "day-pack.json", series._bytes(manifest))
    return {"status": "sealed", "day_pack_sha256": digest, "storage": storage, "source_retained": True}


def _read_day_pack(manifest_file, expected_manifest_sha256, out):
    raw, manifest = series._read(manifest_file)
    if quote._sha256(raw) != expected_manifest_sha256:
        raise ValueError("external day pack SHA mismatch")
    series._keys(
        manifest,
        {
            "schema_version",
            "date",
            "request_sha256",
            "observation_window_end_utc",
            "files",
            "directories",
        },
        "daily source pack",
    )
    if manifest["schema_version"] != DAY_PACK_SCHEMA:
        raise ValueError("daily source pack schema invalid")
    files, directories = manifest["files"], manifest["directories"]
    if not isinstance(files, list) or not isinstance(directories, list):
        raise ValueError("daily file/directory inventories required")
    names = []
    for name in [*(r["path"] for r in files), *directories]:
        series._file(out, name, allow_limiter=True)
        if Path(name).as_posix() != name:
            raise ValueError("noncanonical daily path")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("duplicate daily path")
    return manifest


def restore_watchlist_day(
    *,
    manifest_file: Path,
    expected_manifest_sha256: str,
    object_root: Path,
    output_dir: Path,
    runtime_root: Path,
    prune_roots: list[Path],
    min_free_bytes: int,
):
    """Restore original bytes into a new private day; never repair or overwrite an existing day."""
    manifest_file, objects, out = (series._path(p) for p in (manifest_file, object_root, output_dir))
    for target in (manifest_file.parent, objects):
        _day_boundaries(target, runtime_root, prune_roots)
    _day_boundaries(out, runtime_root, prune_roots, [manifest_file.parent, objects])
    manifest = _read_day_pack(manifest_file, expected_manifest_sha256, out)
    if type(min_free_bytes) is not int or min_free_bytes < 1:
        raise ValueError("positive explicit free-space reserve required")
    files, directories = manifest["files"], manifest["directories"]
    if out.exists():
        raise FileExistsError(out)
    series._private_dir(out.parent)
    size = sum(r["size"] for r in files if type(r.get("size")) is int and r["size"] >= 0)
    if shutil.disk_usage(out.parent).free < size + min_free_bytes:
        raise ValueError("daily restore space reserve insufficient")
    # A failed restore stays visibly partial; it cannot replace an existing source directory.
    series._private_dir(out, fresh=True)
    for name in sorted(directories, key=lambda p: (len(Path(p).parts), p)):
        series._private_dir(series._file(out, name, allow_limiter=True))
    for record in files:
        content = group._object_bytes(objects, record)
        path = series._file(out, record["path"], allow_limiter=True)
        series._private_dir(path.parent)
        quote._write_once(path, content)
    if (files, directories) != _day_tree(out):
        raise ValueError("restored daily inventory differs")
    _, day = _load_day(out / "prepared", manifest["request_sha256"])
    if day["date"] != manifest["date"]:
        raise ValueError("restored daily date differs")
    _, request = series._read(out / "prepared/request.json")
    _, intervals = series._safe_window(
        out / "prepared/observation_window.json",
        request["observation_window"]["sha256"],
        request["scan_input"]["sha256"],
    )
    if series._stamp(max(end for _, end in intervals)) != manifest["observation_window_end_utc"]:
        raise ValueError("restored observation window differs")
    if day["watchlist_series_sha256"] is not None:
        group._load(out / "prepared/plan", day["watchlist_series_sha256"])
    if shutil.disk_usage(out.parent).free < min_free_bytes:
        raise ValueError("daily restore space reserve insufficient after allocation")
    return {
        "status": "restored",
        "day_pack_sha256": expected_manifest_sha256,
        "request_sha256": manifest["request_sha256"],
        "files": len(files),
        "logical_bytes": size,
    }


def run_watchlist_schedule(
    *,
    schedule_file: Path,
    expected_schedule_sha256: str,
    archive_root: Path,
    budget_roots: list[Path],
    runtime_root: Path,
    prune_roots: list[Path],
    host: str,
    port: int,
    confirm=False,
    clock=None,
    gateway_factory=None,
):
    """Wake one frozen multi-day schedule; only existing approved windows may call the provider."""
    schedule_file, archive_root = series._path(schedule_file), series._path(archive_root)
    raw, schedule = series._read(schedule_file)
    if quote._sha256(raw) != expected_schedule_sha256:
        raise ValueError("external schedule SHA mismatch")
    if schedule.get("schema_version") in {RECURRING_SCHEMA, ARCHIVING_SCHEMA}:
        return _run_recurring(
            schedule_file=schedule_file,
            schedule=schedule,
            raw=raw,
            archive_root=archive_root,
            budget_roots=budget_roots,
            runtime_root=runtime_root,
            prune_roots=prune_roots,
            host=host,
            port=port,
            confirm=confirm,
            clock=clock,
            gateway_factory=gateway_factory,
        )
    series._keys(schedule, {"schema_version", "requests"}, "research schedule")
    if (
        schedule["schema_version"] != SCHEDULE_SCHEMA
        or not isinstance(schedule["requests"], list)
        or not schedule["requests"]
    ):
        raise ValueError("nonempty explicit research schedule required")
    series._outside_checkout(archive_root)
    series._disjoint(archive_root, [schedule_file.parent, runtime_root, *prune_roots])
    if not prune_roots or not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("explicit runtime/prune roots and endpoint required")
    now = series._now(clock)
    selected, days = [], []
    previous = None
    last_end = None
    for ref in schedule["requests"]:
        path, _, request = _reference(schedule_file.parent, ref)
        if request.get("schema_version") not in {REQUEST_SCHEMA, CALENDAR_REQUEST_SCHEMA}:
            raise ValueError("schedule daily request schema invalid")
        calendar_key = "calendar_query" if request["schema_version"] == CALENDAR_REQUEST_SCHEMA else "calendar"
        references = (
            (REFERENCES - {"calendar"})
            | {calendar_key}
            | ({"ancillary"} if "ancillary" in request else set())
            | ({"ancillary_capture"} if "ancillary_capture" in request else set())
        )
        series._keys(request, {"schema_version", "date", "previous_day"} | references, "scheduled daily request")
        day = request["date"]
        if date.fromisoformat(day).isoformat() != day or (previous is not None and day <= previous["date"]):
            raise ValueError("schedule days must be unique and chronological")
        target = archive_root / day
        if previous is not None and request["previous_day"] != {
            "path": str(archive_root / previous["date"] / "prepared"),
            "request_sha256": previous["sha256"],
        }:
            raise ValueError("schedule must preserve previous daily tracking")
        inputs = {key: _reference(path.parent, request[key]) for key in references}
        windows = []
        for key in ("discovery_window", "observation_window"):
            _, intervals = series._safe_window(inputs[key][0], request[key]["sha256"], request["scan_input"]["sha256"])
            windows.extend(intervals)
        start, end = min(a for a, _ in windows), max(b for _, b in windows)
        days.append({"date": day, "first_window_utc": series._stamp(start), "last_window_utc": series._stamp(end)})
        last_end = end if last_end is None else max(last_end, end)
        if any(a <= now < b for a, b in windows):
            selected.append((path, ref["sha256"], target))
        previous = {"date": day, "sha256": ref["sha256"]}
    if not confirm:
        return {"status": "preview", "provider_called": False, "days": days, "active_days": len(selected)}
    results = []
    for path, digest, target in selected:
        _, active_request = series._read(path)
        for key in ("discovery_budget", "observation_budget"):
            group._storage(archive_root, budget_roots, _reference(path.parent, active_request[key])[2])
        series._private_dir(archive_root)
        results.append(
            run_watchlist_day(
                request_file=path,
                expected_request_sha256=digest,
                day_dir=target,
                budget_roots=budget_roots,
                runtime_root=runtime_root,
                prune_roots=prune_roots,
                host=host,
                port=port,
                confirm=True,
                clock=clock,
                gateway_factory=gateway_factory,
            )
        )
    return {
        "status": "expired" if now >= last_end else "checked",
        "days": days,
        "results": results,
        "note": "No provider calls outside frozen windows; no replay of missed days.",
    }


def _recurring_windows(day, windows):
    if not isinstance(windows, list) or not windows:
        raise ValueError("explicit recurring safe windows required")
    intervals = []
    for window in windows:
        series._keys(window, {"timezone", "start_local", "end_local"}, "recurring safe window")
        zone = ZoneInfo(window["timezone"])
        points = []
        for key in ("start_local", "end_local"):
            value = time.fromisoformat(window[key])
            if value.isoformat(timespec="minutes") != window[key] or value.tzinfo is not None:
                raise ValueError("recurring window requires local HH:MM")
            local = datetime.combine(day, value, zone)
            utc = local.astimezone(timezone.utc)
            if utc.astimezone(zone) != local or local.utcoffset() != local.replace(fold=1).utcoffset():
                raise ValueError("recurring window has nonexistent/ambiguous local time")
            points.append(utc)
        if points[0] >= points[1]:
            raise ValueError("recurring window must end after start on its market date")
        intervals.append(tuple(points))
    intervals.sort()
    if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
        raise ValueError("recurring safe windows overlap")
    return intervals


def _run_recurring(
    *,
    schedule_file,
    schedule,
    raw,
    archive_root,
    budget_roots,
    runtime_root,
    prune_roots,
    host,
    port,
    confirm,
    clock,
    gateway_factory,
):
    """Freeze each new day from an approved recurring policy; never backfill missed quotes."""
    series._keys(
        schedule,
        {
            "schema_version",
            "start_date",
            "timezone",
            "calendar_markets",
            "inputs",
            "discovery_windows",
            "observation_windows",
        },
        "recurring schedule",
    )
    start = date.fromisoformat(schedule["start_date"])
    if start.isoformat() != schedule["start_date"]:
        raise ValueError("recurring start date invalid")
    now = series._now(clock)
    today = now.astimezone(ZoneInfo(schedule["timezone"])).date()
    names = {"scan_input", "policy", "discovery_budget", "observation_budget"} | (
        {"ancillary_capture"} if "ancillary_capture" in schedule["inputs"] else set()
    )
    series._keys(schedule["inputs"], names, "recurring inputs")
    inputs = {key: _reference(schedule_file.parent, schedule["inputs"][key]) for key in names}
    if "ancillary_capture" in inputs:
        from src.application.research.market_ancillary import validate_policy

        validate_policy(inputs["ancillary_capture"][2])
    source, _, seed = inputs["scan_input"]
    if seed.get("schema_version") != scope.SCAN_INPUT_SCHEMA:
        raise ValueError("recurring discovery requires original scan-input v1")
    scope.build_watchlist_scope(input_file=source, expected_input_sha256=schedule["inputs"]["scan_input"]["sha256"])
    normalized, originals, _ = scope._scan_input(source.parent, seed, now)
    if series._utc(seed["as_of_utc"]) > now:
        raise ValueError("future recurring scan input")
    discovery.validate_calendar_query(
        {
            "schema_version": discovery.CALENDAR_QUERY_SCHEMA,
            "markets": schedule["calendar_markets"],
            "start": start.isoformat(),
            "end": max(start, today).isoformat(),
        },
        {row["underlier_code"].split(".")[0] for row in normalized["symbols"]},
    )
    for key in ("previous_tracking", "simulation_tracking"):
        if seed.get(key) is not None:
            _, content = scope._reference(source.parent, seed[key], now)
            originals[seed[key]["path"]] = content
    series._outside_checkout(archive_root)
    series._disjoint(archive_root, [schedule_file.parent, runtime_root, *prune_roots])
    if not prune_roots or not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("explicit runtime/prune roots and endpoint required")
    for key, schema, limit in (
        ("discovery_budget", discovery.BUDGET_SCHEMA, "max_calls"),
        ("observation_budget", group.BUDGET_SCHEMA, "max_calls_per_slot"),
    ):
        budget = inputs[key][2]
        series._keys(budget, {"schema_version", limit, "max_bytes", "min_free_bytes"}, "recurring budget")
        if budget["schema_version"] != schema or any(
            type(budget[k]) is not int or budget[k] <= 0 for k in (limit, "max_bytes", "min_free_bytes")
        ):
            raise ValueError("positive recurring budget required")
        group._storage(archive_root, budget_roots, budget)
    days = []
    # The US close can occur on the following Asian date; keep yesterday's active slot.
    for day in (today - timedelta(days=1), today):
        discovery_slots = _recurring_windows(day, schedule["discovery_windows"])
        observation_slots = _recurring_windows(day, schedule["observation_windows"])
        if max(b for _, b in discovery_slots) > min(a for a, _ in observation_slots):
            raise ValueError("recurring discovery overlaps observation windows")
        if day >= start:
            days.append((day, discovery_slots, observation_slots))
    if not confirm:
        return {
            "status": "preview",
            "recurring": True,
            "provider_called": False,
            "active_days": [day.isoformat() for day, a, b in days if any(x <= now < y for x, y in a + b)],
        }
    series._private_dir(archive_root)
    control = series._private_dir(archive_root / "recurring")
    with group._lock(control):
        series._put(control / "schedule.json", raw)
        gaps_root = series._private_dir(control / "gaps")
        results = []
        for day, discovery_slots, observation_slots in days:
            if not any(a <= now < b for a, b in discovery_slots + observation_slots):
                continue
            day_text = day.isoformat()
            request_root = control / "requests" / day_text
            request_path = request_root / "request.json"
            if not request_path.exists():
                if request_root.exists():
                    results.append(
                        {"date": day_text, "status": "abandoned", "reason": "request_publication_interrupted"}
                    )
                    continue
                if (gaps_root / (day_text + ".json")).exists():
                    continue
                if not any(a <= now < b for a, b in discovery_slots):
                    series._put(
                        gaps_root / (day_text + ".json"),
                        series._bytes(
                            {
                                "date": day_text,
                                "status": "missed",
                                "reason": "daily_preparation_missing_scope_unknown",
                                "recorded_at_utc": series._stamp(now),
                            }
                        ),
                    )
                    continue
                previous = None
                cursor = start
                while cursor < day:
                    prepared = archive_root / cursor.isoformat() / "prepared"
                    if (prepared / "day.json").exists() or _seal_dir(prepared.parent).exists():
                        prior_raw = _prepared_read(prepared, "request.json")
                        prior_sha = quote._sha256(prior_raw)
                        _load_day(prepared, prior_sha)
                        previous = {"path": str(prepared), "request_sha256": prior_sha}
                    else:
                        marker = gaps_root / (cursor.isoformat() + ".json")
                        if not marker.exists():
                            series._put(
                                marker,
                                series._bytes(
                                    {
                                        "date": cursor.isoformat(),
                                        "status": "missed",
                                        "reason": "daily_preparation_missing_or_interrupted_scope_unknown",
                                        "recorded_at_utc": series._stamp(now),
                                    }
                                ),
                            )
                    cursor += timedelta(days=1)
                series._private_dir(request_root, fresh=True)
                request = {"schema_version": CALENDAR_REQUEST_SCHEMA, "date": day_text, "previous_day": previous}
                source_root = series._private_dir(request_root / "source")
                for name, content in originals.items():
                    dest = series._file(source_root, name)
                    series._private_dir(dest.parent)
                    series._put(dest, content)
                derived = {**seed, **({"previous_tracking": None} if previous is not None else {})}
                seed_sha = quote._write_once(source_root / "scan-input.json", series._bytes(derived))
                request["scan_input"] = {"path": "source/scan-input.json", "sha256": seed_sha}
                for key in names - {"scan_input"}:
                    digest = quote._write_once(request_root / (key + ".json"), inputs[key][1])
                    request[key] = {"path": key + ".json", "sha256": digest}
                generated = {
                    "calendar_query": {
                        "schema_version": discovery.CALENDAR_QUERY_SCHEMA,
                        "markets": schedule["calendar_markets"],
                        "start": day_text,
                        "end": day_text,
                    }
                }
                for key, slots in (("discovery_window", discovery_slots), ("observation_window", observation_slots)):
                    generated[key] = {
                        "schema_version": "research.safe-window.v1",
                        "series_plan_sha256": seed_sha,
                        "intervals": [{"start_utc": series._stamp(a), "end_utc": series._stamp(b)} for a, b in slots],
                    }
                for key, value in generated.items():
                    digest = quote._write_once(request_root / (key + ".json"), series._bytes(value))
                    request[key] = {"path": key + ".json", "sha256": digest}
                request_raw = series._bytes(request)
                quote._write_once(
                    request_root / "binding.json",
                    series._bytes(
                        {
                            "schedule_sha256": quote._sha256(raw),
                            "request_sha256": quote._sha256(request_raw),
                        }
                    ),
                )
                quote._write_once(request_path, request_raw)
            request_raw, _ = series._read(request_path)
            if series._read(request_root / "binding.json")[1] != {
                "schedule_sha256": quote._sha256(raw),
                "request_sha256": quote._sha256(request_raw),
            }:
                raise ValueError("recurring daily request binding differs")
            results.append(
                run_watchlist_day(
                    request_file=request_path,
                    expected_request_sha256=quote._sha256(request_raw),
                    day_dir=archive_root / day_text,
                    budget_roots=budget_roots,
                    runtime_root=runtime_root,
                    prune_roots=prune_roots,
                    host=host,
                    port=port,
                    confirm=True,
                    clock=clock,
                    gateway_factory=gateway_factory,
                )
            )
        sealed = (
            _seal_closed_days(
                archive_root,
                runtime_root,
                prune_roots,
                clock,
                budget_roots,
                [inputs[key][2] for key in ("discovery_budget", "observation_budget")],
            )
            if schedule["schema_version"] == ARCHIVING_SCHEMA
            and not any(
                a <= now < b
                for _, discovery_slots, observation_slots in days
                for a, b in discovery_slots + observation_slots
            )
            else None
        )
        return {
            "status": "checked",
            "recurring": True,
            "results": results,
            "continuity_gaps": [series._read(path)[1] for path in sorted(gaps_root.glob("*.json"))],
            **({"sealed_days": sealed or []} if schedule["schema_version"] == ARCHIVING_SCHEMA else {}),
        }


def run_watchlist_day(
    *,
    request_file: Path,
    expected_request_sha256: str,
    day_dir: Path,
    budget_roots: list[Path],
    runtime_root: Path,
    prune_roots: list[Path],
    host: str,
    port: int,
    confirm=False,
    clock=None,
    gateway_factory=None,
):
    """The caller schedules frozen day requests; this never selects a live/latest input."""
    request_file, day_dir = series._path(request_file), series._path(day_dir)
    request_raw, request = series._read(request_file)
    if quote._sha256(request_raw) != expected_request_sha256:
        raise ValueError("external daily request SHA mismatch")
    calendar_key = "calendar_query" if request.get("schema_version") == CALENDAR_REQUEST_SCHEMA else "calendar"
    references = (
        (REFERENCES - {"calendar"})
        | {calendar_key}
        | ({"ancillary"} if "ancillary" in request else set())
        | ({"ancillary_capture"} if "ancillary_capture" in request else set())
    )
    series._keys(request, {"schema_version", "date", "previous_day"} | references, "daily request")
    if (
        request["schema_version"] not in {REQUEST_SCHEMA, CALENDAR_REQUEST_SCHEMA}
        or date.fromisoformat(request["date"]).isoformat() != request["date"]
    ):
        raise ValueError("daily request schema/date invalid")
    inputs = {key: _reference(request_file.parent, request[key]) for key in references}
    capture_policy = inputs.get("ancillary_capture", (None, None, None))[2]
    if capture_policy is not None:
        from src.application.research.market_ancillary import validate_policy

        validate_policy(capture_policy)
    ancillary = (
        group._ancillary_originals(inputs["ancillary"][0], request["ancillary"]["sha256"])
        if "ancillary" in inputs
        else {}
    )
    input_path, _, payload = inputs["scan_input"]
    if payload.get("schema_version") != scope.SCAN_INPUT_SCHEMA:
        raise ValueError("daily discovery requires original scan-input v1")
    now = series._now(clock)
    scope.build_watchlist_scope(input_file=input_path, expected_input_sha256=request["scan_input"]["sha256"])
    if series._utc(payload["as_of_utc"]) > now:
        raise ValueError("future daily scan input")
    if calendar_key == "calendar_query":
        normalized, _, _ = scope._scan_input(input_path.parent, payload, now)
        query = inputs[calendar_key][2]
        discovery.validate_calendar_query(
            query, {item["underlier_code"].split(".")[0] for item in normalized["symbols"]}
        )
        if not query["start"] <= request["date"] <= query["end"]:
            raise ValueError("calendar query does not cover daily date")
    _, discovery_intervals = series._safe_window(
        inputs["discovery_window"][0], request["discovery_window"]["sha256"], request["scan_input"]["sha256"]
    )
    _, observation_intervals = series._safe_window(
        inputs["observation_window"][0], request["observation_window"]["sha256"], request["scan_input"]["sha256"]
    )
    # Discovery must finish before any sampling window; late planning cannot shrink that day's scope.
    if max(end for _, end in discovery_intervals) > min(start for start, _ in observation_intervals):
        raise ValueError("daily discovery overlaps observation windows")
    series._outside_checkout(day_dir)
    series._disjoint(day_dir, [request_file.parent, input_path.parent, runtime_root, *prune_roots])
    if not prune_roots or not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("explicit runtime/prune roots and endpoint required")
    for key, schema, limit in (
        ("discovery_budget", discovery.BUDGET_SCHEMA, "max_calls"),
        ("observation_budget", group.BUDGET_SCHEMA, "max_calls_per_slot"),
    ):
        budget = inputs[key][2]
        series._keys(budget, {"schema_version", limit, "max_bytes", "min_free_bytes"}, "daily budget")
        if budget["schema_version"] != schema or any(
            type(budget[k]) is not int or budget[k] <= 0 for k in (limit, "max_bytes", "min_free_bytes")
        ):
            raise ValueError("positive explicit daily budget required")
        group._storage(day_dir, budget_roots, budget)
    previous = request["previous_day"]
    prior = None
    if previous is not None:
        series._keys(previous, {"path", "request_sha256"}, "previous research day")
        if not isinstance(previous["path"], str) or not Path(previous["path"]).is_absolute():
            raise ValueError("explicit absolute previous prepared directory required")
        prior_root = series._path(Path(previous["path"]))
        _, prior = _load_day(prior_root, previous["request_sha256"])
        if prior["date"] >= request["date"] or series._utc(prior["prepared_at_utc"]) > now:
            raise ValueError("previous research day is future/out of order")
        if payload["previous_tracking"] is not None:
            raise ValueError("choose previous daily tracking or explicit input tracking, not both")
    prepared = day_dir / "prepared"
    if not prepared.exists() and any(start <= now < end for start, end in discovery_intervals):
        normalized, _, _ = scope._scan_input(input_path.parent, payload, now)
        for symbol in normalized["symbols"]:
            market = symbol["underlier_code"].split(".")[0]
            policy = series.INSTRUMENT_POLICY_REGISTRY[f"{market}:standard_equity_option"]
            if now.astimezone(ZoneInfo(policy["timezone"])).date().isoformat() != request["date"]:
                raise ValueError("daily discovery market date differs from request date")
    preview = {
        "status": "preview",
        "provider_called": False,
        "date": request["date"],
        "preparation_exists": prepared.exists(),
        "roster_policy": "explicit_frozen_export_not_live_config",
    }
    if not confirm:
        return preview
    if not prepared.exists() and _seal_dir(day_dir).exists():
        _load_day(prepared, expected_request_sha256)
        return {"status": "sealed", "provider_called": False, "date": request["date"]}
    series._private_dir(day_dir)
    with group._lock(day_dir):
        if (day_dir / "missed.json").exists():
            _, missed = series._read(day_dir / "missed.json")
            if missed.get("request_sha256") != expected_request_sha256:
                raise ValueError("missed daily request mismatch")
            return missed
        if prepared.exists():
            if not (prepared / "day.json").exists():
                return {
                    "status": "abandoned",
                    "provider_called": False,
                    "date": request["date"],
                    "reason": "preparation_interrupted_no_automatic_retry",
                }
            _, manifest = _load_day(prepared, expected_request_sha256)
        else:
            if not any(start <= now < end for start, end in discovery_intervals):
                outcome = {
                    "status": "missed" if now >= discovery_intervals[-1][1] else "pending",
                    "provider_called": False,
                    "date": request["date"],
                    "reason": "outside_daily_discovery_window",
                }
                if outcome["status"] == "missed":
                    series._put(
                        day_dir / "missed.json", series._bytes({**outcome, "request_sha256": expected_request_sha256})
                    )
                return outcome
            series._private_dir(prepared, fresh=True)
            series._put(prepared / "request.json", request_raw)
            for key, (_, raw, _) in inputs.items():
                series._put(prepared / (key + ".json"), raw)
            for name, content in ancillary.items():
                target = series._file(prepared / "ancillary", name)
                series._private_dir(target.parent)
                series._put(target, content)
            source = series._private_dir(prepared / "source")
            _, originals, _ = scope._scan_input(input_path.parent, payload, now)
            for key in ("previous_tracking", "simulation_tracking"):
                if payload.get(key) is not None:
                    _, content = scope._reference(input_path.parent, payload[key], now)
                    originals[payload[key]["path"]] = content
            derived = {**payload, "as_of_utc": series._stamp(now)}
            if prior is not None:
                prior_raw = _prepared_read(prior_root, "scope/tracking.json")
                previous_path = "daily-previous-tracking.json"
                if previous_path in originals:
                    raise ValueError("daily previous tracking path conflicts with source")
                originals[previous_path] = prior_raw
                derived["previous_tracking"] = {
                    "path": previous_path,
                    "sha256": quote._sha256(prior_raw),
                    "pointer": "",
                    "observed_at_utc": prior["prepared_at_utc"],
                }
            for path, raw in originals.items():
                target = series._file(source, path)
                series._private_dir(target.parent)
                series._put(target, raw)
            input_sha = quote._write_once(source / "scan-input.json", series._bytes(derived))
            discovery_safe = {**inputs["discovery_window"][2], "series_plan_sha256": input_sha}
            discovery_sha = quote._write_once(prepared / "derived-discovery-window.json", series._bytes(discovery_safe))
            result = discovery.discover_watchlist(
                input_file=source / "scan-input.json",
                expected_input_sha256=input_sha,
                safe_window_file=prepared / "derived-discovery-window.json",
                expected_safe_window_sha256=discovery_sha,
                budget_file=prepared / "discovery_budget.json",
                expected_budget_sha256=request["discovery_budget"]["sha256"],
                output_dir=prepared / "discovery",
                budget_roots=budget_roots,
                runtime_root=runtime_root,
                prune_roots=prune_roots,
                host=host,
                port=port,
                confirm=True,
                clock=clock,
                gateway_factory=gateway_factory,
                calendar_query=inputs[calendar_key][2] if calendar_key == "calendar_query" else None,
            )
            if calendar_key == "calendar_query":
                calendar_path = prepared / "discovery/calendar.json"
                if not calendar_path.is_file():
                    raise ValueError("calendar unavailable; discovery retained, no automatic retry")
                series._put(prepared / "calendar.json", calendar_path.read_bytes())
            scoped = scope.build_watchlist_scope(
                input_file=prepared / "discovery/scope-input.json",
                expected_input_sha256=result["scope_input_sha256"],
                output_dir=prepared / "scope",
            )
            scope_sha = quote._sha256((prepared / "scope/watchlist-scope.json").read_bytes())
            plan = (
                group.build_watchlist_series(
                    scope_dir=prepared / "scope",
                    expected_scope_sha256=scope_sha,
                    calendar_file=prepared / "calendar.json",
                    policy_file=prepared / "policy.json",
                    budget_file=prepared / "observation_budget.json",
                    start_date=request["date"],
                    end_date=request["date"],
                    output_dir=prepared / "plan",
                    **(
                        {
                            "ancillary_file": prepared / "ancillary/manifest.json",
                            "expected_ancillary_sha256": request["ancillary"]["sha256"],
                        }
                        if ancillary
                        else {}
                    ),
                )
                if scoped["batches"]
                else None
            )
            for slot in plan["slots"] if plan is not None else ():
                start, end = series._utc(slot["expected_at_utc"]), series._utc(slot["deadline_utc"])
                if capture_policy is not None:
                    from src.application.research.market_ancillary import with_underliers

                    start -= timedelta(seconds=capture_policy["lead_seconds"])
                    if capture_policy["max_calls"] < 3 + len(
                        with_underliers(prepared / "plan", slot)["underlier_codes"]
                    ):
                        raise ValueError("ancillary request budget cannot cover daily scope")
                if not any(a <= start and end <= b for a, b in observation_intervals):
                    raise ValueError(
                        "observation window does not cover planned slot; plan retained, no automatic retry"
                    )
            safe = (
                {
                    "schema_version": "research.watchlist-safe-window.v1",
                    "watchlist_series_sha256": plan["watchlist_series_sha256"],
                    "intervals": inputs["observation_window"][2]["intervals"],
                }
                if plan is not None
                else None
            )
            safe_sha = (
                quote._write_once(prepared / "safe-window.json", series._bytes(safe)) if safe is not None else None
            )
            manifest = {
                "schema_version": DAY_SCHEMA,
                "date": request["date"],
                "request_sha256": expected_request_sha256,
                "prepared_at_utc": series._stamp(series._now(clock)),
                "discovery_status": result["status"],
                "watchlist_series_sha256": plan["watchlist_series_sha256"] if plan is not None else None,
                "safe_window_sha256": safe_sha,
                "files": series._inventory(prepared, prepared),
            }
            quote._write_once(prepared / "day.json", series._bytes(manifest))
        if manifest["watchlist_series_sha256"] is None:
            return {
                "status": "no_observable_scope",
                "date": request["date"],
                "manifest": manifest,
                "reason": "retain_discovery_and_scope_gaps_no_quote_denominator_known",
            }
        supplemental = {}
        if capture_policy is not None:
            from src.application.research.market_ancillary import capture_market_ancillary, with_underliers

            _, plan = group._load(prepared / "plan", manifest["watchlist_series_sha256"])
            now = series._now(clock)
            # A daily request has one market/slot; multiple slots need their own immutable manifests.
            if len(plan["slots"]) != 1:
                raise ValueError("ancillary daily capture requires exactly one market observation")
            slot = with_underliers(prepared / "plan", plan["slots"][0])
            outcome = capture_market_ancillary(
                plan_sha256=manifest["watchlist_series_sha256"],
                slot=slot,
                policy=capture_policy,
                output_dir=day_dir / "market-ancillary",
                budget_roots=budget_roots,
                host=host,
                port=port,
                clock=clock,
                gateway_factory=gateway_factory,
            )
            if now < series._utc(slot["expected_at_utc"]):
                return {
                    "status": "prepared_and_checked",
                    "date": request["date"],
                    "manifest": manifest,
                    "ancillary_capture": outcome,
                    "collection": None,
                }
            if outcome["manifest_sha256"] is not None:
                supplemental = {
                    "ancillary_file": day_dir / "market-ancillary/manifest.json",
                    "expected_ancillary_sha256": outcome["manifest_sha256"],
                }
        result = group.collect_watchlist_series(
            plan_dir=prepared / "plan",
            expected_plan_sha256=manifest["watchlist_series_sha256"],
            safe_window_file=prepared / "safe-window.json",
            expected_safe_window_sha256=manifest["safe_window_sha256"],
            archive_root=day_dir / "archive",
            budget_roots=budget_roots,
            runtime_root=runtime_root,
            prune_roots=prune_roots,
            host=host,
            port=port,
            confirm=True,
            clock=clock,
            gateway_factory=gateway_factory,
            **supplemental,
        )
        return {
            "status": "prepared_and_checked",
            "date": request["date"],
            "day_sha256": quote._sha256((prepared / "day.json").read_bytes()),
            "manifest": manifest,
            "collection": result,
        }
