from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest  # pyright: ignore[reportMissingImports]

import src.application.ledger.bootstrap as ledger_bootstrap
import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository


def _repo_with_open_event(tmp_path: Path):
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )
    event_id = repo.list_trade_events()[0]["event_id"]
    return repo, event_id


def _repo_with_assignment(
    tmp_path: Path,
    *,
    wheel_start_enabled: bool = False,
):
    from domain.domain.option_position_lots import OpenPositionCommand
    from src.application.ledger.commands import record_manual_assignment

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=1_000,
        ),
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])
    result = record_manual_assignment(
        repo,
        record_id=put_lot_id,
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=2_000,
        request_id="assignment-for-intervention-test",
        wheel_start_enabled=wheel_start_enabled,
    )
    assignment_event_id = str(result["result"]["event_id"])
    if wheel_start_enabled:
        from domain.domain.wheel import wheel_started_event_from_assignment

        assignment = next(
            row
            for row in repo.list_trade_events()
            if row["event_id"] == assignment_event_id
        )
        source_put_lot = next(
            row
            for row in repo.list_position_lots()
            if row["record_id"] == put_lot_id
        )
        with repo._writer_connection(begin_immediate=True) as conn:  # noqa: SLF001 - explicit legacy Wheel history fixture
            repo.append_wheel_event_once(
                wheel_started_event_from_assignment(
                    assignment,
                    source_put_lot,
                    recorded_at_ms=2_000,
                ),
                conn=conn,
            )
    return repo, assignment_event_id, f"assigned-stock-{assignment_event_id}"


def _durable_ledger_state(repo) -> dict:
    return deepcopy(
        {
            "trade_events": repo.list_trade_events(),
            "assigned_stock_events": repo.list_assigned_stock_events(),
            "wheel_events": repo.list_wheel_events(account="lx"),
            "position_lots": repo.list_position_lots(),
            "projection_source": repo.read_position_projection_source_state(),
        }
    )


def _append_assigned_stock_sale(
    repo,
    *,
    stock_lot_id: str,
    source_deal_id: str,
) -> dict:
    from src.application.positions.workflows import execute_manual_assigned_stock_sale

    preview = execute_manual_assigned_stock_sale(
        repo,
        target_stock_lot_id=stock_lot_id,
        shares=100,
        price=105.0,
        trade_time_ms=3_000,
        source_deal_id=source_deal_id,
        dry_run=True,
    )
    event = dict(preview["sale_event"])
    with repo._writer_connection(begin_immediate=True) as conn:  # noqa: SLF001 - canonical race fixture
        assert repo.upsert_assigned_stock_event(event, conn=conn) is True
    return event

def _attach_opend_time_evidence(
    repo,
    *,
    event_id: str,
    trade_time_ms: int = 900,
    quantity: int = 1,
) -> None:
    with repo._connect() as conn:  # noqa: SLF001 - exact persisted evidence fixture
        row = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        payload = json.loads(str(row["event_json"]))
        payload["raw_payload"]["opend_order_evidence"] = {
            "schema_version": "opend_order_evidence.v1",
            "provider": "opend",
            "classification": "single_order",
            "observed_at_ms": 3000,
            "orders": [
                {
                    "futu_account_id": "123",
                    "order_id": "order-1",
                    "deal_ids": ["deal-1"],
                    "quantity": quantity,
                    "price": "3.930000",
                    "trade_time_ms": trade_time_ms,
                }
            ],
        }
        payload["raw_payload"]["cash_conversions"] = {
            "option_trade_cash_gross": {"status": "observed"}
        }
        conn.execute(
            "UPDATE trade_events SET event_json=?, updated_at_ms=? WHERE event_id=?",
            (json.dumps(payload, ensure_ascii=False), 1500, event_id),
        )


def _install_legacy_trade_time_immutable_trigger(repo) -> None:
    with repo._connect() as conn:  # noqa: SLF001 - deployed-schema upgrade fixture
        conn.execute("DROP TRIGGER trg_trade_events_query_projection_immutable")
        conn.execute(
            """
            CREATE TRIGGER trg_trade_events_query_projection_immutable
            BEFORE UPDATE OF event_json, trade_time_ms ON trade_events
            WHEN NEW.trade_time_ms IS NOT OLD.trade_time_ms
              OR json_extract(NEW.event_json, '$.event_time_ms')
                 IS NOT json_extract(OLD.event_json, '$.event_time_ms')
            BEGIN
              SELECT RAISE(ABORT, 'trade event query projection is immutable');
            END
            """
        )


def _append_canonical_void_event(
    repo,
    *,
    target_event_id: str,
    event_id: str,
    event_time_ms: int,
) -> None:
    from domain.domain.ledger import TradeEvent
    from src.application.ledger.event_codec import stored_trade_event_to_ledger_event

    target_payload = next(
        item
        for item in repo.list_trade_events()
        if str(item.get("event_id") or "").strip() == str(target_event_id).strip()
    )
    target, diagnostics = stored_trade_event_to_ledger_event(target_payload)
    assert target is not None
    assert [item.code for item in diagnostics if item.severity == "error"] == []
    repo.upsert_trade_event(
        TradeEvent(
            event_id=event_id,
            event_type="void",
            event_time_ms=event_time_ms,
            contract_key=target.contract_key,
            contracts=0,
            price=0.0,
            currency=target.currency,
            source="test",
            multiplier=target.multiplier,
            target_event_id=target_event_id,
            raw_payload={},
        )
    )


def _insert_invalid_legacy_void_event(
    repo,
    *,
    target_event_id: str,
    event_id: str,
    event_time_ms: int,
) -> None:
    payload = {
        "event_id": event_id,
        "trade_time_ms": event_time_ms,
        "position_effect": "void",
        "raw_payload": {"void_target_event_id": target_event_id},
    }
    with sqlite3.connect(str(repo.db_path)) as conn:
        # This fixture deliberately represents a row written before the
        # pagination schema and its canonical write guards existed.
        for trigger_name in ledger_repository.TRADE_EVENT_PAGINATION_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        conn.execute(
            """
            INSERT INTO trade_events (event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, json.dumps(payload, ensure_ascii=False), event_time_ms, event_time_ms, event_time_ms),
        )


def test_trade_events_list_text_shows_trade_time_beijing(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, _event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["list", "--format", "text"]) == 0

    out = capsys.readouterr().out
    assert "time 1970-01-01 08:00:01 北京时间" in out


def test_trade_events_fee_sync_defaults_to_dry_run(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    config_path = tmp_path / "config.us.json"
    data_config = tmp_path / "data.json"
    calls: list[dict] = []

    class _Client:
        def __init__(self, **kwargs):
            calls.append({"client": kwargs})

        def close(self):
            calls.append({"closed": True})

    monkeypatch.setattr(cli, "load_runtime_config", lambda **_kwargs: (config_path, {"accounts": ["lx"]}))
    monkeypatch.setattr(cli, "resolve_position_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))
    monkeypatch.setattr(
        cli,
        "resolve_account_broker_binding_sets",
        lambda _values: {
            "lx": SimpleNamespace(
                ok=True,
                host="127.0.0.1",
                port=11111,
                trd_env="REAL",
                required_account_ids=("123",),
            )
        },
    )
    monkeypatch.setattr(cli, "OpenDHistoryDealClient", _Client)

    def _sync(_repo, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"sync": kwargs})
        return {
            "selected_order_count": 0,
            "actual_observation_count": 0,
            "reason_counts": {},
            "migration": {"status_counts": {}},
        }

    monkeypatch.setattr(cli, "sync_order_fees", _sync)

    result = cli.main(
        [
            "fees-sync",
            "--config-key",
            "us",
            "--account",
            "lx",
            "--start-date",
            "2026-08-01",
            "--end-date",
            "2026-08-23",
            "--format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["dry_run"] is True
    assert payload["write_applied"] is False
    assert next(item["sync"] for item in calls if "sync" in item)["apply"] is False
    assert next(item["sync"] for item in calls if "sync" in item)[
        "allowed_futu_account_ids"
    ] == ("123",)
    assert calls[-1] == {"closed": True}


def test_trade_events_list_treats_canonical_void_as_voided(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    _append_canonical_void_event(
        repo,
        target_event_id=event_id,
        event_id="canonical-void-open",
        event_time_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["list", "--status", "voided", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert [item["event_id"] for item in out] == [event_id]


def test_trade_events_list_ignores_invalid_void_when_filtering_active(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    _insert_invalid_legacy_void_event(
        repo,
        target_event_id=event_id,
        event_id="invalid-legacy-void-open",
        event_time_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["list", "--status", "active", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert [item["event_id"] for item in out] == [event_id]


def test_trade_events_show_json_includes_trade_time_beijing(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["show", event_id, "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["event"]["trade_time_ms"] == 1000
    assert out["event"]["trade_time_beijing"] == "1970-01-01 08:00:01 北京时间"


def test_trade_events_repair_dry_run_does_not_mutate(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(
        [
            "repair",
            event_id,
            "--strike",
            "500",
            "--futu-account-id",
            "123",
            "--order-id",
            "order-1",
            "--dry-run",
            "--format",
            "json",
        ]
    ) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert out["backup_path"] is None
    assert out["audit_id"].startswith("audit_")
    assert out["target_event"]["event_id"] == event_id
    assert out["repair_event"]["contract_key"]["strike"] == 500.0
    assert out["repair_event"]["raw_payload"]["futu_account_id"] == "123"
    assert out["repair_event"]["raw_payload"]["order_id"] == "order-1"
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["ledger_preflight"]["event_type"] == "repair"
    assert out["ledger_preflight"]["target_event_id"] == event_id
    assert out["projection_preview"]["position_lot_count"] == 1
    assert out["projection_preview"]["projection_diagnostic_count"] == 0
    assert len(repo.list_trade_events()) == 1
    assert repo.list_position_lots()[0]["fields"]["strike"] == 480.0


def test_trade_events_repair_apply_voids_and_replaces_event(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--confirm", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "applied"
    assert out["dry_run"] is False
    assert out["write_applied"] is True
    assert out["backup_path"] is None
    assert out["audit_id"].startswith("audit_")
    assert out["rollback_hint"]
    assert out["target_event_id"] == event_id
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["ledger_preflight"]["event_type"] == "repair"
    assert out["void_event_id"].startswith("manual-repair-void-")
    assert out["repair_event_id"].startswith("manual-repair-")
    events = repo.list_trade_events()
    assert len(events) == 3
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["strike"] == 500.0


@pytest.mark.parametrize("with_fx", [True, False])
@pytest.mark.parametrize(
    ("overrides", "expected_native"),
    [(["--symbol", "0883.HK", "--multiplier", "1000"], "480"),
     (["--symbol", "0883.HK"], "48"),
     (["--contracts", "1", "--price", "0.5"], "50")],
)
def test_repair_preserves_fees_and_rebuilds_cash_conversion_identity(
    monkeypatch, tmp_path: Path, capsys, with_fx, overrides, expected_native,
) -> None:
    from decimal import Decimal

    from domain.domain.ledger import ContractKey, TradeEvent
    from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
    from domain.domain.performance.cash_conversion import validate_observed_cash_conversion
    from src.application.cash_conversion import cash_fx_observation_facts
    from src.application.ledger import writer_trade_events
    from src.application.ledger.writer import persist_trade_event_object
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository
    import src.interfaces.cli.trade_events as cli

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "repair.sqlite3")
    event_time = 1774419413873
    fx = {"source": "tencent_quote", "rates": {"HKDCNY": 0.88092, "USDCNY": 6.9},
          "timestamp": "2026-03-25T06:16:53+00:00",
          "quote_timestamps": {pair: "2026-03-25T06:16:53+00:00" for pair in ("HKDCNY", "USDCNY")}}
    monkeypatch.setattr(writer_trade_events, "load_cash_fx_payload", lambda *a, **k: fx)
    event = TradeEvent(
        event_id="cnc-open", event_type="open", event_time_ms=event_time,
        contract_key=ContractKey.from_values(
            broker="富途", account="lx", underlying_symbol="CNC",
            option_type="call", position_side="short", strike=30,
            expiration_ymd="2026-03-30",
        ),
        contracts=2, price=0.24, multiplier=100, currency="HKD", fees=20,
        source="opend_push", raw_payload={"source_type": "broker_trade_event",
            "futu_account_id": "123", "order_id": "order-cnc", "fee_provenance": {
            "basis": "actual", "amount": "20", "source": "opend.order_fee_query",
        }},
    )
    persist_trade_event_object(repo, event)
    if with_fx:
        PerformanceEvidenceSQLiteRepository(repo.db_path).freeze_cash_fx_daily_rates(
            cash_fx_observation_facts(fx, observed_at_ms=event_time),
            migrated_at_ms=event_time,
        )
    before = deepcopy(repo.list_trade_events())
    assert before[0]["fees"] == 20, before[0]
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **kw: (tmp_path / "data.json", repo))
    args = ["repair", event.event_id, *overrides, "--format", "json"]
    assert cli.main([*args, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)["repair_event"]
    assert preview["fees"] == 20
    assert repo.list_trade_events() == before
    assert cli.main([*args, "--confirm"]) == 0
    applied = json.loads(capsys.readouterr().out)
    stored = next(row for row in repo.list_trade_events() if row["event_id"] == applied["repair_event_id"])
    assert stored["fees"] == preview["fees"] == 20
    assert stored["raw_payload"]["fee_provenance"] == before[0]["raw_payload"]["fee_provenance"]
    for payload in (preview, stored):
        repaired = TradeEvent.from_dict(payload)
        facts = {fact.fact_kind: fact for fact in cash_facts_for_trade_event(repaired)}
        assert facts["option_trade_cash_gross"].amount == Decimal(expected_native)
        assert facts["option_fee_cash"].amount == Decimal("-20")
        for fact in facts.values():
            conversion = fact.cash_conversion
            assert conversion["cash_fact_id"] == fact.fact_id
            assert Decimal(conversion["native_amount"]) == fact.amount
            if with_fx:
                amount, error = validate_observed_cash_conversion(
                    conversion, cash_fact_id=fact.fact_id, native_amount=fact.amount,
                    native_currency="HKD", effective_at_ms=event_time,
                )
                assert error is None, json.dumps(conversion)
                assert amount == fact.amount * Decimal("0.88092")
            else:
                assert conversion["status"] == "pending"
                assert conversion["amount_cny"] is None
    assert next(row for row in repo.list_trade_events() if row["event_id"] == event.event_id) == before[0]
    assert cli.main([*args, "--confirm"]) == 2
    capsys.readouterr()
    assert len(repo.list_trade_events()) == 3


def test_trade_events_repair_rejects_second_repair(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--confirm", "--format", "json"]) == 0
    capsys.readouterr()

    assert cli.main(["repair", event_id, "--strike", "510", "--confirm"]) == 2

    out = capsys.readouterr().out
    assert "trade event already voided" in out
    assert len(repo.list_trade_events()) == 3
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["strike"] == 500.0


def test_trade_events_repair_rejects_canonical_void_without_legacy_payload(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    _append_canonical_void_event(
        repo,
        target_event_id=event_id,
        event_id="canonical-void-open",
        event_time_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--dry-run"]) == 2

    out = capsys.readouterr().out
    assert "trade event already voided" in out
    assert "canonical-void-open" in out
    assert len(repo.list_trade_events()) == 2


def test_trade_events_repair_rejects_open_event_with_downstream_close(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    lot = repo.list_position_lots()[0]
    ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--confirm"]) == 2

    out = capsys.readouterr().out
    assert "cannot repair an open event with downstream close/adjust dependencies" in out
    assert "explicit_target" in out
    assert repo.list_position_lots()[0]["fields"]["contracts_open"] == 0


def test_trade_events_identity_repair_binds_in_place_with_downstream_and_is_idempotent(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli
    from src.application.ledger.position_projection_runtime import run_position_projection_forced_full

    repo, event_id = _repo_with_open_event(tmp_path)
    with repo._connect() as conn:  # noqa: SLF001 - legacy byte-preservation fixture
        row = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        payload = json.loads(str(row["event_json"]))
        payload["fee_provenance"] = {
            "basis": "estimated",
            "source": "legacy_fee_estimate",
        }
        payload["raw_payload"]["unknown_legacy_key"] = {"keep": True}
        conn.execute(
            "UPDATE trade_events SET event_json=?, updated_at_ms=? WHERE event_id=?",
            (json.dumps(payload, ensure_ascii=False), 1500, event_id),
        )
    run_position_projection_forced_full(repo)
    lot = repo.list_position_lots()[0]
    ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    def stored_state() -> tuple[str, int, list[dict], int]:
        with repo._connect() as conn:  # noqa: SLF001 - exact storage proof
            row = conn.execute(
                "SELECT event_json, ingest_seq FROM trade_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        generation = int(repo.read_position_projection_source_state().get("source_generation") or 0)
        return str(row["event_json"]), int(row["ingest_seq"]), repo.list_position_lots(), generation

    before_json, before_ingest_seq, before_lots, before_generation = stored_state()
    args = [
        "repair",
        event_id,
        "--futu-account-id",
        "123",
        "--order-id",
        "order-1",
        "--reason",
        "OpenD manual evidence: deal-1",
        "--format",
        "json",
    ]

    assert cli.main([*args, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "dry_run"
    assert preview["advisory"] is True
    assert preview["expected_before_sha256"] == hashlib.sha256(before_json.encode()).hexdigest()
    assert preview["write_applied"] is False
    assert stored_state() == (before_json, before_ingest_seq, before_lots, before_generation)

    assert cli.main([*args, "--confirm"]) == 0
    applied = json.loads(capsys.readouterr().out)
    after_json, after_ingest_seq, after_lots, after_generation = stored_state()
    assert applied["mode"] == "applied"
    assert applied["write_applied"] is True
    assert "void_event_id" not in applied and "repair_event_id" not in applied
    assert after_ingest_seq == before_ingest_seq
    assert after_lots == before_lots
    assert after_generation == before_generation + 1
    before_payload = json.loads(before_json)
    after_payload = json.loads(after_json)
    before_outer = deepcopy(before_payload)
    after_outer = deepcopy(after_payload)
    before_outer.pop("raw_payload")
    after_outer.pop("raw_payload")
    assert after_outer == before_outer
    assert after_payload["fee_provenance"] == before_payload["fee_provenance"]
    after_raw = deepcopy(after_payload["raw_payload"])
    provenance = after_raw.pop("order_identity_provenance")
    assert after_raw.pop("futu_account_id") == "123"
    assert after_raw.pop("order_id") == "order-1"
    assert after_raw == before_payload["raw_payload"]
    assert provenance["expected_before_sha256"] == preview["expected_before_sha256"]
    assert len(repo.list_trade_events()) == 2

    assert cli.main([*args, "--confirm"]) == 0
    no_op = json.loads(capsys.readouterr().out)
    assert no_op["mode"] == "no_op"
    assert no_op["write_applied"] is False
    assert stored_state() == (after_json, after_ingest_seq, after_lots, after_generation)


def test_trade_events_opend_time_correction_updates_in_place_with_downstream_and_is_idempotent(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli
    from src.application.ledger.position_projection_runtime import run_position_projection_forced_full

    repo, event_id = _repo_with_open_event(tmp_path)
    _attach_opend_time_evidence(repo, event_id=event_id)
    run_position_projection_forced_full(repo)
    lot = repo.list_position_lots()[0]
    ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    _install_legacy_trade_time_immutable_trigger(repo)
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )

    def stored_state() -> tuple[str, int, int, list[dict], int]:
        with repo._connect() as conn:  # noqa: SLF001 - exact storage proof
            row = conn.execute(
                "SELECT event_json, trade_time_ms, ingest_seq FROM trade_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        generation = int(repo.read_position_projection_source_state().get("source_generation") or 0)
        return (
            str(row["event_json"]),
            int(row["trade_time_ms"]),
            int(row["ingest_seq"]),
            repo.list_position_lots(),
            generation,
        )

    before_json, before_time, before_ingest_seq, before_lots, before_generation = stored_state()
    args = [
        "repair",
        event_id,
        "--trade-time-ms",
        "900",
        "--reason",
        "OpenD stored evidence: order-1",
        "--format",
        "json",
    ]

    assert cli.main([*args, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["operation"] == "opend_trade_time_correction"
    assert preview["mode"] == "dry_run"
    assert preview["before_trade_time_ms"] == 1000
    assert preview["after_trade_time_ms"] == 900
    assert preview["evidence_order_ids"] == ["order-1"]
    assert preview["write_applied"] is False
    assert stored_state() == (
        before_json,
        before_time,
        before_ingest_seq,
        before_lots,
        before_generation,
    )

    assert cli.main([*args, "--confirm"]) == 0
    applied = json.loads(capsys.readouterr().out)
    after_json, after_time, after_ingest_seq, after_lots, after_generation = stored_state()
    assert applied["mode"] == "applied"
    assert applied["write_applied"] is True
    assert applied["invalidated_cash_conversion_count"] == 1
    assert after_time == 900
    assert after_ingest_seq == before_ingest_seq
    assert after_generation == before_generation + 1
    assert len(repo.list_trade_events()) == 2
    assert after_lots[0]["fields"]["opened_at"] == 900
    before_without_time = deepcopy(before_lots)
    after_without_time = deepcopy(after_lots)
    before_without_time[0]["fields"].pop("opened_at")
    after_without_time[0]["fields"].pop("opened_at")
    assert after_without_time == before_without_time
    after_payload = json.loads(after_json)
    assert "cash_conversions" not in after_payload["raw_payload"]
    provenance = after_payload["raw_payload"]["trade_time_correction_provenance"]
    assert provenance["schema_version"] == "opend_trade_time_correction.v1"
    assert provenance["invalidated_cash_conversion_keys"] == ["option_trade_cash_gross"]
    assert provenance["expected_before_sha256"] == preview["expected_before_sha256"]
    with repo._connect() as conn:  # noqa: SLF001 - deployed trigger upgrade proof
        trigger_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_trade_events_query_projection_immutable'"
            ).fetchone()["sql"]
        )
    assert "opend_trade_time_correction.v1" in trigger_sql

    assert cli.main([*args, "--confirm"]) == 0
    no_op = json.loads(capsys.readouterr().out)
    assert no_op["mode"] == "no_op"
    assert no_op["write_applied"] is False
    assert stored_state() == (
        after_json,
        after_time,
        after_ingest_seq,
        after_lots,
        after_generation,
    )


@pytest.mark.parametrize(
    ("evidence_time_ms", "quantity", "requested_time_ms", "message"),
    [
        (900, 2, 900, "quantity does not match"),
        (900, 1, 800, "earliest stored OpenD order time"),
    ],
)
def test_trade_events_opend_time_correction_rejects_unmatched_evidence(
    monkeypatch,
    tmp_path: Path,
    capsys,
    evidence_time_ms: int,
    quantity: int,
    requested_time_ms: int,
    message: str,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    _attach_opend_time_evidence(
        repo,
        event_id=event_id,
        trade_time_ms=evidence_time_ms,
        quantity=quantity,
    )
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )
    before = repo.list_trade_events()

    assert cli.main(
        [
            "repair",
            event_id,
            "--trade-time-ms",
            str(requested_time_ms),
            "--reason",
            "OpenD stored evidence: order-1",
            "--confirm",
        ]
    ) == 2

    assert message in capsys.readouterr().out
    assert repo.list_trade_events() == before


@pytest.mark.parametrize(
    ("identity_args", "reason", "message"),
    [
        (["--futu-account-id", "123"], "OpenD manual evidence: deal-1", "requires both"),
        (
            ["--futu-account-id", "", "--order-id", ""],
            "OpenD manual evidence: deal-1",
            "requires both",
        ),
        (
            ["--futu-account-id", "00123", "--order-id", "order-1"],
            "OpenD manual evidence: deal-1",
            "canonical positive integer",
        ),
        (
            ["--futu-account-id", "123", "--order-id", "order 1"],
            "OpenD manual evidence: deal-1",
            "whitespace or control",
        ),
        (
            ["--futu-account-id", "123", "--order-id", " order-1"],
            "OpenD manual evidence: deal-1",
            "whitespace or control",
        ),
        (
            ["--futu-account-id", "123", "--order-id", "order-1"],
            "manual_repair",
            "manual OpenD evidence",
        ),
    ],
)
def test_trade_events_identity_repair_rejects_invalid_identity_without_writing(
    monkeypatch,
    tmp_path: Path,
    capsys,
    identity_args: list[str],
    reason: str,
    message: str,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))
    before = repo.list_trade_events()

    assert cli.main(
        [
            "repair",
            event_id,
            *identity_args,
            "--reason",
            reason,
            "--confirm",
        ]
    ) == 2

    assert message in capsys.readouterr().out
    assert repo.list_trade_events() == before


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("partial", "partial or conflicting identity"),
        ("actual", "actual fee evidence"),
        ("duplicate", "already used by active event"),
        ("voided", "already voided"),
    ],
)
def test_trade_events_identity_repair_rejects_ineligible_state_without_writing(
    monkeypatch,
    tmp_path: Path,
    capsys,
    case: str,
    message: str,
) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand
    from src.application.ledger.interventions import persist_manual_order_identity_binding

    repo, event_id = _repo_with_open_event(tmp_path)
    if case in {"partial", "actual"}:
        with repo._connect() as conn:  # noqa: SLF001 - ineligible legacy-row fixture
            row = conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            payload = json.loads(str(row["event_json"]))
            if case == "partial":
                payload["raw_payload"]["futu_account_id"] = "123"
            else:
                payload["raw_payload"]["fee_provenance"] = {
                    "basis": "actual",
                    "amount": 0,
                    "source": "test",
                }
            conn.execute(
                "UPDATE trade_events SET event_json=? WHERE event_id=?",
                (json.dumps(payload, ensure_ascii=False), event_id),
            )
    elif case == "duplicate":
        ledger_manual_trades.persist_manual_open_event(
            repo,
            OpenPositionCommand(
                broker="富途",
                account="lx",
                symbol="NVDA",
                option_type="put",
                side="short",
                contracts=1,
                currency="USD",
                strike=100.0,
                multiplier=100,
                expiration_ymd="2026-04-29",
                premium_per_share=2.0,
                opened_at_ms=1100,
            ),
        )
        other_event_id = next(
            item["event_id"] for item in repo.list_trade_events() if item["event_id"] != event_id
        )
        persist_manual_order_identity_binding(
            repo,
            target_event_id=other_event_id,
            overrides={"futu_account_id": "123", "order_id": "order-1"},
            repair_reason="OpenD manual evidence: deal-1",
        )
    else:
        _append_canonical_void_event(
            repo,
            target_event_id=event_id,
            event_id="canonical-void-open",
            event_time_ms=2000,
        )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))
    before = repo.list_trade_events()

    assert cli.main(
        [
            "repair",
            event_id,
            "--futu-account-id",
            "123",
            "--order-id",
            "order-1",
            "--reason",
            "OpenD manual evidence: deal-1",
            "--confirm",
        ]
    ) == 2

    assert message in capsys.readouterr().out
    assert repo.list_trade_events() == before


def test_trade_events_identity_repair_rolls_back_projection_failure(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.application.ledger.interventions as interventions
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))
    with repo._connect() as conn:  # noqa: SLF001 - rollback proof
        before_json = str(
            conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id=?",
                (event_id,),
            ).fetchone()["event_json"]
        )
    before_generation = repo.read_position_projection_source_state()["source_generation"]
    monkeypatch.setattr(
        interventions,
        "run_position_projection_in_transaction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("injected projection failure")),
    )

    assert cli.main(
        [
            "repair",
            event_id,
            "--futu-account-id",
            "123",
            "--order-id",
            "order-1",
            "--reason",
            "OpenD manual evidence: deal-1",
            "--confirm",
        ]
    ) == 2
    assert "injected projection failure" in capsys.readouterr().out
    with repo._connect() as conn:  # noqa: SLF001 - rollback proof
        after_json = str(
            conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id=?",
                (event_id,),
            ).fetchone()["event_json"]
        )
    assert after_json == before_json
    assert repo.read_position_projection_source_state()["source_generation"] == before_generation


def test_trade_events_identity_repair_rejects_cas_conflict_without_writing(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))
    monkeypatch.setattr(
        type(repo),
        "compare_and_swap_trade_event_order_identity_json",
        lambda *_args, **_kwargs: False,
    )
    before = repo.list_trade_events()

    assert cli.main(
        [
            "repair",
            event_id,
            "--futu-account-id",
            "123",
            "--order-id",
            "order-1",
            "--reason",
            "OpenD manual evidence: deal-1",
            "--confirm",
        ]
    ) == 2

    assert "CAS conflict" in capsys.readouterr().out
    assert repo.list_trade_events() == before


def test_trade_events_repair_close_record_id_updates_canonical_target_lot(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand

    repo, _event_id = _repo_with_open_event(tmp_path)
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=4.0,
            opened_at_ms=1100,
        ),
    )
    first_lot, second_lot = repo.list_position_lots()
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=first_lot["record_id"],
        fields=first_lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main([
        "repair",
        str(close_result.event_id),
        "--record-id",
        second_lot["record_id"],
        "--close-target-source-event-id",
        second_lot["fields"]["source_event_id"],
        "--dry-run",
        "--format",
        "json",
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["repair_event"]["target_lot_id"] == second_lot["record_id"]
    assert out["repair_event"]["raw_payload"]["record_id"] == second_lot["record_id"]
    assert out["projection_preview"]["projection_diagnostic_count"] == 0


def test_trade_events_repair_allows_open_when_downstream_close_was_canonical_voided(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    lot = repo.list_position_lots()[0]
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    _append_canonical_void_event(
        repo,
        target_event_id=str(close_result.event_id),
        event_id="canonical-void-close",
        event_time_ms=3000,
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["repair", event_id, "--strike", "500", "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["repair_event"]["contract_key"]["strike"] == 500.0
    assert len(repo.list_trade_events()) == 3


def test_trade_events_void_dry_run_includes_projection_preview(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["void", event_id, "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["ledger_preflight"]["event_type"] == "void"
    assert out["ledger_preflight"]["target_event_id"] == event_id
    assert out["projection_preview"]["position_lot_count"] == 0
    assert len(repo.list_trade_events()) == 1
    assert len(repo.list_position_lots()) == 1


def test_assignment_void_apply_rechecks_sale_created_after_preflight(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.application.ledger.commands as ledger_commands
    import src.interfaces.cli.trade_events as cli

    repo, assignment_event_id, stock_lot_id = _repo_with_assignment(tmp_path)
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )
    original_persist = ledger_commands.persist_manual_void_event

    def _persist_after_sale(*args, **kwargs):
        _append_assigned_stock_sale(
            repo,
            stock_lot_id=stock_lot_id,
            source_deal_id="sale-created-after-void-preflight",
        )
        state_after_sale = _durable_ledger_state(repo)
        with pytest.raises(ValueError, match="downstream stock dependencies"):
            original_persist(*args, **kwargs)
        assert _durable_ledger_state(repo) == state_after_sale
        raise ValueError("apply race rejected after dependency re-read")

    monkeypatch.setattr(ledger_commands, "persist_manual_void_event", _persist_after_sale)

    assert cli.main(["void", assignment_event_id, "--confirm", "--format", "json"]) == 2

    assert "apply race rejected after dependency re-read" in capsys.readouterr().out
    assert len(repo.list_trade_events()) == 2
    assert len(repo.list_assigned_stock_events()) == 1
    assert repo.list_position_lots()[0]["fields"]["contracts_open"] == 0


def test_assignment_repair_apply_rechecks_sale_created_after_preview(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.application.ledger.interventions as interventions
    import src.interfaces.cli.trade_events as cli

    repo, assignment_event_id, stock_lot_id = _repo_with_assignment(tmp_path)
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )
    original_transaction = interventions.with_sqlite_repo_transaction

    def _transaction_after_sale(repo_arg, fn, **kwargs):
        _append_assigned_stock_sale(
            repo,
            stock_lot_id=stock_lot_id,
            source_deal_id="sale-created-after-repair-preview",
        )
        state_after_sale = _durable_ledger_state(repo)
        with pytest.raises(ValueError, match="downstream stock dependencies"):
            original_transaction(repo_arg, fn, **kwargs)
        assert _durable_ledger_state(repo) == state_after_sale
        raise ValueError("repair apply race rejected after dependency re-read")

    monkeypatch.setattr(
        interventions,
        "with_sqlite_repo_transaction",
        _transaction_after_sale,
    )

    assert cli.main(["repair", assignment_event_id, "--price", "99", "--confirm"]) == 2

    assert "repair apply race rejected after dependency re-read" in capsys.readouterr().out
    assert len(repo.list_trade_events()) == 2
    assert len(repo.list_assigned_stock_events()) == 1


def test_assignment_void_and_repair_reject_historical_sale_with_stable_dependency(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, assignment_event_id, stock_lot_id = _repo_with_assignment(tmp_path)
    sale = _append_assigned_stock_sale(
        repo,
        stock_lot_id=stock_lot_id,
        source_deal_id="historical-stock-sale",
    )
    sale_event_id = str(sale["stock_event_id"])
    before = _durable_ledger_state(repo)
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )

    assert cli.main(["void", assignment_event_id, "--dry-run", "--format", "json"]) == 2
    void_error = capsys.readouterr().out
    assert json.dumps(
        [
            {
                "event_id": sale_event_id,
                "kind": "assigned_stock_sale",
                "lot_id": stock_lot_id,
                "status": "effective",
            }
        ],
        ensure_ascii=False,
        sort_keys=True,
    ) in void_error
    assert _durable_ledger_state(repo) == before

    assert cli.main(["repair", assignment_event_id, "--strike", "101", "--confirm"]) == 2
    assert "downstream stock dependencies" in capsys.readouterr().out
    assert _durable_ledger_state(repo) == before


def test_assignment_void_rejects_closed_covered_call_once_then_allows_legally_voided_call(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger.writer import persist_trade_event_objects_atomically

    repo, assignment_event_id, stock_lot_id = _repo_with_assignment(tmp_path)
    call_event_id = "historical-covered-call-open"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id=call_event_id,
                event_type="open",
                event_time_ms=3_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="call",
                    position_side="short",
                    strike=110,
                    expiration_ymd="2026-08-21",
                ),
                contracts=1,
                price=2.0,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id="historical-covered-call-lot",
                raw_payload={"source_stock_lot_id": stock_lot_id},
            )
        ],
    )
    call_lot = next(
        row for row in repo.list_position_lots() if row["record_id"] == "historical-covered-call-lot"
    )
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=call_lot["record_id"],
        fields=call_lot["fields"],
        contracts_to_close=1,
        close_price=0.5,
        close_reason="manual_buy_to_close",
        as_of_ms=4_000,
    )
    close_event_id = str(close_result.event_id)
    before = _durable_ledger_state(repo)
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )

    assert cli.main(["void", assignment_event_id, "--dry-run"]) == 2
    error = capsys.readouterr().out
    assert error.count(call_event_id) == 1
    assert '"kind": "covered_call"' in error
    assert '"status": "effective"' in error
    assert _durable_ledger_state(repo) == before

    _append_canonical_void_event(
        repo,
        target_event_id=close_event_id,
        event_id="void-historical-covered-call-close",
        event_time_ms=5_000,
    )
    _append_canonical_void_event(
        repo,
        target_event_id=call_event_id,
        event_id="void-historical-covered-call-open",
        event_time_ms=6_000,
    )
    assert cli.main(["void", assignment_event_id, "--dry-run", "--format", "json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["ledger_preflight"]["status"] == "ok"


def test_assignment_void_rejects_unresolved_explicit_stock_lot_reference(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger.writer import persist_trade_event_objects_atomically

    repo, assignment_event_id, stock_lot_id = _repo_with_assignment(tmp_path)
    call_event_id = "unresolved-covered-call-open"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id=call_event_id,
                event_type="open",
                event_time_ms=3_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="call",
                    position_side="short",
                    strike=110,
                    expiration_ymd="2026-08-21",
                ),
                contracts=2,
                price=2.0,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id="unresolved-covered-call-lot",
                raw_payload={
                    "strategy_snapshot": {"source_stock_lot_id": stock_lot_id}
                },
            )
        ],
    )
    before = _durable_ledger_state(repo)
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )

    assert cli.main(["void", assignment_event_id, "--dry-run"]) == 2

    error = capsys.readouterr().out
    assert json.dumps(
        [
            {
                "event_id": call_event_id,
                "kind": "covered_call",
                "lot_id": stock_lot_id,
                "status": "unresolved",
            }
        ],
        ensure_ascii=False,
        sort_keys=True,
    ) in error
    assert _durable_ledger_state(repo) == before


def test_assignment_void_rejects_ended_wheel_then_allows_legally_voided_wheel_history(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.wheel import build_wheel_event
    from src.application.wheel import build_wheel_read_model, end_wheel_lifecycle

    repo, assignment_event_id, stock_lot_id = _repo_with_assignment(
        tmp_path,
        wheel_start_enabled=True,
    )
    batch = build_wheel_read_model(repo, "lx", 3_000)["batches"][0]
    ended = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-before-intervention",
        actor="test",
        market="us",
        apply_changes=True,
        as_of_ms=3_000,
    )
    end_event_id = str(ended["event_id"])
    wheel_events = repo.list_wheel_events(account="lx")
    start_event_id = next(
        str(row["event_id"])
        for row in wheel_events
        if row["event_type"] == "wheel_started"
    )
    before = _durable_ledger_state(repo)
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )

    assert cli.main(["void", assignment_event_id, "--dry-run"]) == 2
    error = capsys.readouterr().out
    assert start_event_id in error
    assert end_event_id in error
    assert '"kind": "wheel_event"' in error
    assert _durable_ledger_state(repo) == before

    for index, target_wheel_event_id in enumerate((start_event_id, end_event_id), start=1):
        event = build_wheel_event(
            event_id=f"void-wheel-history-{index}",
            account="lx",
            stock_lot_id=stock_lot_id,
            event_type="wheel_event_voided",
            occurred_at_ms=4_000 + index,
            recorded_at_ms=4_000 + index,
            payload={"target_wheel_event_id": target_wheel_event_id},
        )
        with repo._writer_connection(begin_immediate=True) as conn:  # noqa: SLF001 - legal void fixture
            assert repo.append_wheel_event_once(event, conn=conn) is True
    assert cli.main(["void", assignment_event_id, "--dry-run", "--format", "json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["ledger_preflight"]["status"] == "ok"


def test_assignment_void_apply_preserves_ordinary_no_dependency_path(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, assignment_event_id, _stock_lot_id = _repo_with_assignment(tmp_path)
    monkeypatch.setattr(
        cli,
        "resolve_option_positions_repo",
        lambda **_kwargs: (tmp_path / "data.json", repo),
    )

    assert cli.main(["void", assignment_event_id, "--dry-run", "--format", "json"]) == 0
    capsys.readouterr()
    assert cli.main(["void", assignment_event_id, "--confirm", "--format", "json"]) == 0

    applied = json.loads(capsys.readouterr().out)
    assert applied["write_applied"] is True
    assert applied["created"] is True
    assert len(repo.list_trade_events()) == 3
    assert repo.list_assigned_stock_events() == []
    lots = repo.list_position_lots()
    assert [row["record_id"] for row in lots] == [
        next(
            row["raw_payload"]["record_id"]
            for row in repo.list_trade_events()
            if row["event_type"] == "assignment"
        )
    ]
    assert lots[0]["fields"]["contracts_open"] == 1


def test_trade_events_rejects_apply_and_dry_run_together(monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    with pytest.raises(SystemExit, match="--dry-run cannot be combined"):
        cli.main(["repair", event_id, "--strike", "500", "--apply", "--dry-run"])


def test_trade_events_repair_apply_alone_requires_confirm(monkeypatch, tmp_path: Path) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    with pytest.raises(SystemExit, match="use --confirm or --yes"):
        cli.main(["repair", event_id, "--strike", "500", "--apply"])

    assert len(repo.list_trade_events()) == 1


def test_trade_events_replay_dry_run_reports_projection(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli

    repo, _event_id = _repo_with_open_event(tmp_path)
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (tmp_path / "data.json", repo))

    assert cli.main(["replay", "--dry-run", "--format", "json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["trade_event_count"] == 1
    assert out["position_lot_count"] == 1
    assert out["ledger_store"]["sqlite_path"] == str((tmp_path / "option_positions.sqlite3").resolve())
    assert out["ledger_store"]["trade_event_count"] == 1
    assert out["ledger_store"]["position_lot_count"] == 1


def test_trade_events_replay_apply_ignores_deprecated_sqlite_path(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "option_positions.sqlite3")}}),
        encoding="utf-8",
    )
    repo = ledger_bootstrap.load_option_positions_repo(data_config)
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )
    monkeypatch.setattr(cli, "resolve_option_positions_repo", lambda **_kwargs: (data_config, repo))

    assert cli.main(["replay", "--apply", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "applied"
    assert out["ledger_store"]["sqlite_path"] == str((tmp_path / "output_shared" / "state" / "option_positions.sqlite3").resolve())
    assert "legacy_sqlite_path" not in out["ledger_store"]
    assert out["ledger_store"]["warnings"] == []


def test_trade_events_replay_accepts_explicit_runtime_root(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = tmp_path / "release" / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("{}", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )

    assert cli.main([
        "--data-config",
        str(data_config),
        "replay",
        "--runtime-root",
        str(runtime_root),
        "--dry-run",
        "--format",
        "json",
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["trade_event_count"] == 1
    assert out["ledger_store"]["runtime_root"] == str(runtime_root.resolve())
    assert out["ledger_store"]["runtime_root_source"] == "argument"


def test_trade_events_repair_apply_outputs_explicit_runtime_root_store(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.trade_events as cli
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = tmp_path / "release" / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("{}", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )
    event_id = str(repo.list_trade_events()[0]["event_id"])

    assert cli.main([
        "--data-config",
        str(data_config),
        "repair",
        "--runtime-root",
        str(runtime_root),
        event_id,
        "--strike",
        "500",
        "--confirm",
        "--format",
        "json",
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "applied"
    assert out["ledger_store"]["sqlite_path"] == str(
        (runtime_root / "output_shared" / "state" / "option_positions.sqlite3").resolve()
    )
    assert out["ledger_store"]["runtime_root"] == str(runtime_root.resolve())
    assert out["ledger_store"]["runtime_root_source"] == "argument"
