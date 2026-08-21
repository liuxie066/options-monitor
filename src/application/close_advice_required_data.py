from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

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
from functools import partial


_required_text = partial(required_text, error=lambda m: CloseAdviceRequiredDataPlanError(m))


CLOSE_ADVICE_REQUIRED_DATA_PLAN_SCHEMA = "close_advice_required_data_plan.v1"
PLAN_FILE_NAME = "close_advice_required_data_plan.json"
_ACCOUNT_STATUSES = frozenset(
    {"not_applicable", "ready", "partial", "unavailable"}
)
_PLAN_STATUSES = frozenset({"complete", "partial", "failed"})


class CloseAdviceRequiredDataPlanError(RuntimeError):
    pass


def build_close_advice_required_data_plan(
    *,
    run_id: str,
    run_started_at_utc: datetime,
    business_date: date,
    account_configs: Mapping[str, Mapping[str, Any]],
    base_config: Mapping[str, Any],
    markets_to_run: list[str] | None,
    position_records_by_account: Mapping[str, list[dict[str, Any]]],
    unavailable_by_account: Mapping[str, str] | None = None,
    blocked_markets_by_account: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    run_id_norm = _required_text(run_id, "run_id")
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
                view = position_lot_risk_view(
                    record,
                    as_of_date=business_date,
                )
            except Exception:
                continue
            if not view.fields or not view.is_open or int(view.contracts_open or 0) <= 0:
                continue
            if normalize_account(view.account) != account:
                continue
            if expected_broker and normalize_broker(view.broker) != expected_broker:
                continue
            position = view.as_open_position_min(as_of_date=business_date)
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
            lot_id = str(position.get("record_id") or "").strip()
            try:
                expiration_date = datetime.strptime(
                    str(expiration or ""),
                    "%Y-%m-%d",
                ).date()
            except ValueError:
                expiration_date = None
            if expiration_date is None or expiration_date <= business_date:
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
        "business_date": business_date.isoformat(),
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
    try:
        datetime.strptime(
            _required_text(payload.get("business_date"), "business_date"),
            "%Y-%m-%d",
        )
    except ValueError as exc:
        raise CloseAdviceRequiredDataPlanError(
            "close-advice required-data plan business date is invalid"
        ) from exc
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
    "build_close_advice_required_data_plan",
    "finalize_close_advice_required_data_plan",
    "load_close_advice_required_data_plan",
    "publish_close_advice_required_data_plan",
    "resolve_bound_close_advice_required_data_plan",
    "resolve_bound_close_advice_required_data_plan_snapshot",
    "resolve_position_fetch_binding",
]
