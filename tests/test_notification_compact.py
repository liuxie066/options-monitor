from __future__ import annotations

import csv
from pathlib import Path

import pytest



def test_build_notification_block_compact_sell_put() -> None:
    from src.application.notify_symbols import _build_notification_block_compact

    out = _build_notification_block_compact(
        symbol_name="腾讯",
        action_label="卖Put",
        contract="2026-04-29 460",
        income_line="- 收益: 权利金=2.3 | 年化 12% | 净收 2300",
        contract_line="- 合约: 行权价=460 | 数量=1张(默认) | DTE=29",
        risk_line="- 风控: 风险=保守 | delta=0.25 | IV=0.45",
        detail_line="- 资金: 保证金占用=¥46000",
        note="通过准入",
        suggestion="2.3",
    )

    assert "🟢 CSP 腾讯 460P · 04-29 · 挂单 2.3" in out
    assert "收益｜权利金 2.3 · 年化 12% · 29天" in out
    assert "风险｜保守 · Δ 0.25 · 担保 ¥46000" in out
    assert "---" not in out


def test_build_notification_block_compact_combo_yield() -> None:
    from src.application.notify_symbols import _build_notification_block_compact

    out = _build_notification_block_compact(
        symbol_name="英伟达",
        action_label="组合收益",
        contract="2026-06-19 95+110",
        income_line="- 收益: 组合净权利金=95 | 年化 8% | 场景评分=0.82",
        contract_line="- 组合: Put=95 | Call=110 | DTE=45",
        risk_line="- 风控: 风险=中性 | Call delta=0.15 | Call ask=1.2",
        detail_line="- 预期波动: expected_move=5.2 | IV=0.35",
        note="组合收益推荐",
    )

    assert "💎 组合·同期 英伟达 95P+110C · 06-19" in out
    assert "收益｜净权利金 95 · 年化 8.0% · 45天" in out
    assert "风险｜中性 · Call Δ 0.15" in out
    assert "评分" not in out
    assert "预期波动" not in out
    assert "---" not in out


def test_format_alert_line_compact_sell_put() -> None:
    from src.application.notify_symbols import _format_alert_line_compact

    line = "腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | cash_req_cny ¥46000 | delta 0.25 | 风险 保守 | 通过准入"
    out = _format_alert_line_compact(line, account_label="lx")

    assert "🟢 CSP 腾讯" in out
    assert "年化 12%" in out
    assert "---" not in out
    assert "###" not in out


def test_format_alert_line_compact_sell_put_shows_event_risk() -> None:
    from src.application.notify_symbols import _format_alert_line_compact

    line = "腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | cash_req_cny ¥46000 | delta 0.25 | event earnings@2026-04-20 | 风险 保守 | 通过准入"
    out = _format_alert_line_compact(line, account_label="lx")

    assert "🟢 CSP 腾讯" in out
    assert "事件 earnings@2026-04-20" in out
    assert "---" not in out


def test_fmt_date_compact_same_year() -> None:
    from src.application.notify_symbols import _fmt_date_compact

    result = _fmt_date_compact("2026-04-29 460")
    assert result == "@ 04-29"


def test_fmt_pct_compact() -> None:
    from src.application.notify_symbols import _fmt_pct_compact

    assert _fmt_pct_compact("12%") == "12%"
    assert _fmt_pct_compact("8.5%") == "8.5%"
    assert _fmt_pct_compact("0.05") == "5.0%"


def test_build_notification_compact_style() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = "## 高优先级\n腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | 风险 保守 | 通过准入\n"
    out = build_notification("", alerts_text, render_style="compact")

    assert "### CSP" in out
    assert "🟢 CSP 腾讯" in out
    assert "---" not in out


def test_build_notification_defaults_to_compact_and_rejects_unknown_renderer() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = "## 高优先级\n腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | 风险 保守 | 通过准入\n"

    assert build_notification("", alerts_text) == build_notification("", alerts_text, render_style="compact")
    with pytest.raises(ValueError, match="compact, legacy"):
        build_notification("", alerts_text, render_style="unknown")


def test_build_notification_compact_style_uses_markdown_enhancement_heading() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = (
        "## 高优先级\n"
        "NVDA | yield_enhancement | 2026-06-19 95P+110C | 年化 8% | DTE 45 | 保守 | "
        "mid 0.950 | put_bid 2.150 | net_credit 95 | scenario_score 0.82 | put_strike 95 | call_strike 110 | call_delta 0.15 | call_ask 1.2 | 通过准入\n"
    )
    out = build_notification("", alerts_text, render_style="compact")

    assert "### 组合" in out
    assert "· 卖2.15/买1.2" in out
    assert "卖0.950/买1.2" not in out


def test_build_notification_legacy_style_uses_flat_fields() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = "## 高优先级\n腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | 风险 保守 | 通过准入\n"
    with pytest.warns(DeprecationWarning, match="Legacy Tick renderer"):
        out = build_notification("", alerts_text, render_style="legacy")

    assert "CSP" in out
    assert "**[当前账户] 腾讯｜CSP｜2026-04-29 460**" in out
    assert "收益｜" in out
    assert "---" not in out


def test_build_notification_compact_keeps_medium_strategy_with_total_limit() -> None:
    from src.application.notify_symbols import build_notification

    put_lines = [
        (
            f"PUT{i} | sell_put | 2026-06-19 10{i}P | 年化 {20 - i:.2f}% | 净收入 {100 + i:.2f} | "
            f"DTE 30 | Strike 10{i} | 中性 | ccy USD | mid 1.000 | 通过准入后，收益/风险组合较强，值得优先看。"
        )
        for i in range(1, 7)
    ]
    medium_call = (
        "CALL1 | sell_call | 2026-06-19 180C | 年化 6.50% | 净收入 80.00 | "
        "DTE 30 | Strike 180 | 保守 | ccy USD | mid 0.800 | cover 1 | shares 100(-0) | 已通过准入，可作为 CC 备选。"
    )
    alerts_text = (
        "## 高优先级\n"
        + "\n".join(put_lines)
        + "\n\n## 中优先级\n"
        + medium_call
        + "\n"
    )

    out = build_notification("", alerts_text, render_style="compact")

    assert out.count("🟢 CSP") == 5
    assert "PUT5" in out
    assert "PUT6" not in out
    assert "CALL1" in out
    assert "🟢 CC CALL1" in out
    assert out.index("### CSP") < out.index("### CC")


@pytest.mark.parametrize("strategy", ["sell_put", "sell_call", "combo_yield", "yield_enhancement"])
@pytest.mark.parametrize("annual,expected", [
    ("-8.00%", "-8.0%"), ("-18.25%", "-18.2%"), ("-0.05%", "-0.1%"),
    ("0.00%", "0.0%"), ("8.00%", "8.0%"), ("+8.00%", "8.0%"),
])
def test_compact_preserves_signed_annual_across_strategies(strategy, annual, expected) -> None:
    from src.application.notify_symbols import build_notification

    contract = "95P" if strategy == "sell_put" else "110C" if strategy == "sell_call" else "95P+110C"
    strike = "110" if strategy == "sell_call" else "95"
    alerts = (
        f"## 低优先级\n[腾讯](0700.HK) | {strategy} | 2026-11-20 {contract} | "
        f"年化 {annual} | 净收入 95 | DTE 30 | Strike {strike} | 中性 | ccy HKD | "
        "mid 0.95 | net_credit 95 | put_bid 3 | put_strike 95 | call_strike 110 | call_ask 1.5\n"
    )
    compact = build_notification("", alerts)
    assert f"年化 {expected}" in compact
    assert "腾讯" in compact
    assert compact == build_notification("", alerts)
    with pytest.warns(DeprecationWarning, match="Legacy Tick renderer"):
        legacy = build_notification("", alerts, render_style="legacy")
    assert f"年化 {annual}" in legacy


@pytest.mark.parametrize("action", ["卖Put", "CC", "组合收益"])
@pytest.mark.parametrize("note,eligible", [
    ("当前组合收益推荐未通过优先级阈值，仅供观察。", True),
    ("所需担保现金不足，仅供观察。", True),
    ("已按组合收益筛出推荐 Call，可作为 Combo Yield 组合方案。", False),
    ("组合所需现金未知。", False), ("", False),
])
def test_compact_caution_note_preserves_existing_prefix_and_cc_boundary(action, note, eligible) -> None:
    from src.application.notify_symbols import _build_notification_block_compact

    out = _build_notification_block_compact(
        symbol_name="NVDA", action_label=action, contract="2026-11-20 95P+110C",
        income_line="权利金=95 | 年化 8%", contract_line="Put=95 | Call=110 | DTE=30",
        risk_line="风险=中性", detail_line="", note=note,
    )
    assert out.count("备注｜") == int(eligible and action != "CC")
    if eligible and action != "CC":
        assert f"备注｜{note}" in out


@pytest.mark.parametrize("annual", ["", "-", "缺失(告警未提供年化)"])
def test_compact_missing_annual_does_not_invent_zero_or_warning(annual) -> None:
    from src.application.notify_symbols import build_notification

    alerts = (
        "## 低优先级\nNVDA | combo_yield | 2026-11-20 95P+110C | "
        f"年化 {annual} | DTE 30 | 中性 | put_strike 95 | call_strike 110\n"
    )
    compact = build_notification("", alerts)
    assert "年化" not in compact
    assert "净权利金 -" in compact
    assert "仅供观察" not in compact
    assert "· 卖" not in compact


@pytest.mark.parametrize("annual,expected", [(-0.08, "-8.0%"), (0.0, "0.0%"), (0.08, "8.0%")])
def test_manual_combo_stages_preserve_annual_and_observation(tmp_path: Path, monkeypatch, annual, expected) -> None:
    from src.application import alert_engine
    from src.application.pipeline_reporting import run_pipeline_alert_stage, run_pipeline_notification_stage

    warning = "当前组合收益推荐未通过优先级阈值，仅供观察。"
    display_loader = alert_engine._load_symbol_display_map
    outputs = []
    for account, symbol in (("invented735a", "NVDA"), ("invented735b", "0700.HK")):
        account_root = tmp_path / "output_accounts" / account
        reports, state = account_root / "reports", account_root / "state"
        reports.mkdir(parents=True)
        state.mkdir()
        display_root = account_root / "absent-display-config"
        display_root.mkdir()

        def load_isolated_display(_base, *, state_dir=None):
            assert state_dir == state
            return display_loader(display_root, state_dir=state_dir)

        monkeypatch.setattr(alert_engine, "_load_symbol_display_map", load_isolated_display)
        summary = reports / "symbols_summary.csv"
        row = {
            "symbol": symbol, "strategy": "combo_yield", "candidate_count": 1,
            "top_contract": "2026-11-20 95P+110C", "annualized_return": annual,
            "net_income": 95, "dte": 30, "strike": 95, "risk_label": "中性",
            "net_credit": 95, "put_bid": 3, "put_strike": 95, "call_strike": 110,
            "call_ask": 1.5, "call_delta": 0.15,
        }
        with summary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        before = summary.read_bytes()
        alerts, changes = reports / "alerts.txt", reports / "changes.txt"
        previous = state / "symbols_summary_prev.csv"
        result = run_pipeline_alert_stage(
            summary_input=summary, output=alerts, changes_output=changes,
            previous_summary=previous, state_dir=state, update_snapshot=False,
        )
        assert result.alert_text == alerts.read_text(encoding="utf-8")
        compact_path, legacy_path = reports / "compact.txt", reports / "legacy.txt"
        compact = run_pipeline_notification_stage(
            alerts_input=alerts, changes_input=changes, output=compact_path,
        )
        with pytest.warns(DeprecationWarning, match="Legacy Tick renderer"):
            legacy = run_pipeline_notification_stage(
                alerts_input=alerts, changes_input=changes, output=legacy_path, render_style="legacy",
            )
        assert compact_path.read_text(encoding="utf-8") == compact
        assert legacy_path.read_text(encoding="utf-8") == legacy
        assert summary.read_bytes() == before
        assert not previous.exists()
        assert f"年化 {annual * 100:.2f}%" in result.alert_text
        assert f"年化 {annual * 100:.2f}%" in legacy
        assert f"年化 {expected}" in compact
        assert compact.count(warning) == int(annual <= 0)
        assert legacy.count(warning) == int(annual <= 0)
        assert "净权利金 95" in compact and "卖3/买1.5" in compact
        assert "30天" in compact and "Call Δ 0.15" in compact
        assert "## 低优先级" in result.alert_text if annual <= 0 else "## 高优先级" in result.alert_text
        assert f"[{account}] {symbol}" in legacy
        assert "备注｜" not in compact if annual > 0 else f"备注｜{warning}" in compact
        outputs.append(compact)
    assert "NVDA" in outputs[0] and "0700.HK" not in outputs[0]
    assert "0700.HK" in outputs[1] and "NVDA" not in outputs[1]
