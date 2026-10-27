from __future__ import annotations

from concurrent.futures import CancelledError
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from domain.domain.trade_contract_identity import contract_share_quantity
from domain.domain.wheel import project_wheel_coverage
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.risk_capacity import (
    allocate_opening_share_capacity,
    allocate_wheel_put_cash_capacity,
    withdraw_opening_share_capacity_grants,
)
from src.application.futu_portfolio_context import (
    fetch_futu_portfolio_context, infer_futu_portfolio_settings, resolve_futu_account_ids,
)
from src.application.portfolio_context_service import (
    portfolio_position_snapshot_errors,
    load_account_portfolio_context,
)
from src.application.ledger.api import (read_wheel_capacity_context,
    trade_attribution_capacity_check as trade_attribution_capacity_check)
from src.infrastructure.exchange_rates import (current_exchange_rate_snapshot, project_exchange_rate_snapshot)


from src.application.ledger.api import (
    broker_capacity_observation_is_fresh as broker_capacity_observation_is_fresh,
    build_shared_cash_capacity_fact as build_shared_cash_capacity_fact,
    revalidate_selected_wheel_put_candidate as revalidate_selected_wheel_put_candidate,
    revalidate_selected_wheel_put_candidate_from_rows as revalidate_selected_wheel_put_candidate_from_rows,
    normalize_wheel_fx_snapshot as _normalized_fx_snapshot,
    wheel_ordinary_put_cash_claims as _ordinary_put_cash_claims,
    wheel_capacity_currency_converter as _convert_currency_fn,
    WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA as WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA,
)


def _read_ordinary_attribution_broker_evidence(*, config: Mapping[str, Any], account: str,
                                               runtime_root: Path, start_ms: int) -> dict[str, Any]:
    from src.infrastructure.futu_history_deals import OpenDHistoryDealClient
    from src.application.trades.normalizer import normalize_trade_deal

    ids = resolve_futu_account_ids(config, account=account)
    settings = infer_futu_portfolio_settings(config, account=account)
    if len(ids) != 1 or str(settings.get("trd_env") or "").upper() != "REAL":
        raise ValueError("ordinary attribution requires one REAL physical account")
    physical = next(iter(ids))
    scope = {"broker_id": "futu", "external_account_id": physical, "environment": "REAL", "account": account}
    client = OpenDHistoryDealClient(host=str(settings.get("host") or "127.0.0.1"),
                                   port=int(settings.get("port") or 11111))
    try:
        evidence = client.fetch_attribution_evidence(futu_account_id=physical, start_ms=start_ms)
        evidence["executions"] = [normalize_trade_deal(payload,
            futu_account_mapping={physical: account}, repo_base=runtime_root, runtime_root=runtime_root,
            config=dict(config), allow_opend_refresh=False).execution_input for payload in evidence.pop("deals")]
        evidence["scope"] = scope
        for name in ("current_orders", "history_orders"):
            evidence[name]["scope"] = scope
        return evidence
    finally:
        client.close()


def _attribution_capacity_worker(connection: Any, config: dict[str, Any], account: str, runtime_root: Path,
                                 ordinary_history_start_ms: int | None = None) -> None:
    try:
        cache_path = runtime_root / "output_shared" / "state" / "rate_cache.json"
        try:
            fx = project_exchange_rate_snapshot(current_exchange_rate_snapshot(cache_path=cache_path, write_cache=False), purpose="capacity")
        except (TimeoutError, CancelledError):
            raise
        except Exception:
            fx = None
        portfolio_args = dict(
            runtime_config=config, account=account, market="富途", portfolio_source="futu",
            state_dir=runtime_root / "output_accounts" / account / "state", log=lambda _: None,
            fetch_futu_portfolio_context_fn=fetch_futu_portfolio_context,
            include_options=True, write_cache=False, exchange_rate_observation=fx,
            exchange_rate_cache_path=cache_path,
        )
        portfolio = load_account_portfolio_context(**portfolio_args)
        connection.send({"portfolio": portfolio})
        if ordinary_history_start_ms is None:
            return
        try:
            ordinary = _read_ordinary_attribution_broker_evidence(config=config, account=account,
                runtime_root=runtime_root, start_ms=ordinary_history_start_ms)
        except Exception as exc:
            ordinary = {"error": type(exc).__name__}
        if ordinary and not ordinary.get("error"):
            portfolio = load_account_portfolio_context(**portfolio_args, load_json_fn=lambda _: None)
        connection.send({"portfolio": portfolio, "ordinary_evidence": ordinary})
    except Exception as exc:
        connection.send({"error": type(exc).__name__})
    finally:
        connection.close()


def observe_trade_attribution_capacity(*, config: dict[str, Any], account: str, runtime_root: Path,
                                       stop_event: Any = None, ordinary_history_start_ms: int | None = None) -> dict[str, Any]:
    """One physical-account observation, with a cancellable ten-second I/O budget."""
    import multiprocessing
    import time

    if stop_event is not None and stop_event.is_set():
        return {"error": "cancelled"}
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    args = (sender, config, account, Path(runtime_root).resolve())
    if ordinary_history_start_ms is not None:
        args += (ordinary_history_start_ms,)
    process = context.Process(target=_attribution_capacity_worker, args=args, daemon=True)
    deadline = time.monotonic() + 10
    base_result = None

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        if stop_event is not None and stop_event.is_set():
            return {"error": "cancelled"}
        return result

    def unavailable(error: Any) -> dict[str, Any]:
        return finish({**base_result, "ordinary_evidence": {"error": error}}
                      if base_result is not None else {"error": error})

    try:
        process.start()
        sender.close()
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return {"error": "cancelled"}
            if receiver.poll(min(0.1, max(0, deadline - time.monotonic()))):
                result = receiver.recv()
                if stop_event is not None and stop_event.is_set():
                    return {"error": "cancelled"}
                if time.monotonic() >= deadline:
                    return unavailable("provider_timeout")
                if not isinstance(result, Mapping):
                    return unavailable("provider_unavailable")
                if "error" in result:
                    return unavailable(result["error"])
                if "portfolio" not in result:
                    return unavailable("provider_unavailable")
                if ordinary_history_start_ms is None or "ordinary_evidence" in result:
                    return finish(dict(result))
                if base_result is not None:
                    return unavailable("provider_unavailable")
                base_result = dict(result)
            if not process.is_alive():
                if receiver.poll(0):
                    continue
                return unavailable("provider_worker_exited")
        return unavailable("provider_timeout")
    except (EOFError, OSError):
        return unavailable("provider_unavailable")
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
            process.close()


def _load_frozen_wheel_capacity_context(
    repo: Any, *, config: dict[str, Any], account: str, broker: str, as_of_ms: int,
    runtime_root: Path, fx_snapshot: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    wheel_model, option_context = read_wheel_capacity_context(
        repo, account=account, broker=broker, as_of_ms=as_of_ms,
    )
    portfolio = load_account_portfolio_context(
        runtime_config=config, account=account, market=broker, portfolio_source="futu",
        state_dir=runtime_root / "output_accounts" / account / "state", log=lambda _: None,
        fetch_futu_portfolio_context_fn=fetch_futu_portfolio_context, write_cache=False,
        exchange_rate_cache_path=runtime_root / "output_shared" / "state" / "rate_cache.json",
        **({"exchange_rate_observation": fx_snapshot} if fx_snapshot is not None else {}),
        required_position_asset_types=("stock",),
    )
    return wheel_model, portfolio, option_context


def load_shared_cash_capacity_fact(
    repo: Any,
    *,
    config: dict[str, Any],
    runtime_root: Path,
    account: str,
    broker: str,
    as_of_ms: int,
    fx_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    try:
        wheel_model, portfolio, option_context = _load_frozen_wheel_capacity_context(
            repo, config=config, account=account_value, broker=broker, as_of_ms=as_of_ms,
            runtime_root=runtime_root, fx_snapshot=fx_snapshot,
        )
        return build_shared_cash_capacity_fact(
            account=account_value,
            portfolio_context=portfolio,
            option_context=option_context,
            wheel_read_model=wheel_model,
            fx_snapshot=fx_snapshot,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}:{exc}"
        return {
            "schema_version": WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA,
            "account": account_value,
            "status": "unavailable",
            "reason": reason,
            "cash_authority": {},
            "cash_authority_hash": canonical_sha256({"reason": reason}),
            "cash_by_currency": None,
            "cash_secured_by_currency": None,
            "wheel_intent_reservations": [],
            "fx_snapshot": _normalized_fx_snapshot(fx_snapshot),
            "fx_snapshot_hash": canonical_sha256(_normalized_fx_snapshot(fx_snapshot)),
            "capacity_identity_hash": canonical_sha256(
                {"account": account_value, "reason": reason}
            ),
        }


def load_shared_coverage_fact(
    repo: Any,
    *,
    config: dict[str, Any],
    runtime_root: Path,
    account: str,
    symbol: str,
    broker: str,
    as_of_ms: int,
    source_identity: str = "",
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    symbol_value = str(symbol or "").strip().upper()
    try:
        wheel_model, portfolio, option_context = _load_frozen_wheel_capacity_context(
            repo, config=config, account=account_value, broker=broker, as_of_ms=as_of_ms,
            runtime_root=runtime_root,
        )
        matches = [
            row
            for row in build_shared_coverage_facts(
                account=account_value,
                portfolio_context=portfolio,
                option_context=option_context,
                wheel_read_model=wheel_model,
            )
            if row.get("symbol") == symbol_value
        ]
        if len(matches) != 1:
            raise ValueError("wheel_coverage_fact_not_unique")
        return matches[0]
    except Exception as exc:
        reason = f"{type(exc).__name__}:{exc}"
        return {
            "account": account_value,
            "symbol": symbol_value,
            "status": "unavailable",
            "reason": reason,
            "shares_available_for_cover": 0,
            "capacity_identity_hash": canonical_sha256(
                {
                    "account": account_value,
                    "symbol": symbol_value,
                    "source_identity": str(source_identity or ""),
                    "reason": reason,
                }
            ),
        }


def build_shared_coverage_facts(
    *,
    account: str,
    portfolio_context: Mapping[str, Any],
    option_context: Mapping[str, Any],
    wheel_read_model: Mapping[str, Any],
) -> list[dict[str, Any]]:
    position_errors = portfolio_position_snapshot_errors(portfolio_context, account=account)
    position_evidence = deepcopy({key: portfolio_context.get(key) for key in (
        "position_snapshot_input", "capacity_authority", "portfolio_source_name", "portfolio_source_account", "filters", "position_refresh_error",
    )})
    stocks = portfolio_context.get("stocks_by_symbol")
    stocks = stocks if isinstance(stocks, Mapping) else {}
    locked_by_symbol = option_context.get("locked_shares_by_symbol")
    locked_by_symbol = locked_by_symbol if isinstance(locked_by_symbol, Mapping) else {}
    locked_unavailable = option_context.get("locked_shares_unavailable_by_symbol")
    locked_unavailable_valid = locked_unavailable is None or isinstance(locked_unavailable, Mapping)
    locked_unavailable = locked_unavailable if isinstance(locked_unavailable, Mapping) else {}
    reserved: dict[str, int | None] = {}
    for batch in wheel_read_model.get("batches") or []:
        if not isinstance(batch, Mapping) or batch.get("lifecycle_status") != "active":
            continue
        symbol = str(batch.get("symbol") or "").strip().upper()
        prior = reserved.get(symbol, 0)
        if prior is None:
            continue
        try:
            reserved[symbol] = prior + contract_share_quantity(
                batch.get("active_intent_reserved_shares", 0), 1,
            )
        except ValueError:
            reserved[symbol] = None
    facts: list[dict[str, Any]] = []
    for symbol in sorted(set(stocks) | set(locked_by_symbol) | set(locked_unavailable) | set(reserved)):
        stock = stocks.get(symbol)
        status = "available"
        reason = None
        try:
            if position_errors:
                raise ValueError("position_snapshot_unavailable:" + ",".join(position_errors))
            if not isinstance(stock, Mapping):
                raise ValueError("holding_missing")
            if stock.get("capacity_authority_status") not in (None, "available"):
                raise ValueError("stock_capacity_authority_unavailable")
            shares_total = int(stock.get("shares"))
            shares_can_sell = int(stock.get("can_sell_qty"))
            shares_locked = int(locked_by_symbol.get(symbol, 0))
            if reserved.get(symbol, 0) is None:
                raise ValueError("wheel_intent_reserved_shares_invalid")
            shares_reserved = int(reserved.get(symbol, 0))
            if min(shares_total, shares_can_sell, shares_locked, shares_reserved) < 0:
                raise ValueError("holding_invalid")
            if shares_can_sell < shares_total and shares_locked:
                raise ValueError("broker_ledger_stock_lock_overlap_unproven")
            if (str(option_context.get("locked_shares_status") or "") != "available"
                    or option_context.get("decision_snapshot_status") != "trusted"
                    or not locked_unavailable_valid):
                raise ValueError("short_call_coverage_unavailable")
            if symbol in locked_unavailable:
                raise ValueError(str(locked_unavailable[symbol]))
        except (TypeError, ValueError) as exc:
            shares_total = shares_can_sell = shares_locked = shares_reserved = 0
            status = "unavailable"
            reason = str(exc)
        eligible = min(shares_total, shares_can_sell)
        identity = canonical_sha256(
            {
                "account": account,
                "symbol": symbol,
                "shares_total": shares_total,
                "shares_can_sell": shares_can_sell,
                "shares_locked": shares_locked,
                "shares_reserved": shares_reserved,
                "status": status,
                "reason": reason,
                "position_evidence": position_evidence,
                "decision_state_fingerprint": option_context.get("decision_state_fingerprint"),
                "source_observed_at": portfolio_context.get("source_observed_at"),
                "ledger_generation_sha256": (
                    (option_context.get("prepared_authority") or {}).get(
                        "ledger_generation_sha256"
                    )
                    if isinstance(option_context.get("prepared_authority"), Mapping)
                    else None
                ),
            }
        )
        facts.append(
            {
                "account": account,
                "symbol": symbol,
                "status": status,
                "reason": reason,
                "shares_total": shares_total,
                "shares_can_sell": shares_can_sell,
                "shares_eligible": eligible,
                "shares_locked": shares_locked,
                "shares_reserved": shares_reserved,
                "shares_available_for_cover": max(
                    0, eligible - shares_locked - shares_reserved
                ),
                "capacity_identity_hash": identity,
                "position_evidence": position_evidence,
                "source_observed_at": stock.get("source_observed_at") or portfolio_context.get("source_observed_at"),
                "decision_state_fingerprint": option_context.get("decision_state_fingerprint"),
            }
        )
    return facts


def _final_wheel_candidate(
    *,
    account: str,
    batch: Mapping[str, Any],
    internal_candidates: list[dict[str, Any]],
    allocation: Mapping[str, Any] | None,
    coverage_facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    granted = int((allocation or {}).get("granted_contracts") or 0)
    if not internal_candidates or granted <= 0:
        return None
    top = internal_candidates[0]
    grant_evaluations = top.get("_grant_evaluations")
    if isinstance(grant_evaluations, Mapping):
        exact = grant_evaluations.get(str(granted))
    else:
        exact = top if int(top.get("contracts") or 0) == granted else None
    if not isinstance(exact, Mapping) or exact.get("accepted") is not True:
        return None
    return {
        **{key: value for key, value in top.items() if key != "_grant_evaluations"},
        **dict(exact),
        "account": account,
        "stock_lot_id": str(batch.get("stock_lot_id") or ""),
        "wheel_branch_id": str(
            batch.get("wheel_branch_id") or batch.get("stock_lot_id") or ""
        ),
        "direction": "call",
        "final_candidate_id": top["candidate_id"],
        "requested_contracts": int((allocation or {}).get("requested_contracts") or 0),
        "granted_contracts": granted,
        "granted_shares": int((allocation or {}).get("granted_shares") or 0),
        "capacity_identity_hash": next(
            (
                row["capacity_identity_hash"]
                for row in coverage_facts
                if row.get("account") == account
                and row.get("symbol") == batch.get("symbol")
            ),
            None,
        ),
    }


def finalize_wheel_capacity(
    *,
    account: str,
    wheel_read_model: Mapping[str, Any],
    wheel_scan: Mapping[str, Any],
    opening_call_candidates: list[dict[str, Any]],
    coverage_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    ordinary_claims: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for index, candidate in enumerate(opening_call_candidates):
        symbol = str(candidate.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        ordinary_claims.append(
            {
                "claim_id": f"covered_call:{symbol}",
                "strategy_family": "covered_call",
                "account": account,
                "symbol": symbol,
                "candidate_id": str(
                    candidate.get("candidate_id")
                    or candidate.get("contract_symbol")
                    or candidate.get("code")
                    or index
                ),
                "requested_contracts": int(
                    candidate.get("max_new_contracts")
                    or candidate.get("covered_contracts_available")
                    or 1
                ),
                "multiplier": int(float(candidate.get("multiplier") or 0)),
            }
        )
    wheel_claims = [
        dict(item)
        for item in wheel_scan.get("capacity_claims") or []
        if isinstance(item, Mapping)
    ]
    allocations = allocate_opening_share_capacity(
        coverage_facts,
        ordinary_claims + wheel_claims,
    )
    by_claim = {str(row.get("claim_id") or ""): row for row in allocations}
    branch_rows = wheel_read_model.get("wheel_branches") or wheel_read_model.get("batches") or []
    batches_by_id = {
        str(row.get("stock_lot_id") or ""): dict(row)
        for row in branch_rows
        if isinstance(row, Mapping)
        and str(row.get("direction") or "call").strip().lower() == "call"
    }
    assigned_stock = wheel_read_model.get("assigned_stock_projection")
    assigned_stock = assigned_stock if isinstance(assigned_stock, Mapping) else {}
    stock_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for stock in assigned_stock.get("_all_assigned_stock_lots") or []:
        if isinstance(stock, Mapping):
            stock_by_id.setdefault(str(stock.get("stock_lot_id") or ""), []).append(stock)
    raw_by_batch = wheel_scan.get("raw_candidates")
    raw_by_batch = raw_by_batch if isinstance(raw_by_batch, Mapping) else {}
    rejected_claim_ids: set[str] = set()
    for lot_id, batch in batches_by_id.items():
        branch_id = str(batch.get("wheel_branch_id") or lot_id)
        claim_id = f"wheel:call:{branch_id}"
        allocation = by_claim.get(claim_id) or by_claim.get(f"wheel:{lot_id}")
        internal_candidates = [
            dict(item)
            for item in raw_by_batch.get(lot_id) or []
            if isinstance(item, Mapping)
        ]
        if (
            int((allocation or {}).get("granted_contracts") or 0) > 0
            and _final_wheel_candidate(
                account=account,
                batch=batch,
                internal_candidates=internal_candidates,
                allocation=allocation,
                coverage_facts=coverage_facts,
            )
            is None
        ):
            rejected_claim_ids.add(
                claim_id if claim_id in by_claim else f"wheel:{lot_id}"
            )
    allocations = withdraw_opening_share_capacity_grants(
        allocations,
        rejected_claim_ids,
    )
    by_claim = {str(row.get("claim_id") or ""): row for row in allocations}
    snapshot_batches: list[dict[str, Any]] = []
    for scope in wheel_scan.get("scope_results") or []:
        if not isinstance(scope, Mapping):
            continue
        lot_id = str(scope.get("stock_lot_id") or "")
        batch = batches_by_id.get(lot_id)
        if batch is None:
            continue
        internal_candidates = [
            dict(item)
            for item in raw_by_batch.get(lot_id) or []
            if isinstance(item, Mapping)
        ]
        raw_candidates = [
            {key: value for key, value in item.items() if key != "_grant_evaluations"}
            for item in internal_candidates
        ]
        branch_id = str(batch.get("wheel_branch_id") or lot_id)
        allocation = by_claim.get(f"wheel:call:{branch_id}") or by_claim.get(
            f"wheel:{lot_id}"
        )
        granted = int((allocation or {}).get("granted_contracts") or 0)
        source_scope = next(
            (
                item
                for item in wheel_scan.get("scope_results") or []
                if isinstance(item, Mapping)
                and str(item.get("stock_lot_id") or "") == lot_id
            ),
            {},
        )
        final = _final_wheel_candidate(
            account=account,
            batch=batch,
            internal_candidates=internal_candidates,
            allocation=allocation,
            coverage_facts=coverage_facts,
        )
        source_stocks = stock_by_id.get(lot_id) or []
        stock = source_stocks[0] if len(source_stocks) == 1 else None
        assignment_facts: dict[str, Any] = {}
        if stock is not None:
            try:
                price = Decimal(str(stock.get("assignment_price")))
            except (TypeError, ValueError, ArithmeticError):
                price = Decimal(0)
            if (
                str(stock.get("account") or "").strip().lower() == account
                and str(stock.get("symbol") or "").strip().upper()
                == str(batch.get("symbol") or "").strip().upper()
                and str(stock.get("broker") or "").strip()
                and str(stock.get("currency") or "").strip()
                and price.is_finite() and price > 0
                and type(stock.get("assigned_at_ms")) is int
                and stock["assigned_at_ms"] > 0
            ):
                assignment_facts = {
                    key: stock[key]
                    for key in ("broker", "currency", "assignment_price", "assigned_at_ms")
                }
        snapshot_batches.append(
            {
                "account": account,
                "symbol": str(batch.get("symbol") or "").upper(),
                "stock_lot_id": lot_id,
                "wheel_branch_id": branch_id,
                "direction": "call",
                "batch_generation_hash": batch.get("batch_generation_hash")
                or batch.get("batch_generation_hash"),
                "batch_generation_hash": batch.get("batch_generation_hash")
                or batch.get("batch_generation_hash"),
                "projection_hash": batch.get("projection_hash"),
                "shares_remaining": batch.get("shares_remaining"),
                **assignment_facts,
                "coverage": dict(batch.get("coverage") or project_wheel_coverage(batch)),
                "active_option_contracts": list(batch.get("active_option_contracts") or []),
                "phase": batch.get("phase"),
                "reason_codes": list(batch.get("reason_codes") or []),
                "candidate_status": source_scope.get("status"),
                "reason_code": (
                    (allocation or {}).get("allocation_reason")
                    if raw_candidates and granted <= 0
                    else "wheel_capacity_grant_candidate_rejected"
                    if raw_candidates and granted > 0 and final is None
                    else source_scope.get("reason_code")
                ),
                "raw_candidates": raw_candidates,
                "allocation": allocation,
                "granted_contracts": granted,
                "final_candidate": final,
            }
        )
    scopes: list[dict[str, Any]] = []
    for symbol in sorted({str(row.get("symbol") or "").upper() for row in snapshot_batches}):
        symbol_batches = [row for row in snapshot_batches if row["symbol"] == symbol]
        source_scopes = [
            row
            for row in wheel_scan.get("scope_results") or []
            if isinstance(row, Mapping) and str(row.get("symbol") or "").upper() == symbol
        ]
        raw_count = sum(len(row.get("raw_candidates") or []) for row in symbol_batches)
        statuses = {str(row.get("status") or "") for row in source_scopes}
        partial_data = any(
            str(row.get("reason_code") or "") == "partial_data"
            for row in source_scopes
        )
        failed_reasons = {
            str(row.get("reason_code") or "").strip()
            for row in source_scopes
            if str(row.get("status") or "") == "failed"
            and str(row.get("reason_code") or "").strip()
        }
        if statuses == {"completed"}:
            status = "completed"
            reason = "partial_data" if partial_data else None if raw_count else "no_candidate"
        elif statuses == {"not_applicable"}:
            status = "not_applicable"
            reason = str(source_scopes[0].get("reason_code") or "wheel_not_applicable")
        elif "completed" in statuses:
            status = "completed"
            reason = "partial_data"
        elif "failed" in statuses:
            status = "failed"
            reason = (
                next(iter(failed_reasons))
                if statuses == {"failed"} and len(failed_reasons) == 1
                else "wheel_integrity_conflict"
            )
        else:
            status, reason = "unavailable", "wheel_candidate_data_unavailable"
        scopes.append(
            {
                "scope": "strategy",
                "account": account,
                "symbol": symbol,
                "direction": "call",
                "strategy_family": "wheel",
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "status": status,
                "reason_code": reason,
                "candidate_count": raw_count,
            }
        )
    return {
        "coverage_facts": coverage_facts,
        "allocations": allocations,
        "scope_results": scopes,
        "batches": snapshot_batches,
        "allocation_hash": canonical_sha256(allocations),
    }


def _final_wheel_put_candidate(
    *,
    account: str,
    branch: Mapping[str, Any],
    internal_candidates: list[dict[str, Any]],
    allocation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    granted = int((allocation or {}).get("granted_contracts") or 0)
    if not internal_candidates or granted <= 0:
        return None
    top = internal_candidates[0]
    grant_evaluations = top.get("_grant_evaluations")
    exact = (
        grant_evaluations.get(str(granted))
        if isinstance(grant_evaluations, Mapping)
        else top if int(top.get("contracts") or 0) == granted else None
    )
    if not isinstance(exact, Mapping) or exact.get("accepted") is not True:
        return None
    currency = str((allocation or {}).get("cash_reservation_currency") or "").upper()
    reserved_amount = float((allocation or {}).get("cash_reservation_amount") or 0)
    if (
        currency != str(exact.get("cash_reservation_currency") or "").upper()
        or round(reserved_amount, 6)
        != round(float(exact.get("cash_reservation_amount") or 0), 6)
    ):
        return None
    branch_id = str(branch.get("wheel_branch_id") or "")
    return {
        **{key: value for key, value in top.items() if key != "_grant_evaluations"},
        **dict(exact),
        "account": account,
        "wheel_branch_id": branch_id,
        "direction": "put",
        "final_candidate_id": top["candidate_id"],
        "requested_contracts": int((allocation or {}).get("requested_contracts") or 0),
        "granted_contracts": granted,
        "capacity_identity_hash": (allocation or {}).get("capacity_identity_hash"),
        "allocation_input_hash": (allocation or {}).get("allocation_input_hash"),
        "cash_reservation_amount": reserved_amount,
        "cash_reservation_currency": currency,
        "batch_generation_hash": branch.get("batch_generation_hash"),
    }


def finalize_wheel_put_capacity(
    *,
    account: str,
    wheel_read_model: Mapping[str, Any],
    wheel_scan: Mapping[str, Any],
    opening_put_candidates: list[dict[str, Any]],
    cash_capacity_fact: Mapping[str, Any],
    exchange_rate_converter: Any,
) -> dict[str, Any]:
    """Allocate one account-wide cash pool after immutable ordinary Put claims."""

    account_value = str(account or "").strip().lower()
    allocation_result = allocate_wheel_put_cash_capacity(
        cash_capacity_fact=cash_capacity_fact,
        ordinary_put_claims=_ordinary_put_cash_claims(
            opening_put_candidates,
            account=account_value,
        ),
        active_wheel_put_intents=[
            dict(item)
            for item in cash_capacity_fact.get("wheel_intent_reservations") or []
            if isinstance(item, Mapping)
        ],
        wheel_put_claims=[
            dict(item)
            for item in wheel_scan.get("capacity_claims") or []
            if isinstance(item, Mapping)
        ],
        convert_currency=_convert_currency_fn(
            exchange_rate_converter,
            fx_snapshot=(
                cash_capacity_fact.get("fx_snapshot")
                if isinstance(cash_capacity_fact.get("fx_snapshot"), Mapping)
                else None
            ),
        ),
    )
    allocations = [
        dict(item)
        for item in allocation_result.get("allocations") or []
        if isinstance(item, Mapping)
    ]
    by_branch = {
        str(row.get("wheel_branch_id") or ""): row for row in allocations
    }
    branches = {
        str(row.get("wheel_branch_id") or ""): dict(row)
        for row in wheel_read_model.get("wheel_branches") or []
        if isinstance(row, Mapping)
        and str(row.get("direction") or "").strip().lower() == "put"
    }
    raw_by_branch = wheel_scan.get("raw_candidates")
    raw_by_branch = raw_by_branch if isinstance(raw_by_branch, Mapping) else {}
    snapshot_batches: list[dict[str, Any]] = []
    branch_scopes: list[dict[str, Any]] = []
    for raw_scope in wheel_scan.get("scope_results") or []:
        if not isinstance(raw_scope, Mapping):
            continue
        scope = dict(raw_scope)
        branch_id = str(scope.get("wheel_branch_id") or "")
        branch = branches.get(branch_id)
        if branch is None:
            continue
        internal_candidates = [
            dict(item)
            for item in raw_by_branch.get(branch_id) or []
            if isinstance(item, Mapping)
        ]
        raw_candidates = [
            {key: value for key, value in item.items() if key != "_grant_evaluations"}
            for item in internal_candidates
        ]
        allocation = by_branch.get(branch_id)
        granted = int((allocation or {}).get("granted_contracts") or 0)
        final = _final_wheel_put_candidate(
            account=account_value,
            branch=branch,
            internal_candidates=internal_candidates,
            allocation=allocation,
        )
        reason = scope.get("reason_code")
        if raw_candidates and granted <= 0:
            reason = (allocation or {}).get("allocation_reason")
        elif raw_candidates and final is None:
            reason = "wheel_capacity_grant_candidate_rejected"
        snapshot_batches.append(
            {
                "account": account_value,
                "symbol": str(branch.get("symbol") or "").upper(),
                "wheel_branch_id": branch_id,
                "direction": "put",
                "batch_generation_hash": branch.get("batch_generation_hash"),
                "projection_hash": branch.get("projection_hash"),
                "phase": branch.get("phase"),
                "coverage": dict(branch.get("coverage") or project_wheel_coverage(branch)),
                "active_option_contracts": list(branch.get("active_option_contracts") or []),
                "remaining_contracts": branch.get("remaining_contracts"),
                "principal_anchor": branch.get("principal_anchor"),
                "currency": branch.get("currency"),
                "reason_codes": list(branch.get("reason_codes") or []),
                "candidate_status": scope.get("status"),
                "reason_code": reason,
                "raw_candidates": raw_candidates,
                "allocation": allocation,
                "granted_contracts": granted,
                "final_candidate": final,
            }
        )
        branch_scopes.append(
            {
                **scope,
                "direction": "put",
                "candidate_count": len(raw_candidates),
                "reason_code": reason,
            }
        )
    scope_results: list[dict[str, Any]] = []
    symbols = sorted(
        {str(row.get("symbol") or "").strip().upper() for row in branch_scopes}
    )
    for symbol in symbols:
        rows = [
            row
            for row in branch_scopes
            if str(row.get("symbol") or "").strip().upper() == symbol
        ]
        statuses = {str(row.get("status") or "") for row in rows}
        reasons = {str(row.get("reason_code") or "") for row in rows}
        failed_reasons = {
            str(row.get("reason_code") or "").strip()
            for row in rows
            if str(row.get("status") or "") == "failed"
            and str(row.get("reason_code") or "").strip()
        }
        candidate_count = sum(int(row.get("candidate_count") or 0) for row in rows)
        if statuses == {"completed"}:
            status = "completed"
            reason = "partial_data" if "partial_data" in reasons else None
            if candidate_count == 0 and reason is None:
                reason = "no_candidate"
        elif statuses == {"not_applicable"}:
            status = "not_applicable"
            reason = next(iter(reasons)) if len(reasons) == 1 else "wheel_not_applicable"
        elif "completed" in statuses:
            status, reason = "completed", "partial_data"
        elif "failed" in statuses:
            status = "failed"
            reason = (
                next(iter(failed_reasons))
                if statuses == {"failed"} and len(failed_reasons) == 1
                else "wheel_integrity_conflict"
            )
        else:
            status, reason = "unavailable", "wheel_candidate_data_unavailable"
        scope_results.append(
            {
                "scope": "strategy",
                "account": account_value,
                "symbol": symbol,
                "direction": "put",
                "strategy_family": "wheel",
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "status": status,
                "reason_code": reason,
                "candidate_count": candidate_count,
            }
        )
    return {
        "cash_capacity_fact": dict(cash_capacity_fact),
        "allocations": allocations,
        "scope_results": scope_results,
        "batches": snapshot_batches,
        "capacity_identity_hash": allocation_result.get("capacity_identity_hash"),
        "allocation_input_hash": allocation_result.get("allocation_input_hash"),
        "allocation_hash": canonical_sha256(allocations),
    }


def finalize_wheel_bidirectional_capacity(
    *,
    account: str,
    wheel_read_model: Mapping[str, Any],
    call_scan: Mapping[str, Any],
    put_scan: Mapping[str, Any],
    opening_call_candidates: list[dict[str, Any]],
    opening_put_candidates: list[dict[str, Any]],
    coverage_facts: list[dict[str, Any]],
    cash_capacity_fact: Mapping[str, Any],
    exchange_rate_converter: Any,
) -> dict[str, Any]:
    call_result = finalize_wheel_capacity(
        account=account,
        wheel_read_model=wheel_read_model,
        wheel_scan=call_scan,
        opening_call_candidates=opening_call_candidates,
        coverage_facts=coverage_facts,
    )
    put_result = finalize_wheel_put_capacity(
        account=account,
        wheel_read_model=wheel_read_model,
        wheel_scan=put_scan,
        opening_put_candidates=opening_put_candidates,
        cash_capacity_fact=cash_capacity_fact,
        exchange_rate_converter=exchange_rate_converter,
    )
    scopes = sorted(
        [*call_result["scope_results"], *put_result["scope_results"]],
        key=lambda row: (
            str(row.get("symbol") or ""),
            str(row.get("direction") or ""),
        ),
    )
    batches = sorted(
        [*call_result["batches"], *put_result["batches"]],
        key=lambda row: (
            str(row.get("wheel_branch_id") or ""),
            str(row.get("direction") or ""),
        ),
    )
    allocations = [*call_result["allocations"], *put_result["allocations"]]
    return {
        "coverage_facts": call_result["coverage_facts"],
        "cash_capacity_fact": put_result["cash_capacity_fact"],
        "share_allocations": call_result["allocations"],
        "cash_allocations": put_result["allocations"],
        "allocations": allocations,
        "scope_results": scopes,
        "batches": batches,
        "allocation_hash": canonical_sha256(
            {
                "share": call_result["allocation_hash"],
                "cash": put_result["allocation_hash"],
            }
        ),
    }


__all__ = [
    "WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA",
    "build_shared_cash_capacity_fact",
    "build_shared_coverage_facts",
    "finalize_wheel_bidirectional_capacity",
    "finalize_wheel_capacity",
    "finalize_wheel_put_capacity",
    "load_shared_cash_capacity_fact",
    "load_shared_coverage_fact",
    "revalidate_selected_wheel_put_candidate",
    "revalidate_selected_wheel_put_candidate_from_rows",
]
