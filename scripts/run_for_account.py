#!/usr/bin/env python3
"""Run options-monitor pipeline for a specific account, with isolated output/state.

Why:
- Multi-account support (lx/sy) requires separate:
  - portfolio context (cash/holdings)
  - option positions context (cash-secured used, locked shares)
  - scheduler state / previous summary snapshot
  - final notification text

Approach:
- We *do not* rewrite the core pipeline right now.
- Instead, we create a temporary per-account config.json and per-account output directory.
- Then we invoke scripts/run_pipeline.py pointing at that temp config.

This keeps changes minimal and avoids breaking existing single-account flows.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path, timeout_sec: int | None = None):
    print(f"[RUN] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec)


def main():
    ap = argparse.ArgumentParser(description="Run pipeline for a specific account (lx/sy)")
    ap.add_argument("--config", default="config.json", help="base config")
    ap.add_argument("--account", required=True, help="account value in holdings/option_positions table, e.g. lx/sy")
    ap.add_argument("--market", default=None, help="override market (default uses config.portfolio.market)")
    ap.add_argument("--out-root", default="output_accounts", help="where to store per-account outputs (must NOT be inside output/)" )
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    # Build a per-account config override
    cfg.setdefault('portfolio', {})
    if args.market is not None:
        cfg['portfolio']['market'] = args.market
    cfg['portfolio']['account'] = args.account

    # Prepare per-account output directory
    out_root = Path(args.out_root)
    if not out_root.is_absolute():
        out_root = (base / out_root).resolve()

    # Safety: out_root must not be inside output/ because we will swap output/ into a symlink.
    if str(out_root).startswith(str((base / 'output').resolve())):
        raise SystemExit(f"out_root must not be inside output/: {out_root}")
    out_dir = (out_root / args.account)
    out_dir.mkdir(parents=True, exist_ok=True)

    # We isolate output by swapping the output/ directory via symlink.
    # - Move current output -> output/_shared (first time)
    # - Link output -> out_dir
    # We'll isolate outputs by swapping the whole output/ directory to a per-account folder.
    # To preserve existing output/, we rename it to output_shared/ (sibling), not inside itself.
    shared = base / 'output_shared'
    output_path = base / 'output'

    # First-time migration: if output is a real dir and not a symlink, stash it.
    if output_path.exists() and not output_path.is_symlink():
        if not shared.exists():
            shutil.move(str(output_path), str(shared))
        else:
            # shared exists already; keep current output as-is
            pass

    # Ensure output symlink points to this account's out_dir
    if output_path.exists():
        if output_path.is_symlink():
            output_path.unlink()
        else:
            # If it's a real dir here, it means we chose not to migrate; don't risk deleting.
            raise SystemExit(f"Refusing to replace real directory: {output_path}. Move it away first (e.g. to output_shared/).")

    output_path.symlink_to(out_dir, target_is_directory=True)

    # Write temp config inside out_dir so outputs keep references local
    tmp_cfg = out_dir / 'config.account.json'
    tmp_cfg.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    # Run pipeline
    res = run([sys.executable, 'scripts/run_pipeline.py', '--config', str(tmp_cfg)], cwd=base)

    if not args.keep_temp:
        # keep tmp_cfg for traceability? default keep (it's inside per-account output). we can keep it.
        pass

    raise SystemExit(res.returncode)


if __name__ == '__main__':
    main()
