from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn, cast

from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.research import (
    RESEARCH_EVALUATION_INPUT_SCHEMA,
)
from src.infrastructure.private_storage import open_private_text, private_path


_HASH_64 = re.compile(r"[0-9a-f]{64}\Z")


class ResearchArtifactError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise ResearchArtifactError(reason_code, message)


def _safe_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("research_artifact_invalid", f"{label} must be canonical text")
    parts = value.split("/")
    if value.startswith("/") or "\\" in value or any(
        part in {"", ".", ".."} for part in parts
    ):
        _fail("research_artifact_invalid", f"{label} must be a safe relative ref")
    return value


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_64.fullmatch(value) is None:
        _fail("research_artifact_invalid", f"{label} must be a lowercase SHA-256")
    return value


def _read_canonical_json(
    artifact_root: str | Path,
    *,
    ref: object,
    expected_file_sha256: object,
    label: str,
) -> dict[str, Any]:
    relative_ref = _safe_ref(ref, f"{label}.ref")
    expected_hash = _hash(expected_file_sha256, f"{label}.file_sha256")
    path = private_path(artifact_root).joinpath(*relative_ref.split("/"))
    try:
        with open_private_text(path) as handle:
            text = handle.read()
        content = text.encode("utf-8")
        payload = json.loads(text)
        canonical_text = render_json_text(payload) if isinstance(payload, dict) else None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ResearchArtifactError(
            "research_artifact_invalid", f"{label} artifact cannot be read"
        ) from exc
    if hashlib.sha256(content).hexdigest() != expected_hash:
        _fail("research_artifact_invalid", f"{label} file hash changed")
    if canonical_text != text:
        _fail("research_artifact_invalid", f"{label} bytes are not canonical JSON")
    return cast(dict[str, Any], payload)


def load_materialized_research_input(
    artifact_root: str | Path,
    spec: Mapping[str, Any],
) -> dict[str, object]:
    source = spec.get("research_source")
    if not isinstance(source, Mapping):
        _fail("research_artifact_invalid", "research source is invalid")
    sealed_dataset = _read_canonical_json(
        artifact_root,
        ref=source.get("dataset_ref"),
        expected_file_sha256=source.get("dataset_sha256"),
        label="sealed_dataset",
    )
    raw_days = sealed_dataset.get("days")
    if not isinstance(raw_days, list):
        _fail("research_artifact_invalid", "sealed dataset days must be a list")
    projections: list[dict[str, object]] = []
    for raw_day in raw_days:
        if not isinstance(raw_day, Mapping) or not isinstance(
            raw_day.get("points"), list
        ):
            _fail("research_artifact_invalid", "sealed dataset point index is invalid")
        for raw_point in cast(list[object], raw_day["points"]):
            if not isinstance(raw_point, Mapping):
                _fail("research_artifact_invalid", "sealed dataset point is invalid")
            ref = raw_point.get("projection_ref")
            projections.append(
                {
                    "projection_ref": ref,
                    "projection": _read_canonical_json(
                        artifact_root,
                        ref=ref,
                        expected_file_sha256=raw_point.get(
                            "projection_file_sha256"
                        ),
                        label="ranking_projection",
                    ),
                }
            )
    return {
        "schema_version": RESEARCH_EVALUATION_INPUT_SCHEMA,
        "experiment_spec": dict(spec),
        "dataset_ref": source.get("dataset_ref"),
        "sealed_dataset": sealed_dataset,
        "ranking_projections": projections,
    }


def load_recorded_research_revision(
    artifact_root: str | Path,
    generation: Mapping[str, Any],
) -> dict[str, Any]:
    return _read_canonical_json(
        artifact_root,
        ref=generation.get("last_revision_ref"),
        expected_file_sha256=generation.get("last_revision_file_sha256"),
        label="research_revision",
    )


__all__ = [
    "ResearchArtifactError",
    "load_materialized_research_input",
    "load_recorded_research_revision",
]
