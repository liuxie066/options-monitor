"""Regressions for a fixed 60-second collection slot, using real collectors."""

from datetime import timedelta
import json
import os
import stat
from time import perf_counter
from pathlib import Path
import subprocess
import sys

import pytest

from src.application.research import watchlist_series as group
from test_watchlist_research_ancillary import acquisition, at_capture  # noqa: F401
from test_watchlist_research_day import calendar_job, job, setup, freeze, write  # noqa: F401
from test_watchlist_research_series import planned, inputs  # noqa: F401


def test_all_shards_share_one_connection_and_close_once(planned):
    _, _, _, args, state = planned
    original = args["gateway_factory"]
    lifecycle = []

    class Connection:
        def __init__(self):
            lifecycle.append("open")
            self.gateway = original()

        def get_snapshot(self, codes):
            return self.gateway.get_snapshot(codes)

        def close(self):
            lifecycle.append("close")
            self.gateway.close()

    args["gateway_factory"] = lambda **_: Connection()
    result = group.collect_watchlist_series(**args, confirm=True)
    assert len(result["batches"]) == 2
    assert [len(codes) for codes in state.calls] == [200, 1, 1, 1]
    assert lifecycle == ["open", "close"]
    group.collect_watchlist_series(**args, confirm=True)
    assert lifecycle == ["open", "close"]


def test_shared_connection_closes_on_provider_failure(planned):
    _, _, _, args, state = planned
    original = args["gateway_factory"]
    lifecycle = []

    class Connection:
        def __init__(self):
            lifecycle.append("open")
            self.gateway = original()

        def get_snapshot(self, codes):
            return self.gateway.get_snapshot(codes)

        def close(self):
            lifecycle.append("close")
            self.gateway.close()

    args["gateway_factory"] = lambda **_: Connection()
    state.fail = True
    result = group.collect_watchlist_series(**args, confirm=True)
    attempts = [batch["result"]["observations"][0] for batch in result["batches"]]
    assert all(attempt["quote_package_status"] == "incomplete" for attempt in attempts)
    assert all(not attempt["research_complete"] for attempt in attempts)
    assert lifecycle == ["open", "close"]


def test_shared_cleanup_failure_keeps_committed_result_and_replay(planned, caplog):
    root, _, _, args, state = planned
    original = args["gateway_factory"]
    lifecycle = []

    class Connection:
        def __init__(self):
            self.gateway = original()

        def get_snapshot(self, codes):
            return self.gateway.get_snapshot(codes)

        def close(self):
            lifecycle.append("close")
            raise RuntimeError("transport cleanup failed")

    args["gateway_factory"] = lambda **_: Connection()
    result = group.collect_watchlist_series(**args, confirm=True)
    assert result["status"] == "collected"
    assert len(list((root / "archive").glob("batches/*/observations/*/attempts/*/attempt.json"))) == 2
    assert "RuntimeError" in caplog.text
    calls = list(state.calls)
    group.collect_watchlist_series(**args, confirm=True)
    assert state.calls == calls
    assert lifecycle == ["close"]


@pytest.mark.parametrize("cleanup_error", [RuntimeError, SystemExit])
def test_shared_cleanup_keeps_original_interrupt(planned, caplog, cleanup_error):
    root, _, _, args, _ = planned

    class Connection:
        def get_snapshot(self, codes):
            raise KeyboardInterrupt("capture interrupted")

        def close(self):
            raise cleanup_error("cleanup failed after capture interrupt")

    args["gateway_factory"] = lambda **_: Connection()
    with pytest.raises(KeyboardInterrupt, match="capture interrupted"):
        group.collect_watchlist_series(**args, confirm=True)
    attempts = list((root / "archive").glob("batches/*/observations/*/attempts/*/attempt.json"))
    assert len(attempts) == 1
    assert "KeyboardInterrupt" in json.loads(attempts[0].read_bytes())["reasons"]
    assert cleanup_error.__name__ in caplog.text


def test_prepare_only_does_not_claim_even_when_clock_crosses_slot(planned):
    root, _, _, args, state = planned
    state.now -= timedelta(seconds=2)
    original = group.series._initialize_archive

    def slow_prepare(*values, **kwargs):
        result = original(*values, **kwargs)
        state.now += timedelta(seconds=2)
        return result

    from unittest.mock import patch

    with patch.object(group.series, "_initialize_archive", slow_prepare):
        result = group.collect_watchlist_series(**args, confirm=True, prepare_only=True)
    assert result["status"] == "prepared"
    assert not state.calls
    assert (root / "archive/watchlist-series.json").exists()
    assert not list((root / "archive").glob("batches/*/observations/*/claim"))
    group.collect_watchlist_series(**args, confirm=True)
    assert len(state.calls) == 4


def test_ancillary_lead_window_prepares_archive_before_quote_slot(acquisition):
    from src.application.research import watchlist_day as daily

    args, _, state = acquisition
    daily.run_watchlist_day(**args, confirm=True)
    at_capture(state)
    result = daily.run_watchlist_day(**args, confirm=True)
    assert result["collection"] is None
    archive = args["day_dir"] / "archive"
    assert (archive / "watchlist-series.json").exists()
    assert list(archive.glob("batches/*/inputs/ancillary/captured/capture.json"))
    assert not list(archive.glob("batches/*/observations/*/claim"))
    assert not [call for call in state.calls if call[0] == "snapshot"]
    state.now += timedelta(minutes=1)
    daily.run_watchlist_day(**args, confirm=True)
    assert [call[0] for call in state.calls].count("snapshot") == 2


def test_storage_counts_hardlinks_once_and_symlinks_without_following(tmp_path):
    root = tmp_path / "research"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    payload = nested / "data"
    payload.write_bytes(b"x" * 4097)
    os.link(payload, root / "same-inode")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "unrelated").write_bytes(b"y" * 20000)
    (root / "link").symlink_to(external, target_is_directory=True)
    (root / "broken").symlink_to(tmp_path / "absent")
    stats = {
        (p.lstat().st_dev, p.lstat().st_ino): p.lstat()
        for p in [root, nested, payload, root / "same-inode", root / "link", root / "broken"]
    }
    result = group._storage(root, [root, nested], {"max_bytes": 10**9, "min_free_bytes": 1})
    assert result["logical_bytes"] == sum(v.st_size for v in stats.values() if not stat.S_ISDIR(v.st_mode))
    assert result["allocated_bytes"] == sum(v.st_blocks * 512 for v in stats.values())
    with pytest.raises(ValueError, match="budget exceeded"):
        group._storage(root, [root], {"max_bytes": result["used_bytes"], "min_free_bytes": 1})


@pytest.mark.parametrize("planned", [850, 1397], indirect=True)
def test_full_market_roster_finishes_in_sixty_second_slot(planned):
    root, _, plan, args, state = planned
    expected = set(plan["slots"][0]["option_codes"])
    opened, closed = [], []
    original = args["gateway_factory"]

    class Connection:
        def __init__(self):
            opened.append(state.now)
            self.gateway = original()
            # Account for a real connection/readiness delay in the frozen clock.
            state.now += timedelta(seconds=2)

        def get_snapshot(self, codes):
            state.now += timedelta(seconds=0.5)
            return self.gateway.get_snapshot(codes)

        def close(self):
            closed.append(state.now)
            self.gateway.close()

    args["gateway_factory"] = lambda **_: Connection()
    started = perf_counter()
    result = group.collect_watchlist_series(**args, confirm=True)
    elapsed = perf_counter() - started
    captured = set()
    for batch in result["batches"]:
        attempt = batch["result"]["observations"][0]
        assert attempt["status"] != "missed"
        assert attempt["quote_package_status"] == "complete"
    for manifest in (root / "archive").glob("batches/*/observations/*/attempts/*/quotes/manifest.json"):
        captured.update(json.loads(manifest.read_bytes())["returned_codes"])
    assert captured == expected
    assert len(opened) == len(closed) == 1
    assert elapsed < 60
    assert state.now < group.series._utc(plan["slots"][0]["deadline_utc"])
    print(f"full roster: contracts={len(expected)} batches={len(result['batches'])} elapsed={elapsed:.3f}s")


def test_research_cli_import_does_not_load_live_application():
    root = Path(__file__).resolve().parents[1]
    code = """
import importlib.abc
import sys
class BlockLiveImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'src.application.agent_tool_config', 'src.application.pipeline_runtime', 'src.application.config_yaml'}:
            raise AssertionError('research startup imported ' + fullname)
sys.meta_path.insert(0, BlockLiveImports())
import src.interfaces.cli.research
import src.application.research
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["research", "collect", "--write-outputs"],
        [
            "research",
            "observation-series-plan",
            "--scope",
            "/absent-scope",
            "--calendar",
            "/absent-calendar",
            "--policy",
            "/absent-policy",
            "--start",
            "2026-11-02",
            "--end",
            "2026-11-02",
            "--output-dir",
            "/unused-output",
            "--observation-only",
            "--observations",
            "/absent-observations",
        ],
    ],
)
def test_lightweight_cli_preserves_failure_contract(arguments):
    root = Path(__file__).resolve().parents[1]
    outputs = [
        subprocess.run(
            [sys.executable, "-B", "-m", module, *arguments],
            cwd=root,
            capture_output=True,
            text=True,
        )
        for module in ("src.interfaces.cli.main", "src.interfaces.cli.research")
    ]
    assert outputs[0].returncode == outputs[1].returncode == 2
    assert json.loads(outputs[0].stdout) == json.loads(outputs[1].stdout)


def test_systemd_uses_the_lightweight_research_entry():
    root = Path(__file__).resolve().parents[1]
    template = (root / "services/systemd/options-monitor-research-watchlist-schedule.service.in").read_text()
    command = next(line for line in template.splitlines() if line.startswith("ExecStart="))
    assert "-m src.interfaces.cli.research research watchlist-schedule-run" in command
    assert "--confirm" in command and "--expected-schedule-sha256" in command
