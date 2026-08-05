"""Required-data fetch step.

Extracted from pipeline_symbol.py (Stage 3): keep per-symbol orchestration smaller.

Goal: minimal/no behavior change.
"""

from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from src.application import pipeline_fetch_models
from src.application.opend_symbol_outputs import (
    finalize_required_data_quote_candidate,
    finalize_unplanned_required_data_candidate,
    resolve_exact_fresh_required_data_quote_receipt,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
)
from src.application.required_data_coverage import (
    build_required_data_coverage,
    load_required_data_payload_from_csv,
    required_data_csv_covers_fetch_plan,
)
from src.application.required_data_fetching import (
    RequiredDataFetchRequest,
    bind_merged_payload_evidence as _bind_merged_payload_evidence,
    bind_required_data_child_request_evidence,
    build_fetch_request_from_spec,
    execute_required_data_opend,
    merge_required_data_payloads,
)
from src.application.opend_fetch_config import filter_opend_fetch_kwargs
from src.application.required_data_planning import RequiredDataFetchPlanBundle
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
)
from src.application.required_data_snapshot import resolve_frozen_required_data


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
    position_advice_producer_run_id: str | None = None,
    required_data_snapshot_manifest: Path | None = None,
    required_data_snapshot_run_id: str | None = None,
) -> dict[str, Any] | None:
    sym = symbol
    parsed = (required_data_dir / 'parsed' / f"{sym}_required_data.csv").resolve()

    if not (want_put or want_call):
        return None
    if required_data_snapshot_manifest is not None:
        return resolve_frozen_required_data(
            manifest_path=required_data_snapshot_manifest,
            expected_run_id=str(required_data_snapshot_run_id or ""),
            symbol=sym,
            required_data_root=required_data_dir,
        )

    src = 'opend'
    producer_run_id = str(position_advice_producer_run_id or "").strip()
    if producer_run_id and fetch_plan is None:
        raise PositionAdviceSourceError(
            "required-data producer run id requires an exact fetch plan"
        )
    fetch_plan_payload = (
        fetch_plan.to_debug_dict()
        if fetch_plan is not None
        else None
    )
    expected_fetch_contract = (
        build_required_data_expected_fetch_contract(
            symbol=sym,
            fetch_plan=fetch_plan_payload,
            source=str(fetch_source or "opend"),
            host=str(fetch_host),
            port=int(fetch_port),
        )
        if fetch_plan_payload is not None
        else None
    )
    fetch_policy = _pipeline_fallback_fetch_policy(
        fetch_source=fetch_source,
        fetch_host=fetch_host,
        fetch_port=fetch_port,
        limit_expirations=limit_expirations,
        max_strike=max_strike,
        min_dte=min_dte,
        max_dte=max_dte,
        opend_fetch_config=opend_fetch_config,
    )

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
                    if producer_run_id and expected_fetch_contract is not None:
                        finalized = finalize_required_data_quote_candidate(
                            base=base,
                            producer_root=required_data_dir,
                            producer_run_id=producer_run_id,
                            symbol=sym,
                            fetch_policy=fetch_policy,
                            expected_fetch_contract=expected_fetch_contract,
                            mode="cached",
                        )
                        _write_fetch_plan_debug(
                            symbol=sym,
                            required_data_dir=required_data_dir,
                            report_dir=report_dir,
                            fetch_plan=fetch_plan,
                            merged_payload=load_required_data_payload_from_csv(parsed=parsed, symbol=sym),
                        )
                        evidence = finalized.get("evidence")
                        return (
                            dict(evidence)
                            if isinstance(evidence, dict)
                            else None
                        )
                    if required_data_csv_covers_fetch_plan(
                        parsed=parsed, fetch_plan=fetch_plan
                    ):
                        _write_fetch_plan_debug(
                            symbol=sym,
                            required_data_dir=required_data_dir,
                            report_dir=report_dir,
                            fetch_plan=fetch_plan,
                            merged_payload=load_required_data_payload_from_csv(
                                parsed=parsed, symbol=sym
                            ),
                        )
                        return None
                except Exception:
                    should_refetch = True
            elif min_dte is not None:
                try:
                    import pandas as pd

                    df0 = pd.read_csv(parsed, usecols=['dte'])
                    mx = pd.to_numeric(df0['dte'], errors='coerce').max()
                    if mx is not None and mx >= float(min_dte):
                        return None
                except Exception:
                    # On read/parse failure, refetch to be safe.
                    pass
            else:
                return None

    requests: list[RequiredDataFetchRequest]
    if fetch_plan is not None:
        requests = [
            build_fetch_request_from_spec(
                spec=spec,
                output_root=required_data_dir,
                chain_cache=True,
                chain_cache_force_refresh=False,
                opend_fetch_config=opend_fetch_config,
                spot_override=fetch_plan.spot_reference,
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
            merged_payload = execute_required_data_opend(
                base=base,
                request=requests[0],
            )
            payloads = [merged_payload]
        else:
            payloads = []
            for request in requests:
                payload = execute_required_data_opend(
                    base=base,
                    request=request,
                )
                if _payload_fetch_status(payload) != "ok":
                    raise RuntimeError(
                        _payload_fetch_error_message(
                            symbol=sym,
                            payload=payload,
                        )
                    )
                payloads.append(payload)
            if fetch_plan is not None and len(payloads) > 1:
                if len(payloads) != len(fetch_plan.merged_specs):
                    raise RuntimeError(
                        "required-data provider child count does not match fetch plan"
                    )
                payloads = [
                    bind_required_data_child_request_evidence(
                        payload=payload,
                        planned_request=spec.to_debug_dict(),
                        request_index=index,
                    )
                    for index, (spec, payload) in enumerate(
                        zip(fetch_plan.merged_specs, payloads, strict=True)
                    )
                ]
            if not payloads and fetch_plan is not None:
                merged_payload = _success_empty_payload_from_plan(
                    symbol=sym,
                    fetch_plan=fetch_plan,
                    fetch_source=fetch_source,
                    fetch_host=fetch_host,
                    fetch_port=fetch_port,
                )
            elif len(payloads) == 1:
                merged_payload = payloads[0]
            else:
                merged_payload = merge_required_data_payloads(
                    symbol=sym,
                    payloads=payloads,
                )
                _bind_merged_payload_evidence(
                    merged_payload=merged_payload,
                    payloads=payloads,
                )
        _write_fetch_plan_debug(
            symbol=sym,
            required_data_dir=required_data_dir,
            report_dir=report_dir,
            fetch_plan=fetch_plan,
            merged_payload=merged_payload,
        )
        try:
            if fetch_plan is None:
                finalized = finalize_unplanned_required_data_candidate(
                    base=base,
                    producer_root=required_data_dir,
                    symbol=sym,
                    payload=merged_payload,
                    source=str(fetch_source or "opend"),
                    host=str(fetch_host),
                    port=int(fetch_port),
                    require_realized_volatility=bool(
                        requests[0].include_realized_volatility
                    ),
                )
            else:
                if expected_fetch_contract is None:
                    raise RuntimeError(
                        "required-data expected fetch contract is missing"
                    )
                finalized = finalize_required_data_quote_candidate(
                    base=base,
                    producer_root=required_data_dir,
                    producer_run_id=producer_run_id,
                    symbol=sym,
                    expected_fetch_contract=expected_fetch_contract,
                    fetch_policy=fetch_policy,
                    mode=(
                        "success_empty"
                        if _payload_source_outcome(merged_payload)
                        == "success_empty"
                        else "fresh"
                    ),
                    payload=merged_payload,
                )
        except Exception as exc:
            if _payload_fetch_status(merged_payload) != "ok":
                raise RuntimeError(
                    _payload_fetch_error_message(
                        symbol=sym,
                        payload=merged_payload,
                    )
                ) from exc
            raise
        if (not is_scheduled) and (state_dir is not None):
            pipeline_fetch_models.record_fetch_snapshot(
                state_dir=state_dir,
                symbol=sym,
                source=src,
                status='ok',
            )
        evidence = finalized.get("evidence")
        if isinstance(evidence, dict):
            return dict(evidence)
        if not producer_run_id:
            return None
        return resolve_exact_fresh_required_data_quote_receipt(
            producer_root=required_data_dir,
            symbol=sym,
            expected_producer_run_id=producer_run_id,
            expected_fetch_contract=expected_fetch_contract,
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


def _payload_fetch_status(payload: dict[str, object] | object) -> str:
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("status") or "").strip().lower()


def _payload_source_outcome(payload: dict[str, object] | object) -> str:
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("source_outcome") or "").strip().lower()


def _payload_fetch_error_message(*, symbol: str, payload: dict[str, object] | object) -> str:
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    message = str(meta.get("error") or meta.get("message") or meta.get("error_code") or "").strip()
    return message or f"{symbol} required_data fetch failed"


def _pipeline_fallback_fetch_policy(
    *,
    fetch_source: str,
    fetch_host: str,
    fetch_port: int,
    limit_expirations: int,
    max_strike: float | None,
    min_dte: int | None,
    max_dte: int | None,
    opend_fetch_config: dict[str, float | int] | None,
) -> dict[str, Any]:
    return {
        "source": str(fetch_source or "opend"),
        "host": str(fetch_host),
        "port": int(fetch_port),
        "limit_expirations": int(limit_expirations),
        "max_strike": max_strike,
        "min_dte": min_dte,
        "max_dte": max_dte,
        "opend_fetch": dict(opend_fetch_config or {}),
        "execution_mode": "pipeline_fallback",
    }


def _success_empty_payload_from_plan(
    *,
    symbol: str,
    fetch_plan: RequiredDataFetchPlanBundle,
    fetch_source: str,
    fetch_host: str,
    fetch_port: int,
) -> dict[str, object]:
    discovery = fetch_plan.expiration_discovery
    if (
        fetch_plan.projection_outcome != "success_empty"
        or discovery is None
        or discovery.outcome != "success_empty"
        or discovery.reason_code not in {"no_expirations", "no_contract_rows"}
    ):
        raise RuntimeError(
            f"{symbol} required_data has no executable requests or valid "
            "success-empty discovery evidence"
        )
    identity = dict(discovery.request_identity or {})
    return {
        "symbol": symbol,
        "underlier_code": identity.get("underlier"),
        "spot": fetch_plan.spot_reference,
        "expiration_count": 0,
        "expirations": [],
        "rows": [],
        "meta": {
            "source": identity.get("source") or fetch_source,
            "host": identity.get("host") or fetch_host,
            "port": identity.get("port") or int(fetch_port),
            "status": "ok",
            "source_outcome": "success_empty",
            "reason_code": discovery.reason_code,
            "snapshot_complete": True,
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "realized_volatility": {
                "status": "not_applicable_no_contracts",
                "reason": "not_applicable_no_contracts",
            },
            "source_observed_at": discovery.observed_at_utc,
            "completed_at_utc": discovery.completed_at_utc,
            "trading_date": identity.get("trading_date"),
        },
    }


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
        bounds_coverage = build_required_data_coverage(rows if isinstance(rows, list) else [])
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
