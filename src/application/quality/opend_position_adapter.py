from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from domain.domain.symbol_identity import OPTION_CODE_RE
from src.application.account_config import resolve_futu_account_ids
from src.application.futu_portfolio_context import _is_stock_position, build_futu_position_snapshot, infer_futu_portfolio_settings
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.application.futu_option_terms import (
    OpenDOptionEvidenceError, OpenDOptionTermsEvidenceError,
    _rows, _position_quantity, _enrich_option_contract_terms,
)
from src.infrastructure.futu_gateway import (
    build_ready_futu_broker_gateway,
    build_ready_futu_quote_gateway,
)


def _account_fingerprint(account_id: str) -> str:
    digest = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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
    snapshot_input: dict[str, Any] = field(default_factory=dict)

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
        position_rows: list[dict[str, Any]] = []
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
            position_rows = _rows(raw_positions, strict=True)
            for row in position_rows:
                row_account = next((row[key] for key in ("acc_id", "account_id", "trd_acc_id", "trade_acc_id", "accID") if row.get(key) is not None), None)
                row_env = next((row[key] for key in ("trd_env", "trdEnv", "trade_env", "tradeEnv") if row.get(key) is not None), None)
                if row_account is not None and str(row_account) != account_id:
                    raise ValueError("OpenD position response account mismatch")
                if row_env is not None and str(row_env).upper() != environment:
                    raise ValueError("OpenD position response environment mismatch")
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
            standard = build_futu_position_snapshot(
                rows=option_rows,
                broker_account_ref={
                    "broker_account_id": f"futu:{environment}:{account_id}",
                    "broker_id": "futu", "external_account_id": account_id,
                    "environment": environment, "account_label": account,
                },
                markets=[market.upper()], asset_types=["option"],
                observed_at_utc=observed_at_utc, completeness="complete",
            )
            return OpenDOptionSnapshot(
                account=account,
                market=market,
                environment=environment,
                account_fingerprint=_account_fingerprint(account_id),
                observed_at_utc=observed_at_utc,
                snapshot_id=standard["snapshot_id"],
                complete=not standard["errors"],
                refresh_cache=True,
                rows=option_rows,
                trading_days=trading_days,
                snapshot_input=standard,
                error_code="OPEND_POSITION_INPUT_INVALID" if standard["errors"] else None,
            )
        except Exception as exc:
            standard = build_futu_position_snapshot(
                rows=position_rows,
                broker_account_ref={
                    "broker_account_id": f"futu:{environment}:{account_id}",
                    "broker_id": "futu", "external_account_id": account_id,
                    "environment": environment, "account_label": account,
                },
                markets=[market.upper()], asset_types=["option"],
                observed_at_utc=observed_at_utc, completeness="unknown",
                source_errors=[getattr(exc, "code", None) or type(exc).__name__.upper()],
            )
            standard["source_payload"] = {"rows": position_rows}
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
                snapshot_input=standard,
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
            sec_type = str(row.get("sec_type") or row.get("security_type") or "").upper()
            if not _is_stock_position(row) and sec_type not in {"FUTURE", "IDX", "BOND", "WARRANT"} and _position_quantity(row) != 0:
                ambiguous_nonzero_codes.append(str(row.get("code") or "unknown"))
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
