"""Opt-in CI file sharding with fail-closed collection/execution receipts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from regression_manifest import shard_for


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
