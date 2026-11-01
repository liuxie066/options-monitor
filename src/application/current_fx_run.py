"""Immutable current FX quote for one formal tick run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.performance.cash_conversion import RunFXError, decode_run_fx_snapshot as _decode
from src.application.payload_helpers import canonical_json_bytes_lines
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_run_state_bytes_safely,
    write_run_state_bytes_once_safely,
)
from src.infrastructure.exchange_rates import current_exchange_rate_snapshot


RUN_FX_NAME = "current_exchange_rates.v1.json"


def encode_run_fx_snapshot(snapshot: dict[str, Any], *, run_id: str) -> bytes:
    """Seal the owner's observation without changing its times or pair qualification."""
    return canonical_json_bytes_lines(
        {"schema_version": 1, "run_id": run_id, "snapshot_sha256": canonical_sha256(snapshot), "snapshot": snapshot}
    )


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
    try:
        write_run_state_bytes_once_safely(
            base=base,
            run_id=run_id,
            name=RUN_FX_NAME,
            payload=encode_run_fx_snapshot(snapshot, run_id=run_id),
        )
    except AccountRunConfigError as exc:
        raise RunFXError("sealed FX artifact conflicts with this run") from exc
    return load_run_fx_snapshot(base=base, run_id=run_id)
