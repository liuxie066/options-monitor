from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts.close_advice.runner import run_close_advice
from scripts.multi_tick.misc import AccountResult
from scripts.multi_tick.notify_format import build_account_message


def test_run_close_advice_builds_csv_and_markdown_from_local_fixtures(tmp_path: Path) -> None:
    context = {
        "open_positions_min": [
            {
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts_open": 1,
                "currency": "USD",
                "strike": 100,
                "multiplier": 100,
                "premium": 1.6,
                "expiration": "2026-05-15",
            }
        ]
    }
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    required_root = tmp_path / "required_data"
    parsed = required_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-05-15",
                "strike": 100,
                "mid": 0.22,
                "bid": 0.21,
                "ask": 0.23,
                "dte": 29,
                "multiplier": 100,
                "spot": 120,
                "currency": "USD",
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_dir = tmp_path / "reports"
    result = run_close_advice(
        config={
            "close_advice": {
                "enabled": True,
                "notify_levels": ["strong", "medium"],
                "max_items_per_account": 5,
            }
        },
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    assert result["enabled"] is True
    assert result["rows"] == 1
    text = (out_dir / "close_advice.txt").read_text(encoding="utf-8")
    assert "平仓建议" in text
    assert "NVDA Put 2026-05-15" in text
    assert "强烈建议平仓" in text

    csv_text = (out_dir / "close_advice.csv").read_text(encoding="utf-8")
    assert "capture_ratio" in csv_text
    assert "strong" in csv_text


def test_run_close_advice_records_missing_quote_but_does_not_notify(tmp_path: Path) -> None:
    ctx_path = tmp_path / "option_positions_context.json"
    ctx_path.write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "lx",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "contracts_open": 1,
                        "currency": "USD",
                        "strike": 100,
                        "multiplier": 100,
                        "premium": 1.0,
                        "expiration": "2026-05-15",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    required_root = tmp_path / "required_data"
    (required_root / "parsed").mkdir(parents=True)
    out_dir = tmp_path / "reports"

    run_close_advice(
        config={"close_advice": {"enabled": True}},
        context_path=ctx_path,
        required_data_root=required_root,
        output_dir=out_dir,
        base_dir=Path.cwd(),
    )

    assert (out_dir / "close_advice.txt").read_text(encoding="utf-8") == ""
    assert "missing_quote" in (out_dir / "close_advice.csv").read_text(encoding="utf-8")


def test_close_advice_text_can_drive_account_message_without_opening_candidates() -> None:
    result = AccountResult(
        account="lx",
        ran_scan=True,
        should_notify=True,
        meaningful=True,
        decision_reason="到达通知点",
        notification_text=(
            "### [lx] 平仓建议\n"
            "- NVDA Put 2026-05-15 100.00P · 强烈建议平仓\n"
            "- 已锁定: 86.0% | 剩余DTE=29 | 剩余收益年化=6.8%\n"
            "---\n"
        ),
    )

    msg = build_account_message(result, now_bj="2026-04-16 21:30:00", cash_footer_lines=[])

    assert "账户提醒（lx）" in msg
    assert "平仓建议" in msg
    assert "Put 0 / Call 0" in msg
