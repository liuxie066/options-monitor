from __future__ import annotations

import pytest

from src.application.agent_tools.operations_impl import normalize_option_positions_read_input
from src.application.agent_tools.positions import OPTION_POSITIONS_READ_TOOL, _option_positions_output_contract
from src.application.bot.control.command_parser import parse_bot_control_command
from src.application.bot.control.renderer import _render_assigned_stock_lifecycle


def test_period_normalization_preserves_current_request_and_legacy_defaults():
    current = normalize_option_positions_read_input({"assignment_month": "2026-11"})
    assert current == {"assignment_month": "2026-11", "action": "assigned-stock", "status": "all"}
    assert "as_of_ms" not in current
    assert normalize_option_positions_read_input({"action": "assigned-stock"}) == {"action": "assigned-stock"}
    assert normalize_option_positions_read_input({"action": "assigned-stock", "as_of_ms": 100})["as_of_ms"] == 100


@pytest.mark.parametrize("extra", [{"status": "open"}, {"as_of_ms": 100}, {"action": "events"}])
def test_period_normalization_rejects_conflicting_legacy_filters(extra):
    with pytest.raises(ValueError):
        normalize_option_positions_read_input({"assignment_year": 2026, **extra})


def test_period_fields_reach_gateway_and_bot_schemas():
    tool = OPTION_POSITIONS_READ_TOOL
    names = {"assignment_year", "assignment_month", "assignment_start_date", "assignment_end_date"}
    assert names <= set(tool.input_schema)
    assert names <= set(tool.bot_input_schema["properties"])
    contract = _option_positions_output_contract({"action": "assigned-stock", "assignment_year": 2026})
    assert "summary" in contract["fact_fields"]
    assert "coverage" in contract["fact_fields"]


def test_deterministic_period_command_includes_closed_lots():
    intent = parse_bot_control_command("/assigned-stock lx FUTU month=2026-11 no-refresh")
    assert intent.arguments["assigned_stock_status"] == "all"
    assert intent.arguments["assignment_month"] == "2026-11"
    assert intent.arguments["refresh_quotes"] is False
    assert "as_of_ms" not in intent.arguments
    old = parse_bot_control_command("/assigned-stock lx FUTU")
    assert old.arguments["assigned_stock_status"] == "open"


def test_period_command_rejects_duplicate_and_partial_status():
    for command in ("/assigned-stock month=2026-11 open", "/assigned-stock year=2026 year=2025"):
        with pytest.raises(Exception):
            parse_bot_control_command(command)


def test_period_renderer_uses_full_owner_totals_and_preserves_null():
    text = _render_assigned_stock_lifecycle({
        "query": {"assignment_month": "2026-11"},
        "summary": {"by_currency": {"USD": {
            "stock_unrealized_pnl": {"value": "-126", "coverage": "partial"},
            "remaining_pnl_with_put": {"value": None, "coverage": "unavailable"},
        }}},
        "coverage": {"status": "partial"},
        "rows": [],
    })
    assert "-126" in text
    assert "待核实/不可用" in text
    assert "0 条" not in text
    assert "账本估算" in text


@pytest.mark.parametrize("scenario", ["open", "sold", "pending", "priced", "wrong_account_quote", "wrong_broker_quote"])
def test_public_period_schema_to_real_report_refreshes_current_quote(tmp_path, monkeypatch, scenario):
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import src.application.positions.assignment_period_report as report
    from src.application.ledger import api
    from src.application.ledger.manual_trades import persist_manual_open_event
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.tool_execution import execute_tool
    from tests.test_agent_plugin_smoke import _public_cfg_with_futu

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr("src.application.agent_tools.positions.assert_quality_allows", lambda *a, **kw: None)
    path = tmp_path / "output_shared/state/option_positions.sqlite3"
    writer = SQLiteOptionPositionsRepository(path)
    clock = datetime.fromisoformat("2026-11-25T15:00:00+00:00")
    stamp = int(clock.timestamp() * 1000)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)

    monkeypatch.setattr(report, "datetime", FrozenDateTime)
    instant = stamp - 5000
    persist_manual_open_event(
        writer, broker="富途", account="lx", symbol="NVDA", option_type="put", side="short",
        contracts=1, currency="USD", strike=100, multiplier=100, expiration_ymd="2026-12-18",
        premium_per_share=2.5, opened_at_ms=instant - 1000,
    )
    api.record_manual_assignment(
        writer, lot_id=writer.list_position_lots()[0]["record_id"], contracts_to_close=1,
        stock_side="buy", stock_qty=100, stock_price=100, as_of_ms=instant,
    )
    if scenario in {"priced", "wrong_account_quote", "wrong_broker_quote"}:
        # Explicit isolated fixture facts: actual stock settlement fee, not an
        # estimated/missing zero silently relabeled as actual by the report.
        from tests.ledger_sqlite_test_support import connect_ledger_fixture
        # Seed only this temporary DB's frozen source fixture before querying;
        # the production query never enters a writer or repairs these facts.
        with connect_ledger_fixture(path) as conn:
            for event_id, encoded in conn.execute("SELECT event_id,event_json FROM trade_events").fetchall():
                event = json.loads(encoded)
                if event.get("event_type") == "assignment":
                    settlement = event["raw_payload"]["stock_settlement"]
                    settlement["fees"] = 10
                    settlement["fee_provenance"] = {"basis": "actual", "source": "fixture"}
                    conn.execute("UPDATE trade_events SET event_json=? WHERE event_id=?", (json.dumps(event), event_id))
        api.refresh_position_lot_projection(writer)
    if scenario == "sold":
        from src.application.positions.workflows import execute_manual_assigned_stock_sale
        assignment = next(r for r in writer.list_trade_events() if r.get("event_type") == "assignment")
        execute_manual_assigned_stock_sale(
            writer, target_lot_id="assigned-stock-" + assignment["event_id"],
            account="lx", broker="富途", symbol="NVDA", currency="USD",
            shares=100, price=105, trade_time_ms=instant + 1000, dry_run=False,
        )
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}")
    cfg = _public_cfg_with_futu(str(data_config))
    cfg["accounts"] = ["lx"]
    cfg["account_settings"]["lx"] = cfg["account_settings"].pop("user1")
    cfg["portfolio"]["account"] = "lx"
    cfg["trade_intake"]["account_mapping"]["futu"] = {"999000000000000001": "lx"}
    config = tmp_path / "config.us.json"
    config.write_text(json.dumps(cfg))
    monkeypatch.setattr(report, "read_stock_sell_observations", lambda *a, **kw: {
        "observations": ([{"account": "lx", "broker": "富途", "symbol": "NVDA", "currency": "USD",
                           "shares": 40, "trade_time_ms": instant + 1000, "source_deal_id": "pending-sell"}]
                         if scenario == "pending" else []),
        "coverage": "complete", "source_history": "unknown",
    })
    monkeypatch.setattr(report, "current_exchange_rate_snapshot", lambda **kw: {})
    calls = []

    def refresh(rows, **kwargs):
        calls.append((rows, kwargs))
        quotes = []
        if scenario in {"priced", "wrong_account_quote", "wrong_broker_quote"}:
            from src.application.opening_quote_evidence import normalize_underlier_observation
            evidence = normalize_underlier_observation(
                code="US.NVDA", market="US",
                snapshot_row={"code": "US.NVDA", "last_price": 105, "update_time": "2026-11-25 09:59:30",
                              "sec_status": "NORMAL", "suspension": False},
                market_state_row={"code": "US.NVDA", "market_state": "MORNING"}, now_utc=clock,
            ).to_dict()
            quotes = [{"symbol": "NVDA", "spot": 105, "quote_time_ms": stamp - 30_000,
                       "quote_observation": evidence, "account": "other" if scenario == "wrong_account_quote" else "lx",
                       "broker": "other" if scenario == "wrong_broker_quote" else "富途"}]
        return SimpleNamespace(quote_snapshots=quotes, diagnostics={"enabled": True, "status": "ok" if quotes else "missing_quote"})

    monkeypatch.setattr("src.application.agent_tools.positions.refresh_assigned_stock_quotes", refresh)
    result = execute_tool("option_positions_read", {
        "config_path": str(config), "action": "assigned-stock", "account": "lx",
        "assignment_year": clock.year,
        **({"opend_host": "diagnostic-only.invalid", "opend_port": 22222} if scenario == "priced" else {}),
    })
    assert result["ok"], result
    assert len(calls) == (0 if scenario in {"sold", "pending"} else 1), json.dumps(result, ensure_ascii=False)
    if calls:
        assert calls[0][1]["preserve_evidence"] is True
    assert result["data"]["row_count"] == 1
    assert result["data"]["schema_version"] == "option_positions_read.assignment_period.v1"
    assert result["data"]["coverage"]["status"] == "partial"
    assert result["data"]["coverage"]["source_history"] == "unknown"
    row = result["data"]["rows"][0]
    if scenario == "sold":
        assert row["book_shares_remaining"] == 0
        assert row["stock_unrealized_pnl"] == "0"
        assert row["remaining_pnl_with_put"] == "0"
        assert row["quote_status"] == "not_required"
        assert row["assigned_stock_unrealized_pnl"] == "0"
    elif scenario == "pending":
        assert row["book_shares_remaining"] == 100
        assert row["quantity_status"] == "pending"
        assert row["review_status"] == "pending"
        assert row["assigned_stock_unrealized_pnl"] is None
        assert row["shares_remaining"] is None
        assert row["stock_unrealized_pnl"] is None
        assert row["remaining_pnl_with_put"] is None
    elif scenario == "priced":
        assert calls[0][1]["host"] == "diagnostic-only.invalid"
        assert calls[0][1]["port"] == 22222
        assert row["stock_unrealized_pnl"] == "490"
        assert row["assigned_stock_unrealized_pnl"] == row["stock_unrealized_pnl"]
        assert row["quote_status"] == "fresh"
        assert row["spot_time"] == stamp - 30_000
        assert row["review_status"] == "partial"
        assert "lifecycle_pnl_net" not in row
        assert row["quote"]["quote_time_ms"] == stamp - 30_000
        assert row["quantity_status"] == "ledger_only"
        assert result["data"]["summary"]["by_currency"]["USD"]["stock_unrealized_pnl"]["value"] == "490.00"
    elif scenario in {"wrong_account_quote", "wrong_broker_quote"}:
        assert row["spot"] is None
        assert row["quote_status"] == "missing_quote"
        assert row["spot_time"] is None
        assert row["assigned_stock_unrealized_pnl"] is None
        assert row["stock_unrealized_pnl"] is None
    else:
        assert row["remaining_pnl_with_put"] is None


def test_inbound_period_payload_does_not_inherit_open_or_fill_as_of():
    from src.application.bot.control.contracts import BotInboundRequest, ControlCommand
    from src.application.bot.control.inbound_control import _tool_payload

    result = _tool_payload(
        ControlCommand(intent_name="assigned_stock_position_query", arguments={"assignment_year": 2026}),
        request=BotInboundRequest(text="今年被指派Put亏多少", sender_id="test", config_key="us"),
        tool_name="option_positions_read",
    )
    assert result["assignment_year"] == 2026
    assert result.get("status", "all") == "all"
    assert "as_of_ms" not in result


@pytest.mark.parametrize("field,value", [
    ("option_type", "call"), ("side", "long"), ("strike", 50), ("exp", "2026-12-18"),
    ("expiration_month", "2026-12"), ("query", {"symbol": "FUTU"}), ("limit", 20),
    ("cursor", "page"), ("include_total", True), ("position_effect", "close"),
])
def test_period_input_rejects_ignored_financial_filters(field, value):
    from src.application.agent_tool_contracts import AgentToolError

    with pytest.raises(AgentToolError, match="unsupported fields for assignment period query"):
        OPTION_POSITIONS_READ_TOOL.call({"action": "assigned-stock", "assignment_year": 2026, field: value})


@pytest.mark.parametrize("batch_count", [7, 34, 80])
def test_actual_bot_observation_keeps_complete_period_totals_when_details_exceed_budget(tmp_path, monkeypatch, batch_count):
    from copy import deepcopy
    from decimal import Decimal
    import hashlib

    import src.application.tool_execution as execution
    from src.application.bot.tools import MAX_NATIVE_OBSERVATION_TOKENS, conservative_json_tokens, model_observation
    from src.application.positions.assignment_period_report import summarize_lots

    original = execution.execute_tool
    responses = []

    def capture(*args, **kwargs):
        response = original(*args, **kwargs)
        responses.append(response)
        return response

    monkeypatch.setattr(execution, "execute_tool", capture)
    test_public_period_schema_to_real_report_refreshes_current_quote(tmp_path, monkeypatch, "priced")
    response = deepcopy(responses[0])
    data = response["data"]
    template = data["rows"][0]
    rows = []
    for index in range(batch_count):
        row = deepcopy(template)
        row["stock_lot_id"] = "assigned-stock-" + hashlib.sha256(f"batch-{index}".encode()).hexdigest()
        row["stock_unrealized_pnl"] = str(Decimal(index + 1) / Decimal(10))
        row["remaining_pnl_with_put"] = None if index == 0 else str(index)
        row["reasons"] = ["actual_put_fee_missing"] if index == 0 else ["source_history_not_proven"]
        rows.append(row)
    # This fixture explicitly models the actual report's repeated evidence shape;
    # totals are built by the same financial owner before wire projection.
    data["rows"] = rows
    data["assigned_stock_lots"] = rows
    data["row_count"] = data["matched_count"] = batch_count
    owner_summary = summarize_lots(rows)
    data["summary"] = owner_summary
    data["accounts"] = [{"account": "lx", "summary": owner_summary,
                         "symbols": [{"symbol": "NVDA", "lots": rows, "summary": owner_summary}]}]
    assert conservative_json_tokens(response) > MAX_NATIVE_OBSERVATION_TOKENS
    observed = model_observation("option_positions_read", response)
    assert observed["ok"] is True
    assert conservative_json_tokens(observed) <= MAX_NATIVE_OBSERVATION_TOKENS
    projected = observed["data"]
    for metric in ("stock_unrealized_pnl", "remaining_pnl_with_put"):
        actual = projected["summary"]["by_currency"]["USD"][metric]
        owner = owner_summary["by_currency"]["USD"][metric]
        assert actual["value"] == owner["value"]
        assert actual["exact_value"] == owner["exact_value"]
        assert actual["coverage"] == owner["coverage"]
        assert actual["basis"] == owner["basis"]
        account_metric = projected["accounts"][0]["summary"]["by_currency"]["USD"][metric]
        assert account_metric["value"] == owner["value"]
    assert projected["coverage"] == data["coverage"]
    assert projected["query"] == data["query"]
    assert projected["detail_projection"]["summary_complete"] is True
    assert projected["detail_projection"]["omitted_count"] == batch_count
    assert projected["detail_projection"]["detail_query"]["assignment_start_date"] == "2026-01-01"
    assert "stock_lot_id" in projected["detail_projection"]["narrow_by"]
    assert projected["detail_projection"]["detail_query"]["stock_lot_id"] == rows[0]["stock_lot_id"]
    index_count = len(projected["batch_index"])
    assert 0 < index_count <= min(batch_count, 64)
    assert projected["batch_index_coverage"] == {
        "status": "complete" if index_count == batch_count else "partial", "total_count": batch_count,
        "returned_count": index_count, "omitted_count": batch_count - index_count,
    }
    if batch_count <= 34:
        assert index_count == batch_count
    assert projected["batch_index"] == [
        {key: row[key] for key in ("stock_lot_id", "account", "symbol")} for row in rows[:index_count]
    ]
    assert "rows" not in projected and "assigned_stock_lots" not in projected
    assert len(data["rows"]) == batch_count  # no mutation of the operator result
