"""Discover the research chain with existing OM owners, independently of strategy DTE."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from src.application.opend_fetch_config import OpenDFetchLimits
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_symbol_chain_fetching import list_option_expirations_with_gateway, select_symbol_expirations
from src.application.option_chain_fetching import OptionChainFetchRequest, QueryFetchCache, fetch_option_chains
from src.application.research import observation_series as series
from src.application.research import option_quote_capture as quote
from src.application.research import watchlist_scope as scope
from src.application.research.watchlist_series import _storage
from src.application.source_receipts import publish_source_receipt
from src.infrastructure.futu_dataframe import futu_dataframe_records
from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway, retry_futu_gateway_call

SCHEMA = "research.watchlist-discovery.v1"
BUDGET_SCHEMA = "research.watchlist-discovery-budget.v1"
CALENDAR_QUERY_SCHEMA = "research.watchlist-calendar-query.v1"


def validate_calendar_query(query, markets):
    series._keys(query, {"schema_version", "markets", "start", "end"}, "calendar query")
    selected = query["markets"]
    if (
        query["schema_version"] != CALENDAR_QUERY_SCHEMA
        or not isinstance(selected, list)
        or any(market not in {"HK", "US"} for market in selected)
        or selected != sorted(set(selected))
        or not set(markets) <= set(selected)
    ):
        raise ValueError("calendar query market/schema mismatch")
    start, end = date.fromisoformat(query["start"]), date.fromisoformat(query["end"])
    if start.isoformat() != query["start"] or end.isoformat() != query["end"] or start > end:
        raise ValueError("calendar query dates invalid")


def discover_watchlist(
    *,
    input_file: Path,
    expected_input_sha256: str,
    safe_window_file: Path,
    expected_safe_window_sha256: str,
    budget_file: Path,
    expected_budget_sha256: str,
    output_dir: Path,
    budget_roots: list[Path],
    runtime_root: Path,
    prune_roots: list[Path],
    host: str,
    port: int,
    confirm=False,
    clock=None,
    gateway_factory=None,
    calendar_query=None,
):
    """Preview is offline; a confirmed invocation uses one fresh, non-resumable archive."""
    raw, payload = series._read(input_file)
    if quote._sha256(raw) != expected_input_sha256 or payload.get("schema_version") != scope.SCAN_INPUT_SCHEMA:
        raise ValueError("external scan input SHA/schema mismatch")
    scope.build_watchlist_scope(input_file=input_file, expected_input_sha256=expected_input_sha256)
    started = series._now(clock)
    normalized, originals, _ = scope._scan_input(input_file.parent, payload, started)
    if series._utc(payload["as_of_utc"]) > started:
        raise ValueError("future scan input")
    safe_raw, intervals = series._safe_window(safe_window_file, expected_safe_window_sha256, expected_input_sha256)
    budget_raw, budget = series._read(budget_file)
    series._keys(budget, {"schema_version", "max_calls", "max_bytes", "min_free_bytes"}, "discovery budget")
    if quote._sha256(budget_raw) != expected_budget_sha256 or budget["schema_version"] != BUDGET_SCHEMA:
        raise ValueError("external discovery budget SHA/schema mismatch")
    if any(type(budget[key]) is not int or budget[key] <= 0 for key in ("max_calls", "max_bytes", "min_free_bytes")):
        raise ValueError("positive explicit discovery budgets required")
    output_dir = series._path(output_dir)
    series._outside_checkout(output_dir)
    series._disjoint(output_dir, [input_file.parent, runtime_root, *prune_roots])
    if not prune_roots or not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("explicit runtime/prune roots and endpoint required")
    storage = _storage(output_dir, budget_roots, budget)
    owners = sorted(item["underlier_code"] for item in normalized["symbols"])
    if calendar_query is not None:
        validate_calendar_query(calendar_query, {code.split(".")[0] for code in owners})
    preview = {
        "status": "preview",
        "provider_called": False,
        "underliers": owners,
        "min_dte": 0,
        "max_dte": 90,
        "max_calls": budget["max_calls"],
        "storage": storage,
        "capacity_note": "One expiration request per underlier plus one chain request per selected expiry; shared SDK handshake excluded.",
    }
    if calendar_query is not None:
        preview["calendar_query"] = calendar_query
    if not confirm:
        return preview

    def before_call():
        _storage(output_dir, budget_roots, budget)
        now = series._now(clock)
        if not any(start <= now < end for start, end in intervals):
            raise ValueError("discovery outside approved safe window")
        return now

    before_call()
    series._private_dir(output_dir, fresh=True)
    # Preserve the full frozen roster before connecting; never infer it from successful responses.
    for name, content in {
        "scan-input.json": raw,
        "safe-window.json": safe_raw,
        "budget.json": budget_raw,
        **originals,
    }.items():
        dest = series._file(output_dir, name)
        series._private_dir(dest.parent)
        quote._write_once(dest, content)
    for key in ("previous_tracking", "simulation_tracking"):
        ref = payload.get(key)
        if ref is None:
            continue
        _, content = scope._reference(input_file.parent, ref, started)
        dest = series._file(output_dir, ref["path"])
        series._private_dir(dest.parent)
        series._put(dest, content)
    if calendar_query is not None:
        quote._write_once(output_dir / "calendar-query.json", series._bytes(calendar_query))
    implementation = series._implementation()
    quote._write_once(output_dir / "implementation.json", series._bytes(implementation))
    run_id = series._read(series._file(output_dir, payload["watchlist"]["path"]))[1]["producer_run_id"]
    limits = OpenDFetchLimits.from_flat_kwargs()
    limiter = series._private_dir(output_dir / "limiter")
    results = {owner: {"status": "not_attempted", "receipt": None} for owner in owners}
    quote._write_once(
        output_dir / "discovery-plan.json",
        series._bytes(
            {
                **preview,
                "schema_version": SCHEMA,
                "input_sha256": expected_input_sha256,
                "safe_window_sha256": expected_safe_window_sha256,
                "budget_sha256": expected_budget_sha256,
                "started_at_utc": series._stamp(started),
            }
        ),
    )
    gateway, calls, active_calls = None, [], []
    error = None

    class RecordedGateway:
        def call(self, endpoint, arguments, callback, *, receipt=False):
            now = before_call()  # Runs after the owner's limiter has waited.
            if len(calls) >= budget["max_calls"]:
                raise ValueError("discovery request budget exhausted")
            record = {
                "endpoint": endpoint,
                "arguments": arguments,
                "reserved_at_utc": series._stamp(now),
                "requested_at_utc": None,
                "received_at_utc": None,
                "status": "reserved",
                "rows": None,
                "error_type": None,
            }
            folder = series._private_dir(output_dir / "calls" / f"{len(calls):06d}")
            quote._write_once(folder / "request.json", series._bytes(record))
            calls.append(record)
            active_calls.append(record)
            try:
                record["requested_at_utc"] = series._stamp(before_call())
                record["status"] = "failed"
                value = callback()
                record["rows"] = quote._json_safe(value if receipt else futu_dataframe_records(value))
                record["status"] = "returned"
                return value
            except BaseException as exc:
                record["error_type"] = type(exc).__name__
                raise
            finally:
                if record["requested_at_utc"] is not None:
                    record["received_at_utc"] = series._stamp(series._now(clock))
                quote._write_once(folder / "response.json", series._bytes(record))

        def get_option_expiration_dates(self, code):
            return self.call("option_expiration", {"code": code}, lambda: gateway.get_option_expiration_dates(code))

        def get_option_chain(self, **kwargs):
            return self.call("option_chain", kwargs, lambda: gateway.get_option_chain(**kwargs))

    try:
        before_call()
        gateway = (gateway_factory or build_ready_futu_quote_gateway)(host=host, port=port)
        if calendar_query is not None:
            calendar = {"markets": {}}
            for market in calendar_query["markets"]:
                arguments = {"market": market, "start": calendar_query["start"], "end": calendar_query["end"]}
                receipt = rate_limited_opend_call(
                    base_dir=limiter,
                    endpoint="trading_days",
                    window_sec=30,
                    max_calls=10,
                    max_wait_sec=30,
                    call=lambda: RecordedGateway().call(
                        "trading_days",
                        arguments,
                        lambda: gateway.get_trading_days_with_receipt(**arguments),
                        receipt=True,
                    ),
                )
                series._validate_calendar_receipt(
                    receipt, start=date.fromisoformat(arguments["start"]), end=date.fromisoformat(arguments["end"])
                )
                calendar["markets"][market] = {
                    "start": arguments["start"],
                    "end": arguments["end"],
                    "source_observed_at_utc": calls[-1]["received_at_utc"],
                    "receipt": quote._json_safe(receipt),
                }
            quote._write_once(output_dir / "calendar.json", series._bytes(calendar))
        for owner in owners:
            active_calls = []
            body = {
                "schema_version": SCHEMA,
                "underlier_code": owner,
                "min_dte": 0,
                "max_dte": 90,
                "available_expirations": [],
                "requested_expirations": [],
                "chain": {"rows": [], "meta": None},
                "provider_calls": active_calls,
                "status": "incomplete",
                "error_type": None,
            }
            try:
                now = before_call()
                if len(calls) >= budget["max_calls"]:
                    raise ValueError("discovery request budget exhausted")
                local_date = now.astimezone(
                    ZoneInfo("Asia/Hong_Kong" if owner.startswith("HK.") else "America/New_York")
                ).date()
                body["local_date"] = local_date.isoformat()
                available = list_option_expirations_with_gateway(
                    RecordedGateway(),
                    underlier_code=owner,
                    base_dir=limiter,
                    expiration_limit=limits.option_expiration,
                    no_retry=True,
                    use_cache=False,
                )
                selected = select_symbol_expirations(
                    expirations_all=available,
                    explicit_expirations_norm=[],
                    limit_expirations=0,
                    min_dte=0,
                    max_dte=90,
                    today=local_date,
                )
                body.update(available_expirations=available, requested_expirations=selected)
                if selected:  # An empty target list means unbounded scope to the chain owner.
                    chain = fetch_option_chains(
                        gateway=RecordedGateway(),
                        request=OptionChainFetchRequest(
                            symbol=owner,
                            underlier_code=owner,
                            expirations=selected,
                            host=host,
                            port=port,
                            option_types="put,call",
                            strike_windows=None,
                            base_dir=limiter,
                            asof_date=local_date.isoformat(),
                            freshness_policy="force_refresh",
                            chain_cache=False,
                            is_force_refresh=True,
                            no_retry=True,
                            query_cache=QueryFetchCache(),
                        ),
                        retry_call=retry_futu_gateway_call,
                    )
                    body["chain"] = {"rows": quote._json_safe(chain.rows), "meta": chain.to_meta()}
                    good = (
                        not chain.errors
                        and set(chain.expiration_statuses) == set(selected)
                        and all(status in {"fetched", "empty"} for status in chain.expiration_statuses.values())
                    )
                    body["status"] = "complete" if good else "incomplete"
                else:
                    body["status"] = "complete"
            except Exception as exc:
                body["error_type"] = type(exc).__name__
            finally:
                completed = series._stamp(series._now(clock))
                body["completed_at_utc"] = completed
                content = series._bytes(body)
                digest = quote._sha256(content)
                prefix = "source_receipts/research_discovery/" + digest
                series._private_dir(output_dir / prefix)
                receipt = publish_source_receipt(
                    producer_root=output_dir,
                    receipt_relpath=prefix + "/receipt.json",
                    payload_relpath=prefix + "/payload.json",
                    payload_bytes=content,
                    source_kind="research_supplemental",
                    producer_schema_version=SCHEMA,
                    producer_run_id=run_id,
                    broker="futu",
                    included_markets=[owner.split(".")[0]],
                    source_native_id="research-discovery:" + digest,
                    source_observed_at=completed,
                    completed_at=completed,
                    producer_policy_hash=quote._sha256(series._bytes({"schema": SCHEMA, "min_dte": 0, "max_dte": 90})),
                    private_files=True,
                )
                results[owner] = {
                    "status": body["status"],
                    "receipt": {
                        "path": prefix + "/receipt.json",
                        "sha256": quote._sha256((output_dir / (prefix + "/receipt.json")).read_bytes()),
                        "pointer": "",
                        "observed_at_utc": receipt["completed_at"],
                    },
                }
    except Exception as exc:
        error = type(exc).__name__
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception as exc:
                error = error or type(exc).__name__
        finished = series._stamp(series._now(clock))
        next_input = {
            **payload,
            "schema_version": scope.DISCOVERY_INPUT_SCHEMA,
            "as_of_utc": finished,
            "discoveries": [item["receipt"] for item in results.values() if item["receipt"] is not None],
        }
        quote._write_once(output_dir / "scope-input.json", series._bytes(next_input))
        manifest = {
            "schema_version": SCHEMA,
            "input_sha256": expected_input_sha256,
            "status": "complete"
            if error is None and all(x["status"] == "complete" for x in results.values())
            else "incomplete",
            "provider_called": any(call["status"] != "reserved" for call in calls),
            "provider_call_count": sum(call["status"] != "reserved" for call in calls),
            "request_reservations": len(calls),
            "error_type": error,
            "completed_at_utc": finished,
            "underliers": results,
            "files": series._inventory(output_dir, output_dir),
            "scope_input_sha256": quote._sha256((output_dir / "scope-input.json").read_bytes()),
            "note": "Discovery coverage only; not quote freshness, authoritative last-trade or backtest qualification.",
        }
        quote._write_once(output_dir / "discovery-manifest.json", series._bytes(manifest))
    return manifest
