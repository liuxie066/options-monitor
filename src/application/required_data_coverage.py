from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from src.application.required_data_planning import RequiredDataFetchPlanBundle


def build_required_data_coverage(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    coverage: dict[str, dict[str, Any]] = {}
    for option_type in ("put", "call"):
        side_rows = [
            row
            for row in rows
            if isinstance(row, dict) and _normalize_option_type(row.get("option_type")) == option_type
        ]
        strikes = [
            float(row.get("strike"))
            for row in side_rows
            if _safe_float(row.get("strike")) is not None
        ]
        dtes = [
            int(float(row.get("dte")))
            for row in side_rows
            if _safe_float(row.get("dte")) is not None
        ]
        expirations = sorted({
            str(row.get("expiration"))
            for row in side_rows
            if str(row.get("expiration") or "").strip()
        })
        coverage[option_type] = {
            "row_count": len(side_rows),
            "min_strike": (min(strikes) if strikes else None),
            "max_strike": (max(strikes) if strikes else None),
            "min_dte": (min(dtes) if dtes else None),
            "max_dte": (max(dtes) if dtes else None),
            "expirations": expirations,
        }
    return coverage


def load_required_data_payload_from_csv(*, parsed: Path, symbol: str) -> dict[str, object]:
    df = _read_required_data_csv(parsed)
    rows = df.to_dict(orient="records") if not df.empty else []
    expirations = sorted({
        str(row.get("expiration"))
        for row in rows
        if isinstance(row, dict) and row.get("expiration")
    })
    return {
        "symbol": symbol,
        "rows": rows,
        "expirations": expirations,
        "expiration_count": len(expirations),
    }


def required_data_csv_covers_fetch_plan(*, parsed: Path, fetch_plan: RequiredDataFetchPlanBundle) -> bool:
    df = _read_required_data_csv(parsed)
    if df.empty:
        return False
    for side_plan in fetch_plan.side_plans:
        side_df = _filter_option_type(df, side_plan.option_type)
        if side_df.empty:
            return False
        requested_expirations = list(side_plan.explicit_expirations or [])
        base_min = side_plan.strike_window.base_min_strike
        base_max = side_plan.strike_window.base_max_strike
        if requested_expirations:
            expirations_to_check = requested_expirations
        elif "expiration" in side_df.columns:
            expirations_to_check = sorted({
                str(value)
                for value in side_df["expiration"].astype(str).tolist()
                if str(value)
            })
        else:
            expirations_to_check = []
        if not expirations_to_check:
            return False
        for expiration in expirations_to_check:
            exp_df = (
                side_df[side_df["expiration"].astype(str) == str(expiration)].copy()
                if "expiration" in side_df.columns
                else pd.DataFrame()
            )
            if exp_df.empty:
                return False
            strikes = _numeric_series(exp_df, "strike")
            if not _strikes_cover_bounds(
                strikes=strikes,
                base_min=base_min,
                base_max=base_max,
            ):
                return False
    return True


def required_data_csv_covers_strategy_bounds(
    *,
    parsed: Path,
    option_types: str,
    min_dte: int | None = None,
    max_dte: int | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
    side_strike_windows: dict[str, dict[str, float | None]] | None = None,
) -> bool:
    df = _read_required_data_csv(parsed)
    return required_data_frame_covers_strategy_bounds(
        df=df,
        option_types=option_types,
        min_dte=min_dte,
        max_dte=max_dte,
        min_strike=min_strike,
        max_strike=max_strike,
        side_strike_windows=side_strike_windows,
    )


def required_data_frame_covers_strategy_bounds(
    *,
    df: pd.DataFrame,
    option_types: str,
    min_dte: int | None = None,
    max_dte: int | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
    side_strike_windows: dict[str, dict[str, float | None]] | None = None,
) -> bool:
    if df.empty:
        return False
    wanted_types = _parse_option_types(option_types)
    if not wanted_types:
        wanted_types = ("put", "call")

    for option_type in wanted_types:
        side_df = _filter_option_type(df, option_type)
        if side_df.empty:
            return False
        if "dte" not in side_df.columns and (min_dte is not None or max_dte is not None):
            return False
        if "dte" in side_df.columns and (min_dte is not None or max_dte is not None):
            dtes = pd.to_numeric(side_df["dte"], errors="coerce")
            if dtes.dropna().empty:
                return False
            if max_dte is not None and float(dtes.dropna().max()) < float(max_dte):
                return False
            if min_dte is not None:
                side_df = side_df[dtes >= int(min_dte)].copy()
                dtes = pd.to_numeric(side_df["dte"], errors="coerce") if not side_df.empty else dtes.iloc[0:0]
            if max_dte is not None:
                side_df = side_df[dtes <= int(max_dte)].copy()
        if side_df.empty:
            return False

        side_window = (side_strike_windows or {}).get(option_type)
        side_min = _safe_float((side_window or {}).get("min_strike")) if isinstance(side_window, dict) else None
        side_max = _safe_float((side_window or {}).get("max_strike")) if isinstance(side_window, dict) else None
        effective_min = side_min if side_min is not None else _safe_float(min_strike)
        effective_max = side_max if side_max is not None else _safe_float(max_strike)
        strikes = _numeric_series(side_df, "strike")
        if not _strikes_cover_bounds(
            strikes=strikes,
            base_min=effective_min,
            base_max=effective_max,
        ):
            return False
    return True


def _read_required_data_csv(parsed: Path) -> pd.DataFrame:
    try:
        path = Path(parsed)
        if not path.exists() or path.stat().st_size <= 0:
            return pd.DataFrame()
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _filter_option_type(df: pd.DataFrame, option_type: str) -> pd.DataFrame:
    if "option_type" not in df.columns:
        return pd.DataFrame()
    normalized = df["option_type"].apply(_normalize_option_type)
    return df[normalized == str(option_type)].copy()


def _normalize_option_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"put", "call"}:
        return raw
    if "put" in raw:
        return "put"
    if "call" in raw:
        return "call"
    return raw


def _parse_option_types(value: str) -> tuple[str, ...]:
    out: list[str] = []
    for item in str(value or "").split(","):
        option_type = _normalize_option_type(item)
        if option_type in {"put", "call"} and option_type not in out:
            out.append(option_type)
    return tuple(out)


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[column], errors="coerce").dropna()


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        parsed = float(value)
        if math.isnan(parsed):
            return None
        return parsed
    except Exception:
        return None


def _strikes_cover_bounds(*, strikes: pd.Series, base_min: float | None, base_max: float | None) -> bool:
    if strikes.empty:
        return False
    unique_strikes = sorted({float(v) for v in strikes.tolist()})
    if not unique_strikes:
        return False
    if base_min is not None and max(unique_strikes) < float(base_min):
        return False
    if base_max is not None and min(unique_strikes) > float(base_max):
        return False

    in_bounds = [
        strike
        for strike in unique_strikes
        if (base_min is None or strike >= float(base_min))
        and (base_max is None or strike <= float(base_max))
    ]

    if base_min is not None and base_max is not None and float(base_max) > float(base_min):
        return len(in_bounds) >= 3
    if base_min is not None or base_max is not None:
        return len(in_bounds) >= 1
    return len(unique_strikes) >= 1
