from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_import_fetch_source_does_not_require_pandas() -> None:
    base = Path(__file__).resolve().parents[1]
    code = """
import sys
from pathlib import Path
base = Path(sys.argv[1]).resolve()
if str(base) not in sys.path:
    sys.path.insert(0, str(base))
assert 'pandas' not in sys.modules
from domain.domain.fetch_source import normalize_fetch_source
assert normalize_fetch_source('futu') == 'opend'
assert 'pandas' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(base)],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
