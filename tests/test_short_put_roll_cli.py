"""Roll-down CLI projections with mocked tool execution and no runtime data."""
from contextlib import nullcontext
import json

import pytest

from src.application.agent_tool_contracts import build_response
from src.application import tool_execution
from src.interfaces.cli import main as cli
from src.interfaces.cli.position_advice import render_short_put_roll


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch):
    monkeypatch.setattr(cli, "command_environment", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        tool_execution, "execute_tool",
        lambda *args, **kwargs: pytest.fail("unexpected tool execution"),
    )


def request():
    return {
        "account": "sample", "lot_id": "sample-lot", "max_new_expiration": "2026-12-18",
        "min_new_strike": 90, "limit": 10,
    }


def result():
    return {
        "schema_version": "short_put_roll.output.v1", "status": "success", "outcome": "found",
        "account": "sample", "evaluated_at_utc": "2026-11-04T15:00:00Z",
        "position": {
            "symbol": "NVDA", "strike": 100, "expiration": "2026-11-14", "contracts_open": 1,
            "multiplier": 100, "currency": "USD", "lot_id": "sample-lot", "spot": 98, "old_otm_pct": -0.020408,
            "old_close_cost": 500, "old_remaining_max_annualized_return": 1.825,
        },
        "scope": {
            "max_new_expiration": "2026-12-18", "min_new_strike": 90,
            "old_otm_max_pct": 0.05, "new_put_must_be_otm": True,
            "enumerated_expirations": ["2026-11-14", "2026-12-14"],
        },
        "coverage": {
            "status": "complete", "enumeration_complete": True, "discovered": 12,
            "evaluated": 12, "matched": 2, "excluded": 10, "unassessed": 0,
        },
        "candidates": [{
            "contract": "US.NVDA261214P95000", "expiration": "2026-12-14", "strike": 95,
            "contracts": 1, "old_close_cost": 500, "new_net_premium": 540, "net_credit": 40,
            "old_capital_basis": 10000, "new_capital_basis": 9500,
            "new_otm_pct": 0.030612, "strike_drop_pct": 0.05, "old_dte": 10,
            "new_dte": 40, "extension_days": 30, "incremental_period_return": 0.00421,
            "incremental_annualized_return": 0.0384, "new_remaining_max_annualized_return": 0.5187,
            "old_remaining_max_annualized_return": 1.825, "remaining_annualized_change": -1.3063,
            "old_buy_fee": {
                "amount": 1.5, "currency": "USD", "fee_basis": "sample_buy_fee",
                "fee_schedule_version": "sample_fee.v1",
            },
            "new_sell_fee": {
                "amount": 2, "currency": "USD", "fee_basis": "sample_sell_fee",
                "fee_schedule_version": "sample_fee.v1",
            },
            "evidence": {"quote_as_of_utc": "2026-11-04T15:00:00Z", "quote_mode": "leg_cashflow_estimate"},
        }],
        "displayed": 1, "omitted": 1, "reason_codes": [],
        "report_path": "output_shared/position_advice_requests/sample/short_put_roll.json",
        "interpretation": "leg_cashflow_estimate",
    }


def install_tool(monkeypatch, data=None, error=None):
    calls = []

    def execute(tool_name, payload):
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=error is None, data=data, error=error)

    monkeypatch.setattr(tool_execution, "execute_tool", execute)
    return calls


def invoke(tmp_path, payload, *options):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return cli.main(["position-advice", "roll-down", "--request-file", str(path), *options])


def test_roll_down_dispatches_request_and_text_renders_cash_yields_and_fees(tmp_path, monkeypatch, capsys):
    data = result()
    calls = install_tool(monkeypatch, data)
    payload = request()
    assert invoke(tmp_path, payload, "--config-key", "us") == 0
    assert calls == [("short_put_roll_candidates", dict(payload, config_key="us"))]
    captured = capsys.readouterr()
    assert captured.err == ""
    output = captured.out
    for text in (
        "Short Put 向下移仓", "执行状态：请求完成（success）", "筛选结果：找到候选（found）",
        "账户：sample", "NVDA", "strike 100", "张数 1", "乘数 100", "旧仓价外幅度：-2.04%",
        "旧仓继续持有到期归零最高年化：182.50%", "单 lot", "≤5%", "严格价外", "旧 lot：sample-lot",
        "最大新到期日 2026-12-18（无默认）", "最低新 strike 90", "实际已请求到期日",
        "发现 12 / 已评价 12 / 匹配 2 / 排除 10 / 不可评估 0", "净增现金年化优先",
        "US.NVDA261214P95000", "strike 95", "降幅 5.00%", "新仓价外 3.06%",
        "到期 2026-12-14", "延后 30 天", "净增现金·双腿扣费后 40.00 USD", "净增现金年化 3.84%",
        "新仓到期归零最高年化 51.87%", "较旧仓变化 -130.63 pp", "资金分母（旧/新）",
        "DTE（旧/新）10/40", "买回费用：", "新卖出费用：", "sample_buy_fee", "sample_sell_fee",
        "sample_fee.v1", '"amount": 1.5', '"amount": 2', "报价与证据", "2026-11-04T15:00:00Z",
        "展示 1；省略 1", "单腿 bid/ask 现金估算", "旧腿按 ask 买回、新腿按 bid 卖出并分别扣费",
        "不是保证可成交的组合报价", "不等于交易盈利", "不是期望收益", "独立报告：",
    ):
        assert text in output
    assert "持仓评估：" not in output and "整体最优" not in output
    assert "净利润" not in output


@pytest.mark.parametrize("config_key", ["us", "hk"])
def test_json_is_original_roll_business_payload(tmp_path, monkeypatch, capsys, config_key):
    data = result()
    calls = install_tool(monkeypatch, data)
    payload = request()
    payload["config_key"] = "hk" if config_key == "us" else "us"
    assert invoke(tmp_path, payload, "--config-key", config_key, "--format", "json") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == data
    assert calls == [("short_put_roll_candidates", dict(payload, config_key=config_key))]


def test_put_roll_copy_keeps_fees_and_return_basis_out_of_parentheses() -> None:
    payload = result()
    payload["candidates"][0].update(net_credit=24.3734, new_capital_basis=7900)
    payload["candidates"].append(dict(payload["candidates"][0]))
    text = render_short_put_roll(payload)

    assert "净增现金·双腿扣费后 24.37 USD" in text
    assert "24.3734" not in text
    assert "净增现金期间回报 = 净现金流 ÷ 新仓担保资金" in text
    assert "年化按新到期剩余期限折算" in text
    assert text.count("移仓收益口径") == 1
    assert text.index("移仓收益口径") > text.index("2. US.NVDA")
    assert "（净增现金 /" not in text and "（）" not in text and "()" not in text
    assert "资金分母（旧/新）10000.00 USD / 7900.00 USD" in text


def test_cli_does_not_inject_expiration_or_display_defaults(tmp_path, monkeypatch, capsys):
    calls = install_tool(monkeypatch, error={"code": "INPUT_ERROR", "message": "max_new_expiration is required"})
    payload = {"account": "sample", "lot_id": "sample-lot"}
    assert invoke(tmp_path, payload, "--format", "json") == 2
    assert calls == [("short_put_roll_candidates", payload)]
    assert "max_new_expiration" not in calls[0][1] and "limit" not in calls[0][1]
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INPUT_ERROR"


@pytest.mark.parametrize("outcome,coverage,label,forbidden", [
    ("not_applicable", None, "旧仓价外幅度超过 5%，未搜索移仓候选", "无合格候选"),
    ("none", {"status": "complete", "enumeration_complete": True}, "覆盖完整的合法空结果", "缺失数据"),
    ("unable", {"status": "partial", "enumeration_complete": False}, "不可评估（unable", "无合格候选"),
])
def test_outcomes_do_not_confuse_unsearched_missing_and_complete_empty(
    tmp_path, monkeypatch, capsys, outcome, coverage, label, forbidden,
):
    data = result()
    data.update(outcome=outcome, coverage=coverage, candidates=[], displayed=0, omitted=0)
    if outcome == "unable":
        data.update(status="partial", reason_codes=["old_quote_stale", "candidate_chain_incomplete"])
    if outcome == "not_applicable":
        data["position"]["old_otm_pct"] = 0.08
        data["reason_codes"] = ["old_position_outside_roll_scope"]
    install_tool(monkeypatch, data)
    assert invoke(tmp_path, request()) == 0
    output = capsys.readouterr().out
    assert label in output and forbidden not in output
    assert "候选比较" not in output and "持仓评估：" not in output
    for reason in data["reason_codes"]:
        assert reason in output


def test_partial_found_keeps_candidates_and_discloses_gap_and_stale_reason(tmp_path, monkeypatch, capsys):
    data = result()
    data.update(status="partial", reason_codes=["candidate_quote_stale", "chain_enumeration_incomplete"])
    data["coverage"].update(status="partial", enumeration_complete=False, unassessed=3)
    install_tool(monkeypatch, data)
    assert invoke(tmp_path, request()) == 0
    output = capsys.readouterr().out
    for text in (
        "执行状态：部分结果（partial）", "筛选结果：找到候选（found）", "覆盖存在缺口",
        "排名仅涵盖已评价集合", "未评价范围可能仍有机会", "US.NVDA261214P95000",
        "candidate_quote_stale", "chain_enumeration_incomplete",
    ):
        assert text in output
    assert "合法空结果" not in output


@pytest.mark.parametrize("status,label", [("disabled", "插件未启用"), ("unavailable", "插件不可用")])
def test_plugin_state_is_distinct_from_incomplete_market_data(tmp_path, monkeypatch, capsys, status, label):
    data = {
        "schema_version": "short_put_roll.output.v1", "status": status, "outcome": "unable",
        "reason_codes": ["position_advice_" + status],
    }
    install_tool(monkeypatch, data)
    assert invoke(tmp_path, request()) == 0
    output = capsys.readouterr().out
    assert label in output and "筛选结果：不可评估" in output
    assert "合法空结果" not in output and "未搜索移仓候选" not in output


def test_unavailable_quote_dependency_is_not_reported_as_missing_plugin(tmp_path, monkeypatch, capsys):
    data = {
        "status": "unavailable", "outcome": "unable", "reason_codes": ["quote_dependency_unavailable"],
        "scope": {"max_new_expiration": "2026-12-18", "enumerated_expirations": []},
    }
    install_tool(monkeypatch, data)
    assert invoke(tmp_path, request()) == 0
    output = capsys.readouterr().out
    assert "执行状态：查询不可用（unavailable）" in output
    assert "quote_dependency_unavailable" in output and "插件不可用" not in output
    assert "实际已请求到期日：不可用" in output


def test_missing_diagnostics_render_unavailable_without_inventing_zeros(tmp_path, monkeypatch, capsys):
    data = {
        "status": "partial", "outcome": "found", "account": "sample", "position": None,
        "coverage": None, "candidates": [{"contract": "sample-contract", "old_buy_fee": None}],
    }
    install_tool(monkeypatch, data)
    assert invoke(tmp_path, request()) == 0
    output = capsys.readouterr().out
    assert "旧仓继续持有到期归零最高年化：不可用" in output
    assert "净增现金·双腿扣费后 不可用" in output and "买回费用：不可用" in output
    assert "覆盖：不可用" in output and "未提供诊断" in output
    assert "不可用" in output and "0.00%" not in output


@pytest.mark.parametrize("mode", ["text", "json"])
@pytest.mark.parametrize("kind", ["missing", "invalid_json", "array", "oversize"])
def test_bad_request_files_reuse_evaluate_error_exit(tmp_path, capsys, mode, kind):
    path = tmp_path / "request.json"
    if kind == "invalid_json":
        path.write_text("{", encoding="utf-8")
    elif kind == "array":
        path.write_text("[]", encoding="utf-8")
    elif kind == "oversize":
        path.write_text(" " * 262145, encoding="utf-8")
    assert cli.main(["position-advice", "roll-down", "--request-file", str(path), "--format", mode]) == 2
    captured = capsys.readouterr()
    assert captured.err == "" and "Traceback" not in captured.out
    if mode == "json":
        envelope = json.loads(captured.out)
        assert envelope["tool_name"] == "om" and envelope["ok"] is False
        assert envelope["error"]["code"] == "INPUT_ERROR"
    else:
        assert "Short Put 向下移仓执行失败 [INPUT_ERROR]" in captured.out
        assert "持仓评估执行失败" not in captured.out


@pytest.mark.parametrize("mode", ["text", "json"])
@pytest.mark.parametrize("code", ["INPUT_ERROR", "INTERNAL_ERROR"])
def test_tool_errors_keep_existing_exit_and_envelope(tmp_path, monkeypatch, capsys, mode, code):
    error = {"code": code, "message": "sample failure", "hint": "check request", "details": {"field": "lot_id"}}
    calls = install_tool(monkeypatch, error=error)
    assert invoke(tmp_path, request(), "--format", mode) == 2
    assert calls[0][0] == "short_put_roll_candidates"
    captured = capsys.readouterr()
    assert captured.err == "" and "Traceback" not in captured.out
    if mode == "json":
        envelope = json.loads(captured.out)
        assert envelope["tool_name"] == "om" and envelope["ok"] is False
        assert envelope["error"] == error
    else:
        assert f"Short Put 向下移仓执行失败 [{code}]" in captured.out
        assert "sample failure" in captured.out and "check request" in captured.out


def test_evaluate_still_uses_its_original_tool_and_heading(tmp_path, monkeypatch, capsys):
    payload = {"account": "sample", "symbols": ["NVDA"]}
    calls = install_tool(monkeypatch, {"status": "success", "rows": []})
    path = tmp_path / "evaluate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert cli.main(["position-advice", "evaluate", "--request-file", str(path)]) == 0
    assert calls == [("position_advice_evaluate", payload)]
    output = capsys.readouterr().out
    assert "持仓评估：成功" in output and "Short Put 向下移仓" not in output


def test_roll_help_documents_required_expiry_and_home_discovers_command(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["position-advice", "roll-down", "--help"])
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for text in ("--request-file", "max_new_expiration", "min_new_strike", "limit", "无默认"):
        assert text in output
    assert cli.main(["help"]) == 0
    assert "om position-advice roll-down --help" in capsys.readouterr().out


def test_invalid_format_is_argparse_error_before_environment_or_tool(monkeypatch, capsys):
    monkeypatch.setattr(cli, "command_environment", lambda *a, **kw: pytest.fail("environment loaded before parsing"))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["position-advice", "roll-down", "--request-file", "unused.json", "--format", "yaml"])
    assert exit_info.value.code == 2
    assert "--format" in capsys.readouterr().err


def test_renderer_does_not_mutate_business_result():
    data = result()
    before = json.dumps(data, sort_keys=True)
    render_short_put_roll(data)
    assert json.dumps(data, sort_keys=True) == before
