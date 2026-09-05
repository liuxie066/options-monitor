from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from datetime import date, datetime, time as wall_time, timedelta, timezone
from numbers import Real
from pathlib import Path
from typing import Any, Callable, NoReturn
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.performance.models import select_fx_rate
from domain.domain.symbol_identity import OPTION_CODE_RE, resolve_symbol_identity
from src.application.account_config import normalize_account_label
from src.application.candidate_snapshot_contract import utc_timestamp
from src.application.opend_call_coordinator import (
    INTERRUPTIBLE_OPEND_UNIT_TIMEOUT_SECONDS,
    InterruptibleOpenDCallError,
    LowPriorityOpenDCallDeferred,
    run_interruptible_opend_unit,
    try_low_priority_opend_call,
)
from src.application.performance.account_fee_plan import load_account_fee_plan_receipt
from src.application.research.formal_corpus import (
    FormalCorpusError,
    load_formal_expectation,
    load_formal_point,
    read_expectation_bound_market_calendar_snapshot,
    read_market_calendar_binding,
)
from src.application.recommendation_point import build_recommendation_point_id
from src.application.scan_scheduler import (
    scheduled_scan_targets_for_date,
    scheduled_session_slots_for_date,
)
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.contracts import (
    ACCOUNT,
    HIDDEN_SNAPSHOT_BATCH_CEILING,
    RESEARCH_SESSIONS,
    TICK_PROTECTION_SECONDS,
    VALIDATION_SESSIONS,
    VALIDATION_WAKE_TOLERANCE_SECONDS,
    build_strategy_lab_timer_binding,
)
from src.application.strategy_lab.recipe import (
    StrategyLabRecipeError,
    build_concentration_arms,
    project_validation_arms,
    validate_recipe_leader,
)
from src.infrastructure.performance_evidence_sqlite import (
    PerformanceEvidenceSQLiteRepository,
)
from src.infrastructure.private_storage import (
    atomic_write_private_text,
    exclusive_private_file_lock,
    open_private_text,
    private_path,
)


HISTORY_K_READINESS_SCHEMA = "strategy_lab_history_k_readiness"
HISTORY_K_READINESS_TTL = timedelta(hours=24)
HISTORY_K_LOW_PRIORITY_CALLS_PER_WINDOW = 4
HISTORY_K_MAX_PAGES = 3
HISTORY_K_POC_NOT_BEFORE_HK = wall_time(16, 10)
MAX_HISTORY_K_READINESS_BYTES = 16 * 1024
_HK_TZ = ZoneInfo("Asia/Hong_Kong")
_HASH = frozenset("0123456789abcdef")


class HistoryKReadinessError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise HistoryKReadinessError(reason_code, message)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("history_k_probe_invalid", f"{label} must be canonical text")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or set(text) - _HASH:
        _fail("history_k_probe_invalid", f"{label} must be a lowercase SHA-256")
    return text


def _binding(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"host", "port"}:
        _fail("history_k_probe_invalid", "OpenD binding is invalid")
    host = _text(value.get("host"), "opend_binding.host")
    port = value.get("port")
    if type(port) is not int or not 0 < port <= 65535:
        _fail("history_k_probe_invalid", "opend_binding.port is invalid")
    return {"host": host, "port": port}


def _timestamp(value: object, label: str) -> str:
    try:
        return utc_timestamp(value, label)
    except ValueError as exc:
        raise HistoryKReadinessError("history_k_probe_invalid", str(exc)) from exc


def _date(value: object, label: str) -> date:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise HistoryKReadinessError("history_k_probe_invalid", f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        _fail("history_k_probe_invalid", f"{label} must use YYYY-MM-DD")
    return parsed


def _contract_expiration(contract_symbol: str) -> date:
    match = OPTION_CODE_RE.fullmatch(contract_symbol)
    if match is None or match.group("market") != "HK" or match.group("cp") != "P":
        _fail("history_k_probe_invalid", "contract_symbol must be a canonical HK PUT code")
    try:
        return date(2000 + int(match.group("yy")), int(match.group("mm")), int(match.group("dd")))
    except ValueError as exc:
        raise HistoryKReadinessError("history_k_probe_invalid", "contract_symbol expiry is invalid") from exc


def build_history_k_probe_request(
    *,
    market: object,
    account: object,
    opend_binding: object,
    contract_symbol: object,
    underlier_code: object,
    sample_date: object,
    as_of_utc: object,
) -> dict[str, object]:
    try:
        normalized_account = normalize_account_label(account)
    except ValueError as exc:
        raise HistoryKReadinessError("history_k_probe_invalid", str(exc)) from exc
    if market != "HK" or account != normalized_account:
        _fail("history_k_probe_invalid", "history-K PoC supports one canonical HK account")
    binding = _binding(opend_binding)
    contract = _text(contract_symbol, "contract_symbol").upper()
    expiration = _contract_expiration(contract)
    contract_identity = resolve_symbol_identity(contract)
    identity = resolve_symbol_identity(underlier_code)
    quota_code = _text(underlier_code, "underlier_code").upper()
    if identity is None or identity.market != "HK" or identity.futu_code != quota_code:
        _fail("history_k_probe_invalid", "underlier_code must be a canonical HK OpenD code")
    if contract_identity is None or contract_identity.futu_code != quota_code:
        _fail("history_k_probe_invalid", "contract_symbol and underlier_code must identify the same security")
    sample = _date(sample_date, "sample_date")
    observed = datetime.fromisoformat(_timestamp(as_of_utc, "as_of_utc").replace("Z", "+00:00"))
    if expiration >= observed.astimezone(_HK_TZ).date():
        _fail("history_k_probe_invalid", "history-K PoC requires an expired option contract")
    if sample > expiration:
        _fail("history_k_probe_invalid", "sample_date must not be after contract expiry")
    return {
        "schema": "strategy_lab_history_k_probe",
        "market": "HK",
        "account": normalized_account,
        "opend_binding": binding,
        "quota_code": quota_code,
        "sample_query": {
            "code": contract,
            "start": sample.isoformat(),
            "end": sample.isoformat(),
            "ktype": "K_1M",
            "autype": "NONE",
            "fields": ["time_key", "high", "volume"],
            "max_count": 1000,
        },
    }


def preview_history_k_readiness(**request: object) -> dict[str, object]:
    probe_request = build_history_k_probe_request(**request)
    return {
        "status": "confirmation_required",
        "probe_request": probe_request,
        "probe_sha256": canonical_sha256(probe_request),
    }


def _normalized_probe_request(value: object, *, as_of_utc: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "market",
        "account",
        "opend_binding",
        "quota_code",
        "sample_query",
    }:
        _fail("history_k_probe_invalid", "probe request fields are invalid")
    query = value.get("sample_query")
    if not isinstance(query, Mapping):
        _fail("history_k_probe_invalid", "sample query is invalid")
    expected = build_history_k_probe_request(
        market=value.get("market"),
        account=value.get("account"),
        opend_binding=value.get("opend_binding"),
        contract_symbol=query.get("code"),
        underlier_code=value.get("quota_code"),
        sample_date=query.get("start"),
        as_of_utc=as_of_utc,
    )
    if dict(value) != expected:
        _fail("history_k_probe_invalid", "probe request is not canonical")
    return expected


def _receipt_ref(observed_date: str, probe_sha256: str) -> str:
    return f"readiness/history-k/{observed_date}/{probe_sha256}.json"


def _rows(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if any(not isinstance(item, Mapping) for item in value):
            _fail("history_k_probe_invalid_response", "history K-line page rows are invalid")
        return [dict(item) for item in value]
    if hasattr(value, "to_dict"):
        converted = value.to_dict("records")
        if isinstance(converted, list):
            if any(not isinstance(item, Mapping) for item in converted):
                _fail("history_k_probe_invalid_response", "history K-line page rows are invalid")
            return [dict(item) for item in converted]
    if value is None:
        return []
    _fail("history_k_probe_invalid_response", "history K-line page rows are invalid")


def _number(value: object, label: str, *, positive: bool) -> float:
    if isinstance(value, (bool, str, bytes)):
        _fail("history_k_probe_invalid_response", f"{label} is invalid")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryKReadinessError(
            "history_k_probe_invalid_response",
            f"{label} is invalid",
        ) from exc
    if not math.isfinite(result):
        _fail("history_k_probe_invalid_response", f"{label} is invalid")
    if result < 0 or (positive and result <= 0):
        _fail("history_k_probe_invalid_response", f"{label} is invalid")
    return result


def _normalize_bars(raw_rows: list[dict[str, Any]], sample_date: str) -> list[dict[str, object]]:
    bars: list[dict[str, object]] = []
    previous: datetime | None = None
    for raw in raw_rows:
        time_key = str(raw.get("time_key") or "").strip()
        try:
            parsed = datetime.strptime(time_key, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise HistoryKReadinessError(
                "history_k_probe_invalid_response",
                "history K-line time_key is invalid",
            ) from exc
        if parsed.date().isoformat() != sample_date or (previous is not None and parsed <= previous):
            _fail("history_k_probe_invalid_response", "history K-line bars are unordered or outside sample date")
        bars.append(
            {
                "time_key": time_key,
                "high": _number(raw.get("high"), "high", positive=True),
                "volume": _number(raw.get("volume"), "volume", positive=False),
            }
        )
        previous = parsed
    if not bars:
        _fail("history_k_probe_empty", "history K-line sample returned no bars")
    return bars


def _sparse_bar_observed(bars: list[dict[str, object]]) -> bool:
    if len(bars) < 2:
        return True
    times = [datetime.strptime(str(item["time_key"]), "%Y-%m-%d %H:%M:%S") for item in bars]
    for previous, current in zip(times, times[1:]):
        if current - previous <= timedelta(minutes=1):
            continue
        if previous.time() == wall_time(12, 0) and current.time() in {
            wall_time(13, 0),
            wall_time(13, 1),
        }:
            continue
        return True
    return False


def _quota_observation(value: object, sample_quota_code: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail("history_k_probe_invalid_response", "history K-line quota is invalid")
    used = value.get("used_quota")
    remaining = value.get("remain_quota")
    details = value.get("detail_list")
    if (
        type(used) is not int
        or used < 0
        or type(remaining) is not int
        or remaining < 0
        or not isinstance(details, list)
    ):
        _fail("history_k_probe_invalid_response", "history K-line quota facts are invalid")
    if any(not isinstance(item, Mapping) or not str(item.get("code") or "").strip() for item in details):
        _fail("history_k_probe_invalid_response", "history K-line quota details are invalid")
    codes = sorted(
        str(item.get("code") or "").strip().upper()
        for item in details
        if isinstance(item, Mapping) and str(item.get("code") or "").strip()
    )
    if len(set(codes)) != used:
        _fail("history_k_probe_invalid_response", "history K-line quota usage is inconsistent")
    return {
        "used_quota": used,
        "remain_quota": remaining,
        "detail_record_count": len(codes),
        "distinct_security_count": len(set(codes)),
        "security_quota_ceiling": used + remaining,
        "sample_quota_code": sample_quota_code,
        "sample_quota_code_counted": sample_quota_code in codes,
        "detail_sha256": canonical_sha256(details),
    }


def _call_low_priority(
    *,
    limiter_root: Path,
    window_sec: float,
    max_calls: int,
    call: Callable[[], Any],
) -> Any:
    reserve = max_calls - min(HISTORY_K_LOW_PRIORITY_CALLS_PER_WINDOW, max(0, max_calls - 1))
    try:
        return try_low_priority_opend_call(
            base_dir=limiter_root,
            endpoint="history_kline",
            window_sec=window_sec,
            max_calls=max_calls,
            production_reserve_calls=reserve,
            call=call,
        )
    except LowPriorityOpenDCallDeferred as exc:
        raise HistoryKReadinessError(exc.reason_code, str(exc)) from exc


def _probe_provider(
    *,
    gateway: Any,
    probe_request: Mapping[str, object],
    limiter_root: Path,
    window_sec: float,
    max_calls: int,
    monotonic: Callable[[], float],
) -> dict[str, object]:
    query = dict(probe_request["sample_query"])
    raw_rows: list[dict[str, Any]] = []
    page_req_key: object = None
    page_count = 0
    started = monotonic()
    try:
        for _ in range(HISTORY_K_MAX_PAGES):
            result = _call_low_priority(
                limiter_root=limiter_root,
                window_sec=window_sec,
                max_calls=max_calls,
                call=lambda page_req_key=page_req_key: gateway.request_history_kline(
                    **query,
                    page_req_key=page_req_key,
                ),
            )
            if not isinstance(result, Mapping):
                _fail("history_k_probe_invalid_response", "history K-line page is invalid")
            raw_rows.extend(_rows(result.get("data")))
            page_count += 1
            next_key = result.get("page_req_key")
            if next_key in (None, ""):
                break
            if next_key == page_req_key:
                _fail("history_k_probe_invalid_response", "history K-line pagination did not advance")
            page_req_key = next_key
        else:
            _fail("history_k_probe_incomplete", "history K-line pagination exceeded the PoC bound")
        bars = _normalize_bars(raw_rows, str(query["start"]))
        quota = _quota_observation(
            _call_low_priority(
                limiter_root=limiter_root,
                window_sec=window_sec,
                max_calls=max_calls,
                call=gateway.get_history_kl_quota,
            ),
            str(probe_request["quota_code"]),
        )
    except HistoryKReadinessError:
        raise
    except Exception as exc:
        raise HistoryKReadinessError("history_k_probe_failed", "history K-line provider probe failed") from exc
    sparse = _sparse_bar_observed(bars)
    zero_volume_count = sum(item["volume"] == 0 for item in bars)
    no_trade_semantics_observed = sparse or zero_volume_count > 0
    counted = quota["sample_quota_code_counted"] is True
    blockers = [
        reason
        for reason, present in (
            ("sample_quota_code_not_counted", not counted),
            ("no_trade_bar_semantics_not_observed", not no_trade_semantics_observed),
        )
        if present
    ]
    return {
        "permission_status": "query_succeeded",
        "page_count": page_count,
        "row_count": len(bars),
        "first_time": bars[0]["time_key"],
        "last_time": bars[-1]["time_key"],
        "bars_sha256": canonical_sha256(bars),
        "pagination_complete": True,
        "sparse_bar_observed": sparse,
        "zero_volume_bar_count": zero_volume_count,
        "no_trade_bar_semantics_observed": no_trade_semantics_observed,
        "duration_ms": max(0, int(round((monotonic() - started) * 1000))),
        "quota": quota,
        "readiness_status": "ready" if not blockers else "blocked",
        "blockers": blockers,
    }


def _read_receipt_path(
    path: Path,
    *,
    probe_sha256: str,
    expected_opend_binding: object,
    as_of_utc: object,
) -> dict[str, object]:
    try:
        with open_private_text(path) as handle:
            content = handle.read(MAX_HISTORY_K_READINESS_BYTES + 1)
        if len(content.encode("utf-8")) > MAX_HISTORY_K_READINESS_BYTES:
            raise ValueError("receipt is too large")
        payload = json.loads(content)
        if not isinstance(payload, dict) or content != render_json_text(payload):
            raise ValueError("receipt is not canonical")
        expected_hash = canonical_sha256({key: value for key, value in payload.items() if key != "content_sha256"})
        if (
            payload.get("schema") != HISTORY_K_READINESS_SCHEMA
            or _sha256(payload.get("probe_sha256"), "probe_sha256") != probe_sha256
            or _sha256(payload.get("content_sha256"), "content_sha256") != expected_hash
            or _binding(payload.get("opend_binding")) != _binding(expected_opend_binding)
        ):
            raise ValueError("receipt identity changed")
        expires = datetime.fromisoformat(
            _timestamp(payload.get("expires_at_utc"), "expires_at_utc").replace("Z", "+00:00")
        )
        current = datetime.fromisoformat(_timestamp(as_of_utc, "as_of_utc").replace("Z", "+00:00"))
        if expires <= current:
            _fail("history_k_readiness_expired", "history-K readiness receipt expired")
    except HistoryKReadinessError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HistoryKReadinessError(
            "history_k_readiness_invalid",
            "history-K readiness receipt is unavailable or invalid",
        ) from exc
    receipt_ref = str(path.relative_to(private_path(path.parents[3]))).replace("\\", "/")
    return {
        **payload,
        "receipt_ref": receipt_ref,
        "receipt_file_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def read_history_k_readiness_receipt(
    artifact_root: str | Path,
    *,
    probe_sha256: object,
    expected_opend_binding: object,
    as_of_utc: object,
) -> dict[str, object]:
    probe_hash = _sha256(probe_sha256, "probe_sha256")
    root = private_path(artifact_root)
    candidates = sorted((root / "readiness" / "history-k").glob(f"*/{probe_hash}.json"), reverse=True)
    if not candidates:
        _fail("history_k_readiness_unavailable", "history-K readiness receipt is unavailable")
    return _read_receipt_path(
        candidates[0],
        probe_sha256=probe_hash,
        expected_opend_binding=expected_opend_binding,
        as_of_utc=as_of_utc,
    )


def refresh_history_k_readiness(
    artifact_root: str | Path,
    *,
    gateway: Any | None = None,
    gateway_factory: Callable[[], Any] | None = None,
    request: object,
    confirmed_probe_sha256: object,
    actor: object,
    occurred_at_utc: object,
    limiter_root: str | Path,
    provider_guard: Callable[[], str | None],
    window_sec: float,
    max_calls: int,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    if (gateway is None) == (gateway_factory is None):
        _fail("history_k_probe_invalid", "exactly one history-K gateway source is required")
    if (
        isinstance(window_sec, bool)
        or not isinstance(window_sec, Real)
        or not math.isfinite(float(window_sec))
        or float(window_sec) <= 0
        or type(max_calls) is not int
        or max_calls <= 0
    ):
        _fail("history_k_probe_invalid", "history-K rate limit is invalid")
    occurred_text = _timestamp(occurred_at_utc, "occurred_at_utc")
    occurred = datetime.fromisoformat(occurred_text.replace("Z", "+00:00"))
    probe_request = _normalized_probe_request(request, as_of_utc=occurred_text)
    probe_hash = canonical_sha256(probe_request)
    if _sha256(confirmed_probe_sha256, "confirmed_probe_sha256") != probe_hash:
        _fail("history_k_probe_confirmation_mismatch", "confirmed probe hash does not match current request")
    actor_text = _text(actor, "actor")
    observed_date = occurred.date().isoformat()
    ref = _receipt_ref(observed_date, probe_hash)
    target = private_path(artifact_root).joinpath(*ref.split("/"))
    lock_path = private_path(artifact_root) / "readiness" / "history-k" / ".refresh.lock"
    with exclusive_private_file_lock(lock_path):
        if target.exists():
            return _read_receipt_path(
                target,
                probe_sha256=probe_hash,
                expected_opend_binding=probe_request["opend_binding"],
                as_of_utc=occurred_text,
            )
        blocker = provider_guard()
        if blocker is not None:
            _fail(blocker, "Strategy Lab provider guard blocked history-K readiness")

        def probe_with_owned_gateway() -> dict[str, object]:
            assert gateway_factory is not None
            provider_gateway = gateway_factory()
            try:
                return _probe_provider(
                    gateway=provider_gateway,
                    probe_request=probe_request,
                    limiter_root=private_path(limiter_root),
                    window_sec=float(window_sec),
                    max_calls=max_calls,
                    monotonic=monotonic,
                )
            finally:
                provider_gateway.close()

        try:
            if gateway_factory is not None:
                observation = run_interruptible_opend_unit(
                    probe_with_owned_gateway,
                    timeout_seconds=INTERRUPTIBLE_OPEND_UNIT_TIMEOUT_SECONDS,
                )
            else:
                observation = _probe_provider(
                    gateway=gateway,
                    probe_request=probe_request,
                    limiter_root=private_path(limiter_root),
                    window_sec=float(window_sec),
                    max_calls=max_calls,
                    monotonic=monotonic,
                )
        except InterruptibleOpenDCallError as exc:
            _fail(exc.reason_code, str(exc))
        payload: dict[str, object] = {
            "schema": HISTORY_K_READINESS_SCHEMA,
            "probe_sha256": probe_hash,
            "market": probe_request["market"],
            "account": probe_request["account"],
            "opend_binding": probe_request["opend_binding"],
            "quota_code": probe_request["quota_code"],
            "sample_query": probe_request["sample_query"],
            "provider_observation": observation,
            "actor": actor_text,
            "observed_at_utc": occurred_text,
            "expires_at_utc": (occurred + HISTORY_K_READINESS_TTL)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        payload["content_sha256"] = canonical_sha256(payload)
        content = render_json_text(payload)
        if len(content.encode("utf-8")) > MAX_HISTORY_K_READINESS_BYTES:
            _fail("history_k_readiness_invalid", "history-K readiness receipt is too large")
        atomic_write_private_text(target, content)
        return _read_receipt_path(
            target,
            probe_sha256=probe_hash,
            expected_opend_binding=probe_request["opend_binding"],
            as_of_utc=occurred_text,
        )


def _blocked(reason_code: str, message: str, **facts: Any) -> dict[str, Any]:
    return {
        "status": "blocked",
        "blockers": [{"reason_code": reason_code, "message": message}],
        **facts,
    }


def _expiration_is_mature(expiration: object, cutoff_ms: int) -> bool:
    observed = expiration_observation_start_ms(str(expiration or ""), "HK")
    return observed is not None and observed < cutoff_ms


def _after_cutoff(value: object, cutoff: datetime) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed.tzinfo is None or parsed.astimezone(timezone.utc) > cutoff


def _calendar_session(calendar: Mapping[str, Any], trading_date: str) -> dict[str, Any] | None:
    sessions = calendar.get("trading_sessions")
    if not isinstance(sessions, list):
        return None
    found = [dict(item) for item in sessions if isinstance(item, Mapping) and item.get("trading_date") == trading_date]
    return found[0] if len(found) == 1 else None


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slot_breaks(slots: list[datetime]) -> list[dict[str, str]]:
    return [
        {"start_utc": _utc_text(previous + timedelta(minutes=1)), "end_utc": _utc_text(current)}
        for previous, current in zip(slots, slots[1:])
        if current - previous > timedelta(minutes=1)
    ]


def build_validation_plan(
    context: Mapping[str, Any],
    experiment: Mapping[str, Any],
    research_receipt: Mapping[str, Any],
    research_confirmation: Mapping[str, Any],
    *,
    requested_start: str,
    occurred_at_utc: str,
    schedule: Mapping[str, Any],
    account_run_config_sha256: str,
    provider_source: Mapping[str, Any],
    timer_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the exact provider-free 10-session validation plan."""

    try:
        start = date.fromisoformat(requested_start)
        occurred = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise StrategyLabRecipeError(
            "validation_plan_invalid", "validation start or evaluation time is invalid"
        ) from exc
    if start.isoformat() != requested_start or occurred.tzinfo is None:
        raise StrategyLabRecipeError("validation_plan_invalid", "validation start or evaluation time is not canonical")
    spec = experiment.get("spec")
    leader = validate_recipe_leader(experiment.get("leader"))
    conclusion = research_receipt.get("conclusion")
    if (
        not isinstance(spec, Mapping)
        or research_receipt.get("experiment_id") != experiment.get("experiment_id")
        or research_receipt.get("spec_sha256") != experiment.get("spec_sha256")
        or not isinstance(conclusion, Mapping)
        or conclusion.get("status") != "leader"
        or conclusion.get("leader") != leader
        or canonical_sha256(experiment.get("behavior_manifest")) != experiment.get("evaluator_behavior_sha256")
        or not isinstance(account_run_config_sha256, str)
        or len(account_run_config_sha256) != 64
    ):
        raise StrategyLabRecipeError("validation_plan_invalid", "research or evaluator binding changed")
    if schedule.get("timezone") != "Asia/Hong_Kong" or not bool(schedule.get("enabled", True)):
        raise StrategyLabRecipeError("validation_plan_invalid", "HK schedule is unavailable")
    try:
        calendar = read_market_calendar_binding(context["artifact_root"], market="HK")
    except Exception as exc:
        raise StrategyLabRecipeError("market_calendar_binding_unavailable", str(exc)) from exc
    trading_dates = calendar.get("trading_dates")
    if not isinstance(trading_dates, list) or requested_start not in trading_dates:
        raise StrategyLabRecipeError("validation_plan_invalid", "requested start is not a frozen trading session")
    offset = trading_dates.index(requested_start)
    selected_dates = trading_dates[offset : offset + VALIDATION_SESSIONS]
    if len(selected_dates) != VALIDATION_SESSIONS:
        raise StrategyLabRecipeError("validation_plan_invalid", "market calendar does not cover 10 validation sessions")
    sessions: list[dict[str, Any]] = []
    try:
        for trading_date in selected_dates:
            session = _calendar_session(calendar, trading_date)
            if session is None:
                raise ValueError(f"calendar session is missing: {trading_date}")
            trade_date_type = str(session["trade_date_type"])
            targets = scheduled_scan_targets_for_date(dict(schedule), trading_date, trade_date_type=trade_date_type)
            slots = scheduled_session_slots_for_date(dict(schedule), trading_date, trade_date_type=trade_date_type)
            if not targets or not slots:
                raise ValueError(f"validation session is empty: {trading_date}")
            target_values = [_utc_text(value) for value in targets]
            sessions.append(
                {
                    "trading_date": trading_date,
                    "session": session,
                    "scheduled_scan_targets_utc": target_values,
                    "expected_recommendation_point_ids": [
                        build_recommendation_point_id("HK", ACCOUNT, value) for value in target_values
                    ],
                    "minute_grid_utc": [_utc_text(value) for value in slots],
                    "breaks_utc": _slot_breaks(slots),
                    "session_endpoint_utc": _utc_text(slots[-1] + timedelta(minutes=1)),
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyLabRecipeError("validation_plan_invalid", str(exc)) from exc
    first_target = datetime.fromisoformat(sessions[0]["scheduled_scan_targets_utc"][0].replace("Z", "+00:00"))
    if occurred.astimezone(timezone.utc) >= first_target:
        raise StrategyLabRecipeError("validation_preview_blocked", "the first validation target has already begun")
    timer = dict(timer_binding)
    provider = dict(provider_source)
    provider_authority = {key: provider.get(key) for key in ("provider", "endpoint", "opend_binding")}
    if (
        timer != build_strategy_lab_timer_binding()
        or set(provider)
        != {
            "provider",
            "endpoint",
            "opend_binding",
            "source_authority_sha256",
        }
        or provider_authority["provider"] != "futu_opend"
        or provider_authority["endpoint"] != "market_snapshot"
        or provider_authority["opend_binding"] != context.get("opend_binding")
        or provider.get("source_authority_sha256") != canonical_sha256(provider_authority)
        or bool(set(account_run_config_sha256) - set("0123456789abcdef"))
    ):
        raise StrategyLabRecipeError("validation_plan_invalid", "validation runtime binding is invalid")
    schedule_copy = dict(schedule)
    return {
        "experiment_id": experiment["experiment_id"],
        "research_receipt": {
            "receipt_ref": experiment["research_receipt_ref"],
            "receipt_sha256": experiment["research_receipt_sha256"],
        },
        "research_confirmation": dict(research_confirmation),
        "leader": leader,
        "recipe": spec.get("recipe"),
        "scope": spec.get("scope"),
        "requested_start": requested_start,
        "selected_trading_dates": selected_dates,
        "market_calendar": {
            "market_calendar_version": calendar["market_calendar_version"],
            "snapshot_ref": calendar["snapshot_ref"],
            "snapshot_content_sha256": calendar["snapshot_content_sha256"],
            "snapshot_file_sha256": calendar["snapshot_file_sha256"],
            "sessions": sessions,
        },
        "schedule": {
            "config": schedule_copy,
            "schedule_config_sha256": canonical_sha256(schedule_copy),
        },
        "account_run_config_sha256": account_run_config_sha256,
        "provider_source": provider,
        "timer_binding": timer,
        "timer_binding_sha256": canonical_sha256(timer),
        "behavior_manifest": experiment["behavior_manifest"],
        "evaluator_behavior_sha256": experiment["evaluator_behavior_sha256"],
        "source_commit_sha": experiment["source_commit_sha"],
        "hidden_snapshot_batch_ceiling": HIDDEN_SNAPSHOT_BATCH_CEILING,
        "validation_wake_tolerance_seconds": VALIDATION_WAKE_TOLERANCE_SECONDS,
        "tick_protection_seconds": TICK_PROTECTION_SECONDS,
    }


def _load_window_day(
    context: Mapping[str, Any],
    trading_date: str,
    cutoff: datetime,
    cutoff_ms: int,
    calendar: Mapping[str, Any],
    *,
    require_mature_outcomes: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    runtime_root = context["runtime_root"]
    try:
        expectation = load_formal_expectation(
            runtime_root,
            market="HK",
            account=ACCOUNT,
            trading_date=trading_date,
        )
    except FormalCorpusError as exc:
        return None, exc.reason_code
    if expectation.get("status") != "available":
        return None, str(expectation.get("reason_code") or "formal_expectation_missing")
    expectation_payload = expectation["expectation"]
    if _after_cutoff(expectation_payload.get("sealed_at_utc"), cutoff):
        return None, "research_point_post_cutoff"
    try:
        bound_calendar = read_expectation_bound_market_calendar_snapshot(
            context["artifact_root"],
            market="HK",
            market_calendar_version=expectation_payload["market_calendar_version"],
            market_calendar_sha256=expectation_payload["market_calendar_sha256"],
        )
    except (FormalCorpusError, KeyError) as exc:
        return None, str(getattr(exc, "reason_code", "market_calendar_binding_unavailable"))
    bound_session = _calendar_session(bound_calendar, trading_date)
    current_session = _calendar_session(calendar, trading_date)
    if bound_session is None or current_session is None:
        return None, "market_calendar_binding_changed"
    if bound_session["trade_date_type"] != current_session["trade_date_type"]:
        return None, "market_calendar_session_changed"
    targets = expectation_payload["scheduled_scan_targets_market"]
    expected = expectation_payload["expected_recommendation_point_ids"]
    points: list[dict[str, Any]] = []
    for target, point_id in zip(targets, expected, strict=True):
        if _after_cutoff(target, cutoff):
            return None, "research_point_post_cutoff"
        try:
            loaded = load_formal_point(
                runtime_root,
                market="HK",
                account=ACCOUNT,
                trading_date=trading_date,
                recommendation_point_id=point_id,
            )
        except FormalCorpusError as exc:
            return None, exc.reason_code
        if loaded.get("status") != "available":
            return None, str(loaded.get("reason_code") or "formal_point_evidence_missing")
        point = loaded["point"]
        recommendation = point["recommendation_point"]
        opening = point["opening_snapshot"]
        coherence = recommendation["formal_point_time_coherence"]
        authoritative_times = (
            point["captured_at_utc"],
            point["source_binding"]["scheduled_scan_target_market"],
            recommendation["scheduled_scan_target_market"],
            recommendation["decision_at_utc"],
            opening["sealed_at_utc"],
            coherence["maximum_observed_at_utc"],
        )
        if any(_after_cutoff(value, cutoff) for value in authoritative_times):
            return None, "research_point_post_cutoff"
        try:
            arms = build_concentration_arms(
                point,
                {
                    "formal_point_ref": loaded["artifact_ref"],
                    "formal_point_file_sha256": loaded["artifact_file_sha256"],
                },
            )
        except StrategyLabRecipeError as exc:
            return None, exc.reason_code
        if require_mature_outcomes and any(
            not _expiration_is_mature(arm["candidate"].get("expiration"), cutoff_ms) for arm in arms["arms"]
        ):
            return None, "research_outcome_immature"
        points.append(arms)
    return (
        {
            "trading_date": trading_date,
            "expectation_ref": expectation["artifact_ref"],
            "expectation_content_sha256": expectation["artifact_content_sha256"],
            "expectation_file_sha256": expectation["artifact_file_sha256"],
            "market_calendar_binding": {
                "market_calendar_version": bound_calendar["market_calendar_version"],
                "snapshot_ref": bound_calendar["snapshot_ref"],
                "snapshot_content_sha256": bound_calendar["snapshot_content_sha256"],
                "snapshot_file_sha256": bound_calendar["snapshot_file_sha256"],
                "session": bound_session,
            },
            "points": points,
        },
        None,
    )


def select_research_window(
    context: Mapping[str, Any],
    maturity_cutoff_utc: str,
) -> dict[str, Any]:
    try:
        cutoff = datetime.fromisoformat(maturity_cutoff_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
        calendar = read_market_calendar_binding(context["artifact_root"], market="HK")
    except Exception as exc:
        return _blocked("market_calendar_binding_unavailable", str(exc), sessions=[])
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cutoff_date = cutoff.astimezone(_HK_TZ).date().isoformat()
    dates = [value for value in calendar["trading_dates"] if value <= cutoff_date]
    if len(dates) < RESEARCH_SESSIONS:
        return _blocked("research_corpus_warming", "fewer than 20 trading sessions", sessions=[])
    skipped_newer_suffix = 0
    skipped_suffix_reason = "research_outcome_immature"
    for end in range(len(dates) - 1, RESEARCH_SESSIONS - 2, -1):
        selected_dates = dates[end - RESEARCH_SESSIONS + 1 : end + 1]
        sessions: list[dict[str, Any]] = []
        invalid: list[tuple[int, str]] = []
        for index, trading_date in enumerate(selected_dates):
            day, reason = _load_window_day(
                context,
                trading_date,
                cutoff,
                cutoff_ms,
                calendar,
            )
            if day is not None:
                sessions.append(day)
            else:
                invalid.append((index, reason or "research_window_coverage_missing"))
        if invalid:
            first_invalid = invalid[0][0]
            if [index for index, _reason in invalid] == list(range(first_invalid, RESEARCH_SESSIONS)) and all(
                reason in {"research_outcome_immature", "research_point_post_cutoff"} for _index, reason in invalid
            ):
                skipped_newer_suffix += 1
                if any(reason == "research_point_post_cutoff" for _index, reason in invalid):
                    skipped_suffix_reason = "research_point_post_cutoff"
                continue
            index, reason = invalid[0]
            return _blocked(
                reason,
                f"research session is incomplete: {selected_dates[index]}",
                sessions=sessions,
                selected_trading_dates=selected_dates,
            )
        return {
            "status": "available",
            "blockers": [],
            "selected_trading_dates": selected_dates,
            "ignored_immature_window_count": skipped_newer_suffix,
            "market_calendar": {
                "market_calendar_version": calendar["market_calendar_version"],
                "snapshot_ref": calendar["snapshot_ref"],
                "snapshot_content_sha256": calendar["snapshot_content_sha256"],
                "snapshot_file_sha256": calendar["snapshot_file_sha256"],
            },
            "sessions": sessions,
        }
    return _blocked(
        skipped_suffix_reason,
        "no mature 20-session research window is available",
        sessions=[],
    )


def select_engineering_canary_window(
    context: Mapping[str, Any],
    observed_at_utc: str,
) -> dict[str, Any]:
    try:
        cutoff = datetime.fromisoformat(observed_at_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
        calendar = read_market_calendar_binding(context["artifact_root"], market="HK")
    except Exception as exc:
        return _blocked("market_calendar_binding_unavailable", str(exc), sessions=[])
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cutoff_date = cutoff.astimezone(_HK_TZ).date().isoformat()
    if not calendar["coverage_start"] <= cutoff_date <= calendar["coverage_end"]:
        return _blocked(
            "market_calendar_binding_unavailable",
            "HK observation date is outside calendar coverage",
            sessions=[],
        )
    dates = [value for value in calendar["trading_dates"] if value <= cutoff_date]
    if len(dates) < 2:
        return _blocked("research_corpus_warming", "fewer than 2 trading sessions", sessions=[])
    selected_dates = dates[-2:]
    sessions: list[dict[str, Any]] = []
    for trading_date in selected_dates:
        day, reason = _load_window_day(
            context,
            trading_date,
            cutoff,
            cutoff_ms,
            calendar,
            require_mature_outcomes=False,
        )
        if day is None:
            return _blocked(
                reason or "research_window_coverage_missing",
                f"engineering canary session is incomplete: {trading_date}",
                sessions=sessions,
                selected_trading_dates=selected_dates,
            )
        sessions.append(day)
    return {
        "status": "available",
        "blockers": [],
        "selected_trading_dates": selected_dates,
        "sessions": sessions,
    }


def _all_arms(window: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        arm
        for session in window.get("sessions", [])
        for point in session.get("points", [])
        for arm in point.get("arms", [])
    ]


def _terminal_fx_bindings(
    context: Mapping[str, Any],
    arms: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    bundle = PerformanceEvidenceSQLiteRepository(context["ledger_path"]).read_all()
    if bundle.schema_state != "initialized_v1":
        return [], [{"reason_code": "terminal_fx_unavailable", "message": bundle.schema_state}]
    bindings: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    identities = sorted({(str(arm["candidate"]["expiration"]), str(arm["candidate"]["currency"])) for arm in arms})
    for expiration, currency in identities:
        observation_ms = expiration_observation_start_ms(expiration, "HK")
        if observation_ms is None:
            blockers.append({"reason_code": "terminal_fx_unavailable", "message": f"invalid expiry: {expiration}"})
            continue
        selection = select_fx_rate(
            bundle.fx_rates,
            base_currency=currency,
            quote_currency="CNY",
            at_ms=observation_ms,
        )
        if selection.status != "selected" or selection.fact is None:
            blockers.append(
                {
                    "reason_code": "terminal_fx_unavailable",
                    "message": f"{currency} FX is {selection.status} at {expiration}",
                }
            )
            continue
        fact = selection.fact
        payload = fact.normalized_payload(include_fact_id=True)
        bindings.append(
            {
                "expiration": expiration,
                "currency": currency,
                "observation_start_ms": observation_ms,
                "fact_ref": {"kind": "fx_rate", "fact_id": fact.fact_id},
                "fact_sha256": canonical_sha256(payload),
                "fact": payload,
            }
        )
    return bindings, blockers


def resolve_terminal_fx_binding(
    context: Mapping[str, Any], *, expiration: str, currency: str
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Resolve one expiry-date FX fact from the existing evidence owner."""

    bindings, blockers = _terminal_fx_bindings(
        context,
        [{"candidate": {"expiration": expiration, "currency": currency}}],
    )
    return (bindings[0], None) if bindings else (None, blockers[0])


def _history_k_authority(
    context: Mapping[str, Any],
    arms: list[dict[str, Any]],
    *,
    maturity_cutoff_utc: str,
    occurred_at_utc: str,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    tuples: set[tuple[str, str, str]] = set()
    for arm in arms:
        candidate = arm["candidate"]
        contract = str(candidate.get("contract_symbol") or "").upper()
        identity = resolve_symbol_identity(contract)
        sample_date = str(candidate.get("sample_trading_date") or arm.get("trading_date") or "")
        if identity is None or identity.market != "HK" or not sample_date:
            return None, [{"reason_code": "history_k_projection_incomplete", "message": contract or "missing contract"}]
        tuples.add((identity.futu_code, contract, sample_date))
    ordered = sorted(tuples)
    if not ordered:
        return None, [{"reason_code": "history_k_projection_incomplete", "message": "no arms"}]
    representative = ordered[0]
    try:
        probe_request = build_history_k_probe_request(
            market="HK",
            account=ACCOUNT,
            opend_binding=context["opend_binding"],
            contract_symbol=representative[1],
            underlier_code=representative[0],
            sample_date=representative[2],
            as_of_utc=maturity_cutoff_utc,
        )
    except HistoryKReadinessError as exc:
        return None, [{"reason_code": exc.reason_code, "message": str(exc)}]
    probe_sha256 = canonical_sha256(probe_request)
    authority: dict[str, Any] = {
        "queries": [
            {"security_quota_identity": item[0], "contract_symbol": item[1], "sample_date": item[2]} for item in ordered
        ],
        "representative": {
            "security_quota_identity": representative[0],
            "contract_symbol": representative[1],
            "sample_date": representative[2],
        },
        "probe_request": probe_request,
        "probe_sha256": probe_sha256,
        "required_unique_security_identity_count": len({item[0] for item in ordered}),
        "quota_rule": "required_unique_security_identity_count <= provider_observation.quota.remain_quota",
    }
    try:
        receipt = read_history_k_readiness_receipt(
            context["artifact_root"],
            probe_sha256=probe_sha256,
            expected_opend_binding=context["opend_binding"],
            as_of_utc=occurred_at_utc,
        )
    except HistoryKReadinessError as exc:
        return authority, [{"reason_code": exc.reason_code, "message": str(exc)}]
    observation = receipt.get("provider_observation")
    quota = observation.get("quota") if isinstance(observation, Mapping) else None
    remaining = quota.get("remain_quota") if isinstance(quota, Mapping) else None
    ready = (
        isinstance(observation, Mapping)
        and observation.get("readiness_status") == "ready"
        and observation.get("pagination_complete") is True
        and observation.get("no_trade_bar_semantics_observed") is True
        and isinstance(quota, Mapping)
        and quota.get("sample_quota_code_counted") is True
        and quota.get("sample_quota_code") == representative[0]
        and type(remaining) is int
        and authority["required_unique_security_identity_count"] <= remaining
    )
    authority["receipt"] = {
        "receipt_ref": receipt.get("receipt_ref"),
        "content_sha256": receipt.get("content_sha256"),
        "receipt_file_sha256": receipt.get("receipt_file_sha256"),
        "observed_at_utc": receipt.get("observed_at_utc"),
        "expires_at_utc": receipt.get("expires_at_utc"),
        "observed_remaining_quota": remaining,
    }
    if not ready:
        return authority, [
            {"reason_code": "history_k_readiness_insufficient", "message": "targeted readiness proof is incomplete"}
        ]
    return authority, []


def check_recipe_readiness(
    context: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    occurred_at_utc: str,
) -> dict[str, Any]:
    window = select_research_window(context, str(request["maturity_cutoff_utc"]))
    blockers = list(window.get("blockers", []))
    try:
        fee_plan = load_account_fee_plan_receipt(Path(str(request["fee_plan_receipt_path"])))
        if (fee_plan["market"], fee_plan["account"]) != ("HK", ACCOUNT):
            raise ValueError("fee-plan identity changed")
    except Exception as exc:
        fee_plan = None
        blockers.append({"reason_code": "account_fee_plan_unavailable", "message": str(exc)})
    arms = _all_arms(window)
    for session in window.get("sessions", []):
        for point in session.get("points", []):
            for arm in point.get("arms", []):
                arm["trading_date"] = session["trading_date"]
                arm["candidate"]["sample_trading_date"] = session["trading_date"]
    terminal_fx, fx_blockers = _terminal_fx_bindings(context, arms) if arms else ([], [])
    blockers.extend(fx_blockers)
    history_k, history_blockers = (
        _history_k_authority(
            context,
            arms,
            maturity_cutoff_utc=str(request["maturity_cutoff_utc"]),
            occurred_at_utc=occurred_at_utc,
        )
        if arms
        else (None, [])
    )
    blockers.extend(history_blockers)
    return {
        "status": "available" if not blockers else "blocked",
        "blockers": blockers,
        "window": window,
        "fee_plan": fee_plan,
        "terminal_fx_bindings": terminal_fx,
        "history_k_authority": history_k,
    }


__all__ = [
    "HISTORY_K_LOW_PRIORITY_CALLS_PER_WINDOW",
    "HISTORY_K_MAX_PAGES",
    "HISTORY_K_POC_NOT_BEFORE_HK",
    "HISTORY_K_READINESS_SCHEMA",
    "HISTORY_K_READINESS_TTL",
    "HistoryKReadinessError",
    "build_history_k_probe_request",
    "build_validation_plan",
    "check_recipe_readiness",
    "preview_history_k_readiness",
    "read_history_k_readiness_receipt",
    "refresh_history_k_readiness",
    "resolve_terminal_fx_binding",
    "select_engineering_canary_window",
    "select_research_window",
]
