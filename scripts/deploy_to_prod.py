#!/usr/bin/env python3
"""Deploy from dev repo (options-monitor) to prod repo (options-monitor-prod).

Policy:
- Copy selected paths (scripts/tests/docs/requirements/.gitignore + selected root configs)
- Default: skip runtime configs (prod keeps local runtime config files)
- Keep syncing config.example.*.json templates
- Use --include-runtime-config to allow config overwrite explicitly
- Runtime config overwrite is gated by allowlist (required with --include-runtime-config)
- Default: dry-run; use --apply to write

This is a minimal replacement for rsync (not always available in the image).
"""

from __future__ import annotations

import argparse
import copy
import filecmp
import json
import shutil
import subprocess
from pathlib import Path

ROOT_DEV = Path("/home/node/.openclaw/workspace/options-monitor")
ROOT_PROD = Path("/home/node/.openclaw/workspace/options-monitor-prod")

ITEMS = [
    ".gitignore",
    "requirements.txt",
    "README.md",
    "RUNBOOK.md",
    "DEPLOY.md",
    "CONFIGS.md",
    "CONFIGURATION_GUIDE.md",
    "SKILL.md",
    "config.us.json",
    "config.hk.json",
    "config.legacy.example.json",
    "config.scheduled.example.json",
    "config.market_us.example.json",
    "config.market_hk.example.json",
    "config.market_us.fallback_yahoo.example.json",
    "scripts",
    "tests",
    "docs",
]

# Exclusions relative to repo root
EXCLUDE_TOP = {
    ".venv",
    "output",
    "output_accounts",
    "output_shared",
    "secrets",
    "cache",
}

# Exclusions by filename (anywhere)
EXCLUDE_FILES = {"disable_autodeploy.flag"}

RUNTIME_CONFIG_EXACT = {
    "config.us.json",
    "config.hk.json",
}


class RuntimeAllowlist:
    def __init__(self, rules: list[dict[str, object]]) -> None:
        self.rules = rules

    @classmethod
    def from_file(cls, path: Path) -> "RuntimeAllowlist":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise SystemExit(f"[ARG_ERROR] failed to load runtime allowlist {path}: {e}")

        if isinstance(data, dict):
            rules = data.get("rules", [])
        else:
            rules = data

        if not isinstance(rules, list) or not rules:
            raise SystemExit(f"[ARG_ERROR] runtime allowlist {path} must contain non-empty rules")

        norm_rules: list[dict[str, object]] = []
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise SystemExit(f"[ARG_ERROR] allowlist rule #{i + 1} must be an object")
            fields = rule.get("fields")
            if not isinstance(fields, list) or not fields or any(not isinstance(f, str) or not f for f in fields):
                raise SystemExit(f"[ARG_ERROR] allowlist rule #{i + 1} requires non-empty string fields")
            market = rule.get("market")
            symbol = rule.get("symbol")
            if market is not None and not isinstance(market, str):
                raise SystemExit(f"[ARG_ERROR] allowlist rule #{i + 1} market must be string")
            if symbol is not None and not isinstance(symbol, str):
                raise SystemExit(f"[ARG_ERROR] allowlist rule #{i + 1} symbol must be string")
            norm_rules.append({"market": market, "symbol": symbol, "fields": fields})
        return cls(norm_rules)

    def allowed_fields(self, market: str | None, symbol: str | None) -> set[str]:
        out: set[str] = set()
        for rule in self.rules:
            rule_market = rule.get("market")
            rule_symbol = rule.get("symbol")
            if rule_market is not None and rule_market != market:
                continue
            if rule_symbol is not None and rule_symbol != symbol:
                continue
            out.update(rule["fields"])
        return out


def sync_items() -> list[str]:
    """Return sync scope, including all top-level config.example.*.json templates."""
    items = list(ITEMS)
    for p in sorted(ROOT_DEV.glob("config.example.*.json")):
        if p.is_file():
            items.append(p.name)
    # Keep a stable order while removing duplicates.
    return list(dict.fromkeys(items))


def dev_ref() -> str:
    try:
        out = subprocess.check_output(["git", "-c", f"safe.directory={ROOT_DEV}", "rev-parse", "--short", "HEAD"], cwd=str(ROOT_DEV)).decode().strip()
        return out
    except Exception:
        return "unknown"


def is_runtime_config(path_rel: Path) -> bool:
    if len(path_rel.parts) != 1:
        return False
    name = path_rel.name
    if name in RUNTIME_CONFIG_EXACT:
        return True
    if name.startswith("config.local.") and name.endswith(".json"):
        return True
    return False


def should_skip(path_rel: Path, include_runtime_config: bool = False) -> bool:
    parts = path_rel.parts
    if parts and parts[0] in EXCLUDE_TOP:
        return True
    if path_rel.name in EXCLUDE_FILES:
        return True
    if not include_runtime_config and is_runtime_config(path_rel):
        return True
    return False


def iter_files(src_root: Path, include_runtime_config: bool) -> list[Path]:
    if src_root.is_file():
        rel0 = src_root.relative_to(ROOT_DEV)
        if should_skip(rel0, include_runtime_config=include_runtime_config):
            return []
        return [src_root]
    files: list[Path] = []
    for p in src_root.rglob("*"):
        if not p.is_file():
            continue
        # Skip bytecode/cache.
        if p.suffix == ".pyc" or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(ROOT_DEV)
        if should_skip(rel, include_runtime_config=include_runtime_config):
            continue
        files.append(p)
    return files


def _leaf_field_paths(obj: object, prefix: str = "") -> list[str]:
    if isinstance(obj, dict):
        out: list[str] = []
        for key, val in obj.items():
            if not isinstance(key, str):
                continue
            p = f"{prefix}.{key}" if prefix else key
            if isinstance(val, dict):
                out.extend(_leaf_field_paths(val, p))
            else:
                out.append(p)
        return out
    return [prefix] if prefix else []


def _get_path(obj: dict[str, object], field: str) -> object:
    cur: object = obj
    for key in field.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(field)
        cur = cur[key]
    return cur


def _set_path(obj: dict[str, object], field: str, value: object) -> None:
    cur = obj
    keys = field.split(".")
    for key in keys[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[keys[-1]] = value


def plan_runtime_merge(src: Path, dst: Path, allowlist: RuntimeAllowlist) -> tuple[str, list[str], list[str]]:
    src_data = json.loads(src.read_text(encoding="utf-8"))
    dst_data = json.loads(dst.read_text(encoding="utf-8"))
    merged_data = copy.deepcopy(dst_data)
    allowed_updates: list[str] = []
    protected_skips: list[str] = []

    src_symbols_raw = src_data.get("symbols", [])
    merged_symbols_raw = merged_data.get("symbols", [])
    if not isinstance(src_symbols_raw, list) or not isinstance(merged_symbols_raw, list):
        merged_text = json.dumps(merged_data, ensure_ascii=False, indent=2) + "\n"
        return merged_text, allowed_updates, protected_skips

    src_symbols: dict[str, dict[str, object]] = {}
    for item in src_symbols_raw:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        if isinstance(symbol, str) and symbol:
            src_symbols[symbol] = item

    for merged_sym in merged_symbols_raw:
        if not isinstance(merged_sym, dict):
            continue
        symbol = merged_sym.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        src_sym = src_symbols.get(symbol)
        if src_sym is None:
            continue
        market = src_sym.get("market")
        market_str = market if isinstance(market, str) else None
        allow_fields = allowlist.allowed_fields(market_str, symbol)

        for field in _leaf_field_paths(src_sym):
            if field in {"symbol", "market"}:
                continue
            try:
                src_val = _get_path(src_sym, field)
            except KeyError:
                continue
            dst_val_missing = False
            try:
                dst_val = _get_path(merged_sym, field)
            except KeyError:
                dst_val_missing = True
                dst_val = None

            changed = dst_val_missing or (dst_val != src_val)
            if not changed:
                continue

            item_id = f"{src.name}:{symbol}:{field}"
            if field in allow_fields:
                _set_path(merged_sym, field, copy.deepcopy(src_val))
                allowed_updates.append(item_id)
            else:
                protected_skips.append(item_id)

    merged_text = json.dumps(merged_data, ensure_ascii=False, indent=2) + "\n"
    return merged_text, sorted(allowed_updates), sorted(protected_skips)


def plan_copy(
    include_runtime_config: bool,
    runtime_allowlist: RuntimeAllowlist | None,
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]], list[Path], list[Path], dict[Path, str], list[str], list[str]]:
    """Return (added, updated, deletes, skipped_runtime_configs, runtime_write_map, runtime_allowed, runtime_protected)."""
    added: list[tuple[Path, Path]] = []
    updated: list[tuple[Path, Path]] = []
    skipped: list[Path] = []
    runtime_write_map: dict[Path, str] = {}
    runtime_allowed: list[str] = []
    runtime_protected: list[str] = []

    # Build dev file set
    dev_files: set[Path] = set()
    for it in sync_items():
        src = ROOT_DEV / it
        if not src.exists():
            continue
        if src.is_file():
            rel = src.relative_to(ROOT_DEV)
            if (not include_runtime_config) and is_runtime_config(rel):
                skipped.append(rel)
                continue
        for f in iter_files(src, include_runtime_config=include_runtime_config):
            rel = f.relative_to(ROOT_DEV)
            dev_files.add(rel)

    # Determine adds/updates
    for rel in sorted(dev_files):
        src = ROOT_DEV / rel
        dst = ROOT_PROD / rel

        if include_runtime_config and runtime_allowlist is not None and is_runtime_config(rel):
            if not dst.exists():
                runtime_protected.append(f"{src.name}:FILE_MISSING_IN_PROD")
                continue
            merged_text, allowed_updates, protected_skips = plan_runtime_merge(src=src, dst=dst, allowlist=runtime_allowlist)
            runtime_allowed.extend(allowed_updates)
            runtime_protected.extend(protected_skips)
            dst_text = ""
            if dst.exists():
                dst_text = dst.read_text(encoding="utf-8")
            if (not dst.exists()) or (dst_text != merged_text):
                runtime_write_map[dst] = merged_text
                if not dst.exists():
                    added.append((src, dst))
                else:
                    updated.append((src, dst))
            continue

        if not dst.exists():
            added.append((src, dst))
        else:
            try:
                same = filecmp.cmp(src, dst, shallow=False)
            except Exception:
                same = False
            if not same:
                updated.append((src, dst))

    # Determine deletes inside scope
    deletes: list[Path] = []
    for it in sync_items():
        prod_base = ROOT_PROD / it
        if not prod_base.exists():
            continue
        if prod_base.is_file():
            rel = prod_base.relative_to(ROOT_PROD)
            if rel not in dev_files and not should_skip(rel, include_runtime_config=include_runtime_config):
                deletes.append(prod_base)
            continue
        for p in prod_base.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix == ".pyc" or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(ROOT_PROD)
            if should_skip(rel, include_runtime_config=include_runtime_config):
                continue
            if rel not in dev_files:
                deletes.append(p)

    return (
        added,
        updated,
        sorted(deletes),
        sorted(set(skipped)),
        runtime_write_map,
        sorted(set(runtime_allowed)),
        sorted(set(runtime_protected)),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually copy files")
    ap.add_argument("--prune", action="store_true", help="Also delete files in prod that are not present in dev under the synced scope")
    # Backward/UX compat: default is dry-run; allow explicit flag for clarity.
    ap.add_argument("--dry-run", action="store_true", help="Dry-run only (default behavior). Kept for CLI compatibility")
    ap.add_argument(
        "--include-runtime-config",
        action="store_true",
        help="Include runtime config files in sync (default: skip runtime configs to avoid overwriting prod local settings)",
    )
    ap.add_argument(
        "--runtime-config-allowlist",
        type=Path,
        help="JSON allowlist path for runtime config updates; required with --include-runtime-config",
    )
    args = ap.parse_args()

    if args.apply and args.dry_run:
        raise SystemExit("[ARG_ERROR] --apply and --dry-run cannot be used together")

    runtime_allowlist: RuntimeAllowlist | None = None
    if args.include_runtime_config:
        if not args.runtime_config_allowlist:
            raise SystemExit("[ARG_ERROR] --include-runtime-config requires --runtime-config-allowlist <path>")
        runtime_allowlist = RuntimeAllowlist.from_file(args.runtime_config_allowlist)

    ref = dev_ref()
    added, updated, deletes, skipped, runtime_write_map, runtime_allowed, runtime_protected = plan_copy(
        include_runtime_config=args.include_runtime_config,
        runtime_allowlist=runtime_allowlist,
    )
    if not args.prune:
        deletes = []

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[deploy] dev={ROOT_DEV}@{ref} -> prod={ROOT_PROD} mode={mode}")
    print(f"[deploy] summary: add={len(added)} update={len(updated)} delete={len(deletes)}")
    if args.include_runtime_config:
        print("[deploy] runtime-config mode: INCLUDED (explicit flag)")
        print(f"[deploy] runtime-config allowlist: {args.runtime_config_allowlist}")
        for item in runtime_allowed[:200]:
            print(f"  UPD_ALLOWED {item}")
        if len(runtime_allowed) > 200:
            print(f"  ... ({len(runtime_allowed) - 200} more allowed updates)")
        for item in runtime_protected[:200]:
            print(f"  SKIP_PROTECTED {item}")
        if len(runtime_protected) > 200:
            print(f"  ... ({len(runtime_protected) - 200} more protected skips)")
    else:
        print("[deploy] runtime-config mode: SKIPPED (default)")
        for rel in skipped:
            print(f"  SKIP {rel}")

    for src, dst in added[:200]:
        print(f"  ADD  {src.relative_to(ROOT_DEV)} -> {dst.relative_to(ROOT_PROD)}")
    if len(added) > 200:
        print(f"  ... ({len(added)-200} more added)")

    for src, dst in updated[:200]:
        print(f"  UPD  {src.relative_to(ROOT_DEV)} -> {dst.relative_to(ROOT_PROD)}")
    if len(updated) > 200:
        print(f"  ... ({len(updated)-200} more updated)")

    for p in deletes[:200]:
        print(f"  DEL  {p.relative_to(ROOT_PROD)}")
    if len(deletes) > 200:
        print(f"  ... ({len(deletes)-200} more deleted)")

    if not args.apply:
        return

    # Apply deletes
    for p in deletes:
        try:
            p.unlink(missing_ok=True)
        except Exception as e:
            print(f"[WARN] failed delete {p}: {e}")

    # Apply copies
    for src, dst in [*added, *updated]:
        dst.parent.mkdir(parents=True, exist_ok=True)
        runtime_text = runtime_write_map.get(dst)
        if runtime_text is not None:
            dst.write_text(runtime_text, encoding="utf-8")
        else:
            shutil.copy2(src, dst)

    # Cleanup empty dirs under scope
    for it in ["scripts", "tests"]:
        base = ROOT_PROD / it
        if not base.exists():
            continue
        for d in sorted([p for p in base.rglob("*") if p.is_dir()], key=lambda x: len(x.as_posix()), reverse=True):
            try:
                d.rmdir()
            except Exception:
                pass

    print("[deploy] applied")


if __name__ == "__main__":
    main()
