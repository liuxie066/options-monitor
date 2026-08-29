from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def test_position_maintenance_filters_account_and_broker_in_dry_run(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()
    captured: dict[str, Any] = {}

    records = [
        {
            "record_id": "rec_keep",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "status": "open",
                "contracts": 1,
                "position_id": "pos_keep",
            },
        },
        {
            "record_id": "rec_other_account",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "status": "open",
                "contracts": 1,
            },
        },
        {
            "record_id": "rec_other_broker",
            "fields": {
                "broker": "other",
                "account": "lx",
                "status": "open",
                "contracts": 1,
            },
        },
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
                "position_id": "pos_keep",
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


@pytest.mark.parametrize(
    ("market", "expected_record_ids"),
    [
        ("us", ["rec_us"]),
        ("hk", ["rec_hk"]),
    ],
)
def test_position_maintenance_filters_runtime_market_in_dry_run(
    monkeypatch,
    tmp_path: Path,
    market: str,
    expected_record_ids: list[str],
) -> None:
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()
    captured: dict[str, Any] = {}
    records = [
        {
            "record_id": "rec_us",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "symbol": "PDD",
                "status": "open",
                "contracts": 1,
                "position_id": "PDD_20260618_85P_short",
            },
        },
        {
            "record_id": "rec_hk",
            "fields": {
                "broker": "富途",
                "account": "sy",
                "symbol": "0700.HK",
                "status": "open",
                "contracts": 1,
                "position_id": "0700_HK_20260618_420P_short",
            },
        },
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
                "position_id": item["position_id"],
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
    assert [p["record_id"] for p in captured["positions"]] == expected_record_ids
    assert [item["record_id"] for item in result["decision_items"]] == expected_record_ids


def test_position_maintenance_refreshes_assignment_quote_before_dry_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from domain.domain.option_position_lots import parse_exp_to_ms
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()
    calls: list[dict[str, Any]] = []
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None

    class _Gateway:
        def close(self) -> None:
            calls.append({"stage": "close"})

    class _Underlier:
        code = "HK.00700"

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(
        mod,
        "_load_expiry_close_position_lots",
        lambda _repo: [
            {
                "record_id": "rec_0700",
                "fields": {
                    "broker": "富途",
                    "account": "sy",
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "side": "short",
                    "strike": 420,
                    "status": "open",
                    "contracts": 2,
                    "contracts_open": 2,
                    "expiration": exp_ms,
                    "position_id": "0700_HK_20260618_420P_short",
                },
            }
        ],
    )
    monkeypatch.setattr(mod, "build_ready_futu_quote_gateway", lambda **_kwargs: _Gateway())
    monkeypatch.setattr(mod, "normalize_underlier", lambda symbol, *, base_dir: _Underlier())

    def _get_spot(_gateway: Any, code: str, **_kwargs: Any) -> float:
        calls.append({"stage": "spot", "code": code})
        return 430.0

    monkeypatch.setattr(mod, "get_spot_opend", _get_spot)

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
    )

    assert calls[0] == {"stage": "spot", "code": "HK.00700"}
    assert calls[-1] == {"stage": "close"}
    assert result["expiry_assignment_quote_refresh"]["status"] == "ok"
    assert result["expiry_assignment_quote_refresh"]["refreshed_symbols"] == ["0700.HK"]
    assert result["candidates_should_close"] == 1
    assert result["decision_items"][0]["should_close"] is True
    assert result["decision_items"][0]["assignment_review"]["status"] == "otm_verified"
    assert result["decision_items"][0]["assignment_review"]["spot"] == 430.0


def test_position_maintenance_waits_for_assignment_when_assignment_quote_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from domain.domain.option_position_lots import parse_exp_to_ms
    from src.application.positions import maintenance as mod

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
            {
                "record_id": "rec_pdd",
                "fields": {
                    "broker": "富途",
                    "account": "sy",
                    "symbol": "PDD",
                    "option_type": "put",
                    "side": "short",
                    "strike": 85,
                    "status": "open",
                    "contracts": 2,
                    "contracts_open": 2,
                    "expiration": exp_ms,
                    "position_id": "PDD_20260618_85P_short",
                },
            }
        ],
    )
    monkeypatch.setattr(
        mod,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("opend unavailable")),
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

    assert result["expiry_assignment_quote_refresh"]["status"] == "source_unavailable"
    assert result["expiry_assignment_quote_refresh"]["missing_symbols"] == ["PDD"]
    assert result["candidates_should_close"] == 0
    assert result["skipped_review_required"] == 1
    assert result["decision_items"][0]["skip_reason"] == "expiry_assignment_review_required"
    assert result["decision_items"][0]["assignment_review"]["status"] == "missing_spot"
    assert "skipped_review_required: 1" in result["summary_text"]
    assert "skip=expiry_assignment_review_required" in result["summary_text"]


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
            {
                "record_id": "rec_wait",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "status": "open",
                    "contracts": 2,
                    "contracts_open": 2,
                    "position_id": "0700_20260605_440P_short",
                },
            }
        ],
    )
    monkeypatch.setattr(
        mod,
        "plan_expired_position_closes",
        lambda *_args, **_kwargs: [
            {
                "record_id": "rec_wait",
                "position_id": "0700_20260605_440P_short",
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


def test_position_maintenance_external_account_requires_manual_expiry_review(monkeypatch, tmp_path: Path) -> None:
    from domain.domain.option_position_lots import parse_exp_to_ms
    from src.application.positions import maintenance as mod

    data_config = tmp_path / "data.json"
    data_config.write_text(json.dumps({"option_positions": {"sqlite_path": str(tmp_path / "pos.sqlite3")}}), encoding="utf-8")
    report_dir = tmp_path / "reports"
    fake_repo = object()
    expiration = parse_exp_to_ms("2026-05-22")
    assert expiration is not None

    monkeypatch.setattr(mod, "resolve_data_config_path", lambda **_kwargs: data_config)
    monkeypatch.setattr(mod, "open_position_ledger", lambda _path, **kwargs: fake_repo)
    monkeypatch.setattr(
        mod,
        "_load_expiry_close_position_lots",
        lambda _repo: [
            {
                "record_id": "lot_tigr",
                "fields": {
                    "broker": "富途",
                    "account": "sy",
                    "status": "open",
                    "contracts": 10,
                    "contracts_open": 10,
                    "position_id": "pos_tigr",
                    "expiration": expiration,
                },
            }
        ],
    )

    result = mod.run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={
            "accounts": {"sy": {"type": "external_holdings"}},
            "portfolio": {"data_config": str(data_config), "broker": "富途"},
            "option_positions": {"auto_close": {"grace_days": 1}},
        },
        account="sy",
        broker="富途",
        report_dir=report_dir,
        as_of_ms=parse_exp_to_ms("2026-05-25"),
        dry_run=True,
    )

    assert result["mode"] == "dry_run"
    assert result["candidates_should_close"] == 0
    assert result["skipped_review_required"] == 1
    assert result["decisions"] == 1
    assert result["decision_items"][0]["skip_reason"] == "manual_expiry_review_required"
    assert "manual assignment/expiry review" in result["decision_items"][0]["reason"]
    assert "Review required:" in result["summary_text"]


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
        cfg={"portfolio": {"data_config": str(data_config)}},
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
                position_side="long",
                strike=100,
                expiration_ymd="2026-08-28",
            ),
            contracts=1,
            price=1.0,
            currency="USD",
            source="test_startup_recovery",
            multiplier=100,
            lot_id="lot_startup_recovery",
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
            {
                "record_id": "rec_1",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "status": "open",
                    "contracts": 1,
                    "position_id": "pos_1",
                },
            }
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
                        "position_id": "pos_1",
                        "should_close": True,
                        "expiration_ymd": "2026-05-01",
                    }
                ],
                "applied": [
                    {
                        "record_id": "rec_1",
                        "position_id": "pos_1",
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
            {
                "record_id": "rec_1",
                "fields": {"broker": "富途", "account": "lx", "status": "open", "contracts": 1},
            }
        ],
    )
    monkeypatch.setattr(
        mod,
        "record_expired_position_closes",
        lambda *_args, **_kwargs: SimpleNamespace(
            to_payload=lambda: {
                "decisions": [{"record_id": "rec_1", "position_id": "pos_1", "should_close": True}],
                "applied": [{"record_id": "rec_1", "position_id": "pos_1", "should_close": True}],
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

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        mod.run_expired_position_maintenance_for_account(
            base=tmp_path,
            cfg={**base_cfg, "option_positions": {"auto_close": {"enabled": "false"}}},
            account="lx",
            report_dir=tmp_path / "reports",
        )

    with pytest.raises(ValueError, match="grace_days must be >= 0"):
        mod.run_expired_position_maintenance_for_account(
            base=tmp_path,
            cfg={**base_cfg, "option_positions": {"auto_close": {"grace_days": -1}}},
            account="lx",
            report_dir=tmp_path / "reports",
        )

    with pytest.raises(ValueError, match="max_close_per_run must be >= 1"):
        mod.run_expired_position_maintenance_for_account(
            base=tmp_path,
            cfg={**base_cfg, "option_positions": {"auto_close": {"max_close_per_run": 0}}},
            account="lx",
            report_dir=tmp_path / "reports",
        )

    with pytest.raises(ValueError, match="receipt.enabled must be a boolean"):
        mod.run_expired_position_maintenance_for_account(
            base=tmp_path,
            cfg={**base_cfg, "option_positions": {"auto_close": {"receipt": {"enabled": "yes"}}}},
            account="lx",
            report_dir=tmp_path / "reports",
        )


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
