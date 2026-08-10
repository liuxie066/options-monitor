from __future__ import annotations

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from domain.domain.daily_decision_brief import (
    DAILY_DECISION_BRIEF_DIFF_SCHEMA_VERSION,
    build_daily_brief_candidate_identity,
    daily_brief_compatible_digests,
    daily_brief_digest,
    diff_daily_decision_briefs,
    normalize_daily_decision_brief,
    reconcile_daily_decision_brief_evidence,
)
from domain.domain.combo_candidate_evidence import (
    combo_exposure_render_context,
    derive_combo_candidate_exposures,
)
from domain.storage import paths
from domain.storage.json_io import atomic_write_json, atomic_write_text
from src.application.channels.feishu_notification_renderer import (
    feishu_notification_envelope_sha256,
    normalize_feishu_notification_envelope,
)


CURRENT_INDEX_SCHEMA_VERSION = "daily_decision_brief_current_index.v1"
DELIVERY_POINTER_SCHEMA_VERSION = "daily_decision_brief_delivery.v1"
LEGACY_DELIVERY_POINTER_SCHEMA_VERSION = DELIVERY_POINTER_SCHEMA_VERSION
DELIVERY_STATE_SCHEMA_VERSION = "daily_decision_brief_delivery.v2"

_MARKET_RE = re.compile(r"^[A-Z0-9_-]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REVISION_RE = re.compile(r"\.r(?P<revision>\d{4})\.json$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_IDENTITY_RE = re.compile(
    r"^candidate:v1:(?P<account>[^:]+):(?P<market>US|HK|CN):(?P<symbol>[^:]+):"
    r"(?P<family>sell_put|covered_call|combo_yield)$"
)
_MISSING = object()


class DailyDecisionBriefStateError(RuntimeError):
    """Raised when persisted Daily Decision Brief state is unsafe to infer from."""


def persist_daily_decision_brief_success(
    *,
    base: Path,
    brief: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one reliable successful brief without making a delivery decision."""

    base_path = Path(base).resolve()
    source = dict(brief or {})
    market = _normalize_market(source.get("market"))
    account = _normalize_account(source.get("account"))
    market_date = _normalize_market_date(source.get("market_trading_date"))
    run_id = str(source.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id is required for daily brief persistence")

    state_dir = paths.account_state_dir(base_path, account)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"daily_decision_brief.{market}.lock"
    with _exclusive_lock(lock_path):
        current_path = _current_path(base_path, account, market)
        current_raw = _read_json_strict(current_path)
        previous = None
        if current_raw is not _MISSING:
            current = _normalize_persisted_brief(
                current_raw,
                path=current_path,
                account=account,
                market=market,
            )
            _validate_current_revision(base=base_path, current=current, current_path=current_path)
            if current.get("status") in {"ready", "degraded"}:
                previous = current

        revisions = _list_revision_numbers(
            base=base_path,
            account=account,
            market=market,
            market_trading_date=market_date,
        )
        revision = revisions[-1] + 1 if revisions else 0
        candidate = dict(source)
        candidate.update(
            {
                "market": market,
                "market_trading_date": market_date,
                "account": account,
                "revision": revision,
                "run_id": run_id,
            }
        )
        if (
            previous is not None
            and previous.get("market_trading_date") == market_date
        ):
            candidate = reconcile_daily_decision_brief_evidence(
                previous,
                candidate,
            )
        normalized = normalize_daily_decision_brief(candidate)
        if normalized.get("status") not in {"ready", "degraded"}:
            raise ValueError("only ready or degraded daily briefs may advance successful current")
        if normalized.get("actionability") == "blocked":
            raise ValueError("blocked daily brief may not advance successful current")

        revision_path = _revision_path(base_path, account, market, market_date, revision)
        if revision_path.exists():
            raise DailyDecisionBriefStateError(f"daily brief revision already exists: {revision_path}")
        run_brief_path = (
            paths.run_account_state_dir(base_path, run_id, account)
            / f"daily_decision_brief.{market}.json"
        )
        shared_index_path = (
            paths.shared_state_dir(base_path)
            / "current"
            / "daily_decision_briefs.current.json"
        )
        shared_lock_path = (
            paths.shared_state_dir(base_path)
            / "current"
            / "daily_decision_briefs.current.lock"
        )
        brief_digest = daily_brief_digest(normalized)
        with _exclusive_lock(shared_lock_path):
            shared_index = _load_current_index(shared_index_path)
            shared_index["items"][f"{market}/{account}"] = {
                "market": market,
                "market_trading_date": market_date,
                "account": account,
                "revision": revision,
                "run_id": run_id,
                "brief_digest": brief_digest,
                "path": _relative_path(base_path, current_path),
            }
            shared_index["updated_at_utc"] = normalized.get("generated_at_utc") or _utc_now_iso()
            atomic_write_json(revision_path, normalized)
            atomic_write_json(current_path, normalized)
            atomic_write_json(run_brief_path, normalized)
            atomic_write_json(shared_index_path, shared_index)

        previous_ids = (
            _candidate_identity_set(previous)
            if previous is not None and previous.get("market_trading_date") == market_date
            else set()
        )
        current_ids = _candidate_identity_set(normalized)
        return {
            "brief": normalized,
            "previous_successful_brief": previous,
            "previous_successful_revision": (int(previous["revision"]) if previous else None),
            "previous_successful_brief_digest": (daily_brief_digest(previous) if previous else None),
            "previous_candidate_identities": sorted(previous_ids),
            "current_candidate_identities": sorted(current_ids),
            "newly_detected_candidate_identities": sorted(current_ids - previous_ids),
            "current_revision": revision,
            "current_brief_digest": brief_digest,
            "paths": {
                "revision": revision_path,
                "current": current_path,
                "run_brief": run_brief_path,
                "shared_index": shared_index_path,
            },
        }


def record_daily_decision_brief_candidates(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    revision: int,
    brief_digest: str,
    candidate_identities: list[str] | tuple[str, ...],
    observed_at_utc: datetime | str | None = None,
) -> dict[str, Any]:
    """Replace the current day's pending set from one successful snapshot."""

    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    date_norm = _normalize_market_date(market_trading_date)
    revision_norm = _normalize_revision(revision)
    digest_norm = _normalize_sha256(brief_digest, field="brief_digest")
    identities = _normalize_candidate_identities(
        candidate_identities,
        account=account_norm,
        market=market_norm,
    )
    _validate_successful_revision_source(
        base=base_path,
        account=account_norm,
        market=market_norm,
        market_trading_date=date_norm,
        revision=revision_norm,
        source_digest=digest_norm,
    )
    observed_at = _coerce_utc_iso(observed_at_utc)

    state_dir = paths.account_state_dir(base_path, account_norm)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"daily_decision_brief.{market_norm}.lock"
    delivery_path = _delivery_path(base_path, account_norm, market_norm)
    with _exclusive_lock(lock_path):
        state = _load_or_create_delivery_state(
            base=base_path,
            path=delivery_path,
            account=account_norm,
            market=market_norm,
        )
        day = _delivery_day(state, date_norm)
        alerted = set(day["alerted_candidates"])
        existing_pending = day["pending_candidates"]
        day["pending_candidates"] = {
            identity: dict(
                existing_pending.get(identity)
                or {
                    "first_seen_revision": revision_norm,
                    "first_seen_at_utc": observed_at,
                }
            )
            for identity in identities
            if identity not in alerted
        }
        atomic_write_json(delivery_path, state)
        return {
            "state": state,
            "market_trading_date": date_norm,
            "pending_candidate_identities": sorted(day["pending_candidates"]),
            "alerted_candidate_identities": sorted(alerted),
            "path": delivery_path,
        }


def prepare_daily_decision_brief_delivery(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    run_id: str,
    delivery_kind: str,
    source_kind: str,
    source_digest: str,
    rendered_message: str,
    revision: int | None = None,
    scheduled_target_market: str | None = None,
    candidate_identities: list[str] | tuple[str, ...] = (),
    source_reference: str | None = None,
    render_context: Mapping[str, Any] | None = None,
    rendered_transport: Mapping[str, Any] | None = None,
    prepared_at_utc: datetime | str | None = None,
) -> dict[str, Any]:
    """Persist an exact v2 delivery envelope and its run-scoped audit copy."""

    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    date_norm = _normalize_market_date(market_trading_date)
    run_id_norm = _normalize_run_id(run_id)
    kind_norm = str(delivery_kind or "").strip().lower()
    if kind_norm not in {"fixed_report", "fixed_failure", "candidate_alert"}:
        raise ValueError("delivery_kind must be fixed_report, fixed_failure, or candidate_alert")
    source_kind_norm = str(source_kind or "").strip().lower()
    if source_kind_norm not in {"successful_brief", "scan_failure"}:
        raise ValueError("source_kind must be successful_brief or scan_failure")
    if (kind_norm == "fixed_failure") != (source_kind_norm == "scan_failure"):
        raise ValueError("fixed_failure must use scan_failure and other deliveries must use successful_brief")
    digest_norm = _normalize_sha256(source_digest, field="source_digest")
    message = str(rendered_message or "")
    if not message.strip():
        raise ValueError("rendered_message is required")
    message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
    rendered_transport_norm = (
        normalize_feishu_notification_envelope(
            rendered_transport,
            expected_text=message,
        )
        if rendered_transport is not None
        else None
    )
    if kind_norm == "fixed_failure" and rendered_transport_norm is not None:
        raise ValueError("fixed_failure must remain a plain-text notification")
    rendered_transport_sha256 = (
        feishu_notification_envelope_sha256(rendered_transport_norm)
        if rendered_transport_norm is not None
        else None
    )
    prepared_at = _coerce_utc_iso(prepared_at_utc)
    context = dict(render_context or {})
    identities = _normalize_candidate_identities(
        candidate_identities,
        account=account_norm,
        market=market_norm,
    )

    revision_norm: int | None
    source_reference_norm = str(source_reference or "").strip()
    source_brief: dict[str, Any] | None = None
    if source_kind_norm == "successful_brief":
        if revision is None:
            raise ValueError("revision is required for successful_brief delivery")
        revision_norm = _normalize_revision(revision)
        source_brief = _validate_successful_revision_source(
            base=base_path,
            account=account_norm,
            market=market_norm,
            market_trading_date=date_norm,
            revision=revision_norm,
            source_digest=digest_norm,
        )
        source_reference_norm = ""
    else:
        if revision is not None:
            raise ValueError("scan_failure delivery must not reference a revision")
        if not source_reference_norm:
            raise ValueError("source_reference is required for scan_failure delivery")
        source_reference_norm = _validate_failure_source_reference(
            base=base_path,
            source_reference=source_reference_norm,
            source_digest=digest_norm,
        )
        revision_norm = None
    if source_brief is not None and not set(identities).issubset(_candidate_identity_set(source_brief)):
        raise ValueError("candidate_identities must come from the referenced successful brief")
    if source_brief is not None:
        _validate_optional_combo_render_context(
            context,
            brief=source_brief,
            candidate_identities=identities,
        )
    if kind_norm == "fixed_failure" and identities:
        raise ValueError("fixed_failure must not include candidate_identities")

    target_norm: str | None = None
    if kind_norm.startswith("fixed_"):
        target_norm = _normalize_scheduled_target(scheduled_target_market, market_date=date_norm)
        delivery_key = _fixed_delivery_key(
            market=market_norm,
            market_date=date_norm,
            account=account_norm,
            scheduled_target_market=target_norm,
        )
    else:
        if scheduled_target_market not in (None, ""):
            raise ValueError("candidate_alert must not include scheduled_target_market")
        if not identities:
            raise ValueError("candidate_alert requires candidate_identities")
        delivery_key = _candidate_delivery_key(
            market=market_norm,
            market_date=date_norm,
            account=account_norm,
            candidate_identities=identities,
        )

    envelope = {
        "status": "pending",
        "delivery_kind": kind_norm,
        "source_kind": source_kind_norm,
        "revision": revision_norm,
        "source_digest": digest_norm,
        "source_reference": source_reference_norm or None,
        "delivery_key": delivery_key,
        "rendered_message": message,
        "message_sha256": message_sha256,
        "rendered_transport": rendered_transport_norm,
        "rendered_transport_sha256": rendered_transport_sha256,
        "candidate_identities": identities,
        "scheduled_target_market": target_norm,
        "render_context": context,
        "first_prepared_at_utc": prepared_at,
        "last_attempt_at_utc": None,
        "confirmed_at_utc": None,
    }

    state_dir = paths.account_state_dir(base_path, account_norm)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"daily_decision_brief.{market_norm}.lock"
    delivery_path = _delivery_path(base_path, account_norm, market_norm)
    plan_path = (
        paths.run_account_state_dir(base_path, run_id_norm, account_norm)
        / f"daily_decision_brief_delivery_plan.{market_norm}.json"
    )
    with _exclusive_lock(lock_path):
        state = _load_or_create_delivery_state(
            base=base_path,
            path=delivery_path,
            account=account_norm,
            market=market_norm,
        )
        day = _delivery_day(state, date_norm)
        if kind_norm.startswith("fixed_"):
            existing = day["fixed_reports"].get(target_norm)
            persisted, prepared = _resolve_prepared_envelope(
                existing=existing,
                candidate=envelope,
                allow_pending_upgrade=(
                    isinstance(existing, Mapping)
                    and existing.get("delivery_kind") == "fixed_failure"
                    and kind_norm == "fixed_report"
                ),
                preserve_attempt_metadata=True,
            )
            day["fixed_reports"][target_norm] = persisted
        else:
            existing = day.get("candidate_delivery")
            if not set(identities).issubset(day["pending_candidates"]):
                raise DailyDecisionBriefStateError(
                    "candidate delivery identities must be pending for the market date"
                )
            if (
                isinstance(existing, Mapping)
                and existing.get("status") == "confirmed"
                and existing.get("delivery_key") != envelope["delivery_key"]
            ):
                history = day["candidate_delivery_history"]
                if not any(
                    item.get("delivery_key") == existing.get("delivery_key")
                    for item in history
                ):
                    history.append(dict(existing))
                existing = None
            persisted, prepared = _resolve_prepared_envelope(
                existing=existing,
                candidate=envelope,
                allow_pending_upgrade=(
                    isinstance(existing, Mapping)
                    and existing.get("delivery_key") != envelope["delivery_key"]
                ),
                preserve_attempt_metadata=False,
            )
            day["candidate_delivery"] = persisted
        atomic_write_json(delivery_path, state)
        plan = {
            "schema_version": "daily_decision_brief_delivery_plan.v1",
            "run_id": run_id_norm,
            "account": account_norm,
            "market": market_norm,
            "market_trading_date": date_norm,
            "envelope": persisted,
        }
        atomic_write_json(plan_path, plan)
        return {
            "prepared": prepared,
            "reason": "prepared" if prepared else "already_prepared",
            "envelope": persisted,
            "state": state,
            "paths": {"delivery": delivery_path, "run_plan": plan_path},
        }


def read_daily_decision_brief_delivery_state(
    *,
    base: Path,
    account: str,
    market: str,
) -> dict[str, Any]:
    """Read and validate only v2 delivery state without writing anything."""

    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    path = _delivery_path(base_path, account_norm, market_norm)
    raw = _read_json_strict(path)
    if raw is _MISSING:
        return {"available": False, "reason": "not_found", "state": None, "path": path}
    try:
        state = _normalize_delivery_state(
            raw,
            base=base_path,
            path=path,
            account=account_norm,
            market=market_norm,
        )
    except DailyDecisionBriefStateError as exc:
        return {"available": False, "reason": "state_invalid", "error": str(exc), "state": None, "path": path}
    return {"available": True, "reason": "ok", "state": state, "path": path}


def read_retryable_daily_decision_brief_delivery(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
) -> dict[str, Any]:
    """Return the next exact pending/ambiguous envelope without mutation."""

    date_norm = _normalize_market_date(market_trading_date)
    result = read_daily_decision_brief_delivery_state(base=base, account=account, market=market)
    if not result.get("available"):
        return {**result, "envelope": None}
    day = result["state"]["days"].get(date_norm)
    if not day:
        return {**result, "reason": "no_delivery_day", "envelope": None}
    fixed = [day["fixed_reports"][key] for key in sorted(day["fixed_reports"])]
    candidate = day.get("candidate_delivery")
    envelopes = fixed + ([candidate] if isinstance(candidate, Mapping) else [])
    ambiguous = [item for item in envelopes if item.get("status") == "ambiguous"]
    if ambiguous:
        return {**result, "reason": "ambiguous", "envelope": ambiguous[0]}
    pending_fixed = [item for item in fixed if item.get("status") == "pending"]
    if pending_fixed:
        return {**result, "reason": "pending_fixed", "envelope": pending_fixed[0]}
    if (
        isinstance(candidate, Mapping)
        and candidate.get("status") == "pending"
        and set(candidate.get("candidate_identities") or []).issubset(day["pending_candidates"])
    ):
        return {**result, "reason": "pending_candidates", "envelope": candidate}
    if isinstance(candidate, Mapping) and candidate.get("status") == "pending":
        return {**result, "reason": "stale_candidate_envelope", "envelope": None}
    return {**result, "reason": "none", "envelope": None}


def record_daily_decision_brief_delivery_attempt(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    delivery_key: str,
    source_digest: str,
    message_sha256: str,
    transport_idempotency_key: str,
    ambiguous: bool,
    attempted_at_utc: datetime | str | None = None,
) -> dict[str, Any]:
    """Record one exact provider attempt without rotating its envelope."""

    attempted_at = _coerce_utc_iso(attempted_at_utc)
    with _locked_delivery_envelope(
        base=base,
        account=account,
        market=market,
        market_trading_date=market_trading_date,
        delivery_key=delivery_key,
        source_digest=source_digest,
        message_sha256=message_sha256,
        transport_idempotency_key=transport_idempotency_key,
    ) as (state, day, envelope, delivery_path):
        if envelope["status"] in {"confirmed", "expired_unconfirmed"}:
            return {
                "updated": False,
                "reason": "already_final",
                "envelope": dict(envelope),
                "path": delivery_path,
            }
        envelope["last_attempt_at_utc"] = attempted_at
        if ambiguous:
            envelope["status"] = "ambiguous"
        atomic_write_json(delivery_path, state)
        return {
            "updated": True,
            "reason": "ambiguous" if ambiguous else "definite_failure",
            "envelope": dict(envelope),
            "path": delivery_path,
        }


def validate_daily_decision_brief_delivery_identity(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    delivery_key: str,
    source_digest: str,
    message_sha256: str,
    transport_idempotency_key: str,
) -> dict[str, Any]:
    """Validate and return the exact persisted envelope without mutating it."""

    with _locked_delivery_envelope(
        base=base,
        account=account,
        market=market,
        market_trading_date=market_trading_date,
        delivery_key=delivery_key,
        source_digest=source_digest,
        message_sha256=message_sha256,
        transport_idempotency_key=transport_idempotency_key,
    ) as (_state, _day, envelope, delivery_path):
        return {
            "available": True,
            "delivery_kind": str(envelope["delivery_kind"]),
            "status": str(envelope["status"]),
            "envelope": dict(envelope),
            "path": delivery_path,
        }


def confirm_daily_decision_brief_delivery_v2(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    delivery_key: str,
    source_digest: str,
    message_sha256: str,
    transport_idempotency_key: str,
    confirmed_at_utc: datetime | str | None = None,
) -> dict[str, Any]:
    """Confirm one exact v2 envelope and advance candidate delivery state."""

    confirmed_at = _coerce_utc_iso(confirmed_at_utc)
    with _locked_delivery_envelope(
        base=base,
        account=account,
        market=market,
        market_trading_date=market_trading_date,
        delivery_key=delivery_key,
        source_digest=source_digest,
        message_sha256=message_sha256,
        transport_idempotency_key=transport_idempotency_key,
    ) as (state, day, envelope, delivery_path):
        if envelope["status"] == "expired_unconfirmed":
            raise DailyDecisionBriefStateError("expired daily brief delivery cannot be confirmed")
        if envelope["status"] == "confirmed":
            return {
                "advanced": False,
                "reason": "already_confirmed",
                "envelope": dict(envelope),
                "path": delivery_path,
            }
        _mark_delivery_envelope_confirmed(
            day=day,
            envelope=envelope,
            confirmed_at=confirmed_at,
        )
        atomic_write_json(delivery_path, state)
        return {
            "advanced": True,
            "reason": "confirmed",
            "envelope": dict(envelope),
            "path": delivery_path,
        }


def reconcile_daily_decision_brief_delivery_resolution(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    delivery_key: str,
    source_digest: str,
    message_sha256: str,
    transport_idempotency_key: str,
    resolution: str,
    resolved_at_utc: datetime | str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Bind an operator resolution to the exact frozen delivery envelope."""

    outcome = str(resolution or "").strip().lower()
    if outcome not in {"delivered", "failed"}:
        raise ValueError("daily brief delivery resolution must be delivered or failed")
    resolved_at = _coerce_utc_iso(resolved_at_utc)
    with _locked_delivery_envelope(
        base=base,
        account=account,
        market=market,
        market_trading_date=market_trading_date,
        delivery_key=delivery_key,
        source_digest=source_digest,
        message_sha256=message_sha256,
        transport_idempotency_key=transport_idempotency_key,
    ) as (state, day, envelope, delivery_path):
        status = str(envelope["status"])
        if outcome == "delivered":
            if status == "expired_unconfirmed":
                raise DailyDecisionBriefStateError(
                    "expired daily brief delivery cannot be resolved as delivered"
                )
            would_change = status != "confirmed"
            if would_change and not dry_run:
                _mark_delivery_envelope_confirmed(
                    day=day,
                    envelope=envelope,
                    confirmed_at=resolved_at,
                )
        else:
            if status == "confirmed":
                raise DailyDecisionBriefStateError(
                    "confirmed daily brief delivery cannot be resolved as failed"
                )
            if status == "expired_unconfirmed":
                raise DailyDecisionBriefStateError(
                    "expired daily brief delivery cannot be retried"
                )
            would_change = status != "pending"
            if not dry_run:
                envelope["status"] = "pending"
                envelope["last_attempt_at_utc"] = resolved_at
                envelope["confirmed_at_utc"] = None
        if would_change and not dry_run:
            atomic_write_json(delivery_path, state)
        return {
            "updated": bool(would_change and not dry_run),
            "would_change": would_change,
            "dry_run": bool(dry_run),
            "reason": (
                "confirmed"
                if outcome == "delivered" and would_change
                else "already_confirmed"
                if outcome == "delivered"
                else "retry_enabled"
                if would_change
                else "already_pending"
            ),
            "envelope": dict(envelope),
            "path": delivery_path,
        }


def _mark_delivery_envelope_confirmed(
    *,
    day: dict[str, Any],
    envelope: dict[str, Any],
    confirmed_at: str,
) -> None:
    envelope["status"] = "confirmed"
    envelope["last_attempt_at_utc"] = confirmed_at
    envelope["confirmed_at_utc"] = confirmed_at
    if envelope["delivery_kind"] not in {"fixed_report", "candidate_alert"}:
        return
    revision = int(envelope["revision"])
    via = str(envelope["delivery_kind"])
    for identity in envelope["candidate_identities"]:
        day["alerted_candidates"].setdefault(
            identity,
            {
                "revision": revision,
                "brief_digest": str(envelope["source_digest"]),
                "delivery_key": str(envelope["delivery_key"]),
                "confirmed_at_utc": confirmed_at,
                "via": via,
            },
        )
        day["pending_candidates"].pop(identity, None)


def expire_daily_decision_brief_delivery_day(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
) -> dict[str, Any]:
    """Expire unresolved envelopes for one market day without touching later days."""

    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    date_norm = _normalize_market_date(market_trading_date)
    delivery_path = _delivery_path(base_path, account_norm, market_norm)
    lock_path = paths.account_state_dir(base_path, account_norm) / f"daily_decision_brief.{market_norm}.lock"
    with _exclusive_lock(lock_path):
        raw = _read_json_strict(delivery_path)
        if raw is _MISSING:
            return {"updated": False, "reason": "not_found", "path": delivery_path}
        state = _normalize_delivery_state(
            raw,
            base=base_path,
            path=delivery_path,
            account=account_norm,
            market=market_norm,
        )
        day = state["days"].get(date_norm)
        if not day:
            return {"updated": False, "reason": "no_delivery_day", "path": delivery_path}
        changed = False
        envelopes = list(day["fixed_reports"].values())
        if isinstance(day.get("candidate_delivery"), Mapping):
            envelopes.append(day["candidate_delivery"])
        for envelope in envelopes:
            if envelope["status"] in {"pending", "ambiguous"}:
                envelope["status"] = "expired_unconfirmed"
                changed = True
        if changed:
            atomic_write_json(delivery_path, state)
        return {
            "updated": changed,
            "reason": "expired" if changed else "already_final",
            "path": delivery_path,
        }


def inspect_daily_decision_brief_delivery(
    *,
    base: Path,
    account: str,
    market: str,
) -> dict[str, Any]:
    """Inspect v1/v2 delivery state and preview a safe v1 migration."""

    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    path = _delivery_path(base_path, account_norm, market_norm)
    raw = _read_json_strict(path)
    if raw is _MISSING:
        return {
            "available": False,
            "reason": "not_found",
            "account": account_norm,
            "market": market_norm,
            "path": path,
        }
    if not isinstance(raw, Mapping):
        raise DailyDecisionBriefStateError(f"daily brief delivery state is not an object: {path}")
    schema = str(raw.get("schema_version") or "")
    if schema == DELIVERY_STATE_SCHEMA_VERSION:
        state = _normalize_delivery_state(
            raw,
            base=base_path,
            path=path,
            account=account_norm,
            market=market_norm,
        )
        return {
            "available": True,
            "reason": "already_v2",
            "source_schema_version": schema,
            "account": account_norm,
            "market": market_norm,
            "path": path,
            "state": state,
            "migration": None,
        }
    if schema != LEGACY_DELIVERY_POINTER_SCHEMA_VERSION:
        raise DailyDecisionBriefStateError(f"unsupported daily brief delivery schema: {path}")
    pointer = _normalize_delivery_pointer(raw, path=path, account=account_norm, market=market_norm)
    migration = _build_v2_state_from_legacy_pointer(
        base=base_path,
        pointer=pointer,
        pointer_path=path,
    )
    return {
        "available": True,
        "reason": "migration_available",
        "source_schema_version": schema,
        "account": account_norm,
        "market": market_norm,
        "path": path,
        "legacy_pointer": pointer,
        "migration": migration,
    }


def migrate_daily_decision_brief_delivery(
    *,
    base: Path,
    account: str,
    market: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Explicitly migrate one validated v1 pointer; dry-run is the default."""

    preview = inspect_daily_decision_brief_delivery(base=base, account=account, market=market)
    if not confirm or preview.get("reason") != "migration_available":
        return {**preview, "dry_run": not confirm, "write_applied": False, "backup_path": None}

    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    state_dir = paths.account_state_dir(base_path, account_norm)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"daily_decision_brief.{market_norm}.lock"
    pointer_path = _delivery_path(base_path, account_norm, market_norm)
    with _exclusive_lock(lock_path):
        source_text = pointer_path.read_text(encoding="utf-8")
        try:
            source_raw = json.loads(source_text)
        except json.JSONDecodeError as exc:
            raise DailyDecisionBriefStateError(f"failed to read daily brief state: {pointer_path}: {exc}") from exc
        pointer = _normalize_delivery_pointer(
            source_raw,
            path=pointer_path,
            account=account_norm,
            market=market_norm,
        )
        target_state = _build_v2_state_from_legacy_pointer(
            base=base_path,
            pointer=pointer,
            pointer_path=pointer_path,
        )["target_state"]
        backup_path = _delivery_backup_path(pointer_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with backup_path.open("x", encoding="utf-8") as handle:
            handle.write(source_text)
        try:
            atomic_write_json(pointer_path, target_state)
            reread = _read_json_strict(pointer_path)
            verified = _normalize_delivery_state(
                reread,
                base=base_path,
                path=pointer_path,
                account=account_norm,
                market=market_norm,
            )
        except BaseException:
            atomic_write_text(pointer_path, source_text)
            raise
        return {
            "available": True,
            "reason": "migrated",
            "source_schema_version": LEGACY_DELIVERY_POINTER_SCHEMA_VERSION,
            "target_schema_version": DELIVERY_STATE_SCHEMA_VERSION,
            "account": account_norm,
            "market": market_norm,
            "path": pointer_path,
            "dry_run": False,
            "write_applied": True,
            "backup_path": backup_path,
            "state": verified,
            "rollback": {
                "backup_path": backup_path,
                "target_path": pointer_path,
                "requires_stopped_scheduler": True,
            },
        }


def prepare_daily_decision_brief(
    *,
    base: Path,
    brief: Mapping[str, Any],
) -> dict[str, Any]:
    """Allocate and persist one revision, then prepare full/delta/none delivery state.

    Revision and delivery state are serialized per account+market. Every JSON file
    is replaced atomically. Existing malformed or incompatible state fails closed.
    """

    base_path = Path(base).resolve()
    source = dict(brief or {})
    market = _normalize_market(source.get("market"))
    account = _normalize_account(source.get("account"))
    market_date = _normalize_market_date(source.get("market_trading_date"))
    run_id = str(source.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id is required for daily brief persistence")

    account_dir = paths.account_state_dir(base_path, account)
    account_dir.mkdir(parents=True, exist_ok=True)
    lock_path = account_dir / f"daily_decision_brief.{market}.lock"

    with _exclusive_lock(lock_path):
        current_path = _current_path(base_path, account, market)
        current_raw = _read_json_strict(current_path)
        current = None
        if current_raw is not _MISSING:
            current = _normalize_persisted_brief(
                current_raw,
                path=current_path,
                account=account,
                market=market,
            )
            current_revision_path = _revision_path(
                base_path,
                account,
                market,
                current["market_trading_date"],
                int(current["revision"]),
            )
            current_revision_raw = _read_json_strict(current_revision_path)
            if current_revision_raw is _MISSING:
                raise DailyDecisionBriefStateError(
                    f"daily brief current state references a missing revision: {current_revision_path}"
                )
            current_revision = _normalize_persisted_brief(
                current_revision_raw,
                path=current_revision_path,
                account=account,
                market=market,
            )
            if daily_brief_digest(current_revision) != daily_brief_digest(current):
                raise DailyDecisionBriefStateError(
                    f"daily brief current state digest mismatch: {current_path}"
                )

        existing_revisions = _list_revision_numbers(
            base=base_path,
            account=account,
            market=market,
            market_trading_date=market_date,
        )
        revision = (existing_revisions[-1] + 1) if existing_revisions else 0

        candidate = dict(source)
        candidate.update(
            {
                "market": market,
                "market_trading_date": market_date,
                "account": account,
                "revision": revision,
                "run_id": run_id,
            }
        )
        normalized = normalize_daily_decision_brief(candidate)

        revision_path = _revision_path(base_path, account, market, market_date, revision)
        if revision_path.exists():
            raise DailyDecisionBriefStateError(f"daily brief revision already exists: {revision_path}")

        delivery_path = _delivery_path(base_path, account, market)
        delivery_raw = _read_json_strict(delivery_path)
        delivery = None
        if delivery_raw is not _MISSING:
            delivery = _normalize_delivery_pointer(
                delivery_raw,
                path=delivery_path,
                account=account,
                market=market,
            )

        last_delivered_brief = None
        if delivery is not None and delivery["market_trading_date"] == market_date:
            if int(delivery["revision"]) >= revision:
                raise DailyDecisionBriefStateError(
                    "daily brief delivery pointer is not behind the newly allocated revision"
                )
            delivered_path = _revision_path(
                base_path,
                account,
                market,
                market_date,
                int(delivery["revision"]),
            )
            delivered_raw = _read_json_strict(delivered_path)
            if delivered_raw is _MISSING:
                raise DailyDecisionBriefStateError(
                    f"daily brief delivery pointer references a missing revision: {delivered_path}"
                )
            last_delivered_brief = _normalize_persisted_brief(
                delivered_raw,
                path=delivered_path,
                account=account,
                market=market,
            )
            delivered_digest = daily_brief_digest(last_delivered_brief)
            if delivery.get("brief_digest") != delivered_digest:
                raise DailyDecisionBriefStateError(
                    f"daily brief delivery pointer digest mismatch: {delivery_path}"
                )

        if last_delivered_brief is None:
            full_semantic_digest = _full_brief_semantic_digest(normalized)
            diff = _initial_full_diff(
                normalized,
                full_semantic_digest=full_semantic_digest,
            )
            delivery_kind = "full"
            delivery_key = _full_delivery_key(
                market=market,
                market_date=market_date,
                account=account,
                semantic_digest=full_semantic_digest,
            )
            last_delivered_revision = None
            last_delivered_digest = None
        else:
            diff = diff_daily_decision_briefs(last_delivered_brief, normalized)
            last_delivered_revision = int(last_delivered_brief["revision"])
            last_delivered_digest = daily_brief_digest(last_delivered_brief)
            if bool(diff.get("material")):
                delivery_kind = "delta"
                delivery_key = (
                    f"daily-brief:{market}:{market_date}:{account}:from:"
                    f"{last_delivered_digest}:{diff['material_diff_digest']}"
                )
            else:
                delivery_kind = "none"
                delivery_key = ""

        run_state_dir = paths.run_account_state_dir(base_path, run_id, account)
        run_brief_path = run_state_dir / f"daily_decision_brief.{market}.json"
        run_diff_path = run_state_dir / f"daily_decision_brief_diff.{market}.json"
        shared_index_path = paths.shared_state_dir(base_path) / "current" / "daily_decision_briefs.current.json"
        shared_lock_path = paths.shared_state_dir(base_path) / "current" / "daily_decision_briefs.current.lock"

        with _exclusive_lock(shared_lock_path):
            shared_index = _load_current_index(shared_index_path)
            brief_digest = daily_brief_digest(normalized)
            shared_index["items"][f"{market}/{account}"] = {
                "market": market,
                "market_trading_date": market_date,
                "account": account,
                "revision": revision,
                "run_id": run_id,
                "brief_digest": brief_digest,
                "path": _relative_path(base_path, current_path),
            }
            shared_index["updated_at_utc"] = normalized.get("generated_at_utc") or _utc_now_iso()

            atomic_write_json(revision_path, normalized)
            atomic_write_json(current_path, normalized)
            atomic_write_json(run_brief_path, normalized)
            atomic_write_json(shared_index_path, shared_index)
            atomic_write_json(run_diff_path, diff)

        return {
            "brief": normalized,
            "diff": diff,
            "delivery_kind": delivery_kind,
            "delivery_key": delivery_key,
            "last_delivered_revision": last_delivered_revision,
            "last_delivered_brief_digest": last_delivered_digest,
            "current_revision": revision,
            "current_brief_digest": daily_brief_digest(normalized),
            "paths": {
                "revision": revision_path,
                "current": current_path,
                "run_brief": run_brief_path,
                "run_diff": run_diff_path,
                "delivery": delivery_path,
                "shared_index": shared_index_path,
            },
        }


def confirm_daily_decision_brief_delivery(
    *,
    base: Path,
    market: str,
    market_trading_date: str,
    account: str,
    revision: int,
    delivery_kind: str,
    delivery_key: str,
    brief_digest: str | None = None,
    confirmed_at_utc: datetime | str | None = None,
) -> dict[str, Any]:
    """Advance the delivery pointer after confirmed provider delivery only."""

    base_path = Path(base).resolve()
    market_norm = _normalize_market(market)
    account_norm = _normalize_account(account)
    date_norm = _normalize_market_date(market_trading_date)
    revision_norm = _normalize_revision(revision)
    kind_norm = str(delivery_kind or "").strip().lower()
    if kind_norm not in {"full", "delta"}:
        raise ValueError("delivery_kind must be full or delta")
    key_norm = str(delivery_key or "").strip()
    if not key_norm:
        raise ValueError("delivery_key is required for confirmed delivery")

    state_dir = paths.account_state_dir(base_path, account_norm)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"daily_decision_brief.{market_norm}.lock"
    pointer_path = _delivery_path(base_path, account_norm, market_norm)

    with _exclusive_lock(lock_path):
        revision_path = _revision_path(base_path, account_norm, market_norm, date_norm, revision_norm)
        raw = _read_json_strict(revision_path)
        if raw is _MISSING:
            raise DailyDecisionBriefStateError(
                f"cannot confirm missing daily brief revision: {revision_path}"
            )
        persisted = _normalize_persisted_brief(
            raw,
            path=revision_path,
            account=account_norm,
            market=market_norm,
        )
        if persisted["market_trading_date"] != date_norm or int(persisted["revision"]) != revision_norm:
            raise DailyDecisionBriefStateError(f"daily brief revision identity mismatch: {revision_path}")
        persisted_digest = daily_brief_digest(persisted)
        if brief_digest is not None and str(brief_digest).strip() != persisted_digest:
            raise DailyDecisionBriefStateError("confirmed daily brief digest does not match persisted revision")

        existing_raw = _read_json_strict(pointer_path)
        existing = None
        if existing_raw is not _MISSING:
            existing = _normalize_delivery_pointer(
                existing_raw,
                path=pointer_path,
                account=account_norm,
                market=market_norm,
            )
        if existing is not None:
            existing_date = existing["market_trading_date"]
            existing_revision = int(existing["revision"])
            if existing_date > date_norm or (existing_date == date_norm and existing_revision > revision_norm):
                return {"advanced": False, "reason": "stale_completion", "pointer": existing, "path": pointer_path}
            if existing_date == date_norm and existing_revision == revision_norm:
                if existing.get("brief_digest") != persisted_digest:
                    raise DailyDecisionBriefStateError("same delivery revision has a conflicting digest")
                return {"advanced": False, "reason": "already_confirmed", "pointer": existing, "path": pointer_path}

        expected = _prepared_delivery_expectation(
            base=base_path,
            brief=persisted,
        )
        if kind_norm != expected["delivery_kind"] or key_norm != expected["delivery_key"]:
            raise DailyDecisionBriefStateError(
                "confirmed daily brief delivery envelope does not match prepared lifecycle"
            )

        pointer = {
            "schema_version": DELIVERY_POINTER_SCHEMA_VERSION,
            "market": market_norm,
            "market_trading_date": date_norm,
            "account": account_norm,
            "revision": revision_norm,
            "brief_digest": persisted_digest,
            "delivery_kind": kind_norm,
            "delivery_key": key_norm,
            "confirmed_at_utc": _coerce_utc_iso(confirmed_at_utc),
        }
        atomic_write_json(pointer_path, pointer)
        return {"advanced": True, "reason": "confirmed", "pointer": pointer, "path": pointer_path}


def read_latest_daily_decision_brief(*, base: Path, account: str, market: str) -> dict[str, Any]:
    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    path = _current_path(base_path, account_norm, market_norm)
    result = _read_brief_result(path=path, account=account_norm, market=market_norm)
    if not result.get("available"):
        return result
    try:
        _validate_current_revision(
            base=base_path,
            current=result["brief"],
            current_path=path,
        )
    except DailyDecisionBriefStateError as exc:
        return {"available": False, "reason": "state_invalid", "error": str(exc), "brief": None, "path": path}
    return result


def read_daily_decision_brief(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    revision: int | None = None,
) -> dict[str, Any]:
    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    date_norm = _normalize_market_date(market_trading_date)
    if revision is None:
        listed = list_daily_decision_brief_revisions(
            base=base_path,
            account=account_norm,
            market=market_norm,
            market_trading_date=date_norm,
        )
        if not listed["available"]:
            return listed
        revision_norm = int(listed["revisions"][-1])
    else:
        revision_norm = _normalize_revision(revision)
    path = _revision_path(base_path, account_norm, market_norm, date_norm, revision_norm)
    return _read_brief_result(
        path=path,
        account=account_norm,
        market=market_norm,
        market_trading_date=date_norm,
        revision=revision_norm,
    )


def list_daily_decision_brief_revisions(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
) -> dict[str, Any]:
    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    date_norm = _normalize_market_date(market_trading_date)
    revisions = _list_revision_numbers(
        base=base_path,
        account=account_norm,
        market=market_norm,
        market_trading_date=date_norm,
    )
    if not revisions:
        return {
            "available": False,
            "reason": "not_found",
            "account": account_norm,
            "market": market_norm,
            "market_trading_date": date_norm,
            "revisions": [],
        }
    return {
        "available": True,
        "reason": "ok",
        "account": account_norm,
        "market": market_norm,
        "market_trading_date": date_norm,
        "revisions": revisions,
    }


def read_combo_candidate_exposures(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
) -> dict[str, Any]:
    """Rebuild exact Combo exposure facts from frozen Brief and delivery state."""

    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    date_norm = _normalize_market_date(market_trading_date)
    listed = list_daily_decision_brief_revisions(
        base=base_path,
        account=account_norm,
        market=market_norm,
        market_trading_date=date_norm,
    )
    if not listed.get("available"):
        return {**listed, "exposures": []}
    delivery_result = read_daily_decision_brief_delivery_state(
        base=base_path,
        account=account_norm,
        market=market_norm,
    )
    delivery_day = (
        (delivery_result.get("state") or {}).get("days", {}).get(date_norm)
        if delivery_result.get("available")
        else None
    )
    envelopes: list[dict[str, Any]] = []
    if isinstance(delivery_day, Mapping):
        envelopes.extend(
            dict(item)
            for item in (delivery_day.get("fixed_reports") or {}).values()
            if isinstance(item, Mapping)
        )
        candidate = delivery_day.get("candidate_delivery")
        if isinstance(candidate, Mapping):
            envelopes.append(dict(candidate))
        envelopes.extend(
            dict(item)
            for item in (delivery_day.get("candidate_delivery_history") or [])
            if isinstance(item, Mapping)
        )

    out_by_id: dict[str, dict[str, Any]] = {}
    invalid_revisions: list[int] = []
    for revision in listed["revisions"]:
        result = read_daily_decision_brief(
            base=base_path,
            account=account_norm,
            market=market_norm,
            market_trading_date=date_norm,
            revision=int(revision),
        )
        if not result.get("available"):
            invalid_revisions.append(int(revision))
            continue
        brief = result["brief"]
        raw_brief = _read_json_strict(result["path"])
        compatible_digests = set(daily_brief_compatible_digests(raw_brief))
        exposures = derive_combo_candidate_exposures(brief)
        for exposure in exposures:
            item = dict(exposure)
            confirmed_envelopes = [
                envelope
                for envelope in envelopes
                if envelope.get("status") == "confirmed"
                and envelope.get("source_kind") == "successful_brief"
                and envelope.get("revision") is not None
                and int(envelope["revision"]) == int(revision)
                and str(envelope.get("source_digest") or "") in compatible_digests
                and str(item["candidate_exposure_id"])
                in set((envelope.get("render_context") or {}).get("candidate_exposure_ids") or [])
                and str(item["candidate_occurrence_id"])
                in set((envelope.get("render_context") or {}).get("candidate_occurrence_ids") or [])
            ]
            item["delivery_confirmed"] = bool(confirmed_envelopes)
            item["confirmed_delivery_keys"] = sorted(
                {str(envelope.get("delivery_key") or "") for envelope in confirmed_envelopes}
                - {""}
            )
            out_by_id[str(item["candidate_exposure_id"])] = item
    return {
        "available": True,
        "reason": "partial" if invalid_revisions else "ok",
        "account": account_norm,
        "market": market_norm,
        "market_trading_date": date_norm,
        "invalid_revisions": invalid_revisions,
        "exposures": [out_by_id[key] for key in sorted(out_by_id)],
    }


def read_daily_decision_brief_delivery(*, base: Path, account: str, market: str) -> dict[str, Any]:
    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    path = _delivery_path(base_path, account_norm, market_norm)
    try:
        raw = _read_json_strict(path)
        if raw is _MISSING:
            return {"available": False, "reason": "not_found", "pointer": None, "path": path}
        pointer = _normalize_delivery_pointer(raw, path=path, account=account_norm, market=market_norm)
    except DailyDecisionBriefStateError as exc:
        return {"available": False, "reason": "state_invalid", "error": str(exc), "pointer": None, "path": path}
    return {"available": True, "reason": "ok", "pointer": pointer, "path": path}


def _read_brief_result(
    *,
    path: Path,
    account: str,
    market: str,
    market_trading_date: str | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    try:
        raw = _read_json_strict(path)
        if raw is _MISSING:
            return {"available": False, "reason": "not_found", "brief": None, "path": path}
        brief = _normalize_persisted_brief(raw, path=path, account=account, market=market)
        if market_trading_date is not None and brief["market_trading_date"] != market_trading_date:
            raise DailyDecisionBriefStateError(f"daily brief date mismatch: {path}")
        if revision is not None and int(brief["revision"]) != revision:
            raise DailyDecisionBriefStateError(f"daily brief revision mismatch: {path}")
    except DailyDecisionBriefStateError as exc:
        return {"available": False, "reason": "state_invalid", "error": str(exc), "brief": None, "path": path}
    return {"available": True, "reason": "ok", "brief": brief, "path": path}


def _current_path(base: Path, account: str, market: str) -> Path:
    return paths.account_state_dir(base, account) / f"daily_decision_brief.{market}.current.json"


def _revision_path(base: Path, account: str, market: str, market_date: str, revision: int) -> Path:
    return paths.account_state_dir(base, account) / (
        f"daily_decision_brief.{market}.{market_date}.r{int(revision):04d}.json"
    )


def _delivery_path(base: Path, account: str, market: str) -> Path:
    return paths.account_state_dir(base, account) / f"daily_decision_brief.{market}.delivery.json"


def _list_revision_numbers(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
) -> list[int]:
    state_dir = paths.account_state_dir(base, account)
    prefix = f"daily_decision_brief.{market}.{market_trading_date}.r"
    revisions: list[int] = []
    for path in sorted(state_dir.glob(f"{prefix}*.json")):
        match = _REVISION_RE.search(path.name)
        if match:
            revisions.append(int(match.group("revision")))
    return sorted(set(revisions))


def _normalize_persisted_brief(raw: Any, *, path: Path, account: str, market: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DailyDecisionBriefStateError(f"daily brief state is not an object: {path}")
    try:
        brief = normalize_daily_decision_brief(raw)
    except (TypeError, ValueError) as exc:
        raise DailyDecisionBriefStateError(f"daily brief state is incompatible: {path}: {exc}") from exc
    if brief["account"] != account or brief["market"] != market:
        raise DailyDecisionBriefStateError(f"daily brief state identity mismatch: {path}")
    return brief


def _empty_delivery_state(*, account: str, market: str) -> dict[str, Any]:
    return {
        "schema_version": DELIVERY_STATE_SCHEMA_VERSION,
        "account": account,
        "market": market,
        "days": {},
        "legacy_last_confirmation": None,
    }


def _load_or_create_delivery_state(
    *,
    base: Path,
    path: Path,
    account: str,
    market: str,
) -> dict[str, Any]:
    raw = _read_json_strict(path)
    if raw is _MISSING:
        return _empty_delivery_state(account=account, market=market)
    return _normalize_delivery_state(
        raw,
        base=base,
        path=path,
        account=account,
        market=market,
    )


def _delivery_day(state: dict[str, Any], market_date: str) -> dict[str, Any]:
    return state["days"].setdefault(
        market_date,
        {
            "fixed_reports": {},
            "pending_candidates": {},
            "alerted_candidates": {},
            "candidate_delivery": None,
            "candidate_delivery_history": [],
        },
    )


def _normalize_delivery_state(
    raw: Any,
    *,
    base: Path,
    path: Path,
    account: str,
    market: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DailyDecisionBriefStateError(f"daily brief delivery state is not an object: {path}")
    if raw.get("schema_version") != DELIVERY_STATE_SCHEMA_VERSION:
        raise DailyDecisionBriefStateError(f"unsupported daily brief delivery schema: {path}")
    try:
        state_account = _normalize_account(raw.get("account"))
        state_market = _normalize_market(raw.get("market"))
    except ValueError as exc:
        raise DailyDecisionBriefStateError(f"daily brief delivery state is incompatible: {path}: {exc}") from exc
    if state_account != account or state_market != market:
        raise DailyDecisionBriefStateError(f"daily brief delivery identity mismatch: {path}")
    raw_days = raw.get("days")
    if not isinstance(raw_days, Mapping):
        raise DailyDecisionBriefStateError(f"daily brief delivery days are invalid: {path}")

    days: dict[str, Any] = {}
    for raw_date, raw_day in sorted(raw_days.items()):
        try:
            market_date = _normalize_market_date(raw_date)
        except ValueError as exc:
            raise DailyDecisionBriefStateError(f"daily brief delivery date is invalid: {path}: {exc}") from exc
        if not isinstance(raw_day, Mapping):
            raise DailyDecisionBriefStateError(f"daily brief delivery day is not an object: {path}: {market_date}")
        fixed_raw = raw_day.get("fixed_reports")
        pending_raw = raw_day.get("pending_candidates")
        alerted_raw = raw_day.get("alerted_candidates")
        if not isinstance(fixed_raw, Mapping) or not isinstance(pending_raw, Mapping) or not isinstance(alerted_raw, Mapping):
            raise DailyDecisionBriefStateError(f"daily brief delivery day collections are invalid: {path}: {market_date}")

        fixed: dict[str, Any] = {}
        for raw_target, raw_envelope in sorted(fixed_raw.items()):
            try:
                target = _normalize_scheduled_target(raw_target, market_date=market_date)
            except ValueError as exc:
                raise DailyDecisionBriefStateError(f"daily brief fixed target is invalid: {path}: {exc}") from exc
            fixed[target] = _normalize_delivery_envelope(
                raw_envelope,
                base=base,
                path=path,
                account=account,
                market=market,
                market_date=market_date,
                expected_target=target,
                expected_candidate=False,
            )

        pending: dict[str, Any] = {}
        for raw_identity, raw_pending in sorted(pending_raw.items()):
            try:
                identity = _normalize_candidate_identities(
                    [raw_identity],
                    account=account,
                    market=market,
                )[0]
            except ValueError as exc:
                raise DailyDecisionBriefStateError(
                    f"daily brief pending candidate identity is invalid: {path}: {exc}"
                ) from exc
            if not isinstance(raw_pending, Mapping):
                raise DailyDecisionBriefStateError(f"daily brief pending candidate is invalid: {path}: {identity}")
            try:
                first_revision = _normalize_revision(raw_pending.get("first_seen_revision"))
                first_seen = _normalize_required_utc_iso(
                    raw_pending.get("first_seen_at_utc"),
                    field="first_seen_at_utc",
                )
            except ValueError as exc:
                raise DailyDecisionBriefStateError(f"daily brief pending candidate is invalid: {path}: {exc}") from exc
            pending[identity] = {
                "first_seen_revision": first_revision,
                "first_seen_at_utc": first_seen,
            }

        alerted: dict[str, Any] = {}
        for raw_identity, raw_alerted in sorted(alerted_raw.items()):
            try:
                identity = _normalize_candidate_identities(
                    [raw_identity],
                    account=account,
                    market=market,
                )[0]
            except ValueError as exc:
                raise DailyDecisionBriefStateError(
                    f"daily brief alerted candidate identity is invalid: {path}: {exc}"
                ) from exc
            if not isinstance(raw_alerted, Mapping):
                raise DailyDecisionBriefStateError(f"daily brief alerted candidate is invalid: {path}: {identity}")
            try:
                alerted_revision = _normalize_revision(raw_alerted.get("revision"))
                alerted_digest = _normalize_sha256(raw_alerted.get("brief_digest"), field="brief_digest")
                confirmed_at = _normalize_required_utc_iso(
                    raw_alerted.get("confirmed_at_utc"),
                    field="confirmed_at_utc",
                )
            except ValueError as exc:
                raise DailyDecisionBriefStateError(f"daily brief alerted candidate is invalid: {path}: {exc}") from exc
            delivery_key = str(raw_alerted.get("delivery_key") or "").strip()
            via = str(raw_alerted.get("via") or "").strip().lower()
            if not delivery_key or via not in {"fixed_report", "candidate_alert", "legacy_delivery"}:
                raise DailyDecisionBriefStateError(f"daily brief alerted candidate metadata is invalid: {path}: {identity}")
            source_brief = _validate_successful_revision_source(
                base=base,
                account=account,
                market=market,
                market_trading_date=market_date,
                revision=alerted_revision,
                source_digest=alerted_digest,
            )
            if identity not in _candidate_identity_set(source_brief):
                raise DailyDecisionBriefStateError(
                    f"daily brief alerted candidate is absent from its revision: {path}: {identity}"
                )
            alerted[identity] = {
                "revision": alerted_revision,
                "brief_digest": alerted_digest,
                "delivery_key": delivery_key,
                "confirmed_at_utc": confirmed_at,
                "via": via,
            }

        candidate_raw = raw_day.get("candidate_delivery")
        candidate = None
        if candidate_raw is not None:
            candidate = _normalize_delivery_envelope(
                candidate_raw,
                base=base,
                path=path,
                account=account,
                market=market,
                market_date=market_date,
                expected_target=None,
                expected_candidate=True,
            )
        history_raw = raw_day.get("candidate_delivery_history", [])
        if not isinstance(history_raw, list):
            raise DailyDecisionBriefStateError(
                f"daily brief candidate delivery history is invalid: {path}: {market_date}"
            )
        history: list[dict[str, Any]] = []
        history_keys: set[str] = set()
        for raw_envelope in history_raw:
            normalized_envelope = _normalize_delivery_envelope(
                raw_envelope,
                base=base,
                path=path,
                account=account,
                market=market,
                market_date=market_date,
                expected_target=None,
                expected_candidate=True,
            )
            if normalized_envelope["status"] != "confirmed":
                raise DailyDecisionBriefStateError(
                    f"daily brief candidate delivery history must be confirmed: {path}: {market_date}"
                )
            delivery_key = str(normalized_envelope["delivery_key"])
            if delivery_key in history_keys:
                raise DailyDecisionBriefStateError(
                    f"daily brief candidate delivery history contains duplicate keys: {path}: {market_date}"
                )
            history_keys.add(delivery_key)
            history.append(normalized_envelope)
        if candidate is not None and str(candidate["delivery_key"]) in history_keys:
            raise DailyDecisionBriefStateError(
                f"daily brief current candidate delivery duplicates history: {path}: {market_date}"
            )
        days[market_date] = {
            "fixed_reports": fixed,
            "pending_candidates": pending,
            "alerted_candidates": alerted,
            "candidate_delivery": candidate,
            "candidate_delivery_history": history,
        }
        if set(pending) & set(alerted):
            raise DailyDecisionBriefStateError(
                f"daily brief candidate cannot be both pending and alerted: {path}: {market_date}"
            )

    legacy_raw = raw.get("legacy_last_confirmation")
    legacy = None
    if legacy_raw is not None:
        legacy = _normalize_legacy_confirmation(
            legacy_raw,
            base=base,
            path=path,
            account=account,
            market=market,
        )
    return {
        "schema_version": DELIVERY_STATE_SCHEMA_VERSION,
        "account": account,
        "market": market,
        "days": days,
        "legacy_last_confirmation": legacy,
    }


def _normalize_delivery_envelope(
    raw: Any,
    *,
    base: Path,
    path: Path,
    account: str,
    market: str,
    market_date: str,
    expected_target: str | None,
    expected_candidate: bool,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DailyDecisionBriefStateError(f"daily brief delivery envelope is not an object: {path}")
    envelope = dict(raw)
    status = str(envelope.get("status") or "").strip().lower()
    if status not in {"pending", "ambiguous", "confirmed", "expired_unconfirmed"}:
        raise DailyDecisionBriefStateError(f"daily brief delivery status is invalid: {path}")
    delivery_kind = str(envelope.get("delivery_kind") or "").strip().lower()
    valid_kinds = {"candidate_alert"} if expected_candidate else {"fixed_report", "fixed_failure"}
    if delivery_kind not in valid_kinds:
        raise DailyDecisionBriefStateError(f"daily brief delivery kind is invalid: {path}")
    source_kind = str(envelope.get("source_kind") or "").strip().lower()
    if source_kind not in {"successful_brief", "scan_failure"}:
        raise DailyDecisionBriefStateError(f"daily brief delivery source kind is invalid: {path}")
    if (delivery_kind == "fixed_failure") != (source_kind == "scan_failure"):
        raise DailyDecisionBriefStateError(f"daily brief delivery source/kind mismatch: {path}")
    try:
        source_digest = _normalize_sha256(envelope.get("source_digest"), field="source_digest")
        candidate_identities = _normalize_candidate_identities(
            envelope.get("candidate_identities") or [],
            account=account,
            market=market,
        )
        first_prepared = _normalize_required_utc_iso(
            envelope.get("first_prepared_at_utc"),
            field="first_prepared_at_utc",
        )
        last_attempt = _normalize_optional_utc_iso(
            envelope.get("last_attempt_at_utc"),
            field="last_attempt_at_utc",
        )
        confirmed_at = _normalize_optional_utc_iso(
            envelope.get("confirmed_at_utc"),
            field="confirmed_at_utc",
        )
    except ValueError as exc:
        raise DailyDecisionBriefStateError(f"daily brief delivery envelope is invalid: {path}: {exc}") from exc
    rendered_message = str(envelope.get("rendered_message") or "")
    message_sha256 = str(envelope.get("message_sha256") or "").strip()
    if not rendered_message.strip() or not _SHA256_RE.fullmatch(message_sha256):
        raise DailyDecisionBriefStateError(f"daily brief delivery message is invalid: {path}")
    if hashlib.sha256(rendered_message.encode("utf-8")).hexdigest() != message_sha256:
        raise DailyDecisionBriefStateError(f"daily brief delivery message digest mismatch: {path}")
    rendered_transport_raw = envelope.get("rendered_transport")
    rendered_transport_sha256 = str(envelope.get("rendered_transport_sha256") or "").strip() or None
    if rendered_transport_raw is None:
        if rendered_transport_sha256 is not None:
            raise DailyDecisionBriefStateError(f"daily brief delivery transport digest has no payload: {path}")
        rendered_transport = None
    else:
        try:
            rendered_transport = normalize_feishu_notification_envelope(
                rendered_transport_raw,
                expected_text=rendered_message,
            )
        except ValueError as exc:
            raise DailyDecisionBriefStateError(
                f"daily brief delivery transport is invalid: {path}: {exc}"
            ) from exc
        actual_transport_sha256 = feishu_notification_envelope_sha256(rendered_transport)
        if rendered_transport_sha256 != actual_transport_sha256:
            raise DailyDecisionBriefStateError(f"daily brief delivery transport digest mismatch: {path}")
    if delivery_kind == "fixed_failure" and rendered_transport is not None:
        raise DailyDecisionBriefStateError(f"fixed failure delivery must not contain card transport: {path}")
    if status == "confirmed" and confirmed_at is None:
        raise DailyDecisionBriefStateError(f"confirmed daily brief delivery has no confirmation time: {path}")
    if status != "confirmed" and confirmed_at is not None:
        raise DailyDecisionBriefStateError(f"unconfirmed daily brief delivery has a confirmation time: {path}")
    render_context = envelope.get("render_context")
    if not isinstance(render_context, Mapping):
        raise DailyDecisionBriefStateError(f"daily brief delivery render context is invalid: {path}")
    source_reference = str(envelope.get("source_reference") or "").strip() or None

    revision: int | None
    if source_kind == "successful_brief":
        try:
            revision = _normalize_revision(envelope.get("revision"))
        except ValueError as exc:
            raise DailyDecisionBriefStateError(f"daily brief delivery revision is invalid: {path}: {exc}") from exc
        if source_reference is not None:
            raise DailyDecisionBriefStateError(f"successful daily brief delivery has a failure source: {path}")
        source_brief = _validate_successful_revision_source(
            base=base,
            account=account,
            market=market,
            market_trading_date=market_date,
            revision=revision,
            source_digest=source_digest,
        )
        if not set(candidate_identities).issubset(_candidate_identity_set(source_brief)):
            raise DailyDecisionBriefStateError(
                f"daily brief delivery candidates are absent from the source revision: {path}"
            )
        try:
            _validate_optional_combo_render_context(
                render_context,
                brief=source_brief,
                candidate_identities=candidate_identities,
            )
        except ValueError as exc:
            raise DailyDecisionBriefStateError(
                f"daily brief delivery Combo evidence is invalid: {path}: {exc}"
            ) from exc
    else:
        if envelope.get("revision") is not None or not source_reference:
            raise DailyDecisionBriefStateError(f"scan failure delivery source is invalid: {path}")
        revision = None

    target = envelope.get("scheduled_target_market")
    delivery_key = str(envelope.get("delivery_key") or "").strip()
    if expected_candidate:
        if target not in (None, "") or not candidate_identities:
            raise DailyDecisionBriefStateError(f"candidate delivery target or identities are invalid: {path}")
        target_norm = None
        expected_key = _candidate_delivery_key(
            market=market,
            market_date=market_date,
            account=account,
            candidate_identities=candidate_identities,
        )
    else:
        try:
            target_norm = _normalize_scheduled_target(target, market_date=market_date)
        except ValueError as exc:
            raise DailyDecisionBriefStateError(f"fixed delivery target is invalid: {path}: {exc}") from exc
        if target_norm != expected_target:
            raise DailyDecisionBriefStateError(f"fixed delivery target does not match its state key: {path}")
        expected_key = _fixed_delivery_key(
            market=market,
            market_date=market_date,
            account=account,
            scheduled_target_market=target_norm,
        )
    if delivery_key != expected_key:
        raise DailyDecisionBriefStateError(f"daily brief delivery key mismatch: {path}")
    return {
        "status": status,
        "delivery_kind": delivery_kind,
        "source_kind": source_kind,
        "revision": revision,
        "source_digest": source_digest,
        "source_reference": source_reference,
        "delivery_key": delivery_key,
        "rendered_message": rendered_message,
        "message_sha256": message_sha256,
        "rendered_transport": rendered_transport,
        "rendered_transport_sha256": rendered_transport_sha256,
        "candidate_identities": candidate_identities,
        "scheduled_target_market": target_norm,
        "render_context": dict(render_context),
        "first_prepared_at_utc": first_prepared,
        "last_attempt_at_utc": last_attempt,
        "confirmed_at_utc": confirmed_at,
    }


def _normalize_legacy_confirmation(
    raw: Any,
    *,
    base: Path,
    path: Path,
    account: str,
    market: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DailyDecisionBriefStateError(f"legacy daily brief confirmation is invalid: {path}")
    try:
        market_date = _normalize_market_date(raw.get("market_trading_date"))
        revision = _normalize_revision(raw.get("revision"))
        brief_digest = _normalize_sha256(raw.get("brief_digest"), field="brief_digest")
        confirmed_at = _normalize_required_utc_iso(raw.get("confirmed_at_utc"), field="confirmed_at_utc")
    except ValueError as exc:
        raise DailyDecisionBriefStateError(f"legacy daily brief confirmation is invalid: {path}: {exc}") from exc
    delivery_kind = str(raw.get("delivery_kind") or "").strip().lower()
    delivery_key = str(raw.get("delivery_key") or "").strip()
    if delivery_kind not in {"full", "delta"} or not delivery_key:
        raise DailyDecisionBriefStateError(f"legacy daily brief confirmation metadata is invalid: {path}")
    _validate_successful_revision_source(
        base=base,
        account=account,
        market=market,
        market_trading_date=market_date,
        revision=revision,
        source_digest=brief_digest,
    )
    return {
        "market_trading_date": market_date,
        "revision": revision,
        "delivery_kind": delivery_kind,
        "delivery_key": delivery_key,
        "brief_digest": brief_digest,
        "confirmed_at_utc": confirmed_at,
    }


def _validate_optional_combo_render_context(
    context: Mapping[str, Any],
    *,
    brief: Mapping[str, Any],
    candidate_identities: list[str] | tuple[str, ...],
) -> None:
    occurrence_raw = context.get("candidate_occurrence_ids")
    exposure_raw = context.get("candidate_exposure_ids")
    rendered_identity_raw = context.get("rendered_combo_candidate_identities")
    if occurrence_raw is None and exposure_raw is None:
        return
    if (
        not isinstance(occurrence_raw, list)
        or not isinstance(exposure_raw, list)
        or not isinstance(rendered_identity_raw, list)
    ):
        raise ValueError("Combo evidence IDs must be lists")
    rendered_identities = sorted(
        {
            str(item).strip()
            for item in rendered_identity_raw
            if str(item).strip()
        }
    )
    if not set(rendered_identities).issubset(set(candidate_identities)):
        raise ValueError("rendered Combo identities must be delivered candidate identities")
    expected = combo_exposure_render_context(
        derive_combo_candidate_exposures(
            brief,
            candidate_identities=rendered_identities,
        )
    )
    observed = {
        "candidate_occurrence_ids": sorted(
            {str(item).strip() for item in occurrence_raw if str(item).strip()}
        ),
        "candidate_exposure_ids": sorted(
            {str(item).strip() for item in exposure_raw if str(item).strip()}
        ),
    }
    if observed != expected:
        raise ValueError("Combo evidence IDs do not match the frozen Brief revision")


@contextmanager
def _locked_delivery_envelope(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    delivery_key: str,
    source_digest: str,
    message_sha256: str,
    transport_idempotency_key: str,
) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]]:
    base_path = Path(base).resolve()
    account_norm = _normalize_account(account)
    market_norm = _normalize_market(market)
    date_norm = _normalize_market_date(market_trading_date)
    key_norm = str(delivery_key or "").strip()
    digest_norm = _normalize_sha256(source_digest, field="source_digest")
    message_digest_norm = _normalize_sha256(message_sha256, field="message_sha256")
    from src.application.notification_delivery_adapter import build_notification_transport_key

    if str(transport_idempotency_key or "").strip() != build_notification_transport_key(key_norm):
        raise DailyDecisionBriefStateError("daily brief provider idempotency key mismatch")
    delivery_path = _delivery_path(base_path, account_norm, market_norm)
    lock_path = paths.account_state_dir(base_path, account_norm) / f"daily_decision_brief.{market_norm}.lock"
    with _exclusive_lock(lock_path):
        raw = _read_json_strict(delivery_path)
        if raw is _MISSING:
            raise DailyDecisionBriefStateError(f"daily brief delivery state is missing: {delivery_path}")
        state = _normalize_delivery_state(
            raw,
            base=base_path,
            path=delivery_path,
            account=account_norm,
            market=market_norm,
        )
        day = state["days"].get(date_norm)
        if not day:
            raise DailyDecisionBriefStateError("daily brief delivery day is missing")
        envelope = next(
            (item for item in day["fixed_reports"].values() if item["delivery_key"] == key_norm),
            None,
        )
        candidate = day.get("candidate_delivery")
        if envelope is None and isinstance(candidate, Mapping) and candidate.get("delivery_key") == key_norm:
            envelope = candidate
        if envelope is None:
            envelope = next(
                (
                    item
                    for item in day.get("candidate_delivery_history") or []
                    if item.get("delivery_key") == key_norm
                ),
                None,
            )
        if envelope is None:
            raise DailyDecisionBriefStateError("daily brief delivery key does not reference a prepared envelope")
        if envelope["source_digest"] != digest_norm:
            raise DailyDecisionBriefStateError("daily brief delivery source digest mismatch")
        if envelope["message_sha256"] != message_digest_norm:
            raise DailyDecisionBriefStateError("daily brief delivery message digest mismatch")
        yield state, day, envelope, delivery_path


def _validate_successful_revision_source(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    revision: int,
    source_digest: str,
) -> dict[str, Any]:
    revision_path = _revision_path(base, account, market, market_trading_date, revision)
    raw = _read_json_strict(revision_path)
    if raw is _MISSING:
        raise DailyDecisionBriefStateError(f"daily brief delivery references a missing revision: {revision_path}")
    brief = _normalize_persisted_brief(raw, path=revision_path, account=account, market=market)
    if brief["market_trading_date"] != market_trading_date or int(brief["revision"]) != revision:
        raise DailyDecisionBriefStateError(f"daily brief delivery revision identity mismatch: {revision_path}")
    if source_digest not in daily_brief_compatible_digests(raw):
        raise DailyDecisionBriefStateError(f"daily brief delivery source digest mismatch: {revision_path}")
    return brief


def _resolve_prepared_envelope(
    *,
    existing: Any,
    candidate: dict[str, Any],
    allow_pending_upgrade: bool,
    preserve_attempt_metadata: bool,
) -> tuple[dict[str, Any], bool]:
    if existing is None:
        return candidate, True
    if not isinstance(existing, Mapping):
        raise DailyDecisionBriefStateError("existing daily brief delivery envelope is invalid")
    current = dict(existing)
    status = str(current.get("status") or "")
    if _delivery_envelope_content(current) == _delivery_envelope_content(candidate):
        return current, False
    if status == "ambiguous":
        raise DailyDecisionBriefStateError("ambiguous daily brief delivery envelope is frozen")
    if status in {"confirmed", "expired_unconfirmed"}:
        return current, False
    if status != "pending" or not allow_pending_upgrade:
        raise DailyDecisionBriefStateError("pending daily brief delivery envelope cannot change content")
    updated = dict(candidate)
    if preserve_attempt_metadata:
        updated["first_prepared_at_utc"] = current.get("first_prepared_at_utc")
        updated["last_attempt_at_utc"] = current.get("last_attempt_at_utc")
    return updated, True


def _delivery_envelope_content(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: envelope.get(key)
        for key in (
            "delivery_kind",
            "source_kind",
            "revision",
            "source_digest",
            "source_reference",
            "delivery_key",
            "rendered_message",
            "message_sha256",
            "rendered_transport",
            "rendered_transport_sha256",
            "candidate_identities",
            "scheduled_target_market",
            "render_context",
        )
    }


def _build_v2_state_from_legacy_pointer(
    *,
    base: Path,
    pointer: Mapping[str, Any],
    pointer_path: Path,
) -> dict[str, Any]:
    account = str(pointer["account"])
    market = str(pointer["market"])
    market_date = str(pointer["market_trading_date"])
    revision = int(pointer["revision"])
    revision_path = _revision_path(base, account, market, market_date, revision)
    raw = _read_json_strict(revision_path)
    if raw is _MISSING:
        raise DailyDecisionBriefStateError(f"daily brief delivery pointer references a missing revision: {revision_path}")
    brief = _normalize_persisted_brief(raw, path=revision_path, account=account, market=market)
    if brief["market_trading_date"] != market_date or int(brief["revision"]) != revision:
        raise DailyDecisionBriefStateError(f"daily brief delivery pointer revision identity mismatch: {revision_path}")
    recomputed_digest = daily_brief_digest(brief)
    delivery_kind = str(pointer.get("delivery_kind") or "").strip().lower()
    delivery_key = str(pointer.get("delivery_key") or "").strip()
    try:
        confirmed_at = _normalize_required_utc_iso(pointer.get("confirmed_at_utc"), field="confirmed_at_utc")
    except ValueError as exc:
        raise DailyDecisionBriefStateError(f"daily brief delivery confirmation time is invalid: {pointer_path}: {exc}") from exc
    if delivery_kind == "full":
        expected_prefix = f"daily-brief:{market}:{market_date}:{account}:full:"
        if not _legacy_delivery_key_has_digests(delivery_key, prefix=expected_prefix, count=1):
            raise DailyDecisionBriefStateError(f"legacy daily brief delivery key mismatch: {pointer_path}")
        alerted_ids = _candidate_identity_set(brief)
    elif delivery_kind == "delta":
        expected_prefix = f"daily-brief:{market}:{market_date}:{account}:from:"
        if not _legacy_delivery_key_has_digests(delivery_key, prefix=expected_prefix, count=2):
            raise DailyDecisionBriefStateError(f"legacy daily brief delivery key mismatch: {pointer_path}")
        alerted_ids = _legacy_delta_candidate_identities(base=base, brief=brief)
    else:
        raise DailyDecisionBriefStateError(f"legacy daily brief delivery kind is invalid: {pointer_path}")

    state = _empty_delivery_state(account=account, market=market)
    day = _delivery_day(state, market_date)
    day["alerted_candidates"] = {
        identity: {
            "revision": revision,
            "brief_digest": recomputed_digest,
            "delivery_key": delivery_key,
            "confirmed_at_utc": confirmed_at,
            "via": "legacy_delivery",
        }
        for identity in sorted(alerted_ids)
    }
    state["legacy_last_confirmation"] = {
        "market_trading_date": market_date,
        "revision": revision,
        "delivery_kind": delivery_kind,
        "delivery_key": delivery_key,
        "brief_digest": recomputed_digest,
        "confirmed_at_utc": confirmed_at,
    }
    verified = _normalize_delivery_state(
        state,
        base=base,
        path=pointer_path,
        account=account,
        market=market,
    )
    legacy_digest = str(pointer.get("brief_digest") or "").strip()
    return {
        "target_schema_version": DELIVERY_STATE_SCHEMA_VERSION,
        "recomputed_brief_digest": recomputed_digest,
        "legacy_brief_digest_matches_revision": legacy_digest == recomputed_digest,
        "alerted_candidate_identities": sorted(alerted_ids),
        "target_state": verified,
    }


def _legacy_delta_candidate_identities(*, base: Path, brief: Mapping[str, Any]) -> set[str]:
    run_id = str(brief.get("run_id") or "").strip()
    if not run_id:
        return set()
    diff_path = (
        paths.run_account_state_dir(base, run_id, str(brief["account"]))
        / f"daily_decision_brief_diff.{brief['market']}.json"
    )
    try:
        raw = _read_json_strict(diff_path)
    except DailyDecisionBriefStateError:
        return set()
    if not isinstance(raw, Mapping) or raw.get("schema_version") != DAILY_DECISION_BRIEF_DIFF_SCHEMA_VERSION:
        return set()
    try:
        to_revision = _normalize_revision(raw.get("to_revision"))
    except ValueError:
        return set()
    if (
        str(raw.get("account") or "").strip().lower() != brief["account"]
        or str(raw.get("market") or "").strip().upper() != brief["market"]
        or str(raw.get("market_trading_date") or "").strip() != brief["market_trading_date"]
        or to_revision != int(brief["revision"])
    ):
        return set()
    eligible = _candidate_identity_set(brief)
    out: set[str] = set()
    for change in raw.get("changes") or []:
        if not isinstance(change, Mapping) or change.get("change_type") != "candidate_added":
            continue
        action = change.get("action")
        if not isinstance(action, Mapping):
            continue
        try:
            identity = build_daily_brief_candidate_identity(
                account=brief["account"],
                market=brief["market"],
                symbol=action.get("symbol"),
                strategy_family=action.get("strategy_family"),
            )
        except ValueError:
            continue
        if identity in eligible:
            out.add(identity)
    return out


def _fixed_delivery_key(
    *,
    market: str,
    market_date: str,
    account: str,
    scheduled_target_market: str,
) -> str:
    return f"option-report:{market}:{market_date}:{account}:{scheduled_target_market}"


def _legacy_delivery_key_has_digests(value: str, *, prefix: str, count: int) -> bool:
    if not value.startswith(prefix):
        return False
    parts = value[len(prefix) :].split(":")
    return len(parts) == count and all(_SHA256_RE.fullmatch(part) for part in parts)


def _candidate_delivery_key(
    *,
    market: str,
    market_date: str,
    account: str,
    candidate_identities: list[str],
) -> str:
    raw = json.dumps(sorted(candidate_identities), ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"option-candidates:{market}:{market_date}:{account}:{digest}"


def _normalize_candidate_identities(
    values: Any,
    *,
    account: str,
    market: str,
) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("candidate_identities must be a collection")
    out: set[str] = set()
    for raw in values:
        identity = str(raw or "").strip()
        match = _CANDIDATE_IDENTITY_RE.fullmatch(identity)
        if not match or match.group("account") != account or match.group("market") != market:
            raise ValueError(f"candidate identity is incompatible: {identity!r}")
        try:
            rebuilt = build_daily_brief_candidate_identity(
                account=match.group("account"),
                market=match.group("market"),
                symbol=match.group("symbol"),
                strategy_family=match.group("family"),
            )
        except ValueError as exc:
            raise ValueError(f"candidate identity is incompatible: {identity!r}") from exc
        if rebuilt != identity:
            raise ValueError(f"candidate identity is not canonical: {identity!r}")
        out.add(identity)
    return sorted(out)


def _validate_failure_source_reference(
    *,
    base: Path,
    source_reference: str,
    source_digest: str,
) -> str:
    candidate = Path(source_reference)
    path = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        relative = path.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError("source_reference must stay under the runtime root") from exc
    if not path.is_file():
        raise DailyDecisionBriefStateError(f"scan failure source artifact is missing: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != source_digest:
        raise DailyDecisionBriefStateError(f"scan failure source digest mismatch: {path}")
    return relative.as_posix()


def _normalize_scheduled_target(value: Any, *, market_date: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("scheduled_target_market is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled_target_market must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.date().isoformat() != market_date:
        raise ValueError("scheduled_target_market must be timezone-aware and match market_trading_date")
    return parsed.isoformat()


def _normalize_sha256(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return text


def _normalize_run_id(value: Any) -> str:
    run_id = str(value or "").strip()
    if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a non-empty path-safe identifier")
    return run_id


def _normalize_required_utc_iso(value: Any, *, field: str) -> str:
    if value in (None, ""):
        raise ValueError(f"{field} is required")
    return _normalize_optional_utc_iso(value, field=field) or ""


def _normalize_optional_utc_iso(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _delivery_backup_path(pointer_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return pointer_path.with_name(f"{pointer_path.name}.v1.backup.{stamp}")


def _normalize_delivery_pointer(raw: Any, *, path: Path, account: str, market: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DailyDecisionBriefStateError(f"daily brief delivery state is not an object: {path}")
    pointer = dict(raw)
    if pointer.get("schema_version") != DELIVERY_POINTER_SCHEMA_VERSION:
        raise DailyDecisionBriefStateError(f"unsupported daily brief delivery schema: {path}")
    try:
        pointer_account = _normalize_account(pointer.get("account"))
        pointer_market = _normalize_market(pointer.get("market"))
        pointer_date = _normalize_market_date(pointer.get("market_trading_date"))
        pointer_revision = _normalize_revision(pointer.get("revision"))
    except ValueError as exc:
        raise DailyDecisionBriefStateError(f"daily brief delivery state is incompatible: {path}: {exc}") from exc
    if pointer_account != account or pointer_market != market:
        raise DailyDecisionBriefStateError(f"daily brief delivery identity mismatch: {path}")
    pointer["account"] = account
    pointer["market"] = market
    pointer["market_trading_date"] = pointer_date
    pointer["revision"] = pointer_revision
    pointer["brief_digest"] = str(pointer.get("brief_digest") or "").strip()
    if not pointer["brief_digest"]:
        raise DailyDecisionBriefStateError(f"daily brief delivery digest is missing: {path}")
    return pointer


def _prepared_delivery_expectation(*, base: Path, brief: Mapping[str, Any]) -> dict[str, str]:
    account = str(brief["account"])
    market = str(brief["market"])
    market_date = str(brief["market_trading_date"])
    revision = int(brief["revision"])
    run_id = str(brief.get("run_id") or "").strip()
    if not run_id:
        raise DailyDecisionBriefStateError("persisted daily brief run_id is missing")

    diff_path = (
        paths.run_account_state_dir(base, run_id, account)
        / f"daily_decision_brief_diff.{market}.json"
    )
    raw = _read_json_strict(diff_path)
    if raw is _MISSING or not isinstance(raw, Mapping):
        raise DailyDecisionBriefStateError(f"prepared daily brief diff is unavailable: {diff_path}")
    diff = dict(raw)
    if diff.get("schema_version") != DAILY_DECISION_BRIEF_DIFF_SCHEMA_VERSION:
        raise DailyDecisionBriefStateError(f"prepared daily brief diff schema is incompatible: {diff_path}")
    try:
        diff_to_revision = _normalize_revision(diff.get("to_revision"))
    except ValueError as exc:
        raise DailyDecisionBriefStateError(f"prepared daily brief diff revision is invalid: {diff_path}") from exc
    if (
        str(diff.get("market") or "").strip().upper() != market
        or str(diff.get("market_trading_date") or "").strip() != market_date
        or str(diff.get("account") or "").strip().lower() != account
        or diff_to_revision != revision
    ):
        raise DailyDecisionBriefStateError(f"prepared daily brief diff identity mismatch: {diff_path}")

    from_revision = diff.get("from_revision")
    if from_revision is None:
        full_semantic_digest = str(diff.get("full_semantic_digest") or "").strip()
        if not _SHA256_RE.fullmatch(full_semantic_digest):
            raise DailyDecisionBriefStateError(
                f"prepared daily brief full semantic digest is invalid: {diff_path}"
            )
        if full_semantic_digest != _full_brief_semantic_digest(brief):
            raise DailyDecisionBriefStateError(
                f"prepared daily brief full semantic digest mismatch: {diff_path}"
            )
        return {
            "delivery_kind": "full",
            "delivery_key": _full_delivery_key(
                market=market,
                market_date=market_date,
                account=account,
                semantic_digest=full_semantic_digest,
            ),
        }
    try:
        from_revision_norm = _normalize_revision(from_revision)
    except ValueError as exc:
        raise DailyDecisionBriefStateError(f"prepared daily brief base revision is invalid: {diff_path}") from exc
    if from_revision_norm >= revision or not bool(diff.get("material")):
        raise DailyDecisionBriefStateError(f"prepared daily brief delta is not material: {diff_path}")
    material_digest = str(diff.get("material_diff_digest") or "").strip()
    if not material_digest:
        raise DailyDecisionBriefStateError(f"prepared daily brief delta digest is missing: {diff_path}")

    from_path = _revision_path(base, account, market, market_date, from_revision_norm)
    from_raw = _read_json_strict(from_path)
    if from_raw is _MISSING:
        raise DailyDecisionBriefStateError(f"prepared daily brief base revision is missing: {from_path}")
    from_brief = _normalize_persisted_brief(from_raw, path=from_path, account=account, market=market)
    from_digest = daily_brief_digest(from_brief)
    return {
        "delivery_kind": "delta",
        "delivery_key": (
            f"daily-brief:{market}:{market_date}:{account}:from:{from_digest}:{material_digest}"
        ),
    }


def _validate_current_revision(*, base: Path, current: Mapping[str, Any], current_path: Path) -> None:
    revision_path = _revision_path(
        base,
        str(current["account"]),
        str(current["market"]),
        str(current["market_trading_date"]),
        int(current["revision"]),
    )
    raw = _read_json_strict(revision_path)
    if raw is _MISSING:
        raise DailyDecisionBriefStateError(
            f"daily brief current state references a missing revision: {revision_path}"
        )
    revision = _normalize_persisted_brief(
        raw,
        path=revision_path,
        account=str(current["account"]),
        market=str(current["market"]),
    )
    if daily_brief_digest(revision) != daily_brief_digest(current):
        raise DailyDecisionBriefStateError(f"daily brief current state digest mismatch: {current_path}")


def _load_current_index(path: Path) -> dict[str, Any]:
    raw = _read_json_strict(path)
    if raw is _MISSING:
        return {"schema_version": CURRENT_INDEX_SCHEMA_VERSION, "updated_at_utc": "", "items": {}}
    if not isinstance(raw, Mapping) or raw.get("schema_version") != CURRENT_INDEX_SCHEMA_VERSION:
        raise DailyDecisionBriefStateError(f"unsupported daily brief current index: {path}")
    items = raw.get("items")
    if not isinstance(items, Mapping):
        raise DailyDecisionBriefStateError(f"daily brief current index items are invalid: {path}")
    out = dict(raw)
    out["items"] = dict(items)
    return out


def _initial_full_diff(
    brief: Mapping[str, Any],
    *,
    full_semantic_digest: str,
) -> dict[str, Any]:
    canonical = {
        "change_type": "full_required",
        "market": brief["market"],
        "market_trading_date": brief["market_trading_date"],
        "account": brief["account"],
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": DAILY_DECISION_BRIEF_DIFF_SCHEMA_VERSION,
        "brief_id": brief["brief_id"],
        "market": brief["market"],
        "market_trading_date": brief["market_trading_date"],
        "account": brief["account"],
        "from_revision": None,
        "to_revision": brief["revision"],
        "material": True,
        "changes": [{"change_type": "full_required", "priority": "P0", "material": True}],
        "material_diff_digest": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "full_semantic_digest": full_semantic_digest,
    }


def _full_delivery_key(
    *,
    market: str,
    market_date: str,
    account: str,
    semantic_digest: str,
) -> str:
    return f"daily-brief:{market}:{market_date}:{account}:full:{semantic_digest}"


def _full_brief_semantic_digest(brief: Mapping[str, Any]) -> str:
    """Digest full-brief semantics while excluding revision and display/audit noise."""

    normalized = normalize_daily_decision_brief(brief)
    semantic = dict(normalized)
    semantic.update(
        {
            "revision": 0,
            "run_id": "",
            "generated_at_utc": "",
            "data_as_of_utc": "",
            "strategy_summary": "",
            "source_artifacts": [],
        }
    )
    semantic["actions"] = [
        {
            key: value
            for key, value in action.items()
            if key not in {"title", "reason", "source"}
        }
        for action in normalized["actions"]
    ]
    for field in ("positions", "candidates", "events"):
        semantic[field] = _without_source_provenance(normalized[field])
    return daily_brief_digest(semantic)


def _candidate_identity_set(brief: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(brief, Mapping):
        return set()
    identities = {
        str(item.get("identity") or "").strip()
        for item in brief.get("candidate_index") or []
        if isinstance(item, Mapping) and str(item.get("identity") or "").strip()
    }
    if identities or brief.get("actionability") == "blocked":
        return identities
    for action in brief.get("actions") or []:
        if not isinstance(action, Mapping):
            continue
        if action.get("state") != "active" or action.get("priority") not in {"P0", "P1"}:
            continue
        if action.get("action_type") not in {"open_candidate", "open_combo_yield"}:
            continue
        metrics = action.get("metrics")
        capacity = metrics.get("capacity") if isinstance(metrics, Mapping) else None
        try:
            contracts = int(float(capacity.get("contracts_available"))) if isinstance(capacity, Mapping) else 0
        except (TypeError, ValueError, OverflowError):
            contracts = 0
        if contracts < 1:
            continue
        try:
            identities.add(
                build_daily_brief_candidate_identity(
                    account=brief.get("account"),
                    market=brief.get("market"),
                    symbol=action.get("symbol"),
                    strategy_family=action.get("strategy_family"),
                )
            )
        except ValueError:
            continue
    return identities


def _without_source_provenance(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_source_provenance(item)
            for key, item in value.items()
            if str(key) != "source"
        }
    if isinstance(value, list):
        return [_without_source_provenance(item) for item in value]
    if isinstance(value, tuple):
        return [_without_source_provenance(item) for item in value]
    return value


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_json_strict(path: Path) -> Any:
    if not path.exists():
        return _MISSING
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DailyDecisionBriefStateError(f"failed to read daily brief state: {path}: {exc}") from exc


def _normalize_market(value: Any) -> str:
    market = str(value or "").strip().upper()
    if not market or not _MARKET_RE.fullmatch(market):
        raise ValueError("market must be a non-empty path-safe identifier")
    return market


def _normalize_account(value: Any) -> str:
    account = str(value or "").strip().lower()
    if not account or "/" in account or "\\" in account or account in {".", ".."}:
        raise ValueError("account must be a non-empty path-safe identifier")
    return account


def _normalize_market_date(value: Any) -> str:
    text = str(value or "").strip()
    if not _DATE_RE.fullmatch(text):
        raise ValueError("market_trading_date must use YYYY-MM-DD")
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("market_trading_date must be a valid date") from exc
    return text


def _normalize_revision(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("revision must be a non-negative integer")
    try:
        revision = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("revision must be a non-negative integer") from exc
    if revision < 0:
        raise ValueError("revision must be a non-negative integer")
    return revision


def _coerce_utc_iso(value: datetime | str | None) -> str:
    if value is None:
        return _utc_now_iso()
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("confirmed_at_utc must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative_path(base: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


__all__ = [
    "CURRENT_INDEX_SCHEMA_VERSION",
    "DELIVERY_STATE_SCHEMA_VERSION",
    "DELIVERY_POINTER_SCHEMA_VERSION",
    "LEGACY_DELIVERY_POINTER_SCHEMA_VERSION",
    "DailyDecisionBriefStateError",
    "confirm_daily_decision_brief_delivery",
    "inspect_daily_decision_brief_delivery",
    "list_daily_decision_brief_revisions",
    "migrate_daily_decision_brief_delivery",
    "persist_daily_decision_brief_success",
    "prepare_daily_decision_brief",
    "prepare_daily_decision_brief_delivery",
    "reconcile_daily_decision_brief_delivery_resolution",
    "read_daily_decision_brief",
    "read_daily_decision_brief_delivery",
    "read_daily_decision_brief_delivery_state",
    "read_combo_candidate_exposures",
    "read_latest_daily_decision_brief",
    "read_retryable_daily_decision_brief_delivery",
    "record_daily_decision_brief_candidates",
    "validate_daily_decision_brief_delivery_identity",
]
