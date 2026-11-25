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
    elif scenario == "pending":
        assert row["book_shares_remaining"] == 100
        assert row["quantity_status"] == "pending"
        assert row["shares_remaining"] is None
        assert row["stock_unrealized_pnl"] is None
        assert row["remaining_pnl_with_put"] is None
    elif scenario == "priced":
        assert row["stock_unrealized_pnl"] == "490"
        assert row["quote"]["quote_time_ms"] == stamp - 30_000
        assert row["quantity_status"] == "ledger_only"
        assert result["data"]["summary"]["by_currency"]["USD"]["stock_unrealized_pnl"]["value"] == "490.00"
    elif scenario in {"wrong_account_quote", "wrong_broker_quote"}:
        assert row["spot"] is None
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
