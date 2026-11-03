"""Normalize AP Longbridge asset observations without granting unproven capacity."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from concurrent.futures import CancelledError
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_position_identity import normalize_currency
from domain.domain.position_snapshot import POSITION_SNAPSHOT_VERSION, normalize_position_snapshot_input
from domain.domain.source_evidence import build_source_evidence
from domain.domain.symbol_identity import canonical_symbol, looks_like_option_contract_label
from src.application.account_config import normalize_account_label, resolve_account_longbridge_settings
from src.infrastructure.exchange_rates import exchange_rate_observation_status
from src.infrastructure.longbridge_gateway import LongbridgeGateway


def _field(row: Any, name: str, default: Any = None) -> Any:
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def _list(value: Any) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("longbridge asset response must contain a list")
    return list(value)


def _decimal(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("missing or invalid decimal")
    try:
        parsed = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("invalid decimal") from exc
    if not parsed.is_finite() or not math.isfinite(float(parsed)):
        raise ValueError("nonfinite decimal")
    return parsed


def _error(stage: str, exc: Exception) -> str:
    # SDK exception text may contain credentials/URLs. Retain safe type/code only.
    code = getattr(exc, "code", None)
    suffix = f":{code}" if type(code) is int else ""
    return f"longbridge_{stage}_failed:{type(exc).__name__}{suffix}"


def _cash(rows: list[Any]) -> tuple[dict[str, float], list[dict[str, Any]], dict[str, str]]:
    amounts: dict[str, float] = {}
    raw: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    seen: set[str] = set()
    if len(rows) != 1:
        # The response is a list, but no unique account ids bind multiple balances.
        errors["balance_snapshot"] = "longbridge_balance_scope_ambiguous_or_empty"
        return amounts, raw, errors
    try:
        infos = _list(_field(rows[0], "cash_infos"))
    except ValueError:
        return amounts, raw, {"cash_infos": "longbridge_cash_infos_missing_or_invalid"}
    if not infos:
        errors["cash_infos"] = "longbridge_cash_infos_empty"
    for index, row in enumerate(infos):
        currency = normalize_currency(_field(row, "currency"))
        raw.append({name: str(_field(row, name)) if _field(row, name) is not None else None
                    for name in ("currency", "available_cash", "withdraw_cash", "frozen_cash", "settling_cash")})
        try:
            if not currency or not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("invalid currency")
            if currency in seen:
                amounts.pop(currency, None)
                errors[f"cash_infos.{index}"] = "longbridge_duplicate_cash_currency"
                continue
            seen.add(currency)
            # Signed cash, excluding margin, buying power and frozen/unsettled cash.
            amounts[currency] = float(_decimal(_field(row, "available_cash")))
        except ValueError:
            errors[f"cash_infos.{index}"] = "longbridge_cash_amount_or_currency_invalid"
    return amounts, raw, errors


def _positions(response: Any) -> tuple[list[dict[str, Any]], list[str]]:
    mapped: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for channel in _list(_field(response, "channels")):
        for row in _list(_field(channel, "positions")):
            code = str(_field(row, "symbol") or "").strip().upper()
            raw_market = _field(row, "market")
            row_market = str(getattr(raw_market, "name", raw_market) or "").upper()
            if row_market.startswith("MARKET."):
                row_market = row_market.removeprefix("MARKET.")
            suffix = code.rsplit(".", 1)[-1]
            if row_market not in {"US", "HK"}:
                errors.append("longbridge_position_market_unsupported")
                continue
            if row_market != suffix:
                errors.append("longbridge_position_market_mismatch")
                continue
            if looks_like_option_contract_label(code):
                continue  # Stock-only scope; option scope is explicitly unsupported below.
            if not re.fullmatch(r"(?:[A-Z][A-Z0-9.\-]*\.US|[0-9]{1,5}\.HK)", code):
                errors.append("longbridge_position_instrument_unsupported")
                continue
            symbol = canonical_symbol(code[:-3] if suffix == "US" else code)
            if not symbol:
                errors.append("longbridge_position_symbol_invalid")
                continue
            if symbol in seen:
                errors.append("longbridge_duplicate_position_symbol")
            seen.add(symbol)
            try:
                quantity = _decimal(_field(row, "quantity"))
                currency = normalize_currency(_field(row, "currency"))
                if not currency or not re.fullmatch(r"[A-Z]{3}", currency):
                    raise ValueError("invalid currency")
            except ValueError:
                errors.append("longbridge_position_quantity_or_currency_invalid")
                continue
            if quantity == 0:
                continue
            sellable = None
            raw_sellable = _field(row, "available_quantity")
            if raw_sellable is not None:
                try:
                    available = _decimal(raw_sellable)
                    # Negative available quantities are documented; do not abs or clamp.
                    if available < 0 or available > abs(quantity):
                        raise ValueError("unusable available quantity")
                    sellable = format(available, "f")
                except ValueError:
                    errors.append("longbridge_position_sellable_quantity_unavailable")
            cost = None
            if _field(row, "cost_price") is not None:
                try:
                    cost = format(_decimal(_field(row, "cost_price")), "f")
                except ValueError:
                    errors.append("longbridge_position_cost_invalid")
            mapped.append({
                "instrument_ref": {"asset_type": "stock", "symbol": symbol,
                    "market": row_market, "currency": currency, "source_code": code},
                "position_side": "long" if quantity > 0 else "short",
                "quantity": format(abs(quantity), "f"), "sellable_quantity": sellable,
                "sellable_quantity_source": "longbridge.available_quantity" if sellable is not None else None,
                "source_row": {"symbol": code, "quantity": format(quantity, "f"),
                    "available_quantity": str(raw_sellable) if raw_sellable is not None else None,
                    "currency": currency, "cost_price": cost,
                    "symbol_name": _field(row, "symbol_name"),
                    "account_channel": str(_field(channel, "account_channel") or "")},
            })
    return mapped, errors


def _valuation_cash(rows: list[Any], *, observed_at: str | None, identity_error: str | None) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "status": "unavailable", "amount": None, "currency": None,
        "source": "longbridge.account_balance.total_cash", "source_observed_at": observed_at,
        "scope": "physical_account_aggregate", "additive_with_cash_by_currency": False,
        "errors": [identity_error] if identity_error else [],
    }
    try:
        if len(rows) != 1:
            raise ValueError("ambiguous or missing balance")
        currency = normalize_currency(_field(rows[0], "currency"))
        if not currency or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("invalid currency")
        fact.update(amount=float(_decimal(_field(rows[0], "total_cash"))), currency=currency)
    except ValueError:
        fact["errors"].append("longbridge_total_cash_invalid_or_missing")
    if not fact["errors"]:
        fact["status"] = "available"
    return fact


def fetch_longbridge_portfolio_context(
    *, cfg: Mapping[str, Any], account: str | None, market: str = "长桥",
    base_currency: str = "CNY", write_cache: bool = False,
    exchange_rate_observation: Mapping[str, Any] | None = None,
    exchange_rate_cache_path: Any = None, include_options: bool = False,
    client: Any = None, context_factory: Any = None, environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read assets only. Cache/FX I/O belongs to the caller; supplied FX is reused.

    The SDK lacks an account-profile identity API. Capacity needs a previously
    user-confirmed credential/account fingerprint that still matches actual credentials.
    """
    account = normalize_account_label(account)
    settings = resolve_account_longbridge_settings(cfg, account=account)
    scope_market = next((str(cfg[key].get("market") or "").lower()
                         for key in ("_resolved", "_generated")
                         if isinstance(cfg.get(key), Mapping) and cfg[key].get("market")),
                        str(market).lower())
    if scope_market not in {"us", "hk"}:
        raise ValueError("longbridge portfolio context requires explicit us/hk runtime market")
    gateway = LongbridgeGateway(settings, client=client, context_factory=context_factory, environ=environ)
    cash_observed_at = position_observed_at = None
    identity_error = gateway.identity_error
    physical_id = gateway.bound_account_id
    identity_evidence = gateway.account_identity_evidence
    cash_errors: dict[str, str] = {"identity": identity_error} if identity_error else {}
    position_errors = [identity_error] if identity_error else []
    balances: list[Any] = []
    position_rows: list[dict[str, Any]] = []
    provider_error = None
    try:
        try:
            balances = _list(gateway.account_balance())
            cash_observed_at = datetime.now(timezone.utc).isoformat()
        except (TimeoutError, CancelledError):
            raise
        except Exception as exc:
            provider_error = _error("balance", exc)
            cash_errors["provider"] = provider_error
        try:
            response = gateway.stock_positions()
            position_observed_at = datetime.now(timezone.utc).isoformat()
            position_rows, errors = _positions(response)
            position_errors.extend(errors)
        except (TimeoutError, CancelledError):
            raise
        except Exception as exc:
            position_errors.append(_error("positions", exc))
    finally:
        gateway.close()
    amounts, cash_rows, errors = _cash(balances)
    cash_errors.update(errors)
    observed_at = datetime.now(timezone.utc).isoformat()
    authority = {
        "schema_version": "physical_account_capacity_authority.v2", "status": "available" if physical_id else "unavailable",
        "logical_account": account, "broker_id": "longbridge", "broker_account_id": physical_id,
        "trd_env": settings["trd_env"], "market": scope_market,
        "source": "longbridge", "source_observed_at": observed_at,
        "credential_binding_sha256": identity_evidence["credential_binding_sha256"],
        "binding_method": "user_confirmed_credentials_sha256", "broker_echoed": False,
        "account_identity_evidence": identity_evidence,
    }
    identity_hash = canonical_sha256(authority)
    if include_options:
        position_errors.append("longbridge_option_contract_identity_unsupported")
    content = {
        "schema_version": POSITION_SNAPSHOT_VERSION, "source_id": "longbridge.positions",
        "broker_account_ref": {"broker_id": "longbridge", "broker_account_id": f"longbridge:{settings['trd_env']}:{physical_id}" if physical_id else None,
            "external_account_id": physical_id, "environment": settings["trd_env"], "account_label": account},
        "scope": {"markets": ["US", "HK"], "asset_types": ["stock", "option"] if include_options else ["stock"],
                  "filtered": False},
        "observed_at_utc": position_observed_at or observed_at, "source_as_of_utc": None,
        "completeness": "partial" if position_errors else "complete",
        "quality": {"status": "unavailable" if position_errors else "ready"}, "rows": position_rows,
        "errors": position_errors,
    }
    content["snapshot_id"] = "longbridge-" + canonical_sha256(content)[:24]
    evidence = build_source_evidence(
        source="longbridge", source_id="longbridge.positions", account=account, data_type="position",
        source_record_identity=content["snapshot_id"], payload_version="longbridge-position.v1",
        content_digest=canonical_sha256(position_rows),
        received_at_ms=int(datetime.fromisoformat(content["observed_at_utc"]).timestamp() * 1000),
        adapter_version="longbridge-position.v1",
    )
    content.update(source_evidence=[evidence], evidence_refs=[evidence["evidence_id"]])
    snapshot = normalize_position_snapshot_input(content)
    stocks: dict[str, Any] = {}
    for row in snapshot["rows"]:
        if row["position_side"] != "long":
            continue
        instrument, raw = row["instrument_ref"], row["source_row"]
        shares = float(row["quantity"])
        cost = float(raw["cost_price"]) if raw["cost_price"] is not None else None
        stocks[instrument["symbol"]] = {
            "symbol": instrument["symbol"], "name": raw["symbol_name"], "shares": shares,
            "can_sell_qty": float(row["sellable_quantity"]) if row["sellable_quantity"] is not None else None,
            "eligible_underlying_shares": min(shares, float(row["sellable_quantity"])) if not snapshot["errors"] and row["sellable_quantity"] is not None else None,
            "avg_cost": cost,
            "cost_basis_complete": cost is not None, "cost_known_shares": shares if cost is not None else 0,
            "cost_unknown_shares": shares if cost is None else 0, "currency": instrument["currency"],
            "broker": market, "account": account, "delivery_asset_type": "ordinary_stock",
            "coverage_allocation_status": "unallocated", "stock_lot_id": None,
            "wheel_batch_return_status": "not_calculated_unallocated", "broker_id": "longbridge",
            "broker_account_id": physical_id, "trd_env": settings["trd_env"], "market": scope_market,
            "source_observed_at": position_observed_at, "capacity_identity_hash": identity_hash,
            "capacity_authority_status": "available" if not snapshot["errors"] else "unavailable", "position_snapshot_id": snapshot["snapshot_id"],
            "position_snapshot_errors": snapshot["errors"],
        }
    fx = dict(exchange_rate_observation) if isinstance(exchange_rate_observation, Mapping) else None
    return {
        "as_of_utc": observed_at, "source_observed_at": position_observed_at,
        "cash_source_observed_at": cash_observed_at, "cash_source_rows": cash_rows,
        "source_account_identifiers": [physical_id] if physical_id else [], "capacity_authority": authority,
        "account_identity_evidence": identity_evidence,
        "capacity_identity_hash": identity_hash, "filters": {"broker": market, "account": account},
        "cash_by_currency": amounts, "cash_balance_reliable": not cash_errors,
        "cash_valuation": _valuation_cash(balances, observed_at=cash_observed_at, identity_error=identity_error),
        "cash_balance_unavailable_by_row": cash_errors,
        "cash_components_by_currency": {ccy: {"longbridge.available_cash": amount} for ccy, amount in amounts.items()},
        "cash_capacity_by_currency": {ccy: {"currency": ccy, "amount": amount,
            "broker_id": "longbridge", "broker_account_id": physical_id, "trd_env": settings["trd_env"],
            "market": scope_market, "source_observed_at": cash_observed_at,
            "capacity_identity_hash": identity_hash, "capacity_authority_status": "available" if not cash_errors else "unavailable",
            "pool_additive_across_candidates": False} for ccy, amount in amounts.items()},
        "cash_source": "longbridge.available_cash", "cash_power_by_currency": {}, "cash_power_source": None,
        "cash_source_observation_status": "unavailable" if cash_errors else "trusted",
        "source_observation_status": "unavailable" if snapshot["errors"] else "trusted",
        "cash_provider_error": provider_error, "stocks_by_symbol": stocks, "position_snapshot_input": snapshot,
        "exchange_rates": fx, "exchange_rate_status": exchange_rate_observation_status(fx, max_age_hours=24),
        "raw_selected_count": len(balances) + len(position_rows), "portfolio_source_name": "longbridge",
        "errors": sorted(set(position_errors + list(cash_errors.values()))),
    }
