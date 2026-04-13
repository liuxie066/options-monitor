#!/usr/bin/env python3
"""Thin CLI wrapper for multi-account merged tick notifier."""

from __future__ import annotations

# Ensure repo root is on sys.path for `scripts.*` imports when run as a script
import sys
import warnings
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
from scripts.multi_tick.main import current_run_id as _current_run_id
from scripts.multi_tick.opend_guard import should_send_opend_alert as _domain_should_send_opend_alert
from domain.domain import select_markets_to_run as _domain_select_markets_to_run

# Dev mainline entrypoint:
#   scripts/send_if_needed_multi.py -> scripts.multi_tick.main.main
# Production scheduler entrypoint remains scripts/send_if_needed.py (unchanged).


def _warn_deprecated_private_export(name: str, replacement: str) -> None:
    warnings.warn(
        (
            f"{name} is deprecated compatibility export from scripts.send_if_needed_multi; "
            f"use {replacement} instead."
        ),
        DeprecationWarning,
        stacklevel=2,
    )


def _select_markets_to_run(now_utc, cfg, market_config):
    _warn_deprecated_private_export(
        "scripts.send_if_needed_multi._select_markets_to_run",
        "domain.domain.select_markets_to_run",
    )
    return _domain_select_markets_to_run(now_utc, cfg, market_config)


def _should_send_opend_alert(base, error_code, cooldown_sec=600):
    _warn_deprecated_private_export(
        "scripts.send_if_needed_multi._should_send_opend_alert",
        "scripts.multi_tick.opend_guard.should_send_opend_alert",
    )
    return _domain_should_send_opend_alert(base, error_code, cooldown_sec=cooldown_sec)


if __name__ == '__main__':
    try:
        raise SystemExit(_multi_main())
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
