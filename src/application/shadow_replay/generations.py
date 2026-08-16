from __future__ import annotations

from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterable, Mapping
from uuid import uuid4
import zlib


GENERATION_SCHEMA_VERSION = "shadow_replay_generation.v1"
GENERATION_REF_SCHEMA_VERSION = "shadow_replay_generation_ref.v1"
PARTITION_REF_SCHEMA_VERSION = "shadow_replay_partition_ref.v1"
MAX_DELTA_DEPTH = 32
PARTITION_MAX_ROWS = 256
MANIFEST_MAX_BYTES = 16 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_FIELDS = (
    "market_date",
    "decision_date",
    "mark_date",
    "outcome_date",
    "trade_date",
    "date",
    "observed_at_utc",
    "decision_at_utc",
    "captured_at_utc",
    "quote_as_of_utc",
    "mark_at",
    "entry_observed_at_utc",
    "generated_at_utc",
    "expiration",
)


class ResearchGenerationError(ValueError):
    """Raised when an immutable research generation is invalid."""


def publish_dataset_generation(
    dataset_dir: Path,
    *,
    dataset_manifest: Mapping[str, Any],
    required_files: Iterable[str],
    file_schemas: Mapping[str, str],
    legacy_revision: int,
) -> dict[str, Any]:
    """Publish or adopt one immutable base/delta generation."""

    root = _dataset_root(dataset_dir)
    previous_ref = dataset_manifest.get("generation")
    parent = resolve_dataset_generation(root, previous_ref) if isinstance(previous_ref, Mapping) else None
    files, summaries = _partition_dataset(
        root,
        required_files=tuple(required_files),
        file_schemas=file_schemas,
        parent_files=(parent or {}).get("files") or {},
    )
    logical_summary = _logical_summary(files, summaries)
    resolved_sha256 = _resolved_sha256(files, logical_summary)
    manifest_projection = _manifest_projection(dataset_manifest)
    dataset_binding = {
        "schema_version": _required_text(dataset_manifest.get("schema_version"), "dataset schema_version"),
        "dataset_id": _required_text(dataset_manifest.get("dataset_id") or root.name, "dataset_id"),
    }
    dataset_binding["dataset_id"] = _safe_dataset_id(dataset_binding["dataset_id"])
    if parent and parent["dataset"] != dataset_binding:
        raise ResearchGenerationError("research generation dataset binding changed")
    if (
        parent
        and parent["resolved_generation_sha256"] == resolved_sha256
        and parent["manifest_projection"] == manifest_projection
    ):
        return {
            "generation_ref": validate_generation_ref(previous_ref),
            "integrity_files": summaries,
            "changed": False,
        }

    use_delta = parent is not None and int(parent["depth"]) < MAX_DELTA_DEPTH
    changes = _file_changes(parent["files"], files) if use_delta else []
    added = [ref["sha256"] for change in changes for ref in change["added"]]
    removed = [digest for change in changes for digest in change["removed_sha256"]]
    body: dict[str, Any] = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "kind": "delta" if use_delta else "base",
        "depth": int(parent["depth"]) + 1 if use_delta else 0,
        "parent": parent["generation_ref"] if use_delta else None,
        "generation_store_root_relpath": "..",
        "dataset": dataset_binding,
        "provenance": {
            "legacy_revision": int(legacy_revision),
            "manifest_projection": manifest_projection,
        },
        "added_partition_sha256": added,
        "removed_partition_sha256": removed,
        "files": files if not use_delta else {},
        "changes": changes,
        "logical_summary": logical_summary,
        "resolved_generation_sha256": resolved_sha256,
    }
    identity = _canonical_sha256(body)
    manifest = {**body, "generation_id": f"generation:{identity}"}
    manifest_bytes = _canonical_bytes(manifest)
    relpath = f"generations/{identity}.manifest.json"
    _write_once(root, relpath, manifest_bytes)
    generation_ref = {
        "schema_version": GENERATION_REF_SCHEMA_VERSION,
        "generation_id": manifest["generation_id"],
        "relpath": relpath,
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "size_bytes": len(manifest_bytes),
        "resolved_generation_sha256": resolved_sha256,
        "depth": manifest["depth"],
    }
    validate_generation_ref(generation_ref)
    return {
        "generation_ref": generation_ref,
        "integrity_files": summaries,
        "changed": True,
    }


def resolve_dataset_generation(
    dataset_dir: Path,
    generation_ref: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve one generation using metadata only (one base plus <=32 deltas)."""

    root = _dataset_root(dataset_dir)
    wanted = validate_generation_ref(generation_ref)
    chain: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()
    current = wanted
    while True:
        if current["generation_id"] in seen or len(chain) > MAX_DELTA_DEPTH:
            raise ResearchGenerationError("research generation parent chain is invalid")
        seen.add(current["generation_id"])
        manifest = _load_manifest(root, current)
        chain.append((current, manifest))
        if manifest["kind"] == "base":
            break
        parent = manifest.get("parent")
        if not isinstance(parent, Mapping):
            raise ResearchGenerationError("research generation parent is missing")
        current = validate_generation_ref(parent)
        if current["depth"] != manifest["depth"] - 1:
            raise ResearchGenerationError("research generation parent depth mismatch")
    if len(chain) - 1 > MAX_DELTA_DEPTH:
        raise ResearchGenerationError("research generation delta depth exceeds limit")
    if len(chain) - 1 != chain[0][1]["depth"]:
        raise ResearchGenerationError("research generation chain depth mismatch")
    if any(manifest["dataset"] != chain[0][1]["dataset"] for _ref, manifest in chain[1:]):
        raise ResearchGenerationError("research generation dataset chain mismatch")

    files = {name: list(refs) for name, refs in chain[-1][1]["files"].items()}
    for _ref, manifest in reversed(chain[:-1]):
        for change in manifest["changes"]:
            name = _safe_file_name(change.get("file_name"))
            deleting = change["delete_file"]
            if deleting and name not in files:
                raise ResearchGenerationError("research generation deletes an unknown file")
            current_refs = files.get(name, [])
            prefix_count = change["prefix_count"]
            removed = change["removed_sha256"]
            if prefix_count > len(current_refs) or [ref["sha256"] for ref in current_refs[prefix_count:]] != removed:
                raise ResearchGenerationError("research generation removed partition mismatch")
            added = list(change["added"])
            if deleting:
                if prefix_count or added:
                    raise ResearchGenerationError("research generation file deletion is invalid")
                files.pop(name)
            else:
                files[name] = current_refs[:prefix_count] + added

    tip = chain[0][1]
    logical_summary = _validate_logical_summary(tip["logical_summary"], files)
    resolved_sha256 = _resolved_sha256(files, logical_summary)
    if resolved_sha256 != tip["resolved_generation_sha256"]:
        raise ResearchGenerationError("research generation resolved hash mismatch")
    if wanted["resolved_generation_sha256"] != resolved_sha256:
        raise ResearchGenerationError("research generation reference resolved hash mismatch")
    return {
        "generation_ref": wanted,
        "generation_id": wanted["generation_id"],
        "depth": tip["depth"],
        "resolved_generation_sha256": resolved_sha256,
        "dataset": dict(tip["dataset"]),
        "files": files,
        "logical_summary": logical_summary,
        "manifest_projection": tip["provenance"]["manifest_projection"],
        "legacy_revision": tip["provenance"]["legacy_revision"],
        "manifest_read_count": len(chain),
        "delta_manifest_read_count": len(chain) - 1,
        "partition_payload_read_count": 0,
    }


@contextmanager
def materialized_dataset_generation(
    dataset_dir: Path,
    generation_ref: Mapping[str, Any],
):
    """Materialize one bound generation for an existing legacy dataset reader."""

    root = _dataset_root(dataset_dir)
    resolved = resolve_dataset_generation(root, generation_ref)
    with tempfile.TemporaryDirectory(prefix="shadow-replay-generation-") as raw:
        target = Path(raw) / resolved["dataset"]["dataset_id"]
        target.mkdir()
        for name, refs in resolved["files"].items():
            output = target / name
            digest = hashlib.sha256()
            size = 0
            rows = 0
            with output.open("wb") as handle:
                for ref in refs:
                    payload = _load_partition(root, ref)
                    handle.write(payload)
                    digest.update(payload)
                    size += len(payload)
                    rows += int(ref["row_count"])
            expected = resolved["logical_summary"]["files"][name]
            if digest.hexdigest() != expected["sha256"] or size != expected["bytes"] or rows != expected["row_count"]:
                raise ResearchGenerationError("materialized research generation file mismatch")
        projection = dict(resolved["manifest_projection"])
        projection["dataset_dir"] = str(target)
        projection["files"] = {name: str((target / name).resolve()) for name in resolved["files"]}
        projection["integrity"] = {
            "schema_version": "shadow_replay_dataset_integrity.v1",
            "generation_id": resolved["generation_id"],
            "revision": resolved["legacy_revision"],
            "completed_at_utc": "1970-01-01T00:00:00Z",
            "files": resolved["logical_summary"]["files"],
        }
        (target / "manifest.json").write_bytes(_pretty_bytes(projection))
        (target / ".dataset.lock").touch()
        yield target


def validate_generation_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    ref = dict(value or {})
    if set(ref) != {
        "schema_version",
        "generation_id",
        "relpath",
        "sha256",
        "size_bytes",
        "resolved_generation_sha256",
        "depth",
    }:
        raise ResearchGenerationError("research generation reference fields do not match schema")
    if ref.get("schema_version") != GENERATION_REF_SCHEMA_VERSION:
        raise ResearchGenerationError("research generation reference schema mismatch")
    identity = _generation_identity(ref.get("generation_id"))
    if ref.get("relpath") != f"generations/{identity}.manifest.json":
        raise ResearchGenerationError("research generation reference path mismatch")
    return {
        **ref,
        "generation_id": f"generation:{identity}",
        "sha256": _sha256(ref.get("sha256"), "generation manifest"),
        "size_bytes": _positive_int(ref.get("size_bytes"), "generation manifest size"),
        "resolved_generation_sha256": _sha256(ref.get("resolved_generation_sha256"), "resolved generation"),
        "depth": _bounded_depth(ref.get("depth")),
    }


def validate_partition_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    ref = dict(value or {})
    if set(ref) != {
        "schema_version",
        "artifact_class",
        "relpath",
        "sha256",
        "size_bytes",
        "content_sha256",
        "content_size_bytes",
        "file_name",
        "file_schema",
        "row_count",
        "scope",
    }:
        raise ResearchGenerationError("research partition reference fields do not match schema")
    if (
        ref.get("schema_version") != PARTITION_REF_SCHEMA_VERSION
        or ref.get("artifact_class") != "immutable_shared_partition"
    ):
        raise ResearchGenerationError("research partition reference contract mismatch")
    digest = _sha256(ref.get("sha256"), "partition")
    if ref.get("relpath") != f"partitions/sha256/{digest[:2]}/{digest}.jsonl.gz":
        raise ResearchGenerationError("research partition path mismatch")
    scope = ref.get("scope")
    if (
        not isinstance(scope, dict)
        or set(scope)
        != {
            "schema_version",
            "market",
            "date",
            "account",
        }
        or any(not isinstance(item, str) or not item for item in scope.values())
    ):
        raise ResearchGenerationError("research partition scope is invalid")
    if (
        scope["market"] != scope["market"].strip().lower()
        or scope["account"] != scope["account"].strip().lower()
        or (scope["date"] != "unknown" and not _DATE.fullmatch(scope["date"]))
    ):
        raise ResearchGenerationError("research partition scope is not canonical")
    file_schema = _required_text(ref.get("file_schema"), "file_schema")
    if scope["schema_version"] != file_schema:
        raise ResearchGenerationError("research partition scope schema mismatch")
    return {
        **ref,
        "sha256": digest,
        "size_bytes": _positive_int(ref.get("size_bytes"), "partition size"),
        "content_sha256": _sha256(ref.get("content_sha256"), "partition content"),
        "content_size_bytes": _positive_int(ref.get("content_size_bytes"), "partition content size"),
        "file_name": _safe_file_name(ref.get("file_name")),
        "file_schema": file_schema,
        "row_count": _nonnegative_int(ref.get("row_count"), "row_count"),
        "scope": scope,
    }


def _partition_dataset(
    root: Path,
    *,
    required_files: tuple[str, ...],
    file_schemas: Mapping[str, str],
    parent_files: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    missing = [name for name in required_files if not (root / name).is_file() or (root / name).is_symlink()]
    if missing:
        raise ResearchGenerationError(f"required dataset file missing: {missing[0]}")
    jsonl_paths = list(root.glob("*.jsonl"))
    if any(path.is_symlink() or not path.is_file() for path in jsonl_paths):
        raise ResearchGenerationError("dataset JSONL input must be a regular file")
    names = sorted({*required_files, *(path.name for path in jsonl_paths)})
    files: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for name in names:
        path = root / _safe_file_name(name)
        parent_refs = list(parent_files.get(name, []))
        refs: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        total_bytes = 0
        row_count = 0
        chunks = _partition_chunks(path, expected_schema=file_schemas.get(name))
        for index, (payload, scope, rows, file_schema) in enumerate(chunks):
            digest.update(payload)
            total_bytes += len(payload)
            row_count += rows
            content_sha256 = hashlib.sha256(payload).hexdigest()
            if (
                index < len(parent_refs)
                and parent_refs[index]["content_sha256"] == content_sha256
                and parent_refs[index]["content_size_bytes"] == len(payload)
                and parent_refs[index]["scope"] == scope
                and parent_refs[index]["file_name"] == name
            ):
                refs.append(parent_refs[index])
                continue
            compressed = _gzip_bytes(payload)
            compressed_sha256 = hashlib.sha256(compressed).hexdigest()
            relpath = f"partitions/sha256/{compressed_sha256[:2]}/{compressed_sha256}.jsonl.gz"
            _write_once(root, relpath, compressed)
            refs.append(
                validate_partition_ref(
                    {
                        "schema_version": PARTITION_REF_SCHEMA_VERSION,
                        "artifact_class": "immutable_shared_partition",
                        "relpath": relpath,
                        "sha256": compressed_sha256,
                        "size_bytes": len(compressed),
                        "content_sha256": content_sha256,
                        "content_size_bytes": len(payload),
                        "file_name": name,
                        "file_schema": file_schema,
                        "row_count": rows,
                        "scope": scope,
                    }
                )
            )
        files[name] = refs
        summaries[name] = {
            "sha256": digest.hexdigest(),
            "bytes": total_bytes,
            "row_count": row_count,
        }
    return files, summaries


def _partition_chunks(path: Path, *, expected_schema: str | None):
    payload = bytearray()
    active_scope: dict[str, str] | None = None
    active_schema = expected_schema or "unversioned"
    rows = 0
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            row, scope, file_schema = _line_contract(
                line,
                path=path,
                line_number=line_number,
                expected_schema=expected_schema,
            )
            if payload and (scope != active_scope or rows + row > PARTITION_MAX_ROWS):
                yield bytes(payload), active_scope, rows, active_schema
                payload.clear()
                rows = 0
            if not payload:
                active_scope = scope
                active_schema = file_schema
            payload.extend(line)
            rows += row
    if payload:
        yield bytes(payload), active_scope, rows, active_schema


def _line_contract(
    line: bytes,
    *,
    path: Path,
    line_number: int,
    expected_schema: str | None,
) -> tuple[int, dict[str, str], str]:
    if not line.strip():
        schema = expected_schema or "unversioned"
        return 0, _scope(schema=schema), schema
    try:
        row = json.loads(
            line.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ResearchGenerationError) as exc:
        raise ResearchGenerationError(f"invalid JSONL at {path}:{line_number}") from exc
    if not isinstance(row, dict):
        raise ResearchGenerationError(f"JSONL row must be an object at {path}:{line_number}")
    row_schema = str(row.get("schema_version") or "").strip()
    if expected_schema and row_schema and row_schema != expected_schema:
        raise ResearchGenerationError(
            f"dataset schema mismatch: {path.name} expected {expected_schema}, got {row_schema}"
        )
    schema = expected_schema or row_schema or "unversioned"
    return 1, _scope(row, schema=schema), schema


def _scope(row: Mapping[str, Any] | None = None, *, schema: str) -> dict[str, str]:
    value = row or {}
    market = str(value.get("market") or value.get("source_market") or "unknown").strip().lower()
    account = str(value.get("account") or value.get("source_account") or "unknown").strip().lower()
    date = "unknown"
    for field in _DATE_FIELDS:
        raw = str(value.get(field) or "").strip()
        candidate = raw[:10]
        if _DATE.fullmatch(candidate):
            date = candidate
            break
    return {
        "schema_version": schema,
        "market": market or "unknown",
        "date": date,
        "account": account or "unknown",
    }


def _file_changes(
    before: Mapping[str, list[dict[str, Any]]],
    after: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name, [])
        new = after.get(name, [])
        prefix = 0
        while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
            prefix += 1
        if name in before and name in after and prefix == len(old) == len(new):
            continue
        changes.append(
            {
                "file_name": name,
                "prefix_count": prefix,
                "removed_sha256": [ref["sha256"] for ref in old[prefix:]],
                "added": new[prefix:],
                "delete_file": name not in after,
            }
        )
    return changes


def _load_manifest(root: Path, ref: Mapping[str, Any]) -> dict[str, Any]:
    value = validate_generation_ref(ref)
    payload = _read_regular(root, value["relpath"], max_bytes=MANIFEST_MAX_BYTES)
    if len(payload) != value["size_bytes"] or hashlib.sha256(payload).hexdigest() != value["sha256"]:
        raise ResearchGenerationError("research generation manifest hash or size mismatch")
    manifest = _strict_json_object(payload, "research generation manifest")
    generation_id = manifest.pop("generation_id", None)
    identity = _generation_identity(generation_id)
    if identity != _canonical_sha256(manifest) or generation_id != value["generation_id"]:
        raise ResearchGenerationError("research generation manifest identity mismatch")
    manifest["generation_id"] = generation_id
    _validate_manifest_shape(manifest, expected_ref=value)
    return manifest


def _validate_manifest_shape(manifest: dict[str, Any], *, expected_ref: Mapping[str, Any]) -> None:
    if set(manifest) != {
        "schema_version",
        "kind",
        "depth",
        "parent",
        "generation_store_root_relpath",
        "dataset",
        "provenance",
        "added_partition_sha256",
        "removed_partition_sha256",
        "files",
        "changes",
        "logical_summary",
        "resolved_generation_sha256",
        "generation_id",
    }:
        raise ResearchGenerationError("research generation manifest fields do not match schema")
    if manifest["schema_version"] != GENERATION_SCHEMA_VERSION or manifest["generation_store_root_relpath"] != "..":
        raise ResearchGenerationError("research generation manifest contract mismatch")
    kind = manifest.get("kind")
    depth = _bounded_depth(manifest.get("depth"))
    if kind not in {"base", "delta"} or (kind == "base") != (depth == 0):
        raise ResearchGenerationError("research generation kind or depth mismatch")
    if (
        depth != expected_ref["depth"]
        or manifest["resolved_generation_sha256"] != expected_ref["resolved_generation_sha256"]
    ):
        raise ResearchGenerationError("research generation reference binding mismatch")
    if not isinstance(manifest.get("dataset"), dict) or set(manifest["dataset"]) != {"schema_version", "dataset_id"}:
        raise ResearchGenerationError("research generation dataset binding is invalid")
    _required_text(manifest["dataset"].get("schema_version"), "dataset schema_version")
    _safe_dataset_id(manifest["dataset"].get("dataset_id"))
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {"legacy_revision", "manifest_projection"}:
        raise ResearchGenerationError("research generation provenance is invalid")
    _positive_int(provenance.get("legacy_revision"), "legacy_revision")
    if not isinstance(provenance.get("manifest_projection"), dict):
        raise ResearchGenerationError("research generation manifest projection is invalid")
    if any(
        provenance["manifest_projection"].get(key) != manifest["dataset"][key]
        for key in ("schema_version", "dataset_id")
    ):
        raise ResearchGenerationError("research generation provenance dataset mismatch")
    if not isinstance(manifest.get("files"), dict) or not isinstance(manifest.get("changes"), list):
        raise ResearchGenerationError("research generation file changes are invalid")
    if kind == "base" and (manifest["parent"] is not None or manifest["changes"]):
        raise ResearchGenerationError("research base generation has delta fields")
    if kind == "delta" and (not isinstance(manifest["parent"], dict) or manifest["files"]):
        raise ResearchGenerationError("research delta generation has base fields")
    for name, refs in manifest["files"].items():
        _safe_file_name(name)
        manifest["files"][name] = _partition_refs(refs, file_name=name)
    changed_names: set[str] = set()
    for change in manifest["changes"]:
        if not isinstance(change, dict) or set(change) != {
            "file_name",
            "prefix_count",
            "removed_sha256",
            "added",
            "delete_file",
        }:
            raise ResearchGenerationError("research generation change is invalid")
        name = _safe_file_name(change.get("file_name"))
        if name in changed_names:
            raise ResearchGenerationError("research generation changes a file more than once")
        changed_names.add(name)
        _nonnegative_int(change.get("prefix_count"), "prefix_count")
        if (
            not isinstance(change.get("removed_sha256"), list)
            or not isinstance(change.get("added"), list)
            or not isinstance(change.get("delete_file"), bool)
        ):
            raise ResearchGenerationError("research generation change lists are invalid")
        change["removed_sha256"] = [_sha256(digest, "removed partition") for digest in change["removed_sha256"]]
        change["added"] = _partition_refs(change["added"], file_name=name)
        if change["delete_file"] and (change["prefix_count"] or change["added"]):
            raise ResearchGenerationError("research generation file deletion is invalid")
    added = [ref["sha256"] for change in manifest["changes"] for ref in change["added"]]
    removed = [digest for change in manifest["changes"] for digest in change["removed_sha256"]]
    if not isinstance(manifest.get("added_partition_sha256"), list) or not isinstance(
        manifest.get("removed_partition_sha256"), list
    ):
        raise ResearchGenerationError("research generation partition summary is invalid")
    if manifest["added_partition_sha256"] != added or manifest["removed_partition_sha256"] != removed:
        raise ResearchGenerationError("research generation partition summary mismatch")


def _logical_summary(
    files: Mapping[str, list[dict[str, Any]]],
    summaries: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "file_count": len(files),
        "partition_reference_count": sum(len(refs) for refs in files.values()),
        "row_count": sum(int(item["row_count"]) for item in summaries.values()),
        "uncompressed_bytes": sum(int(item["bytes"]) for item in summaries.values()),
        "files": {name: dict(summaries[name]) for name in sorted(files)},
    }


def _validate_logical_summary(value: Any, files: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "file_count",
            "partition_reference_count",
            "row_count",
            "uncompressed_bytes",
            "files",
        }
        or not isinstance(value.get("files"), dict)
    ):
        raise ResearchGenerationError("research generation logical summary is invalid")
    summaries: dict[str, dict[str, Any]] = {}
    if set(value["files"]) != set(files):
        raise ResearchGenerationError("research generation logical file set mismatch")
    for name, item in value["files"].items():
        if not isinstance(item, dict) or set(item) != {"sha256", "bytes", "row_count"}:
            raise ResearchGenerationError("research generation logical file summary is invalid")
        summaries[name] = {
            "sha256": _sha256(item.get("sha256"), "logical file"),
            "bytes": _nonnegative_int(item.get("bytes"), "logical file bytes"),
            "row_count": _nonnegative_int(item.get("row_count"), "logical file rows"),
        }
        if summaries[name]["bytes"] != sum(ref["content_size_bytes"] for ref in files[name]) or summaries[name][
            "row_count"
        ] != sum(ref["row_count"] for ref in files[name]):
            raise ResearchGenerationError("research generation logical file totals mismatch")
    expected = _logical_summary(files, summaries)
    if value != expected:
        raise ResearchGenerationError("research generation logical totals mismatch")
    return expected


def _resolved_sha256(files: Mapping[str, Any], logical_summary: Mapping[str, Any]) -> str:
    return _canonical_sha256(
        {
            "files": {name: files[name] for name in sorted(files)},
            "logical_summary": logical_summary,
        }
    )


def _manifest_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    projected = {
        key: item for key, item in value.items() if key not in {"integrity", "generation", "files", "dataset_dir"}
    }
    return _strict_json_object(_canonical_bytes(projected), "dataset manifest projection")


def _load_partition(root: Path, ref: Mapping[str, Any]) -> bytes:
    value = validate_partition_ref(ref)
    compressed = _read_regular(root, value["relpath"], max_bytes=value["size_bytes"])
    if len(compressed) != value["size_bytes"] or hashlib.sha256(compressed).hexdigest() != value["sha256"]:
        raise ResearchGenerationError("research partition compressed hash or size mismatch")
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        payload = decoder.decompress(compressed, value["content_size_bytes"] + 1)
    except zlib.error as exc:
        raise ResearchGenerationError("research partition is not readable gzip") from exc
    if len(payload) > value["content_size_bytes"] or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ResearchGenerationError("research partition decompressed size is invalid")
    payload += decoder.flush()
    if (
        len(payload) != value["content_size_bytes"]
        or hashlib.sha256(payload).hexdigest() != value["content_sha256"]
        or _gzip_bytes(payload) != compressed
    ):
        raise ResearchGenerationError("research partition content mismatch")
    return payload


def _write_once(root: Path, relpath: str, payload: bytes) -> None:
    target = _bounded_path(root, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ResearchGenerationError("research generation target must not be a symlink")
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("research generation write made no progress")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if _read_regular(root, relpath, max_bytes=len(payload)) != payload:
                raise ResearchGenerationError("research generation immutable target conflicts")
        if _read_regular(root, relpath, max_bytes=len(payload)) != payload:
            raise ResearchGenerationError("research generation publication mismatch")
    except ResearchGenerationError:
        raise
    except OSError as exc:
        raise ResearchGenerationError("research generation publication failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_regular(root: Path, relpath: str, *, max_bytes: int) -> bytes:
    target = _bounded_path(root, relpath)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise ResearchGenerationError("research generation object size or type is invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ResearchGenerationError("research generation object exceeds declared size")
    except ResearchGenerationError:
        raise
    except OSError as exc:
        raise ResearchGenerationError("research generation object is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _bounded_path(root: Path, relpath: str) -> Path:
    path = Path(str(relpath or ""))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ResearchGenerationError("research generation path is invalid")
    target = root.joinpath(*path.parts)
    try:
        target.parent.resolve().relative_to(root)
    except (OSError, ValueError) as exc:
        raise ResearchGenerationError("research generation path escapes dataset") from exc
    return target


def _dataset_root(value: Path) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise ResearchGenerationError("research dataset root must not be a symlink")
    root = raw.resolve()
    if not root.is_dir():
        raise ResearchGenerationError("research dataset root is unavailable")
    return root


def _gzip_bytes(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=buffer, mtime=0) as handle:
        handle.write(payload)
    return buffer.getvalue()


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _strict_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ResearchGenerationError) as exc:
        raise ResearchGenerationError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ResearchGenerationError(f"{label} must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ResearchGenerationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ResearchGenerationError(f"non-finite JSON number: {value}")


def _generation_identity(value: Any) -> str:
    text = str(value or "").strip()
    if not text.startswith("generation:"):
        raise ResearchGenerationError("research generation id is invalid")
    return _sha256(text.removeprefix("generation:"), "generation id")


def _sha256(value: Any, label: str) -> str:
    digest = str(value or "").strip()
    if len(digest) != 64 or digest != digest.lower() or any(char not in _HEX for char in digest):
        raise ResearchGenerationError(f"research {label} sha256 is invalid")
    return digest


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ResearchGenerationError(f"research {label} is required")
    return text


def _safe_file_name(value: Any) -> str:
    name = _required_text(value, "file_name")
    if Path(name).name != name or "/" in name or "\\" in name or not name.endswith(".jsonl"):
        raise ResearchGenerationError("research generation file name is invalid")
    return name


def _safe_dataset_id(value: Any) -> str:
    dataset_id = _required_text(value, "dataset_id")
    if Path(dataset_id).name != dataset_id or dataset_id in {".", ".."} or "/" in dataset_id or "\\" in dataset_id:
        raise ResearchGenerationError("research dataset_id is invalid")
    return dataset_id


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResearchGenerationError(f"research {label} is invalid")
    return value


def _positive_int(value: Any, label: str) -> int:
    number = _nonnegative_int(value, label)
    if number <= 0:
        raise ResearchGenerationError(f"research {label} must be positive")
    return number


def _bounded_depth(value: Any) -> int:
    depth = _nonnegative_int(value, "generation depth")
    if depth > MAX_DELTA_DEPTH:
        raise ResearchGenerationError("research generation depth exceeds limit")
    return depth


def _partition_refs(value: Any, *, file_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ResearchGenerationError("research generation partition list is invalid")
    refs = [validate_partition_ref(ref) for ref in value]
    if any(ref["file_name"] != file_name for ref in refs):
        raise ResearchGenerationError("research partition file binding mismatch")
    return refs


__all__ = [
    "GENERATION_REF_SCHEMA_VERSION",
    "GENERATION_SCHEMA_VERSION",
    "MAX_DELTA_DEPTH",
    "PARTITION_REF_SCHEMA_VERSION",
    "ResearchGenerationError",
    "materialized_dataset_generation",
    "publish_dataset_generation",
    "resolve_dataset_generation",
    "validate_generation_ref",
    "validate_partition_ref",
]
