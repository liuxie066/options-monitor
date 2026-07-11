from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.candidate_defaults import normalize_event_risk_mode
from domain.domain.expiration_dates import expiration_business_today
from domain.domain.symbol_identity import canonical_symbol
from src.application.events.source_yfinance import to_date_str


def load_event_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def annotate_candidates_with_event_snapshot(
    df: pd.DataFrame,
    *,
    snapshot: dict[str, Any] | None = None,
    snapshot_path: Path | None = None,
    event_risk_cfg: dict[str, Any] | None = None,
    as_of_date: Any | None = None,
) -> pd.DataFrame:
    out = df.copy()
    for col, default in (
        ("event_flag", False),
        ("event_types", ""),
        ("event_dates", ""),
        ("event_source_status", ""),
        ("event_source_error", ""),
        ("reject_stage_candidate", ""),
    ):
        if col not in out.columns:
            out[col] = default

    cfg = _normalize_event_risk_cfg(event_risk_cfg)
    if not cfg.get("enabled"):
        out["event_source_status"] = "disabled"
        return out

    window_start = _as_of_date(as_of_date if as_of_date is not None else cfg.get("as_of_date"))
    payload = snapshot if isinstance(snapshot, dict) else load_event_snapshot(snapshot_path)
    symbols = _snapshot_symbols(payload)

    flagged = []
    types_list = []
    dates_list = []
    source_status_list = []
    source_error_list = []
    reject_stage = []
    for _, row in out.iterrows():
        sym = _canonical(row.get("symbol"))
        expiration = to_date_str(row.get("expiration"))
        item = symbols.get(sym) if sym else None
        if not isinstance(item, dict):
            item = _missing_snapshot_item(sym)
        source_status = str(item.get("source_status") or "error")
        source_error = str(item.get("source_error") or "")
        if not sym or not expiration:
            flagged.append(False)
            types_list.append("")
            dates_list.append("")
            source_status_list.append(source_status)
            source_error_list.append(source_error)
            reject_stage.append(str(row.get("reject_stage_candidate") or ""))
            continue

        exp_date = datetime.fromisoformat(expiration).date()
        hits = []
        for ev in item.get("events") or []:
            if not isinstance(ev, dict):
                continue
            d = to_date_str(ev.get("date"))
            t = str(ev.get("type") or "").strip()
            if not d or not t:
                continue
            event_date = datetime.fromisoformat(d).date()
            if window_start <= event_date <= exp_date:
                hits.append((d, t))
        hits = sorted(set(hits))

        if hits:
            flagged.append(True)
            types_list.append(",".join(sorted({t for _, t in hits})))
            dates_list.append(",".join([d for d, _ in hits]))
            source_status_list.append(source_status)
            source_error_list.append(source_error)
            reject_stage.append("EVENT_WARN" if cfg.get("mode") == "warn" else str(row.get("reject_stage_candidate") or ""))
        else:
            flagged.append(False)
            types_list.append("")
            dates_list.append("")
            source_status_list.append(source_status)
            source_error_list.append(source_error)
            reject_stage.append(str(row.get("reject_stage_candidate") or ""))

    out["event_flag"] = flagged
    out["event_types"] = types_list
    out["event_dates"] = dates_list
    out["event_source_status"] = source_status_list
    out["event_source_error"] = source_error_list
    out["reject_stage_candidate"] = reject_stage
    return out


def _normalize_event_risk_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    out = {"enabled": True, "mode": "warn"}
    if isinstance(cfg, dict):
        out.update(cfg)
    out["enabled"] = bool(out.get("enabled", True))
    out["mode"] = normalize_event_risk_mode(out.get("mode"))
    return out


def _as_of_date(value: Any | None) -> date:
    if value in (None, ""):
        return expiration_business_today()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    ds = to_date_str(value)
    if not ds:
        return expiration_business_today()
    return datetime.fromisoformat(ds).date()


def _snapshot_symbols(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        sym = _canonical(key)
        if sym and isinstance(value, dict):
            out[sym] = value
    return out


def _canonical(value: Any) -> str:
    return canonical_symbol(value) or str(value or "").strip().upper()


def _missing_snapshot_item(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "events": [],
        "source_status": "error",
        "source_error": "event snapshot missing for symbol",
        "error_code": "snapshot_missing",
    }
