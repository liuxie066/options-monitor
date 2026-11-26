"""Current, read-only assignment cohorts; financial facts remain with their owners."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.assigned_stock import assigned_put_remaining_economics
from domain.domain.trade_contract_identity import derive_position_side
from domain.domain.option_position_identity import normalize_broker
from src.application.agent_tool_contracts import AgentToolError
from src.application.account_config import resolve_configured_accounts
from src.application.runtime_paths import resolve_runtime_root
from src.application.ledger import api as ledger_api
from src.application.performance.adapters import ledger_performance_inputs_from_rows
from src.application.positions.assigned_stock_view import build_assigned_stock_view
from src.application.positions.assigned_stock_quotes import assigned_stock_quote_is_usable, assigned_stock_quote_unavailable_reason
from src.application.trades.account_mapping import resolve_futu_account_mapping
from src.application.trades.inbox import read_stock_sell_observations
from src.infrastructure.exchange_rates import current_exchange_rate_snapshot, project_exchange_rate_snapshot

_PERIOD_KEYS = ('assignment_year', 'assignment_month', 'assignment_start_date', 'assignment_end_date')
_SHANGHAI = ZoneInfo('Asia/Shanghai')
_METRICS = ('stock_unrealized_pnl', 'stock_realized_pnl')


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
                'basis': 'unavailable' if not included else 'ledger' if provisional else 'confirmed',
                'included_lot_ids': [r['stock_lot_id'] for r in included],
                'provisional_lot_ids': [r['stock_lot_id'] for r in provisional],
                'omitted_lot_ids': [r['stock_lot_id'] for r in omitted],
                'omitted_reasons': {r['stock_lot_id']: r['reasons'] for r in omitted},
            }
    return {'by_currency': result, 'empty_value': '0.00' if not lots and empty_is_zero else None}


def _affected(row: dict, observation: dict) -> bool:
    if row.get('stock_lot_id') in observation.get('accepted_sale_conflict_lot_ids', []):
        return True
    if "candidate_stock_lot_ids" in observation:
        return row.get("stock_lot_id") in observation["candidate_stock_lot_ids"]
    if (observation.get('broker') and normalize_broker(observation['broker']) != normalize_broker(row.get('broker'))):
        return False
    if any(observation.get(k) and observation[k] != row.get(k) for k in ('account', 'symbol', 'currency')):
        return False
    instant = observation.get('trade_time_ms')
    return instant is None or int(row['assigned_at_ms']) <= int(instant)


def _quote_scope_matches(quote: dict, row: dict) -> bool:
    return (not quote.get('account') or str(quote['account']).lower() == row['account']) and (
        not quote.get('broker') or normalize_broker(quote['broker']) == normalize_broker(row['broker']))


def _period_leaf(row: dict[str, Any]) -> dict[str, Any]:
    """Expose one coherent period result, without unrelated lifecycle/CC totals."""
    fields = {
        'stock_lot_id', 'source_assignment_event_id', 'source_option_lot_id',
        'account', 'broker', 'symbol', 'currency', 'strategy', 'strategy_group_id',
        'source_option_leg_role', 'structure_mode', 'expiry_structure', 'strategy_snapshot',
        'assigned_at_ms', 'assigned_date', 'opened_at_ms', 'shares_opened', 'shares_sold',
        'shares_remaining', 'book_shares_sold', 'book_shares_remaining', 'quantity_status',
        'status', 'strike', 'assignment_price', 'stock_cost_basis_total',
        'sale_event_ids', 'fee_evidence',
        'stock_unrealized_pnl', 'stock_realized_pnl', 'remaining_stock_cost_basis_exact',
        'spot', 'quote', 'reasons', 'valuation_basis', 'confirmed', 'provisional',
    }
    result = {key: value for key, value in row.items() if key in fields}
    # Lifecycle dates use the exchange timezone; period selection and display
    # share Asia/Shanghai without changing the underlying assignment instant.
    result['assigned_date'] = datetime.fromtimestamp(row['assigned_at_ms'] / 1000, _SHANGHAI).date().isoformat()
    quote = row['quote']
    remaining = row['book_shares_remaining']
    pending = row['quantity_status'] in {'pending', 'unknown'}
    result['quote_status'] = (
        'not_required' if remaining == 0 and not pending else
        ('closed_market_carry' if (quote.get('market_evidence') or {}).get('closed_market_carry') else 'fresh') if quote else 'missing_quote'
    )
    result['spot_time'] = quote.get('quote_time_ms')
    result['quote_source'] = quote.get('quote_source') or quote.get('source')
    result['review_status'] = (
        'pending' if pending else 'ready' if all(row.get(k) is not None for k in _METRICS) else
        'partial' if row['stock_unrealized_pnl'] is not None else
        'missing_quote' if remaining and not quote else 'incomplete_actual_fees'
    )
    # The legacy alias, when retained in this versioned result, names the same
    # qualified amount. Never publish the pre-price projection's stale None.
    result['assigned_stock_unrealized_pnl'] = row['stock_unrealized_pnl']
    result['book_remaining_stock_cost_basis_exact'] = row['remaining_stock_cost_basis_exact']
    if pending:
        result['remaining_stock_cost_basis_exact'] = None
    result['fee_evidence'] = [fact for fact in row.get('fee_evidence', [])
        if fact.get('component') == 'assignment_stock_fee'
        or str(fact.get('component', '')).startswith('stock_sale')]
    return result


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
                # Instrument is economic content, not execution identity. Read
                # the authorized accounts before filtering a requested symbol:
                # a source revision may contradict this symbol's accepted sale.
                symbols=None, repo_base=root,
                runtime_root=quote_state_base_dir or resolve_runtime_root(repo_root=root).runtime_root)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        observations['diagnostics'] = [{'code': 'stock_sell_source_unavailable', 'message': type(exc).__name__}]
    qualified_observations = ledger_api.qualify_assigned_stock_sell_observations(
        raw, observations['observations'], as_of_ms=request_at,
    )
    selected_ids = {r['stock_lot_id'] for r in selected}
    pending = [o for o in qualified_observations
               if (o.get('accepted_sale_conflict') or o.get('trade_time_ms') is None or o['trade_time_ms'] <= request_at)
               and not o['resolved']
               and (selected_ids.intersection(o.get('accepted_sale_conflict_lot_ids', []))
                    or not payload.get('symbol') or o.get('symbol') in (None, payload['symbol'].upper()))]
    reviews = [r for r in base.get('assigned_stock_review_rows', [])
               if r.get('status') != 'missing_quote' and not str(r.get('status', '')).startswith('covered_call_')
               and r.get('account') in allowed_accounts
               and (not payload.get('symbol') or r.get('symbol') == payload['symbol'].upper())]
    for review in reviews:
        source_event = events.get(review.get('event_id'), {})
        review['trade_time_ms'] = source_event.get('trade_time_ms') or source_event.get('event_time_ms')

    # Identity failures retain their scoped economic observation and are
    # qualified at its disposal time. Do not also turn them into an account-wide
    # timeless read failure after qualification.
    observed_ids = {o.get('source_id') for o in qualified_observations}
    diagnostics = observations.get('diagnostics') or []
    source_gaps = [d for d in diagnostics if not (
        d.get('code') == 'stock_sell_identity_invalid' and d.get('source_id') in observed_ids)]
    if not diagnostics and observations['coverage'] != 'complete':
        source_gaps = [{}]
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
                    base_dir=root, state_base_dir=quote_state_base_dir, preserve_evidence=True,
                    host=payload.get('opend_host'), port=payload.get('opend_port'))
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
                     and assigned_stock_quote_is_usable(q, valuation_at, expected_currency=row['currency'])
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
        row['valuation_basis'] = 'unavailable' if is_pending else 'confirmed' if confirmed else 'ledger'
        values = {key: row.get(key) for key in (
            'shares_sold', 'shares_remaining', *_METRICS)}
        row['confirmed'] = values if confirmed else {key: None for key in values}
        row['provisional'] = ({**values, 'shares_sold': row['book_shares_sold'],
            'shares_remaining': row['book_shares_remaining'], 'basis': 'ledger',
            'source_history': observations.get('source_history', 'unknown')}
            if not confirmed and not is_pending else None)
        row['strike'] = events.get(row['source_assignment_event_id'], {}).get('strike')
        rows.append(_period_leaf(row))
    if refresh.get('enabled'):
        # Acquisition may precede FX retrieval and final valuation. Reconcile
        # refresh diagnostics with the marks actually accepted at that time.
        requested = list(dict.fromkeys(r['symbol'] for r in needed))
        accepted = {r['symbol'] for r in rows if r['quote']}
        refresh['received_symbols'] = refresh.get('received_symbols', refresh.get('refreshed_symbols', []))
        refresh['qualified_symbols'] = [s for s in requested if s in accepted]
        refresh['refreshed_symbols'] = list(refresh['qualified_symbols'])
        refresh['missing_symbols'] = [s for s in requested if s not in accepted]
        refresh['valuation_at_ms'] = valuation_at
        refresh['qualification_gaps'] = {}
        for symbol in refresh['missing_symbols']:
            failures = {assigned_stock_quote_unavailable_reason(q, valuation_at) or 'quote_scope_mismatch'
                        for q in quote_rows if q.get('symbol') == symbol}
            failures.update(e.get('message') or e.get('error_code') for e in refresh.get('errors', [])
                            if e.get('code') == symbol and (e.get('message') or e.get('error_code')))
            refresh['qualification_gaps'][symbol] = sorted(failures or {'qualified_quote_missing'})
        if refresh.get('status') not in {'source_error', 'source_unavailable'}:
            refresh['status'] = ('partial' if refresh['missing_symbols'] else 'ok') if refresh['qualified_symbols'] else 'missing_quote'
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
    return {'schema_version': 'option_positions_read.assignment_period.v2', 'action': 'assigned-stock',
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
                     or pending or source_gaps or reviews or any(r[k] is None for r in rows for k in _METRICS)
                     else 'complete', 'ledger_read': 'gap' if inputs.diagnostics else 'complete',
                     'source_history': observations.get('source_history', 'unknown')},
        'pagination': {'has_more': False, 'source': 'full_snapshot'},
        'freshness': {'status': 'observed', 'ledger_read_at_ms': raw.get('source', {}).get('read_at_ms'),
                      'ledger_state_cutoff_ms': request_at, 'valuation_at_ms': valuation_at,
                      'quote_refresh': refresh},
        'warnings': ['账本口径估算；历史来源未完全核实'] if observations.get('source_history') != 'complete' else []}
