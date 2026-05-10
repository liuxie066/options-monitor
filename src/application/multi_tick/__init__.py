"""Helpers for ``src.application.multi_account_tick``.

This package only hosts shared utilities (cash footer, notify format, opend
guard, project guard, required-data prefetch, misc). It is intentionally NOT
an orchestrator and must not be confused with the sibling module
``src/application/multi_account_tick.py``, which owns the unified tick flow.

Submodules are imported lazily by consumers (``from .cash_footer import ...``)
to keep package-level import lightweight for test/runtime utilities that only
need a single helper.
"""

from __future__ import annotations
