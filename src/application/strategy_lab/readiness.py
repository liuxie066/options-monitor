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
from domain.domain.symbol_identity import OPTION_CODE_RE, resolve_symbol_identity
from src.application.account_config import normalize_account_label
from src.application.candidate_snapshot_contract import utc_timestamp
from src.application.opend_call_coordinator import (
    LowPriorityOpenDCallDeferred,
    try_low_priority_opend_call,
)
from src.application.shadow_replay.common import render_json_text
from src.application.tick_cron import tick_cron_is_busy
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
    if type(used) is not int or used < 0 or type(remaining) is not int or remaining < 0 or not isinstance(details, list):
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
        expires = datetime.fromisoformat(_timestamp(payload.get("expires_at_utc"), "expires_at_utc").replace("Z", "+00:00"))
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
    tick_lock_path: str | Path,
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
        local = occurred.astimezone(_HK_TZ)
        if local.weekday() < 5 and local.time() < HISTORY_K_POC_NOT_BEFORE_HK:
            _fail("tick_protection_window", "history-K PoC is allowed only after the HK Tick window")
        if tick_cron_is_busy(tick_lock_path):
            _fail("tick_busy", "HK Tick is running")
        provider_gateway = gateway
        owns_gateway = False
        try:
            if gateway_factory is not None:
                provider_gateway = gateway_factory()
                owns_gateway = True
            observation = _probe_provider(
                gateway=provider_gateway,
                probe_request=probe_request,
                limiter_root=private_path(limiter_root),
                window_sec=float(window_sec),
                max_calls=max_calls,
                monotonic=monotonic,
            )
        finally:
            if owns_gateway and provider_gateway is not None:
                provider_gateway.close()
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
            "expires_at_utc": (occurred + HISTORY_K_READINESS_TTL).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
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


__all__ = [
    "HISTORY_K_LOW_PRIORITY_CALLS_PER_WINDOW",
    "HISTORY_K_MAX_PAGES",
    "HISTORY_K_POC_NOT_BEFORE_HK",
    "HISTORY_K_READINESS_SCHEMA",
    "HISTORY_K_READINESS_TTL",
    "HistoryKReadinessError",
    "build_history_k_probe_request",
    "preview_history_k_readiness",
    "read_history_k_readiness_receipt",
    "refresh_history_k_readiness",
]
