#!/usr/bin/env python3
from __future__ import annotations

"""Pipeline context loading (portfolio/position-lots/exchange rates).

Stage 3 refactor target:
- keep unified scan entrypoint thin (orchestration only)
- move context fetch/caching logic into cohesive module

Design constraints:
- minimal/no behavior change
- best-effort context (should not fail the whole pipeline in scheduled mode)
"""

from pathlib import Path
from typing import Mapping

from src.application.runtime_paths import resolve_runtime_root
from src.application.account_config import accounts_from_config, build_account_portfolio_source_plan
from src.application.config_loader import resolve_data_config_path
from src.application.positions.context_builder import (
    STRATEGY_FAMILY_SOURCE,
    build_context as build_option_positions_context,
    build_shared_context as build_shared_option_positions_context,
    validate_option_positions_context_account,
)
from src.application.futu_portfolio_context import fetch_futu_portfolio_context
from src.infrastructure.io_utils import atomic_write_json, is_fresh, load_cached_json
from src.application.ledger.api import (
    decision_state_snapshot,
    list_position_lot_snapshots,
    position_lot_risk_view,
    open_position_ledger,
)
from domain.domain.portfolio_scope import portfolio_scope_id
from src.application.portfolio_context_service import (
    load_account_portfolio_context,
    with_context_source,
)
from src.application.prepared_portfolio_context import (
    PreparedPortfolioContextError,
    load_prepared_portfolio_context,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
    exchange_rate_scalars_from_option_context,
    load_prepared_option_positions_context,
)
from src.application.current_fx_run import load_run_fx_snapshot
from src.infrastructure.exchange_rates import (
    shared_exchange_rate_cache_path,
    exchange_rate_observation_status,
    get_exchange_rates_or_fetch_latest,
    project_exchange_rate_snapshot,
)
from domain.services import adapt_holdings_context, adapt_option_positions_context
from src.application.positions.context_builder import slice_shared_context_for_account as slice_shared_option_context_for_account
from domain.storage.repositories import state_repo


def _persist_source_snapshot(base: Path, snapshot: dict) -> None:
    try:
        state_repo.append_source_snapshot_event(base, snapshot)
    except Exception:
        pass


def _load_option_position_records(data_config: str) -> tuple[object, list[dict]]:
    repo = open_position_ledger(Path(data_config))
    return repo, list(list_position_lot_snapshots(repo))


def _decision_snapshots_for_records(
    repo: object,
    records: list[dict],
    *,
    accounts: tuple[str, ...] = (),
) -> dict[str, dict]:
    accounts = sorted(
        {str(account).strip().lower() for account in accounts if str(account).strip()} | {
            account
            for item in records
            if isinstance(item, dict)
            and (account := position_lot_risk_view(item).account)
        }
    )
    return {
        account: decision_state_snapshot(
            repo,
            account=account,
            portfolio_scope_id=portfolio_scope_id(account),
        )
        for account in accounts
    }


_PORTFOLIO_FX_NOT_PROVIDED = object()


def load_portfolio_context(
    *,
    data_config: str,
    market: str,
    account: str | None,
    base: Path,
    state_dir: Path,
    shared_state_dir: Path | None,
    log,
    runtime_config: dict | None = None,
    portfolio_source: str | None = None,
    exchange_rate_observation: Mapping | None | object = _PORTFOLIO_FX_NOT_PROVIDED,
) -> dict | None:
    """Best-effort load portfolio context to dict."""
    try:
        ctx = load_account_portfolio_context(
            market=market,
            account=account,
            state_dir=state_dir,
            log=log,
            runtime_config=runtime_config,
            portfolio_source=portfolio_source,
            fetch_futu_portfolio_context_fn=fetch_futu_portfolio_context,
            exchange_rate_cache_path=(shared_state_dir / "rate_cache.json" if shared_state_dir else shared_exchange_rate_cache_path(resolve_runtime_root(repo_root=base).runtime_root)),
            **({"exchange_rate_observation": exchange_rate_observation} if exchange_rate_observation is not _PORTFOLIO_FX_NOT_PROVIDED else {}),
            load_json_fn=load_cached_json,
        )
        if isinstance(ctx.get("cash_by_currency"), dict) and isinstance(ctx.get("stocks_by_symbol"), dict):
            snap = adapt_holdings_context(ctx)
            _persist_source_snapshot(base, snap)
        return ctx
    except Exception as e:
        log(f"[WARN] portfolio context not available: {e}")
        return None


def load_option_positions_context(
    *,
    base: Path,
    data_config: str,
    market: str,
    account: str | None,
    ttl_sec: int,
    state_dir: Path,
    shared_state_dir: Path | None,
    log,
    exchange_rate_observation: Mapping | None = None,
    runtime_config: dict | None = None,
) -> tuple[dict | None, bool]:
    """Best-effort load position-lot context.

    Returns (context, refreshed).
    """
    try:
        requested_accounts = (account,) if account else tuple(accounts_from_config(runtime_config, fallback=()))
        if not requested_accounts:
            raise ValueError("aggregate option context requires configured account scope")
        current_decision_snapshot: dict | None = None

        def _is_exact_account(context: dict, *, source: str) -> bool:
            nonlocal current_decision_snapshot
            try:
                validate_option_positions_context_account(
                    context,
                    account=account,
                    broker=market,
                )
                # The strategy family reaches ``combo_yield_groups`` from the
                # event layer now (``write-side-definition.md`` §2), so a context
                # cached before that re-pointing holds groups built from an empty
                # family. The version travels in the payload; reject the earlier
                # shape rather than serving its stale groups until the TTL lapses.
                if context.get("strategy_family_source") != STRATEGY_FAMILY_SOURCE:
                    raise ValueError(
                        "context predates the event-layer strategy family "
                        "(strategy_family_source="
                        f"{context.get('strategy_family_source')!r}, expected "
                        f"{STRATEGY_FAMILY_SOURCE!r})"
                    )
                if source in {"account_cache", "shared_slice"}:
                    if not account:
                        # Aggregate contexts have no single decision snapshot;
                        # rebuild through the per-account decision owners.
                        return False
                    if current_decision_snapshot is None:
                        normalized_account = str(account).strip().lower()
                        current_decision_snapshot = decision_state_snapshot(
                            open_position_ledger(Path(data_config)),
                            account=normalized_account,
                            portfolio_scope_id=portfolio_scope_id(normalized_account),
                        )
                    if (
                        current_decision_snapshot.get("snapshot_status") != "trusted"
                        or context.get("decision_snapshot_status") != "trusted"
                    ):
                        raise ValueError("cached option context has no trusted current decision snapshot")
                return True
            except ValueError as exc:
                log(
                    "[WARN] option_positions_context rejected "
                    f"source={source}: {exc}"
                )
                return False

        opt_path = (state_dir / 'option_positions_context.json').resolve()
        cached = None
        if ttl_sec > 0 and is_fresh(opt_path, ttl_sec):
            cached = load_cached_json(opt_path)
        if isinstance(cached, dict) and _is_exact_account(
            cached,
            source="account_cache",
        ):
            cached = with_context_source(cached, 'account_cache')
            log(f"[CTX] option_positions_context source=account_cache account={account or '-'}")
            snap = adapt_option_positions_context(cached)
            _persist_source_snapshot(base, snap)
            return cached, False

        shared_root = (shared_state_dir or state_dir).resolve()
        shared_root.mkdir(parents=True, exist_ok=True)
        shared_path = (shared_root / 'option_positions_context.shared.json').resolve()

        # Reuse shared cache first; this keeps per-account output schema unchanged.
        try:
            if ttl_sec > 0 and is_fresh(shared_path, ttl_sec):
                shared_cached = load_cached_json(shared_path)
                if isinstance(shared_cached, dict):
                    sliced = slice_shared_option_context_for_account(shared_cached, account)
                    if isinstance(sliced, dict) and _is_exact_account(
                        sliced,
                        source="shared_slice",
                    ):
                        sliced = with_context_source(sliced, 'shared_slice')
                        opt_path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(opt_path, sliced)
                        log(f"[CTX] option_positions_context source=shared_slice account={account or '-'}")
                        snap = adapt_option_positions_context(sliced)
                        _persist_source_snapshot(base, snap)
                        # Keep existing semantics: account-level context was refreshed for this run.
                        return sliced, True
        except Exception:
            pass

        # Refresh shared cache (single fetch) and produce account context in one command.
        try:
            _repo, records = _load_option_position_records(data_config)
            rates = (exchange_rate_observation if exchange_rate_observation is not None
                     else _load_option_position_exchange_rates(base=base, state_dir=shared_root, log=log))
            decision_snapshots = _decision_snapshots_for_records(
                _repo,
                records,
                accounts=requested_accounts,
            )
            shared_ctx = build_shared_option_positions_context(
                records,
                broker=str(market),
                rates=rates,
                decision_snapshots_by_account=decision_snapshots,
            )
            for snapshot_account, snapshot in decision_snapshots.items():
                account_context = (shared_ctx.get("by_account") or {}).get(
                    snapshot_account
                )
                if isinstance(account_context, dict):
                    account_context["current_decision_shadow"] = dict(
                        snapshot["current_decision_shadow"]
                    )
            ctx = dict(slice_shared_option_context_for_account(shared_ctx, account) or {})
            if not _is_exact_account(ctx, source="shared_refresh"):
                raise ValueError("shared option context account validation failed")
            atomic_write_json(shared_path, shared_ctx)
            ctx = with_context_source(ctx, 'shared_refresh')
            atomic_write_json(opt_path, ctx)
            log(f"[CTX] option_positions_context source=shared_refresh account={account or '-'}")
            snap = adapt_option_positions_context(ctx)
            _persist_source_snapshot(base, snap)
            return ctx, True
        except Exception:
            pass

        # Fallback: direct per-account fetch path.
        if not account:
            raise ValueError("aggregate option context requires per-account decision snapshots")
        _repo, records = _load_option_position_records(data_config)
        rates = (exchange_rate_observation if exchange_rate_observation is not None
                 else _load_option_position_exchange_rates(base=base, state_dir=shared_root, log=log))
        normalized_account = str(account or "").strip().lower()
        decision_snapshot = (
            decision_state_snapshot(
                _repo,
                account=normalized_account,
                portfolio_scope_id=portfolio_scope_id(normalized_account),
            )
            if normalized_account
            else None
        )
        ctx = build_option_positions_context(
            records,
            broker=str(market),
            account=account,
            rates=rates,
            decision_snapshot=decision_snapshot,
        )
        if decision_snapshot is not None:
            ctx["current_decision_shadow"] = dict(
                decision_snapshot["current_decision_shadow"]
            )
        if not _is_exact_account(ctx, source="direct_fetch"):
            raise ValueError("direct option context account validation failed")
        ctx = with_context_source(ctx, 'direct_fetch')
        atomic_write_json(opt_path, ctx)
        log(f"[CTX] option_positions_context source=direct_fetch account={account or '-'}")
        snap = adapt_option_positions_context(ctx)
        _persist_source_snapshot(base, snap)
        return ctx, True
    except Exception as e:
        log(f"[WARN] option positions context not available: {e}")
        return None, False


def _load_option_position_exchange_rates(*, base: Path, state_dir: Path, log) -> dict | None:
    try:
        from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest

        return get_exchange_rates_or_fetch_latest(
            cache_path=shared_exchange_rate_cache_path(resolve_runtime_root(repo_root=base).runtime_root),
            max_age_hours=24,
        )
    except Exception as exc:
        log(f"[WARN] option position exchange rates not available: {exc}")
        return None


def load_exchange_rates(
    *,
    base: Path,
    state_dir: Path,
    log,
    shared_state_dir: Path | None = None,
    status_out: dict[str, str] | None = None,
    exchange_rate_observation: Mapping | None = None,
) -> tuple[float | None, float | None]:
    """Best-effort exchange-rate loader.

    Use the shared infrastructure exchange-rate helper so cache miss behavior
    stays consistent with other entrypoints.
    """
    usd_per_cny_exchange_rate = None
    cny_per_hkd_exchange_rate = None
    if status_out is not None:
        status_out["status"] = "unavailable"
    try:
        rates_obj = exchange_rate_observation if exchange_rate_observation is not None else get_exchange_rates_or_fetch_latest(
            cache_path=(
                (shared_state_dir / "rate_cache.json" if shared_state_dir else shared_exchange_rate_cache_path(resolve_runtime_root(repo_root=base).runtime_root))
            ).resolve(),
            max_age_hours=24,
            log=log,
        )
        if exchange_rate_observation is not None:
            rates_obj = project_exchange_rate_snapshot(exchange_rate_observation, purpose="capacity")
        rates_map = rates_obj.get('rates') if isinstance(rates_obj, dict) and isinstance(rates_obj.get('rates'), dict) else rates_obj
        if isinstance(rates_map, dict):
            try:
                usdcny = rates_map.get('USDCNY')
                usdcny = float(usdcny) if usdcny else None
            except Exception:
                usdcny = None
            try:
                cny_per_hkd_rate_value = rates_map.get('HKDCNY')
                cny_per_hkd_exchange_rate = float(cny_per_hkd_rate_value) if cny_per_hkd_rate_value else None
            except Exception:
                cny_per_hkd_exchange_rate = None
            if usdcny and usdcny > 0:
                usd_per_cny_exchange_rate = 1.0 / usdcny
            if status_out is not None:
                status_out["status"] = exchange_rate_observation_status(rates_obj)
    except Exception as e:
        log(f"[WARN] exchange rates not available: {e}")
    return usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate


def build_pipeline_context(
    *,
    py: str,
    base: Path,
    cfg: dict,
    report_dir: Path,
    portfolio_timeout_sec: int,
    runtime: dict,
    is_scheduled: bool,
    state_dir: Path,
    shared_state_dir: Path | None = None,
    log,
    no_context: bool,
    want_scan: bool,
    market_data_only: bool = False,
    prepared_portfolio_context_manifest: Path | None = None,
    prepared_portfolio_context_run_id: str | None = None,
    prepared_portfolio_context_account_config_sha256: str | None = None,
    prepared_portfolio_context_manifest_sha256: str | None = None,
    prepared_option_positions_context_manifest: Path | None = None,
    prepared_option_positions_context_run_id: str | None = None,
    prepared_option_positions_context_account_config_sha256: str | None = None,
    prepared_option_positions_context_manifest_sha256: str | None = None,
) -> tuple[dict | None, dict | None, float | None, float | None]:
    """Load portfolio_ctx, option_ctx, usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate."""
    if not want_scan:
        return None, None, None, None
    if market_data_only:
        usd_per_cny, cny_per_hkd = load_exchange_rates(
            base=base,
            state_dir=state_dir,
            shared_state_dir=shared_state_dir,
            log=log,
        )
        return None, None, usd_per_cny, cny_per_hkd
    if bool(no_context):
        return None, None, None, None

    portfolio_cfg = cfg.get('portfolio', {}) or {}
    data_config = resolve_data_config_path(base=base, data_config=portfolio_cfg.get('data_config'))
    broker = portfolio_cfg.get('broker') or '富途'
    account = portfolio_cfg.get('account')
    portfolio_source = build_account_portfolio_source_plan(
        cfg,
        account=(str(account) if account else None),
    ).requested_source

    # Cache policy (TTL seconds)
    ttl_opt_ctx = int(runtime.get('option_positions_context_ttl_sec', 900 if is_scheduled else 120) or 0)
    direct_fx: Mapping | None = None
    if prepared_portfolio_context_manifest is None and prepared_option_positions_context_manifest is None:
        try:
            direct_fx = get_exchange_rates_or_fetch_latest(
                cache_path=((shared_state_dir / "rate_cache.json" if shared_state_dir else shared_exchange_rate_cache_path(resolve_runtime_root(repo_root=base).runtime_root))).resolve(),
                max_age_hours=24,
                log=log,
            ) or {}
        except Exception as exc:
            log(f"[WARN] exchange rates not available: {exc}")
            direct_fx = {}

    if prepared_portfolio_context_manifest is not None:
        try:
            portfolio_ctx = load_prepared_portfolio_context(
                manifest_path=prepared_portfolio_context_manifest,
                expected_base=base,
                expected_run_id=str(prepared_portfolio_context_run_id or ""),
                expected_account=str(account or ""),
                expected_account_config_sha256=str(
                    prepared_portfolio_context_account_config_sha256 or ""
                ),
                expected_manifest_sha256=str(
                    prepared_portfolio_context_manifest_sha256 or ""
                ),
                expected_runtime_config=cfg,
            )
            source = "prepared" if portfolio_ctx is not None else "prepared_unavailable"
            log(f"[CTX] portfolio_context source={source} account={account or '-'}")
        except PreparedPortfolioContextError as exc:
            log(f"[WARN] prepared portfolio context not available: {exc}")
            raise
    else:
        portfolio_ctx = load_portfolio_context(
            base=base,
            data_config=str(data_config),
            market=str(broker),
            account=(str(account) if account else None),
            state_dir=state_dir,
            shared_state_dir=shared_state_dir,
            log=log,
            runtime_config=cfg,
            portfolio_source=str(portfolio_source),
            exchange_rate_observation=direct_fx,
        )

    if prepared_option_positions_context_manifest is not None:
        try:
            option_ctx = load_prepared_option_positions_context(
                manifest_path=(
                    prepared_option_positions_context_manifest
                ),
                expected_base=base,
                expected_run_id=str(
                    prepared_option_positions_context_run_id or ""
                ),
                expected_account=str(account or ""),
                expected_account_config_sha256=str(
                    prepared_option_positions_context_account_config_sha256
                    or ""
                ),
                expected_manifest_sha256=str(
                    prepared_option_positions_context_manifest_sha256 or ""
                ),
                expected_runtime_config=cfg,
            )
            log(
                "[CTX] option_positions_context source=prepared "
                f"account={account or '-'}"
            )
            _persist_source_snapshot(
                base,
                adapt_option_positions_context(option_ctx),
            )
        except PreparedOptionPositionsContextError as exc:
            log(f"[WARN] prepared option context not available: {exc}")
            raise
    else:
        option_ctx, _ = load_option_positions_context(
            base=base,
            data_config=str(data_config),
            market=str(broker),
            account=(str(account) if account else None),
            ttl_sec=ttl_opt_ctx,
            state_dir=state_dir,
            shared_state_dir=shared_state_dir,
            log=log,
            exchange_rate_observation=direct_fx,
            runtime_config=cfg,
        )

    if direct_fx is not None:
        if isinstance(portfolio_ctx, dict) and "exchange_rates" in portfolio_ctx:
            portfolio_ctx = dict(portfolio_ctx)
            portfolio_ctx["exchange_rates"] = direct_fx
            portfolio_ctx["exchange_rate_status"] = exchange_rate_observation_status(direct_fx)
        if isinstance(option_ctx, dict) and "exchange_rates" in option_ctx:
            prior = option_ctx.get("exchange_rates")
            prior_rates = prior.get("rates") if isinstance(prior, dict) else None
            current_rates = direct_fx.get("rates") if isinstance(direct_fx.get("rates"), dict) else {}
            secured = option_ctx.get("cash_secured_total_by_ccy")
            required_pairs = {
                {"USD": "USDCNY", "HKD": "HKDCNY"}[ccy]
                for ccy, amount in (secured.items() if isinstance(secured, dict) else ())
                if ccy in {"USD", "HKD"} and amount
            }
            option_ctx = dict(option_ctx)
            option_ctx["exchange_rates"] = direct_fx
            if not isinstance(prior_rates, dict) or any(
                prior_rates.get(pair) != current_rates.get(pair) for pair in required_pairs
            ):
                option_ctx["cash_secured_total_cny"] = None

    if prepared_option_positions_context_manifest is not None:
        fx_authority = option_ctx.get("prepared_authority") if isinstance(option_ctx, dict) else None
        if (
            isinstance(option_ctx, dict)
            and isinstance(portfolio_ctx, dict)
            and (
                portfolio_ctx.get("fx_snapshot_sha256")
                or (fx_authority.get("run_fx_snapshot_sha256") if isinstance(fx_authority, dict) else None)
            )
        ):
            run_id = str(prepared_option_positions_context_run_id or "")
            snapshot, fx_hash = load_run_fx_snapshot(base=base, run_id=run_id)
            authority = option_ctx.get("prepared_authority")
            if (
                portfolio_ctx.get("fx_snapshot_sha256") != fx_hash
                or not isinstance(authority, dict)
                or authority.get("run_fx_snapshot_sha256") != fx_hash
            ):
                raise PreparedOptionPositionsContextError("prepared context FX snapshot mismatch")
            current_fx = project_exchange_rate_snapshot(snapshot, purpose="capacity")
            prior_fx = option_ctx.get("exchange_rates")
            prior_rates = prior_fx.get("rates") if isinstance(prior_fx, dict) else None
            current_rates = current_fx["rates"]
            secured = option_ctx.get("cash_secured_total_by_ccy")
            required_pairs = {
                {"USD": "USDCNY", "HKD": "HKDCNY"}[ccy]
                for ccy, amount in (secured.items() if isinstance(secured, dict) else ())
                if ccy in {"USD", "HKD"} and amount
            }
            option_ctx = dict(option_ctx)
            option_ctx["exchange_rates"] = current_fx
            if not isinstance(prior_rates, dict) or any(
                prior_rates.get(pair) != current_rates.get(pair) for pair in required_pairs
            ):
                option_ctx["cash_secured_total_cny"] = None
            portfolio_ctx = dict(portfolio_ctx)
            portfolio_ctx["exchange_rates"] = current_fx
            portfolio_ctx["exchange_rate_status"] = exchange_rate_observation_status(current_fx)
        usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = (
            exchange_rate_scalars_from_option_context(option_ctx or {})
        )
        prepared_authority = (
            option_ctx.get("prepared_authority")
            if isinstance(option_ctx, dict)
            and isinstance(option_ctx.get("prepared_authority"), dict)
            else {}
        )
        fx_status = exchange_rate_observation_status(
            option_ctx.get("exchange_rates") if isinstance(option_ctx, dict) else None,
        )
    else:
        rate_status: dict[str, str] = {}
        usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = (
            load_exchange_rates(
                base=base,
                state_dir=state_dir,
                shared_state_dir=shared_state_dir,
                log=log,
                status_out=rate_status,
                exchange_rate_observation=direct_fx,
            )
        )
        fx_status = str(rate_status.get("status") or "").strip().lower()

    if fx_status == "unavailable_stale" and isinstance(portfolio_ctx, dict):
        portfolio_ctx = dict(portfolio_ctx)
        portfolio_ctx["_sell_put_fx_status"] = fx_status

    return portfolio_ctx, option_ctx, usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate
