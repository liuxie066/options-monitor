#!/usr/bin/env python3
"""Provider-free benchmark of the real execute_tick/main runtime facade.

Use -I -B with the task Python. Each sample copies the same sealed inputs before
starting the wall timer. Cold means first Tick in a fresh process (imports are
excluded); warm means subsequent Ticks in that process, each with fresh runtime
state. Neither label claims a purged operating-system page cache.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
from dataclasses import asdict
from datetime import datetime, timezone
import functools
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import traceback
from unittest.mock import patch

sys.dont_write_bytecode = True
HARNESS_ROOT = Path(__file__).resolve().parents[1]
BASE = "967498d3bf5039af8ed62a1ce808f265989f7856"
SCENARIOS = ("complete", "localized_discovery_failure")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")


def file_manifest(root):
    return {str(p.relative_to(root)): {"sha256": sha(p), "bytes": p.stat().st_size}
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def canonical(value, base):
    if isinstance(value, dict):
        return {key: canonical(item, base) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical(item, base) for item in value]
    return value.replace(str(base), "{runtime}") if isinstance(value, str) else value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def trade_event_facts(base):
    path = base / "output_shared/state/option_positions.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT * FROM trade_events ORDER BY event_id").fetchall()
    return {"count": len(rows), "sha256": digest(rows)}


@contextmanager
def observe_owners():
    """Instrument real owners through all existing imported aliases, without stubs."""
    names = [
        ("src.application.daily_decision_brief_service", "assemble_daily_decision_brief", "brief.assemble"),
        ("src.application.trades.attribution", "build_trade_attribution_view", "attribution.view"),
        ("src.application.ledger.trade_attribution", "combo_attribution_candidates_from_rows", "attribution.history"),
        ("src.application.multi_tick.required_data_prefetch", "prefetch_required_data", "required_data.prefetch"),
        ("src.application.required_data_snapshot", "seal_required_data_snapshot", "required_data.seal"),
        ("src.application.tick_account_execution", "run_tick_account_execution", "tick.accounts"),
        ("src.application.tick_notification_flow", "run_tick_notification_flow", "tick.notification"),
        ("src.application.tick_notification_flow", "_prepare_daily_brief_notification", "brief.prepare"),
        ("src.application.multi_account_tick", "prepare_tick_briefs", "brief.prepare_source"),
        ("src.application.multi_tick_finalization", "finalize_multi_tick_run", "tick.finalize"),
        ("src.application.multi_tick_finalization", "finalize_no_account_notification", "tick.finalize_no_message"),
        ("src.application.scan_scheduler", "mark_scheduler_accounts", "scheduler.commit"),
        ("src.application.tick_run_context", "complete_tick_idempotency", "idempotency.complete"),
        ("src.application.daily_decision_brief_repository", "_normalize_delivery_state", "cpu.normalize_delivery_state"),
        ("src.application.daily_decision_brief_repository", "_read_validated_successful_revision_source", "history.validate_source"),
        ("src.application.daily_decision_brief_repository", "read_combo_candidate_exposures", "history.combo_exposures"),
        ("src.application.ledger.assigned_stock_projection", "build_wheel_read_model_from_rows", "cpu.wheel_projection"),
        ("src.application.ledger.assigned_stock_projection", "build_wheel_read_model_with_capacity_from_rows", "cpu.wheel_projection_with_capacity"),
        ("src.application.daily_decision_brief_service", "_pending_attribution_for_brief", "attribution.pending_for_brief"),
    ]
    counts, elapsed, returned, details = Counter(), Counter(), defaultdict(list), {}
    with ExitStack() as stack:
        from src.infrastructure.decision_history_sqlite import DecisionHistoryStore
        for method in ("get", "connect"):
            original_method = getattr(DecisionHistoryStore, method)
            def instrument_method(original, label):
                @functools.wraps(original)
                def call(*args, **kwargs):
                    counts[label] += 1
                    return original(*args, **kwargs)
                return call
            stack.enter_context(patch.object(DecisionHistoryStore, method,
                                instrument_method(original_method, f"history.{method}")))
        for method in ("read_text", "read_bytes"):
            original_method = getattr(Path, method)
            def instrument_read(original, label):
                @functools.wraps(original)
                def call(path, *args, **kwargs):
                    result = original(path, *args, **kwargs)
                    if path.name.startswith("daily_decision_brief.") and path.name.endswith(".delivery.json"):
                        counts[label] += 1
                        counts[f"{label}.bytes"] += len(result.encode("utf-8") if isinstance(result, str) else result)
                    return result
                return call
            stack.enter_context(patch.object(Path, method,
                                instrument_read(original_method, f"io.delivery_json.{method}")))
        details["preparation_owner_scope"] = ("separate Brief source preparation then notification freezing"
            if hasattr(importlib.import_module("src.application.multi_account_tick"), "prepare_tick_briefs")
            else "legacy combined Brief preparation and notification freezing")
        details["owner_bindings"] = {}
        for module_name, name, label in names:
            module = importlib.import_module(module_name)
            if not hasattr(module, name):
                details[label] = "owner name absent"
                continue
            original = getattr(module, name)
            def wrap(original, label):
                @functools.wraps(original)
                def call(*args, **kwargs):
                    counts[label] += 1
                    started = time.perf_counter()
                    try:
                        value = original(*args, **kwargs)
                        if label == "brief.assemble":
                            returned[label].append(value)
                            details.setdefault("brief_formed_markers", []).append({"account": value.get("account"),
                                "completed_after_seconds": time.perf_counter() - details["_tick_start"],
                                "status": value.get("status"), "actionability": value.get("actionability")})
                        elif label == "tick.accounts":
                            returned[label].append({"results": [vars(x) for x in value.results],
                                "account_metrics": value.account_metrics, "snapshot_status": value.snapshot_status})
                        elif label in {"tick.finalize", "required_data.prefetch"}:
                            returned[label].append(value)
                        return value
                    finally:
                        elapsed[label] += time.perf_counter() - started
                return call
            wrapped = wrap(original, label)
            bindings = [(loaded, key) for loaded in tuple(sys.modules.values())
                        if loaded is not None and getattr(loaded, "__name__", "").startswith(("src.", "domain."))
                        for key, value in tuple(vars(loaded).items()) if value is original]
            details["owner_bindings"][label] = [f"{loaded.__name__}.{key}" for loaded, key in bindings]
            for loaded, key in bindings:
                stack.enter_context(patch.object(loaded, key, wrapped))
        yield counts, elapsed, returned, details


def worker(args):
    sys.path[:0] = [str(args.source_root), str(HARNESS_ROOT / "tests"), str(HARNESS_ROOT)]
    from tick_execution_benchmark_fixtures import ACCOUNTS, NOW, offline_tick_boundaries
    from src.application import multi_account_tick as tick
    # Import entry points before observation so imported aliases are measured.
    importlib.import_module("src.application.tick_notification_flow")
    for index in range(args.samples):
        name = f"{args.scenario}-{index}"
        sample_dir = args.output_dir / name
        sample_dir.mkdir(parents=True, exist_ok=False)
        base = sample_dir / "runtime"
        shutil.copytree(args.inputs, base)
        for path in [base, *base.rglob("*")]:
            if path.is_symlink():
                raise AssertionError("sealed benchmark inputs must not contain symlinks")
            path.chmod(path.stat().st_mode | 0o200)
        record = {"index": index, "scenario": args.scenario,
                  "condition": "process_cold" if index == 0 else "process_warm",
                  "input_manifest_digest": digest(file_manifest(args.inputs)),
                  "started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "started"}
        write_json(sample_dir / "sample.json", record)
        trace, stdout, stderr = [], io.StringIO(), io.StringIO()
        record["measurement_status"] = "mutable_source_preflight" if args.preflight else "timing"
        record["trade_events_before"] = trade_event_facts(base)
        try:
            with offline_tick_boundaries(base, scenario=args.scenario, trace=trace) as facade:
                with observe_owners() as (counts, elapsed, returned, details):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        started = time.perf_counter()
                        details["_tick_start"] = started
                        try:
                            result = facade.execute_tick(["--config", str(base / "config.hk.json"),
                                "--accounts", *ACCOUNTS, "--market-config", "hk", "--no-send", "--allow-stale-config"])
                        finally:
                            record["wall_seconds"] = time.perf_counter() - started
                    details.pop("_tick_start", None)
                    record.update(status="completed", execution=asdict(result), counts=dict(counts),
                                  owner_seconds=dict(elapsed), observations=canonical(dict(returned), base),
                                  instrumentation_gaps=details)
                    briefs = returned.get("brief.assemble", [])
                    record["brief_results"] = [{"account": b.get("account"), "status": b.get("status"),
                        "actionability": b.get("actionability"), "attribution_pending_count": len(b.get("attribution_pending") or []),
                        "candidate_counts": {k: len(v) for k, v in (b.get("candidates") or {}).items()},
                        "data_gaps": b.get("data_gaps"), "run_id": b.get("run_id")} for b in briefs]
                    record["brief_digest"] = digest(canonical(briefs, base))
                    projections = [{k: b.get(k) for k in ("account", "status", "actionability", "candidates", "attribution_pending", "data_gaps")}
                                   for b in briefs]
                    record["semantic_projection"] = canonical(projections, base)
                    record["semantic_digest"] = digest(record["semantic_projection"])
                    account_results = returned.get("tick.accounts", [])
                    results = account_results[0]["results"] if account_results else []
                    record["invariants"] = {
                        "two_account_results_once": Counter(r["account"] for r in results) == Counter(ACCOUNTS),
                        "real_pipeline_two_accounts": bool(account_results) and len(account_results[0]["account_metrics"]) == 2
                            and all(m.get("ran_pipeline") for m in account_results[0]["account_metrics"]),
                        "nonempty_candidates_two_accounts": len(briefs) >= 2 and all(any(b.get("candidates", {}).values()) for b in briefs),
                        "real_assembly_two_accounts": len(briefs) >= 2 and {b.get("account") for b in briefs} == set(ACCOUNTS),
                        "nonempty_attribution": any(b.get("attribution_pending") for b in briefs),
                        "qualified_briefs": bool(briefs) and all(b.get("status") in {"ready", "degraded"}
                                                                and b.get("actionability") != "blocked" for b in briefs),
                        "real_required_data_prefetch": counts["required_data.prefetch"] > 0,
                        "real_required_data_seal": counts["required_data.seal"] > 0,
                        "real_finalizer": counts["tick.finalize"] + counts["tick.finalize_no_message"] > 0,
                        "real_idempotency_completion": counts["idempotency.complete"] > 0,
                        "real_scan_commit": counts["scheduler.commit"] > 0,
                        "no_unhandled_network_or_process": not any(t["boundary"].startswith("forbidden_") for t in trace),
                        "facade_rc0": result.rc == 0,
                    }
                    record["outcome"] = ("normal_success" if result.rc == 0 else "business_failure")
                    if args.scenario == "localized_discovery_failure":
                        record["fault_expectation"] = ("healthy_scope_continues" if hasattr(tick, "prepare_tick_briefs")
                                                       else "original_global_barrier")
        except BaseException as exc:
            record.update(status="measurement_crash", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
        record["boundary_trace"] = trace
        record["trade_events_after"] = trade_event_facts(base)
        origins = {name: str(Path(module.__file__).resolve()) for name, module in tuple(sys.modules.items())
                   if name.startswith(("src.", "domain.")) and getattr(module, "__file__", None)}
        record["source_origin_escapes"] = {name: path for name, path in origins.items()
                                           if not Path(path).is_relative_to(args.source_root)}
        record["harness_module_sources"] = {name: {"path": str(Path(module.__file__).resolve()),
            "sha256": sha(module.__file__)} for name, module in tuple(sys.modules.items())
            if getattr(module, "__file__", None) and any(Path(module.__file__).resolve().is_relative_to(root)
                for root in (HARNESS_ROOT / "tests", args.source_root / "tests"))}
        record["loaded_plugin_sources"] = {name: {"path": str(Path(module.__file__).resolve()), "sha256": sha(module.__file__)}
            for name, module in tuple(sys.modules.items()) if name.startswith("om_") and getattr(module, "__file__", None)}
        events = []
        for path in (base / "audit/run_logs").glob("*.jsonl"):
            events.extend(json.loads(line) for line in path.read_text().splitlines() if line)
        record["stage_events"] = [e for e in events if e.get("step") in {"tick_latency", "daily_brief_checkpoint", "daily_brief_phase", "fetch_chain_cache", "run_end"}]
        if "invariants" in record:
            record["invariants"].update(no_trade_events_changed=record["trade_events_before"] == record["trade_events_after"],
                                       all_implementation_imports_from_selected_source=not record["source_origin_escapes"])
            if args.scenario == "complete":
                record["valid_expected_outcome"] = all(record["invariants"].values())
            elif record["fault_expectation"] == "original_global_barrier":
                expected_global_failure = any("global required-data plan incomplete" in str(e.get("message")) for e in events)
                required = ("two_account_results_once", "real_assembly_two_accounts", "nonempty_attribution",
                    "real_required_data_prefetch", "real_finalizer", "real_idempotency_completion",
                    "no_unhandled_network_or_process", "no_trade_events_changed",
                    "all_implementation_imports_from_selected_source")
                record["valid_expected_outcome"] = result.rc == 1 and expected_global_failure and all(
                    record["invariants"][key] for key in required)
                record["outcome"] = ("expected_injected_failure_under_original_global_barrier"
                    if record["valid_expected_outcome"] else "unexpected_business_failure")
            else:
                candidate_symbols = [{str(row.get("symbol")) for rows in (brief.get("candidates") or {}).values()
                                      for row in rows} for brief in briefs]
                record["invariants"].update(
                    healthy_scope_candidates_retained=len(candidate_symbols) == len(ACCOUNTS)
                        and all("3690.HK" in symbols for symbols in candidate_symbols),
                    failed_scope_candidates_absent=bool(candidate_symbols)
                        and all("0700.HK" not in symbols for symbols in candidate_symbols))
                record["valid_expected_outcome"] = all(record["invariants"].values())
                record["outcome"] = ("healthy_scope_completed_with_localized_discovery_failure"
                    if record["valid_expected_outcome"] else "unexpected_business_failure")
        record["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        (sample_dir / "stdout.log").write_text(stdout.getvalue())
        (sample_dir / "stderr.log").write_text(stderr.getvalue())
        write_json(sample_dir / "sample.json", record)
        print(json.dumps({k: record.get(k) for k in ("scenario", "index", "condition", "status", "wall_seconds", "execution", "error")}, default=str), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=HARNESS_ROOT)
    parser.add_argument("--preflight", action="store_true", help="label functional samples against mutable source; not formal A18 timing")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--preparation-inputs", type=Path)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--samples", type=int, default=4)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    args.source_root = args.source_root.resolve()
    args.output_dir = args.output_dir.resolve()
    args.inputs = (args.inputs or args.output_dir / "inputs").resolve()
    if args.worker:
        return worker(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(args.source_root), str(HARNESS_ROOT / "tests"), str(HARNESS_ROOT)]
    if args.seed:
        from tick_execution_benchmark_fixtures import seed_tick_inputs
        meta = seed_tick_inputs(args.inputs, preparation_inputs=args.preparation_inputs)
        write_json(args.output_dir / "fixture.json", meta)
    if not args.inputs.is_dir():
        parser.error("--inputs must exist, or use --seed")
    manifest = file_manifest(args.inputs)
    metadata = {"baseline_commit": BASE, "source_root": str(args.source_root),
        "measurement_status": "mutable_source_preflight" if args.preflight else "timing",
        "preparation_owner_note": "brief.prepare is legacy combined preparation before the split, notification freezing after; brief.prepare_source is the new separate owner; these inclusive timers cannot be added",
        "source_version": (args.source_root / "VERSION").read_text().strip(),
        "source_code_hashes": {str(p.relative_to(args.source_root)): sha(p) for top in ("src", "domain", "plugins")
                               for p in sorted((args.source_root / top).rglob("*.py"))},
        "python": sys.version, "executable": sys.executable, "prefix": sys.prefix,
        "platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(),
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata["Name"]},
        "harness_sha256": sha(Path(__file__)), "fixture_sha256": sha(HARNESS_ROOT / "tests/tick_execution_benchmark_fixtures.py"),
        "input_manifest_digest": digest(manifest),
        "boundary_limitations": ["frozen provider-adapter observations; no provider SDK/network latency", "inline process transport; no child startup/concurrency/kill/cancellation timing",
             "CLI config-origin freshness/canonical admission excluded", "live watchdog preflight omitted; no capability or readiness success asserted", "notifications --no-send; no external transport or delivery confirmation",
             "process cold/warm, not OS-cache cold; input copy and imports excluded", "HK put-only selected universe; no US earnings or call/close-advice/wheel scope"]}
    metadata.update(command=sys.argv, cwd=str(Path.cwd()),
        harness_workspace=str(HARNESS_ROOT),
        harness_branch=subprocess.check_output(["git", "-C", str(HARNESS_ROOT), "branch", "--show-current"], text=True).strip(),
        fixture_source_hashes={str(path.relative_to(HARNESS_ROOT)): sha(path) for name in (
            "daily_brief_preparation_fixtures.py", "cash_evidence_helpers.py", "conftest.py",
            "test_required_data_prefetch_inprocess.py", "test_daily_decision_brief_service.py",
            "test_trade_attribution_meituan.py", "test_daily_decision_brief_repository_v2.py")
            if (path := HARNESS_ROOT / "tests" / name).is_file()},
        benchmark_scope="whole offline Tick business execution via real facade; inline process transport; no live readiness/delivery",
        clock_binding="project datetime/date imports and time.time frozen; perf_counter/monotonic remain real",
        io_counter_meaning="successful Path.read_text/read_bytes API reads and byte lengths, not physical disk syscalls",
        acceptance="same sealed inputs/interpreter/packages/account/market/strategy universe/clock and conditions; lower matched warm p50 with same qualified normal results; fault work coverage reported separately")
    write_json(args.output_dir / "metadata.json", metadata)
    write_json(args.output_dir / "input-manifest.json", manifest)
    outcomes = []
    for scenario in ([args.scenario] if args.scenario else SCENARIOS):
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker", "--scenario", scenario,
            "--samples", str(args.samples), "--source-root", str(args.source_root), "--inputs", str(args.inputs),
            "--output-dir", str(args.output_dir)]
        if args.preflight:
            command.append("--preflight")
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout,
                                 env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
            (args.output_dir / f"{scenario}.log").write_text(run.stdout + run.stderr)
            outcomes.append({"scenario": scenario, "returncode": run.returncode, "timed_out": False})
        except subprocess.TimeoutExpired as exc:
            outcomes.append({"scenario": scenario, "returncode": None, "timed_out": True,
                             "stdout": str(exc.stdout), "stderr": str(exc.stderr)})
    for outcome in outcomes:
        for index in range(args.samples):
            path = args.output_dir / f"{outcome['scenario']}-{index}" / "sample.json"
            if not path.exists():
                write_json(path, {"scenario": outcome["scenario"], "index": index, "status": "not_started",
                    "reason": "worker_timeout" if outcome["timed_out"] else "worker_ended_before_sample",
                    "condition": "process_cold" if index == 0 else "process_warm"})
            else:
                sample = json.loads(path.read_text())
                if sample["status"] == "started":
                    sample.update(status="measurement_timeout" if outcome["timed_out"] else "measurement_worker_crash",
                                  worker_outcome=outcome)
                    write_json(path, sample)
    samples = [json.loads(p.read_text()) for p in sorted(args.output_dir.glob("*/sample.json"))]
    summary = {"denominator": {"planned": args.samples * len(outcomes), "records": len(samples),
                              "completed": sum(s["status"] == "completed" for s in samples),
                              "crashed": sum(s["status"] == "measurement_crash" for s in samples),
                              "measurement_timeouts": sum(s["status"] == "measurement_timeout" for s in samples),
                              "not_started": sum(s["status"] == "not_started" for s in samples),
                              "valid_expected_outcomes": sum(s.get("valid_expected_outcome", False) for s in samples),
                              "worker_timeouts": sum(o["timed_out"] for o in outcomes)},
               "workers": outcomes, "samples": samples, "statistics": {}}
    summary["acceptance_valid"] = (
        len(samples) == summary["denominator"]["planned"] > 0
        and all(s["status"] == "completed" and s.get("valid_expected_outcome", False) for s in samples)
        and all(o["returncode"] == 0 and not o["timed_out"] for o in outcomes)
    )
    for scenario in ([args.scenario] if args.scenario else SCENARIOS):
        rows = [s for s in samples if s["scenario"] == scenario]
        warm_rows = [s for s in rows if s["status"] == "completed" and s["condition"] == "process_warm"]
        warm = [s["wall_seconds"] for s in warm_rows]
        summary["statistics"][scenario] = {"warm_count": len(warm),
            "valid_warm_count": sum(s.get("valid_expected_outcome", False) for s in warm_rows),
            "warm_p50_seconds": statistics.median(warm) if summary["acceptance_valid"] and warm else None,
            "cold_seconds": [s.get("wall_seconds") for s in rows if s["condition"] == "process_cold"],
            "p95": None, "p95_reason": "cold1/warm3 is insufficient for tail quantiles"}
    write_json(args.output_dir / "results.json", summary)
    print(json.dumps({"acceptance_valid": summary["acceptance_valid"],
        "denominator": summary["denominator"], "statistics": summary["statistics"]}, indent=2))
    return 0 if summary["acceptance_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
