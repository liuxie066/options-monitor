from __future__ import annotations

import json
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_shadow_replay_builds_universe_and_analyzes_closed_replay(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        summarize_shadow_replay_readiness,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    candidate_path = account_dir / "nvda_sell_put_candidates_labeled.csv"
    trace_path = account_dir / "candidate_filter_trace.jsonl"
    mark_path = account_dir / "mark_path_snapshots.jsonl"
    outcome_path = account_dir / "outcome_facts.jsonl"
    candidate_path.write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,spot,iv_rv_ratio,"
            "annualized_net_return_on_cash_basis,net_income,otm_pct,spread_ratio,"
            "single_trade_concentration,open_interest,volume\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,110,1.25,"
            "0.12,120,0.09,0.10,0.04,500,20\n"
        ),
        encoding="utf-8",
    )
    trace_path.write_text(
        json.dumps(
            {
                "schema_version": "candidate_filter_trace.v1",
                "run_id": "run-1",
                "account": "lx",
                "symbol": "AMD",
                "contract_symbol": "AMD260619P00080000",
                "function": "sell_put",
                "mode": "put",
                "option_type": "put",
                "expiration": "2026-06-19",
                "strike": 80,
                "dte": 30,
                "delta": -0.28,
                "iv_rv_ratio": 0.9,
                "spread_ratio": 0.45,
                "status": "rejected",
                "stage": "stage3_risk_filter",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mark_path.write_text(
        "\n".join(
            [
                json.dumps({"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-05-31", "unrealized_pnl": 12}),
                json.dumps({"contract_symbol": "AMD260619P00080000", "mark_at": "2026-05-31", "unrealized_pnl": -35}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    outcome_path.write_text(
        "\n".join(
            [
                json.dumps({"contract_symbol": "NVDA260619P00100000", "outcome": "expired_worthless", "realized_pnl": 120}),
                json.dumps({"contract_symbol": "AMD260619P00080000", "outcome": "would_close_loss", "realized_pnl": -80}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-1")
    dataset_dir = Path(manifest["dataset_dir"])
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    readiness = summarize_shadow_replay_readiness(
        candidate_paths=[candidate_path],
        trace_paths=[trace_path],
        mark_paths=[mark_path],
        outcome_paths=[outcome_path],
        base=tmp_path,
        min_sample=2,
    )

    snapshots = _jsonl(dataset_dir / "candidate_snapshots.jsonl")
    assert manifest["schema_version"] == "shadow_replay_dataset.v1"
    assert manifest["summary"]["candidate_snapshot_count"] == 2
    assert manifest["summary"]["rejected_count"] == 1
    assert manifest["summary"]["mark_path_snapshot_count"] == 2
    assert manifest["summary"]["outcome_fact_count"] == 2
    assert {row["status"] for row in snapshots} == {"accepted", "rejected"}
    assert analysis["summary"]["status"] == "needs_human_review"
    assert analysis["summary"]["evidence_level"] == "closed_replay"
    assert analysis["outcome_coverage"]["marked_instrument_count"] == 2
    assert analysis["outcome_coverage"]["outcome_instrument_count"] == 2
    assert analysis["path_risk"]["by_status"]["rejected"]["max_adverse_pnl"] == -35
    assert analysis["outcome_stats"]["by_status"]["accepted"]["realized_pnl_total"] == 120
    assert analysis["outcome_stats"]["by_status"]["rejected"]["win_rate"] == 0
    assert analysis["outcome_by_bucket"]["dte"]["30-44"]["realized_pnl_total"] == 40
    assert analysis["outcome_by_bucket"]["dte"]["30-44"]["by_status"]["accepted"]["realized_pnl_total"] == 120
    assert analysis["outcome_by_bucket"]["spread_ratio"]["0.40+"]["by_status"]["rejected"]["loss_count"] == 1
    assert readiness["summary"]["status"] == "needs_human_review"
    assert readiness["evidence_checks"]["survivorship_bias_risk"] == "low"
    assert readiness["outcome_by_bucket"]["dte"]["30-44"]["win_count"] == 1
    assert readiness["safety"]["writes_runtime_config"] is False


def test_shadow_replay_readiness_flags_final_candidates_only_survivorship_bias(tmp_path: Path) -> None:
    from src.application.shadow_replay import summarize_shadow_replay_readiness

    candidate_path = tmp_path / "sell_put_candidates.csv"
    candidate_path.write_text(
        (
            "symbol,account,option_type,contract_symbol,dte,delta,strike,iv_rv_ratio,spread_ratio\n"
            "NVDA,lx,put,NVDA260619P00100000,30,-0.2,100,1.25,0.10\n"
        ),
        encoding="utf-8",
    )

    readiness = summarize_shadow_replay_readiness(
        candidate_paths=[candidate_path],
        trace_paths=[],
        base=tmp_path,
        min_sample=1,
    )

    assert readiness["summary"]["status"] == "evidence_incomplete"
    assert readiness["summary"]["reason"] == "rejected_universe_missing"
    assert readiness["evidence_checks"]["final_candidates_only"] is True
    assert readiness["evidence_checks"]["survivorship_bias_risk"] == "high"
    assert readiness["recommendations"][0]["writes_runtime_config"] is False


def test_shadow_replay_settle_derives_outcomes_from_mark_path(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,dte,delta,strike,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,30,-0.2,100,120\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "contract_symbol": "AMD260619P00080000",
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (account_dir / "mark_path_snapshots.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"contract_symbol": "NVDA260619P00100000", "mark_at": "2026-05-31", "unrealized_pnl": 15}),
                json.dumps({"contract_symbol": "AMD260619P00080000", "mark_at": "2026-05-31", "unrealized_pnl": -40}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-settle")
    dataset_dir = Path(manifest["dataset_dir"])
    before = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    after = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)

    assert before["summary"]["reason"] == "outcome_facts_missing"
    assert settlement["summary"]["generated_outcome_fact_count"] == 2
    assert settlement["summary"]["written"] is True
    assert after["summary"]["status"] == "needs_human_review"
    assert after["outcome_stats"]["by_status"]["rejected"]["realized_pnl_total"] == -40


def test_shadow_replay_settle_derives_expiration_outcomes_from_spot_marks(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "multiplier": 100,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (account_dir / "mark_path_snapshots.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "contract_symbol": "NVDA260619P00100000",
                        "option_type": "put",
                        "expiration": "2026-06-19",
                        "strike": 100,
                        "spot": 110,
                        "dte": 0,
                        "mark_at": "2026-06-19",
                    }
                ),
                json.dumps(
                    {
                        "contract_symbol": "AMD260619P00080000",
                        "option_type": "put",
                        "expiration": "2026-06-19",
                        "strike": 80,
                        "spot": 70,
                        "dte": 0,
                        "mark_at": "2026-06-19",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-expiry")
    dataset_dir = Path(manifest["dataset_dir"])
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    outcomes = _jsonl(dataset_dir / "outcome_facts.jsonl")

    assert settlement["summary"]["generated_outcome_fact_count"] == 2
    assert {row["outcome"] for row in outcomes} == {"expired_worthless", "assigned_at_expiry"}
    assert {row["quality"] for row in outcomes} == {"derived_from_expiration_spot"}
    assert analysis["summary"]["status"] == "needs_human_review"
    assert analysis["outcome_stats"]["by_status"]["accepted"]["realized_pnl_total"] == 120
    assert analysis["outcome_stats"]["by_status"]["rejected"]["realized_pnl_total"] == -910


def test_shadow_replay_mark_generates_required_data_marks_and_settles(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        mark_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    required_parsed = tmp_path / "output_shared" / "required_data" / "parsed"
    required_parsed.mkdir(parents=True)
    (required_parsed / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,delta,implied_volatility,dte,spot,multiplier\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,0.7,0.9,0.8,-0.2,0.31,30,110,100\n"
        ),
        encoding="utf-8",
    )
    (required_parsed / "AMD_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,delta,implied_volatility,dte,spot,multiplier\n"
            "AMD,put,AMD260619P00080000,2026-06-19,80,1.4,1.8,1.6,-0.28,0.36,30,95,100\n"
        ),
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-mark")
    dataset_dir = Path(manifest["dataset_dir"])
    marking = mark_shadow_replay_dataset(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        as_of="2026-05-31T00:00:00Z",
        repo_root=tmp_path,
        write=True,
    )
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    marks = _jsonl(dataset_dir / "mark_path_snapshots.jsonl")

    assert marking["summary"]["generated_mark_snapshot_count"] == 2
    assert marking["summary"]["usable_mark_snapshot_count"] == 2
    assert marking["summary"]["missing_quote_count"] == 0
    assert {row["matched_by"] for row in marks} == {"contract_symbol"}
    assert marks[0]["quote_status"] == "matched"
    assert marks[0]["quote_flags"] == ["mid_from_bid_ask"]
    assert settlement["summary"]["generated_outcome_fact_count"] == 2
    assert analysis["summary"]["status"] == "needs_human_review"
    assert analysis["outcome_stats"]["by_status"]["accepted"]["realized_pnl_total"] == 40
    assert analysis["outcome_stats"]["by_status"]["rejected"]["realized_pnl_total"] == -70


def test_shadow_replay_collect_marks_fetches_opend_before_marking(monkeypatch, tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset, collect_shadow_replay_marks
    import src.application.shadow_replay.collection as collection

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    calls = []

    def _fake_execute_required_data_opend(*, base: Path, request):
        calls.append(request)
        contract = "NVDA260619P00100000" if request.symbol == "NVDA" else "AMD260619P00080000"
        strike = 100 if request.symbol == "NVDA" else 80
        bid = 0.7 if request.symbol == "NVDA" else 1.4
        ask = 0.9 if request.symbol == "NVDA" else 1.8
        return {
            "symbol": request.symbol,
            "expiration_count": 1,
            "expirations": ["2026-06-19"],
            "rows": [
                {
                    "symbol": request.symbol,
                    "option_type": "put",
                    "contract_symbol": contract,
                    "expiration": "2026-06-19",
                    "strike": strike,
                    "bid": bid,
                    "ask": ask,
                    "last_price": (bid + ask) / 2,
                    "dte": 30,
                    "spot": 110,
                    "multiplier": 100,
                }
            ],
            "meta": {"source": "opend", "status": "ok"},
        }

    monkeypatch.setattr(collection, "execute_required_data_opend", _fake_execute_required_data_opend)

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-collect")
    dataset_dir = Path(manifest["dataset_dir"])
    result = collect_shadow_replay_marks(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        source="opend",
        repo_root=tmp_path,
        as_of="2026-05-31T00:00:00Z",
        write=True,
    )
    marks = _jsonl(dataset_dir / "mark_path_snapshots.jsonl")
    outcomes = _jsonl(dataset_dir / "outcome_facts.jsonl")

    assert [request.symbol for request in calls] == ["AMD", "NVDA"]
    assert {tuple(request.explicit_expirations or []) for request in calls} == {("2026-06-19",)}
    assert result["summary"]["opend_fetch_ok_count"] == 2
    assert result["summary"]["generated_mark_snapshot_count"] == 2
    assert result["summary"]["usable_mark_snapshot_count"] == 2
    assert result["summary"]["settled"] is False
    assert result["summary"]["generated_outcome_fact_count"] == 0
    assert result["safety"]["reads_opend"] is True
    assert result["safety"]["writes_required_data_cache"] is True
    assert result["safety"]["writes_persistent_outputs"] is True
    assert result["safety"]["persistent_write_targets"] == [
        "shadow_replay_dataset",
        "required_data_cache",
        "opend_rate_limit_state",
        "opend_cache",
    ]
    assert {row["quote_status"] for row in marks} == {"matched"}
    assert outcomes == []
    assert (tmp_path / "output_shared" / "required_data" / "parsed" / "NVDA_required_data.csv").exists()


def test_shadow_replay_collect_marks_opend_preview_does_not_persist(monkeypatch, tmp_path: Path) -> None:
    from src.application.shadow_replay import build_shadow_replay_dataset, collect_shadow_replay_marks
    import src.application.shadow_replay.collection as collection

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
        ),
        encoding="utf-8",
    )

    fetch_bases = []

    def _fake_execute_required_data_opend(*, base: Path, request):
        fetch_bases.append(Path(base))
        return {
            "symbol": request.symbol,
            "expiration_count": 1,
            "expirations": ["2026-06-19"],
            "rows": [
                {
                    "symbol": request.symbol,
                    "option_type": "put",
                    "contract_symbol": "NVDA260619P00100000",
                    "expiration": "2026-06-19",
                    "strike": 100,
                    "bid": 0.7,
                    "ask": 0.9,
                    "last_price": 0.8,
                    "dte": 30,
                    "spot": 110,
                    "multiplier": 100,
                }
            ],
            "meta": {"source": "opend", "status": "ok"},
        }

    monkeypatch.setattr(collection, "execute_required_data_opend", _fake_execute_required_data_opend)

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-preview")
    dataset_dir = Path(manifest["dataset_dir"])
    result = collect_shadow_replay_marks(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        source="opend",
        repo_root=tmp_path,
        as_of="2026-05-31T00:00:00Z",
        write=False,
    )

    assert result["summary"]["opend_fetch_attempted"] is True
    assert result["summary"]["opend_fetch_persisted"] is False
    assert result["summary"]["generated_mark_snapshot_count"] == 1
    assert result["safety"]["writes_required_data_cache"] is False
    assert result["safety"]["writes_persistent_outputs"] is False
    assert result["safety"]["persistent_write_targets"] == []
    assert fetch_bases and fetch_bases[0] != tmp_path
    assert not fetch_bases[0].exists()
    assert _jsonl(dataset_dir / "mark_path_snapshots.jsonl") == []
    assert not (tmp_path / "output_shared" / "required_data").exists()


def test_shadow_replay_mark_uses_expiration_spot_when_mid_is_missing(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        mark_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income,multiplier\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120,100\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "net_income": 90,
                "multiplier": 100,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    required_parsed = tmp_path / "output_shared" / "required_data" / "parsed"
    required_parsed.mkdir(parents=True)
    (required_parsed / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,dte,spot,multiplier\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,0,110,100\n"
        ),
        encoding="utf-8",
    )
    (required_parsed / "AMD_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,dte,spot,multiplier\n"
            "AMD,put,AMD260619P00080000,2026-06-19,80,0,70,100\n"
        ),
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-expiry-mark")
    dataset_dir = Path(manifest["dataset_dir"])
    marking = mark_shadow_replay_dataset(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        as_of="2026-06-19",
        repo_root=tmp_path,
        write=True,
    )
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    marks = _jsonl(dataset_dir / "mark_path_snapshots.jsonl")

    assert marking["summary"]["usable_mark_snapshot_count"] == 2
    assert {row["mark_quality"] for row in marks} == {"expiration_spot"}
    assert {row["pnl_outcome"] for row in marks} == {"expired_worthless", "assigned_at_expiry"}
    assert settlement["summary"]["generated_outcome_fact_count"] == 2
    assert analysis["outcome_stats"]["by_status"]["accepted"]["realized_pnl_total"] == 120
    assert analysis["outcome_stats"]["by_status"]["rejected"]["realized_pnl_total"] == -910


def test_shadow_replay_mark_missing_quote_is_not_usable_evidence(tmp_path: Path) -> None:
    from src.application.shadow_replay import (
        analyze_shadow_replay_dataset,
        build_shadow_replay_dataset,
        mark_shadow_replay_dataset,
        settle_shadow_replay_dataset,
    )

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "sell_put_candidates.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,expiration,dte,delta,strike,net_income\n"
            "NVDA,lx,put,NVDA260619P00100000,2026-06-19,30,-0.2,100,120\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    required_parsed = tmp_path / "output_shared" / "required_data" / "parsed"
    required_parsed.mkdir(parents=True)
    (required_parsed / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,mid\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,0,0,0,0\n"
        ),
        encoding="utf-8",
    )

    manifest = build_shadow_replay_dataset(repo_root=tmp_path, run_id="run-1", dataset_id="case-missing-mark")
    dataset_dir = Path(manifest["dataset_dir"])
    marking = mark_shadow_replay_dataset(
        dataset=dataset_dir,
        required_data_root=tmp_path / "output_shared" / "required_data",
        repo_root=tmp_path,
        write=True,
    )
    settlement = settle_shadow_replay_dataset(dataset=dataset_dir, write=True)
    analysis = analyze_shadow_replay_dataset(dataset=dataset_dir, min_sample=2)
    marks = _jsonl(dataset_dir / "mark_path_snapshots.jsonl")

    assert marking["summary"]["generated_mark_snapshot_count"] == 2
    assert marking["summary"]["usable_mark_snapshot_count"] == 0
    assert marking["summary"]["missing_quote_count"] == 1
    assert {row["quote_status"] for row in marks} == {"matched", "missing_quote"}
    assert {row["mark_quality"] for row in marks} == {"missing_mid", "missing_quote"}
    assert settlement["summary"]["generated_outcome_fact_count"] == 0
    assert analysis["summary"]["status"] == "not_ready"
    assert analysis["summary"]["reason"] == "usable_mark_path_snapshots_missing"
    assert analysis["outcome_coverage"]["usable_marked_instrument_count"] == 0


def test_shadow_replay_pipeline_stays_split_by_stage() -> None:
    module_dir = BASE / "src" / "application" / "shadow_replay"
    assert {
        "capture.py",
        "marking.py",
        "settlement.py",
        "analysis.py",
        "readiness.py",
    }.issubset({path.name for path in module_dir.glob("*.py")})

    facade = (module_dir / "evidence.py").read_text(encoding="utf-8")
    assert len(facade.splitlines()) <= 80
    assert "CandidateScoreWeights" not in facade
    assert "read_candidate_filter_trace" not in facade
    assert "load_runtime_symbol_aliases" not in facade
