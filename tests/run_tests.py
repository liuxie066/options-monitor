#!/usr/bin/env python3
"""Tiny test runner (no pytest dependency).

We keep this repo runnable in minimal environments.

Usage:
  ./.venv/bin/python tests/run_tests.py
  ./.venv/bin/python tests/run_tests.py --all
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import subprocess
import traceback
import types
import unittest
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
VPY = BASE / '.venv' / 'bin' / 'python'
TESTS_DIR = Path(__file__).resolve().parent

# Keep default scope as key regressions so minimal environments stay stable.
CRITICAL_MODULES = [
    "test_multiplier_no_default_in_scanners",
    "test_futu_gateway_minimal",
    "test_opend_chain_cache_minimal",
    "test_opend_watchdog_alerts",
    "test_fetch_portfolio_context_richtext",
    "test_report_labels_no_stale",
    "test_sell_call_cover_capacity",
    "test_scan_scheduler_notify_semantics",
    "test_scan_scheduler_scan_per_account",
    "test_market_session_single_source_of_truth",
    "test_config_loader_validation_cache",
    "test_runtime_config_sync",
    "test_atomic_write_json",
    "test_pipeline_watchlist_whitelist",
    "test_pipeline_runner_stage_plan",
    "test_multi_tick_account_state_dir",
    "test_pipeline_postprocess_notify_gate",
    "test_notify_symbols_markdown",
    "test_sell_call_min_annualized_resolution",
    "test_sell_put_min_annualized_resolution",
    "test_multi_tick_notify_format",
    "test_http_json_http_error_handling",
    "test_feishu_bitable",
    "test_option_candidate_strategy",
    "test_candidate_engine_contract",
    "test_candidate_engine_parity",
    "test_phase1_tool_boundary",
    "test_domain_engine_batch4",
    "test_domain_engine_batch5",
    "test_send_if_needed_batch3",
    "test_send_if_needed_batch4",
    "test_agent_plugin_contract",
    "test_agent_plugin_smoke",
]

# Keep default critical scope behavior-compatible with the previous curated list.
# We still use auto-discovery, but trim non-critical additions in a few modules.
CRITICAL_EXCLUDED_TESTS: dict[str, set[str]] = {
    "test_opend_chain_cache_minimal": {"test_chain_cache_prune_by_mtime"},
    "test_feishu_bitable": {"test_http_json_logs_warn_retries_when_rate_limited"},
}


def run_parser(text: str) -> dict:
    p = subprocess.run(
        [str(VPY), 'scripts/parse_option_message.py', '--text', text, '--accounts', 'lx', 'sy'],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=True,
    )
    # Some scripts may print logs; extract the JSON block from the first '{' to the last '}'.
    out = (p.stdout or '').strip()
    if not out:
        raise RuntimeError('empty stdout')
    s = out.find('{')
    e = out.rfind('}')
    if s < 0 or e < 0 or e <= s:
        raise RuntimeError(f'cannot find json in stdout: {out[:200]}')
    j = out[s : e + 1]
    return json.loads(j)


def test_parse_futu_fill_message() -> None:
    msg = "【成交提醒】成功卖出2张$中海油 260330 30.00 购$，成交价格：0.24，此笔订单委托已全部成交，2026/03/25 14:16:53 (香港)。【富途证券(香港)】 lx"
    out = run_parser(msg)
    parsed = out['parsed']
    assert parsed['symbol'] == '0883.HK'
    assert parsed['exp'] == '2026-03-30'
    assert parsed['option_type'] == 'call'
    assert parsed['side'] == 'short'
    assert parsed['strike'] == 30.0
    assert parsed['premium_per_share'] == 0.24
    assert parsed['contracts'] == 2
    assert parsed['account'] == 'lx'
    assert parsed['currency'] == 'HKD'
    if parsed['multiplier'] is None:
        assert 'multiplier' in out['missing']
        assert out['ok'] is False
    else:
        assert parsed['multiplier'] == 1000
        assert out['ok'] is True


def test_run_log_writer_create_and_append() -> None:
    import sys
    from tempfile import TemporaryDirectory

    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.run_log import RunLogger

    with TemporaryDirectory() as td:
        root = Path(td)
        lg = RunLogger(root)
        lg.event('run_start', 'start', data={'accounts': ['lx', 'sy'], 'symbols_count': 3})
        lg.event('run_end', 'ok', duration_ms=12)

        files = list((root / 'audit' / 'run_logs').glob('*.jsonl'))
        assert len(files) == 1
        lines = [ln for ln in files[0].read_text(encoding='utf-8').splitlines() if ln.strip()]
        assert len(lines) == 2

        rec1 = json.loads(lines[0])
        rec2 = json.loads(lines[1])
        assert rec1['run_id'] == rec2['run_id']
        assert rec1['step'] == 'run_start'
        assert rec2['step'] == 'run_end'


def test_run_log_data_small() -> None:
    import sys

    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    from scripts.run_log import _compact_data

    big = {
        'k1': 'x' * 2000,
        'k2': list(range(500)),
        'k3': {'a': 1, 'b': 2, 'c': 3},
    }
    out = _compact_data(big, max_chars=300)
    payload = json.dumps(out, ensure_ascii=False)
    assert len(payload) <= 300


def _discover_all_module_names() -> list[str]:
    names = []
    for p in sorted(TESTS_DIR.glob("test_*.py")):
        names.append(p.stem)
    return names


def _pick_module_names(run_all: bool) -> list[str]:
    if run_all:
        return _discover_all_module_names()
    return list(CRITICAL_MODULES)


def _iter_function_tests(mod: types.ModuleType, *, exclude_names: set[str] | None = None):
    excluded = exclude_names or set()
    for _, fn in inspect.getmembers(mod, inspect.isfunction):
        if not fn.__name__.startswith("test_"):
            continue
        if fn.__name__ in excluded:
            continue
        if fn.__module__ != mod.__name__:
            continue
        sig = inspect.signature(fn)
        required = [
            p for p in sig.parameters.values()
            if p.default is inspect._empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        if required:
            continue
        yield fn


def _run_unittest_module(mod: types.ModuleType) -> tuple[int, list[str]]:
    suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
    total = suite.countTestCases()
    if total == 0:
        return 0, []
    result = unittest.TestResult()
    suite.run(result)
    errs = []
    for case, tb in result.errors + result.failures:
        errs.append(f"[FAIL] {case.id()}\n{tb}")
    return total, errs


def run_modules(module_names: list[str], *, run_all: bool) -> tuple[int, list[str]]:
    total = 0
    failures: list[str] = []

    for mod_name in module_names:
        mod = importlib.import_module(mod_name)

        excluded = set() if run_all else CRITICAL_EXCLUDED_TESTS.get(mod_name, set())
        fn_tests = sorted(_iter_function_tests(mod, exclude_names=excluded), key=lambda f: f.__name__)
        for fn in fn_tests:
            total += 1
            try:
                fn()
            except Exception:
                failures.append(f"[FAIL] {mod_name}.{fn.__name__}\n{traceback.format_exc()}")

        ut_count, ut_failures = _run_unittest_module(mod)
        total += ut_count
        failures.extend(ut_failures)

    return total, failures


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Run regression tests without pytest")
    ap.add_argument("--all", action="store_true", help="run all test_*.py modules instead of critical suite")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    module_names = _pick_module_names(run_all=bool(args.all))

    total, failures = run_modules(module_names, run_all=bool(args.all))
    if failures:
        print("\n\n".join(failures))
        raise SystemExit(1)

    scope = "all" if args.all else "critical"
    print(f"OK ({total} tests, scope={scope})")


if __name__ == '__main__':
    main()
