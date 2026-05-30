from __future__ import annotations

import csv
from pathlib import Path


def test_candidate_reject_summary_groups_trace_rejections(tmp_path: Path) -> None:
    from src.application.candidate_filter_trace import append_candidate_filter_trace_rows, build_candidate_filter_trace_row
    from src.application.candidate_reject_summary import build_candidate_reject_summary, render_candidate_reject_summary

    trace_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="PDD",
                function="sell_put",
                mode="put",
                status="post_filtered",
                stage="post_filter",
                rule="volatility_estimate_missing",
            ),
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="FUTU",
                function="sell_put",
                mode="put",
                status="post_filtered",
                stage="post_filter",
                rule="vol_edge_ratio_below_min",
            ),
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="NVDA",
                function="sell_call",
                mode="call",
                status="rejected",
                stage="stage3_risk_filter",
                rule="risk_spread",
            ),
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="AAPL",
                function="sell_call",
                mode="call",
                status="accepted",
                stage="stage4_ranking",
                rule="candidate_accepted",
            ),
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="AAPL",
                function="close_advice",
                mode="call",
                status="rejected",
                stage="post_filter",
                rule="spread_too_wide",
            ),
        ],
    )

    summary = build_candidate_reject_summary(trace_path=trace_path, account="lx", run_id="run-1")

    assert summary["source"] == "trace"
    assert summary["accepted_count"] == 1
    assert summary["total_rejected"] == 3
    assert summary["function_counts"] == {"sell_put": 2, "sell_call": 1}
    categories = {item["category"]: item for item in summary["top_categories"]}
    assert categories["data_missing"]["count"] == 1
    assert categories["vol_edge"]["count"] == 1
    assert categories["liquidity"]["count"] == 1

    rendered = render_candidate_reject_summary(summary)
    assert "### 拒绝摘要" in rendered
    assert "通过 1 条；拒绝/后过滤 3 条" in rendered
    assert "Sell Put 2 / Covered Call 1" in rendered
    assert "数据缺失 1：RV 缺失 1；样例 PDD" in rendered
    assert "波动率边际不足 1：IV/RV 不足 1；样例 FUTU" in rendered
    assert "流动性不足 1：价差不合格 1；样例 NVDA" in rendered
    assert "spread_too_wide" not in rendered


def test_candidate_reject_summary_falls_back_to_reject_log(tmp_path: Path) -> None:
    from src.application.candidate_reject_summary import build_candidate_reject_summary

    reject_path = tmp_path / "nvda_sell_put_candidates_reject_log.csv"
    with reject_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "reject_stage",
                "reject_rule",
                "symbol",
                "contract_symbol",
                "expiration",
                "strike",
                "mode",
                "engine_reject_stage",
                "engine_reject_reason",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "reject_stage": "step3_risk_gate",
                "reject_rule": "max_spread_ratio",
                "symbol": "NVDA",
                "contract_symbol": "NVDA_PUT",
                "expiration": "2026-06-19",
                "strike": "100",
                "mode": "put",
                "engine_reject_stage": "stage3_risk_filter",
                "engine_reject_reason": "risk_spread",
            }
        )

    summary = build_candidate_reject_summary(reject_log_paths=[reject_path])

    assert summary["source"] == "reject_log"
    assert summary["total_rejected"] == 1
    assert summary["function_counts"] == {"sell_put": 1}
    assert summary["top_categories"][0]["category"] == "liquidity"


def test_append_candidate_reject_summary_reports_unavailable_when_no_evidence(tmp_path: Path) -> None:
    from src.application.candidate_reject_summary import append_candidate_reject_summary_to_text

    text = append_candidate_reject_summary_to_text(
        "📋 本轮扫描完成，暂无符合条件的候选。",
        trace_path=tmp_path / "missing.jsonl",
        reject_log_paths=[],
        account="lx",
        run_id="run-1",
    )

    assert "暂无符合条件的候选" in text
    assert "### 拒绝摘要" in text
    assert "拒绝摘要不可用" in text
