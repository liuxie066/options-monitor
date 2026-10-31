"""Freeze a research scope from explicit exported OM windows and chain originals."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from domain.domain.option_lifecycle import SUPPORTED_LAST_TRADE_SOURCES
from domain.domain.symbol_identity import OPTION_CODE_RE, canonical_symbol
from domain.domain.symbol_identity import resolve_symbol_identity
from domain.domain.trade_execution import _parse_futu_option_code
from src.application.opend_fetch_config import DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT
from src.application.opend_normalize import normalize_opend_option_type
from src.application.research import observation_series as series
from src.application.research import option_quote_capture as quote
from src.application.source_receipts import validate_source_receipt

INPUT_SCHEMA = "research.watchlist-scope-input.v1"
MANIFEST_SCHEMA = "research.watchlist-scope.v1"
TRACKING_SCHEMA = "research.watchlist-tracking.v1"
SIMULATION_TRACKING_SCHEMA = "research.watchlist-tracking.v2"
SCAN_INPUT_SCHEMA = "research.watchlist-scan-input.v1"
DISCOVERY_INPUT_SCHEMA = "research.watchlist-scan-input.v2"
REDUNDANCY = Decimal("0.05")


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("positive finite numeric strike required")
    return Decimal(str(value))


def _underlier(value):
    if not isinstance(value, str) or not re.fullmatch(r"HK\.[0-9]{5}|US\.[A-Z][A-Z0-9.-]*", value):
        raise ValueError("explicit provider underlier code required")
    return value


def _reference(root, ref, asof):
    series._keys(ref, {"path", "sha256", "pointer", "observed_at_utc"}, "source reference")
    if series._utc(ref["observed_at_utc"]) > asof:
        raise ValueError("source was not available at scope time")
    path = series._file(root, ref["path"])
    raw, value = series._read(path)
    if quote._sha256(raw) != ref["sha256"]:
        raise ValueError("source SHA mismatch")
    pointer = ref["pointer"]
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("JSON pointer required")
    for token in pointer.split("/")[1:] if pointer else []:
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid JSON pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise ValueError("invalid list index")
                value = value[int(token)]
            else:
                value = value[token]
        except (IndexError, KeyError, TypeError) as exc:
            raise ValueError("source pointer missing") from exc
    return value, raw


def _contract(value):
    code, owner = value["option_code"], _underlier(value["underlier_code"])
    match = OPTION_CODE_RE.fullmatch(code) if isinstance(code, str) else None
    if match is None or not owner.startswith(match.group("market") + "."):
        raise ValueError("contract/underlier market mismatch")
    resolved = canonical_symbol(code)
    if resolved is not None and resolved != canonical_symbol(owner):
        raise ValueError("contract/underlier identity mismatch")
    return code, owner


def _retired(record, asof):
    cutoff = record.get("last_trade")
    if cutoff is None:
        return False
    series._keys(cutoff, {"cutoff_ms", "source", "observed_at_utc"}, "last trade")
    if (
        type(cutoff["cutoff_ms"]) is not int
        or cutoff["cutoff_ms"] <= 0
        or not isinstance(cutoff["source"], str)
        or cutoff["source"] not in SUPPORTED_LAST_TRADE_SOURCES
        or series._utc(cutoff["observed_at_utc"]) > asof
    ):
        raise ValueError("unverified last trade cutoff")
    return asof.timestamp() * 1000 >= cutoff["cutoff_ms"]


def _scan_input(root, payload, asof):
    """Adapt sealed market-only receipts; leave incomplete scan discovery explicit."""
    series._keys(
        payload,
        {"schema_version", "scope_id", "as_of_utc", "fx_pairs", "watchlist", "captures", "previous_tracking"}
        | ({"discoveries"} if payload["schema_version"] == DISCOVERY_INPUT_SCHEMA else set())
        | ({"simulation_tracking"} if "simulation_tracking" in payload else set()),
        "scan input",
    )
    originals, gaps = {}, []

    def receipt(ref, schema):
        value, raw = _reference(root, ref, asof)
        if ref["pointer"] != "":
            raise ValueError("scan receipt must reference whole original")
        validated = validate_source_receipt(
            value,
            producer_root=root,
            now=asof,
            require_fresh=False,
            expected_source_kind="research_supplemental",
        )["receipt"]
        if validated["producer_schema_version"] != schema or series._utc(ref["observed_at_utc"]) != series._utc(
            validated["completed_at"]
        ):
            raise ValueError("scan receipt schema/availability mismatch")
        raw_payload, body = series._read(series._file(root, validated["payload_relpath"]))
        if body.get("schema_version") != schema:
            raise ValueError("scan payload schema mismatch")
        for name, content in [(ref["path"], raw), (validated["payload_relpath"], raw_payload)]:
            if name in {"input.json", "derived-input.json", "tracking.json", "watchlist-scope.json"} or name.startswith(
                "batches/"
            ):
                raise ValueError("scan original conflicts with scope output")
            if name in originals and originals[name] != content:
                raise ValueError("conflicting scan original")
            originals[name] = content
        return validated, body

    roster, roster_body = receipt(payload["watchlist"], "research.watchlist-plan.v1")
    symbols = {}
    for index, entry in enumerate(roster_body["plan"]["symbols"]):
        identity = resolve_symbol_identity(entry["symbol"])
        if identity is None or identity.market not in {"HK", "US"}:
            raise ValueError("scan roster symbol invalid")
        # Multiple owner endpoints/windows may monitor the same underlier.
        symbols.setdefault(identity.futu_code, {"underlier_code": identity.futu_code, "owner_windows": [], "chain": []})
        if entry.get("fetch_plan") is not None:
            symbols[identity.futu_code]["owner_windows"].append(
                {
                    "path": roster["payload_relpath"],
                    "sha256": roster["payload_sha256"],
                    "pointer": f"/plan/symbols/{index}/fetch_plan",
                    "observed_at_utc": roster["completed_at"],
                }
            )
    if not isinstance(payload["captures"], list):
        raise ValueError("scan roster/capture list invalid")
    seen_receipts = set()
    for ref in payload["captures"]:
        source, body = receipt(ref, "research.scan-prescreen.v1")
        if source["snapshot_id"] in seen_receipts:
            raise ValueError("duplicate scan capture")
        seen_receipts.add(source["snapshot_id"])
        owner = body["underlier_code"]
        if (
            owner not in symbols
            or source["producer_run_id"] != roster["producer_run_id"]
            or canonical_symbol(body["symbol"]) != canonical_symbol(owner)
        ):
            raise ValueError("scan capture outside frozen roster/run")
        capture = body["capture"]
        if capture["underlier_code"] != owner or series._utc(capture["completed_at_utc"]) != series._utc(
            source["completed_at"]
        ):
            raise ValueError("scan capture identity/time mismatch")
        base = {
            "path": source["payload_relpath"],
            "sha256": source["payload_sha256"],
            "observed_at_utc": source["completed_at"],
        }
        if body["fetch_plan"] is not None:
            symbols[owner]["owner_windows"].append({**base, "pointer": "/fetch_plan"})
        if capture["chain"] is not None:
            symbols[owner]["chain"].append({**base, "pointer": "/capture/chain/rows"})
        else:
            gaps.append(f"{owner}:chain_not_captured")
    discoveries = payload.get("discoveries", [])
    if not isinstance(discoveries, list):
        raise ValueError("discovery receipt list required")
    seen_discoveries = set()
    for ref in discoveries:
        source, body = receipt(ref, "research.watchlist-discovery.v1")
        owner = body["underlier_code"]
        if owner not in symbols or owner in seen_discoveries or source["producer_run_id"] != roster["producer_run_id"]:
            raise ValueError("discovery outside frozen roster/run or duplicate")
        seen_discoveries.add(owner)
        if (
            body["min_dte"] != 0
            or body["max_dte"] != 90
            or series._utc(body["completed_at_utc"]) != series._utc(source["completed_at"])
            or body["status"] not in {"complete", "incomplete"}
        ):
            raise ValueError("discovery scope/time/status mismatch")
        # Keep scan rows too: partial discovery must not erase already observed contracts.
        symbols[owner]["chain"].append(
            {
                "path": source["payload_relpath"],
                "sha256": source["payload_sha256"],
                "pointer": "/chain/rows",
                "observed_at_utc": source["completed_at"],
            }
        )
        if body["status"] != "complete":
            gaps.append(f"{owner}:independent_discovery_incomplete")
    if payload["schema_version"] == DISCOVERY_INPUT_SCHEMA:
        gaps.extend(f"{owner}:independent_discovery_not_attempted" for owner in symbols.keys() - seen_discoveries)
    for owner, symbol in symbols.items():
        if not symbol["owner_windows"]:
            gaps.append(f"{owner}:owner_window_missing")
        if not symbol["chain"]:
            gaps.append(f"{owner}:chain_not_captured")
    normalized = {k: payload[k] for k in ("scope_id", "as_of_utc", "fx_pairs", "previous_tracking")}
    normalized.update(schema_version=INPUT_SCHEMA, symbols=list(symbols.values()))
    if "simulation_tracking" in payload:
        normalized["simulation_tracking"] = payload["simulation_tracking"]
    originals["derived-input.json"] = series._bytes(normalized)
    return normalized, originals, gaps


def build_watchlist_scope(*, input_file: Path, expected_input_sha256: str, output_dir: Path | None = None):
    """Preview without writes/provider calls; publishing never overwrites an output."""
    input_file = series._path(input_file)
    raw, payload = series._read(input_file)
    if quote._sha256(raw) != expected_input_sha256:
        raise ValueError("external input SHA mismatch")
    native = payload.get("schema_version") in {SCAN_INPUT_SCHEMA, DISCOVERY_INPUT_SCHEMA}
    native_originals, native_gaps = {}, []
    if native:
        payload, native_originals, native_gaps = _scan_input(
            input_file.parent, payload, series._utc(payload["as_of_utc"])
        )
    series._keys(
        payload,
        {"schema_version", "scope_id", "as_of_utc", "fx_pairs", "symbols", "previous_tracking"}
        | ({"simulation_tracking"} if "simulation_tracking" in payload else set()),
        "scope input",
    )
    if (
        payload["schema_version"] != INPUT_SCHEMA
        or not isinstance(payload["scope_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", payload["scope_id"])
    ):
        raise ValueError("scope input identity invalid")
    asof = series._utc(payload["as_of_utc"])
    fx = payload["fx_pairs"]
    if (
        not isinstance(fx, list)
        or not fx
        or any(not isinstance(x, str) or x not in {"HKDCNY", "USDCNY"} for x in fx)
        or len(set(fx)) != len(fx)
    ):
        raise ValueError("explicit unique native FX pairs required")
    if not isinstance(payload["symbols"], list):
        raise ValueError("explicit monitored symbols required")
    originals = {"input.json": raw, **native_originals}
    gaps, windows, tracking, retired, seen_symbols = list(native_gaps), [], {}, [], set()
    previous = payload["previous_tracking"]
    obligations = None
    if previous is not None:
        prior, source = _reference(input_file.parent, previous, asof)
        originals["sources/" + previous["sha256"] + ".json"] = source
        series._keys(
            prior,
            {"schema_version", "as_of_utc", "contracts"}
            | ({"simulation_tracking"} if prior.get("schema_version") == SIMULATION_TRACKING_SCHEMA else set()),
            "previous tracking",
        )
        if prior["schema_version"] not in {TRACKING_SCHEMA, SIMULATION_TRACKING_SCHEMA} or not isinstance(
            prior["contracts"], list
        ):
            raise ValueError("previous tracking schema invalid")
        if series._utc(prior["as_of_utc"]) > asof:
            raise ValueError("future tracking state")
        seen_previous = set()
        for record in prior["contracts"]:
            series._keys(
                record, {"option_code", "underlier_code", "first_seen_at_utc", "last_trade"}, "tracked contract"
            )
            code, owner = _contract(record)
            if series._utc(record["first_seen_at_utc"]) > series._utc(prior["as_of_utc"]):
                raise ValueError("tracking first seen after tracking snapshot")
            if code in seen_previous:
                raise ValueError("duplicate previous tracking contract")
            seen_previous.add(code)
            if _retired(record, asof):
                retired.append(code)
            else:
                tracking[code] = dict(record)
        obligations = prior.get("simulation_tracking")
        if obligations is not None and series._utc(obligations["as_of_utc"]) > series._utc(prior["as_of_utc"]):
            raise ValueError("simulation export was not available at previous scope time")
    # Simulation obligations are a full current snapshot, never a production ledger read.
    simulation_stocks, simulation_codes = set(), set()
    new_codes = set()
    if "simulation_tracking" in payload:
        ref = payload["simulation_tracking"]
        obligations, source = _reference(input_file.parent, ref, asof)
        if ref["pointer"] != "":
            raise ValueError("simulation tracking must reference a whole export")
        originals["sources/" + ref["sha256"] + ".json"] = source
    if obligations is not None:
        series._keys(
            obligations,
            {
                "schema_version",
                "as_of_utc",
                "report_sha256",
                "state_pointer",
                "identities_sha256",
                "contracts",
                "stocks",
            },
            "simulation tracking",
        )
        if (
            obligations["schema_version"] != "optionbench.simulation-tracking.v1"
            or series._utc(obligations["as_of_utc"]) > asof
        ):
            raise ValueError("simulation tracking schema/time invalid")
        for field in ("report_sha256", "identities_sha256"):
            if not isinstance(obligations[field], str) or not re.fullmatch(r"[0-9a-f]{64}", obligations[field]):
                raise ValueError("simulation provenance digest required")
        if not isinstance(obligations["state_pointer"], str) or not obligations["state_pointer"].startswith("/"):
            raise ValueError("simulation state pointer required")
        if not isinstance(obligations["contracts"], list) or not isinstance(obligations["stocks"], list):
            raise ValueError("simulation obligations must be arrays")
        for record in obligations["contracts"]:
            series._keys(record, {"option_code", "underlier_code"}, "simulation contract")
            code, owner = _contract(record)
            if code in simulation_codes or code in retired:
                raise ValueError("duplicate/retired simulation contract")
            simulation_codes.add(code)
            if code in tracking and tracking[code]["underlier_code"] != owner:
                raise ValueError("simulation contract owner conflict")
            if code not in tracking:
                new_codes.add(code)
                tracking[code] = {**record, "first_seen_at_utc": payload["as_of_utc"], "last_trade": None}
        for value in obligations["stocks"]:
            code = _underlier(value)
            if code in simulation_stocks:
                raise ValueError("duplicate simulation stock")
            simulation_stocks.add(code)
    for symbol in payload["symbols"]:
        series._keys(symbol, {"underlier_code", "owner_windows", "chain"}, "symbol")
        owner = _underlier(symbol["underlier_code"])
        if owner in seen_symbols:
            raise ValueError("duplicate monitored symbol")
        seen_symbols.add(owner)
        market = owner.split(".")[0]
        local_date = asof.astimezone(ZoneInfo("Asia/Hong_Kong" if market == "HK" else "America/New_York")).date()
        if native and not symbol["owner_windows"]:
            continue
        if not isinstance(symbol["owner_windows"], list) or not symbol["owner_windows"]:
            raise ValueError("explicit exported OM window references required")
        bands = {"put": [], "call": []}
        for ref in symbol["owner_windows"]:
            plan, source = _reference(input_file.parent, ref, asof)
            originals["sources/" + ref["sha256"] + ".json"] = source
            if not isinstance(plan, dict) or canonical_symbol(plan.get("symbol")) != canonical_symbol(owner):
                raise ValueError("OM window bundle symbol mismatch")
            if not isinstance(plan.get("side_plans"), list) or not plan["side_plans"]:
                raise ValueError("OM bundle side plans required")
            for side_plan in plan["side_plans"]:
                side = side_plan.get("option_type") if isinstance(side_plan, dict) else None
                if not isinstance(side, str) or side not in bands:
                    raise ValueError("ordinary Put/Call window required")
                window = side_plan.get("strike_window")
                if not isinstance(window, dict):
                    raise ValueError("OM strike window required")
                low, high = _number(window.get("min_strike")), _number(window.get("max_strike"))
                if high < low:
                    raise ValueError("infeasible OM window")
                expanded = (low * (1 - REDUNDANCY), high * (1 + REDUNDANCY))
                bands[side].append(expanded)
                windows.append(
                    {
                        "underlier_code": owner,
                        "option_type": side,
                        "source": ref,
                        "base_min": float(low),
                        "base_max": float(high),
                        "min_strike": float(expanded[0]),
                        "max_strike": float(expanded[1]),
                    }
                )
        for side in bands:
            if not bands[side]:
                gaps.append(f"{owner}:{side}:owner_window_missing")
        chains = symbol["chain"] if native else [symbol["chain"]]
        if not chains:
            continue
        chain, across = [], {}
        for ref in chains:
            part, source = _reference(input_file.parent, ref, asof)
            originals["sources/" + ref["sha256"] + ".json"] = source
            if not isinstance(part, list):
                raise ValueError("chain reference must point to original provider rows")
            within = set()
            for row in part:
                if not isinstance(row, dict) or not isinstance(row.get("code"), str) or row["code"] in within:
                    raise ValueError("invalid/duplicate chain row")
                within.add(row["code"])
                identity = tuple(row.get(k) for k in ("stock_owner", "option_type", "strike_price", "strike_time"))
                if row["code"] in across:
                    if across[row["code"]] != identity:
                        raise ValueError("conflicting scan chain identity")
                    continue
                across[row["code"]] = identity
                chain.append(row)
        # Original rows prove observed contracts, not complete exchange-chain discovery.
        gaps.append(f"{owner}:chain_coverage_unverified")
        seen_rows = set()
        for row in chain:
            if (
                not isinstance(row, dict)
                or not {"code", "stock_owner", "option_type", "strike_price", "strike_time"} <= row.keys()
            ):
                raise ValueError("chain row fields missing")
            code = row["code"]
            _contract({"option_code": code, "underlier_code": owner})
            if code in seen_rows:
                raise ValueError("duplicate chain contract")
            seen_rows.add(code)
            if row.get("stock_owner") != owner:
                raise ValueError("chain owner mismatch")
            side = normalize_opend_option_type(row["option_type"])
            if side not in bands:
                raise ValueError("unknown chain option type")
            strike = _number(row["strike_price"])
            expiry = date.fromisoformat(row["strike_time"])
            identity = _parse_futu_option_code(code)
            if (
                identity["option_type"] != side
                or identity["expiration_ymd"] != expiry.isoformat()
                or Decimal(str(identity["strike"])) != strike
            ):
                raise ValueError("chain fields contradict contract identity")
            dte = (expiry - local_date).days
            if 0 <= dte <= 90 and any(low <= strike <= high for low, high in bands[side]):
                if code in retired:
                    raise ValueError("chain selected contract past authoritative last trade")
                existing = tracking.get(code)
                if existing and existing["underlier_code"] != owner:
                    raise ValueError("conflicting contract owner")
                if not existing:
                    tracking[code] = {
                        "option_code": code,
                        "underlier_code": owner,
                        "first_seen_at_utc": payload["as_of_utc"],
                        "last_trade": None,
                    }
                    new_codes.add(code)
    batches, counts = [], {}
    for market in ("HK", "US"):
        contracts = sorted(
            (x for x in tracking.values() if x["option_code"].startswith(market + ".")), key=lambda x: x["option_code"]
        )
        extra_stocks = sorted(
            code
            for code in simulation_stocks
            if code.startswith(market + ".") and code not in {x["underlier_code"] for x in contracts}
        )
        counts[market] = {
            "contracts": len(contracts),
            "option_snapshot_batches": math.ceil(len(contracts) / DEFAULT_OPEND_BATCH_MARKET_SNAPSHOT),
            "underliers": len({x["underlier_code"] for x in contracts} | set(extra_stocks)),
        }
        for start in range(0, len(contracts), quote.MAX_OPTION_CODES):
            batch_id = f"{market.lower()}-{start // quote.MAX_OPTION_CODES:04d}"
            scope = {
                "scope_id": payload["scope_id"] + "-" + batch_id,
                "fx_pairs": sorted(fx),
                "contracts": [
                    {k: x[k] for k in ("option_code", "underlier_code")}
                    for x in contracts[start : start + quote.MAX_OPTION_CODES]
                ],
            }
            originals[f"batches/{batch_id}/scope.json"] = series._bytes(scope)
            batches.append(
                {"batch_id": batch_id, "path": f"batches/{batch_id}/scope.json", "contracts": len(scope["contracts"])}
            )
        for start in range(0, len(extra_stocks), quote.MAX_OPTION_CODES):
            batch_id = f"{market.lower()}-s{start // quote.MAX_OPTION_CODES:04d}"
            scope = {
                "scope_id": payload["scope_id"] + "-" + batch_id,
                "fx_pairs": sorted(fx),
                "contracts": [],
                "underlier_codes": extra_stocks[start : start + quote.MAX_OPTION_CODES],
            }
            originals[f"batches/{batch_id}/scope.json"] = series._bytes(scope)
            batches.append({"batch_id": batch_id, "path": f"batches/{batch_id}/scope.json", "contracts": 0})
    state = {
        "schema_version": SIMULATION_TRACKING_SCHEMA if obligations is not None else TRACKING_SCHEMA,
        "as_of_utc": payload["as_of_utc"],
        "contracts": sorted(tracking.values(), key=lambda x: x["option_code"]),
    }
    if obligations is not None:
        state["simulation_tracking"] = obligations
    originals["tracking.json"] = series._bytes(state)
    result = {
        "schema_version": MANIFEST_SCHEMA,
        "scope_id": payload["scope_id"],
        "as_of_utc": payload["as_of_utc"],
        "input_sha256": expected_input_sha256,
        "redundancy": "0.05",
        "min_dte": 0,
        "max_dte": 90,
        "status": "incomplete",
        "missing_reasons": sorted(set(gaps)),
        "provider_called": False,
        "windows": sorted(windows, key=lambda x: (x["underlier_code"], x["option_type"], x["min_strike"])),
        "counts": counts,
        "new_contracts": len(new_codes),
        "retired_codes": sorted(retired),
        "batches": batches,
        "files": [
            {"path": name, "sha256": quote._sha256(content), "size": len(content)}
            for name, content in sorted(originals.items())
        ],
        "capacity_note": "Snapshot batch counts exclude discovery, retries, shared quota and duration; not deployment approval.",
    }
    if output_dir is not None:
        series._outside_checkout(series._path(output_dir))
        out = series._private_dir(output_dir, fresh=True)
        for name, content in originals.items():
            dest = out / name
            series._private_dir(dest.parent)
            quote._write_once(dest, content)
        quote._write_once(out / "watchlist-scope.json", series._bytes(result))
    return result
