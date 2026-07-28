from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from domain.domain.candidate_defaults import CandidateLiquidityDefaults, CandidateWindowDefaults
from src.application.combo_yield_steps import (
    run_combo_yield_for_symbol_and_summarize,
    run_combo_yield_scan_and_summarize,
)
from src.application.yield_enhancement_config import derive_yield_enhancement_policy
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates


def _candidate(**overrides) -> dict:
    row = {
        "symbol": "NVDA",
        "expiration": "2026-08-21",
        "dte": 35,
        "contract_symbol": "NVDA260821P00100000",
        "multiplier": 100,
        "currency": "USD",
        "strike": 100.0,
        "spot": 110.0,
        "bid": 2.0,
        "ask": 2.1,
        "mid": 2.05,
        "open_interest": 500,
        "volume": 50,
        "delta": -0.2,
        "event_flag": False,
        "event_source_status": "ok",
    }
    row.update(overrides)
    return row


def _run(
    tmp_path: Path,
    *,
    candidates: list[dict],
    find_pairs_fn,
    yield_sp: dict | None = None,
    underwriting_filter_put_candidates_fn=None,
    output_mode: str = "separate",
    is_scheduled: bool = True,
    attach_calls_fn=None,
    render_alerts_fn=None,
):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, pd.DataFrame] = {}

    def run_put_scan_fn(**kwargs):
        captured["scan_kwargs"] = kwargs
        pd.DataFrame(candidates).to_csv(kwargs["output"], index=False)

    def label_put_candidates_fn(_base, input_path, output_path):
        Path(output_path).write_bytes(Path(input_path).read_bytes())

    def capture_pairs(**kwargs):
        captured["df"] = kwargs["df_candidates"].copy()
        return find_pairs_fn(**kwargs)

    yield_cfg = {"enabled": True, "output_mode": output_mode}
    policy = derive_yield_enhancement_policy(yield_cfg)
    run_combo_yield_scan_and_summarize(
        base=tmp_path,
        sym="NVDA",
        symbol="NVDA",
        symbol_lower="nvda",
        symbol_cfg={"symbol": "NVDA", "combo_yield": {"enabled": True}},
        yield_enhancement_cfg=yield_cfg,
        yield_sp={
            "strategy": "insurance_underwriting",
            "min_annualized_net_return": 0.10,
            "reject_event_risk": True,
            "event_source_fail_closed": True,
            **(yield_sp or {}),
        },
        yield_enhancement_policy=policy,
        df_sell_put_labeled=pd.DataFrame(),
        sell_put_labeled_path=report_dir / "nvda_sell_put_candidates_labeled.csv",
        required_data_dir=tmp_path / "required_data",
        report_dir=report_dir,
        yield_window=CandidateWindowDefaults(min_dte=7, max_dte=60),
        liquidity=CandidateLiquidityDefaults(),
        event_risk={"enabled": True, "mode": "warn"},
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        portfolio_ctx=None,
        top_n=3,
        is_scheduled=is_scheduled,
        run_put_scan_fn=run_put_scan_fn,
        label_put_candidates_fn=label_put_candidates_fn,
        find_pairs_fn=capture_pairs,
        select_pairs_fn=lambda df: df,
        cash_filter_put_candidates_fn=None,
        underwriting_filter_put_candidates_fn=(
            underwriting_filter_put_candidates_fn
            or (lambda **kwargs: kwargs["df_labeled"])
        ),
        **({"attach_calls_fn": attach_calls_fn} if attach_calls_fn is not None else {}),
        **({"render_alerts_fn": render_alerts_fn} if render_alerts_fn is not None else {}),
    )
    trace = [
        json.loads(line)
        for line in (report_dir / "candidate_filter_trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return captured["df"], trace, captured["scan_kwargs"]


def test_combo_yield_reuses_sell_put_underwriting_before_pairing(tmp_path: Path) -> None:
    captured_underwriting: dict[str, object] = {}

    def underwriting_gate(**kwargs):
        captured_underwriting.update(kwargs)
        out = kwargs["df_labeled"].copy()
        out["premium_edge_score"] = 0.25
        out["strike_safety_margin_pct"] = 0.12
        return out

    captured, _trace, _scan_kwargs = _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.18)],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        yield_sp={"strategy": "insurance_underwriting", "min_annualized_net_return": 0.15},
        underwriting_filter_put_candidates_fn=underwriting_gate,
    )

    assert captured_underwriting["sell_put_cfg"]["strategy"] == "insurance_underwriting"
    assert captured_underwriting["symbol"] == "NVDA"
    assert len(captured) == 1
    assert float(captured.iloc[0]["premium_edge_score"]) == 0.25
    assert float(captured.iloc[0]["strike_safety_margin_pct"]) == 0.12


def test_combo_yield_facade_forces_funding_put_underwriting_when_sell_put_is_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.combo_yield_steps as steps

    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return steps._empty_result(report_dir=kwargs["report_dir"], symbol_lower=kwargs["symbol_lower"]), {
            "strategy": "combo_yield",
        }

    monkeypatch.setattr(steps, "run_combo_yield_scan_and_summarize", fake_run)

    summary = run_combo_yield_for_symbol_and_summarize(
        base=tmp_path,
        sym="NVDA",
        symbol="NVDA",
        symbol_lower="nvda",
        symbol_cfg={
            "symbol": "NVDA",
            "combo_yield": {"enabled": True},
            "sell_put": {"enabled": False},
        },
        sell_put_cfg={"enabled": False},
        top_n=3,
        required_data_dir=tmp_path / "required_data",
        report_dir=tmp_path / "reports",
        is_scheduled=True,
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
        portfolio_ctx=None,
    )

    assert summary == {"strategy": "combo_yield"}
    assert captured["yield_sp"] == {
        "enabled": False,
        "strategy": "insurance_underwriting",
    }
    assert captured["yield_enhancement_policy"].requires_realized_volatility is True


def test_combo_yield_event_gate_fails_closed_without_iv_rv_underwriting(tmp_path: Path) -> None:
    captured, trace, _scan_kwargs = _run(
        tmp_path,
        candidates=[
            _candidate(event_source_status="error"),
            _candidate(contract_symbol="NVDA260821P00095000", strike=95.0, event_flag=True),
        ],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
    )

    assert captured.empty
    rules = {row["rule"] for row in trace if row["stage"] == "combo_event_risk"}
    assert rules == {"event_source_unavailable", "event_risk_within_expiry"}
    assert "combo_yield_put_event_filtered" in {row["rule"] for row in trace}


def test_combo_yield_writes_pair_rejection_aggregates_to_trace(tmp_path: Path) -> None:
    def rejected_pairs(**_kwargs):
        out = pd.DataFrame()
        out.attrs["reject_counts"] = {"min_net_credit_retention": 3}
        out.attrs["pair_diagnostics"] = pd.DataFrame(
            [
                {
                    "diagnostic_scope": "pair",
                    "diagnostic_stage": "pair_filter",
                    "accepted": False,
                    "reject_reasons": "min_net_credit_retention",
                    "put_contract_symbol": "NVDA260821P00100000",
                    "call_contract_symbol": "NVDA260821C00120000",
                    "net_credit_retention": 0.72,
                }
            ]
        )
        return out

    captured, trace, _scan_kwargs = _run(
        tmp_path,
        candidates=[_candidate()],
        find_pairs_fn=rejected_pairs,
    )

    assert len(captured) == 1
    pair_rows = [row for row in trace if row["stage"] == "combo_pair_filter"]
    assert len(pair_rows) == 1
    assert pair_rows[0]["rule"] == "min_net_credit_retention"
    assert pair_rows[0]["metric_value"] == 3
    diagnostics = pd.read_csv(tmp_path / "reports" / "nvda_combo_yield_pair_diagnostics.csv")
    assert diagnostics[["put_contract_symbol", "call_contract_symbol"]].to_dict("records") == [
        {
            "put_contract_symbol": "NVDA260821P00100000",
            "call_contract_symbol": "NVDA260821C00120000",
        }
    ]
    assert diagnostics.loc[0, "reject_reasons"] == "min_net_credit_retention"
    assert {"run_id", "account"}.issubset(diagnostics.columns)


def test_combo_yield_writes_real_diagnostics_when_call_prefilter_removes_all_pairs(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs

    parsed = tmp_path / "required_data" / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-08-21",
                "dte": 35,
                "contract_symbol": "NVDA260821C00120000",
                "strike": 120.0,
                "spot": 110.0,
                "bid": 0.9,
                "ask": 1.0,
                "mid": 0.95,
                "open_interest": 500,
                "volume": 50,
                "implied_volatility": 0.40,
                "currency": "USD",
                "delta": 0.30,
                "multiplier": 100,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.18)],
        find_pairs_fn=find_sell_put_yield_enhancement_pairs,
    )

    diagnostics = pd.read_csv(tmp_path / "reports" / "nvda_combo_yield_pair_diagnostics.csv")
    call_reject = diagnostics.loc[diagnostics["diagnostic_scope"] == "call"].iloc[0]
    assert call_reject["call_contract_symbol"] == "NVDA260821C00120000"
    assert call_reject["reject_reasons"] == "call_delta_above_max"
    assert float(call_reject["policy_call_max_delta"]) == 0.20
    assert int(diagnostics["accepted"].sum()) == 0


def test_combo_yield_uses_standalone_sell_put_return_floor_and_annotations(tmp_path: Path) -> None:
    captured, trace, scan_kwargs = _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.18)],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        yield_sp={"strategy": "insurance_underwriting", "min_annualized_net_return": 0.15},
    )

    assert scan_kwargs["min_annualized_net_return"] == 0.15
    assert scan_kwargs["min_net_income"] == 0.0
    assert len(captured) == 1
    row = captured.iloc[0]
    assert bool(row["funding_put_eligible"]) is True
    assert float(row["funding_put_min_annualized_return"]) == 0.15
    assert float(row["put_only_annualized_net_return"]) == 0.18
    assert "combo_yield_no_pair" in {item["rule"] for item in trace}


def test_combo_yield_traces_when_no_funding_put_is_eligible(tmp_path: Path) -> None:
    _captured, trace, scan_kwargs = _run(
        tmp_path,
        candidates=[],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        yield_sp={"min_annualized_net_return": 0.16},
    )

    assert scan_kwargs["min_annualized_net_return"] == 0.16
    row = next(item for item in trace if item["stage"] == "post_filter")
    assert row["rule"] == "combo_yield_no_funding_put_eligible"
    assert row["threshold"] == 0.16


def test_combo_yield_writes_shadow_rank_artifact_without_changing_selection(tmp_path: Path) -> None:
    pairs = pd.DataFrame(
        [
            {
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C110",
                "funding_accepted": True,
                "premium_funding_score": 0.9,
                "net_credit_retention": 0.80,
                "call_cost_to_put_credit": 0.20,
                "call_delta": 0.18,
                "call_spread_ratio": 0.12,
                "call_open_interest": 500,
                "call_payoff_multiple_at_1_5_sigma": 1.8,
                "call_payoff_multiple_at_2_0_sigma": 4.0,
                "put_assignment_margin_pct": 0.05,
                "put_only_annualized_net_return": 0.14,
                "combo_spread_ratio": 0.20,
                "annualized_net_credit_yield": 0.09,
                "residual_premium_ratio": 0.80,
            },
            {
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C115",
                "funding_accepted": True,
                "premium_funding_score": 1.1,
                "net_credit_retention": 0.88,
                "call_cost_to_put_credit": 0.12,
                "call_delta": 0.10,
                "call_spread_ratio": 0.08,
                "call_open_interest": 900,
                "call_payoff_multiple_at_1_5_sigma": 1.0,
                "call_payoff_multiple_at_2_0_sigma": 3.0,
                "put_assignment_margin_pct": 0.05,
                "put_only_annualized_net_return": 0.14,
                "combo_spread_ratio": 0.15,
                "annualized_net_credit_yield": 0.11,
                "residual_premium_ratio": 0.88,
            },
        ]
    )

    def find_pairs(**_kwargs):
        return pairs.copy()

    _captured, _trace, _scan_kwargs = _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.14)],
        find_pairs_fn=find_pairs,
    )

    artifact = pd.read_csv(tmp_path / "reports" / "nvda_combo_yield_rank_shadow.csv")
    assert artifact.loc[artifact["baseline_selected"], "call_contract_symbol"].tolist() == ["NVDA_C115"]
    assert artifact.loc[artifact["shadow_selected"], "call_contract_symbol"].tolist() == ["NVDA_C110"]
    assert artifact["rank_changed"].all()


def test_combo_yield_candidate_write_error_is_not_swallowed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.combo_yield_steps as steps

    original_write = steps._atomic_write_dataframe

    def fail_candidate_write(path: Path, df: pd.DataFrame) -> None:
        if path.name == "nvda_combo_yield_candidates.csv":
            raise OSError("disk full")
        original_write(path, df)

    monkeypatch.setattr(steps, "_atomic_write_dataframe", fail_candidate_write)

    with pytest.raises(RuntimeError, match="failed to persist Combo Yield candidates"):
        _run(
            tmp_path,
            candidates=[_candidate()],
            find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        )


def test_combo_yield_render_failure_happens_before_inline_commit(tmp_path: Path) -> None:
    attached: list[bool] = []

    def fail_render(**_kwargs) -> str:
        raise RuntimeError("render failed")

    with pytest.raises(RuntimeError, match="render failed"):
        _run(
            tmp_path,
            candidates=[_candidate()],
            find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
            output_mode="both",
            is_scheduled=False,
            attach_calls_fn=lambda **_kwargs: attached.append(True),
            render_alerts_fn=fail_render,
        )

    assert attached == []


def test_empty_combo_yield_materialization_clears_stale_inline_columns(tmp_path: Path) -> None:
    from src.application.combo_yield_steps import materialize_empty_combo_yield_artifacts

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    labeled_path = report_dir / "nvda_sell_put_candidates_labeled.csv"
    pd.DataFrame(
        [
            {
                "contract_symbol": "NVDA_P100",
                "label": "候选",
                "linked_call_contract": "2026-08-21 110C",
                "linked_call_contract_symbol": "NVDA_C110",
            }
        ]
    ).to_csv(labeled_path, index=False)

    materialize_empty_combo_yield_artifacts(report_dir=report_dir, symbol_lower="nvda")

    cleaned = pd.read_csv(labeled_path)
    assert cleaned.to_dict("records") == [{"contract_symbol": "NVDA_P100", "label": "候选"}]
