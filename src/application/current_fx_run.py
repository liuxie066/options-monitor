"""Immutable current FX quote for one formal tick run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.payload_helpers import canonical_json_bytes_lines
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_run_state_bytes_safely,
    write_run_state_bytes_once_safely,
)
from src.infrastructure.exchange_rates import current_exchange_rate_snapshot


RUN_FX_NAME = "current_exchange_rates.v1.json"


class RunFXError(RuntimeError):
    pass


def _decode(raw: bytes, *, run_id: str) -> tuple[dict[str, Any], str]:
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RunFXError("sealed FX artifact is invalid") from exc
    if not isinstance(envelope, dict) or envelope.get("schema_version") != 1 or envelope.get("run_id") != run_id:
        raise RunFXError("sealed FX artifact identity mismatch")
    snapshot = envelope.get("snapshot")
    digest = envelope.get("snapshot_sha256")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("pairs"), dict) or not isinstance(digest, str) or canonical_sha256(snapshot) != digest:
        raise RunFXError("sealed FX artifact hash mismatch")
    return snapshot, digest


def load_run_fx_snapshot(*, base: Path, run_id: str) -> tuple[dict[str, Any], str]:
    try:
        raw = read_run_state_bytes_safely(base=base, run_id=run_id, name=RUN_FX_NAME)
    except AccountRunConfigError as exc:
        raise RunFXError("sealed FX artifact unavailable") from exc
    return _decode(raw, run_id=run_id)


def seal_run_fx_snapshot(*, base: Path, run_id: str) -> tuple[dict[str, Any], str]:
    path = Path(base).resolve() / "output_runs" / run_id / "state" / RUN_FX_NAME
    try:
        return load_run_fx_snapshot(base=base, run_id=run_id)
    except RunFXError as exc:
        if path.exists() or path.is_symlink() or str(exc) != "sealed FX artifact unavailable":
            raise
    snapshot = current_exchange_rate_snapshot(
        cache_path=Path(base).resolve() / "output_shared" / "state" / "rate_cache.json",
    )
    digest = canonical_sha256(snapshot)
    payload: Mapping[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "snapshot_sha256": digest,
        "snapshot": snapshot,
    }
    try:
        write_run_state_bytes_once_safely(
            base=base, run_id=run_id, name=RUN_FX_NAME,
            payload=canonical_json_bytes_lines(payload),
        )
    except AccountRunConfigError as exc:
        raise RunFXError("sealed FX artifact conflicts with this run") from exc
    return load_run_fx_snapshot(base=base, run_id=run_id)
