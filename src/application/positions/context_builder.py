#!/usr/bin/env python3
from __future__ import annotations

from src.application.runtime_paths import resolve_runtime_root
from src.infrastructure.exchange_rates import shared_exchange_rate_cache_path

from pathlib import Path

import argparse
from typing import Any

from domain.domain.ledger.position_fields import (
    normalize_account,
    normalize_broker,
)
from domain.domain.portfolio_scope import portfolio_scope_id
from src.infrastructure.io_utils import atomic_write_json
from src.application.ledger.api import (
    decision_state_snapshot,
    resolve_position_lot_snapshots,
)

from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest

JsonDict = dict[str, Any]

from src.application.ledger.api import (
    validate_option_positions_context_account as validate_option_positions_context_account,
    build_position_risk_context as build_context,
    build_shared_position_risk_context as build_shared_context,
    build_lifecycle_read_models_from_decision_snapshot as build_lifecycle_read_models_from_decision_snapshot,
    slice_position_risk_context_for_account as slice_shared_context_for_account,
    OPTION_CONTEXT_STRATEGY_FAMILY_SOURCE as STRATEGY_FAMILY_SOURCE,
)

__all__ = [
    "JsonDict",
    "validate_option_positions_context_account",
    "build_context",
    "build_shared_context",
    "build_lifecycle_read_models_from_decision_snapshot",
    "slice_shared_context_for_account",
    "STRATEGY_FAMILY_SOURCE",
    "main",
]


def main():
    parser = argparse.ArgumentParser(description="Fetch projected position lot context")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    parser.add_argument("--broker", default="富途")
    parser.add_argument("--account", default=None, help="可信经济上下文所需账户；省略时输出不可用状态")
    parser.add_argument("--shared-out", default=None, help="Optional output path for shared context cache")
    parser.add_argument("--out", default=None, help="Output JSON path (default: <state-dir>/option_positions_context.json)")
    parser.add_argument("--state-dir", default="output_shared/state", help="Directory for outputs (default: output_shared/state)")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout (scheduled/cron)")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[3]
    _data_config_path, _repo, records = resolve_position_lot_snapshots(base=base, data_config=args.data_config)
    # Load exchange rates for base-currency normalization (CNY).
    # Uses current-project cache plus live refresh when needed.
    base = Path(__file__).resolve().parents[3]
    # Resolve output path/state_dir
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = (base / out_path).resolve()
        state_dir = out_path.parent
    else:
        sd = Path(args.state_dir)
        if not sd.is_absolute():
            sd = (base / sd).resolve()
        sd.mkdir(parents=True, exist_ok=True)
        state_dir = sd
        out_path = (state_dir / 'option_positions_context.json').resolve()

    # Output location does not select a different FX cache.

    rates = get_exchange_rates_or_fetch_latest(
        cache_path=shared_exchange_rate_cache_path(resolve_runtime_root(repo_root=base).runtime_root),
        max_age_hours=24,
    )
    broker = normalize_broker(args.broker)

    account = normalize_account(args.account) if args.account else None
    # This CLI has no configured aggregate scope. Only an explicit account can
    # obtain economic context; raw lots remain available through inspection.
    snapshot = decision_state_snapshot(
        _repo, account=account, portfolio_scope_id=portfolio_scope_id(account),
    ) if account else {}
    ctx = build_context(
        records, broker=broker, account=account, rates=rates, decision_snapshot=snapshot,
    )

    atomic_write_json(out_path, ctx)
    if args.shared_out:
        shared_out = Path(args.shared_out)
        if not shared_out.is_absolute():
            shared_out = (base / shared_out).resolve()
        atomic_write_json(shared_out, build_shared_context(
            [], broker=broker, rates=rates,
            decision_snapshots_by_account={account: snapshot} if account else {},
        ))

    if not args.quiet:
        print(f"[DONE] option positions context -> {out_path}")
        print(f"broker={broker} account={args.account or '-'} selected={ctx['raw_selected_count']}")

        # Keep summary stats best-effort because old records may miss optional fields.
        cash_secured_syms = 0
        try:
            m = ctx.get('cash_secured_by_symbol_by_ccy') or {}
            cash_secured_syms = len(m)
        except Exception:
            cash_secured_syms = 0

        print(f"locked_symbols={len(ctx.get('locked_shares_by_symbol') or {})} cash_secured_symbols={cash_secured_syms}")


if __name__ == "__main__":
    main()
