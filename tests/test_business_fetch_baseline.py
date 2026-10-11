"""Offline entry counts, truthful failures and isolation of the measurement harness."""
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import socket
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "business_fetch_baseline", Path(__file__).resolve().parents[1] / "scripts/benchmark_business_fetch.py")
baseline = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(baseline)

# Counts are for the actual provider and its real gateway/client over a synthetic SDK context.
EXPECTED = {"scan_cold": 1, "scan_warm": 0, "cash_cli_cold": 1, "cash_tool_cold": 2,
            "cash_tool_warm": 0, "cross_entry": 2, "prepared": 1, "prepared_new_run": 2,
            "prepared_bad_hash": 1, "prepared_bad_account": 1, "account_isolation": 2,
            "physical_account_isolation": 2, "path_isolation": 2, "stock_301_cash_fresh": 1,
            "stock_option_61": 1, "partial_stock": 2, "positions_failure": 2,
            "balance_failure": 2, "timeout": 2, "cancel": 2, "assignment_pm_unavailable": 1,
            "wheel_cli_cold": 2, "wheel_tool_cold": 2}


@pytest.mark.parametrize("case", baseline.CASES)
def test_real_entry_counts_and_failure_status(case):
    row = baseline.sample(case)
    expected = EXPECTED[case]
    assert row["counts"].get("portfolio_fetch", 0) == expected
    sdk_count = 0 if case in {"timeout", "cancel"} else expected
    assert row["counts"].get("sdk_accinfo_substitute", 0) == sdk_count
    assert row["counts"].get("sdk_positions_substitute", 0) == (0 if case == "balance_failure" else sdk_count)
    assert row["blocked_network_attempts"] == 0
    if case in {"timeout", "cancel"}:
        assert row["outcome"] == ["TimeoutError" if case == "timeout" else "CancelledError"] * 2
    elif case == "balance_failure":
        assert all(ctx["source"] == "unavailable" and ctx["cash"] == "unknown" for ctx in row["contexts"])
    else:
        assert all(ctx["cash"] == "fresh" for ctx in row["contexts"])
    if case in {"partial_stock", "positions_failure"}:
        assert all(ctx["stock_errors"] for ctx in row["contexts"])
    elif case not in {"stock_301_cash_fresh", "balance_failure"}:
        assert all(not ctx["stock_errors"] for ctx in row["contexts"])
    if case == "stock_301_cash_fresh":
        assert row["contexts"][0]["stock_errors"] and not row["contexts"][1]["stock_errors"]
    if case == "prepared":
        assert row["outcome"] == [True, True, "ready"]
        assert [stage["counts"].get("portfolio_fetch", 0) for stage in row["stages"]] == [1, 0, 0, 0]
    if case in {"prepared_bad_hash", "prepared_bad_account"}:
        assert row["outcome"] == "rejected_without_fallback"
    if case == "cross_entry":
        assert row["outcome"]["assignment"] == "complete"
        assert row["outcome"]["wheel"]["coverage"] == row["outcome"]["wheel"]["cash"] == "available"
        assert row["counts"]["quotes_fixture"] == 1
        assert row["counts"].get("pm_fixture", 0) == 0
    if case == "assignment_pm_unavailable":
        assert row["outcome"]["status"] == "partial"
        assert row["counts"]["pm_fixture"] == row["counts"]["quotes_fixture"] == 1
    if case.startswith("wheel_"):
        assert row["outcome"]["coverage"] == row["outcome"]["cash"] == "available"


def test_aggregation_rejects_mixed_paths_and_accepts_timing_variance():
    row = baseline.sample("prepared")
    later = {**row, "wall_ms": row["wall_ms"] + 1}
    assert len(baseline.aggregate([row, later])["samples"]) == 2
    bad = {**row, "outcome": "different"}
    with pytest.raises(AssertionError, match="inconsistent"):
        baseline.aggregate([row, bad])


@pytest.mark.parametrize("value", [0, 51, True, "7", 1.2])
def test_sample_bound(value):
    with pytest.raises(ValueError, match="1..50"):
        baseline.run(value)


def test_network_guard_and_fixture_path_escape(tmp_path):
    original_connect, original_futu = socket.socket.connect, sys.modules.get("futu")
    with ExitStack() as stack:
        harness = baseline.Harness(tmp_path, stack)
        with pytest.raises(baseline.IsolationError, match="network"):
            socket.create_connection(("offline.invalid", 1))
        assert harness.blocked == 1
        with pytest.raises(baseline.IsolationError, match="escaped"):
            harness.ensure_path(tmp_path.parent / "not-fixture")
    assert socket.socket.connect is original_connect
    assert sys.modules.get("futu") is original_futu


def test_swallowed_network_attempt_invalidates_sample(monkeypatch):
    actual = baseline.Harness.execute
    def attempted(harness, case):
        try:
            harness.deny_network()
        except baseline.IsolationError:
            pass
        return actual(harness, case)
    monkeypatch.setattr(baseline.Harness, "execute", attempted)
    with pytest.raises(baseline.IsolationError, match="blocked network attempts"):
        baseline.sample("scan_warm")
