#!/usr/bin/env python3
"""Thin CLI wrapper for multi-account per-account tick notifications."""

from __future__ import annotations

# Ensure repo root is on sys.path for `scripts.*` imports when run as a script
import sys
from pathlib import Path as _PathLib

_repo_root = _PathLib(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from pathlib import Path

try:
    from scripts.run_log import RunLogger
except Exception:
    from run_log import RunLogger

from src.application.multi_account_tick import current_run_id as _current_run_id
from src.application.multi_account_tick import run_tick as _multi_main

# Compatibility/developer entrypoint:
#   scripts/send_if_needed_multi.py -> src.application.multi_account_tick.run_tick
# Preferred human-facing path is the unified CLI:
#   ./om run tick --config ... --accounts ...
# Production scheduler entrypoint remains scripts/send_if_needed.py (unchanged).


if __name__ == '__main__':
    try:
        raise SystemExit(_multi_main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        try:
            base = Path(__file__).resolve().parents[1]
            RunLogger(base, run_id=_current_run_id()).event(
                'run_error',
                'error',
                error_code=(getattr(e, 'error_code', None) or type(e).__name__),
                message=str(e),
            )
        except Exception:
            pass
        raise
