from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_execute_manual_open_triggers_best_effort_sync(monkeypatch, tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc
    import src.application.position_workflows as workflows

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": str(repo.db_path)},
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
    assert out["sync_result"]["action"] == "update"
    assert captured["record_id"].startswith("lot_manual-open-")
    assert captured["data_config"] == str(repo.data_config_path)
    assert captured["apply_mode"] == "1"


def test_build_trade_open_command_keeps_optional_contract_fields_null_instead_of_string_none() -> None:
    import src.application.position_workflows as workflows
    from scripts.trade_event_normalizer import NormalizedTradeDeal

    command = workflows._build_trade_open_command(
        NormalizedTradeDeal(
            broker="富途",
            futu_account_id="REAL_1",
            internal_account="lx",
            deal_id="deal-preview-1",
            order_id="order-1",
            symbol="NVDA",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.23,
            strike=None,
            multiplier=None,
            multiplier_source=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=1000,
            raw_payload={},
        )
    )

    assert command.symbol == "NVDA"
    assert command.currency == "USD"
    assert command.strike is None
    assert command.multiplier is None
    assert command.expiration_ymd is None
    assert "multiplier_source=" in str(command.note)
    assert "None" not in str(command.note)


def test_manual_open_record_id_prefers_explicit_record_id_before_event_id_guess() -> None:
    import src.application.position_workflows as workflows

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
    import scripts.option_positions_core.service as svc
    import src.application.position_workflows as workflows
    from scripts.option_positions_core.domain import OpenPositionCommand

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": str(repo.db_path)},
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
    svc.persist_manual_open_event(
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


def test_execute_manual_open_keeps_local_lot_when_best_effort_sync_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    import scripts.option_positions_core.service as svc
    import src.application.position_workflows as workflows

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    repo.data_config_path.write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": str(repo.db_path)},
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
