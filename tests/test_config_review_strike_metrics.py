from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from src.application.reports.strike_metrics import (
    DIRECTIONS, EVIDENCE_SCHEMA, assess_boundary, load_candidate_evidence,
    previous_week, report_conclusion, summarize_rejections, unavailable_evidence,
)

NOW = datetime(2026, 11, 12, 4, tzinfo=timezone.utc)


def _evidence(*, n=30, rejected=30, legacy=False, marked=True, mode="put", reason="put_strike_above_max", extra=0, threshold=100):
    snapshots = []
    for day in range(3):
        rows = []
        for index in range(day, n, 3):
            rejects = []
            if index < rejected:
                rejects.append({"reason": "hard_strike" if legacy else reason, "stage": "prefilter", "threshold": threshold})
                if index < extra:
                    rejects.append({"reason": "return_annualized", "stage": "quality", "threshold": 0.1})
            rows.append({"candidate_id": f"id-{index}", "symbol": "NVDA", "strategy_mode": mode,
                         "contract_symbol": f"NVDA_OPT_{index % 6}",
                         "normalized_input": {"policy_min_strike": 100, "policy_max_strike": 100},
                         "opening_decision": {"accepted": not rejects, "rejects": rejects}})
        snapshots.append({"run_id": f"run-{day}", "account": "fixture", "market": "US", "scan_mode": "standard",
                          "sealed_at_utc": f"2026-11-0{day + 2}T04:00:00Z", "content_sha256": str(day) * 64,
                          "source_path": f"fixture/{day}", "strike_boundary_reason_codes_version": 1 if marked else None,
                          "universe": {"status": "complete", "affected_scopes": []},
                          "scope_keys": [["NVDA", mode]], "candidate_decisions": rows})
    return {"schema_version": EVIDENCE_SCHEMA, "period": previous_week(NOW), "exported_at_utc": NOW.isoformat(),
            "source": {"fixture": True}, "snapshots": snapshots, "errors": [], "excluded_runs": 0}


def _assess(evidence, *, spot=115, path="sell_put.max_strike", enabled=True, timestamp=None):
    return assess_boundary(market="US", symbol="NVDA", path=path, value=100,
                           enabled=enabled, price={"value": spot, "source": "fixture", "observed_at_utc": timestamp or NOW.isoformat()},
                           evidence=evidence, now=NOW)


def _load(tmp_path, evidence):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    return load_candidate_evidence(path, NOW)


@pytest.mark.parametrize("rejected,marked,spot,expected,threshold", [(30, True, 115, "建议调整", 100),
                                                                     (0, True, 105, "无需调整", 100),
                                                                     (0, False, 105, "观察", 100),
                                                                     (30, True, 105, "观察", None),
                                                                     (30, True, 105, "观察", "unavailable")])
def test_real_report_cli_persists_each_conclusion(tmp_path, monkeypatch, capsys, rejected, marked, spot, expected, threshold):
    from src.interfaces.cli import report_ops
    from src.interfaces.cli.main import parse_args

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz or timezone.utc)
    monkeypatch.setattr(report_ops, "datetime", FrozenDatetime)
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "user.hk.json").write_text('{"symbols":[]}')
    (configs / "user.us.json").write_text(json.dumps({"symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 100}}]}))
    (configs / "system.json").write_text("{}")
    price_file = tmp_path / "prices.json"
    price_file.write_text(json.dumps({"generated_at_utc": NOW.isoformat(), "prices": {"NVDA": spot}}))
    evidence_file = tmp_path / "evidence.json"
    evidence_file.write_text(json.dumps(_evidence(rejected=rejected, marked=marked, threshold=threshold)))
    args = parse_args(["report", "run", "config_review", "--runtime-root", str(tmp_path), "--configs-root", str(configs),
                       "--prices-file", str(price_file), "--candidate-evidence-file", str(evidence_file), "--no-deliver", "--json"])
    assert report_ops.handle_report_command(args, repo_base_fn=lambda: tmp_path) == 0
    receipt = json.loads(capsys.readouterr().out)
    run_dir = Path(receipt["run_dir"])
    payload = json.loads((run_dir / "report.json").read_text())
    markdown = (run_dir / "report.md").read_text()
    assert payload["conclusion"]["state"] == receipt["summary"]["conclusion"] == expected
    assert markdown.index(f"**结论：{expected}**") < markdown.index("## 指标 A")
    assert (run_dir / "candidate_evidence.json").is_file()
    if expected == "无需调整":
        assert payload["strike_rejections"]["count"] == 0
    if not marked or threshold is None or threshold == "unavailable":
        assert payload["strike_rejections"]["count"] is None and "该期无数据" in markdown


def test_week_uses_shanghai_complete_calendar_week():
    period = previous_week(NOW)
    assert period["start_utc"] == "2026-11-01T16:00:00Z"
    assert period["end_utc"] == "2026-11-08T16:00:00Z"
    assert previous_week(datetime(2026, 11, 8, 15, 59, tzinfo=timezone.utc))["end_utc"] == "2026-11-01T16:00:00Z"
    assert previous_week(datetime(2026, 11, 8, 16, tzinfo=timezone.utc))["end_utc"] == "2026-11-08T16:00:00Z"
    with pytest.raises(ValueError, match="timezone"):
        previous_week(datetime(2026, 11, 12))


def test_old_and_unmarked_history_are_unknown_not_zero(tmp_path):
    for evidence in (_evidence(legacy=True, marked=False), _evidence(rejected=0, marked=False)):
        metric = summarize_rejections(_load(tmp_path, evidence))
        assert metric["coverage"] == "unavailable"
        assert metric["count"] is None
        assert all(value is None for value in metric["direction_counts"].values())
    metric = summarize_rejections(_load(tmp_path, _evidence(legacy=True, marked=False)))
    assert metric["legacy_hard_strike_count"] == 30


def test_marked_complete_zero_is_real_zero_and_can_need_no_adjustment(tmp_path):
    evidence = _load(tmp_path, _evidence(rejected=0))
    metric = summarize_rejections(evidence)
    assert metric["coverage"] == "complete" and metric["count"] == 0
    assert metric["direction_counts"] == dict.fromkeys(DIRECTIONS, 0)
    assessment = _assess(evidence, spot=105)
    assert assessment["conclusion"] == "无需调整"
    assert assessment["N"] == 30 and assessment["R"] == 0
    assert report_conclusion([assessment], [])["state"] == "无需调整"


def test_positive_unmarked_and_mixed_history_are_partial(tmp_path):
    evidence = _evidence(marked=False)
    evidence["snapshots"][0]["strike_boundary_reason_codes_version"] = 1
    loaded = _load(tmp_path, evidence)
    metric = summarize_rejections(loaded)
    assert metric["coverage"] == "partial" and metric["count"] is None
    assert metric["observed_count"] == 30
    assert _assess(loaded)["conclusion"] == "观察"


def test_directions_union_repeated_reason_and_distinct_contracts(tmp_path):
    evidence = _evidence()
    row = evidence["snapshots"][0]["candidate_decisions"][0]
    row["opening_decision"]["rejects"] *= 2
    row["opening_decision"]["rejects"].append({"reason": "call_strike_below_min", "stage": "prefilter", "threshold": 100})
    metric = summarize_rejections(_load(tmp_path, evidence))
    assert metric["count"] == 30
    assert metric["direction_counts"]["put_strike_above_max"] == 30
    assert metric["direction_counts"]["call_strike_below_min"] == 1
    assert metric["unique_contracts"] == 6
    assert metric["markets"][0]["count"] == 30


def test_duplicate_copy_is_once_but_conflicting_identity_is_gap(tmp_path):
    evidence = _evidence()
    copy = deepcopy(evidence["snapshots"][0])
    copy["source_path"] = "another-copy"
    evidence["snapshots"].append(copy)
    assert summarize_rejections(_load(tmp_path, evidence))["count"] == 30
    copy["candidate_decisions"][0]["normalized_input"]["policy_max_strike"] = 99
    loaded = _load(tmp_path, evidence)
    assert loaded["errors"] and not loaded["snapshots"]
    assert "conflicting snapshot" in loaded["errors"][0]["reason"]


@pytest.mark.parametrize("reason", list(DIRECTIONS))
def test_all_direction_reason_codes_are_counted(tmp_path, reason):
    metric = summarize_rejections(_load(tmp_path, _evidence(reason=reason)))
    assert metric["direction_counts"][reason] == 30


def test_recommendation_counts_only_strike_and_current_same_value(tmp_path):
    evidence = _load(tmp_path, _evidence(extra=10))
    row = _assess(evidence)
    assert (row["N"], row["R"], row["C"], row["unique_contracts"], row["rejection_dates"]) == (30, 30, 20, 6, 3)
    assert row["conclusion"] == "建议调整"
    assert row["distance_pct"] == 15
    assert _assess(evidence, spot=114.999999)["conclusion"] == "观察"
    # A dynamic bound may reject, but cannot be attributed to the current config.
    for snapshot in evidence["snapshots"]:
        for candidate in snapshot["candidate_decisions"]:
            candidate["normalized_input"]["policy_max_strike"] = 110
    assert _assess(evidence)["N"] == 0
    assert _assess(evidence)["conclusion"] == "观察"
    assert summarize_rejections(evidence)["count"] == 30


@pytest.mark.parametrize("n,rejected,extra,expected", [(29, 29, 0, "观察"), (30, 19, 0, "观察"),
                                                       (100, 29, 0, "观察"), (100, 30, 0, "建议调整"),
                                                       (30, 30, 11, "观察")])
def test_recommendation_sample_count_and_rate_edges(tmp_path, n, rejected, extra, expected):
    assert _assess(_load(tmp_path, _evidence(n=n, rejected=rejected, extra=extra)))["conclusion"] == expected


@pytest.mark.parametrize("rejected,expected", [(2, "无需调整"), (3, "观察")])
def test_no_change_rejection_rate_is_strictly_below_ten_percent(tmp_path, rejected, expected):
    assert _assess(_load(tmp_path, _evidence(rejected=rejected)), spot=105)["conclusion"] == expected
    assert _assess(_load(tmp_path, _evidence(rejected=0)), spot=105.00001)["conclusion"] == "观察"


def test_sample_breadth_and_rejection_dates_are_required(tmp_path):
    evidence = _evidence()
    for snapshot in evidence["snapshots"]:
        for row in snapshot["candidate_decisions"]:
            row["contract_symbol"] = "ONE_CONTRACT"
    assert _assess(_load(tmp_path, evidence))["conclusion"] == "观察"
    evidence = _evidence()
    for snapshot in evidence["snapshots"]:
        snapshot["sealed_at_utc"] = "2026-11-02T04:00:00Z"
    assert _assess(_load(tmp_path, evidence))["conclusion"] == "观察"


def test_min_boundary_reverses_pressure_and_disabled_row_does_not_drive_report(tmp_path):
    evidence = _load(tmp_path, _evidence(mode="call", reason="call_strike_below_min"))
    row = _assess(evidence, spot=85, path="sell_call.min_strike")
    assert row["distance_pct"] == -15 and row["pressure_pct"] == 15
    assert row["conclusion"] == "建议调整"
    inactive = _assess(evidence, spot=85, path="sell_call.min_strike", enabled=False)
    assert inactive["conclusion"] == "观察"
    assert report_conclusion([inactive], [])["state"] == "观察"


@pytest.mark.parametrize("stamp", ["2026-11-07T00:00:00Z", "2026-11-12T05:00:00Z", "2026-11-12T04:00:00", "bad"])
def test_stale_future_naive_or_invalid_price_time_is_observe(tmp_path, stamp):
    assert _assess(_load(tmp_path, _evidence()), timestamp=stamp)["conclusion"] == "观察"


def test_96_hour_price_age_boundary(tmp_path):
    evidence = _load(tmp_path, _evidence())
    assert _assess(evidence, timestamp=(NOW - timedelta(hours=96)).isoformat())["conclusion"] == "建议调整"
    assert _assess(evidence, timestamp=(NOW - timedelta(hours=96, seconds=1)).isoformat())["conclusion"] == "观察"


@pytest.mark.parametrize("threshold", [None, "unavailable", float("nan"), float("inf"), True])
def test_directional_rejection_requires_a_finite_threshold(tmp_path, threshold):
    evidence = _load(tmp_path, _evidence(threshold=threshold))
    assert evidence["errors"] and evidence["snapshots"] == []
    assert _assess(evidence, spot=105)["conclusion"] == "观察"
    assert summarize_rejections(evidence)["count"] is None


def test_missing_threshold_empty_reason_and_unknown_symbol_are_gaps(tmp_path):
    mutations = [lambda r: r["opening_decision"]["rejects"][0].pop("threshold"),
                 lambda r: r["opening_decision"]["rejects"][0].update(reason=""),
                 lambda r: r["opening_decision"]["rejects"][0].update(reason=" put_strike_above_max "),
                 lambda r: r.update(symbol="???")]
    for mutate in mutations:
        evidence = _evidence()
        mutate(evidence["snapshots"][0]["candidate_decisions"][0])
        loaded = _load(tmp_path, evidence)
        assert loaded["errors"] and loaded["snapshots"] == []
        assert _assess(loaded, spot=105)["conclusion"] == "观察"
        assert summarize_rejections(loaded)["count"] is None


def test_missing_or_invalid_inputs_do_not_fall_back(tmp_path):
    assert load_candidate_evidence(tmp_path / "missing.json", NOW)["errors"]
    for mutate in (
        lambda e: e.update(period="not-a-period"),
        lambda e: e["period"].update(end_utc="2026-11-01T16:00:00Z"),
        lambda e: e.update(exported_at_utc="2026-11-13T00:00:00Z"),
        lambda e: e["snapshots"][0]["universe"].update(status="false"),
        lambda e: e["snapshots"][0]["candidate_decisions"][0]["opening_decision"].update(accepted=True),
        lambda e: e["snapshots"][0].update(strike_boundary_reason_codes_version=True),
    ):
        evidence = _evidence()
        mutate(evidence)
        loaded = _load(tmp_path, evidence)
        assert not loaded["snapshots"] and loaded["errors"]


def test_relevant_scan_failure_and_export_error_block_recommendation(tmp_path):
    evidence = _evidence()
    evidence["snapshots"][0]["universe"] = {"status": "partial", "affected_scopes": [{"symbol": "NVDA", "strategy_mode": "put"}]}
    assert _assess(_load(tmp_path, evidence))["conclusion"] == "观察"
    evidence = _evidence()
    evidence["errors"].append({"reason": "snapshot_validation_failed"})
    assert _assess(_load(tmp_path, evidence))["conclusion"] == "观察"
    assert report_conclusion([], ["missing market"])["state"] == "观察"
    assert summarize_rejections(unavailable_evidence(NOW, "fetch_failed"))["count"] is None


def test_out_of_week_snapshot_does_not_decode_large_candidate_body(tmp_path, monkeypatch):
    from src.application.reports.strike_metrics import collect_candidate_evidence

    path = tmp_path / "output_runs/arbitrary-name/accounts/fixture/state/opening_candidate_snapshot.json"
    path.parent.mkdir(parents=True)
    # Selection follows the sealed field, regardless of the directory identity.
    path.write_text(json.dumps({"candidate_decisions": "x" * 100000,
                                "sealed_at_utc": "2026-10-01T00:00:00Z"}, sort_keys=True))
    original = Path.read_text
    forbidden_reads = []

    def read_text(current, *args, **kwargs):
        if current == path:
            forbidden_reads.append(current)
            raise AssertionError("out-of-week body must not be fully decoded")
        return original(current, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    evidence = collect_candidate_evidence(tmp_path, NOW)
    assert forbidden_reads == []
    assert evidence["snapshots"] == [] and evidence["errors"] == []
    assert summarize_rejections(evidence)["count"] is None


def test_seal_selection_falls_back_for_nested_keys_and_unusual_json(tmp_path):
    from src.application.reports.strike_metrics import _snapshot_sealed_at

    path = tmp_path / "snapshot.json"
    stamp = "2026-11-03T04:00:00Z"
    for payload in ({"sealed_at_utc": stamp, "z": {"sealed_at_utc": "2026-10-01T00:00:00Z"}},
                    {"sealed_at_utc": stamp, "z": "x" * 100000}):
        path.write_text(json.dumps(payload))
        assert _snapshot_sealed_at(path) == stamp
    path.write_text('{"sealed_at_utc" \n : "2026-11-03T04:00:00Z"}')
    assert _snapshot_sealed_at(path) == stamp
    path.write_text("invalid JSON")
    with pytest.raises(ValueError):
        _snapshot_sealed_at(path)
