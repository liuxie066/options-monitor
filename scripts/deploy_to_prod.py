#!/usr/bin/env python3
"""Deploy from dev repo (options-monitor) to prod repo (options-monitor-prod).

Policy:
- Copy only selected paths (scripts/tests/docs/requirements/.gitignore)
- Do NOT touch runtime configs/output/secrets/.venv/cache
- Default: dry-run; use --apply to write

This is a minimal replacement for rsync (not always available in the image).
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
from pathlib import Path

ROOT_DEV = Path("/home/node/.openclaw/workspace/options-monitor")
ROOT_PROD = Path("/home/node/.openclaw/workspace/options-monitor-prod")

ITEMS = [
    ".gitignore",
    "requirements.txt",
    "README.md",
    "SKILL.md",
    "scripts",
    "tests",
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
EXCLUDE_FILES = {
    "config.json",
}


def dev_ref() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT_DEV)).decode().strip()
        return out
    except Exception:
        return "unknown"


def should_skip(path_rel: Path) -> bool:
    parts = path_rel.parts
    if parts and parts[0] in EXCLUDE_TOP:
        return True
    if path_rel.name in EXCLUDE_FILES:
        return True
    if path_rel.name.startswith("config.local") and path_rel.name.endswith(".json"):
        return True
    return False


def iter_files(src_root: Path) -> list[Path]:
    if src_root.is_file():
        return [src_root]
    files: list[Path] = []
    for p in src_root.rglob("*"):
        if not p.is_file():
            continue
        # skip bytecode
        if p.suffix == '.pyc' or '__pycache__' in p.parts:
            continue
        rel = p.relative_to(ROOT_DEV)
        if should_skip(rel):
            continue
        files.append(p)
    return files


def plan_copy() -> tuple[list[tuple[Path, Path]], list[Path]]:
    """Return (copies, deletes).

    deletes are paths in prod that should be deleted because they exist under ITEMS scope
    but no longer exist in dev.
    """
    copies: list[tuple[Path, Path]] = []

    # Build dev file set
    dev_files: set[Path] = set()
    for it in ITEMS:
        src = ROOT_DEV / it
        for f in iter_files(src):
            dev_files.add(f.relative_to(ROOT_DEV))

    # Determine copies (new/changed)
    for rel in sorted(dev_files):
        src = ROOT_DEV / rel
        dst = ROOT_PROD / rel
        if not dst.exists():
            copies.append((src, dst))
        else:
            # compare content
            try:
                same = filecmp.cmp(src, dst, shallow=False)
            except Exception:
                same = False
            if not same:
                copies.append((src, dst))

    # Determine deletes inside scope
    deletes: list[Path] = []
    for it in ITEMS:
        prod_base = ROOT_PROD / it
        if not prod_base.exists():
            continue
        if prod_base.is_file():
            rel = prod_base.relative_to(ROOT_PROD)
            if rel not in dev_files and not should_skip(rel):
                deletes.append(prod_base)
            continue
        for p in prod_base.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix == '.pyc' or '__pycache__' in p.parts:
                continue
            rel = p.relative_to(ROOT_PROD)
            if should_skip(rel):
                continue
            if rel not in dev_files:
                deletes.append(p)

    return copies, sorted(deletes)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually copy files")
    ap.add_argument("--prune", action="store_true", help="Also delete files in prod that are not present in dev under the synced scope")
    # Backward/UX compat: default is dry-run; allow explicit flag for clarity.
    ap.add_argument("--dry-run", action="store_true", help="Dry-run only (default behavior). Kept for CLI compatibility")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        raise SystemExit("[ARG_ERROR] --apply and --dry-run cannot be used together")

    ref = dev_ref()
    copies, deletes = plan_copy()
    if not args.prune:
        deletes = []

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[deploy] dev={ROOT_DEV}@{ref} -> prod={ROOT_PROD} mode={mode}")
    print(f"[deploy] plan: {len(copies)} copy/update, {len(deletes)} delete")

    for src, dst in copies[:200]:
        print(f"  COPY {src.relative_to(ROOT_DEV)} -> {dst.relative_to(ROOT_PROD)}")
    if len(copies) > 200:
        print(f"  ... ({len(copies)-200} more copies)")

    for p in deletes[:200]:
        print(f"  DEL  {p.relative_to(ROOT_PROD)}")
    if len(deletes) > 200:
        print(f"  ... ({len(deletes)-200} more deletes)")

    if not args.apply:
        return

    # Apply deletes
    for p in deletes:
        try:
            p.unlink(missing_ok=True)
        except Exception as e:
            print(f"[WARN] failed delete {p}: {e}")

    # Apply copies
    for src, dst in copies:
        dst.parent.mkdir(parents=True, exist_ok=True)
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
