from __future__ import annotations

from datetime import date

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.parser import parse_inbound_text
from src.application.assistant.contracts import AssistantFrame, AssistantRequest
from src.application.assistant.frame_planner import frame_from_intent, tool_plan_from_frame
from src.application.ledger.read_model import list_position_rows


class _Repo:
    def list_position_lots(self) -> list[dict]:
        return [
            {
                "record_id": "lot-0700-call-may",
                "fields": {
                    "broker": "富途",
                    "account": "sy",
                    "symbol": "0700.HK",
                    "option_type": "call",
                    "side": "short",
                    "status": "open",
                    "strike": 510,
                    "expiration_ymd": "2026-05-28",
                    "contracts": 2,
                    "contracts_open": 2,
                },
            },
            {
                "record_id": "lot-0700-put-jun",
                "fields": {
                    "broker": "富途",
                    "account": "sy",
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "side": "short",
                    "status": "open",
                    "strike": 450,
                    "expiration_ymd": "2026-06-29",
                    "contracts": 3,
                    "contracts_open": 3,
                },
            },
            {
                "record_id": "lot-tigr-put-may-closed",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "TIGR",
                    "option_type": "put",
                    "side": "short",
                    "status": "close",
                    "strike": 6,
                    "expiration_ymd": "2026-05-22",
                    "contracts": 10,
                    "contracts_open": 0,
                },
            },
        ]


def test_position_query_parser_preserves_expiration_month_constraint() -> None:
    intent = parse_inbound_text("sy 5月到期的持仓", now_fn=lambda: date(2026, 5, 19))

    assert intent.name == "position_query"
    assert intent.arguments == {
        "account": "sy",
        "status": "open",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }


def test_position_query_parser_preserves_month_without_account() -> None:
    intent = parse_inbound_text("5月到期的持仓", now_fn=lambda: date(2026, 5, 19))

    assert intent.name == "position_query"
    assert intent.arguments == {
        "status": "open",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }


def test_position_query_frame_planner_preserves_query_constraints() -> None:
    intent = parse_inbound_text("0700 5月 call 持仓", now_fn=lambda: date(2026, 5, 19))
    frame = frame_from_intent(intent)
    plan = tool_plan_from_frame(
        frame,
        request=AssistantRequest(text="0700 5月 call 持仓", sender_id="local", config_key="hk"),
    )

    assert frame.public_payload()["payload"] == {
        "query": {
            "status": "open",
            "symbol": "0700.HK",
            "option_type": "call",
            "expiration": {"month": "2026-05"},
            "limit": 50,
        }
    }
    assert plan.tool_name == "option_positions_read"
    assert plan.payload == {
        "config_key": "hk",
        "action": "list",
        "query": {
            "status": "open",
            "symbol": "0700.HK",
            "option_type": "call",
            "expiration": {"month": "2026-05"},
            "limit": 50,
        },
    }


def test_frame_planner_rejects_safety_class_mismatch() -> None:
    frame = AssistantFrame(
        intent="runtime_status",
        payload={},
        safety_class="write_preview",
    )

    with pytest.raises(AgentToolError) as exc:
        tool_plan_from_frame(frame, request=AssistantRequest(text="状态", sender_id="local", config_key="us"))

    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.details == {
        "safety_class": "write_preview",
        "expected_safety_class": "read",
    }


def test_position_query_parser_keeps_symbol_type_and_month_constraints() -> None:
    intent = parse_inbound_text("0700 5月 call 持仓", now_fn=lambda: date(2026, 5, 19))

    assert intent.arguments == {
        "status": "open",
        "symbol": "0700.HK",
        "option_type": "call",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }


def test_position_query_parser_does_not_treat_year_month_as_symbol() -> None:
    intent = parse_inbound_text("lx 2026-05 到期 put 持仓", now_fn=lambda: date(2026, 5, 19))

    assert intent.arguments == {
        "account": "lx",
        "status": "open",
        "option_type": "put",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }


def test_position_query_read_model_filters_month_symbol_and_option_type() -> None:
    rows = list_position_rows(
        _Repo(),
        broker="富途",
        status="open",
        limit=50,
        symbol="HK.00700",
        option_type="call",
        expiration_month="2026-05",
    )

    assert [row["record_id"] for row in rows] == ["lot-0700-call-may"]


def test_position_query_read_model_filters_closed_positions() -> None:
    rows = list_position_rows(
        _Repo(),
        broker="富途",
        status="close",
        limit=50,
        expiration_before="2026-05-31",
    )

    assert [row["record_id"] for row in rows] == ["lot-tigr-put-may-closed"]
