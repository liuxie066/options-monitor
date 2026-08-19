"""Regression: watchlist runner should honor --symbols whitelist."""

from __future__ import annotations

import threading
import time
from pathlib import Path


def test_watchlist_whitelist_filters_symbols() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    calls: list[str] = []

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        calls.append(str(item.get('symbol')))
        return [{'symbol': str(item.get('symbol')), 'strategy': 'sell_put', 'candidate_count': 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        'symbols': [
            {'symbol': '0700.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
            {'symbol': '3690.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
        ],
        'templates': {},
        'runtime': {},
    }

    out = run_watchlist_pipeline(
        py='python',
        base=Path('.'),
        cfg=cfg,
        report_dir=Path('.'),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg='0700.HK',
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    assert calls == ['0700.HK']
    assert len(out) == 1


def test_watchlist_reuses_one_required_data_batch_for_all_symbols() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    batch = object()
    received: list[object] = []

    def _process_symbol(*args, **kwargs):
        received.append(kwargs["required_data_snapshot_batch"])
        return [
            {
                "symbol": str(args[2]["symbol"]),
                "strategy": "sell_put",
                "candidate_count": 0,
            }
        ]

    cfg = {
        "symbols": [
            {"symbol": "NVDA", "sell_put": {"enabled": True}},
            {"symbol": "PDD", "sell_put": {"enabled": True}},
        ],
        "templates": {},
        "runtime": {},
    }
    run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg=cfg,
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=lambda item, _profiles: dict(item),
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=lambda **_: ({}, None, None, None),
        build_symbols_summary_fn=lambda *_: None,
        build_symbols_digest_fn=lambda *_: None,
        required_data_snapshot_manifest=Path("manifest.json"),
        required_data_snapshot_batch=batch,
    )

    assert received == [batch, batch]


def test_watchlist_combo_sink_receives_typed_evidence(tmp_path: Path) -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline
    from src.application.strategy_scan_status import publish_strategy_scan_status

    received: list[dict] = []

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        report_dir = tmp_path / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        publish_strategy_scan_status(
            report_dir=report_dir,
            run_id="run-1",
            account="lx",
            market="US",
            symbol="NVDA",
            strategy_family="sell_put",
            status="completed",
            candidate_count=0,
            snapshot_id="quote-1",
            receipt_relpath="quotes/quote-1/receipt.json",
        )
        sink = kwargs.get("combo_evidence_sink_fn")
        if sink is not None:
            sink(
                {
                    "schema_version": "combo_yield_scan_evidence.v1",
                    "variant": "sp_lc",
                    "symbol": "NVDA",
                    "ranked_pairs": [],
                }
            )
        return [{'symbol': 'NVDA', 'strategy': 'combo_yield', 'candidate_count': 1}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    def _combo_sink(payload: dict) -> None:
        received.append(dict(payload))

    cfg = {
        'symbols': [
            {'symbol': 'NVDA', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': False}},
        ],
        'templates': {},
        'runtime': {},
        'portfolio': {'account': 'lx'},
    }

    run_watchlist_pipeline(
        py='python',
        base=tmp_path,
        cfg=cfg,
        report_dir=tmp_path / 'reports',
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg='NVDA',
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
        source_producer_run_id='run-1',
        candidate_capture_status_sink_fn=_noop,
        required_data_snapshot_manifest=tmp_path / 'required.json',
        account_config_sha256='a' * 64,
        combo_evidence_sink_fn=_combo_sink,
    )

    assert received == [
        {
            "schema_version": "combo_yield_scan_evidence.v1",
            "variant": "sp_lc",
            "symbol": "NVDA",
            "ranked_pairs": [],
        }
    ]


def test_watchlist_symbol_timeout_covers_the_whole_processor() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    calls: list[str] = []

    def _process_symbol(*args, **kwargs):
        symbol = str(args[2]["symbol"])
        calls.append(symbol)
        if symbol == "AAPL":
            time.sleep(5)
        return [
            {
                "symbol": symbol,
                "strategy": "sell_put",
                "candidate_count": 0,
            }
        ]

    started = time.monotonic()
    out = run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg={
            "symbols": [
                {"symbol": "AAPL", "sell_put": {"enabled": True}},
                {"symbol": "MSFT", "sell_put": {"enabled": True}},
            ],
            "templates": {},
            "runtime": {"pipeline_symbol_max_workers": 4},
        },
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=lambda item, _profiles: dict(item),
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=lambda **_kwargs: ({}, None, None, None),
        build_symbols_summary_fn=lambda *_args, **_kwargs: None,
        build_symbols_digest_fn=lambda *_args, **_kwargs: None,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.5
    assert calls == ["AAPL", "MSFT"]
    aapl = [row for row in out if row["symbol"] == "AAPL"]
    assert {row["strategy"] for row in aapl} == {"sell_put", "sell_call"}
    assert all("deadline" in row["note"] for row in aapl)
    assert any(row["symbol"] == "MSFT" for row in out)


def test_watchlist_whitelist_is_case_insensitive_and_trimmed() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    calls: list[str] = []

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        calls.append(str(item.get('symbol')))
        return [{'symbol': str(item.get('symbol')), 'strategy': 'sell_put', 'candidate_count': 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        'symbols': [
            {'symbol': '0700.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
            {'symbol': '3690.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
        ],
        'templates': {},
        'runtime': {},
    }

    out = run_watchlist_pipeline(
        py='python',
        base=Path('.'),
        cfg=cfg,
        report_dir=Path('.'),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=' 0700.hk ',
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    assert calls == ['0700.HK']
    assert len(out) == 1


def test_watchlist_global_liquidity_excludes_underwriting_income_threshold() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    seen: dict[str, dict] = {}

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        seen['put'] = dict(item.get('_global_sell_put_liquidity') or {})
        seen['call'] = dict(item.get('_global_sell_call_liquidity') or {})
        return [{'symbol': str(item.get('symbol')), 'strategy': 'sell_put', 'candidate_count': 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        'symbols': [
            {'symbol': '0700.HK', 'use': 'base_profile', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
        ],
        'templates': {
            'base_profile': {
                'sell_put': {'min_net_income': 100, 'min_open_interest': 50},
                'sell_call': {'min_net_income': 200, 'min_volume': 12},
            }
        },
        'runtime': {},
    }

    run_watchlist_pipeline(
        py='python',
        base=Path('.'),
        cfg=cfg,
        report_dir=Path('.'),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    assert seen['put'] == {'min_open_interest': 50}
    assert seen['call'] == {'min_volume': 12}


def test_watchlist_passes_runtime_config_to_symbol_processor() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    seen: list[dict] = []

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        seen.append(dict(kwargs.get("runtime_config") or {}))
        item = args[2]
        return [{"symbol": str(item.get("symbol")), "strategy": "sell_put", "candidate_count": 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        "symbols": [
            {"symbol": "0700.HK", "sell_put": {"enabled": True}, "sell_call": {"enabled": False}},
        ],
        "templates": {},
        "runtime": {"option_chain_fetch": {"max_calls": 7}},
    }

    run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg=cfg,
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    assert seen == [cfg]


def test_watchlist_forwards_opening_candidate_decision_sink() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    captured: list[dict] = []
    observed_option_contexts: list[dict] = []
    decision = {"opening_decision": {"accepted": False}}

    def _process_symbol(*args, **kwargs):
        observed_option_contexts.append(dict(kwargs["portfolio_ctx"]["option_ctx"]))
        kwargs["candidate_decisions_sink_fn"]("put", [decision])
        return [
            {
                "symbol": str(args[2]["symbol"]),
                "strategy": "sell_put",
                "candidate_count": 0,
            }
        ]

    run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg={
            "symbols": [
                {
                    "symbol": "NVDA",
                    "sell_put": {"enabled": True},
                    "sell_call": {"enabled": False},
                }
            ],
            "templates": {},
            "runtime": {},
        },
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=lambda item, _profiles: dict(item),
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=lambda **_kwargs: ({}, None, None, None),
        build_symbols_summary_fn=lambda *_args, **_kwargs: None,
        build_symbols_digest_fn=lambda *_args, **_kwargs: None,
        source_producer_run_id="run-1",
        candidate_capture_status_sink_fn=lambda _row: None,
        opening_candidate_decisions_sink_fn=(
            lambda _mode, rows: captured.extend(rows)
        ),
    )

    assert captured == [decision]
    assert observed_option_contexts == [
        {
            "context_status": "unavailable",
            "locked_shares_status": "unavailable",
            "locked_shares_unavailable_reason": (
                "option_positions_context_unavailable"
            ),
            "locked_shares_by_symbol": {},
            "locked_shares_unavailable_by_symbol": {},
            "cash_secured_by_symbol_by_ccy": {},
            "cash_secured_total_by_ccy": {},
            "cash_secured_unavailable_by_symbol": {},
        }
    ]


def test_watchlist_fetch_stage_preserves_strategy_config_but_skips_scan_output() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    seen: list[tuple[bool, bool, bool]] = []
    summary_called: list[bool] = []

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        seen.append(
            (
                bool((item.get("sell_put") or {}).get("enabled")),
                bool((item.get("sell_call") or {}).get("enabled")),
                bool(kwargs.get("fetch_only")),
            )
        )
        return [{"symbol": str(item.get("symbol")), "strategy": "sell_put", "candidate_count": 1}]

    def _build_ctx(**kwargs):
        assert kwargs["want_scan"] is False
        return ({}, None, None, None)

    cfg = {
        "symbols": [
            {"symbol": "NVDA", "sell_put": {"enabled": True}, "sell_call": {"enabled": True}},
        ],
        "templates": {},
        "runtime": {},
    }

    out = run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg=cfg,
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=False,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda name: name == "fetch",
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=lambda rows: summary_called.append(True),
        build_symbols_digest_fn=lambda rows, n: summary_called.append(True),
    )

    assert out == []
    assert seen == [(True, True, True)]
    assert summary_called == []


def test_watchlist_pipeline_processes_symbols_in_parallel_when_configured() -> None:
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    started: list[str] = []
    lock = threading.Lock()
    both_started = threading.Event()
    warnings: list[str] = []

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        symbol = str(item.get("symbol"))
        with lock:
            started.append(symbol)
            if len(started) == 2:
                both_started.set()
        assert both_started.wait(1.0), "symbol processing did not overlap"
        return [{"symbol": symbol, "strategy": "sell_put", "candidate_count": 1}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        "symbols": [
            {"symbol": "0700.HK", "sell_put": {"enabled": True}, "sell_call": {"enabled": False}},
            {"symbol": "3690.HK", "sell_put": {"enabled": True}, "sell_call": {"enabled": False}},
        ],
        "templates": {},
        "runtime": {"pipeline_symbol_max_workers": 2},
    }

    out = run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg=cfg,
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=0,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda msg: warnings.append(msg),
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    assert warnings == []
    assert [row["symbol"] for row in out] == ["0700.HK", "3690.HK"]
    assert sorted(started) == ["0700.HK", "3690.HK"]


def test_resolve_watchlist_item_runtime_config_centralizes_template_expansion() -> None:
    from src.application.pipeline_watchlist import resolve_watchlist_item_runtime_config

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        out = dict(item)
        for name in ([item.get('use')] if isinstance(item.get('use'), str) else item.get('use') or []):
            prof = profiles.get(name) or {}
            for key, value in prof.items():
                if isinstance(value, dict) and isinstance(out.get(key), dict):
                    merged = dict(value)
                    merged.update(out.get(key) or {})
                    out[key] = merged
                else:
                    out.setdefault(key, value)
        return out

    profiles = {
        'put_base': {
            'sell_put': {
                'min_annualized_net_return': 0.12,
                'min_net_income': 100,
                'min_open_interest': 50,
            }
        },
        'call_base': {
            'sell_call': {
                'min_annualized_net_return': 0.11,
                'min_volume': 12,
                'min_strike_cost_multiplier': 1.02,
            }
        },
    }
    item = {
        'symbol': '0700.HK',
        'use': ['put_base', 'call_base'],
        'sell_put': {'enabled': True, 'min_dte': 20},
        'sell_call': {'enabled': True},
    }

    resolved = resolve_watchlist_item_runtime_config(
        item=item,
        profiles=profiles,
        apply_profiles_fn=_apply_profiles,
    )

    assert resolved['sell_put']['enabled'] is True
    assert resolved['sell_put']['min_dte'] == 20
    assert resolved['sell_put']['min_annualized_net_return'] == 0.12
    assert resolved['sell_call']['enabled'] is True
    assert resolved['sell_call']['min_annualized_net_premium_return'] == 0.11
    assert resolved['sell_call']['min_strike_cost_multiplier'] == 1.02
    assert 'min_annualized_net_return' not in resolved['sell_call']
    assert resolved['_global_sell_put_liquidity'] == {'min_open_interest': 50}
    assert resolved['_global_sell_call_liquidity'] == {'min_volume': 12}


def test_resolve_watchlist_item_runtime_config_revalidates_merged_dte_window() -> None:
    import pytest

    from src.application.pipeline_watchlist import resolve_watchlist_item_runtime_config

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        merged = dict(profiles["put_base"])
        merged["sell_put"] = {**merged["sell_put"], **item["sell_put"]}
        return {**item, **merged}

    with pytest.raises(SystemExit, match="min_dte > .*max_dte"):
        resolve_watchlist_item_runtime_config(
            item={
                "symbol": "NVDA",
                "use": ["put_base"],
                "sell_put": {"enabled": True, "max_dte": 30},
            },
            profiles={"put_base": {"sell_put": {"min_dte": 60}}},
            apply_profiles_fn=_apply_profiles,
        )
