from __future__ import annotations

import sys
from pathlib import Path


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.application import multi_account_tick as _impl
from src.application.multi_account_tick import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(_impl.main())

sys.modules[__name__] = _impl
