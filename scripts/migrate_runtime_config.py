#!/usr/bin/env python3
"""Migrate runtime config files to the current schedule policy.

Production configs are recommended to live outside this Git repository; pass
them via --config /absolute/path/to/config.us.json. For local development,
omitting --config keeps the compatibility default of repo-local config.us.json
and config.hk.json. By default the script only reports what would change; pass
--apply to write the migrated file after creating a .bak copy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.infrastructure.io_utils import atomic_write_text
from src.application.config_validator import validate_config


LEGACY_SCHEDULE_KEYS = (
    "market_timezone",
    "market_open",
    "market_close",
    "market_break_start",
    "market_break_end",
    "market_dense_interval_min",
    "market_sparse_interval_min",
    "market_hours_interval_min",
    "monitor_off_hours",
    "notify_cooldown_min",
    "notify_cooldown_dense_min",
    "notify_cooldown_sparse_min",
    "sparse_after_beijing",
    "interval_min",
    "first_notify_after_open_min",
    "notify_interval_min",
    "final_notify_before_close_min",
    "schedule_v2",
)

DEFAULT_RUN_POINTS = {
    "start_plus_min": 10,
    "hourly_minute": 0,
    "end_minus_min": 10,
}

US_BEIJING_BEFORE_2AM_GATE = {
    "type": "before",
    "timezone": "Asia/Shanghai",
    "time": "02:00",
    "day_offset_from_window_start": 1,
}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"[CONFIG_ERROR] missing file: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[CONFIG_ERROR] invalid JSON in {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"[CONFIG_ERROR] JSON root must be object: {path}")
    return data


def _dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _backup(path: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, backup)
    return backup


def _infer_market_from_path(path: Path) -> str | None:
    name = path.name.lower()
    if ".hk." in name or name.startswith("config.hk"):
        return "hk"
    if ".us." in name or name.startswith("config.us"):
        return "us"
    return None


def _infer_market_for_schedule(schedule_key: str, schedule: dict[str, Any], market: str | None) -> str:
    if schedule_key == "schedule_hk":
        return "hk"
    if market in {"us", "hk"}:
        return market
    tz = str(schedule.get("timezone") or schedule.get("market_timezone") or "").strip()
    if tz == "Asia/Hong_Kong":
        return "hk"
    return "us"


def _default_timezone(market: str) -> str:
    return "Asia/Hong_Kong" if market == "hk" else "America/New_York"


def _default_breaks(market: str) -> list[dict[str, str]]:
    return [{"start": "12:00", "end": "13:00"}] if market == "hk" else []


def _target_run_window(schedule: dict[str, Any], market: str) -> dict[str, Any]:
    current = schedule.get("run_window") if isinstance(schedule.get("run_window"), dict) else {}
    breaks = current.get("breaks") if isinstance(current.get("breaks"), list) else None
    legacy_break_start = schedule.get("market_break_start")
    legacy_break_end = schedule.get("market_break_end")
    if legacy_break_start and legacy_break_end:
        breaks = [{"start": str(legacy_break_start), "end": str(legacy_break_end)}]
    if breaks is None:
        breaks = _default_breaks(market)
    return {
        "start": str(schedule.get("market_open") or current.get("start") or "09:30"),
        "end": str(schedule.get("market_close") or current.get("end") or "16:00"),
        "breaks": breaks,
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return int(default)
    return parsed if parsed > 0 else int(default)


def migrate_schedule(schedule: Any, *, schedule_key: str = "schedule", market: str | None = None) -> tuple[bool, list[str]]:
    if not isinstance(schedule, dict):
        return False, []

    changes: list[str] = []
    changed = False
    schedule_market = _infer_market_for_schedule(schedule_key, schedule, market)

    desired_values: dict[str, Any] = {
        "timezone": str(schedule.get("timezone") or schedule.get("market_timezone") or _default_timezone(schedule_market)),
        "cron_interval_min": _positive_int(schedule.get("cron_interval_min"), 10),
        "run_window": _target_run_window(schedule, schedule_market),
        "run_points": (
            schedule.get("run_points")
            if isinstance(schedule.get("run_points"), dict)
            else dict(DEFAULT_RUN_POINTS)
        ),
    }
    if schedule_market == "us":
        gates = schedule.get("gates") if isinstance(schedule.get("gates"), list) else [dict(US_BEIJING_BEFORE_2AM_GATE)]
        desired_values["gates"] = gates

    for key, value in desired_values.items():
        if schedule.get(key) != value:
            schedule[key] = value
            changes.append(f"set {key}={value}")
            changed = True

    removed = []
    for key in LEGACY_SCHEDULE_KEYS:
        if key in schedule:
            removed.append(key)
            del schedule[key]
            changed = True
    if removed:
        changes.append("remove " + ",".join(removed))

    return changed, changes


def migrate_config(data: dict[str, Any], *, market: str | None = None) -> tuple[dict[str, Any], list[str]]:
    migrated = json.loads(json.dumps(data, ensure_ascii=False))
    changes: list[str] = []
    for key in ("schedule", "schedule_hk"):
        changed, schedule_changes = migrate_schedule(migrated.get(key), schedule_key=key, market=market)
        if changed:
            changes.extend(f"{key}: {item}" for item in schedule_changes)
    return migrated, changes


def _resolve_config_paths(base_dir: Path, values: list[str] | None) -> list[Path]:
    raw = values or ["config.us.json", "config.hk.json"]
    paths: list[Path] = []
    for value in raw:
        path = Path(value)
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        paths.append(path)
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate local runtime config schedule fields to the simplified policy"
    )
    parser.add_argument("--base-dir", default=str(BASE_DIR))
    parser.add_argument(
        "--config",
        action="append",
        help=(
            "Config file to migrate. Can be provided multiple times; absolute "
            "paths are recommended for production. Default: repo-local "
            "config.us.json and config.hk.json"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write migrated configs and create .bak backups")
    mode.add_argument("--check", action="store_true", help="Exit non-zero when migration would change any file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(args.base_dir).resolve()
    paths = _resolve_config_paths(base_dir, args.config)

    changed_paths: list[Path] = []
    for path in paths:
        data = _load_json(path)
        migrated, changes = migrate_config(data, market=_infer_market_from_path(path))
        changed = _dump_json(data) != _dump_json(migrated)
        status = "CHANGED" if changed else "OK"
        rel = path.relative_to(base_dir) if path.is_relative_to(base_dir) else path
        print(f"[{status}] {rel}")
        for item in changes:
            print(f"  - {item}")

        if not changed:
            continue

        changed_paths.append(path)
        validate_config(migrated)
        if args.apply:
            backup = _backup(path)
            atomic_write_text(path, _dump_json(migrated))
            print(f"  backup: {backup}")
            print("  wrote migrated config")

    if args.check:
        if changed_paths:
            print("[CHECK_FAIL] migration needed: " + ", ".join(str(p) for p in changed_paths))
            raise SystemExit(1)
        print("[CHECK_OK] configs already use the current schedule policy")
        return

    if args.apply:
        print(f"[APPLY_OK] updated files: {len(changed_paths)}")
        return

    if changed_paths:
        print("[DRY_RUN] run again with --apply to write changes")
    else:
        print("[DRY_RUN] no changes needed")


if __name__ == "__main__":
    main()
