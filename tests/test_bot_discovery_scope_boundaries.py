"""Real read owners retain Bot market/account authorization after discovery."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.application.bot import tools
from src.application.bot.host import run_contract
from tests.test_bot_cross_market_read import _scope
from tests.test_bot_python_runtime import MODEL, answer, call, script


def _observation(fixed, name, args, mode):
    from src.application.bot.contracts import BotRequest, BotScope, new_id
    from src.application.bot.service import prepare_contract

    request = BotRequest(request_id=new_id("scope_test"), source_entry="test", user_message="读取已授权数据",
                         explicit_scope=BotScope(config_key=fixed["config_key"], config_path=fixed["config_path"]),
                         execution_environment="eval", trusted_tool_scope=fixed)
    contract = prepare_contract(request, reference_year=2026)
    replies = [call("get_tool_schema", {"name": name}, id="schema")] if mode == "index" else []
    replies += [call(name, args, id="business"), answer("测试读取完成。")]
    result = run_contract(contract, model_settings=MODEL,
                          process_environ={"OM_BOT_TOOL_DISCOVERY": mode}, model_request=script(replies, []))
    assert result.ok, result
    return next(e.payload for e in result.events if e.type == "tool_result" and e.payload["tool_name"] == name)


@pytest.mark.parametrize("mode", ["full", "index"])
@pytest.mark.parametrize("args", [{}, {"account": "lx"}])
@pytest.mark.parametrize("producer_checks", [False, True])
def test_quality_status_requires_account_and_scopes_final_evidence(monkeypatch, tmp_path, mode, args, producer_checks):
    from src.application.quality.model import check_result, dataset_status, summarize, validated_quality_status
    from src.application.quality.paths import default_quality_artifact_path

    _configs, fixed = _scope(tmp_path, monkeypatch, ["us"])
    at = "2026-11-05T00:00:00Z"
    status = {
        "schema_version": "investment.quality_status.v1",
        "producer": {"service": "options-monitor", "producer_version": "test",
                     "policy_version": "quality-policy-v1", "instance_id": "scope-fixture"},
        "observed_at_utc": at,
        "runtime": {"status": "unhealthy", "as_of_utc": at, "checks": [
            check_result(check_id="US_OK", status="pass", scope={"account": "lx", "market": "us"},
                         observed_at_utc=at, reason_code="US_OK", message="US runtime"),
            check_result(check_id="HK_FAILURE", status="fail", scope={"account": "lx", "market": "hk"},
                         observed_at_utc=at, reason_code="HK_FAILURE", message="HK private runtime"),
        ], "extensions": {"runtime_status_errors": [{"market": "hk", "error": "HK private detail"}]}},
        "datasets": [dataset_status(dataset_id="om.current_decision_evidence",
                                    scope={"account": account, "market": market}, status="trusted",
                                    as_of_utc=at, checks=[])
                     for account, market in (("lx", "us"), ("unconfigured", "us"), ("lx", "hk"))],
        "incidents": [{"incident_id": "hk-secret", "fingerprint": "a" * 64, "subject_id": "hk", "state": "new",
                       "severity": "blocking", "scope": {"market": "hk"}, "preexisting": False,
                       "first_seen_at_utc": at, "last_seen_at_utc": at, "reason_code": "HK_FAILURE",
                       "occurrence_count": 1, "blocked_consumers": [], "evidence_refs": []}],
        "extensions": {"authoritative_refresh_scopes": [{"account": "lx", "market": "hk"}]},
    }
    if producer_checks:
        from datetime import datetime
        from src.application.quality.runtime_checks import build_runtime_checks

        status["runtime"]["checks"] = build_runtime_checks(runtime_statuses=[{
            "config": {"config_key": "hk"},
            "trade_intake": {"enabled": True, "sources": [{
                "enabled": True, "account": "lx", "name": "hk-listener", "status": "failed",
                "heartbeat_age_seconds": 0, "health": {"healthy_max_seconds": 600, "unhealthy_after_seconds": 1500},
            }]},
        }], observed_at_utc=at, now=datetime.fromisoformat(at.replace("Z", "+00:00")))
        assert any(item.get("scope", {}).get("account") == "lx" and not item["scope"].get("market")
                   for item in status["runtime"]["checks"])
    status["summary"] = summarize(status)
    assert validated_quality_status(status) is not None
    artifact = default_quality_artifact_path()
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(status))
    event = _observation(fixed, "quality_status", args, mode)
    if not args:
        assert not event["ok"] and event["error"]["code"] == "INPUT_ERROR"
        return
    assert event["ok"] and event["tool_input"] == {"account": "lx", "market": "us"}
    data = event["data"]
    assert [row["scope"] for row in data["datasets"]] == [{"account": "lx", "market": "us"}]
    assert [row["check_id"] for row in data["runtime"]["checks"]] == ([] if producer_checks else ["US_OK"])
    assert data["runtime"]["status"] == ("unknown" if producer_checks else "healthy")
    assert data["incidents"] == []
    assert data["summary"]["dataset_counts"]["trusted"] == 1
    assert "extensions" not in data and "extensions" not in data["runtime"]
    assert "HK private" not in json.dumps(data)
    assert validated_quality_status(data) is not None


@pytest.mark.parametrize("mode", ["full", "index"])
@pytest.mark.parametrize("grant", [["us"], ["hk"]])
def test_account_wide_performance_rejects_single_market_before_owner(monkeypatch, tmp_path, mode, grant):
    from src.application.bot.model_config import load_bot_read_scope

    configs, fixed = _scope(tmp_path, monkeypatch, ["us"])
    Path(fixed["bot_config_path"]).write_text(json.dumps({"bot": {"enabled": True, "read_markets": grant}}))
    markets, generation = load_bot_read_scope(config_path=fixed["bot_config_path"], primary_market=grant[0])
    fixed = {**fixed, "config_key": grant[0], "config_path": str(configs[grant[0]][0]),
             "read_markets": sorted(markets), "read_generation": generation}
    invoked = []

    def forbidden(*args, **kwargs):
        invoked.append(args)
        raise AssertionError("account-wide report executed without both market grants")

    monkeypatch.setattr(tools, "execute_tool", forbidden)
    event = _observation(fixed, "option_performance_report", {"account": "lx"}, mode)
    assert not event["ok"] and event["error"]["code"] == "INPUT_ERROR"
    assert "both US and HK" in event["error"]["message"] and invoked == []


@pytest.mark.parametrize("account", [None, "lx"])
def test_account_wide_performance_verifies_both_config_account_authorities(monkeypatch, tmp_path, account):
    source = Path(__file__).resolve().parents[1] / "configs/examples/config.yaml.example"
    text = source.read_text()
    changed = tmp_path / "account-split.yaml"
    changed.write_text(text.replace("hk:\n    accounts: [lx]", "hk:\n    accounts: [other]", 1).replace(
        "accounts:\n  lx:", "accounts:\n  other:\n    type: futu\n    futu_account_id: REPLACE_OTHER\n  lx:", 1))
    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"], source=changed)
    assert configs["hk"][1]["accounts"] == ["other"]
    args = {"config_key": "us", **({"account": account} if account else {})}
    payload, error = tools.build_tool_payload("option_performance_report", args, fixed_input=fixed)
    assert payload is None and error


@pytest.mark.parametrize("mode", ["full", "index"])
def test_authorized_account_wide_performance_keeps_real_two_market_reducer(monkeypatch, tmp_path, mode):
    from domain.domain.ledger import ContractKey
    from src.application.agent_tools import positions
    from src.application.ledger import api as ledger_api
    from tests.test_performance_service_v2 import _event

    _configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    us = replace(_event("open-us", "open", "2026-09-02T10:00:00"), lot_id="lot-us")
    hk_key = ContractKey.from_values(broker="富途", account="lx", underlying_symbol="0700.HK",
                                     option_type="put", expiration_ymd="2026-09-30", strike=370)
    hk = replace(_event("open-hk", "open", "2026-09-02T10:00:00", fx_rate=None),
                 contract_key=hk_key, currency="HKD", lot_id="lot-hk")
    monkeypatch.delenv("OM_PERFORMANCE_SERVICE_URL", raising=False)
    monkeypatch.setattr(positions, "open_performance_query_ledger_from_data_config",
                        lambda *args, **kwargs: (tmp_path / "fixture-data.json", object()))
    monkeypatch.setattr(ledger_api, "trade_event_log", lambda repo: [event.to_dict() for event in (us, hk)])
    event = _observation(fixed, "option_performance_report", {
        "config_key": "us", "account": "lx", "period": "month", "month": "2026-09"}, mode)
    assert event["ok"], event
    cash = event["data"]["period_option_net_cashflow"]["by_currency"]
    assert {currency: row["total"]["amount"] for currency, row in cash.items()} == {"HKD": 200.0, "USD": 200.0}
    assert event["data"]["scope"]["accounts"] == ["lx"]


@pytest.mark.parametrize("mode", ["full", "index"])
@pytest.mark.parametrize("grant", [["us"], ["us", "hk"]])
def test_configured_symbol_alias_cannot_switch_unverified_market(monkeypatch, tmp_path, mode, grant):
    from src.application.config_yaml import build_yaml_runtime_config_file

    _configs, fixed = _scope(tmp_path, monkeypatch, grant)
    root = Path(__file__).resolve().parents[1]
    system = json.loads((root / "configs/system.json").read_text())
    system["defaults"]["intake"]["symbol_aliases"]["CHINA_OIL"] = "0700.HK"
    system_path = tmp_path / "system-alias.json"
    system_path.write_text(json.dumps(system))
    for market in ("us", "hk"):
        build_yaml_runtime_config_file(repo_root=root, market=market,
            config_path=root / "configs/examples/config.yaml.example", system_config_path=system_path,
            output_config_path=tmp_path / f"config.{market}.json")
    event = _observation(fixed, "symbol_config_read", {"symbol": "CHINA_OIL", "strategy": "csp"}, mode)
    if grant == ["us"]:
        assert not event["ok"] and event["error"]["code"] == "INPUT_ERROR"
    else:
        assert event["ok"], event
        assert event["tool_input"]["config_key"] == "hk"
        assert event["tool_input"]["symbol"] == "0700.HK"
        assert event["data"]["canonical_symbol"] == "0700.HK"
        assert Path(event["data"]["config_path"]).name == "config.hk.json"
