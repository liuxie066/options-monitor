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

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest
from daily_brief_history_fixtures import seed_delivery_history
from test_daily_decision_brief_notification_flow import _patch_assembler, _patch_sender, _request
from src.application import daily_decision_brief_repository as repository
from src.application import tick_notification_flow as flow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
                _patch_assembler(monkeypatch)
                sends = []
                _patch_sender(monkeypatch, calls=sends)
                bundle = _request(base, run_id="benchmark-current")
                with patch.object(repository, "_normalize_delivery_state", normalized):
                    start = time.perf_counter()
                    if args.profile:
                        profile.enable()
                    rc = flow.run_tick_notification_flow(bundle.request)
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
