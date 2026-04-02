"""Regression: prevent stale labeled CSV reuse when upstream candidates are empty."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def test_add_sell_put_labels_overwrites_on_empty() -> None:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from scripts.report_labels import add_sell_put_labels

    with TemporaryDirectory() as td:
        root = Path(td)
        input_path = root / 'input.csv'
        output_path = root / 'out.csv'

        # Seed output with stale content.
        output_path.write_text('symbol,strike\n9992.HK,127.5\n', encoding='utf-8')
        # Empty input (simulate 0 candidates).
        input_path.write_text('symbol,strike\n', encoding='utf-8')

        add_sell_put_labels(root, input_path, output_path)

        out = output_path.read_text(encoding='utf-8').strip().splitlines()
        assert out == ['symbol,strike']
