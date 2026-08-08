from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)


COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA = "combo_yield_candidate_snapshot.v1"
COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE = "combo_yield_candidate_snapshot.json"
COMBO_YIELD_OPENING_STATUSES = frozenset(
    {
        "candidates_found",
        "no_candidate",
        "data_unavailable",
        "partial_data",
        "market_closed",
        "not_applicable",
    }
)


class ComboYieldCandidateSnapshotError(RuntimeError):
    """Raised when a Combo Yield candidate snapshot cannot be trusted."""


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ComboYieldCandidateSnapshotError(f"{field} is required")
    return text


def _sha256(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ComboYieldCandidateSnapshotError(f"{field} is invalid")
    return text


def _timestamp(value: datetime | str | Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _required_text(value, "sealed_at_utc")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ComboYieldCandidateSnapshotError(
                "sealed_at_utc is invalid"
            ) from exc
    if parsed.tzinfo is None:
        raise ComboYieldCandidateSnapshotError(
            "sealed_at_utc must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
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


def _pairs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ComboYieldCandidateSnapshotError(
            "combo ranked_pairs must be a list"
        )
    out: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise ComboYieldCandidateSnapshotError(
                "combo ranked pair must be an object"
            )
        pair = dict(raw)
        if not str(pair.get("candidate_pair_id") or "").strip():
            raise ComboYieldCandidateSnapshotError(
                "combo ranked pair identity is missing"
            )
        out.append(pair)
    return out


def _pair_ids(pairs: Iterable[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pair in pairs:
        pair_id = str(pair.get("candidate_pair_id") or "").strip()
        if pair_id and pair_id not in seen:
            seen.add(pair_id)
            out.append(pair_id)
    return out


def validate_combo_yield_candidate_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
) -> None:
    item = dict(payload or {})
    if item.get("schema_version") != COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA:
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate snapshot schema mismatch"
        )
    if item.get("run_id") != expected_run_id:
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate snapshot run mismatch"
        )
    if item.get("account") != expected_account:
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate snapshot account mismatch"
        )
    for field in ("account_config_sha256", "strategy_policy_sha256", "content_sha256"):
        _sha256(item.get(field), field)
    content_hash = str(item["content_sha256"])
    content = {key: value for key, value in item.items() if key != "content_sha256"}
    if canonical_sha256(content) != content_hash:
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate snapshot content hash mismatch"
        )
    _timestamp(item.get("sealed_at_utc"))
    if item.get("opening_status") not in COMBO_YIELD_OPENING_STATUSES:
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate snapshot status is invalid"
        )
    _required_text(item.get("market"), "market")
    _pairs(item.get("ranked_pairs") or [])
    reject_reasons = item.get("reject_reasons") or []
    if not isinstance(reject_reasons, list) or any(
        not isinstance(reason, dict) for reason in reject_reasons
    ):
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate reject_reasons is invalid"
        )


def seal_combo_yield_candidate_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    account_config_sha256: str,
    strategy_policy_sha256: str,
    ranked_pairs: Iterable[Mapping[str, Any]],
    reject_reasons: Iterable[Mapping[str, Any]] = (),
    opening_status: str | None = None,
    sealed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Assemble, validate, and immutably publish one account-run Combo snapshot."""

    run_id_norm = _required_text(run_id, "run_id")
    account_norm = _required_text(account, "account").lower()
    market_norm = _required_text(market, "market").lower()
    account_config_hash = _sha256(account_config_sha256, "account_config_sha256")
    policy_hash = _sha256(strategy_policy_sha256, "strategy_policy_sha256")
    pairs = _pairs(list(ranked_pairs))
    rejects = [dict(item) for item in reject_reasons if isinstance(item, Mapping)]
    resolved_status = str(opening_status or "").strip().lower()
    if not resolved_status:
        resolved_status = "candidates_found" if pairs else "no_candidate"
    if resolved_status not in COMBO_YIELD_OPENING_STATUSES:
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate snapshot status is invalid"
        )
    seal_time = _timestamp(sealed_at or datetime.now(timezone.utc))

    payload: dict[str, Any] = {
        "schema_version": COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
        "run_id": run_id_norm,
        "account": account_norm,
        "market": market_norm,
        "account_config_sha256": account_config_hash,
        "strategy_policy_sha256": policy_hash,
        "sealed_at_utc": seal_time,
        "opening_status": resolved_status,
        "ranked_pairs": pairs,
        "reject_reasons": rejects,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    validate_combo_yield_candidate_snapshot(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
    )
    encoded = _canonical_json_bytes(payload)
    try:
        write_account_run_state_bytes_once_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
            payload=encoded,
        )
    except AccountRunConfigError as exc:
        raise ComboYieldCandidateSnapshotError(
            "terminal combo yield candidate snapshot conflicts or cannot be published"
        ) from exc
    return payload


def load_combo_yield_candidate_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    run_id_norm = _required_text(run_id, "run_id")
    account_norm = _required_text(account, "account").lower()
    try:
        encoded = read_account_run_state_bytes_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
        )
        payload = json.loads(encoded.decode("utf-8"))
    except Exception as exc:
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate snapshot is unavailable"
        ) from exc
    if not isinstance(payload, dict):
        raise ComboYieldCandidateSnapshotError(
            "combo yield candidate snapshot must be an object"
        )
    validate_combo_yield_candidate_snapshot(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
    )
    return payload


__all__ = [
    "COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE",
    "COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA",
    "ComboYieldCandidateSnapshotError",
    "load_combo_yield_candidate_snapshot",
    "seal_combo_yield_candidate_snapshot",
    "validate_combo_yield_candidate_snapshot",
]
