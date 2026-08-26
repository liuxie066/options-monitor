from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from domain.domain.symbol_identity import OPTION_CODE_RE, canonical_symbol
from domain.domain.trade_contract_identity import normalize_contract_expiration
from src.application.account_config import resolve_futu_account_ids
from src.application.futu_portfolio_context import infer_futu_portfolio_settings
from src.application.opend_normalize import normalize_opend_option_type
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.infrastructure.futu_gateway import (
    build_ready_futu_broker_gateway,
    build_ready_futu_quote_gateway,
)


_MARKET_SNAPSHOT_BATCH_SIZE = 200


class OpenDOptionEvidenceError(RuntimeError):
    code = "OPEND_OPTION_MULTIPLIER_EVIDENCE_INCOMPLETE"


class OpenDOptionTermsEvidenceError(RuntimeError):
    code = "OPEND_OPTION_TERMS_EVIDENCE_INCOMPLETE"


def _rows(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict("records")
        except Exception:
            records = None
        if isinstance(records, list):
            return [dict(item) for item in records if isinstance(item, dict)]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [dict(value)]
    return []


def _account_fingerprint(account_id: str) -> str:
    digest = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _snapshot_id(*, account: str, observed_at_utc: str, rows: list[dict[str, Any]]) -> str:
    safe = {
        "account": account,
        "observed_at_utc": observed_at_utc,
        "row_count": len(rows),
        "codes": sorted(str(row.get("code") or "") for row in rows),
    }
    digest = hashlib.sha256(
        json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"opend-{digest[:24]}"


@dataclass(frozen=True)
class OpenDOptionSnapshot:
    account: str
    market: str
    environment: str
    account_fingerprint: str
    observed_at_utc: str
    snapshot_id: str
    complete: bool
    refresh_cache: bool
    rows: list[dict[str, Any]]
    trading_days: list[date]
    error_code: str | None = None
    error_message: str | None = None

    def public_source_snapshot(self) -> dict[str, Any]:
        return {
            "provider": "futu-opend",
            "snapshot_id": self.snapshot_id,
            "observed_at_utc": self.observed_at_utc,
            "complete": self.complete,
            "refresh_cache": self.refresh_cache,
            "account_fingerprint": self.account_fingerprint,
            "environment": self.environment,
            "market": self.market,
        }


class OpenDOptionPositionAdapter:
    def fetch(
        self,
        *,
        cfg: Mapping[str, Any],
        account: str,
        market: str,
        calendar_start: date | None = None,
        calendar_end: date | None = None,
    ) -> OpenDOptionSnapshot:
        observed_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        account_ids = resolve_futu_account_ids(cfg, account=account)
        if len(account_ids) != 1 or not str(account_ids[0]).isdigit():
            return OpenDOptionSnapshot(
                account=account,
                market=market,
                environment="UNKNOWN",
                account_fingerprint="sha256:" + ("0" * 64),
                observed_at_utc=observed_at_utc,
                snapshot_id=f"opend-unavailable-{account}",
                complete=False,
                refresh_cache=True,
                rows=[],
                trading_days=[],
                error_code="OPEND_ACCOUNT_MAPPING_INVALID",
                error_message="OpenD option snapshot requires one explicit numeric account_id.",
            )
        account_id = str(account_ids[0])
        settings = infer_futu_portfolio_settings(cfg, account=account)
        host = str(settings.get("host") or "").strip()
        try:
            port = int(settings.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        environment = str(settings.get("trd_env") or "REAL").strip().upper()
        if not host or port <= 0 or environment != "REAL":
            return OpenDOptionSnapshot(
                account=account,
                market=market,
                environment=environment or "UNKNOWN",
                account_fingerprint=_account_fingerprint(account_id),
                observed_at_utc=observed_at_utc,
                snapshot_id=f"opend-unavailable-{account}",
                complete=False,
                refresh_cache=True,
                rows=[],
                trading_days=[],
                error_code="OPEND_SETTINGS_INVALID",
                error_message="OpenD host/port and REAL environment are required.",
            )
        quote_route = resolve_futu_quote_route(cfg, market=market)
        if not quote_route.ok:
            return OpenDOptionSnapshot(
                account=account,
                market=market,
                environment=environment,
                account_fingerprint=_account_fingerprint(account_id),
                observed_at_utc=observed_at_utc,
                snapshot_id=f"opend-unavailable-{account}",
                complete=False,
                refresh_cache=True,
                rows=[],
                trading_days=[],
                error_code="OPEND_QUOTE_ROUTE_UNAVAILABLE",
                error_message="canonical Futu quote route is missing or conflicting",
            )
        broker_gateway = None
        quote_gateway = None
        try:
            broker_gateway = build_ready_futu_broker_gateway(
                host=host,
                port=port,
                expected_account_ids=[account_id],
                trd_env=environment,
                is_option_chain_cache_enabled=False,
            )
            quote_gateway = build_ready_futu_quote_gateway(
                host=str(quote_route.host),
                port=int(quote_route.port or 0),
                is_option_chain_cache_enabled=False,
            )
            raw_positions = broker_gateway.get_positions(
                acc_id=int(account_id),
                trd_env=environment,
                refresh_cache=True,
            )
            position_rows = _rows(raw_positions)
            option_rows = _option_rows_for_market(
                position_rows,
                market=market,
            )
            start = calendar_start or (datetime.now(timezone.utc).date() - timedelta(days=45))
            end = calendar_end or (datetime.now(timezone.utc).date() + timedelta(days=14))
            raw_days = quote_gateway.get_trading_days(
                market=market,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            trading_days = [
                parsed
                for row in _rows(raw_days)
                for parsed in [_parse_trading_day(row)]
                if parsed is not None
            ]
            option_rows = _enrich_option_contract_terms(
                quote_gateway,
                option_rows,
            )
            return OpenDOptionSnapshot(
                account=account,
                market=market,
                environment=environment,
                account_fingerprint=_account_fingerprint(account_id),
                observed_at_utc=observed_at_utc,
                snapshot_id=_snapshot_id(
                    account=account,
                    observed_at_utc=observed_at_utc,
                    rows=option_rows,
                ),
                complete=True,
                refresh_cache=True,
                rows=option_rows,
                trading_days=trading_days,
            )
        except Exception as exc:
            return OpenDOptionSnapshot(
                account=account,
                market=market,
                environment=environment,
                account_fingerprint=_account_fingerprint(account_id),
                observed_at_utc=observed_at_utc,
                snapshot_id=f"opend-unavailable-{account}",
                complete=False,
                refresh_cache=True,
                rows=[],
                trading_days=[],
                error_code=getattr(exc, "code", None) or type(exc).__name__.upper(),
                error_message=str(exc),
            )
        finally:
            if broker_gateway is not None:
                broker_gateway.close()
            if quote_gateway is not None and quote_gateway is not broker_gateway:
                quote_gateway.close()


def _looks_like_option(row: dict[str, Any]) -> bool:
    code = str(row.get("code") or row.get("symbol") or row.get("stock_code") or "").strip().upper()
    sec_type = str(row.get("sec_type") or row.get("security_type") or "").strip().upper()
    return bool(code and (sec_type in {"DRVT", "OPTION"} or OPTION_CODE_RE.match(code)))


def _option_rows_for_market(
    rows: list[dict[str, Any]],
    *,
    market: str,
) -> list[dict[str, Any]]:
    market_key = str(market or "").strip().upper()
    scoped: list[dict[str, Any]] = []
    ambiguous_nonzero_codes: list[str] = []
    for row in rows:
        if not _looks_like_option(row):
            continue
        code = str(
            row.get("code") or row.get("symbol") or row.get("stock_code") or ""
        ).strip().upper()
        match = OPTION_CODE_RE.match(code)
        prefix = code.partition(".")[0] if "." in code else ""
        code_market = str(match.group("market") or "").upper() if match else (
            prefix if prefix in {"US", "HK"} else ""
        )
        if not code_market:
            if _position_quantity(row) not in (0.0,):
                ambiguous_nonzero_codes.append(code or "unknown")
            continue
        if code_market != market_key:
            continue
        scoped.append(row)
    if ambiguous_nonzero_codes:
        raise OpenDOptionTermsEvidenceError(
            "OpenD option position market identity is unavailable for "
            f"{len(ambiguous_nonzero_codes)} non-zero option position(s)."
        )
    return scoped


def _positive_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _position_quantity(row: Mapping[str, Any]) -> float | None:
    raw = row.get("qty") if "qty" in row else row.get("quantity")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _row_multiplier(row: Mapping[str, Any]) -> float | None:
    for key in (
        "options_per_contract",
        "option_contract_multiplier",
        "option_contract_size",
        "contract_multiplier",
        "lot_size",
        "multiplier",
    ):
        value = _positive_number(row.get(key))
        if value is not None:
            return value
    return None


def _enrich_option_contract_terms(
    gateway: Any,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    required_codes = sorted(
        {
            str(row.get("code") or row.get("symbol") or row.get("stock_code") or "")
            .strip()
            .upper()
            for row in rows
            if _position_quantity(row) not in (None, 0.0)
        }
        - {""}
    )
    required_code_set = set(required_codes)
    snapshot_by_code: dict[str, dict[str, Any]] = {}
    for start in range(0, len(required_codes), _MARKET_SNAPSHOT_BATCH_SIZE):
        batch = required_codes[start : start + _MARKET_SNAPSHOT_BATCH_SIZE]
        snapshot_rows = _rows(gateway.get_snapshot(batch))
        for snapshot_row in snapshot_rows:
            code = str(snapshot_row.get("code") or "").strip().upper()
            if not code or code not in required_code_set:
                continue
            if code in snapshot_by_code:
                raise OpenDOptionTermsEvidenceError(
                    f"OpenD market snapshot returned duplicate option terms for {code}."
                )
            snapshot_by_code[code] = dict(snapshot_row)

    enriched: list[dict[str, Any]] = []
    missing_multiplier_codes: list[str] = []
    incomplete_terms_codes: list[str] = []
    for row in rows:
        item = dict(row)
        quantity = _position_quantity(item)
        if quantity in (None, 0.0):
            enriched.append(item)
            continue
        code = str(
            item.get("code") or item.get("symbol") or item.get("stock_code") or ""
        ).strip().upper()
        snapshot_row = snapshot_by_code.get(code)
        if snapshot_row is None:
            incomplete_terms_codes.append(code or "unknown")
            enriched.append(item)
            continue

        multiplier = _snapshot_multiplier(snapshot_row)
        if multiplier is None:
            missing_multiplier_codes.append(code or "unknown")
            enriched.append(item)
            continue
        if not _has_complete_current_option_terms(snapshot_row):
            incomplete_terms_codes.append(code or "unknown")
            enriched.append(item)
            continue

        _copy_snapshot_option_terms(item, snapshot_row)
        item["options_per_contract"] = multiplier
        enriched.append(item)

    if missing_multiplier_codes:
        raise OpenDOptionEvidenceError(
            "OpenD market snapshot did not provide multiplier evidence for "
            f"{len(missing_multiplier_codes)} non-zero option position(s)."
        )
    if incomplete_terms_codes:
        raise OpenDOptionTermsEvidenceError(
            "OpenD market snapshot did not provide complete current terms for "
            f"{len(incomplete_terms_codes)} non-zero option position(s)."
        )
    return enriched


def _copy_snapshot_option_terms(
    target: dict[str, Any],
    snapshot: Mapping[str, Any],
) -> None:
    aliases = {
        "stock_owner": ("stock_owner", "owner_code", "underlying"),
        "option_type": ("option_type",),
        "strike_time": ("strike_time", "expiration_ymd", "expiration"),
        "option_strike_price": ("option_strike_price", "strike_price"),
        "option_contract_size": ("option_contract_size", "contract_size"),
        "option_contract_multiplier": (
            "option_contract_multiplier",
            "contract_multiplier",
        ),
        "lot_size": ("lot_size",),
        "option_valid": ("option_valid",),
    }
    for field, candidates in aliases.items():
        for candidate in candidates:
            value = snapshot.get(candidate)
            if value not in (None, ""):
                target[field] = value
                break
    target["option_terms_source"] = "market_snapshot"


def _snapshot_multiplier(row: Mapping[str, Any]) -> float | None:
    values = {
        value
        for key in (
            "option_contract_multiplier",
            "option_contract_size",
        )
        if (value := _positive_number(row.get(key))) is not None
    }
    if len(values) == 1:
        return next(iter(values))
    if len(values) > 1:
        return None
    return _positive_number(row.get("lot_size"))


def _has_complete_current_option_terms(row: Mapping[str, Any]) -> bool:
    if row.get("option_valid") is not True:
        return False
    option_type = normalize_opend_option_type(row.get("option_type"))
    expiration = normalize_contract_expiration(
        row.get("strike_time")
        or row.get("expiration_ymd")
        or row.get("expiration")
    )
    strike = _positive_number(
        row.get("option_strike_price")
        if row.get("option_strike_price") not in (None, "")
        else row.get("strike_price")
    )
    multiplier = _snapshot_multiplier(row)
    owner = canonical_symbol(
        row.get("stock_owner")
        or row.get("owner_code")
        or row.get("underlying")
    )
    return bool(
        owner
        and option_type in {"put", "call"}
        and expiration
        and strike is not None
        and multiplier is not None
    )


def _parse_trading_day(row: dict[str, Any]) -> date | None:
    raw = str(row.get("time") or row.get("date") or row.get("trade_date") or "").strip()
    try:
        parsed = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    kind = str(row.get("trade_date_type") or "").strip().upper()
    if kind and kind not in {"WHOLE", "MORNING", "AFTERNOON", "TRADING"}:
        return None
    return parsed


__all__ = ["OpenDOptionPositionAdapter", "OpenDOptionSnapshot"]
