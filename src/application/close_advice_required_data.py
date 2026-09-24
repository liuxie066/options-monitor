from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fetch_source import (
    is_futu_fetch_source,
    normalize_fetch_source,
)
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.option_position_identity import normalize_broker
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from domain.domain.trade_contract_identity import (
    contract_key,
    normalize_contract_expiration,
    normalize_contract_option_type,
)
from src.application.config_profiles import apply_profiles
from src.application.config_sections import (
    resolve_templates_config,
    resolve_watchlist_config,
)
from src.application.ledger.api import position_lot_risk_view
from src.application.pipeline_watchlist import (
    resolve_watchlist_item_runtime_config,
)
from src.infrastructure.io_utils import atomic_write_json
from src.application.payload_helpers import required_text
from src.application.opend_utils import normalize_underlier
from functools import partial


_required_text = partial(required_text, error=lambda m: CloseAdviceRequiredDataPlanError(m))


CLOSE_ADVICE_REQUIRED_DATA_PLAN_SCHEMA = "close_advice_required_data_plan.v2"
PLAN_FILE_NAME = "close_advice_required_data_plan.json"
_MARKET_TIMEZONES = {"US": ZoneInfo("America/New_York"), "HK": ZoneInfo("Asia/Hong_Kong")}
_ACCOUNT_STATUSES = frozenset(
    {"not_applicable", "ready", "partial", "unavailable"}
)
_PLAN_STATUSES = frozenset({"complete", "partial", "failed"})


class CloseAdviceRequiredDataPlanError(RuntimeError):
    pass


def close_advice_market_date(value: datetime, market: str) -> date:
    if value.tzinfo is None or market not in _MARKET_TIMEZONES:
        raise CloseAdviceRequiredDataPlanError("market date requires aware UTC time and supported market")
    return value.astimezone(_MARKET_TIMEZONES[market]).date()


def _provider_rows(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        value = value.to_dict("records")
    if isinstance(value, list) and all(isinstance(row, Mapping) for row in value):
        return [dict(row) for row in value]
    raise ValueError("provider rows invalid")


def _calendar_dates(receipt: Any, *, start: date, end: date) -> list[str]:
    if not isinstance(receipt, Mapping) or (
        receipt.get("retcode") != 0
        or receipt.get("coverage_complete") is not True
        or receipt.get("pagination_complete") is not True
        or receipt.get("page_count") != 1
    ):
        raise ValueError("calendar receipt incomplete")
    dates: set[str] = set()
    for row in _provider_rows(receipt.get("rows")):
        raw = row.get("time") or row.get("date") or row.get("trade_date")
        kind = str(row.get("trade_date_type") or "").strip().upper()
        if not isinstance(raw, str) or kind not in {"WHOLE", "MORNING", "AFTERNOON"}:
            raise ValueError("calendar row invalid")
        day = date.fromisoformat(raw)
        if day.isoformat() != raw or not start <= day <= end:
            raise ValueError("calendar date outside request")
        dates.add(raw)
    if not dates:
        raise ValueError("calendar returned no trading dates")
    return sorted(dates)


def enrich_close_advice_required_data_plan(
    *,
    plan_path: Path,
    expected_run_id: str,
    gateway_factory: Any = None,
) -> dict[str, Any]:
    """Seal independent calendar and post-prefetch market-state observations."""
    if gateway_factory is None:
        from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway
        gateway_factory = build_ready_futu_quote_gateway
    plan = load_close_advice_required_data_plan(path=plan_path, expected_run_id=expected_run_id)
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
        if end.year != start.year:
            calendar["trading_calendar_reason"] = "calendar_cross_year_unsupported"
            for requirement in requirements:
                requirement.update(calendar)
                requirement["trading_calendar_expiration"] = requirement["expiration"]
            continue
        gateway = None
        try:
            gateway = gateway_factory(host=host, port=port)
            response = gateway.get_trading_days_with_receipt(
                market=market, start=start.isoformat(), end=end.isoformat()
            )
            dates = _calendar_dates(response, start=start, end=end)
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
            calendar["trading_calendar_reason"] = f"calendar_unavailable:{type(exc).__name__}"
        states_by_symbol: dict[str, tuple[str, str] | None] = {}
        for requirement in requirements:
            requirement.update(calendar)
            requirement["trading_calendar_expiration"] = requirement["expiration"]
            if gateway is None:
                continue
            symbol = str(requirement["symbol"])
            if symbol not in states_by_symbol:
                states_by_symbol[symbol] = None
                try:
                    code = normalize_underlier(symbol).code
                    state_rows = _provider_rows(gateway.get_market_state([code]))
                    received = datetime.now(timezone.utc)
                    matching = [row for row in state_rows if str(row.get("code") or "") == code]
                    if len(matching) == 1:
                        states_by_symbol[symbol] = (
                            str(matching[0].get("market_state") or "").strip().upper(),
                            _utc_iso(received),
                        )
                except Exception:
                    pass
            state = states_by_symbol[symbol]
            if state is not None:
                requirement["market_state_after_snapshot"] = state[0]
                requirement["market_state_received_at_utc"] = state[1]
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                pass
    return publish_close_advice_required_data_plan(path=plan_path, payload=plan)


def enrich_close_advice_required_data_plan_bounded(
    *, plan_path: Path, expected_run_id: str, python: Path, repo_root: Path,
) -> str | None:
    """Keep a stalled OpenD call from blocking the tick indefinitely."""
    try:
        subprocess.run(
            [str(python), "-m", "src.application.close_advice_required_data", str(plan_path), expected_run_id],
            cwd=repo_root, capture_output=True, check=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
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
) -> dict[str, Any]:
    run_id_norm = _required_text(run_id, "run_id")
    market_dates = {
        market: close_advice_market_date(run_started_at_utc, market)
        for market in _MARKET_TIMEZONES
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
        for record in position_records_by_account.get(account, []):
            try:
                raw_fields = record.get("fields") if isinstance(record.get("fields"), Mapping) else record
                market_hint = str(symbol_market(raw_fields.get("symbol")) or "").upper()
                if market_hint not in market_dates:
                    continue
                as_of_date = market_dates[market_hint]
                view = position_lot_risk_view(record, as_of_date=as_of_date)
            except Exception:
                continue
            if not view.fields or not view.is_open or int(view.contracts_open or 0) <= 0:
                continue
            if normalize_account(view.account) != account:
                continue
            if expected_broker and normalize_broker(view.broker) != expected_broker:
                continue
            position = view.as_open_position_min(as_of_date=as_of_date)
            if str(position.get("side") or "").strip().lower() != "short":
                continue
            symbol = canonical_symbol(position.get("symbol")) or str(
                position.get("symbol") or ""
            ).strip().upper()
            market = str(symbol_market(symbol) or "").strip().upper()
            if market_allow and market not in market_allow:
                continue
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
            if not (lot_id and symbol and option_type in {"put", "call"} and strike):
                errors.append(
                    {
                        "reason": "required_data_position_identity_invalid",
                        "position_lot_id": lot_id or None,
                        "quote_key": None,
                    }
                )
                continue
            binding, binding_error = resolve_position_fetch_binding(
                symbol=symbol,
                account_config=config,
                base_config=base_config,
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
                        "quote_key": quote_key,
                    }
                )
                continue
            requirement: dict[str, Any] = {
                "position_lot_id": lot_id,
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
            source = normalize_fetch_source(raw_source)
            if not is_futu_fetch_source(source):
                return None, "required_data_symbol_source_unsupported"
            binding_payload = {
                "source": source,
                "host": host,
                "port": port,
            }
            return {
                **binding_payload,
                "config_scope": scope,
                "binding_id": canonical_sha256(binding_payload),
            }, None
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
    if not isinstance(market_dates, dict) or set(market_dates) != set(_MARKET_TIMEZONES):
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
            for market in _MARKET_TIMEZONES
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
        lot_id = str(item.get("position_lot_id") or "").strip()
        if not lot_id:
            continue
        requirement = dict(item)
        requirements[lot_id] = requirement
        reason = str(requirement.get("planning_reason") or "").strip()
        if reason:
            reasons[lot_id] = reason
    for item in list(raw.get("planning_errors") or []):
        if not isinstance(item, Mapping):
            continue
        lot_id = str(item.get("position_lot_id") or "").strip()
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


__all__ = [
    "CLOSE_ADVICE_REQUIRED_DATA_PLAN_SCHEMA",
    "CloseAdviceRequiredDataPlanError",
    "PLAN_FILE_NAME",
    "account_requirement_index",
    "close_advice_market_date",
    "build_close_advice_required_data_plan",
    "finalize_close_advice_required_data_plan",
    "load_close_advice_required_data_plan",
    "publish_close_advice_required_data_plan",
    "resolve_bound_close_advice_required_data_plan",
    "resolve_bound_close_advice_required_data_plan_snapshot",
    "resolve_position_fetch_binding",
]


if __name__ == "__main__":
    enrich_close_advice_required_data_plan(
        plan_path=Path(sys.argv[1]), expected_run_id=sys.argv[2]
    )
