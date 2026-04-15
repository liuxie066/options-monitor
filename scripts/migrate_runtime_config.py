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

from scripts.io_utils import atomic_write_text


LEGACY_SCHEDULE_KEYS = (
    "market_dense_interval_min",
    "market_sparse_interval_min",
    "market_hours_interval_min",
    "notify_cooldown_min",
    "notify_cooldown_dense_min",
    "notify_cooldown_sparse_min",
    "sparse_after_beijing",
    "interval_min",
    "schedule_v2",
)

DEFAULT_SCHEDULE_VALUES = {
    "first_notify_after_open_min": 30,
    "notify_interval_min": 60,
    "final_notify_before_close_min": 10,
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


def migrate_schedule(schedule: Any) -> tuple[bool, list[str]]:
    if not isinstance(schedule, dict):
        return False, []

    changes: list[str] = []
    changed = False

    interval_value = schedule.get("notify_interval_min")
    if interval_value is None:
        interval_value = schedule.get("interval_min")
    if interval_value is None:
        interval_value = schedule.get("notify_cooldown_min")
    if interval_value is None:
        interval_value = DEFAULT_SCHEDULE_VALUES["notify_interval_min"]

    desired_values = {
        **DEFAULT_SCHEDULE_VALUES,
        "notify_interval_min": interval_value,
    }
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


def migrate_config(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    migrated = json.loads(json.dumps(data, ensure_ascii=False))
    changes: list[str] = []
    for key in ("schedule", "schedule_hk"):
        changed, schedule_changes = migrate_schedule(migrated.get(key))
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
        migrated, changes = migrate_config(data)
        changed = _dump_json(data) != _dump_json(migrated)
        status = "CHANGED" if changed else "OK"
        rel = path.relative_to(base_dir) if path.is_relative_to(base_dir) else path
        print(f"[{status}] {rel}")
        for item in changes:
            print(f"  - {item}")

        if not changed:
            continue

        changed_paths.append(path)
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
