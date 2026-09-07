"""Futu trade-history transport and response normalization."""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_account_identity import extract_visible_account_fields
from src.infrastructure.futu_gateway import (
    FutuGatewayError,
    FutuGatewayRateLimitError,
    FutuGatewayUnreachableError,
    build_futu_gateway,
)


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
    """Reusable Futu gateway owned by one intake source loop."""

    def __init__(self, *, host: str, port: int) -> None:
        self.host = str(host)
        self.port = int(port)
        self._gateway: Any = None
        self._fee_query_times: dict[str, deque[float]] = {}

    def fetch(
        self,
        *,
        futu_account_ids: list[str],
        lookback_hours: float,
        now: datetime | None = None,
        **_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        gateway = self._gateway_client()
        try:
            rows, diagnostics = _query_history_deals(
                gateway=gateway,
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
        gateway = self._gateway
        self._gateway = None
        close = getattr(gateway, "close", None)
        if callable(close):
            close()

    def fetch_terminal_orders(
        self,
        *,
        futu_account_id: str,
        order_ids: list[str],
        start: str,
        end: str,
        exact: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        requested = _normalized_order_ids(order_ids)
        if len(requested) > 400:
            raise ValueError("order query supports at most 400 order IDs")
        if exact and len(requested) != 1:
            raise ValueError("exact order query requires exactly one order ID")
        try:
            gateway = self._gateway_client()
            acc_id = _numeric_account_id(futu_account_id)
            requested_set = set(requested)
            rows: dict[str, dict[str, Any]] = {}
            current_order_error_type: str | None = None
            if exact:
                try:
                    current = gateway.get_order_list(
                        trd_env="REAL",
                        acc_id=acc_id,
                        order_id=requested[0],
                        refresh_cache=True,
                    )
                    _collect_terminal_orders(
                        rows,
                        _gateway_rows(current),
                        requested=requested_set,
                        futu_account_id=acc_id,
                        source="current order",
                    )
                except FutuGatewayError as exc:
                    current_order_error_type = type(exc).__name__
                if rows:
                    return rows, {
                        "requested_count": 1,
                        "returned_count": 1,
                        "query_source": "current_order",
                    }
            result = gateway.get_history_orders(
                start=str(start),
                end=str(end),
                trd_env="REAL",
                acc_id=acc_id,
            )
            _collect_terminal_orders(
                rows,
                _gateway_rows(result),
                requested=requested_set,
                futu_account_id=acc_id,
                source="history order",
            )
            diagnostics = {
                "requested_count": len(requested),
                "returned_count": len(rows),
                "missing_order_ids": sorted(requested_set - set(rows)),
                "query_source": "history_order",
            }
            if current_order_error_type:
                diagnostics["current_order_error_type"] = current_order_error_type
            return rows, diagnostics
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
            gateway = self._gateway_client()
            acc_id = _numeric_account_id(futu_account_id)
            self._reserve_fee_query(str(acc_id))
            data = gateway.get_order_fees(
                order_id_list=requested,
                trd_env="REAL",
                acc_id=acc_id,
            )
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

    def _reserve_fee_query(self, futu_account_id: str) -> None:
        now = time.monotonic()
        calls = self._fee_query_times.setdefault(str(futu_account_id), deque())
        while calls and now - calls[0] >= 30.0:
            calls.popleft()
        if len(calls) >= 10:
            raise FutuGatewayRateLimitError(
                "order_fee_query local limit exceeded: 10 calls per 30 seconds"
            )
        calls.append(now)

    def _gateway_client(self) -> Any:
        if self._gateway is None:
            self._gateway = build_futu_gateway(
                host=self.host,
                port=self.port,
                is_option_chain_cache_enabled=False,
            )
        return self._gateway


def _query_history_deals(
    *,
    gateway: Any,
    futu_account_ids: list[str],
    lookback_hours: float,
    now: datetime | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start_date, end_date, window_start_utc, window_end_utc = history_deal_query_dates(
        lookback_hours=lookback_hours,
        now=now,
    )
    request_id = str(uuid4())
    rows: list[dict[str, Any]] = []
    account_results: list[dict[str, Any]] = []
    for raw_acc_id in futu_account_ids:
        acc_id_text = str(raw_acc_id or "").strip()
        if not acc_id_text:
            continue
        account_result: dict[str, Any] = {
            "request_id": f"{request_id}:{acc_id_text}",
            "futu_account_id": acc_id_text,
            "trd_env": "REAL",
            "requested_start_utc": window_start_utc,
            "requested_end_utc": window_end_utc,
            "covered_start_utc": None,
            "covered_end_utc": None,
            "coverage_status": "unknown",
            "coverage_complete": None,
            "pagination_complete": None,
            "page_count": None,
            "truncated": None,
            "ret": None,
            "row_count": 0,
            "accepted_row_count": 0,
        }
        try:
            acc_id = int(acc_id_text)
        except ValueError:
            account_result.update(skipped=True, reason="non_numeric_account_id")
            account_results.append(account_result)
            continue
        try:
            result = gateway.get_history_deals(
                start=start_date,
                end=end_date,
                trd_env="REAL",
                acc_id=acc_id,
            )
            account_rows = _gateway_rows(result)
        except Exception as exc:
            if isinstance(exc, FutuGatewayUnreachableError) and not account_results:
                raise
            account_result["ret"] = None
            account_result["error"] = str(exc)
            account_result["error_type"] = type(exc).__name__
            account_results.append(account_result)
            continue
        account_result["ret"] = result.get("retcode", 0) if isinstance(result, dict) else 0
        if isinstance(result, dict):
            for key in ("coverage_complete", "pagination_complete", "page_count", "truncated"):
                account_result[key] = result.get(key)
            if result.get("error"):
                account_result["error"] = str(result["error"])
        if account_result["ret"] not in (0, "0"):
            account_result.setdefault("error", f"history_query_returned:{account_result['ret']}")
        if (
            account_result["coverage_complete"] is False
            or account_result["pagination_complete"] is False
            or account_result["truncated"] is True
        ):
            account_result["coverage_status"] = "partial"
        elif (
            account_result["coverage_complete"] is True
            and account_result["pagination_complete"] is True
            and not account_result.get("error")
        ):
            account_result.update(
                coverage_status="complete",
                covered_start_utc=window_start_utc,
                covered_end_utc=window_end_utc,
            )
        for item in account_rows:
            payload = dict(item)
            conflicts = [
                key for key, value in extract_visible_account_fields(payload).items()
                if value != acc_id_text
            ]
            bindings = {
                "environment": "REAL",
                "broker_account_id": f"futu:REAL:{acc_id_text}",
                "external_id_namespace": "futu.deal",
                "futu_account_id": acc_id_text,
                "trd_acc_id": acc_id_text,
            }
            if any(str(payload.get(key) or "").strip() for key in ("order_id", "orderID", "orderId", "external_order_id")):
                bindings["external_order_namespace"] = "futu.order"
            for key, value in {**bindings, "trd_env": "REAL", "execution_id_namespace": "futu.deal"}.items():
                actual = str(payload.get(key) or "").strip()
                if actual and actual != value:
                    conflicts.append(key)
            if conflicts:
                account_result.setdefault("rejected_rows", []).append(
                    {"reason": "history_source_identity_conflict", "fields": sorted(set(conflicts)), "payload": payload}
                )
                account_result.update(
                    coverage_status="partial", coverage_complete=False,
                    covered_start_utc=None, covered_end_utc=None,
                    error="history_source_identity_conflict",
                )
                continue
            payload.update(bindings)
            rows.append(payload)
        account_result["row_count"] = len(account_rows)
        account_result["accepted_row_count"] = len(account_rows) - len(account_result.get("rejected_rows", []))
        account_results.append(account_result)

    statuses = {item["coverage_status"] for item in account_results}
    diagnostics = {
        "schema_version": "futu_history_query_receipt.v1",
        "request_id": request_id,
        "dataset": "executions",
        "trd_env": "REAL",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "coverage_status": (
            "complete" if statuses == {"complete"}
            else "partial" if "partial" in statuses or "complete" in statuses
            else "unknown"
        ),
        "provider_timezone": "Asia/Hong_Kong",
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


def _collect_terminal_orders(
    rows: dict[str, dict[str, Any]],
    provider_rows: list[dict[str, Any]],
    *,
    requested: set[str],
    futu_account_id: int,
    source: str,
) -> None:
    for raw in provider_rows:
        order_id = str(raw.get("order_id") or raw.get("orderID") or "").strip()
        if order_id not in requested:
            continue
        _put_unique(
            rows,
            order_id,
            {
                "provider": "opend",
                "futu_account_id": str(futu_account_id),
                "order_id": order_id,
                "status": _normalize_order_status(
                    raw.get("order_status") or raw.get("status")
                ),
                "dealt_qty": _decimal_text(raw.get("dealt_qty"), nonnegative=True),
                "currency": normalize_currency(raw.get("currency")) or None,
            },
            source=source,
        )


def _numeric_account_id(value: str) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("futu_account_id must be numeric") from exc


def _plain_records(value: Any) -> list[dict[str, Any]]:
    rows = value.to_dict("records") if hasattr(value, "to_dict") else value
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def _gateway_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and "rows" in value:
        value = value.get("rows")
    return _plain_records(value)


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
