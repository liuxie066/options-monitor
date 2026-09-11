from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA,
    CandidateSnapshotManifestError,
    load_candidate_snapshot_bundle,
    load_candidate_snapshot_bundle_v3,
    load_latest_candidate_snapshot_bundle,
    publish_candidate_snapshot_manifest,
    publish_candidate_snapshot_manifest_v3,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    seal_combo_yield_candidate_snapshot,
)
from src.application.cc_lp_candidate_snapshot import (
    seal_cc_lp_candidate_snapshot,
)
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V4_SCHEMA,
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
)
from src.application.wheel.candidate_snapshot import seal_wheel_candidate_snapshot


CONFIG_HASH = "a" * 64
POLICY_HASH = "b" * 64


def _account_dir(base: Path) -> Path:
    path = base / "output_runs" / "run-1" / "accounts" / "lx"
    (path / "state").mkdir(parents=True, exist_ok=True)
    return path


def _dependencies() -> list[dict]:
    return [
        {"kind": kind, "relpath": None, "sha256": char * 64}
        for kind, char in (
            ("required_data", "1"),
            ("portfolio", "2"),
            ("ledger", "3"),
            ("fx", "4"),
            ("earnings_rv", "5"),
        )
    ]


def _pair() -> dict:
    return {
        "candidate_pair_id": "combo_yield:NVDA:P:C",
        "symbol": "NVDA",
        "put_contract_symbol": "P",
        "call_contract_symbol": "C",
    }


def _expected() -> list[dict[str, str]]:
    return [
        {
            "market": "US",
            "symbol": "NVDA",
            "strategy_family": "combo_yield",
            "strategy_mode": "combo_yield",
            "candidate_owner": "sp_lc",
            "account_config_sha256": CONFIG_HASH,
        }
    ]


def _seal_combo_bundle(base: Path) -> dict:
    account_dir = _account_dir(base)
    (account_dir / "nvda_combo_yield_candidates.csv").write_text(
        "symbol\nNVDA\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="combo_yield",
        status="completed",
        candidate_count=1,
        snapshot_id="quote-1",
        receipt_relpath="quotes/quote-1/receipt.json",
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=_expected(),
    )
    pair = _pair()
    seal_combo_yield_candidate_snapshot(
        base=base,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": "completed",
                "quote_snapshot_id": "quote-1",
                "quote_receipt_relpath": "quotes/quote-1/receipt.json",
            }
        ],
        pair_evaluations=[
            {
                **pair,
                "diagnostic_scope": "pair",
                "diagnostic_stage": "pair_filter",
                "accepted": True,
                "reject_reasons": "",
            }
        ],
        rank_records=[
            {
                **pair,
                "baseline_rank": 1,
                "shadow_rank": 1,
                "baseline_selected": True,
                "shadow_selected": True,
                "rank_changed": False,
            }
        ],
        ranked_pairs=[pair],
        sealed_at="2026-08-12T01:00:00Z",
    )
    return publish_candidate_snapshot_manifest(
        base=base,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256=POLICY_HASH,
        sealed_at="2026-08-12T01:00:01Z",
    )


def test_manifest_binds_terminal_status_and_owner_snapshot(tmp_path: Path) -> None:
    manifest = _seal_combo_bundle(tmp_path)

    assert manifest["completion_reason"] == "complete"
    assert manifest["expected_owners"] == ["sp_lc"]
    bundle = load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )
    assert bundle["manifest"] == manifest
    assert set(bundle["owners"]) == {"sp_lc"}
    assert load_latest_candidate_snapshot_bundle(
        base=tmp_path,
        account="lx",
    )["manifest"] == manifest


def test_latest_bundle_does_not_fall_back_past_incomplete_candidate_run(
    tmp_path: Path,
) -> None:
    _seal_combo_bundle(tmp_path)
    complete_run = tmp_path / "output_runs" / "run-1"
    incomplete_state = (
        tmp_path
        / "output_runs"
        / "run-2"
        / "accounts"
        / "lx"
        / "state"
    )
    incomplete_state.mkdir(parents=True)
    newer_ns = complete_run.stat().st_mtime_ns + 1_000_000_000
    os.utime(incomplete_state.parents[2], ns=(newer_ns, newer_ns))

    with pytest.raises(CandidateSnapshotManifestError, match="manifest is unavailable"):
        load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")


def test_manifest_supports_empty_no_applicable_scope_commit(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=[],
    )

    manifest = publish_candidate_snapshot_manifest(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256=POLICY_HASH,
        sealed_at="2026-08-12T01:00:00Z",
    )

    assert manifest["completion_reason"] == "no_applicable_scope"
    assert manifest["expected_scopes"] == []
    assert manifest["owner_snapshots"] == []
    assert load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )["owners"] == {}


def test_missing_manifest_does_not_salvage_owner_snapshot(tmp_path: Path) -> None:
    _seal_combo_bundle(tmp_path)
    manifest_path = (
        _account_dir(tmp_path) / "state" / CANDIDATE_SNAPSHOT_MANIFEST_FILE
    )
    manifest_path.unlink()

    with pytest.raises(CandidateSnapshotManifestError, match="manifest is unavailable"):
        load_candidate_snapshot_bundle(
            base=tmp_path,
            run_id="run-1",
            account="lx",
        )


def test_manifest_rejects_owner_snapshot_not_declared_by_index(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=[],
    )
    seal_cc_lp_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "combo_yield",
                "variant": "cc_lp",
                "status": "not_applicable",
                "reason": "no_covered_stock",
            }
        ],
        ranked_pairs=[],
        sealed_at="2026-08-12T01:00:00Z",
    )

    with pytest.raises(CandidateSnapshotManifestError, match="unexpected: cc_lp"):
        publish_candidate_snapshot_manifest(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            strategy_policy_sha256=POLICY_HASH,
            sealed_at="2026-08-12T01:00:01Z",
        )


@pytest.mark.parametrize(
    "bound_name",
    [STRATEGY_SCAN_STATUS_INDEX_V2_FILE, f"state/{COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE}"],
)
def test_manifest_rejects_tampered_bound_file(
    tmp_path: Path,
    bound_name: str,
) -> None:
    _seal_combo_bundle(tmp_path)
    path = _account_dir(tmp_path) / bound_name
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(CandidateSnapshotManifestError, match="hash mismatch"):
        load_candidate_snapshot_bundle(
            base=tmp_path,
            run_id="run-1",
            account="lx",
        )


def test_manifest_rejects_status_snapshot_quote_binding_mismatch(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_combo_yield_candidates.csv").write_text(
        "symbol\nNVDA\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="combo_yield",
        status="completed",
        candidate_count=0,
        snapshot_id="quote-index",
        receipt_relpath="quotes/index/receipt.json",
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=_expected(),
    )
    seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": "completed",
                "quote_snapshot_id": "quote-snapshot",
                "quote_receipt_relpath": "quotes/snapshot/receipt.json",
            }
        ],
        ranked_pairs=[],
        sealed_at="2026-08-12T01:00:00Z",
    )

    with pytest.raises(CandidateSnapshotManifestError, match="quote binding mismatch"):
        publish_candidate_snapshot_manifest(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            strategy_policy_sha256=POLICY_HASH,
            sealed_at="2026-08-12T01:00:01Z",
        )


def test_manifest_rejects_owner_snapshot_market_mismatch(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_combo_yield_candidates.csv").write_text(
        "symbol\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="combo_yield",
        status="completed",
        candidate_count=0,
        snapshot_id="quote-1",
        receipt_relpath="quotes/quote-1/receipt.json",
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=_expected(),
    )
    seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="hk",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": "completed",
                "quote_snapshot_id": "quote-1",
                "quote_receipt_relpath": "quotes/quote-1/receipt.json",
            }
        ],
        sealed_at="2026-08-12T01:00:00Z",
    )

    with pytest.raises(CandidateSnapshotManifestError, match="market mismatch"):
        publish_candidate_snapshot_manifest(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            strategy_policy_sha256=POLICY_HASH,
            sealed_at="2026-08-12T01:00:01Z",
        )


def test_manifest_rejects_snapshot_only_terminal_reason(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_combo_yield_candidates.csv").write_text(
        "symbol\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="combo_yield",
        status="completed",
        candidate_count=0,
        snapshot_id="quote-1",
        receipt_relpath="quotes/quote-1/receipt.json",
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=_expected(),
    )
    seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": "completed",
                "reason": "partial_data",
                "quote_snapshot_id": "quote-1",
                "quote_receipt_relpath": "quotes/quote-1/receipt.json",
            }
        ],
        sealed_at="2026-08-12T01:00:00Z",
    )

    with pytest.raises(CandidateSnapshotManifestError, match="reason mismatch"):
        publish_candidate_snapshot_manifest(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            strategy_policy_sha256=POLICY_HASH,
            sealed_at="2026-08-12T01:00:01Z",
        )


def test_manifest_rejects_selected_pair_in_failed_scope(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_combo_yield_candidates.csv").write_text(
        "symbol\nNVDA\n",
        encoding="utf-8",
    )
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="combo_yield",
        status="failed",
        reason="combo_yield_scan_failed",
        snapshot_id="quote-1",
        receipt_relpath="quotes/quote-1/receipt.json",
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=_expected(),
    )
    pair = _pair()
    seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": "failed",
                "reason": "combo_yield_scan_failed",
                "quote_snapshot_id": "quote-1",
                "quote_receipt_relpath": "quotes/quote-1/receipt.json",
            }
        ],
        pair_evaluations=[
            {
                **pair,
                "diagnostic_scope": "pair",
                "diagnostic_stage": "pair_filter",
                "accepted": True,
                "reject_reasons": "",
            }
        ],
        rank_records=[
            {
                **pair,
                "baseline_rank": 1,
                "shadow_rank": 1,
                "baseline_selected": True,
                "shadow_selected": True,
                "rank_changed": False,
            }
        ],
        ranked_pairs=[pair],
        sealed_at="2026-08-12T01:00:00Z",
    )

    with pytest.raises(CandidateSnapshotManifestError, match="non-completed"):
        publish_candidate_snapshot_manifest(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            strategy_policy_sha256=POLICY_HASH,
            sealed_at="2026-08-12T01:00:01Z",
        )


def test_manifest_is_write_once_and_adopts_exact_retry(tmp_path: Path) -> None:
    first = _seal_combo_bundle(tmp_path)
    second = publish_candidate_snapshot_manifest(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256=POLICY_HASH,
        sealed_at="2026-08-12T01:00:01Z",
    )
    assert second == first

    path = _account_dir(tmp_path) / "state" / CANDIDATE_SNAPSHOT_MANIFEST_FILE
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == first
    with pytest.raises(CandidateSnapshotManifestError, match="conflicts"):
        publish_candidate_snapshot_manifest(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            strategy_policy_sha256=POLICY_HASH,
            sealed_at="2026-08-12T01:00:02Z",
        )


def _publish_wheel_v4_statuses(account_dir: Path) -> None:
    expected = []
    for direction in ("call", "put"):
        publish_strategy_scan_status(
            report_dir=account_dir,
            run_id="run-1",
            account="lx",
            market="US",
            symbol="NVDA",
            strategy_family="wheel",
            direction=direction,
            status="completed",
            candidate_count=0,
        )
        expected.append(
            {
                "market": "US",
                "symbol": "NVDA",
                "strategy_family": "wheel",
                "direction": direction,
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "account_config_sha256": CONFIG_HASH,
            }
        )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=expected,
    )


def _wheel_v2_snapshot() -> dict:
    return {
        "schema_version": "wheel_candidate_snapshot.v2",
        "run_id": "run-1",
        "account": "lx",
        "market": "us",
        "candidate_owner": "wheel",
        "account_config_sha256": CONFIG_HASH,
        "strategy_policy_sha256": POLICY_HASH,
        "content_sha256": "c" * 64,
        "opening_status": "no_candidate",
        "scope_results": [
            {
                "scope": "strategy",
                "symbol": "NVDA",
                "direction": direction,
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "status": "completed",
                "reason_code": None,
                "candidate_count": 0,
                "quote_snapshot_id": None,
                "quote_receipt_relpath": None,
            }
            for direction in ("call", "put")
        ],
        "batches": [],
    }


def test_wheel_v4_index_publishes_and_loads_manifest_v3(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    _publish_wheel_v4_statuses(account_dir)
    seal_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependencies(),
        scope_results=[
            {
                "symbol": "NVDA",
                "direction": direction,
                "status": "completed",
                "candidate_count": 0,
            }
            for direction in ("call", "put")
        ],
        batches=[],
    )

    manifest = publish_candidate_snapshot_manifest_v3(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256=POLICY_HASH,
        sealed_at="2026-08-12T01:00:01Z",
    )

    assert manifest["schema_version"] == CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA
    assert manifest["status_index"]["schema_version"] == (
        STRATEGY_SCAN_STATUS_INDEX_V4_SCHEMA
    )
    assert {row["direction"] for row in manifest["expected_scopes"]} == {
        "call",
        "put",
    }
    assert (account_dir / "state" / CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE).is_file()
    loaded = load_candidate_snapshot_bundle_v3(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )
    assert loaded["manifest"] == manifest

    (account_dir / "nvda_wheel_put_scan_status.v2.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    with pytest.raises(CandidateSnapshotManifestError, match="status hash mismatch"):
        load_candidate_snapshot_bundle(base=tmp_path, run_id="run-1", account="lx")


def test_manifest_v3_rejects_legacy_wheel_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_dir = _account_dir(tmp_path)
    _publish_wheel_v4_statuses(account_dir)
    snapshot = _wheel_v2_snapshot()
    state_dir = account_dir / "state"
    (state_dir / "wheel_candidate_snapshot.v2.json").write_text(
        json.dumps(snapshot),
        encoding="utf-8",
    )
    (state_dir / "wheel_candidate_snapshot.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "src.application.candidate_snapshot_manifest._load_owner_snapshot",
        lambda **_kwargs: snapshot,
    )

    with pytest.raises(CandidateSnapshotManifestError, match="artifact_version_mismatch"):
        publish_candidate_snapshot_manifest(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            strategy_policy_sha256=POLICY_HASH,
        )


def test_legacy_wheel_bundle_adapts_to_call_and_rejects_v2_mix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_dir = _account_dir(tmp_path)
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="wheel",
        status="completed",
        candidate_count=0,
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256=CONFIG_HASH,
        expected=[
            {
                "market": "US",
                "symbol": "NVDA",
                "strategy_family": "wheel",
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "account_config_sha256": CONFIG_HASH,
            }
        ],
    )
    snapshot = {
        **_wheel_v2_snapshot(),
        "schema_version": "wheel_candidate_snapshot.v1",
        "scope_results": [
            {
                key: value
                for key, value in _wheel_v2_snapshot()["scope_results"][0].items()
                if key != "direction"
            }
        ],
    }
    state_dir = account_dir / "state"
    (state_dir / "wheel_candidate_snapshot.json").write_text(
        json.dumps(snapshot),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.application.candidate_snapshot_manifest._load_owner_snapshot",
        lambda **_kwargs: snapshot,
    )
    publish_candidate_snapshot_manifest(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256=POLICY_HASH,
    )

    bundle = load_candidate_snapshot_bundle(base=tmp_path, run_id="run-1", account="lx")
    assert bundle["status_index"]["items"][0]["direction"] == "call"
    assert bundle["manifest"]["expected_scopes"][0]["direction"] == "call"
    assert bundle["owners"]["wheel"]["scope_results"][0]["direction"] == "call"

    (state_dir / "wheel_candidate_snapshot.v2.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    with pytest.raises(CandidateSnapshotManifestError, match="artifact_version_mismatch"):
        load_candidate_snapshot_bundle(base=tmp_path, run_id="run-1", account="lx")


def _write_scheduler_skip(
    base: Path, monkeypatch, *, run_id: str = "run-2", interruption: str | None = None,
) -> Path:
    from types import SimpleNamespace
    from src.application import account_run
    from src.application.tick_run_workspace import publish_account_run_config

    config = {"portfolio": {"account": "lx"}, "_generated": {"market": "us"}, "symbols": []}
    authority = publish_account_run_config(base=base, run_id=run_id, account="lx", config=config)
    request = account_run.AccountRunRequest(
        acct="lx", base=base, account_config_authority=authority, vpy=base / "python",
        markets_to_run=["US"], scheduler_ms=1, scheduler_view=None,
        notify_decision_by_account={}, should_run_global=True, reason_global="global_due",
        run_id=run_id, run_dir=base / "output_runs" / run_id,
        shared_required=base / "required_data", accounts_root=base / "output_runs" / run_id / "accounts",
        prefetch_done=True,
        scan_decision_by_account={"lx": {"source": "account_scheduler", "should_run": False, "reason": "not_due"}},
    )
    monkeypatch.setattr(account_run, "run_pipeline_script", lambda **_: pytest.fail("skip started pipeline"))
    writer = account_run.state_repo.write_account_run_state

    def write(*args):
        payload = args[-1]
        final = payload.get("scan_outcome") == "scheduler_skipped"
        if final and interruption in {"before_final", "write_failed"}:
            if interruption == "write_failed":
                raise OSError("injected final write failure")
            raise KeyboardInterrupt("before final publication")
        result = writer(*args)
        if (not final and interruption == "after_initial") or (final and interruption == "after_final"):
            raise KeyboardInterrupt("after publication")
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(account_run.state_repo, "write_account_run_state", write)
        if interruption in {"after_initial", "before_final", "after_final"}:
            with pytest.raises(KeyboardInterrupt):
                account_run.run_one_account(
                    request=request, runlog=SimpleNamespace(safe_event=lambda *a, **k: None),
                    audit_fn=lambda *a, **k: None, fail_schema_validation=lambda **_: pytest.fail("schema"),
                )
        else:
            outcome = account_run.run_one_account(
                request=request, runlog=SimpleNamespace(safe_event=lambda *a, **k: None),
                audit_fn=lambda *a, **k: None, fail_schema_validation=lambda **_: pytest.fail("schema"),
            )
            assert outcome.ran_pipeline is False
    return authority.state_path.parent / "account_metrics.json"


def _older_bundle_with_frozen_config(tmp_path, monkeypatch, *, market="us"):
    from src.application.tick_run_workspace import publish_account_run_config

    authority = publish_account_run_config(
        base=tmp_path, run_id="run-1", account="lx",
        config={"portfolio": {"account": "lx"}, "_generated": {"market": market}, "symbols": []},
    )
    monkeypatch.setattr(__import__(__name__, fromlist=["CONFIG_HASH"]), "CONFIG_HASH", authority.account_config_sha256)
    return _seal_combo_bundle(tmp_path)


@pytest.mark.parametrize("pointer", [False, True])
@pytest.mark.parametrize("interruption", [None, "after_initial", "before_final", "write_failed", "after_final"])
def test_latest_requires_actual_terminal_skip_publication(tmp_path, monkeypatch, pointer, interruption):
    manifest = _older_bundle_with_frozen_config(tmp_path, monkeypatch)
    metrics_path = _write_scheduler_skip(tmp_path, monkeypatch, interruption=interruption)
    previous = tmp_path / "output_runs" / "run-1"
    latest = tmp_path / "output_runs" / "run-2"
    newer = previous.stat().st_mtime_ns + 1_000_000_000
    os.utime(latest, ns=(newer, newer))
    if pointer:
        path = tmp_path / "output_shared" / "state" / "last_run_dir.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(latest))
    published = interruption in {None, "after_final"}
    assert (json.loads(metrics_path.read_text()).get("scan_outcome") == "scheduler_skipped") is published
    if published:
        bundle = load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")
        assert bundle["manifest"] == manifest
        assert bundle["source_selection"] == {"skipped_non_scan_runs": 1}
    else:
        with pytest.raises(CandidateSnapshotManifestError, match="manifest is unavailable") as error:
            load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")
        assert error.value.run_id == "run-2"
    with pytest.raises(CandidateSnapshotManifestError, match="manifest is unavailable"):
        load_candidate_snapshot_bundle(base=tmp_path, run_id="run-2", account="lx")


@pytest.mark.parametrize("change", [
    {"scan_outcome": None}, {"scan_outcome": "prefetch_failed"},
    {"ran_scan": 0}, {"ran_pipeline": "false"}, {"ran_scan": True},
    {"pipeline_started_at_utc": "2026-09-11T00:00:00Z"}, {"pipeline_ms": 0},
    {"error_code": "CONFIG_ERROR"}, {"snapshot_status": "failed"},
    {"account_config_sha256": "f" * 64}, {"run_id": "other"}, {"account": "sy"},
    {"markets_to_run": ["HK"]}, {"markets_to_run": []}, {"scan_mode": "experience"},
])
def test_latest_never_skips_unproven_or_conflicting_metrics(tmp_path, monkeypatch, change):
    _older_bundle_with_frozen_config(tmp_path, monkeypatch)
    path = _write_scheduler_skip(tmp_path, monkeypatch)
    metrics = json.loads(path.read_text())
    path.write_text(json.dumps({**metrics, **change}))
    with pytest.raises(CandidateSnapshotManifestError) as error:
        load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")
    assert error.value.run_id == "run-2"


@pytest.mark.parametrize("artifact", ["corrupt_metrics", "legacy_metrics", "config_corrupt", "config_account", "market_conflict", "market_missing", "candidate_output", "manifest_output", "metrics_symlink", "state_symlink", "accounts_symlink", "pointer_symlink"])
def test_latest_skip_rejects_corrupt_legacy_unsafe_or_output_evidence(tmp_path, monkeypatch, artifact):
    from hashlib import sha256

    _older_bundle_with_frozen_config(tmp_path, monkeypatch)
    metrics_path = _write_scheduler_skip(tmp_path, monkeypatch)
    if artifact == "corrupt_metrics":
        metrics_path.write_text("{")
    elif artifact == "legacy_metrics":
        metrics_path.write_text('{"ran_scan":false,"ran_pipeline":false}')
    elif artifact in {"config_corrupt", "config_account", "market_conflict", "market_missing"}:
        config_path = metrics_path.parent / "config.override.json"
        config = json.loads(config_path.read_text())
        if artifact == "config_account":
            config["portfolio"]["account"] = "sy"
        elif artifact == "market_conflict":
            config["_generated"]["market"] = "hk"
        elif artifact == "market_missing":
            del config["_generated"]
        encoded = json.dumps(config).encode()
        config_path.write_bytes(encoded)
        if artifact != "config_corrupt":
            (config_path.parent.parent / "config.override.json").write_bytes(encoded)
            metrics = json.loads(metrics_path.read_text())
            metrics["account_config_sha256"] = sha256(encoded).hexdigest()
            metrics_path.write_text(json.dumps(metrics))
    elif artifact in {"candidate_output", "manifest_output"}:
        (metrics_path.parent / ("opening_candidate_snapshot.json" if artifact == "candidate_output" else CANDIDATE_SNAPSHOT_MANIFEST_FILE)).write_text("{}")
    else:
        if artifact == "pointer_symlink":
            pointer = tmp_path / "output_shared" / "state" / "last_run_dir.txt"
            pointer.parent.mkdir(parents=True, exist_ok=True)
            target = tmp_path / "pointer.txt"
            target.write_text(str(tmp_path / "output_runs" / "run-2"))
            pointer.symlink_to(target)
        else:
            path = {"metrics_symlink": metrics_path, "state_symlink": metrics_path.parent, "accounts_symlink": metrics_path.parents[2]}[artifact]
            moved = path.with_name(path.name + "-moved")
            path.rename(moved)
            path.symlink_to(moved)
    with pytest.raises(CandidateSnapshotManifestError):
        load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")


@pytest.mark.parametrize("conflict", ["selected_market", "selected_hash", "intervening_failure", "skip_market"])
def test_skip_chain_preserves_frozen_target_scope_and_stops_at_failed_scan(tmp_path, monkeypatch, conflict):
    from hashlib import sha256

    _older_bundle_with_frozen_config(tmp_path, monkeypatch, market="hk" if conflict == "selected_market" else "us")
    metrics_path = _write_scheduler_skip(tmp_path, monkeypatch)
    if conflict == "selected_hash":
        state = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "state"
        config_path = state / "config.override.json"
        config = json.loads(config_path.read_text())
        config["_generated"]["market"] = "hk"
        encoded = json.dumps(config).encode()
        config_path.write_bytes(encoded)
        (state.parent / config_path.name).write_bytes(encoded)
    elif conflict == "intervening_failure":
        broken = tmp_path / "output_runs" / "run-failed" / "accounts" / "lx" / "state"
        broken.mkdir(parents=True)
        broken.joinpath("account_metrics.json").write_text('{"ran_scan":false,"ran_pipeline":false,"error_code":"PREFETCH_FAILED"}')
        old_time = (tmp_path / "output_runs" / "run-1").stat().st_mtime_ns
        os.utime(broken.parents[2], ns=(old_time + 1_000_000_000, old_time + 1_000_000_000))
        os.utime(metrics_path.parents[3], ns=(old_time + 2_000_000_000, old_time + 2_000_000_000))
    elif conflict == "skip_market":
        path = _write_scheduler_skip(tmp_path, monkeypatch, run_id="run-3")
        config_path = path.parent / "config.override.json"
        config = json.loads(config_path.read_text())
        config["_generated"]["market"] = "hk"
        encoded = json.dumps(config).encode()
        config_path.write_bytes(encoded)
        (path.parent.parent / config_path.name).write_bytes(encoded)
        metrics = json.loads(path.read_text())
        metrics.update(account_config_sha256=sha256(encoded).hexdigest(), markets_to_run=["HK"])
        path.write_text(json.dumps(metrics))
    with pytest.raises(CandidateSnapshotManifestError):
        load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")


def test_latest_pointer_skip_does_not_select_run_newer_than_pointer(tmp_path, monkeypatch):
    manifest = _older_bundle_with_frozen_config(tmp_path, monkeypatch)
    _write_scheduler_skip(tmp_path, monkeypatch)
    # The established pointer is authoritative even if a later workspace exists.
    newer = tmp_path / "output_runs" / "run-3" / "accounts" / "lx"
    newer.mkdir(parents=True)
    pointer = tmp_path / "output_shared" / "state" / "last_run_dir.txt"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("output_runs/run-2")
    bundle = load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")
    assert bundle["manifest"] == manifest
