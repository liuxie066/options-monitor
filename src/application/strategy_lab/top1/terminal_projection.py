from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.application.shadow_replay.common import (
    artifact_content_sha256,
    attach_artifact_provenance,
    render_json_text,
)
from src.infrastructure.private_storage import (
    PRIVATE_FILE_MODE,
    ensure_private_directory,
    open_private_text,
    private_path,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore


GENERATION_TERMINAL_SCHEMA = "sell_put_top1_generation_terminal.v2"
EXPERIMENT_RECEIPT_SCHEMA = "sell_put_top1_experiment_receipt.v2"

Publisher = Callable[[str | Path, str, bytes], Path]


def _file_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _projection_request(ref: str, payload: dict[str, Any]) -> dict[str, object]:
    text = render_json_text(payload)
    request: dict[str, object] = {
        "ref": ref,
        "content_sha256": artifact_content_sha256(payload),
        "file_sha256": _file_sha256(text),
        "text": text,
    }
    if len(json.dumps(request, separators=(",", ":")).encode("utf-8")) > 8192:
        raise ValueError("terminal projection request exceeds 8 KiB")
    return request


def build_generation_terminal_request(
    generation: Mapping[str, object],
    *,
    terminal_mode: str,
    reason: str | None,
    occurred_at_utc: str,
    partial_summary: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if terminal_mode not in {"completed", "aborted"}:
        raise ValueError("terminal_mode must be completed or aborted")
    if terminal_mode == "completed" and reason is not None:
        raise ValueError("completed terminal cannot have reason")
    if terminal_mode == "aborted" and reason not in {
        "human_abandoned",
        "behavior_binding_drift",
    }:
        raise ValueError("aborted terminal reason is unsupported")

    experiment_id = str(generation["experiment_id"])
    generation_kind = str(generation["generation_kind"])
    revision_value = generation["revision"]
    if isinstance(revision_value, bool) or not isinstance(revision_value, int):
        raise ValueError("generation revision must be an integer")
    revision = revision_value
    payload: dict[str, Any] = {
        "schema_version": GENERATION_TERMINAL_SCHEMA,
        "experiment_id": experiment_id,
        "generation": {
            "generation_id": f"{experiment_id}:{generation_kind}",
            "generation_kind": generation_kind,
            "revision": revision,
            "last_revision_ref": generation["last_revision_ref"],
            "last_revision_file_sha256": generation[
                "last_revision_file_sha256"
            ],
            "frozen_row_content_sha256": generation[
                "frozen_row_content_sha256"
            ],
        },
        "terminal": {
            "mode": terminal_mode,
            "reason": reason,
            "occurred_at_utc": occurred_at_utc,
            "partial_summary": dict(partial_summary or {}) if terminal_mode == "aborted" else None,
        },
    }
    attach_artifact_provenance(
        payload,
        artifact_kind="sell_put_top1_generation_terminal",
        source_generation={
            "generation_id": f"{experiment_id}:{generation_kind}",
            "revision": revision,
        },
    )
    ref = (
        f"strategy_lab/top1/experiments/{experiment_id}/generations/"
        f"{generation_kind}.terminal.json"
    )
    return {
        "experiment_id": experiment_id,
        "generation_kind": generation_kind,
        "revision": revision,
        "last_revision_ref": generation["last_revision_ref"],
        "last_revision_file_sha256": generation["last_revision_file_sha256"],
        "frozen_row_content_sha256": generation["frozen_row_content_sha256"],
        "terminal_mode": terminal_mode,
        "reason": reason,
        **_projection_request(ref, payload),
    }


def _generation_views(
    generations: Sequence[Mapping[str, object]],
    generation_requests: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    request_by_kind = {
        str(item["generation_kind"]): item for item in generation_requests
    }
    row_by_kind = {str(item["generation_kind"]): item for item in generations}
    generation_views: dict[str, object] = {}
    for kind in ("research", "hidden", "outcome"):
        row = row_by_kind.get(kind)
        request = request_by_kind.get(kind)
        if row is None:
            generation_views[kind] = {
                "state": "not_started",
                "terminal_mode": None,
                "ref": None,
                "content_sha256": None,
                "file_sha256": None,
            }
            continue
        if request is not None:
            generation_views[kind] = {
                "state": "terminal",
                "terminal_mode": request["terminal_mode"],
                "ref": request["ref"],
                "content_sha256": request["content_sha256"],
                "file_sha256": request["file_sha256"],
            }
            continue
        generation_views[kind] = {
            "state": "terminal",
            "terminal_mode": row["terminal_mode"],
            "ref": row["terminal_ref"],
            "content_sha256": row["terminal_content_sha256"],
            "file_sha256": row["terminal_file_sha256"],
        }
    return generation_views


def build_aborted_receipt_request(
    experiment: Mapping[str, object],
    generations: Sequence[Mapping[str, object]],
    generation_requests: Sequence[Mapping[str, object]],
    *,
    reason: str,
    occurred_at_utc: str,
    terminated_at_partition: int | None,
) -> dict[str, object]:
    generation_views = _generation_views(generations, generation_requests)

    experiment_id = str(experiment["experiment_id"])
    payload: dict[str, Any] = {
        "schema_version": EXPERIMENT_RECEIPT_SCHEMA,
        "experiment_id": experiment_id,
        "topic_id": experiment["topic_id"],
        "market": experiment["market"],
        "account": experiment["account"],
        "strategy_family": experiment["strategy_family"],
        "terminal": {
            "mode": "aborted",
            "reason": reason,
            "occurred_at_utc": occurred_at_utc,
            "terminated_at_partition": terminated_at_partition,
        },
        "bindings": {
            "research_spec_sha256": experiment["research_spec_sha256"],
            "validation_spec_sha256": experiment["validation_spec_sha256"],
            "hidden_window_commitment_sha256": experiment[
                "proposed_commitment_sha256"
            ],
        },
        "generations": generation_views,
        "outcome_status": "insufficient_evidence",
        "metrics": None,
    }
    attach_artifact_provenance(
        payload,
        artifact_kind="sell_put_top1_experiment_receipt",
        source_generation={"generation_id": f"experiment:{experiment_id}:terminal"},
    )
    ref = f"strategy_lab/top1/experiments/{experiment_id}/experiment_receipt.json"
    return {"experiment_id": experiment_id, **_projection_request(ref, payload)}


def build_completed_receipt_request(
    experiment: Mapping[str, object],
    generations: Sequence[Mapping[str, object]],
    outcome_terminal_request: Mapping[str, object],
    *,
    final_outcome_status: str,
    result: Mapping[str, object],
    coverage: Mapping[str, object],
    contract_versions: Mapping[str, object],
    occurred_at_utc: str,
) -> dict[str, object]:
    if final_outcome_status not in {
        "candidate_for_adoption",
        "keep_baseline",
        "insufficient_evidence",
    }:
        raise ValueError("completed outcome status is invalid")
    generation_views = _generation_views(generations, [outcome_terminal_request])
    experiment_id = str(experiment["experiment_id"])
    payload: dict[str, Any] = {
        "schema_version": EXPERIMENT_RECEIPT_SCHEMA,
        "experiment_id": experiment_id,
        "topic_id": experiment["topic_id"],
        "market": experiment["market"],
        "account": experiment["account"],
        "strategy_family": experiment["strategy_family"],
        "terminal": {
            "mode": "completed",
            "reason": None,
            "occurred_at_utc": occurred_at_utc,
            "terminated_at_partition": experiment["completed_validation_partitions"],
        },
        "bindings": {
            "research_spec_sha256": experiment["research_spec_sha256"],
            "validation_spec_sha256": experiment["validation_spec_sha256"],
            "hidden_window_commitment_sha256": experiment[
                "proposed_commitment_sha256"
            ],
        },
        "contract_versions": dict(contract_versions),
        "generations": generation_views,
        "outcome_status": final_outcome_status,
        "coverage": dict(coverage),
        "metrics": dict(result),
    }
    attach_artifact_provenance(
        payload,
        artifact_kind="sell_put_top1_experiment_receipt",
        source_generation={"generation_id": f"experiment:{experiment_id}:terminal"},
    )
    ref = f"strategy_lab/top1/experiments/{experiment_id}/experiment_receipt.json"
    return {"experiment_id": experiment_id, **_projection_request(ref, payload)}


def publish_exact_text(
    artifact_root: str | Path,
    relative_ref: str,
    content: bytes,
) -> Path:
    parts = relative_ref.split("/")
    if (
        not relative_ref
        or relative_ref.startswith("/")
        or "\\" in relative_ref
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("artifact ref must be a safe relative POSIX path")
    root = ensure_private_directory(private_path(artifact_root))
    parent = root
    for part in parts[:-1]:
        parent = ensure_private_directory(parent / part)
    target = parent / parts[-1]
    if target.is_symlink():
        raise ValueError("artifact target must not be a symlink")
    if target.exists():
        with open_private_text(target) as handle:
            existing = handle.read().encode("utf-8")
        if existing != content:
            raise ValueError("artifact bytes conflict")
        return target

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=parent
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, target)
        except FileExistsError:
            with open_private_text(target) as handle:
                existing = handle.read().encode("utf-8")
            if existing != content:
                raise ValueError("artifact bytes conflict")
        target_stat = os.lstat(target)
        if not stat.S_ISREG(target_stat.st_mode):
            raise ValueError("artifact target is not a regular file")
        os.chmod(target, PRIVATE_FILE_MODE)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def recover_terminal_projection(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str | None = None,
    publisher: Publisher | None = None,
) -> dict[str, int]:
    publish = publisher or publish_exact_text
    recovered = 0
    for event in store.pending_projections(experiment_id=experiment_id):
        request = json.loads(str(event["payload_json"]))
        text = request.get("text")
        if not isinstance(text, str):
            raise ValueError("projection request text is missing")
        if _file_sha256(text) != request.get("file_sha256"):
            raise ValueError("projection file hash mismatch")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("projection payload must be an object")
        if artifact_content_sha256(payload) != request.get("content_sha256"):
            raise ValueError("projection content hash mismatch")
        published_path = publish(
            artifact_root, str(request["ref"]), text.encode("utf-8")
        )
        expected_path = private_path(artifact_root).joinpath(
            *str(request["ref"]).split("/")
        )
        if private_path(published_path) != expected_path:
            raise ValueError("publisher returned an unexpected artifact path")
        with open_private_text(published_path) as handle:
            if handle.read() != text:
                raise ValueError("published artifact bytes changed")
        store.mark_projection_published(
            request_event_id=str(event["event_id"]),
            actor="terminal-projection-recovery",
            occurred_at_utc=str(event["occurred_at_utc"]),
        )
        recovered += 1
    return {
        "recovered": recovered,
        "pending": len(store.pending_projections(experiment_id=experiment_id)),
    }


__all__ = [
    "EXPERIMENT_RECEIPT_SCHEMA",
    "GENERATION_TERMINAL_SCHEMA",
    "build_aborted_receipt_request",
    "build_completed_receipt_request",
    "build_generation_terminal_request",
    "publish_exact_text",
    "recover_terminal_projection",
]
