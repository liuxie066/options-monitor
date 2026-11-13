#!/usr/bin/env python3
"""Time a scheduled notification flow against synthetic history; never send or scan.

Run with the repository's development Python. Fixtures use the real repository,
notification planner and confirmation path, with a fake assembler and transport.
Compare revisions on the same host/interpreter and with the same --days/--repeats.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

HARNESS_ROOT = Path(__file__).resolve().parents[1]
_source_parser = argparse.ArgumentParser(add_help=False)
_source_parser.add_argument("--source-root", type=Path, default=HARNESS_ROOT)
ROOT = _source_parser.parse_known_args()[0].source_root.resolve()
sys.path[:0] = [str(ROOT), str(HARNESS_ROOT / "tests"), str(HARNESS_ROOT)]

import pytest
from daily_brief_history_fixtures import seed_delivery_history
from daily_brief_benchmark_fixtures import notification_bundle, patch_delivery_microbenchmark, run_notification
from src.application import daily_decision_brief_repository as repository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=HARNESS_ROOT)
    parser.add_argument("--preflight", action="store_true", help="label functional samples against mutable source; not formal A18 timing")
    parser.add_argument("--days", type=int, default=35)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.days < 1 or args.repeats < 1:
        parser.error("--days and --repeats must be positive")
    samples = []
    profile = cProfile.Profile()
    with tempfile.TemporaryDirectory(prefix="om-delivery-benchmark-") as temp:
        root = Path(temp)
        fixture = root / "fixture"
        delivery_path = seed_delivery_history(fixture, days=args.days)
        size = delivery_path.stat().st_size
        for repeat in range(args.repeats):
            base = root / f"sample-{repeat}"
            shutil.copytree(fixture, base)
            repository._VALIDATED_BRIEF_CACHE.clear()
            validations = []
            original = repository._normalize_delivery_state

            def normalized(*a, **kw):
                validations.append(len(a[0]["days"]))
                return original(*a, **kw)

            with pytest.MonkeyPatch.context() as monkeypatch:
                sends = []
                patch_delivery_microbenchmark(monkeypatch, sends)
                bundle = notification_bundle(base, run_id="benchmark-current")
                with patch.object(repository, "_normalize_delivery_state", normalized):
                    start = time.perf_counter()
                    if args.profile:
                        profile.enable()
                    rc = run_notification(bundle)
                    # This is the root's integer return, not the delivery owner's
                    # new TickNotificationOutcome. Finalization remains real.
                    if args.profile:
                        profile.disable()
                    elapsed = time.perf_counter() - start
                assert rc == 0 and len(sends) == 1, (rc, sends, bundle.completions)
                readback = repository.read_daily_decision_brief_delivery_state(
                    base=base, account="lx", market="US",
                )
                today = readback["state"]["days"]["2026-07-21"]
                envelopes = list(today["fixed_reports"].values())
                assert len(envelopes) == 1 and envelopes[0]["status"] == "confirmed"
                samples.append({"seconds": elapsed, "normalize_calls": len(validations),
                                "normalized_days": sum(validations), "provider_sends": len(sends),
                                "delivery_key": envelopes[0]["delivery_key"],
                                "message_sha256": envelopes[0]["message_sha256"]})
    result = {"fixture_days": args.days, "fixture_bytes": size,
              "source_root": str(ROOT), "harness_root": str(HARNESS_ROOT),
              "measurement_status": "mutable_source_preflight" if args.preflight else "timing",
              "scope": "isolated delivery microbenchmark: synthetic assembler and transport; real history, planner, root composition and finalizer",
              "instrumentation": "cProfile" if args.profile else "none",
              "median_seconds": statistics.median(item["seconds"] for item in samples),
              "samples": samples}
    if args.profile:
        profile.dump_stats(str(args.profile))
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
