from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    utc_timestamp,
)
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    load_opening_candidate_snapshot,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
    find_prepared_option_positions_manifest,
    load_prepared_option_positions_context_receipt,
)
from src.application.recommendation_point import (
    RECOMMENDATION_POINT_FILE,
    RECOMMENDATION_POINT_SCHEMA_V2,
    RecommendationPointError,
    build_recommendation_point_id,
    load_recommendation_point,
    point_binding_from_recommendation_point,
    strategy_lab_top1_available,
)
from src.application.scan_scheduler import scheduled_scan_targets_for_date
from src.application.research.formal_corpus import (
    FormalCorpusError,
    MARKET_CALENDAR_POINTER_SCHEMA,
    MARKET_CALENDAR_SNAPSHOT_SCHEMA,
    build_corpus_health_receipt as build_formal_corpus_health_receipt,
    formal_corpus_present,
    load_formal_expectation,
    load_formal_point,
    read_bound_market_calendar_snapshot as _read_bound_market_calendar_snapshot,
    read_market_calendar_binding as _read_market_calendar_binding,
    refresh_market_calendar_binding as _refresh_market_calendar_binding,
)
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.contracts import (
    RECOMMENDATION_POINT_SELECTOR,
    RESEARCH_REQUIRED_DAYS,
    SEALED_HISTORICAL_DATASET_SCHEMA,
)
from src.application.strategy_lab.top1.ranking import (
    RANKING_PROJECTION_SCHEMA_VERSION,
    RANKING_PROJECTION_SCHEMA_V2,
    RANKING_PROJECTION_SCHEMA_V3,
    Top1RankingError,
    build_top1_recipe_projection,
    build_ranking_projection,
    materialize_top1_recipe_input,
    rerank_recommendation_point,
    validate_ranking_projection,
)
from src.application.strategy_lab.top1.terminal_projection import publish_exact_text
from src.infrastructure.private_storage import (
    atomic_write_private_text,
    open_private_text,
    private_path,
)
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
)


CORPUS_DAY_EXPECTATION_SCHEMA = "corpus_day_expectation.v1"
CORPUS_COMMAND_RESULT_SCHEMA = "sell_put_top1_corpus_command_result.v1"
CORPUS_STATUS_SCHEMA = "sell_put_top1_corpus_status.v1"
CORPUS_HEALTH_RECEIPT_SCHEMA = "sell_put_top1_corpus_health_receipt.v1"
MAX_CORPUS_HEALTH_RECEIPT_BYTES = 16 * 1024
RESEARCH_WINDOW_FACTS_SCHEMA = "sell_put_top1_research_window_facts.v1"
DATASET_FREEZE_RESULT_SCHEMA = "sell_put_top1_dataset_freeze_result.v1"

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
_WINDOW_FACT_FIELDS = frozenset(
    {
        "schema_version",
        "market",
        "account",
        "cutoff_at_utc",
        "cutoff_trading_date",
        "market_calendar_version",
        "market_calendar_ref",
        "market_calendar_sha256",
        "trading_calendar_dates",
        "trading_calendar_dates_sha256",
        "latest_mature_trading_date",
        "maturity_evidence_ref",
        "maturity_evidence_sha256",
        "recommendation_point_selector",
        "content_sha256",
    }
)
_POINT_REF = re.compile(
    rf"output_runs/([^/]+)/accounts/([^/]+)/state/{re.escape(RECOMMENDATION_POINT_FILE)}\Z"
)
_SCHEDULER_DECISION_REF = re.compile(r"output_runs/([^/]+)/state/scheduler_decision.json\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class CorpusError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise CorpusError(reason_code, message)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("corpus_input_invalid", f"{label} must be canonical text")
    return value


def _segment(value: object, label: str) -> str:
    text = _text(value, label)
    if _SEGMENT.fullmatch(text) is None:
        _fail("corpus_input_invalid", f"{label} must be a safe path segment")
    return text


def _identity(market: object, account: object) -> tuple[str, str]:
    market_text = _text(market, "market")
    if market_text != "HK":
        _fail("corpus_input_invalid", "market must equal HK")
    account_text = _segment(account, "account")
    if account_text != account_text.lower():
        _fail("corpus_input_invalid", "account must be lowercase")
    return market_text, account_text


def _trading_date(value: object) -> str:
    text = _text(value, "trading_date")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise CorpusError("corpus_input_invalid", "trading_date must be an ISO date") from exc
    if parsed.isoformat() != text:
        _fail("corpus_input_invalid", "trading_date must be a canonical ISO date")
    return text


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        canonical = utc_timestamp(text, label)
    except CandidateSnapshotContractError as exc:
        raise CorpusError("corpus_input_invalid", str(exc)) from exc
    if text != canonical:
        _fail("corpus_input_invalid", f"{label} must be canonical UTC")
    return text


def _before(left: str, right: str) -> bool:
    return datetime.fromisoformat(left.replace("Z", "+00:00")) < datetime.fromisoformat(
        right.replace("Z", "+00:00")
    )


def _hash(value: object, label: str) -> str:
    text = _text(value, label)
    if _HASH.fullmatch(text) is None:
        _fail("corpus_input_invalid", f"{label} must be a lowercase SHA-256")
    return text


def _relative_ref(value: object, label: str) -> str:
    text = _text(value, label)
    parts = text.split("/")
    if text.startswith("/") or "\\" in text or any(
        part in {"", ".", ".."} for part in parts
    ):
        _fail("corpus_input_invalid", f"{label} must be a safe relative POSIX path")
    return text


def _expectation_ref(market: str, account: str, trading_date: str) -> str:
    return (
        f"strategy_lab/top1/corpus/{market.lower()}/{account}/days/"
        f"{trading_date}.expectation.json"
    )


def _projection_ref(market: str, account: str, point_id: str) -> str:
    return f"strategy_lab/top1/corpus/{market.lower()}/{account}/points/{point_id}.json"


def _dataset_ref(market: str, account: str, content_sha256: str) -> str:
    return (
        f"strategy_lab/top1/corpus/{market.lower()}/{account}/datasets/"
        f"{content_sha256}.json"
    )


def _corpus_health_current_ref(market: str, account: str) -> str:
    return (
        f"strategy_lab/top1/corpus/{market.lower()}/{account}/health/current.json"
    )


def _corpus_health_day_ref(
    market: str, account: str, trading_date: str
) -> str:
    return (
        f"strategy_lab/top1/corpus/{market.lower()}/{account}/health/days/"
        f"{trading_date}.json"
    )


def _render(payload: dict[str, Any]) -> bytes:
    return render_json_text(payload).encode("utf-8")


def _file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_bound_market_calendar_snapshot(
    artifact_root: str | Path,
    *,
    market: str,
    snapshot_ref: str,
    snapshot_content_sha256: str,
    snapshot_file_sha256: str,
) -> dict[str, Any]:
    return _calendar_call(
        _read_bound_market_calendar_snapshot,
        artifact_root,
        market=market,
        snapshot_ref=snapshot_ref,
        snapshot_content_sha256=snapshot_content_sha256,
        snapshot_file_sha256=snapshot_file_sha256,
    )


def read_market_calendar_binding(
    artifact_root: str | Path,
    *,
    market: str,
) -> dict[str, Any]:
    return _calendar_call(
        _read_market_calendar_binding,
        artifact_root,
        market=market,
    )


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
    return _calendar_call(
        _refresh_market_calendar_binding,
        artifact_root,
        gateway=gateway,
        market=market,
        market_calendar_version=market_calendar_version,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        observed_at_utc=observed_at_utc,
    )


def _calendar_call(function: Any, /, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except FormalCorpusError as exc:
        raise CorpusError(exc.reason_code, str(exc)) from exc


def _command_result(
    *,
    operation: str,
    status: str,
    reason_code: str | None,
    market: str,
    account: str,
    trading_date: str | None,
    recommendation_point_id: str | None = None,
    artifact_ref: str | None = None,
    artifact_sha256: str | None = None,
    artifact_content_sha256: str | None = None,
    expected_point_count: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": CORPUS_COMMAND_RESULT_SCHEMA,
        "operation": operation,
        "status": status,
        "reason_code": reason_code,
        "market": market,
        "account": account,
        "trading_date": trading_date,
        "recommendation_point_id": recommendation_point_id,
        "artifact_ref": artifact_ref,
        "artifact_sha256": artifact_sha256,
        "artifact_content_sha256": artifact_content_sha256,
        "expected_point_count": expected_point_count,
    }


def _service_available(environ: Mapping[str, str] | None) -> bool:
    return strategy_lab_top1_available(environ)


def _store_call(function: Any, /, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except ExperimentStoreError as exc:
        reason = (
            "schema_unsupported"
            if exc.reason_code == "schema_unsupported"
            else "corpus_artifact_conflict"
        )
        raise CorpusError(reason, str(exc)) from exc


def _expectation_reason(payload: Mapping[str, Any]) -> str | None:
    targets = list(payload["scheduled_scan_targets_market"])
    if not targets:
        return "corpus_day_expectation_empty"
    if payload["sealed_before_first_target"] is not True:
        return "corpus_day_expectation_late"
    return None


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
    expected_market: str,
    expected_account: str,
    expected_trading_date: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _fail("corpus_artifact_invalid", "day expectation must be an object")
    item = dict(payload)
    if set(item) != _EXPECTATION_FIELDS:
        _fail("corpus_artifact_invalid", "day expectation keys are invalid")
    if item["schema_version"] != CORPUS_DAY_EXPECTATION_SCHEMA:
        _fail("corpus_artifact_invalid", "day expectation schema is invalid")
    if (
        item["market"] != expected_market
        or item["account"] != expected_account
        or item["trading_date"] != expected_trading_date
    ):
        _fail("corpus_artifact_invalid", "day expectation identity does not match")
    _text(item["market_calendar_version"], "market_calendar_version")
    _hash(item["market_calendar_sha256"], "market_calendar_sha256")
    _hash(item["schedule_config_sha256"], "schedule_config_sha256")
    sealed_at = _timestamp(item["sealed_at_utc"], "sealed_at_utc")
    targets = item["scheduled_scan_targets_market"]
    point_ids = item["expected_recommendation_point_ids"]
    if not isinstance(targets, list) or not isinstance(point_ids, list):
        _fail("corpus_artifact_invalid", "expectation targets and point IDs must be lists")
    canonical_targets = [
        _timestamp(value, f"scheduled_scan_targets_market[{index}]")
        for index, value in enumerate(targets)
    ]
    if canonical_targets != sorted(set(canonical_targets)):
        _fail("corpus_artifact_invalid", "expectation targets must be ordered and unique")
    expected_ids = [
        build_recommendation_point_id(expected_market, expected_account, target)
        for target in canonical_targets
    ]
    if point_ids != expected_ids:
        _fail("corpus_artifact_invalid", "expectation point IDs do not match targets")
    first_target = canonical_targets[0] if canonical_targets else None
    if item["first_target_at_utc"] != first_target:
        _fail("corpus_artifact_invalid", "expectation first target does not match")
    before = bool(first_target and _before(sealed_at, first_target))
    if item["sealed_before_first_target"] is not before:
        _fail("corpus_artifact_invalid", "expectation seal timing does not match")
    content = {key: value for key, value in item.items() if key != "content_sha256"}
    if canonical_sha256(content) != item["content_sha256"]:
        _fail("corpus_artifact_invalid", "day expectation content hash does not match")
    return item


def _read_expectation(
    artifact_root: str | Path,
    ref: str,
    *,
    market: str,
    account: str,
    trading_date: str,
) -> tuple[dict[str, Any], bytes]:
    if ref != _expectation_ref(market, account, trading_date):
        _fail("corpus_artifact_invalid", "day expectation ref is invalid")
    path = private_path(artifact_root).joinpath(*ref.split("/"))
    try:
        with open_private_text(path) as handle:
            content = handle.read().encode("utf-8")
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusError(
            "corpus_artifact_invalid", "day expectation artifact is unavailable"
        ) from exc
    try:
        item = _validate_expectation(
            payload,
            expected_market=market,
            expected_account=account,
            expected_trading_date=trading_date,
        )
    except CorpusError as exc:
        if exc.reason_code == "corpus_artifact_invalid":
            raise
        raise CorpusError("corpus_artifact_invalid", str(exc)) from exc
    except RecommendationPointError as exc:
        raise CorpusError("corpus_artifact_invalid", str(exc)) from exc
    if content != _render(item):
        _fail("corpus_artifact_invalid", "day expectation bytes are not canonical")
    return item, content


def _read_indexed_expectation(
    artifact_root: str | Path,
    row: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    payload, content = _read_expectation(
        artifact_root,
        str(row["expectation_ref"]),
        market=str(row["market"]),
        account=str(row["account"]),
        trading_date=str(row["trading_date"]),
    )
    expected = {
        "expectation_content_sha256": payload["content_sha256"],
        "expectation_file_sha256": _file_sha256(content),
        "market_calendar_version": payload["market_calendar_version"],
        "market_calendar_sha256": payload["market_calendar_sha256"],
        "schedule_config_sha256": payload["schedule_config_sha256"],
        "expected_point_count": len(payload["expected_recommendation_point_ids"]),
        "first_target_at_utc": payload["first_target_at_utc"],
        "sealed_at_utc": payload["sealed_at_utc"],
        "sealed_before_first_target": int(payload["sealed_before_first_target"]),
        "completeness_reason": _expectation_reason(payload),
    }
    if any(row[key] != value for key, value in expected.items()):
        _fail("corpus_artifact_invalid", "day expectation index binding does not match")
    return payload, content


def _record_day(
    store: ExperimentStore,
    payload: Mapping[str, Any],
    content: bytes,
    *,
    conflict_observed: bool = False,
) -> dict[str, Any]:
    return _store_call(
        store.record_corpus_day,
        market=payload["market"],
        account=payload["account"],
        trading_date=payload["trading_date"],
        expectation_ref=_expectation_ref(
            payload["market"], payload["account"], payload["trading_date"]
        ),
        expectation_content_sha256=payload["content_sha256"],
        expectation_file_sha256=_file_sha256(content),
        market_calendar_version=payload["market_calendar_version"],
        market_calendar_sha256=payload["market_calendar_sha256"],
        schedule_config_sha256=payload["schedule_config_sha256"],
        expected_point_count=len(payload["expected_recommendation_point_ids"]),
        first_target_at_utc=payload["first_target_at_utc"],
        sealed_at_utc=payload["sealed_at_utc"],
        sealed_before_first_target=payload["sealed_before_first_target"],
        completeness_reason=_expectation_reason(payload),
        conflict_observed=conflict_observed,
    )


def _mark_day_conflict(store: ExperimentStore, row: Mapping[str, Any]) -> None:
    _store_call(
        store.record_corpus_day,
        market=row["market"],
        account=row["account"],
        trading_date=row["trading_date"],
        expectation_ref=row["expectation_ref"],
        expectation_content_sha256=row["expectation_content_sha256"],
        expectation_file_sha256=row["expectation_file_sha256"],
        market_calendar_version=row["market_calendar_version"],
        market_calendar_sha256=row["market_calendar_sha256"],
        schedule_config_sha256=row["schedule_config_sha256"],
        expected_point_count=row["expected_point_count"],
        first_target_at_utc=row["first_target_at_utc"],
        sealed_at_utc=row["sealed_at_utc"],
        sealed_before_first_target=bool(row["sealed_before_first_target"]),
        completeness_reason=row["completeness_reason"],
        conflict_observed=True,
    )


def _seal_result(
    payload: Mapping[str, Any],
    content: bytes,
    *,
    status: str,
) -> dict[str, Any]:
    reason = _expectation_reason(payload)
    if status == "conflict":
        return _command_result(
            operation="seal_day_expectation",
            status="conflict",
            reason_code="research_corpus_conflict",
            market=payload["market"],
            account=payload["account"],
            trading_date=payload["trading_date"],
        )
    elif reason is not None:
        status = "not_evaluable"
    return _command_result(
        operation="seal_day_expectation",
        status=status,
        reason_code=reason,
        market=payload["market"],
        account=payload["account"],
        trading_date=payload["trading_date"],
        artifact_ref=_expectation_ref(
            payload["market"], payload["account"], payload["trading_date"]
        ),
        artifact_sha256=_file_sha256(content),
        artifact_content_sha256=payload["content_sha256"],
        expected_point_count=len(payload["expected_recommendation_point_ids"]),
    )


def _build_expectation(
    *,
    market: str,
    account: str,
    trading_date: str,
    market_calendar_version: str,
    market_calendar_sha256: str,
    schedule_config_sha256: str,
    targets: list[str],
    sealed_at_utc: str,
) -> tuple[dict[str, Any], bytes]:
    canonical_targets = [
        _timestamp(target, f"scheduled_scan_targets_market[{index}]")
        for index, target in enumerate(targets)
    ]
    if canonical_targets != sorted(set(canonical_targets)):
        _fail("corpus_input_invalid", "scheduled targets must be ordered and unique")
    point_ids = [
        build_recommendation_point_id(market, account, target)
        for target in canonical_targets
    ]
    first_target = canonical_targets[0] if canonical_targets else None
    payload: dict[str, Any] = {
        "schema_version": CORPUS_DAY_EXPECTATION_SCHEMA,
        "market": market,
        "account": account,
        "trading_date": trading_date,
        "market_calendar_version": market_calendar_version,
        "market_calendar_sha256": market_calendar_sha256,
        "schedule_config_sha256": schedule_config_sha256,
        "sealed_at_utc": sealed_at_utc,
        "first_target_at_utc": first_target,
        "sealed_before_first_target": bool(
            first_target and _before(sealed_at_utc, first_target)
        ),
        "scheduled_scan_targets_market": canonical_targets,
        "expected_recommendation_point_ids": point_ids,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload, _render(payload)


def _persist_expectation(
    store: ExperimentStore,
    artifact_root: str | Path,
    payload: Mapping[str, Any],
    content: bytes,
) -> dict[str, Any]:
    market = str(payload["market"])
    account = str(payload["account"])
    day = str(payload["trading_date"])
    ref = _expectation_ref(market, account, day)

    existing = _store_call(store.corpus_day, market, account, day)
    if existing is not None:
        if existing["conflict_status"] == "conflict":
            return _seal_result(payload, content, status="conflict")
        try:
            adopted, adopted_content = _read_indexed_expectation(
                artifact_root, existing
            )
        except CorpusError:
            _mark_day_conflict(store, existing)
            return _seal_result(payload, content, status="conflict")
        if _expectation_denominator(adopted) != _expectation_denominator(payload):
            _mark_day_conflict(store, existing)
            return _seal_result(adopted, adopted_content, status="conflict")
        return _seal_result(adopted, adopted_content, status="idempotent")

    try:
        publish_exact_text(artifact_root, ref, content)
    except ValueError:
        try:
            adopted, adopted_content = _read_expectation(
                artifact_root,
                ref,
                market=market,
                account=account,
                trading_date=day,
            )
        except CorpusError:
            _record_day(store, payload, content, conflict_observed=True)
            return _seal_result(payload, content, status="conflict")
        if _expectation_denominator(adopted) != _expectation_denominator(payload):
            _record_day(store, payload, content, conflict_observed=True)
            return _seal_result(adopted, adopted_content, status="conflict")
        payload, content = adopted, adopted_content
    except OSError as exc:
        raise CorpusError(
            "corpus_artifact_conflict", "day expectation cannot be published"
        ) from exc

    recorded = _record_day(store, payload, content)
    status = {
        "inserted": "published",
        "idempotent": "idempotent",
        "conflict": "conflict",
    }[recorded["status"]]
    return _seal_result(payload, content, status=status)


def seal_day_expectation(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    schedule: Mapping[str, Any],
    trading_date: str,
    market_calendar_version: str,
    market_calendar_sha256: str,
    sealed_at_utc: str,
    trade_date_type: str = "WHOLE",
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    market, account = _identity(market, account)
    day = _trading_date(trading_date)
    calendar_version = _text(market_calendar_version, "market_calendar_version")
    calendar_hash = _hash(market_calendar_sha256, "market_calendar_sha256")
    sealed_at = _timestamp(sealed_at_utc, "sealed_at_utc")
    if not isinstance(schedule, Mapping):
        _fail("corpus_input_invalid", "schedule must be an object")
    if not _service_available(environ):
        return _command_result(
            operation="seal_day_expectation",
            status="not_evaluable",
            reason_code="strategy_lab_service_disabled",
            market=market,
            account=account,
            trading_date=day,
        )

    try:
        targets = scheduled_scan_targets_for_date(
            dict(schedule), day, trade_date_type=trade_date_type
        )
        schedule_hash = canonical_sha256(dict(schedule))
    except (TypeError, ValueError) as exc:
        raise CorpusError("corpus_input_invalid", "schedule is invalid") from exc
    target_texts = [
        utc_timestamp(target, "scheduled_scan_target_market") for target in targets
    ]
    payload, content = _build_expectation(
        market=market,
        account=account,
        trading_date=day,
        market_calendar_version=calendar_version,
        market_calendar_sha256=calendar_hash,
        schedule_config_sha256=schedule_hash,
        targets=target_texts,
        sealed_at_utc=sealed_at,
    )
    return _persist_expectation(store, artifact_root, payload, content)


def seal_committed_day_expectation(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    committed_day: Mapping[str, Any],
    market_calendar_version: str,
    market_calendar_sha256: str,
    schedule_config_sha256: str,
    sealed_at_utc: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Seal the shared M4 expectation from an exact M3 commitment denominator."""

    market, account = _identity(market, account)
    if not isinstance(committed_day, Mapping) or set(committed_day) != {
        "trading_date",
        "scheduled_scan_targets_market",
        "expected_recommendation_point_ids",
    }:
        _fail("corpus_input_invalid", "committed day is invalid")
    day = _trading_date(committed_day["trading_date"])
    calendar_version = _text(market_calendar_version, "market_calendar_version")
    calendar_hash = _hash(market_calendar_sha256, "market_calendar_sha256")
    schedule_hash = _hash(schedule_config_sha256, "schedule_config_sha256")
    sealed_at = _timestamp(sealed_at_utc, "sealed_at_utc")
    targets = committed_day["scheduled_scan_targets_market"]
    point_ids = committed_day["expected_recommendation_point_ids"]
    if not isinstance(targets, list) or not isinstance(point_ids, list):
        _fail("corpus_input_invalid", "committed targets and point IDs must be lists")
    if not _service_available(environ):
        return _command_result(
            operation="seal_day_expectation",
            status="not_evaluable",
            reason_code="strategy_lab_service_disabled",
            market=market,
            account=account,
            trading_date=day,
        )
    payload, content = _build_expectation(
        market=market,
        account=account,
        trading_date=day,
        market_calendar_version=calendar_version,
        market_calendar_sha256=calendar_hash,
        schedule_config_sha256=schedule_hash,
        targets=list(targets),
        sealed_at_utc=sealed_at,
    )
    if payload["expected_recommendation_point_ids"] != point_ids:
        _fail("corpus_input_invalid", "committed point IDs do not match targets")
    return _persist_expectation(store, artifact_root, payload, content)


def _record_point(
    store: ExperimentStore,
    point: Mapping[str, Any],
    *,
    trading_date: str,
    captured_at_utc: str,
    capture_status: str,
    reason_code: str | None,
    projection: Mapping[str, Any] | None = None,
    projection_content: bytes | None = None,
    conflict_observed: bool = False,
) -> dict[str, Any]:
    projection_ref = None
    projection_content_sha256 = None
    projection_file_sha256 = None
    projection_schema = None
    if projection is not None and projection_content is not None:
        projection_ref = _projection_ref(
            point["market"], point["account"], point["recommendation_point_id"]
        )
        projection_content_sha256 = projection["artifact_provenance"]["content_sha256"]
        projection_file_sha256 = _file_sha256(projection_content)
        projection_schema = projection["schema_version"]
    return _store_call(
        store.record_corpus_point,
        market=point["market"],
        account=point["account"],
        recommendation_point_id=point["recommendation_point_id"],
        trading_date=trading_date,
        source_run_id=point["run_id"],
        source_point_ref=(
            f"output_runs/{point['run_id']}/accounts/{point['account']}/state/"
            f"{RECOMMENDATION_POINT_FILE}"
        ),
        source_point_content_sha256=point["content_sha256"],
        opening_snapshot_ref=point["opening_snapshot_ref"],
        opening_snapshot_sha256=point["opening_snapshot_sha256"],
        ranking_projection_schema_version=projection_schema,
        projection_ref=projection_ref,
        projection_content_sha256=projection_content_sha256,
        projection_file_sha256=projection_file_sha256,
        captured_at_utc=captured_at_utc,
        capture_status=capture_status,
        reason_code=reason_code,
        conflict_observed=conflict_observed,
    )


def _capture_not_evaluable(
    store: ExperimentStore,
    point: Mapping[str, Any],
    *,
    trading_date: str,
    captured_at_utc: str,
    reason_code: str,
    expected_point_count: int,
) -> dict[str, Any]:
    recorded = _record_point(
        store,
        point,
        trading_date=trading_date,
        captured_at_utc=captured_at_utc,
        capture_status="not_evaluable",
        reason_code=reason_code,
    )
    if recorded["status"] == "conflict":
        return _command_result(
            operation="capture_recommendation_point",
            status="conflict",
            reason_code="research_corpus_conflict",
            market=point["market"],
            account=point["account"],
            trading_date=trading_date,
            recommendation_point_id=point["recommendation_point_id"],
            expected_point_count=expected_point_count,
        )
    return _command_result(
        operation="capture_recommendation_point",
        status=(
            "idempotent"
            if recorded["status"] == "idempotent"
            else "not_evaluable"
        ),
        reason_code=reason_code,
        market=point["market"],
        account=point["account"],
        trading_date=trading_date,
        recommendation_point_id=point["recommendation_point_id"],
        expected_point_count=expected_point_count,
    )


def _build_point_ranking_projection(
    source_root: str | Path,
    point: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    option_market_evidence = None
    if point["schema_version"] == RECOMMENDATION_POINT_SCHEMA_V2:
        prepared_manifest = find_prepared_option_positions_manifest(
            base=Path(source_root),
            run_id=str(point["run_id"]),
            account=str(point["account"]),
        )
        if prepared_manifest is None:
            raise PreparedOptionPositionsContextError(
                "option_market_evidence_contract_missing"
            )
        prepared_receipt = load_prepared_option_positions_context_receipt(
            manifest_path=prepared_manifest,
            expected_base=Path(source_root),
            expected_run_id=str(point["run_id"]),
            expected_account=str(point["account"]),
            expected_account_config_sha256=str(snapshot["account_config_sha256"]),
            expected_manifest_sha256=str(
                point["option_market_evidence_manifest_sha256"]
            ),
            require_option_market_evidence=True,
        )
        if prepared_receipt["manifest"].get("payload_sha256") != point[
            "option_market_evidence_payload_sha256"
        ]:
            raise PreparedOptionPositionsContextError(
                "prepared option payload generation mismatch"
            )
        option_market_evidence = prepared_receipt["payload"].get(
            "strategy_lab_option_market_evidence"
        )
    return build_ranking_projection(
        snapshot,
        point_binding=point_binding_from_recommendation_point(point),
        option_market_evidence=option_market_evidence,
        require_option_market_evidence=(
            point["schema_version"] == RECOMMENDATION_POINT_SCHEMA_V2
        ),
    )


def capture_recommendation_point(
    store: ExperimentStore,
    source_root: str | Path,
    artifact_root: str | Path,
    *,
    point_ref: str,
    trading_date: str,
    captured_at_utc: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    ref = _text(point_ref, "point_ref")
    matched = _POINT_REF.fullmatch(ref)
    if matched is None:
        _fail("corpus_input_invalid", "point_ref is not the canonical M2 ref")
    run_id = _segment(matched.group(1), "run_id")
    account_from_ref = _segment(matched.group(2), "account")
    if account_from_ref != account_from_ref.lower():
        _fail("corpus_input_invalid", "point_ref account must be lowercase")
    day = _trading_date(trading_date)
    captured_at = _timestamp(captured_at_utc, "captured_at_utc")
    try:
        point = load_recommendation_point(Path(source_root), run_id, account_from_ref)
    except RecommendationPointError as exc:
        raise CorpusError("corpus_artifact_invalid", str(exc)) from exc
    market, account = _identity(point["market"], point["account"])
    if point["run_id"] != run_id or account != account_from_ref:
        _fail("corpus_artifact_invalid", "point ref and body identity do not match")
    if _before(captured_at, str(point["decision_at_utc"])):
        _fail("corpus_input_invalid", "captured_at_utc cannot precede decision_at_utc")
    if not _service_available(environ):
        return _command_result(
            operation="capture_recommendation_point",
            status="not_evaluable",
            reason_code="strategy_lab_service_disabled",
            market=market,
            account=account,
            trading_date=day,
            recommendation_point_id=point["recommendation_point_id"],
        )

    day_row = _store_call(store.corpus_day, market, account, day)
    if day_row is None:
        return _command_result(
            operation="capture_recommendation_point",
            status="not_evaluable",
            reason_code="corpus_day_expectation_missing",
            market=market,
            account=account,
            trading_date=day,
            recommendation_point_id=point["recommendation_point_id"],
        )
    expected_count = int(day_row["expected_point_count"])
    if day_row["conflict_status"] == "conflict":
        return _command_result(
            operation="capture_recommendation_point",
            status="conflict",
            reason_code="research_corpus_conflict",
            market=market,
            account=account,
            trading_date=day,
            recommendation_point_id=point["recommendation_point_id"],
            expected_point_count=expected_count,
        )
    if day_row["completeness_reason"] is not None:
        return _command_result(
            operation="capture_recommendation_point",
            status="not_evaluable",
            reason_code="corpus_day_not_evaluable",
            market=market,
            account=account,
            trading_date=day,
            recommendation_point_id=point["recommendation_point_id"],
            expected_point_count=expected_count,
        )
    try:
        expectation, _expectation_content = _read_indexed_expectation(
            artifact_root, day_row
        )
    except CorpusError:
        _mark_day_conflict(store, day_row)
        return _command_result(
            operation="capture_recommendation_point",
            status="conflict",
            reason_code="research_corpus_conflict",
            market=market,
            account=account,
            trading_date=day,
            recommendation_point_id=point["recommendation_point_id"],
            expected_point_count=expected_count,
        )
    if point["recommendation_point_id"] not in expectation[
        "expected_recommendation_point_ids"
    ]:
        return _command_result(
            operation="capture_recommendation_point",
            status="not_evaluable",
            reason_code="unexpected_recommendation_point",
            market=market,
            account=account,
            trading_date=day,
            recommendation_point_id=point["recommendation_point_id"],
            expected_point_count=expected_count,
        )

    if point["terminal_sell_put_status"] in {"partial_data", "data_unavailable"}:
        return _capture_not_evaluable(
            store,
            point,
            trading_date=day,
            captured_at_utc=captured_at,
            reason_code="official_decision_incomplete",
            expected_point_count=expected_count,
        )

    try:
        snapshot = load_opening_candidate_snapshot(
            base=Path(source_root),
            run_id=run_id,
            account=account,
            require_current_contract=True,
        )
    except OpeningCandidateSnapshotError as exc:
        reason = (
            "opening_snapshot_missing"
            if "unavailable" in str(exc).lower()
            else "opening_snapshot_conflict"
        )
        return _capture_not_evaluable(
            store,
            point,
            trading_date=day,
            captured_at_utc=captured_at,
            reason_code=reason,
            expected_point_count=expected_count,
        )
    if snapshot.get("content_sha256") != point["opening_snapshot_sha256"]:
        return _capture_not_evaluable(
            store,
            point,
            trading_date=day,
            captured_at_utc=captured_at,
            reason_code="opening_snapshot_conflict",
            expected_point_count=expected_count,
        )
    try:
        projection = _build_point_ranking_projection(source_root, point, snapshot)
    except (
        PreparedOptionPositionsContextError,
        RecommendationPointError,
        Top1RankingError,
    ):
        return _capture_not_evaluable(
            store,
            point,
            trading_date=day,
            captured_at_utc=captured_at,
            reason_code="ranking_projection_incomplete",
            expected_point_count=expected_count,
        )
    if projection["producer_accepted_candidate_ids"] != point[
        "producer_accepted_candidate_ids"
    ]:
        return _capture_not_evaluable(
            store,
            point,
            trading_date=day,
            captured_at_utc=captured_at,
            reason_code="ranking_projection_incomplete",
            expected_point_count=expected_count,
        )

    projection_content = _render(projection)
    projection_ref = _projection_ref(
        market, account, point["recommendation_point_id"]
    )
    try:
        publish_exact_text(artifact_root, projection_ref, projection_content)
    except ValueError:
        recorded = _record_point(
            store,
            point,
            trading_date=day,
            captured_at_utc=captured_at,
            capture_status="captured",
            reason_code=None,
            projection=projection,
            projection_content=projection_content,
            conflict_observed=True,
        )
        _ = recorded
        return _command_result(
            operation="capture_recommendation_point",
            status="conflict",
            reason_code="research_corpus_conflict",
            market=market,
            account=account,
            trading_date=day,
            recommendation_point_id=point["recommendation_point_id"],
            expected_point_count=expected_count,
        )
    except OSError as exc:
        raise CorpusError(
            "corpus_artifact_conflict", "ranking projection cannot be published"
        ) from exc

    recorded = _record_point(
        store,
        point,
        trading_date=day,
        captured_at_utc=captured_at,
        capture_status="captured",
        reason_code=None,
        projection=projection,
        projection_content=projection_content,
    )
    if recorded["status"] == "conflict":
        return _command_result(
            operation="capture_recommendation_point",
            status="conflict",
            reason_code="research_corpus_conflict",
            market=market,
            account=account,
            trading_date=day,
            recommendation_point_id=point["recommendation_point_id"],
            expected_point_count=expected_count,
        )
    return _command_result(
        operation="capture_recommendation_point",
        status=("published" if recorded["status"] == "inserted" else "idempotent"),
        reason_code=None,
        market=market,
        account=account,
        trading_date=day,
        recommendation_point_id=point["recommendation_point_id"],
        artifact_ref=projection_ref,
        artifact_sha256=_file_sha256(projection_content),
        artifact_content_sha256=projection["artifact_provenance"]["content_sha256"],
        expected_point_count=expected_count,
    )


def discover_recommendation_points(
    source_root: str | Path,
    *,
    market: str,
    account: str,
) -> list[dict[str, Any]]:
    """Validate retained canonical M2 points and project only routing facts."""

    market, account = _identity(market, account)
    root = Path(source_root)
    pattern = f"output_runs/*/accounts/{account}/state/{RECOMMENDATION_POINT_FILE}"
    results: list[dict[str, Any]] = []
    for path in sorted(root.glob(pattern)):
        ref = path.relative_to(root).as_posix()
        matched = _POINT_REF.fullmatch(ref)
        if matched is None:
            continue
        run_id = matched.group(1)
        try:
            point = load_recommendation_point(root, run_id, account)
            if point["market"] != market or point["account"] != account:
                raise RecommendationPointError(
                    "official_point_invalid", "point identity does not match scope"
                )
            target = str(point["scheduled_scan_target_market"])
            trading_date = (
                datetime.fromisoformat(target.replace("Z", "+00:00"))
                .astimezone(ZoneInfo("Asia/Hong_Kong"))
                .date()
                .isoformat()
            )
        except (RecommendationPointError, ValueError) as exc:
            results.append(
                {
                    "status": "invalid",
                    "point_ref": ref,
                    "reason_code": getattr(exc, "reason_code", "official_point_invalid"),
                }
            )
            continue
        results.append(
            {
                "status": "available",
                "point_ref": ref,
                "recommendation_point_id": point["recommendation_point_id"],
                "scheduled_scan_target_market": target,
                "trading_date": trading_date,
            }
        )
    return sorted(
        results,
        key=lambda item: (
            str(item.get("scheduled_scan_target_market") or ""),
            str(item["point_ref"]),
        ),
    )


def _validate_window_facts(window_facts: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(window_facts, Mapping):
        _fail("corpus_input_invalid", "window_facts must be an object")
    item = dict(window_facts)
    if set(item) != _WINDOW_FACT_FIELDS:
        _fail("corpus_input_invalid", "window_facts keys are invalid")
    if item["schema_version"] != RESEARCH_WINDOW_FACTS_SCHEMA:
        _fail("corpus_input_invalid", "window_facts schema is invalid")
    market, account = _identity(item["market"], item["account"])
    cutoff_at = _timestamp(item["cutoff_at_utc"], "cutoff_at_utc")
    cutoff_day = _trading_date(item["cutoff_trading_date"])
    calendar_version = _text(
        item["market_calendar_version"], "market_calendar_version"
    )
    calendar_ref = _relative_ref(item["market_calendar_ref"], "market_calendar_ref")
    calendar_hash = _hash(
        item["market_calendar_sha256"], "market_calendar_sha256"
    )
    raw_dates = item["trading_calendar_dates"]
    if not isinstance(raw_dates, list) or not raw_dates:
        _fail(
            "corpus_input_invalid",
            "trading_calendar_dates must be a non-empty list",
        )
    dates = [_trading_date(value) for value in raw_dates]
    if dates != sorted(set(dates)):
        _fail(
            "corpus_input_invalid",
            "trading_calendar_dates must be strictly increasing and unique",
        )
    if dates[-1] != cutoff_day:
        _fail(
            "corpus_input_invalid",
            "trading_calendar_dates must end at cutoff_trading_date",
        )
    dates_hash = _hash(
        item["trading_calendar_dates_sha256"],
        "trading_calendar_dates_sha256",
    )
    if dates_hash != canonical_sha256(dates):
        _fail(
            "corpus_input_invalid",
            "trading_calendar_dates_sha256 does not match",
        )
    latest_mature = item["latest_mature_trading_date"]
    if latest_mature is not None:
        latest_mature = _trading_date(latest_mature)
        if latest_mature not in dates:
            _fail(
                "corpus_input_invalid",
                "latest_mature_trading_date must be in trading_calendar_dates",
            )
    maturity_ref = _relative_ref(
        item["maturity_evidence_ref"], "maturity_evidence_ref"
    )
    maturity_hash = _hash(
        item["maturity_evidence_sha256"], "maturity_evidence_sha256"
    )
    if item["recommendation_point_selector"] != RECOMMENDATION_POINT_SELECTOR:
        _fail("corpus_input_invalid", "recommendation_point_selector is invalid")
    content_hash = _hash(item["content_sha256"], "content_sha256")
    if canonical_sha256(
        {key: value for key, value in item.items() if key != "content_sha256"}
    ) != content_hash:
        _fail("corpus_input_invalid", "window_facts content_sha256 does not match")
    return {
        **item,
        "market": market,
        "account": account,
        "cutoff_at_utc": cutoff_at,
        "cutoff_trading_date": cutoff_day,
        "market_calendar_version": calendar_version,
        "market_calendar_ref": calendar_ref,
        "market_calendar_sha256": calendar_hash,
        "trading_calendar_dates": dates,
        "trading_calendar_dates_sha256": dates_hash,
        "latest_mature_trading_date": latest_mature,
        "maturity_evidence_ref": maturity_ref,
        "maturity_evidence_sha256": maturity_hash,
        "content_sha256": content_hash,
    }


def _read_indexed_projection(
    artifact_root: str | Path,
    row: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    point_id = str(row["recommendation_point_id"])
    ref = row["projection_ref"]
    if ref != _projection_ref(str(row["market"]), str(row["account"]), point_id):
        _fail("corpus_artifact_invalid", "ranking projection ref is invalid")
    path = private_path(artifact_root).joinpath(*str(ref).split("/"))
    try:
        with open_private_text(path) as handle:
            content = handle.read().encode("utf-8")
        payload = json.loads(content.decode("utf-8"))
        item = validate_ranking_projection(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, Top1RankingError) as exc:
        raise CorpusError(
            "corpus_artifact_invalid", "ranking projection artifact is invalid"
        ) from exc
    if content != _render(item):
        _fail("corpus_artifact_invalid", "ranking projection bytes are not canonical")
    expected = {
        "schema_version": row["ranking_projection_schema_version"],
        "market": row["market"],
        "account": row["account"],
        "recommendation_point_id": point_id,
        "run_id": row["source_run_id"],
        "opening_snapshot_ref": row["opening_snapshot_ref"],
        "opening_snapshot_sha256": row["opening_snapshot_sha256"],
    }
    if any(item[key] != value for key, value in expected.items()):
        _fail("corpus_artifact_invalid", "ranking projection index binding does not match")
    if (
        item["artifact_provenance"]["content_sha256"]
        != row["projection_content_sha256"]
        or _file_sha256(content) != row["projection_file_sha256"]
    ):
        _fail("corpus_artifact_invalid", "ranking projection hashes do not match")
    return item, content


def read_validation_day_source(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    trading_date: str,
) -> dict[str, Any]:
    """Read one indexed validation denominator without repairing producer data."""

    market, account = _identity(market, account)
    trading_date = _trading_date(trading_date)
    if not formal_corpus_present(
        artifact_root, market=market, account=account
    ):
        row = _store_call(store.corpus_day, market, account, trading_date)
        if row is None:
            return {"status": "missing", "row": None, "expectation": None}
        if (
            row["conflict_status"] != "clean"
            or row["completeness_reason"] is not None
        ):
            return {
                "status": "not_evaluable",
                "reason_code": row["completeness_reason"]
                or "research_corpus_conflict",
                "row": dict(row),
                "expectation": None,
            }
        expectation, _content = _read_indexed_expectation(artifact_root, row)
        if not expectation["expected_recommendation_point_ids"]:
            return {
                "status": "not_evaluable",
                "reason_code": "corpus_day_expectation_empty",
                "row": dict(row),
                "expectation": expectation,
            }
        return {
            "status": "available",
            "reason_code": None,
            "row": dict(row),
            "expectation": expectation,
        }
    del store
    try:
        loaded = load_formal_expectation(
            artifact_root,
            market=market,
            account=account,
            trading_date=trading_date,
        )
    except FormalCorpusError as exc:
        raise CorpusError(exc.reason_code, str(exc)) from exc
    expectation = loaded.get("expectation")
    row = (
        {
            "market": market,
            "account": account,
            "trading_date": trading_date,
            "expectation_ref": loaded.get("artifact_ref"),
            "expectation_content_sha256": loaded.get(
                "artifact_content_sha256"
            ),
            "expectation_file_sha256": loaded.get("artifact_file_sha256"),
        }
        if isinstance(expectation, Mapping)
        else None
    )
    if loaded["status"] != "available":
        return {
            "status": loaded["status"],
            "reason_code": loaded.get("reason_code"),
            "row": row,
            "expectation": expectation,
        }
    return {
        "status": "available",
        "reason_code": None,
        "row": row,
        "expectation": expectation,
    }


def _read_validation_day_row(
    artifact_root: str | Path,
    *,
    row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if row is None:
        return {"status": "missing", "row": None, "expectation": None}
    if row["conflict_status"] != "clean" or row["completeness_reason"] is not None:
        return {
            "status": "not_evaluable",
            "reason_code": row["completeness_reason"] or "research_corpus_conflict",
            "row": dict(row),
            "expectation": None,
        }
    expectation, _content = _read_indexed_expectation(artifact_root, row)
    if not expectation["expected_recommendation_point_ids"]:
        return {
            "status": "not_evaluable",
            "reason_code": "corpus_day_expectation_empty",
            "row": dict(row),
            "expectation": expectation,
        }
    return {
        "status": "available",
        "reason_code": None,
        "row": dict(row),
        "expectation": expectation,
    }


def _read_validation_point_row(
    artifact_root: str | Path,
    *,
    trading_date: str,
    row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if row is None:
        return {
            "status": "missing",
            "reason_code": "corpus_point_missing",
            "point_row": None,
            "projection": None,
        }
    if row["trading_date"] != trading_date:
        _fail("corpus_artifact_invalid", "point index trading date changed")
    if row["conflict_status"] != "clean":
        return {
            "status": "not_evaluable",
            "reason_code": "research_corpus_conflict",
            "point_row": dict(row),
            "projection": None,
        }
    if row["capture_status"] != "captured":
        return {
            "status": "not_evaluable",
            "reason_code": row["reason_code"],
            "point_row": dict(row),
            "projection": None,
        }
    projection, _content = _read_indexed_projection(artifact_root, row)
    return {
        "status": "available",
        "reason_code": None,
        "point_row": dict(row),
        "projection": projection,
    }


def read_validation_point_source(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    trading_date: str,
    recommendation_point_id: str,
) -> dict[str, Any]:
    """Read one point strictly bound to its canonical day expectation and index."""

    market, account = _identity(market, account)
    trading_date = _trading_date(trading_date)
    day = read_validation_day_source(
        store,
        artifact_root,
        market=market,
        account=account,
        trading_date=trading_date,
    )
    if day["status"] != "available":
        return {**day, "point_row": None, "projection": None}
    expectation = day["expectation"]
    assert isinstance(expectation, dict)
    if recommendation_point_id not in expectation["expected_recommendation_point_ids"]:
        _fail(
            "unexpected_recommendation_point",
            "validation point is absent from the canonical expectation",
        )
    if not formal_corpus_present(
        artifact_root, market=market, account=account
    ):
        row = _store_call(
            store.corpus_point, market, account, recommendation_point_id
        )
        if row is None:
            return {
                **day,
                "status": "missing",
                "reason_code": "corpus_point_missing",
                "point_row": None,
                "projection": None,
            }
        if row["trading_date"] != trading_date:
            _fail("corpus_artifact_invalid", "point index trading date changed")
        if row["conflict_status"] != "clean":
            return {
                **day,
                "status": "not_evaluable",
                "reason_code": "research_corpus_conflict",
                "point_row": dict(row),
                "projection": None,
            }
        if row["capture_status"] != "captured":
            return {
                **day,
                "status": "not_evaluable",
                "reason_code": row["reason_code"],
                "point_row": dict(row),
                "projection": None,
            }
        projection, _content = _read_indexed_projection(artifact_root, row)
        return {
            **day,
            "status": "available",
            "reason_code": None,
            "point_row": dict(row),
            "projection": projection,
        }
    try:
        loaded = load_formal_point(
            artifact_root,
            market=market,
            account=account,
            trading_date=trading_date,
            recommendation_point_id=recommendation_point_id,
        )
    except FormalCorpusError as exc:
        raise CorpusError(exc.reason_code, str(exc)) from exc
    if loaded["status"] == "missing":
        return {
            **day,
            "status": "missing",
            "reason_code": loaded.get("reason_code"),
            "point_row": None,
            "projection": None,
        }
    if loaded["status"] != "available":
        point = loaded.get("point")
        return {
            **day,
            "status": loaded["status"],
            "reason_code": loaded.get("reason_code"),
            "point_row": (
                {
                    "recommendation_point_id": recommendation_point_id,
                    "source_point_ref": loaded.get("artifact_ref"),
                    "source_point_content_sha256": (
                        point.get("content_sha256")
                        if isinstance(point, Mapping)
                        else None
                    ),
                }
                if point is not None
                else None
            ),
            "projection": None,
        }
    point = loaded["point"]
    try:
        recipe_projection = build_top1_recipe_projection(
            point,
            formal_point_ref=str(loaded["artifact_ref"]),
        )
        projection = materialize_top1_recipe_input(point, recipe_projection)
    except Top1RankingError as exc:
        return {
            **day,
            "status": "not_evaluable",
            "reason_code": exc.reason_code,
            "point_row": None,
            "projection": None,
        }
    projection_content = _render(recipe_projection)
    row = {
        "recommendation_point_id": recommendation_point_id,
        "trading_date": trading_date,
        "projection_ref": loaded["artifact_ref"],
        "projection_content_sha256": recipe_projection["artifact_provenance"][
            "content_sha256"
        ],
        "projection_file_sha256": _file_sha256(projection_content),
        "source_point_ref": loaded["artifact_ref"],
        "source_point_content_sha256": loaded["artifact_content_sha256"],
        "captured_at_utc": point["captured_at_utc"],
    }
    return {
        **day,
        "status": "available",
        "reason_code": None,
        "point_row": dict(row),
        "projection": projection,
        "recipe_projection": recipe_projection,
    }


def read_corpus_status(
    store: ExperimentStore,
    *,
    market: str,
    account: str,
    artifact_root: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    if artifact_root is not None:
        return build_formal_corpus_health_receipt(
            artifact_root,
            market=market,
            account=account,
            repo_root=repo_root,
        )
    market, account = _identity(market, account)
    days = _store_call(store.corpus_days, market, account)
    points = _store_call(store.corpus_points, market, account)
    clean_days = [row for row in days if row["conflict_status"] == "clean"]
    clean_points = [row for row in points if row["conflict_status"] == "clean"]
    expected_points = sum(int(row["expected_point_count"]) for row in days)
    return {
        "schema_version": CORPUS_STATUS_SCHEMA,
        "market": market,
        "account": account,
        "days_total": len(days),
        "days_on_time": sum(
            row["completeness_reason"] is None for row in clean_days
        ),
        "days_not_evaluable": sum(
            row["completeness_reason"] is not None for row in clean_days
        ),
        "days_conflicting": len(days) - len(clean_days),
        "expected_points_total": expected_points,
        "points_captured": sum(
            row["capture_status"] == "captured" for row in clean_points
        ),
        "points_not_evaluable": sum(
            row["capture_status"] == "not_evaluable" for row in clean_points
        ),
        "points_conflicting": len(points) - len(clean_points),
        "points_missing": max(expected_points - len(points), 0),
        "earliest_trading_date": days[0]["trading_date"] if days else None,
        "latest_trading_date": days[-1]["trading_date"] if days else None,
        "ranking_projection_schema_version": RANKING_PROJECTION_SCHEMA_VERSION,
    }


_DAY_HEALTH_FIELDS = frozenset(
    {
        "trading_date",
        "state",
        "reason_codes",
        "expected_count",
        "captured_count",
        "pending_count",
        "overdue_count",
        "missing_count",
        "not_evaluable_count",
        "conflicting_count",
        "unexpected_count",
        "expectation_ref",
        "expectation_content_sha256",
        "expectation_file_sha256",
        "evidence_content_sha256",
    }
)
_HEALTH_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "market",
        "account",
        "subject_trading_date",
        "observed_at_utc",
        "observed_hk_date",
        "advance_interval_seconds",
        "fresh_until_utc",
        "day",
        "accumulation",
        "last_successful_capture_at_utc",
        "market_calendar",
        "source_evidence_sha256",
        "content_sha256",
    }
)
_ACCUMULATION_FIELDS = frozenset(
    {
        "latest_mature_trading_date",
        "required_complete_days",
        "consecutive_complete_days",
        "remaining_complete_days",
        "research_window_ready",
        "window_start_trading_date",
        "window_end_trading_date",
        "first_blocker",
    }
)
_CALENDAR_HEALTH_FIELDS = frozenset(
    {
        "market_calendar_version",
        "coverage_start",
        "coverage_end",
        "snapshot_ref",
        "snapshot_content_sha256",
        "snapshot_file_sha256",
        "source_receipt_sha256",
    }
)


def _utc_datetime(value: object, label: str) -> datetime:
    text = _timestamp(value, label)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _health_calendar_binding(calendar: Mapping[str, Any]) -> dict[str, Any]:
    return {key: calendar[key] for key in sorted(_CALENDAR_HEALTH_FIELDS)}


def _day_evidence_sha256(
    day_row: Mapping[str, Any] | None,
    point_rows: list[Mapping[str, Any]],
) -> str:
    return canonical_sha256(
        {
            "day": (
                {
                    key: day_row[key]
                    for key in (
                        "trading_date",
                        "expectation_content_sha256",
                        "expectation_file_sha256",
                        "market_calendar_sha256",
                        "schedule_config_sha256",
                        "expected_point_count",
                        "completeness_reason",
                        "conflict_status",
                    )
                }
                if day_row is not None
                else None
            ),
            "points": [
                {
                    key: row[key]
                    for key in (
                        "recommendation_point_id",
                        "source_point_content_sha256",
                        "opening_snapshot_sha256",
                        "projection_content_sha256",
                        "projection_file_sha256",
                        "captured_at_utc",
                        "capture_status",
                        "reason_code",
                        "conflict_status",
                    )
                }
                for row in point_rows
            ],
        }
    )


def _day_health_result(
    *,
    trading_date: str,
    state: str,
    reason_codes: list[str],
    expected_count: int,
    captured_count: int,
    pending_count: int = 0,
    overdue_count: int = 0,
    missing_count: int = 0,
    not_evaluable_count: int = 0,
    conflicting_count: int = 0,
    unexpected_count: int = 0,
    expectation_ref: str | None = None,
    expectation_content_sha256: str | None = None,
    expectation_file_sha256: str | None = None,
    evidence_content_sha256: str,
) -> dict[str, Any]:
    return {
        "trading_date": trading_date,
        "state": state,
        "reason_codes": sorted(set(reason_codes)),
        "expected_count": expected_count,
        "captured_count": captured_count,
        "pending_count": pending_count,
        "overdue_count": overdue_count,
        "missing_count": missing_count,
        "not_evaluable_count": not_evaluable_count,
        "conflicting_count": conflicting_count,
        "unexpected_count": unexpected_count,
        "expectation_ref": expectation_ref,
        "expectation_content_sha256": expectation_content_sha256,
        "expectation_file_sha256": expectation_file_sha256,
        "evidence_content_sha256": evidence_content_sha256,
    }


def _evaluate_corpus_day_health(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    trading_date: str,
    observed_at_utc: str,
    mature: bool,
    market_calendar_sha256: str,
) -> dict[str, Any]:
    row = _store_call(store.corpus_day, market, account, trading_date)
    point_rows = _store_call(
        store.corpus_points, market, account, trading_date=trading_date
    )
    evidence_hash = _day_evidence_sha256(row, point_rows)
    if row is None:
        return _day_health_result(
            trading_date=trading_date,
            state="degraded",
            reason_codes=["corpus_day_missing"],
            expected_count=0,
            captured_count=0,
            evidence_content_sha256=evidence_hash,
        )
    if row["market_calendar_sha256"] != market_calendar_sha256:
        return _day_health_result(
            trading_date=trading_date,
            state="degraded",
            reason_codes=["market_calendar_binding_mismatch"],
            expected_count=int(row["expected_point_count"]),
            captured_count=0,
            not_evaluable_count=int(row["expected_point_count"]),
            expectation_ref=str(row["expectation_ref"]),
            expectation_content_sha256=str(row["expectation_content_sha256"]),
            expectation_file_sha256=str(row["expectation_file_sha256"]),
            evidence_content_sha256=evidence_hash,
        )

    try:
        day_source = _read_validation_day_row(
            artifact_root,
            row=row,
        )
    except CorpusError:
        return _day_health_result(
            trading_date=trading_date,
            state="conflict",
            reason_codes=["corpus_artifact_invalid"],
            expected_count=int(row["expected_point_count"]),
            captured_count=0,
            conflicting_count=int(row["expected_point_count"]),
            expectation_ref=str(row["expectation_ref"]),
            expectation_content_sha256=str(row["expectation_content_sha256"]),
            expectation_file_sha256=str(row["expectation_file_sha256"]),
            evidence_content_sha256=evidence_hash,
        )

    if day_source["status"] != "available":
        reason = str(day_source.get("reason_code") or "corpus_day_missing")
        state = "conflict" if reason in {
            "research_corpus_conflict",
            "corpus_artifact_invalid",
        } else "degraded"
        return _day_health_result(
            trading_date=trading_date,
            state=state,
            reason_codes=[reason],
            expected_count=int(row["expected_point_count"]),
            captured_count=0,
            conflicting_count=(
                int(row["expected_point_count"]) if state == "conflict" else 0
            ),
            not_evaluable_count=(
                int(row["expected_point_count"]) if state == "degraded" else 0
            ),
            expectation_ref=str(row["expectation_ref"]),
            expectation_content_sha256=str(row["expectation_content_sha256"]),
            expectation_file_sha256=str(row["expectation_file_sha256"]),
            evidence_content_sha256=evidence_hash,
        )

    expectation = day_source["expectation"]
    assert isinstance(expectation, Mapping)
    expected_ids = list(expectation["expected_recommendation_point_ids"])
    targets = list(expectation["scheduled_scan_targets_market"])
    rows_by_id = {str(item["recommendation_point_id"]): item for item in point_rows}
    expected_set = set(expected_ids)
    unexpected = sorted(set(rows_by_id) - expected_set)
    observed_at = _utc_datetime(observed_at_utc, "observed_at_utc")
    captured: list[str] = []
    pending: list[str] = []
    overdue: list[str] = []
    missing: list[str] = []
    not_evaluable: list[str] = []
    conflicting: list[str] = []
    reasons: list[str] = []

    for index, point_id in enumerate(expected_ids):
        point_row = rows_by_id.get(point_id)
        if point_row is None:
            if mature:
                missing.append(point_id)
                continue
            next_target_due = index + 1 < len(targets) and observed_at >= _utc_datetime(
                targets[index + 1], "scheduled_scan_target"
            )
            later_captured = any(
                later_id in rows_by_id for later_id in expected_ids[index + 1 :]
            )
            (overdue if next_target_due or later_captured else pending).append(point_id)
            continue
        try:
            source = _read_validation_point_row(
                artifact_root,
                trading_date=trading_date,
                row=point_row,
            )
        except CorpusError:
            conflicting.append(point_id)
            reasons.append("corpus_artifact_invalid")
            continue
        if source["status"] == "available":
            captured.append(point_id)
        elif source.get("reason_code") == "research_corpus_conflict":
            conflicting.append(point_id)
        else:
            not_evaluable.append(point_id)
            reasons.append(str(source.get("reason_code") or "corpus_point_missing"))

    if conflicting or unexpected:
        state = "conflict"
        reasons.append("research_corpus_conflict")
    elif missing or overdue or not_evaluable:
        state = "degraded"
        if missing:
            reasons.append("corpus_point_missing")
        if overdue:
            reasons.append("corpus_point_overdue")
    elif mature:
        state = "complete"
    else:
        state = "collecting"

    return _day_health_result(
        trading_date=trading_date,
        state=state,
        reason_codes=reasons,
        expected_count=len(expected_ids),
        captured_count=len(captured),
        pending_count=len(pending),
        overdue_count=len(overdue),
        missing_count=len(missing),
        not_evaluable_count=len(not_evaluable),
        conflicting_count=len(conflicting),
        unexpected_count=len(unexpected),
        expectation_ref=str(row["expectation_ref"]),
        expectation_content_sha256=str(row["expectation_content_sha256"]),
        expectation_file_sha256=str(row["expectation_file_sha256"]),
        evidence_content_sha256=evidence_hash,
    )


def _market_closed_day(observed_hk_date: str) -> dict[str, Any]:
    return _day_health_result(
        trading_date=observed_hk_date,
        state="market_closed",
        reason_codes=[],
        expected_count=0,
        captured_count=0,
        evidence_content_sha256=canonical_sha256(
            {"trading_date": observed_hk_date, "state": "market_closed"}
        ),
    )


def _latest_successful_capture(
    point_rows: list[Mapping[str, Any]],
) -> str | None:
    values = [
        str(row["captured_at_utc"])
        for row in point_rows
        if row["capture_status"] == "captured"
        and row["conflict_status"] == "clean"
    ]
    return max(values) if values else None


def _finalize_health_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload["content_sha256"] = canonical_sha256(payload)
    if len(_render(payload)) > MAX_CORPUS_HEALTH_RECEIPT_BYTES:
        _fail("corpus_health_receipt_conflict", "corpus health receipt exceeds 16 KiB")
    return payload


def build_corpus_health_receipt(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    observed_at_utc: str,
    advance_interval_seconds: int,
) -> dict[str, Any]:
    market, account = _identity(market, account)
    if (
        not isinstance(advance_interval_seconds, int)
        or isinstance(advance_interval_seconds, bool)
        or advance_interval_seconds <= 0
    ):
        _fail("corpus_input_invalid", "advance interval must be a positive integer")
    observed_at = _utc_datetime(observed_at_utc, "observed_at_utc")
    observed_hk_date = observed_at.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
    calendar = read_market_calendar_binding(artifact_root, market=market)
    if not calendar["coverage_start"] <= observed_hk_date <= calendar["coverage_end"]:
        _fail(
            "market_calendar_binding_unavailable",
            "observed Hong Kong date is outside market calendar coverage",
        )
    trading_dates = list(calendar["trading_dates"])
    mature_dates = [value for value in trading_dates if value < observed_hk_date]
    latest_mature = mature_dates[-1] if mature_dates else None
    day_rows = _store_call(store.corpus_days, market, account)
    point_rows = _store_call(store.corpus_points, market, account)
    earliest_day = str(day_rows[0]["trading_date"]) if day_rows else None

    streak: list[dict[str, Any]] = []
    first_blocker: dict[str, Any] | None = None
    if latest_mature is not None and earliest_day is not None:
        eligible = [
            value for value in mature_dates if earliest_day <= value <= latest_mature
        ][-RESEARCH_REQUIRED_DAYS:]
        for trading_date in reversed(eligible):
            health = _evaluate_corpus_day_health(
                store,
                artifact_root,
                market=market,
                account=account,
                trading_date=trading_date,
                observed_at_utc=observed_at_utc,
                mature=True,
                market_calendar_sha256=str(calendar["snapshot_content_sha256"]),
            )
            if health["state"] != "complete":
                first_blocker = {
                    "trading_date": trading_date,
                    "state": health["state"],
                    "reason_codes": health["reason_codes"],
                }
                break
            streak.append(health)

    consecutive = len(streak)
    ready = consecutive >= RESEARCH_REQUIRED_DAYS
    window = list(reversed(streak[:RESEARCH_REQUIRED_DAYS]))
    if consecutive < RESEARCH_REQUIRED_DAYS and first_blocker is None:
        first_blocker = {
            "trading_date": None,
            "state": "warming",
            "reason_codes": ["research_corpus_warming"],
        }
    current_day = (
        _evaluate_corpus_day_health(
            store,
            artifact_root,
            market=market,
            account=account,
            trading_date=observed_hk_date,
            observed_at_utc=observed_at_utc,
            mature=False,
            market_calendar_sha256=str(calendar["snapshot_content_sha256"]),
        )
        if observed_hk_date in trading_dates
        else _market_closed_day(observed_hk_date)
    )
    source_hash = canonical_sha256(
        {
            "current_day": current_day["evidence_content_sha256"],
            "streak": [
                {
                    "trading_date": item["trading_date"],
                    "evidence_content_sha256": item["evidence_content_sha256"],
                }
                for item in streak
            ],
            "first_blocker": first_blocker,
        }
    )
    fresh_until = observed_at + timedelta(seconds=advance_interval_seconds * 2)
    payload = _finalize_health_payload(
        {
            "schema_version": CORPUS_HEALTH_RECEIPT_SCHEMA,
            "receipt_kind": "current",
            "market": market,
            "account": account,
            "subject_trading_date": (
                observed_hk_date if observed_hk_date in trading_dates else None
            ),
            "observed_at_utc": observed_at_utc,
            "observed_hk_date": observed_hk_date,
            "advance_interval_seconds": advance_interval_seconds,
            "fresh_until_utc": fresh_until.isoformat().replace("+00:00", "Z"),
            "day": current_day,
            "accumulation": {
                "latest_mature_trading_date": latest_mature,
                "required_complete_days": RESEARCH_REQUIRED_DAYS,
                "consecutive_complete_days": consecutive,
                "remaining_complete_days": max(
                    RESEARCH_REQUIRED_DAYS - consecutive, 0
                ),
                "research_window_ready": ready,
                "window_start_trading_date": (
                    window[0]["trading_date"] if window else None
                ),
                "window_end_trading_date": (
                    window[-1]["trading_date"] if window else None
                ),
                "first_blocker": first_blocker,
            },
            "last_successful_capture_at_utc": _latest_successful_capture(point_rows),
            "market_calendar": _health_calendar_binding(calendar),
            "source_evidence_sha256": source_hash,
        }
    )
    return _validate_health_receipt(
        payload,
        expected_kind="current",
        expected_market=market,
        expected_account=account,
    )


def _build_daily_health_receipt(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    trading_date: str,
    observed_at_utc: str,
    calendar: Mapping[str, Any],
) -> dict[str, Any]:
    health = _evaluate_corpus_day_health(
        store,
        artifact_root,
        market=market,
        account=account,
        trading_date=trading_date,
        observed_at_utc=observed_at_utc,
        mature=True,
        market_calendar_sha256=str(calendar["snapshot_content_sha256"]),
    )
    point_rows = _store_call(
        store.corpus_points, market, account, trading_date=trading_date
    )
    observed_at = _utc_datetime(observed_at_utc, "observed_at_utc")
    payload = _finalize_health_payload(
        {
            "schema_version": CORPUS_HEALTH_RECEIPT_SCHEMA,
            "receipt_kind": "trading_day",
            "market": market,
            "account": account,
            "subject_trading_date": trading_date,
            "observed_at_utc": observed_at_utc,
            "observed_hk_date": observed_at.astimezone(
                ZoneInfo("Asia/Hong_Kong")
            ).date().isoformat(),
            "advance_interval_seconds": None,
            "fresh_until_utc": None,
            "day": health,
            "accumulation": None,
            "last_successful_capture_at_utc": _latest_successful_capture(point_rows),
            "market_calendar": _health_calendar_binding(calendar),
            "source_evidence_sha256": health["evidence_content_sha256"],
        }
    )
    return _validate_health_receipt(
        payload,
        expected_kind="trading_day",
        expected_market=market,
        expected_account=account,
        expected_subject_trading_date=trading_date,
    )


def _validate_day_health(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _DAY_HEALTH_FIELDS:
        _fail("corpus_health_receipt_conflict", "day health fields are invalid")
    item = dict(value)
    _trading_date(item["trading_date"])
    if item["state"] not in {
        "collecting",
        "complete",
        "degraded",
        "conflict",
        "market_closed",
    }:
        _fail("corpus_health_receipt_conflict", "day health state is invalid")
    count_fields = (
        "expected_count",
        "captured_count",
        "pending_count",
        "overdue_count",
        "missing_count",
        "not_evaluable_count",
        "conflicting_count",
        "unexpected_count",
    )
    for field in count_fields:
        if (
            not isinstance(item[field], int)
            or isinstance(item[field], bool)
            or item[field] < 0
        ):
            _fail("corpus_health_receipt_conflict", f"{field} is invalid")
    if item["captured_count"] > item["expected_count"]:
        _fail("corpus_health_receipt_conflict", "captured points exceed expected")
    classified = sum(
        item[field]
        for field in count_fields
        if field not in {"expected_count", "unexpected_count"}
    )
    if classified != item["expected_count"]:
        _fail("corpus_health_receipt_conflict", "point counts do not match expected")
    reasons = item["reason_codes"]
    if (
        not isinstance(reasons, list)
        or any(not isinstance(reason, str) or not reason for reason in reasons)
        or reasons != sorted(set(reasons))
    ):
        _fail("corpus_health_receipt_conflict", "day health reasons are invalid")
    hard_gap_count = sum(
        item[field]
        for field in (
            "overdue_count",
            "missing_count",
            "not_evaluable_count",
            "conflicting_count",
            "unexpected_count",
        )
    )
    if item["state"] == "complete" and (
        item["expected_count"] == 0
        or item["captured_count"] != item["expected_count"]
        or hard_gap_count
        or item["pending_count"]
        or reasons
    ):
        _fail("corpus_health_receipt_conflict", "complete day health is invalid")
    if item["state"] == "collecting" and (hard_gap_count or reasons):
        _fail("corpus_health_receipt_conflict", "collecting day health is invalid")
    if item["state"] in {"degraded", "conflict"} and not reasons:
        _fail("corpus_health_receipt_conflict", "unhealthy day reasons are missing")
    if item["state"] == "market_closed" and (
        classified or item["unexpected_count"] or reasons
    ):
        _fail("corpus_health_receipt_conflict", "market-closed day health is invalid")
    for field in (
        "expectation_content_sha256",
        "expectation_file_sha256",
    ):
        if item[field] is not None:
            _hash(item[field], field)
    if item["expectation_ref"] is not None:
        _relative_ref(item["expectation_ref"], "expectation_ref")
    expectation_values = (
        item["expectation_ref"],
        item["expectation_content_sha256"],
        item["expectation_file_sha256"],
    )
    if any(value is None for value in expectation_values) != all(
        value is None for value in expectation_values
    ):
        _fail("corpus_health_receipt_conflict", "expectation binding is incomplete")
    _hash(item["evidence_content_sha256"], "evidence_content_sha256")
    return item


def _validate_health_receipt(
    payload: object,
    *,
    expected_kind: str,
    expected_market: str,
    expected_account: str,
    expected_subject_trading_date: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _HEALTH_RECEIPT_FIELDS:
        _fail("corpus_health_receipt_conflict", "health receipt fields are invalid")
    item = dict(payload)
    if item["schema_version"] != CORPUS_HEALTH_RECEIPT_SCHEMA:
        _fail("corpus_health_receipt_conflict", "health receipt schema is invalid")
    if (
        item["receipt_kind"] != expected_kind
        or item["market"] != expected_market
        or item["account"] != expected_account
    ):
        _fail("corpus_health_receipt_conflict", "health receipt identity changed")
    subject = item["subject_trading_date"]
    if subject is not None:
        subject = _trading_date(subject)
    if expected_subject_trading_date is not None and subject != expected_subject_trading_date:
        _fail("corpus_health_receipt_conflict", "health receipt subject changed")
    observed = _utc_datetime(item["observed_at_utc"], "observed_at_utc")
    if item["observed_hk_date"] != observed.astimezone(
        ZoneInfo("Asia/Hong_Kong")
    ).date().isoformat():
        _fail("corpus_health_receipt_conflict", "observed HK date changed")
    day = _validate_day_health(item["day"])
    if item["last_successful_capture_at_utc"] is not None:
        last_capture = _utc_datetime(
            item["last_successful_capture_at_utc"],
            "last_successful_capture_at_utc",
        )
        if last_capture > observed:
            _fail("corpus_health_receipt_conflict", "last capture is in the future")
    if expected_kind == "current":
        if day["state"] == "complete":
            _fail("corpus_health_receipt_conflict", "current day cannot be final")
        if (
            not isinstance(item["advance_interval_seconds"], int)
            or isinstance(item["advance_interval_seconds"], bool)
            or item["advance_interval_seconds"] <= 0
            or item["fresh_until_utc"] is None
        ):
            _fail("corpus_health_receipt_conflict", "current cadence is invalid")
        fresh_until = _utc_datetime(item["fresh_until_utc"], "fresh_until_utc")
        if fresh_until != observed + timedelta(
            seconds=int(item["advance_interval_seconds"]) * 2
        ):
            _fail("corpus_health_receipt_conflict", "freshness boundary changed")
        accumulation = item["accumulation"]
        if not isinstance(accumulation, Mapping) or set(accumulation) != _ACCUMULATION_FIELDS:
            _fail("corpus_health_receipt_conflict", "accumulation fields are invalid")
        if accumulation["required_complete_days"] != RESEARCH_REQUIRED_DAYS:
            _fail("corpus_health_receipt_conflict", "required days changed")
        for field in ("consecutive_complete_days", "remaining_complete_days"):
            if (
                not isinstance(accumulation[field], int)
                or isinstance(accumulation[field], bool)
                or accumulation[field] < 0
            ):
                _fail("corpus_health_receipt_conflict", f"{field} is invalid")
        if not isinstance(accumulation["research_window_ready"], bool):
            _fail("corpus_health_receipt_conflict", "window readiness is invalid")
        for field in (
            "latest_mature_trading_date",
            "window_start_trading_date",
            "window_end_trading_date",
        ):
            if accumulation[field] is not None:
                _trading_date(accumulation[field])
        consecutive = int(accumulation["consecutive_complete_days"])
        if (
            consecutive > RESEARCH_REQUIRED_DAYS
            or accumulation["remaining_complete_days"]
            != RESEARCH_REQUIRED_DAYS - consecutive
            or accumulation["research_window_ready"]
            != (consecutive == RESEARCH_REQUIRED_DAYS)
        ):
            _fail("corpus_health_receipt_conflict", "accumulation counts disagree")
        window_dates = (
            accumulation["window_start_trading_date"],
            accumulation["window_end_trading_date"],
        )
        if (consecutive == 0) != all(value is None for value in window_dates):
            _fail("corpus_health_receipt_conflict", "accumulation window is invalid")
        blocker = accumulation["first_blocker"]
        if (consecutive == RESEARCH_REQUIRED_DAYS) != (blocker is None):
            _fail("corpus_health_receipt_conflict", "first blocker disagrees with readiness")
        if blocker is not None:
            if not isinstance(blocker, Mapping) or set(blocker) != {
                "trading_date",
                "state",
                "reason_codes",
            }:
                _fail("corpus_health_receipt_conflict", "first blocker is invalid")
            if blocker["trading_date"] is not None:
                _trading_date(blocker["trading_date"])
            if blocker["state"] not in {"warming", "degraded", "conflict"}:
                _fail("corpus_health_receipt_conflict", "first blocker state is invalid")
            blocker_reasons = blocker["reason_codes"]
            if (
                not isinstance(blocker_reasons, list)
                or any(
                    not isinstance(reason, str) or not reason
                    for reason in blocker_reasons
                )
                or blocker_reasons != sorted(set(blocker_reasons))
                or not blocker_reasons
            ):
                _fail("corpus_health_receipt_conflict", "first blocker reasons are invalid")
        if consecutive and window_dates[0] > window_dates[1]:
            _fail("corpus_health_receipt_conflict", "accumulation window is reversed")
        expected_subject = None if day["state"] == "market_closed" else day["trading_date"]
        if subject != expected_subject or day["trading_date"] != item["observed_hk_date"]:
            _fail("corpus_health_receipt_conflict", "current subject is invalid")
    elif expected_kind == "trading_day":
        if day["state"] in {"collecting", "market_closed"}:
            _fail("corpus_health_receipt_conflict", "daily day state is invalid")
        if subject is None or subject != day["trading_date"]:
            _fail("corpus_health_receipt_conflict", "daily subject is invalid")
        if subject >= item["observed_hk_date"]:
            _fail("corpus_health_receipt_conflict", "daily observation is not mature")
        if (
            item["advance_interval_seconds"] is not None
            or item["fresh_until_utc"] is not None
            or item["accumulation"] is not None
        ):
            _fail("corpus_health_receipt_conflict", "daily receipt is not immutable")
    else:
        _fail("corpus_health_receipt_conflict", "health receipt kind is invalid")
    calendar = item["market_calendar"]
    if not isinstance(calendar, Mapping) or set(calendar) != _CALENDAR_HEALTH_FIELDS:
        _fail("corpus_health_receipt_conflict", "health calendar binding is invalid")
    _text(calendar["market_calendar_version"], "market_calendar_version")
    coverage_start = _trading_date(calendar["coverage_start"])
    coverage_end = _trading_date(calendar["coverage_end"])
    if coverage_start > coverage_end:
        _fail("corpus_health_receipt_conflict", "health calendar coverage is invalid")
    if not coverage_start <= day["trading_date"] <= coverage_end:
        _fail("corpus_health_receipt_conflict", "health day exceeds calendar coverage")
    _relative_ref(calendar["snapshot_ref"], "snapshot_ref")
    for field in (
        "snapshot_content_sha256",
        "snapshot_file_sha256",
        "source_receipt_sha256",
    ):
        _hash(calendar[field], field)
    _hash(item["source_evidence_sha256"], "source_evidence_sha256")
    content_hash = item.pop("content_sha256", None)
    if canonical_sha256(item) != content_hash:
        _fail("corpus_health_receipt_conflict", "health receipt hash changed")
    item["content_sha256"] = content_hash
    return item


def _read_corpus_health_ref(
    artifact_root: str | Path,
    *,
    ref: str,
    receipt_kind: str,
    market: str,
    account: str,
    subject_trading_date: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    path = private_path(artifact_root).joinpath(*ref.split("/"))
    try:
        with open_private_text(path) as handle:
            content = handle.read().encode("utf-8")
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusError(
            "corpus_health_receipt_unavailable",
            "corpus health receipt is unavailable",
        ) from exc
    try:
        item = _validate_health_receipt(
            payload,
            expected_kind=receipt_kind,
            expected_market=market,
            expected_account=account,
            expected_subject_trading_date=subject_trading_date,
        )
    except CorpusError as exc:
        if exc.reason_code == "corpus_health_receipt_conflict":
            raise
        raise CorpusError(
            "corpus_health_receipt_conflict",
            "corpus health receipt is invalid",
        ) from exc
    if content != _render(item) or len(content) > MAX_CORPUS_HEALTH_RECEIPT_BYTES:
        _fail("corpus_health_receipt_conflict", "health receipt bytes are invalid")
    calendar = item["market_calendar"]
    try:
        bound_calendar = read_bound_market_calendar_snapshot(
            artifact_root,
            market=market,
            snapshot_ref=calendar["snapshot_ref"],
            snapshot_content_sha256=calendar["snapshot_content_sha256"],
            snapshot_file_sha256=calendar["snapshot_file_sha256"],
        )
    except CorpusError as exc:
        raise CorpusError(
            "corpus_health_receipt_conflict",
            "corpus health calendar binding is unavailable",
        ) from exc
    if _health_calendar_binding(bound_calendar) != calendar:
        _fail("corpus_health_receipt_conflict", "health calendar binding changed")
    return item, content


def read_corpus_health_receipt(
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    now_utc: str,
) -> dict[str, Any]:
    market, account = _identity(market, account)
    ref = _corpus_health_current_ref(market, account)
    item, content = _read_corpus_health_ref(
        artifact_root,
        ref=ref,
        receipt_kind="current",
        market=market,
        account=account,
    )
    result = {
        **item,
        "receipt_ref": ref,
        "receipt_file_sha256": _file_sha256(content),
        "fresh": _utc_datetime(now_utc, "now_utc")
        <= _utc_datetime(item["fresh_until_utc"], "fresh_until_utc"),
    }
    return result


def publish_corpus_health_receipt(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    observed_at_utc: str,
    advance_interval_seconds: int,
) -> dict[str, Any]:
    market, account = _identity(market, account)
    calendar = read_market_calendar_binding(artifact_root, market=market)
    observed_at = _utc_datetime(observed_at_utc, "observed_at_utc")
    observed_hk_date = observed_at.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
    if not calendar["coverage_start"] <= observed_hk_date <= calendar["coverage_end"]:
        _fail(
            "market_calendar_binding_unavailable",
            "observed Hong Kong date is outside market calendar coverage",
        )
    current = build_corpus_health_receipt(
        store,
        artifact_root,
        market=market,
        account=account,
        observed_at_utc=observed_at_utc,
        advance_interval_seconds=advance_interval_seconds,
    )
    day_rows = _store_call(store.corpus_days, market, account)
    first_day = str(day_rows[0]["trading_date"]) if day_rows else None
    mature_dates = [
        value for value in calendar["trading_dates"] if value < observed_hk_date
    ]
    active_dates = mature_dates[-RESEARCH_REQUIRED_DAYS:]
    if first_day is not None:
        active_dates = [value for value in active_dates if value >= first_day]
    else:
        active_dates = []
    published: list[str] = []
    daily_errors: list[dict[str, str]] = []
    for trading_date in active_dates:
        ref = _corpus_health_day_ref(market, account, trading_date)
        target = private_path(artifact_root).joinpath(*ref.split("/"))
        try:
            if target.exists():
                _read_corpus_health_ref(
                    artifact_root,
                    ref=ref,
                    receipt_kind="trading_day",
                    market=market,
                    account=account,
                    subject_trading_date=trading_date,
                )
                continue
            daily = _build_daily_health_receipt(
                store,
                artifact_root,
                market=market,
                account=account,
                trading_date=trading_date,
                observed_at_utc=observed_at_utc,
                calendar=calendar,
            )
            publish_exact_text(artifact_root, ref, _render(daily))
            published.append(ref)
        except Exception as exc:
            daily_errors.append(
                {
                    "trading_date": trading_date,
                    "receipt_ref": ref,
                    "reason_code": str(
                        getattr(
                            exc,
                            "reason_code",
                            (
                                "corpus_health_receipt_unavailable"
                                if isinstance(exc, OSError)
                                else "corpus_health_receipt_conflict"
                            ),
                        )
                    ),
                    "message": str(exc),
                }
            )

    current_ref = _corpus_health_current_ref(market, account)
    current_path = private_path(artifact_root).joinpath(*current_ref.split("/"))
    # ponytail: the existing timer serializes advance; add an account lock only if concurrent advances are supported.
    atomic_write_private_text(current_path, render_json_text(current))
    return {
        "receipt": read_corpus_health_receipt(
            artifact_root,
            market=market,
            account=account,
            now_utc=observed_at_utc,
        ),
        "daily_receipts_published": published,
        "daily_receipt_errors": daily_errors,
    }


def _freeze_result(
    facts: Mapping[str, Any],
    *,
    status: str,
    reason_code: str | None,
    selected_dates: list[str],
    dataset_ref: str | None = None,
    dataset_sha256: str | None = None,
    dataset_content_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": DATASET_FREEZE_RESULT_SCHEMA,
        "status": status,
        "reason_code": reason_code,
        "market": facts["market"],
        "account": facts["account"],
        "window_facts_content_sha256": facts["content_sha256"],
        "selected_trading_dates": selected_dates,
        "dataset_ref": dataset_ref,
        "dataset_sha256": dataset_sha256,
        "dataset_content_sha256": dataset_content_sha256,
    }


def _freeze_research_dataset(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    window_facts: Mapping[str, Any],
    required_days: int = RESEARCH_REQUIRED_DAYS,
    environ: Mapping[str, str] | None = None,
    ranking_projection_schema_version: str,
    publisher: Any,
    use_formal_corpus: bool = False,
) -> dict[str, Any]:
    if (
        isinstance(required_days, bool)
        or not isinstance(required_days, int)
        or required_days != RESEARCH_REQUIRED_DAYS
    ):
        _fail(
            "corpus_input_invalid",
            f"required_days must equal {RESEARCH_REQUIRED_DAYS}",
        )
    facts = _validate_window_facts(window_facts)
    market = str(facts["market"])
    account = str(facts["account"])
    if not _service_available(environ):
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="strategy_lab_service_disabled",
            selected_dates=[],
        )

    if use_formal_corpus:
        return _freeze_formal_research_dataset(
            store,
            artifact_root,
            facts=facts,
            required_days=required_days,
            publisher=publisher,
        )

    dates = list(facts["trading_calendar_dates"])
    latest_mature = facts["latest_mature_trading_date"]
    if latest_mature is None:
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="research_corpus_warming",
            selected_dates=[],
        )
    mature_index = dates.index(latest_mature)
    if mature_index + 1 < required_days:
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="research_corpus_warming",
            selected_dates=[],
        )
    selected_dates = dates[mature_index - required_days + 1 : mature_index + 1]
    days_by_date = {
        row["trading_date"]: row
        for row in _store_call(store.corpus_days, market, account)
    }
    points_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for row in _store_call(store.corpus_points, market, account):
        points_by_date.setdefault(str(row["trading_date"]), {})[
            str(row["recommendation_point_id"])
        ] = row

    has_conflict = False
    has_coverage_gap = False
    dataset_days: list[dict[str, Any]] = []
    for trading_date in selected_dates:
        day_row = days_by_date.get(trading_date)
        if day_row is None:
            has_coverage_gap = True
            continue
        if day_row["conflict_status"] == "conflict":
            has_conflict = True
            continue
        try:
            expectation, expectation_content = _read_indexed_expectation(
                artifact_root, day_row
            )
        except CorpusError:
            has_conflict = True
            continue
        if day_row["completeness_reason"] is not None:
            has_coverage_gap = True
            continue
        if (
            day_row["market_calendar_version"]
            != facts["market_calendar_version"]
            or day_row["market_calendar_sha256"]
            != facts["market_calendar_sha256"]
        ):
            has_coverage_gap = True
            continue
        if not _before(
            str(expectation["sealed_at_utc"]), str(facts["cutoff_at_utc"])
        ):
            has_coverage_gap = True
            continue
        expected_ids = list(expectation["expected_recommendation_point_ids"])
        if not expected_ids:
            has_coverage_gap = True
            continue
        point_rows = points_by_date.get(trading_date, {})
        if set(point_rows) - set(expected_ids):
            has_conflict = True
        if set(expected_ids) - set(point_rows):
            has_coverage_gap = True

        dataset_points: list[dict[str, Any]] = []
        for point_id in expected_ids:
            point_row = point_rows.get(point_id)
            if point_row is None:
                continue
            if point_row["conflict_status"] == "conflict":
                has_conflict = True
                continue
            if (
                point_row["capture_status"] != "captured"
                or point_row["reason_code"] is not None
            ):
                has_coverage_gap = True
                continue
            if (
                point_row["ranking_projection_schema_version"]
                != ranking_projection_schema_version
            ):
                has_coverage_gap = True
                continue
            try:
                captured_at = _timestamp(
                    point_row["captured_at_utc"], "captured_at_utc"
                )
            except CorpusError:
                has_conflict = True
                continue
            try:
                projection, projection_content = _read_indexed_projection(
                    artifact_root, point_row
                )
            except CorpusError:
                has_conflict = True
                continue
            decision_at = str(projection["decision_at_utc"])
            if _before(captured_at, decision_at):
                has_conflict = True
                continue
            if not _before(decision_at, str(facts["cutoff_at_utc"])) or not _before(
                captured_at, str(facts["cutoff_at_utc"])
            ):
                has_coverage_gap = True
                continue
            try:
                rerank_recommendation_point(
                    projection, ranking_profile="current_tie_break"
                )
            except Top1RankingError:
                has_coverage_gap = True
                continue
            dataset_points.append(
                {
                    "recommendation_point_id": point_id,
                    "projection_ref": point_row["projection_ref"],
                    "projection_content_sha256": projection[
                        "artifact_provenance"
                    ]["content_sha256"],
                    "projection_file_sha256": _file_sha256(projection_content),
                }
            )
        if len(dataset_points) == len(expected_ids):
            dataset_days.append(
                {
                    "trading_date": trading_date,
                    "expectation_ref": day_row["expectation_ref"],
                    "expectation_content_sha256": expectation["content_sha256"],
                    "expectation_file_sha256": _file_sha256(expectation_content),
                    "points": dataset_points,
                }
            )

    if has_conflict:
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="research_corpus_conflict",
            selected_dates=selected_dates,
        )
    if has_coverage_gap or len(dataset_days) != required_days:
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="research_dataset_coverage_missing",
            selected_dates=selected_dates,
        )

    dataset: dict[str, Any] = {
        "schema_version": SEALED_HISTORICAL_DATASET_SCHEMA,
        "market": market,
        "account": account,
        "cutoff_at_utc": facts["cutoff_at_utc"],
        "cutoff_trading_date": facts["cutoff_trading_date"],
        "required_days": required_days,
        "window_facts_content_sha256": facts["content_sha256"],
        "market_calendar_version": facts["market_calendar_version"],
        "market_calendar_ref": facts["market_calendar_ref"],
        "market_calendar_sha256": facts["market_calendar_sha256"],
        "trading_calendar_dates_sha256": facts[
            "trading_calendar_dates_sha256"
        ],
        "latest_mature_trading_date": latest_mature,
        "maturity_evidence_ref": facts["maturity_evidence_ref"],
        "maturity_evidence_sha256": facts["maturity_evidence_sha256"],
        "recommendation_point_selector": RECOMMENDATION_POINT_SELECTOR,
        "ranking_projection_schema_version": ranking_projection_schema_version,
        "selected_trading_dates": selected_dates,
        "days": dataset_days,
    }
    dataset["content_sha256"] = canonical_sha256(dataset)
    content = _render(dataset)
    ref = _dataset_ref(market, account, dataset["content_sha256"])
    try:
        publisher(artifact_root, ref, content)
    except ValueError:
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="research_corpus_conflict",
            selected_dates=selected_dates,
        )
    except OSError as exc:
        raise CorpusError(
            "corpus_artifact_conflict", "research dataset cannot be published"
        ) from exc
    return _freeze_result(
        facts,
        status="ready",
        reason_code=None,
        selected_dates=selected_dates,
        dataset_ref=ref,
        dataset_sha256=_file_sha256(content),
        dataset_content_sha256=dataset["content_sha256"],
    )


def _freeze_formal_research_dataset(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    facts: Mapping[str, Any],
    required_days: int,
    publisher: Any,
) -> dict[str, Any]:
    market = str(facts["market"])
    account = str(facts["account"])
    dates = list(facts["trading_calendar_dates"])
    latest_mature = facts["latest_mature_trading_date"]
    if latest_mature is None or dates.index(latest_mature) + 1 < required_days:
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="research_corpus_warming",
            selected_dates=[],
        )
    mature_index = dates.index(latest_mature)
    selected_dates = dates[mature_index - required_days + 1 : mature_index + 1]
    dataset_days: list[dict[str, Any]] = []
    conflict = False
    gap = False
    for trading_date in selected_dates:
        day = read_validation_day_source(
            store,
            artifact_root,
            market=market,
            account=account,
            trading_date=trading_date,
        )
        if day["status"] != "available":
            conflict |= day["status"] == "conflict"
            gap |= day["status"] != "conflict"
            continue
        expectation = day["expectation"]
        row = day["row"]
        assert isinstance(expectation, Mapping) and isinstance(row, Mapping)
        if (
            expectation["market_calendar_version"]
            != facts["market_calendar_version"]
            or expectation["market_calendar_sha256"]
            != facts["market_calendar_sha256"]
            or not _before(
                str(expectation["sealed_at_utc"]), str(facts["cutoff_at_utc"])
            )
        ):
            gap = True
            continue
        points: list[dict[str, Any]] = []
        for point_id in expectation["expected_recommendation_point_ids"]:
            source = read_validation_point_source(
                store,
                artifact_root,
                market=market,
                account=account,
                trading_date=trading_date,
                recommendation_point_id=str(point_id),
            )
            if source["status"] != "available":
                conflict |= source["status"] == "conflict"
                gap |= source["status"] != "conflict"
                continue
            point_row = source["point_row"]
            projection = source["projection"]
            assert isinstance(point_row, Mapping) and isinstance(
                projection, Mapping
            )
            captured_at = str(point_row["captured_at_utc"])
            decision_at = str(projection["decision_at_utc"])
            if _before(captured_at, decision_at):
                conflict = True
                continue
            if not _before(decision_at, str(facts["cutoff_at_utc"])) or not _before(
                captured_at, str(facts["cutoff_at_utc"])
            ):
                gap = True
                continue
            try:
                rerank_recommendation_point(
                    projection,
                    ranking_profile="current_tie_break",
                )
            except Top1RankingError:
                gap = True
                continue
            points.append(
                {
                    "recommendation_point_id": point_id,
                    "projection_ref": point_row["projection_ref"],
                    "projection_content_sha256": point_row[
                        "projection_content_sha256"
                    ],
                    "projection_file_sha256": point_row[
                        "projection_file_sha256"
                    ],
                }
            )
        if len(points) == len(expectation["expected_recommendation_point_ids"]):
            dataset_days.append(
                {
                    "trading_date": trading_date,
                    "expectation_ref": row["expectation_ref"],
                    "expectation_content_sha256": row[
                        "expectation_content_sha256"
                    ],
                    "expectation_file_sha256": row["expectation_file_sha256"],
                    "points": points,
                }
            )
        else:
            gap = True
    if conflict:
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="formal_corpus_conflict",
            selected_dates=selected_dates,
        )
    if gap or len(dataset_days) != required_days:
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="research_window_coverage_missing",
            selected_dates=selected_dates,
        )
    dataset: dict[str, Any] = {
        "schema_version": SEALED_HISTORICAL_DATASET_SCHEMA,
        "market": market,
        "account": account,
        "cutoff_at_utc": facts["cutoff_at_utc"],
        "cutoff_trading_date": facts["cutoff_trading_date"],
        "required_days": required_days,
        "window_facts_content_sha256": facts["content_sha256"],
        "market_calendar_version": facts["market_calendar_version"],
        "market_calendar_ref": facts["market_calendar_ref"],
        "market_calendar_sha256": facts["market_calendar_sha256"],
        "trading_calendar_dates_sha256": facts[
            "trading_calendar_dates_sha256"
        ],
        "latest_mature_trading_date": latest_mature,
        "maturity_evidence_ref": facts["maturity_evidence_ref"],
        "maturity_evidence_sha256": facts["maturity_evidence_sha256"],
        "recommendation_point_selector": RECOMMENDATION_POINT_SELECTOR,
        "ranking_projection_schema_version": RANKING_PROJECTION_SCHEMA_V3,
        "selected_trading_dates": selected_dates,
        "days": dataset_days,
    }
    dataset["content_sha256"] = canonical_sha256(dataset)
    content = _render(dataset)
    ref = _dataset_ref(market, account, dataset["content_sha256"])
    try:
        publisher(artifact_root, ref, content)
    except (OSError, ValueError):
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="formal_corpus_conflict",
            selected_dates=selected_dates,
        )
    return _freeze_result(
        facts,
        status="ready",
        reason_code=None,
        selected_dates=selected_dates,
        dataset_ref=ref,
        dataset_sha256=_file_sha256(content),
        dataset_content_sha256=dataset["content_sha256"],
    )


def freeze_research_dataset(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    window_facts: Mapping[str, Any],
    required_days: int = RESEARCH_REQUIRED_DAYS,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _freeze_research_dataset(
        store,
        artifact_root,
        window_facts=window_facts,
        required_days=required_days,
        environ=environ,
        ranking_projection_schema_version=RANKING_PROJECTION_SCHEMA_VERSION,
        publisher=publish_exact_text,
    )


def preview_research_dataset(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    window_facts: Mapping[str, Any],
    required_days: int = RESEARCH_REQUIRED_DAYS,
    environ: Mapping[str, str] | None = None,
    use_formal_corpus: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    captured: list[bytes] = []
    result = _freeze_research_dataset(
        store,
        artifact_root,
        window_facts=window_facts,
        required_days=required_days,
        environ=environ,
        ranking_projection_schema_version=(
            RANKING_PROJECTION_SCHEMA_V3
            if use_formal_corpus
            else RANKING_PROJECTION_SCHEMA_V2
        ),
        publisher=lambda _root, _ref, content: captured.append(content),
        use_formal_corpus=use_formal_corpus,
    )
    return result, json.loads(captured[0]) if captured else None


__all__ = [
    "CORPUS_COMMAND_RESULT_SCHEMA",
    "CORPUS_DAY_EXPECTATION_SCHEMA",
    "CORPUS_HEALTH_RECEIPT_SCHEMA",
    "CORPUS_STATUS_SCHEMA",
    "DATASET_FREEZE_RESULT_SCHEMA",
    "MAX_CORPUS_HEALTH_RECEIPT_BYTES",
    "MARKET_CALENDAR_POINTER_SCHEMA",
    "MARKET_CALENDAR_SNAPSHOT_SCHEMA",
    "RESEARCH_WINDOW_FACTS_SCHEMA",
    "SEALED_HISTORICAL_DATASET_SCHEMA",
    "CorpusError",
    "build_corpus_health_receipt",
    "capture_recommendation_point",
    "discover_recommendation_points",
    "freeze_research_dataset",
    "preview_research_dataset",
    "publish_corpus_health_receipt",
    "read_bound_market_calendar_snapshot",
    "read_corpus_status",
    "read_corpus_health_receipt",
    "read_market_calendar_binding",
    "refresh_market_calendar_binding",
    "read_validation_day_source",
    "read_validation_point_source",
    "seal_committed_day_expectation",
    "seal_day_expectation",
]
