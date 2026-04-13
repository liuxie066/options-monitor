#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from domain.storage.repositories import state_repo
except Exception:
    from scripts.domain.storage.repositories import state_repo  # type: ignore


def main():
    ap = argparse.ArgumentParser(description='Write last_run.json')
    ap.add_argument('--state-dir', default='output/state', help='Directory for last_run.json (default: output/state)')
    ap.add_argument('--path', default=None, help='[deprecated] explicit last_run.json path. Prefer --state-dir.' )
    ap.add_argument('--status', required=True, choices=['ok','error','skip'])
    ap.add_argument('--stage', default='')
    ap.add_argument('--reason', default='')
    ap.add_argument('--details', default='')
    ap.add_argument('--started-at', default='')
    ap.add_argument('--finished-at', default='')
    ap.add_argument('--duration-ms', type=int, default=-1)
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]

    if args.path:
        out = Path(args.path)
        if not out.is_absolute():
            out = (base / out).resolve()
        target_state_dir: Path | None = None
    else:
        state_dir = Path(args.state_dir)
        if not state_dir.is_absolute():
            state_dir = (base / state_dir).resolve()
        target_state_dir = state_dir
        out = (state_dir / 'last_run.json').resolve()

    out.parent.mkdir(parents=True, exist_ok=True)

    def now():
        return datetime.now(timezone.utc).isoformat()

    finished_at = args.finished_at or now()

    # Compute duration if not provided
    duration_ms = None if args.duration_ms < 0 else args.duration_ms
    if duration_ms is None and args.started_at and finished_at:
        try:
            s = datetime.fromisoformat(args.started_at)
            f = datetime.fromisoformat(finished_at)
            duration_ms = int((f - s).total_seconds() * 1000)
        except Exception:
            duration_ms = None

    payload = {
        'status': args.status,
        'stage': args.stage or None,
        'reason': args.reason or None,
        'details': args.details or None,
        'started_at': args.started_at or None,
        'finished_at': finished_at,
        'duration_ms': duration_ms,
    }

    if target_state_dir is not None:
        state_repo.write_state_json(target_state_dir, 'last_run.json', payload)
    else:
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"[DONE] last_run -> {out}")


if __name__ == '__main__':
    main()
