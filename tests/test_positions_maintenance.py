from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


_SKIPPED_ASSIGNMENT_QUOTE_REFRESH = {
    "enabled": False,
    "quote_source": None,
    "requested_symbols": [],
    "refreshed_symbols": [],
    "missing_symbols": [],
    "errors": [],
    "quote_count": 0,
    "status": "skipped_broker_evidence_required",
}


def _forbid_assignment_quote_fetch(monkeypatch, maintenance_module) -> list[str]:
    from src.application import opend_market_snapshot_fetching
    from src.infrastructure import futu_gateway

    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("unexpected_quote_fetch")
        raise AssertionError("expiry maintenance must not fetch spot for settlement permission")

    monkeypatch.setattr(futu_gateway, "build_ready_futu_quote_gateway", forbidden)
    # A previously imported quote-builder alias still calls this owner global.
    monkeypatch.setattr(futu_gateway, "build_ready_futu_gateway", forbidden)
    monkeypatch.setattr(opend_market_snapshot_fetching, "get_spot_opend", forbidden)
    # Also intercept aliases if a regression reintroduces the old imports before
    # these owner patches are installed. Never reload the production module.
    for name in ("build_ready_futu_quote_gateway", "get_spot_opend"):
        if hasattr(maintenance_module, name):
            monkeypatch.setattr(maintenance_module, name, forbidden)
    return calls


def _lot(record_id: str, *, account: str = "sy", contracts: int = 1, **fields: Any) -> dict:
    """One position-lot row as ``_load_expiry_close_position_lots`` returns it.

    The defaults are the ``sy`` single-contract lot this module repeats most
    often, so a call site names only the fields that differ from it.
    """
    base: dict[str, Any] = {
        "broker": "富途",
        "account": account,
        "status": "open",
        "contracts": contracts,
    }
    base.update(fields)
    return {"record_id": record_id, "fields": base}


def test_position_maintenance_filters_account_and_broker_in_dry_run(monkeypatch, tmp_path: Path, capsys) -> None:
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()
    captured: dict[str, Any] = {}

    records = [
        _lot("rec_keep", account="lx", position_key="pos_keep"),
        _lot("rec_other_account"),
        _lot("rec_other_broker", broker="other", account="lx"),
    ]

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(mod, "_load_expiry_close_position_lots", lambda _repo: records)

    def _build_decisions(positions, **kwargs):
        captured["positions"] = list(positions)
        captured["kwargs"] = dict(kwargs)
        return [
            {
                "record_id": "rec_keep",
                "position_key": "pos_keep",
                "should_close": True,
                "expiration_ymd": "2026-05-01",
            }
        ]

    monkeypatch.setattr(mod, "plan_expired_position_closes", _build_decisions)
    monkeypatch.setattr(
        mod,
        "record_expired_position_closes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not write")),
    )

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={
            "notifications": {"enabled": True},
            "portfolio": {"data_config": str(data_config), "broker": "富途"},
            "option_positions": {"auto_close": {"grace_days": 2}},
        },
        account="lx",
        broker="富途",
        report_dir=report_dir,
        as_of_ms=1777766400000,
        dry_run=True,
    )

    assert result["mode"] == "dry_run"
    assert result["broker"] == "富途"
    assert result["candidates_should_close"] == 1
    assert result["applied_closed"] == 0
    assert [p["record_id"] for p in captured["positions"]] == ["rec_keep"]
    assert captured["kwargs"]["grace_days"] == 2
    assert "Auto-close expired positions (grace_days=2)" in result["summary_text"]
    assert (report_dir / "auto_close_summary.txt").exists()
    assert result["receipt"]["status"] == "skipped"
    assert result["receipt"]["reason"] == "dry_run"
    stages = capsys.readouterr().err.splitlines()
    assert stages[0] == "[AUTO_CLOSE_STAGE] account=lx stage=account_started"
    assert "[AUTO_CLOSE_STAGE] account=lx stage=ledger_open_started" in stages
    assert "[AUTO_CLOSE_STAGE] account=lx stage=assignment_quote_refresh_started" in stages
    assert stages[-1] == "[AUTO_CLOSE_STAGE] account=lx stage=account_finished"


@pytest.mark.parametrize(
    ("market", "expected_lot_ids"),
    [
        ("us", ["rec_us"]),
        ("hk", ["rec_hk"]),
    ],
)
def test_position_maintenance_filters_runtime_market_in_dry_run(
    monkeypatch,
    tmp_path: Path,
    market: str,
    expected_lot_ids: list[str],
) -> None:
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()
    captured: dict[str, Any] = {}
    records = [
        _lot("rec_us", symbol="PDD", position_key="PDD_20260618_85P_short"),
        _lot("rec_hk", symbol="0700.HK", position_key="0700_HK_20260618_420P_short"),
    ]

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(mod, "_load_expiry_close_position_lots", lambda _repo: records)

    def _build_decisions(positions, **kwargs):
        captured["positions"] = list(positions)
        captured["kwargs"] = dict(kwargs)
        return [
            {
                "record_id": item["record_id"],
                "position_key": item["position_key"],
                "should_close": True,
                "expiration_ymd": "2026-06-18",
            }
            for item in positions
        ]

    monkeypatch.setattr(mod, "plan_expired_position_closes", _build_decisions)
    monkeypatch.setattr(
        mod,
        "record_expired_position_closes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not write")),
    )

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={
            "_generated": {"market": market},
            "portfolio": {"data_config": str(data_config), "broker": "富途"},
            "option_positions": {"auto_close": {"grace_days": 1}},
        },
        account="sy",
        broker="富途",
        report_dir=report_dir,
        as_of_ms=1781830827103,
        dry_run=True,
    )

    assert result["market_filter"] == market.upper()
    assert [p["record_id"] for p in captured["positions"]] == expected_lot_ids
    assert [item["record_id"] for item in result["decision_items"]] == expected_lot_ids


def test_position_maintenance_does_not_fetch_assignment_quote_before_dry_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from domain.domain.option_position_lots import parse_exp_to_ms
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()
    calls = _forbid_assignment_quote_fetch(monkeypatch, mod)
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(
        mod,
        "_load_expiry_close_position_lots",
        lambda _repo: [
            _lot(
                "rec_0700",
                symbol="0700.HK",
                option_type="put",
                side="short",
                strike=420,
                contracts=2,
                contracts_open=2,
                multiplier=1000,
                expiration=exp_ms,
                position_key="0700_HK_20260618_420P_short",
            )
        ],
    )
    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={
            "_generated": {"market": "hk"},
            "portfolio": {"data_config": str(data_config), "broker": "富途"},
            "option_positions": {"auto_close": {"grace_days": 1}},
            "symbols": [{"symbol": "0700.HK", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}}],
        },
        account="sy",
        broker="富途",
        report_dir=report_dir,
        as_of_ms=int(datetime(2026, 6, 19, 1, 0, tzinfo=timezone.utc).timestamp() * 1000),
        dry_run=True,
        send_receipt=False,
    )

    assert calls == []
    assert result["expiry_assignment_quote_refresh"] == _SKIPPED_ASSIGNMENT_QUOTE_REFRESH
    assert result["candidates_should_close"] == 0
    assert result["skipped_review_required"] == 1
    assert result["decision_items"][0]["should_close"] is False
    assert result["decision_items"][0]["patch"] is None
    assert result["decision_items"][0]["assignment_review"]["status"] == "missing_spot"
    assert result["decision_items"][0]["assignment_review"]["spot"] is None


def test_position_maintenance_review_does_not_depend_on_quote_provider_availability(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from domain.domain.option_position_lots import parse_exp_to_ms
    from src.application.positions import maintenance as mod

    calls = _forbid_assignment_quote_fetch(monkeypatch, mod)
    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(
        mod,
        "_load_expiry_close_position_lots",
        lambda _repo: [
            _lot(
                "rec_pdd",
                symbol="PDD",
                option_type="put",
                side="short",
                strike=85,
                contracts=2,
                contracts_open=2,
                multiplier=100,
                expiration=exp_ms,
                position_key="PDD_20260618_85P_short",
            )
        ],
    )
    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={
            "_generated": {"market": "us"},
            "portfolio": {"data_config": str(data_config), "broker": "富途"},
            "option_positions": {"auto_close": {"grace_days": 1}},
            "symbols": [{"symbol": "PDD", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}}],
        },
        account="sy",
        broker="富途",
        report_dir=report_dir,
        as_of_ms=int(datetime(2026, 6, 19, 4, 0, tzinfo=timezone.utc).timestamp() * 1000),
        dry_run=True,
        send_receipt=False,
    )

    assert calls == []
    assert result["expiry_assignment_quote_refresh"] == _SKIPPED_ASSIGNMENT_QUOTE_REFRESH
    assert result["candidates_should_close"] == 0
    assert result["skipped_review_required"] == 1
    assert result["decision_items"][0]["skip_reason"] == "expiry_assignment_review_required"
    assert result["decision_items"][0]["assignment_review"]["status"] == "missing_spot"
    assert "skipped_review_required: 1" in result["summary_text"]
    assert "skip=expiry_assignment_review_required" in result["summary_text"]


@pytest.mark.parametrize(
    ("option_type", "expiry_scene_spot", "current_spot"),
    [("put", 90, 110), ("call", 110, 90)],
)
@pytest.mark.parametrize("quote_kind", ["missing", "bare", "post_expiry", "stale", "wrong_underlier", "claimed_complete"])
@pytest.mark.parametrize("case_kind", ["none", "waiting", "zero_price", "conflict", "stock"])
def test_expired_short_maintenance_requires_broker_evidence_through_real_ledger(
    monkeypatch, tmp_path: Path, option_type: str, expiry_scene_spot: float,
    current_spot: float, quote_kind: str, case_kind: str,
) -> None:
    from domain.domain.ledger import ContractKey, TradeEvent
    from domain.domain.option_position_lots import parse_exp_to_ms
    from src.application.ledger.api import open_position_ledger
    from src.application.ledger.writer import persist_trade_event_object
    from src.application.positions import maintenance as mod

    # This historical price describes the reported crossing, not evidence that
    # production has collected. Only the later input spot reaches the facade.
    assert (expiry_scene_spot < 100 < current_spot) if option_type == "put" else (
        current_spot < 100 < expiry_scene_spot
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    calls = _forbid_assignment_quote_fetch(monkeypatch, mod)
    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {}}), encoding="utf-8")
    repo = open_position_ledger(data_config, runtime_root=tmp_path)
    as_of_ms = parse_exp_to_ms("2026-06-22")
    assert as_of_ms is not None

    # Each out-of-scope lot has its own persisted case/evidence as well, so the
    # readback checks actual account/broker/market isolation, not just counts.
    scopes = [
        ("target", "lx", "富途", "AAPL", "2026-06-18"),
        ("later", "lx", "富途", "AAPL", "2026-07-17"),
        ("other_account", "sy", "富途", "AAPL", "2026-06-18"),
        ("other_broker", "lx", "other", "AAPL", "2026-06-18"),
        ("other_market", "lx", "富途", "0700.HK", "2026-06-18"),
    ]
    for lot_id, account, broker, symbol, expiry in scopes:
        persist_trade_event_object(repo, TradeEvent(
            event_id=f"open-{lot_id}", event_type="open", event_time_ms=1000,
            contract_key=ContractKey.from_values(
                broker=broker, account=account, underlying_symbol=symbol,
                option_type=option_type, strike=100, expiration_ymd=expiry,
            ),
            contracts=1, price=1, currency="HKD" if symbol.endswith(".HK") else "USD",
            source="test_seed_open_lot", multiplier=100, lot_id=lot_id,
            raw_payload={"side": "sell", "source_type": "test_seed"},
        ))
        kind = case_kind if lot_id == "target" else "zero_price"
        if kind == "none":
            continue
        case_id = f"case-{lot_id}"
        repo.upsert_trade_lifecycle_case({
            "case_id": case_id,
            "case_key": f"{broker}|{account}|{symbol}|{option_type}|short|100|{expiry}",
            "broker": broker, "account": account, "symbol": symbol, "option_type": option_type,
            "position_side": "short", "strike": 100, "expiration_ymd": expiry,
            "contracts": 1, "status": "conflict" if kind == "conflict" else "waiting_settlement_evidence",
            "decision_type": "needs_review", "target_lot_ids": [],
        })
        if kind in {"zero_price", "stock"}:
            evidence = {
                "evidence_id": f"evidence-{lot_id}", "case_id": case_id,
                "source_type": "opend_deal", "source_event_id": f"deal-{lot_id}",
                "evidence_type": "stock_settlement_leg" if kind == "stock" else "option_zero_price_close",
                "account": account, "symbol": symbol, "raw": {"broker": broker},
            }
            if kind == "stock":
                evidence.update(
                    side="buy" if option_type == "put" else "sell", stock_qty=100,
                    stock_price=100, trade_time_ms=parse_exp_to_ms("2026-06-19"),
                )
            repo.upsert_trade_lifecycle_evidence(evidence)

    def economic_snapshot():
        # Projection publication/generation metadata can legitimately change on
        # apply. Persisted domain fields, events and lifecycle facts cannot.
        return {
            "events": repo.list_trade_events(),
            "lots": {row["record_id"]: row["fields"] for row in repo.list_position_lots()},
            "cases": repo.list_trade_lifecycle_cases(),
            "evidence": repo.list_trade_lifecycle_evidence(),
            "allocations": repo.list_trade_lifecycle_allocations(),
        }

    before = economic_snapshot()
    quote = {"_auto_close_underlying_spot": current_spot}
    if quote_kind == "post_expiry":
        quote.update(_auto_close_quote_time_ms=as_of_ms, _auto_close_quote_source="opend_realtime",
                     _auto_close_quote_status="fresh", _auto_close_underlier_code="US.AAPL")
    elif quote_kind == "stale":
        quote.update(_auto_close_quote_time_ms=1000, _auto_close_quote_status="stale")
    elif quote_kind == "wrong_underlier":
        quote.update(_auto_close_underlier_code="US.MSFT", _auto_close_quote_status="fresh")
    elif quote_kind == "claimed_complete":
        quote.update(complete=True, unassigned=True)
    if quote_kind != "missing":
        real_load = mod._load_expiry_close_position_lots

        def load_with_input_quote(ledger):
            return [
                {**row, "fields": {**row["fields"], **quote}} if row["record_id"] == "target" else row
                for row in real_load(ledger)
            ]

        monkeypatch.setattr(mod, "_load_expiry_close_position_lots", load_with_input_quote)

    decisions_by_mode = []
    for run_index, dry_run in enumerate((True, False, False)):
        report_dir = tmp_path / f"reports-{run_index}"
        result = mod.run_expired_position_maintenance_for_account(
            base=tmp_path,
            cfg={
                "_generated": {"market": "us"},
                "portfolio": {"data_config": str(data_config), "broker": "富途"},
                "option_positions": {"auto_close": {"grace_days": 1}},
                "symbols": [{"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}}],
            },
            account="lx", broker="富途", report_dir=report_dir, as_of_ms=as_of_ms,
            dry_run=dry_run, send_receipt=False,
        )
        assert result["mode"] == ("dry_run" if dry_run else "applied")
        expected_keys = {
            "mode", "account", "broker", "market_filter", "as_of_utc", "grace_days", "max_close",
            "positions_checked", "decisions", "decision_items", "candidates_should_close", "applied_closed",
            "skipped_review_required", "skipped_grace_pending", "skipped_already_closed", "errors", "applied",
            "ledger_store", "expiry_assignment_quote_refresh", "summary_text", "receipt",
        }
        if not dry_run:
            expected_keys.add("projection_refresh")
        assert set(result) == expected_keys
        assert result["account"] == "lx"
        assert result["broker"] == "富途"
        assert result["market_filter"] == "US"
        assert result["positions_checked"] == result["decisions"] == 2
        assert result["candidates_should_close"] == result["applied_closed"] == 0
        assert result["skipped_review_required"] == 1
        assert result["skipped_grace_pending"] == result["skipped_already_closed"] == 0
        assert result["errors"] == result["applied"] == []
        assert result["expiry_assignment_quote_refresh"] == _SKIPPED_ASSIGNMENT_QUOTE_REFRESH
        assert result["receipt"]["delivery_confirmed"] is False
        assert result["receipt"]["reason"] == "skipped_no_send"
        assert Path(result["ledger_store"]["sqlite_path"]).is_relative_to(tmp_path)
        decisions = {item["record_id"]: item for item in result["decision_items"]}
        assert set(decisions) == {"target", "later"}
        target = decisions["target"]
        assert target["should_close"] is False
        assert target["skip_reason"] == "expiry_assignment_review_required"
        assert target["patch"] is None
        assert target["assignment_review"]["status"] == (
            "missing_spot" if quote_kind == "missing" else "broker_evidence_required"
        )
        assert target["assignment_review"]["spot"] == (None if quote_kind == "missing" else current_spot)
        assert decisions["later"]["skip_reason"] == "not_expired"
        decisions_by_mode.append(decisions)
        assert economic_snapshot() == before
        assert "Review required:" in result["summary_text"]
        assert "skipped_review_required: 1" in result["summary_text"]
        assert "Closed:" not in result["summary_text"]
        assert (report_dir / "auto_close_summary.txt").read_text(encoding="utf-8").strip() == result["summary_text"]
        assert calls == []
    assert decisions_by_mode[0] == decisions_by_mode[1] == decisions_by_mode[2]


def test_position_maintenance_surfaces_grace_pending_expired_positions(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(
        mod,
        "_load_expiry_close_position_lots",
        lambda _repo: [
            _lot("rec_wait", account="lx", contracts=2, contracts_open=2, position_key="0700_20260605_440P_short")
        ],
    )
    monkeypatch.setattr(
        mod,
        "plan_expired_position_closes",
        lambda *_args, **_kwargs: [
            {
                "record_id": "rec_wait",
                "position_key": "0700_20260605_440P_short",
                "should_close": False,
                "skip_reason": "grace_period_pending",
                "expiration_ymd": "2026-06-05",
                "eligible_after_utc": "2026-06-06T00:00:00+00:00",
            }
        ],
    )

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={
            "portfolio": {"data_config": str(data_config), "broker": "富途"},
            "option_positions": {"auto_close": {"enabled": True}},
        },
        account="lx",
        broker="富途",
        report_dir=report_dir,
        as_of_ms=1780702200000,
        dry_run=True,
        send_receipt=False,
    )

    assert result["candidates_should_close"] == 0
    assert result["skipped_grace_pending"] == 1
    assert "skipped_grace_pending: 1" in result["summary_text"]
    assert "eligible_after=2026-06-06T00:00:00+00:00" in result["summary_text"]
    assert (report_dir / "auto_close_summary.txt").exists()


def test_position_maintenance_refreshes_projection_before_apply(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import maintenance as mod
    from src.application.ledger.results import ExpiredCloseRunResult, ProjectionRefreshResult

    class FakeRepo:
        def count_trade_events(self) -> int:
            return 2

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    fake_repo = FakeRepo()
    order: list[str] = []
    captured: dict[str, Any] = {}
    refresh_result = ProjectionRefreshResult(trade_event_count=2, position_lot_count=1)

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)

    def _refresh(repo):
        assert repo is fake_repo
        order.append("refresh")
        return refresh_result

    def _load_records(repo):
        assert repo is fake_repo
        order.append("load_records")
        return [
            {
                "record_id": "recovered-lot",
                "fields": {"account": "lx", "status": "open", "contracts": 1},
            }
        ]

    monkeypatch.setattr(mod, "refresh_position_lot_projection", _refresh)
    monkeypatch.setattr(mod, "_load_expiry_close_position_lots", _load_records)
    def _record(_repo, positions, **kwargs):
        order.append("record")
        captured["positions"] = list(positions)
        captured["projection_refresh"] = kwargs.get("projection_refresh")
        return ExpiredCloseRunResult(decisions=[], applied=[], errors=[])

    monkeypatch.setattr(mod, "record_expired_position_closes", _record)

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={"notifications": {"enabled": True}, "portfolio": {"data_config": str(data_config)}},
        account="lx",
        report_dir=tmp_path / "reports",
        as_of_ms=1777766400000,
    )

    assert order == ["refresh", "load_records", "record"]
    assert [item["record_id"] for item in captured["positions"]] == ["recovered-lot"]
    assert captured["projection_refresh"] is refresh_result
    assert result["projection_refresh"] == refresh_result.to_dict()
    assert result["summary_text"] == ""
    assert result["receipt"]["status"] == "skipped"
    assert result["receipt"]["reason"] == "noop"


def test_position_maintenance_reuses_startup_projection_recovery_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger import bootstrap
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.results import ExpiredCloseRunResult
    from src.application.positions import maintenance as mod

    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}),
        encoding="utf-8",
    )
    seed_repo = SQLiteOptionPositionsRepository(sqlite_path)
    seed_repo.upsert_trade_event(
        TradeEvent(
            event_id="startup-recovery-open",
            event_type="open",
            event_time_ms=1_000,
            contract_key=ContractKey.from_values(
                broker="富途",
                account="lx",
                underlying_symbol="NVDA",
                option_type="put",
                strike=100,
                expiration_ymd="2026-08-28",
                        ),
            contracts=1,
            price=1.0,
            currency="USD",
            source="test_startup_recovery",
            multiplier=100,
            lot_id="lot_startup_recovery",
            # §9.2 step 3: the short put side travels as the trade side.
            raw_payload={"side": "sell"},
        )
    )
    assert seed_repo.count_trade_events() == 1
    assert seed_repo.count_position_lots() == 0

    rebuild_calls: list[object] = []
    real_rebuild = bootstrap.run_position_projection_in_transaction

    def _rebuild(repo, *args, **kwargs):
        rebuild_calls.append(repo)
        return real_rebuild(repo, *args, **kwargs)

    monkeypatch.setattr(bootstrap, "run_position_projection_in_transaction", _rebuild)
    monkeypatch.setattr(
        mod,
        "refresh_position_lot_projection",
        lambda _repo: (_ for _ in ()).throw(
            AssertionError("startup recovery receipt must skip rebuild")
        ),
    )
    captured: dict[str, Any] = {}

    def _record(repo, positions, **kwargs):
        captured["repo"] = repo
        captured["positions"] = list(positions)
        captured["projection_refresh"] = kwargs["projection_refresh"]
        return ExpiredCloseRunResult(decisions=[], applied=[], errors=[])

    monkeypatch.setattr(mod, "record_expired_position_closes", _record)

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={"portfolio": {"data_config": str(data_config), "broker": "富途"}},
        account="lx",
        report_dir=tmp_path / "reports",
        as_of_ms=1788048000000,
        send_receipt=False,
    )

    assert len(rebuild_calls) == 1
    assert [item["record_id"] for item in captured["positions"]] == ["lot_startup_recovery"]
    assert captured["projection_refresh"].trade_event_count == 1
    assert result["projection_refresh"] == captured["projection_refresh"].to_dict()
    assert captured["repo"].bootstrap_projection_refresh is None


def test_position_maintenance_projection_refresh_failure_stops_before_load_or_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.positions import maintenance as mod

    class FakeRepo:
        def count_trade_events(self) -> int:
            return 1

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **_kwargs: FakeRepo())
    monkeypatch.setattr(
        mod,
        "refresh_position_lot_projection",
        lambda _repo: (_ for _ in ()).throw(RuntimeError("locking protocol")),
    )
    monkeypatch.setattr(
        mod,
        "_load_expiry_close_position_lots",
        lambda _repo: (_ for _ in ()).throw(AssertionError("failed refresh must stop before lot load")),
    )
    monkeypatch.setattr(
        mod,
        "record_expired_position_closes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("failed refresh must not write")),
    )

    with pytest.raises(RuntimeError, match="projection refresh failed before auto-close: locking protocol"):
        mod.run_expired_position_maintenance_for_account(
            base=tmp_path,
            cfg={"portfolio": {"data_config": str(data_config)}},
            account="lx",
            report_dir=tmp_path / "reports",
            as_of_ms=1777766400000,
        )


def test_position_maintenance_attaches_receipt_after_apply(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    fake_repo = object()
    receipt_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(
        mod,
        "_load_expiry_close_position_lots",
        lambda _repo: [
            _lot("rec_1", account="lx", position_key="pos_1")
        ],
    )
    monkeypatch.setattr(
        mod,
        "record_expired_position_closes",
        lambda *_args, **_kwargs: SimpleNamespace(
            to_payload=lambda: {
                "decisions": [
                    {
                        "record_id": "rec_1",
                        "position_key": "pos_1",
                        "should_close": True,
                        "expiration_ymd": "2026-05-01",
                    }
                ],
                "applied": [
                    {
                        "record_id": "rec_1",
                        "position_key": "pos_1",
                        "should_close": True,
                        "expiration_ymd": "2026-05-01",
                    }
                ],
                "errors": [],
            }
        ),
    )

    def _send_receipt(**kwargs):
        receipt_calls.append(dict(kwargs))
        return {"status": "sent", "delivery_confirmed": True, "message_id": "msg-auto-1"}

    monkeypatch.setattr(mod, "safe_send_auto_close_receipt", _send_receipt)

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={"portfolio": {"data_config": str(data_config), "broker": "富途"}},
        account="lx",
        report_dir=tmp_path / "reports",
        as_of_ms=1777766400000,
    )

    assert result["applied_closed"] == 1
    assert result["receipt"]["status"] == "sent"
    assert result["receipt"]["message_id"] == "msg-auto-1"
    assert receipt_calls[0]["dry_run"] is False
    assert receipt_calls[0]["result"]["applied_closed"] == 1


def test_position_maintenance_skips_receipt_in_no_send_mode(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    fake_repo = object()

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(
        mod,
        "_load_expiry_close_position_lots",
        lambda _repo: [
            _lot("rec_1", account="lx")
        ],
    )
    monkeypatch.setattr(
        mod,
        "record_expired_position_closes",
        lambda *_args, **_kwargs: SimpleNamespace(
            to_payload=lambda: {
                "decisions": [{"record_id": "rec_1", "position_key": "pos_1", "should_close": True}],
                "applied": [{"record_id": "rec_1", "position_key": "pos_1", "should_close": True}],
                "errors": [],
            }
        ),
    )
    monkeypatch.setattr(
        mod,
        "safe_send_auto_close_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no-send must not call receipt sender")),
    )

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={"portfolio": {"data_config": str(data_config), "broker": "富途"}},
        account="lx",
        report_dir=tmp_path / "reports",
        as_of_ms=1777766400000,
        send_receipt=False,
    )

    assert result["applied_closed"] == 1
    assert result["receipt"]["status"] == "skipped"
    assert result["receipt"]["reason"] == "skipped_no_send"


def test_position_maintenance_rejects_invalid_auto_close_config(tmp_path: Path) -> None:
    from src.application.positions import maintenance as mod

    base_cfg = {"portfolio": {"data_config": str(tmp_path / "missing.json")}}

    def _rejects(match: str, auto_close: dict) -> None:
        with pytest.raises(ValueError, match=match):
            mod.run_expired_position_maintenance_for_account(
                base=tmp_path,
                cfg={**base_cfg, "option_positions": {"auto_close": auto_close}},
                account="lx",
                report_dir=tmp_path / "reports",
            )

    _rejects("enabled must be a boolean", {"enabled": "false"})
    _rejects("grace_days must be >= 0", {"grace_days": -1})
    _rejects("max_close_per_run must be >= 1", {"max_close_per_run": 0})
    _rejects("receipt.enabled must be a boolean", {"receipt": {"enabled": "yes"}})


def test_position_maintenance_missing_data_config_is_failed_not_skipped(tmp_path: Path) -> None:
    from src.application.positions import maintenance as mod

    missing = tmp_path / "missing" / "portfolio.runtime.json"
    report_dir = tmp_path / "reports"

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={
            "portfolio": {"data_config": str(missing), "broker": "富途"},
            "option_positions": {"auto_close": {"enabled": True}},
        },
        account="lx",
        report_dir=report_dir,
        dry_run=False,
        send_receipt=False,
    )

    assert result["mode"] == "error"
    assert result["reason"] == "missing_data_config"
    assert result["positions_checked"] == 0
    assert result["applied_closed"] == 0
    assert result["errors"] == [f"missing_data_config: {missing}"]
    assert "ERRORS: 1" in result["summary_text"]
    assert (report_dir / "auto_close_summary.txt").exists()
    assert result["receipt"]["status"] == "skipped"


def test_position_maintenance_uses_runtime_ledger_default_when_data_config_omitted(tmp_path: Path) -> None:
    from src.application.positions import maintenance as mod

    report_dir = tmp_path / "reports"

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={
            "portfolio": {"broker": "富途"},
            "option_positions": {"auto_close": {"enabled": True}},
        },
        account="lx",
        report_dir=report_dir,
        dry_run=False,
        send_receipt=False,
    )

    assert result["mode"] == "applied"
    assert result["errors"] == []
    assert result["positions_checked"] == 0
    assert result["ledger_store"]["sqlite_path"] == str((tmp_path / "output_shared" / "state" / "option_positions.sqlite3").resolve())
    assert not (tmp_path / "portfolio.runtime.json").exists()
    assert "summary_text" in result
