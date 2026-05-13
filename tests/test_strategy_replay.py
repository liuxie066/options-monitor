from __future__ import annotations

import json
import subprocess
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]


def test_strategy_replay_analyze_answers_parameter_questions() -> None:
    from src.application.strategy_replay import analyze_strategy_replay

    rows = [
        {"symbol": "NVDA", "dte": 20, "delta": -0.18, "predicted_return": 0.04, "actual_return": 0.05, "max_drawdown": -0.04, "accepted": True},
        {"symbol": "NVDA", "dte": 25, "delta": -0.19, "predicted_return": 0.035, "actual_return": 0.04, "max_drawdown": -0.05, "accepted": True},
        {"symbol": "MSFT", "dte": 35, "delta": -0.26, "predicted_return": 0.025, "actual_return": 0.02, "max_drawdown": -0.03, "accepted": True},
        {"symbol": "MSFT", "dte": 40, "delta": -0.28, "predicted_return": 0.02, "actual_return": 0.01, "max_drawdown": -0.04, "accepted": True},
        {"symbol": "TSLA", "dte": 35, "delta": -0.36, "predicted_return": 0.08, "actual_return": 0.12, "max_drawdown": -0.31, "roll_triggered": True, "accepted": True},
        {"symbol": "TSLA", "dte": 38, "delta": -0.34, "predicted_return": 0.07, "actual_return": 0.10, "max_drawdown": -0.28, "accepted": True},
        {"symbol": "AAPL", "dte": 10, "delta": -0.12, "predicted_return": 0.02, "actual_return": -0.04, "max_drawdown": -0.22, "filter_reason": "max_spread_ratio", "accepted": False},
        {"symbol": "AAPL", "dte": 12, "delta": -0.15, "predicted_return": 0.02, "actual_return": -0.02, "max_drawdown": -0.18, "filter_reason": "max_spread_ratio", "accepted": False},
    ]

    out = analyze_strategy_replay(rows, min_sample=2, bad_drawdown_threshold=-0.15)

    assert out["summary"]["row_count"] == 8
    assert out["dte_effectiveness"]["best_ranges"][0]["range"] == "15-30"
    assert out["delta_effectiveness"]["best_win_rate_ranges"][0]["range"] == "0.20-0.30"
    assert out["symbol_risk_return"][0]["symbol"] == "TSLA"
    assert out["symbol_risk_return"][0]["high_return_drawdown_bad"] is True
    assert out["filter_value"][0]["filter"] == "max_spread_ratio"
    assert out["filter_value"][0]["status"] == "valuable"
    assert out["dry_run_config_suggestions"][0]["apply_mode"] == "shadow_dry_run_only"


def test_strategy_replay_normalizes_percent_strings() -> None:
    from src.application.strategy_replay import analyze_strategy_replay

    out = analyze_strategy_replay(
        [
            {"symbol": "NVDA", "dte": "21", "delta": "-18%", "actual_return": "4%", "max_drawdown": "6%"},
        ],
        min_sample=1,
    )

    row = out["delta_effectiveness"]["best_ranges"][0]
    assert row["range"] == "0.10-0.20"
    assert row["avg_actual_return"] == 0.04
    assert row["avg_max_drawdown_loss"] == 0.06


def test_strategy_replay_cli_analyze_reads_csv(tmp_path: Path) -> None:
    replay_path = tmp_path / "strategy_replay.csv"
    replay_path.write_text(
        "symbol,dte,delta,actual_return,max_drawdown\n"
        "NVDA,21,-0.18,0.04,-0.06\n",
        encoding="utf-8",
    )

    p = subprocess.run(
        [
            str((BASE / "om").resolve()),
            "strategy-replay",
            "analyze",
            "--replay-path",
            str(replay_path),
            "--min-sample",
            "1",
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(p.stdout)

    assert payload["ok"] is True
    assert payload["data"]["summary"]["row_count"] == 1
    assert payload["data"]["dte_effectiveness"]["best_ranges"][0]["range"] == "15-30"
