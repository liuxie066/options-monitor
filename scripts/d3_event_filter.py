from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd


DEFAULT_D3_EVENT_CFG = {
    "enabled": True,
    "mode": "warn",
}


def normalize_d3_event_cfg(cfg: dict | None) -> dict:
    out = dict(DEFAULT_D3_EVENT_CFG)
    if isinstance(cfg, dict):
        out.update(cfg)
    out["enabled"] = bool(out.get("enabled", True))
    mode = str(out.get("mode") or "warn").strip().lower()
    out["mode"] = mode or "warn"
    return out


def _to_date_str(value) -> str | None:
    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_convert(None)
    except Exception:
        pass
    return ts.date().isoformat()


def fetch_symbol_events_yfinance(symbol: str) -> list[dict]:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    events: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(event_type: str, raw_value) -> None:
        ds = _to_date_str(raw_value)
        if not ds:
            return
        key = (event_type, ds)
        if key in seen:
            return
        seen.add(key)
        events.append({"type": event_type, "date": ds})

    try:
        edf = ticker.get_earnings_dates(limit=8)
        if isinstance(edf, pd.DataFrame) and not edf.empty:
            for idx in edf.index:
                _add("earnings", idx)
    except Exception:
        pass

    try:
        cal = ticker.calendar
        if isinstance(cal, pd.DataFrame) and not cal.empty:
            for key in ("Earnings Date", "Ex-Dividend Date"):
                if key not in cal.index:
                    continue
                row = cal.loc[key]
                if isinstance(row, pd.Series):
                    for v in row.tolist():
                        _add("earnings" if key == "Earnings Date" else "ex_dividend", v)
                else:
                    _add("earnings" if key == "Earnings Date" else "ex_dividend", row)
    except Exception:
        pass

    try:
        div = ticker.get_dividends()
        if isinstance(div, pd.Series) and not div.empty:
            cutoff = datetime.now(timezone.utc).date() - timedelta(days=180)
            for idx in div.index:
                ds = _to_date_str(idx)
                if not ds:
                    continue
                if ds >= cutoff.isoformat():
                    _add("ex_dividend", ds)
    except Exception:
        pass

    events.sort(key=lambda x: (x.get("date") or "", x.get("type") or ""))
    return events


@dataclass
class EventCache:
    path: Path
    ttl_seconds: int = 86400

    def _load(self) -> dict:
        if not self.path.exists():
            return {"symbols": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("symbols", {})
                return data
        except Exception:
            pass
        return {"symbols": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_events(
        self,
        symbol: str,
        *,
        fetcher: Callable[[str], list[dict]],
        now: datetime | None = None,
    ) -> list[dict]:
        now_dt = now or datetime.now(timezone.utc)
        data = self._load()
        symbols = data.get("symbols")
        if not isinstance(symbols, dict):
            symbols = {}
            data["symbols"] = symbols
        key = str(symbol or "").upper()
        entry = symbols.get(key) if isinstance(symbols.get(key), dict) else None

        if entry:
            fetched_at = _to_date_str(entry.get("fetched_at"))
            if fetched_at:
                try:
                    dt = datetime.fromisoformat(str(entry.get("fetched_at")))
                    if (now_dt - dt).total_seconds() <= self.ttl_seconds:
                        events = entry.get("events")
                        if isinstance(events, list):
                            return [x for x in events if isinstance(x, dict)]
                except Exception:
                    pass

        events: list[dict] = []
        try:
            events = fetcher(key)
        except Exception:
            events = []
        symbols[key] = {
            "fetched_at": now_dt.isoformat(),
            "events": events,
        }
        self._save(data)
        return events


def annotate_candidates_with_d3_events(
    df: pd.DataFrame,
    *,
    base_dir: Path,
    d3_event_cfg: dict | None = None,
    event_fetcher: Callable[[str], list[dict]] | None = None,
) -> pd.DataFrame:
    out = df.copy()
    for col, default in (
        ("event_flag", False),
        ("event_types", ""),
        ("event_dates", ""),
        ("reject_stage_candidate", ""),
    ):
        if col not in out.columns:
            out[col] = default

    cfg = normalize_d3_event_cfg(d3_event_cfg)
    if not cfg.get("enabled"):
        return out

    cache = EventCache((base_dir / "output_shared" / "state" / "event_cache.json").resolve(), ttl_seconds=86400)
    fetcher = event_fetcher or fetch_symbol_events_yfinance
    symbol_events: dict[str, list[dict]] = {}

    symbols = sorted({str(s).upper() for s in out.get("symbol", pd.Series(dtype=str)).dropna().tolist() if str(s).strip()})
    for sym in symbols:
        symbol_events[sym] = cache.get_events(sym, fetcher=fetcher)

    flagged = []
    types_list = []
    dates_list = []
    reject_stage = []
    for _, row in out.iterrows():
        sym = str(row.get("symbol") or "").upper()
        expiration = _to_date_str(row.get("expiration"))
        if not sym or not expiration:
            flagged.append(False)
            types_list.append("")
            dates_list.append("")
            reject_stage.append(str(row.get("reject_stage_candidate") or ""))
            continue

        exp_date = datetime.fromisoformat(expiration).date()
        events = symbol_events.get(sym) or []
        hits = []
        for ev in events:
            d = _to_date_str(ev.get("date"))
            t = str(ev.get("type") or "").strip()
            if not d or not t:
                continue
            if datetime.fromisoformat(d).date() <= exp_date:
                hits.append((d, t))
        hits = sorted(set(hits))

        if hits:
            flagged.append(True)
            types_list.append(",".join(sorted({t for _, t in hits})))
            dates_list.append(",".join([d for d, _ in hits]))
            reject_stage.append("D3_EVENT_WARN" if cfg.get("mode") == "warn" else str(row.get("reject_stage_candidate") or ""))
        else:
            flagged.append(False)
            types_list.append("")
            dates_list.append("")
            reject_stage.append(str(row.get("reject_stage_candidate") or ""))

    out["event_flag"] = flagged
    out["event_types"] = types_list
    out["event_dates"] = dates_list
    out["reject_stage_candidate"] = reject_stage
    return out
