#!/usr/bin/env python3
"""Thin CLI wrapper for multi-account merged tick notifier."""

from __future__ import annotations

# Ensure repo root is on sys.path for `scripts.*` imports when run as a script
import importlib
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

from scripts.multi_tick import main as _multi_main
_multi_main_mod = importlib.import_module('scripts.multi_tick.main')
from scripts.multi_tick.opend_guard import should_send_opend_alert as _should_send_opend_alert
from om.domain import select_markets_to_run as _domain_select_markets_to_run

# Legacy compatibility exports for tests/callers still importing old private names
_select_markets_to_run = _domain_select_markets_to_run


if __name__ == '__main__':
    try:
        raise SystemExit(_multi_main())
    except SystemExit:
        raise
    except Exception as e:
        try:
            base = Path(__file__).resolve().parents[1]
            RunLogger(base, run_id=getattr(_multi_main_mod, '_CURRENT_RUN_ID', None)).event(
                'run_error',
                'error',
                error_code=(getattr(e, 'error_code', None) or type(e).__name__),
                message=str(e),
            )
        except Exception:
            pass
        raise
