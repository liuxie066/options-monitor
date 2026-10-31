"""Capture current option snapshots for one explicit, immutable research observation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from domain.domain.symbol_identity import OPTION_CODE_RE
from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.application.opend_market_snapshot_fetching import fetch_option_snapshots
from src.application.payload_helpers import canonical_json_bytes_lines
from src.application.source_receipts import publish_source_receipt
from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway

PLAN_SCHEMA = "research.option-quote-capture-plan.v1"
QUOTES_SCHEMA = "research.option-quote-capture.v1"
MANIFEST_SCHEMA = "research.option-quote-capture-manifest.v1"
MAX_OBSERVATION_LAG_SECONDS = 30 * 60
MAX_OPTION_CODES = 200
SNAPSHOT_LIMIT = OpenDEndpointRateLimit(window_sec=30, max_calls=10, max_wait_sec=30)
PRODUCER_POLICY = b"research.option-quote-capture.v1|fetch_option_snapshots|market_snapshot"
IMPLEMENTATION_SOURCES = (
    "domain/domain/decision_state_fingerprint.py",
    "domain/domain/expiration_dates.py",
    "domain/domain/money.py",
    "domain/domain/option_position_identity.py",
    "domain/domain/symbol_identity.py",
    "domain/domain/trade_contract_identity.py",
    "src/application/opend_call_coordinator.py",
    "src/application/opend_fetch_config.py",
    "src/application/opend_market_snapshot_fetching.py",
    "src/application/opend_normalize.py",
    "src/application/opend_rate_gate.py",
    "src/application/opend_utils.py",
    "src/application/opening_quote_evidence.py",
    "src/application/option_chain_fetching.py",
    "src/application/expiration_normalization.py",
    "src/application/payload_helpers.py",
    "src/application/research/option_quote_capture.py",
    "src/application/source_receipts.py",
    "src/application/symbol_aliases.py",
    "src/infrastructure/futu_dataframe.py",
    "src/infrastructure/futu_gateway.py",
    "src/infrastructure/io_utils.py",
    "src/infrastructure/net_port.py",
    "src/infrastructure/opend_retcodes.py",
)
REMAINING = [
    "historical_bid_ask_not_backfillable_by_snapshot",
    "fixed_scope_requires_explicit_future_observations_until_contract_termination",
    "provider_quota_is_shared_with_other_opend_consumers",
]


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    return parsed.astimezone(timezone.utc)


def validate_option_quote_capture_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or set(plan) != {
        "schema_version",
        "scope_id",
        "observation_at_utc",
        "option_codes",
    }:
        raise ValueError("option quote capture plan fields invalid")
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise ValueError("unsupported option quote capture plan")
    scope_id = plan.get("scope_id")
    if not isinstance(scope_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", scope_id) is None:
        raise ValueError("explicit bounded scope_id required")
    _parse_utc(plan.get("observation_at_utc"), "observation_at_utc")
    codes = plan.get("option_codes")
    if (
        not isinstance(codes, list)
        or not codes
        or len(codes) > MAX_OPTION_CODES
        or any(not isinstance(code, str) or code.strip() != code for code in codes)
        or len(codes) != len(set(codes))
    ):
        raise ValueError("explicit unique bounded option_codes required")
    for code in codes:
        match = OPTION_CODE_RE.fullmatch(code)
        if match is None or match.group("market") not in {"US", "HK"}:
            raise ValueError("option code identity invalid")
    return plan


def _validate_observation_window(plan: Mapping[str, Any], now_utc: datetime) -> datetime:
    observation = _parse_utc(plan["observation_at_utc"], "observation_at_utc")
    if observation > now_utc:
        raise ValueError("observation_at_utc is in the future")
    if now_utc - observation > timedelta(seconds=MAX_OBSERVATION_LAG_SECONDS):
        raise ValueError("observation_at_utc is stale; historical snapshots cannot be captured")
    return observation


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    scalar = getattr(value, "item", None)
    if callable(scalar):
        return _json_safe(scalar())
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    raise ValueError(f"unsupported provider value: {type(value).__name__}")


def _write_once(path: Path, content: bytes) -> str:
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
    return hashlib.sha256(content).hexdigest()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _implementation_sha256(root: Path | None = None) -> str:
    repository_root = root or Path(__file__).resolve().parents[3]
    sources = [
        {
            "path": relpath,
            "sha256": _sha256((repository_root / relpath).read_bytes()),
        }
        for relpath in IMPLEMENTATION_SOURCES
    ]
    return _sha256(
        canonical_json_bytes_lines(
            {
                "policy": PRODUCER_POLICY.decode("utf-8"),
                "sources": sources,
            }
        )
    )


def _missing_quote_fields(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    missing = {}
    for row in rows:
        gaps = []
        bid = row.get("bid_price", row.get("bid"))
        ask = row.get("ask_price", row.get("ask"))
        if isinstance(bid, bool) or not isinstance(bid, (int, float)) or not math.isfinite(bid) or bid < 0:
            gaps.append("bid")
        if isinstance(ask, bool) or not isinstance(ask, (int, float)) or not math.isfinite(ask) or ask <= 0:
            gaps.append("ask")
        elif not gaps and ask < bid:
            gaps.append("ask")
        if gaps:
            missing[str(row.get("code") or "")] = gaps
    return missing


def _sanitize_provider_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = {
        "stage",
        "batch_start",
        "batch_size",
        "error_code",
        "error_type",
        "missing_codes",
        "unexpected_codes",
        "duplicate_codes",
    }
    return [
        {key: _json_safe(error[key]) for key in fields if key in error}
        for error in errors
        if isinstance(error, Mapping)
    ]


def _write_terminal_manifest(
    *,
    out: Path,
    plan: Mapping[str, Any],
    host: str,
    port: int,
    execution_error_type: str,
    implementation_sha256: str,
    terminal_manifest: Mapping[str, Any] | None = None,
) -> None:
    files = {}
    plan_bytes = canonical_json_bytes_lines(plan)
    plan_path = out / "plan.json"
    if plan_path.is_file() and plan_path.read_bytes() == plan_bytes:
        files["plan.json"] = _sha256(plan_bytes)
    for name in ("quotes.json", "receipt.json"):
        path = out / name
        if path.is_file():
            files[name] = _sha256(path.read_bytes())

    summary = dict(terminal_manifest or {})
    if not summary:
        try:
            archived_quotes = json.loads((out / "quotes.json").read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            archived_quotes = None
        if (
            isinstance(archived_quotes, Mapping)
            and archived_quotes.get("schema_version") == QUOTES_SCHEMA
            and archived_quotes.get("scope_id") == plan["scope_id"]
            and archived_quotes.get("observation_at_utc") == plan["observation_at_utc"]
            and archived_quotes.get("requested_codes") == list(plan["option_codes"])
        ):
            summary = dict(archived_quotes)
    try:
        receipt = json.loads((out / "receipt.json").read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        receipt = None
    source_snapshot_id = summary.get("source_snapshot_id")
    if (
        not source_snapshot_id
        and isinstance(receipt, Mapping)
        and receipt.get("completed") is True
        and receipt.get("payload_sha256") == files.get("quotes.json")
    ):
        source_snapshot_id = receipt.get("snapshot_id")

    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "status": "incomplete",
        "complete": False,
        "scope_id": plan["scope_id"],
        "observation_at_utc": plan["observation_at_utc"],
        "requested_codes": list(plan["option_codes"]),
        "returned_codes": summary.get("returned_codes", []),
        "missing_codes": summary.get("missing_codes", list(plan["option_codes"])),
        "unexpected_codes": summary.get("unexpected_codes", []),
        "duplicate_codes": summary.get("duplicate_codes", []),
        "missing_quote_fields": summary.get("missing_quote_fields", {}),
        "source_kind": "research_supplemental",
        "source_snapshot_id": source_snapshot_id,
        "provider": "futu_opend",
        "endpoint": {"host": host, "port": port},
        "provider_quota": "shared_with_other_opend_consumers",
        "provider_requested_at_utc": summary.get("provider_requested_at_utc"),
        "provider_received_at_utc": summary.get("provider_received_at_utc"),
        "execution_error_type": execution_error_type,
        "implementation_sha256": implementation_sha256,
        "files": files,
        "remaining": list(REMAINING),
    }
    path = out / "manifest.json"
    try:
        _write_once(path, canonical_json_bytes_lines(manifest))
    except FileExistsError:
        return


def collect_option_quotes(
    plan: Any,
    *,
    output_dir: Path,
    host: str,
    port: int,
    confirm: bool = False,
    gateway_factory=None,
    fetch_snapshots_fn=None,
    now_utc: datetime | None = None,
    snapshot_limiter_root: Path | None = None,
    rate_limited_call=None,
):
    plan = validate_option_quote_capture_plan(plan)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    observation = _validate_observation_window(plan, now)
    if not confirm:
        return {"status": "preview", "plan": plan, "provider_called": False}
    if not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("explicit quote endpoint required")

    implementation_sha256 = _implementation_sha256()
    out = Path(output_dir)
    limiter_root = snapshot_limiter_root if snapshot_limiter_root is not None else out / "limiter"
    plan_hash = None
    try:
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        plan_bytes = canonical_json_bytes_lines(plan)
        plan_hash = _write_once(out / "plan.json", plan_bytes)
        limiter_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        if out.is_dir():
            _write_terminal_manifest(
                out=out,
                plan=plan,
                host=host,
                port=port,
                execution_error_type=type(exc).__name__,
                implementation_sha256=implementation_sha256,
            )
        raise
    gateway = None
    result = None
    execution_error = None
    cancelled = None
    final_manifest = None
    try:
        gateway = (gateway_factory or build_ready_futu_quote_gateway)(host=host, port=port)
        fetcher = fetch_snapshots_fn or fetch_option_snapshots
        result = fetcher(
            option_codes=plan["option_codes"],
            gateway=gateway,
            snapshot_limit=SNAPSHOT_LIMIT,
            base_dir=limiter_root,
            **({"rate_limited_call": rate_limited_call} if rate_limited_call is not None else {}),
        )
    except Exception as exc:
        execution_error = type(exc).__name__
    except BaseException as exc:
        result = getattr(exc, "partial_result", None)
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

    requested_codes = list(plan["option_codes"])
    provider_called = result is not None and (result.opend_call_count > 0 or bool(result.snap_map))
    if result is None or not provider_called:
        returned_codes = []
        missing_codes = requested_codes
        unexpected_codes = []
        duplicate_codes = []
        provider_rows = []
        missing_quote_fields = {}
        provider_errors = [
            {
                "stage": "collector",
                "error_code": "COLLECTOR_FAILED" if execution_error else "PROVIDER_NOT_CALLED",
                "error_type": execution_error,
            }
        ]
        provider_requested_at = None
        provider_received_at = None
        batch_receipts = [
            {
                "stage": "collector",
                "batch_start": 0,
                "requested_codes": requested_codes,
                "provider_call_started": False,
                "requested_at_utc": None,
                "received_at_utc": None,
                "status": "failed",
                "error_code": provider_errors[0]["error_code"],
            }
        ]
        complete = False
    else:
        returned_codes = sorted(result.snap_map.keys() & requested_codes)
        missing_codes = sorted(result.missing_codes)
        unexpected_codes = sorted(result.unexpected_codes)
        duplicate_codes = sorted(
            {
                code
                for error in result.errors
                if error.get("error_code") == "SNAPSHOT_DUPLICATE_CODES"
                for code in error.get("duplicate_codes", [])
            }
        )
        provider_rows = [result.snap_map[code] for code in requested_codes if code in result.snap_map]
        missing_quote_fields = _missing_quote_fields(provider_rows)
        provider_errors = _sanitize_provider_errors(list(result.errors))
        batch_receipts = list(result.batch_receipts)
        provider_requested_at = result.requested_at_utc
        provider_received_at = result.received_at_utc
        timing_valid = False
        if provider_requested_at and provider_received_at:
            try:
                provider_requested = _parse_utc(provider_requested_at, "provider_requested_at_utc")
                provider_received = _parse_utc(provider_received_at, "provider_received_at_utc")
                batch_times = [
                    (
                        _parse_utc(batch.get("requested_at_utc"), "batch requested_at_utc"),
                        _parse_utc(batch.get("received_at_utc"), "batch received_at_utc"),
                    )
                    for batch in batch_receipts
                    if batch.get("provider_call_started") is True
                ]
                batch_time_shape_valid = all(
                    isinstance(batch.get("provider_call_started"), bool)
                    and (
                        batch["provider_call_started"]
                        or (batch.get("requested_at_utc") is None and batch.get("received_at_utc") is None)
                    )
                    for batch in batch_receipts
                )
                row_times = [
                    (
                        _parse_utc(row.get("snapshot_requested_at_utc"), "provider row requested_at_utc"),
                        _parse_utc(row.get("snapshot_received_at_utc"), "provider row received_at_utc"),
                    )
                    for row in provider_rows
                ]
                observation_deadline = observation + timedelta(seconds=MAX_OBSERVATION_LAG_SECONDS)
                timing_valid = (
                    batch_time_shape_valid
                    and provider_received >= provider_requested
                    and all(
                        observation <= requested <= observation_deadline and received >= requested
                        for requested, received in [*batch_times, *row_times]
                    )
                )
            except (TypeError, ValueError):
                timing_valid = False
        complete = (
            result.complete
            and not duplicate_codes
            and not missing_quote_fields
            and execution_error is None
            and timing_valid
        )

    status = "complete" if complete else "incomplete"
    try:
        quotes = {
            "schema_version": QUOTES_SCHEMA,
            "source_kind": "research_supplemental",
            "scope_id": plan["scope_id"],
            "observation_at_utc": plan["observation_at_utc"],
            "status": status,
            "complete": complete,
            "requested_codes": requested_codes,
            "returned_codes": returned_codes,
            "missing_codes": missing_codes,
            "unexpected_codes": unexpected_codes,
            "duplicate_codes": duplicate_codes,
            "missing_quote_fields": missing_quote_fields,
            "provider_rows": _json_safe(provider_rows),
            "provider_errors": _json_safe(provider_errors),
            "batch_receipts": _json_safe(batch_receipts),
            "provider_requested_at_utc": provider_requested_at,
            "provider_received_at_utc": provider_received_at,
            "remaining": list(REMAINING),
        }
        quotes_bytes = canonical_json_bytes_lines(quotes)
        quotes_hash = _sha256(quotes_bytes)
        receipt = None
        receipt_hash = None
        if provider_called and provider_requested_at and provider_received_at:
            markets = sorted({OPTION_CODE_RE.fullmatch(code).group("market") for code in requested_codes})
            receipt = publish_source_receipt(
                private_files=True,
                producer_root=out,
                receipt_relpath="receipt.json",
                payload_relpath="quotes.json",
                payload_bytes=quotes_bytes,
                source_kind="research_supplemental",
                producer_schema_version=QUOTES_SCHEMA,
                producer_run_id=f"option-quote-capture:{plan_hash[:24]}",
                broker="futu",
                included_markets=markets,
                source_native_id=f"{plan['scope_id']}:{provider_requested_at}",
                source_observed_at=provider_requested_at,
                completed_at=provider_received_at,
                producer_policy_hash=implementation_sha256,
            )
            receipt_hash = _sha256((out / "receipt.json").read_bytes())
        else:
            _write_once(out / "quotes.json", quotes_bytes)
        files = {"plan.json": plan_hash, "quotes.json": quotes_hash}
        if receipt is not None:
            files.update(
                {
                    "quotes.json": receipt["payload_sha256"],
                    "receipt.json": receipt_hash,
                }
            )
        final_manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "status": status,
            "complete": complete,
            "scope_id": plan["scope_id"],
            "observation_at_utc": plan["observation_at_utc"],
            "requested_codes": requested_codes,
            "returned_codes": returned_codes,
            "missing_codes": missing_codes,
            "unexpected_codes": unexpected_codes,
            "duplicate_codes": duplicate_codes,
            "missing_quote_fields": missing_quote_fields,
            "source_kind": "research_supplemental",
            "source_snapshot_id": receipt["snapshot_id"] if receipt is not None else None,
            "provider": "futu_opend",
            "endpoint": {"host": host, "port": port},
            "provider_quota": "shared_with_other_opend_consumers",
            "provider_requested_at_utc": provider_requested_at,
            "provider_received_at_utc": provider_received_at,
            "execution_error_type": execution_error,
            "implementation_sha256": implementation_sha256,
            "files": files,
            "remaining": list(REMAINING),
        }
        _write_once(out / "manifest.json", canonical_json_bytes_lines(final_manifest))
    except BaseException as exc:
        _write_terminal_manifest(
            out=out,
            plan=plan,
            host=host,
            port=port,
            execution_error_type=type(exc).__name__,
            implementation_sha256=implementation_sha256,
            terminal_manifest=final_manifest,
        )
        raise
    if cancelled is not None:
        raise cancelled
    return final_manifest


__all__ = [
    "MANIFEST_SCHEMA",
    "PLAN_SCHEMA",
    "QUOTES_SCHEMA",
    "collect_option_quotes",
    "validate_option_quote_capture_plan",
]
