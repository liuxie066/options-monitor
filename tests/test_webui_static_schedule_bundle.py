"""Regression: committed WebUI bundle should match current schedule fields."""

from __future__ import annotations

from pathlib import Path


REMOVED_SCHEDULE_FIELDS = (
    'market_open',
    'market_close',
    'market_timezone',
    'market_break_start',
    'market_break_end',
    'first_notify_after_open_min',
    'notify_interval_min',
    'final_notify_before_close_min',
    'schedule_v2',
)

CURRENT_SCHEDULE_FIELDS = (
    'cron_interval_min',
    'run_window',
    'run_points',
)


def test_webui_static_schedule_bundle_uses_current_schedule_fields() -> None:
    root = Path(__file__).resolve().parents[1]
    static_dir = root / 'scripts' / 'webui' / 'static'
    bundle_files = sorted(
        path
        for path in static_dir.rglob('*')
        if path.suffix in {'.html', '.js'}
    )
    assert bundle_files, 'expected committed WebUI static bundle files'

    bundle_text = '\n'.join(path.read_text(encoding='utf-8') for path in bundle_files)
    for field in REMOVED_SCHEDULE_FIELDS:
        assert field not in bundle_text
    for field in CURRENT_SCHEDULE_FIELDS:
        assert field in bundle_text
