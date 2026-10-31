"""Finite offline research observations; no account or production state access."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from domain.domain.option_lifecycle import INSTRUMENT_POLICY_REGISTRY, SUPPORTED_LAST_TRADE_SOURCES
from domain.domain.symbol_identity import OPTION_CODE_RE
from src.application.opening_quote_evidence import _parse_opend_time
from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_market_snapshot_fetching import trading_calendar_dates
from src.application.payload_helpers import canonical_json_bytes_lines
from src.application.research import option_quote_capture as quote
from src.application.source_receipts import publish_source_receipt, validate_source_receipt
from src.infrastructure.futu_dataframe import futu_dataframe_records
from src.infrastructure.futu_gateway import build_futu_gateway

PLAN_SCHEMA = "research.observation-series-plan.v1"
EXPLICIT_PLAN_SCHEMA = "research.observation-series-plan.v2"
OBSERVATION_ONLY_PLAN_SCHEMA = "research.observation-series-plan.v3"
UNDERLIER_PLAN_SCHEMA = "research.observation-series-plan.v4"
ATTEMPT_SCHEMA = "research.observation-series-attempt.v1"
EXPORT_SCHEMA = "research.observation-series-export.v1"
REQUIRED_DIMENSIONS = ["option_quote", "option_research", "underlier_quote", "fx", "calendar", "corporate_actions"]
SERIES_POLICY = "research.observation-series.v1|60s|close-minus-5m|bidask-time-unknown"
IMPLEMENTATION_SOURCES = tuple(
    sorted(
        set(quote.IMPLEMENTATION_SOURCES)
        | {
            "src/application/research/observation_series.py",
            "src/application/research/archive.py",
            "src/interfaces/cli/research.py",
            "src/interfaces/cli/main.py",
            "domain/domain/option_lifecycle.py",
            "domain/domain/performance/strategy_evaluation.py",
            "src/application/opening_quote_evidence.py",
            "src/application/current_fx_run.py",
            "src/infrastructure/exchange_rates.py",
            "src/application/research/underlier_history.py",
            "src/application/research/watchlist_scope.py",
            "src/application/research/watchlist_discovery.py",
            "src/application/research/watchlist_day.py",
            "src/application/research/market_ancillary.py",
            "src/application/research/watchlist_series.py",
            "src/application/required_data_blobs.py",
            "src/application/research/prescreen.py",
            "src/application/opend_symbol_fetching.py",
            "src/application/opend_symbol_fetching_cli.py",
            "src/application/opend_symbol_chain_fetching.py",
            "src/application/required_data_steps.py",
            "src/application/multi_tick/required_data_prefetch.py",
            "src/application/required_data_planning.py",
            "domain/domain/trade_execution.py",
            "domain/domain/fx_quote_policy.py",
        }
    )
)


def _bytes(value):
    return canonical_json_bytes_lines(value).removesuffix(b"\n")


def _utc(value, field="timestamp"):
    parsed = quote._parse_utc(value, field)
    if not isinstance(value, str) or not value.endswith(("Z", "+00:00")):
        raise ValueError(f"{field} must explicitly be UTC")
    return parsed


def _stamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("clock must return aware UTC")
    return value.isoformat().replace("+00:00", "Z")


def _now(clock):
    value = clock() if clock else datetime.now(timezone.utc)
    _stamp(value)
    return value


def _keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{name} fields invalid")


def _path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink path forbidden")
    return path


def _read(path):
    path = _path(path)
    if not path.is_file():
        raise ValueError("regular input file required")
    raw = path.read_bytes()

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(
        raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON"))
    )
    return raw, value


def _private_dir(path, *, fresh=False):
    path = _path(path)
    if not path.parent.exists():
        _private_dir(path.parent)
    if fresh:
        path.mkdir(mode=0o700, exist_ok=False)
    else:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise ValueError("private directory permissions required")
    return path


def _outside_checkout(path):
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("private research artifacts must be outside Git checkouts")


def _put(path, raw):
    _path(path)
    if path.exists():
        if not path.is_file() or path.read_bytes() != raw or path.stat().st_mode & 0o077:
            raise ValueError("immutable archive differs")
    else:
        try:
            quote._write_once(path, raw)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("concurrent immutable archive differs")


def _disjoint(path, others):
    path = _path(path)
    for other in others:
        other = _path(other)
        if path == other or path in other.parents or other in path.parents:
            raise ValueError("archive/runtime/prune/output paths overlap")


def _record(root, path):
    path = _path(path)
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("private regular evidence file required")
    raw = path.read_bytes()
    return {"path": path.relative_to(root).as_posix(), "size": len(raw), "sha256": quote._sha256(raw)}


def _file(root, relpath, *, allow_limiter=False):
    if (
        not isinstance(relpath, str)
        or not relpath
        or Path(relpath).is_absolute()
        or any(p in {"..", "."} or p == "limiter" and not allow_limiter for p in relpath.split("/"))
    ):
        raise ValueError("unsafe evidence path")
    result = _path(root / relpath)
    if root not in result.parents:
        raise ValueError("evidence path escapes series")
    return result


def _verify_record(root, record):
    if _record(root, _file(root, record["path"])) != record:
        raise ValueError(f"evidence hash/size mismatch: {record['path']}")


def _implementation():
    root = Path(__file__).resolve().parents[3]
    manifest = {
        "policy": SERIES_POLICY,
        "sources": [
            {"path": path, "sha256": quote._sha256((root / path).read_bytes())} for path in IMPLEMENTATION_SOURCES
        ],
        "quote_implementation_sha256": quote._implementation_sha256(),
    }
    return manifest


def _validate_calendar_receipt(receipt, *, start, end):
    # The trading-date owner rejects all-holiday ranges; retain its existing empty-range contract.
    if not isinstance(receipt, dict) or not isinstance(receipt.get("rows"), list):
        raise ValueError("calendar receipt invalid")
    if receipt["rows"]:
        trading_calendar_dates(receipt, start=start, end=end)
    elif not (
        type(receipt.get("retcode")) is int
        and receipt["retcode"] == 0
        and receipt.get("coverage_complete") is True
        and receipt.get("pagination_complete") is True
        and receipt.get("page_count") == 1
    ):
        raise ValueError("holiday calendar coverage incomplete")


def _make_plan(raw_inputs, start_date, end_date, *, observation_only=False):
    if type(observation_only) is not bool:
        raise ValueError("observation_only must be explicit boolean")
    if observation_only:
        _keys(raw_inputs, {"scope", "calendar", "policy"}, "observation-only inputs")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start.isoformat() != start_date or end.isoformat() != end_date or end < start:
        raise ValueError("finite canonical date interval required")
    values = {key: json.loads(raw) for key, raw in raw_inputs.items()}
    scope, calendars, policies = (values[k] for k in ("scope", "calendar", "policy"))
    last = None if observation_only else values["last_trade"]
    standalone = "underlier_codes" in scope
    _keys(scope, {"scope_id", "contracts", "fx_pairs"} | ({"underlier_codes"} if standalone else set()), "scope")
    if standalone and not observation_only:
        raise ValueError("standalone stocks require an observation-only plan")
    underliers = scope.get("underlier_codes", [])
    if standalone and (
        not isinstance(underliers, list)
        or not underliers
        or len(underliers) != len(set(underliers))
        or any(
            not isinstance(code, str) or not re.fullmatch(r"HK\.[0-9]{5}|US\.[A-Z][A-Z0-9.-]*", code)
            for code in underliers
        )
    ):
        raise ValueError("explicit unique stock codes required")
    if (
        not isinstance(scope["fx_pairs"], list)
        or not scope["fx_pairs"]
        or any(not isinstance(pair, str) or pair not in {"USDCNY", "HKDCNY"} for pair in scope["fx_pairs"])
    ):
        raise ValueError("explicit native FX pairs required")
    if not isinstance(scope["scope_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", scope["scope_id"]
    ):
        raise ValueError("explicit scope identity required")
    if not isinstance(scope["contracts"], list) or (not scope["contracts"] and not standalone):
        raise ValueError("nonempty explicit scope required")
    pairs = {}
    for contract in scope["contracts"]:
        _keys(contract, {"option_code", "underlier_code"}, "contract")
        code, underlier = contract["option_code"], contract["underlier_code"]
        match = OPTION_CODE_RE.fullmatch(code) if isinstance(code, str) else None
        if match is None or match.group("market") not in {"HK", "US"}:
            raise ValueError("option identity invalid")
        market = match.group("market")
        if (
            not isinstance(underlier, str)
            or not re.fullmatch(r"(?:HK\.[0-9]{5}|US\.[A-Z][A-Z0-9.-]*)", underlier)
            or not underlier.startswith(market + ".")
        ):
            raise ValueError("explicit same-market underlier required")
        if code in pairs and pairs[code] != underlier:
            raise ValueError("conflicting underlier identity")
        pairs[code] = underlier
    normalized_scope = {
        "scope_id": scope["scope_id"],
        "fx_pairs": sorted(set(scope["fx_pairs"])),
        "contracts": [{"option_code": code, "underlier_code": pairs[code]} for code in sorted(pairs)],
    }
    if standalone:
        normalized_scope["underlier_codes"] = sorted(underliers)
    scope_hash = quote._sha256(_bytes(normalized_scope))
    inputs = {
        key: {
            "path": "inputs/" + ("last-trade" if key == "last_trade" else key) + ".json",
            "sha256": quote._sha256(raw),
        }
        for key, raw in raw_inputs.items()
    }
    _keys(calendars, {"markets"}, "calendar")
    if not observation_only:
        _keys(last, {"contracts"}, "last trade")
    markets = sorted({code.split(".")[0] for code in [*pairs, *underliers]})
    if (
        not isinstance(policies, dict)
        or set(policies) != set(markets)
        or set(calendars["markets"]) != set(markets)
        or (not observation_only and set(last["contracts"]) != set(pairs))
    ):
        raise ValueError("market/contract input coverage mismatch")
    for metadata in () if observation_only else last["contracts"].values():
        _keys(
            metadata,
            {"last_trade_cutoff_ms", "last_trade_cutoff_source", "source_observed_at_utc"},
            "last trade metadata",
        )
        if (
            type(metadata["last_trade_cutoff_ms"]) is not int
            or metadata["last_trade_cutoff_ms"] <= 0
            or metadata["last_trade_cutoff_source"] not in SUPPORTED_LAST_TRADE_SOURCES
        ):
            raise ValueError("authoritative last trade cutoff unavailable")
        _utc(metadata["source_observed_at_utc"])
    extra = {}
    if "observations" in values:
        schedule = values["observations"]
        _keys(schedule, {"schema_version", "observations"}, "observation schedule")
        if (
            schedule["schema_version"] != "research.observation-schedule.v1"
            or not isinstance(schedule["observations"], list)
            or not schedule["observations"]
        ):
            raise ValueError("nonempty explicit observation schedule required")
        for item in schedule["observations"]:
            _keys(item, {"market", "trade_date", "expected_at_utc", "purpose"}, "explicit observation")
            market, day = item["market"], item["trade_date"]
            if (
                not isinstance(market, str)
                or market not in markets
                or item["purpose"] not in ("execution", "valuation")
            ):
                raise ValueError("explicit observation market/purpose invalid")
            expected = _utc(item["expected_at_utc"], "explicit observation time")
            if (
                expected.second
                or expected.microsecond
                or expected.astimezone(ZoneInfo(policies[market]["timezone"])).date().isoformat() != day
                or not isinstance(day, str)
                or not start_date <= day <= end_date
            ):
                raise ValueError("explicit observation date/minute invalid")
            key = (market, day)
            rows = extra.setdefault(key, [])
            if any(old[0] == expected for old in rows):
                raise ValueError("duplicate explicit observation")
            rows.append((expected, item["purpose"]))
    slots = []
    for market in markets:
        codes = sorted(code for code in pairs if code.startswith(market + "."))
        market_underliers = sorted(
            {pairs[code] for code in codes} | {code for code in underliers if code.startswith(market + ".")}
        )
        if max(len(codes), len(market_underliers)) > quote.MAX_OPTION_CODES:
            raise ValueError("whole plan exceeds 200 contracts per market slot")
        policy = policies[market]
        if policy != INSTRUMENT_POLICY_REGISTRY[f"{market}:standard_equity_option"]:
            raise ValueError("policy differs from authoritative instrument owner")
        calendar = calendars["markets"][market]
        _keys(calendar, {"start", "end", "source_observed_at_utc", "receipt"}, "market calendar")
        if date.fromisoformat(calendar["start"]) > start or date.fromisoformat(calendar["end"]) < end:
            raise ValueError("calendar coverage insufficient")
        known = _utc(calendar["source_observed_at_utc"])
        receipt = calendar["receipt"]
        _validate_calendar_receipt(
            receipt, start=date.fromisoformat(calendar["start"]), end=date.fromisoformat(calendar["end"])
        )
        seen = set()
        for row in receipt["rows"]:
            day = row.get("time") or row.get("date") or row.get("trade_date")
            if day in seen:
                raise ValueError("duplicate calendar date")
            seen.add(day)
            if not start_date <= day <= end_date:
                continue
            kind = str(row["trade_date_type"]).strip().upper()
            close_key = "half_trade_session_close" if kind == "MORNING" else "last_trade_session_close"
            close = datetime.combine(
                date.fromisoformat(day), time.fromisoformat(policy[close_key]), ZoneInfo(policy["timezone"])
            ).astimezone(timezone.utc)
            reference = close - timedelta(minutes=5)
            observations = [(reference, "session_reference"), *extra.pop((market, day), [])]
            if len({item[0] for item in observations}) != len(observations):
                raise ValueError("explicit observation duplicates daily reference")
            for expected, purpose in observations:
                if expected >= close:
                    raise ValueError("explicit observation must precede session close")
                deadline = min(expected + timedelta(seconds=60), close)
                if known > expected:
                    raise ValueError("calendar asof_unavailable")
                for code in () if observation_only else codes:
                    metadata = last["contracts"][code]
                    _keys(
                        metadata,
                        {"last_trade_cutoff_ms", "last_trade_cutoff_source", "source_observed_at_utc"},
                        "last trade metadata",
                    )
                    cutoff = metadata["last_trade_cutoff_ms"]
                    if (
                        type(cutoff) is not int
                        or cutoff <= 0
                        or metadata["last_trade_cutoff_source"] not in SUPPORTED_LAST_TRADE_SOURCES
                    ):
                        raise ValueError("authoritative last trade cutoff unavailable")
                    if deadline.timestamp() * 1000 > cutoff or _utc(metadata["source_observed_at_utc"]) > expected:
                        raise ValueError("observation exceeds authoritative last trade/asof cutoff")
                preimage = {
                    "scope_sha256": scope_hash,
                    "calendar_sha256": inputs["calendar"]["sha256"],
                    "policy_sha256": inputs["policy"]["sha256"],
                    "market": market,
                    "trade_date": day,
                    "timezone": policy["timezone"],
                    "session_close_at_utc": _stamp(close),
                    "expected_at_utc": _stamp(expected),
                    "option_codes": codes,
                    "underlier_codes": market_underliers,
                    "required_dimensions": REQUIRED_DIMENSIONS,
                }
                if "observations" in values:
                    preimage["purpose"] = purpose
                slots.append(
                    {
                        **{
                            key: value
                            for key, value in preimage.items()
                            if key not in {"scope_sha256", "calendar_sha256", "policy_sha256"}
                        },
                        "session_type": kind,
                        "observation_id": quote._sha256(_bytes(preimage)),
                    }
                )
    if extra:
        raise ValueError("explicit observation missing trading calendar day")
    slots.sort(key=lambda item: (item["expected_at_utc"], item["market"]))
    return {
        "schema_version": (
            UNDERLIER_PLAN_SCHEMA
            if standalone
            else OBSERVATION_ONLY_PLAN_SCHEMA
            if observation_only
            else EXPLICIT_PLAN_SCHEMA
            if "observations" in values
            else PLAN_SCHEMA
        ),
        "scope_id": scope["scope_id"],
        "scope_sha256": scope_hash,
        "start_date": start_date,
        "end_date": end_date,
        "inputs": inputs,
        "required_dimensions": list(REQUIRED_DIMENSIONS),
        "observations": slots,
    }


def build_series_plan(
    *,
    scope_file: Path,
    calendar_file: Path,
    policy_file: Path,
    last_trade_file: Path | None = None,
    start_date: str,
    end_date: str,
    output_dir: Path,
    observations_file: Path | None = None,
    observation_only: bool = False,
) -> dict:
    if type(observation_only) is not bool:
        raise ValueError("observation_only must be explicit boolean")
    if observation_only and (last_trade_file is not None or observations_file is not None):
        raise ValueError("observation-only forbids last-trade and additional observations")
    if not observation_only and last_trade_file is None:
        raise ValueError("authoritative last-trade file required")
    paths = dict(scope=scope_file, calendar=calendar_file, policy=policy_file)
    if last_trade_file is not None:
        paths["last_trade"] = last_trade_file
    if observations_file is not None:
        paths["observations"] = observations_file
    raw_inputs = {key: _read(path)[0] for key, path in paths.items()}
    plan = _make_plan(raw_inputs, start_date, end_date, observation_only=observation_only)
    _outside_checkout(_path(output_dir))
    out = _private_dir(output_dir, fresh=True)
    _private_dir(out / "inputs")
    for key, raw in raw_inputs.items():
        _put(out / plan["inputs"][key]["path"], raw)
    digest = quote._write_once(out / "series-plan.json", _bytes(plan))
    return {**plan, "series_plan_sha256": digest}


def _load_plan(root, expected):
    root = _path(root)
    raw, plan = _read(root / "series-plan.json")
    if not re.fullmatch(r"[0-9a-f]{64}", expected or "") or quote._sha256(raw) != expected:
        raise ValueError("external plan SHA mismatch")
    if not isinstance(plan, dict) or plan.get("schema_version") not in {
        PLAN_SCHEMA,
        EXPLICIT_PLAN_SCHEMA,
        OBSERVATION_ONLY_PLAN_SCHEMA,
        UNDERLIER_PLAN_SCHEMA,
    }:
        raise ValueError("series plan schema invalid")
    raw_inputs = {}
    observation_only = plan["schema_version"] in {OBSERVATION_ONLY_PLAN_SCHEMA, UNDERLIER_PLAN_SCHEMA}
    keys = {"scope", "calendar", "policy"}
    if not observation_only:
        keys.add("last_trade")
    if plan["schema_version"] == EXPLICIT_PLAN_SCHEMA:
        keys.add("observations")
    _keys(plan["inputs"], keys, "plan inputs")
    for key in sorted(keys):
        item = plan["inputs"][key]
        original, _ = _read(_file(root, item["path"]))
        if quote._sha256(original) != item["sha256"]:
            raise ValueError("frozen input SHA mismatch")
        raw_inputs[key] = original
    if (
        _make_plan(raw_inputs, plan["start_date"], plan["end_date"], observation_only=observation_only) != plan
        or _bytes(plan) != raw
    ):
        raise ValueError("frozen plan identity invalid")
    return plan, raw, raw_inputs


def _safe_window(path, expected, plan_hash):
    raw, value = _read(path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected or "") or quote._sha256(raw) != expected:
        raise ValueError("external safe-window SHA mismatch")
    _keys(value, {"schema_version", "series_plan_sha256", "intervals"}, "safe-window")
    if (
        value["schema_version"] != "research.safe-window.v1"
        or value["series_plan_sha256"] != plan_hash
        or not isinstance(value["intervals"], list)
        or not value["intervals"]
    ):
        raise ValueError("safe-window identity/intervals invalid")
    intervals, previous = [], None
    for interval in value["intervals"]:
        _keys(interval, {"start_utc", "end_utc"}, "safe interval")
        start, end = _utc(interval["start_utc"]), _utc(interval["end_utc"])
        if start >= end or previous is not None and start < previous:
            raise ValueError("safe intervals must be sorted nonoverlapping")
        intervals.append((start, end))
        previous = end
    return raw, intervals


def _slot_window(slot):
    start = _utc(slot["expected_at_utc"])
    return start, min(start + timedelta(seconds=60), _utc(slot["session_close_at_utc"]))


class _WindowGateway:
    """Only snapshot access, checked at every real request including owner retries."""

    def __init__(self, gateway, *, start, end, clock, calls):
        self.gateway, self.start, self.end, self.clock, self.calls = gateway, start, end, clock, calls

    def get_snapshot(self, codes):
        return self._call("market_snapshot", list(codes), lambda: self.gateway.get_snapshot(codes))

    def ensure_quote_ready(self):
        def before_request():
            requested = _now(self.clock)
            if not self.start <= requested < self.end:
                raise ValueError("provider_request_outside_window")
            self.calls[-1]["requested_at_utc"] = _stamp(requested)

        return self._call("quote_preflight", [], lambda: self.gateway.ensure_quote_ready(before_request=before_request))

    def _call(self, endpoint, codes, callback):
        requested = _now(self.clock)
        if not self.start <= requested < self.end:
            raise ValueError("provider_request_outside_window")
        call = {
            "endpoint": endpoint,
            "codes": list(codes),
            "requested_at_utc": _stamp(requested),
            "received_at_utc": None,
            "status": "started",
        }
        self.calls.append(call)
        try:
            result = callback()
            call["status"] = "received"
            return result
        except BaseException as exc:
            call["status"] = type(exc).__name__
            raise
        finally:
            received = _now(self.clock)
            call["received_at_utc"] = _stamp(received)
            if received < requested or received >= self.end:
                call["status"] = "received_outside_window"

    def close(self):
        # The series owner closes after both option and underlier capture.
        pass


def _snapshot_underliers(
    root, attempt_dir, slot, gateway, implementation_hash, snapshot_limiter_root=None, rate_limited_call=None
):
    requested = len(gateway.calls)
    raw_rows = (rate_limited_call or rate_limited_opend_call)(
        base_dir=snapshot_limiter_root if snapshot_limiter_root is not None else attempt_dir / "quotes/limiter",
        endpoint="market_snapshot",
        **quote.SNAPSHOT_LIMIT.call_kwargs(),
        call=lambda: gateway.get_snapshot(slot["underlier_codes"]),
    )
    rows = quote._json_safe(futu_dataframe_records(raw_rows))
    call = gateway.calls[requested]
    payload = {
        "schema_version": "research.underlier-snapshot.v1",
        "autype": "NONE",
        "provider_rows": rows,
        "requested_codes": slot["underlier_codes"],
        "requested_at_utc": call["requested_at_utc"],
        "received_at_utc": call["received_at_utc"],
    }
    publish_source_receipt(
        private_files=True,
        producer_root=attempt_dir,
        receipt_relpath="underlier-receipt.json",
        payload_relpath="underlier.json",
        payload_bytes=_bytes(payload),
        source_kind="research_supplemental",
        producer_schema_version=payload["schema_version"],
        producer_run_id=attempt_dir.name,
        broker="futu",
        included_markets=[slot["market"]],
        source_native_id=slot["observation_id"] + ":underliers",
        source_observed_at=call["requested_at_utc"],
        completed_at=call["received_at_utc"],
        producer_policy_hash=implementation_hash,
    )


def _dimension(reasons=(), facts=()):
    return {
        "status": "incomplete" if reasons else "complete",
        "missing_reasons": sorted(set(reasons)),
        "facts": list(facts),
    }


def _fact(root, path, pointer, field, unit, source=None, requested=None, received=None, **extra):
    item = _record(root, path)
    return {
        "path": item["path"],
        "sha256": item["sha256"],
        "json_pointer": pointer,
        "provider_field": field,
        "unit": unit,
        "source_observed_at_utc": source,
        "requested_at_utc": requested,
        "received_at_utc": received,
        **extra,
    }


def _finite(value, *, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def _ancillary_dimensions(root, plan, slot, asof):
    from src.application.current_fx_run import _decode, RunFXError
    from src.infrastructure.exchange_rates import verified_exchange_rate_pairs

    fx_facts, fx_reasons, ca_facts, ca_reasons = [], [], [], []
    fx_pairs, covered = set(), set()
    native_units = {"USDCNY": "CNY/USD", "HKDCNY": "CNY/HKD"}
    ancillary = root / "inputs" / "ancillary"
    paths = sorted(ancillary.rglob("*.json")) if ancillary.exists() else []
    for path in paths:
        raw, value = _read(path)
        if not isinstance(value, dict):
            continue
        if "snapshot" in value and "snapshot_sha256" in value:
            try:
                snapshot, _ = _decode(raw, run_id=value.get("run_id"))
            except (RunFXError, TypeError):
                fx_reasons.append("fx_envelope_invalid")
                continue
            verified = verified_exchange_rate_pairs(snapshot, observed_at=asof)
            for pair, row in snapshot["pairs"].items():
                if not isinstance(row, dict):
                    fx_reasons.append("fx_pair_invalid:" + pair)
                    continue
                pointer = "/snapshot/pairs/" + pair.replace("~", "~0").replace("/", "~1")
                for field in ("rate", "source", "quote_at_utc", "observed_at_utc"):
                    if field in row:
                        fx_facts.append(
                            _fact(
                                root,
                                path,
                                pointer + "/" + field,
                                field,
                                native_units.get(pair) if field == "rate" else "provider_native",
                                row.get("observed_at_utc"),
                                effective_at_utc=row.get("quote_at_utc"),
                            )
                        )
                if pair not in verified:
                    fx_reasons.append("fx_pair_invalid_or_asof_unavailable:" + pair)
                elif pair in native_units:
                    fx_pairs.add(pair)
        if value.get("kind") == "corporate_actions" and isinstance(value.get("request"), dict):
            request = value["request"]
            code = request.get("code")
            if code not in slot["underlier_codes"]:
                continue
            received = value.get("received_at_utc")
            try:
                available = _utc(received) <= asof
            except ValueError:
                available = False
            for i, response in enumerate(value.get("responses", [])):
                if not isinstance(response, dict) or not isinstance(response.get("rows"), list):
                    ca_reasons.append("corporate_actions_response_invalid:" + code)
                    continue
                for j, row in enumerate(response["rows"]):
                    for field in row:
                        ca_facts.append(
                            _fact(
                                root,
                                path,
                                f"/responses/{i}/rows/{j}/" + field.replace("~", "~0").replace("/", "~1"),
                                field,
                                "provider_native",
                                received,
                                value.get("started_at_utc"),
                                received,
                                effective_date=row.get("ex_div_date"),
                            )
                        )
                for field in ("rows", "coverage_complete", "retcode"):
                    if field in response:
                        ca_facts.append(
                            _fact(
                                root,
                                path,
                                f"/responses/{i}/{field}",
                                field,
                                "coverage" if field != "rows" else "provider_native",
                                received,
                                value.get("started_at_utc"),
                                received,
                            )
                        )
            # Current history owner explicitly says received_unverified_coverage; never elevate it.
            ca_reasons.append("corporate_actions_coverage_unverified:" + code)
            if not available:
                ca_reasons.append("asof_unavailable:" + code)
            covered.add(code)
    required_pairs = set(_read(root / "inputs/scope.json")[1]["fx_pairs"])
    fx_reasons.extend("fx_pair_missing:" + pair for pair in sorted(required_pairs - fx_pairs))
    ca_reasons.extend("corporate_actions_missing:" + code for code in sorted(set(slot["underlier_codes"]) - covered))
    return _dimension(fx_reasons, fx_facts), _dimension(ca_reasons, ca_facts)


def _dimensions(root, plan, slot, attempt_dir, calls):
    dims = {key: _dimension(["not_collected"]) for key in REQUIRED_DIMENSIONS}
    option_facts, option_reasons, research_facts, research_reasons = [], ["quote_freshness_unknown"], [], []
    path = attempt_dir / "quotes" / "quotes.json"
    if path.exists():
        _, payload = _read(path)
        rows = payload.get("provider_rows", [])
        found = set()
        for i, row in enumerate(rows):
            code = row.get("code")
            found.add(code)
            requested, received = row.get("snapshot_requested_at_utc"), row.get("snapshot_received_at_utc")
            for field in ("code", "bid_price", "ask_price", "bid_vol", "ask_vol", "update_time"):
                if field in row:
                    option_facts.append(
                        _fact(
                            root,
                            path,
                            f"/provider_rows/{i}/{field}",
                            field,
                            "provider_native"
                            if field in {"code", "update_time"}
                            else (
                                "contracts" if field.endswith("vol") else ("HKD" if slot["market"] == "HK" else "USD")
                            ),
                            None,
                            requested,
                            received,
                        )
                    )
            for field in ("bid_vol", "ask_vol"):
                if not _finite(row.get(field)):
                    option_reasons.append(f"{code}:{field}_missing_or_invalid")
            if quote._missing_quote_fields([row]):
                option_reasons.append(f"{code}:bidask_invalid")
            for field in (
                "option_implied_volatility",
                "option_delta",
                "option_gamma",
                "option_theta",
                "option_vega",
                "option_contract_multiplier",
                "option_contract_size",
            ):
                value = row.get(field)
                if field in row:
                    research_facts.append(
                        _fact(
                            root,
                            path,
                            f"/provider_rows/{i}/{field}",
                            field,
                            "provider_native_unverified",
                            None,
                            requested,
                            received,
                        )
                    )
                if type(value) not in (int, float) or not math.isfinite(value):
                    research_reasons.append(f"{code}:{field}_missing_or_invalid")
            # Raw fields are retained; no second unit conversion or inferred source time.
            research_reasons.append(f"{code}:research_units_and_source_time_unverified")
        for code in sorted(set(slot["option_codes"]) - found):
            option_reasons.append("option_missing:" + code)
            research_reasons.append("option_missing:" + code)
    else:
        option_reasons.append("quotes_missing")
        research_reasons.append("quotes_missing")
    dims["option_quote"] = _dimension(option_reasons, option_facts)
    dims["option_research"] = _dimension(research_reasons, research_facts)
    underlier_facts, underlier_reasons, found = [], [], set()
    path = attempt_dir / "underlier.json"
    if path.exists():
        _, payload = _read(path)
        for i, row in enumerate(payload["provider_rows"]):
            code = row.get("code")
            if code in found or code not in slot["underlier_codes"]:
                underlier_reasons.append("underlier_identity_mismatch")
            found.add(code)
            source = _parse_opend_time(row.get("update_time"), market=slot["market"])
            requested, received = payload["requested_at_utc"], payload["received_at_utc"]
            if source is None:
                underlier_reasons.append(f"{code}:underlier_source_time_unknown")
            elif not 0 <= (_utc(received) - source).total_seconds() <= 60:
                underlier_reasons.append(f"{code}:underlier_freshness_invalid")
            if not _finite(row.get("last_price"), positive=True):
                underlier_reasons.append(f"{code}:last_price_invalid")
            for field in ("code", "last_price", "update_time"):
                if field in row:
                    underlier_facts.append(
                        _fact(
                            root,
                            path,
                            f"/provider_rows/{i}/{field}",
                            field,
                            ("HKD" if slot["market"] == "HK" else "USD")
                            if field == "last_price"
                            else "provider_native",
                            _stamp(source) if source else None,
                            requested,
                            received,
                        )
                    )
        if not (attempt_dir / "underlier-receipt.json").is_file():
            underlier_reasons.append("underlier_receipt_missing")
    underlier_reasons.extend("underlier_missing:" + code for code in sorted(set(slot["underlier_codes"]) - found))
    dims["underlier_quote"] = _dimension(underlier_reasons, underlier_facts)
    _, calendar = _read(root / "inputs/calendar.json")
    entry = calendar["markets"][slot["market"]]
    prefix = "/markets/" + slot["market"]
    facts = [
        _fact(
            root,
            root / "inputs/calendar.json",
            prefix + "/" + field,
            field,
            "coverage",
            entry["source_observed_at_utc"],
        )
        for field in ("start", "end", "source_observed_at_utc")
    ]
    for field in ("coverage_complete", "pagination_complete", "retcode", "page_count"):
        facts.append(
            _fact(
                root,
                root / "inputs/calendar.json",
                prefix + "/receipt/" + field,
                field,
                "coverage",
                entry["source_observed_at_utc"],
            )
        )
    for i, row in enumerate(entry["receipt"]["rows"]):
        for field in row:
            facts.append(
                _fact(
                    root,
                    root / "inputs/calendar.json",
                    prefix + f"/receipt/rows/{i}/" + field.replace("~", "~0").replace("/", "~1"),
                    field,
                    "date/session",
                    entry["source_observed_at_utc"],
                    effective_date=row.get("time") or row.get("date") or row.get("trade_date"),
                )
            )
    dims["calendar"] = _dimension([], facts)
    dims["fx"], dims["corporate_actions"] = _ancillary_dimensions(root, plan, slot, _utc(slot["expected_at_utc"]))
    start, end = _slot_window(slot)
    for call in calls:
        if (
            call["status"] != "received"
            or not call["received_at_utc"]
            or not start <= _utc(call["requested_at_utc"]) <= _utc(call["received_at_utc"]) < end
        ):
            for key in ("option_quote", "option_research", "underlier_quote"):
                dims[key] = _dimension(
                    dims[key]["missing_reasons"] + ["provider_call_failed_or_outside_window"], dims[key]["facts"]
                )
    return dims


def _market_only(item):
    if isinstance(item, dict):
        for key, val in item.items():
            if key.lower() in {
                "account",
                "accounts",
                "portfolio_account_identity_hash",
                "producer_account_run_id",
                "credentials",
                "token",
                "secret",
            } and val not in (None, "", []):
                raise ValueError("account/secret ancillary forbidden")
            _market_only(val)
    elif isinstance(item, list):
        for val in item:
            _market_only(val)


def _copy_ancillary(source, root):
    target = root / "inputs/ancillary"
    if source is None:
        if target.exists():
            _, index = _read(root / "ancillary-manifest.json")
            for item in index["files"]:
                _verify_record(root, item)
        return
    source = _path(source)
    _disjoint(source, [root])
    files = []
    for path in sorted(source.rglob("*")):
        _path(path)
        if path.is_dir():
            if path.name == "limiter":
                raise ValueError("limiter ancillary forbidden")
            continue
        if path.suffix != ".json":
            raise ValueError("only explicitly exported JSON market evidence allowed")
        raw, value = _read(path)

        _market_only(value)
        destination = target / path.relative_to(source)
        _private_dir(destination.parent)
        _put(destination, raw)
        files.append(_record(root, destination))
    _put(root / "ancillary-manifest.json", _bytes({"files": files}))


def _initialize_archive(root, plan, plan_raw, raw_inputs, safe_raw, runtime_root, prune_roots, ancillary_dir):
    _private_dir(root)
    _private_dir(root / "inputs")
    _put(root / "series-plan.json", plan_raw)
    for key, raw in raw_inputs.items():
        _put(root / plan["inputs"][key]["path"], raw)
    _put(root / "safe-window.json", safe_raw)
    _put(
        root / "local-isolation.json",
        _bytes(
            {
                "runtime_root": str(_path(runtime_root)),
                "prune_roots": sorted({str(_path(path)) for path in prune_roots}),
            }
        ),
    )
    implementation = _implementation()
    _put(root / "implementation.json", _bytes(implementation))
    _copy_ancillary(ancillary_dir, root)
    return quote._sha256(_bytes(implementation))


def collect_series(
    *,
    series_dir: Path,
    expected_plan_sha256: str,
    safe_window_file: Path,
    expected_safe_window_sha256: str,
    archive_root: Path,
    runtime_root: Path,
    prune_roots: list[Path],
    host: str,
    port: int,
    ancillary_dir: Path | None = None,
    confirm: bool = False,
    clock=None,
    gateway_factory=None,
    snapshot_limiter_root: Path | None = None,
    rate_limited_call=None,
) -> dict:
    plan, plan_raw, raw_inputs = _load_plan(series_dir, expected_plan_sha256)
    safe_raw, intervals = _safe_window(safe_window_file, expected_safe_window_sha256, expected_plan_sha256)
    if not prune_roots or not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("explicit runtime/prune roots and endpoint required")
    root = _path(archive_root)
    _outside_checkout(root)
    _disjoint(root, [runtime_root, *prune_roots])
    if snapshot_limiter_root is not None:
        snapshot_limiter_root = _path(snapshot_limiter_root)
        _outside_checkout(snapshot_limiter_root)
        _disjoint(snapshot_limiter_root, [runtime_root, *prune_roots])
    if _path(series_dir) != root:
        _disjoint(root, [series_dir])
    if root.exists():
        _private_mode_check(root)
        archived_safe = _path(root / "safe-window.json")
        if not archived_safe.exists() and any(root.iterdir()):
            raise ValueError("existing archive safe-window missing; rebinding forbidden")
        if archived_safe.exists() and _read(archived_safe)[0] != safe_raw:
            raise ValueError("immutable safe-window changed")
    now = _now(clock)
    if not confirm:
        return {
            "status": "preview",
            "provider_called": False,
            "series_plan_sha256": expected_plan_sha256,
            "safe_window_sha256": expected_safe_window_sha256,
            "observations": plan["observations"],
        }
    if snapshot_limiter_root is not None:
        _private_dir(snapshot_limiter_root)
    implementation_hash = _initialize_archive(
        root, plan, plan_raw, raw_inputs, safe_raw, runtime_root, prune_roots, ancillary_dir
    )
    results = []
    for slot in plan["observations"]:
        start, end = _slot_window(slot)
        now = _now(clock)
        if now < start:
            continue
        observation = root / "observations" / slot["observation_id"]
        _private_dir(observation)
        claim_dir = observation / "claim"
        try:
            _private_dir(claim_dir, fresh=True)
        except FileExistsError:
            results.append({"observation_id": slot["observation_id"], "status": "already_claimed"})
            continue
        attempt_id = str(uuid.uuid4())
        claim = {
            "attempt_id": attempt_id,
            "pid": os.getpid(),
            "started_at_utc": _stamp(now),
            "expected_safe_window_sha256": expected_safe_window_sha256,
            "safe_window_sha256": expected_safe_window_sha256,
        }
        _put(claim_dir / "claim.json", _bytes(claim))
        attempt_dir = _private_dir(observation / "attempts" / attempt_id, fresh=True)
        approved = any(a <= start and end <= b for a, b in intervals)
        calls, reasons, gateway, cancelled = [], [], None, None
        status, quote_status = "failed", "missing"
        if now >= end:
            status, reasons = "missed", ["deadline_expired"]
        elif not approved:
            status, reasons = "skipped", ["skipped_safe_window"]
        else:
            try:
                gateway = (gateway_factory or build_futu_gateway)(host=host, port=port)
                wrapper = _WindowGateway(gateway, start=start, end=end, clock=clock, calls=calls)
                if gateway_factory is None:
                    wrapper.ensure_quote_ready()
                single_plan = {
                    "schema_version": quote.PLAN_SCHEMA,
                    "scope_id": plan["scope_id"],
                    "observation_at_utc": slot["expected_at_utc"],
                    "option_codes": slot["option_codes"],
                }
                manifest = (
                    quote.collect_option_quotes(
                        single_plan,
                        output_dir=attempt_dir / "quotes",
                        host=host,
                        port=port,
                        confirm=True,
                        gateway_factory=lambda **_: wrapper,
                        now_utc=_now(clock),
                        **(
                            {"snapshot_limiter_root": snapshot_limiter_root}
                            if snapshot_limiter_root is not None
                            else {}
                        ),
                        **({"rate_limited_call": rate_limited_call} if rate_limited_call is not None else {}),
                    )
                    if slot["option_codes"]
                    else None
                )
                quote_status = manifest["status"] if manifest else "missing"
                _snapshot_underliers(
                    root, attempt_dir, slot, wrapper, implementation_hash, snapshot_limiter_root, rate_limited_call
                )
                status = "partial"
            except BaseException as exc:
                reasons.append(type(exc).__name__)
                status = "partial" if calls else "failed"
                if not isinstance(exc, Exception):
                    cancelled = exc
            finally:
                if gateway is not None:
                    try:
                        gateway.close()
                    except BaseException as exc:
                        reasons.append(type(exc).__name__)
                        if not isinstance(exc, Exception):
                            cancelled = cancelled or exc
        try:
            dims = _dimensions(root, plan, slot, attempt_dir, calls)
        except BaseException as exc:
            reasons.append("dimension_validation_failed:" + type(exc).__name__)
            dims = {key: _dimension(["dimension_validation_failed"]) for key in REQUIRED_DIMENSIONS}
            if not isinstance(exc, Exception):
                cancelled = cancelled or exc
        files = _inventory(root, attempt_dir)
        ended = _now(clock)
        published = _now(clock)
        times = [_utc(call["received_at_utc"]) for call in calls if call["received_at_utc"]]
        if published < ended or any(t > ended for t in times):
            reasons.append("clock_order_invalid")
        if quote_status == "missing" and (attempt_dir / "quotes/manifest.json").exists():
            quote_status = _read(attempt_dir / "quotes/manifest.json")[1]["status"]
        if plan["schema_version"] in {OBSERVATION_ONLY_PLAN_SCHEMA, UNDERLIER_PLAN_SCHEMA}:
            reasons.append("contract_timing_unverified")
        complete = not reasons and all(dim["status"] == "complete" for dim in dims.values())
        if complete:
            status = "success"
        attempt = {
            "schema_version": ATTEMPT_SCHEMA,
            "observation_id": slot["observation_id"],
            "attempt_id": attempt_id,
            "expected_safe_window_sha256": expected_safe_window_sha256,
            "safe_window_sha256": expected_safe_window_sha256,
            "started_at_utc": claim["started_at_utc"],
            "ended_at_utc": _stamp(ended),
            "published_at_utc": _stamp(published),
            "status": status,
            "reasons": sorted(set(reasons)),
            "quote_package_status": quote_status,
            "research_complete": complete,
            "dimensions": dims,
            "provider_calls": calls,
            "files": files,
        }
        try:
            _put(attempt_dir / "attempt.json", _bytes(attempt))
        except BaseException as exc:
            if not isinstance(exc, Exception):
                attempt_path = attempt_dir / "attempt.json"
                if not attempt_path.exists():
                    attempt.update(
                        status="partial" if calls else "failed",
                        research_complete=False,
                        published_at_utc=_stamp(_now(clock)),
                        reasons=sorted(set([*attempt["reasons"], type(exc).__name__])),
                    )
                    _put(attempt_path, _bytes(attempt))
            raise
        results.append(attempt)
        if cancelled is not None:
            raise cancelled
    return {
        "status": "collected",
        "series_plan_sha256": expected_plan_sha256,
        "producer_implementation_sha256": implementation_hash,
        "safe_window_sha256": expected_safe_window_sha256,
        "observations": results,
    }


def _private_mode_check(path):
    _path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("private archive permissions required")


def _inventory(root, directory):
    files = []
    for path in sorted(directory.rglob("*")):
        # Never follow links, even within excluded limiter data.
        _path(path)
        if "limiter" in path.relative_to(root).parts:
            continue
        _private_mode_check(path)
        if path == root / "local-isolation.json":
            continue
        parts = path.relative_to(root).parts
        if len(parts) >= 4 and parts[0] == "observations" and re.fullmatch(r"[0-9a-f]{64}", parts[1]):
            names = ()
            if len(parts) == 4 and parts[2] == "claim":
                names = ("claim.json",)
            elif (
                len(parts) in {5, 6}
                and parts[2] == "attempts"
                and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", parts[3])
            ):
                if len(parts) == 5:
                    names = ("attempt.json", "underlier.json", "underlier-receipt.json")
                elif parts[4] == "quotes":
                    names = ("plan.json", "quotes.json", "receipt.json", "manifest.json")
            if path.is_file() and any(
                re.fullmatch(r"\." + re.escape(name) + r"\.tmp\.[0-9a-f]{32}", parts[-1]) for name in names
            ):
                # A hard exit can leave the atomic writer's unpublished staging file.
                continue
        if path.is_file():
            files.append(_record(root, path))
        elif not path.is_dir():
            raise ValueError("nonregular archive evidence")
    return files


def _pointer(value, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("fact JSON pointer invalid")
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def export_series(*, series_dir: Path, expected_plan_sha256: str, cutoff: str, output_dir: Path) -> dict:
    root, out = _path(series_dir), _path(output_dir)
    _private_mode_check(root)
    _outside_checkout(out)
    _disjoint(out, [root])
    if out.exists():
        raise FileExistsError(out)
    cutoff_time = _utc(cutoff, "cutoff")
    _, isolation = _read(root / "local-isolation.json")
    _keys(isolation, {"runtime_root", "prune_roots"}, "local isolation")
    if not isolation["prune_roots"]:
        raise ValueError("explicit prune exclusion required")
    excluded_roots = [isolation["runtime_root"], *isolation["prune_roots"]]
    _disjoint(root, excluded_roots)
    _disjoint(out, excluded_roots)
    plan, _, _ = _load_plan(root, expected_plan_sha256)
    _, implementation = _read(root / "implementation.json")
    implementation_hash = quote._sha256(_bytes(implementation))
    if implementation != _implementation():
        raise ValueError("producer implementation changed within series")
    safe_raw = _read(root / "safe-window.json")[0]
    safe_hash = quote._sha256(safe_raw)
    _safe_window(root / "safe-window.json", safe_hash, expected_plan_sha256)
    inventory = _inventory(root, root)
    exported_paths = {record["path"] for record in inventory}
    allowed = {"series-plan.json", "safe-window.json", "implementation.json"} | {
        item["path"] for item in plan["inputs"].values()
    }
    if (root / "ancillary-manifest.json").exists():
        allowed.add("ancillary-manifest.json")
        for record in _read(root / "ancillary-manifest.json")[1]["files"]:
            _verify_record(root, record)
            allowed.add(record["path"])
    observations = []
    for slot in plan["observations"]:
        observation = root / "observations" / slot["observation_id"]
        claim_path = observation / "claim/claim.json"
        attempts, status = [], "missing"
        claim = _read(claim_path)[1] if claim_path.exists() else None
        claim_record_missing = (observation / "claim").is_dir() and claim is None
        if claim_record_missing:
            status = "abandoned"
            attempts.append(
                {
                    "attempt_id": None,
                    "status": "abandoned",
                    "reason": "claim_record_missing",
                    "started_at_utc": None,
                    "ended_at_utc": None,
                    "published_at_utc": None,
                    "files": [],
                }
            )
        if claim:
            allowed.add(claim_path.relative_to(root).as_posix())
            if claim["safe_window_sha256"] != safe_hash or claim["expected_safe_window_sha256"] != safe_hash:
                raise ValueError("claim safe-window binding mismatch")
        attempt_root = observation / "attempts"
        for directory in sorted(attempt_root.iterdir()) if attempt_root.exists() else []:
            _path(directory)
            if not directory.is_dir() or not claim or directory.name != claim["attempt_id"]:
                raise ValueError("unclaimed/duplicate attempt")
            attempt_path = directory / "attempt.json"
            if not attempt_path.exists():
                partial_files = _inventory(root, directory)
                # Crash leftovers are copied as partial evidence, never promoted to a quote package.
                for record in partial_files:
                    if Path(record["path"]).name not in {
                        "plan.json",
                        "quotes.json",
                        "receipt.json",
                        "manifest.json",
                        "underlier.json",
                        "underlier-receipt.json",
                    }:
                        raise ValueError("unknown unfinished-attempt file")
                    allowed.add(record["path"])
                attempts.append(
                    {
                        "attempt_id": directory.name,
                        "status": "abandoned",
                        "ended_at_utc": None,
                        "published_at_utc": None,
                        "files": partial_files,
                    }
                )
                status = "abandoned"
                continue
            _, attempt = _read(attempt_path)
            if (
                attempt.get("schema_version") != ATTEMPT_SCHEMA
                or attempt.get("observation_id") != slot["observation_id"]
                or attempt.get("attempt_id") != directory.name
                or set(attempt.get("dimensions", {})) != set(REQUIRED_DIMENSIONS)
            ):
                raise ValueError("attempt identity/dimensions invalid")
            if plan["schema_version"] in {OBSERVATION_ONLY_PLAN_SCHEMA, UNDERLIER_PLAN_SCHEMA} and (
                attempt.get("research_complete") is not False
                or "contract_timing_unverified" not in attempt.get("reasons", [])
            ):
                raise ValueError("observation-only attempt timing qualification invalid")
            if attempt["safe_window_sha256"] != safe_hash or attempt["expected_safe_window_sha256"] != safe_hash:
                raise ValueError("attempt safe-window binding mismatch")
            allowed.add(attempt_path.relative_to(root).as_posix())
            expected_files = {record["path"] for record in attempt["files"]}
            actual_files = {record["path"] for record in _inventory(root, directory)} - {
                attempt_path.relative_to(root).as_posix()
            }
            if expected_files != actual_files:
                raise ValueError("attempt file inventory differs")
            for record in attempt["files"]:
                _verify_record(root, record)
                allowed.add(record["path"])
            for receipt_path in (directory / "quotes/receipt.json", directory / "underlier-receipt.json"):
                if receipt_path.exists():
                    _, receipt = _read(receipt_path)
                    validate_source_receipt(
                        receipt,
                        producer_root=receipt_path.parent,
                        now=receipt["completed_at"],
                        require_fresh=False,
                        expected_source_kind="research_supplemental",
                    )
            for dimension in attempt["dimensions"].values():
                for fact in dimension["facts"]:
                    if fact["path"] not in exported_paths:
                        raise ValueError("fact references excluded or missing export file")
                    path = _file(root, fact["path"])
                    raw, value = _read(path)
                    if quote._sha256(raw) != fact["sha256"]:
                        raise ValueError("fact file SHA mismatch")
                    _pointer(value, fact["json_pointer"])
            published, ended, started = (
                _utc(attempt[key]) for key in ("published_at_utc", "ended_at_utc", "started_at_utc")
            )
            if (
                started > ended
                or ended > published
                or any(
                    _utc(call[key]) > ended
                    for call in attempt["provider_calls"]
                    for key in ("requested_at_utc", "received_at_utc")
                    if call[key] is not None
                )
            ):
                raise ValueError("attempt publication time order invalid")
            status = attempt["status"] if published <= cutoff_time else "published_after_cutoff"
            attempts.append(attempt)
        if claim and not attempts:
            status = "abandoned"
            attempts.append(
                {
                    "attempt_id": claim["attempt_id"],
                    "status": "abandoned",
                    "ended_at_utc": None,
                    "published_at_utc": None,
                    "files": [],
                }
            )
        observations.append(
            {
                "observation_id": slot["observation_id"],
                "due": cutoff_time >= _slot_window(slot)[1],
                "status": status,
                "missing": not attempts,
                "attempts": attempts,
            }
        )
    if {record["path"] for record in inventory} != allowed:
        raise ValueError("unknown or missing series files")
    manifest = {
        "schema_version": EXPORT_SCHEMA,
        "series_plan_sha256": expected_plan_sha256,
        "producer_implementation_sha256": implementation_hash,
        "implementation": implementation,
        "cutoff_utc": _stamp(cutoff_time),
        "expected_safe_window_sha256": safe_hash,
        "safe_window_sha256": safe_hash,
        "observations": observations,
        "files": inventory,
    }
    _private_dir(out, fresh=True)
    for record in inventory:
        source, destination = _file(root, record["path"]), _file(out, record["path"])
        raw = source.read_bytes()
        if quote._sha256(raw) != record["sha256"] or len(raw) != record["size"]:
            raise ValueError("source changed during export")
        _private_dir(destination.parent)
        _put(destination, raw)
    digest = quote._write_once(out / "series-export.json", _bytes(manifest))
    return {**manifest, "series_export_sha256": digest}
