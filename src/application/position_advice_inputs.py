"""Request identity and temporary holding validation, without ledger writes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from domain.domain.close_advice import safe_float, safe_int
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_position_identity import normalize_account, normalize_broker, normalize_currency
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from domain.domain.trade_contract_identity import normalize_contract_expiration

MAX_ADVICE_POSITIONS = 100


def read_advice_holdings(*, config: dict[str, Any], config_path: Path, base_dir: Path,
                         runtime_root: Path, account: str) -> list[dict[str, Any]]:
    from src.application.ledger.api import read_position_query_ledger, attach_event_strategy_metadata
    from src.application.agent_tool_contracts import AgentToolError

    try:
        _repo, snapshots = read_position_query_ledger(
            base=base_dir, accounts=[account], cfg=config, config_path=config_path, runtime_root=runtime_root,
        )
    except (sqlite3.Error, OSError) as exc:
        raise AgentToolError(
            code="DEPENDENCY_MISSING", message="Canonical ledger is unavailable; holdings were not treated as empty",
            details={"reason": "ledger_unavailable", "cause": type(exc).__name__},
        ) from exc
    snapshot = snapshots.get(account) or {}
    if snapshot.get("snapshot_status") != "trusted":
        raise ValueError("canonical ledger evidence is unavailable; holdings were not treated as empty")
    return attach_event_strategy_metadata(snapshot["account_position_lots"], snapshot.get("trade_events"))


def aware_time(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def advice_position_key(position: Mapping[str, Any]) -> str:
    if position.get("input_source") == "user_provided":
        value = str(position.get("input_id") or "").strip()
        return "user:" + value if value else ""
    return str(position.get("lot_id") or position.get("record_id") or "").strip()


def position_economics_sha256(position: Mapping[str, Any]) -> str:
    """Bind a reference to the exact holding economics, independent of input kind."""
    return canonical_sha256({
        "account": normalize_account(position.get("account")),
        "broker": normalize_broker(position.get("broker")),
        "symbol": canonical_symbol(position.get("symbol")),
        "option_type": str(position.get("option_type") or "").lower(),
        "side": str(position.get("side") or position.get("position_side") or "").lower(),
        "expiration": normalize_contract_expiration(position.get("expiration_ymd") or position.get("expiration"), fallback_raw=False),
        "strike": safe_float(position.get("strike")),
        "contracts_open": safe_int(position.get("contracts_open")),
        "multiplier": safe_float(position.get("multiplier")),
        "currency": normalize_currency(position.get("currency")),
        "premium": safe_float(position.get("premium")),
        "opened_at": position.get("opened_at"),
    })


def normalize_manual_positions(
    raw: Any, *, account: str, broker: str, now_utc: datetime,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ADVICE_POSITIONS:
        raise ValueError(f"positions must contain 1..{MAX_ADVICE_POSITIONS} temporary holdings")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed = {"input_id", "input_as_of_utc", "symbol", "option_type", "side", "expiration", "strike", "contracts_open", "multiplier", "currency", "premium", "opened_at", "account", "broker"}
    for item in raw:
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError("manual position contains unknown fields; ledger lot IDs are not accepted")
        identity = str(item.get("input_id") or "").strip()
        if not identity or len(identity) > 128 or identity in seen:
            raise ValueError("manual input_id must be unique and contain 1..128 characters")
        seen.add(identity)
        if "account" in item and normalize_account(item["account"]) != account:
            raise ValueError("manual position account differs from request")
        if "broker" in item and normalize_broker(item["broker"]) != broker:
            raise ValueError("manual position broker differs from configured account")
        symbol = canonical_symbol(item.get("symbol"))
        option_type = str(item.get("option_type") or "").strip().lower()
        expiration = normalize_contract_expiration(item.get("expiration"), fallback_raw=False)
        strike = safe_float(item.get("strike"))
        if not symbol or symbol_market(symbol) not in {"US", "HK"} or option_type not in {"put", "call"} or item.get("side", "short") != "short" or not expiration or strike is None or strike <= 0:
            raise ValueError("manual position requires a valid US/HK short put/call contract")
        observed = aware_time(item.get("input_as_of_utc"))
        if observed is None or observed > now_utc:
            raise ValueError("manual input_as_of_utc must be an aware nonfuture timestamp")
        for field in ("contracts_open", "multiplier", "premium"):
            if item.get(field) is not None:
                value = safe_int(item[field]) if field in {"contracts_open", "multiplier"} else safe_float(item[field])
                if value is None or value <= 0:
                    raise ValueError(f"manual {field} must be positive when supplied")
        currency = normalize_currency(item.get("currency"))
        if currency and currency != ("USD" if symbol_market(symbol) == "US" else "HKD"):
            raise ValueError("manual currency does not match contract market")
        result.append({
            **item, "account": account, "broker": broker, "symbol": symbol,
            "option_type": option_type, "side": "short", "expiration": expiration,
            "strike": strike, "input_source": "user_provided", "input_id": identity,
            "input_as_of_utc": observed.isoformat(), "currency": currency or None,
        })
    return result


def select_ledger_positions(
    payload: Mapping[str, Any], *, records: list[dict[str, Any]], account: str,
    broker: str, now_utc: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from src.application.ledger.api import position_lot_risk_view
    from domain.domain.expiration_dates import expiration_market_date

    symbols_raw = payload.get("symbols", [])
    lots_raw = payload.get("lot_ids", [])
    contracts = payload.get("contracts", [])
    if not all(isinstance(value, list) for value in (symbols_raw, lots_raw, contracts)):
        raise ValueError("symbols, lot_ids and contracts selectors must be lists")
    if any(len(value) > MAX_ADVICE_POSITIONS for value in (symbols_raw, lots_raw, contracts)):
        raise ValueError("too many position selectors")
    symbols = {canonical_symbol(value) for value in symbols_raw}
    lots = {str(value).strip() for value in lots_raw if isinstance(value, str)}
    if None in symbols or len(lots) != len(lots_raw) or "" in lots:
        raise ValueError("invalid symbol or lot selector")
    contract_keys: set[tuple[Any, ...]] = set()
    for item in contracts:
        if not isinstance(item, dict) or set(item) != {"symbol", "option_type", "expiration", "strike"}:
            raise ValueError("contracts selector requires symbol, option_type, expiration and strike")
        key = (canonical_symbol(item["symbol"]), str(item["option_type"]).lower(), normalize_contract_expiration(item["expiration"], fallback_raw=False), safe_float(item["strike"]))
        if not key[0] or key[1] not in {"put", "call"} or not key[2] or key[3] is None or key[3] <= 0:
            raise ValueError("invalid contract selector")
        contract_keys.add(key)
    chosen_records: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        view = position_lot_risk_view(record)
        if normalize_account(view.account) != account or normalize_broker(view.broker) != broker or not view.is_open:
            continue
        market = symbol_market(view.canonical_underlying_symbol)
        day = expiration_market_date(now_utc, market) if market else None
        if day is None:
            continue
        position = view.as_open_position_min(as_of_date=day)
        if position.get("side") != "short" or position.get("option_type") not in {"put", "call"}:
            continue
        identity = advice_position_key(position)
        key = (canonical_symbol(position.get("symbol")), position.get("option_type"), normalize_contract_expiration(position.get("expiration_ymd") or position.get("expiration"), fallback_raw=False), safe_float(position.get("strike")))
        if symbols and key[0] not in symbols or lots and identity not in lots or contract_keys and key not in contract_keys:
            continue
        if not identity or identity in seen:
            raise ValueError("ledger selected holding identity is missing or duplicated")
        seen.add(identity)
        position["input_source"] = "ledger"
        positions.append(position)
        chosen_records.append(record)
    if lots - seen:
        raise ValueError("some selected lots are not open short options in this account/broker scope")
    if len(positions) > MAX_ADVICE_POSITIONS:
        raise ValueError("select at most 100 holdings per request")
    return positions, chosen_records
