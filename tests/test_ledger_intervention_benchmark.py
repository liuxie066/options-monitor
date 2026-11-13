"""Acceptance gates operate on the recorded real SQLite/API measurements."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests import ledger_intervention_benchmark as benchmark

FIXTURES = Path(__file__).parent / "fixtures" / "ledger_intervention_benchmark"


def report(name):
    return json.loads((FIXTURES / name).read_text())


def test_timing_comparison_rejects_counts_without_timing_key_access():
    counts = report("optimized-counts.json")
    assert benchmark.compare_timing_reports(counts, counts) == [
        "timing comparison requires two timing reports"]


def test_counts_comparison_rejects_timing_without_count_key_access():
    timing = report("baseline-timing.json")
    assert benchmark.compare_count_reports(timing, timing) == [
        "count comparison requires two counts reports"]


@pytest.mark.parametrize("suffix", ["counts.json", "single-lot-counts.json"])
def test_recorded_read_and_conversion_budgets_pass(suffix):
    before = report("baseline-" + suffix)
    after = report("optimized-" + suffix)
    assert benchmark.check_count_budgets(after) == []
    assert benchmark.compare_count_reports(before, after) == []


@pytest.mark.parametrize("metric,fragment", [
    ("ordinary_history_reads", "ordinary history read budget"),
    ("forced_full_history_reads", "independent forced-full read budget"),
    ("all_history_reads", "inconsistent all-history read count"),
    ("canonical", "canonical conversion budget"),
])
def test_counts_gate_rejects_a_regressed_actual_measurement(metric, fragment):
    counts = deepcopy(report("optimized-counts.json"))
    row = counts["results"][0]
    if metric == "canonical":
        row["conversion_calls"]["_canonical_payload_to_ledger_event"] += 1
    else:
        row[metric] += 1
    assert any(fragment in failure for failure in benchmark.check_count_budgets(counts))


def test_counts_gate_rejects_skipping_the_post_write_full_read():
    counts = deepcopy(report("optimized-counts.json"))
    row = next(row for row in counts["results"] if row["operation"] == "repair_apply")
    row["forced_full_history_reads"] = 0
    row["all_history_reads"] = row["ordinary_history_reads"]
    assert any("independent forced-full read budget" in failure
        for failure in benchmark.check_count_budgets(counts))


def test_all_recorded_timing_groups_pass():
    assert benchmark.compare_timing_reports(
        report("baseline-timing.json"), report("optimized-timing.json")) == []


def test_timing_gate_rejects_p95_regression_at_the_smallest_scale():
    before = report("baseline-timing.json")
    after = deepcopy(report("optimized-timing.json"))
    after["results"][0]["p95_seconds"] = before["results"][0]["p95_seconds"] + 0.001
    assert any("p95 regressed" in failure
        for failure in benchmark.compare_timing_reports(before, after))


def test_counts_comparison_rejects_a_different_fixture():
    before = report("baseline-counts.json")
    after = deepcopy(report("optimized-counts.json"))
    after["results"][0]["fixture_sha256"] = "different"
    assert any("fixture differs" in failure
        for failure in benchmark.compare_count_reports(before, after))
