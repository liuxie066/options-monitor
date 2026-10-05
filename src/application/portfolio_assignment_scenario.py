"""Application boundary for the portfolio assignment stress scenario."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from domain.domain.portfolio_assignment_scenario import (
    PORTFOLIO_EVIDENCE_VERSION,
    project_assignment_scenario,
)
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.symbol_identity import canonical_symbol
from domain.domain.option_position_identity import normalize_broker, normalize_currency
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.portfolio_management import (
    PORTFOLIO_MANAGEMENT_DISABLED,
    portfolio_management_failure_code,
    resolve_portfolio_management_client,
)
from src.application.ledger.api import (
    list_open_short_assignment_rows,
    open_position_ledger_from_runtime_config,
)
from src.infrastructure.portfolio_management_client import (
    SERVICE_URL_ENV,
    PortfolioManagementClient,
    PortfolioManagementError,
    PortfolioManagementHTTPError,
)
from src.application.payload_helpers import parse_utc as _iso_datetime
from src.application.portfolio_context_service import load_account_portfolio_context, cash_snapshot_is_usable
from src.application.futu_portfolio_context import fetch_futu_portfolio_context, infer_futu_portfolio_settings
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.application.opend_fetch_config import DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT, resolve_opend_fetch_limits
from src.application.opend_market_snapshot_fetching import get_underlier_observations_opend
from src.application.opend_utils import normalize_underlier
from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway
from src.infrastructure.exchange_rates import (
    current_exchange_rate_snapshot,
    exchange_rate_observation_status,
    project_exchange_rate_snapshot,
)


MAX_ACCOUNTS = 20
MAX_SUPPLEMENTAL_CODES = 500


class AssignmentScenarioInputError(ValueError):
    """Raised when the public business input violates the scenario contract."""


class PortfolioEvidenceReadError(RuntimeError):
    """Raised when the portfolio-management evidence boundary is unavailable."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_assignment_accounts(accounts: Sequence[str]) -> list[str]:
    if isinstance(accounts, (str, bytes)) or not isinstance(accounts, Sequence):
        raise AssignmentScenarioInputError("accounts must be an array of account labels")
    normalized = list(dict.fromkeys(str(item or "").strip().lower() for item in accounts if str(item or "").strip()))
    if not normalized:
        raise AssignmentScenarioInputError("accounts must contain at least one account")
    if len(normalized) > MAX_ACCOUNTS:
        raise AssignmentScenarioInputError(f"accounts must contain at most {MAX_ACCOUNTS} accounts")
    invalid = [
        account
        for account in normalized
        if len(account) > 32
        or not account[0].isalnum()
        or any(not (char.islower() or char.isdigit() or char in {"_", "-"}) for char in account)
    ]
    if invalid:
        raise AssignmentScenarioInputError(f"invalid account labels: {', '.join(invalid)}")
    return normalized


def read_portfolio_valuation_evidence(
    *,
    accounts: Sequence[str],
    supplemental_codes: Sequence[str],
    price_timeout: int = 30,
    client: PortfolioManagementClient | None = None,
    runtime_config: dict[str, Any] | None = None,
    holdings_scope: str | None = None,
) -> dict[str, Any]:
    try:
        if client is None and runtime_config is None:
            _config_path, runtime_config = load_runtime_config(config_key="us")
        resolved_client = resolve_portfolio_management_client(
            runtime_config,
            client=client,
            urlopen_fn=urllib.request.urlopen,
        )
        if resolved_client == PORTFOLIO_MANAGEMENT_DISABLED:
            raise PortfolioEvidenceReadError(
                "portfolio-management integration is disabled",
                code=PORTFOLIO_MANAGEMENT_DISABLED,
            )
        kwargs = {"holdings_scope": holdings_scope} if holdings_scope is not None else {}
        return resolved_client.read_valuation_evidence(
            accounts=list(accounts),
            supplemental_codes=list(supplemental_codes),
            price_timeout=int(price_timeout),
            **kwargs,
        )
    except PortfolioManagementHTTPError as exc:
        if exc.error_code == "INPUT_ERROR" and holdings_scope != "non_futu":
            raise AssignmentScenarioInputError(str(exc)) from exc
        raise PortfolioEvidenceReadError(
            str(exc),
            code=portfolio_management_failure_code(exc),
        ) from exc
    except PortfolioManagementError as exc:
        raise PortfolioEvidenceReadError(
            str(exc),
            code=portfolio_management_failure_code(exc),
        ) from exc


def approved_non_futu_brokers(config: Mapping[str, Any], accounts: Sequence[str]) -> dict[str, list[str]] | None:
    portfolio = config.get("portfolio")
    holdings = portfolio.get("holdings") if isinstance(portfolio, Mapping) else None
    approved = holdings.get("approved_non_futu_brokers") if isinstance(holdings, Mapping) else None
    if not isinstance(approved, Mapping) or not set(accounts).issubset(approved):
        return None
    if any(not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values) or len(values) != len(set(values)) for values in approved.values()):
        return None
    return {account: list(approved[account]) for account in accounts}


def non_futu_broker_inventory(evidence: Mapping[str, Any], accounts: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    scope = evidence.get("scope")
    inventory = scope.get("broker_inventory") if isinstance(scope, Mapping) else None
    counts = scope.get("holding_counts") if isinstance(scope, Mapping) else None
    if not isinstance(scope, Mapping) or scope.get("holdings_scope") != "non_futu" or not isinstance(inventory, Mapping) or not isinstance(counts, Mapping) or set(inventory) != set(accounts) or set(counts) != set(accounts):
        raise ValueError("PM non_futu broker inventory is missing or incomplete")
    return {account: list(inventory[account]["brokers"]) for account in accounts}


def _load_runtime_and_positions(
    accounts: Sequence[str],
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key="us")
    configured_accounts = {
        str(item or "").strip().lower() for item in (cfg.get("accounts") or []) if str(item or "").strip()
    }
    unknown = [account for account in accounts if account not in configured_accounts]
    if unknown:
        raise AssignmentScenarioInputError(f"unknown accounts: {', '.join(unknown)}")
    _data_config, repo = open_position_ledger_from_runtime_config(
        base=repo_base(),
        cfg=cfg,
        config_path=config_path,
    )
    return (
        list_open_short_assignment_rows(repo, accounts=list(accounts)),
        str(config_path.resolve()),
        cfg,
    )


def _snapshot_payload(
    *,
    accounts: Sequence[str],
    option_positions: Sequence[Mapping[str, Any]],
    portfolio_evidence: Mapping[str, Any],
    options_observed_at: str,
    runtime_config_name: str,
) -> dict[str, Any]:
    portfolio_snapshot = (
        portfolio_evidence.get("snapshot") if isinstance(portfolio_evidence.get("snapshot"), Mapping) else {}
    )
    portfolio_observed_at = (
        str(portfolio_snapshot.get("observed_at") or portfolio_evidence.get("observed_at") or "").strip() or None
    )
    portfolio_time = _iso_datetime(portfolio_observed_at)
    pm_observed_at = portfolio_evidence.get("pm_observed_at")
    pm_time = _iso_datetime(pm_observed_at)
    options_time = _iso_datetime(options_observed_at)
    futu_observed_at = portfolio_evidence.get("futu_observed_at_by_account") or {}
    quote_observed_at = {
        canonical_symbol(row.get("code")): row.get("observed_at")
        for row in portfolio_evidence.get("quotes") or []
        if isinstance(row, Mapping) and canonical_symbol(row.get("code")) and row.get("observed_at")
    }
    source_times = [value for value in (portfolio_time, pm_time, options_time) if value is not None]
    if isinstance(futu_observed_at, Mapping):
        source_times.extend(value for raw in futu_observed_at.values() if (value := _iso_datetime(raw)) is not None)
    source_times.extend(value for raw in quote_observed_at.values() if (value := _iso_datetime(raw)) is not None)
    max_skew = (max(source_times) - min(source_times)).total_seconds() if len(source_times) >= 2 else None
    option_identity = [
        {
            "record_id": row.get("record_id"),
            "account": row.get("account"),
            "broker": row.get("broker"),
            "symbol": row.get("symbol"),
            "option_type": row.get("option_type"),
            "strike": row.get("strike"),
            "multiplier": row.get("multiplier"),
            "expiration_ymd": row.get("expiration_ymd"),
            "currency": row.get("currency"),
            "contracts_open": row.get("contracts_open"),
            "status": row.get("status"),
        }
        for row in option_positions
    ]
    digest = hashlib.sha256(
        json.dumps(
            {
                "accounts": list(accounts),
                "portfolio_snapshot_id": portfolio_snapshot.get("snapshot_id"),
                "pm_snapshot_id": portfolio_evidence.get("pm_snapshot_id"),
                "pm_observed_at": pm_observed_at,
                "futu_observed_at_by_account": futu_observed_at,
                "quote_observed_at_by_code": quote_observed_at,
                "fx_observation": portfolio_evidence.get("fx_observation"),
                "options_observed_at": options_observed_at,
                "option_identity": option_identity,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "snapshot_id": f"assignment-{digest}",
        "observed_at": options_observed_at,
        "portfolio_snapshot_id": portfolio_snapshot.get("snapshot_id"),
        "pm_snapshot_id": portfolio_evidence.get("pm_snapshot_id"),
        "pm_observed_at": pm_observed_at,
        "portfolio_observed_at": portfolio_observed_at,
        "futu_observed_at_by_account": futu_observed_at,
        "quote_observed_at_by_code": quote_observed_at,
        "fx_observation": portfolio_evidence.get("fx_observation"),
        "holdings_sources": portfolio_evidence.get("holdings_sources") or [],
        "pm_supplement": portfolio_evidence.get("pm_supplement") or {"enabled": False, "status": "disabled", "included_rows": 0},
        "options_observed_at": options_observed_at,
        "max_source_skew_seconds": (format(max_skew, ".6f") if max_skew is not None else None),
        "runtime_config": Path(runtime_config_name).name,
        "cash_snapshots": portfolio_evidence.get("cash_snapshots", {}),
    }


def _unavailable_evidence(
    message: str,
    *,
    accounts: Sequence[str],
    reason_code: str = "PORTFOLIO_EVIDENCE_UNAVAILABLE",
) -> dict[str, Any]:
    return {
        "schema_version": PORTFOLIO_EVIDENCE_VERSION,
        "success": True,
        "status": "unavailable",
        "freshness": {
            "status": "unavailable",
            "trust_status": "unavailable",
            "observed_at_utc": None,
            "dataset_ids": [],
            "reason_codes": [reason_code],
        },
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "accounts": list(accounts),
            "reporting_currency": "CNY",
        },
        "snapshot": {},
        "holdings": [],
        "quotes": [],
        "account_status": [],
        "warnings": [message],
    }


def _amount(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _read_futu_quotes(
    codes: Sequence[str],
    *,
    runtime_config: Mapping[str, Any],
    contexts: Mapping[str, Mapping[str, Any]],
    fx_observation: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read one OpenD price per Futu symbol using the scenario FX observation."""

    if not codes:
        return [], []
    route = resolve_futu_quote_route(runtime_config)
    if route.status == "missing":
        endpoints = {
            (str(settings.get("host") or "").strip(), str(settings.get("port") or "").strip())
            for account in contexts
            for settings in [infer_futu_portfolio_settings(runtime_config, account=account)]
        }
        if len(endpoints) != 1:
            return [], ["Futu quote route unavailable: account endpoints conflict or are missing"]
        host, port = next(iter(endpoints))
        if not host or not port:
            return [], ["Futu quote route unavailable: account endpoint is missing"]
    elif route.ok:
        host, port = route.host, route.port
    else:
        return [], [f"Futu quote route unavailable: {', '.join(route.errors)}"]
    underliers = {}
    warnings: list[str] = []
    for code in codes:
        try:
            underliers[code] = normalize_underlier(code, base_dir=repo_base())
        except ValueError:
            warnings.append(f"{code}: Futu quote symbol unsupported")
    if not underliers:
        return [], warnings
    observed_rates = fx_observation.get("rates") if isinstance(fx_observation, Mapping) else None
    rates: dict[str, Decimal] = {"CNY": Decimal(1)}
    if isinstance(observed_rates, Mapping):
        for currency in {item.currency for item in underliers.values()}:
            rate = _amount(observed_rates.get(f"{currency}CNY"))
            if rate is not None and rate > 0:
                rates[currency] = rate
    gateway = None
    observations = {}
    try:
        gateway = build_ready_futu_quote_gateway(
            host=str(host), port=int(port), is_option_chain_cache_enabled=False
        )
        for market in {item.market for item in underliers.values()}:
            market_codes = [item.code for item in underliers.values() if item.market == market]
            observations.update(
                get_underlier_observations_opend(
                    gateway,
                    market_codes,
                    market=market,
                    base_dir=repo_base(),
                    snapshot_limit=resolve_opend_fetch_limits(dict(runtime_config)).market_snapshot,
                    snapshot_batch_size=DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT,
                )
            )
    except Exception as exc:
        warnings.append(f"Futu quote read failed: {exc}")
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                pass
    quotes: list[dict[str, Any]] = []
    for code, underlier in underliers.items():
        observed = observations.get(underlier.code)
        price = _amount(observed.last_price) if observed is not None else None
        observed_at = observed.observed_at_utc if observed is not None else None
        age = observed.age_seconds if observed is not None else None
        if (
            observed is None
            or observed.code != underlier.code
            or observed.market != underlier.market
            or (observed.status not in {"ready", "market_closed"} and observed.reason_code != "underlier_quote_stale")
            or observed.sec_status != "NORMAL"
            or observed.suspension is not False
            or price is None
            or price <= 0
            or not observed_at
            or age is None
            or age < 0
            or age > 7 * 86400
        ):
            warnings.append(f"{code}: Futu quote unavailable ({observed.reason_code if observed else 'missing'})")
            continue
        is_stale = observed.status != "ready" or age > 300
        if is_stale:
            warnings.append(f"{code}: Futu quote is dated ({observed_at})")
        rate = rates.get(underlier.currency)
        if rate is None:
            warnings.append(f"{code}: Futu quote FX evidence missing")
        quotes.append(
            {
                "code": code,
                "currency": underlier.currency,
                "price_native": str(price),
                "price_cny": str(price * rate) if rate is not None else None,
                "exchange_rate_to_cny": str(rate) if rate is not None else None,
                "source": "futu_opend_market_snapshot",
                "observed_at": observed_at,
                "is_stale": is_stale,
            }
        )
    return quotes, warnings


def _futu_evidence(accounts: Sequence[str], contexts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    observed = {account: (context.get("position_snapshot_input") or {}).get("observed_at_utc") for account, context in contexts.items()}
    now = datetime.now(timezone.utc)
    stale_accounts = [
        account
        for account, raw_time in observed.items()
        if (source_time := _iso_datetime(raw_time)) is None
        or not 0 <= (now - source_time).total_seconds() <= 300
    ]
    stale_accounts = sorted(set(stale_accounts) | {account for account, context in contexts.items() if not cash_snapshot_is_usable(context)})
    snapshot_ids = {
        account: (context.get("position_snapshot_input") or {}).get("snapshot_id")
        for account, context in contexts.items()
    }
    snapshot_id = hashlib.sha256(
        json.dumps({"observed": observed, "positions": snapshot_ids}, sort_keys=True, default=str).encode()
    ).hexdigest()[:20]
    observed_at = max((str(value) for value in observed.values() if value), default=None)
    return {
        "schema_version": PORTFOLIO_EVIDENCE_VERSION,
        "success": True,
        "status": "partial" if stale_accounts else "complete",
        "freshness": {
            "status": "stale" if stale_accounts else "fresh",
            "trust_status": "partial" if stale_accounts else "trusted",
            "observed_at_utc": observed_at,
            "dataset_ids": ["futu.positions", "futu.cash", "futu.market_snapshot"],
            "reason_codes": ["FUTU_SNAPSHOT_TIME_INVALID"] if stale_accounts else [],
        },
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"accounts": list(accounts), "reporting_currency": "CNY"},
        "snapshot": {"snapshot_id": f"futu-{snapshot_id}", "observed_at": observed_at},
        "holdings": [],
        "quotes": [],
        "account_status": [{"account": account, "status": "partial" if account in stale_accounts else "complete"} for account in accounts],
        "warnings": [f"{account}: Futu snapshot time is missing or stale" for account in stale_accounts],
    }


def _futu_context_error(account: str, context: Mapping[str, Any]) -> str | None:
    cash = context.get("cash_by_currency")
    snapshot = context.get("position_snapshot_input")
    if not isinstance(cash, Mapping) or not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("rows"), list):
        return f"{account}: Futu cash or stock snapshot is missing"
    if not cash_snapshot_is_usable(context):
        return f"{account}: Futu cash snapshot is incomplete"
    if snapshot.get("errors") or snapshot.get("completeness") != "complete" or (snapshot.get("quality") or {}).get("status") != "ready":
        return f"{account}: Futu stock snapshot has scope errors"
    filters = context.get("filters")
    if isinstance(filters, Mapping) and filters.get("account") != account:
        return f"{account}: Futu account identity mismatch"
    return None


def _futu_holdings(
    account: str,
    context: Mapping[str, Any],
    quotes: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    if issue := _futu_context_error(account, context):
        raise ValueError(issue)
    cash = context["cash_by_currency"]
    snapshot = context["position_snapshot_input"]
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    fx = context.get("exchange_rates")
    rates = fx.get("rates") if isinstance(fx, Mapping) else None
    for currency, raw_amount in cash.items():
        code = normalize_currency(currency)
        amount = _amount(raw_amount)
        if not code or amount is None:
            raise ValueError(f"{account}: Futu cash row has invalid currency or amount")
        rate = Decimal(1) if code == "CNY" else _amount(rates.get(f"{code}CNY") if isinstance(rates, Mapping) else None)
        if rate is None or rate <= 0:
            warnings.append(f"{account}/{code}: Futu cash FX evidence missing")
            rate = None
        rows.append(
            {
                "account": account,
                "broker": "富途",
                "code": f"{code}-CASH",
                "asset_type": "cash",
                "currency": code,
                "quantity": str(amount),
                "market_value_cny": str(amount * rate) if rate is not None else None,
                "source": "futu_cash_including_mmf",
            }
        )
    for position in snapshot["rows"]:
        if not isinstance(position, Mapping) or not isinstance(position.get("instrument_ref"), Mapping):
            raise ValueError(f"{account}: Futu position snapshot row is invalid")
        instrument = position["instrument_ref"]
        if instrument.get("asset_type") != "stock":
            continue
        code = canonical_symbol(instrument.get("symbol"))
        shares = _amount(position.get("quantity"))
        side = position.get("position_side")
        if not code or shares is None or shares <= 0 or side not in {"long", "short"}:
            raise ValueError(f"{account}: Futu stock side, symbol or quantity is invalid")
        if side == "short":
            shares = -shares
        currency = normalize_currency(instrument.get("currency"))
        quote = quotes.get(code)
        price_cny = _amount(quote.get("price_cny")) if isinstance(quote, Mapping) else None
        if (
            not currency
            or not isinstance(quote, Mapping)
            or normalize_currency(quote.get("currency")) != currency
            or price_cny is None
            or price_cny <= 0
        ):
            price_cny = None
        source_row = position.get("source_row")
        rows.append(
            {
                "account": account,
                "broker": "富途",
                "code": code,
                "name": source_row.get("stock_name") if isinstance(source_row, Mapping) else code,
                "asset_type": "stock",
                "currency": currency,
                "quantity": str(shares),
                "market_value_cny": str(shares * price_cny) if price_cny is not None else None,
                "source": "futu_positions",
            }
        )
    return rows, warnings


def query_portfolio_assignment_scenario(
    accounts: Sequence[str],
) -> dict[str, Any]:
    """Read one current snapshot and return the assignment scenario.

    This public application function is read-only. It never applies assignment
    events or mutates portfolio holdings.
    """

    normalized_accounts = normalize_assignment_accounts(accounts)
    options_observed_at = datetime.now(timezone.utc).isoformat()
    try:
        option_positions, runtime_config_name, runtime_config = _load_runtime_and_positions(normalized_accounts)
    except (AssignmentScenarioInputError, AgentToolError):
        raise
    except Exception as exc:
        evidence = _unavailable_evidence(
            f"option position ledger read failed: {exc}",
            accounts=normalized_accounts,
        )
        snapshot = _snapshot_payload(
            accounts=normalized_accounts,
            option_positions=[],
            portfolio_evidence=evidence,
            options_observed_at=options_observed_at,
            runtime_config_name="unknown",
        )
        return project_assignment_scenario(
            accounts=normalized_accounts,
            portfolio_evidence=evidence,
            option_positions=[],
            snapshot=snapshot,
        )
    runtime_root = Path(runtime_config_name).parent if Path(runtime_config_name).is_absolute() else repo_base()
    futu_contexts: dict[str, dict[str, Any]] = {}
    futu_error: str | None = None
    fx_snapshot: Mapping[str, Any] | None = None
    capacity_fx: Mapping[str, Any] | None = None
    try:
        fx_snapshot = current_exchange_rate_snapshot(
            cache_path=runtime_root / "output_shared" / "state" / "rate_cache.json",
            write_cache=False,
        )
        fx_observation = project_exchange_rate_snapshot(fx_snapshot, purpose="display")
        capacity_fx = project_exchange_rate_snapshot(fx_snapshot, purpose="capacity")
    except Exception:
        fx_observation = None
    try:
        for account in normalized_accounts:
            futu_contexts[account] = load_account_portfolio_context(
                runtime_config=runtime_config, market="富途", portfolio_source="futu",
                state_dir=runtime_root / "output_accounts" / account / "state",
                log=lambda _: None, fetch_futu_portfolio_context_fn=fetch_futu_portfolio_context,
                exchange_rate_cache_path=runtime_root / "output_shared" / "state" / "rate_cache.json",
                write_cache=False, required_position_asset_types=("stock",),
                account=account,
                exchange_rate_observation=fx_observation,
            )
    except Exception as exc:
        futu_error = f"Futu portfolio read failed: {exc}"
    if not futu_error:
        for account, context in futu_contexts.items():
            if issue := _futu_context_error(account, context):
                futu_error = issue
                break
    codes = {
        canonical_symbol(row.get("symbol"))
        for row in option_positions
        if normalize_broker(row.get("broker")) == "富途"
    }
    if not futu_error:
        for context in futu_contexts.values():
            snapshot = context.get("position_snapshot_input")
            if isinstance(snapshot, Mapping):
                for row in snapshot.get("rows") or []:
                    if isinstance(row, Mapping) and isinstance(row.get("instrument_ref"), Mapping):
                        codes.add(canonical_symbol(row["instrument_ref"].get("symbol")))
    supplemental_codes = sorted(code for code in codes if code)
    if not futu_error and len(supplemental_codes) > MAX_SUPPLEMENTAL_CODES:
        raise AssignmentScenarioInputError(f"portfolio references more than {MAX_SUPPLEMENTAL_CODES} underlyings")
    if futu_error:
        evidence = _unavailable_evidence(
            futu_error,
            accounts=normalized_accounts,
            reason_code="FUTU_PORTFOLIO_UNAVAILABLE",
        )
    else:
        include_pm = (
            isinstance(runtime_config.get("portfolio"), Mapping)
            and isinstance(runtime_config["portfolio"].get("holdings"), Mapping)
            and runtime_config["portfolio"]["holdings"].get("enabled") is True
        )
        evidence = _futu_evidence(normalized_accounts, futu_contexts)
        observed_rates = fx_observation.get("rates") if isinstance(fx_observation, Mapping) else None
        evidence["fx_rates_to_cny"] = dict(observed_rates) if isinstance(observed_rates, Mapping) else {}
        capacity_rates = capacity_fx.get("rates") if isinstance(capacity_fx, Mapping) else None
        evidence["capacity_fx_rates_to_cny"] = dict(capacity_rates) if isinstance(capacity_rates, Mapping) else {}
        evidence["fx_observation"] = {
            "source": fx_observation.get("source"),
            "timestamp": fx_observation.get("timestamp"),
            "status": exchange_rate_observation_status(fx_observation, max_age_hours=24),
            "snapshot_sha256": canonical_sha256(fx_snapshot) if fx_snapshot is not None else None,
            "evaluated_at_utc": fx_observation.get("evaluated_at_utc"),
            "calendar": fx_observation.get("calendar"),
            "pairs": fx_observation.get("pairs"),
        } if isinstance(fx_observation, Mapping) else {"status": "unavailable"}
        pm_holdings: list[Any] = []
        pm_issues: list[str] = []
        pm_status: str | None = None
        pm_inventory: dict[str, list[dict[str, Any]]] | None = None
        pm_counts: Mapping[str, Any] | None = None
        pm_quality: Mapping[str, Any] | None = None
        pm_quote_provenance: list[dict[str, Any]] = []
        try:
            if include_pm:
                approved = approved_non_futu_brokers(runtime_config, normalized_accounts)
                if approved is None:
                    raise ValueError("PM non-Futu approved broker set is missing; re-preview and confirm Holdings")
                pm_evidence = read_portfolio_valuation_evidence(
                    accounts=normalized_accounts,
                    supplemental_codes=[],
                    runtime_config=runtime_config,
                    holdings_scope="non_futu",
                )
                pm_inventory = non_futu_broker_inventory(pm_evidence, normalized_accounts)
                pm_counts = pm_evidence["scope"]["holding_counts"]
                new_brokers = {
                    account: [row["broker"] for row in pm_inventory[account] if row["classification"] == "non_futu" and row["broker"] not in approved[account]]
                    for account in normalized_accounts
                }
                if any(new_brokers.values()):
                    raise ValueError(f"PM non-Futu broker names changed: {new_brokers}; re-preview and confirm Holdings")
                if pm_evidence.get("status") not in {"complete", "partial"}:
                    raise ValueError("PM non-Futu valuation is unavailable")
                pm_holdings = list(pm_evidence.get("holdings") or [])
                evidence["pm_snapshot_id"] = pm_evidence["snapshot"]["snapshot_id"]
                evidence["pm_observed_at"] = pm_evidence["snapshot"].get("observed_at") or pm_evidence["snapshot"].get(
                    "observed_at_utc"
                )
                pm_status = str(pm_evidence["status"])
                pm_issues.extend(pm_evidence.get("warnings") or [])
                pm_quality = pm_evidence.get("freshness") or {}
                pm_quote_provenance = [
                    {key: row.get(key) for key in ("code", "source", "fetched_at_utc", "cache_expires_at_utc", "market_as_of", "fx_evidence")}
                    for row in pm_evidence.get("quotes") or [] if isinstance(row, Mapping)
                ]
                if pm_status == "partial" or pm_quality.get("status") != "fresh" or pm_quality.get("trust_status") != "trusted":
                    pm_issues.append("PM non-Futu valuation evidence is partial")
        except AssignmentScenarioInputError:
            raise
        except (PortfolioEvidenceReadError, ValueError) as exc:
            pm_issues.append(str(exc))
        finally:
            futu_quotes, quote_issues = _read_futu_quotes(
                supplemental_codes,
                runtime_config=runtime_config,
                contexts=futu_contexts,
                fx_observation=fx_observation,
            )
            evidence["quotes"] = futu_quotes
            quotes: dict[str, Mapping[str, Any]] = {}
            for row in futu_quotes:
                if isinstance(row, Mapping) and (code := canonical_symbol(row.get("code") or row.get("symbol"))):
                    quotes.setdefault(code, row)
            holdings: list[dict[str, Any]] = []
            warnings = list(evidence.get("warnings") or []) + pm_issues + quote_issues
            for account, context in futu_contexts.items():
                try:
                    rows, issues = _futu_holdings(account, context, quotes)
                except ValueError as exc:
                    evidence = _unavailable_evidence(
                        str(exc),
                        accounts=normalized_accounts,
                        reason_code="FUTU_PORTFOLIO_INVALID",
                    )
                    break
                holdings.extend(rows)
                warnings.extend(issues)
            else:
                if include_pm and pm_status is not None:
                    for row in pm_holdings:
                        if not isinstance(row, Mapping):
                            warnings.append("PM holding row is invalid; skipped")
                            continue
                        holdings.append(dict(row))
                evidence["holdings"] = holdings
                evidence["holdings_sources"] = ["futu"] + (["pm_non_futu"] if include_pm and pm_status is not None else [])
                evidence["pm_supplement"] = {
                    "enabled": include_pm,
                    "status": pm_status or ("missing" if include_pm else "disabled"),
                    "included_rows": len(pm_holdings),
                    "holding_counts": pm_counts,
                    "broker_inventory": pm_inventory,
                    "freshness": pm_quality,
                    "quote_provenance": pm_quote_provenance,
                }
                evidence["warnings"] = warnings
                if warnings and evidence.get("status") == "complete":
                    evidence["status"] = "partial"
                if warnings:
                    evidence["freshness"] = {
                        **(evidence.get("freshness") or {}),
                        "status": "stale" if evidence.get("freshness", {}).get("status") == "stale" or any(row.get("is_stale") for row in futu_quotes) else "fresh",
                        "trust_status": "partial",
                        "reason_codes": list(dict.fromkeys(["SCENARIO_SOURCE_PARTIAL"] + [str(item) for item in warnings])),
                    }
                evidence["futu_observed_at_by_account"] = {
                    account: context.get("source_observed_at") for account, context in futu_contexts.items()
                }

    evidence["cash_snapshots"] = {account: context.get("cash_snapshot") for account, context in futu_contexts.items()}
    snapshot = _snapshot_payload(
        accounts=normalized_accounts,
        option_positions=option_positions,
        portfolio_evidence=evidence,
        options_observed_at=options_observed_at,
        runtime_config_name=runtime_config_name,
    )
    return project_assignment_scenario(
        accounts=normalized_accounts,
        portfolio_evidence=evidence,
        option_positions=option_positions,
        snapshot=snapshot,
    )


def render_assignment_scenario_text(result: Mapping[str, Any]) -> str:
    scope = result.get("scope") if isinstance(result.get("scope"), Mapping) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    cash = result.get("cash_coverage") if isinstance(result.get("cash_coverage"), Mapping) else {}
    distribution = result.get("distribution") if isinstance(result.get("distribution"), Mapping) else {}
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), Mapping) else {}
    supplement = snapshot.get("pm_supplement") if isinstance(snapshot.get("pm_supplement"), Mapping) else {}
    pm_counts = supplement.get("holding_counts") if isinstance(supplement.get("holding_counts"), Mapping) else {}
    excluded = sum(
        count.get("excluded_futu", 0) + count.get("excluded_unknown_broker", 0)
        for count in pm_counts.values() if isinstance(count, Mapping)
    )
    lines = [
        "# 指派后资产分布（不含 Long Option）",
        "",
        f"- 状态：{result.get('status')}",
        f"- 账户：{', '.join(scope.get('accounts') or [])}",
        f"- PM 非富途补充：{'启用' if supplement.get('enabled') else '关闭'}（{supplement.get('status', 'disabled')}）；纳入 {supplement.get('included_rows', 0)} 行，排除 {excluded} 行",
        (
            f"- 全部券商指派：{summary.get('assignment_count', 0)} 笔"
            f"（CSP {summary.get('short_put_count', 0)}；"
            f"CC {summary.get('short_call_count', 0)}）"
        ),
        "",
        "## 仅富途期权资金覆盖（跨账户、币种 CNY 经济汇总）",
        "",
        *( ["- 资金能力汇率：不可用"] if cash.get("fx_status") == "unavailable" else [] ),
        f"- 现金 + MMF：{cash.get('available_cash_and_mmf_cny') or '-'}",
        f"- 富途 CSP 指派需求：{cash.get('gross_put_requirement_cny') or '-'}",
        f"- CC 回款：{cash.get('call_assignment_inflow_cny') or '-'}",
        f"- 指派后净现金（估算）：{cash.get('ending_cash_net_estimated_cny') or '-'}",
        f"- 终局资金缺口：{cash.get('terminal_funding_gap_cny') or '-'}",
        "",
        "## 指派后分布",
        "",
        "| 类别 | CNY 市值/负债 | 正资产权重 |",
        "|---|---:|---:|",
    ]
    for row in distribution.get("by_category") or []:
        lines.append(
            f"| {row.get('category')} | {row.get('value_cny') or '-'} | {row.get('weight_of_gross_assets') or '-'} |"
        )
    lines.extend(
        [
            "",
            f"- 正资产：{distribution.get('gross_assets_cny') or '-'}",
            f"- 负债：{distribution.get('liabilities_cny') or '-'}",
            f"- 净资产：{distribution.get('net_assets_cny') or '-'}",
        ]
    )
    warnings = list(result.get("warnings") or [])
    if warnings:
        lines.extend(["", "## 告警", ""])
        lines.extend(f"- {item}" for item in warnings)
    return "\n".join(lines) + "\n"


__all__ = [
    "AssignmentScenarioInputError",
    "PortfolioEvidenceReadError",
    "normalize_assignment_accounts",
    "query_portfolio_assignment_scenario",
    "read_portfolio_valuation_evidence",
    "render_assignment_scenario_text",
]
