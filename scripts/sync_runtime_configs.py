#!/usr/bin/env python3
"""Sync derived runtime configs from canonical config.us.json/config.hk.json.

Single-source policy:
- Canonical runtime configs: config.us.json, config.hk.json
- Derived compatibility configs:
  - config.scheduled.json
  - config.market_us.json
  - config.market_hk.json
  - config.market_us.fallback_yahoo.json
  - config.json

Current sync scope (minimal-risk):
- notifications
"""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_FIELDS = ("notifications",)

@dataclass(frozen=True)
class DerivedTarget:
    path: str
    canonical: str
    template: str | None = None


DERIVED_TARGETS: tuple[DerivedTarget, ...] = (
    DerivedTarget("config.scheduled.json", "config.us.json", "configs/examples/config.scheduled.example.json"),
    DerivedTarget("config.market_us.json", "config.us.json", "configs/examples/config.market_us.example.json"),
    DerivedTarget("config.market_hk.json", "config.hk.json", "configs/examples/config.market_hk.example.json"),
    DerivedTarget(
        "config.market_us.fallback_yahoo.json",
        "config.us.json",
        "configs/examples/config.market_us.fallback_yahoo.example.json",
    ),
    DerivedTarget("config.json", "config.us.json", "configs/examples/config.legacy.example.json"),
)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"[CONFIG_ERROR] missing file: {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"[CONFIG_ERROR] invalid JSON in {path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"[CONFIG_ERROR] JSON root must be object: {path}")
    return data


def _json_dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _get_path(data: dict, path: str) -> object:
    cur: object = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


def _set_path(data: dict, path: str, value: object) -> None:
    cur = data
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _load_target_base(base_dir: Path, target: DerivedTarget, canonical_data: dict) -> tuple[dict, str]:
    dst_path = (base_dir / target.path).resolve()
    if dst_path.exists():
        return _load_json(dst_path), "existing"

    if target.template:
        tpl_path = (base_dir / target.template).resolve()
        if tpl_path.exists():
            return _load_json(tpl_path), "template"

    return copy.deepcopy(canonical_data), "canonical"


def compute_sync_plan(base_dir: Path, fields: tuple[str, ...] = DEFAULT_FIELDS) -> list[dict[str, object]]:
    canonical_cache: dict[str, dict] = {}
    plans: list[dict[str, object]] = []

    for target in DERIVED_TARGETS:
        src_path = (base_dir / target.canonical).resolve()
        if target.canonical not in canonical_cache:
            canonical_cache[target.canonical] = _load_json(src_path)
        src_data = canonical_cache[target.canonical]

        dst_data_before, seed = _load_target_base(base_dir, target, src_data)
        dst_data_after = copy.deepcopy(dst_data_before)

        copied_fields: list[str] = []
        for path in fields:
            val = copy.deepcopy(_get_path(src_data, path))
            _set_path(dst_data_after, path, val)
            copied_fields.append(path)

        before_text = _json_dump(dst_data_before)
        after_text = _json_dump(dst_data_after)
        dst_path = (base_dir / target.path).resolve()
        exists = dst_path.exists()
        changed = (not exists) or (before_text != after_text)

        plans.append(
            {
                "target": target.path,
                "canonical": target.canonical,
                "seed": seed,
                "exists": exists,
                "changed": changed,
                "copied_fields": copied_fields,
                "after_text": after_text,
            }
        )
    return plans


def apply_sync_plan(base_dir: Path, plans: list[dict[str, object]]) -> int:
    changed = 0
    for item in plans:
        if not bool(item["changed"]):
            continue
        dst_path = (base_dir / str(item["target"])).resolve()
        dst_path.write_text(str(item["after_text"]), encoding="utf-8")
        changed += 1
    return changed


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Sync derived runtime configs from canonical US/HK configs")
    ap.add_argument("--base-dir", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument(
        "--fields",
        default=",".join(DEFAULT_FIELDS),
        help="Comma-separated field paths copied from canonical config to derived configs (default: notifications)",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write synced derived config files")
    mode.add_argument("--check", action="store_true", help="Exit non-zero when any derived config is out of sync")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(args.base_dir).resolve()
    fields = tuple(x.strip() for x in str(args.fields).split(",") if x.strip())
    if not fields:
        raise SystemExit("[ARG_ERROR] --fields cannot be empty")

    plans = compute_sync_plan(base_dir=base_dir, fields=fields)
    changed_targets = [str(p["target"]) for p in plans if bool(p["changed"])]

    for p in plans:
        status = "CHANGED" if bool(p["changed"]) else "OK"
        print(
            f"[{status}] {p['target']} <= {p['canonical']} "
            f"(seed={p['seed']}, fields={','.join(p['copied_fields'])})"
        )

    if args.check:
        if changed_targets:
            print(f"[CHECK_FAIL] out of sync files: {', '.join(changed_targets)}")
            raise SystemExit(1)
        print("[CHECK_OK] all derived runtime configs are in sync")
        return

    if args.apply:
        n = apply_sync_plan(base_dir=base_dir, plans=plans)
        print(f"[APPLY_OK] updated files: {n}")
        return

    if changed_targets:
        print(
            f"[DRY_RUN] out of sync files: {', '.join(changed_targets)}. "
            "Run with --apply to sync or --check for strict validation."
        )
    else:
        print("[DRY_RUN] all derived runtime configs are already in sync")


if __name__ == "__main__":
    main()
