from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.inbound import InboundRequest, handle_inbound_request
from src.application.inbound.contracts import InboundToolCall
from src.application.inbound.feishu import feishu_payload_to_inbound_request, handle_feishu_payload
from src.application.inbound.parser import parse_inbound_text
from src.application.inbound.policy import PURE_READ_TOOLS, check_sender_allowed, enforce_tool_allowed
from src.application.inbound.renderer import render_inbound_text


def _write_inbound_runtime_config(tmp_path: Path) -> tuple[Path, Path]:
    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_runtime_cfg(str(data_cfg_path)), ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path, sqlite_path


def _write_symbols_runtime_config(tmp_path: Path) -> Path:
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(json.dumps({"option_positions": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_runtime_cfg(str(data_cfg_path)), ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def _runtime_cfg(data_config_ref: str) -> dict:
    return {
        "accounts": ["sy"],
        "portfolio": {
            "broker": "富途",
            "source": "futu",
            "account": "sy",
            "data_config": data_config_ref,
        },
        "templates": {
            "put_base": {
                "sell_put": {
                    "min_annualized_net_return": 0.1,
                    "min_net_income": 50,
                    "min_open_interest": 10,
                    "min_volume": 1,
                    "max_spread_ratio": 0.3,
                }
            }
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {"source": "futu", "limit_expirations": 8},
                "use": ["put_base"],
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 45,
                    "min_strike": 100,
                    "max_strike": 120,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }


def _enable_inbound_trade_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_TRADE_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1")


def _enable_inbound_symbol_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_SYMBOL_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1")


def test_inbound_parser_maps_core_read_only_commands() -> None:
    assert parse_inbound_text("状态").name == "runtime_status"
    assert parse_inbound_text("健康检查").name == "healthcheck"

    positions = parse_inbound_text("持仓 sy")
    assert positions.name == "option_positions_open"
    assert positions.arguments == {"account": "sy", "status": "open"}

    income = parse_inbound_text("收益 sy 本月", now_fn=lambda: date(2026, 5, 19))
    assert income.name == "monthly_income_report"
    assert income.arguments == {"account": "sy", "month": "2026-05"}

    last_month = parse_inbound_text("收益 lx 上月", now_fn=lambda: date(2026, 1, 3))
    assert last_month.arguments == {"account": "lx", "month": "2025-12"}

    logs = parse_inbound_text("日志 20260515T182459Z-474761")
    assert logs.name == "runtime_logs"
    assert logs.arguments["run_id"] == "20260515T182459Z-474761"


def test_inbound_parser_requires_clarification_for_missing_account() -> None:
    with pytest.raises(AgentToolError) as exc:
        parse_inbound_text("持仓")

    assert exc.value.code == "NEEDS_CLARIFICATION"
    assert "账户" in exc.value.message


def test_inbound_policy_allows_sender_and_rejects_non_pure_read_tool() -> None:
    allowed = check_sender_allowed(channel="feishu", sender_id="ou_1", allowed_senders="feishu:ou_1")
    assert allowed.allowed is True

    denied = check_sender_allowed(channel="feishu", sender_id="ou_2", allowed_senders="feishu:ou_1")
    assert denied.allowed is False
    assert denied.reason == "sender_not_allowed"

    with pytest.raises(AgentToolError) as exc:
        enforce_tool_allowed(InboundToolCall(tool_name="scan_opportunities", payload={"config_key": "us"}))

    assert exc.value.code == "PERMISSION_DENIED"
    assert "inbound.manual_trade" not in PURE_READ_TOOLS


def test_inbound_parser_maps_manual_trade_and_symbol_operations() -> None:
    open_intent = parse_inbound_text("记录开仓 sy 0700.HK short put strike 450 exp 2026-05-28 6张 premium 2.35 multiplier 100")
    assert open_intent.name == "manual_trade_open"
    assert open_intent.arguments == {
        "account": "sy",
        "symbol": "0700.HK",
        "option_type": "put",
        "side": "short",
        "contracts": 6,
        "strike": 450.0,
        "expiration_ymd": "2026-05-28",
        "multiplier": 100.0,
        "premium_per_share": 2.35,
    }

    close_intent = parse_inbound_text("记录平仓 sy 0700.HK short put strike 450 exp 2026-05-28 2张 close 1.2")
    assert close_intent.name == "manual_trade_close"
    assert close_intent.arguments["contracts_to_close"] == 2
    assert close_intent.arguments["close_price"] == 1.2

    assert parse_inbound_text("确认记录 in_abc123").arguments == {"operation_id": "in_abc123"}
    assert parse_inbound_text("取消记录 in_abc123").name == "manual_trade_cancel"

    assert parse_inbound_text("查看监控标的").name == "symbol_list"
    symbol_add = parse_inbound_text("增加监控标的 700 put")
    assert symbol_add.name == "symbol_add"
    assert symbol_add.arguments == {"symbol": "700", "sell_put_enabled": True, "sell_call_enabled": False}
    symbol_edit = parse_inbound_text("修改监控标的 HK.00700 sell_put.max_strike=480")
    assert symbol_edit.name == "symbol_edit"
    assert symbol_edit.arguments == {"symbol": "HK.00700", "set": {"sell_put.max_strike": 480}}
    assert parse_inbound_text("删除监控标的 腾讯").arguments == {"symbol": "腾讯"}
    assert parse_inbound_text("确认监控 in_abc123").name == "symbol_confirm"


def test_inbound_manual_trade_preview_and_confirm_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    preview = handle_inbound_request(
        InboundRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_open_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert preview["ok"] is True
    assert preview["tool_name"] == "inbound.manual_trade"
    assert preview["data"]["response_text"].startswith("交易记录预览：开仓")
    assert "未写入账本" in preview["data"]["response_text"]

    operation_id = preview["data"]["operation_id"]
    confirmed = handle_inbound_request(
        InboundRequest(
            text=f"确认记录 {operation_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_open_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is True
    assert "交易已写入 OM 本地账本：开仓" in confirmed["data"]["response_text"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert len(repo.list_trade_events()) == 1


def test_inbound_manual_trade_preview_and_confirm_close(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.ledger.repository as ledger_repository

    _enable_inbound_trade_write(monkeypatch)
    cfg_path, sqlite_path = _write_inbound_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    open_preview = handle_inbound_request(
        InboundRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 2张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_close_open_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    open_id = open_preview["data"]["operation_id"]
    handle_inbound_request(
        InboundRequest(
            text=f"确认记录 {open_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_close_open_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    close_preview = handle_inbound_request(
        InboundRequest(
            text="记录平仓 sy NVDA short put strike 100 exp 2026-06-19 1张 close 1.0",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_close_preview",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    assert close_preview["ok"] is True
    assert close_preview["data"]["response_text"].startswith("交易记录预览：平仓")

    close_id = close_preview["data"]["operation_id"]
    confirmed = handle_inbound_request(
        InboundRequest(
            text=f"确认记录 {close_id}",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_close_confirm",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert confirmed["ok"] is True
    assert "交易已写入 OM 本地账本：平仓" in confirmed["data"]["response_text"]
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    assert len(repo.list_trade_events()) == 2


def test_inbound_symbol_add_edit_remove_preview_and_confirm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _enable_inbound_symbol_write(monkeypatch)
    cfg_path = _write_symbols_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    listed = handle_inbound_request(
        InboundRequest(text="查看监控标的", sender_id="ou_1", channel="feishu", message_id="msg_symbol_list", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert listed["ok"] is True
    assert listed["tool_name"] == "inbound.symbols"
    assert "当前监控标的" in listed["data"]["response_text"]

    add_preview = handle_inbound_request(
        InboundRequest(text="增加监控标的 700 put", sender_id="ou_1", channel="feishu", message_id="msg_symbol_add", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert add_preview["ok"] is True
    assert "校准为：0700.HK" in add_preview["data"]["response_text"]
    add_id = add_preview["data"]["operation_id"]
    add_confirm = handle_inbound_request(
        InboundRequest(text=f"确认监控 {add_id}", sender_id="ou_1", channel="feishu", message_id="msg_symbol_add_confirm", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert add_confirm["ok"] is True

    edit_preview = handle_inbound_request(
        InboundRequest(text="修改监控标的 HK.00700 sell_put.max_strike=480", sender_id="ou_1", channel="feishu", message_id="msg_symbol_edit", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    edit_id = edit_preview["data"]["operation_id"]
    edit_confirm = handle_inbound_request(
        InboundRequest(text=f"确认监控 {edit_id}", sender_id="ou_1", channel="feishu", message_id="msg_symbol_edit_confirm", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert edit_confirm["ok"] is True

    remove_preview = handle_inbound_request(
        InboundRequest(text="删除监控标的 腾讯", sender_id="ou_1", channel="feishu", message_id="msg_symbol_remove", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    remove_id = remove_preview["data"]["operation_id"]
    remove_confirm = handle_inbound_request(
        InboundRequest(text=f"确认监控 {remove_id}", sender_id="ou_1", channel="feishu", message_id="msg_symbol_remove_confirm", config_path=str(cfg_path), audit_db=str(audit_db)),
        allowed_senders="feishu:ou_1",
    )
    assert remove_confirm["ok"] is True

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [item["symbol"] for item in current["symbols"]] == ["NVDA"]


def test_inbound_write_operations_are_disabled_by_default(tmp_path: Path) -> None:
    cfg_path = _write_symbols_runtime_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"

    trade_out = handle_inbound_request(
        InboundRequest(
            text="记录开仓 sy NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_disabled_trade",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )
    symbol_out = handle_inbound_request(
        InboundRequest(
            text="增加监控标的 700 put",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_disabled_symbol",
            config_path=str(cfg_path),
            audit_db=str(audit_db),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert trade_out["ok"] is False
    assert trade_out["error"]["code"] == "PERMISSION_DENIED"
    assert symbol_out["ok"] is False
    assert symbol_out["error"]["code"] == "PERMISSION_DENIED"


def test_inbound_handle_executes_read_only_tool_and_replays_duplicate_message(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}]},
        )

    request = InboundRequest(
        text="收益 sy 2026-05",
        sender_id="ou_1",
        channel="feishu",
        message_id="msg_1",
        audit_db=str(audit_db),
    )

    first = handle_inbound_request(request, execute_tool_fn=_execute_tool, allowed_senders="feishu:ou_1")
    second = handle_inbound_request(request, execute_tool_fn=_execute_tool, allowed_senders="feishu:ou_1")

    assert first["ok"] is True
    assert first["data"]["tool_call"] == {
        "tool_name": "monthly_income_report",
        "payload": {"config_key": "us", "account": "sy", "month": "2026-05"},
    }
    assert "基于 OM 本地账本" in first["data"]["response_text"]
    assert second["meta"]["idempotent_replay"] is True
    assert calls == [("monthly_income_report", {"config_key": "us", "account": "sy", "month": "2026-05"})]

    with sqlite3.connect(audit_db) as conn:
        row = conn.execute(
            """
            SELECT intent_name, tool_name, decision, result_ok, duplicate_count, last_duplicate_sender_id
            FROM inbound_command_audit
            """
        ).fetchone()

    assert row == ("monthly_income_report", "monthly_income_report", "allowed", 1, 1, "ou_1")


def test_inbound_monthly_income_renderer_prefers_return_summary() -> None:
    intent = parse_inbound_text("收益 lx 2026-05")
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="monthly_income_report",
            ok=True,
            data={
                "summary": [{"month": "2026-05", "account": "lx", "currency": "USD"}],
                "return_summary": [
                    {
                        "month": "2026-05",
                        "account": "lx",
                        "net_return_rate": 0.0681,
                        "net_income_cny": 36097.23,
                        "cash_secured_cny": 530385.93,
                        "annualized_basis_days": 19,
                        "annualized_net_return_rate": 1.3074,
                        "premium_return_rate": 0.0697,
                    }
                ],
            },
        ),
    )

    assert "lx 2026-05 收益摘要" in text
    assert "净收益率：6.81%" in text
    assert "净收入：CNY 36,097" in text
    assert "按 19 天折年化：130.74%" in text


def test_inbound_monthly_income_renderer_explains_incomplete_summary() -> None:
    intent = parse_inbound_text("收益 sy 2026-05")
    text = render_inbound_text(
        intent=intent,
        tool_result=build_response(
            tool_name="monthly_income_report",
            ok=True,
            data={
                "summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}],
                "return_summary": [
                    {
                        "month": "2026-05",
                        "account": "sy",
                        "net_return_rate": None,
                        "net_income_cny": None,
                        "cash_secured_cny": None,
                        "annualized_basis_days": 20,
                        "annualized_net_return_rate": None,
                        "premium_return_rate": None,
                    }
                ],
                "diagnostics": [
                    {
                        "account": "sy",
                        "month": "2026-05",
                        "status": "incomplete",
                        "matched_trade_events_count": 0,
                        "matched_lots_count": 13,
                        "closed_lots_count": 0,
                        "premium_rows_count": 0,
                        "cash_secured_available": False,
                        "missing_fields": ["cash_secured", "closed_lots", "currency_conversion", "premium"],
                    }
                ],
            },
        ),
    )

    assert "sy 2026-05 暂无可计算收益。" in text
    assert "账本缺少已平仓/close 数据" in text
    assert "匹配事件：0，持仓 lot：13，已平仓 lot：0，权利金行：0。" in text
    assert "缺失项：cash_secured、closed_lots、currency_conversion、premium" in text


def test_inbound_audit_keeps_monthly_income_diagnostics(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={
                "summary": [],
                "return_summary": [],
                "diagnostics": [
                    {
                        "account": "sy",
                        "month": "2026-05",
                        "status": "empty",
                        "matched_trade_events_count": 0,
                        "matched_lots_count": 13,
                        "closed_lots_count": 0,
                        "premium_rows_count": 0,
                        "missing_fields": ["income_rows", "closed_lots", "premium"],
                    }
                ],
            },
        )

    out = handle_inbound_request(
        InboundRequest(
            text="收益 sy 2026-05",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_diag",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1",
    )

    assert out["data"]["response_text"].startswith("sy 2026-05 暂无可计算收益")
    with sqlite3.connect(audit_db) as conn:
        response_json = conn.execute("SELECT response_json FROM inbound_command_audit").fetchone()[0]

    stored = json.loads(response_json)
    diagnostics = stored["data"]["tool_result"]["data"]["diagnostics"]
    assert diagnostics[0]["status"] == "empty"
    assert diagnostics[0]["matched_lots_count"] == 13


def test_inbound_duplicate_message_from_other_sender_is_denied_and_marked(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        return build_response(tool_name=tool_name, ok=True, data={"summary": []})

    first = handle_inbound_request(
        InboundRequest(text="收益 sy", sender_id="ou_1", channel="feishu", message_id="msg_1", audit_db=str(audit_db)),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1,feishu:ou_2",
    )
    second = handle_inbound_request(
        InboundRequest(text="收益 sy", sender_id="ou_2", channel="feishu", message_id="msg_1", audit_db=str(audit_db)),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1,feishu:ou_2",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"]["code"] == "PERMISSION_DENIED"

    with sqlite3.connect(audit_db) as conn:
        row = conn.execute(
            "SELECT duplicate_count, last_duplicate_sender_id, last_duplicate_decision FROM inbound_command_audit"
        ).fetchone()

    assert row == (1, "ou_2", "sender_conflict")


def test_inbound_handle_denies_unknown_remote_sender_and_audits(tmp_path: Path) -> None:
    audit_db = tmp_path / "inbound.sqlite3"
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={})

    out = handle_inbound_request(
        InboundRequest(
            text="持仓 sy",
            sender_id="ou_bad",
            channel="feishu",
            message_id="msg_bad",
            audit_db=str(audit_db),
        ),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_good",
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "PERMISSION_DENIED"
    assert calls == []

    with sqlite3.connect(audit_db) as conn:
        row = conn.execute("SELECT decision, error_code FROM inbound_command_audit").fetchone()

    assert row == ("denied", "PERMISSION_DENIED")


def test_feishu_payload_adapter_extracts_text_message_and_calls_inbound(tmp_path: Path) -> None:
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1", "user_id": "user_1"}},
            "message": {
                "message_id": "om_1",
                "message_type": "text",
                "content": json.dumps({"text": '<at user_id="bot">Bot</at> 收益 sy 2026-05'}, ensure_ascii=False),
            },
        },
    }
    calls: list[tuple[str, dict]] = []

    def _execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}]},
        )

    request = feishu_payload_to_inbound_request(payload, audit_db=str(tmp_path / "audit.sqlite3"))
    assert request == InboundRequest(
        text="收益 sy 2026-05",
        sender_id="ou_1",
        channel="feishu",
        message_id="om_1",
        config_key="us",
        audit_db=str(tmp_path / "audit.sqlite3"),
    )

    out = handle_feishu_payload(
        payload,
        audit_db=str(tmp_path / "audit.sqlite3"),
        execute_tool_fn=_execute_tool,
        allowed_senders="feishu:ou_1",
    )

    assert out["ok"] is True
    assert out["tool_name"] == "inbound.feishu"
    assert out["data"]["response_text"].startswith("收益统计完成")
    assert calls == [("monthly_income_report", {"config_key": "us", "account": "sy", "month": "2026-05"})]


def test_feishu_payload_adapter_ignores_non_message_events() -> None:
    out = handle_feishu_payload(
        {
            "schema": "2.0",
            "header": {"event_id": "evt_1", "event_type": "im.message.message_read_v1"},
            "event": {},
        }
    )

    assert out["ok"] is True
    assert out["data"]["kind"] == "ignored_event"
    assert out["data"]["reason"] == "unsupported_event_type"


def test_inbound_cli_wires_request(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    seen: list[InboundRequest] = []

    def _handle(request: InboundRequest) -> dict:
        seen.append(request)
        return build_response(
            tool_name="inbound.handle",
            ok=True,
            data={"response_text": "状态查询完成。"},
        )

    monkeypatch.setattr(cli, "handle_inbound_request", _handle)

    rc = cli.main(
        [
            "inbound",
            "handle",
            "--text",
            "状态",
            "--sender",
            "ou_1",
            "--channel",
            "feishu",
            "--message-id",
            "msg_1",
            "--audit-db",
            str(tmp_path / "audit.sqlite3"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "inbound.handle"
    assert seen == [
        InboundRequest(
            text="状态",
            sender_id="ou_1",
            channel="feishu",
            message_id="msg_1",
            config_key="us",
            audit_db=str(tmp_path / "audit.sqlite3"),
        )
    ]


def test_inbound_cli_feishu_wires_payload(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    seen: list[dict] = []

    def _handle(payload: dict, **kwargs) -> dict:
        seen.append({"payload": payload, "kwargs": kwargs})
        return build_response(
            tool_name="inbound.feishu",
            ok=True,
            data={"response_text": "状态查询完成。"},
        )

    monkeypatch.setattr(cli, "handle_feishu_payload", _handle)
    payload_path = tmp_path / "feishu.json"
    payload_path.write_text(json.dumps({"event": {"message": {"content": "{}"}}}), encoding="utf-8")

    rc = cli.main(
        [
            "inbound",
            "feishu",
            "--input-file",
            str(payload_path),
            "--audit-db",
            str(tmp_path / "audit.sqlite3"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["tool_name"] == "inbound.feishu"
    assert seen == [
        {
            "payload": {"event": {"message": {"content": "{}"}}},
            "kwargs": {"config_key": "us", "config_path": None, "audit_db": str(tmp_path / "audit.sqlite3")},
        }
    ]


def test_inbound_cli_feishu_reports_invalid_json(capsys) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main(["inbound", "feishu", "--input-json", "{bad"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INPUT_ERROR"


def test_inbound_cli_feishu_ws_check_reports_redacted_config(capsys, monkeypatch) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "app_1")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "secret_1")
    monkeypatch.setenv("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "ou_1")
    monkeypatch.setattr("src.application.inbound.feishu_ws.is_feishu_ws_sdk_available", lambda: True)

    rc = cli.main(
        [
            "inbound",
            "feishu-ws",
            "--check",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"]["settings"]["app_id_configured"] is True
    assert "secret_1" not in json.dumps(payload, ensure_ascii=False)


def test_inbound_cli_feishu_ws_rejects_secret_override_flags(capsys) -> None:
    import src.interfaces.cli.main as cli

    try:
        cli.main(["inbound", "feishu-ws", "--app-id", "app_1", "--check"])
    except SystemExit as exc:
        assert int(exc.code or 0) == 2
    else:
        raise AssertionError("expected argparse to reject --app-id")
    _ = capsys.readouterr()
