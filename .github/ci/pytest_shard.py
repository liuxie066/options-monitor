"""Opt-in CI file sharding with fail-closed collection/execution receipts."""

from __future__ import annotations

import json
import math
import os
import platform
import sys
import time
from pathlib import Path

import pytest

from regression_manifest import shard_for


class DurationDiagnostics:
    """Controller-only, best-effort metadata; never a coverage receipt.

    Opt in with CI_DURATION_DIAGNOSTICS=1. Offline consumers must also verify
    all eight original receipts, matching run/attempt/revision/config and node
    union. A green gate alone does not establish usable performance evidence.
    """

    def __init__(self):
        self.started = None
        self.finished = None
        self.samples = []
        self.errors = set()

    def capture(self, report):
        if report.when not in {"setup", "call", "teardown"}:
            self.errors.add("unknown_phase")
            return
        duration = report.duration
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration < 0):
            self.errors.add("invalid_duration")
            return
        if report.outcome not in {"passed", "failed", "skipped"}:
            self.errors.add("invalid_outcome")
            return
        self.samples.append(dict(nodeid=report.nodeid, phase=report.when,
                                 duration=duration, outcome=report.outcome))

    def write(self, owner, session, path):
        wall = (None if self.started is None or self.finished is None
                else self.finished - self.started)
        if wall is None or not math.isfinite(wall) or wall < 0:
            self.errors.add("invalid_wall_clock")
            wall = None
        phases = {}
        for sample in self.samples:
            node = phases.setdefault(sample["nodeid"], {})
            if sample["phase"] in node:
                self.errors.add("duplicate_phase")
            node[sample["phase"]] = sample["outcome"]
        if set(phases) != set(owner.selected):
            self.errors.add("sample_selection_mismatch")
        for node in phases.values():
            if "setup" not in node or "teardown" not in node:
                self.errors.add("missing_phase")
            if node.get("setup") == "passed" and "call" not in node:
                self.errors.add("missing_call")
            if node.get("setup") in {"skipped", "failed"} and "call" in node:
                self.errors.add("unexpected_call")
        if owner.errors or int(session.exitstatus) != 0:
            self.errors.add("suite_unsuccessful")
        payload = dict(
            schema_version=1, revision=owner.config.getoption("ci_revision"),
            run_id=os.environ.get("GITHUB_RUN_ID", "unknown"),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "unknown"),
            shard=owner.shard, count=owner.count,
            python_version=platform.python_version(),
            runner_os=os.environ.get("RUNNER_OS", "unknown"),
            runner_image={key: os.environ.get(key, "unknown")
                          for key in ("ImageOS", "ImageVersion")},
            xdist_workers=owner.config.getoption("numprocesses", default=0) or 0,
            algorithm_version="file-hash-v1", wall_seconds=wall,
            samples=self.samples, complete=not self.errors, errors=sorted(self.errors),
        )
        path.write_text(json.dumps(payload, allow_nan=False) + "\n")


def _diagnostic_warning():
    # Never interpolate exception text, report payloads or environment values.
    try:
        print("WARNING: CI duration diagnostics unusable", file=sys.stderr)
    except Exception:
        pass


def pytest_addoption(parser):
    group = parser.getgroup("ci sharding")
    group.addoption("--ci-shard", type=int)
    group.addoption("--ci-shard-count", type=int, default=8)
    group.addoption("--ci-manifest")
    group.addoption("--ci-revision")


def pytest_configure(config):
    shard = config.getoption("ci_shard")
    if shard is None:
        return
    count = config.getoption("ci_shard_count")
    if not 0 <= shard < count:
        raise pytest.UsageError("ci shard must be within shard count")
    if not config.getoption("ci_manifest") or not config.getoption("ci_revision"):
        raise pytest.UsageError("ci manifest and revision are required")
    # A shard is always a partition of automatic full discovery.
    if (config.option.file_or_dir or config.option.keyword or config.option.markexpr
            or config.option.deselect or config.option.ignore or config.option.ignore_glob
            or config.option.collectonly or config.getoption("lf", default=False)):
        raise pytest.UsageError("ci shards require unfiltered automatic discovery")
    config.pluginmanager.register(ShardReceipt(config, shard, count), "ci-shard-receipt")


class ShardReceipt:
    def __init__(self, config, shard, count):
        self.config = config
        self.shard = shard
        self.count = count
        self.collected = None
        self.selected = []
        self.completed = []
        self.errors = []
        self.workers = 0
        self.diagnostics = None
        if not hasattr(config, "workeroutput") and os.environ.get("CI_DURATION_DIAGNOSTICS") == "1":
            try:
                self.diagnostics = DurationDiagnostics()
            except Exception:
                _diagnostic_warning()

    def pytest_sessionstart(self, session):
        if self.diagnostics is not None:
            try:
                self.diagnostics.started = time.monotonic()
            except Exception:
                self.diagnostics.errors.add("clock_failed")
                _diagnostic_warning()

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        self.collected = sorted(item.nodeid for item in items)
        selected, other = [], []
        for item in items:
            (selected if shard_for(item.nodeid, self.count) == self.shard else other).append(item)
        self.selected = sorted(item.nodeid for item in selected)
        items[:] = selected
        self.config.hook.pytest_deselected(items=other)

    def pytest_runtest_logreport(self, report):
        if report.when == "teardown":
            self.completed.append(report.nodeid)
        if report.failed:
            self.errors.append(f"{report.nodeid}: {report.when} failed")
        if self.diagnostics is not None:
            try:
                self.diagnostics.capture(report)
            except Exception:
                self.diagnostics.errors.add("capture_failed")
                _diagnostic_warning()

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.workers += 1
        receipt = node.workeroutput.get("ci_collection")
        if error or receipt is None:
            self.errors.append("worker failed or omitted collection receipt")
            return
        full, selected = receipt
        if self.collected is None:
            self.collected, self.selected = full, selected
        elif (self.collected, self.selected) != (full, selected):
            self.errors.append("workers disagree on collection")

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        if self.diagnostics is not None:
            try:
                self.diagnostics.finished = time.monotonic()
            except Exception:
                self.diagnostics.errors.add("clock_failed")
                _diagnostic_warning()
        if hasattr(self.config, "workeroutput"):
            self.config.workeroutput["ci_collection"] = (self.collected, self.selected)
            return
        expected_workers = self.config.getoption("numprocesses", default=0) or 0
        if self.workers != expected_workers:
            self.errors.append("missing worker receipts")
        if not self.collected or sorted(self.completed) != self.selected:
            self.errors.append("collection or execution incomplete")
        if self.errors:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        receipt = dict(
            shard=self.shard, count=self.count,
            revision=self.config.getoption("ci_revision"),
            collected=self.collected, selected=self.selected,
            completed=self.completed, errors=self.errors,
            exitstatus=int(session.exitstatus),
        )
        path = Path(self.config.getoption("ci_manifest"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt) + "\n")
        # Keep original manifest failures outside the diagnostic error boundary.
        if self.diagnostics is not None:
            try:
                self.diagnostics.write(self, session, path.with_name("durations.json"))
            except Exception:
                _diagnostic_warning()
