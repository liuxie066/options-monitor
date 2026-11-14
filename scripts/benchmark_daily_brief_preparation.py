#!/usr/bin/env python3
"""Reproducible offline timing of real attribution, Brief assembly and preparation.

Run with -I -B and the development Python. No provider, scan or sender is run.
Inputs and every attempted sample are retained below --output-dir. Compare the
same input manifest, interpreter and host; these timings do not cover a full Tick.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import functools
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
HARNESS_ROOT = Path(__file__).resolve().parents[1]
_source_parser = argparse.ArgumentParser(add_help=False)
_source_parser.add_argument("--source-root", type=Path, default=HARNESS_ROOT)
ROOT = _source_parser.parse_known_args()[0].source_root.resolve()
sys.path[:0] = [str(ROOT), str(HARNESS_ROOT / "tests"), str(HARNESS_ROOT)]
BASELINE = "967498d3bf5039af8ed62a1ce808f265989f7856"
DESIGN_SHA256 = "c2135b3d71fb31eb088a97e4b44b9ff974866e45906d95b436528ca4b86d34e3"
PHASES = ("pending_attribution", "assemble", "offline_preparation")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def brief_digest(brief, base):
    # Source artifacts contain the sample directory; that spelling is not a fact.
    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value.replace(str(base), "{runtime_root}") if isinstance(value, str) else value
    return digest(canonical(brief))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


class Runlog:
    def __init__(self):
        self.events = []

    def safe_event(self, *args, **kwargs):
        self.events.append({"event": args[0], "status": args[1] if len(args) > 1 else None, **kwargs})


@contextmanager
def no_network(attempts):
    def blocked(*args, **kwargs):
        attempts.append({"operation": "socket_connect", "argument": repr(args[1:] or kwargs)})
        raise RuntimeError("network is forbidden in the offline benchmark")
    with patch.object(socket.socket, "connect", blocked), patch.object(socket.socket, "connect_ex", blocked), patch.object(socket, "create_connection", blocked):
        yield


def load_owners():
    # Load request wiring/root aliases before observation and the sample timer.
    import daily_brief_benchmark_fixtures
    from src.application import daily_decision_brief_repository as repository
    from src.application import daily_decision_brief_service as service
    from src.application import tick_notification_flow as flow
    from src.application.trades import attribution
    from src.application.ledger import api as ledger
    from src.infrastructure.decision_history_sqlite import DecisionHistoryStore
    return repository, service, flow, attribution, ledger, DecisionHistoryStore


@contextmanager
def measurements(owners):
    """Wrap owners AND already-bound aliases, always invoking the original code.

    Delivery read counters observe Path.read_text/read_bytes (one API read, not
    physical disk syscalls). normalize/projection counters are CPU calls, never I/O.
    """
    repository, service, flow, attribution, ledger, history = owners
    counts, elapsed, io_bytes = Counter(), Counter(), Counter()
    details = {"history_get_scopes": Counter(), "combo_read_scopes": Counter(), "projection_as_of_ms": Counter()}
    targets = [
        (repository, "read_latest_daily_decision_brief", "query.read_latest"),
        (repository, "_history_raw", "history.source_observation"),
        (repository, "_read_validated_successful_revision_source", "history.validate_source"),
        (repository, "_normalize_delivery_state", "cpu.normalize_delivery_state"),
        (repository, "_observe_delivery_state", "history.observe_delivery_state"),
        (repository, "read_combo_candidate_exposures", "history.combo_exposures"),
        (repository, "normalize_persisted_daily_decision_brief_with_digests", "cpu.normalize_persisted_brief"),
        (service, "_pending_attribution_for_brief", "real.pending_attribution"),
        (service, "assemble_daily_decision_brief", "real.assemble"),
        (attribution, "read_attribution_combo_evidence", "real.attribution_evidence"),
        (ledger, "read_trade_attribution_snapshot", "ledger.snapshot"),
        (ledger, "build_trade_attribution_view", "real.attribution_arbiter"),
        (ledger, "build_wheel_read_model_from_rows", "cpu.wheel_projection"),
        (ledger, "build_wheel_read_model_with_capacity_from_rows", "cpu.wheel_projection_with_capacity"),
        (ledger, "project_position_lots_from_trade_facts", "cpu.position_projection"),
    ]
    if hasattr(repository, "_read_latest_attempt"):
        targets.append((repository, "_read_latest_attempt", "query.latest_attempt"))
    counts.update({label: 0 for _, _, label in targets})
    counts.update({key: 0 for key in ("history.get", "history.connection", "cpu.scoped_delivery_normalize",
                                    "io.delivery_json.read_text", "io.delivery_json.read_bytes", "query.latest_attempt")})
    with ExitStack() as stack:
        def wrapper(original, label):
            @functools.wraps(original)
            def wrapped(*args, **kwargs):
                counts[label] += 1
                if label == "history.get":
                    key = json.dumps({key: kwargs.get(key) for key in ("account", "market", "market_date", "revision", "successful")}, sort_keys=True)
                    details["history_get_scopes"][key] += 1
                elif label == "history.combo_exposures":
                    details["combo_read_scopes"][f"{kwargs.get('market')}:{kwargs.get('market_trading_date')}"] += 1
                elif label in {"cpu.wheel_projection", "cpu.wheel_projection_with_capacity"}:
                    details["projection_as_of_ms"][str(kwargs.get("as_of_ms"))] += 1
                started = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    elapsed[label] += time.perf_counter() - started
            return wrapped

        # These two observation owners did not exist before PR #1153. Record
        # their absence without replacing any historical business operation.
        details["optional_observer_availability"] = {
            "history.observe_delivery_state": hasattr(repository, "_observe_delivery_state"),
            "cpu.scoped_delivery_normalize": hasattr(repository, "_DeliveryValidation"),
        }
        # Imported aliases must use the same wrapper, including lazy imports of owners.
        for module, name, label in targets:
            if name == "_observe_delivery_state" and not hasattr(module, name):
                continue
            original = getattr(module, name)
            wrapped = wrapper(original, label)
            bindings = [(loaded, key) for loaded in tuple(sys.modules.values())
                        if loaded is not None and getattr(loaded, "__name__", "").startswith(("src.", "domain."))
                        for key, value in tuple(vars(loaded).items()) if value is original]
            for loaded, key in bindings:
                stack.enter_context(patch.object(loaded, key, wrapped))
        for name, label in (("get", "history.get"), ("connect", "history.connection")):
            stack.enter_context(patch.object(history, name, wrapper(getattr(history, name), label)))
        if hasattr(repository, "_DeliveryValidation"):
            stack.enter_context(patch.object(repository._DeliveryValidation, "normalize",
                                             wrapper(repository._DeliveryValidation.normalize, "cpu.scoped_delivery_normalize")))
        for name in ("read_text", "read_bytes"):
            original = getattr(Path, name)

            def read(path, *args, _original=original, _name=name, **kwargs):
                result = _original(path, *args, **kwargs)
                if path.name.startswith("daily_decision_brief.") and path.name.endswith(".delivery.json"):
                    counts[f"io.delivery_json.{_name}"] += 1
                    io_bytes[f"delivery_json.{_name}"] += len(result.encode("utf-8") if isinstance(result, str) else result)
                return result
            stack.enter_context(patch.object(Path, name, read))
        yield counts, elapsed, io_bytes, details


def call_phase(phase, base, config, runlog, owners, *, production_validation_scope=False):
    from daily_brief_preparation_fixtures import ACCOUNT, MARKET, NOW, RUN_ID
    from test_daily_decision_brief_service import _result
    from daily_brief_benchmark_fixtures import notification_bundle, prepare_notification
    from src.application.multi_tick_audit import daily_brief_timing_scope
    repository, service, flow, *_ = owners
    with daily_brief_timing_scope(runlog=runlog, account=ACCOUNT, market=MARKET):
        if phase == "pending_attribution":
            pending, error = service._pending_attribution_for_brief(
                base=base, config=config, account=ACCOUNT, market=MARKET,
                now_ms=int(NOW.timestamp() * 1000))
            if error or not pending:
                raise AssertionError(f"real attribution did not return pending facts: {error}, {pending}")
            return {"pending_count": len(pending), "pending": pending, "semantic_sha256": digest(pending)}
        if phase == "assemble":
            brief = service.assemble_daily_decision_brief(
                base=base, run_id=RUN_ID, account=ACCOUNT, market=MARKET,
                scheduler_decision={"in_run_window": True}, account_result=_result(),
                pipeline_succeeded=True, config=config, now_utc=NOW)
            if not brief.get("attribution_pending"):
                raise AssertionError(f"assembler lost pending ledger facts: {brief.get('data_gaps')}")
            if not brief.get("candidates", {}).get("covered_call"):
                raise AssertionError(f"sealed candidate fixture was not consumed: {brief.get('data_gaps')}")
            return {"status": brief["status"], "actionability": brief["actionability"],
                    "pending_count": len(brief["attribution_pending"]),
                    "candidate_counts": {key: len(value) for key, value in brief["candidates"].items()},
                    "data_gaps": brief["data_gaps"], "semantic_sha256": brief_digest(brief, base)}
        target = "2026-09-28T10:00:00+08:00"
        scheduler = {"in_run_window": True, "should_run_scan": True, "now_utc": NOW.isoformat(),
                     "now_market": NOW.astimezone(ZoneInfo("Asia/Hong_Kong")).isoformat(),
                     "scheduled_scan_target_market": target, "scheduled_target_market": target}
        request = notification_bundle(base, run_id=RUN_ID, config=config, accounts=(ACCOUNT,), markets=(MARKET,)).request
        request = replace(request, runlog=runlog, cfg_path=base / "benchmark_config.json",
                          scheduler_decision=scheduler, scheduler_decisions_by_account={ACCOUNT: scheduler},
                          scheduled_scan_targets_by_account={ACCOUNT: target}, vpy=Path(sys.executable))
        # The root now composes source preparation and obligation freezing. Keep
        # both real owners in the timed operation, including the real intent factory.
        with (repository.daily_brief_delivery_validation_scope() if production_validation_scope else nullcontext()):
            preparation, scan_outcomes = prepare_notification(request)
        messages = preparation.prepared_messages.messages_by_account
        if preparation.blocked_error_code or not messages or not preparation.transport_envelopes_by_account:
            raise AssertionError(f"preparation did not form an envelope: {preparation.blocked_error_code}, {request.tick_metrics}")
        return {"messages": len(messages), "message_bytes": {key: len(value.encode()) for key, value in messages.items()},
                "envelopes": len(preparation.transport_envelopes_by_account),
                "scan_outcomes": scan_outcomes,
                "semantic_sha256": digest(messages), "tick_metrics": request.tick_metrics}


def module_locations(owners):
    names = [owner.__module__ if isinstance(owner, type) else owner.__name__ for owner in owners]
    names += ["domain.domain.daily_decision_brief", "daily_brief_preparation_fixtures"]
    # Record defining source bytes through the public functions used by observation.
    names += [getattr(owners[4], name).__module__ for name in (
        "read_trade_attribution_snapshot", "build_trade_attribution_view",
        "build_wheel_read_model_from_rows", "build_wheel_read_model_with_capacity_from_rows",
        "project_position_lots_from_trade_facts",
    )]
    paths = {name: Path(sys.modules[name].__file__).resolve() for name in names}
    if any(not path.is_relative_to(ROOT if name.startswith(("src.", "domain.")) else HARNESS_ROOT)
           for name, path in paths.items()):
        raise RuntimeError(f"application imports escaped the selected source root: {paths}")
    return {name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in paths.items()}


def worker(args):
    from daily_brief_preparation_fixtures import NOW
    owners = load_owners()
    source_modules = module_locations(owners)
    service, flow = owners[1:3]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    config = json.loads((args.inputs / "benchmark_config.json").read_text(encoding="utf-8"))
    attempts = []
    failed = False
    with tempfile.TemporaryDirectory(prefix=f"samples-{args.worker_phase}-", dir=args.output_dir) as temporary:
        base = Path(temporary) / "runtime"
        for index in range(args.repeats + 1):
            # Same path for warm application caches; reset all durable inputs each time.
            if base.exists():
                shutil.rmtree(base)
            shutil.copytree(args.inputs, base)
            runlog = Runlog()
            sample = {"phase": args.worker_phase, "index": index,
                      "condition": "process_cold_os_cache_uncontrolled" if index == 0 else "warm_process_restored_identical_input",
                      "ok": False, "timeout": False,
                      "measurement_status": "mutable_source_preflight" if args.preflight else "timing",
                      "production_validation_scope": args.production_validation_scope,
                      "source_tree_root": str(ROOT), "source_modules": source_modules}
            with no_network(attempts), patch.object(service, "datetime", FrozenDatetime), patch.object(flow, "utc_now", lambda: NOW), measurements(owners) as measured:
                counts, elapsed, io_bytes, details = measured
                started = time.perf_counter()
                try:
                    sample["result"] = call_phase(args.worker_phase, base, config, runlog, owners,
                                                  production_validation_scope=args.production_validation_scope)
                    required = ["real.pending_attribution", "real.attribution_arbiter", "real.attribution_evidence"]
                    if args.worker_phase != "pending_attribution":
                        required.append("real.assemble")
                    if any(counts[key] < 1 for key in required):
                        raise AssertionError(f"nonempty real-path coverage was lost: {dict(counts)}")
                    if counts["io.delivery_json.read_text"] + counts["io.delivery_json.read_bytes"] < 1:
                        raise AssertionError("no underlying delivery JSON read was observed")
                    if attempts:
                        raise AssertionError(f"offline code attempted network access: {attempts}")
                    sample["ok"] = True
                except Exception as exc:
                    failed = True
                    sample["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
                finally:
                    sample["wall_seconds"] = time.perf_counter() - started
                    sample["calls"] = dict(counts)
                    sample["inclusive_function_seconds"] = dict(elapsed)
                    sample["io_bytes"] = dict(io_bytes)
                    sample["details"] = {key: dict(value) for key, value in details.items()}
                    sample["events"] = runlog.events
                    sample["network_attempts"] = list(attempts)
            sample_path = args.output_dir / f"{args.worker_phase}-{index}.json"
            write_json(sample_path, sample)
            print(json.dumps({key: sample.get(key) for key in ("phase", "index", "condition", "ok", "wall_seconds", "error")}), flush=True)
    return int(failed or bool(attempts))


def input_manifest(inputs, config, days, owners):
    from daily_brief_preparation_fixtures import ACCOUNT, MARKET, NOW
    from src.application.ledger.api import open_trade_reconciliation_evidence_repo, read_trade_attribution_snapshot
    from src.infrastructure.decision_history_sqlite import DecisionHistoryStore, history_path
    repo = open_trade_reconciliation_evidence_repo(inputs / "output_shared/state/option_positions.sqlite3")
    snapshot = read_trade_attribution_snapshot(repo, account=ACCOUNT, market=MARKET.lower())
    preview = owners[3].combo_attribution_candidates_from_rows(snapshot, account=ACCOUNT, runtime_environment="",
        exposures=[], effective_now_ms=int(NOW.timestamp() * 1000), include_claimed=True)
    scopes = sorted({(row["market"], row["market_date"]) for row in preview["lot_facts"]})
    with DecisionHistoryStore(history_path(inputs)).connect() as conn:
        revisions = [dict(row) for row in conn.execute("SELECT account,market,market_date,revision FROM decisions ORDER BY market_date,revision")]
    delivery_path = owners[0]._delivery_path(inputs, ACCOUNT, MARKET)
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    files = [{"path": str(path.relative_to(inputs)), "bytes": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in sorted(inputs.rglob("*")) if path.is_file()]
    return {"files": files, "files_manifest_sha256": digest(files), "accounts_markets": [[ACCOUNT, MARKET]],
            "history_days": len(delivery["days"]), "history_revisions": len(revisions), "history_revision_rows": revisions,
            "delivery_envelopes": sum(len(row.get("fixed_reports", {})) for row in delivery["days"].values()),
            "delivery_json_bytes": delivery_path.stat().st_size, "actions_per_history_revision": 24,
            "ledger_table_counts": {key: len(value) for key, value in snapshot.items() if isinstance(value, (list, tuple))},
            "ledger_trade_events": len(snapshot["trade_events"]), "attribution_facts": len(preview["lot_facts"]),
            "attribution_scopes": scopes, "attribution_scopes_with_history": [scope for scope in scopes if scope[1] in delivery["days"]],
            "strategy_universe": config["symbols"], "sealed_current_candidates": 1,
            "current_market_date": NOW.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat(),
            "fixed_clock_utc": NOW.isoformat()}


def coordinator(args):
    revision = git("rev-parse", "HEAD")
    if args.expected_revision and revision != args.expected_revision:
        raise SystemExit(f"expected {args.expected_revision}, found {revision}")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    inputs = args.output_dir / "inputs"
    from daily_brief_preparation_fixtures import seed_preparation_inputs
    owners = load_owners()
    attempts = []
    with no_network(attempts):
        if args.inputs:
            shutil.copytree(args.inputs, inputs)
            config = json.loads((inputs / "benchmark_config.json").read_text(encoding="utf-8"))
        else:
            inputs.mkdir()
            config = seed_preparation_inputs(inputs, days=args.days, fill_dates=args.fill_dates,
                fills_per_date=args.fills_per_date, revisions_per_day=args.revisions_per_day)
        manifest = input_manifest(inputs, config, args.days, owners)
    write_json(args.output_dir / "input-manifest.json", manifest)
    source_files = ["scripts/benchmark_daily_brief_preparation.py", "tests/daily_brief_preparation_fixtures.py",
                    "tests/daily_brief_history_fixtures.py", "tests/test_trade_attribution_meituan.py",
                    "tests/test_daily_decision_brief_service.py", "tests/test_daily_decision_brief_repository_v2.py",
                    "src/application/daily_decision_brief_service.py", "src/application/daily_decision_brief_repository.py",
                    "src/application/trades/attribution.py", "src/application/ledger/trade_attribution.py",
                    "src/infrastructure/decision_history_sqlite.py", "src/application/tick_notification_flow.py",
                    "src/application/daily_decision_brief_renderer.py",
                    "tests/daily_brief_benchmark_fixtures.py", "src/application/multi_account_tick.py"]
    if (ROOT / "src/application/tick_brief_preparation.py").is_file():
        source_files.append("src/application/tick_brief_preparation.py")
    metadata = {"revision": revision, "reference_baseline": BASELINE, "design_sha256": DESIGN_SHA256,
                "measurement_status": "mutable_source_preflight" if args.preflight else "timing",
                "harness_root": str(HARNESS_ROOT),
                "source_tree_root": str(ROOT), "git_worktree_root": git("rev-parse", "--show-toplevel"),
                "source_modules": module_locations(owners),
                "production_validation_scope": args.production_validation_scope,
                "preparation_measurement": ("real source preparation and notification freezing within production's existing operation scope" if args.production_validation_scope
                                            else "selected source's real preparation owners without an added outer validation scope; use --production-validation-scope for matched timing"),
                "git_status": git("status", "--short"), "python": sys.version, "executable": sys.executable,
                "platform": platform.platform(), "machine": platform.machine(),
                "processor": platform.processor(), "sqlite": __import__("sqlite3").sqlite_version,
                "pandas": __import__("pandas").__version__, "cpu_count": os.cpu_count(),
                "isolated_python": bool(sys.flags.isolated), "ambient_pythonpath": os.environ.get("PYTHONPATH"),
                "broker_futu_available": importlib.util.find_spec("om_broker_futu") is not None,
                "source_sha256": {name: hashlib.sha256(
                    ((ROOT if name.startswith("src/") else HARNESS_ROOT) / name).read_bytes()).hexdigest()
                    for name in source_files},
                "baseline_source_sha256": {name: hashlib.sha256(subprocess.check_output(
                    ["git", "-C", str(ROOT), "show", f"{BASELINE}:{name}"])).hexdigest() for name in source_files[2:]
                    if name not in {"tests/daily_brief_benchmark_fixtures.py", "src/application/tick_brief_preparation.py"}},
                "command": [sys.executable, *sys.orig_argv[1:]],
                "instrumentation": "original-preserving wrappers; inclusive function times overlap; Path API reads are logical I/O, not physical disk reads",
                "cold_condition": "fresh worker per phase; owner imports excluded; OS caches uncontrolled and likely warm after fixture generation",
                "warm_condition": "same worker and runtime path, restored identical fixture bytes before every sample; application caches retained",
                "coverage": "real pending attribution, real assembler, real notification preparation through frozen transport envelope; no scan, sender, finalizer, failure-budget or full-Tick measurement",
                "input_manifest_sha256": digest(manifest), "setup_network_attempts": attempts}
    write_json(args.output_dir / "metadata.json", metadata)
    commands, returncodes = [], {}
    for phase in PHASES:
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker-phase", phase,
                   "--source-root", str(ROOT), "--inputs", str(inputs), "--output-dir", str(args.output_dir), "--repeats", str(args.repeats)]
        if args.preflight:
            command.append("--preflight")
        if args.production_validation_scope:
            command.append("--production-validation-scope")
        commands.append(command)
        print(f"START {phase}: {json.dumps(command)}", flush=True)
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=args.phase_timeout_seconds, cwd=ROOT)
            returncodes[phase] = process.returncode
            (args.output_dir / f"{phase}.log").write_text(process.stdout + process.stderr, encoding="utf-8")
            print(process.stdout, end="", flush=True)
            if process.returncode:
                print(process.stderr, end="", flush=True)
        except subprocess.TimeoutExpired as exc:
            returncodes[phase] = 124
            output = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            (args.output_dir / f"{phase}.log").write_text(output + "\nWORKER TIMEOUT\n", encoding="utf-8")
    samples = []
    for phase in PHASES:
        for index in range(args.repeats + 1):
            path = args.output_dir / f"{phase}-{index}.json"
            samples.append(json.loads(path.read_text(encoding="utf-8")) if path.exists() else
                {"phase": phase, "index": index, "ok": False, "timeout": returncodes[phase] == 124,
                 "error": {"type": "WorkerTimeoutOrExit", "message": f"worker exit {returncodes[phase]}; sample not completed"}})
    summary = {}
    for phase in PHASES:
        rows = [sample for sample in samples if sample["phase"] == phase]
        warm = [sample["wall_seconds"] for sample in rows if sample["index"] > 0 and sample["ok"]]
        summary[phase] = {"planned": len(rows), "completed": sum("wall_seconds" in row for row in rows),
                          "passed": sum(row["ok"] for row in rows), "failed": sum(not row["ok"] for row in rows),
                          "timeout_or_unfinished_after_timeout": sum(row["timeout"] for row in rows),
                          "cold_seconds": rows[0].get("wall_seconds"), "warm_seconds": warm,
                          "warm_p50_seconds": statistics.median(warm) if warm else None,
                          "warm_p95_seconds": None, "p95_note": "insufficient sample count; not estimated",
                          "semantic_sha256_values": sorted({row["result"]["semantic_sha256"] for row in rows if row["ok"]})}
    result = {"metadata": metadata, "inputs": manifest, "worker_commands": commands,
              "worker_exit_codes": returncodes, "samples": samples, "summary": summary,
              "complete_denominator": {"planned": len(samples), "passed": sum(row["ok"] for row in samples),
                                       "failed": sum(not row["ok"] for row in samples)}}
    write_json(args.output_dir / "baseline.json", result)
    print(json.dumps({"output": str(args.output_dir / "baseline.json"), "summary": summary}, indent=2), flush=True)
    return int(any(returncodes.values()) or any(not sample["ok"] for sample in samples) or bool(attempts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=HARNESS_ROOT)
    parser.add_argument("--preflight", action="store_true", help="label functional samples against mutable source; not formal A18 timing")
    parser.add_argument("--days", type=int, default=35)
    parser.add_argument("--fill-dates", type=int, default=8)
    parser.add_argument("--fills-per-date", type=int, default=3)
    parser.add_argument("--revisions-per-day", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3, help="warm repetitions, in addition to one process-cold sample per phase")
    parser.add_argument("--phase-timeout-seconds", type=int, default=600)
    parser.add_argument("--expected-revision")
    parser.add_argument("--production-validation-scope", action="store_true",
                        help="measure preparation inside the existing production delivery validation scope")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--worker-phase", choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument("--inputs", type=Path, help="replay a previously frozen inputs directory instead of regenerating fixtures")
    args = parser.parse_args()
    if min(args.days, args.fill_dates, args.fills_per_date, args.revisions_per_day, args.phase_timeout_seconds) < 1 or args.repeats < 0:
        parser.error("scale and timeout values must be positive; warm repeats must be nonnegative")
    if args.fill_dates > 26:
        parser.error("--fill-dates must be <= 26 to retain the Meituan fixture's ordering")
    args.output_dir = args.output_dir.resolve()
    if not args.output_dir.is_relative_to(Path(git("rev-parse", "--show-toplevel")) / "docs/gateflow"):
        parser.error("--output-dir must be below this worktree's docs/gateflow")
    return worker(args) if args.worker_phase else coordinator(args)


if __name__ == "__main__":
    raise SystemExit(main())
