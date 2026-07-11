from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_sell_put_scan_writes_candidate_filter_trace(tmp_path: Path) -> None:
    from src.application.scan_sell_put import run_sell_put_scan

    parsed = tmp_path / "input" / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-19",
                "contract_symbol": "PASS",
                "currency": "USD",
                "dte": 30,
                "strike": 100,
                "spot": 110,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "realized_volatility_estimate": 0.2,
                "delta": -0.2,
                "multiplier": 100,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-19",
                "contract_symbol": "FAIL_LIQUIDITY",
                "currency": "USD",
                "dte": 30,
                "strike": 98,
                "spot": 110,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "open_interest": 1,
                "volume": 0,
                "implied_volatility": 0.3,
                "realized_volatility_estimate": 0.2,
                "delta": -0.2,
                "multiplier": 100,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-19",
                "contract_symbol": "FAIL_METRICS",
                "currency": "USD",
                "dte": 30,
                "strike": 96,
                "spot": 110,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 0.0,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "realized_volatility_estimate": 0.2,
                "delta": -0.2,
                "multiplier": 100,
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_path = tmp_path / "reports" / "nvda_sell_put_candidates.csv"
    out = run_sell_put_scan(
        symbols=["NVDA"],
        input_root=tmp_path / "input",
        output=out_path,
        min_dte=7,
        max_dte=60,
        min_annualized_net_return=0.01,
        min_net_income=1,
        min_open_interest=10,
        min_volume=10,
        max_spread_ratio=1.0,
        strategy_family="sell_put",
        strategy_profile="short_vol",
        quiet=True,
    )

    assert list(out["contract_symbol"]) == ["PASS"]
    trace_rows = _read_jsonl(out_path.parent / "candidate_filter_trace.jsonl")
    rules = {row["rule"] for row in trace_rows}
    assert "candidate_accepted" in rules
    assert "risk_open_interest" in rules
    assert "risk_volume" in rules
    assert "metrics_mid_non_positive" in rules
    assert {row["function"] for row in trace_rows} == {"sell_put"}
    assert {row["option_type"] for row in trace_rows} == {"put"}
    assert {row["strategy_family"] for row in trace_rows} == {"sell_put"}
    assert {row["strategy_profile"] for row in trace_rows} == {"short_vol"}
    by_contract = {row["contract_symbol"]: row for row in trace_rows if row.get("contract_symbol")}
    assert by_contract["PASS"]["dte"] == 30
    assert by_contract["PASS"]["delta"] == -0.2
    assert by_contract["PASS"]["abs_delta"] == 0.2
    assert by_contract["PASS"]["iv_rv_ratio"] == 1.5
    assert by_contract["PASS"]["iv_minus_rv"] == 0.1
    assert by_contract["FAIL_LIQUIDITY"]["dte"] == 30
    assert by_contract["FAIL_LIQUIDITY"]["abs_delta"] == 0.2


def test_candidate_scan_traces_missing_required_data_chain(tmp_path: Path) -> None:
    from src.application.scan_sell_call import run_sell_call_scan

    (tmp_path / "input" / "parsed").mkdir(parents=True)
    out_path = tmp_path / "reports" / "nvda_sell_call_candidates.csv"

    out = run_sell_call_scan(
        symbols=["NVDA"],
        input_root=tmp_path / "input",
        output=out_path,
        avg_cost=100,
        shares=100,
        min_annualized_net_return=0.01,
        quiet=True,
    )

    assert out.empty
    trace_rows = _read_jsonl(out_path.parent / "candidate_filter_trace.jsonl")
    assert trace_rows[0]["function"] == "sell_call"
    assert trace_rows[0]["stage"] == "fetch_visibility"
    assert trace_rows[0]["rule"] == "required_data_missing_call_chain"


def test_sell_put_cash_filter_writes_cash_reserve_trace(tmp_path: Path) -> None:
    from src.application.sell_put_steps import _enrich_and_filter_sell_put_cash
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA_PUT",
                "expiration": "2026-06-19",
                "strike": 100,
                "multiplier": 100,
                "currency": "USD",
            }
        ]
    )

    filtered = _enrich_and_filter_sell_put_cash(
        df_labeled=df,
        symbol="NVDA",
        portfolio_ctx={"cash_by_currency": {"USD": 100.0}},
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace_rows = _read_jsonl(out_path.parent / "candidate_filter_trace.jsonl")
    assert trace_rows[0]["function"] == "cash_reserve"
    assert trace_rows[0]["account"] == "lx"
    assert trace_rows[0]["run_id"] == "run-1"
    assert trace_rows[0]["rule"] == "usd_cash_insufficient"


def test_candidate_filter_explain_reads_trace_path(tmp_path: Path) -> None:
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )
    from src.application.tool_execution import execute_tool as run_tool

    trace_path = tmp_path / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="NVDA",
                function="cash_reserve",
                mode="put",
                status="post_filtered",
                stage="post_filter",
                rule="usd_cash_insufficient",
                metric_value=10000,
                threshold=100,
                message="cash not enough",
                evidence_path="nvda_sell_put_candidates_labeled.csv",
            )
        ],
    )

    out = run_tool(
        "candidate_filter_explain",
        {"trace_path": str(trace_path), "account": "lx", "symbol": "NVDA"},
    )

    assert out["ok"] is True
    assert out["data"]["trace_count"] == 1
    cash = next(item for item in out["data"]["functions"] if item["function"] == "cash_reserve")
    assert cash["status"] == "post_filtered"
    assert cash["reason_counts"]["usd_cash_insufficient"] == 1
    assert out["meta"]["source_files"][0]["rows"] == 1


def test_candidate_filter_explain_resolves_symbol_alias_before_matching_trace(tmp_path: Path) -> None:
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )
    from src.application.tool_execution import execute_tool as run_tool

    trace_path = tmp_path / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="sy",
                symbol="9992.HK",
                function="sell_put",
                mode="put",
                status="rejected",
                stage="risk_filter",
                rule="risk_spread",
                metric_value=0.35,
                threshold=0.2,
                message="spread too wide",
                evidence_path="9992_sell_put_candidates_labeled.csv",
            )
        ],
    )

    out = run_tool(
        "candidate_filter_explain",
        {"trace_path": str(trace_path), "symbol": "泡泡玛特"},
    )

    assert out["ok"] is True
    assert out["data"]["raw_symbol"] == "泡泡玛特"
    assert out["data"]["symbol"] == "9992.HK"
    assert out["data"]["canonical_symbol"] == "9992.HK"
    assert out["data"]["trace_count"] == 1
    assert out["data"]["scope"]["account_semantics"] == "scan_scope"
    sell_put = next(item for item in out["data"]["functions"] if item["function"] == "sell_put")
    assert sell_put["status"] == "rejected"
    assert sell_put["reason_counts"]["risk_spread"] == 1


def test_candidate_filter_explain_uses_config_symbol_aliases_before_matching_trace(tmp_path: Path) -> None:
    from src.application.agent_tool_candidate_filter import candidate_filter_explain_tool
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )

    trace_path = tmp_path / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="sy",
                symbol="3690.HK",
                function="sell_put",
                mode="put",
                status="rejected",
                stage="risk_filter",
                rule="risk_delta",
                metric_value=-0.45,
                threshold=-0.3,
                message="delta too high",
                evidence_path="3690_sell_put_candidates_labeled.csv",
            )
        ],
    )

    data, warnings, meta = candidate_filter_explain_tool(
        {"trace_path": str(trace_path), "symbol": "MELIHK"},
        repo_base=lambda: tmp_path,
        mask_path=lambda path: f".../{Path(path).name}" if path else None,
        symbol_aliases={"MELIHK": "3690.HK"},
    )

    assert warnings == []
    assert data["raw_symbol"] == "MELIHK"
    assert data["symbol"] == "3690.HK"
    assert data["canonical_symbol"] == "3690.HK"
    assert data["trace_count"] == 1
    assert meta["source_files"][0]["path"] == ".../candidate_filter_trace.jsonl"
    sell_put = next(item for item in data["functions"] if item["function"] == "sell_put")
    assert sell_put["status"] == "rejected"
    assert sell_put["reason_counts"]["risk_delta"] == 1


def test_candidate_filter_explain_discovers_runtime_last_run_trace(tmp_path: Path) -> None:
    from src.application.agent_tool_candidate_filter import candidate_filter_explain_tool
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )

    runtime = tmp_path / "runtime"
    run_dir = runtime / "output_runs" / "run-hk-1"
    trace_path = run_dir / "accounts" / "sy" / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="run-hk-1",
                account="sy",
                symbol="9992.HK",
                function="sell_put",
                mode="put",
                status="rejected",
                stage="risk_filter",
                rule="risk_spread",
                metric_value=0.35,
                threshold=0.2,
                message="spread too wide",
            )
        ],
    )
    pointer_dir = runtime / "output_shared" / "state"
    pointer_dir.mkdir(parents=True)
    (pointer_dir / "last_run_dir.txt").write_text(str(run_dir), encoding="utf-8")

    data, warnings, meta = candidate_filter_explain_tool(
        {"runtime_root": str(runtime), "symbol": "泡泡玛特"},
        repo_base=lambda: tmp_path / "repo",
        mask_path=lambda path: str(path) if path else None,
    )

    assert warnings == []
    assert data["symbol"] == "9992.HK"
    assert data["trace_count"] == 1
    assert meta["trace_discovery"]["strategy"] == "runtime_roots_latest_runs"
    assert meta["trace_discovery"]["matched_file_count"] == 1
    assert meta["source_files"][0]["path"] == str(trace_path.resolve())


def test_candidate_filter_explain_marks_missing_trace_evidence_indeterminate(tmp_path: Path) -> None:
    from src.application.agent_tool_candidate_filter import candidate_filter_explain_tool

    data, warnings, _meta = candidate_filter_explain_tool(
        {"runtime_root": str(tmp_path / "runtime"), "symbol": "NVDA"},
        repo_base=lambda: tmp_path / "repo",
        mask_path=lambda path: str(path) if path else None,
    )

    assert data["trace_count"] == 0
    assert data["evidence_status"] == "trace_files_missing"
    assert data["conclusion_status"] == "indeterminate"
    assert any(item.startswith("no_trace_files:") for item in warnings)


def test_candidate_filter_explain_discovers_recent_runtime_run_without_pointer(tmp_path: Path) -> None:
    from src.application.agent_tool_candidate_filter import candidate_filter_explain_tool
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )

    runtime = tmp_path / "runtime"
    (runtime / "output_runs" / "old-run" / "accounts" / "sy").mkdir(parents=True)
    trace_path = runtime / "output_runs" / "new-run" / "accounts" / "sy" / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="new-run",
                account="sy",
                symbol="9992.HK",
                function="sell_put",
                mode="put",
                status="post_filtered",
                stage="post_filter",
                rule="hk_cash_insufficient",
                metric_value=5000,
                threshold=10000,
                message="cash not enough",
            )
        ],
    )

    data, warnings, meta = candidate_filter_explain_tool(
        {"runtime_root": str(runtime), "account": "sy", "symbol": "泡泡玛特"},
        repo_base=lambda: tmp_path / "repo",
        mask_path=lambda path: str(path) if path else None,
    )

    assert warnings == []
    assert data["trace_count"] == 1
    assert data["status_counts"] == {"post_filtered": 1}
    assert meta["trace_discovery"]["matched_file_count"] == 1


def test_candidate_filter_explain_infers_runtime_root_from_config_path(tmp_path: Path) -> None:
    from src.application.agent_tool_candidate_filter import candidate_filter_explain_tool
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )

    runtime = tmp_path / "runtime"
    config_path = runtime / "config.hk.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    trace_path = runtime / "output_runs" / "run-from-config" / "accounts" / "sy" / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="run-from-config",
                account="sy",
                symbol="9992.HK",
                function="sell_put",
                mode="put",
                status="rejected",
                stage="risk_filter",
                rule="risk_delta",
                metric_value=-0.42,
                threshold=-0.3,
                message="delta too high",
            )
        ],
    )

    data, warnings, meta = candidate_filter_explain_tool(
        {"config_path": str(config_path), "symbol": "泡泡玛特"},
        repo_base=lambda: tmp_path / "repo",
        mask_path=lambda path: str(path) if path else None,
    )

    assert warnings == []
    assert data["trace_count"] == 1
    assert meta["source_files"][0]["path"] == str(trace_path.resolve())


def test_candidate_filter_explain_uses_config_key_resolved_path_for_trace_discovery(tmp_path: Path) -> None:
    from src.application.agent_tools.candidate import CANDIDATE_FILTER_EXPLAIN_TOOL
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )

    runtime = tmp_path / "runtime"
    config_path = runtime / "config.hk.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    trace_path = runtime / "output_runs" / "run-from-config-key" / "accounts" / "sy" / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="run-from-config-key",
                account="sy",
                symbol="9992.HK",
                function="sell_put",
                mode="put",
                status="rejected",
                stage="risk_filter",
                rule="risk_spread",
                metric_value=0.35,
                threshold=0.2,
                message="spread too wide",
            )
        ],
    )

    class _Ctx:
        def repo_base(self) -> Path:
            return tmp_path / "repo"

        def mask_path(self, value: object) -> str | None:
            return str(value) if value else None

        def load_runtime_config(self, **_kwargs: object) -> tuple[Path, dict[str, object]]:
            return config_path, {}

    data, warnings, meta = CANDIDATE_FILTER_EXPLAIN_TOOL.call(
        _Ctx(),
        {"config_key": "hk", "symbol": "泡泡玛特"},
    )

    assert warnings == []
    assert data["trace_count"] == 1
    assert meta["config_path"] == str(config_path)
    assert meta["source_files"][0]["path"] == str(trace_path.resolve())


def test_symbol_resolve_tool_maps_name_alias_to_canonical_symbol() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool("symbol_resolve", {"symbol": "泡泡玛特"})

    assert out["ok"] is True
    assert out["data"]["resolved"] is True
    assert out["data"]["raw_input"] == "泡泡玛特"
    assert out["data"]["canonical_symbol"] == "9992.HK"
    assert out["data"]["market"] == "HK"
    assert out["data"]["currency"] == "HKD"
    assert out["data"]["futu_code"] == "HK.09992"


def test_candidate_rank_explain_reads_run_account_candidates(tmp_path: Path) -> None:
    from src.application.agent_tool_candidate_rank import candidate_rank_explain_tool

    account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        (
            "symbol,option_type,dte,delta,strike,spot,annualized_net_return_on_cash_basis,"
            "net_income,otm_pct,spread_ratio,open_interest,volume\n"
            "NVDA,put,30,-0.2,100,110,0.12,120,0.09,0.1,500,20\n"
        ),
        encoding="utf-8",
    )

    data, warnings, meta = candidate_rank_explain_tool(
        {"run_id": "run-1", "account": "lx", "mode": "put", "top_n": 5, "compare_baseline": True},
        repo_base=lambda: tmp_path,
        resolve_output_root=lambda _value: tmp_path / "output_shared" / "agent_tools",
        mask_path=lambda path: f".../{Path(path).name}" if path else None,
    )

    assert warnings == []
    assert data["row_count"] == 1
    assert data["source_files"][0]["path"] == ".../nvda_sell_put_candidates_labeled.csv"
    assert data["groups"][0]["baseline"]["name"] == "return_then_income"
    assert meta["source_files"][0]["row_count"] == 1


def test_candidate_rank_explain_uses_underwriting_policy_for_underwriting_csv(tmp_path: Path) -> None:
    from src.application.agent_tool_candidate_rank import candidate_rank_explain_tool

    candidate_path = tmp_path / "sell_put_candidates_labeled.csv"
    candidate_path.write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,max_strike,spot,strategy_profile,"
            "premium_edge_score,strike_safety_margin_pct,net_income,spread_ratio,"
            "annualized_net_return_on_cash_basis,iv_rv_ratio,iv_minus_rv,dte\n"
            "NVDA,put,NVDA_NEAR,2026-06-19,105,110,110,insurance_underwriting,"
            "1.10,0.045455,300,0.05,0.30,1.20,0.08,30\n"
            "NVDA,put,NVDA_SAFE,2026-06-19,95,110,110,insurance_underwriting,"
            "1.10,0.136364,180,0.05,0.12,1.20,0.08,30\n"
        ),
        encoding="utf-8",
    )

    data, warnings, meta = candidate_rank_explain_tool(
        {"candidate_path": str(candidate_path), "mode": "put", "top_n": 2, "compare_baseline": True},
        repo_base=lambda: tmp_path,
        resolve_output_root=lambda _value: tmp_path / "output_shared" / "agent_tools",
        mask_path=lambda path: f".../{Path(path).name}" if path else None,
    )

    assert warnings == []
    assert data["groups"][0]["ranking_policy"] == "insurance_underwriting"
    assert data["ranked"][0]["contract_symbol"] == "NVDA_SAFE"
    assert data["ranked"][0]["ranking_policy"] == "insurance_underwriting"
    assert data["ranked"][0]["score_components"]["strike_safety_margin_pct"] > data["ranked"][1]["score_components"][
        "strike_safety_margin_pct"
    ]


def test_candidate_rank_explain_partitions_mixed_ranking_policies(tmp_path: Path) -> None:
    from src.application.agent_tool_candidate_rank import candidate_rank_explain_tool

    legacy_path = tmp_path / "legacy_sell_put_candidates_labeled.csv"
    underwriting_path = tmp_path / "underwriting_sell_put_candidates_labeled.csv"
    legacy_path.write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,spot,"
            "annualized_net_return_on_cash_basis,net_income,spread_ratio,open_interest,volume,dte,strategy_profile\n"
            "NVDA,put,NVDA_LEGACY,2026-06-19,95,110,0.30,500,0.05,500,20,30,return_first\n"
        ),
        encoding="utf-8",
    )
    underwriting_path.write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,max_strike,spot,strategy_profile,"
            "premium_edge_score,strike_safety_margin_pct,net_income,spread_ratio,"
            "annualized_net_return_on_cash_basis,dte\n"
            "AMD,put,AMD_UW,2026-06-19,80,90,100,insurance_underwriting,"
            "0.10,0.111111,100,0.05,0.10,30\n"
        ),
        encoding="utf-8",
    )

    data, warnings, meta = candidate_rank_explain_tool(
        {
            "candidate_paths": [str(legacy_path), str(underwriting_path)],
            "mode": "put",
            "top_n": 5,
            "score_weights": {"annualized_return": 1.0},
        },
        repo_base=lambda: tmp_path,
        resolve_output_root=lambda _value: tmp_path / "output_shared" / "agent_tools",
        mask_path=lambda path: f".../{Path(path).name}" if path else None,
    )

    groups_by_policy = {group["ranking_policy"]: group for group in data["groups"]}
    assert warnings == []
    assert len(data["groups"]) == 2
    assert groups_by_policy["candidate_engine"]["ranked"][0]["contract_symbol"] == "NVDA_LEGACY"
    assert groups_by_policy["candidate_engine"].get("score_weights_ignored") is None
    assert groups_by_policy["insurance_underwriting"]["ranked"][0]["contract_symbol"] == "AMD_UW"
    assert groups_by_policy["insurance_underwriting"]["score_weights_ignored"] is True
    assert meta["source_files"][0]["row_count"] == 1
    assert meta["source_files"][1]["row_count"] == 1
