from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def test_position_maintenance_filters_account_and_broker_in_dry_run(monkeypatch, tmp_path: Path) -> None:
    from src.application import position_maintenance as mod

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
    monkeypatch.setattr(mod, "load_option_positions_repo", lambda _path: fake_repo)
    monkeypatch.setattr(mod, "load_option_position_records", lambda _repo: records)

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

    monkeypatch.setattr(mod, "build_expired_close_decisions", _build_decisions)
    monkeypatch.setattr(
        mod,
        "auto_close_expired_positions",
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


def test_position_maintenance_rejects_invalid_auto_close_config(tmp_path: Path) -> None:
    from src.application import position_maintenance as mod

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
