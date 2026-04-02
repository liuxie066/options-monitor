"""Regression: atomic_write_json should not leave partial JSON on replace."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory


def test_atomic_write_json_writes_valid_json() -> None:
    from scripts.io_utils import atomic_write_json

    with TemporaryDirectory() as td:
        p = Path(td) / 'a.json'
        atomic_write_json(p, {'as_of_utc': 'x', 'filters': {}})
        obj = json.loads(p.read_text(encoding='utf-8'))
        assert obj['as_of_utc'] == 'x'
