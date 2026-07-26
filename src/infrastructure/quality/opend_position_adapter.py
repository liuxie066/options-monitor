from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from domain.domain.symbol_identity import OPTION_CODE_RE
from src.application.account_config import resolve_futu_account_ids
from src.application.futu_portfolio_context import infer_futu_portfolio_settings
from src.application.opend_utils import market_to_futu_trade_date_market
from src.infrastructure.futu_gateway import build_ready_futu_gateway


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
        gateway = None
        try:
            gateway = build_ready_futu_gateway(
                host=host,
                port=port,
                is_option_chain_cache_enabled=False,
            )
            raw_positions = gateway.get_positions(
                acc_id=int(account_id),
                trd_env=environment,
                refresh_cache=True,
            )
            position_rows = _rows(raw_positions)
            start = calendar_start or (datetime.now(timezone.utc).date() - timedelta(days=45))
            end = calendar_end or (datetime.now(timezone.utc).date() + timedelta(days=14))
            trade_market = market_to_futu_trade_date_market(market)
            if trade_market is None:
                raise ValueError(f"unsupported market calendar: {market}")
            raw_days = gateway.get_trading_days(
                market=trade_market,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            trading_days = [
                parsed
                for row in _rows(raw_days)
                for parsed in [_parse_trading_day(row)]
                if parsed is not None
            ]
            option_rows = [row for row in position_rows if _looks_like_option(row)]
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
            if gateway is not None:
                gateway.close()


def _looks_like_option(row: dict[str, Any]) -> bool:
    code = str(row.get("code") or row.get("symbol") or row.get("stock_code") or "").strip().upper()
    sec_type = str(row.get("sec_type") or row.get("security_type") or "").strip().upper()
    return bool(code and (sec_type in {"DRVT", "OPTION"} or OPTION_CODE_RE.match(code)))


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
