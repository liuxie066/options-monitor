from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Callable, Mapping, Sequence

from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.api import (
    apply_lifecycle_migration_manifest,
    current_decision_projection_migration_status,
    position_projection_migration_status,
)
from src.application.quality.cutover import quality_hot_path_cutover_preview
from src.application.research.storage_baseline import (
    collect_storage_runtime_baseline,
    preview_scan_blob_gc,
)


HISTORICAL_CLEANUP_PREVIEW_SCHEMA = "historical_cleanup_preview.v1"
HISTORICAL_CLEANUP_BACKUP_PROOF_SCHEMA = "historical_cleanup_backup_proof.v1"
_MAX_PROOF_BYTES = 64 * 1024
_MAX_INVENTORY_BYTES = 64 * 1024 * 1024


def build_historical_cleanup_preview(
    *,
    repo_root: str | Path,
    runtime_root: str | Path,
    lifecycle_inventory: str | Path,
    quality_cutover_evidence: str | Path | None = None,
    backup_proof: str | Path | None = None,
    ledger_sqlite: str | Path | None = None,
    history_reports: Sequence[str | Path] | None = None,
    allow_external_ledger: bool = False,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build a deterministic cleanup preview without changing runtime state."""

    observed_at = _utc_now(now_fn)
    root = _regular_directory(runtime_root)
    ledger = _ledger_path(
        root=root,
        value=ledger_sqlite,
        allow_external=allow_external_ledger,
    )
    persistent_before = _sqlite_persistent_identity(ledger)
    blockers: list[dict[str, str]] = []

    quality = _quality_gate(quality_cutover_evidence)
    if quality["status"] != "pass":
        blockers.append({"gate": "stable_new_path_evidence", "reason": quality["reason"]})

    position = _position_gate(ledger)
    if position["status"] != "pass":
        blockers.append({"gate": "position_projection", "reason": position["reason"]})

    current = _current_decision_gate(ledger)
    if current["status"] != "pass":
        blockers.append({"gate": "current_decision_projection", "reason": current["reason"]})

    lifecycle = _lifecycle_gate(lifecycle_inventory, ledger=ledger)
    if lifecycle["status"] != "pass":
        blockers.append({"gate": "lifecycle_reconciliation", "reason": lifecycle["reason"]})

    fixed_now = lambda: observed_at
    try:
        baseline = collect_storage_runtime_baseline(
            repo_root=repo_root,
            runtime_root=root,
            ledger_sqlite=ledger,
            history_reports=history_reports,
            allow_external_ledger=allow_external_ledger,
            now_fn=fixed_now,
        )
        forecast = _forecast_gate(baseline)
    except (AgentToolError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        baseline = {}
        forecast = {
            "status": "blocked",
            "reason": "storage_baseline_unavailable",
            "error_type": type(exc).__name__,
        }
    if forecast["status"] == "blocked":
        blockers.append({"gate": "space_forecast", "reason": forecast["reason"]})

    try:
        scan_gc = preview_scan_blob_gc(runtime_root=root, now_fn=fixed_now)
        scan = {
            "status": "pass" if scan_gc.get("deletion_allowed") is True else "blocked",
            "reason": (
                "scan_blob_inventory_verified"
                if scan_gc.get("deletion_allowed") is True
                else "scan_blob_inventory_blocked"
            ),
            "plan_sha256": scan_gc.get("plan_sha256"),
            "summary": dict(scan_gc.get("summary") or {}),
            "blockers": list(scan_gc.get("blockers") or []),
        }
    except (AgentToolError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        scan_gc = {}
        scan = {
            "status": "blocked",
            "reason": "scan_blob_inventory_unavailable",
            "error_type": type(exc).__name__,
            "summary": {},
            "blockers": [],
        }
    if scan["status"] != "pass":
        blockers.append({"gate": "scan_blob_integrity", "reason": scan["reason"]})

    try:
        live_logical_sha256 = _sqlite_logical_sha256(ledger)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        live_logical_sha256 = None
        blockers.append({"gate": "ledger_snapshot", "reason": "ledger_logical_hash_unavailable"})
    expected_backup_bindings = {
        "ledger_logical_sha256": live_logical_sha256,
        "lifecycle_inventory_manifest_hash": lifecycle.get("manifest_hash"),
        "position_projection_status_sha256": position.get("status_sha256"),
        "current_decision_projection_status_sha256": current.get("status_sha256"),
    }
    backup = _backup_gate(
        backup_proof,
        ledger=ledger,
        expected_bindings=expected_backup_bindings,
        observed_at=observed_at,
    )
    if backup["status"] != "pass":
        blockers.append({"gate": "backup_restore", "reason": backup["reason"]})

    persistent_after = _sqlite_persistent_identity(ledger)
    if persistent_before != persistent_after:
        blockers.append({"gate": "ledger_snapshot", "reason": "ledger_changed_during_preview"})

    blockers = sorted(blockers, key=lambda item: (item["gate"], item["reason"]))
    ready = not blockers
    candidates = (
        _cleanup_candidates(
            root=root,
            ledger=ledger,
            baseline=baseline,
            scan_gc=scan_gc,
        )
        if ready
        else []
    )
    gates = {
        "stable_new_path_evidence": quality,
        "position_projection": position,
        "current_decision_projection": current,
        "lifecycle_reconciliation": lifecycle,
        "backup_restore": backup,
        "space_forecast": forecast,
        "scan_blob_integrity": scan,
    }
    exclusions = [
        {
            "class": "required_data_legacy_csv_and_base64",
            "reason": "required_data_14_day_zero_legacy_read_evidence_not_available",
        },
        {
            "class": "ledger_history_rows",
            "reason": "row_deletion_or_compaction_algorithm_not_authorized_or_implemented",
        },
        {
            "class": "research_generation_roots",
            "reason": "permanent_logical_replay_roots",
        },
    ]
    status = "ready_for_authorization" if ready else "not_ready"
    hash_payload = {
        "schema_version": HISTORICAL_CLEANUP_PREVIEW_SCHEMA,
        "status": status,
        "runtime_root": str(root),
        "ledger_sqlite": str(ledger),
        "gates": gates,
        "blockers": blockers,
        "candidates": [{key: value for key, value in item.items() if key != "age_hours"} for item in candidates],
        "excluded_cleanup_classes": exclusions,
    }
    return {
        **hash_payload,
        "observed_at_utc": observed_at.isoformat(),
        "preview_ready": ready,
        "authorization_required": True,
        "expected_backup_bindings": expected_backup_bindings,
        "plan_sha256": _canonical_sha256(hash_payload),
        "summary": {
            "candidate_count": len(candidates),
            "candidate_bytes": sum(int(item.get("estimated_reclaimable_bytes") or 0) for item in candidates),
            "blocker_count": len(blockers),
        },
        "safety": {
            "read_only": True,
            "mutation_operations": 0,
            "automatic_actions": [],
            "actual_cleanup_authorized": False,
        },
    }


def _quality_gate(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "blocked", "reason": "quality_cutover_evidence_missing"}
    try:
        preview = quality_hot_path_cutover_preview(_safe_file(path))
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "reason": "quality_cutover_evidence_invalid",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "pass",
        "reason": "fourteen_eligible_days_verified",
        "evidence_sha256": preview["evidence_sha256"],
        "consumer_inventory_sha256": preview["consumer_inventory_sha256"],
        "eligible_market_day_counts": preview["eligible_market_day_counts"],
    }


def _position_gate(ledger: Path) -> dict[str, Any]:
    try:
        payload = position_projection_migration_status(ledger)
        stable = {
            key: payload.get(key)
            for key in (
                "checkpoint_mode",
                "source_generation",
                "head_count",
                "trusted_head_count",
                "checkpoint_count",
                "trusted_checkpoint_count",
                "checkpoint_state_bytes",
                "checkpoint_max_state_bytes",
                "checkpoint_k_within_bound",
                "checkpoint_space_within_bound",
                "last_full_verified_source_generation",
                "fingerprint_scope",
                "readiness",
                "reasons",
            )
        }
    except (OSError, sqlite3.Error, UnicodeError, TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "reason": "position_projection_status_unavailable",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "pass" if stable["readiness"] == "ready" else "blocked",
        "reason": "position_projection_reconciled"
        if stable["readiness"] == "ready"
        else "position_projection_not_ready",
        "status_sha256": _canonical_sha256(stable),
        **stable,
    }


def _current_decision_gate(ledger: Path) -> dict[str, Any]:
    try:
        payload = current_decision_projection_migration_status(ledger)
        stable = {
            key: payload.get(key)
            for key in (
                "status",
                "readiness",
                "readiness_reasons",
                "account_count",
                "repair",
                "shadow_status",
                "mixed_version_guard_status",
            )
        }
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "reason": "current_decision_status_unavailable",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "pass" if stable["status"] == "clean" else "blocked",
        "reason": "current_decision_projection_reconciled"
        if stable["status"] == "clean"
        else "current_decision_projection_not_ready",
        "status_sha256": _canonical_sha256(stable),
        "details": stable,
    }


def _lifecycle_gate(path: str | Path, *, ledger: Path) -> dict[str, Any]:
    try:
        payload = _strict_json_object(_read_regular(path, limit=_MAX_INVENTORY_BYTES))
        manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else payload
        dry_run = apply_lifecycle_migration_manifest(None, manifest=dict(manifest), apply_changes=False)
        rows = [dict(item) for item in manifest.get("rows") or [] if isinstance(item, dict)]
        identities = [str(item.get("target_key") or "").strip() for item in rows]
        if any(not item for item in identities) or len(identities) != len(set(identities)):
            raise ValueError("lifecycle inventory target identity is invalid")
        lifecycle_rows = [item for item in rows if item.get("kind") == "lifecycle_case"]
        observed_cases = [
            (
                str(item.get("case_id") or "").strip(),
                str(item.get("account") or "").strip().lower(),
            )
            for item in lifecycle_rows
        ]
        if any(
            not case_id or item.get("target_key") != f"lifecycle:{case_id}"
            for item, (case_id, _account) in zip(lifecycle_rows, observed_cases, strict=True)
        ):
            raise ValueError("lifecycle inventory case identity is invalid")
        observed_cases.sort()
        uri = f"{ledger.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            expected_cases = sorted(
                (str(case_id), str(account or "").strip().lower())
                for case_id, account in conn.execute("SELECT case_id,account FROM trade_lifecycle_cases")
            )
        review_count = sum(str(item.get("mapping_status") or "") != "exact" for item in rows)
        unowned_count = sum(not str(item.get("account") or "").strip() for item in rows)
        accounts = Counter(
            str(item.get("account") or "").strip().lower() for item in rows if str(item.get("account") or "").strip()
        )
    except (OSError, TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "reason": "lifecycle_inventory_invalid",
            "error_type": type(exc).__name__,
        }
    coverage_matches = observed_cases == expected_cases
    passed = review_count == 0 and unowned_count == 0 and coverage_matches
    return {
        "status": "pass" if passed else "blocked",
        "reason": (
            "all_lifecycle_targets_exact"
            if passed
            else "lifecycle_inventory_ledger_mismatch"
            if not coverage_matches
            else "lifecycle_targets_need_review"
        ),
        "manifest_hash": dry_run["manifest_hash"],
        "row_count": len(rows),
        "exact_count": len(rows) - review_count,
        "review_count": review_count,
        "unowned_count": unowned_count,
        "ledger_case_count": len(expected_cases),
        "case_coverage_matches": coverage_matches,
        "account_row_counts": dict(sorted(accounts.items())),
    }


def _forecast_gate(baseline: Mapping[str, Any]) -> dict[str, Any]:
    thresholds = dict(baseline.get("thresholds") or {})
    growth = dict((baseline.get("research_storage") or {}).get("growth") or {})
    status = str(thresholds.get("status") or "")
    ready = baseline.get("status") == "complete" and growth.get("status") == "complete" and status in {"ok", "warning"}
    return {
        "status": "pass" if ready else "blocked",
        "reason": "space_forecast_available" if ready else "space_forecast_incomplete",
        "capacity_status": status or None,
        "growth_status": growth.get("status"),
        "forecast_90d_free_bytes": thresholds.get("forecast_90d_free_bytes"),
        "warning_reasons": list(thresholds.get("warning_reasons") or []),
        "critical_reasons": list(thresholds.get("critical_reasons") or []),
        "operator_decision_required": bool(thresholds.get("operator_decision_required")),
    }


def _backup_gate(
    path: str | Path | None,
    *,
    ledger: Path,
    expected_bindings: Mapping[str, Any],
    observed_at: datetime,
) -> dict[str, Any]:
    if path is None:
        return {"status": "blocked", "reason": "backup_proof_missing"}
    try:
        proof_path = _safe_file(path)
        payload = _strict_json_object(_read_regular(proof_path, limit=_MAX_PROOF_BYTES))
        required = {
            "schema_version",
            "ledger_sqlite",
            "backup_path",
            "backup_sha256",
            "backup_size_bytes",
            "created_at_utc",
            "restore_verified",
            *expected_bindings,
        }
        if set(payload) != required or payload.get("schema_version") != HISTORICAL_CLEANUP_BACKUP_PROOF_SCHEMA:
            raise ValueError("backup proof shape is invalid")
        if _safe_file(payload["ledger_sqlite"]) != ledger:
            raise ValueError("backup proof ledger path mismatch")
        backup = _safe_file(_resolve_from(proof_path.parent, payload["backup_path"]))
        if backup == ledger or Path(f"{backup}-wal").exists() or Path(f"{backup}-journal").exists():
            raise ValueError("backup must be a standalone SQLite file")
        size, digest = _file_sha256(backup)
        if payload.get("backup_size_bytes") != size or payload.get("backup_sha256") != digest:
            raise ValueError("backup file identity mismatch")
        created_at = datetime.fromisoformat(str(payload["created_at_utc"]).replace("Z", "+00:00"))
        if created_at.tzinfo is None or created_at.astimezone(timezone.utc) > observed_at:
            raise ValueError("backup proof timestamp is invalid")
        if payload.get("restore_verified") is not True:
            raise ValueError("backup restore verification is missing")
        for key, expected in expected_bindings.items():
            if payload.get(key) != expected:
                raise ValueError(f"backup proof binding mismatch: {key}")
        backup_logical = _sqlite_logical_sha256(backup, verify_integrity=True)
        if backup_logical != expected_bindings["ledger_logical_sha256"]:
            raise ValueError("backup logical state mismatch")
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "reason": "backup_proof_invalid",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "pass",
        "reason": "backup_file_and_restore_proof_verified",
        "proof_sha256": _canonical_sha256(payload),
        "backup_sha256": digest,
        "backup_size_bytes": size,
        "created_at_utc": created_at.astimezone(timezone.utc).isoformat(),
        "ledger_logical_sha256": backup_logical,
    }


def _cleanup_candidates(
    *,
    root: Path,
    ledger: Path,
    baseline: Mapping[str, Any],
    scan_gc: Mapping[str, Any],
) -> list[dict[str, Any]]:
    candidates = [
        {
            "kind": "scan_blob_delete",
            "target": str(item["blob_relpath"]),
            "blob_sha256": item["blob_sha256"],
            "estimated_reclaimable_bytes": int(item["compressed_size_bytes"]),
            "age_hours": item.get("age_hours"),
            "action": "requires_explicit_authorization",
        }
        for item in scan_gc.get("candidates") or []
    ]
    page = dict((baseline.get("sqlite") or {}).get("page") or {})
    reclaimable = int(page.get("page_size_bytes") or 0) * int(page.get("freelist_count") or 0)
    if reclaimable > 0:
        candidates.append(
            {
                "kind": "sqlite_vacuum",
                "target": str(ledger.relative_to(root)) if ledger.is_relative_to(root) else str(ledger),
                "estimated_reclaimable_bytes": reclaimable,
                "action": "requires_explicit_authorization",
            }
        )
    return sorted(candidates, key=lambda item: (str(item["kind"]), str(item["target"])))


def _ledger_path(*, root: Path, value: str | Path | None, allow_external: bool) -> Path:
    target = _safe_file(value or root / "output_shared/state/option_positions.sqlite3")
    if not allow_external and not target.is_relative_to(root):
        raise ValueError("ledger SQLite must be inside runtime root")
    return target


def _regular_directory(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if _path_has_symlink(path) or not path.is_dir():
        raise ValueError("runtime root must be an existing non-symlink directory")
    return path.resolve()


def _safe_file(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if _path_has_symlink(path) or not path.is_file():
        raise ValueError("cleanup preview input must be an existing non-symlink file")
    resolved = path.resolve()
    if not stat.S_ISREG(resolved.stat(follow_symlinks=False).st_mode):
        raise ValueError("cleanup preview input must be a regular file")
    return resolved


def _path_has_symlink(path: Path) -> bool:
    current = path.absolute()
    while True:
        if current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _read_regular(value: str | Path, *, limit: int) -> bytes:
    path = _safe_file(value)
    before = path.stat(follow_symlinks=False)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("cleanup preview input exceeds its size limit")
        chunks: list[bytes] = []
        remaining = int(info.st_size) + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise ValueError("cleanup preview input exceeds its size limit")
        after = path.stat(follow_symlinks=False)
        if _stat_identity(before) != _stat_identity(info) or _stat_identity(info) != _stat_identity(after):
            raise ValueError("cleanup preview input changed while it was read")
        return raw
    finally:
        os.close(descriptor)


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("cleanup preview JSON input must be an object")
    return value


def _file_sha256(path: Path) -> tuple[int, str]:
    before = path.stat(follow_symlinks=False)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(before) != _stat_identity(opened):
            raise ValueError("backup changed before it was verified")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    after = path.stat(follow_symlinks=False)
    if _stat_identity(opened) != _stat_identity(after):
        raise ValueError("backup changed while it was verified")
    return int(after.st_size), digest


def _sqlite_logical_sha256(path: Path, *, verify_integrity: bool = False) -> str:
    uri = f"{path.as_uri()}?mode=ro"
    if verify_integrity:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        if verify_integrity:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]) != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("backup SQLite integrity verification failed")
        digest = hashlib.sha256()
        for line in conn.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()
    finally:
        conn.close()


def _sqlite_persistent_identity(path: Path) -> tuple[tuple[str, bool, int | None, int | None, int | None], ...]:
    rows = []
    for suffix in ("", "-wal"):
        item = Path(f"{path}{suffix}")
        try:
            info = item.stat(follow_symlinks=False)
            rows.append((suffix, True, int(info.st_size), int(info.st_mtime_ns), int(info.st_ino)))
        except FileNotFoundError:
            rows.append((suffix, False, None, None, None))
    return tuple(rows)


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns)


def _resolve_from(base: Path, value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    return path if path.is_absolute() else base / path


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _utc_now(now_fn: Callable[[], datetime] | None) -> datetime:
    value = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("cleanup preview clock must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "HISTORICAL_CLEANUP_BACKUP_PROOF_SCHEMA",
    "HISTORICAL_CLEANUP_PREVIEW_SCHEMA",
    "build_historical_cleanup_preview",
]
