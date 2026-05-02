#!/usr/bin/env python3
"""Auto-close expired position lots.

Design goals:
- Do NOT add extra list/scan calls: consume the already-generated option_positions_context.json
  (produced by fetch_option_positions_context.py), which includes open_positions_min with record_id.
- Close rule: status=open AND (as_of >= expiration + grace_days).
- expiration is taken from lot field `expiration` (ms) when available, else from note `exp=YYYY-MM-DD`.
- Safety:
  - Default apply (per user requirement) but can run --dry-run.
  - max-close-per-run guardrail.
  - Skip and report positions missing expiration.

This appends synthetic close events and rebuilds position lots; it does not trade with the broker.
"""

from __future__ import annotations

# Allow running as a script without installation.
# When executed as `python scripts/auto_close_expired_positions.py`, ensure repo root is on sys.path
# so `import scripts.*` works consistently.
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

import json
import argparse
from datetime import datetime, timezone

from scripts.option_positions_core.service import (
    auto_close_expired_positions,
    build_expired_close_decisions,
)
from src.application.option_positions_facade import resolve_option_positions_repo


def main():
    ap = argparse.ArgumentParser(description='Auto-close expired position lots')
    ap.add_argument('--data-config', default=None, help='portfolio data config path; auto-resolves when omitted')
    ap.add_argument('--context', default=None, help='Position lot context JSON (default: <state-dir>/option_positions_context.json)')
    ap.add_argument('--state-dir', default='output/state', help='State dir for default context path (default: output/state)')
    ap.add_argument('--as-of-utc', default=None, help='ISO time; default now UTC')
    ap.add_argument('--grace-days', type=int, default=1)
    ap.add_argument('--max-close', type=int, default=20)
    ap.add_argument('--dry-run', action='store_true', help='do not write updates')
    ap.add_argument('--summary-out', default='output/reports/auto_close_summary.txt')
    ap.add_argument('--quiet', action='store_true', help='suppress stdout (scheduled/cron)')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]

    if args.context:
        ctx_path = Path(args.context)
        if not ctx_path.is_absolute():
            ctx_path = (base / ctx_path).resolve()
    else:
        sd = Path(args.state_dir)
        if not sd.is_absolute():
            sd = (base / sd).resolve()
        ctx_path = (sd / 'option_positions_context.json').resolve()
    ctx = json.loads(ctx_path.read_text(encoding='utf-8')) if ctx_path.exists() else {}

    positions = ctx.get('open_positions_min') or []

    # as_of
    if args.as_of_utc:
        as_of = datetime.fromisoformat(args.as_of_utc)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        else:
            as_of = as_of.astimezone(timezone.utc)
    else:
        as_of = datetime.now(timezone.utc)

    as_of_ms = int(as_of.timestamp() * 1000)
    decisions = build_expired_close_decisions(
        [p for p in positions if isinstance(p, dict)],
        as_of_ms=as_of_ms,
        grace_days=int(args.grace_days),
    )
    to_close = [d for d in decisions if bool(d.get('should_close')) and d.get('record_id')]
    applied: list[dict] = []
    errors: list[str] = []

    if to_close and not args.dry_run:
        _data_config, repo = resolve_option_positions_repo(base=base, data_config=args.data_config)
        decisions, applied, errors = auto_close_expired_positions(
            repo,
            [p for p in positions if isinstance(p, dict)],
            as_of_ms=as_of_ms,
            grace_days=int(args.grace_days),
            max_close=int(args.max_close),
        )
        to_close = [d for d in decisions if bool(d.get('should_close')) and d.get('record_id')]
    elif len(to_close) > int(args.max_close):
        errors.append(f"too many to close: {len(to_close)} > max_close={args.max_close}; abort")

    # Summary
    lines: list[str] = []
    lines.append(f"Auto-close expired positions (grace_days={args.grace_days})")
    lines.append(f"as_of_utc: {as_of.isoformat()}")
    lines.append(f"context: {ctx_path}")
    lines.append("")

    if errors:
        lines.append(f"ERRORS: {len(errors)}")
        for e in errors[:20]:
            lines.append(f"- {e}")
        lines.append("")

    if args.dry_run:
        lines.append(f"MODE: DRY_RUN")
    else:
        lines.append(f"MODE: APPLY")

    lines.append(f"candidates_should_close: {len(to_close)}")
    lines.append(f"applied_closed: {len(applied)}")
    skipped_already_closed = [
        d for d in decisions if d.get("skip_reason") == "already_closed_or_zero_open"
    ]
    if skipped_already_closed:
        lines.append(f"skipped_already_closed: {len(skipped_already_closed)}")
    lines.append(f"skipped_or_not_due: {len(decisions) - len(to_close)}")
    lines.append("")

    if to_close:
        lines.append("Closed / To close list:")
        for d in (applied if (applied and not args.dry_run) else to_close)[:50]:
            lines.append(
                f"- {d.get('record_id')} | {d.get('position_id')} | "
                f"exp_src={d.get('effective_exp_source')} | exp_ms={d.get('expiration_ms')}"
            )
        lines.append("")

    # skipped highlights
    missing_exp = [d for d in decisions if 'missing expiration' in str(d.get('reason') or '')]
    missing_rid = [d for d in decisions if str(d.get('reason') or '') == 'missing record_id']
    if missing_exp or missing_rid:
        lines.append("Skipped:")
        if missing_rid:
            lines.append(f"- missing record_id: {len(missing_rid)}")
        if missing_exp:
            lines.append(f"- missing expiration (field and note): {len(missing_exp)}")

    summary_out = Path(args.summary_out)
    if not summary_out.is_absolute():
        summary_out = (base / summary_out).resolve()
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text("\n".join(lines).strip() + "\n", encoding='utf-8')

    if not args.quiet:
        print(f"[DONE] auto_close summary -> {summary_out}")
        print(f"should_close={len(to_close)} applied={len(applied)} errors={len(errors)} dry_run={bool(args.dry_run)}")


if __name__ == '__main__':
    main()
