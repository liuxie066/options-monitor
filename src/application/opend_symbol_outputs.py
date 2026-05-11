from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.infrastructure.io_utils import atomic_write_text


REQUIRED_DATA_COLUMNS = [
    "symbol",
    "option_type",
    "expiration",
    "dte",
    "contract_symbol",
    "strike",
    "spot",
    "bid",
    "ask",
    "last_price",
    "mid",
    "volume",
    "open_interest",
    "implied_volatility",
    "in_the_money",
    "currency",
    "otm_pct",
    "delta",
    "multiplier",
]


def append_metrics_json(metrics_path: Path, payload: dict[str, Any], max_entries: int = 400) -> None:
    """Append payload into a bounded JSON list file. Keeps last max_entries records."""
    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        arr = []
        if metrics_path.exists() and metrics_path.stat().st_size > 0:
            try:
                obj = json.loads(metrics_path.read_text(encoding="utf-8"))
                if isinstance(obj, list):
                    arr = obj
            except Exception:
                arr = []
        arr.append(payload)
        if len(arr) > int(max_entries):
            arr = arr[-int(max_entries) :]
        metrics_path.write_text(json.dumps(arr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def save_outputs(base: Path, symbol: str, payload: dict[str, Any], *, output_root: Path | None = None) -> tuple[Path, Path]:
    root = output_root.resolve() if output_root is not None else (base / "output").resolve()
    raw_dir = root / "raw"
    parsed_dir = root / "parsed"
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"{symbol}_required_data.json"
    csv_path = parsed_dir / f"{symbol}_required_data.csv"

    try:
        from src.application.required_data_validation import validate_required_rows

        rows0 = payload.get("rows") or []
        rows1, st = validate_required_rows(rows0)
        payload["rows"] = rows1
        meta = payload.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {"meta": str(meta)}
        meta["validation"] = {
            "total_rows": int(st.total_rows),
            "kept_rows": int(st.kept_rows),
            "dropped_rows": int(st.dropped_rows),
            "missing_strike": int(st.missing_strike),
            "missing_expiration": int(st.missing_expiration),
            "missing_dte": int(st.missing_dte),
            "missing_option_type": int(st.missing_option_type),
        }
        payload["meta"] = meta
    except Exception:
        pass

    atomic_write_text(raw_path, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    df = pd.DataFrame(payload.get("rows") or [])
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if df.empty and str((meta or {}).get("status") or "").lower() == "error" and csv_path.exists() and csv_path.stat().st_size > 0:
        return raw_path, csv_path

    if df.empty:
        df_out = pd.DataFrame(columns=REQUIRED_DATA_COLUMNS)
    else:
        for column in REQUIRED_DATA_COLUMNS:
            if column not in df.columns:
                df[column] = pd.NA
        df_out = df[REQUIRED_DATA_COLUMNS]

    buf = io.StringIO()
    df_out.to_csv(buf, index=False)
    atomic_write_text(csv_path, buf.getvalue(), encoding="utf-8")
    return raw_path, csv_path
