#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.close_advice.main import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
