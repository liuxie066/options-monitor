from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


def test_strategy_lab_contract_rejects_standard_ratio_metrics_without_equity_curve() -> None:
    from src.application.strategy_lab import MetricSet, validate_metric_names

    with pytest.raises(ValueError, match="trusted equity curve"):
        validate_metric_names(["sharpe_ratio"])

    with pytest.raises(ValueError, match="trusted equity curve"):
        MetricSet(risk={"sortino_ratio": 1.2})


def test_strategy_lab_backtest_result_contract_round_trips() -> None:
    from src.application.strategy_lab import (
        BacktestResult,
        MetricSet,
        StrategyExperiment,
        StrategyLabEvidence,
        StrategyPolicy,
    )

    baseline = StrategyPolicy(name="baseline", strategy_type="put", params={"delta_min": 0.1})
    candidate = StrategyPolicy(name="candidate", strategy_type="sell_put", params={"delta_min": 0.15})
    experiment = StrategyExperiment(
        experiment_id="exp_1",
        strategy_type="sell-put",
        account="sy",
        baseline_policy=baseline,
        candidate_policy=candidate,
        requested_metrics=("net_cash_inflow", "return_per_locked_cash_day"),
    )
    result = BacktestResult(
        experiment=experiment,
        baseline_metrics=MetricSet(returns={"net_cash_inflow": 100.0}),
        candidate_metrics=MetricSet(returns={"net_cash_inflow": 120.0}),
        comparison={"net_cash_inflow_lift": 20.0},
        conclusion="shadow",
        evidence=StrategyLabEvidence(),
    )

    assert result.to_dict()["strategy_type"] == "sell_put"
    assert result.to_dict()["conclusion"] == "shadow"
    assert result.to_dict()["comparison"] == {"net_cash_inflow_lift": 20.0}


def test_strategy_lab_loader_keeps_candidates_and_reject_logs_separate(tmp_path: Path) -> None:
    from src.application.strategy_lab import load_strategy_lab_evidence

    candidate_path = tmp_path / "sell_put_candidates.csv"
    candidate_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,selected\n"
        "NVDA,sy,put,short,900,2026-06-19,26,12.3,-0.18,true\n",
        encoding="utf-8",
    )
    reject_path = tmp_path / "sell_put_candidates_reject_log.csv"
    reject_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,engine_reject_reason\n"
        "TSLA,sy,put,short,200,2026-06-19,26,5.1,-0.32,risk_spread\n",
        encoding="utf-8",
    )

    evidence = load_strategy_lab_evidence(
        candidate_paths=[candidate_path],
        reject_log_paths=[reject_path],
        base=tmp_path,
    )

    assert evidence.summary()["candidate_count"] == 1
    assert evidence.summary()["reject_log_count"] == 1
    assert evidence.candidates[0].symbol == "NVDA"
    assert evidence.candidates[0].strategy_type == "sell_put"
    assert evidence.candidates[0].selected is True
    assert evidence.candidates[0].contracts == 1
    assert evidence.candidates[0].multiplier is None
    assert evidence.candidates[0].locked_cash is None
    assert evidence.reject_logs[0].symbol == "TSLA"
    assert evidence.reject_logs[0].reject_reasons == ("risk_spread",)
    assert evidence.reject_logs[0].evidence_ref.kind == "reject_log"


def test_strategy_lab_loader_reads_trace_and_replay_rows(tmp_path: Path) -> None:
    from src.application.strategy_lab import load_strategy_lab_evidence

    trace_path = tmp_path / "candidate_filter_trace.jsonl"
    trace_path.write_text('{"symbol":"NVDA","stage":"rank"}\n', encoding="utf-8")
    replay_path = tmp_path / "strategy_replay.json"
    replay_path.write_text('{"rows":[{"symbol":"NVDA","actual_return":0.04}]}', encoding="utf-8")

    evidence = load_strategy_lab_evidence(
        trace_paths=[trace_path],
        replay_paths=[replay_path],
        base=tmp_path,
    )

    assert evidence.summary()["trace_count"] == 1
    assert evidence.summary()["replay_row_count"] == 1
    assert evidence.traces[0]["stage"] == "rank"
    assert evidence.replay_rows[0]["actual_return"] == 0.04


def test_strategy_lab_loader_normalizes_first_party_sell_put_candidate_files(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
        load_strategy_lab_evidence,
        run_replay_backtest,
    )

    candidate_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    candidate_dir.mkdir(parents=True)
    candidate_path = candidate_dir / "pdd_sell_put_candidates.csv"
    candidate_path.write_text(
        "symbol,expiration,dte,contract_symbol,multiplier,currency,strike,bid,mid,net_income,cash_basis,delta\n"
        "PDD,2026-06-12,21,US.PDD260612P85000,100.0,USD,85.0,0.78,0.865,84.16085,8415.83915,-0.157842633\n",
        encoding="utf-8",
    )
    evidence = load_strategy_lab_evidence(candidate_paths=[candidate_path], base=tmp_path)
    row = evidence.candidates[0]

    assert row.account == "lx"
    assert row.strategy_type == "sell_put"
    assert row.option_type == "put"
    assert row.side == "short"
    assert row.contracts == 1
    assert row.locked_cash == 8415.83915

    experiment = StrategyExperiment(
        experiment_id="first_party_sell_put",
        strategy_type="sell_put",
        account="lx",
        baseline_policy=StrategyPolicy(
            name="baseline",
            strategy_type="sell_put",
            params={"selection_source": "existing", "min_sample": 1},
        ),
        candidate_policy=StrategyPolicy(
            name="candidate",
            strategy_type="sell_put",
            params={"selection_source": "rules", "min_sample": 1},
        ),
    )
    result = run_replay_backtest(experiment, evidence)

    assert result.candidate_metrics.execution["candidate_count"] == 1
    assert result.candidate_metrics.execution["selected_count"] == 1
    assert result.candidate_metrics.returns["net_cash_inflow"] == 84.16085
    assert result.candidate_metrics.capital["locked_cash_days"] == 8415.83915 * 21
    assert "candidate_evidence_empty" not in result.warnings


def test_strategy_lab_historical_snapshot_cache_round_trips_under_repo(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        HistoricalBar,
        HistoricalDataCache,
        HistoricalDataRequest,
        build_historical_data_snapshot,
    )

    request = HistoricalDataRequest(
        symbols=("nvda",),
        start_date="2026-05-01",
        end_date="2026-05-03",
        provider="manual",
        fields=("close", "volume"),
    )
    snapshot = build_historical_data_snapshot(
        request=request,
        source="manual",
        generated_at="2026-05-24T08:00:00Z",
        bars=[
            HistoricalBar(symbol="NVDA", timestamp="2026-05-02", close=910, volume=1000, source="manual"),
            HistoricalBar(symbol="NVDA", timestamp="2026-05-01", close=900, volume=1200, source="manual"),
        ],
    )
    cache = HistoricalDataCache(base=tmp_path)

    path = cache.write_snapshot(snapshot)
    loaded = cache.read_snapshot(path)

    assert cache.relative(path).startswith("output_shared/strategy_lab/historical_data/manual-")
    assert loaded.request.symbols == ("NVDA",)
    assert loaded.request.fingerprint == request.fingerprint
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert [bar.timestamp for bar in loaded.bars] == ["2026-05-01", "2026-05-02"]
    assert loaded.to_dict()["schema_version"] == "strategy_lab_historical_data.v1"
    assert loaded.to_dict()["summary"]["bar_count"] == 2


def test_strategy_lab_historical_snapshot_warns_on_missing_symbols() -> None:
    from src.application.strategy_lab import HistoricalBar, HistoricalDataRequest, build_historical_data_snapshot

    snapshot = build_historical_data_snapshot(
        request=HistoricalDataRequest(symbols=("NVDA", "MSFT"), start_date="2026-05-01", end_date="2026-05-02"),
        source="manual",
        generated_at="2026-05-24T08:00:00Z",
        bars=[HistoricalBar(symbol="NVDA", timestamp="2026-05-01", close=900)],
    )

    assert "historical_data_missing_symbol:MSFT" in snapshot.warnings
    assert snapshot.to_dict()["summary"]["bar_counts_by_symbol"] == {"NVDA": 1}


def test_strategy_lab_historical_cache_rejects_output_dir_outside_repo(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.strategy_lab import HistoricalDataCache

    with pytest.raises(AgentToolError, match="historical data cache must stay under the repo root"):
        HistoricalDataCache(base=tmp_path, cache_dir=tmp_path.parent / "outside-historical-cache")


def test_strategy_lab_historical_fetch_dry_run_does_not_call_provider(tmp_path: Path) -> None:
    from src.application.strategy_lab import fetch_historical_data_tool

    class ExplodingProvider:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("dry-run must not build provider")

    data, warnings, meta = fetch_historical_data_tool(
        {
            "provider": "futu",
            "symbols": "nvda,0700.HK",
            "start_date": "2026-05-01",
            "end_date": "2026-05-03",
            "timeframe": "1d",
        },
        base=tmp_path,
        provider_factory=ExplodingProvider,
    )

    assert data["schema_version"] == "strategy_lab_historical_fetch.v1"
    assert data["dry_run"] is True
    assert data["write_applied"] is False
    assert data["request"]["symbols"] == ["NVDA", "0700.HK"]
    assert data["output"]["snapshot_path"].startswith("output_shared/strategy_lab/historical_data/futu-")
    assert "historical_fetch_dry_run_no_opend_call" in warnings
    assert meta["base"] == f".../{tmp_path.name}"
    assert not (tmp_path / "output_shared").exists()


def test_strategy_lab_historical_fetch_writes_frozen_snapshot_with_provider(tmp_path: Path) -> None:
    from src.application.strategy_lab import HistoricalBar, build_historical_data_snapshot, fetch_historical_data_tool

    class FakeProvider:
        def __init__(self, *, base, options) -> None:
            self.base = base
            self.options = options

        def fetch(self, request):
            assert request.symbols == ("NVDA",)
            assert self.options.host == "127.0.0.9"
            assert self.options.port == 11119
            return build_historical_data_snapshot(
                request=request,
                source="futu",
                generated_at="2026-05-24T08:00:00Z",
                bars=[
                    HistoricalBar(symbol="NVDA", timestamp="2026-05-01", open=900, high=910, low=890, close=905, volume=1000, source="futu"),
                    HistoricalBar(symbol="NVDA", timestamp="2026-05-02", open=905, high=915, low=901, close=912, volume=1200, source="futu"),
                ],
            )

    data, warnings, _meta = fetch_historical_data_tool(
        {
            "provider": "futu",
            "symbols": "NVDA",
            "start_date": "2026-05-01",
            "end_date": "2026-05-03",
            "host": "127.0.0.9",
            "port": 11119,
            "confirm": True,
        },
        base=tmp_path,
        provider_factory=FakeProvider,
    )

    assert warnings == []
    assert data["dry_run"] is False
    assert data["write_applied"] is True
    assert data["snapshot"]["bar_count"] == 2
    snapshot_path = tmp_path / data["output"]["snapshot_path"]
    assert snapshot_path.exists()
    payload = snapshot_path.read_text(encoding="utf-8")
    assert '"schema_version": "strategy_lab_historical_data.v1"' in payload
    assert '"source": "futu"' in payload


def test_strategy_lab_futu_historical_provider_fetches_pages_and_maps_rows(tmp_path: Path) -> None:
    from src.application.strategy_lab import FutuHistoricalFetchOptions, FutuHistoricalMarketDataProvider, HistoricalDataRequest

    class FakeGateway:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.closed = False

        def request_history_kline(self, **kwargs):
            self.calls.append(dict(kwargs))
            if len(self.calls) == 1:
                return {
                    "data": [
                        {
                            "code": "US.NVDA",
                            "time_key": "2026-05-01 00:00:00",
                            "open": "900",
                            "high": "910",
                            "low": "890",
                            "close": "905",
                            "volume": "1000",
                        }
                    ],
                    "page_req_key": "next-page",
                }
            return {
                "data": [
                    {
                        "code": "US.NVDA",
                        "time_key": "2026-05-02 00:00:00",
                        "open": 905,
                        "high": 915,
                        "low": 901,
                        "close": 912,
                        "volume": 1200,
                    }
                ],
                "page_req_key": None,
            }

        def close(self) -> None:
            self.closed = True

    gateway = FakeGateway()
    provider = FutuHistoricalMarketDataProvider(
        base=tmp_path,
        options=FutuHistoricalFetchOptions(no_retry=True, max_pages=3),
        gateway_factory=lambda **_kwargs: gateway,
        retry_call=lambda _what, fn, **_kwargs: fn(),
        rate_limited_call=lambda **kwargs: kwargs["call"](),
    )
    snapshot = provider.fetch(
        HistoricalDataRequest(
            symbols=("NVDA",),
            start_date="2026-05-01",
            end_date="2026-05-03",
            adjusted=True,
        )
    )

    assert snapshot.source == "futu"
    assert snapshot.request.symbols == ("NVDA",)
    assert [bar.close for bar in snapshot.bars] == [905.0, 912.0]
    assert gateway.calls[0]["code"] == "US.NVDA"
    assert gateway.calls[0]["ktype"] == "K_DAY"
    assert gateway.calls[0]["autype"] == "QFQ"
    assert gateway.calls[1]["page_req_key"] == "next-page"
    assert gateway.closed is True


def test_strategy_lab_replay_backtest_compares_sell_put_policies(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
        load_strategy_lab_evidence,
        run_replay_backtest,
    )

    candidate_path = tmp_path / "sell_put_candidates.csv"
    candidate_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,selected,multiplier,contracts\n"
        "NVDA,sy,put,short,900,2026-06-19,26,12,-0.18,true,100,1\n"
        "MSFT,sy,put,short,400,2026-06-19,26,4,-0.12,true,100,1\n",
        encoding="utf-8",
    )
    reject_path = tmp_path / "sell_put_candidates_reject_log.csv"
    reject_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,engine_reject_reason,multiplier,contracts\n"
        "TSLA,sy,put,short,200,2026-06-19,26,8,-0.30,risk_spread,100,1\n",
        encoding="utf-8",
    )
    evidence = load_strategy_lab_evidence(candidate_paths=[candidate_path], reject_log_paths=[reject_path], base=tmp_path)
    experiment = StrategyExperiment(
        experiment_id="exp_sell_put_1",
        strategy_type="sell_put",
        account="sy",
        baseline_policy=StrategyPolicy(
            name="baseline",
            strategy_type="sell_put",
            params={"selection_source": "existing", "min_sample": 1},
        ),
        candidate_policy=StrategyPolicy(
            name="candidate",
            strategy_type="sell_put",
            params={
                "selection_source": "rules",
                "min_dte": 20,
                "max_dte": 35,
                "min_abs_delta": 0.10,
                "max_abs_delta": 0.35,
                "min_premium": 4,
                "max_candidates": 3,
                "min_sample": 1,
            },
        ),
    )

    result = run_replay_backtest(experiment, evidence)

    assert result.baseline_metrics.execution["selected_count"] == 2
    assert result.candidate_metrics.execution["selected_count"] == 2
    assert result.candidate_metrics.execution["reject_log_count"] == 1
    assert result.candidate_metrics.execution["reject_reason_distribution"] == {"risk_spread": 1}
    assert result.candidate_metrics.returns["net_cash_inflow"] == 1600
    assert result.candidate_metrics.capital["locked_cash_days"] == 3380000
    assert result.candidate_metrics.capital["return_per_locked_cash_day"] == 1600 / 3380000
    assert result.candidate_metrics.risk["tail_loss_scenario"] == -128400
    assert result.comparison["selected_count_lift"] == 0
    assert result.conclusion == "reject"


def test_strategy_lab_replay_does_not_promote_reject_logs_to_candidates(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
        build_strategy_lab_report,
        load_strategy_lab_evidence,
        run_replay_backtest,
    )

    candidate_path = tmp_path / "sell_put_candidates.csv"
    candidate_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,selected\n",
        encoding="utf-8",
    )
    reject_path = tmp_path / "sell_put_candidates_reject_log.csv"
    reject_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,engine_reject_reason,multiplier,contracts\n"
        "TSLA,sy,put,short,200,2026-06-19,26,8,-0.30,risk_spread,100,1\n",
        encoding="utf-8",
    )
    evidence = load_strategy_lab_evidence(candidate_paths=[candidate_path], reject_log_paths=[reject_path], base=tmp_path)
    experiment = StrategyExperiment(
        experiment_id="exp_reject_logs_are_diagnostics",
        strategy_type="sell_put",
        account="sy",
        baseline_policy=StrategyPolicy(name="baseline", strategy_type="sell_put", params={"selection_source": "existing"}),
        candidate_policy=StrategyPolicy(name="candidate", strategy_type="sell_put", params={"selection_source": "rules"}),
    )

    result = run_replay_backtest(experiment, evidence)
    report = build_strategy_lab_report(result)

    assert result.evidence.summary()["candidate_count"] == 0
    assert result.evidence.summary()["reject_log_count"] == 1
    assert result.candidate_metrics.execution["candidate_count"] == 0
    assert result.candidate_metrics.execution["selected_count"] == 0
    assert result.candidate_metrics.execution["reject_reason_distribution"] == {"risk_spread": 1}
    assert "candidate_evidence_empty" in result.warnings
    assert result.conclusion == "reject"
    assert "可评估候选行：0" in report.markdown
    assert "资金效率不可用原因：无已选候选" in report.markdown
    assert "当前没有可评估候选行" in report.markdown


def test_strategy_lab_replay_backtest_supports_sell_call_with_explicit_capital(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
        load_strategy_lab_evidence,
        run_replay_backtest,
    )

    candidate_path = tmp_path / "sell_call_candidates.csv"
    candidate_path.write_text(
        "symbol,account,strategy,option_type,side,strike,expiry,dte,premium,delta,selected,multiplier,contracts,capital_basis,worst_trade_pnl\n"
        "NVDA,sy,sell_call,call,short,1100,2026-06-19,26,7,0.18,true,100,1,90000,-3000\n"
        "MSFT,sy,sell_call,call,short,460,2026-06-19,26,3,0.12,false,100,1,40000,-1000\n",
        encoding="utf-8",
    )
    evidence = load_strategy_lab_evidence(candidate_paths=[candidate_path], base=tmp_path)
    experiment = StrategyExperiment(
        experiment_id="exp_sell_call_1",
        strategy_type="sell_call",
        account="sy",
        baseline_policy=StrategyPolicy(
            name="baseline",
            strategy_type="sell_call",
            params={"selection_source": "existing", "min_sample": 1},
        ),
        candidate_policy=StrategyPolicy(
            name="candidate",
            strategy_type="sell_call",
            params={"selection_source": "rules", "min_premium": 3, "min_sample": 1},
        ),
    )

    result = run_replay_backtest(experiment, evidence)

    assert result.baseline_metrics.execution["selected_count"] == 1
    assert result.candidate_metrics.execution["selected_count"] == 2
    assert result.candidate_metrics.returns["net_cash_inflow"] == 1000
    assert result.candidate_metrics.capital["locked_cash_days"] == 3380000
    assert result.candidate_metrics.risk["tail_loss_scenario"] == -4000
    assert "candidate_capital_basis_unavailable" not in result.warnings


def test_strategy_lab_replay_backtest_does_not_fake_sell_call_capital_basis(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
        load_strategy_lab_evidence,
        run_replay_backtest,
    )

    candidate_path = tmp_path / "sell_call_candidates.csv"
    candidate_path.write_text(
        "symbol,account,strategy,option_type,side,strike,expiry,dte,premium,delta,selected,multiplier,contracts\n"
        "NVDA,sy,sell_call,call,short,1100,2026-06-19,26,7,0.18,true,100,1\n",
        encoding="utf-8",
    )
    evidence = load_strategy_lab_evidence(candidate_paths=[candidate_path], base=tmp_path)
    experiment = StrategyExperiment(
        experiment_id="exp_sell_call_no_capital",
        strategy_type="sell_call",
        account="sy",
        baseline_policy=StrategyPolicy(name="baseline", strategy_type="sell_call", params={"selection_source": "existing", "min_sample": 1}),
        candidate_policy=StrategyPolicy(name="candidate", strategy_type="sell_call", params={"selection_source": "rules", "min_sample": 1}),
    )

    result = run_replay_backtest(experiment, evidence)

    assert result.candidate_metrics.capital["locked_cash_days"] is None
    assert result.candidate_metrics.capital["return_per_locked_cash_day"] is None
    assert "candidate_capital_basis_unavailable" in result.warnings


def test_strategy_lab_replay_backtest_supports_close_advice_realized_pnl(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
        load_strategy_lab_evidence,
        run_replay_backtest,
    )

    candidate_path = tmp_path / "close_advice.csv"
    candidate_path.write_text(
        "symbol,account,strategy,dte,selected,realized_if_close\n"
        "NVDA,sy,close_advice,12,true,120\n"
        "MSFT,sy,close_advice,18,false,80\n",
        encoding="utf-8",
    )
    evidence = load_strategy_lab_evidence(candidate_paths=[candidate_path], base=tmp_path)
    experiment = StrategyExperiment(
        experiment_id="exp_close_advice_1",
        strategy_type="close_advice",
        account="sy",
        baseline_policy=StrategyPolicy(name="baseline", strategy_type="close_advice", params={"selection_source": "existing", "min_sample": 1}),
        candidate_policy=StrategyPolicy(name="candidate", strategy_type="close_advice", params={"selection_source": "rules", "min_sample": 1}),
    )

    result = run_replay_backtest(experiment, evidence)

    assert result.baseline_metrics.execution["selected_count"] == 1
    assert result.candidate_metrics.execution["selected_count"] == 2
    assert result.baseline_metrics.returns["realized_pnl"] == 120
    assert result.candidate_metrics.returns["realized_pnl"] == 200
    assert result.comparison["realized_pnl_lift"] == 80


def test_strategy_lab_replay_backtest_supports_yield_enhancement_snapshots(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
        load_strategy_lab_evidence,
        run_replay_backtest,
    )

    candidate_path = tmp_path / "yield_enhancement_candidates.csv"
    candidate_path.write_text(
        "symbol,account,strategy,dte,premium,delta,selected,multiplier,contracts,capital_basis,worst_trade_pnl\n"
        "NVDA,sy,yield_enhancement,26,5,0.12,true,100,1,90000,-2500\n"
        "MSFT,sy,yield_enhancement,26,2,0.08,false,100,1,40000,-800\n",
        encoding="utf-8",
    )
    evidence = load_strategy_lab_evidence(candidate_paths=[candidate_path], base=tmp_path)
    experiment = StrategyExperiment(
        experiment_id="exp_yield_enhancement_1",
        strategy_type="yield_enhancement",
        account="sy",
        baseline_policy=StrategyPolicy(name="baseline", strategy_type="yield_enhancement", params={"selection_source": "existing", "min_sample": 1}),
        candidate_policy=StrategyPolicy(name="candidate", strategy_type="yield_enhancement", params={"selection_source": "rules", "min_sample": 1}),
    )

    result = run_replay_backtest(experiment, evidence)

    assert result.baseline_metrics.execution["selected_count"] == 1
    assert result.candidate_metrics.execution["selected_count"] == 2
    assert result.candidate_metrics.returns["net_cash_inflow"] == 700
    assert result.candidate_metrics.capital["locked_cash_days"] == 3380000
    assert result.candidate_metrics.risk["tail_loss_scenario"] == -3300


def test_strategy_lab_report_renders_stable_sections_without_pseudo_standard_ratios(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
        build_strategy_lab_report,
        load_strategy_lab_evidence,
        run_replay_backtest,
    )

    candidate_path = tmp_path / "sell_put_candidates.csv"
    candidate_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,selected,multiplier,contracts\n"
        "NVDA,sy,put,short,900,2026-06-19,26,12,-0.18,true,100,1\n"
        "MSFT,sy,put,short,400,2026-06-19,26,4,-0.12,true,100,1\n",
        encoding="utf-8",
    )
    reject_path = tmp_path / "sell_put_candidates_reject_log.csv"
    reject_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,engine_reject_reason,multiplier,contracts\n"
        "TSLA,sy,put,short,200,2026-06-19,26,8,-0.30,risk_spread,100,1\n",
        encoding="utf-8",
    )
    evidence = load_strategy_lab_evidence(candidate_paths=[candidate_path], reject_log_paths=[reject_path], base=tmp_path)
    experiment = StrategyExperiment(
        experiment_id="exp_sell_put_report",
        strategy_type="sell_put",
        account="sy",
        baseline_policy=StrategyPolicy(
            name="baseline",
            strategy_type="sell_put",
            params={"selection_source": "existing", "min_sample": 1},
        ),
        candidate_policy=StrategyPolicy(
            name="candidate",
            strategy_type="sell_put",
            params={
                "selection_source": "rules",
                "min_dte": 20,
                "max_dte": 35,
                "min_abs_delta": 0.10,
                "max_abs_delta": 0.35,
                "min_premium": 4,
                "max_candidates": 3,
                "min_sample": 1,
            },
        ),
    )

    report = build_strategy_lab_report(run_replay_backtest(experiment, evidence))

    assert report.summary["experiment_id"] == "exp_sell_put_report"
    assert report.summary["baseline_sample_size"] == 2
    assert report.summary["candidate_sample_size"] == 2
    assert report.summary["risk_worsening"] is False
    assert report.summary["evidence_summary"]["candidate_count"] == 2
    assert report.summary["evidence_summary"]["reject_log_count"] == 1
    for section in (
        "## 结论",
        "## 实验范围",
        "## 证据诊断",
        "## Baseline 指标",
        "## Candidate 指标",
        "## 收益差异",
        "## 风险差异",
        "## 资金效率差异",
        "## 数据质量 warning",
        "## 下一步建议",
    ):
        assert section in report.markdown
    assert "sample_size：2" in report.markdown
    assert "risk_worsening：否" in report.markdown
    assert "locked_cash_days：3,380,000.00" in report.markdown
    assert "return_per_locked_cash_day：0.000473" in report.markdown
    assert "可评估候选行：2" in report.markdown
    assert "拒绝日志行：1" in report.markdown
    assert "输入文件行数：" in report.markdown
    assert "Sharpe" not in report.markdown
    assert "Sortino" not in report.markdown
    assert "Calmar" not in report.markdown
    assert "sharpe_ratio" not in report.markdown
    assert "sortino_ratio" not in report.markdown
    assert "calmar_ratio" not in report.markdown


def test_strategy_lab_service_runs_dry_by_default_without_writing_outputs(tmp_path: Path) -> None:
    from src.application.strategy_lab import strategy_lab_tool

    candidate_path, reject_path = _write_sell_put_fixture(tmp_path)

    data, warnings, meta = strategy_lab_tool(
        {
            "experiment_id": "exp_service_dry",
            "strategy_type": "sell_put",
            "account": "sy",
            "candidate_paths": [str(candidate_path)],
            "reject_log_paths": [str(reject_path)],
            "baseline_params": {"selection_source": "existing", "min_sample": 1},
            "candidate_params": {
                "selection_source": "rules",
                "min_dte": 20,
                "max_dte": 35,
                "min_abs_delta": 0.10,
                "max_abs_delta": 0.35,
                "min_premium": 4,
                "max_candidates": 3,
                "min_sample": 1,
            },
        },
        base=tmp_path,
    )

    assert data["schema_version"] == "strategy_lab.v1"
    assert data["dry_run"] is True
    assert data["write_applied"] is False
    assert data["output"]["written"] is False
    assert data["result"]["experiment_id"] == "exp_service_dry"
    assert data["report"]["markdown"].startswith("# Strategy Lab 回测报告")
    assert warnings == data["result"]["warnings"]
    assert meta["base"] == f".../{tmp_path.name}"
    assert not (tmp_path / "output_shared" / "strategy_lab").exists()


def test_strategy_lab_service_includes_frozen_historical_snapshot_summary(tmp_path: Path) -> None:
    from src.application.strategy_lab import (
        HistoricalBar,
        HistoricalDataCache,
        HistoricalDataRequest,
        build_historical_data_snapshot,
        strategy_lab_tool,
    )

    candidate_path, reject_path = _write_sell_put_fixture(tmp_path)
    cache = HistoricalDataCache(base=tmp_path)
    snapshot_path = cache.write_snapshot(
        build_historical_data_snapshot(
            request=HistoricalDataRequest(symbols=("NVDA", "MSFT"), start_date="2026-05-01", end_date="2026-05-03"),
            source="manual",
            generated_at="2026-05-24T08:00:00Z",
            bars=[
                HistoricalBar(symbol="NVDA", timestamp="2026-05-01", close=900),
                HistoricalBar(symbol="NVDA", timestamp="2026-05-02", close=910),
            ],
        )
    )

    data, warnings, _meta = strategy_lab_tool(
        {
            "experiment_id": "exp_service_historical",
            "strategy_type": "sell_put",
            "candidate_paths": [str(candidate_path)],
            "reject_log_paths": [str(reject_path)],
            "historical_snapshot_paths": [cache.relative(snapshot_path)],
            "baseline_params": {"selection_source": "existing", "min_sample": 1},
            "candidate_params": {"selection_source": "rules", "min_sample": 1},
        },
        base=tmp_path,
    )

    assert data["historical_data"]["snapshot_count"] == 1
    assert data["historical_data"]["bar_count"] == 2
    assert data["historical_data"]["snapshots"][0]["symbols"] == ["NVDA", "MSFT"]
    assert "historical_data_missing_symbol:MSFT" in data["historical_data"]["warnings"]
    assert "historical_data_missing_symbol:MSFT" in warnings


def test_strategy_lab_service_requires_confirm_for_local_output_writes(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.strategy_lab import strategy_lab_tool

    candidate_path, reject_path = _write_sell_put_fixture(tmp_path)

    with pytest.raises(AgentToolError, match="WRITE_CONFIRMATION_REQUIRED"):
        strategy_lab_tool(
            {
                "experiment_id": "exp_service_requires_confirm",
                "candidate_paths": [str(candidate_path)],
                "reject_log_paths": [str(reject_path)],
                "baseline_params": {"selection_source": "existing", "min_sample": 1},
                "candidate_params": {"selection_source": "rules", "min_sample": 1},
                "write_outputs": True,
            },
            base=tmp_path,
        )


def test_strategy_lab_service_writes_result_report_and_current_pointer(tmp_path: Path) -> None:
    import json

    from src.application.strategy_lab import strategy_lab_tool

    from src.application.strategy_lab import HistoricalBar, HistoricalDataCache, HistoricalDataRequest, build_historical_data_snapshot

    candidate_path, reject_path = _write_sell_put_fixture(tmp_path)
    cache = HistoricalDataCache(base=tmp_path)
    snapshot_path = cache.write_snapshot(
        build_historical_data_snapshot(
            request=HistoricalDataRequest(symbols=("NVDA",), start_date="2026-05-01", end_date="2026-05-03"),
            source="manual",
            generated_at="2026-05-24T07:00:00Z",
            bars=[HistoricalBar(symbol="NVDA", timestamp="2026-05-01", close=900)],
        )
    )

    data, warnings, _meta = strategy_lab_tool(
        {
            "experiment_id": "exp service write",
            "strategy_type": "sell_put",
            "account": "sy",
            "candidate_paths": [str(candidate_path)],
            "reject_log_paths": [str(reject_path)],
            "historical_snapshot_paths": [cache.relative(snapshot_path)],
            "baseline_params": {"selection_source": "existing", "min_sample": 1},
            "candidate_params": {
                "selection_source": "rules",
                "min_dte": 20,
                "max_dte": 35,
                "min_abs_delta": 0.10,
                "max_abs_delta": 0.35,
                "min_premium": 4,
                "max_candidates": 3,
                "min_sample": 1,
            },
            "write_outputs": True,
            "confirm": True,
        },
        base=tmp_path,
        now_fn=lambda: datetime(2026, 5, 24, 8, 0, tzinfo=timezone.utc),
    )

    assert warnings == data["result"]["warnings"]
    assert data["dry_run"] is False
    assert data["write_applied"] is True
    assert data["backup_path"] is None
    assert data["audit_id"] is None
    assert data["rollback_hint"]
    output = data["output"]
    assert output["written"] is True
    assert output["result_path"] == "output_shared/strategy_lab/exp_service_write.result.json"
    assert output["report_path"] == "output_shared/strategy_lab/exp_service_write.md"
    assert output["current_path"] == "output_shared/state/current/strategy_lab.current.json"

    result_payload = json.loads((tmp_path / output["result_path"]).read_text(encoding="utf-8"))
    current_payload = json.loads((tmp_path / output["current_path"]).read_text(encoding="utf-8"))
    report_markdown = (tmp_path / output["report_path"]).read_text(encoding="utf-8")

    assert result_payload["schema_version"] == "strategy_lab.v1"
    assert result_payload["generated_at"] == "2026-05-24T08:00:00Z"
    assert result_payload["result"]["experiment_id"] == "exp service write"
    assert result_payload["historical_data"]["snapshot_count"] == 1
    assert current_payload["schema_version"] == "strategy_lab_current.v1"
    assert current_payload["historical_snapshot_count"] == 1
    assert current_payload["result_path"] == output["result_path"]
    assert current_payload["report_path"] == output["report_path"]
    assert "## 下一步建议" in report_markdown


def test_strategy_lab_service_rejects_output_paths_outside_repo(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.strategy_lab import strategy_lab_tool

    candidate_path, reject_path = _write_sell_put_fixture(tmp_path)

    with pytest.raises(AgentToolError, match="output directories must stay under the repo root"):
        strategy_lab_tool(
            {
                "experiment_id": "exp_service_outside",
                "candidate_paths": [str(candidate_path)],
                "reject_log_paths": [str(reject_path)],
                "baseline_params": {"selection_source": "existing", "min_sample": 1},
                "candidate_params": {"selection_source": "rules", "min_sample": 1},
                "write_outputs": True,
                "confirm": True,
                "output_dir": str(tmp_path.parent / "outside-strategy-lab"),
            },
            base=tmp_path,
        )


def test_strategy_lab_dataset_collect_dry_run_freezes_evidence_without_writing(tmp_path: Path) -> None:
    from src.application.strategy_lab import strategy_lab_dataset_collect_tool

    candidate_path, reject_path, trace_path, outcome_path = _write_strategy_lab_mvp_fixture(tmp_path)

    data, warnings, meta = strategy_lab_dataset_collect_tool(
        {
            "runtime_root": str(tmp_path),
            "market": "us",
            "account": "sy",
            "strategy_type": "sell_put",
            "candidate_paths": [str(candidate_path)],
            "reject_log_paths": [str(reject_path)],
            "trace_paths": [str(trace_path)],
            "outcome_paths": [str(outcome_path)],
        },
        base=tmp_path,
        now_fn=lambda: datetime(2026, 5, 24, 8, 0, tzinfo=timezone.utc),
    )

    assert data["schema_version"] == "strategy_lab_dataset_collect.v1"
    assert data["dry_run"] is True
    assert data["write_applied"] is False
    assert data["dataset"]["summary"]["candidate_count"] == 5
    assert data["dataset"]["summary"]["outcome_count"] == 5
    assert data["dataset"]["summary"]["reject_count"] == 1
    assert data["dataset"]["summary"]["trace_count"] == 1
    assert data["dataset"]["sources"]["artifacts"][0]["sample_rows"][0]["symbol"] == "NVDA"
    assert data["output"]["dataset_path"].startswith("output_shared/strategy_lab/datasets/")
    assert warnings == ["strategy_lab_ledger_sqlite_missing:option_positions.sqlite3"]
    assert meta["runtime_root"] == f".../{tmp_path.name}"
    assert not (tmp_path / data["output"]["dataset_path"]).exists()


def test_strategy_lab_dataset_collect_confirm_writes_dataset(tmp_path: Path) -> None:
    import json

    from src.application.strategy_lab import strategy_lab_dataset_collect_tool

    candidate_path, reject_path, trace_path, outcome_path = _write_strategy_lab_mvp_fixture(tmp_path)

    data, warnings, _meta = strategy_lab_dataset_collect_tool(
        {
            "runtime_root": str(tmp_path),
            "market": "us",
            "account": "sy",
            "strategy_type": "sell_put",
            "candidate_paths": [str(candidate_path)],
            "reject_log_paths": [str(reject_path)],
            "trace_paths": [str(trace_path)],
            "outcome_paths": [str(outcome_path)],
            "confirm": True,
        },
        base=tmp_path,
        now_fn=lambda: datetime(2026, 5, 24, 8, 0, tzinfo=timezone.utc),
    )

    dataset_path = tmp_path / data["output"]["dataset_path"]
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))

    assert data["dry_run"] is False
    assert data["write_applied"] is True
    assert data["output"]["written"] is True
    assert payload["schema_version"] == "strategy_lab_dataset.v1"
    assert payload["summary"]["candidate_count"] == 5
    assert warnings == ["strategy_lab_ledger_sqlite_missing:option_positions.sqlite3"]


def test_strategy_lab_experiment_reports_not_evaluable_without_outcomes(tmp_path: Path) -> None:
    from src.application.strategy_lab import strategy_lab_experiment_tool

    candidate_path, reject_path = _write_sell_put_fixture(tmp_path)

    data, warnings, _meta = strategy_lab_experiment_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "sy",
            "strategy_type": "sell_put",
            "candidate_paths": [str(candidate_path)],
            "reject_log_paths": [str(reject_path)],
        },
        base=tmp_path,
    )

    assert data["dry_run"] is True
    assert data["write_applied"] is False
    assert data["result"]["status"] == "not_evaluable"
    assert data["result"]["recommendation"]["recommendation"] == "not_evaluable"
    assert "close_or_expiry_outcomes" in data["result"]["preflight"]["missing"]
    assert "strategy_lab_outcomes_empty" in warnings
    assert data["report"]["markdown"].startswith("# Strategy Lab 实验报告")
    assert not (tmp_path / data["output"]["current_path"]).exists()


def test_strategy_lab_experiment_confirm_writes_report_and_current_pointer(tmp_path: Path) -> None:
    import json

    from src.application.strategy_lab import strategy_lab_experiment_tool

    candidate_path, reject_path, trace_path, outcome_path = _write_strategy_lab_mvp_fixture(tmp_path)

    data, warnings, _meta = strategy_lab_experiment_tool(
        {
            "runtime_root": str(tmp_path),
            "account": "sy",
            "strategy_type": "sell_put",
            "candidate_paths": [str(candidate_path)],
            "reject_log_paths": [str(reject_path)],
            "trace_paths": [str(trace_path)],
            "outcome_paths": [str(outcome_path)],
            "candidate_params": {"selection_source": "rules", "max_candidates": 5, "min_sample": 1},
            "baseline_params": {"selection_source": "existing", "min_sample": 1},
            "confirm": True,
        },
        base=tmp_path,
        now_fn=lambda: datetime(2026, 5, 24, 8, 0, tzinfo=timezone.utc),
    )

    result_path = tmp_path / data["output"]["result_path"]
    report_path = tmp_path / data["output"]["report_path"]
    current_path = tmp_path / data["output"]["current_path"]
    current = json.loads(current_path.read_text(encoding="utf-8"))

    assert data["dry_run"] is False
    assert data["write_applied"] is True
    assert data["result"]["status"] == "evaluable"
    assert data["result"]["recommendation"]["recommendation"] in {"reject", "watch", "shadow"}
    assert result_path.exists()
    assert report_path.read_text(encoding="utf-8").startswith("# Strategy Lab 实验报告")
    assert current["schema_version"] == "strategy_lab_current.v1"
    assert current["dataset_id"] == data["dataset"]["dataset_id"]
    assert current["result_path"] == data["output"]["result_path"]
    assert warnings == data["result"]["warnings"]


def test_strategy_lab_replay_backtest_stays_in_supported_strategy_set() -> None:
    from src.application.strategy_lab import (
        StrategyExperiment,
        StrategyPolicy,
    )

    with pytest.raises(ValueError, match="unsupported strategy_type"):
        StrategyExperiment(
            experiment_id="exp_unknown",
            strategy_type="unknown",
            baseline_policy=StrategyPolicy(name="baseline", strategy_type="sell_put"),
            candidate_policy=StrategyPolicy(name="candidate", strategy_type="sell_put"),
        )


def _write_strategy_lab_mvp_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    candidate_path = tmp_path / "sell_put_candidates.csv"
    candidate_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,selected,multiplier,contracts,locked_cash\n"
        "NVDA,sy,put,short,900,2026-06-19,26,12,-0.18,true,100,1,90000\n"
        "MSFT,sy,put,short,400,2026-06-19,26,4,-0.12,true,100,1,40000\n"
        "AAPL,sy,put,short,180,2026-06-19,26,2,-0.16,true,100,1,18000\n"
        "TSLA,sy,put,short,200,2026-06-19,26,8,-0.30,true,100,1,20000\n"
        "AMD,sy,put,short,150,2026-06-19,26,3,-0.22,true,100,1,15000\n",
        encoding="utf-8",
    )
    reject_path = tmp_path / "sell_put_candidates_reject_log.csv"
    reject_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,engine_reject_reason,multiplier,contracts\n"
        "META,sy,put,short,500,2026-06-19,26,5,-0.08,delta_too_low,100,1\n",
        encoding="utf-8",
    )
    trace_path = tmp_path / "candidate_filter_trace.jsonl"
    trace_path.write_text('{"symbol":"NVDA","account":"sy","stage":"rank"}\n', encoding="utf-8")
    outcome_path = tmp_path / "strategy_replay.csv"
    outcome_path.write_text(
        "symbol,account,actual_return,realized_pnl\n"
        "NVDA,sy,0.03,120\n"
        "MSFT,sy,0.02,40\n"
        "AAPL,sy,0.01,20\n"
        "TSLA,sy,-0.01,-20\n"
        "AMD,sy,0.02,30\n",
        encoding="utf-8",
    )
    return candidate_path, reject_path, trace_path, outcome_path


def _write_sell_put_fixture(tmp_path: Path) -> tuple[Path, Path]:
    candidate_path = tmp_path / "sell_put_candidates.csv"
    candidate_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,selected,multiplier,contracts\n"
        "NVDA,sy,put,short,900,2026-06-19,26,12,-0.18,true,100,1\n"
        "MSFT,sy,put,short,400,2026-06-19,26,4,-0.12,true,100,1\n",
        encoding="utf-8",
    )
    reject_path = tmp_path / "sell_put_candidates_reject_log.csv"
    reject_path.write_text(
        "symbol,account,option_type,side,strike,expiry,dte,premium,delta,engine_reject_reason,multiplier,contracts\n"
        "TSLA,sy,put,short,200,2026-06-19,26,8,-0.30,risk_spread,100,1\n",
        encoding="utf-8",
    )
    return candidate_path, reject_path
