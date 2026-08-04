from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.api.types import is_bool

from src.application.opend_normalize import normalize_opend_option_type
from src.application.required_data_plan_identity import (
    required_data_expiration_dtes,
)
from src.application.required_data_planning import RequiredDataFetchPlanBundle


_INVALID = object()
_EXACT_STRIKE_ABS_TOLERANCE = 1e-9


def build_required_data_coverage(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    coverage: dict[str, dict[str, Any]] = {}
    for option_type in ("put", "call"):
        side_rows = [
            row
            for row in rows
            if isinstance(row, dict) and normalize_opend_option_type(row.get("option_type")) == option_type
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
    return required_data_frame_covers_fetch_plan(df=df, fetch_plan=fetch_plan)


def required_data_frame_covers_fetch_plan(*, df: pd.DataFrame, fetch_plan: RequiredDataFetchPlanBundle) -> bool:
    if df.empty:
        return False
    if not _spot_reference_matches_frame(df=df, spot_reference=fetch_plan.spot_reference):
        return False
    trading_date = _typed_fetch_plan_trading_date(fetch_plan)
    if not isinstance(trading_date, date):
        return False
    require_realized_volatility = fetch_plan.require_realized_volatility
    if not isinstance(require_realized_volatility, bool):
        return False
    if any(
        not isinstance(spec.include_realized_volatility, bool)
        or spec.include_realized_volatility != require_realized_volatility
        for spec in fetch_plan.merged_specs
    ):
        return False
    if require_realized_volatility and not _has_realized_volatility(df):
        return False
    active_side_count = 0
    for side_plan in fetch_plan.side_plans:
        requested_expirations = _strict_expiration_list(
            side_plan.explicit_expirations
        )
        if requested_expirations is None:
            return False
        if not requested_expirations:
            continue
        active_side_count += 1
        side_min_dte = _strict_optional_nonnegative_int_value(
            side_plan.min_dte
        )
        side_max_dte = _strict_optional_nonnegative_int_value(
            side_plan.max_dte
        )
        if side_min_dte is _INVALID or side_max_dte is _INVALID:
            return False
        if not _valid_optional_range(side_min_dte, side_max_dte):
            return False
        try:
            expected_dtes = required_data_expiration_dtes(
                trading_date=trading_date,
                expirations=requested_expirations,
            )
        except ValueError:
            return False
        if any(
            not _value_within_optional_range(
                value=dte,
                minimum=side_min_dte,
                maximum=side_max_dte,
            )
            for dte in expected_dtes.values()
        ):
            return False
        side_df = _filter_option_type(df, side_plan.option_type)
        if side_df.empty or "expiration" not in side_df.columns:
            return False
        base_min = side_plan.strike_window.base_min_strike
        base_max = side_plan.strike_window.base_max_strike
        exact_strikes_by_expiration = _strict_exact_strikes_by_expiration(
            side_plan.required_exact_strikes_by_expiration,
            allowed_expirations=requested_expirations,
        )
        if exact_strikes_by_expiration is None:
            return False
        for expiration in requested_expirations:
            exp_df = side_df[
                side_df["expiration"].astype(str) == expiration
            ].copy()
            if exp_df.empty or not _frame_dte_matches(
                df=exp_df,
                expected_dte=expected_dtes[expiration],
                minimum=side_min_dte,
                maximum=side_max_dte,
            ):
                return False
            strikes = _numeric_series(exp_df, "strike")
            if not _strikes_cover_bounds(
                strikes=strikes,
                base_min=base_min,
                base_max=base_max,
            ):
                return False
            if not _strikes_cover_exact_requirements(
                strikes=strikes,
                required_strikes=exact_strikes_by_expiration.get(
                    str(expiration),
                    [],
                ),
            ):
                return False
    if active_side_count <= 0:
        return False

    active_request_count = 0
    for spec in fetch_plan.merged_specs:
        if spec.trading_date != trading_date.isoformat():
            return False
        requested_expirations = _strict_expiration_list(
            spec.explicit_expirations
        )
        if requested_expirations is None:
            return False
        if not requested_expirations:
            return False
        active_request_count += 1
        request_min_dte = _strict_optional_nonnegative_int_value(spec.min_dte)
        request_max_dte = _strict_optional_nonnegative_int_value(spec.max_dte)
        if request_min_dte is _INVALID or request_max_dte is _INVALID:
            return False
        if not _valid_optional_range(request_min_dte, request_max_dte):
            return False
        try:
            expected_dtes = required_data_expiration_dtes(
                trading_date=trading_date,
                expirations=requested_expirations,
            )
        except ValueError:
            return False
        if any(
            not _value_within_optional_range(
                value=dte,
                minimum=request_min_dte,
                maximum=request_max_dte,
            )
            for dte in expected_dtes.values()
        ):
            return False

        option_types = list(spec.option_types)
        if (
            not option_types
            or any(option_type not in {"put", "call"} for option_type in option_types)
            or len(option_types) != len(set(option_types))
        ):
            return False
        nested_side_plans = {
            side_plan.option_type: side_plan
            for side_plan in spec.side_plans
        }
        if (
            len(nested_side_plans) != len(spec.side_plans)
            or set(nested_side_plans) != set(option_types)
        ):
            return False
        for option_type in option_types:
            side_plan = nested_side_plans[option_type]
            side_expirations = _strict_expiration_list(
                side_plan.explicit_expirations
            )
            if side_expirations != requested_expirations:
                return False
            side_min_dte = _strict_optional_nonnegative_int_value(
                side_plan.min_dte
            )
            side_max_dte = _strict_optional_nonnegative_int_value(
                side_plan.max_dte
            )
            if side_min_dte is _INVALID or side_max_dte is _INVALID:
                return False
            if not _valid_optional_range(side_min_dte, side_max_dte):
                return False
            if any(
                not _value_within_optional_range(
                    value=dte,
                    minimum=side_min_dte,
                    maximum=side_max_dte,
                )
                for dte in expected_dtes.values()
            ):
                return False
            exact_strikes_by_expiration = (
                _strict_exact_strikes_by_expiration(
                    side_plan.required_exact_strikes_by_expiration,
                    allowed_expirations=side_expirations,
                )
            )
            if exact_strikes_by_expiration is None:
                return False
            side_df = _filter_option_type(df, option_type)
            if side_df.empty or "expiration" not in side_df.columns:
                return False
            for expiration in requested_expirations:
                exp_df = side_df[
                    side_df["expiration"].astype(str) == expiration
                ].copy()
                if exp_df.empty or not _frame_dte_matches(
                    df=exp_df,
                    expected_dte=expected_dtes[expiration],
                    minimum=request_min_dte,
                    maximum=request_max_dte,
                ):
                    return False
                if not _frame_dte_matches(
                    df=exp_df,
                    expected_dte=expected_dtes[expiration],
                    minimum=side_min_dte,
                    maximum=side_max_dte,
                ):
                    return False
                strikes = _numeric_series(exp_df, "strike")
                if not _strikes_cover_bounds(
                    strikes=strikes,
                    base_min=side_plan.strike_window.base_min_strike,
                    base_max=side_plan.strike_window.base_max_strike,
                ):
                    return False
                if not _strikes_cover_exact_requirements(
                    strikes=strikes,
                    required_strikes=exact_strikes_by_expiration.get(
                        expiration,
                        [],
                    ),
                ):
                    return False
    if active_request_count <= 0:
        return False
    return True


def required_data_frame_covers_fetch_plan_debug(
    df: pd.DataFrame,
    fetch_plan: Mapping[str, Any],
) -> bool:
    """Validate CSV rows against the stable ``to_debug_dict`` plan shape."""

    if df.empty or not isinstance(fetch_plan, Mapping):
        return False
    if not _spot_reference_matches_frame(
        df=df,
        spot_reference=fetch_plan.get("spot_reference"),
    ):
        return False
    if _numeric_series(df, "strike").empty:
        return False

    trading_date = _fetch_plan_trading_date(fetch_plan)
    if not isinstance(trading_date, date):
        return False

    merged_requests = fetch_plan.get("merged_requests")
    if not isinstance(merged_requests, list):
        return False
    require_realized_volatility = fetch_plan.get(
        "require_realized_volatility"
    )
    if not isinstance(require_realized_volatility, bool):
        return False
    active_request_count = 0
    for raw_request in merged_requests:
        if not isinstance(raw_request, Mapping):
            return False
        if raw_request.get("trading_date") != trading_date.isoformat():
            return False
        requested_expirations = _strict_expiration_list(
            raw_request.get("explicit_expirations")
        )
        if requested_expirations is None:
            return False
        raw_rv_flag = raw_request.get("include_realized_volatility")
        if (
            not isinstance(raw_rv_flag, bool)
            or raw_rv_flag != require_realized_volatility
        ):
            return False
        if not requested_expirations:
            return False

        active_request_count += 1
        request_min_dte = _strict_optional_nonnegative_int(
            raw_request,
            "min_dte",
        )
        request_max_dte = _strict_optional_nonnegative_int(
            raw_request,
            "max_dte",
        )
        if request_min_dte is _INVALID or request_max_dte is _INVALID:
            return False
        if not _valid_optional_range(request_min_dte, request_max_dte):
            return False

        option_types = raw_request.get("option_types")
        if (
            not isinstance(option_types, list)
            or not option_types
            or any(
                not isinstance(option_type, str)
                or option_type not in {"put", "call"}
                for option_type in option_types
            )
            or len(option_types) != len(set(option_types))
        ):
            return False
        raw_side_plans = raw_request.get("side_plans")
        if not isinstance(raw_side_plans, list):
            return False
        side_plans: dict[str, Mapping[str, Any]] = {}
        for raw_side_plan in raw_side_plans:
            if not isinstance(raw_side_plan, Mapping):
                return False
            option_type = raw_side_plan.get("option_type")
            if option_type not in option_types or option_type in side_plans:
                return False
            side_plans[str(option_type)] = raw_side_plan
        if set(side_plans) != set(option_types):
            return False

        raw_request_windows = raw_request.get("side_strike_windows")
        if (
            not isinstance(raw_request_windows, Mapping)
            or set(raw_request_windows) != set(option_types)
        ):
            return False

        try:
            expected_dtes = required_data_expiration_dtes(
                trading_date=trading_date,
                expirations=requested_expirations,
            )
        except ValueError:
            return False
        if any(
            not _value_within_optional_range(
                value=dte,
                minimum=request_min_dte,
                maximum=request_max_dte,
            )
            for dte in expected_dtes.values()
        ):
            return False

        for option_type in option_types:
            raw_request_window = raw_request_windows.get(option_type)
            if not _valid_request_strike_window(raw_request_window):
                return False
            raw_side_plan = side_plans[option_type]
            side_expirations = _strict_expiration_list(
                raw_side_plan.get("explicit_expirations")
            )
            if side_expirations != requested_expirations:
                return False
            exact_strikes_by_expiration = (
                _strict_exact_strikes_by_expiration(
                    raw_side_plan.get(
                        "required_exact_strikes_by_expiration"
                    ),
                    allowed_expirations=side_expirations,
                )
            )
            if exact_strikes_by_expiration is None:
                return False
            side_min_dte = _strict_optional_nonnegative_int(
                raw_side_plan,
                "min_dte",
            )
            side_max_dte = _strict_optional_nonnegative_int(
                raw_side_plan,
                "max_dte",
            )
            if side_min_dte is _INVALID or side_max_dte is _INVALID:
                return False
            if not _valid_optional_range(side_min_dte, side_max_dte):
                return False

            effective_bounds = _effective_side_strike_bounds(raw_side_plan)
            if effective_bounds is None:
                return False
            effective_min, effective_max = effective_bounds
            side_df = _filter_option_type(df, option_type)
            if side_df.empty or "expiration" not in side_df.columns:
                return False
            for expiration in requested_expirations:
                expected_dte = expected_dtes[expiration]
                if not _value_within_optional_range(
                    value=expected_dte,
                    minimum=side_min_dte,
                    maximum=side_max_dte,
                ):
                    return False
                exp_df = side_df[
                    side_df["expiration"].astype(str) == expiration
                ].copy()
                if exp_df.empty or not _frame_dte_matches(
                    df=exp_df,
                    expected_dte=expected_dte,
                    minimum=request_min_dte,
                    maximum=request_max_dte,
                ):
                    return False
                if not _frame_dte_matches(
                    df=exp_df,
                    expected_dte=expected_dte,
                    minimum=side_min_dte,
                    maximum=side_max_dte,
                ):
                    return False
                strikes = _numeric_series(exp_df, "strike")
                if not _strikes_cover_bounds(
                    strikes=strikes,
                    base_min=effective_min,
                    base_max=effective_max,
                ):
                    return False
                if not _strikes_cover_exact_requirements(
                    strikes=strikes,
                    required_strikes=exact_strikes_by_expiration.get(
                        expiration,
                        [],
                    ),
                ):
                    return False

    if active_request_count <= 0:
        return False
    if require_realized_volatility and not _has_realized_volatility(df):
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
    require_realized_volatility: bool = False,
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
        require_realized_volatility=require_realized_volatility,
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
    require_realized_volatility: bool = False,
) -> bool:
    if df.empty:
        return False
    if require_realized_volatility and not _has_realized_volatility(df):
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


def _fetch_plan_trading_date(
    fetch_plan: Mapping[str, Any],
) -> date | object:
    discovery = fetch_plan.get("expiration_discovery")
    if discovery is None:
        return _INVALID
    if not isinstance(discovery, Mapping):
        return _INVALID
    identity = discovery.get("request_identity")
    if not isinstance(identity, Mapping):
        return _INVALID
    trading_date = _strict_iso_date(identity.get("trading_date"))
    return trading_date if trading_date is not None else _INVALID


def _typed_fetch_plan_trading_date(
    fetch_plan: RequiredDataFetchPlanBundle,
) -> date | object:
    discovery = fetch_plan.expiration_discovery
    if discovery is None or not isinstance(discovery.request_identity, Mapping):
        return _INVALID
    trading_date = _strict_iso_date(
        discovery.request_identity.get("trading_date")
    )
    return trading_date if trading_date is not None else _INVALID


def _strict_expiration_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    expirations: list[str] = []
    for raw_expiration in value:
        if not isinstance(raw_expiration, str):
            return None
        expiration = raw_expiration.strip()
        if (
            not expiration
            or expiration != raw_expiration
            or _strict_iso_date(expiration) is None
            or expiration in expirations
        ):
            return None
        expirations.append(expiration)
    return expirations


def _strict_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if parsed.isoformat() != value:
        return None
    return parsed


def _strict_exact_strikes_by_expiration(
    value: Any,
    *,
    allowed_expirations: list[str],
) -> dict[str, list[float]] | None:
    if not isinstance(value, Mapping):
        return None
    allowed = set(allowed_expirations)
    normalized: dict[str, list[float]] = {}
    for raw_expiration, raw_strikes in value.items():
        expiration = _strict_iso_date(raw_expiration)
        if expiration is None or raw_expiration not in allowed:
            return None
        if not isinstance(raw_strikes, list) or not raw_strikes:
            return None
        strikes: list[float] = []
        for raw_strike in raw_strikes:
            strike = _strict_finite_float(raw_strike)
            if strike is None or strike <= 0:
                return None
            strikes.append(strike)
        if strikes != sorted(strikes) or len(strikes) != len(set(strikes)):
            return None
        normalized[str(raw_expiration)] = strikes
    if list(value) != sorted(value):
        return None
    return normalized


def _strict_optional_nonnegative_int(
    mapping: Mapping[str, Any],
    key: str,
) -> int | None | object:
    return _strict_optional_nonnegative_int_value(mapping.get(key))


def _strict_optional_nonnegative_int_value(
    value: Any,
) -> int | None | object:
    if value is None:
        return None
    parsed = _strict_finite_float(value)
    if parsed is None or parsed < 0 or not parsed.is_integer():
        return _INVALID
    return int(parsed)


def _strict_optional_positive_float(
    mapping: Mapping[str, Any],
    key: str,
) -> float | None | object:
    value = mapping.get(key)
    if value is None:
        return None
    parsed = _strict_finite_float(value)
    if parsed is None or parsed <= 0:
        return _INVALID
    return parsed


def _strict_finite_float(value: Any) -> float | None:
    if (
        value is None
        or is_bool(value)
        or isinstance(value, (str, bytes, bytearray))
    ):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _valid_optional_range(minimum: Any, maximum: Any) -> bool:
    return minimum is None or maximum is None or minimum <= maximum


def _value_within_optional_range(
    *,
    value: int,
    minimum: Any,
    maximum: Any,
) -> bool:
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _valid_request_strike_window(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or "min_strike" not in value
        or "max_strike" not in value
    ):
        return False
    minimum = _strict_optional_positive_float(value, "min_strike")
    maximum = _strict_optional_positive_float(value, "max_strike")
    if minimum is _INVALID or maximum is _INVALID:
        return False
    return _valid_optional_range(minimum, maximum)


def _effective_side_strike_bounds(
    raw_side_plan: Mapping[str, Any],
) -> tuple[float | None, float | None] | None:
    raw_window = raw_side_plan.get("strike_window")
    required_keys = {
        "min_strike",
        "max_strike",
        "base_min_strike",
        "base_max_strike",
    }
    if (
        not isinstance(raw_window, Mapping)
        or not required_keys.issubset(raw_window)
    ):
        return None
    fetch_min = _strict_optional_positive_float(raw_window, "min_strike")
    fetch_max = _strict_optional_positive_float(raw_window, "max_strike")
    base_min = _strict_optional_positive_float(raw_window, "base_min_strike")
    base_max = _strict_optional_positive_float(raw_window, "base_max_strike")
    if any(
        value is _INVALID
        for value in (fetch_min, fetch_max, base_min, base_max)
    ):
        return None
    if not _valid_optional_range(fetch_min, fetch_max):
        return None
    if not _valid_optional_range(base_min, base_max):
        return None
    effective_min = base_min if base_min is not None else fetch_min
    effective_max = base_max if base_max is not None else fetch_max
    if not _valid_optional_range(effective_min, effective_max):
        return None
    return effective_min, effective_max


def _frame_dte_matches(
    *,
    df: pd.DataFrame,
    expected_dte: int | None,
    minimum: Any,
    maximum: Any,
) -> bool:
    if "dte" not in df.columns or df.empty:
        return False
    for raw_value in df["dte"].tolist():
        value = _strict_finite_float(raw_value)
        if (
            value is None
            or value < 0
            or not value.is_integer()
        ):
            return False
        normalized = int(value)
        if expected_dte is not None and normalized != expected_dte:
            return False
        if not _value_within_optional_range(
            value=normalized,
            minimum=minimum,
            maximum=maximum,
        ):
            return False
    return True


def _strict_positive_frame_values(
    *,
    df: pd.DataFrame,
    column: str,
) -> list[float] | None:
    if column not in df.columns or df.empty:
        return None
    values: list[float] = []
    for raw_value in df[column].tolist():
        value = _strict_finite_float(raw_value)
        if value is None or value <= 0:
            return None
        values.append(value)
    return values or None


def _spot_reference_matches_frame(*, df: pd.DataFrame, spot_reference: Any) -> bool:
    expected: float | None
    if spot_reference is None:
        expected = None
    else:
        expected = _strict_finite_float(spot_reference)
        if expected is None or expected <= 0:
            return False
    values = _strict_positive_frame_values(df=df, column="spot")
    if values is None:
        return False
    if expected is None:
        return True
    tolerance = max(1e-6, abs(float(expected)) * 1e-6)
    return all(abs(value - expected) <= tolerance for value in values)


def _has_realized_volatility(df: pd.DataFrame) -> bool:
    return (
        _strict_positive_frame_values(
            df=df,
            column="realized_volatility_estimate",
        )
        is not None
    )


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
    normalized = df["option_type"].apply(normalize_opend_option_type)
    return df[normalized == str(option_type)].copy()


def _parse_option_types(value: str) -> tuple[str, ...]:
    out: list[str] = []
    for item in str(value or "").split(","):
        option_type = normalize_opend_option_type(item)
        if option_type in {"put", "call"} and option_type not in out:
            out.append(option_type)
    return tuple(out)


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype=float)
    values: list[float] = []
    for raw_value in df[column].tolist():
        value = _strict_finite_float(raw_value)
        if value is None or value <= 0:
            return pd.Series(dtype=float)
        values.append(value)
    return pd.Series(values, dtype=float)


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return _strict_finite_float(value)


def _strikes_cover_bounds(*, strikes: pd.Series, base_min: float | None, base_max: float | None) -> bool:
    if strikes.empty:
        return False
    normalized_min = None
    if base_min is not None:
        normalized_min = _strict_finite_float(base_min)
        if normalized_min is None or normalized_min <= 0:
            return False
    normalized_max = None
    if base_max is not None:
        normalized_max = _strict_finite_float(base_max)
        if normalized_max is None or normalized_max <= 0:
            return False
    if not _valid_optional_range(normalized_min, normalized_max):
        return False
    unique_strikes = sorted({float(v) for v in strikes.tolist()})
    if not unique_strikes:
        return False
    if any(not math.isfinite(strike) or strike <= 0 for strike in unique_strikes):
        return False
    if normalized_min is not None and max(unique_strikes) < normalized_min:
        return False
    if normalized_max is not None and min(unique_strikes) > normalized_max:
        return False

    in_bounds = [
        strike
        for strike in unique_strikes
        if (normalized_min is None or strike >= normalized_min)
        and (normalized_max is None or strike <= normalized_max)
    ]

    if (
        normalized_min is not None
        and normalized_max is not None
        and normalized_max > normalized_min
    ):
        return _strikes_cover_bounded_edges(
            unique_strikes=unique_strikes,
            base_min=normalized_min,
            base_max=normalized_max,
        )
    if normalized_min is not None or normalized_max is not None:
        return len(in_bounds) >= 1
    return len(unique_strikes) >= 1


def _strikes_cover_exact_requirements(
    *,
    strikes: pd.Series,
    required_strikes: list[float],
) -> bool:
    if not required_strikes:
        return True
    if strikes.empty:
        return False
    actual_strikes = [float(value) for value in strikes.tolist()]
    return all(
        any(
            math.isclose(
                actual,
                expected,
                rel_tol=0.0,
                abs_tol=_EXACT_STRIKE_ABS_TOLERANCE,
            )
            for actual in actual_strikes
        )
        for expected in required_strikes
    )


def _strikes_cover_bounded_edges(*, unique_strikes: list[float], base_min: float, base_max: float) -> bool:
    tolerance = _strike_edge_tolerance(
        unique_strikes=unique_strikes,
        base_min=base_min,
        base_max=base_max,
    )
    nearest_lower_gap = min(abs(strike - base_min) for strike in unique_strikes)
    nearest_upper_gap = min(abs(strike - base_max) for strike in unique_strikes)
    return nearest_lower_gap <= tolerance and nearest_upper_gap <= tolerance


def _strike_edge_tolerance(*, unique_strikes: list[float], base_min: float, base_max: float) -> float:
    width = max(0.0, float(base_max) - float(base_min))
    gaps = [
        abs(float(right) - float(left))
        for left, right in zip(unique_strikes, unique_strikes[1:])
        if abs(float(right) - float(left)) > 0
    ]
    if gaps:
        step = min(gaps)
        if width > 0:
            return max(1e-9, min(float(step), width * 0.25))
        return max(1e-9, float(step))
    if width > 0:
        return max(1e-9, width * 0.05)
    return max(1e-9, abs(float(base_min)) * 0.005)
