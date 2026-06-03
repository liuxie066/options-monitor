from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


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
    futu_mod = importlib.import_module("futu")
    start_date, end_date, window_start_utc, window_end_utc = history_deal_query_dates(
        lookback_hours=lookback_hours,
        now=now,
    )
    ctx = futu_mod.OpenSecTradeContext(host=str(host), port=int(port))
    rows: list[dict[str, Any]] = []
    account_results: list[dict[str, Any]] = []
    try:
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
    finally:
        close = getattr(ctx, "close", None)
        if callable(close):
            close()

    diagnostics = {
        "start_date": start_date,
        "end_date": end_date,
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        "lookback_hours": float(lookback_hours),
        "account_results": account_results,
    }
    return rows, diagnostics
