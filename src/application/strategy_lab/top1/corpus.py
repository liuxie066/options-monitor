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
    build_corpus_health_receipt,
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
RESEARCH_WINDOW_FACTS_SCHEMA = "sell_put_top1_research_window_facts.v1"
DATASET_FREEZE_RESULT_SCHEMA = "sell_put_top1_dataset_freeze_result.v1"
HISTORY_MIGRATION_PREVIEW_SCHEMA = "sell_put_top1_history_migration_preview.v1"

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


def preview_archived_recommendation_point_migration(
    store: ExperimentStore,
    source_root: str | Path,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
) -> dict[str, Any]:
    """Preview strict historical point reuse without repairing old evidence."""

    market, account = _identity(market, account)
    root = Path(source_root).resolve()
    if not root.is_dir():
        _fail("corpus_input_invalid", "historical source root is unavailable")

    inventory_runs: dict[str, dict[str, Any]] = {}
    inventory_status = "missing"
    inventory_path = root / "manifests" / "inventory.latest.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if (
            isinstance(inventory, Mapping)
            and inventory.get("schema_version") == "research_archive.v2"
            and isinstance(inventory.get("runs"), list)
        ):
            for raw_run in inventory["runs"]:
                if not isinstance(raw_run, Mapping):
                    continue
                run_id = str(raw_run.get("run_id") or "")
                if not run_id:
                    continue
                account_evidence = next(
                    (
                        dict(item)
                        for item in (
                            (raw_run.get("candidate_evidence") or {}).get("accounts")
                            if isinstance(raw_run.get("candidate_evidence"), Mapping)
                            else []
                        )
                        if isinstance(item, Mapping)
                        and item.get("account") == account
                    ),
                    {},
                )
                inventory_runs[run_id] = {
                    "candidate_evidence_reason_code": str(
                        account_evidence.get("reason_code")
                        or account_evidence.get("status")
                        or "candidate_snapshot_contract_missing"
                    ),
                }
            inventory_status = "verified"
        else:
            inventory_status = "invalid"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass

    scheduled_runs: list[dict[str, Any]] = []
    pattern = "output_runs/*/state/scheduler_decision.json"
    for path in sorted(root.glob(pattern)):
        ref = path.relative_to(root).as_posix()
        matched = _SCHEDULER_DECISION_REF.fullmatch(ref)
        if matched is None:
            continue
        run_id = matched.group(1)
        if not (root / "output_runs" / run_id / "accounts" / account).is_dir():
            continue
        try:
            content = path.read_bytes()
            envelope = json.loads(content.decode("utf-8"))
            payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
            decision = (
                payload.get("decision")
                if isinstance(payload, Mapping)
                else envelope
            )
            if not isinstance(decision, Mapping) or decision.get("should_run_scan") is not True:
                continue
            target_raw = decision.get("scheduled_scan_target_market") or decision.get(
                "scheduled_target_market"
            )
            market_clock = target_raw or decision.get("now_market")
            parsed_clock = datetime.fromisoformat(str(market_clock))
            if parsed_clock.utcoffset() != timedelta(hours=8):
                continue
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            continue

        target = None
        point_id = None
        trading_date = None
        if target_raw:
            try:
                target = utc_timestamp(target_raw, "scheduled_scan_target_market")
                point_id = build_recommendation_point_id(market, account, target)
                trading_date = (
                    datetime.fromisoformat(target.replace("Z", "+00:00"))
                    .astimezone(ZoneInfo("Asia/Hong_Kong"))
                    .date()
                    .isoformat()
                )
            except (CandidateSnapshotContractError, RecommendationPointError, ValueError):
                target = None
        scheduled_runs.append(
            {
                "run_id": run_id,
                "scheduler_decision_ref": ref,
                "scheduler_decision_sha256": _file_sha256(content),
                "scheduled_scan_target_market": target,
                "recommendation_point_id": point_id,
                "trading_date": trading_date,
                **inventory_runs.get(run_id, {}),
            }
        )

    items: list[dict[str, Any]] = []
    for run in scheduled_runs:
        if run["scheduled_scan_target_market"] is not None:
            continue
        items.append(
            {
                "status": "gap",
                "reason_code": "historical_point_evidence_missing",
                "evidence_reason_code": "scheduler_identity_missing",
                "recommendation_point_id": None,
                "scheduled_scan_target_market": None,
                "trading_date": None,
                "source_run_ids": [run["run_id"]],
                "source_point_ref": None,
                "projection_content_sha256": None,
                "estimated_incremental_bytes": 0,
            }
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in scheduled_runs:
        target = run["scheduled_scan_target_market"]
        if target is not None:
            grouped.setdefault(str(target), []).append(run)

    store_state = store.schema_state()
    for target, sources in sorted(grouped.items()):
        source_run_ids = sorted(str(source["run_id"]) for source in sources)
        valid: list[tuple[dict[str, Any], dict[str, Any], bytes, str]] = []
        point_error: str | None = None
        for source in sources:
            run_id = str(source["run_id"])
            point_ref = (
                f"output_runs/{run_id}/accounts/{account}/state/"
                f"{RECOMMENDATION_POINT_FILE}"
            )
            if not root.joinpath(*point_ref.split("/")).is_file():
                continue
            try:
                point = load_recommendation_point(root, run_id, account)
                if (
                    point["market"] != market
                    or point["scheduled_scan_target_market"] != target
                    or point["schema_version"] != RECOMMENDATION_POINT_SCHEMA_V2
                ):
                    raise RecommendationPointError(
                        "option_market_evidence_contract_missing",
                        "historical point is not equivalent to the current contract",
                    )
                snapshot = load_opening_candidate_snapshot(
                    base=root,
                    run_id=run_id,
                    account=account,
                    require_current_contract=True,
                )
                if snapshot.get("content_sha256") != point["opening_snapshot_sha256"]:
                    raise OpeningCandidateSnapshotError(
                        "opening snapshot hash does not match historical point"
                    )
                projection = _build_point_ranking_projection(root, point, snapshot)
                if projection["producer_accepted_candidate_ids"] != point[
                    "producer_accepted_candidate_ids"
                ]:
                    raise Top1RankingError(
                        "ranking_projection_incomplete",
                        "historical accepted candidate set does not match",
                    )
                projection_content = _render(projection)
                valid.append((point, projection, projection_content, point_ref))
            except RecommendationPointError as exc:
                point_error = exc.reason_code
            except OpeningCandidateSnapshotError as exc:
                point_error = (
                    "opening_snapshot_missing"
                    if "unavailable" in str(exc).lower()
                    else "opening_snapshot_conflict"
                )
            except PreparedOptionPositionsContextError as exc:
                point_error = str(exc) or "option_market_evidence_contract_missing"
            except Top1RankingError as exc:
                point_error = exc.reason_code

        item_base = {
            "recommendation_point_id": sources[0]["recommendation_point_id"],
            "scheduled_scan_target_market": target,
            "trading_date": sources[0]["trading_date"],
            "source_run_ids": source_run_ids,
        }
        if len(valid) > 1 and len(
            {
                (point["content_sha256"], projection["artifact_provenance"]["content_sha256"])
                for point, projection, _content, _ref in valid
            }
        ) > 1:
            items.append(
                {
                    **item_base,
                    "status": "conflict",
                    "reason_code": "historical_point_conflict",
                    "evidence_reason_code": "duplicate_strict_point_content",
                    "source_point_ref": None,
                    "projection_content_sha256": None,
                    "estimated_incremental_bytes": 0,
                }
            )
            continue
        if not valid:
            status = "gap"
            reason_code = "historical_point_evidence_missing"
            evidence_reason = (
                "duplicate_scheduler_identity"
                if len(sources) > 1
                else point_error
                or next(
                    (
                        str(source.get("candidate_evidence_reason_code"))
                        for source in sources
                        if source.get("candidate_evidence_reason_code")
                    ),
                    "option_market_evidence_contract_missing",
                )
            )
            items.append(
                {
                    **item_base,
                    "status": status,
                    "reason_code": reason_code,
                    "evidence_reason_code": evidence_reason,
                    "source_point_ref": None,
                    "projection_content_sha256": None,
                    "estimated_incremental_bytes": 0,
                }
            )
            continue

        point, projection, projection_content, point_ref = valid[0]
        status = "ready"
        reason_code = None
        evidence_reason_code = None
        if store_state.get("status") == "ready":
            row = _store_call(
                store.corpus_point,
                market,
                account,
                point["recommendation_point_id"],
            )
            if row is not None:
                try:
                    indexed, indexed_content = _read_indexed_projection(
                        artifact_root, row
                    )
                    matches = (
                        row["conflict_status"] == "clean"
                        and row["capture_status"] == "captured"
                        and row["source_run_id"] == point["run_id"]
                        and row["source_point_content_sha256"]
                        == point["content_sha256"]
                        and indexed["artifact_provenance"]["content_sha256"]
                        == projection["artifact_provenance"]["content_sha256"]
                        and indexed_content == projection_content
                    )
                except CorpusError:
                    matches = False
                if matches:
                    status = "idempotent"
                    reason_code = "historical_point_already_imported"
                else:
                    status = "conflict"
                    reason_code = "historical_point_conflict"
                    evidence_reason_code = "indexed_point_content_mismatch"
        items.append(
            {
                **item_base,
                "status": status,
                "reason_code": reason_code,
                "evidence_reason_code": evidence_reason_code,
                "source_point_ref": point_ref,
                "projection_content_sha256": projection[
                    "artifact_provenance"
                ]["content_sha256"],
                "estimated_incremental_bytes": (
                    len(projection_content) if status == "ready" else 0
                ),
            }
        )

    items.sort(
        key=lambda item: (
            str(item["scheduled_scan_target_market"] or ""),
            ",".join(item["source_run_ids"]),
        )
    )
    counts = {
        status: sum(item["status"] == status for item in items)
        for status in ("ready", "idempotent", "conflict", "gap")
    }
    result: dict[str, Any] = {
        "schema_version": HISTORY_MIGRATION_PREVIEW_SCHEMA,
        "operation": "preview",
        "market": market,
        "account": account,
        "source_run_count": len(scheduled_runs),
        "point_identity_count": len(grouped),
        "unidentified_run_count": sum(
            run["scheduled_scan_target_market"] is None for run in scheduled_runs
        ),
        "preview_item_count": len(items),
        "archive_inventory_status": inventory_status,
        "store_schema": store_state,
        "counts": counts,
        "estimated_incremental_bytes": sum(
            int(item["estimated_incremental_bytes"]) for item in items
        ),
        "items": items,
    }
    result["preview_sha256"] = canonical_sha256(result)
    return result


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
    expected_ids = expectation["expected_recommendation_point_ids"]
    if recommendation_point_id not in expected_ids:
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
        return build_corpus_health_receipt(
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
    "CORPUS_STATUS_SCHEMA",
    "DATASET_FREEZE_RESULT_SCHEMA",
    "HISTORY_MIGRATION_PREVIEW_SCHEMA",
    "MARKET_CALENDAR_POINTER_SCHEMA",
    "MARKET_CALENDAR_SNAPSHOT_SCHEMA",
    "RESEARCH_WINDOW_FACTS_SCHEMA",
    "SEALED_HISTORICAL_DATASET_SCHEMA",
    "CorpusError",
    "capture_recommendation_point",
    "discover_recommendation_points",
    "freeze_research_dataset",
    "preview_archived_recommendation_point_migration",
    "preview_research_dataset",
    "read_bound_market_calendar_snapshot",
    "read_corpus_status",
    "read_market_calendar_binding",
    "refresh_market_calendar_binding",
    "read_validation_day_source",
    "read_validation_point_source",
    "seal_committed_day_expectation",
    "seal_day_expectation",
]
