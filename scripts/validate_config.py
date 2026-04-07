#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def die(msg: str):
    raise SystemExit(f"[CONFIG_ERROR] {msg}")


def validate_config(cfg: dict):
    # intake config (optional)
    intake = cfg.get('intake') or {}
    if intake and not isinstance(intake, dict):
        die('intake must be an object')
    if isinstance(intake, dict):
        sa = intake.get('symbol_aliases') or {}
        if sa and not isinstance(sa, dict):
            die('intake.symbol_aliases must be an object')
        mb = intake.get('multiplier_by_symbol') or {}
        if mb and not isinstance(mb, dict):
            die('intake.multiplier_by_symbol must be an object')
        for k, v in mb.items():
            try:
                if float(v) <= 0:
                    die(f'intake.multiplier_by_symbol[{k}] must be > 0')
            except Exception:
                die(f'intake.multiplier_by_symbol[{k}] must be a number')
        for key in ('default_multiplier_us', 'default_multiplier_hk'):
            if key in intake and intake[key] is not None:
                try:
                    if float(intake[key]) <= 0:
                        die(f'intake.{key} must be > 0')
                except Exception:
                    die(f'intake.{key} must be a number')

    syms = cfg.get('symbols')
    if syms is None:
        syms = cfg.get('watchlist')
    if not isinstance(syms, list) or not syms:
        die('symbols[] (or watchlist[]) is required and cannot be empty')

    # Backward-compat: normalize for downstream validation loops
    if cfg.get('symbols') is None and isinstance(syms, list):
        cfg['symbols'] = syms

    runtime = cfg.get('runtime') or {}
    if runtime and not isinstance(runtime, dict):
        die('runtime must be an object')
    if isinstance(runtime, dict):
        st = runtime.get('symbol_timeout_sec', 120)
        pt = runtime.get('portfolio_timeout_sec', 60)
        try:
            if st is not None and int(st) <= 0:
                die('runtime.symbol_timeout_sec must be > 0')
        except Exception:
            die('runtime.symbol_timeout_sec must be an integer')
        try:
            if pt is not None and int(pt) <= 0:
                die('runtime.portfolio_timeout_sec must be > 0')
        except Exception:
            die('runtime.portfolio_timeout_sec must be an integer')

    templates = cfg.get('templates')
    if templates is None:
        templates = cfg.get('profiles')
    templates = templates or {}
    if templates and not isinstance(templates, dict):
        die('templates (or profiles) must be an object')

    # Backward-compat: keep templates key populated for older code paths
    if cfg.get('templates') is None and isinstance(templates, dict):
        cfg['templates'] = templates

    seen = set()
    for i, item in enumerate(cfg['symbols']):
        if not isinstance(item, dict):
            die(f"symbols[{i}] must be an object")
        sym = item.get('symbol')
        if not sym or not isinstance(sym, str):
            die(f"symbols[{i}].symbol is required")
        if sym in seen:
            die(f"duplicate symbol: {sym}")
        seen.add(sym)

        # sell_put basic checks if enabled
        sp = item.get('sell_put') or {}
        if sp.get('enabled'):
            for k in ('min_dte','max_dte','min_strike','max_strike'):
                if k not in sp:
                    die(f"{sym}.sell_put enabled but missing {k}")
            if sp['min_dte'] > sp['max_dte']:
                die(f"{sym}.sell_put min_dte > max_dte")
            if sp['min_strike'] > sp['max_strike']:
                die(f"{sym}.sell_put min_strike > max_strike")

        sc = item.get('sell_call') or {}
        if sc.get('enabled'):
            # NOTE:
            # - avg_cost/shares can be sourced from holdings (portfolio_context) at runtime.
            # - Therefore, do not require them in config validation.
            # - If holdings is unavailable for an account, pipeline will skip sell_call for that account.
            for k in ('min_dte', 'max_dte', 'min_strike'):
                if k not in sc:
                    die(f"{sym}.sell_call enabled but missing {k}")

            if sc['min_dte'] > sc['max_dte']:
                die(f"{sym}.sell_call min_dte > max_dte")

            # Optional sanity checks
            if sc.get('shares') is not None:
                try:
                    if int(sc.get('shares')) <= 0:
                        die(f"{sym}.sell_call shares must be > 0")
                except Exception:
                    die(f"{sym}.sell_call shares must be an integer")

            if sc.get('avg_cost') is not None:
                try:
                    if float(sc.get('avg_cost')) <= 0:
                        die(f"{sym}.sell_call avg_cost must be > 0")
                except Exception:
                    die(f"{sym}.sell_call avg_cost must be a number")


def main():
    ap = argparse.ArgumentParser(description='Validate options-monitor config.json')
    ap.add_argument('--config', default='config.json')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    p = Path(args.config)
    if not p.is_absolute():
        p = (base / p).resolve()

    if not p.exists():
        die(f"config not found: {p}")

    cfg = json.loads(p.read_text(encoding='utf-8'))
    validate_config(cfg)
    print('[OK] config valid')


if __name__ == '__main__':
    main()
