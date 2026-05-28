from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.application.events.annotator import annotate_candidates_with_event_snapshot
from src.application.events.source_yfinance import EventSourceError, fetch_symbol_events_yfinance
from src.application.events.store import EventStore


DEFAULT_EVENT_RISK_CFG = {
    "enabled": True,
    "mode": "warn",
}


def normalize_event_risk_cfg(cfg: dict | None) -> dict:
    out = dict(DEFAULT_EVENT_RISK_CFG)
    if isinstance(cfg, dict):
        out.update(cfg)
    out["enabled"] = bool(out.get("enabled", True))
    mode = str(out.get("mode") or "warn").strip().lower()
    out["mode"] = mode or "warn"
    return out


def annotate_candidates_with_event_risk(
    df: pd.DataFrame,
    *,
    base_dir: Path,
    event_risk_cfg: dict | None = None,
    event_fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
) -> pd.DataFrame:
    cfg = normalize_event_risk_cfg(event_risk_cfg)
    snapshot = cfg.get("snapshot") if isinstance(cfg.get("snapshot"), dict) else None
    snapshot_path_raw = cfg.get("snapshot_path")
    snapshot_path = Path(snapshot_path_raw).resolve() if snapshot_path_raw else None
    if snapshot is not None or snapshot_path is not None or event_fetcher is None:
        return annotate_candidates_with_event_snapshot(
            df,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            event_risk_cfg=cfg,
        )

    # Explicit fetcher mode is kept for focused unit tests and manual diagnostics.
    # Production scan entry points should pass a run-level snapshot instead.
    symbols = sorted({str(s).strip().upper() for s in df.get("symbol", pd.Series(dtype=str)).dropna().tolist() if str(s).strip()})
    store = EventStore((Path(base_dir) / "output_shared" / "state" / "event_store.json").resolve())
    snapshot_payload = {
        "schema_version": 1,
        "provider": "yfinance",
        "symbols": {
            symbol: store.resolve(symbol, fetcher=event_fetcher).to_snapshot_item()
            for symbol in symbols
        },
    }
    return annotate_candidates_with_event_snapshot(
        df,
        snapshot=snapshot_payload,
        event_risk_cfg=cfg,
    )
