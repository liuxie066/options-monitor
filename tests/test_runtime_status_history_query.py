from __future__ import annotations

import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.application.agent_tools import runtime_status_impl as status
from src.application.runtime_config_paths import read_json_object_or_empty


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def history(tmp_path, monkeypatch):
    reads = Counter()
    infos = Counter()
    full = Counter()
    text = Counter()
    sorts = Counter()
    monkeypatch.setattr(status, "ledger_store_payload", lambda path: {"runtime_root": str(path.parent)})
    monkeypatch.setattr(status, "build_wheel_activation_readiness", lambda **kwargs: {})
    original_info = status._json_file_info
    original_full = status._run_payload
    original_text = status._compatibility_notification_info
    original_sort = status._run_dirs_newest_first

    def info(path, **kwargs):
        infos[path.resolve()] += 1
        return original_info(path, **kwargs)

    def complete(path, **kwargs):
        full[path.resolve()] += 1
        return original_full(path, **kwargs)

    def notification(path, **kwargs):
        text[path.resolve()] += 1
        return original_text(path, **kwargs)

    def dirs(path):
        sorts[path.resolve()] += 1
        return original_sort(path)

    monkeypatch.setattr(status, "_json_file_info", info)
    monkeypatch.setattr(status, "_run_payload", complete)
    monkeypatch.setattr(status, "_compatibility_notification_info", notification)
    monkeypatch.setattr(status, "_run_dirs_newest_first", dirs)

    def invoke(root=tmp_path, *, market="US", accounts=("lx", "sy"), reader=None, **payload):
        cfg = {"accounts": list(accounts), "portfolio": {}}
        config = root / "config.json"

        def read(path):
            reads[path.resolve()] += 1
            return (reader or read_json_object_or_empty)(path)

        return status.private_runtime_status_tool(
            {"config_key": market, **payload},
            load_runtime_config=lambda **kwargs: (config, cfg),
            normalize_accounts=lambda value, fallback: list(value) if value is not None else list(fallback),
            accounts_from_config=lambda config: config["accounts"],
            read_json_object_or_empty=read,
            repo_base=lambda: root,
            mask_path=lambda path: str(path),
        )[0]

    def run(name, *, root=tmp_path, tick=None, last=None, account_last=None, mtime=1):
        directory = root / "output_runs" / name
        write_json(directory / "state/tick_metrics.json", tick or {})
        write_json(directory / "state/last_run.json", last or {})
        for account in ("lx", "sy"):
            state = directory / "accounts" / account / "state"
            write_json(state / "last_run.json", (account_last or {}).get(account, {}))
            write_json(state / "expired_position_maintenance.json", {})
            write_json(state / "required_data_prefetch_summary.json", {"errors": 0})
            (state.parent / "symbols_notification.txt").write_text("fixture notification", encoding="utf-8")
        os.utime(directory, (mtime, mtime))
        return directory

    return invoke, run, reads, infos, full, text, sorts


def test_history_wrong_market_reads_only_screening_files(history, tmp_path):
    invoke, run, reads, infos, full, text, sorts = history
    for index in range(50):
        run(f"run-{index:02}", tick={"market": "US", "ran_scan": True}, mtime=index + 1)
    data = invoke(market="HK")
    root = tmp_path / "output_runs"
    assert data["latest_run"] is None
    assert data["latest_scanned_run"] is None
    for key in ("latest_run_selection", "latest_scanned_run_selection"):
        assert data[key]["searched_count"] == 50
        assert data[key]["skipped_market_mismatch_count"] == 50
    history_reads = {p: n for p, n in reads.items() if p.is_relative_to(root)}
    assert sum(history_reads.values()) == 200
    assert set(history_reads.values()) == {1}
    assert all(n == 1 for p, n in infos.items() if p.is_relative_to(root))
    assert not full
    assert not any(p.is_relative_to(root) for p in text)
    assert sorts[root] == 1


@pytest.mark.parametrize("pointer", ["valid", "relative", "stale", "wrong", "external"])
def test_pointer_and_scanned_selection_keep_distinct_counts(history, tmp_path, pointer):
    invoke, run, reads, infos, full, text, sorts = history
    old = run("old", tick={"market": "US", "ran_scan": True}, mtime=1)
    new = run("new", tick={"market": "HK", "ran_scan": True}, mtime=2)
    pointed = old
    if pointer == "stale":
        pointed = tmp_path / "missing"
    elif pointer == "wrong":
        pointed = new
    elif pointer == "external":
        pointed = run("outside", root=tmp_path.parent / (tmp_path.name + "-external"),
                      tick={"market": "US", "ran_scan": False})
    state = tmp_path / "output_shared/state"
    state.mkdir(parents=True)
    (state / "last_run_dir.txt").write_text(
        str(pointed.relative_to(tmp_path)) if pointer == "relative" else str(pointed), encoding="utf-8",
    )
    data = invoke()
    expected = pointed if pointer in {"valid", "relative", "external"} else old
    assert data["latest_run"]["path"] == status._relative_path(expected, base=tmp_path)
    assert data["latest_scanned_run"]["path"] == "output_runs/old"
    latest = data["latest_run_selection"]
    assert latest["searched_count"] == {"stale": 2, "wrong": 1}.get(pointer, 0)
    assert latest["skipped_market_mismatch_count"] == int(pointer in {"stale", "wrong"})
    assert data["latest_scanned_run_selection"]["searched_count"] == 2
    assert data["latest_scanned_run_selection"]["skipped_market_mismatch_count"] == 1
    assert full[old] == 1
    assert all(n == 1 for n in full.values())
    assert all(n == 1 for p, n in reads.items() if p.is_relative_to(tmp_path / "output_runs"))
    assert sorts[tmp_path / "output_runs"] == 1


def test_latest_skip_scanned_older_and_tied_mtime(history):
    invoke, run, reads, infos, full, text, sorts = history
    run("a-scan", tick={"market": "US", "ran_scan": True}, mtime=5)
    run("z-skip", tick={"market": "US", "ran_scan": False}, mtime=5)
    data = invoke()
    assert data["latest_run"]["path"].endswith("z-skip")
    assert data["latest_scanned_run"]["path"].endswith("a-scan")
    assert data["latest_run_selection"]["searched_count"] == 1
    assert data["latest_scanned_run_selection"]["searched_count"] == 2
    assert sum(full.values()) == 2


@pytest.mark.parametrize("tick,last,account_last,scanned", [
    ({"market": "US", "ran_scan": True}, {}, {"lx": {"market": "HK"}}, True),
    ({"scheduler_decision": {"markets_to_run": ["HK"]}, "ran_scan": True}, {}, {}, True),
    ({}, {}, {"lx": {"market": "HK", "ran_scan": True}}, True),
    ({"accounts": {"unselected": {"ran_scan": True}}}, {}, {}, True),
    ({"accounts": [{"ran_scan": True}]}, {}, {}, True),
    ({}, {"ran_scan": True}, {}, True),
    ({"ran_scan": "true"}, {"ran_scan": 1}, {"lx": {"ran_scan": "True"}}, False),
    ({"accounts": [{"ran_scan": 1}]}, {}, {}, False),
    ({"ran_scan": False}, {}, {}, False),
])
def test_existing_market_union_unknown_and_strict_scan(history, tick, last, account_last, scanned):
    invoke, run, *_ = history
    run("one", tick=tick, last=last, account_last=account_last)
    data = invoke(market="HK", accounts=("lx",))
    assert data["latest_run"] is not None
    assert (data["latest_scanned_run"] is not None) is scanned
    assert set(data["latest_run"]["accounts"]) == {"lx"}


@pytest.mark.parametrize("payload,found,source", [
    ({"run_id": "hk"}, True, "run_id"),
    ({"run_dir": "output_runs/hk", "run_id": "us"}, True, "run_dir"),
    ({"run_id": "missing"}, False, "run_id"),
    ({"run_id": "../hk"}, False, "run_id"),
    ({"run_dir": "missing", "run_id": "us"}, False, "run_dir"),
])
def test_explicit_run_overrides_market_without_scanned_override(history, payload, found, source):
    invoke, run, *_ = history
    run("hk", tick={"market": "HK", "ran_scan": True}, mtime=2)
    run("us", tick={"market": "US", "ran_scan": True}, mtime=1)
    data = invoke(**payload)
    assert data["latest_run_selection"]["source"] == source
    assert data["latest_run_selection"]["found"] is found
    assert (data["latest_run"] is not None) is found
    if found:
        assert data["latest_run"]["path"].endswith("hk")
    assert data["latest_scanned_run"]["path"].endswith("us")


def test_missing_malformed_metadata_and_next_request_freshness(history, tmp_path):
    invoke, run, reads, infos, full, *_ = history
    directory = run("one", tick={"market": "US", "ran_scan": True})
    tick = directory / "state/tick_metrics.json"
    missing = directory / "accounts/sy/state/last_run.json"
    missing.unlink()
    malformed = directory / "state/last_run.json"
    malformed.write_text("{invalid", encoding="utf-8")

    def change_after_read(path):
        result = read_json_object_or_empty(path)
        if path == tick:
            write_json(tick, {"market": "HK", "ran_scan": False})
        return result

    data = invoke(reader=change_after_read)
    assert data["latest_scanned_run"] is not None
    assert data["latest_run"]["state"]["tick_metrics"]["json"]["market"] == "US"
    assert data["latest_run"]["state"]["last_run"]["json"] == {}
    assert data["latest_run"]["accounts"]["sy"]["last_run"]["exists"] is False
    assert infos[missing] == 1
    assert full[directory] == 1
    assert reads[tick] == 1
    assert invoke()["latest_run"] is None
    newest = run("new", tick={"market": "US", "ran_scan": True}, mtime=99)
    assert invoke()["latest_run"]["path"].endswith(newest.name)


def test_read_error_does_not_leak_partial_request_cache(history):
    invoke, run, *_ = history
    directory = run("one", tick={"ran_scan": True})

    def fail(path):
        if path == directory / "state/tick_metrics.json":
            raise PermissionError("fixture denial")
        return read_json_object_or_empty(path)

    with pytest.raises(PermissionError, match="fixture denial"):
        invoke(reader=fail)
    assert invoke()["latest_scanned_run"] is not None


def test_concurrent_requests_keep_roots_accounts_and_markets_isolated(history, tmp_path):
    invoke, run, *_ = history
    roots = [tmp_path / "us", tmp_path / "hk"]
    for root, market in zip(roots, ("US", "HK")):
        run(market, root=root, tick={"market": market, "ran_scan": True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, root, market=market, accounts=(account,))
                   for root, market, account in zip(roots, ("US", "HK"), ("lx", "sy"))]
        data = [future.result() for future in futures]
    for result, market, account in zip(data, ("US", "HK"), ("lx", "sy")):
        assert result["latest_run"]["path"].endswith(market)
        assert set(result["latest_run"]["accounts"]) == {account}


def test_empty_history_and_activation_view_are_lazy(history, tmp_path):
    invoke, run, reads, infos, full, text, sorts = history
    data = invoke()
    assert data["latest_run"] is None
    assert data["latest_scanned_run_selection"]["searched_count"] == 0
    assert sorts[tmp_path / "output_runs"] == 1
    sorts.clear()
    invoke(view="wheel_activation")
    assert not sorts


def test_one_directory_observation_per_request(history, tmp_path):
    invoke, run, reads, infos, full, text, sorts = history
    old = run("old", tick={"market": "US", "ran_scan": True})

    def add_during_screening(path):
        value = read_json_object_or_empty(path)
        if path == old / "state/tick_metrics.json":
            run("new", tick={"market": "HK", "ran_scan": True}, mtime=99)
        return value

    first = invoke(market="HK", reader=add_during_screening)
    assert first["latest_run"] is None
    assert first["latest_scanned_run"] is None
    assert first["latest_run_selection"]["searched_count"] == 1
    assert first["latest_scanned_run_selection"]["searched_count"] == 1
    assert sorts[tmp_path / "output_runs"] == 1
    assert invoke(market="HK")["latest_run"]["path"].endswith("new")


def test_selected_accounts_keep_market_scope_and_full_read_bound(history, tmp_path):
    invoke, run, reads, infos, full, text, sorts = history
    directory = run("one", tick={"market": "US", "ran_scan": False},
                    account_last={"sy": {"market": "HK", "ran_scan": True}})
    assert invoke(market="HK", accounts=("lx",))["latest_run"] is None
    reads.clear()
    data = invoke(market="HK", accounts=("sy",))
    assert data["latest_run"]["path"] == data["latest_scanned_run"]["path"]
    assert sum(n for p, n in reads.items() if p.is_relative_to(directory)) == 5
    assert full[directory] == 1
    assert all(n == 1 for p, n in reads.items() if p.is_relative_to(directory))
