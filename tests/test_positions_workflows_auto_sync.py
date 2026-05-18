from __future__ import annotations

import json
import sys
from pathlib import Path

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_execute_manual_open_triggers_best_effort_sync(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.workflows as workflows

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(repo.db_path),
                    "sync_to_feishu": {"enabled": True},
                },
                "feishu": {
                    "app_id": "app_id",
                    "app_secret": "app_secret",
                    "tables": {"option_positions": "app_token/table_id"},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    captured: dict[str, str] = {}

    def _fake_sync(*, repo, data_config, record_id, apply_mode):
        captured["record_id"] = record_id
        captured["data_config"] = str(data_config)
        captured["apply_mode"] = str(int(apply_mode))
        return {"record_id": record_id, "action": "update"}

    monkeypatch.setattr(workflows, "sync_single_option_position_record", _fake_sync)

    out = workflows.execute_manual_open(
        repo,
        broker="富途",
        account="lx",
        symbol="TSLA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100.0,
        expiration_ymd="2026-06-19",
        premium_per_share=1.23,
        underlying_share_locked=None,
        note=None,
        dry_run=False,
    )

    assert out["mode"] == "applied"
    assert out["ledger_preflight"]["status"] == "ok"
    assert out["ledger_preflight"]["event_type"] == "open"
    assert out["ledger_preflight"]["target_lot_id"] == out["result"]["record_id"]
    assert out["sync_result"]["action"] == "update"
    assert captured["record_id"].startswith("lot_manual-open-")
    assert captured["data_config"] == str(repo.data_config_path)
    assert captured["apply_mode"] == "1"


def test_execute_manual_open_skips_best_effort_sync_when_switch_is_off(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.workflows as workflows

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(repo.db_path),
                    "sync_to_feishu": {"enabled": False},
                },
                "feishu": {
                    "app_id": "app_id",
                    "app_secret": "app_secret",
                    "tables": {"option_positions": "app_token/table_id"},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    def _should_not_sync(*, repo, data_config, record_id, apply_mode):
        raise AssertionError("disabled sync should not call sync_single_option_position_record")

    monkeypatch.setattr(workflows, "sync_single_option_position_record", _should_not_sync)

    out = workflows.execute_manual_open(
        repo,
        broker="富途",
        account="lx",
        symbol="TSLA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100.0,
        expiration_ymd="2026-06-19",
        premium_per_share=1.23,
        underlying_share_locked=None,
        note=None,
        dry_run=False,
    )

    assert out["mode"] == "applied"
    assert out["sync_result"] is None


def test_execute_manual_open_uses_runtime_sync_override_when_data_config_is_off(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.workflows as workflows
    from src.application.positions.sync_config import apply_option_positions_runtime_config

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(repo.db_path),
                    "sync_to_feishu": {"enabled": False},
                },
                "feishu": {
                    "app_id": "app_id",
                    "app_secret": "app_secret",
                    "tables": {"option_positions": "app_token/table_id"},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    apply_option_positions_runtime_config(repo, {"option_positions": {"sync_to_feishu": {"enabled": True}}})
    captured: dict[str, str] = {}

    def _fake_sync(*, repo, data_config, record_id, apply_mode):
        captured["record_id"] = record_id
        captured["data_config"] = str(data_config)
        captured["apply_mode"] = str(int(apply_mode))
        return {"record_id": record_id, "action": "update"}

    monkeypatch.setattr(workflows, "sync_single_option_position_record", _fake_sync)

    out = workflows.execute_manual_open(
        repo,
        broker="富途",
        account="lx",
        symbol="TSLA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100.0,
        expiration_ymd="2026-06-19",
        premium_per_share=1.23,
        underlying_share_locked=None,
        note=None,
        dry_run=False,
    )

    assert out["mode"] == "applied"
    assert out["sync_result"]["action"] == "update"
    assert captured["record_id"].startswith("lot_manual-open-")
    assert captured["data_config"] == str(repo.data_config_path)
    assert captured["apply_mode"] == "1"


def test_execute_manual_open_runtime_sync_override_can_disable_data_config_on(monkeypatch, tmp_path: Path) -> None:
    import src.application.positions.workflows as workflows
    from src.application.positions.sync_config import apply_option_positions_runtime_config

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(repo.db_path),
                    "sync_to_feishu": {"enabled": True},
                },
                "feishu": {
                    "app_id": "app_id",
                    "app_secret": "app_secret",
                    "tables": {"option_positions": "app_token/table_id"},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    apply_option_positions_runtime_config(repo, {"option_positions": {"sync_to_feishu": {"enabled": False}}})

    def _should_not_sync(*, repo, data_config, record_id, apply_mode):
        raise AssertionError("runtime override should disable post-write sync")

    monkeypatch.setattr(workflows, "sync_single_option_position_record", _should_not_sync)

    out = workflows.execute_manual_open(
        repo,
        broker="富途",
        account="lx",
        symbol="TSLA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100.0,
        expiration_ymd="2026-06-19",
        premium_per_share=1.23,
        underlying_share_locked=None,
        note=None,
        dry_run=False,
    )

    assert out["mode"] == "applied"
    assert out["sync_result"] is None


def test_manual_open_record_id_prefers_explicit_record_id_before_event_id_guess() -> None:
    import src.application.positions.workflows as workflows

    explicit = workflows._manual_open_record_id(
        {
            "event_id": "manual-open-should-not-win",
            "record_id": "rec_bootstrap_like_manual",
        }
    )
    fallback = workflows._manual_open_record_id({"event_id": "manual-open-fallback"})

    assert explicit == "rec_bootstrap_like_manual"
    assert fallback == "lot_manual-open-fallback"


def test_execute_manual_close_warns_when_best_effort_sync_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.application.positions.workflows as workflows
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(repo.db_path),
                    "sync_to_feishu": {"enabled": True},
                },
                "feishu": {
                    "app_id": "app_id",
                    "app_secret": "app_secret",
                    "tables": {"option_positions": "app_token/table_id"},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=2,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]

    def _failing_sync(*, repo, data_config, record_id, apply_mode):
        raise RuntimeError("boom")

    monkeypatch.setattr(workflows, "sync_single_option_position_record", _failing_sync)

    out = workflows.execute_manual_close(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        dry_run=False,
    )

    assert out["mode"] == "applied"
    assert out["sync_result"] is None
    assert "post-write Feishu sync skipped" in capsys.readouterr().err


def test_execute_manual_close_full_close_retry_is_idempotent_without_masking_validation(tmp_path: Path) -> None:
    import src.application.positions.workflows as workflows
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
    lot = repo.list_position_lots()[0]

    first = workflows.execute_manual_close(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        dry_run=False,
    )
    second = workflows.execute_manual_close(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        dry_run=False,
    )

    assert first["result"]["created"] is True
    assert first["ledger_preflight"]["status"] == "ok"
    assert second["idempotent_duplicate"] is True
    assert second["ledger_preflight"]["status"] == "duplicate"
    assert second["result"]["created"] is False
    assert second["result"]["event_id"] == first["result"]["event_id"]
    assert len(repo.list_trade_events()) == 2


def test_manual_close_auto_match_does_not_use_legacy_list_records_fallback() -> None:
    import pytest
    import src.application.positions.workflows as workflows

    class _LegacyOnlyRepo:
        def list_records(self, *, page_size: int = 500) -> list[dict]:
            return [
                {
                    "record_id": "legacy_1",
                    "fields": {
                        "broker": "富途",
                        "account": "lx",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "status": "open",
                        "contracts_open": 1,
                        "strike": 480.0,
                        "expiration_ymd": "2026-04-29",
                    },
                }
            ]

    with pytest.raises(workflows.ManualCloseMatchError) as exc_info:
        workflows.resolve_manual_close_record_id(
            _LegacyOnlyRepo(),
            account="lx",
            symbol="0700.HK",
            option_type="put",
            position_side="short",
            strike=480.0,
            expiration_ymd="2026-04-29",
            contracts_to_close=1,
        )

    assert exc_info.value.code == "not_found"
    assert exc_info.value.candidates == []


def test_execute_manual_close_auto_matches_unique_selector(tmp_path: Path) -> None:
    import src.application.positions.workflows as workflows
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
            contracts=2,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
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
            strike=500.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=2.1,
            opened_at_ms=2000,
        ),
    )
    target_lot = next(row for row in repo.list_position_lots() if row["fields"]["strike"] == 480.0)

    out = workflows.execute_manual_close(
        repo,
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        dry_run=False,
        account="lx",
        symbol="0700.HK",
        option_type="put",
        position_side="short",
        strike=480.0,
        expiration_ymd="2026-04-29",
    )

    assert out["match"]["rule"] == "strict_contract_unique"
    assert out["match"]["record_id"] == target_lot["record_id"]
    assert out["match"]["close_target_resolution"]["record_ids"] == [target_lot["record_id"]]
    assert out["close_target_resolution"]["strategy"] == "explicit_record_id_current_lot"
    fields = repo.get_record_fields(target_lot["record_id"])
    assert fields["contracts_open"] == 1
    assert fields["contracts_closed"] == 1
    assert out["ledger_preflight"]["target_lot_id"] == target_lot["record_id"]


def test_execute_manual_close_sy_0700_same_strike_different_expiry_targets_exact_lot(tmp_path: Path) -> None:
    import src.application.positions.workflows as workflows
    from domain.domain.option_position_lots import OpenPositionCommand, effective_expiration_ymd

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for option_type, strike, expiration_ymd, contracts in (
        ("call", 510.0, "2026-05-28", 2),
        ("put", 450.0, "2026-05-28", 6),
        ("put", 450.0, "2026-06-29", 3),
    ):
        ledger_manual_trades.persist_manual_open_event(
            repo,
            OpenPositionCommand(
                broker="富途",
                account="sy",
                symbol="0700.HK",
                option_type=option_type,
                side="short",
                contracts=contracts,
                currency="HKD",
                strike=strike,
                multiplier=100,
                expiration_ymd=expiration_ymd,
                premium_per_share=1.0,
                opened_at_ms=1000 + contracts,
            ),
        )
    target_lot = next(
        row
        for row in repo.list_position_lots()
        if row["fields"]["option_type"] == "put" and effective_expiration_ymd(row["fields"]) == "2026-05-28"
    )

    out = workflows.execute_manual_close(
        repo,
        contracts_to_close=1,
        close_price=0.5,
        close_reason="manual_buy_to_close",
        dry_run=True,
        account="sy",
        symbol="0700.HK",
        option_type="put",
        position_side="short",
        strike=450.0,
        expiration_ymd="2026-05-28",
    )

    assert out["match"]["rule"] == "strict_contract_unique"
    assert out["match"]["record_id"] == target_lot["record_id"]
    assert out["match"]["close_target_resolution"]["selector"]["expiration_ymd"] == "2026-05-28"
    assert out["close_target_resolution"]["record_ids"] == [target_lot["record_id"]]
    assert out["ledger_preflight"]["target_lot_id"] == target_lot["record_id"]
    assert out["ledger_preflight"]["contracts_open_before"] == 6
    assert out["ledger_preflight"]["contracts_open_after"] == 5


def test_execute_manual_close_auto_match_rejects_multiple_candidates(tmp_path: Path) -> None:
    import pytest
    import src.application.positions.workflows as workflows
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for opened_at in (1000, 2000):
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
                opened_at_ms=opened_at,
            ),
        )

    with pytest.raises(workflows.ManualCloseMatchError) as exc_info:
        workflows.execute_manual_close(
            repo,
            contracts_to_close=1,
            close_price=1.2,
            close_reason="manual_buy_to_close",
            dry_run=True,
            account="lx",
            symbol="0700.HK",
            option_type="put",
            position_side="short",
            strike=480.0,
            expiration_ymd="2026-04-29",
        )

    assert exc_info.value.code == "multiple_matches"
    assert len(exc_info.value.candidates) == 2
    assert all(row["contracts_open"] == 1 for row in exc_info.value.candidates)


def test_execute_manual_close_auto_match_not_found_includes_near_candidates(tmp_path: Path) -> None:
    import pytest
    import src.application.positions.workflows as workflows
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
            strike=500.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=2.1,
            opened_at_ms=1000,
        ),
    )

    with pytest.raises(workflows.ManualCloseMatchError) as exc_info:
        workflows.execute_manual_close(
            repo,
            contracts_to_close=1,
            close_price=1.2,
            close_reason="manual_buy_to_close",
            dry_run=True,
            account="lx",
            symbol="0700.HK",
            option_type="put",
            position_side="short",
            strike=480.0,
            expiration_ymd="2026-04-29",
        )

    assert exc_info.value.code == "not_found"
    assert len(exc_info.value.candidates) == 1
    assert exc_info.value.candidates[0]["strike"] == 500.0


def test_execute_manual_open_keeps_local_lot_when_best_effort_sync_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.application.positions.workflows as workflows

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(repo.db_path),
                    "sync_to_feishu": {"enabled": True},
                },
                "feishu": {
                    "app_id": "app_id",
                    "app_secret": "app_secret",
                    "tables": {"option_positions": "app_token/table_id"},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    def _failing_sync(*, repo, data_config, record_id, apply_mode):
        raise RuntimeError("boom")

    monkeypatch.setattr(workflows, "sync_single_option_position_record", _failing_sync)

    out = workflows.execute_manual_open(
        repo,
        broker="富途",
        account="lx",
        symbol="TSLA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100.0,
        expiration_ymd="2026-06-19",
        premium_per_share=1.23,
        underlying_share_locked=None,
        note=None,
        dry_run=False,
    )

    assert out["mode"] == "applied"
    assert out["sync_result"] is None
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["account"] == "lx"
    assert lots[0]["fields"]["status"] == "open"
    assert lots[0]["fields"]["contracts_open"] == 1
    assert "post-write Feishu sync skipped" in capsys.readouterr().err
