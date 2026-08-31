from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, NoReturn
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import calc_futu_hk_terminal_fee
from domain.domain.symbol_identity import OPTION_CODE_RE, resolve_symbol_identity
from src.application.opend_call_coordinator import (
    LowPriorityOpenDCallDeferred,
    try_low_priority_opend_call,
)
from src.application.shadow_replay.common import render_json_text
from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
from src.application.strategy_lab.contracts import RECIPE_ID
from src.application.strategy_lab.recipe import project_validation_arms
from src.application.strategy_lab.readiness import (
    HISTORY_K_LOW_PRIORITY_CALLS_PER_WINDOW,
    HISTORY_K_MAX_PAGES,
)
from src.infrastructure.private_storage import (
    atomic_write_private_text,
    exclusive_private_file_lock,
    open_private_text,
    private_path,
)


EVIDENCE_SCHEMA = "strategy_lab_evidence"
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
_HK_TZ = ZoneInfo("Asia/Hong_Kong")
_HASH = frozenset("0123456789abcdef")
_ARTIFACT_KINDS = frozenset({"history_k", "expiry_close", "hidden_batch", "validation_fill"})


class StrategyLabEvidenceError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise StrategyLabEvidenceError(reason_code, message)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("research_evidence_invalid", f"{label} must be an object")
    return dict(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("research_evidence_invalid", f"{label} must be canonical text")
    return value


def _sha256(value: object, label: str) -> str:
    text_value = _text(value, label)
    if len(text_value) != 64 or set(text_value) - _HASH:
        _fail("research_evidence_invalid", f"{label} must be a lowercase SHA-256")
    return text_value


def _source_commit(value: object) -> str:
    text_value = _text(value, "producer_source_commit_sha")
    if len(text_value) != 40 or set(text_value) - _HASH:
        _fail(
            "research_evidence_invalid",
            "producer_source_commit_sha must be a lowercase Git commit",
        )
    return text_value


def _decimal(value: object, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        _fail("research_evidence_invalid", f"{label} is invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StrategyLabEvidenceError("research_evidence_invalid", f"{label} is invalid") from exc
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        _fail("research_evidence_invalid", f"{label} is invalid")
    return number


def _positive_int(value: object, label: str) -> int:
    number = _decimal(value, label, positive=True)
    if number != number.to_integral_value():
        _fail("research_evidence_invalid", f"{label} must be integral")
    return int(number)


def _utc(value: object, label: str) -> datetime:
    text_value = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StrategyLabEvidenceError("research_evidence_invalid", f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        _fail("research_evidence_invalid", f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _first_complete_minute(value: object) -> datetime:
    return _utc(value, "recommendation_available_at_utc").replace(second=0, microsecond=0) + timedelta(minutes=1)


def _session_end_utc(trading_date: str, trade_date_type: object) -> datetime:
    try:
        day = date.fromisoformat(trading_date)
    except ValueError as exc:
        raise StrategyLabEvidenceError("research_evidence_invalid", "trading_date is invalid") from exc
    if day.isoformat() != trading_date or trade_date_type not in {"WHOLE", "MORNING", "AFTERNOON"}:
        _fail("research_evidence_invalid", "frozen market session is invalid")
    end = time(12, 0) if trade_date_type == "MORNING" else time(16, 0)
    return datetime.combine(day, end, tzinfo=_HK_TZ).astimezone(timezone.utc)


def _fx_binding(
    value: object,
    *,
    expected_kind: str,
    expiration: str | None = None,
) -> dict[str, Any]:
    binding = _mapping(value, "FX binding")
    fact = binding.get("fact", binding)
    fact = _mapping(fact, "FX fact")
    if (
        fact.get("base_currency") != "HKD"
        or fact.get("quote_currency") != "CNY"
        or (expiration is not None and binding.get("expiration", expiration) != expiration)
    ):
        _fail("research_evidence_invalid", "FX binding identity changed")
    _decimal(fact.get("rate"), "FX rate", positive=True)
    if "fact" in binding:
        if binding.get("fact_sha256") != canonical_sha256(fact):
            _fail("research_evidence_invalid", "FX fact hash changed")
        fact_ref = binding.get("fact_ref")
        if (
            not isinstance(fact_ref, Mapping)
            or fact_ref.get("kind") != expected_kind
            or fact_ref.get("fact_id") != fact.get("fact_id")
        ):
            _fail("research_evidence_invalid", "FX fact reference changed")
        if expected_kind == "formal_point_fx_rate":
            _sha256(binding.get("source_fact_sha256"), "source_fact_sha256")
    return binding


def _terminal_binding(spec: Mapping[str, Any], expiration: str) -> dict[str, Any]:
    matches = [
        item
        for item in spec.get("terminal_fx_bindings", [])
        if isinstance(item, Mapping) and item.get("expiration") == expiration and item.get("currency") == "HKD"
    ]
    if len(matches) != 1:
        _fail("research_evidence_invalid", "terminal FX binding is unavailable")
    return _fx_binding(matches[0], expected_kind="fx_rate", expiration=expiration)


def _provider_source(spec: Mapping[str, Any]) -> dict[str, Any]:
    authority = _mapping(spec.get("history_k_authority"), "history_k_authority")
    probe = _mapping(authority.get("probe_request"), "history_k_authority.probe_request")
    binding = _mapping(probe.get("opend_binding"), "opend_binding")
    host = _text(binding.get("host"), "opend_binding.host")
    port = binding.get("port")
    if type(port) is not int or not 0 < port <= 65535:
        _fail("research_evidence_invalid", "opend_binding.port is invalid")
    return {
        "provider": "futu_opend",
        "opend_binding": {"host": host, "port": port},
        "source_authority_sha256": canonical_sha256(authority),
    }


def _history_query(
    *,
    candidate: Mapping[str, Any],
    trading_date: str,
    recommendation_available_at_utc: object,
    trade_date_type: object,
    provider_source: Mapping[str, Any],
    evaluator_behavior_sha256: str,
) -> dict[str, Any]:
    code = _text(candidate.get("contract_symbol"), "contract_symbol")
    start = _first_complete_minute(recommendation_available_at_utc)
    end = _session_end_utc(trading_date, trade_date_type)
    if start >= end or start.astimezone(_HK_TZ).date().isoformat() != trading_date:
        _fail("research_evidence_invalid", "history-K frozen window is invalid")
    return {
        "code": code,
        "start": trading_date,
        "end": trading_date,
        "ktype": "K_1M",
        "autype": "NONE",
        "fields": ["time_key", "high", "volume"],
        "max_count": 1000,
        "window_start_utc": _utc_text(start),
        "window_end_utc": _utc_text(end),
        "timezone": "Asia/Hong_Kong",
        "provider_source": dict(provider_source),
        "evaluator_behavior_sha256": evaluator_behavior_sha256,
    }


def _outcome_query(
    candidate: Mapping[str, Any],
    terminal_fx_binding: Mapping[str, Any],
    fee_plan: Mapping[str, Any],
    provider_source: Mapping[str, Any],
    evaluator_behavior_sha256: str,
) -> dict[str, Any]:
    contract = _text(candidate.get("contract_symbol"), "contract_symbol")
    identity = resolve_symbol_identity(contract)
    if identity is None or identity.market != "HK":
        _fail("research_evidence_invalid", "contract underlier identity is unavailable")
    source = _mapping(provider_source, "provider_source")
    binding = _mapping(source.get("opend_binding"), "provider_source.opend_binding")
    authority = {
        "provider": _text(source.get("provider"), "provider_source.provider"),
        "endpoint": "history_kline",
        "opend_binding": {
            "host": _text(binding.get("host"), "provider_source.opend_binding.host"),
            "port": _positive_int(binding.get("port"), "provider_source.opend_binding.port"),
        },
    }
    return {
        "underlying_code": identity.futu_code,
        "contract_symbol": contract,
        "expiration": _text(candidate.get("expiration"), "expiration"),
        "strike": float(_decimal(candidate.get("strike"), "strike", positive=True)),
        "multiplier": _positive_int(candidate.get("multiplier"), "multiplier"),
        "terminal_fx_binding": dict(terminal_fx_binding),
        "fee_plan": dict(fee_plan),
        "provider_source": {
            **authority,
            "source_authority_sha256": canonical_sha256(authority),
        },
        "evaluator_behavior_sha256": evaluator_behavior_sha256,
    }


def build_expiry_close_query(
    arm: Mapping[str, Any],
    terminal_fx_binding: Mapping[str, Any],
    fee_plan: Mapping[str, Any],
    provider_source: Mapping[str, Any],
    evaluator_behavior_sha256: str,
) -> dict[str, Any]:
    return _outcome_query(
        _mapping(arm, "arm").get("candidate", {}),
        terminal_fx_binding,
        fee_plan,
        provider_source,
        evaluator_behavior_sha256,
    )


def load_research_projection(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Project one frozen spec into shared queries and per-arm local work."""

    frozen = _mapping(spec, "spec")
    window = _mapping(frozen.get("research_window"), "research_window")
    sessions = window.get("sessions")
    if window.get("status") != "available" or not isinstance(sessions, list) or not sessions:
        _fail("research_evidence_invalid", "research window is unavailable")
    history_queries: dict[str, dict[str, Any]] = {}
    outcome_queries: dict[str, dict[str, Any]] = {}
    projected_arms: list[dict[str, Any]] = []
    expected_points: list[dict[str, str]] = []
    fee_plan_owner = _mapping(frozen.get("fee_plan"), "fee_plan")
    fee_plan = _mapping(fee_plan_owner.get("receipt"), "fee_plan.receipt")
    provider_source = _provider_source(frozen)
    behavior_sha256 = _sha256(frozen.get("evaluator_behavior_sha256"), "evaluator_behavior_sha256")
    for session in sessions:
        session = _mapping(session, "research session")
        trading_date = _text(session.get("trading_date"), "trading_date")
        calendar = _mapping(session.get("market_calendar_binding"), "market_calendar_binding")
        calendar_session = _mapping(calendar.get("session"), "market_calendar_binding.session")
        points = session.get("points")
        if not isinstance(points, list) or not points:
            _fail("research_evidence_invalid", "research session points are unavailable")
        for point in points:
            point = _mapping(point, "research point")
            point_id = _sha256(point.get("recommendation_point_id"), "recommendation_point_id")
            available_at_utc = point.get("recommendation_available_at_utc")
            opening_fx = _fx_binding(
                point.get("opening_fx_binding"),
                expected_kind="formal_point_fx_rate",
            )
            expected_points.append({"trading_day": trading_date, "recommendation_point_id": point_id})
            arms = point.get("arms")
            if not isinstance(arms, list) or not arms:
                _fail("research_evidence_invalid", "research point arms are unavailable")
            for arm in arms:
                arm = _mapping(arm, "research arm")
                arm_id = _text(arm.get("arm_id"), "arm_id")
                candidate = _mapping(arm.get("candidate"), "arm.candidate")
                history_query = _history_query(
                    candidate=candidate,
                    trading_date=trading_date,
                    recommendation_available_at_utc=available_at_utc,
                    trade_date_type=calendar_session.get("trade_date_type"),
                    provider_source=provider_source,
                    evaluator_behavior_sha256=behavior_sha256,
                )
                history_hash = canonical_sha256(history_query)
                history_queries.setdefault(
                    history_hash,
                    {
                        "query_sha256": history_hash,
                        "observation_key": f"history_k_query:{history_hash}",
                        "kind": "history_k_query",
                        "artifact_kind": "history_k",
                        "query": history_query,
                    },
                )
                expiration = _text(candidate.get("expiration"), "expiration")
                terminal_fx = _terminal_binding(frozen, expiration)
                outcome_query = _outcome_query(candidate, terminal_fx, fee_plan, provider_source, behavior_sha256)
                outcome_hash = canonical_sha256(outcome_query)
                outcome_queries.setdefault(
                    outcome_hash,
                    {
                        "query_sha256": outcome_hash,
                        "observation_key": f"expiry_close_query:{outcome_hash}",
                        "kind": "expiry_close_query",
                        "artifact_kind": "expiry_close",
                        "query": outcome_query,
                    },
                )
                projected_arms.append(
                    {
                        "trading_date": trading_date,
                        "recommendation_point_id": point_id,
                        "arm_id": arm_id,
                        "arm": {
                            **arm,
                            "recommendation_point_id": point_id,
                            "trading_day": trading_date,
                            "opening_fx_binding": opening_fx,
                        },
                        "history_k_query_sha256": history_hash,
                        "expiry_close_query_sha256": outcome_hash,
                        "research_fill_key": f"research_fill:{point_id}:{arm_id}",
                        "single_result_key": f"single_result:{point_id}:{arm_id}",
                    }
                )
    if len({item["recommendation_point_id"] for item in expected_points}) != len(expected_points):
        _fail("research_evidence_invalid", "research point identity is duplicated")
    return {
        "expected_points": expected_points,
        "history_k_queries": [history_queries[key] for key in sorted(history_queries)],
        "expiry_close_queries": [outcome_queries[key] for key in sorted(outcome_queries)],
        "arms": sorted(
            projected_arms,
            key=lambda item: (item["trading_date"], item["recommendation_point_id"], item["arm_id"]),
        ),
    }


def _artifact_location(artifact_root: str | Path, kind: str, query_sha256: str) -> tuple[Path, Path, str]:
    if kind not in _ARTIFACT_KINDS:
        _fail("research_evidence_invalid", "Strategy Lab evidence kind is invalid")
    digest = _sha256(query_sha256, "query_sha256")
    ref = f"evidence/{kind}/{digest}.json"
    target = private_path(artifact_root).joinpath(*ref.split("/"))
    return target, Path(f"{target}.lock"), ref


def read_evidence_artifact(artifact_root: str | Path, kind: str, query_sha256: str) -> dict[str, Any] | None:
    target, lock_path, ref = _artifact_location(artifact_root, kind, query_sha256)
    if not target.exists():
        return None
    try:
        with open_private_text(target) as handle:
            content = handle.read(MAX_EVIDENCE_BYTES + 1)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_EVIDENCE_BYTES:
            raise ValueError("artifact is too large")
        payload = json.loads(content)
        if not isinstance(payload, dict) or content != render_json_text(payload):
            raise ValueError("artifact is not canonical")
        expected_hash = canonical_sha256({key: value for key, value in payload.items() if key != "content_sha256"})
        if (
            payload.get("schema") != EVIDENCE_SCHEMA
            or payload.get("kind") != kind
            or payload.get("query_sha256") != query_sha256
            or payload.get("content_sha256") != expected_hash
            or not isinstance(payload.get("query"), Mapping)
            or canonical_sha256(payload["query"]) != query_sha256
        ):
            raise ValueError("artifact identity changed")
        _utc(payload.get("observed_at_utc"), "observed_at_utc")
        _source_commit(payload.get("producer_source_commit_sha"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StrategyLabEvidenceError(
            "research_evidence_artifact_invalid",
            "Strategy Lab evidence artifact is invalid",
        ) from exc
    return {
        "artifact_ref": ref,
        "artifact_sha256": hashlib.sha256(encoded).hexdigest(),
        "content_sha256": payload["content_sha256"],
        "artifact": payload,
        "payload": payload["payload"],
        "artifact_path": str(target),
        "lock_path": str(lock_path),
    }


def _observations(value: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in value:
        item = _mapping(raw, "observation")
        key = _text(item.get("observation_key"), "observation_key")
        if key in indexed:
            _fail("research_evidence_invalid", "observation key is duplicated")
        indexed[key] = item
    return indexed


def _observation_payload(observation: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(observation.get("payload"), "observation.payload")
    nested = payload.get("payload")
    return dict(nested) if isinstance(nested, Mapping) else payload


def _artifact_evidence_ref(observation: Mapping[str, Any]) -> dict[str, str]:
    return {
        "artifact_ref": _text(observation.get("artifact_ref"), "artifact_ref"),
        "artifact_sha256": _sha256(observation.get("artifact_sha256"), "artifact_sha256"),
    }


def _research_fill(arm: Mapping[str, Any], history: Mapping[str, Any]) -> dict[str, Any]:
    if history.get("status") != "available":
        return {
            "status": "not_evaluable",
            "reason_code": str(history.get("reason_code") or "research_history_k_invalid"),
            "simulated_fill_not_real_trade": True,
        }
    candidate = _mapping(arm.get("candidate"), "arm.candidate")
    sell_limit = _decimal(candidate.get("sell_limit"), "sell_limit", positive=True)
    price_tick = _decimal(candidate.get("price_tick"), "price_tick", positive=True)
    crossing = sell_limit + price_tick
    bars = history.get("bars")
    if not isinstance(bars, list) or not bars:
        _fail("research_evidence_invalid", "normalized history-K bars are unavailable")
    for bar in bars:
        bar = _mapping(bar, "history-K bar")
        if (
            _decimal(bar.get("high"), "bar.high", positive=True) >= crossing
            and _decimal(bar.get("volume"), "bar.volume") > 0
        ):
            return {
                "status": "simulated_fill",
                "fill_price": float(sell_limit),
                "crossing_price": float(crossing),
                "bar_time_utc": _text(bar.get("time_utc"), "bar.time_utc"),
                "simulated_fill_not_real_trade": True,
            }
    return {
        "status": "no_fill",
        "simulated_fill_not_real_trade": True,
    }


def next_missing_research_evidence(
    spec: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Return the next deterministic provider, bind, or local derivation action."""

    projection = load_research_projection(spec)
    indexed = _observations(observations)
    queries = {item["query_sha256"]: item for item in projection["history_k_queries"]}
    outcomes = {item["query_sha256"]: item for item in projection["expiry_close_queries"]}
    for query in projection["history_k_queries"]:
        if query["observation_key"] in indexed:
            continue
        artifact = read_evidence_artifact(artifact_root, query["artifact_kind"], query["query_sha256"])
        action = "bind_artifact" if artifact is not None else "collect_history_k"
        target, lock_path, _ref = _artifact_location(artifact_root, query["artifact_kind"], query["query_sha256"])
        return {
            "action": action,
            **query,
            "artifact": artifact,
            "artifact_path": str(target),
            "lock_path": str(lock_path),
        }
    for item in projection["arms"]:
        fill_key = item["research_fill_key"]
        fill_observation = indexed.get(fill_key)
        if fill_observation is None:
            history_query = queries[item["history_k_query_sha256"]]
            history_observation = indexed[history_query["observation_key"]]
            history = _observation_payload(history_observation)
            payload = _research_fill(item["arm"], history)
            payload["fill_evidence_ref"] = _artifact_evidence_ref(history_observation)
            return {
                "action": "derive_research_fill",
                "observation_key": fill_key,
                "recommendation_point_id": item["recommendation_point_id"],
                "arm_id": item["arm_id"],
                "kind": "research_fill",
                "payload": payload,
            }
        fill = _observation_payload(fill_observation)
        outcome: dict[str, Any] | None = None
        if fill.get("status") == "simulated_fill":
            query = outcomes[item["expiry_close_query_sha256"]]
            outcome_observation = indexed.get(query["observation_key"])
            if outcome_observation is None:
                artifact = read_evidence_artifact(artifact_root, query["artifact_kind"], query["query_sha256"])
                action = "bind_artifact" if artifact is not None else "collect_expiry_close"
                target, lock_path, _ref = _artifact_location(
                    artifact_root, query["artifact_kind"], query["query_sha256"]
                )
                return {
                    "action": action,
                    **query,
                    "artifact": artifact,
                    "artifact_path": str(target),
                    "lock_path": str(lock_path),
                }
            outcome = {
                **_observation_payload(outcome_observation),
                "outcome_evidence_ref": _artifact_evidence_ref(outcome_observation),
            }
        if item["single_result_key"] not in indexed:
            return {
                "action": "derive_single_result",
                "observation_key": item["single_result_key"],
                "recommendation_point_id": item["recommendation_point_id"],
                "arm_id": item["arm_id"],
                "kind": "single_result",
                "payload": build_single_recommendation_result(item["arm"], fill, outcome),
            }
    return {"action": "complete", "projection": projection}


def _provider_rows(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
        return [dict(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        rows = to_dict("records")
        if isinstance(rows, list) and all(isinstance(item, Mapping) for item in rows):
            return [dict(item) for item in rows]
    if value is None:
        return []
    _fail("research_history_k_invalid", "history-K rows are invalid")


def _low_priority_call(
    *,
    limiter_root: str | Path,
    endpoint: str,
    window_sec: float,
    max_calls: int,
    call: Callable[[], Any],
) -> Any:
    reserve = max_calls - min(HISTORY_K_LOW_PRIORITY_CALLS_PER_WINDOW, max(0, max_calls - 1))
    try:
        return try_low_priority_opend_call(
            base_dir=private_path(limiter_root),
            endpoint=endpoint,
            window_sec=window_sec,
            max_calls=max_calls,
            production_reserve_calls=reserve,
            call=call,
        )
    except LowPriorityOpenDCallDeferred as exc:
        raise StrategyLabEvidenceError(exc.reason_code, str(exc)) from exc


def _not_evaluable(reason_code: str) -> dict[str, Any]:
    return {"status": "not_evaluable", "reason_code": reason_code}


def collect_research_fill_evidence(
    gateway: Any,
    query: Mapping[str, Any],
    *,
    limiter_root: str | Path,
    window_sec: float,
    max_calls: int,
) -> dict[str, Any]:
    """Fetch and normalize one complete paginated history-K logical unit."""

    item = _mapping(query, "history-K query")
    provider_query = {key: item[key] for key in ("code", "start", "end", "ktype", "autype", "fields", "max_count")}
    raw_rows: list[dict[str, Any]] = []
    page_key: object = None
    page_count = 0
    try:
        for _page in range(HISTORY_K_MAX_PAGES):
            response = _low_priority_call(
                limiter_root=limiter_root,
                endpoint="history_kline",
                window_sec=window_sec,
                max_calls=max_calls,
                call=lambda page_key=page_key: gateway.request_history_kline(
                    **provider_query,
                    page_req_key=page_key,
                ),
            )
            if not isinstance(response, Mapping):
                return _not_evaluable("research_history_k_invalid")
            try:
                raw_rows.extend(_provider_rows(response.get("data")))
            except StrategyLabEvidenceError:
                return _not_evaluable("research_history_k_invalid")
            page_count += 1
            next_key = response.get("page_req_key")
            if next_key in (None, "", b""):
                break
            if next_key == page_key:
                return _not_evaluable("research_history_k_incomplete")
            page_key = next_key
        else:
            return _not_evaluable("research_history_k_incomplete")
    except StrategyLabEvidenceError:
        raise
    except Exception as exc:
        raise StrategyLabEvidenceError(
            "research_provider_failed",
            "history-K provider query failed",
        ) from exc
    if not raw_rows:
        return _not_evaluable("research_history_k_empty")
    start = _utc(item.get("window_start_utc"), "window_start_utc")
    end = _utc(item.get("window_end_utc"), "window_end_utc")
    bars: list[dict[str, Any]] = []
    previous: datetime | None = None
    try:
        for raw in raw_rows:
            time_key = _text(raw.get("time_key"), "bar.time_key")
            parsed_local = datetime.strptime(time_key, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_HK_TZ)
            parsed = parsed_local.astimezone(timezone.utc)
            if parsed > end or (previous is not None and parsed <= previous):
                return _not_evaluable("research_history_k_invalid")
            previous = parsed
            if parsed < start:
                continue
            bars.append(
                {
                    "time_utc": _utc_text(parsed),
                    "high": float(_decimal(raw.get("high"), "bar.high", positive=True)),
                    "volume": float(_decimal(raw.get("volume"), "bar.volume")),
                }
            )
    except (StrategyLabEvidenceError, ValueError):
        return _not_evaluable("research_history_k_invalid")
    if not bars:
        return _not_evaluable("research_history_k_empty")
    return {
        "status": "available",
        "pagination_complete": True,
        "page_count": page_count,
        "bar_count": len(bars),
        "bars": bars,
    }


def resolve_expiry_outcome(
    gateway: Any,
    query: Mapping[str, Any],
    fee_plan: Mapping[str, Any],
    terminal_fx_binding: Mapping[str, Any],
    *,
    limiter_root: str | Path,
    window_sec: float,
    max_calls: int,
) -> dict[str, Any]:
    """Resolve one exact expiration close and its frozen terminal economics."""

    item = _mapping(query, "expiry outcome query")
    expiration = _text(item.get("expiration"), "expiration")
    terminal_fx = _fx_binding(
        terminal_fx_binding,
        expected_kind="fx_rate",
        expiration=expiration,
    )
    if terminal_fx != item.get("terminal_fx_binding"):
        _fail("research_evidence_invalid", "terminal FX binding changed")
    plan = _mapping(fee_plan, "fee_plan")
    if plan != item.get("fee_plan"):
        _fail("research_evidence_invalid", "fee plan binding changed")
    if not isinstance(plan.get("commission_free"), bool):
        _fail("research_evidence_invalid", "fee plan is incomplete")
    _decimal(plan.get("platform_fee"), "platform_fee")
    _text(plan.get("fee_plan_ref"), "fee_plan_ref")
    try:
        row = _low_priority_call(
            limiter_root=limiter_root,
            endpoint="history_kline",
            window_sec=window_sec,
            max_calls=max_calls,
            call=lambda: gateway.get_exact_expiration_close(
                code=item["underlying_code"],
                expiration=expiration,
            ),
        )
    except StrategyLabEvidenceError:
        raise
    except Exception as exc:
        raise StrategyLabEvidenceError(
            "research_provider_failed",
            "expiration close provider query failed",
        ) from exc
    if row is None:
        return _not_evaluable("research_terminal_gap")
    if (
        not isinstance(row, Mapping)
        or row.get("code") != item.get("underlying_code")
        or row.get("expiration") != expiration
    ):
        return _not_evaluable("research_terminal_invalid")
    try:
        close = _decimal(row.get("close"), "expiration close", positive=True)
        strike = _decimal(item.get("strike"), "strike", positive=True)
        multiplier = _positive_int(item.get("multiplier"), "multiplier")
        terminal_kind = "assignment" if close < strike else "expired_worthless"
        terminal_fee = calc_futu_hk_terminal_fee(
            terminal_kind,
            order_price=float(strike),
            shares=multiplier,
            contracts=1,
            account_fee_plan=plan,
        )
    except (StrategyLabEvidenceError, ValueError, TypeError):
        return _not_evaluable("research_terminal_invalid")
    if terminal_fee.get("complete") is not True or terminal_fee.get("amount") is None:
        return _not_evaluable("research_terminal_fee_incomplete")
    return {
        "status": "available",
        "underlying_code": item["underlying_code"],
        "expiration": expiration,
        "underlying_close": float(close),
        "terminal_kind": terminal_kind,
        "terminal_fee": terminal_fee,
        "terminal_fx_binding": terminal_fx,
    }


def publish_evidence_artifact(
    artifact_root: str | Path,
    kind: str,
    query_sha256: str,
    payload: Mapping[str, Any],
    *,
    query: Mapping[str, Any],
    observed_at_utc: str,
    producer_source_commit_sha: str,
) -> dict[str, Any]:
    """Publish once or verify identical canonical Strategy Lab evidence bytes."""

    location = evidence_artifact_location(artifact_root, kind, query_sha256)
    target = Path(location["artifact_path"])
    lock_path = Path(location["lock_path"])
    canonical_query = _mapping(query, "evidence query")
    if canonical_sha256(canonical_query) != query_sha256:
        _fail("research_evidence_invalid", "Strategy Lab evidence query hash changed")
    body: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "kind": kind,
        "query_sha256": query_sha256,
        "query": canonical_query,
        "observed_at_utc": _utc_text(_utc(observed_at_utc, "observed_at_utc")),
        "producer_source_commit_sha": _source_commit(producer_source_commit_sha),
        "payload": _mapping(payload, "evidence payload"),
    }
    body["content_sha256"] = canonical_sha256(body)
    content = render_json_text(body)
    if len(content.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        _fail("research_evidence_artifact_invalid", "Strategy Lab evidence artifact is too large")

    def publish() -> dict[str, Any]:
        existing = read_evidence_artifact(artifact_root, kind, query_sha256)
        if existing is not None:
            if existing["artifact"] != body:
                _fail(
                    "research_evidence_immutable_conflict",
                    "Strategy Lab evidence artifact already has different content",
                )
            return existing
        atomic_write_private_text(target, content)
        readback = read_evidence_artifact(artifact_root, kind, query_sha256)
        if readback is None or readback["artifact"] != body:
            _fail("research_evidence_artifact_invalid", "Strategy Lab evidence readback failed")
        return readback

    with exclusive_private_file_lock(lock_path, blocking=False):
        return publish()


def evidence_artifact_location(artifact_root: str | Path, kind: str, query_sha256: str) -> dict[str, str]:
    target, lock_path, ref = _artifact_location(artifact_root, kind, query_sha256)
    return {"artifact_path": str(target), "lock_path": str(lock_path), "artifact_ref": ref}


def build_validation_point_evidence(
    formal_point: Mapping[str, Any],
    loaded_point: Mapping[str, Any],
    leader: Mapping[str, Any],
    session: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze one formal point and its future-only hidden-observation window."""

    projected = project_validation_arms(formal_point, leader)
    available = _utc(
        projected["recommendation_available_at_utc"],
        "recommendation_available_at_utc",
    )
    first_slot = available.replace(second=0, microsecond=0) + timedelta(minutes=1)
    endpoint = _utc(session.get("session_endpoint_utc"), "session_endpoint_utc")
    slots = [
        _utc_text(slot)
        for value in session.get("minute_grid_utc", [])
        if (slot := _utc(value, "minute_grid_utc")) >= first_slot and slot < endpoint
    ]
    point_id = _sha256(projected.get("recommendation_point_id"), "recommendation_point_id")
    artifact_ref = _text(loaded_point.get("artifact_ref"), "formal point artifact_ref")
    artifact_hash = _sha256(loaded_point.get("artifact_file_sha256"), "formal point artifact_file_sha256")
    arms: list[dict[str, Any]] = []
    for raw in projected["arms"]:
        arm = _mapping(raw, "validation arm")
        candidate = _mapping(arm.get("candidate"), "validation candidate")
        contract = _text(candidate.get("contract_symbol"), "contract_symbol")
        if OPTION_CODE_RE.fullmatch(contract) is None or not contract.startswith("HK."):
            _fail("validation_source_binding_mismatch", "validation option identity changed")
        arms.append(
            {
                **arm,
                "provider_code": contract,
                "sell_limit": float(_decimal(candidate.get("sell_limit"), "sell_limit", positive=True)),
            }
        )
    return {
        "status": "available",
        "trading_day": _text(session.get("trading_date"), "trading_date"),
        "recommendation_point_id": point_id,
        "scheduled_scan_target_market": _text(
            projected.get("scheduled_scan_target_market"), "scheduled_scan_target_market"
        ),
        "recommendation_available_at_utc": projected["recommendation_available_at_utc"],
        "active_slots_utc": slots,
        "session_endpoint_utc": _utc_text(endpoint),
        "formal_point_ref": artifact_ref,
        "formal_point_sha256": artifact_hash,
        "formal_point_content_sha256": _sha256(formal_point.get("content_sha256"), "formal_point.content_sha256"),
        "source_commit_sha": _source_commit(projected.get("source_commit_sha")),
        "opening_fx_binding": projected["opening_fx_binding"],
        "arms": arms,
    }


def build_hidden_batch_manifest(
    validation_plan: Mapping[str, Any],
    validation_points: Sequence[Mapping[str, Any]],
    *,
    trading_day: str,
    observation_slot_utc: str,
) -> dict[str, Any]:
    slot = _utc(observation_slot_utc, "observation_slot_utc")
    tolerance = validation_plan.get("validation_wake_tolerance_seconds")
    if type(tolerance) is not int or tolerance <= 0:
        _fail("validation_plan_invalid", "validation wake tolerance is invalid")
    arms: list[dict[str, Any]] = []
    for raw_point in validation_points:
        point = _mapping(raw_point, "validation point")
        if observation_slot_utc not in point.get("active_slots_utc", []):
            continue
        point_id = _sha256(point.get("recommendation_point_id"), "recommendation_point_id")
        for raw_arm in point.get("arms", []):
            arm = _mapping(raw_arm, "validation arm")
            arms.append(
                {
                    "recommendation_point_id": point_id,
                    "arm_id": _text(arm.get("arm_id"), "arm_id"),
                    "code": _text(arm.get("provider_code"), "provider_code"),
                    "sell_limit": float(_decimal(arm.get("sell_limit"), "sell_limit", positive=True)),
                }
            )
    arms.sort(key=lambda value: (value["recommendation_point_id"], value["arm_id"]))
    codes = sorted({arm["code"] for arm in arms})
    ceiling = validation_plan.get("hidden_snapshot_batch_ceiling")
    if type(ceiling) is not int or ceiling <= 0 or len(codes) > ceiling:
        _fail(
            "validation_snapshot_batch_limit_exceeded",
            "validation snapshot batch exceeds its frozen ceiling",
        )
    if not arms:
        _fail("validation_plan_invalid", "validation batch has no active arms")
    return {
        "trading_day": _text(trading_day, "trading_day"),
        "observation_slot_utc": _utc_text(slot),
        "deadline_utc": _utc_text(slot + timedelta(seconds=tolerance)),
        "validation_plan_sha256": canonical_sha256(validation_plan),
        "arms": arms,
        "option_codes": codes,
        "provider_source": _mapping(validation_plan.get("provider_source"), "provider_source"),
        "evaluator_behavior_sha256": _sha256(
            validation_plan.get("evaluator_behavior_sha256"), "evaluator_behavior_sha256"
        ),
        "query_controls": {
            "max_wait_sec": 0,
            "no_retry": True,
            "snapshot_fallback_max_codes": 0,
        },
    }


def _source_timestamp(row: Mapping[str, Any]) -> str | None:
    for key in (
        "update_time",
        "timestamp",
        "data_time",
        "time",
        "update_time_ms",
        "timestamp_ms",
        "snapshot_time_ms",
        "effective_at_ms",
    ):
        value = row.get(key)
        if value in (None, "") or isinstance(value, bool):
            continue
        try:
            if key.endswith("_ms"):
                parsed = datetime.fromtimestamp(float(value) / 1000, timezone.utc)
            elif isinstance(value, (int, float)):
                parsed = datetime.fromtimestamp(float(value), timezone.utc)
            else:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=_HK_TZ)
            return _utc_text(parsed)
        except (OverflowError, TypeError, ValueError):
            return None
    return None


def normalize_hidden_snapshot(manifest: Mapping[str, Any], result: MarketSnapshotFetchResult) -> dict[str, Any]:
    """Normalize exactly one provider batch without quote or time backfill."""

    expected_codes = frozenset(manifest["option_codes"])
    duplicate_or_unexpected = any(
        isinstance(error, Mapping)
        and error.get("error_code") in {"SNAPSHOT_DUPLICATE_CODES", "SNAPSHOT_UNEXPECTED_CODES"}
        for error in result.errors
    )
    if (
        result.opend_call_count != 1
        or result.requested_codes != expected_codes
        or bool(result.unexpected_codes)
        or bool(result.returned_codes - expected_codes)
        or bool(set(result.snap_map) - expected_codes)
        or duplicate_or_unexpected
        or result.fallback_filled != 0
        or any(isinstance(error, Mapping) and error.get("stage") == "market_snapshot" for error in result.errors)
    ):
        _fail("validation_snapshot_invalid", "hidden snapshot did not use one provider call")
    if result.requested_at_utc is None or result.received_at_utc is None:
        _fail("validation_snapshot_invalid", "hidden snapshot observation times are missing")
    try:
        requested_at = _utc(result.requested_at_utc, "requested_at_utc")
        received_at = _utc(result.received_at_utc, "received_at_utc")
        slot = _utc(manifest.get("observation_slot_utc"), "observation_slot_utc")
        deadline = _utc(manifest.get("deadline_utc"), "deadline_utc")
    except StrategyLabEvidenceError as exc:
        raise StrategyLabEvidenceError(
            "validation_snapshot_invalid", "hidden snapshot time envelope is invalid"
        ) from exc
    if not slot <= requested_at <= received_at <= deadline:
        _fail("validation_snapshot_invalid", "hidden snapshot time envelope is outside tolerance")
    quotes: list[dict[str, Any]] = []
    for code in manifest["option_codes"]:
        row = result.snap_map.get(code)
        source_time = _source_timestamp(row) if isinstance(row, Mapping) else None
        try:
            bid = _decimal(row.get("bid_price"), "bid_price", positive=True)  # type: ignore[union-attr]
            bid_volume = _decimal(row.get("bid_vol"), "bid_vol", positive=True)  # type: ignore[union-attr]
        except (AttributeError, StrategyLabEvidenceError):
            bid = bid_volume = None
        if source_time is None or bid is None or bid_volume is None:
            quotes.append(
                {
                    "code": code,
                    "status": "not_evaluable",
                    "reason_code": "validation_snapshot_invalid",
                }
            )
        else:
            quotes.append(
                {
                    "code": code,
                    "status": "available",
                    "bid_price": float(bid),
                    "bid_vol": float(bid_volume),
                    "source_time_utc": source_time,
                }
            )
    return {
        "status": "available",
        "requested_at_utc": _utc_text(requested_at),
        "observed_at_utc": _utc_text(received_at),
        "quotes": quotes,
    }


def _hidden_snapshot_quotes(manifest: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    body = _mapping(artifact.get("artifact"), "hidden batch artifact")
    payload = _mapping(body.get("payload"), "hidden batch payload")
    if body.get("kind") != "hidden_batch" or body.get("query") != dict(manifest):
        _fail("validation_snapshot_invalid", "hidden batch artifact binding changed")
    if payload.get("status") != "available" or body.get("observed_at_utc") != payload.get("observed_at_utc"):
        _fail("validation_snapshot_invalid", "hidden batch artifact time binding changed")
    quotes = payload.get("quotes")
    if not isinstance(quotes, list):
        _fail("validation_snapshot_invalid", "hidden batch quotes are invalid")
    indexed: dict[str, dict[str, Any]] = {}
    for raw in quotes:
        quote = _mapping(raw, "hidden batch quote")
        code = _text(quote.get("code"), "hidden batch quote code")
        if code in indexed or code not in manifest["option_codes"]:
            _fail("validation_snapshot_invalid", "hidden batch quote identity changed")
        if quote.get("status") == "available":
            _decimal(quote.get("bid_price"), "bid_price", positive=True)
            _decimal(quote.get("bid_vol"), "bid_vol", positive=True)
            _utc(quote.get("source_time_utc"), "source_time_utc")
        elif quote.get("status") != "not_evaluable" or quote.get("reason_code") != "validation_snapshot_invalid":
            _fail("validation_snapshot_invalid", "hidden batch quote status changed")
        indexed[code] = quote
    if set(indexed) != set(manifest["option_codes"]):
        _fail("validation_snapshot_invalid", "hidden batch quote coverage changed")
    return indexed


def hidden_snapshot_crossings(manifest: Mapping[str, Any], artifact: Mapping[str, Any]) -> list[tuple[str, str]]:
    quotes = _hidden_snapshot_quotes(manifest, artifact)
    return [
        (str(arm["recommendation_point_id"]), str(arm["arm_id"]))
        for arm in manifest["arms"]
        if quotes[arm["code"]].get("status") == "available"
        and _decimal(quotes[arm["code"]].get("bid_price"), "bid_price", positive=True)
        >= _decimal(arm["sell_limit"], "sell_limit", positive=True)
    ]


def build_validation_fill_evidence(
    point: Mapping[str, Any],
    arm_id: str,
    batch_observations: Sequence[Mapping[str, Any]],
    batch_artifacts: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, Any]:
    arm = next(
        (raw for raw in point.get("arms", []) if isinstance(raw, Mapping) and raw.get("arm_id") == arm_id),
        None,
    )
    if not isinstance(arm, Mapping):
        _fail("validation_source_binding_mismatch", "validation arm is unavailable")
    expected_slots = list(point.get("active_slots_utc", []))
    indexed = {
        str(observation.get("observation_slot_utc")): observation
        for observation in batch_observations
        if observation.get("kind") == "hidden_batch"
    }
    missing_slots: list[str] = []
    invalid_slots: list[str] = []
    bindings: list[dict[str, Any]] = []
    for slot in expected_slots:
        batch = indexed.get(slot)
        if batch is None:
            missing_slots.append(slot)
            continue
        key = str(batch.get("observation_key"))
        bindings.append(
            {
                "observation_slot_utc": slot,
                "observation_key": key,
                "status": batch.get("status"),
                "batch_ref": batch.get("artifact_ref"),
                "batch_sha256": batch.get("artifact_sha256"),
            }
        )
        artifact = batch_artifacts.get(key)
        if batch.get("status") != "complete" or artifact is None:
            invalid_slots.append(slot)
            continue
        try:
            if artifact.get("artifact_ref") != batch.get("artifact_ref") or artifact.get(
                "artifact_sha256"
            ) != batch.get("artifact_sha256"):
                _fail("validation_snapshot_invalid", "hidden batch Store binding changed")
            manifest = _mapping(batch.get("payload"), "hidden batch manifest")
            manifest_arm = next(
                (
                    raw
                    for raw in manifest.get("arms", [])
                    if isinstance(raw, Mapping)
                    and raw.get("recommendation_point_id") == point.get("recommendation_point_id")
                    and raw.get("arm_id") == arm_id
                ),
                None,
            )
            if not isinstance(manifest_arm, Mapping) or manifest_arm.get("code") != arm.get("provider_code"):
                _fail("validation_snapshot_invalid", "hidden batch arm binding changed")
            quote = _hidden_snapshot_quotes(manifest, artifact)[str(manifest_arm["code"])]
            if quote.get("status") != "available" or _decimal(
                quote.get("bid_price"), "bid_price", positive=True
            ) >= _decimal(manifest_arm.get("sell_limit"), "sell_limit", positive=True):
                _fail("validation_snapshot_invalid", "hidden batch terminal projection changed")
        except StrategyLabEvidenceError:
            invalid_slots.append(slot)
    query = {
        "recommendation_point_id": point["recommendation_point_id"],
        "arm_id": arm_id,
        "validation_plan_sha256": _sha256(point.get("validation_plan_sha256"), "validation_plan_sha256"),
        "evaluator_behavior_sha256": _sha256(point.get("evaluator_behavior_sha256"), "evaluator_behavior_sha256"),
        "formal_point_ref": _text(point.get("formal_point_ref"), "formal_point_ref"),
        "formal_point_sha256": _sha256(point.get("formal_point_sha256"), "formal_point_sha256"),
        "expected_slots_utc": expected_slots,
        "hidden_batches": bindings,
        "missing_slots_utc": missing_slots,
        "invalid_slots_utc": invalid_slots,
    }
    payload = (
        {
            "status": "not_evaluable",
            "reason_code": "validation_snapshot_invalid",
            "missing_slots_utc": missing_slots,
            "invalid_slots_utc": invalid_slots,
        }
        if missing_slots or invalid_slots
        else {"status": "no_fill"}
    )
    return {
        "query": query,
        "query_sha256": canonical_sha256(query),
        "payload": payload,
        "observed_at_utc": _utc_text(_utc(point["session_endpoint_utc"], "validation fill observed_at_utc")),
    }


def _rate(binding: Mapping[str, Any]) -> Decimal:
    fact = binding.get("fact", binding)
    return _decimal(_mapping(fact, "FX fact").get("rate"), "FX rate", positive=True)


def build_single_recommendation_result(
    arm: Mapping[str, Any],
    fill: Mapping[str, Any],
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the one-contract Sell Put result used by every comparison."""

    item = _mapping(arm, "arm")
    candidate = _mapping(item.get("candidate"), "arm.candidate")
    fill_item = _mapping(fill, "fill")
    fill_evidence_ref = _mapping(fill_item.get("fill_evidence_ref"), "fill_evidence_ref")
    _text(fill_evidence_ref.get("artifact_ref"), "fill_evidence_ref.artifact_ref")
    _sha256(
        fill_evidence_ref.get("artifact_sha256"),
        "fill_evidence_ref.artifact_sha256",
    )
    arm_kind = _text(item.get("kind"), "arm kind")
    if arm_kind not in {"baseline", "challenger"}:
        _fail("research_evidence_invalid", "arm kind is invalid")
    threshold = item.get("near_return_threshold")
    if (arm_kind == "baseline" and threshold is not None) or (
        arm_kind == "challenger" and _decimal(threshold, "near_return_threshold", positive=True) <= 0
    ):
        _fail("research_evidence_invalid", "arm threshold is invalid")
    identity = {
        "recommendation_point_id": _sha256(item.get("recommendation_point_id"), "recommendation_point_id"),
        "trading_day": _text(item.get("trading_day"), "trading_day"),
        "arm": arm_kind,
        "recipe_id": RECIPE_ID,
        "variant_id": None if arm_kind == "baseline" else _text(item.get("arm_id"), "variant_id"),
        "near_return_threshold": threshold,
        "arm_id": _text(item.get("arm_id"), "arm_id"),
        "candidate_id": _text(item.get("candidate_id"), "candidate_id"),
        "contract_symbol": _text(candidate.get("contract_symbol"), "contract_symbol"),
        "candidate_ref": {
            "candidate_id": _text(item.get("candidate_id"), "candidate_id"),
            "contract_symbol": _text(candidate.get("contract_symbol"), "contract_symbol"),
        },
        "safety_status": "pass",
        "fill_evidence_ref": fill_evidence_ref,
    }
    status = fill_item.get("status")
    observed_fill = status == "observed_fill"
    fill_declaration = (
        {"quote_evidence_not_broker_execution": True}
        if observed_fill or fill_item.get("quote_evidence_not_broker_execution") is True
        else {"simulated_fill_not_real_trade": True}
    )
    if status == "no_fill":
        return {
            **identity,
            "status": "no_fill",
            "fill_status": "no_fill",
            "fill_price": None,
            "fill_time": None,
            "outcome_status": "not_applicable",
            "outcome_evidence_ref": None,
            "economic_pnl_cny": 0.0,
            "annualized_return": 0.0,
            "return_capital_basis_cny": None,
            "holding_calendar_days": None,
            "reason_codes": [],
            **fill_declaration,
        }
    if status == "not_evaluable":
        return {
            **identity,
            "status": "not_evaluable",
            "fill_status": "not_evaluable",
            "fill_price": None,
            "fill_time": None,
            "outcome_status": "not_evaluable",
            "outcome_evidence_ref": None,
            "reason_codes": [str(fill_item.get("reason_code") or "research_fill_not_evaluable")],
            "economic_pnl_cny": None,
            "annualized_return": None,
            "return_capital_basis_cny": None,
            "holding_calendar_days": None,
            **fill_declaration,
        }
    if (
        status not in {"simulated_fill", "observed_fill"}
        or not isinstance(outcome, Mapping)
        or outcome.get("status") != "available"
    ):
        reason = (
            str(outcome.get("reason_code") or "research_outcome_not_evaluable")
            if isinstance(outcome, Mapping)
            else "research_outcome_not_evaluable"
        )
        return {
            **identity,
            "status": "not_evaluable",
            "fill_status": str(status or "not_evaluable"),
            "fill_price": fill_item.get("fill_price"),
            "fill_time": fill_item.get("fill_time" if observed_fill else "bar_time_utc"),
            "outcome_status": "not_evaluable",
            "outcome_evidence_ref": (outcome.get("outcome_evidence_ref") if isinstance(outcome, Mapping) else None),
            "reason_codes": [reason],
            "economic_pnl_cny": None,
            "annualized_return": None,
            "return_capital_basis_cny": None,
            "holding_calendar_days": None,
            **fill_declaration,
        }
    try:
        outcome_evidence_ref = _mapping(outcome.get("outcome_evidence_ref"), "outcome_evidence_ref")
        _text(
            outcome_evidence_ref.get("artifact_ref"),
            "outcome_evidence_ref.artifact_ref",
        )
        _sha256(
            outcome_evidence_ref.get("artifact_sha256"),
            "outcome_evidence_ref.artifact_sha256",
        )
        opening_fx = _rate(
            _fx_binding(
                item.get("opening_fx_binding"),
                expected_kind="formal_point_fx_rate",
            )
        )
        terminal_fx = _rate(
            _fx_binding(
                outcome.get("terminal_fx_binding"),
                expected_kind="fx_rate",
                expiration=str(candidate["expiration"]),
            )
        )
        opening_net = _decimal(candidate.get("net_premium", candidate.get("net_income")), "net premium", positive=True)
        strike = _decimal(candidate.get("strike"), "strike", positive=True)
        multiplier = _decimal(candidate.get("multiplier"), "multiplier", positive=True)
        close = _decimal(outcome.get("underlying_close"), "underlying close", positive=True)
        terminal_fee = _decimal(
            _mapping(outcome.get("terminal_fee"), "terminal fee").get("amount"),
            "terminal fee amount",
        )
        fill_time = _utc(
            fill_item.get("fill_time" if observed_fill else "bar_time_utc"),
            "fill_time",
        )
        expiration = date.fromisoformat(_text(candidate.get("expiration"), "expiration"))
        holding_days = (expiration - fill_time.astimezone(_HK_TZ).date()).days
        intrinsic = max(strike - close, Decimal("0")) * multiplier
        capital_cny = (strike * multiplier - opening_net) * opening_fx
        pnl_cny = opening_net * opening_fx - (intrinsic + terminal_fee) * terminal_fx
        if holding_days <= 0 or capital_cny <= 0:
            raise ValueError("result denominator is invalid")
        annualized = pnl_cny / capital_cny * Decimal(365) / Decimal(holding_days)
    except (KeyError, TypeError, ValueError, StrategyLabEvidenceError, InvalidOperation):
        return {
            **identity,
            "status": "not_evaluable",
            "fill_status": str(status or "not_evaluable"),
            "fill_price": fill_item.get("fill_price"),
            "fill_time": fill_item.get("fill_time" if observed_fill else "bar_time_utc"),
            "outcome_status": "not_evaluable",
            "outcome_evidence_ref": (outcome.get("outcome_evidence_ref") if isinstance(outcome, Mapping) else None),
            "reason_codes": ["research_result_invalid"],
            "economic_pnl_cny": None,
            "annualized_return": None,
            "return_capital_basis_cny": None,
            "holding_calendar_days": None,
            **fill_declaration,
        }
    return {
        **identity,
        "status": "available",
        "fill_status": status,
        "fill_price": float(_decimal(fill_item.get("fill_price"), "fill_price", positive=True)),
        "fill_time": _utc_text(fill_time),
        "outcome_status": "available",
        "outcome_evidence_ref": outcome_evidence_ref,
        "economic_pnl_cny": round(float(pnl_cny), 6),
        "annualized_return": round(float(annualized), 12),
        "return_capital_basis_cny": round(float(capital_cny), 6),
        "holding_calendar_days": holding_days,
        "reason_codes": [],
        "opening_net_premium": float(opening_net),
        "terminal_intrinsic_loss": float(intrinsic),
        "terminal_fee": float(terminal_fee),
        **fill_declaration,
    }


__all__ = [
    "EVIDENCE_SCHEMA",
    "MAX_EVIDENCE_BYTES",
    "StrategyLabEvidenceError",
    "build_hidden_batch_manifest",
    "build_expiry_close_query",
    "build_single_recommendation_result",
    "build_validation_fill_evidence",
    "build_validation_point_evidence",
    "collect_research_fill_evidence",
    "evidence_artifact_location",
    "hidden_snapshot_crossings",
    "load_research_projection",
    "next_missing_research_evidence",
    "normalize_hidden_snapshot",
    "publish_evidence_artifact",
    "read_evidence_artifact",
    "resolve_expiry_outcome",
]
