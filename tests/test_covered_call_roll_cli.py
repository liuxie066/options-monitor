"""Covered-call public CLI/tool projection for credit and excluded non-credit."""
from contextlib import nullcontext
import json

import pytest

from src.application.agent_tool_contracts import build_response
from src.application.agent_tool_registry import get_tool_definition
from src.application import tool_execution
from src.interfaces.cli import main as cli
from src.interfaces.cli.position_advice import render_covered_call_roll


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch):
    monkeypatch.setattr(cli, "command_environment", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(tool_execution, "execute_tool", lambda *_args, **_kwargs: pytest.fail("unexpected tool execution"))


def result():
    return {"schema_version": "covered_call_roll.output.v1", "status": "success", "outcome": "found",
        "account": "sample", "evaluated_at_utc": "2026-11-04T15:00:00Z",
        "position": {"symbol": "NVDA", "strike": 100, "expiration": "2026-11-14", "contracts_open": 1,
            "multiplier": 100, "currency": "USD", "spot": 102},
        "stock_coverage": {"status": "covered", "required_shares": 100, "available_shares": 100},
        "scope": {"max_new_expiration": "2026-12-14"}, "coverage": {"status": "complete"},
        "candidates": [{"contract": "US.NVDA261214C00105000", "strike": 105, "expiration": "2026-12-14",
            "recommendation": "credit_roll", "cash_flow_type": "credit", "net_credit": 95, "net_debit": 0,
            "strike_increase": 5, "assignment_proceeds_uplift": 500, "assignment_net_improvement": 595,
            "assignment_improvement_annualized_return": 0.5323, "incremental_annualized_return": 0.0850,
            "extension_days": 30, "old_close_cost": 502, "new_net_premium": 597,
            "old_capital_basis": 10200, "new_capital_basis": 10200,
            "old_buy_fee": {"amount": 2, "currency": "USD"}, "new_sell_fee": {"amount": 3, "currency": "USD"}}],
        "displayed": 1, "omitted": 0, "reason_codes": [], "report_path": "/isolated/result.json"}


def invoke(tmp_path, payload, *args):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    return cli.main(["position-advice", "roll-up", "--request-file", str(path), *args])


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_roll_up_routes_to_covered_call_tool_and_keeps_original_json(tmp_path, monkeypatch, capsys, fmt):
    data, calls = result(), []
    def execute(name, payload):
        calls.append((name, payload))
        return build_response(tool_name=name, ok=True, data=data)
    monkeypatch.setattr(tool_execution, "execute_tool", execute)
    payload = {"account": "sample", "lot_id": "old", "max_new_expiration": "2026-12-14", "max_new_strike": 110}
    assert invoke(tmp_path, payload, "--config-key", "us", "--format", fmt) == 0
    assert calls == [("covered_call_roll_candidates", {**payload, "config_key": "us"})]
    output = capsys.readouterr().out
    if fmt == "json":
        assert json.loads(output) == data
    else:
        for text in ("Covered Call 上滚", "净收现金上滚", "95", "每股行权价提升",
                     "交股价提升总额", "500", "595", "当前股价×覆盖股数", "wheel", "不是已实现利润", "买回费用", "新卖出费用",
                     "净增现金年化优先", "仅推荐扣费净现金严格大于零", "零现金及净支出均排除", "non_positive_net_credit"):
            assert text in output
        assert "向下移仓" not in output
        assert "可考虑的净支出上滚" not in output
        assert "debit_roll" not in output


def test_renderer_preserves_credit_same_expiry_candidate():
    data = result()
    data["candidates"][0].update(expiration=data["position"]["expiration"], extension_days=0)
    output = render_covered_call_roll(data)
    assert "净收现金上滚" in output
    assert "到期 2026-11-14；延后 0 天" in output
    assert "扣费净现金 95.00 USD" in output


def test_renderer_explains_zero_and_debit_exclusion_without_recommending_them():
    data = result()
    data.update(outcome="none", candidates=[], displayed=0,
        exclusion_summary=[{"code": "non_positive_net_credit", "count": 2}])
    output = render_covered_call_roll(data)
    assert "non_positive_net_credit" in output
    assert "零现金及净支出均排除" in output
    assert "无合格候选" in output
    assert "候选比较" not in output
    assert "可考虑的净支出上滚" not in output
    assert "现金中性上滚" not in output


@pytest.mark.parametrize("status,outcome,coverage,label", [
    ("success", "not_applicable", None, "不适用"),
    ("success", "none", {"status": "complete"}, "覆盖完整的合法空结果"),
    ("partial", "unable", {"status": "partial"}, "不可评估"),
    ("disabled", "unable", None, "插件未启用"),
    ("unavailable", "unable", None, "查询不可用"),
])
def test_covered_call_renderer_preserves_outcome_meanings(status, outcome, coverage, label):
    data = result()
    data.update(status=status, outcome=outcome, coverage=coverage, candidates=[], displayed=0)
    data["reason_codes"] = ["insufficient_stock_coverage"] if outcome == "not_applicable" else []
    output = render_covered_call_roll(data)
    assert label in output
    if outcome == "not_applicable":
        assert "未搜索候选" in output
        assert "无合格候选" not in output
        assert "insufficient_stock_coverage" in output


def test_roll_up_input_error_is_not_a_successful_empty_query(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tool_execution, "execute_tool", lambda name, payload: build_response(
        tool_name=name, ok=False, error={"code": "INPUT_ERROR", "message": "max_new_expiration is required"}))
    assert invoke(tmp_path, {"account": "sample", "lot_id": "old"}) == 2
    output = capsys.readouterr().out
    assert "Covered Call 上滚执行失败 [INPUT_ERROR]" in output
    assert "无合格候选" not in output


def test_tool_contract_exposes_cost_and_no_business_writes():
    tool = get_tool_definition("covered_call_roll_candidates")
    schema = tool.execution_input_json_schema()
    assert set(schema["required"]) == {"account", "lot_id", "max_new_expiration"}
    assert schema["additionalProperties"] is False
    assert "max_new_strike" in schema["properties"] and "min_new_strike" not in schema["properties"]
    assert tool.side_effects == ("fetches_market_data", "writes_isolated_request_reports")
    assert "candidates[].net_debit" in tool.output_contract["fact_fields"]
    assert "candidates[].assignment_proceeds_uplift" in tool.output_contract["fact_fields"]
