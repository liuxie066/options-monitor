"""Required-data fetch step.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path
import json

from scripts import pipeline_fetch_models
from src.application.opend_symbol_fetching import save_outputs
from src.application.required_data_fetching import (
    RequiredDataFetchRequest,
    build_fetch_request_from_spec,
    execute_required_data_opend,
    fetch_required_data_opend,
    merge_required_data_payloads,
)
from src.application.opend_fetch_config import filter_opend_fetch_kwargs
from src.application.required_data_planning import RequiredDataFetchPlanBundle


def ensure_required_data(
    *,
    py: str,
    base: Path,
    symbol: str,
    required_data_dir: Path,
    limit_expirations: int,
    want_put: bool,
    want_call: bool,
    timeout_sec: int | None,
    is_scheduled: bool,
    state_dir: Path | None = None,
    fetch_source: str = 'opend',
    fetch_host: str = '127.0.0.1',
    fetch_port: int = 11111,
    max_strike: float | None = None,
    min_dte: int | None = None,
    max_dte: int | None = None,
    fetch_plan: RequiredDataFetchPlanBundle | None = None,
    report_dir: Path | None = None,
    opend_fetch_config: dict[str, float | int] | None = None,
) -> None:
    sym = symbol
    parsed = (required_data_dir / 'parsed' / f"{sym}_required_data.csv").resolve()

    if not (want_put or want_call):
        return

    src = 'opend'

    # In dev mode, keep fetch write/read model separated from pipeline orchestration:
    # - write model: fetch_required_data.events.jsonl + fetch_required_data.snapshots.json
    # - read model:  state/current/fetch_required_data.current.json
    # This keeps delivery/pipeline path from directly reading raw fetch artifacts.
    fetch_current = None
    if (not is_scheduled) and (state_dir is not None):
        try:
            fetch_current = pipeline_fetch_models.backfill_symbol_snapshot_from_raw(
                required_data_dir=required_data_dir,
                state_dir=state_dir,
                symbol=sym,
                source=src,
            )
        except Exception:
            fetch_current = None

    # Always fetch before scan if required_data missing.
    # Also refetch when:
    # - read-model shows previous fetch status=error
    # - min_dte is requested but existing required_data doesn't reach that DTE.
    if parsed.exists() and parsed.stat().st_size > 0:
        should_refetch = False
        if isinstance(fetch_current, dict):
            if str(fetch_current.get('status') or '').lower() == 'error':
                should_refetch = True

        if not should_refetch:
            if fetch_plan is not None:
                try:
                    if _existing_required_data_covers_bounds_plan(parsed=parsed, fetch_plan=fetch_plan):
                        _write_fetch_plan_debug(
                            symbol=sym,
                            required_data_dir=required_data_dir,
                            report_dir=report_dir,
                            fetch_plan=fetch_plan,
                            merged_payload=_load_existing_payload(parsed=parsed, symbol=sym),
                        )
                        return
                except Exception:
                    pass
            elif min_dte is not None:
                try:
                    import pandas as pd

                    df0 = pd.read_csv(parsed, usecols=['dte'])
                    mx = pd.to_numeric(df0['dte'], errors='coerce').max()
                    if mx is not None and mx >= float(min_dte):
                        return
                except Exception:
                    # On read/parse failure, refetch to be safe.
                    pass
            else:
                return

    requests: list[RequiredDataFetchRequest]
    if fetch_plan is not None:
        requests = [
            build_fetch_request_from_spec(
                spec=spec,
                output_root=required_data_dir,
                chain_cache=True,
                chain_cache_force_refresh=False,
                opend_fetch_config=opend_fetch_config,
            )
            for spec in fetch_plan.merged_specs
        ]
    else:
        option_types = 'put,call' if (want_put and want_call) else ('put' if want_put else 'call')
        requests = [
            RequiredDataFetchRequest(
                symbol=sym,
                limit_expirations=int(limit_expirations),
                host=str(fetch_host),
                port=int(fetch_port),
                output_root=required_data_dir,
                option_types=option_types,
                max_strike=(float(max_strike) if ((max_strike is not None) and want_put) else None),
                min_dte=(int(min_dte) if min_dte is not None else None),
                max_dte=(int(max_dte) if max_dte is not None else None),
                chain_cache=True,
                **filter_opend_fetch_kwargs(opend_fetch_config),
            )
        ]

    try:
        if fetch_plan is None and len(requests) == 1:
            fetch_required_data_opend(
                base=base,
                request=requests[0],
            )
            merged_payload = _load_existing_payload(parsed=parsed, symbol=sym)
        else:
            payloads = [
                execute_required_data_opend(
                    base=base,
                    request=request,
                )
                for request in requests
            ]
            merged_payload = merge_required_data_payloads(symbol=sym, payloads=payloads)
            save_outputs(
                Path(base),
                str(sym),
                merged_payload,
                output_root=required_data_dir,
            )
        _write_fetch_plan_debug(
            symbol=sym,
            required_data_dir=required_data_dir,
            report_dir=report_dir,
            fetch_plan=fetch_plan,
            merged_payload=merged_payload,
        )
        if (not is_scheduled) and (state_dir is not None):
            pipeline_fetch_models.record_fetch_snapshot(
                state_dir=state_dir,
                symbol=sym,
                source=src,
                status='ok',
            )
    except BaseException as e:
        if (not is_scheduled) and (state_dir is not None):
            pipeline_fetch_models.record_fetch_snapshot(
                state_dir=state_dir,
                symbol=sym,
                source=src,
                status='error',
                reason=str(e),
            )
        raise


def _write_fetch_plan_debug(
    *,
    symbol: str,
    required_data_dir: Path,
    report_dir: Path | None,
    fetch_plan: RequiredDataFetchPlanBundle | None,
    merged_payload: dict[str, object],
) -> None:
    try:
        rows = merged_payload.get("rows") or []
        bounds_coverage: dict[str, dict[str, float | int | None]] = {}
        for option_type in ("put", "call"):
            strikes = [
                float(row.get("strike"))
                for row in rows
                if isinstance(row, dict)
                and str(row.get("option_type") or "").lower() == option_type
                and row.get("strike") is not None
            ]
            expirations = sorted({
                str(row.get("expiration"))
                for row in rows
                if isinstance(row, dict)
                and str(row.get("option_type") or "").lower() == option_type
                and row.get("expiration")
            })
            bounds_coverage[option_type] = {
                "row_count": len([row for row in rows if isinstance(row, dict) and str(row.get("option_type") or "").lower() == option_type]),
                "min_strike": (min(strikes) if strikes else None),
                "max_strike": (max(strikes) if strikes else None),
                "expirations": expirations,
            }
        root = report_dir or (required_data_dir / "reports")
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "symbol": symbol,
            "plan": (fetch_plan.to_debug_dict() if fetch_plan is not None else None),
            "coverage": bounds_coverage,
            "bounds_coverage": bounds_coverage,
        }
        path = root / f"{str(symbol).lower()}_required_data_fetch_plan.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _existing_required_data_covers_bounds_plan(*, parsed: Path, fetch_plan: RequiredDataFetchPlanBundle) -> bool:
    try:
        import pandas as pd

        df = pd.read_csv(parsed)
    except Exception:
        return False
    if df.empty:
        return False
    for side_plan in fetch_plan.side_plans:
        side_df = df[df.get("option_type").astype(str).str.lower() == side_plan.option_type].copy() if "option_type" in df.columns else pd.DataFrame()
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
            exp_df = side_df[side_df["expiration"].astype(str) == str(expiration)].copy() if "expiration" in side_df.columns else pd.DataFrame()
            if exp_df.empty:
                return False
            strikes = pd.to_numeric(exp_df.get("strike"), errors="coerce").dropna() if "strike" in exp_df.columns else pd.Series(dtype=float)
            if not _strikes_cover_bounds(
                strikes=strikes,
                base_min=base_min,
                base_max=base_max,
            ):
                return False
    return True


def _strikes_cover_bounds(*, strikes, base_min: float | None, base_max: float | None) -> bool:
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


def _load_existing_payload(*, parsed: Path, symbol: str) -> dict[str, object]:
    try:
        import pandas as pd

        df = pd.read_csv(parsed)
    except Exception:
        return {"symbol": symbol, "rows": []}
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
