"""Regression: postprocess should only call notify when meaningful and gate allows."""

from __future__ import annotations

from pathlib import Path


def test_postprocess_notify_gate() -> None:
    from scripts.pipeline_postprocess import postprocess_scan_results

    notified: list[str] = []

    def _noop(*args, **kwargs):
        return None

    def _render(_report_dir: Path, _n: int) -> str:
        return 'ALERT'

    def _should(_runtime: dict, _text: str) -> bool:
        return True

    def _notify(_runtime: dict, text: str) -> None:
        notified.append(text)

    result = postprocess_scan_results(
        summary_rows=[{'symbol': '0700.HK', 'strategy': 'sell_put', 'candidate_count': 1}],
        report_dir=Path('.'),
        is_scheduled=True,
        top_n=3,
        symbols=['0700.HK'],
        runtime={},
        want_fn=lambda s: s in ('scan', 'alert', 'notify'),
        build_symbols_summary_fn=lambda rows: _noop(rows),
        build_symbols_digest_fn=lambda rows, n: _noop(rows, n),
        render_sell_put_alerts_fn=_render,
        render_sell_call_alerts_fn=_render,
        should_notify_symbols_fn=_should,
        notify_symbols_fn=_notify,
        log=lambda _: None,
    )

    assert result.meaningful is True
    assert notified == ['ALERT\n\nALERT']
