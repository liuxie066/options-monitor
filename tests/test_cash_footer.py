from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _write_snapshot(state_dir: Path, payload: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    snapshot = dict(payload)
    snapshot.setdefault('as_of_utc', (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    (state_dir / 'cash_snapshot.json').write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def test_query_cash_footer_uses_base_cny_labels_when_present(tmp_path: Path) -> None:
    from scripts.multi_tick.cash_footer import query_cash_footer

    state_dir = tmp_path / 'output_accounts' / 'lx' / 'state'
    _write_snapshot(
        state_dir,
        {
            'cash_available_cny': 1000.0,
            'cash_free_cny': 200.0,
            'cash_available_total_cny': 5000.0,
            'cash_free_total_cny': 3000.0,
        },
    )

    out = query_cash_footer(tmp_path, market='富途', accounts=['lx'])

    assert out[0] == '**💰 现金 CNY**'
    assert out[1] == '- **LX** CNY 持有 ¥1,000 (CNY) | CNY 可用 ¥200 (CNY)'


def test_query_cash_footer_uses_total_labels_when_only_total_is_present(tmp_path: Path) -> None:
    from scripts.multi_tick.cash_footer import query_cash_footer

    state_dir = tmp_path / 'output_accounts' / 'lx' / 'state'
    _write_snapshot(
        state_dir,
        {
            'cash_available_cny': None,
            'cash_free_cny': None,
            'cash_available_total_cny': 5000.0,
            'cash_free_total_cny': 3000.0,
        },
    )

    out = query_cash_footer(tmp_path, market='富途', accounts=['lx'])

    assert out[1] == '- **LX** 总现金折算 ¥5,000 (CNY) | 总可用折算 ¥3,000 (CNY)'


def test_query_cash_footer_keeps_dash_when_all_cash_fields_missing(tmp_path: Path) -> None:
    from scripts.multi_tick.cash_footer import query_cash_footer

    state_dir = tmp_path / 'output_accounts' / 'lx' / 'state'
    _write_snapshot(
        state_dir,
        {
            'cash_available_cny': None,
            'cash_free_cny': None,
            'cash_available_total_cny': None,
            'cash_free_total_cny': None,
        },
    )

    out = query_cash_footer(tmp_path, market='富途', accounts=['lx'])

    assert out[1] == '- **LX** CNY 持有 - | CNY 可用 -'
