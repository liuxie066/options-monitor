from __future__ import annotations

import gzip
import json
import re
import shutil
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.account_config import accounts_from_config
from src.application.agent_tool_config import load_runtime_config
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    utc_timestamp,
)
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    load_opening_candidate_snapshot,
    validate_opening_candidate_snapshot,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
    load_prepared_option_positions_context_receipt,
)
from src.application.recommendation_point import (
    RECOMMENDATION_POINT_SCHEMA_V3,
    RecommendationPointError,
    build_formal_point_time_coherence,
    build_option_position_evidence_binding,
    build_recommendation_point_id,
    validate_option_position_evidence_binding,
    validate_recommendation_point,
)
from src.application.required_data_snapshot import (
    FrozenRequiredDataUnavailable,
    RequiredDataSnapshotError,
    load_required_data_snapshot_manifest_snapshot,
    resolve_frozen_required_data_csv_bytes_batch,
)
from src.application.scan_scheduler import scheduled_scan_targets_for_date
from src.application.source_receipts import sha256_bytes
from src.infrastructure.private_storage import (
    atomic_write_private_bytes,
    atomic_write_private_text,
    ensure_private_directory,
    exclusive_private_file_lock,
    open_private_text,
    private_path,
)


FORMAL_CORPUS_VERSION = "v1"
FORMAL_EXPECTATION_SCHEMA = "formal_day_expectation.v1"
FORMAL_POINT_SCHEMA = "formal_point_attempt.v1"
CORPUS_HEALTH_SCHEMA = "corpus_health_receipt.v2"
FORMAL_POINT_TIME_COHERENCE_SCHEMA = "formal_point_time_coherence.v1"
FORMAL_POINT_MAX_SKEW_MS = 300_000
MARKET_CALENDAR_POINTER_SCHEMA = "sell_put_top1_market_calendar_pointer.v1"
MARKET_CALENDAR_SNAPSHOT_SCHEMA = "sell_put_top1_market_calendar_snapshot.v2"
_MARKET_CALENDAR_SOURCE_RECEIPT_SCHEMA = (
    "sell_put_top1_market_calendar_source_receipt.v1"
)

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MARKET_TIMEZONES = {"HK": "Asia/Hong_Kong", "US": "America/New_York"}
_CALENDAR_POINTER_FIELDS = frozenset(
    {
        "schema_version",
        "market",
        "snapshot_ref",
        "snapshot_content_sha256",
        "snapshot_file_sha256",
        "content_sha256",
    }
)
_CALENDAR_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "market",
        "market_calendar_version",
        "coverage_start",
        "coverage_end",
        "trading_sessions",
        "source_receipt_sha256",
        "observed_at_utc",
        "content_sha256",
    }
)
_CALENDAR_SOURCE_FIELDS = frozenset(
    {"retcode", "rows", "coverage_complete", "pagination_complete", "page_count"}
)
_CALENDAR_SOURCE_ROW_FIELDS = frozenset({"time", "trade_date_type"})
_CALENDAR_SESSION_TYPES = frozenset({"WHOLE", "MORNING", "AFTERNOON"})
_EXPECTATION_FIELDS = frozenset(
    {
        "schema_version",
        "market",
        "account",
        "trading_date",
        "market_calendar_version",
        "market_calendar_sha256",
        "schedule_config_sha256",
        "sealed_at_utc",
        "first_target_at_utc",
        "sealed_before_first_target",
        "scheduled_scan_targets_market",
        "expected_recommendation_point_ids",
        "content_sha256",
    }
)
_POINT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "reason_code",
        "market",
        "account",
        "trading_date",
        "recommendation_point_id",
        "captured_at_utc",
        "source_binding",
        "recommendation_point",
        "opening_snapshot",
        "required_data_symbols",
        "option_position_evidence_binding",
        "content_sha256",
    }
)
_SOURCE_BINDING_FIELDS = frozenset(
    {
        "market",
        "account",
        "run_id",
        "scheduled_scan_target_market",
        "producer_behavior_version",
        "recommendation_point_content_sha256",
        "opening_snapshot_sha256",
        "required_data_manifest_sha256",
        "prepared_context_manifest_sha256",
        "prepared_context_payload_sha256",
    }
)


class FormalCorpusError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(message)


def _fail(reason_code: str, message: str) -> NoReturn:
    raise FormalCorpusError(reason_code, message)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("formal_corpus_input_invalid", f"{label} must be canonical text")
    return value


def _segment(value: Any, label: str) -> str:
    result = _text(value, label)
    if _SEGMENT.fullmatch(result) is None:
        _fail("formal_corpus_input_invalid", f"{label} must be a safe path segment")
    return result


def _identity(market: Any, account: Any) -> tuple[str, str]:
    market_text = _text(market, "market").upper()
    if market_text not in _MARKET_TIMEZONES:
        _fail("formal_corpus_input_invalid", "market must be HK or US")
    account_text = _segment(account, "account")
    if account_text != account_text.lower():
        _fail("formal_corpus_input_invalid", "account must be lowercase")
    return market_text, account_text


def _day(value: Any) -> str:
    text = _text(value, "trading_date")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise FormalCorpusError(
            "formal_corpus_input_invalid", "trading_date must be an ISO date"
        ) from exc
    if parsed.isoformat() != text:
        _fail("formal_corpus_input_invalid", "trading_date must be canonical")
    return text


def _timestamp(value: Any, label: str) -> str:
    try:
        result = utc_timestamp(_text(value, label), label)
    except CandidateSnapshotContractError as exc:
        raise FormalCorpusError("formal_corpus_input_invalid", str(exc)) from exc
    if result != value:
        _fail("formal_corpus_input_invalid", f"{label} must be canonical UTC")
    return result


def _hash(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    text = _text(value, label)
    if _HASH.fullmatch(text) is None:
        _fail("formal_corpus_artifact_invalid", f"{label} must be SHA-256")
    return text


def _render(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _corpus_root(runtime_root: str | Path) -> Path:
    return (
        _runtime_root(runtime_root)
        / "output_shared"
        / "research"
        / "formal_corpus"
        / FORMAL_CORPUS_VERSION
    )


def _runtime_root(value: str | Path) -> Path:
    root = private_path(value)
    if root.parts[-3:] == ("output_shared", "research", "strategy_lab"):
        return root.parents[2]
    return root


def _calendar_market(value: Any) -> str:
    market = _text(value, "market")
    if market not in _MARKET_TIMEZONES:
        _fail("corpus_input_invalid", "market must equal HK or US")
    return market


def _relative_ref(value: Any, label: str) -> str:
    ref = _text(value, label)
    if ref.startswith("/") or "\\" in ref or any(
        part in {"", ".", ".."} for part in ref.split("/")
    ):
        _fail("corpus_input_invalid", f"{label} must be a safe relative POSIX path")
    return ref


def _calendar_pointer_ref(market: str) -> str:
    return f"capabilities/market-calendar/{market.lower()}/current.json"


def _calendar_snapshot_ref(market: str, content_sha256: str) -> str:
    return (
        f"capabilities/market-calendar/{market.lower()}/snapshots/"
        f"{content_sha256}.json"
    )


def _read_canonical_calendar_artifact(
    artifact_root: str | Path,
    ref: str,
) -> tuple[dict[str, Any], bytes]:
    path = private_path(artifact_root).joinpath(*ref.split("/"))
    try:
        with open_private_text(path) as handle:
            content = handle.read().encode("utf-8")
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormalCorpusError(
            "market_calendar_binding_unavailable",
            "market calendar artifact is unavailable",
        ) from exc
    if not isinstance(payload, dict) or content != _render(payload):
        _fail(
            "market_calendar_binding_unavailable",
            "market calendar artifact bytes are not canonical",
        )
    return payload, content


def _validate_calendar_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_market: str,
) -> dict[str, Any]:
    item = dict(payload)
    try:
        if set(item) != _CALENDAR_SNAPSHOT_FIELDS:
            raise ValueError("snapshot keys are invalid")
        if item["schema_version"] != MARKET_CALENDAR_SNAPSHOT_SCHEMA:
            raise ValueError("snapshot schema is invalid")
        if item["market"] != expected_market:
            raise ValueError("snapshot market does not match")
        _text(item["market_calendar_version"], "market_calendar_version")
        coverage_start = _day(item["coverage_start"])
        coverage_end = _day(item["coverage_end"])
        if coverage_start > coverage_end:
            raise ValueError("snapshot coverage is reversed")
        values = item["trading_sessions"]
        if not isinstance(values, list) or not values:
            raise ValueError("snapshot trading sessions are missing")
        trading_dates: list[str] = []
        for raw_session in values:
            if not isinstance(raw_session, Mapping):
                raise ValueError("snapshot trading session must be an object")
            session = dict(raw_session)
            if set(session) != {"trading_date", "trade_date_type"}:
                raise ValueError("snapshot trading session keys are invalid")
            trading_dates.append(_day(session["trading_date"]))
            if session["trade_date_type"] not in _CALENDAR_SESSION_TYPES:
                raise ValueError("snapshot trade date type is invalid")
        if trading_dates != sorted(set(trading_dates)):
            raise ValueError("snapshot trading dates are not ordered and unique")
        if trading_dates[0] < coverage_start or trading_dates[-1] > coverage_end:
            raise ValueError("snapshot trading dates exceed coverage")
        _hash(item["source_receipt_sha256"], "source_receipt_sha256")
        _timestamp(item["observed_at_utc"], "observed_at_utc")
        _hash(item["content_sha256"], "content_sha256")
        content = {
            key: value for key, value in item.items() if key != "content_sha256"
        }
        if canonical_sha256(content) != item["content_sha256"]:
            raise ValueError("snapshot content hash does not match")
    except (FormalCorpusError, KeyError, TypeError, ValueError) as exc:
        raise FormalCorpusError(
            "market_calendar_binding_unavailable",
            f"market calendar snapshot is invalid: {exc}",
        ) from exc
    return {**item, "trading_dates": trading_dates}


def read_bound_market_calendar_snapshot(
    artifact_root: str | Path,
    *,
    market: str,
    snapshot_ref: str,
    snapshot_content_sha256: str,
    snapshot_file_sha256: str,
) -> dict[str, Any]:
    market = _calendar_market(market)
    try:
        ref = _relative_ref(snapshot_ref, "snapshot_ref")
        content_hash = _hash(snapshot_content_sha256, "snapshot_content_sha256")
        file_hash = _hash(snapshot_file_sha256, "snapshot_file_sha256")
    except FormalCorpusError as exc:
        raise FormalCorpusError("market_calendar_binding_unavailable", str(exc)) from exc
    assert content_hash is not None and file_hash is not None
    if ref != _calendar_snapshot_ref(market, content_hash):
        _fail(
            "market_calendar_binding_unavailable",
            "market calendar snapshot ref is not content-addressed",
        )
    payload, content = _read_canonical_calendar_artifact(artifact_root, ref)
    item = _validate_calendar_snapshot(payload, expected_market=market)
    if item["content_sha256"] != content_hash or sha256_bytes(content) != file_hash:
        _fail(
            "market_calendar_binding_unavailable",
            "market calendar snapshot hashes do not match",
        )
    return {
        **item,
        "snapshot_ref": ref,
        "snapshot_content_sha256": content_hash,
        "snapshot_file_sha256": file_hash,
    }


def read_expectation_bound_market_calendar_snapshot(
    artifact_root: str | Path,
    *,
    market: str,
    market_calendar_version: str,
    market_calendar_sha256: str,
) -> dict[str, Any]:
    """Read the immutable calendar snapshot named by a sealed expectation."""

    market = _calendar_market(market)
    try:
        version = _text(market_calendar_version, "market_calendar_version")
        content_hash = _hash(market_calendar_sha256, "market_calendar_sha256")
    except FormalCorpusError as exc:
        raise FormalCorpusError("market_calendar_binding_unavailable", str(exc)) from exc
    assert content_hash is not None
    ref = _calendar_snapshot_ref(market, content_hash)
    payload, content = _read_canonical_calendar_artifact(artifact_root, ref)
    item = _validate_calendar_snapshot(payload, expected_market=market)
    if (
        item["content_sha256"] != content_hash
        or item["market_calendar_version"] != version
    ):
        _fail(
            "market_calendar_binding_unavailable",
            "expectation-bound market calendar identity does not match",
        )
    return {
        **item,
        "snapshot_ref": ref,
        "snapshot_content_sha256": content_hash,
        "snapshot_file_sha256": sha256_bytes(content),
    }


def read_market_calendar_binding(
    artifact_root: str | Path,
    *,
    market: str,
) -> dict[str, Any]:
    market = _calendar_market(market)
    payload, content = _read_canonical_calendar_artifact(
        artifact_root, _calendar_pointer_ref(market)
    )
    try:
        if set(payload) != _CALENDAR_POINTER_FIELDS:
            raise ValueError("pointer keys are invalid")
        if payload["schema_version"] != MARKET_CALENDAR_POINTER_SCHEMA:
            raise ValueError("pointer schema is invalid")
        if payload["market"] != market:
            raise ValueError("pointer market does not match")
        snapshot_ref = _relative_ref(payload["snapshot_ref"], "snapshot_ref")
        snapshot_content_hash = _hash(
            payload["snapshot_content_sha256"], "snapshot_content_sha256"
        )
        snapshot_file_hash = _hash(
            payload["snapshot_file_sha256"], "snapshot_file_sha256"
        )
        _hash(payload["content_sha256"], "content_sha256")
        pointer_body = {
            key: value for key, value in payload.items() if key != "content_sha256"
        }
        if canonical_sha256(pointer_body) != payload["content_sha256"]:
            raise ValueError("pointer content hash does not match")
        if content != _render(payload):
            raise ValueError("pointer bytes are not canonical")
    except (FormalCorpusError, KeyError, TypeError, ValueError) as exc:
        raise FormalCorpusError(
            "market_calendar_binding_unavailable",
            f"market calendar pointer is invalid: {exc}",
        ) from exc
    assert snapshot_content_hash is not None and snapshot_file_hash is not None
    return read_bound_market_calendar_snapshot(
        artifact_root,
        market=market,
        snapshot_ref=snapshot_ref,
        snapshot_content_sha256=snapshot_content_hash,
        snapshot_file_sha256=snapshot_file_hash,
    )


def _normalized_calendar_source_receipt(
    receipt: Any,
    *,
    market: str,
    coverage_start: str,
    coverage_end: str,
) -> dict[str, Any]:
    try:
        if not isinstance(receipt, Mapping):
            raise ValueError("receipt must be an object")
        item = dict(receipt)
        if set(item) != _CALENDAR_SOURCE_FIELDS:
            raise ValueError("receipt keys are invalid")
        if type(item["retcode"]) is not int or item["retcode"] != 0:
            raise ValueError("receipt retcode is invalid")
        if item["coverage_complete"] is not True:
            raise ValueError("receipt coverage is incomplete")
        if item["pagination_complete"] is not True:
            raise ValueError("receipt pagination is incomplete")
        if type(item["page_count"]) is not int or item["page_count"] != 1:
            raise ValueError("receipt page count is invalid")
        rows = item["rows"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("receipt rows are missing")
        sessions: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                raise ValueError("receipt row must be an object")
            row = dict(raw_row)
            if set(row) != _CALENDAR_SOURCE_ROW_FIELDS:
                raise ValueError("receipt row keys are invalid")
            trading_date = _day(row["time"])
            session_type = _text(row["trade_date_type"], "trade_date_type")
            if trading_date < coverage_start or trading_date > coverage_end:
                raise ValueError("receipt row exceeds requested coverage")
            if trading_date in seen:
                raise ValueError("receipt contains duplicate trading dates")
            if session_type not in _CALENDAR_SESSION_TYPES:
                raise ValueError("receipt trade date type is invalid")
            seen.add(trading_date)
            sessions.append(
                {"trading_date": trading_date, "trade_date_type": session_type}
            )
    except (FormalCorpusError, KeyError, TypeError, ValueError) as exc:
        raise FormalCorpusError(
            "market_calendar_source_invalid",
            f"market calendar source receipt is invalid: {exc}",
        ) from exc
    return {
        "schema_version": _MARKET_CALENDAR_SOURCE_RECEIPT_SCHEMA,
        "source": "futu.request_trading_days",
        "market": market,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "trading_sessions": sorted(
            sessions, key=lambda value: value["trading_date"]
        ),
    }


def _publish_exact_calendar_artifact(
    artifact_root: str | Path,
    ref: str,
    content: bytes,
) -> None:
    path = private_path(artifact_root).joinpath(*ref.split("/"))
    if path.exists() or path.is_symlink():
        try:
            with open_private_text(path) as handle:
                existing = handle.read().encode("utf-8")
        except OSError as exc:
            raise ValueError("calendar artifact is unreadable") from exc
        if existing != content:
            raise ValueError("calendar artifact conflicts")
        return
    ensure_private_directory(path.parent)
    atomic_write_private_text(path, content.decode("utf-8"))


def refresh_market_calendar_binding(
    artifact_root: str | Path,
    *,
    gateway: Any,
    market: str,
    market_calendar_version: str,
    coverage_start: str,
    coverage_end: str,
    observed_at_utc: str,
) -> dict[str, Any]:
    market = _calendar_market(market)
    version = _text(market_calendar_version, "market_calendar_version")
    start = _day(coverage_start)
    end = _day(coverage_end)
    observed_at = _timestamp(observed_at_utc, "observed_at_utc")
    if start > end:
        _fail("corpus_input_invalid", "market calendar coverage is reversed")
    try:
        receipt = gateway.get_trading_days_with_receipt(
            market=market,
            start=start,
            end=end,
        )
    except Exception as exc:
        raise FormalCorpusError(
            "market_calendar_source_unavailable",
            "market calendar source is unavailable",
        ) from exc
    normalized_receipt = _normalized_calendar_source_receipt(
        receipt,
        market=market,
        coverage_start=start,
        coverage_end=end,
    )
    sessions = normalized_receipt["trading_sessions"]
    source_receipt_sha256 = canonical_sha256(normalized_receipt)
    pointer_ref = _calendar_pointer_ref(market)
    pointer_path = private_path(artifact_root).joinpath(*pointer_ref.split("/"))
    if pointer_path.exists() or pointer_path.is_symlink():
        try:
            current = read_market_calendar_binding(artifact_root, market=market)
        except FormalCorpusError:
            current = None
        expected = {
            "market": market,
            "market_calendar_version": version,
            "coverage_start": start,
            "coverage_end": end,
            "trading_sessions": sessions,
            "source_receipt_sha256": source_receipt_sha256,
        }
        if current is not None and all(
            current[key] == value for key, value in expected.items()
        ):
            return {"status": "unchanged", "binding": current}
    snapshot: dict[str, Any] = {
        "schema_version": MARKET_CALENDAR_SNAPSHOT_SCHEMA,
        "market": market,
        "market_calendar_version": version,
        "coverage_start": start,
        "coverage_end": end,
        "trading_sessions": sessions,
        "source_receipt_sha256": source_receipt_sha256,
        "observed_at_utc": observed_at,
    }
    snapshot["content_sha256"] = canonical_sha256(snapshot)
    snapshot_content = _render(snapshot)
    snapshot_ref = _calendar_snapshot_ref(market, snapshot["content_sha256"])
    pointer: dict[str, Any] = {
        "schema_version": MARKET_CALENDAR_POINTER_SCHEMA,
        "market": market,
        "snapshot_ref": snapshot_ref,
        "snapshot_content_sha256": snapshot["content_sha256"],
        "snapshot_file_sha256": sha256_bytes(snapshot_content),
    }
    pointer["content_sha256"] = canonical_sha256(pointer)
    try:
        _publish_exact_calendar_artifact(
            artifact_root, snapshot_ref, snapshot_content
        )
        atomic_write_private_text(pointer_path, _render(pointer).decode("utf-8"))
        binding = read_market_calendar_binding(artifact_root, market=market)
    except (FormalCorpusError, OSError, ValueError) as exc:
        raise FormalCorpusError(
            "market_calendar_artifact_conflict",
            "market calendar artifact cannot be published",
        ) from exc
    if binding["snapshot_content_sha256"] != snapshot["content_sha256"]:
        _fail(
            "market_calendar_artifact_conflict",
            "published market calendar does not match the collected evidence",
        )
    return {"status": "published", "binding": binding}


def _expectation_dir(root: Path, market: str, account: str, trading_date: str) -> Path:
    return root / market.lower() / account / "expectations" / trading_date


def _point_dir(
    root: Path,
    market: str,
    account: str,
    trading_date: str,
    point_id: str,
) -> Path:
    return root / market.lower() / account / "points" / trading_date / point_id


def formal_corpus_present(
    runtime_root: str | Path,
    *,
    market: str,
    account: str,
) -> bool:
    market, account = _identity(market, account)
    path = _corpus_root(runtime_root) / market.lower() / account
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        return True
    if not path.is_dir():
        return False
    return any(
        owner.is_symlink() or owner.exists()
        for owner in (path / "expectations", path / "points")
    )


def _relative(runtime_root: str | Path, path: Path) -> str:
    return path.relative_to(_runtime_root(runtime_root)).as_posix()


def _expectation_denominator(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in (
            "schema_version",
            "market",
            "account",
            "trading_date",
            "market_calendar_version",
            "market_calendar_sha256",
            "schedule_config_sha256",
            "scheduled_scan_targets_market",
            "expected_recommendation_point_ids",
        )
    }


def _validate_expectation(
    payload: Mapping[str, Any],
    *,
    market: str,
    account: str,
    trading_date: str,
) -> dict[str, Any]:
    item = dict(payload)
    if set(item) != _EXPECTATION_FIELDS or item.get("schema_version") != FORMAL_EXPECTATION_SCHEMA:
        _fail("formal_corpus_artifact_invalid", "expectation keys or schema are invalid")
    if (item.get("market"), item.get("account"), item.get("trading_date")) != (
        market,
        account,
        trading_date,
    ):
        _fail("formal_corpus_artifact_invalid", "expectation identity does not match")
    _text(item.get("market_calendar_version"), "market_calendar_version")
    _hash(item.get("market_calendar_sha256"), "market_calendar_sha256")
    _hash(item.get("schedule_config_sha256"), "schedule_config_sha256")
    sealed_at = _timestamp(item.get("sealed_at_utc"), "sealed_at_utc")
    raw_targets = item.get("scheduled_scan_targets_market")
    point_ids = item.get("expected_recommendation_point_ids")
    if not isinstance(raw_targets, list) or not isinstance(point_ids, list):
        _fail("formal_corpus_artifact_invalid", "expectation targets must be lists")
    targets = [
        _timestamp(value, f"scheduled_scan_targets_market[{index}]")
        for index, value in enumerate(raw_targets)
    ]
    if targets != sorted(set(targets)):
        _fail("formal_corpus_artifact_invalid", "expectation targets are not canonical")
    if point_ids != [build_recommendation_point_id(market, account, target) for target in targets]:
        _fail("formal_corpus_artifact_invalid", "expectation point IDs do not match")
    first = targets[0] if targets else None
    if item.get("first_target_at_utc") != first:
        _fail("formal_corpus_artifact_invalid", "expectation first target does not match")
    before = bool(
        first
        and datetime.fromisoformat(sealed_at.replace("Z", "+00:00"))
        < datetime.fromisoformat(first.replace("Z", "+00:00"))
    )
    if item.get("sealed_before_first_target") is not before:
        _fail("formal_corpus_artifact_invalid", "expectation timing does not match")
    expected_hash = canonical_sha256({key: value for key, value in item.items() if key != "content_sha256"})
    if item.get("content_sha256") != expected_hash:
        _fail("formal_corpus_artifact_invalid", "expectation hash does not match")
    return item


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        _fail("formal_corpus_artifact_invalid", "artifact is not a regular file")
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormalCorpusError("formal_corpus_artifact_invalid", "artifact is unreadable") from exc
    if not isinstance(payload, dict) or content != _render(payload):
        _fail("formal_corpus_artifact_invalid", "artifact bytes are not canonical")
    return payload, content


def _read_gzip(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        _fail("formal_corpus_artifact_invalid", "artifact is not a regular file")
    try:
        content = gzip.decompress(path.read_bytes())
        payload = json.loads(content)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormalCorpusError("formal_corpus_artifact_invalid", "gzip artifact is unreadable") from exc
    if not isinstance(payload, dict) or content != _render(payload):
        _fail("formal_corpus_artifact_invalid", "gzip payload bytes are not canonical")
    return payload, content


def _expectation_artifacts(
    runtime_root: str | Path,
    market: str,
    account: str,
    trading_date: str,
) -> list[tuple[dict[str, Any], bytes, Path]]:
    directory = _expectation_dir(_corpus_root(runtime_root), market, account, trading_date)
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        _fail("formal_corpus_artifact_invalid", "expectation directory is invalid")
    results: list[tuple[dict[str, Any], bytes, Path]] = []
    for path in sorted(directory.glob("*.json")):
        payload, content = _read_json(path)
        item = _validate_expectation(
            payload,
            market=market,
            account=account,
            trading_date=trading_date,
        )
        if path.name != f"{item['content_sha256']}.json":
            _fail("formal_corpus_artifact_invalid", "expectation filename or bytes do not match")
        results.append((item, content, path))
    return results


def _expectation_result(
    runtime_root: str | Path,
    payload: Mapping[str, Any],
    content: bytes,
    path: Path,
    *,
    status: str,
) -> dict[str, Any]:
    reason = None
    if status == "conflict":
        reason = "formal_corpus_conflict"
    elif not payload["scheduled_scan_targets_market"]:
        status, reason = "not_evaluable", "formal_day_expectation_empty"
    elif payload["sealed_before_first_target"] is not True:
        status, reason = "not_evaluable", "formal_day_expectation_late"
    return {
        "operation": "seal_formal_day_expectation",
        "status": status,
        "reason_code": reason,
        "market": payload["market"],
        "account": payload["account"],
        "trading_date": payload["trading_date"],
        "expected_point_count": len(payload["expected_recommendation_point_ids"]),
        "artifact_ref": _relative(runtime_root, path),
        "artifact_content_sha256": payload["content_sha256"],
        "artifact_file_sha256": sha256_bytes(content),
        "sealed_at_utc": payload["sealed_at_utc"],
    }


def seal_formal_day_expectation(
    runtime_root: str | Path,
    *,
    market: str,
    account: str,
    schedule: Mapping[str, Any],
    trading_date: str,
    market_calendar_version: str,
    market_calendar_sha256: str,
    sealed_at_utc: str,
    trade_date_type: str = "WHOLE",
) -> dict[str, Any]:
    market, account = _identity(market, account)
    trading_date = _day(trading_date)
    sealed_at = _timestamp(sealed_at_utc, "sealed_at_utc")
    calendar_version = _text(market_calendar_version, "market_calendar_version")
    calendar_hash = _hash(market_calendar_sha256, "market_calendar_sha256")
    if not isinstance(schedule, Mapping):
        _fail("formal_corpus_input_invalid", "schedule must be an object")
    try:
        targets = [
            utc_timestamp(value, "scheduled_scan_target_market")
            for value in scheduled_scan_targets_for_date(
                dict(schedule), trading_date, trade_date_type=trade_date_type
            )
        ]
    except (CandidateSnapshotContractError, TypeError, ValueError) as exc:
        raise FormalCorpusError("formal_corpus_input_invalid", "schedule is invalid") from exc
    first = targets[0] if targets else None
    payload: dict[str, Any] = {
        "schema_version": FORMAL_EXPECTATION_SCHEMA,
        "market": market,
        "account": account,
        "trading_date": trading_date,
        "market_calendar_version": calendar_version,
        "market_calendar_sha256": calendar_hash,
        "schedule_config_sha256": canonical_sha256(dict(schedule)),
        "sealed_at_utc": sealed_at,
        "first_target_at_utc": first,
        "sealed_before_first_target": bool(
            first
            and datetime.fromisoformat(sealed_at.replace("Z", "+00:00"))
            < datetime.fromisoformat(first.replace("Z", "+00:00"))
        ),
        "scheduled_scan_targets_market": targets,
        "expected_recommendation_point_ids": [
            build_recommendation_point_id(market, account, target) for target in targets
        ],
    }
    payload["content_sha256"] = canonical_sha256(payload)
    content = _render(payload)
    directory = _expectation_dir(_corpus_root(runtime_root), market, account, trading_date)
    lock = (
        _corpus_root(runtime_root)
        / market.lower()
        / account
        / ".locks"
        / "expectations"
        / f"{trading_date}.lock"
    )
    with exclusive_private_file_lock(lock):
        existing = _expectation_artifacts(runtime_root, market, account, trading_date)
        if len(existing) > 1:
            item, old_content, old_path = existing[0]
            return _expectation_result(
                runtime_root, item, old_content, old_path, status="conflict"
            )
        if existing:
            item, old_content, old_path = existing[0]
            if _expectation_denominator(item) == _expectation_denominator(payload):
                return _expectation_result(
                    runtime_root, item, old_content, old_path, status="idempotent"
                )
        ensure_private_directory(directory)
        path = directory / f"{payload['content_sha256']}.json"
        atomic_write_private_text(path, content.decode("utf-8"))
        written = _expectation_artifacts(runtime_root, market, account, trading_date)
        if not any(item[0]["content_sha256"] == payload["content_sha256"] for item in written):
            _fail("formal_corpus_artifact_invalid", "expectation readback failed")
        status = "published" if len(written) == 1 else "conflict"
        return _expectation_result(runtime_root, payload, content, path, status=status)


def _validate_source_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SOURCE_BINDING_FIELDS:
        _fail("formal_corpus_artifact_invalid", "point source binding is invalid")
    item = dict(value)
    market, account = _identity(item.get("market"), item.get("account"))
    if item["market"] != market or item["account"] != account:
        _fail("formal_corpus_artifact_invalid", "point source identity is not canonical")
    _segment(item.get("run_id"), "run_id")
    _timestamp(item.get("scheduled_scan_target_market"), "scheduled_scan_target_market")
    _text(item.get("producer_behavior_version"), "producer_behavior_version")
    for field in (
        "recommendation_point_content_sha256",
        "opening_snapshot_sha256",
        "required_data_manifest_sha256",
        "prepared_context_manifest_sha256",
        "prepared_context_payload_sha256",
    ):
        _hash(item.get(field), field, optional=True)
    return item


def _validate_formal_point(
    payload: Mapping[str, Any],
    *,
    market: str,
    account: str,
    trading_date: str,
    point_id: str,
) -> dict[str, Any]:
    item = dict(payload)
    if set(item) != _POINT_FIELDS or item.get("schema_version") != FORMAL_POINT_SCHEMA:
        _fail("formal_corpus_artifact_invalid", "formal point keys or schema are invalid")
    if (
        item.get("market"),
        item.get("account"),
        item.get("trading_date"),
        item.get("recommendation_point_id"),
    ) != (market, account, trading_date, point_id):
        _fail("formal_corpus_artifact_invalid", "formal point identity does not match")
    if item.get("status") not in {"ready", "not_evaluable"}:
        _fail("formal_corpus_artifact_invalid", "formal point status is invalid")
    if (item["status"] == "ready") != (item.get("reason_code") is None):
        _fail("formal_corpus_artifact_invalid", "formal point reason does not match status")
    if item["status"] == "not_evaluable":
        _text(item.get("reason_code"), "reason_code")
    _timestamp(item.get("captured_at_utc"), "captured_at_utc")
    binding = _validate_source_binding(item.get("source_binding"))
    if binding["market"] != market or binding["account"] != account:
        _fail("formal_corpus_artifact_invalid", "formal point binding identity changed")
    if build_recommendation_point_id(
        market, account, binding["scheduled_scan_target_market"]
    ) != point_id:
        _fail("formal_corpus_artifact_invalid", "formal point target identity changed")
    point = item.get("recommendation_point")
    validated_point: dict[str, Any] | None = None
    if item["status"] == "ready" and point is None:
        _fail("formal_corpus_artifact_invalid", "ready formal point is missing")
    if point is not None:
        try:
            validated_point = validate_recommendation_point(point)
        except RecommendationPointError as exc:
            raise FormalCorpusError("formal_corpus_artifact_invalid", str(exc)) from exc
        if validated_point["schema_version"] != RECOMMENDATION_POINT_SCHEMA_V3:
            _fail("formal_corpus_artifact_invalid", "formal point contract is unsupported")
        if (
            validated_point["market"],
            validated_point["account"],
            validated_point["run_id"],
            validated_point["scheduled_scan_target_market"],
            validated_point["recommendation_point_id"],
        ) != (
            market,
            account,
            binding["run_id"],
            binding["scheduled_scan_target_market"],
            point_id,
        ):
            _fail("formal_corpus_artifact_invalid", "recommendation point identity changed")
        if validated_point["content_sha256"] != binding["recommendation_point_content_sha256"]:
            _fail("formal_corpus_artifact_invalid", "recommendation point hash changed")
        binding_fields = {
            "opening_snapshot_sha256": "opening_snapshot_sha256",
            "required_data_manifest_sha256": "required_data_manifest_sha256",
            "prepared_context_manifest_sha256": "prepared_context_manifest_sha256",
            "prepared_context_payload_sha256": "prepared_context_payload_sha256",
        }
        if any(
            binding[binding_field] != validated_point[point_field]
            for binding_field, point_field in binding_fields.items()
        ):
            _fail("formal_corpus_artifact_invalid", "recommendation point owner binding changed")
    opening = item.get("opening_snapshot")
    if opening is not None and not isinstance(opening, Mapping):
        _fail("formal_corpus_artifact_invalid", "opening snapshot must be an object")
    if item["status"] == "ready" and not isinstance(opening, Mapping):
        _fail("formal_corpus_artifact_invalid", "ready opening snapshot is missing")
    if isinstance(opening, Mapping) and (
        opening.get("content_sha256") != binding["opening_snapshot_sha256"]
    ):
        _fail("formal_corpus_artifact_invalid", "opening snapshot hash changed")
    if isinstance(opening, Mapping):
        try:
            validate_opening_candidate_snapshot(
                opening,
                expected_run_id=binding["run_id"],
                expected_account=account,
                require_current_contract=True,
            )
        except OpeningCandidateSnapshotError as exc:
            raise FormalCorpusError("formal_corpus_artifact_invalid", str(exc)) from exc
    symbols = item.get("required_data_symbols")
    if not isinstance(symbols, list):
        _fail("formal_corpus_artifact_invalid", "required_data_symbols must be a list")
    symbol_map: dict[str, dict[str, Any]] = {}
    symbol_fields = {
        "symbol",
        "status",
        "source_observed_at",
        "payload_sha256",
        "scan_blob_ref",
    }
    for raw in symbols:
        if not isinstance(raw, Mapping) or set(raw) != symbol_fields:
            _fail("formal_corpus_artifact_invalid", "required-data symbol is invalid")
        row = dict(raw)
        symbol = _text(row["symbol"], "required-data symbol")
        if symbol != symbol.upper() or symbol in symbol_map:
            _fail("formal_corpus_artifact_invalid", "required-data symbol identity is invalid")
        if row["status"] == "ready":
            _timestamp(row["source_observed_at"], "source_observed_at")
            _hash(row["payload_sha256"], "payload_sha256")
        elif row["status"] == "failed":
            if any(
                row[field] is not None
                for field in ("source_observed_at", "payload_sha256", "scan_blob_ref")
            ):
                _fail("formal_corpus_artifact_invalid", "failed required-data symbol has evidence")
        else:
            _fail("formal_corpus_artifact_invalid", "required-data symbol status is invalid")
        symbol_map[symbol] = row
    evidence = item.get("option_position_evidence_binding")
    if item["status"] == "ready" and not isinstance(evidence, Mapping):
        _fail("formal_corpus_artifact_invalid", "ready formal point evidence is missing")
    if validated_point is not None:
        if not isinstance(opening, Mapping) or not isinstance(evidence, Mapping):
            _fail("formal_corpus_artifact_invalid", "formal point owner facts are incomplete")
        try:
            validate_option_position_evidence_binding(
                evidence,
                expected_run_id=binding["run_id"],
                expected_account=account,
                expected_recommendation_point_id=point_id,
                expected_market=market,
            )
        except RecommendationPointError as exc:
            raise FormalCorpusError("formal_corpus_artifact_invalid", str(exc)) from exc
        rebuilt_coherence = build_formal_point_time_coherence(
            opening,
            {"symbols": symbol_map},
            evidence,
        )
        if rebuilt_coherence != validated_point["formal_point_time_coherence"]:
            _fail("formal_corpus_artifact_invalid", "formal point time coherence changed")
    supplied = _hash(item.get("content_sha256"), "content_sha256")
    if supplied != canonical_sha256({key: value for key, value in item.items() if key != "content_sha256"}):
        _fail("formal_corpus_artifact_invalid", "formal point hash does not match")
    return item


def _point_artifacts(
    runtime_root: str | Path,
    market: str,
    account: str,
    trading_date: str,
    point_id: str,
) -> list[tuple[dict[str, Any], bytes, Path]]:
    directory = _point_dir(_corpus_root(runtime_root), market, account, trading_date, point_id)
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        _fail("formal_corpus_artifact_invalid", "formal point directory is invalid")
    results = []
    for path in sorted(directory.glob("*.json.gz")):
        payload, content = _read_gzip(path)
        item = _validate_formal_point(
            payload,
            market=market,
            account=account,
            trading_date=trading_date,
            point_id=point_id,
        )
        if path.name != f"{item['content_sha256']}.json.gz":
            _fail("formal_corpus_artifact_invalid", "formal point filename or bytes do not match")
        results.append((item, content, path))
    return results


def _point_day_layout_conflicts(
    runtime_root: str | Path,
    market: str,
    account: str,
    trading_date: str,
    expected_point_ids: list[str],
) -> bool:
    directory = _corpus_root(runtime_root) / market.lower() / account / "points" / trading_date
    if not directory.exists():
        return False
    if directory.is_symlink() or not directory.is_dir():
        return True
    entries = list(directory.iterdir())
    actual = {
        path.name
        for path in entries
        if path.is_dir() and not path.is_symlink() and _HASH.fullmatch(path.name)
    }
    return len(actual) != len(entries) or bool(actual - set(expected_point_ids))


def _required_data_binding(opening: Mapping[str, Any]) -> tuple[str, str]:
    matches = [
        row
        for row in opening.get("dependencies") or []
        if isinstance(row, Mapping) and row.get("kind") == "required_data"
    ]
    if len(matches) != 1:
        _fail("formal_point_evidence_missing", "required-data binding is missing")
    ref = matches[0].get("relpath")
    digest = matches[0].get("sha256")
    if not isinstance(ref, str) or not ref or ref.startswith("/") or ".." in ref.split("/"):
        _fail("formal_point_evidence_missing", "required-data ref is invalid")
    hashed = _hash(digest, "required_data_manifest_sha256")
    assert hashed is not None
    return ref, hashed


def capture_formal_point_attempt(
    runtime_root: str | Path,
    source_root: str | Path,
    *,
    market: str,
    account: str,
    trading_date: str,
    run_id: str,
    scheduled_scan_target_market: str,
    captured_at_utc: str,
    producer_behavior_version: str,
    recommendation_point: Mapping[str, Any] | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    market, account = _identity(market, account)
    trading_date = _day(trading_date)
    run_id = _segment(run_id, "run_id")
    target = _timestamp(scheduled_scan_target_market, "scheduled_scan_target_market")
    captured_at = _timestamp(captured_at_utc, "captured_at_utc")
    behavior = _text(producer_behavior_version, "producer_behavior_version")
    point_id = build_recommendation_point_id(market, account, target)
    expectations = _expectation_artifacts(runtime_root, market, account, trading_date)
    if len(expectations) > 1:
        _fail("formal_corpus_conflict", "formal point expectation is conflicted")
    if not expectations or point_id not in expectations[0][0]["expected_recommendation_point_ids"]:
        _fail("formal_expectation_missing", "formal point has no unique matching expectation")

    point: dict[str, Any] | None = None
    opening_snapshot: dict[str, Any] | None = None
    symbols: list[dict[str, Any]] = []
    evidence: dict[str, Any] | None = None
    owner_hashes: dict[str, str | None] = {
        "recommendation_point_content_sha256": None,
        "opening_snapshot_sha256": None,
        "required_data_manifest_sha256": None,
        "prepared_context_manifest_sha256": None,
        "prepared_context_payload_sha256": None,
    }
    derived_reason = str(reason_code or "").strip() or None
    if recommendation_point is not None:
        try:
            point = validate_recommendation_point(recommendation_point)
            if point["schema_version"] != RECOMMENDATION_POINT_SCHEMA_V3:
                raise RecommendationPointError(
                    "formal_point_contract_unsupported",
                    "formal corpus requires recommendation_point.v3",
                )
            if (
                point["market"],
                point["account"],
                point["run_id"],
                point["scheduled_scan_target_market"],
            ) != (market, account, run_id, target):
                raise RecommendationPointError(
                    "formal_point_evidence_missing", "recommendation point identity changed"
                )
            opening = load_opening_candidate_snapshot(
                base=Path(source_root),
                run_id=run_id,
                account=account,
                require_current_contract=True,
            )
            validate_opening_candidate_snapshot(
                opening,
                expected_run_id=run_id,
                expected_account=account,
                require_current_contract=True,
            )
            if opening.get("content_sha256") != point["opening_snapshot_sha256"]:
                raise ValueError("opening snapshot binding changed")
            opening_snapshot = dict(opening)
            required_ref, required_hash = _required_data_binding(opening)
            required_path = private_path(source_root).joinpath(*required_ref.split("/"))
            required_manifest, required_root, required_bytes = (
                load_required_data_snapshot_manifest_snapshot(
                    manifest_path=required_path,
                    expected_run_id=run_id,
                )
            )
            if (
                sha256_bytes(required_bytes) != required_hash
                or point["required_data_manifest_ref"] != required_ref
                or point["required_data_manifest_sha256"] != required_hash
            ):
                raise ValueError("required-data manifest binding changed")
            prepared_path = private_path(source_root).joinpath(
                *point["prepared_context_manifest_ref"].split("/")
            )
            receipt = load_prepared_option_positions_context_receipt(
                manifest_path=prepared_path,
                expected_base=Path(source_root),
                expected_run_id=run_id,
                expected_account=account,
                expected_account_config_sha256=point["account_config_sha256"],
                expected_manifest_sha256=point["prepared_context_manifest_sha256"],
            )
            evidence = validate_option_position_evidence_binding(
                point["option_position_evidence_binding"],
                expected_run_id=run_id,
                expected_account=account,
                expected_recommendation_point_id=point_id,
                expected_market=market,
            )
            if sha256_bytes(receipt["payload_bytes"]) != point["prepared_context_payload_sha256"]:
                raise ValueError("prepared payload binding changed")
            required_batch = resolve_frozen_required_data_csv_bytes_batch(
                manifest_path=required_path,
                expected_run_id=run_id,
                required_data_root=required_root,
                require_fresh=False,
            )
            if required_batch.unavailable:
                raise ValueError("required-data snapshot batch is incomplete")
            target_ms = int(
                datetime.fromisoformat(target.replace("Z", "+00:00")).timestamp()
                * 1000
            )
            sealed_at = utc_timestamp(opening["sealed_at_utc"], "opening sealed_at_utc")
            sealed_ms = int(
                datetime.fromisoformat(sealed_at.replace("Z", "+00:00")).timestamp()
                * 1000
            )
            expected_evidence = build_option_position_evidence_binding(
                run_id=run_id,
                account=account,
                market=market,
                recommendation_point_id=point_id,
                account_config_sha256=point["account_config_sha256"],
                evidence_at_utc=sealed_at,
                prepared_receipt=receipt,
                required_data_entries=required_batch.entries,
                formal_time_bounds=(target_ms - FORMAL_POINT_MAX_SKEW_MS, sealed_ms),
            )
            if evidence != expected_evidence:
                raise ValueError("option position evidence does not match owner artifacts")
            owner_hashes = {
                "recommendation_point_content_sha256": point["content_sha256"],
                "opening_snapshot_sha256": point["opening_snapshot_sha256"],
                "required_data_manifest_sha256": required_hash,
                "prepared_context_manifest_sha256": point[
                    "prepared_context_manifest_sha256"
                ],
                "prepared_context_payload_sha256": point[
                    "prepared_context_payload_sha256"
                ],
            }
            symbols = [
                {
                    "symbol": symbol,
                    "status": row.get("status"),
                    "source_observed_at": row.get("source_observed_at"),
                    "payload_sha256": row.get("payload_sha256"),
                    "scan_blob_ref": row.get("scan_blob_ref"),
                }
                for symbol, row in sorted((required_manifest.get("symbols") or {}).items())
                if isinstance(row, Mapping)
            ]
            coherence = point["formal_point_time_coherence"]
            if coherence["status"] != "ready":
                derived_reason = "formal_point_time_skew"
            elif point["terminal_sell_put_status"] in {"partial_data", "data_unavailable"}:
                derived_reason = "official_decision_incomplete"
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            OpeningCandidateSnapshotError,
            PreparedOptionPositionsContextError,
            RecommendationPointError,
            FrozenRequiredDataUnavailable,
            RequiredDataSnapshotError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            derived_reason = str(getattr(exc, "reason_code", "formal_point_evidence_missing"))
            point = None
            opening_snapshot = None
            symbols = []
            evidence = None
            owner_hashes = {key: None for key in owner_hashes}

    status = "ready" if point is not None and derived_reason is None else "not_evaluable"
    if status == "not_evaluable" and derived_reason is None:
        derived_reason = "formal_point_evidence_missing"
    source_binding = {
        "market": market,
        "account": account,
        "run_id": run_id,
        "scheduled_scan_target_market": target,
        "producer_behavior_version": behavior,
        **owner_hashes,
    }
    payload: dict[str, Any] = {
        "schema_version": FORMAL_POINT_SCHEMA,
        "status": status,
        "reason_code": derived_reason,
        "market": market,
        "account": account,
        "trading_date": trading_date,
        "recommendation_point_id": point_id,
        "captured_at_utc": captured_at,
        "source_binding": source_binding,
        "recommendation_point": point,
        "opening_snapshot": opening_snapshot,
        "required_data_symbols": symbols,
        "option_position_evidence_binding": evidence,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    content = _render(payload)
    directory = _point_dir(_corpus_root(runtime_root), market, account, trading_date, point_id)
    lock = (
        _corpus_root(runtime_root)
        / market.lower()
        / account
        / ".locks"
        / "points"
        / trading_date
        / f"{point_id}.lock"
    )
    with exclusive_private_file_lock(lock):
        existing = _point_artifacts(runtime_root, market, account, trading_date, point_id)
        if len(existing) > 1:
            return _point_result(runtime_root, existing[0][0], existing[0][2], status="conflict")
        if existing:
            old, _old_content, old_path = existing[0]
            if old["source_binding"] == source_binding:
                return _point_result(runtime_root, old, old_path, status="idempotent")
        ensure_private_directory(directory)
        path = directory / f"{payload['content_sha256']}.json.gz"
        atomic_write_private_bytes(path, gzip.compress(content, mtime=0))
        written = _point_artifacts(runtime_root, market, account, trading_date, point_id)
        if not any(item[0]["content_sha256"] == payload["content_sha256"] for item in written):
            _fail("formal_corpus_artifact_invalid", "formal point readback failed")
        return _point_result(
            runtime_root,
            payload,
            path,
            status="published" if len(written) == 1 else "conflict",
        )


def _point_result(
    runtime_root: str | Path,
    payload: Mapping[str, Any],
    path: Path,
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "operation": "capture_formal_point_attempt",
        "status": status,
        "reason_code": (
            "formal_corpus_conflict" if status == "conflict" else payload["reason_code"]
        ),
        "market": payload["market"],
        "account": payload["account"],
        "trading_date": payload["trading_date"],
        "recommendation_point_id": payload["recommendation_point_id"],
        "artifact_ref": _relative(runtime_root, path),
        "artifact_content_sha256": payload["content_sha256"],
        "captured_at_utc": payload["captured_at_utc"],
    }


def load_formal_point(
    runtime_root: str | Path,
    *,
    market: str,
    account: str,
    trading_date: str,
    recommendation_point_id: str,
) -> dict[str, Any]:
    market, account = _identity(market, account)
    trading_date = _day(trading_date)
    point_id = _text(recommendation_point_id, "recommendation_point_id")
    if _HASH.fullmatch(point_id) is None:
        _fail("formal_corpus_input_invalid", "recommendation point ID is invalid")
    artifacts = _point_artifacts(runtime_root, market, account, trading_date, point_id)
    if not artifacts:
        return {"status": "missing", "reason_code": "formal_point_evidence_missing", "point": None}
    if len(artifacts) != 1:
        return {"status": "conflict", "reason_code": "formal_corpus_conflict", "point": None}
    point, content, path = artifacts[0]
    if point["status"] != "ready":
        status, reason = "not_evaluable", point["reason_code"]
    else:
        status, reason = "available", None
    return {
        "status": status,
        "reason_code": reason,
        "point": point,
        "artifact_ref": _relative(runtime_root, path),
        "artifact_content_sha256": point["content_sha256"],
        "artifact_payload_sha256": sha256_bytes(content),
        "artifact_file_sha256": sha256_bytes(path.read_bytes()),
    }


def load_formal_expectation(
    runtime_root: str | Path,
    *,
    market: str,
    account: str,
    trading_date: str,
) -> dict[str, Any]:
    market, account = _identity(market, account)
    trading_date = _day(trading_date)
    artifacts = _expectation_artifacts(runtime_root, market, account, trading_date)
    if not artifacts:
        return {"status": "missing", "reason_code": "formal_expectation_missing"}
    if len(artifacts) != 1:
        return {"status": "conflict", "reason_code": "formal_corpus_conflict"}
    expectation, content, path = artifacts[0]
    if _point_day_layout_conflicts(
        runtime_root,
        market,
        account,
        trading_date,
        expectation["expected_recommendation_point_ids"],
    ):
        status, reason = "conflict", "formal_corpus_conflict"
    elif not expectation["scheduled_scan_targets_market"]:
        status, reason = "not_evaluable", "formal_day_expectation_empty"
    elif not expectation["sealed_before_first_target"]:
        status, reason = "not_evaluable", "formal_day_expectation_late"
    else:
        status, reason = "available", None
    return {
        "status": status,
        "reason_code": reason,
        "expectation": expectation,
        "artifact_ref": _relative(runtime_root, path),
        "artifact_content_sha256": expectation["content_sha256"],
        "artifact_file_sha256": sha256_bytes(content),
    }


def build_corpus_health_receipt(
    runtime_root: str | Path,
    *,
    market: str,
    account: str,
    repo_root: str | Path | None = None,
    observed_at_utc: str | None = None,
    scope: str = "full",
    mature_day_limit: int | None = None,
) -> dict[str, Any]:
    market, account = _identity(market, account)
    scope = _text(scope, "scope")
    if scope not in {"full", "latest_mature_window"}:
        _fail("formal_corpus_input_invalid", "scope is invalid")
    if scope == "full":
        if mature_day_limit is not None:
            _fail(
                "formal_corpus_input_invalid",
                "full scope does not accept mature_day_limit",
            )
    elif type(mature_day_limit) is not int or mature_day_limit <= 0:
        _fail(
            "formal_corpus_input_invalid",
            "latest_mature_window requires a positive mature_day_limit",
        )
    normalized_runtime_root = _runtime_root(runtime_root)
    observed_at = _timestamp(
        observed_at_utc
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "observed_at_utc",
    )
    local_date = (
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        .astimezone(ZoneInfo(_MARKET_TIMEZONES[market]))
        .date()
        .isoformat()
    )
    calendar_root = (
        normalized_runtime_root / "output_shared" / "research" / "strategy_lab"
    )
    try:
        calendar = read_market_calendar_binding(calendar_root, market=market)
    except FormalCorpusError:
        if scope == "latest_mature_window":
            raise
        calendar = None
    selected_dates: list[str] | None = None
    if scope == "latest_mature_window":
        assert calendar is not None and mature_day_limit is not None
        if not calendar["coverage_start"] <= local_date <= calendar["coverage_end"]:
            _fail(
                "market_calendar_binding_unavailable",
                f"{market} observation date is outside calendar coverage",
            )
        trading_dates = list(calendar["trading_dates"])
        selected_dates = [
            value for value in trading_dates if value < local_date
        ][-mature_day_limit:]
        if local_date in trading_dates:
            selected_dates.append(local_date)

    identity_root = _corpus_root(runtime_root) / market.lower() / account
    expectation_root = identity_root / "expectations"
    point_root = identity_root / "points"
    days: list[dict[str, Any]] = []
    layout_conflicts = 0
    expectation_dates: set[str] = set()
    candidate_dates: list[str] = []
    if selected_dates is None:
        if expectation_root.is_dir() and not expectation_root.is_symlink():
            for directory in sorted(expectation_root.iterdir()):
                if directory.is_symlink() or not directory.is_dir():
                    layout_conflicts += 1
                    continue
                try:
                    candidate_dates.append(_day(directory.name))
                except FormalCorpusError:
                    layout_conflicts += 1
    else:
        for trading_date in selected_dates:
            directory = expectation_root / trading_date
            if directory.is_symlink() or not directory.is_dir():
                if directory.exists() or directory.is_symlink():
                    layout_conflicts += 1
                continue
            candidate_dates.append(trading_date)
    for trading_date in candidate_dates:
        expectation_dates.add(trading_date)
        try:
            expectations = _expectation_artifacts(
                runtime_root, market, account, trading_date
            )
        except FormalCorpusError:
            expectations = []
            conflict = True
        else:
            conflict = len(expectations) != 1
        if not expectations:
            days.append(
                {
                    "trading_date": trading_date,
                    "status": "conflict" if conflict else "missing",
                    "reason_code": (
                        "formal_corpus_conflict"
                        if conflict
                        else "formal_expectation_missing"
                    ),
                    "market_calendar_version": None,
                    "market_calendar_sha256": None,
                    "schedule_config_sha256": None,
                    "expected_point_count": 0,
                    "captured_point_count": 0,
                    "not_evaluable_point_count": 0,
                    "missing_point_count": 0,
                    "points": [],
                }
            )
            continue
        expectation = expectations[0][0]
        expected = list(expectation["expected_recommendation_point_ids"])
        conflict |= _point_day_layout_conflicts(
            runtime_root, market, account, trading_date, expected
        )
        captured = not_evaluable = missing = 0
        points: list[dict[str, Any]] = []
        for point_id in expected:
            try:
                loaded = load_formal_point(
                    runtime_root,
                    market=market,
                    account=account,
                    trading_date=trading_date,
                    recommendation_point_id=point_id,
                )
            except FormalCorpusError as exc:
                loaded = {
                    "status": "conflict",
                    "reason_code": exc.reason_code,
                    "point": None,
                }
            point = loaded.get("point")
            recommendation_point = (
                point.get("recommendation_point")
                if isinstance(point, Mapping)
                else None
            )
            coherence = (
                recommendation_point.get("formal_point_time_coherence")
                if isinstance(recommendation_point, Mapping)
                else None
            )
            points.append(
                {
                    "recommendation_point_id": point_id,
                    "status": loaded["status"],
                    "reason_code": loaded.get("reason_code"),
                    "captured_at_utc": (
                        point.get("captured_at_utc")
                        if isinstance(point, Mapping)
                        else None
                    ),
                    "source_observed_at_utc": (
                        coherence.get("maximum_observed_at_utc")
                        if isinstance(coherence, Mapping)
                        else None
                    ),
                    "time_coherence": (
                        dict(coherence) if isinstance(coherence, Mapping) else None
                    ),
                }
            )
            if loaded["status"] == "available":
                captured += 1
            elif loaded["status"] == "not_evaluable":
                not_evaluable += 1
            elif loaded["status"] == "missing":
                missing += 1
            else:
                conflict = True
        day_status = "conflict" if conflict else "complete" if (
            expectation["sealed_before_first_target"]
            and expected
            and captured == len(expected)
        ) else "incomplete"
        days.append(
            {
                "trading_date": trading_date,
                "status": day_status,
                "reason_code": (
                    "formal_corpus_conflict"
                    if conflict
                    else None if day_status == "complete" else "formal_day_incomplete"
                ),
                "market_calendar_version": expectation["market_calendar_version"],
                "market_calendar_sha256": expectation["market_calendar_sha256"],
                "schedule_config_sha256": expectation["schedule_config_sha256"],
                "expected_point_count": len(expected),
                "captured_point_count": captured,
                "not_evaluable_point_count": not_evaluable,
                "missing_point_count": missing,
                "points": points,
            }
        )
    if selected_dates is None and point_root.is_dir() and not point_root.is_symlink():
        for directory in point_root.iterdir():
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or directory.name not in expectation_dates
            ):
                layout_conflicts += 1
    elif selected_dates is not None:
        for trading_date in selected_dates:
            if trading_date in expectation_dates:
                continue
            directory = point_root / trading_date
            if directory.exists() or directory.is_symlink():
                layout_conflicts += 1
    if calendar is not None:
        rows_by_date = {item["trading_date"]: item for item in days}
        trading_dates = list(calendar["trading_dates"])
        existing_dates = sorted(rows_by_date)
        if selected_dates is not None:
            denominator = selected_dates
        else:
            if existing_dates:
                start = existing_dates[0]
                end = max(
                    existing_dates[-1],
                    local_date
                    if local_date in trading_dates
                    else existing_dates[-1],
                )
            elif local_date in trading_dates:
                start = end = local_date
            else:
                start = end = None
            denominator = (
                [value for value in trading_dates if start <= value <= end]
                if start is not None and end is not None
                else []
            )
        if denominator:
            if any(value not in trading_dates for value in existing_dates):
                layout_conflicts += 1
            for value in denominator:
                if value not in rows_by_date:
                    rows_by_date[value] = {
                        "trading_date": value,
                        "status": "missing",
                        "reason_code": "formal_expectation_missing",
                        "market_calendar_version": calendar[
                            "market_calendar_version"
                        ],
                        "market_calendar_sha256": calendar[
                            "snapshot_content_sha256"
                        ],
                        "schedule_config_sha256": None,
                        "expected_point_count": 0,
                        "captured_point_count": 0,
                        "not_evaluable_point_count": 0,
                        "missing_point_count": 0,
                        "points": [],
                    }
            days = [rows_by_date[value] for value in sorted(rows_by_date)]
            suffix = 0
            for value in reversed(
                [item for item in denominator if item < local_date]
            ):
                if rows_by_date[value]["status"] != "complete":
                    break
                suffix += 1
        else:
            suffix = 0
    else:
        suffix = 0
    artifact_files: set[Path] = set()
    if selected_dates is None:
        for root in (expectation_root, point_root):
            if root.is_dir() and not root.is_symlink():
                for pattern in ("**/*.json", "**/*.json.gz"):
                    artifact_files.update(
                        path
                        for path in root.glob(pattern)
                        if path.is_file() and not path.is_symlink()
                    )
    else:
        for trading_date in selected_dates:
            artifact_files.update(
                path
                for path in (expectation_root / trading_date).glob("*.json")
                if path.is_file() and not path.is_symlink()
            )
            artifact_files.update(
                path
                for path in (point_root / trading_date).glob("*/*.json.gz")
                if path.is_file() and not path.is_symlink()
            )
    capacity: dict[str, Any] = {
        "status": "unavailable",
        "filesystem_capacity_bytes": None,
        "current_free_bytes": None,
        "critical_floor_bytes": None,
        "critical_reasons": [],
    }
    try:
        disk = shutil.disk_usage(normalized_runtime_root)
    except OSError:
        pass
    else:
        total_bytes = int(disk.total)
        free_bytes = int(disk.free)
        critical_floor = max((total_bytes + 19) // 20, 10 * 1024**3)
        critical_reasons = (
            ["current_free_space_below_critical_floor"]
            if free_bytes < critical_floor
            else []
        )
        capacity = {
            "status": "critical" if critical_reasons else "insufficient_history",
            "filesystem_capacity_bytes": total_bytes,
            "current_free_bytes": free_bytes,
            "critical_floor_bytes": critical_floor,
            "critical_reasons": critical_reasons,
        }
    storage: dict[str, Any] = {
        "formal_corpus_bytes": sum(path.stat().st_size for path in artifact_files),
        "capacity": capacity,
    }
    totals = {
        "days_total": len(days),
        "days_complete": sum(item["status"] == "complete" for item in days),
        "days_incomplete": sum(item["status"] == "incomplete" for item in days),
        "days_missing": sum(item["status"] == "missing" for item in days),
        "days_conflicting": sum(item["status"] == "conflict" for item in days),
        "expected_points_total": sum(item["expected_point_count"] for item in days),
        "points_captured": sum(item["captured_point_count"] for item in days),
        "points_not_evaluable": sum(item["not_evaluable_point_count"] for item in days),
        "points_missing": sum(item["missing_point_count"] for item in days),
        "layout_conflicts": layout_conflicts,
    }
    available_points = [
        (day["trading_date"], point)
        for day in days
        for point in day["points"]
        if point["status"] == "available"
    ]
    latest_success = max(
        available_points,
        key=lambda item: str(item[1]["captured_at_utc"] or ""),
        default=None,
    )
    source_times = [
        str(point["source_observed_at_utc"])
        for _trading_date, point in available_points
        if point["source_observed_at_utc"] is not None
    ]
    latest_source = max(source_times, default=None)
    freshness_seconds = (
        int(
            (
                datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                - datetime.fromisoformat(latest_source.replace("Z", "+00:00"))
            ).total_seconds()
        )
        if latest_source is not None
        else None
    )
    complete_dates = [
        item["trading_date"] for item in days if item["status"] == "complete"
    ]
    capacity_status = capacity["status"]
    healthy = bool(
        calendar is not None
        and days
        and all(item["status"] == "complete" for item in days)
        and layout_conflicts == 0
        and capacity_status == "insufficient_history"
    )
    return {
        "schema_version": CORPUS_HEALTH_SCHEMA,
        "status": "healthy" if healthy else "unhealthy",
        "market": market,
        "account": account,
        "observed_at_utc": observed_at,
        "scope": {
            "mode": scope,
            "mature_day_limit": mature_day_limit,
            "include_current_trading_day": True,
        },
        **totals,
        "continuous_complete_trading_days": suffix,
        "earliest_complete_trading_date": complete_dates[0] if complete_dates else None,
        "latest_complete_trading_date": complete_dates[-1] if complete_dates else None,
        "earliest_trading_date": days[0]["trading_date"] if days else None,
        "latest_trading_date": days[-1]["trading_date"] if days else None,
        "latest_successful_point": (
            {
                "trading_date": latest_success[0],
                "recommendation_point_id": latest_success[1][
                    "recommendation_point_id"
                ],
                "captured_at_utc": latest_success[1]["captured_at_utc"],
            }
            if latest_success is not None
            else None
        ),
        "latest_source_observed_at_utc": latest_source,
        "freshness_seconds": freshness_seconds,
        "calendar": (
            {
                "status": "available",
                "market_calendar_version": calendar["market_calendar_version"],
                "snapshot_content_sha256": calendar["snapshot_content_sha256"],
            }
            if calendar is not None
            else {"status": "unavailable"}
        ),
        "days": days,
        "storage": storage,
        "experiment_requirements": {"fill": "not_required", "outcome": "not_required"},
    }


def seal_profile_formal_expectations(
    runtime_root: str | Path,
    *,
    profile: Mapping[str, Any],
    artifact_root: str | Path,
    occurred_at_utc: str,
) -> dict[str, Any]:
    occurred_at = _timestamp(occurred_at_utc, "occurred_at_utc")
    markets = profile.get("markets")
    accounts = profile.get("accounts")
    config_paths = profile.get("config_paths")
    results: list[dict[str, Any]] = []
    if not isinstance(markets, list) or not isinstance(accounts, list) or not isinstance(config_paths, Mapping):
        _fail("formal_corpus_input_invalid", "service profile scope is invalid")
    account_values = list(
        dict.fromkeys(_identity("HK", value)[1] for value in accounts)
    )
    if not account_values:
        return {"status": "not_applicable", "results": []}
    for market_key in ("hk", "us"):
        if market_key not in {str(value).strip().lower() for value in markets}:
            continue
        market = market_key.upper()
        market_accounts = account_values
        try:
            config_path = Path(str(config_paths.get(market_key) or "")).expanduser()
            _path, config = load_runtime_config(
                config_path=config_path,
                expected_market=market_key,
            )
            configured_accounts = set(accounts_from_config(config, fallback=()))
            market_accounts = [
                account for account in account_values if account in configured_accounts
            ]
            schedule = config.get("schedule")
            if not isinstance(schedule, Mapping):
                raise ValueError(f"{market} runtime schedule is missing")
            timezone_name = str(schedule.get("timezone") or "")
            if timezone_name != _MARKET_TIMEZONES[market]:
                raise ValueError(f"{market} runtime schedule timezone is invalid")
            trading_date = (
                datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                .astimezone(ZoneInfo(timezone_name))
                .date()
                .isoformat()
            )
            calendar = read_market_calendar_binding(artifact_root, market=market)
            if not calendar["coverage_start"] <= trading_date <= calendar["coverage_end"]:
                raise FormalCorpusError(
                    "market_calendar_binding_unavailable",
                    f"{market} trading date is outside calendar coverage",
                )
            session = next(
                (
                    row["trade_date_type"]
                    for row in calendar["trading_sessions"]
                    if row["trading_date"] == trading_date
                ),
                None,
            )
            if session is None:
                results.extend(
                    {
                        "market": market,
                        "account": account,
                        "trading_date": trading_date,
                        "status": "not_applicable",
                        "reason_code": "market_closed",
                    }
                    for account in market_accounts
                )
                continue
            results.extend(
                seal_formal_day_expectation(
                    runtime_root,
                    market=market,
                    account=account,
                    schedule=schedule,
                    trading_date=trading_date,
                    market_calendar_version=calendar["market_calendar_version"],
                    market_calendar_sha256=calendar["snapshot_content_sha256"],
                    sealed_at_utc=occurred_at,
                    trade_date_type=str(session),
                )
                for account in market_accounts
            )
        except Exception as exc:
            results.extend(
                {
                    "market": market,
                    "account": account,
                    "status": "not_evaluable",
                    "reason_code": str(
                        getattr(exc, "reason_code", "market_calendar_binding_unavailable")
                    ),
                    "message": str(exc),
                }
                for account in market_accounts
            )
    return {
        "status": (
            "ok"
            if all(item.get("status") not in {"conflict", "not_evaluable"} for item in results)
            else "degraded"
        ),
        "results": results,
    }


__all__ = [
    "CORPUS_HEALTH_SCHEMA",
    "FORMAL_CORPUS_VERSION",
    "FORMAL_EXPECTATION_SCHEMA",
    "FORMAL_POINT_MAX_SKEW_MS",
    "FORMAL_POINT_SCHEMA",
    "FORMAL_POINT_TIME_COHERENCE_SCHEMA",
    "MARKET_CALENDAR_POINTER_SCHEMA",
    "MARKET_CALENDAR_SNAPSHOT_SCHEMA",
    "FormalCorpusError",
    "build_corpus_health_receipt",
    "capture_formal_point_attempt",
    "formal_corpus_present",
    "load_formal_point",
    "load_formal_expectation",
    "read_bound_market_calendar_snapshot",
    "read_expectation_bound_market_calendar_snapshot",
    "read_market_calendar_binding",
    "refresh_market_calendar_binding",
    "seal_formal_day_expectation",
    "seal_profile_formal_expectations",
]
