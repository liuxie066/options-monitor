from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from domain.domain.option_lifecycle import INSTRUMENT_POLICY_REGISTRY
from src.application import opend_market_snapshot_fetching as snapshots
from src.application.research import observation_series as series
from src.application.research import watchlist_series as group
from src.interfaces.cli.research import add_research_commands, handle_research_command
from test_watchlist_research_scope import inputs, row, chain, run, save  # noqa: F401


@pytest.fixture
def planned(inputs, monkeypatch):
    root, _ = inputs
    chain(inputs, [row(80 + x / 1000) for x in range(201)])
    run(inputs, output=root / "scope")
    save(
        root,
        "calendar.json",
        {
            "markets": {
                "US": {
                    "start": "2026-10-30",
                    "end": "2026-10-30",
                    "source_observed_at_utc": "2026-10-30T15:00:00Z",
                    "receipt": {
                        "retcode": 0,
                        "coverage_complete": True,
                        "pagination_complete": True,
                        "page_count": 1,
                        "rows": [{"time": "2026-10-30", "trade_date_type": "WHOLE"}],
                    },
                }
            }
        },
    )
    save(root, "policy.json", {"US": INSTRUMENT_POLICY_REGISTRY["US:standard_equity_option"]})
    save(
        root,
        "budget.json",
        {"schema_version": group.BUDGET_SCHEMA, "max_calls_per_slot": 6, "max_bytes": 100_000_000, "min_free_bytes": 1},
    )
    build = dict(
        scope_dir=root / "scope",
        expected_scope_sha256=group.quote._sha256((root / "scope/watchlist-scope.json").read_bytes()),
        calendar_file=root / "calendar.json",
        policy_file=root / "policy.json",
        budget_file=root / "budget.json",
        start_date="2026-10-30",
        end_date="2026-10-30",
        output_dir=root / "plan",
    )
    plan = group.build_watchlist_series(**build)
    safe = save(
        root,
        "safe.json",
        {
            "schema_version": "research.watchlist-safe-window.v1",
            "watchlist_series_sha256": plan["watchlist_series_sha256"],
            "intervals": [{"start_utc": "2026-10-30T19:55:00Z", "end_utc": "2026-10-30T19:56:00Z"}],
        },
    )
    state = SimpleNamespace(now=datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc), calls=[], fail=False)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now if tz else state.now.replace(tzinfo=None)

    monkeypatch.setattr(snapshots, "datetime", Clock)

    class Gateway:
        def get_snapshot(self, codes):
            state.calls.append(list(codes))
            if state.fail:
                raise RuntimeError("provider failure")
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

    collect = dict(
        plan_dir=root / "plan",
        expected_plan_sha256=plan["watchlist_series_sha256"],
        safe_window_file=root / "safe.json",
        expected_safe_window_sha256=safe["sha256"],
        archive_root=root / "archive",
        budget_roots=[root],
        runtime_root=root / "runtime",
        prune_roots=[root / "prune"],
        host="fixture.invalid",
        port=11111,
        clock=lambda: state.now,
        gateway_factory=lambda **_: Gateway(),
    )
    return root, build, plan, collect, state


def export(planned, name="export", cutoff="2026-10-30T19:56:00Z"):
    root, _, plan, _, _ = planned
    return group.export_watchlist_series(
        series_dir=root / "archive",
        expected_plan_sha256=plan["watchlist_series_sha256"],
        cutoff=cutoff,
        output_dir=root / name,
    )


def ancillary(planned):
    root, build, _, args, state = planned
    evidence = root / "evidence"
    evidence.mkdir()
    snapshot = {
        "schema_version": 2,
        "pairs": {
            "USDCNY": {
                "rate": 7.1,
                "source": "tencent_quote",
                "quote_at_utc": "2026-10-30T14:00:00Z",
                "observed_at_utc": "2026-10-30T14:00:01Z",
            }
        },
    }
    from domain.domain.decision_state_fingerprint import canonical_sha256

    originals = {
        "fx.json": {
            "schema_version": 1,
            "run_id": "research-ancillary-fixture",
            "snapshot": snapshot,
            "snapshot_sha256": canonical_sha256(snapshot),
        },
        "actions.json": {
            "kind": "corporate_actions",
            "request": {"code": "US.ACME", "start": "2026-10-30", "end": "2026-10-30"},
            "started_at_utc": "2026-10-30T14:00:00Z",
            "received_at_utc": "2026-10-30T14:00:01Z",
            "status": "received_unverified_coverage",
            "responses": [{"rows": []}],
        },
    }
    for name, value in originals.items():
        save(evidence, name, value)
        (evidence / name).chmod(0o600)
    manifest = {
        "schema_version": "research.market-ancillary.v1",
        "files": [series._record(evidence, evidence / name) for name in sorted(originals)],
    }
    anchor = save(evidence, "manifest.json", manifest)
    build = {
        **build,
        "output_dir": root / "plan-with-ancillary",
        "ancillary_file": evidence / "manifest.json",
        "expected_ancillary_sha256": anchor["sha256"],
    }
    return root, build, args, state, originals


def test_explicit_ancillary_originals_reach_all_shards_and_export(planned):
    root, build, args, _, originals = ancillary(planned)
    plan = group.build_watchlist_series(**build)
    safe = json.loads((root / "safe.json").read_bytes())
    safe["watchlist_series_sha256"] = plan["watchlist_series_sha256"]
    anchor = save(root, "safe-with-ancillary.json", safe)
    args.update(
        plan_dir=build["output_dir"],
        expected_plan_sha256=plan["watchlist_series_sha256"],
        safe_window_file=root / "safe-with-ancillary.json",
        expected_safe_window_sha256=anchor["sha256"],
    )
    result = group.collect_watchlist_series(**args, confirm=True)
    for batch in result["batches"]:
        attempt = batch["result"]["observations"][0]
        assert attempt["dimensions"]["fx"]["facts"]
        assert "fx_pair_missing:USDCNY" not in attempt["dimensions"]["fx"]["missing_reasons"]
        assert attempt["dimensions"]["corporate_actions"]["facts"]
        assert attempt["dimensions"]["corporate_actions"]["status"] == "incomplete"
        assert (
            "corporate_actions_coverage_unverified:US.ACME"
            in attempt["dimensions"]["corporate_actions"]["missing_reasons"]
        )
    group.export_watchlist_series(
        series_dir=root / "archive",
        expected_plan_sha256=plan["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=root / "export-ancillary",
    )
    for name in originals:
        original = (root / "evidence" / name).read_bytes()
        for batch in plan["batches"]:
            target = root / "export-ancillary/exports" / batch["batch_id"] / "inputs/ancillary/supplemental" / name
            assert target.read_bytes() == original


@pytest.mark.parametrize("failure", ["hash", "size", "missing", "secret", "link", "duplicate", "reserved"])
def test_ancillary_rejected_before_plan_or_provider(planned, failure):
    root, build, _, state, _ = ancillary(planned)
    evidence = root / "evidence"
    manifest = json.loads((evidence / "manifest.json").read_bytes())
    if failure == "hash":
        build["expected_ancillary_sha256"] = "0" * 64
    elif failure == "size":
        manifest["files"][0]["size"] += 1
    elif failure == "missing":
        (evidence / manifest["files"][0]["path"]).unlink()
    elif failure == "secret":
        save(evidence, "actions.json", {"accounts": ["private"]})
        manifest["files"] = [series._record(evidence, evidence / "actions.json")]
    elif failure == "link":
        (evidence / "actions.json").unlink()
        (evidence / "actions.json").symlink_to(evidence / "fx.json")
    elif failure == "duplicate":
        manifest["files"].append(manifest["files"][0])
    else:
        manifest["files"][0]["path"] = "manifest.json"
    if failure != "hash":
        build["expected_ancillary_sha256"] = save(evidence, "manifest.json", manifest)["sha256"]
    with pytest.raises((ValueError, FileNotFoundError)):
        group.build_watchlist_series(**build)
    assert not build["output_dir"].exists() and not state.calls


@pytest.mark.parametrize("missing", ["ancillary_file", "expected_ancillary_sha256"])
def test_ancillary_requires_both_external_anchors(planned, missing):
    _, build, _, state, _ = ancillary(planned)
    build.pop(missing)
    with pytest.raises(ValueError, match="required together"):
        group.build_watchlist_series(**build)
    assert not build["output_dir"].exists() and not state.calls


def test_ancillary_plan_cli_transfers_explicit_sources_without_network(planned):
    _, build, _, state, _ = ancillary(planned)
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    cli = ["research", "watchlist-series-plan"]
    names = {
        "calendar_file": "calendar",
        "policy_file": "policy",
        "budget_file": "budget",
        "start_date": "start",
        "end_date": "end",
        "ancillary_file": "ancillary",
    }
    for key, value in build.items():
        cli.extend(["--" + names.get(key, key.replace("_", "-")), str(value)])
    response = handle_research_command(parser.parse_args(cli))
    assert response["ok"] and not state.calls
    assert (build["output_dir"] / "scope/supplemental/fx.json").is_file()


def test_all_shards_one_budget_real_fetcher_and_export(planned):
    root, _, plan, args, state = planned
    assert len(plan["batches"]) == 2
    assert len(plan["slots"]) == 1
    assert len(plan["slots"][0]["option_codes"]) == 201
    assert plan["slots"][0]["normal_calls"] == 4
    assert group.collect_watchlist_series(**args)["provider_called"] is False
    assert not (root / "archive").exists()
    result = group.collect_watchlist_series(**args, confirm=True)
    assert len(result["batches"]) == 2
    assert [len(codes) for codes in state.calls] == [200, 1, 1, 1]
    assert len(list((root / "archive/requests").glob("*/*.json"))) == 4
    assert (root / "archive/limiter/output_shared/state/opend_market_snapshot_limiter.json").exists()
    group.collect_watchlist_series(**args, confirm=True)
    assert len(state.calls) == 4
    first, second = export(planned), export(planned, "export2")
    assert first == second
    assert len(first["batches"]) == 2
    assert (root / "export/watchlist-export.json").read_bytes() == (root / "export2/watchlist-export.json").read_bytes()
    for batch in first["batches"]:
        single = json.loads((root / "export/exports" / batch["batch_id"] / "series-export.json").read_bytes())
        assert single["observations"][0]["due"] is True
        assert single["observations"][0]["attempts"][0]["quote_package_status"] == "complete"
    with pytest.raises(FileExistsError):
        export(planned)


def test_late_never_requests_and_keeps_full_roster(planned):
    root, _, plan, args, state = planned
    state.now = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
    group.collect_watchlist_series(**args, confirm=True)
    result = export(planned, cutoff="2026-10-30T20:01:00Z")
    assert not state.calls
    assert len(result["batches"]) == 2
    for batch in result["batches"]:
        single = series._read(root / "export/exports" / batch["batch_id"] / "series-export.json")[1]
        assert single["observations"][0]["status"] == "missed"


def test_shared_budget_survives_retry_and_reinvocation(planned):
    root, _, _, args, state = planned
    # Four previously reserved requests leave two; interrupted reservations are not refunded.
    slot = group._load(root / "plan", args["expected_plan_sha256"])[1]["slots"][0]
    key = group.quote._sha256(series._bytes({"market": slot["market"], "at": slot["expected_at_utc"]}))
    reserve = series._private_dir(root / "archive/requests" / key)
    for i in range(4):
        series._put(reserve / f"{i}.json", b"{}")
    group.collect_watchlist_series(**args, confirm=True)
    assert len(state.calls) == 2
    assert len(list(reserve.glob("*.json"))) == 6
    result = export(planned)
    assert len(result["batches"]) == 2
    group.collect_watchlist_series(**args, confirm=True)
    assert len(state.calls) == 2


def test_deadline_rechecked_between_provider_calls(planned):
    _, _, _, args, state = planned
    factory = args["gateway_factory"]

    def gateway(**kwargs):
        inner = factory(**kwargs)
        call = inner.get_snapshot

        def late(codes):
            out = call(codes)
            state.now = datetime(2026, 10, 30, 19, 56, tzinfo=timezone.utc)
            return out

        inner.get_snapshot = late
        return inner

    args["gateway_factory"] = gateway
    group.collect_watchlist_series(**args, confirm=True)
    assert len(state.calls) == 1
    assert len(export(planned)["batches"]) == 2


@pytest.mark.parametrize("fault", ["calls", "bytes", "free", "scope_time"])
def test_invalid_budget_or_past_scope_rejected_before_publish(planned, fault):
    root, build, _, _, _ = planned
    budget = series._read(root / "budget.json")[1]
    if fault == "scope_time":
        build["start_date"] = "2026-10-29"
    else:
        budget[{"calls": "max_calls_per_slot", "bytes": "max_bytes", "free": "min_free_bytes"}[fault]] = 0
        (root / "budget.json").write_bytes(series._bytes(budget))
    build["output_dir"] = root / "bad-plan"
    with pytest.raises(ValueError):
        group.build_watchlist_series(**build)
    assert not build["output_dir"].exists()


def test_budget_too_small_does_not_truncate(planned):
    root, build, _, _, _ = planned
    budget = series._read(root / "budget.json")[1]
    budget["max_calls_per_slot"] = 3
    (root / "budget.json").write_bytes(series._bytes(budget))
    build["output_dir"] = root / "small"
    with pytest.raises(ValueError, match="no truncation"):
        group.build_watchlist_series(**build)
    assert not (root / "small").exists()


@pytest.mark.parametrize("fault", ["plan", "safe", "symlink", "overlap", "prune", "budget_roots"])
def test_reject_invalid_inputs_before_network(planned, fault):
    root, _, _, args, state = planned
    if fault == "plan":
        args["expected_plan_sha256"] = "0" * 64
    if fault == "safe":
        args["expected_safe_window_sha256"] = "0" * 64
    if fault == "symlink":
        (root / "alias").symlink_to(root / "plan", target_is_directory=True)
        args["plan_dir"] = root / "alias"
    if fault == "overlap":
        args["archive_root"] = root / "runtime/archive"
    if fault == "prune":
        args["prune_roots"] = []
    if fault == "budget_roots":
        args["budget_roots"] = [root / "elsewhere"]
    with pytest.raises(ValueError):
        group.collect_watchlist_series(**args, confirm=True)
    assert not state.calls


def test_lock_competing_collector_does_not_request(planned):
    root, _, _, args, state = planned
    series._private_dir(root / "archive")
    with group._lock(root / "archive"):
        with pytest.raises(BlockingIOError):
            group.collect_watchlist_series(**args, confirm=True)
    assert not state.calls


def test_archive_freezes_safe_window_and_budget_roots(planned):
    root, _, _, args, state = planned
    group.collect_watchlist_series(**args, confirm=True)
    args["budget_roots"] = [root / "archive"]
    with pytest.raises(ValueError, match="immutable"):
        group.collect_watchlist_series(**args, confirm=True)
    assert len(state.calls) == 4


def test_storage_budget_prevents_calls(planned, monkeypatch):
    _, _, _, args, state = planned
    monkeypatch.setattr(group.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match="storage budget"):
        group.collect_watchlist_series(**args, confirm=True)
    assert not state.calls


def test_group_cli_preview_does_not_use_runtime_or_network(planned):
    root, _, plan, args, state = planned
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command"))
    parsed = parser.parse_args(
        [
            "research",
            "watchlist-series-collect",
            "--plan-dir",
            str(root / "plan"),
            "--expected-plan-sha256",
            plan["watchlist_series_sha256"],
            "--safe-window",
            str(root / "safe.json"),
            "--expected-safe-window-sha256",
            args["expected_safe_window_sha256"],
            "--archive-root",
            str(root / "archive"),
            "--budget-root",
            str(root),
            "--runtime-root",
            str(root / "runtime"),
            "--prune-root",
            str(root / "prune"),
            "--opend-host",
            "fixture.invalid",
            "--opend-port",
            "11111",
        ]
    )
    result = handle_research_command(parsed, repo_base_fn=lambda: pytest.fail("production repository accessed"))
    assert result["data"]["status"] == "preview"
    assert not state.calls
    assert not (root / "archive").exists()


@pytest.mark.parametrize("case", ["normal", "missing"])
def test_cross_project_fixture(planned, case):
    import os
    import hashlib

    root, _, plan, args, state = planned
    if case == "missing":
        state.now = datetime(2026, 10, 30, 15, 1, tzinfo=timezone.utc)
    group.collect_watchlist_series(**args, confirm=True)
    result = export(planned)
    destination = os.environ.get("WATCHLIST_FIXTURE_OUTPUT")
    if destination:
        import tarfile

        out = Path(destination)
        out.mkdir(mode=0o700, exist_ok=True)
        with tarfile.open(out / (case + ".tar.gz"), "w:gz") as archive:
            for path in sorted((root / "export").rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(root / "export").as_posix(), recursive=False)
        (out / (case + "-anchors.json")).write_bytes(
            series._bytes(
                {
                    "expected_plan_sha256": plan["watchlist_series_sha256"],
                    "expected_export_sha256": result["watchlist_export_sha256"],
                    "expected_implementation_sha256": result["producer_implementation_sha256"],
                    "fixture_sha256": hashlib.sha256((out / (case + ".tar.gz")).read_bytes()).hexdigest(),
                }
            )
        )


def test_slot_roster_cannot_be_shrunk(planned):
    root, _, _, args, state = planned
    path = root / "plan/watchlist-series.json"
    value = series._read(path)[1]
    value["slots"][0]["option_codes"].pop()
    raw = series._bytes(value)
    path.write_bytes(raw)
    args["expected_plan_sha256"] = group.quote._sha256(raw)
    with pytest.raises(ValueError, match="slot roster"):
        group.collect_watchlist_series(**args, confirm=True)
    assert not state.calls


def test_provider_failures_share_budget_and_never_hide_shards(planned):
    root, _, _, args, state = planned
    state.fail = True
    group.collect_watchlist_series(**args, confirm=True)
    assert 0 < len(state.calls) <= 6
    assert len(list((root / "archive/requests").glob("*/*.json"))) == len(state.calls)
    result = export(planned)
    assert len(result["batches"]) == 2
    for batch in result["batches"]:
        child = series._read(root / "export/exports" / batch["batch_id"] / "series-export.json")[1]
        assert child["observations"][0]["due"]
        assert child["observations"][0]["attempts"][0]["research_complete"] is False


def test_storage_rechecked_before_every_request(planned, monkeypatch):
    _, _, _, args, state = planned
    monkeypatch.setattr(group.shutil, "disk_usage", lambda _: SimpleNamespace(free=0 if state.calls else 10_000_000))
    group.collect_watchlist_series(**args, confirm=True)
    assert len(state.calls) == 1
    assert len(export(planned)["batches"]) == 2


def test_plan_and_export_cli_only_use_research_facade(planned):
    root, build, plan, args, _ = planned
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command"))
    command = ["research", "watchlist-series-plan"]
    for flag, value in [
        ("scope-dir", build["scope_dir"]),
        ("expected-scope-sha256", build["expected_scope_sha256"]),
        ("calendar", build["calendar_file"]),
        ("policy", build["policy_file"]),
        ("budget", build["budget_file"]),
        ("start", build["start_date"]),
        ("end", build["end_date"]),
        ("output-dir", root / "cli-plan"),
    ]:
        command.extend(["--" + flag, str(value)])
    no_production = lambda: pytest.fail("production environment accessed")
    response = handle_research_command(parser.parse_args(command), repo_base_fn=no_production)
    assert response["data"]["watchlist_series_sha256"] == plan["watchlist_series_sha256"]
    group.collect_watchlist_series(**args, confirm=True)
    command = [
        "research",
        "archive",
        "watchlist-export",
        "--series-dir",
        str(root / "archive"),
        "--expected-plan-sha256",
        plan["watchlist_series_sha256"],
        "--cutoff",
        "2026-10-30T19:56:00Z",
        "--output-dir",
        str(root / "cli-export"),
    ]
    response = handle_research_command(parser.parse_args(command), repo_base_fn=no_production)
    assert len(response["data"]["batches"]) == 2


def test_storage_counts_virtualenv_links_without_following_targets(tmp_path):
    root = tmp_path / "research"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "large").write_bytes(b"x" * 1_000_000)
    (root / "venv").mkdir()
    (root / "venv/lib64").symlink_to(external, target_is_directory=True)
    (root / "venv/python").symlink_to("/usr/bin/python3.12")
    budget = {"max_bytes": 100_000, "min_free_bytes": 1}
    result = group._storage(root, [root], budget)
    assert 0 < result["used_bytes"] < 100_000
    assert result["logical_bytes"] == sum(p.lstat().st_size for p in (root / "venv").iterdir())


def test_storage_does_not_silently_skip_unreadable_budget_directory(tmp_path, monkeypatch):
    root = tmp_path / "research"
    root.mkdir()
    (root / "private").mkdir()
    original = group.os.scandir

    def denied(path):
        if Path(path) == root / "private":
            raise PermissionError("cannot count research installation")
        return original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(group.os, "scandir", denied)
        with pytest.raises(PermissionError, match="cannot count research installation"):
            group._storage(root, [root], {"max_bytes": 100_000, "min_free_bytes": 1})
