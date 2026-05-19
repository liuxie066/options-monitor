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
from src.application.inbound.policy import check_sender_allowed, enforce_tool_allowed
from src.application.inbound.renderer import render_inbound_text


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


def test_feishu_payload_adapter_returns_url_verification_challenge() -> None:
    out = handle_feishu_payload({"type": "url_verification", "challenge": "challenge-token"})

    assert out["ok"] is True
    assert out["data"]["kind"] == "url_verification"
    assert out["data"]["response"] == {"challenge": "challenge-token"}


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


def test_inbound_cli_feishu_gateway_check_reports_redacted_config(capsys, monkeypatch) -> None:
    import src.interfaces.cli.main as cli

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "app_1")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "secret_1")
    monkeypatch.setenv("OM_FEISHU_BOT_ENCRYPT_KEY", "encrypt_1")
    monkeypatch.setenv("OM_FEISHU_BOT_VERIFICATION_TOKEN", "token_1")
    monkeypatch.setenv("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "ou_1")

    rc = cli.main(
        [
            "inbound",
            "feishu-gateway",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--path",
            "/feishu/events",
            "--check",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"]["settings"]["app_id_configured"] is True
    assert "secret_1" not in json.dumps(payload, ensure_ascii=False)


def test_inbound_cli_feishu_gateway_rejects_secret_override_flags(capsys) -> None:
    import src.interfaces.cli.main as cli

    try:
        cli.main(["inbound", "feishu-gateway", "--app-id", "app_1", "--check"])
    except SystemExit as exc:
        assert int(exc.code or 0) == 2
    else:
        raise AssertionError("expected argparse to reject --app-id")
    _ = capsys.readouterr()
