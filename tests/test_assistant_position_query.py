from __future__ import annotations

from datetime import date

from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.position_query import parse_position_query_text, position_query_intent_arguments
from src.application.assistant.reasoning import resolve_reasoning
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


def _position_intent(text: str, *, today: date, intent_name: str = "position_query") -> PerceptionResult:
    query = parse_position_query_text(text, today=today)
    return PerceptionResult(intent_name=intent_name, arguments=position_query_intent_arguments(query))


def test_position_query_parser_preserves_expiration_month_constraint() -> None:
    intent = _position_intent("sy 5月到期的持仓", today=date(2026, 5, 19))

    assert intent.intent_name == "position_query"
    assert intent.arguments == {
        "account": "sy",
        "status": "open",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }


def test_position_query_parser_preserves_month_without_account() -> None:
    intent = _position_intent("5月到期的持仓", today=date(2026, 5, 19))

    assert intent.intent_name == "position_query"
    assert intent.arguments == {
        "status": "open",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }


def test_position_query_parser_treats_detail_synonyms_as_plain_open_list() -> None:
    for text in ("持仓明细", "持仓明晰", "持仓详情", "当前持仓明细"):
        intent = _position_intent(text, today=date(2026, 6, 9))

        assert intent.arguments == {
            "status": "open",
            "limit": 50,
        }


def test_position_query_reasoning_preserves_query_constraints() -> None:
    perception = _position_intent("0700 5月 call 持仓", today=date(2026, 5, 19))
    resolution = resolve_reasoning(
        perception,
        request=AssistantRequest(text="0700 5月 call 持仓", sender_id="local", config_key="hk"),
    )

    assert perception.public_payload()["arguments"] == {
        "status": "open",
        "symbol": "0700.HK",
        "option_type": "call",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }
    assert resolution.status == "supported"
    assert resolution.tool_call is not None
    assert resolution.tool_call.tool_name == "option_positions_read"
    assert resolution.tool_call.payload == {
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


def test_reasoning_routes_exit_analysis_to_close_advice_read() -> None:
    resolution = resolve_reasoning(
        PerceptionResult(intent_name="position_exit_analysis", arguments={"option_type": "call", "side": "long"}),
        request=AssistantRequest(text="分析 long call", sender_id="local", config_key="us"),
    )

    assert resolution.status == "supported"
    assert resolution.tool_call is not None
    assert resolution.tool_call.tool_name == "close_advice_read"
    assert resolution.tool_call.payload == {
        "config_key": "us",
        "market_scope": "all",
        "query": {
            "status": "open",
            "option_type": "call",
            "side": "long",
            "limit": 50,
        },
    }


def test_exit_analysis_does_not_downgrade_to_position_query() -> None:
    perception = _position_intent(
        "泡泡玛特 long call 的持仓应该止盈吗",
        today=date(2026, 5, 29),
        intent_name="position_exit_analysis",
    )
    resolution = resolve_reasoning(
        perception,
        request=AssistantRequest(text="泡泡玛特 long call 的持仓应该止盈吗", sender_id="local", config_key="hk"),
    )

    assert perception.intent_name == "position_exit_analysis"
    assert perception.arguments["symbol"] == "9992.HK"
    assert perception.arguments["option_type"] == "call"
    assert perception.arguments["side"] == "long"
    assert resolution.status == "supported"
    assert resolution.tool_call is not None
    assert resolution.tool_call.tool_name == "close_advice_read"
    assert resolution.tool_call.payload == {
        "config_key": "hk",
        "query": {
            "status": "open",
            "symbol": "9992.HK",
            "option_type": "call",
            "side": "long",
            "limit": 50,
        },
    }


def test_position_query_parser_keeps_symbol_type_and_month_constraints() -> None:
    intent = _position_intent("0700 5月 call 持仓", today=date(2026, 5, 19))

    assert intent.arguments == {
        "status": "open",
        "symbol": "0700.HK",
        "option_type": "call",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }


def test_position_query_parser_does_not_treat_year_month_as_symbol() -> None:
    intent = _position_intent("lx 2026-05 到期 put 持仓", today=date(2026, 5, 19))

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


def test_position_query_read_model_sorts_by_expiration_before_limit() -> None:
    class _UnsortedRepo:
        def list_position_lots(self) -> list[dict]:
            return [
                {
                    "record_id": "lot-jul",
                    "fields": {
                        "broker": "富途",
                        "account": "sy",
                        "symbol": "PDD",
                        "option_type": "put",
                        "side": "short",
                        "status": "open",
                        "strike": 80,
                        "expiration_ymd": "2026-07-17",
                        "contracts": 1,
                        "contracts_open": 1,
                    },
                },
                {
                    "record_id": "lot-jun",
                    "fields": {
                        "broker": "富途",
                        "account": "lx",
                        "symbol": "FUTU",
                        "option_type": "put",
                        "side": "short",
                        "status": "open",
                        "strike": 110,
                        "expiration_ymd": "2026-06-12",
                        "contracts": 1,
                        "contracts_open": 1,
                    },
                },
                {
                    "record_id": "lot-no-exp",
                    "fields": {
                        "broker": "富途",
                        "account": "lx",
                        "symbol": "MSFT",
                        "option_type": "call",
                        "side": "long",
                        "status": "open",
                        "strike": 500,
                        "contracts": 1,
                        "contracts_open": 1,
                    },
                },
            ]

    rows = list_position_rows(
        _UnsortedRepo(),
        broker="富途",
        status="open",
        limit=2,
    )

    assert [row["record_id"] for row in rows] == ["lot-jun", "lot-jul"]


def test_position_query_read_model_filters_closed_positions() -> None:
    rows = list_position_rows(
        _Repo(),
        broker="富途",
        status="close",
        limit=50,
        expiration_before="2026-05-31",
    )

    assert [row["record_id"] for row in rows] == ["lot-tigr-put-may-closed"]
