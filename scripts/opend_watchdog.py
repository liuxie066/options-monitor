#!/usr/bin/env python3
from __future__ import annotations

"""Operational CLI wrapper for OpenD watchdog checks."""

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _reexec_venv_if_needed() -> None:
    vpy = REPO_ROOT / ".venv" / "bin" / "python"
    if vpy.exists() and str(vpy) != sys.executable:
        os.execv(str(vpy), [str(vpy)] + sys.argv)


if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.infrastructure.opend_watchdog import (  # noqa: E402
    Health,
    _port_retry_loop,
    classify_watchdog_result,
    get_global_state,
    get_global_state_once,
    main,
    port_open,
    run_watchdog_check,
    try_start_opend,
)


if __name__ == "__main__":
    _reexec_venv_if_needed()
    raise SystemExit(main())
