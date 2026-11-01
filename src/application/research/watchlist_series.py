"""Bound all watchlist shards to one finite plan and one persistent request budget."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import gzip
import io
import math
import os
from pathlib import Path
import re
import shutil
import stat
import uuid
import tempfile

from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.required_data_blobs import _gzip_bytes
from src.application.research import observation_series as series
from src.application.research import option_quote_capture as quote

PLAN_SCHEMA = "research.watchlist-series.v1"
BUDGET_SCHEMA = "research.watchlist-budget.v1"
EXPORT_SCHEMA = "research.watchlist-series-export.v1"
PACKED_SCHEMA = "research.watchlist-packed-export.v1"
ANCILLARY_SCHEMA = "research.market-ancillary.v1"


def _ancillary_originals(path, expected):
    if path is None and expected is None:
        return {}
    if path is None or expected is None:
        raise ValueError("ancillary file and external SHA required together")
    path = series._path(path)
    raw, manifest = series._read(path)
    if quote._sha256(raw) != expected:
        raise ValueError("external ancillary SHA mismatch")
    series._keys(manifest, {"schema_version", "files"}, "market ancillary")
    if (
        manifest["schema_version"] != ANCILLARY_SCHEMA
        or not isinstance(manifest["files"], list)
        or not manifest["files"]
    ):
        raise ValueError("nonempty explicit market ancillary files required")
    originals = {"manifest.json": raw}
    for record in manifest["files"]:
        series._keys(record, {"path", "size", "sha256"}, "market ancillary file")
        name = record["path"]
        source = series._file(path.parent, name)
        if name in originals or source.suffix != ".json" or "limiter" in Path(name).parts:
            raise ValueError("duplicate/reserved/non-JSON ancillary file")
        series._verify_record(path.parent, record)
        content, value = series._read(source)
        series._market_only(value)
        originals[name] = content
    return originals


def _copy(source, destination, record):
    path = series._file(source, record["path"])
    raw = path.read_bytes()
    if len(raw) != record["size"] or quote._sha256(raw) != record["sha256"]:
        raise ValueError("watchlist original changed")
    target = series._file(destination, record["path"])
    series._private_dir(target.parent)
    series._put(target, raw)


def _files(root):
    return [series._record(root, path) for path in sorted(root.rglob("*")) if path.is_file()]


def _slots(prepared, budget):
    slots = {}
    for batch_id, plan, _ in prepared:
        for observation in plan["observations"]:
            market = observation["market"]
            key = (market, observation["expected_at_utc"])
            slot = slots.setdefault(
                key,
                {
                    "market": market,
                    "expected_at_utc": observation["expected_at_utc"],
                    "deadline_utc": series._stamp(series._slot_window(observation)[1]),
                    "batches": [],
                    "option_codes": [],
                    "normal_calls": 0,
                },
            )
            if set(slot["option_codes"]) & set(observation["option_codes"]):
                raise ValueError("contract duplicated across shards")
            slot["batches"].append(batch_id)
            slot["option_codes"].extend(observation["option_codes"])
            slot["normal_calls"] += int(bool(observation["option_codes"])) + 1
    for slot in slots.values():
        slot["limiter_wait_lower_bound_seconds"] = (
            math.floor((slot["normal_calls"] - 1) / quote.SNAPSHOT_LIMIT.max_calls) * quote.SNAPSHOT_LIMIT.window_sec
        )
        seconds = (series._utc(slot["deadline_utc"]) - series._utc(slot["expected_at_utc"])).total_seconds()
        if slot["normal_calls"] > budget["max_calls_per_slot"] or slot["limiter_wait_lower_bound_seconds"] >= seconds:
            raise ValueError("all shards cannot fit the declared request budget/slot; no truncation")
        slot["option_codes"].sort()
    return list(slots.values())


def build_watchlist_series(
    *,
    scope_dir: Path,
    expected_scope_sha256: str,
    calendar_file: Path,
    policy_file: Path,
    budget_file: Path,
    start_date: str,
    end_date: str,
    output_dir: Path,
    ancillary_file: Path | None = None,
    expected_ancillary_sha256: str | None = None,
):
    scope_dir, output_dir = series._path(scope_dir), series._path(output_dir)
    ancillary = _ancillary_originals(ancillary_file, expected_ancillary_sha256)
    scope_raw, scope = series._read(scope_dir / "watchlist-scope.json")
    if (
        quote._sha256(scope_raw) != expected_scope_sha256
        or scope.get("schema_version") != "research.watchlist-scope.v1"
    ):
        raise ValueError("external watchlist scope SHA/schema mismatch")
    for record in scope["files"]:
        series._verify_record(scope_dir, record)
    if not scope["batches"]:
        raise ValueError("no contracts to observe; retain discovery insufficiency")
    budget_raw, budget = series._read(budget_file)
    series._keys(budget, {"schema_version", "max_calls_per_slot", "max_bytes", "min_free_bytes"}, "watchlist budget")
    if budget["schema_version"] != BUDGET_SCHEMA or any(
        type(budget[k]) is not int or budget[k] <= 0 for k in ("max_calls_per_slot", "max_bytes", "min_free_bytes")
    ):
        raise ValueError("explicit positive watchlist budgets required")
    calendar_raw, calendars = series._read(calendar_file)
    policy_raw, policies = series._read(policy_file)
    prepared, seen = [], set()
    for batch in scope["batches"]:
        batch_id = batch["batch_id"]
        if (
            not isinstance(batch_id, str)
            or not re.fullmatch(r"(?:hk|us)-s?[0-9]{4}", batch_id)
            or batch_id in seen
            or batch["path"] != f"batches/{batch_id}/scope.json"
        ):
            raise ValueError("invalid/duplicate watchlist batch")
        # Resolve before using a batch ID in an output path.
        raw, value = series._read(series._file(scope_dir, batch["path"]))
        seen.add(batch_id)
        market = (value["contracts"][0]["option_code"] if value["contracts"] else value["underlier_codes"][0]).split(
            "."
        )[0]
        raw_inputs = {
            "scope": raw,
            "calendar": series._bytes({"markets": {market: calendars["markets"][market]}}),
            "policy": series._bytes({market: policies[market]}),
        }
        plan = series._make_plan(raw_inputs, start_date, end_date, observation_only=True)
        if any(series._utc(slot["expected_at_utc"]) < series._utc(scope["as_of_utc"]) for slot in plan["observations"]):
            raise ValueError("scope must be known before every observation")
        if batch["contracts"] != len(value["contracts"]):
            raise ValueError("batch contract count differs")
        prepared.append((batch_id, plan, raw_inputs))
    slots = _slots(prepared, budget)
    series._outside_checkout(output_dir)
    series._private_dir(output_dir, fresh=True)
    series._private_dir(output_dir / "scope")
    for record in scope["files"]:
        _copy(scope_dir, output_dir / "scope", record)
    series._put(output_dir / "scope/watchlist-scope.json", scope_raw)
    for name, content in ancillary.items():
        target = series._file(output_dir / "scope/supplemental", name)
        series._private_dir(target.parent)
        series._put(target, content)
    series._put(output_dir / "budget.json", budget_raw)
    series._put(output_dir / "calendar.json", calendar_raw)
    series._put(output_dir / "policy.json", policy_raw)
    batches = []
    for batch_id, plan, inputs in prepared:
        root = series._private_dir(output_dir / "batches" / batch_id)
        series._private_dir(root / "inputs")
        for key, raw in inputs.items():
            series._put(root / plan["inputs"][key]["path"], raw)
        digest = quote._write_once(root / "series-plan.json", series._bytes(plan))
        batches.append({"batch_id": batch_id, "series_plan_sha256": digest})
    manifest = {
        "schema_version": PLAN_SCHEMA,
        "scope_sha256": expected_scope_sha256,
        "scope_status": scope["status"],
        "scope_missing_reasons": scope["missing_reasons"],
        "start_date": start_date,
        "end_date": end_date,
        "budget": budget,
        "batches": batches,
        "slots": slots,
        "files": _files(output_dir),
        "timing_note": "Limiter delay is a lower bound; provider latency/shared quota require deployment preflight.",
    }
    digest = quote._write_once(output_dir / "watchlist-series.json", series._bytes(manifest))
    return {**manifest, "watchlist_series_sha256": digest}


def _load(root, expected):
    root = series._path(root)
    raw, plan = series._read(root / "watchlist-series.json")
    if quote._sha256(raw) != expected or plan.get("schema_version") != PLAN_SCHEMA:
        raise ValueError("external watchlist-series SHA/schema mismatch")
    for record in plan["files"]:
        series._verify_record(root, record)
    if plan["budget"] != series._read(root / "budget.json")[1]:
        raise ValueError("budget original differs")
    if not plan["batches"] or len({b["batch_id"] for b in plan["batches"]}) != len(plan["batches"]):
        raise ValueError("empty/duplicate frozen batch roster")
    scope_raw, scope = series._read(root / "scope/watchlist-scope.json")
    if (
        quote._sha256(scope_raw) != plan["scope_sha256"]
        or plan["scope_status"] != scope["status"]
        or plan["scope_missing_reasons"] != scope["missing_reasons"]
    ):
        raise ValueError("watchlist scope original mismatch")
    if [b["batch_id"] for b in scope["batches"]] != [b["batch_id"] for b in plan["batches"]]:
        raise ValueError("watchlist scope batch roster differs")
    prepared = []
    for index, batch in enumerate(plan["batches"]):
        if not isinstance(batch["batch_id"], str) or not re.fullmatch(r"(?:hk|us)-s?[0-9]{4}", batch["batch_id"]):
            raise ValueError("invalid batch ID")
        child, _, inputs = series._load_plan(root / "batches" / batch["batch_id"], batch["series_plan_sha256"])
        if (
            inputs["scope"] != series._read(series._file(root / "scope", scope["batches"][index]["path"]))[0]
            or child["start_date"] != plan["start_date"]
            or child["end_date"] != plan["end_date"]
        ):
            raise ValueError("watchlist child scope/date mismatch")
        prepared.append((batch["batch_id"], child, inputs))
    if _slots(prepared, plan["budget"]) != plan["slots"]:
        raise ValueError("watchlist slot roster differs")
    return raw, plan


def _windows(path, expected, plan_hash):
    raw, value = series._read(path)
    if quote._sha256(raw) != expected:
        raise ValueError("external watchlist safe-window SHA mismatch")
    series._keys(value, {"schema_version", "watchlist_series_sha256", "intervals"}, "watchlist safe-window")
    if value["schema_version"] != "research.watchlist-safe-window.v1" or value["watchlist_series_sha256"] != plan_hash:
        raise ValueError("watchlist safe-window plan mismatch")
    derived = {
        "schema_version": "research.safe-window.v1",
        "series_plan_sha256": plan_hash,
        "intervals": value["intervals"],
    }
    previous = None
    for item in derived["intervals"]:
        series._keys(item, {"start_utc", "end_utc"}, "watchlist interval")
        start, end = series._utc(item["start_utc"]), series._utc(item["end_utc"])
        if start >= end or previous is not None and start < previous:
            raise ValueError("watchlist safe intervals invalid")
        previous = end
    if previous is None:
        raise ValueError("nonempty explicit safe intervals required")
    return raw, derived


def _storage(archive_root, budget_roots, budget):
    roots = [series._path(p) for p in budget_roots]
    if not roots or not any(archive_root == r or r in archive_root.parents for r in roots):
        raise ValueError("budget roots must include research archive")
    selected = [r for r in set(roots) if not any(other in r.parents for other in roots)]
    logical, allocated, inodes = 0, 0, set()
    for root in selected:
        if not root.is_dir():
            raise ValueError("existing research budget root required")

        def unreadable(error):
            raise error

        # Installation roots contain venv links: count each link itself, never its target.
        # Unlike rglob, walk must fail when a private subtree cannot be accounted for.
        for parent, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
            for path in (Path(parent), *(Path(parent) / name for name in directories + files)):
                info = path.lstat()
                key = (info.st_dev, info.st_ino)
                if key in inodes:
                    continue
                if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    logical += info.st_size
                allocated += info.st_blocks * 512
                inodes.add(key)
    # Small gzip files still consume allocation blocks; sparse files must retain the logical ceiling.
    used = max(logical, allocated)
    free = shutil.disk_usage(archive_root if archive_root.exists() else archive_root.parent).free
    if used >= budget["max_bytes"] or free < budget["min_free_bytes"]:
        raise ValueError("research storage budget exceeded; no provider calls allowed")
    return {"used_bytes": used, "logical_bytes": logical, "allocated_bytes": allocated, "free_bytes": free}


@contextmanager
def _lock(root):
    descriptor = os.open(series._path(root / "collector.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def collect_watchlist_series(
    *,
    plan_dir: Path,
    expected_plan_sha256: str,
    safe_window_file: Path,
    expected_safe_window_sha256: str,
    archive_root: Path,
    budget_roots: list[Path],
    runtime_root: Path,
    prune_roots: list[Path],
    host: str,
    port: int,
    confirm=False,
    clock=None,
    gateway_factory=None,
    ancillary_file=None,
    expected_ancillary_sha256=None,
):
    plan_dir, archive_root = series._path(plan_dir), series._path(archive_root)
    raw, plan = _load(plan_dir, expected_plan_sha256)
    ancillary = _ancillary_originals(ancillary_file, expected_ancillary_sha256)
    safe_raw, safe = _windows(safe_window_file, expected_safe_window_sha256, expected_plan_sha256)
    series._outside_checkout(archive_root)
    series._disjoint(archive_root, [plan_dir, runtime_root, *prune_roots])
    if not prune_roots or not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("explicit runtime/prune roots and endpoint required")
    storage = _storage(archive_root, budget_roots, plan["budget"])
    if not confirm:
        return {"status": "preview", "provider_called": False, "storage": storage, "slots": plan["slots"]}
    series._private_dir(archive_root)
    results = []
    with _lock(archive_root):
        ancillary_dir = plan_dir / "scope"
        if ancillary:
            # Freeze one combined input before any shard is initialized; never amend a past observation.
            ancillary_dir = archive_root / "ancillary-source"
            series._private_dir(ancillary_dir)
            for record in _files(plan_dir / "scope"):
                _copy(plan_dir / "scope", ancillary_dir, record)
            for name, content in ancillary.items():
                target = series._file(ancillary_dir / "captured", name)
                series._private_dir(target.parent)
                series._put(target, content)
        series._put(archive_root / "watchlist-series.json", raw)
        series._put(archive_root / "safe-window.json", safe_raw)
        isolation = {
            "runtime_root": str(series._path(runtime_root)),
            "prune_roots": sorted({str(series._path(p)) for p in prune_roots}),
            "budget_roots": sorted({str(series._path(p)) for p in budget_roots}),
        }
        series._put(archive_root / "local-isolation.json", series._bytes(isolation))
        implementation = series._implementation()
        series._put(archive_root / "implementation.json", series._bytes(implementation))
        for record in plan["files"]:
            _copy(plan_dir, archive_root, record)
        # Initialize every shard before requesting anything, so export retains even never-run shards.
        for batch in plan["batches"]:
            batch_root = archive_root / "batches" / batch["batch_id"]
            batch_plan, batch_raw, batch_inputs = series._load_plan(batch_root, batch["series_plan_sha256"])
            window_raw = series._bytes({**safe, "series_plan_sha256": batch["series_plan_sha256"]})
            series._initialize_archive(
                batch_root,
                batch_plan,
                batch_raw,
                batch_inputs,
                window_raw,
                runtime_root,
                prune_roots,
                ancillary_dir,
            )
        for batch in plan["batches"]:
            batch_id = batch["batch_id"]
            derived = {**safe, "series_plan_sha256": batch["series_plan_sha256"]}
            window_file = archive_root / "windows" / (batch_id + ".json")
            series._private_dir(window_file.parent)
            window_raw = series._bytes(derived)
            series._put(window_file, window_raw)

            def budgeted_call(**kwargs):
                callback = kwargs.pop("call")

                def request():
                    now = series._now(clock)
                    active = [
                        slot
                        for slot in plan["slots"]
                        if batch_id in slot["batches"]
                        and series._utc(slot["expected_at_utc"]) <= now < series._utc(slot["deadline_utc"])
                    ]
                    if len(active) != 1:
                        raise ValueError("request outside frozen watchlist slot")
                    _storage(archive_root, budget_roots, plan["budget"])
                    slot = active[0]
                    key = quote._sha256(series._bytes({"market": slot["market"], "at": slot["expected_at_utc"]}))
                    requests = series._private_dir(archive_root / "requests" / key)
                    if len(list(requests.glob("*.json"))) >= plan["budget"]["max_calls_per_slot"]:
                        raise ValueError("watchlist request budget exhausted")
                    # Reserve before the owner's callback: denials are not mislabeled provider calls.
                    quote._write_once(
                        requests / (uuid.uuid4().hex + ".json"),
                        series._bytes({"reserved_at_utc": series._stamp(now), "batch_id": batch_id}),
                    )
                    return callback()

                return rate_limited_opend_call(**kwargs, call=request)

            result = series.collect_series(
                series_dir=archive_root / "batches" / batch_id,
                expected_plan_sha256=batch["series_plan_sha256"],
                safe_window_file=window_file,
                expected_safe_window_sha256=quote._sha256(window_raw),
                archive_root=archive_root / "batches" / batch_id,
                runtime_root=runtime_root,
                prune_roots=prune_roots,
                host=host,
                port=port,
                ancillary_dir=ancillary_dir,
                confirm=True,
                clock=clock,
                gateway_factory=gateway_factory or quote.build_ready_futu_quote_gateway,
                snapshot_limiter_root=archive_root / "limiter",
                rate_limited_call=budgeted_call,
            )
            results.append({"batch_id": batch_id, "result": result})
    return {"status": "collected", "watchlist_series_sha256": expected_plan_sha256, "batches": results}


def export_watchlist_series(
    *,
    series_dir: Path,
    expected_plan_sha256: str,
    cutoff: str,
    output_dir: Path,
    object_root: Path | None = None,
):
    if object_root is not None:
        return _export_compressed(
            series_dir=series_dir,
            expected_plan_sha256=expected_plan_sha256,
            cutoff=cutoff,
            output_dir=output_dir,
            object_root=object_root,
        )
    root, out = series._path(series_dir), series._path(output_dir)
    raw, plan = _load(root, expected_plan_sha256)
    series._outside_checkout(out)
    series._disjoint(out, [root])
    _, isolation = series._read(root / "local-isolation.json")
    series._disjoint(out, [isolation["runtime_root"], *isolation["prune_roots"]])
    safe_raw = series._read(root / "safe-window.json")[0]
    _windows(root / "safe-window.json", quote._sha256(safe_raw), expected_plan_sha256)
    implementation = series._read(root / "implementation.json")[1]
    if implementation != series._implementation():
        raise ValueError("watchlist producer changed")
    cutoff = series._stamp(series._utc(cutoff))
    with _lock(root):
        series._private_dir(out, fresh=True)
        for record in plan["files"]:
            _copy(root, out, record)
        series._put(out / "watchlist-series.json", raw)
        series._put(out / "safe-window.json", safe_raw)
        series._put(out / "implementation.json", series._bytes(implementation))
        batches = []
        for batch in plan["batches"]:
            destination = out / "exports" / batch["batch_id"]
            series._private_dir(destination.parent)
            result = series.export_series(
                series_dir=root / "batches" / batch["batch_id"],
                expected_plan_sha256=batch["series_plan_sha256"],
                cutoff=cutoff,
                output_dir=destination,
            )
            batches.append({**batch, "series_export_sha256": result["series_export_sha256"]})
        manifest = {
            "schema_version": EXPORT_SCHEMA,
            "watchlist_series_sha256": expected_plan_sha256,
            "producer_implementation_sha256": quote._sha256(series._bytes(implementation)),
            "safe_window_sha256": quote._sha256(safe_raw),
            "cutoff_utc": cutoff,
            "batches": batches,
            "files": _files(out),
        }
        digest = quote._write_once(out / "watchlist-export.json", series._bytes(manifest))
    return {**manifest, "watchlist_export_sha256": digest}


def _export_compressed(*, series_dir, expected_plan_sha256, cutoff, output_dir, object_root):
    root, out, objects = (series._path(path) for path in (series_dir, output_dir, object_root))
    _load(root, expected_plan_sha256)
    _, isolation = series._read(root / "local-isolation.json")
    for target in (out, objects):
        series._outside_checkout(target)
        series._disjoint(target, [root, isolation["runtime_root"], *isolation["prune_roots"]])
    series._disjoint(out, [objects])
    if out.exists():
        raise FileExistsError(out)
    series._private_dir(out.parent)
    series._private_dir(objects)
    with tempfile.TemporaryDirectory(prefix=".watchlist-export-", dir=out.parent) as name:
        temporary = Path(name) / "package"
        result = export_watchlist_series(
            series_dir=root,
            expected_plan_sha256=expected_plan_sha256,
            cutoff=cutoff,
            output_dir=temporary,
        )
        records = _files(temporary)
        storage = _store_objects(temporary, records, objects)
        series._private_dir(out, fresh=True)
        manifest = {
            "schema_version": PACKED_SCHEMA,
            "original_export_sha256": result["watchlist_export_sha256"],
            "files": records,
        }
        packed_sha = quote._write_once(out / "packed-export.json", series._bytes(manifest))
    return {
        **result,
        "packed_export_sha256": packed_sha,
        "storage": {
            **storage,
            "note": "Original export SHA unchanged; shared gzip objects retained, no source deletion.",
        },
    }


def _store_objects(root, records, objects, *, allow_limiter=False):
    new_bytes, new_objects = 0, 0
    for record in records:
        path = series._file(root, record["path"], allow_limiter=allow_limiter)
        raw = path.read_bytes()
        if len(raw) != record["size"] or quote._sha256(raw) != record["sha256"]:
            raise ValueError("original changed during compression")
        digest = record["sha256"]
        blob = objects / "sha256" / digest[:2] / (digest + ".json.gz")
        series._private_dir(blob.parent)
        compressed = _gzip_bytes(raw)
        exists = blob.exists()
        series._put(blob, compressed)
        if not exists:
            new_bytes += len(compressed)
            new_objects += 1
    return dict(
        logical_bytes=sum(record["size"] for record in records),
        new_object_bytes=new_bytes,
        new_objects=new_objects,
    )


def _object_bytes(objects, record):
    series._keys(record, {"path", "size", "sha256"}, "compressed original")
    digest = record["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid original SHA")
    if type(record["size"]) is not int or record["size"] < 0:
        raise ValueError("invalid original size")
    path = series._file(objects, f"sha256/{digest[:2]}/{digest}.json.gz")
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_mode & 0o077:
        raise ValueError("private regular compressed object required")
    with gzip.GzipFile(fileobj=io.BytesIO(path.read_bytes())) as stream:
        raw = stream.read(record["size"] + 1)
    if len(raw) != record["size"] or quote._sha256(raw) != digest:
        raise ValueError("compressed original hash/size mismatch")
    return raw
