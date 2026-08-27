from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256

from src.application.candidate_evidence_history import (
    NON_CONTRIBUTING_EXPERIENCE,
    load_account_candidate_evidence,
)
from src.application.candidate_snapshot_manifest import (
    CandidateSnapshotManifestError,
    load_candidate_snapshot_bundle,
    load_candidate_snapshot_bundle_readonly,
)
from src.application.experience_candidate_snapshot import (
    ExperienceCandidateSnapshotError,
    seal_experience_candidate_bundle,
)
from src.application.experience_mode import (
    EXPERIENCE_BANNER,
    experience_fields,
    render_experience_report,
    resolve_experience_account_display_name,
    validate_experience_request,
)
from src.application.recommendation_point import (
    RecommendationPointError,
    capture_scheduled_recommendation_point,
)
from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
)
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates


ACCOUNT = "paper"
RUN_ID = "experience-run"
CONFIG_HASH = "a" * 64
POLICY_HASH = "b" * 64
DISPLAY_NAME = "美股模拟期权账户"


def _config(*, trd_env: str = "SIMULATE") -> dict:
    return {
        "accounts": [ACCOUNT],
        "account_settings": {
            ACCOUNT: {
                "type": "futu",
                "market": "us",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "90000001",
                    "trd_env": trd_env,
                },
            }
        },
    }


def _dependency_rows(base: Path) -> list[dict[str, str | None]]:
    required = base / "required-data.json"
    required.write_text("{}\n", encoding="utf-8")
    return [
        {
            "kind": "required_data",
            "relpath": required.relative_to(base).as_posix(),
            "sha256": sha256(required.read_bytes()).hexdigest(),
        },
        {"kind": "fx", "relpath": None, "sha256": "c" * 64},
        {"kind": "earnings_rv", "relpath": None, "sha256": "d" * 64},
    ]


def _status_index(
    base: Path,
    *,
    family: str = "sell_put",
    owner: str = "opening",
    mode: str = "put",
    candidate_count: int = 1,
) -> tuple[Path, dict]:
    report_dir = base / "output_runs" / RUN_ID / "accounts" / ACCOUNT
    publish_strategy_scan_status(
        report_dir=report_dir,
        run_id=RUN_ID,
        account=ACCOUNT,
        market="US",
        symbol="DEMO",
        strategy_family=family,
        status="completed",
        candidate_count=candidate_count,
        reason="no_candidate" if candidate_count == 0 else None,
    )
    index = publish_strategy_scan_status_index_v2(
        report_dir=report_dir,
        run_id=RUN_ID,
        account=ACCOUNT,
        account_config_sha256=CONFIG_HASH,
        expected=[
            {
                "market": "US",
                "symbol": "DEMO",
                "strategy_family": family,
                "strategy_mode": mode,
                "candidate_owner": owner,
                "account_config_sha256": CONFIG_HASH,
            }
        ],
        experience_fields=experience_fields(DISPLAY_NAME),
    )
    return report_dir, index


def _seal_opening_bundle(base: Path) -> None:
    _report_dir, index = _status_index(base)
    seal_experience_candidate_bundle(
        base=base,
        run_id=RUN_ID,
        account=ACCOUNT,
        market="US",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependency_rows(base),
        status_index=index,
        statuses_by_owner={
            "opening": [
                {
                    "symbol": "DEMO",
                    "strategy_mode": "put",
                    "status": "completed",
                    "candidate_count": 1,
                }
            ]
        },
        opening_candidates={"put": [{"symbol": "DEMO", "strike": 10.0}]},
        opening_decisions={"put": []},
        combo_evidence_by_owner={},
        account_display_name=DISPLAY_NAME,
        sealed_at="2026-08-27T00:00:00+00:00",
    )


def test_experience_request_requires_manual_simulate_no_send() -> None:
    validate_experience_request(
        config=_config(),
        accounts=[ACCOUNT],
        no_send=True,
        smoke=False,
        trigger_context={"source": "manual"},
        opend_phone_verify_continue=False,
    )
    with pytest.raises(ValueError, match="trd_env=SIMULATE"):
        validate_experience_request(
            config=_config(trd_env="REAL"),
            accounts=[ACCOUNT],
            no_send=True,
            smoke=False,
            trigger_context={"source": "manual"},
            opend_phone_verify_continue=False,
        )
    with pytest.raises(ValueError, match="--no-send"):
        validate_experience_request(
            config=_config(),
            accounts=[ACCOUNT],
            no_send=False,
            smoke=False,
            trigger_context={"source": "manual"},
            opend_phone_verify_continue=False,
        )


def test_experience_display_name_uses_redacted_account_metadata(monkeypatch) -> None:
    class Gateway:
        def get_account_metadata(self, **kwargs):
            assert kwargs == {
                "expected_account_id": "90000001",
                "trd_env": "SIMULATE",
                "expected_market": "US",
            }
            return {
                "matched": True,
                "sim_acc_type": "OPTION",
                "trdmarket_auth": ["US"],
                "same_type_count": 2,
                "account_id_tail": "0001",
            }

        def close(self):
            pass

    monkeypatch.setattr(
        "src.application.experience_mode.build_futu_gateway",
        lambda **_kwargs: Gateway(),
    )
    assert resolve_experience_account_display_name(
        config=_config(), account=ACCOUNT
    ) == f"{DISPLAY_NAME} · 尾号 0001"


def test_experience_report_marks_non_executable_and_redacts_internal_label() -> None:
    report = render_experience_report(
        rows=[
            {
                "symbol": "DEMO",
                "strategy": "sell_put",
                "candidate_count": 1,
                "note": f"routed by {ACCOUNT}",
            }
        ],
        account_display_name=DISPLAY_NAME,
        internal_account_label=ACCOUNT,
        owner_statuses={"opening": "candidates_found", "sp_lc": "no_candidate"},
    )
    assert EXPERIENCE_BANNER in report
    assert "- 可执行：否" in report
    assert "正式状态（Sell Put / Covered Call）：candidates_found" in report
    assert "正式状态（Combo Yield SP+LC）：no_candidate" in report
    assert f"routed by {ACCOUNT}" not in report
    assert "routed by <account>" in report


def test_experience_child_facade_renders_status_from_sealed_bundle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import pipeline_runtime, pipeline_watchlist

    _seal_opening_bundle(tmp_path)
    config_path = tmp_path / "account.json"
    config_path.write_text("{}\n", encoding="utf-8")
    report_dir = tmp_path / "output_runs" / RUN_ID / "accounts" / ACCOUNT
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(
        pipeline_runtime,
        "load_runtime_pipeline_config",
        lambda **_kwargs: {
            "symbols": [],
            "portfolio": {"account": ACCOUNT},
        },
    )
    monkeypatch.setattr(
        pipeline_watchlist,
        "run_watchlist_pipeline_default",
        lambda **_kwargs: [
            {
                "symbol": "DEMO",
                "strategy": "sell_put",
                "candidate_count": 1,
                "note": "candidate",
            }
        ],
    )

    assert pipeline_runtime.main(
        [
            "--config",
            str(config_path),
            "--report-dir",
            str(report_dir),
            "--state-dir",
            str(report_dir / "state"),
            "--source-account-run-id",
            RUN_ID,
            "--experience",
            "--account-display-name",
            DISPLAY_NAME,
        ]
    ) == 0

    report = (report_dir / "experience_report.md").read_text(encoding="utf-8")
    assert "正式状态（Sell Put / Covered Call）：candidates_found" in report
    assert "| DEMO | sell_put | 1 | candidate |" in report


def test_sell_put_demo_capacity_uses_contract_multiplier_and_native_currency() -> None:
    rows = pd.DataFrame(
        [
            {"strike": 12.5, "multiplier": 10, "currency": "USD"},
            {"strike": 20.0, "multiplier": 100, "currency": "HKD"},
        ]
    )
    out = enrich_sell_put_candidates_with_cash(
        df_labeled=rows,
        symbol="DEMO",
        portfolio_ctx=None,
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
        demo_capacity=True,
    )
    assert out["cash_required_native"].tolist() == [125.0, 2000.0]
    assert out["max_new_contracts"].tolist() == [1, 1]
    assert out["cash_native_currency"].tolist() == ["USD", "HKD"]
    assert set(out["capacity_source"]) == {"demo_scenario"}


def test_experience_bundle_is_readonly_only_and_non_contributing(tmp_path: Path) -> None:
    _seal_opening_bundle(tmp_path)
    with pytest.raises(CandidateSnapshotManifestError):
        load_candidate_snapshot_bundle(base=tmp_path, run_id=RUN_ID, account=ACCOUNT)
    bundle = load_candidate_snapshot_bundle_readonly(
        base=tmp_path, run_id=RUN_ID, account=ACCOUNT
    )
    assert bundle["manifest"]["scan_mode"] == "experience"
    assert bundle["manifest"]["executable"] is False
    evidence = load_account_candidate_evidence(
        base=tmp_path, run_id=RUN_ID, account=ACCOUNT
    )
    assert evidence.classification["status"] == NON_CONTRIBUTING_EXPERIENCE
    assert evidence.classification["reason_code"] == (
        "experience_candidate_not_executable"
    )
    assert evidence.contributes_evidence is False
    with pytest.raises(RecommendationPointError) as caught:
        capture_scheduled_recommendation_point(
            tmp_path,
            RUN_ID,
            ACCOUNT,
            {},
            source_commit_sha="e" * 40,
        )
    assert caught.value.reason_code == "experience_candidate_not_executable"


def test_experience_bundle_rejects_owner_identity_rebinding(tmp_path: Path) -> None:
    _seal_opening_bundle(tmp_path)
    state_dir = (
        tmp_path / "output_runs" / RUN_ID / "accounts" / ACCOUNT / "state"
    )
    owner_path = state_dir / "opening_candidate_snapshot.json"
    manifest_path = state_dir / "candidate_snapshot_manifest.v2.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["account"] = "other"
    owner["content_sha256"] = canonical_sha256(
        {key: value for key, value in owner.items() if key != "content_sha256"}
    )
    owner_path.write_text(
        json.dumps(owner, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["owner_snapshots"][0]["sha256"] = sha256(
        owner_path.read_bytes()
    ).hexdigest()
    manifest["owner_snapshots"][0]["content_sha256"] = owner[
        "content_sha256"
    ]
    manifest["content_sha256"] = canonical_sha256(
        {key: value for key, value in manifest.items() if key != "content_sha256"}
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CandidateSnapshotManifestError, match="identity mismatch"):
        load_candidate_snapshot_bundle_readonly(
            base=tmp_path,
            run_id=RUN_ID,
            account=ACCOUNT,
        )


def test_experience_bundle_allows_no_supported_strategy_scope(tmp_path: Path) -> None:
    report_dir = tmp_path / "output_runs" / RUN_ID / "accounts" / ACCOUNT
    index = publish_strategy_scan_status_index_v2(
        report_dir=report_dir,
        run_id=RUN_ID,
        account=ACCOUNT,
        account_config_sha256=CONFIG_HASH,
        expected=[],
        experience_fields=experience_fields(DISPLAY_NAME),
    )
    manifest = seal_experience_candidate_bundle(
        base=tmp_path,
        run_id=RUN_ID,
        account=ACCOUNT,
        market="MULTI",
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=_dependency_rows(tmp_path),
        status_index=index,
        statuses_by_owner={},
        opening_candidates={},
        opening_decisions={},
        combo_evidence_by_owner={},
        account_display_name=DISPLAY_NAME,
    )
    assert manifest["markets"] == []
    assert manifest["expected_owners"] == []
    assert load_candidate_snapshot_bundle_readonly(
        base=tmp_path,
        run_id=RUN_ID,
        account=ACCOUNT,
    )["owners"] == {}


def test_experience_combo_snapshot_rejects_candidate_count_mismatch(
    tmp_path: Path,
) -> None:
    _report_dir, index = _status_index(
        tmp_path,
        family="combo_yield",
        owner="sp_lc",
        mode="combo_yield",
        candidate_count=0,
    )
    with pytest.raises(ExperienceCandidateSnapshotError, match="count mismatch"):
        seal_experience_candidate_bundle(
            base=tmp_path,
            run_id=RUN_ID,
            account=ACCOUNT,
            market="US",
            account_config_sha256=CONFIG_HASH,
            strategy_policy_sha256=POLICY_HASH,
            dependencies=_dependency_rows(tmp_path),
            status_index=index,
            statuses_by_owner={
                "sp_lc": [
                    {
                        "symbol": "DEMO",
                        "strategy_mode": "combo_yield",
                        "owner": "sp_lc",
                        "status": "completed",
                        "candidate_count": 0,
                    }
                ]
            },
            opening_candidates={},
            opening_decisions={},
            combo_evidence_by_owner={
                "sp_lc": [{"ranked_pairs": [{"symbol": "DEMO"}]}]
            },
            account_display_name=DISPLAY_NAME,
        )
