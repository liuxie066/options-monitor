from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping
from src.application.runtime_trigger_context import resolve_tick_deadline, tick_subprocess_env
from src.infrastructure.external_services import run_command
from src.infrastructure.futu_gateway import check_fetch_control

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_window,
)
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.expiration_dates import MARKET_TIMEZONES, expiration_market_date
from domain.domain.fetch_source import (
    is_futu_fetch_source,
    normalize_fetch_source,
)
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.option_position_identity import normalize_broker
from domain.domain.symbol_identity import (
    canonical_symbol,
    looks_like_option_contract_label,
    resolve_symbol_identity,
    symbol_market,
)
from domain.domain.trade_contract_identity import (
    contract_key,
    normalize_contract_expiration,
    normalize_contract_option_type,
)
from src.application.position_advice_plugin import position_advice_plugin_status
from src.application.config_profiles import apply_profiles
from src.application.config_sections import (
    resolve_templates_config,
    resolve_watchlist_config,
)
from src.application.ledger.api import position_lot_risk_view
from src.application.position_advice_inputs import advice_position_key, position_economics_sha256
from src.application.pipeline_watchlist import (
    resolve_watchlist_item_runtime_config,
)
from src.infrastructure.io_utils import atomic_write_json
from src.application.payload_helpers import required_text
from src.application.opend_utils import normalize_underlier
from src.application.opend_market_snapshot_fetching import trading_calendar_dates
from src.application.market_data_provider import resolve_market_data_provider, provider_by_name, market_data_source
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
    required_data_plan_id,
)
from src.application.required_data_planning import build_required_data_fetch_plan
from functools import partial


_required_text = partial(required_text, error=lambda m: CloseAdviceRequiredDataPlanError(m))


CLOSE_ADVICE_REQUIRED_DATA_PLAN_SCHEMA = "close_advice_required_data_plan.v2"
PLAN_FILE_NAME = "close_advice_required_data_plan.json"
_ACCOUNT_STATUSES = frozenset(
    {"not_applicable", "ready", "partial", "unavailable"}
)
_PLAN_STATUSES = frozenset({"complete", "partial", "failed"})


class CloseAdviceRequiredDataPlanError(RuntimeError):
    pass


class ScenarioInputError(CloseAdviceRequiredDataPlanError):
    """The confirmed scenario input is invalid; no evidence may be read."""


class ScenarioEvidenceError(CloseAdviceRequiredDataPlanError):
    """Quote discovery or the required-data plan is unavailable."""


def close_advice_market_date(value: datetime, market: str) -> date:
    try:
        result = expiration_market_date(value, market)
    except ValueError as exc:
        raise CloseAdviceRequiredDataPlanError("market date requires aware UTC time and supported market") from exc
    if result is None:
        raise CloseAdviceRequiredDataPlanError("market date requires aware UTC time and supported market")
    return result


def _provider_rows(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        value = value.to_dict("records")
    if isinstance(value, list) and all(isinstance(row, Mapping) for row in value):
        return [dict(row) for row in value]
    raise ValueError("provider rows invalid")


def enrich_close_advice_required_data_plan(
    *,
    plan_path: Path,
    expected_run_id: str,
    gateway_factory: Any = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Seal independent calendar and post-prefetch market-state observations."""
    deadline = resolve_tick_deadline(deadline=deadline)
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    plan = load_close_advice_required_data_plan(path=plan_path, expected_run_id=expected_run_id)
    provider = provider_by_name(plan.get("market_data_provider", "opend"))
    if gateway_factory is None:
        gateway_factory = provider.open_quote_gateway
    groups: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}
    for account in plan["accounts"].values():
        for requirement in account.get("requirements") or []:
            binding = requirement.get("fetch_binding") or {}
            if requirement.get("planning_status") != "ready" or not binding:
                continue
            key = (
                str(requirement["market"]), str(binding["host"]), int(binding["port"]),
                date.fromisoformat(requirement["expiration"]).year,
            )
            groups.setdefault(key, []).append(requirement)
    for (market, host, port, _year), requirements in groups.items():
        start = date.fromisoformat(plan["as_of_market_dates"][market])
        end = max(date.fromisoformat(item["expiration"]) for item in requirements)
        calendar: dict[str, Any] = {
            "trading_calendar_market": market,
            "trading_calendar_as_of_market_date": start.isoformat(),
            "trading_calendar_request_start": start.isoformat(),
            "trading_calendar_request_end": end.isoformat(),
            "trading_calendar_status": "unavailable",
        }
        gateway = None
        try:
            check_fetch_control(deadline, cancelled)
            try:
                gateway = gateway_factory(host=host, port=port)
                if end.year != start.year:
                    raise ValueError("calendar_cross_year_unsupported")
                response = provider.get_trading_days_with_receipt(
                    gateway, market=market, start=start.isoformat(), end=end.isoformat(), **controls,
                )
                dates = trading_calendar_dates(response, start=start, end=end)
                calendar.update({
                    "trading_calendar_status": "ok",
                    "trading_calendar_dates": json.dumps(dates, separators=(",", ":")),
                    "trading_calendar_input_hash": canonical_sha256({
                        "market": market, "start": start.isoformat(), "end": end.isoformat(), "dates": dates,
                    }),
                    "trading_calendar_receipt": {
                        "retcode": 0, "coverage_complete": True, "pagination_complete": True,
                        "page_count": 1, "row_count": len(response["rows"]),
                        "received_at_utc": _utc_iso(datetime.now(timezone.utc)),
                    },
                })
            except Exception as exc:
                calendar["trading_calendar_reason"] = ("calendar_cross_year_unsupported" if end.year != start.year
                                                       else f"calendar_unavailable:{type(exc).__name__}")
            check_fetch_control(deadline, cancelled)
            rights = {"quote_rights_status": "unavailable", "quote_rights_market": market,
                      "quote_rights_underlier": "UNKNOWN", "quote_rights_option": "UNKNOWN"}
            if gateway is not None:
                try:
                    response = provider.get_quote_rights(gateway, market=market, **controls)
                    if isinstance(response, Mapping) and response.get("market") == market:
                        allowed = {"LV1", "LV2", "LV3", "BMP", "NO", "SF"}
                        rights["quote_rights_status"] = "ok" if response.get("status") == "ok" else "unavailable"
                        for scope in ("underlier", "option"):
                            value = str(response.get(f"{scope}_right") or "").upper()
                            rights[f"quote_rights_{scope}"] = value if value in allowed else "UNKNOWN"
                except Exception:
                    pass
            check_fetch_control(deadline, cancelled)
            rights["quote_rights_received_at_utc"] = _utc_iso(datetime.now(timezone.utc))
            states_by_symbol: dict[str, tuple[str, str] | None] = {}
            for requirement in requirements:
                check_fetch_control(deadline, cancelled)
                requirement.update(calendar)
                requirement.update(rights)
                requirement["trading_calendar_expiration"] = requirement["expiration"]
                if gateway is None:
                    continue
                symbol = str(requirement["symbol"])
                if symbol not in states_by_symbol:
                    states_by_symbol[symbol] = None
                    try:
                        code = normalize_underlier(symbol).code
                        state_rows = _provider_rows(provider.get_market_state(gateway, [code], **controls))
                        received = datetime.now(timezone.utc)
                        matching = [row for row in state_rows if str(row.get("code") or "") == code]
                        if len(matching) == 1:
                            states_by_symbol[symbol] = (
                                str(matching[0].get("market_state") or "").strip().upper(),
                                _utc_iso(received),
                            )
                    except Exception:
                        pass
                check_fetch_control(deadline, cancelled)
                state = states_by_symbol[symbol]
                if state is not None:
                    requirement["market_state_after_snapshot"] = state[0]
                    requirement["market_state_received_at_utc"] = state[1]
        finally:
            if gateway is not None:
                try:
                    gateway.close()
                except Exception:
                    pass
    check_fetch_control(deadline, cancelled)
    return publish_close_advice_required_data_plan(path=plan_path, payload=plan)


def enrich_close_advice_required_data_plan_bounded(
    *, plan_path: Path, expected_run_id: str, python: Path, repo_root: Path,
    deadline: float | None = None, cancelled: Callable[[], bool] | None = None,
) -> str | None:
    """Bound the existing worker by both its stage cap and the remaining tick."""
    deadline = resolve_tick_deadline(deadline=deadline)
    try:
        result = run_command(
            [str(python), "-m", "src.application.close_advice_required_data", str(plan_path), expected_run_id],
            cwd=repo_root, capture_output=True, timeout_sec=30,
            env=tick_subprocess_env(deadline=deadline), deadline=deadline, cancelled=cancelled,
        )
        result.check_returncode()
    except subprocess.TimeoutExpired:
        check_fetch_control(deadline, cancelled)
        return "calendar_enrichment_timeout"
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"calendar_enrichment_failed:{type(exc).__name__}"
    return None


def build_close_advice_required_data_plan(
    *,
    run_id: str,
    run_started_at_utc: datetime,
    account_configs: Mapping[str, Mapping[str, Any]],
    base_config: Mapping[str, Any],
    markets_to_run: list[str] | None,
    position_records_by_account: Mapping[str, list[dict[str, Any]]],
    unavailable_by_account: Mapping[str, str] | None = None,
    blocked_markets_by_account: Mapping[str, Mapping[str, str]] | None = None,
    manual_positions_by_account: Mapping[str, list[dict[str, Any]]] | None = None,
    include_roll_candidates: bool = False,
) -> dict[str, Any]:
    run_id_norm = _required_text(run_id, "run_id")
    market_dates = {
        market: close_advice_market_date(run_started_at_utc, market)
        for market in MARKET_TIMEZONES
    }
    market_allow = {
        str(value or "").strip().upper()
        for value in (markets_to_run or [])
        if str(value or "").strip().upper() in {"US", "HK"}
    }
    unavailable = {
        normalize_account(account): str(reason or "position_ledger_unavailable")
        for account, reason in (unavailable_by_account or {}).items()
        if normalize_account(account)
    }
    blocked_markets = {
        normalize_account(account): {
            str(market or "").strip().upper(): str(reason or "").strip()
            for market, reason in reasons.items()
            if str(market or "").strip().upper() in {"US", "HK"}
            and str(reason or "").strip()
        }
        for account, reasons in (blocked_markets_by_account or {}).items()
        if normalize_account(account) and isinstance(reasons, Mapping)
    }
    accounts: dict[str, dict[str, Any]] = {}
    for raw_account in sorted(account_configs):
        account = normalize_account(raw_account)
        if not account:
            continue
        config = dict(account_configs[raw_account])
        close_cfg = (
            config.get("close_advice")
            if isinstance(config.get("close_advice"), Mapping)
            else {}
        )
        enabled = bool(close_cfg.get("enabled", False))
        if not enabled:
            accounts[account] = {
                "close_advice_enabled": False,
                "status": "not_applicable",
                "requirements": [],
                "planning_errors": [],
            }
            continue
        if position_advice_plugin_status(config) != "available":
            accounts[account] = {
                "close_advice_enabled": True, "status": "unavailable", "requirements": [],
                "planning_errors": [{"reason": "position_advice_plugin_unavailable",
                                     "position_lot_id": None, "quote_key": None}],
            }
            continue
        if account in unavailable:
            accounts[account] = {
                "close_advice_enabled": True,
                "status": "unavailable",
                "requirements": [],
                "planning_errors": [
                    {
                        "reason": unavailable[account],
                        "position_lot_id": None,
                        "quote_key": None,
                    }
                ],
            }
            continue

        requirements: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        portfolio_cfg = (
            config.get("portfolio")
            if isinstance(config.get("portfolio"), Mapping)
            else {}
        )
        expected_broker = normalize_broker(portfolio_cfg.get("broker"))
        if (manual_positions_by_account or {}).get(account) and position_records_by_account.get(account):
            raise CloseAdviceRequiredDataPlanError("manual and ledger position modes are exclusive per account")
        positions: list[dict[str, Any]] = []
        for record in position_records_by_account.get(account, []):
            try:
                view = position_lot_risk_view(record)
                market_hint = str(symbol_market(view.canonical_underlying_symbol) or "").upper()
                if market_hint not in market_dates:
                    continue
                as_of_date = market_dates[market_hint]
                view = position_lot_risk_view(record, as_of_date=as_of_date)
                if not view.fields or not view.is_open or int(view.contracts_open or 0) <= 0:
                    continue
                if normalize_account(view.account) != account:
                    continue
                if expected_broker and normalize_broker(view.broker) != expected_broker:
                    continue
                positions.append(view.as_open_position_min(as_of_date=as_of_date))
            except Exception:
                continue
        for position in (manual_positions_by_account or {}).get(account, []):
            if (position.get("input_source") != "user_provided"
                or not str(position.get("input_id") or "").strip()
                or position.get("lot_id") or position.get("record_id")
                or normalize_account(position.get("account")) != account
                or (expected_broker and normalize_broker(position.get("broker")) != expected_broker)):
                raise CloseAdviceRequiredDataPlanError("manual position identity/account/broker mismatch")
            positions.append(dict(position))
        seen_positions: set[str] = set()
        for position in positions:
            position_key = advice_position_key(position)
            if position_key in seen_positions:
                raise CloseAdviceRequiredDataPlanError("duplicate advice position identity")
            if position_key:
                seen_positions.add(position_key)
            if str(position.get("side") or "").strip().lower() != "short":
                continue
            symbol = canonical_symbol(position.get("symbol")) or str(
                position.get("symbol") or ""
            ).strip().upper()
            market = str(symbol_market(symbol) or "").strip().upper()
            if market not in market_dates or (market_allow and market not in market_allow):
                continue
            as_of_date = market_dates[market]
            expiration = normalize_contract_expiration(
                position.get("expiration_ymd")
                or position.get("expiration"),
                fallback_raw=False,
            )
            option_type = normalize_contract_option_type(
                position.get("option_type"),
                fallback_raw=False,
            )
            strike = _canonical_strike(position.get("strike"))
            lot_id = str(position.get("lot_id") or position.get("record_id") or "").strip()
            try:
                expiration_date = datetime.strptime(
                    str(expiration or ""),
                    "%Y-%m-%d",
                ).date()
            except ValueError:
                expiration_date = None
            if expiration_date is None or expiration_date <= as_of_date:
                continue
            if not (position_key and symbol and option_type in {"put", "call"} and strike):
                errors.append(
                    {
                        "reason": "required_data_position_identity_invalid",
                        "position_lot_id": lot_id or None,
                        "input_source": position.get("input_source") or "ledger",
                        "input_id": position.get("input_id"),
                        "quote_key": None,
                    }
                )
                continue
            binding, binding_error = resolve_position_fetch_binding(
                symbol=symbol,
                account_config=config,
                base_config=base_config,
                roll_option_type=option_type if include_roll_candidates else None,
            )
            quote_key = "|".join(
                contract_key(
                    symbol,
                    option_type,
                    expiration,
                    strike,
                    option_type_fallback_raw=False,
                    expiration_fallback_raw=False,
                )
            )
            blocked_reason = blocked_markets.get(account, {}).get(market)
            if blocked_reason:
                errors.append(
                    {
                        "reason": blocked_reason,
                        "position_lot_id": lot_id,
                        "input_source": position.get("input_source") or "ledger",
                        "input_id": position.get("input_id"),
                        "quote_key": quote_key,
                    }
                )
                continue
            requirement: dict[str, Any] = {
                "position_lot_id": lot_id or None,
                "input_source": position.get("input_source") or "ledger",
                "input_id": position.get("input_id"),
                "input_as_of_utc": _utc_iso(position["input_as_of_utc"]) if isinstance(position.get("input_as_of_utc"), datetime) else position.get("input_as_of_utc"),
                "position_economics_sha256": position_economics_sha256(position),
                "market": market,
                "symbol": symbol,
                "option_type": option_type,
                "expiration": expiration,
                "strike": strike,
                "requires_realized_volatility": False,
                "quote_key": quote_key,
                "planning_status": (
                    "ready" if binding_error is None else "unavailable"
                ),
            }
            if binding is not None:
                if include_roll_candidates:
                    max_dte = binding.pop("roll_max_dte")
                    requirement["roll_scope"] = {
                        "max_dte": max_dte,
                        "max_new_expiration": (as_of_date + timedelta(days=max_dte)).isoformat(),
                        "direction": "up" if option_type == "call" else "down",
                    }
                requirement["fetch_binding"] = binding
            if binding_error is not None:
                requirement["planning_reason"] = binding_error
            requirement["requirement_id"] = _requirement_id(
                account=account,
                requirement=requirement,
            )
            requirements.append(requirement)
            if binding_error is not None:
                errors.append(
                    {
                        "reason": binding_error,
                        "position_lot_id": lot_id,
                        "input_source": position.get("input_source") or "ledger",
                        "input_id": position.get("input_id"),
                        "quote_key": quote_key,
                        "requirement_id": requirement["requirement_id"],
                    }
                )
        accounts[account] = {
            "close_advice_enabled": True,
            "status": "ready",
            "requirements": requirements,
            "planning_errors": errors,
        }

    payload = {
        "schema_version": CLOSE_ADVICE_REQUIRED_DATA_PLAN_SCHEMA,
        "market_data_provider": resolve_market_data_provider(base_config).name,
        "run_id": run_id_norm,
        "run_started_at_utc": _utc_iso(run_started_at_utc),
        "as_of_market_dates": {market: day.isoformat() for market, day in market_dates.items()},
        "accounts": accounts,
    }
    return finalize_close_advice_required_data_plan(payload)


def resolve_position_fetch_binding(
    *,
    symbol: str,
    account_config: Mapping[str, Any],
    base_config: Mapping[str, Any],
    roll_option_type: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    symbol_norm = canonical_symbol(symbol) or str(symbol or "").strip().upper()
    for scope, config in (
        ("account", account_config),
        ("base", base_config),
    ):
        profiles = resolve_templates_config(dict(config))
        for raw in resolve_watchlist_config(dict(config)):
            if not isinstance(raw, dict):
                continue
            resolved = resolve_watchlist_item_runtime_config(
                item=raw,
                profiles=profiles,
                apply_profiles_fn=apply_profiles,
            )
            candidate = canonical_symbol(resolved.get("symbol")) or str(
                resolved.get("symbol") or ""
            ).strip().upper()
            if candidate != symbol_norm:
                continue
            fetch_cfg = (
                resolved.get("fetch")
                if isinstance(resolved.get("fetch"), Mapping)
                else {}
            )
            raw_source = str(fetch_cfg.get("source") or "").strip()
            host = str(fetch_cfg.get("host") or "").strip().lower()
            port = _positive_int(fetch_cfg.get("port"))
            if not (raw_source and host and port is not None):
                return None, "required_data_symbol_config_missing"
            provider = resolve_market_data_provider(base_config).name
            source = normalize_fetch_source(market_data_source(provider, raw_source))
            if provider == "opend" and not is_futu_fetch_source(source):
                return None, "required_data_symbol_source_unsupported"
            binding_payload = {
                "source": source,
                "host": host,
                "port": port,
            }
            binding = {
                **binding_payload,
                "config_scope": scope,
                "binding_id": canonical_sha256(binding_payload),
            }
            if roll_option_type in {"put", "call"}:
                defaults = DEFAULT_SELL_CALL_WINDOW if roll_option_type == "call" else DEFAULT_SELL_PUT_WINDOW
                side_cfg = resolved.get("sell_call" if roll_option_type == "call" else "sell_put")
                binding["roll_max_dte"] = resolve_candidate_window(side_cfg, defaults=defaults).max_dte
            return binding, None
    return None, "required_data_symbol_config_missing"


def finalize_close_advice_required_data_plan(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    out = deepcopy(dict(payload))
    accounts_raw = out.get("accounts")
    accounts_in = accounts_raw if isinstance(accounts_raw, Mapping) else {}
    accounts: dict[str, dict[str, Any]] = {}
    for raw_account in sorted(accounts_in):
        account = normalize_account(raw_account)
        raw = accounts_in[raw_account]
        if not account or not isinstance(raw, Mapping):
            continue
        item = deepcopy(dict(raw))
        enabled = bool(item.get("close_advice_enabled", False))
        requirements = [
            dict(requirement)
            for requirement in list(item.get("requirements") or [])
            if isinstance(requirement, Mapping)
        ]
        requirements.sort(key=lambda value: str(value.get("requirement_id") or ""))
        errors = [
            dict(error)
            for error in list(item.get("planning_errors") or [])
            if isinstance(error, Mapping)
        ]
        errors.sort(
            key=lambda value: (
                str(value.get("reason") or ""),
                str(value.get("position_lot_id") or ""),
                str(value.get("quote_key") or ""),
            )
        )
        if not enabled:
            status = "not_applicable"
            requirements = []
            errors = []
        elif str(item.get("status") or "") == "unavailable":
            status = "unavailable"
        elif errors:
            status = "partial"
        else:
            status = "ready"
        accounts[account] = {
            "close_advice_enabled": enabled,
            "status": status,
            "requirements": requirements,
            "planning_errors": errors,
        }

    eligible = [
        item for item in accounts.values() if item["status"] != "not_applicable"
    ]
    ready = [item for item in eligible if item["status"] == "ready"]
    unavailable = [item for item in eligible if item["status"] == "unavailable"]
    partial = [item for item in eligible if item["status"] == "partial"]
    ready_requirements = [
        requirement
        for item in eligible
        for requirement in item["requirements"]
        if str(requirement.get("planning_status") or "ready") == "ready"
    ]
    if not eligible or (len(ready) == len(eligible)):
        status = "complete"
    elif len(unavailable) == len(eligible):
        status = "failed"
    else:
        status = "partial"
    out["accounts"] = accounts
    out["status"] = status
    out["summary"] = {
        "accounts_total": len(accounts),
        "accounts_eligible": len(eligible),
        "accounts_not_applicable": len(accounts) - len(eligible),
        "accounts_ready": len(ready),
        "accounts_partial": len(partial),
        "accounts_unavailable": len(unavailable),
        "requirements_total": sum(
            len(item["requirements"]) for item in eligible
        ),
        "requirements_ready": len(ready_requirements),
    }
    out.pop("content_sha256", None)
    out["content_sha256"] = canonical_sha256(out)
    return out


def publish_close_advice_required_data_plan(
    *,
    path: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    finalized = finalize_close_advice_required_data_plan(payload)
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, finalized, sort_keys=True)
    return finalized


def load_close_advice_required_data_plan(
    *,
    path: Path,
    expected_run_id: str,
) -> dict[str, Any]:
    target = Path(path).resolve()
    try:
        payload_bytes = target.read_bytes()
    except OSError as exc:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan is unreadable"
        ) from exc
    return _load_close_advice_required_data_plan_bytes(
        payload_bytes=payload_bytes,
        expected_run_id=expected_run_id,
    )


def _load_close_advice_required_data_plan_bytes(
    *,
    payload_bytes: bytes,
    expected_run_id: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan must be an object"
        )
    if payload.get("schema_version") != CLOSE_ADVICE_REQUIRED_DATA_PLAN_SCHEMA:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan schema mismatch"
        )
    if str(payload.get("run_id") or "").strip() != _required_text(
        expected_run_id,
        "expected_run_id",
    ):
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan run mismatch"
        )
    status = str(payload.get("status") or "").strip()
    if status not in _PLAN_STATUSES:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan status is invalid"
        )
    content_sha256 = str(payload.get("content_sha256") or "").strip()
    content = {
        key: value
        for key, value in payload.items()
        if key != "content_sha256"
    }
    if not _is_sha256(content_sha256) or canonical_sha256(content) != content_sha256:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan content hash mismatch"
        )
    market_dates = payload.get("as_of_market_dates")
    if not isinstance(market_dates, dict) or set(market_dates) != set(MARKET_TIMEZONES):
        raise CloseAdviceRequiredDataPlanError("close-advice market dates are invalid")
    for raw in market_dates.values():
        try:
            if date.fromisoformat(str(raw)).isoformat() != raw:
                raise ValueError
        except ValueError as exc:
            raise CloseAdviceRequiredDataPlanError("close-advice market date is invalid") from exc
    try:
        started = datetime.fromisoformat(str(payload["run_started_at_utc"]).replace("Z", "+00:00"))
        if any(
            close_advice_market_date(started, market).isoformat() != market_dates[market]
            for market in MARKET_TIMEZONES
        ):
            raise ValueError
    except (KeyError, ValueError, CloseAdviceRequiredDataPlanError) as exc:
        raise CloseAdviceRequiredDataPlanError("close-advice market dates do not match run time") from exc
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan accounts are invalid"
        )
    for account, raw in accounts.items():
        if not normalize_account(account) or not isinstance(raw, dict):
            raise CloseAdviceRequiredDataPlanError(
                "close-advice required-data plan account entry is invalid"
            )
        if str(raw.get("status") or "") not in _ACCOUNT_STATUSES:
            raise CloseAdviceRequiredDataPlanError(
                "close-advice required-data plan account status is invalid"
            )
    return payload


def resolve_bound_close_advice_required_data_plan(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    expected_run_id: str,
    expected_plan_path: Path | None = None,
) -> tuple[dict[str, Any], Path] | None:
    snapshot = resolve_bound_close_advice_required_data_plan_snapshot(
        manifest_path=manifest_path,
        manifest=manifest,
        expected_run_id=expected_run_id,
        expected_plan_path=expected_plan_path,
    )
    if snapshot is None:
        return None
    payload, candidate, _payload_bytes = snapshot
    return payload, candidate


def resolve_bound_close_advice_required_data_plan_snapshot(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    expected_run_id: str,
    expected_plan_path: Path | None = None,
) -> tuple[dict[str, Any], Path, bytes] | None:
    relpath_raw = manifest.get("close_advice_required_data_plan_relpath")
    sha_raw = manifest.get("close_advice_required_data_plan_sha256")
    if relpath_raw in (None, "") and sha_raw in (None, ""):
        return None
    relpath = Path(_required_text(relpath_raw, "plan relpath"))
    if relpath.is_absolute() or ".." in relpath.parts:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan path is unsafe"
        )
    root = Path(manifest_path).resolve().parent
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan escapes run state"
        ) from exc
    if (
        not candidate.is_file()
        or candidate.is_symlink()
        or (
            expected_plan_path is not None
            and candidate != Path(expected_plan_path).resolve()
        )
    ):
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan binding is unavailable"
        )
    expected_sha = _required_text(sha_raw, "plan sha256")
    try:
        payload_bytes = candidate.read_bytes()
    except OSError as exc:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan binding is unavailable"
        ) from exc
    if not _is_sha256(expected_sha) or hashlib.sha256(
        payload_bytes
    ).hexdigest() != expected_sha:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan file hash mismatch"
        )
    payload = _load_close_advice_required_data_plan_bytes(
        payload_bytes=payload_bytes,
        expected_run_id=expected_run_id,
    )
    return payload, candidate, payload_bytes


def account_requirement_index(
    *,
    payload: Mapping[str, Any],
    account: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], str]:
    account_norm = normalize_account(account)
    raw = (
        (payload.get("accounts") or {}).get(account_norm)
        if isinstance(payload.get("accounts"), Mapping)
        else None
    )
    if not isinstance(raw, Mapping):
        return {}, {}, "unavailable"
    requirements: dict[str, dict[str, Any]] = {}
    reasons: dict[str, str] = {}
    for item in list(raw.get("requirements") or []):
        if not isinstance(item, Mapping):
            continue
        lot_id = advice_position_key({**item, "lot_id": item.get("position_lot_id")})
        if not lot_id:
            continue
        requirement = dict(item)
        if lot_id in requirements:
            raise CloseAdviceRequiredDataPlanError("duplicate advice requirement identity")
        requirements[lot_id] = requirement
        reason = str(requirement.get("planning_reason") or "").strip()
        if reason:
            reasons[lot_id] = reason
    for item in list(raw.get("planning_errors") or []):
        if not isinstance(item, Mapping):
            continue
        lot_id = advice_position_key({**item, "lot_id": item.get("position_lot_id")})
        reason = str(item.get("reason") or "").strip()
        if lot_id and reason:
            reasons.setdefault(lot_id, reason)
    return requirements, reasons, str(raw.get("status") or "unavailable")


def _requirement_id(
    *,
    account: str,
    requirement: Mapping[str, Any],
) -> str:
    binding = (
        requirement.get("fetch_binding")
        if isinstance(requirement.get("fetch_binding"), Mapping)
        else {}
    )
    return canonical_sha256(
        {
            "account": account,
            "position_lot_id": requirement.get("position_lot_id"),
            "input_source": requirement.get("input_source"),
            "input_id": requirement.get("input_id"),
            "input_as_of_utc": requirement.get("input_as_of_utc"),
            "position_economics_sha256": requirement.get("position_economics_sha256"),
            "quote_key": requirement.get("quote_key"),
            "requires_realized_volatility": bool(
                requirement.get("requires_realized_volatility")
            ),
            "binding_id": binding.get("binding_id"),
        }
    )


def _canonical_strike(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return format(number, ".12g")


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise CloseAdviceRequiredDataPlanError(
            "run_started_at_utc must be timezone-aware"
        )
    return value.isoformat().replace("+00:00", "Z")


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


SCENARIO_PLAN_SCHEMA = "group_query_scenario_plan.v1"

_SCENARIO_OPERATIONS = {"close": None, "roll-down": "put", "roll-up": "call"}
_SCENARIO_PLAN_KEYS = frozenset({
    "schema", "operation", "input_id", "input_as_of_utc", "market", "symbol",
    "fetch_symbol", "as_of_market_date", "contracts", "original_contract",
    "opening_premium", "opened_at", "search_bounds", "limit", "quote_route",
    "policy_sha256", "strategy_version", "fee_model_id", "fee_model_version",
    "fetch_source", "fetch_host", "fetch_port", "requirements", "fetch_plan",
    "expected_fetch_contract", "global_required_data_plan",
})
_CONTRACT_KEYS = frozenset({"option_type", "expiration", "strike"})


def _scenario_error(message: str) -> CloseAdviceRequiredDataPlanError:
    return ScenarioInputError(message)


def _scenario_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _scenario_error(f"scenario {name} must be a nonempty string")
    return value


def _scenario_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CONTRACT_KEYS:
        raise _scenario_error("scenario contract must contain option_type, expiration and strike")
    option_type = value.get("option_type")
    if option_type not in {"put", "call"}:
        raise _scenario_error("scenario contract option_type is invalid")
    expiration = normalize_contract_expiration(value.get("expiration"))
    if not expiration:
        raise _scenario_error("scenario contract expiration is invalid")
    strike = value.get("strike")
    if isinstance(strike, bool) or not isinstance(strike, (int, float)):
        raise _scenario_error("scenario contract strike is invalid")
    strike_float = float(strike)
    if not strike_float > 0 or not math.isfinite(strike_float):
        raise _scenario_error("scenario contract strike is invalid")
    return {"option_type": str(option_type), "expiration": expiration,
            "strike": strike_float}


def _scenario_market_symbol(value: Any) -> tuple[str, str]:
    text = _scenario_text(value, "symbol")
    if looks_like_option_contract_label(text):
        raise _scenario_error("scenario symbol must be the underlier")
    identity = resolve_symbol_identity(text)
    if identity is None or identity.market not in MARKET_TIMEZONES:
        raise _scenario_error("scenario symbol is not a recognized US or HK security")
    return identity.market, identity.canonical


def _scenario_policy_digest(
    *, policy_sha256: Any, strategy_version: Any, fee_model_id: Any,
    fee_model_version: Any,
) -> dict[str, str]:
    values = {
        "policy_sha256": policy_sha256, "strategy_version": strategy_version,
        "fee_model_id": fee_model_id, "fee_model_version": fee_model_version,
    }
    if not _is_sha256(policy_sha256):
        raise _scenario_error("scenario policy_sha256 is invalid")
    for name in ("strategy_version", "fee_model_id", "fee_model_version"):
        _scenario_text(values[name], f"scenario {name}")
    return {name: str(value).strip() for name, value in values.items()}


def build_user_provided_scenario_plan(
    *,
    scenario: Mapping[str, Any],
    policy_sha256: str,
    strategy_version: str,
    fee_model_id: str,
    fee_model_version: str,
    quote_route: Mapping[str, Any],
    runtime_root: Path,
    required_data_dir: Path,
    now_utc: datetime,
    expiration_discovery_cache: Any = None,
    spot_observation_cache: Any = None,
) -> dict[str, Any]:
    """Plan one account-free scenario fetch without creating account lots.

    Expiration discovery and the spot observation follow the existing
    required-data planner; the scenario never indexes accountRequirements.
    """
    if not isinstance(scenario, Mapping):
        raise _scenario_error("scenario must be a mapping")
    policy = _scenario_policy_digest(
        policy_sha256=policy_sha256, strategy_version=strategy_version,
        fee_model_id=fee_model_id, fee_model_version=fee_model_version,
    )
    operation = _scenario_text(scenario.get("operation"), "operation")
    if operation not in _SCENARIO_OPERATIONS:
        raise _scenario_error("scenario operation is not supported")
    input_id = _scenario_text(scenario.get("input_id"), "input_id")
    input_as_of_utc = _scenario_text(scenario.get("input_as_of_utc"), "input_as_of_utc")
    position = scenario.get("position")
    if not isinstance(position, Mapping):
        raise _scenario_error("scenario position is required")
    market, symbol = _scenario_market_symbol(position.get("symbol"))
    fetch_symbol = canonical_symbol(symbol)
    if not fetch_symbol:
        raise _scenario_error("scenario symbol identity is unavailable")
    contract = _scenario_contract(position.get("contract"))
    option_type = _SCENARIO_OPERATIONS[operation]
    if option_type is not None and contract["option_type"] != option_type:
        raise _scenario_error("scenario position direction contradicts the operation")
    if position.get("side") != "short":
        raise _scenario_error("scenario position side must be short")
    contracts = _positive_int(position.get("contracts"))
    if contracts is None:
        raise _scenario_error("scenario position contracts must be a positive integer")
    as_of_date = close_advice_market_date(now_utc, market)
    old_dte = (date.fromisoformat(contract["expiration"]) - as_of_date).days
    if old_dte < 0:
        raise _scenario_error("scenario contract has already expired")
    search_bounds: dict[str, Any] | None = None
    sell_put_cfg: dict[str, Any] = {}
    sell_call_cfg: dict[str, Any] = {}
    raw_premium = scenario.get("opening_premium")
    if operation == "close":
        if not isinstance(raw_premium, Mapping):
            raise _scenario_error("close scenario requires an opening premium")
        if raw_premium.get("unit") not in {"per_share", "per_contract", "total"}:
            raise _scenario_error("opening premium unit is invalid")
        amount = raw_premium.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not float(amount) > 0:
            raise _scenario_error("opening premium amount is invalid")
        if raw_premium.get("currency") not in {"USD", "HKD"}:
            raise _scenario_error("opening premium currency is invalid")
    elif raw_premium is not None:
        raise _scenario_error("roll scenarios do not accept an opening premium")
    if operation != "close" and scenario.get("opened_at") is not None:
        raise _scenario_error("roll scenarios do not accept opened_at")
    if operation == "close" and scenario.get("search_bounds") is not None:
        raise _scenario_error("close scenario does not accept search_bounds")
    if operation != "close":
        raw_bounds = scenario.get("search_bounds")
        if not isinstance(raw_bounds, Mapping):
            raise _scenario_error("scenario search_bounds are required")
        strike_key = "min_new_strike" if option_type == "put" else "max_new_strike"
        if set(raw_bounds) - {"max_new_expiration", strike_key}:
            raise _scenario_error("scenario search_bounds contain unsupported fields")
        max_expiration = normalize_contract_expiration(raw_bounds.get("max_new_expiration"))
        if not max_expiration:
            raise _scenario_error("scenario max_new_expiration is invalid")
        max_dte = (date.fromisoformat(max_expiration) - as_of_date).days
        if max_dte < old_dte:
            raise _scenario_error("scenario max_new_expiration precedes the old contract")
        search_bounds = {"max_new_expiration": max_expiration}
        boundary = raw_bounds.get(strike_key)
        if boundary is not None:
            if isinstance(boundary, bool) or not isinstance(boundary, (int, float)):
                raise _scenario_error(f"scenario {strike_key} is invalid")
            boundary = float(boundary)
            if boundary <= 0 or not math.isfinite(boundary):
                raise _scenario_error(f"scenario {strike_key} is invalid")
            search_bounds[strike_key] = boundary
            if option_type == "put" and boundary >= contract["strike"]:
                raise _scenario_error("scenario min_new_strike must be below the old strike")
            if option_type == "call" and boundary <= contract["strike"]:
                raise _scenario_error("scenario max_new_strike must be above the old strike")
        if option_type == "put":
            sell_put_cfg = {"enabled": True, "min_dte": old_dte, "max_dte": max_dte,
                            "max_strike": contract["strike"]}
            if boundary is not None:
                sell_put_cfg["min_strike"] = boundary
        else:
            sell_call_cfg = {"enabled": True, "min_dte": old_dte, "max_dte": max_dte,
                             "min_strike": contract["strike"]}
            if boundary is not None:
                sell_call_cfg["max_strike"] = boundary
    limit = scenario.get("limit", 10)
    if isinstance(limit, bool) or type(limit) is not int or not 1 <= limit <= 50:
        raise _scenario_error("scenario limit must be an integer from 1 to 50")
    route = {
        "source": _scenario_text(quote_route.get("source"), "quote source"),
        "host": _scenario_text(quote_route.get("host"), "quote host"),
        "port": _positive_int(quote_route.get("port")),
    }
    if route["port"] is None:
        raise _scenario_error("scenario quote port is invalid")
    fetch_source = normalize_fetch_source(
        market_data_source("opend", route["source"]))
    requirement = {
        "option_type": contract["option_type"],
        "expiration": contract["expiration"],
        "strike": contract["strike"],
        "planning_status": "ready",
    }
    try:
        bundle = build_required_data_fetch_plan(
            base=Path(runtime_root),
            required_data_dir=Path(required_data_dir),
            symbol=fetch_symbol,
            limit_expirations=0,
            want_put=bool(sell_put_cfg),
            want_call=bool(sell_call_cfg),
            sell_put_cfg=sell_put_cfg or None,
            sell_call_cfg=sell_call_cfg or None,
            position_requirements=[requirement],
            fetch_host=route["host"],
            fetch_port=int(route["port"]),
            fetch_source=route["source"],
            expiration_discovery_cache=expiration_discovery_cache,
            spot_observation_cache=spot_observation_cache,
            market_data_provider="opend",
        )
        fetch_plan = bundle.to_debug_dict()
        expected_contract = build_required_data_expected_fetch_contract(
            symbol=fetch_symbol,
            fetch_plan=fetch_plan,
            source=fetch_source,
            host=route["host"],
            port=int(route["port"]),
        )
    except ScenarioEvidenceError:
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        raise ScenarioEvidenceError(
            "scenario quote discovery or fetch planning is unavailable") from exc
    plan_symbol = {
        "symbol": fetch_symbol,
        "expected_fetch_contract": expected_contract,
        "fetch_plan": fetch_plan,
        "fetch_binding": expected_contract["fetch_binding"],
    }
    global_plan = {
        "plan_id": required_data_plan_id([plan_symbol]),
        "symbols": [plan_symbol],
    }
    return {
        "schema": SCENARIO_PLAN_SCHEMA,
        "operation": operation,
        "input_id": input_id,
        "input_as_of_utc": input_as_of_utc,
        "market": market,
        "symbol": symbol,
        "fetch_symbol": fetch_symbol,
        "as_of_market_date": as_of_date.isoformat(),
        "contracts": contracts,
        "original_contract": contract,
        "opening_premium": dict(scenario.get("opening_premium") or {}) or None,
        "opened_at": scenario.get("opened_at"),
        "search_bounds": search_bounds,
        "limit": limit,
        "quote_route": route,
        "policy_sha256": policy["policy_sha256"],
        "strategy_version": policy["strategy_version"],
        "fee_model_id": policy["fee_model_id"],
        "fee_model_version": policy["fee_model_version"],
        "fetch_source": fetch_source,
        "fetch_host": route["host"],
        "fetch_port": int(route["port"]),
        "requirements": [requirement],
        "fetch_plan": fetch_plan,
        "expected_fetch_contract": expected_contract,
        "global_required_data_plan": global_plan,
    }


def validate_user_provided_scenario_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Re-validate a stored plan with the same strictness as its builder."""
    if not isinstance(plan, Mapping) or set(plan) != _SCENARIO_PLAN_KEYS:
        raise _scenario_error("scenario plan fields are missing or unsupported")
    if plan.get("schema") != SCENARIO_PLAN_SCHEMA:
        raise _scenario_error("scenario plan schema mismatch")
    operation = plan.get("operation")
    if operation not in _SCENARIO_OPERATIONS:
        raise _scenario_error("scenario plan operation is invalid")
    market, symbol = _scenario_market_symbol(plan.get("symbol"))
    if market != plan.get("market"):
        raise _scenario_error("scenario plan market contradicts its symbol")
    fetch_symbol = plan.get("fetch_symbol")
    if not isinstance(fetch_symbol, str) or canonical_symbol(symbol) != fetch_symbol:
        raise _scenario_error("scenario plan fetch symbol is inconsistent")
    contract = _scenario_contract(plan.get("original_contract"))
    option_type = _SCENARIO_OPERATIONS[operation]
    if option_type is not None and contract["option_type"] != option_type:
        raise _scenario_error("scenario plan contract contradicts the operation")
    if plan.get("contracts") != _positive_int(plan.get("contracts")):
        raise _scenario_error("scenario plan contracts are invalid")
    _scenario_policy_digest(
        policy_sha256=plan.get("policy_sha256"),
        strategy_version=plan.get("strategy_version"),
        fee_model_id=plan.get("fee_model_id"),
        fee_model_version=plan.get("fee_model_version"),
    )
    _scenario_text(plan.get("input_id"), "input id")
    try:
        input_as_of = datetime.fromisoformat(str(plan.get("input_as_of_utc")))
    except ValueError as exc:
        raise _scenario_error("scenario plan input time is invalid") from exc
    if input_as_of.tzinfo is None:
        raise _scenario_error("scenario plan input time must carry an offset")
    try:
        as_of_date = date.fromisoformat(str(plan.get("as_of_market_date")))
    except ValueError as exc:
        raise _scenario_error("scenario plan market date is invalid") from exc
    limit = plan.get("limit")
    if isinstance(limit, bool) or type(limit) is not int or not 1 <= limit <= 50:
        raise _scenario_error("scenario plan limit is invalid")
    premium = plan.get("opening_premium")
    if operation == "close":
        if not isinstance(premium, Mapping):
            raise _scenario_error("close scenario plan requires an opening premium")
        amount = premium.get("amount")
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or not math.isfinite(float(amount)) or float(amount) <= 0):
            raise _scenario_error("scenario plan premium amount is invalid")
        if premium.get("unit") not in {"per_share", "per_contract", "total"}:
            raise _scenario_error("scenario plan premium unit is invalid")
        if premium.get("currency") not in {"USD", "HKD"}:
            raise _scenario_error("scenario plan premium currency is invalid")
    elif premium is not None:
        raise _scenario_error("roll scenario plan does not accept an opening premium")
    opened_at = plan.get("opened_at")
    if opened_at is not None:
        try:
            if date.fromisoformat(str(opened_at)).isoformat() != opened_at:
                raise ValueError("opened_at is not a date")
            opened_date = date.fromisoformat(str(opened_at))
        except ValueError as exc:
            raise _scenario_error("scenario plan opened_at is invalid") from exc
        if opened_date > date.fromisoformat(str(contract["expiration"])):
            raise _scenario_error("scenario plan opened_at is after expiration")
        if opened_date > input_as_of.astimezone(timezone.utc).date():
            raise _scenario_error("scenario plan opened_at is after the input time")
    bounds = plan.get("search_bounds")
    if operation == "close":
        if bounds is not None:
            raise _scenario_error("close scenario plan does not accept search bounds")
    else:
        if not isinstance(bounds, Mapping):
            raise _scenario_error("roll scenario plan requires search bounds")
        try:
            max_expiration = date.fromisoformat(str(bounds.get("max_new_expiration")))
        except ValueError as exc:
            raise _scenario_error("scenario plan max expiration is invalid") from exc
        strike_key = "min_new_strike" if operation == "roll-down" else "max_new_strike"
        boundary = bounds.get(strike_key)
        if set(bounds) - {"max_new_expiration", strike_key} or (
                boundary is not None and (
                    isinstance(boundary, bool) or not isinstance(boundary, (int, float))
                    or not math.isfinite(float(boundary)) or float(boundary) <= 0)):
            raise _scenario_error("scenario plan strike boundary is invalid")
        if boundary is not None:
            if operation == "roll-down" and float(boundary) >= float(contract["strike"]):
                raise _scenario_error("scenario plan strike boundary is not below the old strike")
            if operation == "roll-up" and float(boundary) <= float(contract["strike"]):
                raise _scenario_error("scenario plan strike boundary is not above the old strike")
        if max_expiration < date.fromisoformat(contract["expiration"]):
            raise _scenario_error("scenario plan max expiration precedes the old contract")
    route = plan.get("quote_route")
    if (not isinstance(route, Mapping) or set(route) != {"source", "host", "port"}
            or _positive_int(route.get("port")) is None):
        raise _scenario_error("scenario plan quote route is invalid")
    _scenario_text(route.get("source"), "quote source")
    _scenario_text(route.get("host"), "quote host")
    if (plan.get("fetch_source") != normalize_fetch_source(
            market_data_source("opend", str(route.get("source"))))
            or plan.get("fetch_host") != route.get("host")
            or plan.get("fetch_port") != route.get("port")):
        raise _scenario_error("scenario plan fetch route is inconsistent")
    global_plan = plan.get("global_required_data_plan")
    if not isinstance(global_plan, Mapping):
        raise _scenario_error("scenario plan lacks its global fetch plan")
    symbols = global_plan.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != 1:
        raise _scenario_error("scenario global fetch plan must contain one symbol")
    plan_item = symbols[0]
    if not isinstance(plan_item, Mapping) or plan_item.get("symbol") != fetch_symbol:
        raise _scenario_error("scenario global fetch plan symbol mismatch")
    if global_plan.get("plan_id") != required_data_plan_id([dict(plan_item)]):
        raise _scenario_error("scenario global fetch plan id mismatch")
    expected = plan.get("expected_fetch_contract")
    if not isinstance(expected, Mapping) or dict(plan_item.get("expected_fetch_contract") or {}) != dict(expected):
        raise _scenario_error("scenario expected fetch contract mismatch")
    if plan_item.get("fetch_plan") != plan.get("fetch_plan"):
        raise _scenario_error("scenario fetch plan contradicts its global entry")
    if plan_item.get("fetch_binding") != expected.get("fetch_binding"):
        raise _scenario_error("scenario fetch binding contradicts its contract")
    requirements = plan.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise _scenario_error("scenario plan requires at least one contract requirement")
    for requirement in requirements:
        if not isinstance(requirement, Mapping) or not {"option_type", "expiration", "strike"} <= set(requirement):
            raise _scenario_error("scenario plan requirement is invalid")
    return dict(plan)


def scenario_requirement_index(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return the executable fetch identity for one validated scenario plan."""
    validated = validate_user_provided_scenario_plan(plan)
    global_plan = validated["global_required_data_plan"]
    plan_item = dict(global_plan["symbols"][0])
    return {
        "symbol": validated["symbol"],
        "plan_id": global_plan["plan_id"],
        "fetch_plan": dict(plan_item["fetch_plan"]),
        "expected_fetch_contract": dict(plan_item["expected_fetch_contract"]),
        "fetch_binding": dict(plan_item["fetch_binding"]),
    }


__all__ = [
    "CLOSE_ADVICE_REQUIRED_DATA_PLAN_SCHEMA",
    "CloseAdviceRequiredDataPlanError",
    "PLAN_FILE_NAME",
    "SCENARIO_PLAN_SCHEMA",
    "ScenarioEvidenceError",
    "ScenarioInputError",
    "account_requirement_index",
    "close_advice_market_date",
    "build_close_advice_required_data_plan",
    "build_user_provided_scenario_plan",
    "finalize_close_advice_required_data_plan",
    "load_close_advice_required_data_plan",
    "publish_close_advice_required_data_plan",
    "resolve_bound_close_advice_required_data_plan",
    "resolve_bound_close_advice_required_data_plan_snapshot",
    "resolve_position_fetch_binding",
    "scenario_requirement_index",
    "validate_user_provided_scenario_plan",
]


if __name__ == "__main__":
    enrich_close_advice_required_data_plan(
        plan_path=Path(sys.argv[1]), expected_run_id=sys.argv[2]
    )
