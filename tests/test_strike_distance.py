from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from domain.domain.engine import (
    calculate_opening_candidate_metrics,
    evaluate_opening_candidate_policy,
    inspect_opening_candidate_distances,
    rank_candidate_rows,
)
from src.application.research.strike_distance import analyze_strike_distances, parse_distance_thresholds

FIXTURE = Path(__file__).parent / "fixtures/strike_distance_synthetic.json"
NOW = "2026-10-16T14:00:00Z"


def bundle():
    return json.loads(FIXTURE.read_text())


def inspect(**changes):
    row = bundle()["rows"][0]
    row.update(changes)
    return inspect_opening_candidate_distances(row, mode=row["option_type"], now_utc=NOW)


@pytest.mark.parametrize("mode,strike,expected", [("put", 99, .01), ("put", 100, 0), ("put", 101, -.01),
                                                    ("call", 99, -.01), ("call", 100, 0), ("call", 101, .01)])
def test_signed_distance(mode, strike, expected):
    result = inspect(option_type=mode, strike=strike)
    assert result["strike_distance_pct"] == expected
    assert result["net_breakeven_distance_pct"] == pytest.approx(expected + .03)


@pytest.mark.parametrize("changes,reason", [({"spot": 0}, "spot_or_strike_invalid"),
    ({"spot": None}, "spot_or_strike_invalid"), ({"spot": float("nan")}, "spot_or_strike_invalid"),
    ({"spot": True}, "spot_or_strike_invalid"), ({"strike": -1}, "spot_or_strike_invalid"),
    ({"spot_observed_at_utc": None}, "spot_time_missing_future_or_stale"),
    ({"spot_observed_at_utc": "2026-10-16T14:00:00"}, "spot_time_missing_future_or_stale"),
    ({"spot_observed_at_utc": "2026-10-16T14:00:01Z"}, "spot_time_missing_future_or_stale"),
    ({"spot_observed_at_utc": "2026-10-16T13:54:59Z"}, "spot_time_missing_future_or_stale"),
    ({"expiration": None}, "expiration_invalid"), ({"expiration": "2026-10-15"}, "expired_or_expiration_day"),
    ({"expiration": "2026-10-16"}, "expired_or_expiration_day"), ({"market": []}, "market_or_replay_time_invalid")])
def test_invalid_raw_distance(changes, reason):
    result = inspect(**changes)
    assert result["status"] == "unavailable"
    assert result["strike_distance_pct"] is None
    assert result["reason"] == reason


def test_freshness_boundary_and_exchange_calendar_date():
    assert inspect(spot_observed_at_utc="2026-10-16T13:55:00Z")["status"] == "ok"
    row = bundle()["rows"][0]
    row.update(expiration="2026-10-16", spot_observed_at_utc="2026-10-16T00:59:00Z")
    result = inspect_opening_candidate_distances(row, mode="put", now_utc="2026-10-16T01:00:00Z")
    assert result["dte"] == 1  # New York is still October 15.
    row["market"] = "HK"
    assert inspect_opening_candidate_distances(row, mode="put", now_utc="2026-10-16T01:00:00Z")["status"] == "unavailable"


@pytest.mark.parametrize("net,expected", [(0, .01), (-300, -.02), (300, .04)])
def test_zero_negative_net_and_cost_independence(net, expected):
    assert inspect(net_income=net, avg_cost=999)["net_breakeven_distance_pct"] == expected
    call = inspect(option_type="call", strike=101, net_income=net, avg_cost=1)
    assert call["net_breakeven_distance_pct"] == expected
    assert call["net_basis"] == "short_call_leg_not_covered_stock"


@pytest.mark.parametrize("changes", [{"multiplier": None}, {"snapshot_multiplier": 10}, {"multiplier": 1.5},
    {"option_standard_type": "NON_STANDARD"}, {"net_income": None}, {"net_income": float("inf")},
    {"fee_basis": None}, {"fee_schedule_version": None}, {"currency": "HKD"},
    {"snapshot_received_at_utc": None}, {"snapshot_received_at_utc": "2026-10-16T13:54:59Z"}])
def test_partial_metrics_do_not_hide_raw_distance(changes):
    result = inspect(**changes)
    assert result["status"] == "ok" and result["strike_distance_pct"] == .01
    assert result["net_status"] == "unavailable" and result["net_breakeven_distance_pct"] is None


@pytest.mark.parametrize("expiry,iv,rv", [("2026-10-23", .1, .01), ("2027-02-19", 2., 1.), ("2026-12-18", None, None)])
def test_no_volatility_recommendation(expiry, iv, rv):
    result = inspect(expiration=expiry, implied_volatility=iv, term_matched_rv=rv, term_matched_rv_status="ok")
    assert result["volatility_buffer"]["status"] == "unavailable"
    assert result["volatility_buffer"]["value"] is None


def test_shadow_keeps_all_rows_order_quantity_and_separates_wheel():
    original = bundle()
    before = deepcopy(original)
    off = analyze_strike_distances(original, {})
    on = analyze_strike_distances(original, {"csp": .03, "cc": .05})
    assert off["baseline_rows"] == on["baseline_rows"] == original["rows"] == before["rows"]
    assert [row["decision"] for row in on["observations"]] == ["would_exclude", "retain", "not_requested", "not_requested"]
    assert on["observations"][0]["forgone_net_premium"] == "600.0"
    assert on["summaries"][0]["status"] == "complete"
    assert "1% | 4% | 3% | 假设排除" in on["markdown"]
    assert "5% | 7% | 5% | 保留" in on["markdown"]
    on["baseline_rows"][0]["contracts"] = 123
    assert original == before


def test_mixed_currency_partial_unknown_and_negative_amounts():
    sample = bundle()
    sample["rows"][0]["net_income"] = None
    sample["rows"][1]["spot"] = 0
    sample["rows"][2]["strategy"] = "csp"
    sample["rows"][2]["contracts"] = None
    out = analyze_strike_distances(sample, {"csp": .03, "cc": .1, "wheel_call": .02})
    by_key = {(s["strategy"], s["currency"]): s for s in out["summaries"]}
    assert by_key["csp", "USD"]["status"] == "partial"
    assert by_key["csp", "HKD"]["missing_premium_candidate_ids"] == ["synthetic-wheel-put-1"]
    assert by_key["cc", "USD"]["unassessed_candidate_ids"] == ["synthetic-cc-1"]
    assert by_key["wheel_call", "USD"]["known_forgone_net_premium"] == "-5.0"


@pytest.mark.parametrize("values", [["csp=NaN"], ["csp=inf"], ["csp=-.01"], ["csp=2"], ["csp=.01000000001"],
                                      ["all=.03"], ["csp=.03", "csp=.04"], ["bad"]])
def test_bad_thresholds(values):
    with pytest.raises(ValueError):
        parse_distance_thresholds(values)


def test_canonical_rounding_equal_threshold():
    sample = bundle()
    sample["rows"][0]["strike"] = 98.99999999999
    out = analyze_strike_distances(sample, {"csp": .01})
    assert out["observations"][0]["decision"] == "retain"
    assert "1% | 4% | 1% | 保留" in out["markdown"]


def test_formal_metrics_policy_ranking_and_notifications_unchanged(tmp_path):
    # Load the immutable pre-change owner as an independent arithmetic oracle.
    baseline = subprocess.run(["git", "show", "5c74cb84:domain/domain/engine/candidate_engine.py"],
                              check=True, capture_output=True, text=True).stdout
    module_path = tmp_path / "baseline_candidate_engine.py"
    module_path.write_text(baseline)
    spec = importlib.util.spec_from_file_location("baseline_candidate_engine", module_path)
    oracle = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = oracle
    spec.loader.exec_module(oracle)
    from tests.test_candidate_engine_phase2_contract import _opening_row
    from tests.test_daily_decision_brief_renderer import _brief
    from src.application.daily_decision_brief_renderer import render_full_brief
    brief = _brief()
    rendered = render_full_brief(brief)
    for mode in ("put", "call"):
        rows = []
        for strike in (95, 100, 105):
            raw = _opening_row(mode=mode, strike=strike, spot=100, expiration="2026-11-20", dte=35,
                               contract_symbol=f"US.NVDA261120{mode[0].upper()}{strike * 1000}",
                               snapshot_received_at_utc=NOW, spot_observed_at_utc=NOW)
            metrics = calculate_opening_candidate_metrics(raw, mode=mode, avg_cost=60, now_utc=NOW)
            assert metrics == oracle.calculate_opening_candidate_metrics(raw, mode=mode, avg_cost=60, now_utc=NOW)
            rows.append({**raw, **metrics, "max_new_contracts": 2})
        prior = deepcopy(rows)
        decisions = [evaluate_opening_candidate_policy(row, mode=mode) for row in rows]
        ranked = rank_candidate_rows(rows, mode=mode)
        for row in rows:
            assert inspect_opening_candidate_distances(row, mode=mode, now_utc=NOW)["status"] == "ok"
        assert rows == prior
        assert decisions == [evaluate_opening_candidate_policy(row, mode=mode) for row in rows]
        assert ranked == rank_candidate_rows(rows, mode=mode)
    assert render_full_brief(brief) == rendered


def test_public_cli_no_runtime_network_or_writes(monkeypatch, capsys, tmp_path):
    import socket
    from src.interfaces.cli.main import main
    import src.interfaces.cli.research as cli
    def forbidden(*args, **kwargs):
        raise AssertionError("offline CLI attempted runtime/network/write access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(cli, "repo_base", forbidden)
    monkeypatch.setattr(cli, "run_research_collect", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    assert main(["research", "strike-distance", "--input", str(FIXTURE), "--threshold", "csp=.03"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["observations"][0]["decision"] == "would_exclude"
    assert main(["research", "strike-distance", "--input", str(tmp_path / "absent.json")]) != 0
    assert "INPUT_ERROR" in capsys.readouterr().out


@pytest.mark.parametrize("field", ["fee_basis", "fee_schedule_version"])
@pytest.mark.parametrize("value", [False, 0, [], {}, " "])
def test_fee_provenance_requires_real_nonempty_strings(field, value):
    assert inspect(**{field: value})["net_status"] == "unavailable"


def test_overflowing_multiplier_and_timestamps_fail_explicitly():
    assert inspect(multiplier=10**400, chain_multiplier=10**400, snapshot_multiplier=10**400)["net_status"] == "unavailable"
    sample = bundle()
    sample["as_of"] = "0001-01-01T00:00:00+14:00"
    with pytest.raises(ValueError, match="replay clock"):
        analyze_strike_distances(sample, {"csp": .03})
    assert inspect(spot_observed_at_utc="0001-01-01T00:00:00+14:00")["status"] == "unavailable"
    assert inspect(snapshot_received_at_utc="0001-01-01T00:00:00+14:00")["net_status"] == "unavailable"


@pytest.mark.parametrize("status", ["unavailable", "stale", "error", None, []])
def test_known_underlier_failure_is_not_overridden(status):
    result = inspect(underlier_observation_status=status, underlier_observation_reason_code="provider_failure")
    assert result["status"] == "unavailable" and result["source_reason"] == "provider_failure"


def test_real_main_argv_path_never_discovers_runtime(monkeypatch, capsys):
    import sys
    import src.interfaces.cli.command_environment as environment
    from src.interfaces.cli.main import main
    def forbidden(*args, **kwargs):
        raise AssertionError("offline command tried to discover/load environment")
    monkeypatch.setenv("OM_ENV_FILE", "/synthetic/forbidden.env")
    monkeypatch.setenv("OM_RUNTIME_ROOT", "/synthetic/forbidden-runtime")
    monkeypatch.setattr(environment, "runtime_root_record_path", forbidden)
    monkeypatch.setattr(environment, "read_runtime_root_record", forbidden)
    monkeypatch.setattr(environment, "bootstrap_process_env", forbidden)
    monkeypatch.setattr(environment, "runtime_root_scope", forbidden)
    monkeypatch.setattr(sys, "argv", ["om", "research", "strike-distance", "--input", str(FIXTURE)])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_cli_rejects_nonstandard_json_constants(constant, tmp_path, capsys):
    from src.interfaces.cli.main import main
    source = tmp_path / "nonstandard.json"
    source.write_text(FIXTURE.read_text().replace('"spot": 100,', f'"spot": {constant},', 1))
    assert main(["research", "strike-distance", "--input", str(source)]) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["error"]["code"] == "INPUT_ERROR"
