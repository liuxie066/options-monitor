from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, NoReturn

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    utc_timestamp,
)
from src.application.recommendation_point import strategy_lab_top1_available
from src.application.research.formal_corpus import (
    FormalCorpusError,
    MARKET_CALENDAR_POINTER_SCHEMA,
    MARKET_CALENDAR_SNAPSHOT_SCHEMA,
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
    RANKING_PROJECTION_SCHEMA_V3,
    Top1RankingError,
    build_top1_recipe_projection,
    materialize_top1_recipe_input,
    rerank_recommendation_point,
)


RESEARCH_WINDOW_FACTS_SCHEMA = "sell_put_top1_research_window_facts.v1"
DATASET_FREEZE_RESULT_SCHEMA = "sell_put_top1_dataset_freeze_result.v1"

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


def _service_available(environ: Mapping[str, str] | None) -> bool:
    return strategy_lab_top1_available(environ)


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


def read_validation_day_source(
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    trading_date: str,
) -> dict[str, Any]:
    """Read one indexed validation denominator without repairing producer data."""

    market, account = _identity(market, account)
    trading_date = _trading_date(trading_date)
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
    artifact_root: str | Path,
    *,
    window_facts: Mapping[str, Any],
    required_days: int,
    environ: Mapping[str, str] | None,
    publisher: Any,
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
    if not _service_available(environ):
        return _freeze_result(
            facts,
            status="blocked",
            reason_code="strategy_lab_service_disabled",
            selected_dates=[],
        )
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


def preview_research_dataset(
    artifact_root: str | Path,
    *,
    window_facts: Mapping[str, Any],
    required_days: int = RESEARCH_REQUIRED_DAYS,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    captured: list[bytes] = []
    result = _freeze_research_dataset(
        artifact_root,
        window_facts=window_facts,
        required_days=required_days,
        environ=environ,
        publisher=lambda _root, _ref, content: captured.append(content),
    )
    return result, json.loads(captured[0]) if captured else None


__all__ = [
    "DATASET_FREEZE_RESULT_SCHEMA",
    "MARKET_CALENDAR_POINTER_SCHEMA",
    "MARKET_CALENDAR_SNAPSHOT_SCHEMA",
    "RESEARCH_WINDOW_FACTS_SCHEMA",
    "SEALED_HISTORICAL_DATASET_SCHEMA",
    "CorpusError",
    "preview_research_dataset",
    "read_bound_market_calendar_snapshot",
    "read_market_calendar_binding",
    "refresh_market_calendar_binding",
    "read_validation_day_source",
    "read_validation_point_source",
]
