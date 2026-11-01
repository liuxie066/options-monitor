"""Human and machine projections through the public CLI; all services isolated."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application import position_advice as service
from src.application.agent_tools import close_advice as public
from src.interfaces.cli import main as cli
from src.interfaces.cli.position_advice import render_position_advice
from test_position_advice_qa_public import isolated, request  # noqa: F401
from test_position_advice_pipeline import pipeline  # noqa: F401
from test_position_advice_inputs import manual
from test_position_advice_service import config


def evaluate(tmp_path, *extra):
    source = tmp_path / "request.json"
    source.write_text(json.dumps(request()))
    return cli.main(["position-advice", "evaluate", "--request-file", str(source), *extra])


def test_human_unavailable_includes_contract_time_mode_metrics_and_reason(tmp_path, capsys):
    assert evaluate(tmp_path) == 0
    output = capsys.readouterr().out
    for value in ("部分结果", "NVDA", "put", "行权价", "张数", "数据时间：不可用", "评估模式：不可评估",
                  "净兑现比例", "剩余最高年化", "结论：不可评估", "原因：", "hold 仅表示", "独立报告"):
        assert value in output
    report = next(tmp_path.glob("output_shared/position_advice_requests/*/position_advice.json"))
    assert json.loads(report.read_text())["status"] == "partial"
    assert not (tmp_path / "output_runs").exists()
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_explicit_json_is_original_business_payload_and_matches_saved_report(tmp_path, capsys):
    assert evaluate(tmp_path, "--format", "json") == 0
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert output.err == ""
    assert data["schema_version"] == "position_advice.output.v1"
    assert "data" not in data and "ok" not in data
    assert data == json.loads(Path(data["report_path"]).read_text())


@pytest.mark.parametrize("mode", ["text", "json"])
@pytest.mark.parametrize("failure", ["input", "execution"])
def test_failures_have_exit_two_and_no_traceback(tmp_path, monkeypatch, capsys, mode, failure):
    if failure == "input":
        source = tmp_path / "missing.json"
        result = cli.main(["position-advice", "evaluate", "--request-file", str(source), "--format", mode])
        code = "INPUT_ERROR"
    else:
        monkeypatch.setattr(service, "evaluate_position_advice", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("isolated failure")))
        result = evaluate(tmp_path, "--format", mode)
        code = "INTERNAL_ERROR"
    output = capsys.readouterr()
    assert result == 2 and output.err == ""
    if mode == "json":
        assert json.loads(output.out)["error"]["code"] == code
    else:
        assert "执行失败" in output.out and code in output.out


@pytest.mark.parametrize("state,label", [("disabled", "未启用"), ("unavailable", "插件不可用")])
def test_optional_plugin_status_visible_without_evaluation(tmp_path, monkeypatch, capsys, state, label):
    monkeypatch.setattr("src.application.position_advice_plugin.position_advice_plugin_status", lambda *_: state)
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: pytest.fail("disabled plugin requested market data"))
    assert evaluate(tmp_path) == 0
    assert label in capsys.readouterr().out
    assert not (tmp_path / "output_shared").exists()


def test_trusted_empty_holdings_are_successful_empty_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service, "_read_holdings", lambda **_: [])
    source = tmp_path / "empty.json"
    source.write_text(json.dumps({"account": "sample", "config_key": "us"}))
    assert cli.main(["position-advice", "evaluate", "--request-file", str(source)]) == 0
    output = capsys.readouterr().out
    assert "持仓评估：成功" in output and "合法空结果" in output


@pytest.mark.parametrize("mode", ["text", "json"])
def test_active_cli_reuses_sealed_pipeline(pipeline, tmp_path, monkeypatch, capsys, mode):
    _, provider, root = pipeline
    cfg = config()
    cfg.update(market_data={"provider": provider.name}, runtime={"prefetch_max_workers": 1})
    monkeypatch.setattr(public, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", cfg))
    monkeypatch.setattr(service, "_observe_sessions", lambda *_: {"NVDA": "MORNING"})
    now = datetime.now(timezone.utc)
    source = tmp_path / "current.json"
    source.write_text(json.dumps(request(positions=[manual(expiration=(now + timedelta(days=21)).date().isoformat(),
        input_as_of_utc=now.isoformat(), premium=2, contracts_open=1, multiplier=100, currency="USD")])))
    assert cli.main(["position-advice", "evaluate", "--request-file", str(source), "--format", mode]) == 0
    output = capsys.readouterr().out
    if mode == "text":
        assert "评估模式：当前评估" in output and "可考虑买回平仓（close）" in output
        assert "数据时间：不可用" not in output
        assert "净兑现比例：" in output and "%" in output
    else:
        data = json.loads(output)
        assert data["rows"][0]["assessment_mode"] == "current"
        assert data == json.loads(Path(data["report_path"]).read_text())
    assert any(name == "fetch" for name, _ in provider.calls)
    assert not (root / "output_runs").exists() and not list(root.rglob("*.sqlite*"))


def test_history_cannot_render_as_current_close_and_zero_metrics_survive():
    row = {"symbol": "NVDA", "option_type": "put", "expiration": "2026-11-20", "strike": 100,
           "assessment_mode": "historical_reference", "assessment_as_of_utc": "2026-10-30T15:00:00Z",
           "current_actionable": False, "recommendation_state": "not_evaluable", "reference_recommendation_state": "close",
           "net_capture_ratio": 0.0, "remaining_max_annualized_return": 0.0, "reason": "休市期间仅供历史参考"}
    original = json.dumps(row)
    output = render_position_advice({"status": "success", "rows": [row]})
    assert "评估模式：历史参考（不代表当前建议）" in output
    assert "历史结论：可考虑买回平仓（close）；当前不可据此平仓" in output
    assert "\n结论：可考虑" not in output
    assert "2026-10-30T15:00:00Z" in output and "0.00%" in output
    assert json.dumps(row) == original


def test_hold_describes_policy_only():
    output = render_position_advice({"status": "success", "rows": [{"assessment_mode": "current", "current_actionable": True,
        "recommendation_state": "hold", "reason": "未达到提前止盈条件"}]})
    assert "本策略未触发平仓条件（hold）" in output
    assert "原因：未达到提前止盈条件" in output
