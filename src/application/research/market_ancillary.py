"""Acquire explicit research-only FX and corporate-action originals before valuation."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from time import monotonic

from src.application.current_fx_run import encode_run_fx_snapshot
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.research import observation_series as series
from src.application.research import option_quote_capture as quote
from src.application.research import watchlist_series as group
from src.application.research.underlier_history import corporate_action_response, validate_corporate_action_response
from src.infrastructure import exchange_rates as fx
from src.infrastructure.futu_gateway import build_futu_gateway

POLICY_SCHEMA = "research.market-ancillary-capture.v1"


def with_underliers(plan_dir, slot):
    """Use validated shard rosters, including stock-only tracking, without widening scope."""
    codes = set()
    for batch_id in slot["batches"]:
        child = series._read(plan_dir / "batches" / batch_id / "series-plan.json")[1]
        for observation in child["observations"]:
            if observation["expected_at_utc"] == slot["expected_at_utc"]:
                codes.update(observation["underlier_codes"])
    return {**slot, "underlier_codes": sorted(codes)}


def validate_policy(policy):
    series._keys(
        policy, {"schema_version", "lead_seconds", "max_calls", "max_bytes", "min_free_bytes"}, "ancillary capture"
    )
    if policy["schema_version"] != POLICY_SCHEMA or any(
        type(policy[key]) is not int or policy[key] <= 0
        for key in ("lead_seconds", "max_calls", "max_bytes", "min_free_bytes")
    ):
        raise ValueError("explicit positive ancillary policy required")


def capture_market_ancillary(
    *, plan_sha256, slot, policy, output_dir, budget_roots, host, port, clock=None, gateway_factory=None
):
    """Called only by the daily owner after isolation/window validation; never retries."""
    validate_policy(policy)
    out = series._path(output_dir)
    end = series._utc(slot["expected_at_utc"])
    start = end - timedelta(seconds=policy["lead_seconds"])
    codes = sorted(set(slot["underlier_codes"]))
    request = {
        "schema_version": POLICY_SCHEMA,
        "watchlist_series_sha256": plan_sha256,
        "underlier_codes": codes,
        "start_utc": series._stamp(start),
        "end_utc": series._stamp(end),
        "policy": policy,
    }
    if out.exists():
        if not (out / "request.json").exists():
            return {"status": "abandoned", "manifest_sha256": None}
        if series._read(out / "request.json")[1] != request:
            raise ValueError("ancillary capture request changed")
        if not (out / "manifest.json").exists():
            return {"status": "abandoned", "manifest_sha256": None}
        digest = quote._sha256(series._read(out / "manifest.json")[0])
        group._ancillary_originals(out / "manifest.json", digest)
        return {"status": "captured", "manifest_sha256": digest}
    now = series._now(clock)
    if not start <= now < end:
        return {"status": "pending" if now < start else "missed", "manifest_sha256": None}
    # Two existing HTTP sources, one quote preflight, one rehab request per underlier.
    if policy["max_calls"] < 3 + len(codes):
        raise ValueError("ancillary request budget cannot cover the frozen scope")
    group._storage(out, budget_roots, policy)
    series._private_dir(out, fresh=True)
    series._put(out / "request.json", series._bytes(request))
    calls, problems = [], []

    def invoke(endpoint, callback):
        began = series._now(clock)
        if not start <= began < end:
            raise ValueError("ancillary request outside approved window")
        group._storage(out, budget_roots, policy)
        if len(calls) >= policy["max_calls"]:
            raise ValueError("ancillary request budget exhausted")
        call = {
            "endpoint": endpoint,
            "requested_at_utc": series._stamp(began),
            "received_at_utc": None,
            "status": "started",
        }
        calls.append(call)
        reservations = series._private_dir(out / "limiter/reservations")
        series._put(reservations / f"{len(calls):04d}.json", series._bytes(call))
        try:
            result = callback()
            call["status"] = "received"
            return result
        except BaseException as exc:
            call["status"] = type(exc).__name__
            raise
        finally:
            received = series._now(clock)
            call["received_at_utc"] = series._stamp(received)
            if received < began or received >= end:
                call["status"] = "received_outside_window"
                problems.append("provider_response_outside_window")

    http_count = 0

    def http_get(url, **kwargs):
        nonlocal http_count
        index = http_count
        http_count += 1
        value = {"url": url, "response_text": None, "error_type": None}
        try:
            # This is the original owner's decoded transport text, not claimed wire bytes.
            value["response_text"] = invoke("fx_http", lambda: fx._http_get(url, **kwargs))
            return value["response_text"]
        except Exception as exc:
            value["error_type"] = type(exc).__name__
            raise
        finally:
            series._put(out / f"fx-provider-{index}.json", series._bytes(value))

    gateway = None
    try:
        try:
            remaining = (end - series._now(clock)).total_seconds()
            snapshot = fx.current_exchange_rate_snapshot(
                cache_path=out / "unused-cache.json",
                write_cache=False,
                deadline=monotonic() + remaining,
                request_get=http_get,
            )
            series._put(out / "current_exchange_rates.v1.json", encode_run_fx_snapshot(snapshot, run_id=out.name))
        except Exception as exc:
            problems.append("fx:" + type(exc).__name__)
        if codes:
            if not start <= series._now(clock) < end:
                raise ValueError("ancillary gateway creation outside approved window")
            group._storage(out, budget_roots, policy)
            gateway = (gateway_factory or build_futu_gateway)(host=host, port=port)

            def limited(endpoint, callback):
                return rate_limited_opend_call(
                    base_dir=out / "limiter",
                    endpoint=endpoint,
                    max_wait_sec=30,
                    window_sec=30,
                    max_calls=10,
                    call=lambda: invoke(endpoint, callback),
                )

            try:
                limited("quote_preflight", lambda: gateway.ensure_quote_ready())
            except Exception as exc:
                problems.append("quote_preflight:" + type(exc).__name__)
            else:
                for code in codes:
                    began = series._stamp(series._now(clock))
                    value = {
                        "kind": "corporate_actions",
                        "request": {"code": code},
                        "started_at_utc": began,
                        "status": "received_unverified_coverage",
                        "error_type": None,
                        "responses": [],
                    }
                    try:
                        value["responses"].append(corporate_action_response(gateway, code=code, call=limited))
                        validate_corporate_action_response(value["responses"][-1])
                    except Exception as exc:
                        value.update(status="failed", error_type=type(exc).__name__)
                        problems.append("corporate_actions:" + code)
                    value["received_at_utc"] = series._stamp(series._now(clock))
                    series._put(out / (code + ".corporate_actions.json"), series._bytes(value))
    except Exception as exc:
        problems.append("gateway:" + type(exc).__name__)
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception as exc:
                problems.append("gateway_close:" + type(exc).__name__)
    series._put(
        out / "capture.json",
        series._bytes(
            {
                "schema_version": POLICY_SCHEMA,
                "provider_calls": calls,
                "problems": sorted(set(problems)),
                "qualification": "unverified",
                "ended_at_utc": series._stamp(series._now(clock)),
            }
        ),
    )
    records = [series._record(out, p) for p in sorted(out.rglob("*.json")) if "limiter" not in p.relative_to(out).parts]
    manifest = {"schema_version": group.ANCILLARY_SCHEMA, "files": records}
    digest = quote._write_once(out / "manifest.json", series._bytes(manifest))
    group._ancillary_originals(out / "manifest.json", digest)
    return {"status": "captured", "manifest_sha256": digest}
