from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.application.opend_symbol_outputs import SUCCESS_EMPTY_REASON_CODES
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    sha256_text,
)
from src.application.source_receipts import sha256_bytes
from src.infrastructure.io_utils import atomic_write_json


STRATEGY_SCAN_STATUS_SCHEMA = "strategy_scan_status.v1"
STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA = "strategy_scan_status_index.v2"
STRATEGY_SCAN_STATUS_INDEX_V2_FILE = "strategy_scan_status_index.v2.json"
_TERMINAL = frozenset({"completed", "unavailable", "failed", "not_applicable"})
_COMPLETED_REASONS = frozenset({"no_candidate", "partial_data", "market_closed"})


class StrategyScanStatusError(RuntimeError):
    pass


def strategy_status_path(
    *,
    report_dir: Path,
    symbol: str,
    strategy_family: str,
) -> Path:
    symbol_key = str(symbol or "").strip().lower()
    family = str(strategy_family or "").strip().lower()
    return (Path(report_dir) / f"{symbol_key}_{family}_scan_status.json").resolve()


def publish_strategy_scan_status(
    *,
    report_dir: Path,
    run_id: str,
    account: str,
    market: str,
    symbol: str,
    strategy_family: str,
    status: str,
    candidate_count: int | None = None,
    reason: str | None = None,
    snapshot_id: str | None = None,
    receipt_relpath: str | None = None,
    source_outcome: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    status_norm = str(status or "").strip().lower()
    if status_norm not in _TERMINAL:
        raise StrategyScanStatusError("strategy scan status is not terminal")
    if status_norm == "completed" and candidate_count is None:
        raise StrategyScanStatusError("completed status requires candidate_count")
    if status_norm != "completed" and not str(reason or "").strip():
        raise StrategyScanStatusError("non-completed status requires reason")
    source_outcome_norm = str(source_outcome or "").strip()
    reason_code_norm = str(reason_code or "").strip()
    if source_outcome_norm or reason_code_norm:
        if (
            status_norm != "completed"
            or int(candidate_count or 0) != 0
            or source_outcome_norm != "success_empty"
            or reason_code_norm not in SUCCESS_EMPTY_REASON_CODES
            or not str(snapshot_id or "").strip()
            or not str(receipt_relpath or "").strip()
        ):
            raise StrategyScanStatusError(
                "success-empty strategy status evidence is invalid"
            )

    root = Path(report_dir).resolve()
    payload: dict[str, Any] = {
        "schema_version": STRATEGY_SCAN_STATUS_SCHEMA,
        "run_id": _required(run_id, "run_id"),
        "account": _required(account, "account").lower(),
        "market": _required(market, "market").upper(),
        "symbol": _required(symbol, "symbol").upper(),
        "strategy_family": _required(
            strategy_family,
            "strategy_family",
        ).lower(),
        "status": status_norm,
        "published_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if status_norm == "completed":
        payload["candidate_count"] = max(0, int(candidate_count or 0))
        reason_norm = str(reason or "").strip()
        if reason_norm in _COMPLETED_REASONS:
            payload["reason"] = reason_norm
    else:
        payload["reason"] = str(reason).strip()
    if str(snapshot_id or "").strip():
        payload["snapshot_id"] = str(snapshot_id).strip()
    if str(receipt_relpath or "").strip():
        payload["receipt_relpath"] = str(receipt_relpath).strip()
    if source_outcome_norm:
        payload["source_outcome"] = source_outcome_norm
        payload["reason_code"] = reason_code_norm
    path = strategy_status_path(
        report_dir=root,
        symbol=symbol,
        strategy_family=strategy_family,
    )
    atomic_write_json(path, payload)
    return {**payload, "status_path": str(path)}


def publish_strategy_scan_status_index_v2(
    *,
    report_dir: Path,
    run_id: str,
    account: str,
    account_config_sha256: str,
    expected: Iterable[Mapping[str, str]],
) -> dict[str, Any]:
    """Publish the CSV-independent terminal scope index for one account run."""

    root = Path(report_dir).resolve()
    run_id_norm = _required(run_id, "run_id")
    account_norm = _required(account, "account").lower()
    config_hash = _sha256(account_config_sha256, "account_config_sha256")
    expected_rows = [dict(item) for item in expected]
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in sorted(
        expected_rows,
        key=lambda item: (
            str(item.get("market") or ""),
            str(item.get("symbol") or ""),
            str(item.get("strategy_family") or ""),
        ),
    ):
        market = _required(raw.get("market"), "market").upper()
        symbol = _required(raw.get("symbol"), "symbol").upper()
        family = _required(raw.get("strategy_family"), "strategy_family").lower()
        mode = _required(raw.get("strategy_mode"), "strategy_mode").lower()
        owner = _required(raw.get("candidate_owner"), "candidate_owner").lower()
        expected_owner, expected_mode = _family_owner_mode(family, owner=owner)
        if owner != expected_owner or mode != expected_mode:
            raise StrategyScanStatusError(
                f"strategy scope owner/mode mismatch: {symbol}:{family}"
            )
        item_config_hash = _sha256(
            raw.get("account_config_sha256"),
            "scope account_config_sha256",
        )
        if item_config_hash != config_hash:
            raise StrategyScanStatusError("strategy scope account config hash mismatch")
        key = (symbol, family)
        if key in seen:
            raise StrategyScanStatusError("strategy status scope is duplicated")
        seen.add(key)
        status_path = strategy_status_path(
            report_dir=root,
            symbol=symbol,
            strategy_family=family,
        )
        status = _validate_status_core(
            path=status_path,
            run_id=run_id_norm,
            account=account_norm,
            market=market,
            symbol=symbol,
            strategy_family=family,
        )
        items.append(
            {
                **status,
                "strategy_mode": mode,
                "candidate_owner": owner,
                "account_config_sha256": config_hash,
                "source_status_path": status_path.relative_to(root).as_posix(),
            }
        )
    counts = {
        status: sum(1 for item in items if item.get("status") == status)
        for status in ("completed", "unavailable", "failed", "not_applicable")
    }
    payload: dict[str, Any] = {
        "schema_version": STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA,
        "run_id": run_id_norm,
        "account": account_norm,
        "account_config_sha256": config_hash,
        "published_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected_count": len(items),
        "counts": counts,
        "items": items,
    }
    payload["content_sha256"] = sha256_bytes(_canonical_index_content(payload))
    path = (root / STRATEGY_SCAN_STATUS_INDEX_V2_FILE).resolve()
    atomic_write_json(path, payload)
    validate_strategy_scan_status_index_v2(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
        expected_account_config_sha256=config_hash,
    )
    return {**payload, "index_path": str(path)}


def load_strategy_scan_status_index_v2(
    path: Path,
    *,
    expected_run_id: str | None = None,
    expected_account: str | None = None,
    expected_account_config_sha256: str | None = None,
) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise StrategyScanStatusError("strategy status v2 index is unavailable")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategyScanStatusError("strategy status v2 index is unreadable") from exc
    if not isinstance(payload, dict):
        raise StrategyScanStatusError("strategy status v2 index is invalid")
    validate_strategy_scan_status_index_v2(
        payload,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=expected_account_config_sha256,
    )
    return payload


def validate_strategy_scan_status_index_v2(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str | None = None,
    expected_account: str | None = None,
    expected_account_config_sha256: str | None = None,
) -> None:
    item = dict(payload or {})
    if item.get("schema_version") != STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA:
        raise StrategyScanStatusError("strategy status v2 index schema mismatch")
    run_id = _required(item.get("run_id"), "run_id")
    account = _required(item.get("account"), "account").lower()
    config_hash = _sha256(item.get("account_config_sha256"), "account_config_sha256")
    if expected_run_id is not None and run_id != expected_run_id:
        raise StrategyScanStatusError("strategy status v2 index run mismatch")
    if expected_account is not None and account != expected_account.lower():
        raise StrategyScanStatusError("strategy status v2 index account mismatch")
    if expected_account_config_sha256 is not None and config_hash != _sha256(
        expected_account_config_sha256,
        "expected account_config_sha256",
    ):
        raise StrategyScanStatusError("strategy status v2 index config mismatch")
    content_hash = _sha256(item.get("content_sha256"), "content_sha256")
    content = {key: value for key, value in item.items() if key != "content_sha256"}
    if sha256_bytes(_canonical_index_content(content)) != content_hash:
        raise StrategyScanStatusError("strategy status v2 index content hash mismatch")
    rows = item.get("items")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise StrategyScanStatusError("strategy status v2 index items are invalid")
    if int(item.get("expected_count", -1)) != len(rows):
        raise StrategyScanStatusError("strategy status v2 index count mismatch")
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        row = dict(raw)
        if "artifacts" in row:
            raise StrategyScanStatusError("strategy status v2 item references compatibility artifacts")
        if row.get("run_id") != run_id or str(row.get("account") or "").lower() != account:
            raise StrategyScanStatusError("strategy status v2 item identity mismatch")
        if _sha256(row.get("account_config_sha256"), "scope account_config_sha256") != config_hash:
            raise StrategyScanStatusError("strategy status v2 item config mismatch")
        symbol = _required(row.get("symbol"), "symbol").upper()
        family = _required(row.get("strategy_family"), "strategy_family").lower()
        owner = _required(row.get("candidate_owner"), "candidate_owner").lower()
        mode = _required(row.get("strategy_mode"), "strategy_mode").lower()
        expected_owner, expected_mode = _family_owner_mode(family, owner=owner)
        if owner != expected_owner or mode != expected_mode:
            raise StrategyScanStatusError("strategy status v2 item owner/mode mismatch")
        if row.get("status") not in _TERMINAL:
            raise StrategyScanStatusError("strategy status v2 item is not terminal")
        if row.get("status") == "completed":
            candidate_count = row.get("candidate_count")
            if (
                not isinstance(candidate_count, int)
                or isinstance(candidate_count, bool)
                or candidate_count < 0
            ):
                raise StrategyScanStatusError(
                    "completed v2 status requires a non-negative integer candidate_count"
                )
        elif not str(row.get("reason") or "").strip():
            raise StrategyScanStatusError("non-completed v2 status requires reason")
        snapshot_id = str(row.get("snapshot_id") or "").strip()
        receipt_relpath = str(row.get("receipt_relpath") or "").strip()
        if bool(snapshot_id) != bool(receipt_relpath):
            raise StrategyScanStatusError(
                "strategy status v2 quote binding is incomplete"
            )
        if row.get("source_status_schema") != STRATEGY_SCAN_STATUS_SCHEMA:
            raise StrategyScanStatusError("strategy status v2 source schema mismatch")
        source_path = Path(_required(row.get("source_status_path"), "source_status_path"))
        if source_path.is_absolute() or ".." in source_path.parts or source_path.suffix != ".json":
            raise StrategyScanStatusError("strategy status v2 source path is invalid")
        key = (symbol, family)
        if key in seen:
            raise StrategyScanStatusError("strategy status v2 scope is duplicated")
        seen.add(key)
    expected_counts = {
        status: sum(1 for row in rows if row.get("status") == status)
        for status in ("completed", "unavailable", "failed", "not_applicable")
    }
    if item.get("counts") != expected_counts:
        raise StrategyScanStatusError("strategy status v2 counts mismatch")


def _canonical_index_content(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _family_owner_mode(family: str, *, owner: str) -> tuple[str, str]:
    if family == "sell_put":
        return "opening", "put"
    if family == "covered_call":
        return "opening", "call"
    if family == "combo_yield" and owner in {"sp_lc", "cc_lp"}:
        return owner, "combo_yield"
    if family == "wheel" and owner == "wheel":
        return "wheel", "wheel"
    raise StrategyScanStatusError(f"unknown strategy family/owner: {family}/{owner}")


def _validate_status_core(
    *,
    path: Path,
    run_id: str,
    account: str,
    market: str,
    symbol: str,
    strategy_family: str,
) -> dict[str, Any]:
    """Validate terminal status facts without opening compatibility artifact bytes."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategyScanStatusError("strategy status is unreadable") from exc
    expected = {
        "schema_version": STRATEGY_SCAN_STATUS_SCHEMA,
        "run_id": run_id,
        "account": account.lower(),
        "market": market.upper(),
        "symbol": symbol.upper(),
        "strategy_family": strategy_family.lower(),
    }
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items()):
        raise StrategyScanStatusError("strategy status identity mismatch")
    status = str(payload.get("status") or "")
    if status not in _TERMINAL:
        raise StrategyScanStatusError("strategy status is not terminal")
    if status == "completed":
        candidate_count = payload.get("candidate_count")
        if (
            not isinstance(candidate_count, int)
            or isinstance(candidate_count, bool)
            or candidate_count < 0
        ):
            raise StrategyScanStatusError(
                "completed status requires a non-negative integer candidate_count"
            )
    if status != "completed" and not str(payload.get("reason") or "").strip():
        raise StrategyScanStatusError("non-completed status requires reason")
    snapshot_id = str(payload.get("snapshot_id") or "").strip()
    receipt_relpath = str(payload.get("receipt_relpath") or "").strip()
    if bool(snapshot_id) != bool(receipt_relpath):
        raise StrategyScanStatusError("strategy status quote binding is incomplete")
    source_outcome = str(payload.get("source_outcome") or "").strip()
    reason_code = str(payload.get("reason_code") or "").strip()
    if source_outcome or reason_code:
        if (
            status != "completed"
            or int(payload.get("candidate_count") or 0) != 0
            or source_outcome != "success_empty"
            or reason_code not in SUCCESS_EMPTY_REASON_CODES
            or not str(payload.get("snapshot_id") or "").strip()
            or not str(payload.get("receipt_relpath") or "").strip()
        ):
            raise StrategyScanStatusError("strategy success-empty evidence is invalid")
    out = {
        key: value
        for key, value in payload.items()
        if key
        in {
            "schema_version",
            "run_id",
            "account",
            "market",
            "symbol",
            "strategy_family",
            "status",
            "candidate_count",
            "reason",
            "snapshot_id",
            "receipt_relpath",
            "source_outcome",
            "reason_code",
        }
    }
    out["source_status_schema"] = out.pop("schema_version")
    return out


def _required(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise StrategyScanStatusError(f"{field} is required")
    return text


def _sha256(value: Any, field: str) -> str:
    try:
        return sha256_text(value, field)
    except CandidateSnapshotContractError as exc:
        raise StrategyScanStatusError(str(exc)) from exc


__all__ = [
    "STRATEGY_SCAN_STATUS_INDEX_V2_FILE",
    "STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA",
    "STRATEGY_SCAN_STATUS_SCHEMA",
    "StrategyScanStatusError",
    "load_strategy_scan_status_index_v2",
    "publish_strategy_scan_status",
    "publish_strategy_scan_status_index_v2",
    "strategy_status_path",
    "validate_strategy_scan_status_index_v2",
]
