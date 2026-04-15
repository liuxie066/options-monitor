from __future__ import annotations

import json
from pathlib import Path


def test_migrate_config_updates_schedule_fields_without_touching_other_config() -> None:
    from scripts.migrate_runtime_config import migrate_config

    cfg = {
        "notifications": {"target": "user:secret"},
        "schedule": {
            "enabled": True,
            "market_timezone": "America/New_York",
            "market_open": "09:30",
            "market_close": "16:00",
            "notify_cooldown_min": 60,
            "interval_min": 45,
            "market_dense_interval_min": 10,
            "market_sparse_interval_min": 60,
            "schedule_v2": {"enabled": True},
        },
        "schedule_hk": {
            "enabled": True,
            "market_timezone": "Asia/Hong_Kong",
            "market_break_start": "12:00",
            "market_break_end": "13:00",
            "notify_cooldown_min": 30,
        },
    }

    migrated, changes = migrate_config(cfg)

    assert migrated["notifications"] == cfg["notifications"]
    assert migrated["schedule"]["first_notify_after_open_min"] == 30
    assert migrated["schedule"]["notify_interval_min"] == 45
    assert migrated["schedule"]["final_notify_before_close_min"] == 10
    assert migrated["schedule_hk"]["notify_interval_min"] == 30
    assert migrated["schedule_hk"]["market_break_start"] == "12:00"
    assert "market_dense_interval_min" not in migrated["schedule"]
    assert "market_sparse_interval_min" not in migrated["schedule"]
    assert "notify_cooldown_min" not in migrated["schedule_hk"]
    assert "schedule_v2" not in migrated["schedule"]
    assert any(item.startswith("schedule:") for item in changes)
    assert any(item.startswith("schedule_hk:") for item in changes)


def test_migrate_runtime_config_cli_dry_run_does_not_write(tmp_path: Path, capsys) -> None:
    from scripts import migrate_runtime_config as mod

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schedule": {
                    "enabled": True,
                    "interval_min": 60,
                    "notify_cooldown_min": 60,
                },
                "symbols": [{"symbol": "NVDA"}],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    before = cfg_path.read_text(encoding="utf-8")

    old_parse_args = mod.parse_args
    try:
        mod.parse_args = lambda: type(  # type: ignore[assignment]
            "Args",
            (),
            {
                "base_dir": str(tmp_path),
                "config": [str(cfg_path)],
                "apply": False,
                "check": False,
            },
        )()
        mod.main()
    finally:
        mod.parse_args = old_parse_args  # type: ignore[assignment]

    assert cfg_path.read_text(encoding="utf-8") == before
    assert "[DRY_RUN]" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.bak.*"))


def test_migrate_runtime_config_cli_apply_writes_backup(tmp_path: Path) -> None:
    from scripts import migrate_runtime_config as mod

    cfg_path = tmp_path / "config.hk.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schedule": {
                    "enabled": True,
                    "notify_cooldown_min": 60,
                },
                "symbols": [{"symbol": "0700.HK"}],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    old_parse_args = mod.parse_args
    try:
        mod.parse_args = lambda: type(  # type: ignore[assignment]
            "Args",
            (),
            {
                "base_dir": str(tmp_path),
                "config": [str(cfg_path)],
                "apply": True,
                "check": False,
            },
        )()
        mod.main()
    finally:
        mod.parse_args = old_parse_args  # type: ignore[assignment]

    migrated = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert migrated["schedule"]["first_notify_after_open_min"] == 30
    assert migrated["schedule"]["notify_interval_min"] == 60
    assert migrated["schedule"]["final_notify_before_close_min"] == 10
    assert "notify_cooldown_min" not in migrated["schedule"]
    assert len(list(tmp_path.glob("config.hk.json.bak.*"))) == 1
