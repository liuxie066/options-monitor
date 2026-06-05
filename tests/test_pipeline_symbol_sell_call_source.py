from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_process_symbol_passes_futu_portfolio_stock_to_sell_call_chain(monkeypatch) -> None:
    import src.application.pipeline_symbol as ps
    import src.application.symbol_monitoring as sm

    monkeypatch.setattr(ps, "ensure_required_data", lambda **_kwargs: None)
    monkeypatch.setattr(ps, "apply_multiplier_cache_to_required_data_csv", lambda **_kwargs: None)
    monkeypatch.setattr(ps, "run_sell_put_scan_and_summarize", lambda **_kwargs: {"symbol": "NVDA", "strategy": "sell_put"})
    monkeypatch.setattr(sm, "build_required_data_fetch_plan", lambda **_kwargs: None)

    captured: dict[str, object] = {}

    def _fake_run_sell_call_scan_and_summarize(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {"symbol": "NVDA", "strategy": "sell_call", "candidate_count": 1}

    monkeypatch.setattr(ps, "run_sell_call_scan_and_summarize", _fake_run_sell_call_scan_and_summarize)

    out = ps.process_symbol(
        py='python',
        base=BASE,
        symbol_cfg={
            'symbol': 'NVDA',
            'fetch': {'source': 'futu', 'host': '127.0.0.1', 'port': 11111},
            'sell_put': {'enabled': False},
            'sell_call': {'enabled': True, 'min_dte': 7, 'max_dte': 45, 'min_strike': 100},
        },
        top_n=3,
        portfolio_ctx={
            'portfolio_source_name': 'futu',
            'stocks_by_symbol': {
                'NVDA': {
                    'symbol': 'NVDA',
                    'shares': 300,
                    'avg_cost': 95.5,
                    'currency': 'USD',
                }
            },
            'option_ctx': {'locked_shares_by_symbol': {'NVDA': 100}},
        },
        usd_per_cny_exchange_rate=0.14,
        cny_per_hkd_exchange_rate=0.92,
        timeout_sec=10,
        required_data_dir=BASE / 'output',
        report_dir=BASE / 'output' / 'reports',
        state_dir=BASE / 'output' / 'state',
        is_scheduled=True,
    )

    assert out[-1]["strategy"] == "sell_call"
    assert captured["stock"] == {
        'symbol': 'NVDA',
        'shares': 300,
        'avg_cost': 95.5,
        'currency': 'USD',
    }
    assert captured["locked_shares_by_symbol"] == {'NVDA': 100}
