"""Explicit offline scope and underlier-history acquisition; no runtime config/ledger."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.option_position_identity import normalize_account, normalize_broker
from domain.domain.performance.period import PeriodRequest, normalize_performance_period
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_market_snapshot_fetching import trading_calendar_dates
from src.application.opend_utils import normalize_underlier
from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway

INPUT_SCHEMA = "research.canonical-events-snapshot.v1"
SCHEMA = "research.underlier-history-backfill.v1"
MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}
REMAINING = [
    "historical_option_bid_ask_not_backfillable_by_snapshot",
    "scope_is_corrected_offline_snapshot_not_historical_as_known",
    "future_dates_and_future_account_activity_not_included",
]


def _json_default(value):
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    raise TypeError("unsupported provider value")


def _bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2, default=_json_default) + "\n"
    ).encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _market_dates(window, *, market, as_of_ms):
    tz = MARKET_TIMEZONES[market]
    start = datetime.fromtimestamp(window.effective_start_at_ms / 1000, tz).date()
    current = datetime.fromtimestamp(as_of_ms / 1000, tz).date() - timedelta(days=1)
    end = min(date.fromisoformat(window.requested_end_date), current)
    return start, end


def _load_events_snapshot(events_path):
    raw = Path(events_path).read_bytes()
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "captured_at_ms", "events"}
        or payload.get("schema_version") != INPUT_SCHEMA
        or type(payload.get("captured_at_ms")) is not int
        or payload["captured_at_ms"] <= 0
        or not isinstance(payload.get("events"), list)
    ):
        raise ValueError("canonical events snapshot envelope required")
    events = [TradeEvent.from_dict(row) for row in payload["events"]]
    if any(type(event.event_time_ms) is not int or event.event_time_ms > payload["captured_at_ms"] for event in events):
        raise ValueError("canonical events snapshot contains events after capture")
    return raw, payload["captured_at_ms"], events


def build_underlier_history_plan(events_path, *, account, broker, month, as_of_ms):
    if not account or not broker or type(as_of_ms) is not int or as_of_ms <= 0:
        raise ValueError("explicit account, broker and as_of_ms required")
    account, broker = normalize_account(account), normalize_broker(broker)
    window = normalize_performance_period(PeriodRequest(period="month", month=month), report_now_ms=as_of_ms)
    raw, captured_at_ms, events = _load_events_snapshot(events_path)
    if as_of_ms > captured_at_ms:
        raise ValueError("as_of_ms cannot exceed canonical events snapshot capture time")
    events = [
        e
        for e in events
        if e.contract_key.account == account and e.contract_key.broker == broker and e.event_time_ms <= as_of_ms
    ]
    projection = project_trade_events(events)
    if projection.has_errors:
        raise ValueError("canonical event projection has errors")
    keys = {e.contract_key for e in projection.effective_open_events if window.contains(e.event_time_ms)}
    keys.update(e.contract_key for e in projection.effective_cash_events if window.contains(e.event_time_ms))
    keys.update(lot.contract_key for lot in projection.lots if window.contains(lot.opened_at_ms))
    keys.update(
        lot.contract_key
        for lot in projection.lots
        if lot.opened_at_ms < window.effective_start_at_ms
        and (lot.contracts_open > 0 or (lot.shares_open is not None and lot.shares_open > 0))
    )
    events_by_id = {event.event_id: event for event in events}
    for lot in projection.lots:
        if lot.asset_type != "stock" or lot.shares_open != 0:
            continue
        for event_id in lot.close_event_ids:
            close_event = events_by_id.get(event_id)
            if close_event is not None and window.contains(close_event.event_time_ms):
                keys.add(lot.contract_key)
                break
    if not keys:
        raise ValueError("no effective activity or carry-in holdings in requested scope")
    underliers = {normalize_underlier(key.underlying_symbol).code for key in keys}
    requests = []
    for code in sorted(underliers):
        market = code.split(".")[0]
        # Only completed local dates; this does not claim settlement/final-price evidence.
        start, end = _market_dates(window, market=market, as_of_ms=as_of_ms)
        if end < start:
            raise ValueError("no completed local dates in scope")
        requests.append({"code": code, "market": market, "start": start.isoformat(), "end": end.isoformat()})
    plan = {
        "schema_version": SCHEMA,
        "account": account,
        "broker": broker,
        "month": month,
        "as_of_ms": as_of_ms,
        "captured_at_ms": captured_at_ms,
        "input_sha256": _sha(raw),
        "requests": requests,
        "contracts": sorted([k.to_dict() for k in keys], key=lambda k: json.dumps(k, sort_keys=True)),
        "remaining": list(REMAINING),
    }
    return validate_underlier_history_plan(plan)


def validate_underlier_history_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA:
        raise ValueError("unsupported underlier-history backfill plan")
    requests = plan.get("requests")
    if not isinstance(requests, list) or not requests or len(requests) > 100:
        raise ValueError("explicit bounded requests required")
    if (
        not plan.get("account")
        or not plan.get("broker")
        or plan.get("remaining") != REMAINING
        or not isinstance(plan.get("contracts"), list)
        or not plan["contracts"]
        or not isinstance(plan.get("input_sha256"), str)
        or len(plan["input_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in plan["input_sha256"])
    ):
        raise ValueError("scope identity/evidence invalid")
    keys = [ContractKey.from_values(**key) for key in plan["contracts"]]
    if any(k.account != plan["account"] or k.broker != plan["broker"] for k in keys):
        raise ValueError("scope account/broker mismatch")
    expected_codes = {normalize_underlier(k.underlying_symbol).code for k in keys}
    codes = set()
    as_of = plan.get("as_of_ms")
    if type(as_of) is not int or as_of <= 0:
        raise ValueError("explicit as_of_ms required")
    captured_at = plan.get("captured_at_ms")
    if type(captured_at) is not int or captured_at <= 0 or as_of > captured_at:
        raise ValueError("as_of_ms must be bound to the canonical events snapshot")
    window = normalize_performance_period(PeriodRequest(period="month", month=plan.get("month")), report_now_ms=as_of)
    for request in requests:
        if not isinstance(request, dict) or set(request) != {"code", "market", "start", "end"}:
            raise ValueError("request fields invalid")
        code = request["code"]
        market = request["market"]
        if (
            market not in {"US", "HK"}
            or normalize_underlier(code).code != code
            or not code.startswith(market + ".")
            or code in codes
        ):
            raise ValueError("duplicate or invalid underlier identity")
        codes.add(code)
        start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
        expected_start, expected_end = _market_dates(window, market=market, as_of_ms=as_of)
        if (
            start != expected_start
            or end < start
            or end > date.fromisoformat(window.requested_end_date)
            or end > expected_end
        ):
            raise ValueError("date range outside completed scope")
    if codes != expected_codes:
        raise ValueError("requests differ from contract scope")
    return plan


def _rows(value):
    if hasattr(value, "to_json"):
        value = json.loads(value.to_json(orient="records", date_format="iso", double_precision=15))
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("provider rows invalid")
    return value


def corporate_action_response(gateway, *, code, call):
    """Preserve the full provider response; get_rehab offers no coverage/as-known proof."""
    return {"rows": _rows(call("rehab", lambda: gateway.get_rehab(code=code)))}


def validate_corporate_action_response(response):
    for row in response["rows"]:
        date.fromisoformat(row["ex_div_date"])


def _write_once(path, content):
    temporary = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            temporary.chmod(0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return _sha(content)


def _manifest(*, plan, plan_hash, host, port, execution_error_type, files):
    return {
        "schema_version": SCHEMA,
        "status": "incomplete",
        "plan_sha256": plan_hash,
        "execution_error_type": execution_error_type,
        "collector_sha256": _sha(Path(__file__).read_bytes()),
        "provider": "futu_opend",
        "endpoint": {"host": host, "port": port},
        "provider_quota": "shared_with_other_opend_consumers",
        "files": files,
        "remaining": plan["remaining"],
    }


def _write_terminal_manifest(*, out, plan, plan_hash, host, port, execution_error_type, files=()):
    manifest = _manifest(
        plan=plan,
        plan_hash=plan_hash,
        host=host,
        port=port,
        execution_error_type=execution_error_type,
        files=list(files),
    )
    path = out / "manifest.json"
    try:
        _write_once(path, _bytes(manifest))
    except FileExistsError:
        return


def collect_underlier_history(
    plan,
    *,
    output_dir,
    host,
    port,
    confirm=False,
    gateway_factory=None,
    events_path=None,
):
    validate_underlier_history_plan(plan)
    if not confirm:
        return {"status": "preview", "plan": plan, "provider_called": False}
    if events_path is None:
        raise ValueError("confirmed collection requires the canonical events source")
    rebuilt = build_underlier_history_plan(
        events_path,
        account=plan["account"],
        broker=plan["broker"],
        month=plan["month"],
        as_of_ms=plan["as_of_ms"],
    )
    if rebuilt != plan:
        raise ValueError("plan does not match the canonical events source")
    if not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("explicit quote endpoint required")
    for request in plan["requests"]:
        tz = MARKET_TIMEZONES[request["market"]]
        if date.fromisoformat(request["end"]) >= datetime.now(tz).date():
            raise ValueError("cannot collect incomplete or future local days")
    out = Path(output_dir)
    limiter_root = out / "limiter"
    plan_hash = _sha(_bytes(plan))
    results = []
    gateway = None
    execution_error = None
    cancelled = None
    try:
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        limiter_root.mkdir(mode=0o700)

        def write(name, value):
            raw = _bytes(value)
            return _write_once(out / name, raw)

        plan_hash = write("plan.json", plan)
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        if out.is_dir():
            files = (
                [{"file": "plan.json", "sha256": plan_hash, "status": "received"}]
                if (out / "plan.json").is_file()
                else []
            )
            _write_terminal_manifest(
                out=out,
                plan=plan,
                plan_hash=plan_hash,
                host=host,
                port=port,
                execution_error_type=type(exc).__name__,
                files=files,
            )
        raise
    try:
        gateway = (gateway_factory or build_ready_futu_quote_gateway)(host=host, port=port)

        def call(endpoint, fn):
            return rate_limited_opend_call(
                base_dir=limiter_root, endpoint=endpoint, max_wait_sec=30, window_sec=30, max_calls=10, call=fn
            )

        for request in plan["requests"]:
            code, market = request["code"], request["market"]
            start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
            calendar = None
            for kind in ("calendar", "unadjusted_daily", "corporate_actions"):
                received = []
                started = datetime.now(timezone.utc).isoformat()
                status, error = "received", None
                pending_cancel = None
                dates = set()
                try:
                    if kind == "calendar":
                        receipt = call(
                            "trading_days",
                            lambda: gateway.get_trading_days_with_receipt(
                                market=market, start=request["start"], end=request["end"]
                            ),
                        )
                        receipt = {**receipt, "rows": _rows(receipt["rows"])}
                        received.append(receipt)
                        calendar = set(trading_calendar_dates(receipt, start=start, end=end))
                    elif kind == "unadjusted_daily":
                        token, seen = None, set()
                        for _ in range(20):
                            page = call(
                                "history_kline",
                                lambda: gateway.request_history_kline(
                                    code=code,
                                    start=request["start"],
                                    end=request["end"],
                                    ktype="K_DAY",
                                    autype="NONE",
                                    max_count=300,
                                    page_req_key=token,
                                ),
                            )
                            rows = _rows(page["data"])
                            received.append(
                                {"request_page_key": token, "rows": rows, "next_page_key": page["page_req_key"]}
                            )
                            for row in rows:
                                day = str(row.get("time_key", ""))[:10]
                                close = row.get("close")
                                if (
                                    row.get("code") != code
                                    or not request["start"] <= day <= request["end"]
                                    or day in dates
                                    or type(close) not in (int, float)
                                    or not math.isfinite(close)
                                    or close <= 0
                                ):
                                    raise ValueError("daily row identity/date/close invalid")
                                date.fromisoformat(day)
                                dates.add(day)
                            token = page["page_req_key"]
                            if token in (None, ""):
                                break
                            if not isinstance(token, (str, bytes)) or token in seen:
                                raise ValueError("history pagination did not advance")
                            seen.add(token)
                        else:
                            raise ValueError("history pagination exceeded 20 pages")
                        if calendar is None or dates != calendar:
                            status = "incomplete_calendar_or_daily_coverage"
                    else:
                        # Provider has no date filter: preserve the full response, never infer payment/as-known dates.
                        received.append(corporate_action_response(gateway, code=code, call=call))
                        validate_corporate_action_response(received[-1])
                        status = "received_unverified_coverage"
                except (KeyboardInterrupt, asyncio.CancelledError) as exc:
                    status, error = "cancelled", type(exc).__name__
                    pending_cancel = exc
                except Exception as exc:
                    status, error = "failed", type(exc).__name__
                name = f"{code}.{kind}.json"
                digest = write(
                    name,
                    {
                        "request": request,
                        "kind": kind,
                        "autype": "NONE" if kind == "unadjusted_daily" else None,
                        "started_at_utc": started,
                        "received_at_utc": datetime.now(timezone.utc).isoformat(),
                        "status": status,
                        "error_type": error,
                        "responses": received,
                    },
                )
                results.append({"file": name, "sha256": digest, "status": status})
                if pending_cancel is not None:
                    raise pending_cancel
    except Exception as exc:
        execution_error = type(exc).__name__
    except BaseException as exc:
        cancelled = exc
        execution_error = type(exc).__name__
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception as exc:
                execution_error = execution_error or type(exc).__name__
            except BaseException as exc:
                cancelled = cancelled or exc
                execution_error = execution_error or type(exc).__name__
    result = _manifest(
        plan=plan,
        plan_hash=plan_hash,
        host=host,
        port=port,
        execution_error_type=execution_error,
        files=results,
    )
    try:
        write("manifest.json", result)
    except BaseException as exc:
        result["execution_error_type"] = result["execution_error_type"] or type(exc).__name__
        try:
            _write_terminal_manifest(
                out=out,
                plan=plan,
                plan_hash=plan_hash,
                host=host,
                port=port,
                execution_error_type=result["execution_error_type"],
                files=results,
            )
        except BaseException:
            pass
        cancelled = cancelled or exc
    if cancelled is not None:
        raise cancelled
    return result
