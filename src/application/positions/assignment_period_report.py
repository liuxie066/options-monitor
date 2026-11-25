"""Current, read-only assignment cohorts; financial facts remain with their owners."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.assigned_stock import assigned_put_remaining_economics, assigned_stock_sale_allocations
from domain.domain.trade_contract_identity import derive_position_side
from domain.domain.option_position_identity import normalize_broker
from src.application.agent_tool_contracts import AgentToolError
from src.application.account_config import resolve_configured_accounts
from src.application.runtime_paths import resolve_runtime_root
from src.application.ledger import api as ledger_api
from src.application.performance.adapters import ledger_performance_inputs_from_rows
from src.application.positions.assigned_stock_view import build_assigned_stock_view
from src.application.positions.assigned_stock_quotes import assigned_stock_quote_is_usable
from src.application.trades.account_mapping import resolve_futu_account_mapping
from src.application.trades.inbox import read_stock_sell_observations
from src.infrastructure.exchange_rates import current_exchange_rate_snapshot, project_exchange_rate_snapshot

_PERIOD_KEYS = ('assignment_year', 'assignment_month', 'assignment_start_date', 'assignment_end_date')
_SHANGHAI = ZoneInfo('Asia/Shanghai')
_METRICS = ('stock_unrealized_pnl', 'remaining_pnl_with_put')


def assignment_period(payload: dict[str, Any], request_at_ms: int) -> dict[str, Any]:
    now = datetime.fromtimestamp(request_at_ms / 1000, _SHANGHAI)
    year, month = payload.get('assignment_year'), payload.get('assignment_month')
    start, end = payload.get('assignment_start_date'), payload.get('assignment_end_date')
    try:
        if payload.get('as_of_ms') is not None or payload.get('status', 'all') != 'all':
            raise ValueError('assignment periods require current valuation and status=all')
        forms = int(year is not None) + int(month is not None) + int(start is not None or end is not None)
        if forms != 1:
            raise ValueError('specify exactly one assignment year, month, or date range')
        if year is not None:
            if isinstance(year, bool) or not isinstance(year, int):
                raise ValueError('assignment_year must be an integer')
            first, stop = date(year, 1, 1), date(year + 1, 1, 1)
        elif month is not None:
            first = date.fromisoformat(str(month) + '-01')
            stop = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        else:
            from datetime import timedelta
            first = date.fromisoformat(start)
            stop = date.fromisoformat(end) + timedelta(days=1)
        lo = int(datetime.combine(first, time(), _SHANGHAI).timestamp() * 1000)
        hi = int(datetime.combine(stop, time(), _SHANGHAI).timestamp() * 1000)
        if hi <= lo or lo > request_at_ms:
            raise ValueError('assignment period is reversed or in the future')
    except (ValueError, TypeError, OverflowError) as exc:
        raise AgentToolError(code='INPUT_ERROR', message=str(exc)) from exc
    return {'start_at_ms': lo, 'requested_end_exclusive_at_ms': hi,
            'end_exclusive_at_ms': min(hi, request_at_ms), 'timezone': 'Asia/Shanghai'}


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def summarize_lots(lots: list[dict[str, Any]], *, empty_is_zero: bool = True) -> dict[str, Any]:
    currencies: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in lots:
        currencies[row['currency']].append(row)
    result = {}
    for currency, rows in currencies.items():
        result[currency] = {}
        for metric in _METRICS:
            included = [r for r in rows if r.get(metric) is not None]
            omitted = [r for r in rows if r.get(metric) is None]
            provisional = [r for r in included if r['quantity_status'] == 'ledger_only']
            total = sum((Decimal(r[metric]) for r in included), Decimal(0))
            result[currency][metric] = {
                'value': _money(total) if included else None,
                'exact_value': str(total) if included else None,
                'coverage': 'partial' if omitted or provisional else 'complete',
                'basis': 'ledger' if provisional else 'confirmed',
                'included_lot_ids': [r['stock_lot_id'] for r in included],
                'provisional_lot_ids': [r['stock_lot_id'] for r in provisional],
                'omitted_lot_ids': [r['stock_lot_id'] for r in omitted],
                'omitted_reasons': {r['stock_lot_id']: r['reasons'] for r in omitted},
            }
    return {'by_currency': result, 'empty_value': '0.00' if not lots and empty_is_zero else None}


def _resolved_observation(observation: dict, sales: list[dict]) -> bool:
    """Only a fully scoped, economically identical canonical sale resolves a fill."""
    for sale in sales:
        if not all(str(sale.get(k) or '') == str(observation.get(k) or '')
                   for k in ('account', 'symbol', 'source_deal_id')):
            continue
        if not observation.get('source_deal_id') or not observation.get('futu_account_id'):
            continue
        if str(sale.get('futu_account_id') or '') != str(observation['futu_account_id']):
            continue
        if (normalize_broker(sale.get('broker')) != normalize_broker(observation.get('broker')) or sale.get('currency') != observation.get('currency')
                or int(sale.get('shares') or 0) != int(observation.get('shares') or 0)):
            continue
        if observation.get('price') is None or sale.get('price') is None:
            continue
        if Decimal(str(sale['price'])) != Decimal(str(observation['price'])):
            continue
        if observation.get('trade_time_ms') != sale.get('trade_time_ms'):
            continue
        try:
            allocations = assigned_stock_sale_allocations(sale)
        except (TypeError, ValueError):
            continue
        if sum(int(a.get('shares') or 0) for a in allocations) != int(sale['shares']):
            continue
        return True
    return False


def _affected(row: dict, observation: dict) -> bool:
    if (observation.get('broker') and normalize_broker(observation['broker']) != normalize_broker(row.get('broker'))):
        return False
    if any(observation.get(k) and observation[k] != row.get(k) for k in ('account', 'symbol', 'currency')):
        return False
    instant = observation.get('trade_time_ms')
    return instant is None or int(row['assigned_at_ms']) <= int(instant)


def _quote_scope_matches(quote: dict, row: dict) -> bool:
    return (not quote.get('account') or str(quote['account']).lower() == row['account']) and (
        not quote.get('broker') or normalize_broker(quote['broker']) == normalize_broker(row['broker']))


def _fx_summary(summary: dict, snapshot: dict, valuation_at_ms: int) -> dict:
    projected = project_exchange_rate_snapshot(snapshot, purpose='display',
                    now=datetime.fromtimestamp(valuation_at_ms / 1000, timezone.utc))
    pairs = projected.get('pairs') or {}
    eligible = {}
    day = datetime.fromtimestamp(valuation_at_ms / 1000, _SHANGHAI).date()
    for currency in summary['by_currency']:
        if currency == 'CNY':
            eligible[currency] = (Decimal(1), 'identity')
            continue
        pair = pairs.get(currency + 'CNY') or pairs.get(currency + '/CNY') or {}
        try:
            stamp = datetime.fromisoformat(pair['quote_at_utc'].replace('Z', '+00:00'))
            if pair.get('display_eligible') and stamp.astimezone(_SHANGHAI).date() == day:
                eligible[currency] = (Decimal(str(pair['rate'])), pair.get('source'))
        except (KeyError, ValueError, TypeError):
            continue
    sources = {source for rate, source in eligible.values() if source != 'identity'}
    if len(sources) > 1:
        eligible = {c: v for c, v in eligible.items() if c == 'CNY'}
    result = {'currency': 'CNY', 'fx_evidence': projected, 'metrics': {}}
    for metric in _METRICS:
        values = summary['by_currency']
        covered = [c for c, r in values.items() if c in eligible and r[metric]['exact_value'] is not None]
        missing = [c for c in values if c not in covered]
        amount = sum((Decimal(values[c][metric]['exact_value']) * eligible[c][0] for c in covered), Decimal(0))
        partial = missing or any(values[c][metric]['coverage'] != 'complete' for c in covered) or (not values and summary.get('empty_value') is None)
        result['metrics'][metric] = {'value': _money(amount) if covered else summary.get('empty_value'),
            'coverage': 'partial' if partial else 'complete', 'included_currencies': covered,
            'omitted_currencies': missing}
    return result


def build_assignment_period_report(repo, payload, *, cfg, refresh_assigned_stock_quotes,
                                   repo_base, quote_state_base_dir=None, now_ms=None):
    clock = now_ms or (lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    request_at = clock()
    period = assignment_period(payload, request_at)
    allowed_accounts = set(resolve_configured_accounts(cfg, [payload['account']] if payload.get('account') else None))
    raw = ledger_api.read_assigned_stock_query_inputs(repo)
    inputs = ledger_performance_inputs_from_rows(raw, extra_diagnostics=raw.get('diagnostics', []))
    base = build_assigned_stock_view(repo, ledger_inputs=inputs, as_of_ms=request_at,
                                    account=payload.get('account'), broker=payload.get('broker'), quote_snapshots=[])
    events = {r['event_id']: r for r in inputs.rows}
    selected = []
    for row in base.get('assigned_stock_lots', []):
        if row['account'] not in allowed_accounts:
            continue
        event = events.get(row['source_assignment_event_id'], {})
        raw_event = event.get('raw_payload') or {}
        if str(event.get('close_type') or raw_event.get('close_type') or event.get('event_type')).lower() != 'assignment':
            continue
        if event.get('option_type') != 'put' or derive_position_side(event.get('position_effect'), event.get('side')) != 'short':
            continue
        if not period['start_at_ms'] <= row['assigned_at_ms'] < period['end_exclusive_at_ms']:
            continue
        if payload.get('symbol') and row['symbol'] != payload['symbol'].upper():
            continue
        if payload.get('stock_lot_id') and row['stock_lot_id'] != payload['stock_lot_id']:
            continue
        selected.append(row)
    root = Path(repo_base())
    db = getattr(repo, 'path', None) or getattr(repo, 'db_path', None)
    observations = {'observations': [], 'coverage': 'unknown', 'source_history': 'unknown'}
    try:
        if db:
            observations = read_stock_sell_observations(str(db) + '.trade_intake_inbox.sqlite3',
                futu_account_mapping=resolve_futu_account_mapping(cfg), config=cfg,
                accounts=sorted(allowed_accounts),
                symbols=[payload['symbol']] if payload.get('symbol') else None, repo_base=root,
                runtime_root=resolve_runtime_root(repo_root=root).runtime_root)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        observations['diagnostics'] = [{'code': 'stock_sell_source_unavailable', 'message': type(exc).__name__}]
    pending = [o for o in observations['observations']
               if (o.get('trade_time_ms') is None or o['trade_time_ms'] <= request_at)
               and not _resolved_observation(o, list(inputs.assigned_stock_events))]
    reviews = [r for r in base.get('assigned_stock_review_rows', [])
               if r.get('status') != 'missing_quote' and not str(r.get('status', '')).startswith('covered_call_')
               and r.get('account') in allowed_accounts
               and (not payload.get('symbol') or r.get('symbol') == payload['symbol'].upper())]
    for review in reviews:
        source_event = events.get(review.get('event_id'), {})
        review['trade_time_ms'] = source_event.get('trade_time_ms') or source_event.get('event_time_ms')

    source_gaps = observations.get('diagnostics') or ([] if observations['coverage'] == 'complete' else [{}])
    for row in selected:
        row['_pending'] = (any(_affected(row, gap) for gap in source_gaps) or bool(inputs.diagnostics)
            or any(_affected(row, o) for o in pending)
            or any(_affected(row, r) for r in reviews))
    quotes = payload.get('quote_snapshots') or []
    refresh = {'enabled': False}
    if payload.get('refresh_quotes', True) and 'quote_snapshots' not in payload:
        needed = [r for r in selected if r['shares_remaining'] and not r['_pending']]
        if needed:
            try:
                result = refresh_assigned_stock_quotes(needed, cfg=cfg, account=payload.get('account'),
                    base_dir=root, state_base_dir=quote_state_base_dir, preserve_evidence=True)
                quotes = result.quote_snapshots
                refresh = result.diagnostics
            except Exception as exc:
                refresh = {'enabled': True, 'status': 'source_error', 'error': type(exc).__name__}
    fx = {}
    if (selected and payload.get('refresh_quotes', True) and 'quote_snapshots' not in payload
            and any(r['currency'] != 'CNY' for r in selected)):
        try:
            fx = current_exchange_rate_snapshot(cache_path=(Path(quote_state_base_dir or root) /
                'output_shared/state/rate_cache.json'), write_cache=False)
        except (OSError, ValueError, RuntimeError):
            fx = {}
    valuation_at = clock()
    from domain.domain.assigned_stock import _normalize_quote_snapshots
    quote_rows = _normalize_quote_snapshots(quotes)
    rows = []
    for lot in selected:
        row = dict(lot)
        is_pending = row.pop('_pending')
        qualified = [q for q in quote_rows if q.get('symbol') == row['symbol']
                     and _quote_scope_matches(q, row)
                     and assigned_stock_quote_is_usable(q, valuation_at)
                     and (not q.get('currency') or q['currency'] == row['currency'])
                     and q.get('quote_time_ms') is not None and int(q['quote_time_ms']) <= valuation_at]
        quote = max(qualified, key=lambda q: int(q['quote_time_ms']), default={})
        row['spot'] = quote.get('spot')
        row['quote'] = quote
        row['book_shares_sold'], row['book_shares_remaining'] = row['shares_sold'], row['shares_remaining']
        row.update(assigned_put_remaining_economics(row, price=row['spot'], quantity_usable=not is_pending))
        confirmed = observations.get('source_history') == 'complete' and not is_pending
        row['quantity_status'] = 'pending' if is_pending else 'confirmed' if confirmed else 'ledger_only'
        if not confirmed:
            row['shares_sold'] = row['shares_remaining'] = None
        if is_pending:
            row['reasons'].append('unresolved_stock_sale_or_source_gap')
        elif not confirmed:
            row['reasons'].append('source_history_not_proven')
        row['strike'] = events.get(row['source_assignment_event_id'], {}).get('strike')
        rows.append(row)
    summary = summarize_lots(rows, empty_is_zero=not inputs.diagnostics and not reviews and not source_gaps and not pending)
    summary['cny_estimate'] = _fx_summary(summary, fx, valuation_at)
    accounts = []
    for account in sorted({r['account'] for r in rows}):
        account_rows = [r for r in rows if r['account'] == account]
        symbols = []
        for symbol in sorted({r['symbol'] for r in account_rows}):
            symbol_rows = [r for r in account_rows if r['symbol'] == symbol]
            symbols.append({'symbol': symbol, 'lots': symbol_rows, 'summary': summarize_lots(symbol_rows)})
        account_summary = summarize_lots(account_rows)
        account_summary['cny_estimate'] = _fx_summary(account_summary, fx, valuation_at)
        accounts.append({'account': account, 'symbols': symbols, 'summary': account_summary})
    return {'schema_version': 'option_positions_read.assignment_period.v1', 'action': 'assigned-stock',
        'query': {**period, 'request_at_ms': request_at, 'ledger_state_cutoff_ms': request_at,
                  'valuation_at_ms': valuation_at, 'accounts': sorted(allowed_accounts),
                  'symbol': payload.get('symbol')}, 'rows': rows, 'assigned_stock_lots': rows,
        'row_count': len(rows), 'matched_count': len(rows) if not inputs.diagnostics and (rows or not (reviews or source_gaps or pending)) else None,
        'summary': summary, 'accounts': accounts, 'source': {**raw.get('source', {}),
            'intake': {k: v for k, v in observations.items() if k != 'observations'}},
        'unresolved_stock_sales': [{k: o.get(k) for k in ('account', 'symbol', 'shares', 'trade_time_ms', 'source_deal_id')} for o in pending],
        'assigned_stock_sale_rows': [r for r in base.get('assigned_stock_sale_rows', [])
            if r.get('stock_lot_id') in {lot['stock_lot_id'] for lot in rows}],
        'scope_reviews': reviews, 'assigned_stock_review_rows': reviews, 'quote_refresh': refresh,
        'coverage': {'status': 'partial' if inputs.diagnostics or observations.get('source_history') != 'complete'
                     or pending else 'complete', 'ledger_read': 'gap' if inputs.diagnostics else 'complete',
                     'source_history': observations.get('source_history', 'unknown')},
        'pagination': {'has_more': False, 'source': 'full_snapshot'},
        'freshness': {'status': 'observed', 'ledger_read_at_ms': raw.get('source', {}).get('read_at_ms'),
                      'ledger_state_cutoff_ms': request_at, 'valuation_at_ms': valuation_at,
                      'quote_refresh': refresh},
        'warnings': ['账本口径估算；历史来源未完全核实'] if observations.get('source_history') != 'complete' else []}
