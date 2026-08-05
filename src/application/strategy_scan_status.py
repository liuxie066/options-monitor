from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.application.opend_symbol_outputs import SUCCESS_EMPTY_REASON_CODES
from src.application.position_advice_source_receipts import sha256_bytes
from src.infrastructure.io_utils import atomic_write_json


STRATEGY_SCAN_STATUS_SCHEMA = "strategy_scan_status.v1"
STRATEGY_SCAN_STATUS_INDEX_SCHEMA = "strategy_scan_status_index.v1"
_TERMINAL = frozenset({"completed", "unavailable", "failed", "not_applicable"})


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


def canonical_strategy_artifacts(
    *,
    report_dir: Path,
    symbol: str,
    strategy_family: str,
) -> list[Path]:
    root = Path(report_dir).resolve()
    lower = str(symbol or "").strip().lower()
    family = str(strategy_family or "").strip().lower()
    if family == "sell_put":
        names = (
            f"{lower}_sell_put_candidates.csv",
            f"{lower}_sell_put_candidates_labeled.csv",
        )
    elif family == "covered_call":
        names = (f"{lower}_sell_call_candidates.csv",)
    elif family == "combo_yield":
        names = (f"{lower}_combo_yield_candidates.csv",)
    else:
        raise StrategyScanStatusError(f"unknown strategy family: {family}")
    return [(root / name).resolve() for name in names]


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
    artifacts = canonical_strategy_artifacts(
        report_dir=root,
        symbol=symbol,
        strategy_family=strategy_family,
    )
    artifact_entries: list[dict[str, str]] = []
    for artifact in artifacts:
        if not artifact.is_file() or artifact.is_symlink():
            raise StrategyScanStatusError(
                f"canonical strategy artifact is unavailable: {artifact.name}"
            )
        artifact_entries.append(
            {
                "relpath": artifact.relative_to(root).as_posix(),
                "sha256": sha256_bytes(artifact.read_bytes()),
            }
        )
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
        "artifacts": artifact_entries,
    }
    if status_norm == "completed":
        payload["candidate_count"] = max(0, int(candidate_count or 0))
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


def publish_strategy_scan_status_index(
    *,
    report_dir: Path,
    run_id: str,
    account: str,
    expected: Iterable[Mapping[str, str]],
) -> dict[str, Any]:
    root = Path(report_dir).resolve()
    items: list[dict[str, Any]] = []
    for raw in sorted(
        (dict(item) for item in expected),
        key=lambda item: (
            str(item.get("market") or ""),
            str(item.get("symbol") or ""),
            str(item.get("strategy_family") or ""),
        ),
    ):
        market = _required(raw.get("market"), "market").upper()
        symbol = _required(raw.get("symbol"), "symbol").upper()
        family = _required(
            raw.get("strategy_family"),
            "strategy_family",
        ).lower()
        status_path = strategy_status_path(
            report_dir=root,
            symbol=symbol,
            strategy_family=family,
        )
        reason: str | None = None
        try:
            payload = _validate_status(
                path=status_path,
                report_dir=root,
                run_id=run_id,
                account=account,
                market=market,
                symbol=symbol,
                strategy_family=family,
            )
        except StrategyScanStatusError as exc:
            reason = (
                "strategy_scan_status_missing"
                if not status_path.is_file()
                else "strategy_scan_status_invalid"
            )
            _ensure_empty_artifacts(
                report_dir=root,
                symbol=symbol,
                strategy_family=family,
            )
            payload = publish_strategy_scan_status(
                report_dir=root,
                run_id=run_id,
                account=account,
                market=market,
                symbol=symbol,
                strategy_family=family,
                status="failed",
                reason=reason,
            )
            payload["validation_detail"] = str(exc)
        items.append(
            {
                **payload,
                "source_status_path": status_path.relative_to(root).as_posix(),
            }
        )
    counts = {
        status: sum(1 for item in items if item.get("status") == status)
        for status in ("completed", "unavailable", "failed", "not_applicable")
    }
    payload = {
        "schema_version": STRATEGY_SCAN_STATUS_INDEX_SCHEMA,
        "run_id": _required(run_id, "run_id"),
        "account": _required(account, "account").lower(),
        "published_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected_count": len(items),
        "counts": counts,
        "items": items,
    }
    path = (root / "strategy_scan_status_index.v1.json").resolve()
    atomic_write_json(path, payload)
    return {**payload, "index_path": str(path)}


def load_strategy_scan_status_index(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategyScanStatusError("strategy status index is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != STRATEGY_SCAN_STATUS_INDEX_SCHEMA
        or not isinstance(payload.get("items"), list)
    ):
        raise StrategyScanStatusError("strategy status index is invalid")
    return payload


def _validate_status(
    *,
    path: Path,
    report_dir: Path,
    run_id: str,
    account: str,
    market: str,
    symbol: str,
    strategy_family: str,
) -> dict[str, Any]:
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
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise StrategyScanStatusError("strategy status identity mismatch")
    if payload.get("status") not in _TERMINAL:
        raise StrategyScanStatusError("strategy status is not terminal")
    source_outcome = str(payload.get("source_outcome") or "").strip()
    reason_code = str(payload.get("reason_code") or "").strip()
    if source_outcome or reason_code:
        if (
            payload.get("status") != "completed"
            or int(payload.get("candidate_count") or 0) != 0
            or source_outcome != "success_empty"
            or reason_code not in SUCCESS_EMPTY_REASON_CODES
            or not str(payload.get("snapshot_id") or "").strip()
            or not str(payload.get("receipt_relpath") or "").strip()
        ):
            raise StrategyScanStatusError(
                "strategy success-empty evidence is invalid"
            )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise StrategyScanStatusError("strategy status artifacts are invalid")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise StrategyScanStatusError("strategy artifact binding is invalid")
        relpath = _required(artifact.get("relpath"), "artifact relpath")
        candidate = (report_dir / relpath).resolve()
        try:
            candidate.relative_to(report_dir)
        except ValueError as exc:
            raise StrategyScanStatusError("strategy artifact escapes report dir") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise StrategyScanStatusError("strategy artifact is missing")
        if sha256_bytes(candidate.read_bytes()) != _required(
            artifact.get("sha256"),
            "artifact sha256",
        ):
            raise StrategyScanStatusError("strategy artifact hash mismatch")
    return payload


def _ensure_empty_artifacts(
    *,
    report_dir: Path,
    symbol: str,
    strategy_family: str,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    for path in canonical_strategy_artifacts(
        report_dir=report_dir,
        symbol=symbol,
        strategy_family=strategy_family,
    ):
        if not path.is_file():
            path.write_text("\n", encoding="utf-8")


def _required(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise StrategyScanStatusError(f"{field} is required")
    return text


__all__ = [
    "STRATEGY_SCAN_STATUS_INDEX_SCHEMA",
    "STRATEGY_SCAN_STATUS_SCHEMA",
    "StrategyScanStatusError",
    "canonical_strategy_artifacts",
    "load_strategy_scan_status_index",
    "publish_strategy_scan_status",
    "publish_strategy_scan_status_index",
    "strategy_status_path",
]
