from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    CandidateSnapshotManifestError,
    load_candidate_snapshot_bundle,
    load_latest_candidate_snapshot_bundle,
    publish_candidate_snapshot_manifest,
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
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
)


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
