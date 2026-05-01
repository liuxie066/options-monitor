#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from domain.domain.fetch_source import normalize_fetch_source
from scripts.account_config import ACCOUNT_TYPES, account_settings_from_config, accounts_from_config
from scripts.config_loader import resolve_templates_config, resolve_watchlist_config, set_watchlist_config
from scripts.trade_account_mapping import resolve_trade_intake_config
from src.application.opend_fetch_config import OPEND_RATE_LIMIT_ENDPOINT_KEYS

LIQUIDITY_ALLOWED_GLOBAL_FIELDS = (
    'min_open_interest',
    'min_volume',
    'max_spread_ratio',
)
REMOVED_STRATEGY_FILTER_FIELDS = (
    'require_bid_ask',
    'min_iv',
    'max_iv',
    'min_abs_delta',
    'max_abs_delta',
    'min_delta',
    'max_delta',
)
SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS = LIQUIDITY_ALLOWED_GLOBAL_FIELDS + REMOVED_STRATEGY_FILTER_FIELDS + ('event_risk',)
LEGACY_SELL_CALL_FETCH_FIELDS = ('target_otm_pct_min', 'target_otm_pct_max')


def die(msg: str):
    raise SystemExit(f"[CONFIG_ERROR] {msg}")


def warn(msg: str):
    print(f"[CONFIG_WARN] {msg}", file=sys.stderr)


def validate_positive_number(value, path: str):
    try:
        if float(value) <= 0:
            die(f'{path} must be > 0')
    except Exception:
        die(f'{path} must be a number')


def validate_positive_integer(value, path: str):
    try:
        parsed = int(value)
        if float(value) != float(parsed):
            die(f'{path} must be an integer')
        if parsed <= 0:
            die(f'{path} must be > 0')
    except Exception:
        die(f'{path} must be an integer')


def validate_rate_limit_object(raw: dict, path: str):
    for key in ('window_sec', 'max_wait_sec'):
        if key in raw and raw.get(key) is not None:
            validate_positive_number(raw.get(key), f'{path}.{key}')
    if 'max_calls' in raw and raw.get('max_calls') is not None:
        validate_positive_integer(raw.get('max_calls'), f'{path}.max_calls')


def validate_non_negative_integer(value, path: str):
    try:
        parsed = int(value)
        if float(value) != float(parsed):
            die(f'{path} must be an integer')
        if parsed < 0:
            die(f'{path} must be >= 0')
    except Exception:
        die(f'{path} must be an integer')


def validate_config(cfg: dict):
    if 'watchlist' in cfg:
        die('watchlist is no longer supported; use symbols')
    if 'profiles' in cfg:
        die('profiles is no longer supported; use templates')
    if 'fees' in cfg:
        die('fees is no longer supported; fee rules are built in')

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

    syms = resolve_watchlist_config(cfg)
    if not syms:
        die('symbols[] is required and cannot be empty')

    set_watchlist_config(cfg, syms)

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
        chain_fetch = runtime.get('option_chain_fetch')
        if chain_fetch is not None:
            if not isinstance(chain_fetch, dict):
                die('runtime.option_chain_fetch must be an object')
            validate_rate_limit_object(chain_fetch, 'runtime.option_chain_fetch')
        opend_rate_limits = runtime.get('opend_rate_limits')
        if opend_rate_limits is not None:
            if not isinstance(opend_rate_limits, dict):
                die('runtime.opend_rate_limits must be an object')
            for endpoint, raw in opend_rate_limits.items():
                if str(endpoint) not in OPEND_RATE_LIMIT_ENDPOINT_KEYS:
                    allowed = ', '.join(sorted(OPEND_RATE_LIMIT_ENDPOINT_KEYS))
                    die(f'runtime.opend_rate_limits.{endpoint} is not supported; use one of: {allowed}')
                if not isinstance(raw, dict):
                    die(f'runtime.opend_rate_limits.{endpoint} must be an object')
                validate_rate_limit_object(raw, f'runtime.opend_rate_limits.{endpoint}')

    notifications = cfg.get('notifications') or {}
    if notifications and not isinstance(notifications, dict):
        die('notifications must be an object')
    if isinstance(notifications, dict) and notifications:
        channel = str(notifications.get('channel') or '').strip().lower()
        if channel != 'feishu':
            die('notifications.channel must be feishu')

        target = notifications.get('target')
        if not isinstance(target, str) or not str(target).strip():
            die('notifications.target must be a non-empty open_id string')

        secrets_file_value = str(notifications.get('secrets_file') or 'secrets/notifications.feishu.app.json').strip()
        secrets_path = Path(secrets_file_value)
        if not secrets_path.is_absolute():
            secrets_path = (repo_base / secrets_path).resolve()
        if not secrets_path.exists():
            die(f'notification secrets file not found: {secrets_path}')

        try:
            secrets_payload = json.loads(secrets_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            die(f'notification secrets file is not valid json: {secrets_path}')

        feishu = secrets_payload.get('feishu') if isinstance(secrets_payload, dict) else None
        if not isinstance(feishu, dict):
            die(f'notification secrets missing feishu object: {secrets_path}')
        if not str(feishu.get('app_id') or '').strip() or not str(feishu.get('app_secret') or '').strip():
            die(f'notification secrets missing feishu.app_id/app_secret: {secrets_path}')

    close_advice = cfg.get('close_advice') or {}
    if close_advice and not isinstance(close_advice, dict):
        die('close_advice must be an object')
    if isinstance(close_advice, dict):
        quote_source = str(close_advice.get('quote_source') or '').strip().lower()
        if quote_source and quote_source not in {'auto', 'required_data'}:
            die('close_advice.quote_source must be auto or required_data')
        notify_levels = close_advice.get('notify_levels')
        if notify_levels is not None:
            if not isinstance(notify_levels, list):
                die('close_advice.notify_levels must be a list')
            bad_levels = [
                str(item).strip().lower()
                for item in notify_levels
                if str(item).strip().lower() not in {'strong', 'medium', 'optional', 'weak'}
            ]
            if bad_levels:
                die(f"close_advice.notify_levels has unsupported levels: {', '.join(bad_levels)}")
        if 'max_items_per_account' in close_advice and close_advice.get('max_items_per_account') is not None:
            validate_non_negative_integer(
                close_advice.get('max_items_per_account'),
                'close_advice.max_items_per_account',
            )
        for key in ('max_spread_ratio', 'strong_remaining_annualized_max', 'medium_remaining_annualized_max'):
            if key not in close_advice or close_advice.get(key) is None:
                continue
            try:
                if float(close_advice.get(key)) < 0:
                    die(f'close_advice.{key} must be >= 0')
            except Exception:
                die(f'close_advice.{key} must be a number')

    account_settings = cfg.get('account_settings') or {}
    if account_settings and not isinstance(account_settings, dict):
        die('account_settings must be an object')
    if isinstance(account_settings, dict):
        known_accounts = set(accounts_from_config(cfg))
        for raw_key, raw_value in account_settings.items():
            account = str(raw_key or '').strip().lower()
            if not account:
                die('account_settings contains empty account key')
            if account not in known_accounts:
                die(f'account_settings.{account} must also appear in top-level accounts')
            if not isinstance(raw_value, dict):
                die(f'account_settings.{account} must be an object')
            acct_type = str(raw_value.get('type') or '').strip().lower()
            if acct_type not in ACCOUNT_TYPES:
                die(f'account_settings.{account}.type must be one of: {", ".join(ACCOUNT_TYPES)}')
            holdings_account = raw_value.get('holdings_account')
            if holdings_account is not None and not str(holdings_account).strip():
                die(f'account_settings.{account}.holdings_account must be a non-empty string when set')
        account_settings_from_config(cfg)

    trade_intake = cfg.get('trade_intake') or {}
    if trade_intake and not isinstance(trade_intake, dict):
        die('trade_intake must be an object')
    if isinstance(trade_intake, dict):
        try:
            resolve_trade_intake_config(cfg)
        except ValueError as exc:
            die(str(exc))

    raw_templates = cfg.get('templates')
    if raw_templates is not None and not isinstance(raw_templates, dict):
        die('templates must be an object')
    templates = resolve_templates_config(cfg)

# Strict config contract: global liquidity filters only support 3 hard fields.
    if isinstance(templates, dict):
        for profile_name, profile in templates.items():
            if not isinstance(profile, dict):
                continue
            for side in ('sell_put', 'sell_call'):
                side_cfg = profile.get(side)
                if not isinstance(side_cfg, dict):
                    continue
                bad_keys = [k for k in REMOVED_STRATEGY_FILTER_FIELDS if k in side_cfg]
                if bad_keys:
                    die(
                        f"templates.{profile_name}.{side} has unsupported strategy filter keys: "
                        f"{', '.join(bad_keys)}; only {', '.join(LIQUIDITY_ALLOWED_GLOBAL_FIELDS)} are allowed"
                    )
                if side == 'sell_call':
                    unsupported_fetch_keys = [k for k in LEGACY_SELL_CALL_FETCH_FIELDS if k in side_cfg]
                    if unsupported_fetch_keys:
                        die(
                            f"templates.{profile_name}.{side} has removed legacy fetch planning keys: "
                            f"{', '.join(unsupported_fetch_keys)}; use min_strike/max_strike only"
                        )

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

        fetch = item.get('fetch') or {}
        if fetch and not isinstance(fetch, dict):
            die(f"{sym}.fetch must be an object")
        if isinstance(fetch, dict):
            src_raw = fetch.get('source', 'futu')
            src = normalize_fetch_source(src_raw)
            if src != 'opend':
                die(f"{sym}.fetch.source unsupported: {src_raw}; use futu")
            if str(src_raw or '').strip().lower() == 'opend':
                warn(f"{sym}.fetch.source=opend is legacy; prefer futu")

        # sell_put basic checks if enabled
        sp = item.get('sell_put') or {}
        if isinstance(sp, dict):
            bad_keys = [k for k in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS if k in sp]
            if bad_keys:
                die(f"{sym}.sell_put has forbidden symbol-level strategy filter keys: {', '.join(bad_keys)}")
        if sp.get('enabled'):
            for k in ('min_dte','max_dte'):
                if k not in sp:
                    die(f"{sym}.sell_put enabled but missing {k}")
            if sp['min_dte'] > sp['max_dte']:
                die(f"{sym}.sell_put min_dte > max_dte")
            if ('min_strike' in sp) and (sp['min_strike'] is not None) and (float(sp['min_strike']) <= 0):
                die(f"{sym}.sell_put min_strike must be > 0; use null or omit it instead of 0")
            if ('max_strike' in sp) and (sp['max_strike'] is not None) and (float(sp['max_strike']) <= 0):
                die(f"{sym}.sell_put max_strike must be > 0")
            if (
                ('min_strike' in sp) and (sp['min_strike'] is not None)
                and ('max_strike' in sp) and (sp['max_strike'] is not None)
                and (float(sp['min_strike']) > float(sp['max_strike']))
            ):
                die(f"{sym}.sell_put min_strike > max_strike")
            if ('min_strike' in sp) and (sp.get('max_strike') is None):
                warn(f"{sym}.sell_put only sets min_strike; near-bound max_strike is recommended")

        sc = item.get('sell_call') or {}
        if isinstance(sc, dict):
            bad_keys = [k for k in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS if k in sc]
            if bad_keys:
                die(f"{sym}.sell_call has forbidden symbol-level strategy filter keys: {', '.join(bad_keys)}")
            unsupported_fetch_keys = [k for k in LEGACY_SELL_CALL_FETCH_FIELDS if k in sc]
            if unsupported_fetch_keys:
                die(
                    f"{sym}.sell_call has removed legacy fetch planning keys: {', '.join(unsupported_fetch_keys)}; "
                    "use min_strike/max_strike only"
                )
        if sc.get('enabled'):
            # NOTE:
            # - sell_call cost basis/shares come from account portfolio_context at runtime.
            # - portfolio_context may be backed by OpenD or holdings depending on account/runtime settings.
            # - Therefore, do not require them in config validation.
            # - If portfolio_context is unavailable for an account, pipeline will skip sell_call for that account.
            for k in ('min_dte', 'max_dte'):
                if k not in sc:
                    die(f"{sym}.sell_call enabled but missing {k}")

            if sc['min_dte'] > sc['max_dte']:
                die(f"{sym}.sell_call min_dte > max_dte")
            if ('min_strike' in sc) and (sc['min_strike'] is not None) and (float(sc['min_strike']) <= 0):
                die(f"{sym}.sell_call min_strike must be > 0")
            if ('max_strike' in sc) and (sc['max_strike'] is not None) and (float(sc['max_strike']) <= 0):
                die(f"{sym}.sell_call max_strike must be > 0 when set")
            if (
                ('min_strike' in sc) and (sc['min_strike'] is not None)
                and ('max_strike' in sc) and (sc['max_strike'] is not None)
                and (float(sc['min_strike']) > float(sc['max_strike']))
            ):
                die(f"{sym}.sell_call min_strike > max_strike")
            if ('max_strike' in sc) and (sc.get('min_strike') is None):
                warn(f"{sym}.sell_call only sets max_strike; near-bound min_strike is recommended")


def main():
    ap = argparse.ArgumentParser(description='Validate options-monitor config.us.json/config.hk.json')
    ap.add_argument('--config', default='config.us.json')
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
