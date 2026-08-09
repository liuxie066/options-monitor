from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from src.application.ai_decision_advice.config import (
    ADVICE_RECORDS_FILE,
    MODEL,
    PROVIDER,
)
from src.application.ai_decision_advice.prompts import CompiledPromptPack
from src.application.ai_decision_advice.validation import (
    INPUT_BINDING_KEYS,
    SCHEMA_NAME,
    SEMANTIC_INPUT_BINDING_KEYS,
)
from src.infrastructure.private_storage import append_private_text, open_private_text


ADVICE_RECORD_KIND = "advice_record"


def advice_records_path(run_dir: Path, account: str) -> Path:
    return Path(run_dir) / "accounts" / str(account) / "state" / ADVICE_RECORDS_FILE


def _iter_run_dirs(output_root: Path) -> list[Path]:
    runs_root = Path(output_root) / "output_runs"
    if not runs_root.is_dir():
        return []
    return sorted(
        (item for item in runs_root.iterdir() if item.is_dir()),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )


def read_advice_records(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    rows: list[dict[str, Any]] = []
    with open_private_text(path) as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("kind") == ADVICE_RECORD_KIND:
                rows.append(row)
    return rows


def append_advice_record(path: Path, record: Mapping[str, Any]) -> None:
    encoded = json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
    append_private_text(path, encoded)


def prompt_fingerprint_for(compiled: CompiledPromptPack) -> str:
    return hashlib.sha256(f"{compiled.version}:{compiled.compiled_sha256}".encode("utf-8")).hexdigest()


def bindings_match(record: Mapping[str, Any], bindings: Mapping[str, Any]) -> bool:
    """Reuse predicate (docs 13.2).

    All five semantic input hashes and the binding shape must be equal.
    ``external_evidence_run_id`` may change after a successful no-change
    refresh; the semantic evidence and fact-registry hashes decide reuse.
    """

    record_bindings = record.get("input_bindings")
    if not isinstance(record_bindings, Mapping):
        return False
    if set(record_bindings) != set(INPUT_BINDING_KEYS) or set(bindings) != set(INPUT_BINDING_KEYS):
        return False
    return all(record_bindings.get(key) == bindings.get(key) for key in SEMANTIC_INPUT_BINDING_KEYS)


def versions_match(
    record: Mapping[str, Any],
    *,
    prompt_fingerprint: str,
    model: str = MODEL,
    provider: str = PROVIDER,
    schema_name: str = SCHEMA_NAME,
) -> bool:
    versions = record.get("versions")
    if not isinstance(versions, Mapping):
        return False
    return (
        versions.get("prompt_fingerprint") == prompt_fingerprint
        and versions.get("model") == model
        and versions.get("provider") == provider
        and versions.get("schema_name") == schema_name
    )


def find_reusable_completed(
    output_root: Path,
    *,
    account: str,
    bindings: Mapping[str, Any],
    prompt_fingerprint: str,
) -> dict[str, Any] | None:
    """Latest completed advice record eligible for reuse (docs 12.3 / 13.2)."""

    for run_dir in _iter_run_dirs(Path(output_root)):
        records = read_advice_records(advice_records_path(run_dir, account))
        for record in reversed(records):
            if record.get("status") != "completed":
                continue
            if not bindings_match(record, bindings):
                continue
            if not versions_match(record, prompt_fingerprint=prompt_fingerprint):
                continue
            return record
    return None


def build_reuse_record(
    prior: Mapping[str, Any],
    *,
    advice_id: str,
    run_id: str,
    account_ref: str,
    recorded_at: str,
    bindings: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild a completed record under a new run-local anonymous ref."""

    return {
        "kind": ADVICE_RECORD_KIND,
        "schema": prior.get("schema"),
        "advice_id": advice_id,
        "run_id": run_id,
        "account_ref": account_ref,
        "market": prior.get("market"),
        "recorded_at": recorded_at,
        "input_bindings": dict(bindings),
        "versions": deepcopy(prior.get("versions")),
        "zero_candidate": deepcopy(prior.get("zero_candidate")),
        "status": "completed",
        "unavailable_reason": None,
        "reused": True,
        "reuse_of_advice_id": prior.get("advice_id"),
        "decisions": deepcopy(prior.get("decisions") or {}),
        "demotions": deepcopy(prior.get("demotions") or []),
        "repair_attempted": False,
    }


def advice_id_for(run_id: str, account_ref: str, recorded_at: str) -> str:
    digest = hashlib.sha256(f"{run_id}:{account_ref}:{recorded_at}".encode("utf-8")).hexdigest()[:16]
    return f"adv-{digest}"
