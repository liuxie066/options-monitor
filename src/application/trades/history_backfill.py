from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.option_position_identity import normalize_currency
from src.infrastructure.futu_gateway import FutuGatewayUnreachableError
from src.infrastructure.opend_watchdog import port_open


def history_deal_query_dates(*, lookback_hours: float, now: datetime | None = None) -> tuple[str, str, str, str]:
    end_utc = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    start_utc = end_utc - timedelta(hours=float(lookback_hours))
    try:
        trade_tz = ZoneInfo("Asia/Hong_Kong")
    except Exception:
        trade_tz = timezone.utc
    start_trade = start_utc.astimezone(trade_tz).strftime("%Y-%m-%d %H:%M:%S")
    end_trade = end_utc.astimezone(trade_tz).strftime("%Y-%m-%d %H:%M:%S")
    return start_trade, end_trade, start_utc.isoformat(), end_utc.isoformat()


def fetch_opend_history_deals(
    *,
    host: str,
    port: int,
    futu_account_ids: list[str],
    lookback_hours: float,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    client = OpenDHistoryDealClient(host=host, port=port)
    try:
        return client.fetch(
            futu_account_ids=futu_account_ids,
            lookback_hours=lookback_hours,
            now=now,
        )
    finally:
        client.close()


class OpenDHistoryDealClient:
    """Reusable OpenD history context owned by one intake source loop."""

    def __init__(self, *, host: str, port: int) -> None:
        self.host = str(host)
        self.port = int(port)
        self._futu_mod: Any = None
        self._ctx: Any = None

    def fetch(
        self,
        *,
        futu_account_ids: list[str],
        lookback_hours: float,
        now: datetime | None = None,
        **_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        futu_mod = self._futu()
        ctx = self._context()
        try:
            rows, diagnostics = _query_history_deals(
                futu_mod=futu_mod,
                ctx=ctx,
                futu_account_ids=futu_account_ids,
                lookback_hours=lookback_hours,
                now=now,
            )
            account_results = diagnostics.get("account_results")
            if isinstance(account_results, list) and any(
                isinstance(item, dict) and item.get("error")
                for item in account_results
            ):
                self.close()
            return rows, diagnostics
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        ctx = self._ctx
        self._ctx = None
        close = getattr(ctx, "close", None)
        if callable(close):
            close()

    def fetch_terminal_orders(
        self,
        *,
        futu_account_id: str,
        order_ids: list[str],
        start: str,
        end: str,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        requested = _normalized_order_ids(order_ids)
        if len(requested) > 400:
            raise ValueError("order query supports at most 400 order IDs")
        try:
            futu_mod = self._futu()
            ctx = self._context()
            acc_id = _numeric_account_id(futu_account_id)
            ret, data = ctx.history_order_list_query(
                start=str(start),
                end=str(end),
                trd_env=futu_mod.TrdEnv.REAL,
                acc_id=acc_id,
            )
            if ret != futu_mod.RET_OK:
                raise RuntimeError(f"history_order_list_query failed: {data}")
            requested_set = set(requested)
            rows: dict[str, dict[str, Any]] = {}
            for raw in _plain_records(data):
                order_id = str(raw.get("order_id") or raw.get("orderID") or "").strip()
                if order_id not in requested_set:
                    continue
                normalized = {
                    "provider": "opend",
                    "futu_account_id": str(acc_id),
                    "order_id": order_id,
                    "status": _normalize_order_status(raw.get("order_status") or raw.get("status")),
                    "dealt_qty": _decimal_text(raw.get("dealt_qty"), nonnegative=True),
                    "currency": normalize_currency(raw.get("currency")) or None,
                }
                _put_unique(rows, order_id, normalized, source="history order")
            return rows, {
                "requested_count": len(requested),
                "returned_count": len(rows),
                "missing_order_ids": sorted(requested_set - set(rows)),
            }
        except Exception:
            self.close()
            raise

    def fetch_order_fees(
        self,
        *,
        futu_account_id: str,
        order_ids: list[str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        requested = _normalized_order_ids(order_ids)
        if len(requested) > 400:
            raise ValueError("order_fee_query supports at most 400 order IDs")
        try:
            futu_mod = self._futu()
            ctx = self._context()
            acc_id = _numeric_account_id(futu_account_id)
            ret, data = ctx.order_fee_query(
                order_id_list=requested,
                trd_env=futu_mod.TrdEnv.REAL,
                acc_id=acc_id,
            )
            if ret != futu_mod.RET_OK:
                raise RuntimeError(f"order_fee_query failed: {data}")
            requested_set = set(requested)
            rows: dict[str, dict[str, Any]] = {}
            for raw in _plain_records(data):
                order_id = str(raw.get("order_id") or raw.get("orderID") or "").strip()
                if order_id not in requested_set:
                    continue
                normalized = {
                    "provider": "opend",
                    "futu_account_id": str(acc_id),
                    "order_id": order_id,
                    "fee_amount": _decimal_text(raw.get("fee_amount"), nonnegative=True),
                    "fee_details": _plain_json_value(raw.get("fee_details")),
                }
                _put_unique(rows, order_id, normalized, source="order fee")
            return rows, {
                "requested_count": len(requested),
                "returned_count": len(rows),
                "missing_order_ids": sorted(requested_set - set(rows)),
            }
        except Exception:
            self.close()
            raise

    def _futu(self) -> Any:
        if self._futu_mod is None:
            self._futu_mod = importlib.import_module("futu")
        return self._futu_mod

    def _context(self) -> Any:
        if self._ctx is None:
            if not port_open(self.host, self.port):
                raise FutuGatewayUnreachableError(
                    f"OpenD unreachable: {self.host}:{self.port}; "
                    "start FutuOpenD before history backfill"
                )
            self._ctx = self._futu().OpenSecTradeContext(
                host=self.host,
                port=self.port,
            )
        return self._ctx


def _query_history_deals(
    *,
    futu_mod: Any,
    ctx: Any,
    futu_account_ids: list[str],
    lookback_hours: float,
    now: datetime | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start_date, end_date, window_start_utc, window_end_utc = history_deal_query_dates(
        lookback_hours=lookback_hours,
        now=now,
    )
    rows: list[dict[str, Any]] = []
    account_results: list[dict[str, Any]] = []
    for raw_acc_id in futu_account_ids:
        acc_id_text = str(raw_acc_id or "").strip()
        if not acc_id_text:
            continue
        try:
            acc_id = int(acc_id_text)
        except ValueError:
            account_results.append(
                {
                    "futu_account_id": acc_id_text,
                    "ret": None,
                    "row_count": 0,
                    "skipped": True,
                    "reason": "non_numeric_account_id",
                }
            )
            continue
        ret, data = ctx.history_deal_list_query(
            start=start_date,
            end=end_date,
            trd_env=futu_mod.TrdEnv.REAL,
            acc_id=acc_id,
        )
        account_result = {
            "futu_account_id": acc_id_text,
            "ret": ret,
            "row_count": 0,
        }
        if ret != futu_mod.RET_OK:
            account_result["error"] = str(data)
            account_results.append(account_result)
            continue
        account_rows = data.to_dict("records") if hasattr(data, "to_dict") else []
        if isinstance(account_rows, list):
            for item in account_rows:
                if isinstance(item, dict):
                    payload = dict(item)
                    payload.setdefault("futu_account_id", acc_id_text)
                    payload.setdefault("trd_acc_id", acc_id_text)
                    rows.append(payload)
            account_result["row_count"] = len(account_rows)
        account_results.append(account_result)

    diagnostics = {
        "start_date": start_date,
        "end_date": end_date,
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        "lookback_hours": float(lookback_hours),
        "account_results": account_results,
    }
    return rows, diagnostics


_TERMINAL_WITH_FILL = {"FILLED_ALL", "CANCELLED_PART"}
_RETRYABLE_ORDER_STATUSES = {
    "UNSUBMITTED",
    "WAITING_SUBMIT",
    "SUBMITTING",
    "TIMEOUT",
    "SUBMITTED",
    "FILLED_PART",
    "CANCELLING_PART",
    "CANCELLING_ALL",
}
_TERMINAL_NO_FILL = {
    "SUBMIT_FAILED",
    "CANCELLED_ALL",
    "FAILED",
    "DISABLED",
    "DELETED",
    "FILL_CANCELLED",
}


def _normalize_order_status(value: Any) -> str:
    token = str(value or "").strip().upper().rsplit(".", 1)[-1]
    if token in _TERMINAL_WITH_FILL:
        return "terminal_with_fill"
    if token in _RETRYABLE_ORDER_STATUSES:
        return "retryable"
    if token in _TERMINAL_NO_FILL:
        return "terminal_no_fill"
    return "unknown"


def _normalized_order_ids(values: list[str]) -> list[str]:
    out = list(dict.fromkeys(str(value or "").strip() for value in values))
    if not out or any(not value for value in out):
        raise ValueError("order_ids must be non-empty strings")
    return out


def _numeric_account_id(value: str) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("futu_account_id must be numeric") from exc


def _plain_records(value: Any) -> list[dict[str, Any]]:
    rows = value.to_dict("records") if hasattr(value, "to_dict") else value
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def _decimal_text(value: Any, *, nonnegative: bool) -> str:
    try:
        number = Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("provider decimal is invalid") from exc
    if not number.is_finite() or (nonnegative and number < 0):
        raise ValueError("provider decimal is invalid")
    return format(number, "f")


def _put_unique(
    rows: dict[str, dict[str, Any]],
    order_id: str,
    value: dict[str, Any],
    *,
    source: str,
) -> None:
    existing = rows.get(order_id)
    if existing is not None and existing != value:
        raise ValueError(f"conflicting duplicate {source} row: order_id={order_id}")
    rows[order_id] = value


def _plain_json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("provider fee_details is not plain JSON") from exc


__all__ = [
    "OpenDHistoryDealClient",
    "fetch_opend_history_deals",
    "history_deal_query_dates",
    "_normalize_order_status",
]
