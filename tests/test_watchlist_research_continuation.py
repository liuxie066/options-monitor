from __future__ import annotations

import json

import pytest

from src.application.research import watchlist_scope as scope
from src.application.research import watchlist_series as group
from test_watchlist_research_scope import ASOF, inputs, row, run, save  # noqa: F401
from test_watchlist_research_series import planned  # noqa: F401


def simulated(root, data, *, contracts=(), stocks=()):
    value = {
        "schema_version": "optionbench.simulation-tracking.v1",
        "as_of_utc": ASOF,
        "report_sha256": "a" * 64,
        "identities_sha256": "b" * 64,
        "state_pointer": "/final_state",
        "contracts": list(contracts),
        "stocks": list(stocks),
    }
    data["simulation_tracking"] = save(root, "simulation.json", value)
    return value


def test_daily_roll_keeps_old_pool_and_first_seen_after_all_symbols_removed(inputs):
    root, data = inputs
    first = run(inputs, output=root / "day1")
    original = json.loads((root / "day1/tracking.json").read_bytes())
    data.update(as_of_utc="2026-10-31T15:00:00Z", symbols=[])
    data["previous_tracking"] = save(root, "previous.json", original)
    second = run(inputs, output=root / "day2")
    assert json.loads((root / "day2/tracking.json").read_bytes())["contracts"] == original["contracts"]
    assert first["counts"]["US"]["contracts"] == second["counts"]["US"]["contracts"] == 1
    assert second["new_contracts"] == 0
    assert second["retired_codes"] == []
    with pytest.raises(FileExistsError):
        run(inputs, output=root / "day2")


def test_simulated_option_outside_window_and_stock_only_retained(inputs):
    root, data = inputs
    extra = {"option_code": row(10, owner="US.OTHER")["code"], "underlier_code": "US.OTHER"}
    simulated(root, data, contracts=[extra], stocks=["HK.00700"])
    result = run(inputs, output=root / "scope")
    assert result["counts"]["US"]["contracts"] == 2
    batch = json.loads((root / "scope/batches/hk-s0000/scope.json").read_bytes())
    assert batch["contracts"] == []
    assert batch["underlier_codes"] == ["HK.00700"]
    assert extra["option_code"] in {
        x["option_code"] for x in json.loads((root / "scope/tracking.json").read_bytes())["contracts"]
    }


def test_stock_obligation_ends_but_research_pool_option_survives(inputs):
    root, data = inputs
    simulated(root, data, stocks=["US.OTHER"])
    run(inputs, output=root / "first")
    data["previous_tracking"] = save(root, "previous.json", json.loads((root / "first/tracking.json").read_bytes()))
    simulated(root, data)
    data["symbols"] = []
    result = run(inputs, output=root / "next")
    assert [x["batch_id"] for x in result["batches"]] == ["us-0000"]
    assert result["counts"]["US"]["underliers"] == 1


def test_missing_new_simulation_export_carries_stocks_until_explicit_closed_snapshot(inputs):
    root, data = inputs
    simulated(root, data, stocks=["US.OTHER"])
    run(inputs, output=root / "first")
    data["previous_tracking"] = save(root, "previous.json", json.loads((root / "first/tracking.json").read_bytes()))
    del data["simulation_tracking"]
    data.update(symbols=[], as_of_utc="2026-10-31T15:00:00Z")
    result = run(inputs, output=root / "carried")
    assert result["counts"]["US"]["underliers"] == 2
    assert any(batch["batch_id"] == "us-s0000" for batch in result["batches"])


@pytest.mark.parametrize("fault", ["future", "digest", "duplicate", "owner", "stock", "pointer"])
def test_bad_simulation_export_rejected(inputs, fault):
    root, data = inputs
    record = {"option_code": row(10)["code"], "underlier_code": "US.ACME"}
    value = simulated(root, data, contracts=[record])
    if fault == "future":
        value["as_of_utc"] = "2026-10-31T15:00:00Z"
    elif fault == "digest":
        value["report_sha256"] = "bad"
    elif fault == "duplicate":
        value["contracts"].append(record)
    elif fault == "owner":
        value["contracts"][0]["underlier_code"] = "US.OTHER"
    elif fault == "stock":
        value["stocks"] = ["HK.700"]
    elif fault == "pointer":
        value = {"nested": value}
    data["simulation_tracking"] = save(root, "simulation.json", value, pointer="/nested" if fault == "pointer" else "")
    with pytest.raises(ValueError):
        run(inputs)


@pytest.mark.parametrize("option_count,failed", [(0, False), (201, False), (0, True)])
def test_stocks_real_planner_fetcher_export_and_budget(planned, option_count, failed):
    root, build, _, collect, state = planned
    data = json.loads((root / "input.json").read_bytes())
    if option_count == 0:
        data["symbols"] = []
    simulated(root, data, stocks=["US.OTHER"])
    result = run((root, data), output=root / "scope-stocks")
    plan = group.build_watchlist_series(
        **{
            **build,
            "scope_dir": root / "scope-stocks",
            "expected_scope_sha256": scope.quote._sha256((root / "scope-stocks/watchlist-scope.json").read_bytes()),
            "output_dir": root / "plan-stocks",
        }
    )
    assert plan["slots"][0]["normal_calls"] == (5 if option_count else 1)
    safe = save(
        root,
        "safe-stocks.json",
        {
            "schema_version": "research.watchlist-safe-window.v1",
            "watchlist_series_sha256": plan["watchlist_series_sha256"],
            "intervals": [{"start_utc": "2026-10-30T19:55:00Z", "end_utc": "2026-10-30T19:56:00Z"}],
        },
    )
    args = {
        **collect,
        "plan_dir": root / "plan-stocks",
        "expected_plan_sha256": plan["watchlist_series_sha256"],
        "safe_window_file": root / "safe-stocks.json",
        "expected_safe_window_sha256": safe["sha256"],
        "archive_root": root / "archive-stocks",
    }
    state.fail = failed
    group.collect_watchlist_series(**args, confirm=True)
    assert len(state.calls) == (5 if option_count else 1)
    assert state.calls[-1] == ["US.OTHER"]
    exported = group.export_watchlist_series(
        series_dir=root / "archive-stocks",
        expected_plan_sha256=plan["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=root / "export-stocks",
    )
    assert len(exported["batches"]) == len(result["batches"])
    assert not list((root / "export-stocks/exports/us-s0000").rglob("quotes.json"))
    # Keep genuine producer output for the independent OB fixture/consumer check.
    (root / "wire-anchors.json").write_bytes(
        scope.series._bytes(
            {
                "expected_plan_sha256": plan["watchlist_series_sha256"],
                "expected_export_sha256": scope.quote._sha256(
                    (root / "export-stocks/watchlist-export.json").read_bytes()
                ),
                "expected_implementation_sha256": exported["producer_implementation_sha256"],
            }
        )
    )
