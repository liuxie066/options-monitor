"""Exercise actual expiration/chain owners without a provider connection."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src.application.research import prescreen, watchlist_discovery as discovery, watchlist_scope as scope
from src.interfaces.cli.research import add_research_commands, handle_research_command
from tests.test_watchlist_research_scope import bundle, row
from test_watchlist_research_scope import inputs  # noqa: F401
from test_watchlist_research_series import planned  # noqa: F401


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = SimpleNamespace(
        now=datetime(2026, 10, 30, 15, tzinfo=timezone.utc),
        calls=[],
        closed=False,
        expiries=["2026-10-30", "2026-11-20", "2027-01-28", "2027-01-29"],
        fail=None,
    )
    monkeypatch.setattr(prescreen, "utc_now", lambda: "2026-10-30T14:00:00Z")
    source = tmp_path / "input"
    source.mkdir(mode=0o700)
    receipt = prescreen.publish_watchlist_plan(
        producer_root=source,
        producer_run_id="synthetic-discovery",
        plan={"symbols": [{"symbol": "ACME", "fetch_plan": bundle(("put", 80, 100), ("call", 100, 120))}]},
    )
    receipt_path = str((source / receipt["payload_relpath"]).parent.relative_to(source) / "receipt.json")
    payload = {
        "schema_version": scope.SCAN_INPUT_SCHEMA,
        "scope_id": "discovery-test",
        "as_of_utc": "2026-10-30T14:01:00Z",
        "fx_pairs": ["USDCNY"],
        "watchlist": {
            "path": receipt_path,
            "sha256": scope.quote._sha256((source / receipt_path).read_bytes()),
            "pointer": "",
            "observed_at_utc": receipt["completed_at"],
        },
        "captures": [],
        "previous_tracking": None,
    }
    raw = scope.series._bytes(payload)
    (source / "input.json").write_bytes(raw)
    digest = scope.quote._sha256(raw)
    safe = {
        "schema_version": "research.safe-window.v1",
        "series_plan_sha256": digest,
        "intervals": [{"start_utc": "2026-10-30T15:00:00Z", "end_utc": "2026-10-30T15:10:00Z"}],
    }
    budget = {"schema_version": discovery.BUDGET_SCHEMA, "max_calls": 20, "max_bytes": 20_000_000, "min_free_bytes": 1}
    for name, content in [("safe.json", safe), ("budget.json", budget)]:
        (source / name).write_bytes(scope.series._bytes(content))

    class Gateway:
        def get_option_expiration_dates(self, code):
            state.calls.append(("expiration", code))
            if state.fail == "expiration":
                raise TimeoutError("synthetic failure")
            return pd.DataFrame([{"strike_time": value, "provider_extra": "kept"} for value in state.expiries])

        def get_option_chain(self, **kwargs):
            state.calls.append(("chain", kwargs))
            assert set(kwargs) == {"code", "start", "end", "is_force_refresh"}
            assert kwargs["start"] == kwargs["end"] and kwargs["is_force_refresh"] is True
            if state.fail == kwargs["start"]:
                raise TimeoutError("synthetic failure")
            if state.fail == "empty":
                return pd.DataFrame()
            rows = [
                row(strike, side=side, expiry=kwargs["start"])
                for strike in (75, 76, 90, 105, 106, 120, 126, 127)
                for side in ("put", "call")
            ]
            return pd.DataFrame([{**entry, "provider_extra": "kept"} for entry in rows])

        def close(self):
            state.closed = True

    args = dict(
        input_file=source / "input.json",
        expected_input_sha256=digest,
        safe_window_file=source / "safe.json",
        expected_safe_window_sha256=scope.quote._sha256((source / "safe.json").read_bytes()),
        budget_file=source / "budget.json",
        expected_budget_sha256=scope.quote._sha256((source / "budget.json").read_bytes()),
        output_dir=tmp_path / "discovery",
        budget_roots=[tmp_path],
        runtime_root=tmp_path / "runtime",
        prune_roots=[tmp_path / "prune"],
        host="fixture.invalid",
        port=11111,
        clock=lambda: state.now,
        gateway_factory=lambda **_: Gateway(),
    )
    return args, state


def body(args, result):
    ref = result["underliers"]["US.ACME"]["receipt"]
    receipt = json.loads((args["output_dir"] / ref["path"]).read_bytes())
    return json.loads((args["output_dir"] / receipt["payload_relpath"]).read_bytes())


def test_preview_is_offline_and_writes_nothing(setup):
    args, state = setup
    before = sorted(args["input_file"].parent.parent.rglob("*"))
    assert discovery.discover_watchlist(**args)["status"] == "preview"
    assert sorted(args["input_file"].parent.parent.rglob("*")) == before
    assert state.calls == [] and not state.closed


def test_real_owners_enumerate_inclusive_zero_to_ninety_without_strategy_filters(setup):
    args, state = setup
    originals = {p: p.read_bytes() for p in args["input_file"].parent.rglob("*.json")}
    result = discovery.discover_watchlist(**args, confirm=True)
    data = body(args, result)
    assert result["status"] == "complete" and result["provider_call_count"] == 4
    assert data["requested_expirations"] == state.expiries[:3]
    assert len(data["chain"]["rows"]) == 48
    assert all(call["status"] == "returned" for call in data["provider_calls"])
    assert all(row["provider_extra"] == "kept" for call in data["provider_calls"] for row in call["rows"])
    assert state.closed
    out = args["output_dir"]
    scoped = scope.build_watchlist_scope(
        input_file=out / "scope-input.json",
        expected_input_sha256=result["scope_input_sha256"],
        output_dir=out.parent / "scope",
    )
    assert scoped["counts"]["US"]["contracts"] == 21
    assert scoped["redundancy"] == "0.05"
    assert "US.ACME:chain_coverage_unverified" in scoped["missing_reasons"]
    for path, raw in originals.items():
        assert path.read_bytes() == raw
    for file in out.rglob("*"):
        if "limiter" in file.relative_to(out).parts:
            continue  # Local limiter state is not exported evidence; its root is private.
        assert file.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        discovery.discover_watchlist(**args, confirm=True)
    assert len(state.calls) == 4


@pytest.mark.parametrize("expiries", [[], ["2027-01-29"], ["2026-10-29"]])
def test_no_expiry_never_triggers_unbounded_chain_call(setup, expiries):
    args, state = setup
    state.expiries = expiries
    result = discovery.discover_watchlist(**args, confirm=True)
    assert result["status"] == "complete" and result["provider_call_count"] == 1
    assert body(args, result)["requested_expirations"] == []
    assert state.calls == [("expiration", "US.ACME")]


@pytest.mark.parametrize("failure", ["expiration", "2026-11-20", "empty"])
def test_failures_and_successful_empty_have_different_receipts(setup, failure):
    args, state = setup
    state.fail = failure
    result = discovery.discover_watchlist(**args, confirm=True)
    assert result["status"] == ("complete" if failure == "empty" else "incomplete")
    data = body(args, result)
    assert data["provider_calls"]
    assert state.closed
    if failure == "empty":
        assert set(data["chain"]["meta"]["expiration_statuses"].values()) == {"empty"}
    else:
        assert any(
            call["status"] == "failed" and call["error_type"] == "TimeoutError" for call in data["provider_calls"]
        )


def test_budget_denial_cannot_drop_discovered_expiry_denominator(setup):
    args, state = setup
    budget = json.loads(args["budget_file"].read_bytes())
    budget["max_calls"] = 2
    args["budget_file"].write_bytes(scope.series._bytes(budget))
    args["expected_budget_sha256"] = scope.quote._sha256(args["budget_file"].read_bytes())
    result = discovery.discover_watchlist(**args, confirm=True)
    data = body(args, result)
    assert result["status"] == "incomplete" and len(state.calls) == 2
    assert data["requested_expirations"] == state.expiries[:3]
    assert data["chain"]["meta"]["expiration_statuses"] == {
        "2026-10-30": "fetched",
        "2026-11-20": "error",
        "2027-01-28": "error",
    }


@pytest.mark.parametrize("field", ["expected_input_sha256", "expected_safe_window_sha256", "expected_budget_sha256"])
def test_external_anchor_changes_fail_before_connect(setup, field):
    args, state = setup
    args[field] = "0" * 64
    with pytest.raises(ValueError, match="SHA"):
        discovery.discover_watchlist(**args, confirm=True)
    assert not state.calls and not args["output_dir"].exists()


def test_closed_window_and_production_overlap_fail_before_connect(setup):
    args, state = setup
    state.now += timedelta(hours=1)
    with pytest.raises(ValueError, match="safe window"):
        discovery.discover_watchlist(**args, confirm=True)
    state.now -= timedelta(hours=1)
    args["prune_roots"] = [args["output_dir"].parent]
    with pytest.raises(ValueError, match="overlap"):
        discovery.discover_watchlist(**args, confirm=True)
    assert not state.calls


@pytest.mark.parametrize("wait_on", [1, 2])
def test_wait_cannot_move_a_physical_request_past_safe_window(setup, monkeypatch, wait_on):
    args, state = setup
    from src.application import option_chain_fetching

    acquire = option_chain_fetching.FileRateLimiter.acquire

    acquisitions = 0

    def wait(self, **kw):
        nonlocal acquisitions
        acquisitions += 1
        if acquisitions == wait_on:
            state.now += timedelta(minutes=11)
        return acquire(self, **kw)

    monkeypatch.setattr(option_chain_fetching.FileRateLimiter, "acquire", wait)
    result = discovery.discover_watchlist(**args, confirm=True)
    assert result["status"] == "incomplete"
    assert state.calls == ([] if wait_on == 1 else [("expiration", "US.ACME")])


def test_expiration_parse_failure_keeps_original_bad_rows(setup):
    args, state = setup
    state.expiries = ["not-a-date"]
    result = discovery.discover_watchlist(**args, confirm=True)
    assert result["status"] == "incomplete"
    data = body(args, result)
    assert data["provider_calls"][0]["rows"][0]["strike_time"] == "not-a-date"
    assert len(state.calls) == 1


def test_storage_check_elapsed_time_is_not_a_physical_request(setup, monkeypatch):
    args, state = setup
    storage = discovery._storage

    def delayed_storage(*a, **kw):
        result = storage(*a, **kw)
        if (args["output_dir"] / "calls/000000/request.json").exists():
            state.now = datetime(2026, 10, 30, 15, 11, tzinfo=timezone.utc)
        return result

    monkeypatch.setattr(discovery, "_storage", delayed_storage)
    result = discovery.discover_watchlist(**args, confirm=True)
    assert result["status"] == "incomplete" and not state.calls
    assert result["request_reservations"] == 1 and result["provider_call_count"] == 0
    assert result["provider_called"] is False
    call = body(args, result)["provider_calls"][0]
    assert call["status"] == "reserved" and call["requested_at_utc"] is None and call["received_at_utc"] is None


def test_cli_default_previews_and_explicit_confirm_forwards_all_anchors(setup, monkeypatch):
    args, _ = setup
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command"))
    flags = ["research", "watchlist-discover"]
    for key, value in args.items():
        if key in {"clock", "gateway_factory"}:
            continue
        name = {
            "input_file": "input",
            "safe_window_file": "safe-window",
            "budget_file": "budget",
            "budget_roots": "budget-root",
            "prune_roots": "prune-root",
            "host": "opend-host",
            "port": "opend-port",
        }.get(key, key.replace("_", "-"))
        for item in value if isinstance(value, list) else [value]:
            flags += ["--" + name, str(item)]
    called = []
    monkeypatch.setattr(discovery, "discover_watchlist", lambda **kw: called.append(kw) or {"status": "preview"})
    for confirm in (False, True):
        response = handle_research_command(
            parser.parse_args(flags + (["--confirm"] if confirm else [])),
            repo_base_fn=lambda: pytest.fail("must not load production context"),
        )
        assert response["ok"] is True
        assert called[-1] == {
            **{k: v for k, v in args.items() if k not in {"clock", "gateway_factory"}},
            "confirm": confirm,
        }


def test_discovery_source_tampering_and_run_mismatch_are_rejected(setup):
    args, _ = setup
    result = discovery.discover_watchlist(**args, confirm=True)
    out = args["output_dir"]
    data = json.loads((out / "scope-input.json").read_bytes())
    changed = deepcopy(data)
    changed["discoveries"].append(changed["discoveries"][0])
    path = out / "bad.json"
    path.write_bytes(scope.series._bytes(changed))
    with pytest.raises(ValueError, match="duplicate"):
        scope.build_watchlist_scope(input_file=path, expected_input_sha256=scope.quote._sha256(path.read_bytes()))
    ref = data["discoveries"][0]
    (out / ref["path"]).write_bytes(b"{}")
    with pytest.raises(ValueError, match="SHA"):
        scope.build_watchlist_scope(
            input_file=out / "scope-input.json", expected_input_sha256=result["scope_input_sha256"]
        )


def test_discovery_scope_real_collect_and_export(setup, planned):
    from src.application.research import watchlist_series as group

    args, _ = setup
    result = discovery.discover_watchlist(**args, confirm=True)
    root, build_args, _, collect_args, _ = planned
    out = args["output_dir"]
    scoped = root / "discovery-scope"
    scope.build_watchlist_scope(
        input_file=out / "scope-input.json", expected_input_sha256=result["scope_input_sha256"], output_dir=scoped
    )
    plan = group.build_watchlist_series(
        **{
            **build_args,
            "scope_dir": scoped,
            "expected_scope_sha256": scope.quote._sha256((scoped / "watchlist-scope.json").read_bytes()),
            "output_dir": root / "discovery-series",
        }
    )
    safe_raw = scope.series._bytes(
        {
            "schema_version": "research.watchlist-safe-window.v1",
            "watchlist_series_sha256": plan["watchlist_series_sha256"],
            "intervals": [{"start_utc": "2026-10-30T19:55:00Z", "end_utc": "2026-10-30T19:56:00Z"}],
        }
    )
    safe_path = root / "discovery-safe.json"
    safe_path.write_bytes(safe_raw)
    params = {
        **collect_args,
        "plan_dir": root / "discovery-series",
        "expected_plan_sha256": plan["watchlist_series_sha256"],
        "safe_window_file": safe_path,
        "expected_safe_window_sha256": scope.quote._sha256(safe_raw),
    }
    group.collect_watchlist_series(**params, confirm=True)
    exported = group.export_watchlist_series(
        series_dir=params["archive_root"],
        expected_plan_sha256=plan["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=root / "discovery-export",
    )
    assert len(plan["slots"][0]["option_codes"]) == 21
    assert exported["schema_version"] == group.EXPORT_SCHEMA
    manifest = json.loads((root / "discovery-export/watchlist-export.json").read_bytes())
    assert any("research_discovery/" in item["path"] for item in manifest["files"])
    anchors = {
        "plan": plan["watchlist_series_sha256"],
        "export": scope.quote._sha256((root / "discovery-export/watchlist-export.json").read_bytes()),
        "producer": manifest["producer_implementation_sha256"],
    }
    (root / "discovery-anchors.json").write_bytes(scope.series._bytes(anchors))
