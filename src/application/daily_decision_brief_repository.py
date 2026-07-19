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
    daily_brief_digest,
    diff_daily_decision_briefs,
    normalize_daily_decision_brief,
)
from domain.storage import paths
from domain.storage.json_io import atomic_write_json


CURRENT_INDEX_SCHEMA_VERSION = "daily_decision_brief_current_index.v1"
DELIVERY_POINTER_SCHEMA_VERSION = "daily_decision_brief_delivery.v1"

_MARKET_RE = re.compile(r"^[A-Z0-9_-]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REVISION_RE = re.compile(r"\.r(?P<revision>\d{4})\.json$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


class DailyDecisionBriefStateError(RuntimeError):
    """Raised when persisted Daily Decision Brief state is unsafe to infer from."""


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
    "DELIVERY_POINTER_SCHEMA_VERSION",
    "DailyDecisionBriefStateError",
    "confirm_daily_decision_brief_delivery",
    "list_daily_decision_brief_revisions",
    "prepare_daily_decision_brief",
    "read_daily_decision_brief",
    "read_daily_decision_brief_delivery",
    "read_latest_daily_decision_brief",
]
