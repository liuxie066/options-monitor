from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.agent_tool_contracts import build_response
from src.application.agent_tools.operations_impl import option_positions_read_tool
from src.application.bot.control.command_parser import parse_bot_control_command
from src.application.bot.control.contracts import BotInboundRequest, ControlCommand
from src.application.bot.control.inbound_control import execute_explicit_control
from src.application.bot.control.operation_store import InboundOperationStore
from src.application.bot.control.position_query import parse_position_query_text, position_query_intent_arguments
from src.application.ledger.api import list_position_rows


def _lot(record_id: str, **fields) -> dict:
    """Position-lot fixture with the field order the ledger read model emits."""
    return {
        "record_id": record_id,
        "fields": {"broker": "富途", **fields},
    }


class _Repo:
    def list_position_lots(self) -> list[dict]:
        return [
            _lot("lot-0700-call-may", account="sy", symbol="0700.HK", option_type="call",
                 side="short", status="open", strike=510, expiration_ymd="2026-05-28",
                 contracts=2, contracts_open=2),
            _lot("lot-0700-put-jun", account="sy", symbol="0700.HK", option_type="put",
                 side="short", status="open", strike=450, expiration_ymd="2026-06-29",
                 contracts=3, contracts_open=3),
            _lot("lot-tigr-put-may-closed", account="lx", symbol="TIGR", option_type="put",
                 side="short", status="close", strike=6, expiration_ymd="2026-05-22",
                 contracts=10, contracts_open=0),
        ]


def _position_intent(text: str, *, today: date, intent_name: str = "position_query") -> ControlCommand:
    query = parse_position_query_text(text, today=today)
    return ControlCommand(intent_name=intent_name, arguments=position_query_intent_arguments(query))


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


@pytest.mark.parametrize(("date_clause", "limit"), [
    ("2026-06-29", 50),
    ("2026-06-29 前10条", 10),
    ("2026-06-29 前10个", 10),
    ("2026-06-29 前10", 10),
    ("2026-06-29 前 10 条", 10),
    ("前10条 2026-06-29", 10),
    ("2026/06/29 前10条", 10),
    ("2026.06.29 前10条", 10),
    ("2026-06-29 前10条 前20个", 10),
    ("2026-06-29 limit 10", 10),
    ("2026-06-29 LIMIT10", 10),
    ("2026-06-29 前0条", 1),
    ("2026-06-29 前100条", 100),
    ("2026-06-29 前999条", 500),
])
def test_position_query_quantity_preserves_exact_expiration(date_clause: str, limit: int) -> None:
    # An explicit symbol isolates expiration from the existing numeric-symbol parser behavior.
    command = parse_bot_control_command(
        f"/positions account-a NVDA put short strike 100 all {date_clause} 30天内",
        now_fn=lambda: date(2026, 6, 1), accounts=["account-a"],
    )

    assert command is not None
    assert command.arguments == {
        "account": "account-a", "status": "all", "symbol": "NVDA", "option_type": "put",
        "side": "short", "strike": 100.0, "expiration": {"exact": "2026-06-29", "within_days": 30},
        "limit": limit,
    }


@pytest.mark.parametrize(("date_clause", "before", "limit"), [
    ("2026-06-29前", "2026-06-29", 50),
    ("2026-06-29之前 前10条", "2026-06-29", 10),
    ("前2026-06-29", "2026-06-29", 202),
    ("前 2026-06-29", "2026-06-29", 202),
    ("前2026-06-29 10条", "2026-06-29", 202),
    ("前2026/06/29 前10条", "2026-06-29", 202),
    ("前2026.06.29 前10条", "2026-06-29", 202),
    ("2026-06-29前 前10条", "2026-06-29", 10),
    ("before 2026-06-29 前10条", "2026-06-29", 10),
    ("2026-06-29 BEFORE 前10条", "2026-06-29", 10),
    ("前10条 前2026-06-29 前20个", "2026-06-29", 10),
    ("6月底前 前10条", "2026-06-30", 10),
    ("本月月末前 前10条", "2026-06-30", 10),
])
def test_position_query_quantity_preserves_genuine_cutoff(date_clause: str, before: str, limit: int) -> None:
    command = parse_bot_control_command(
        f"/positions account-a NVDA {date_clause}",
        now_fn=lambda: date(2026, 6, 1), accounts=["account-a"],
    )

    assert command is not None
    assert command.arguments == {
        "account": "account-a", "status": "open", "symbol": "NVDA",
        "expiration": {"before": before}, "limit": limit,
    }


@pytest.mark.parametrize("date_clause", ["2026-06 前10条", "6月 前10个", "本月 前10", "2026/06 limit 10"])
def test_position_query_quantity_preserves_month(date_clause: str) -> None:
    command = parse_bot_control_command(
        f"/positions account-a {date_clause}",
        now_fn=lambda: date(2026, 6, 1), accounts=["account-a"],
    )

    assert command is not None
    assert command.arguments == {
        "account": "account-a", "status": "open", "expiration": {"month": "2026-06"}, "limit": 10,
    }


@pytest.mark.parametrize(("date_clause", "expiration", "expected_ids"), [
    ("2026-06-29 前10条", {"exact": "2026-06-29"}, ["target"]),
    ("2026-06-29 limit 10", {"exact": "2026-06-29"}, ["target"]),
    ("2026-06-29前 前10条", {"before": "2026-06-29"}, [f"earlier-{i:02d}" for i in range(1, 11)]),
    ("2026-06-29前 前20条", {"before": "2026-06-29"}, [*[f"earlier-{i:02d}" for i in range(1, 11)], "target"]),
])
def test_position_query_command_limit_does_not_displace_exact_date(
    tmp_path: Path, date_clause: str, expiration: dict, expected_ids: list[str],
) -> None:
    class QueryRepo:
        ledger_store = SimpleNamespace(to_dict=lambda: {"kind": "synthetic-in-memory"})

        def list_position_lots(self) -> list[dict]:
            return [
                _lot("target", account="account-a", symbol="NVDA", option_type="put", side="short", status="open",
                     strike=100, expiration_ymd="2026-06-29", contracts=1, contracts_open=1),
                *[_lot(f"earlier-{i:02d}", account="account-a", symbol="NVDA", option_type="put", side="short",
                       status="open", strike=100, expiration_ymd=f"2026-06-{i:02d}", contracts=1, contracts_open=1)
                  for i in range(1, 11)],
            ]

    def forbid(*_args, **_kwargs):
        raise AssertionError("position query entered writer or provider adapter")

    calls = []

    def execute_tool(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        data, warnings, meta = option_positions_read_tool(
            payload,
            load_runtime_config=lambda **kw: (tmp_path / "config.json", {"accounts": ["account-a"]}),
            resolve_public_data_config_path=lambda *a: tmp_path / "data.json",
            normalize_broker=lambda value: "富途", normalize_account=lambda value: value.lower(),
            refresh_assigned_stock_quotes=forbid,
            resolve_option_positions_repo=lambda **kw: (tmp_path / "data.json", QueryRepo()),
            list_position_rows=list_position_rows, build_lot_event_history=forbid,
            inspect_projection_state=forbid, repo_base=lambda: tmp_path, mask_path=str,
        )
        return build_response(tool_name=tool_name, ok=True, data=data, warnings=warnings, meta=meta)

    text = f"/positions account-a {date_clause}"
    command = parse_bot_control_command(text, now_fn=lambda: date(2026, 6, 1), accounts=["account-a"])
    assert command is not None
    control = execute_explicit_control(
        command, request=BotInboundRequest(text=text, sender_id="synthetic", config_key="us"),
        command_id="position-query-limit", operation_store=InboundOperationStore(tmp_path / "inbound.sqlite3"),
        execute_tool_fn=execute_tool,
    )

    limit = 20 if "前20条" in date_clause else 10
    assert calls == [("option_positions_read", {
        "config_key": "us", "action": "list",
        "query": {"account": "account-a", "status": "open", "expiration": expiration, "limit": limit},
    })]
    assert control.ok and control.executed and not control.requires_confirmation
    assert control.result is not None
    data = control.result["data"]
    assert [row["lot_id"] for row in data["rows"]] == expected_ids
    assert data["row_count"] == len(expected_ids)
    assert data["filters"]["query"]["expiration"] == {
        "exact": expiration.get("exact"), "before": expiration.get("before"),
        "month": None, "after": None, "within_days": None,
    }
    assert list(tmp_path.iterdir()) == []


def _execute_control(command: ControlCommand, request: BotInboundRequest, tmp_path: Path):
    return execute_explicit_control(
        command,
        request=request,
        command_id="test_control",
        operation_store=InboundOperationStore(tmp_path / "inbound.sqlite3"),
        execute_tool_fn=lambda tool_name, payload: build_response(tool_name=tool_name, ok=True, data={}),
    )


def test_position_query_control_preserves_query_constraints(tmp_path: Path) -> None:
    command = _position_intent("0700 5月 call 持仓", today=date(2026, 5, 19))
    control = _execute_control(
        command,
        BotInboundRequest(text="0700 5月 call 持仓", sender_id="local", config_key="hk"),
        tmp_path,
    )

    assert command.public_payload()["arguments"] == {
        "status": "open",
        "symbol": "0700.HK",
        "option_type": "call",
        "expiration": {"month": "2026-05"},
        "limit": 50,
    }
    assert control.status == "supported"
    assert control.tool_name == "option_positions_read"
    assert control.payload == {
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


def test_control_routes_exit_analysis_to_close_advice_read(tmp_path: Path) -> None:
    control = _execute_control(
        ControlCommand(intent_name="position_exit_analysis", arguments={"option_type": "call", "side": "long"}),
        BotInboundRequest(text="分析 long call", sender_id="local", config_key="us"),
        tmp_path,
    )

    assert control.status == "supported"
    assert control.tool_name == "close_advice_read"
    assert control.payload == {
        "config_key": "us",
        "market_scope": "all",
        "query": {
            "status": "open",
            "option_type": "call",
            "side": "long",
            "limit": 50,
        },
    }


def test_exit_analysis_does_not_downgrade_to_position_query(tmp_path: Path) -> None:
    command = _position_intent(
        "泡泡玛特 long call 的持仓应该止盈吗",
        today=date(2026, 5, 29),
        intent_name="position_exit_analysis",
    )
    control = _execute_control(
        command,
        BotInboundRequest(text="泡泡玛特 long call 的持仓应该止盈吗", sender_id="local", config_key="hk"),
        tmp_path,
    )

    assert command.intent_name == "position_exit_analysis"
    assert command.arguments["symbol"] == "9992.HK"
    assert command.arguments["option_type"] == "call"
    assert command.arguments["side"] == "long"
    assert control.status == "supported"
    assert control.tool_name == "close_advice_read"
    assert control.payload == {
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

    assert [row["lot_id"] for row in rows] == ["lot-0700-call-may"]


def test_position_query_read_model_sorts_by_expiration_before_limit() -> None:
    class _UnsortedRepo:
        def list_position_lots(self) -> list[dict]:
            return [
                _lot("lot-jul", account="sy", symbol="PDD", option_type="put",
                     side="short", status="open", strike=80, expiration_ymd="2026-07-17",
                     contracts=1, contracts_open=1),
                _lot("lot-jun", account="lx", symbol="FUTU", option_type="put",
                     side="short", status="open", strike=110, expiration_ymd="2026-06-12",
                     contracts=1, contracts_open=1),
                _lot("lot-no-exp", account="lx", symbol="MSFT", option_type="call",
                     side="long", status="open", strike=500,
                     contracts=1, contracts_open=1),
            ]

    rows = list_position_rows(
        _UnsortedRepo(),
        broker="富途",
        status="open",
        limit=2,
    )

    assert [row["lot_id"] for row in rows] == ["lot-jun", "lot-jul"]


def test_position_query_read_model_filters_closed_positions() -> None:
    rows = list_position_rows(
        _Repo(),
        broker="富途",
        status="close",
        limit=50,
        expiration_before="2026-05-31",
    )

    assert [row["lot_id"] for row in rows] == ["lot-tigr-put-may-closed"]
