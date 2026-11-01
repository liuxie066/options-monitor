from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import stat

import pytest
import pandas as pd

from domain.domain.option_lifecycle import INSTRUMENT_POLICY_REGISTRY
from src.application.required_data_planning import (
    OptionSideFetchPlan,
    RequiredDataFetchPlanBundle,
    StrikeWindowPlan,
)
from src.application.research import observation_series as series
from src.application.research import watchlist_scope as scope
from src.interfaces.cli.research import add_research_commands, handle_research_command

ASOF = "2026-10-30T15:00:00Z"


def row(strike, *, side="put", expiry="2026-11-20", owner="US.ACME"):
    digits = int(Decimal(str(strike)) * 1000)
    return {
        "code": f"{owner}{date.fromisoformat(expiry):%y%m%d}{side[0].upper()}{digits}",
        "stock_owner": owner,
        "option_type": side.upper(),
        "strike_price": strike,
        "strike_time": expiry,
    }


def bundle(*windows, symbol="ACME"):
    return RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=100,
        side_plans=[
            OptionSideFetchPlan(
                option_type=side,
                min_dte=7,
                max_dte=30,
                explicit_expirations=[],
                strike_window=StrikeWindowPlan(min_strike=low, max_strike=high, source="configured"),
                planning_reason="test",
            )
            for side, low, high in windows
        ],
        merged_specs=[],
    ).to_debug_dict()


def save(root, name, value, *, pointer="", observed=ASOF):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    (root / name).write_bytes(raw)
    return {"path": name, "sha256": scope.quote._sha256(raw), "pointer": pointer, "observed_at_utc": observed}


@pytest.fixture
def inputs(tmp_path):
    data = {
        "schema_version": scope.INPUT_SCHEMA,
        "scope_id": "explicit-watchlist",
        "as_of_utc": ASOF,
        "fx_pairs": ["USDCNY"],
        "symbols": [
            {
                "underlier_code": "US.ACME",
                "owner_windows": [save(tmp_path, "window.json", bundle(("put", 80, 100), ("call", 100, 120)))],
                "chain": save(tmp_path, "chain.json", [row(90)]),
            }
        ],
        "previous_tracking": None,
    }
    return tmp_path, data


def run(inputs, *, output=None):
    root, data = inputs
    raw = series._bytes(data)
    path = root / "input.json"
    path.write_bytes(raw)
    return scope.build_watchlist_scope(
        input_file=path, expected_input_sha256=scope.quote._sha256(raw), output_dir=output
    )


def chain(inputs, rows):
    root, data = inputs
    data["symbols"][0]["chain"] = save(root, "chain.json", rows)


def codes(out, result):
    return [
        contract["option_code"]
        for batch in result["batches"]
        for contract in json.loads((out / batch["path"]).read_bytes())["contracts"]
    ]


def test_five_percent_endpoints_all_listed_and_no_strategy_filters(inputs):
    root, data = inputs
    rows = [row(x) for x in (75.999, 76, 90, 105, 105.001)]
    rows += [row(x, side="call") for x in (94.999, 95, 110, 126, 126.001)]
    for value in rows:
        value.update(delta=None, spread_ratio=99, annualized_return=-1, accepted=False)
    chain(inputs, rows)
    out = root / "out"
    result = run(inputs, output=out)
    assert set(codes(out, result)) == {x["code"] for x in rows if x["strike_price"] in {76, 90, 105, 95, 110, 126}}
    assert {(w["min_strike"], w["max_strike"]) for w in result["windows"]} == {(76, 105), (95, 126)}
    assert result["counts"]["US"]["contracts"] == 6
    assert result["status"] == "incomplete"
    assert result["missing_reasons"] == ["US.ACME:chain_coverage_unverified"]


def test_multiple_window_union_does_not_fill_gaps(inputs):
    root, data = inputs
    data["symbols"][0]["owner_windows"] = [save(root, "window.json", bundle(("put", 80, 100), ("put", 200, 220)))]
    chain(inputs, [row(x) for x in (90, 150, 200)])
    out = root / "out"
    result = run(inputs, output=out)
    assert set(codes(out, result)) == {row(90)["code"], row(200)["code"]}
    assert "US.ACME:call:owner_window_missing" in result["missing_reasons"]


@pytest.mark.parametrize("days,expected", [(-1, 0), (0, 1), (90, 1), (91, 0)])
def test_research_dte_independent_of_strategy_dte(inputs, days, expected):
    expiry = (date(2026, 10, 30) + timedelta(days=days)).isoformat()
    chain(inputs, [row(90, expiry=expiry)])
    assert run(inputs)["counts"]["US"]["contracts"] == expected


def test_dte_uses_market_date_not_utc_date(inputs):
    root, data = inputs
    data["as_of_utc"] = "2026-10-31T00:30:00Z"
    chain(inputs, [row(90, expiry="2026-10-30")])
    assert run(inputs)["counts"]["US"]["contracts"] == 1


def previous(inputs, records):
    root, data = inputs
    data["previous_tracking"] = save(
        root,
        "previous.json",
        {
            "schema_version": scope.TRACKING_SCHEMA,
            "as_of_utc": ASOF,
            "contracts": records,
        },
    )


def tracked(code, *, owner="US.OTHER", cutoff=None):
    return {
        "option_code": code,
        "underlier_code": owner,
        "first_seen_at_utc": "2026-10-29T15:00:00Z",
        "last_trade": cutoff,
    }


def test_keep_contracts_outside_band_removed_watchlist_and_expired_without_cutoff(inputs):
    record = tracked(row(10, owner="US.OTHER", expiry="2026-10-29")["code"])
    previous(inputs, [record])
    out = inputs[0] / "out"
    result = run(inputs, output=out)
    state = json.loads((out / "tracking.json").read_bytes())
    assert record in state["contracts"]
    assert result["counts"]["US"]["contracts"] == 2
    assert result["retired_codes"] == []


def test_retire_only_authoritative_cutoff_and_keep_first_seen(inputs):
    prior_code = row(90)["code"]
    ended = row(10, owner="US.OTHER")["code"]
    cutoff = {"cutoff_ms": 1793372400000, "source": "broker_contract_metadata", "observed_at_utc": ASOF}
    previous(inputs, [tracked(prior_code, owner="US.ACME"), tracked(ended, cutoff=cutoff)])
    out = inputs[0] / "out"
    result = run(inputs, output=out)
    assert result["retired_codes"] == [ended]
    assert result["new_contracts"] == 0
    assert (
        json.loads((out / "tracking.json").read_bytes())["contracts"][0]["first_seen_at_utc"] == "2026-10-29T15:00:00Z"
    )


@pytest.mark.parametrize(
    "fault", ["duplicate", "future_first_seen", "future_source", "invalid_source", "boolean_cutoff", "wrong_owner"]
)
def test_invalid_tracking_rejected(inputs, fault):
    record = tracked(row(90)["code"], owner="US.ACME")
    if fault == "future_first_seen":
        record["first_seen_at_utc"] = "2026-10-31T15:00:00Z"
    if fault in {"future_source", "invalid_source", "boolean_cutoff"}:
        record["last_trade"] = {
            "cutoff_ms": 1793372400000,
            "source": "broker_contract_metadata",
            "observed_at_utc": ASOF,
        }
        if fault == "future_source":
            record["last_trade"]["observed_at_utc"] = "2026-10-31T15:00:00Z"
        if fault == "invalid_source":
            record["last_trade"]["source"] = "expiry_guess"
        if fault == "boolean_cutoff":
            record["last_trade"]["cutoff_ms"] = True
    if fault == "wrong_owner":
        record["underlier_code"] = "US.OTHER"
    previous(inputs, [record, record] if fault == "duplicate" else [record])
    with pytest.raises(ValueError):
        run(inputs)


@pytest.mark.parametrize("fault", ["symbol", "single_side", "missing", "inverted", "bool", "null"])
def test_window_identity_and_bounds_must_be_explicit(inputs, fault):
    root, data = inputs
    value = bundle(("put", 80, 100))
    if fault == "symbol":
        value["symbol"] = "OTHER"
    if fault == "single_side":
        value = value["side_plans"][0]
    if fault == "missing":
        del value["side_plans"][0]["strike_window"]["min_strike"]
    if fault == "inverted":
        value["side_plans"][0]["strike_window"]["min_strike"] = 110
    if fault in {"bool", "null"}:
        value["side_plans"][0]["strike_window"]["min_strike"] = True if fault == "bool" else None
    data["symbols"][0]["owner_windows"] = [save(root, "window.json", value)]
    with pytest.raises(ValueError):
        run(inputs)


@pytest.mark.parametrize("fault", ["owner", "root", "strike", "side", "expiry", "duplicate", "missing"])
def test_chain_inconsistency_rejected(inputs, fault):
    value = row(90)
    if fault == "owner":
        value["stock_owner"] = "US.OTHER"
    if fault == "root":
        value["code"] = row(90, owner="US.OTHER")["code"]
    if fault == "strike":
        value["strike_price"] = 100
    if fault == "side":
        value["option_type"] = "CALL"
    if fault == "expiry":
        value["strike_time"] = "2026-12-20"
    if fault == "missing":
        del value["strike_time"]
    chain(inputs, [value, value] if fault == "duplicate" else [value])
    with pytest.raises(ValueError):
        run(inputs)


def test_over_200_contracts_sharded_not_truncated(inputs):
    chain(inputs, [row(80 + i / 100) for i in range(201)])
    out = inputs[0] / "out"
    result = run(inputs, output=out)
    assert [x["contracts"] for x in result["batches"]] == [200, 1]
    assert len(set(codes(out, result))) == 201
    assert result["counts"]["US"]["option_snapshot_batches"] == 2


def test_original_bytes_manifest_private_permissions_and_no_overwrite(inputs):
    root, data = inputs
    out = root / "out"
    result = run(inputs, output=out)
    for record in result["files"]:
        path = out / record["path"]
        raw = path.read_bytes()
        assert record["size"] == len(raw)
        assert record["sha256"] == scope.quote._sha256(raw)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (out / "sources" / (data["symbols"][0]["owner_windows"][0]["sha256"] + ".json")).read_bytes() == (
        root / "window.json"
    ).read_bytes()
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in [out, *[p for p in out.rglob("*") if p.is_dir()]])
    before = (out / "watchlist-scope.json").read_bytes()
    with pytest.raises(FileExistsError):
        run(inputs, output=out)
    assert (out / "watchlist-scope.json").read_bytes() == before


@pytest.mark.parametrize("fault", ["sha", "future", "pointer", "escape", "symlink", "traversal"])
def test_source_reference_boundary(inputs, fault):
    root, data = inputs
    ref = data["symbols"][0]["owner_windows"][0]
    if fault == "sha":
        ref["sha256"] = "0" * 64
    if fault == "future":
        ref["observed_at_utc"] = "2026-10-31T00:00:00Z"
    if fault == "pointer":
        ref["pointer"] = "/missing"
    if fault == "escape":
        ref["pointer"] = "/bad~2escape"
    if fault == "symlink":
        (root / "linked.json").symlink_to(root / "window.json")
        ref["path"] = "linked.json"
    if fault == "traversal":
        ref["path"] = "../window.json"
    with pytest.raises(ValueError):
        run(inputs)


def test_preview_cli_no_network_no_writes_and_deterministic(inputs, monkeypatch):
    root, _ = inputs
    first = run(inputs)
    path = root / "input.json"
    digest = scope.quote._sha256(path.read_bytes())
    before = {str(p): p.read_bytes() for p in root.iterdir() if p.is_file()}
    monkeypatch.setattr(series, "build_futu_gateway", lambda **_: pytest.fail("preview contacted provider"))
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(
        [
            "research",
            "watchlist-scope",
            "--input",
            str(path),
            "--expected-input-sha256",
            digest,
            "--output-dir",
            str(root / "must-not-exist"),
        ]
    )
    result = handle_research_command(args, repo_base_fn=lambda: pytest.fail("read live config"))
    assert result["ok"] is True
    assert result["data"] == first
    assert {str(p): p.read_bytes() for p in root.iterdir() if p.is_file()} == before
    assert not (root / "must-not-exist").exists()
    args.confirm = True
    args.output_dir = None
    with pytest.raises(ValueError, match="output-dir"):
        handle_research_command(args)


def test_external_input_anchor_required(inputs):
    run(inputs)
    with pytest.raises(ValueError, match="external input SHA"):
        scope.build_watchlist_scope(input_file=inputs[0] / "input.json", expected_input_sha256="0" * 64)


def test_scope_to_real_fetcher_and_immutable_export(inputs, monkeypatch):
    from src.application import opend_market_snapshot_fetching as snapshots

    root, _ = inputs
    scope_dir = root / "scope"
    result = run(inputs, output=scope_dir)
    calendar = {
        "markets": {
            "US": {
                "start": "2026-10-30",
                "end": "2026-10-30",
                "source_observed_at_utc": ASOF,
                "receipt": {
                    "retcode": 0,
                    "coverage_complete": True,
                    "pagination_complete": True,
                    "page_count": 1,
                    "rows": [{"time": "2026-10-30", "trade_date_type": "WHOLE"}],
                },
            }
        }
    }
    save(root, "calendar.json", calendar)
    save(root, "policy.json", {"US": INSTRUMENT_POLICY_REGISTRY["US:standard_equity_option"]})
    plan_dir = root / "series"
    plan = series.build_series_plan(
        scope_file=scope_dir / result["batches"][0]["path"],
        calendar_file=root / "calendar.json",
        policy_file=root / "policy.json",
        start_date="2026-10-30",
        end_date="2026-10-30",
        output_dir=plan_dir,
        observation_only=True,
    )
    assert plan["observations"][0]["option_codes"] == [row(90)["code"]]
    assert plan["observations"][0]["expected_at_utc"] == "2026-10-30T19:55:00Z"
    safe = save(
        root,
        "safe.json",
        {
            "schema_version": "research.safe-window.v1",
            "series_plan_sha256": plan["series_plan_sha256"],
            "intervals": [{"start_utc": "2026-10-30T19:55:00Z", "end_utc": "2026-10-30T19:56:00Z"}],
        },
    )
    now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(snapshots, "datetime", Clock)
    calls = []

    class Gateway:
        def get_snapshot(self, codes):
            calls.append(list(codes))
            return pd.DataFrame(
                [
                    {
                        "code": code,
                        "bid_price": 1.0,
                        "ask_price": 1.1,
                        "bid_vol": 10,
                        "ask_vol": 11,
                        "last_price": 1.05,
                        "update_time": "2026-10-30 15:55:00",
                        "option_implied_volatility": 30,
                        "option_delta": -0.25,
                        "option_gamma": 0.01,
                        "option_theta": -0.02,
                        "option_vega": 0.1,
                    }
                    for code in codes
                ]
            )

        def close(self):
            pass

    arguments = dict(
        series_dir=plan_dir,
        expected_plan_sha256=plan["series_plan_sha256"],
        safe_window_file=root / "safe.json",
        expected_safe_window_sha256=safe["sha256"],
        archive_root=root / "archive",
        runtime_root=root / "runtime",
        prune_roots=[root / "prune"],
        host="fixture.invalid",
        port=11111,
        ancillary_dir=scope_dir,
        clock=lambda: now,
        gateway_factory=lambda **_: Gateway(),
    )
    assert series.collect_series(**arguments)["provider_called"] is False
    assert not (root / "archive").exists()
    collected = series.collect_series(**arguments, confirm=True)
    attempt = collected["observations"][0]
    assert attempt["quote_package_status"] == "complete"
    assert attempt["research_complete"] is False
    assert "contract_timing_unverified" in attempt["reasons"]
    assert calls == [[row(90)["code"]], ["US.ACME"]]
    assert series.collect_series(**arguments, confirm=True)["observations"][0]["status"] == "already_claimed"
    assert len(calls) == 2
    first = series.export_series(
        series_dir=root / "archive",
        expected_plan_sha256=plan["series_plan_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=root / "export1",
    )
    second = series.export_series(
        series_dir=root / "archive",
        expected_plan_sha256=plan["series_plan_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=root / "export2",
    )
    assert first == second
    assert (root / "export1/series-export.json").read_bytes() == (root / "export2/series-export.json").read_bytes()
    assert (root / "export1/inputs/ancillary/watchlist-scope.json").read_bytes() == (
        scope_dir / "watchlist-scope.json"
    ).read_bytes()
    assert first["observations"][0]["due"] is True
