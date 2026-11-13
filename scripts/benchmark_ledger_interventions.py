"""Offline public-API intervention benchmark for issue #1163.

Only creates synthetic SQLite databases in a fresh temporary directory. Timing
excludes fixture preparation, initial projection and repair confirmation preview.
Run counts separately from uninstrumented timing; never pass a runtime DB path.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import cProfile
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger import api, interventions
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.repository_schema import initialize_ledger_connection

NOW_MS = 1_800_000_500_000
TARGET_ID = "active-000"
OPERATIONS = ("void_preview", "void_apply", "repair_preview", "repair_apply")


def synthetic_events(event_count: int, active_lots: int = 101) -> list[TradeEvent]:
    """Fixed order, two accounts/contracts, active opens plus closed pairs."""
    if event_count < active_lots or (event_count - active_lots) % 2:
        raise ValueError("fixture requires an even number of closed-history events")
    keys = [ContractKey.from_values(broker="futu", account=account,
        underlying_symbol=symbol, option_type="put", strike=100,
        expiration_ymd="2027-03-19") for account, symbol in (("lx", "NVDA"), ("sy", "MSFT"))]
    events: list[TradeEvent] = []
    def add(event_id: str, lot_id: str, key: ContractKey, *, closing: bool = False) -> None:
        events.append(TradeEvent(event_id=event_id,
            event_type="close" if closing else "open",
            event_time_ms=1_800_000_000_000 + len(events) * 10,
            contract_key=key, contracts=1, price=0.5 if closing else 2,
            currency="USD", source="synthetic", multiplier=100,
            lot_id=None if closing else lot_id,
            target_lot_id=lot_id if closing else None,
            raw_payload={"side": "buy", "record_id": lot_id} if closing else {"side": "sell"}))
    for i in range(active_lots):
        add(f"active-{i:03d}", f"active-lot-{i:03d}", keys[i % 2])
    for i in range((event_count - active_lots) // 2):
        add(f"history-open-{i:05d}", f"history-lot-{i:05d}", keys[i % 2])
        add(f"history-close-{i:05d}", f"history-lot-{i:05d}", keys[i % 2], closing=True)
    return events


def prepare_template(path: Path, event_count: int, active_lots: int) -> tuple[str, str]:
    repo = SQLiteOptionPositionsRepository(path)
    events = synthetic_events(event_count, active_lots)
    with repo._writer_connection(begin_immediate=True) as conn:
        for event in events:
            repo.upsert_trade_event(event, conn=conn)
    api.refresh_position_lot_projection(repo)
    lots = repo.list_position_lots()
    assert sum(str(lot["fields"].get("status")) == "open" for lot in lots) == active_lots
    confirmation = api.preview_trade_event_repair(repo, event_id=TARGET_ID,
        overrides={"price": 2.5}, reason="benchmark")["expected_input_hash"]
    digest = hashlib.sha256(json.dumps([e.to_dict() for e in events], sort_keys=True).encode()).hexdigest()
    return confirmation, digest


@contextmanager
def fresh_repo(template: Path, directory: Path, storage: str):
    """Restore identical initial state before every invocation, outside timing."""
    path = directory / f"sample-{uuid.uuid4().hex}.sqlite3"
    if storage == "disk":
        with sqlite3.connect(template) as source, sqlite3.connect(path) as dest:
            source.backup(dest)
        repo = SQLiteOptionPositionsRepository(path, initialize=False)
        try:
            yield repo
        finally:
            for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                candidate.unlink(missing_ok=True)
    else:
        uri = f"file:intervention-{uuid.uuid4().hex}?mode=memory&cache=shared"
        anchor = sqlite3.connect(uri, uri=True)
        with sqlite3.connect(template) as source:
            source.backup(anchor)
        repo = SQLiteOptionPositionsRepository(template, initialize=False)
        def connect():
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            initialize_ledger_connection(conn)
            return conn
        repo._connect = connect
        try:
            yield repo
        finally:
            anchor.close()


def invoke(repo, operation: str, confirmation: str):
    kind, action = operation.split("_")
    kwargs = {"overrides": {"price": 2.5}} if kind == "repair" else {}
    if action == "apply" and kind == "repair":
        kwargs["expected_input_hash"] = confirmation
    fn = getattr(api, f"{'record' if action == 'apply' else 'preview'}_trade_event_{kind}")
    result = fn(repo, event_id=TARGET_ID, reason="benchmark", **kwargs)
    assert result["mode"] == ("applied" if action == "apply" else "dry_run")
    return result


def count_operation(repo, operation: str, confirmation: str) -> dict:
    counts: Counter[str] = Counter()
    sites: Counter[str] = Counter()
    def profile(frame, event, arg):
        if event != "call":
            return
        name = frame.f_code.co_name
        if name in {"list_trade_events", "list_position_projection_event_rows",
                    "stored_trade_event_to_ledger_event", "_stored_trade_event_payload_to_ledger_event",
                    "_canonical_payload_to_ledger_event", "resolve_strategy_metadata"}:
            counts[name] += 1
            if "to_ledger_event" in name:
                caller = frame.f_back
                path = Path(caller.f_code.co_filename)
                try:
                    path = path.relative_to(ROOT)
                except ValueError:
                    pass
                sites[f"{name} <- {path}:{caller.f_lineno}:{caller.f_code.co_name}"] += 1
    sys.setprofile(profile)
    try:
        invoke(repo, operation, confirmation)
    finally:
        sys.setprofile(None)
    ordinary = counts["list_trade_events"]
    forced = counts["list_position_projection_event_rows"]
    return {"ordinary_history_reads": ordinary, "forced_full_history_reads": forced,
        "all_history_reads": ordinary + forced, "conversion_calls": dict(counts),
        "conversion_sites": dict(sites)}


def compare_timing_reports(before: dict, after: dict) -> list[str]:
    """Check the issue's engineering budgets against a matching local baseline."""
    failures = []
    for key in ("python", "sqlite", "host", "machine", "fixed_clock_ms"):
        if before[key] != after[key]:
            failures.append(f"environment differs: {key}")
    for report in (before, after):
        if report["mode"] != "timing" or report["warmups"] < 2 or report["samples"] < 20:
            failures.append("acceptance requires uninstrumented timing, 2 warmups and >=20 samples")
    def row_key(row):
        return row["events"], row["active_lots"], row["storage"], row["operation"]
    baseline = {row_key(row): row for row in before["results"]}
    optimized = {row_key(row): row for row in after["results"]}
    expected = {(n, 101, storage, operation) for n in (1001, 8001, 20001)
        for storage in ("memory", "disk") for operation in OPERATIONS}
    if set(baseline) != expected or set(optimized) != expected:
        failures.append("acceptance requires all four APIs, three scales and both storage modes")
    for key, row in optimized.items():
        previous = baseline.get(key)
        if previous is None:
            continue
        if row["fixture_sha256"] != previous["fixture_sha256"]:
            failures.append(f"fixture differs: {key}")
        if len(row["samples_seconds"]) < 20 or len(previous["samples_seconds"]) < 20:
            failures.append(f"insufficient raw samples: {key}")
        if row["p95_seconds"] > previous["p95_seconds"]:
            failures.append(f"p95 regressed: {key}")
        if row["events"] == 20001:
            limit = 2 if row["operation"].endswith("preview") else 5
            if row["p50_seconds"] > limit:
                failures.append(f"p50 exceeds {limit}s: {key}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1001, 8001, 20001])
    parser.add_argument("--storage", choices=["memory", "disk"], nargs="+", default=["memory", "disk"])
    parser.add_argument("--operations", choices=OPERATIONS, nargs="+", default=list(OPERATIONS))
    parser.add_argument("--active-lots", type=int, default=101)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--mode", choices=["timing", "counts", "profile"], default="timing")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, help="Compare a full timing run with unchanged-main timing")
    args = parser.parse_args()
    interventions.now_ms = lambda: NOW_MS
    report = {"schema": "ledger_interventions_benchmark.v1", "source_sha": subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "python": sys.version, "executable": Path(sys.executable).name,
        "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "sqlite": sqlite3.sqlite_version, "host": platform.platform(),
        "machine": platform.machine(), "warmups": args.warmups, "samples": args.samples,
        "mode": args.mode, "fixed_clock_ms": NOW_MS, "storage_settings": {}, "results": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ledger-intervention-benchmark-") as tmp:
        directory = Path(tmp)
        for n in args.sizes:
            template = directory / f"template-{n}.sqlite3"
            confirmation, digest = prepare_template(template, n, args.active_lots)
            for storage in args.storage:
                if storage not in report["storage_settings"]:
                    with fresh_repo(template, directory, storage) as probe:
                        conn = probe._connect()
                        try:
                            report["storage_settings"][storage] = {
                                key: conn.execute(f"PRAGMA {key}").fetchone()[0]
                                for key in ("journal_mode", "synchronous", "temp_store", "cache_size")}
                        finally:
                            conn.close()
                for operation in args.operations:
                    row = {"events": n, "active_lots": args.active_lots,
                        "fixture_sha256": digest, "storage": storage, "operation": operation}
                    if args.mode == "counts":
                        with fresh_repo(template, directory, storage) as repo:
                            row.update(count_operation(repo, operation, confirmation))
                    elif args.mode == "profile":
                        with fresh_repo(template, directory, storage) as repo:
                            profile = cProfile.Profile()
                            profile.runcall(invoke, repo, operation, confirmation)
                            profile.dump_stats(str(args.output.with_suffix(f".{storage}.{n}.{operation}.prof")))
                    else:
                        samples = []
                        for i in range(args.warmups + args.samples):
                            with fresh_repo(template, directory, storage) as repo:
                                start = time.perf_counter()
                                invoke(repo, operation, confirmation)
                                elapsed = time.perf_counter() - start
                            if i >= args.warmups:
                                samples.append(elapsed)
                        ordered = sorted(samples)
                        row.update(samples_seconds=samples, p50_seconds=statistics.median(samples),
                            p95_seconds=ordered[math.ceil(0.95 * len(ordered)) - 1])
                    report["results"].append(row)
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
                    print(json.dumps(row), flush=True)
    if args.baseline is not None:
        failures = compare_timing_reports(json.loads(args.baseline.read_text()), report)
        report["acceptance"] = {"passed": not failures, "failures": failures}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        if failures:
            raise SystemExit("; ".join(failures))


if __name__ == "__main__":
    main()
