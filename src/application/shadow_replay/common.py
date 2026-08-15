from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATASET_SCHEMA_VERSION = "shadow_replay_dataset.v1"
ANALYSIS_SCHEMA_VERSION = "shadow_replay_analysis.v1"
READINESS_SCHEMA_VERSION = "shadow_replay_readiness.v1"

CANDIDATE_SNAPSHOT_SCHEMA_VERSION = "shadow_replay_candidate_snapshot.v1"
FILTER_DECISION_SCHEMA_VERSION = "shadow_replay_filter_decision.v1"
RANK_SNAPSHOT_SCHEMA_VERSION = "shadow_replay_rank_snapshot.v1"
MARK_PATH_SCHEMA_VERSION = "shadow_replay_mark_path_snapshot.v1"
OUTCOME_FACT_SCHEMA_VERSION = "shadow_replay_outcome_fact.v1"
CLOSE_DECISION_EPISODE_SCHEMA_VERSION = "shadow_replay_close_episode.v1"
CLOSE_DECISION_MARK_SCHEMA_VERSION = "shadow_replay_close_mark.v1"
CLOSE_DECISION_OUTCOME_SCHEMA_VERSION = "shadow_replay_close_outcome.v1"

DATASET_FILES = (
    "candidate_snapshots.jsonl",
    "filter_decisions.jsonl",
    "rank_snapshots.jsonl",
    "mark_path_snapshots.jsonl",
    "outcome_facts.jsonl",
)

OPTIONAL_CLOSE_DATASET_FILES = (
    "close_decision_episodes.jsonl",
    "close_decision_marks.jsonl",
    "close_decision_outcomes.jsonl",
)

DATASET_FILE_SCHEMAS = {
    "candidate_snapshots.jsonl": CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
    "filter_decisions.jsonl": FILTER_DECISION_SCHEMA_VERSION,
    "rank_snapshots.jsonl": RANK_SNAPSHOT_SCHEMA_VERSION,
    "mark_path_snapshots.jsonl": MARK_PATH_SCHEMA_VERSION,
    "outcome_facts.jsonl": OUTCOME_FACT_SCHEMA_VERSION,
    "close_decision_episodes.jsonl": CLOSE_DECISION_EPISODE_SCHEMA_VERSION,
    "close_decision_marks.jsonl": CLOSE_DECISION_MARK_SCHEMA_VERSION,
    "close_decision_outcomes.jsonl": CLOSE_DECISION_OUTCOME_SCHEMA_VERSION,
}


def dataset_dir_from_arg(dataset: str | Path) -> Path:
    path = Path(dataset).expanduser().resolve()
    if path.is_file():
        return path.parent
    return path


def dataset_output_dir(output_dir: str | Path | None, *, dataset_id: str, base: Path) -> Path:
    if output_dir:
        return resolve_path(output_dir, base=base)
    return (base / "output_shared" / "research" / "shadow_replay" / "datasets" / dataset_id).resolve()


def resolve_output_path(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    resolved = raw.resolve()
    repo_research_root = (
        Path(__file__).resolve().parents[3]
        / "output_shared"
        / "research"
    ).resolve()
    try:
        managed_research_root = resolved.is_relative_to(repo_research_root)
    except AttributeError:
        try:
            resolved.relative_to(repo_research_root)
            managed_research_root = True
        except ValueError:
            managed_research_root = False
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        under_temp = resolved.is_relative_to(temp_root)
    except AttributeError:
        try:
            resolved.relative_to(temp_root)
            under_temp = True
        except ValueError:
            under_temp = False
    if not managed_research_root and not under_temp:
        raise ValueError(
            "research output must be under output_shared/research or the system temporary directory"
        )
    protected_names = {
        ".env",
        "VERSION",
        "config.yaml",
        "config.yml",
        "config.us.json",
        "config.hk.json",
        "option_positions.sqlite3",
    }
    protected_parts = {
        "ledger",
        "locks",
        "runtime_state",
        "state",
        "trade_events",
    }
    name_lower = resolved.name.lower()
    if (
        resolved.name in protected_names
        or name_lower.startswith("config.")
        or resolved.suffix.lower()
        in {".db", ".sqlite", ".sqlite3", ".service", ".timer", ".socket", ".plist"}
        or protected_parts.intersection(part.lower() for part in resolved.parts)
    ):
        raise ValueError(f"research output target is protected: {resolved}")
    if raw.is_symlink():
        raise ValueError(f"research output target must not be a symlink: {raw}")
    return resolved


def default_dataset_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_optional(value: str | Path | None, *, base: Path) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return resolve_path(value, base=base)


def resolve_many(values: list[str | Path] | tuple[str | Path, ...] | None, *, base: Path) -> list[Path]:
    return [resolve_path(value, base=base) for value in (values or []) if str(value or "").strip()]


def resolve_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def glob_many(directory: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    out: list[Path] = []
    for pattern in patterns:
        out.extend(path.resolve() for path in directory.glob(pattern) if path.is_file())
    return out


def unique(paths: Any) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in paths or []:
        path = Path(raw).resolve()
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def safe_rel(path: Path | None, *, base: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except Exception:
        return str(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSONL at {path}:{line_number}: {exc.msg}"
                ) from exc
            if not isinstance(item, dict):
                raise ValueError(
                    f"JSONL row must be an object at {path}:{line_number}"
                )
            out.append(item)
    return out


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    import csv

    if not path.exists() or not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, render_json_text(payload))


def render_json_text(payload: dict[str, Any]) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
        for row in rows
    )
    _atomic_write_text(path, content)


def write_text_artifact(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, str(content))


def attach_artifact_provenance(
    payload: dict[str, Any],
    *,
    artifact_kind: str,
    source_generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content_sha256 = artifact_content_sha256(payload)
    payload["artifact_provenance"] = {
        "schema_version": "research_artifact_provenance.v1",
        "artifact_kind": str(artifact_kind),
        "artifact_id": f"{artifact_kind}:{content_sha256[:24]}",
        "content_sha256": content_sha256,
        "source_generation": dict(source_generation or {}),
    }
    return payload


def artifact_content_sha256(payload: dict[str, Any]) -> str:
    source = {
        key: value
        for key, value in payload.items()
        if key != "artifact_provenance"
    }
    encoded = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_artifact_provenance(
    payload: dict[str, Any],
    *,
    artifact_kind: str,
    schema_version: str,
) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("schema_version") != schema_version:
        errors.append("schema_version_mismatch")
    provenance = payload.get("artifact_provenance")
    if not isinstance(provenance, dict):
        errors.append("artifact_provenance_missing")
        provenance = {}
    if provenance.get("schema_version") != "research_artifact_provenance.v1":
        errors.append("provenance_schema_version_mismatch")
    if provenance.get("artifact_kind") != artifact_kind:
        errors.append("artifact_kind_mismatch")
    actual_sha256 = artifact_content_sha256(payload)
    if provenance.get("content_sha256") != actual_sha256:
        errors.append("content_sha256_mismatch")
    expected_id = f"{artifact_kind}:{actual_sha256[:24]}"
    if provenance.get("artifact_id") != expected_id:
        errors.append("artifact_id_mismatch")
    source_generation = provenance.get("source_generation")
    if not isinstance(source_generation, dict) or not source_generation.get("generation_id"):
        errors.append("source_generation_missing")
    return {
        "trusted": not errors,
        "errors": errors,
        "artifact_id": provenance.get("artifact_id"),
        "content_sha256": provenance.get("content_sha256"),
        "source_generation": source_generation if isinstance(source_generation, dict) else {},
    }


def _atomic_write_text(path: Path, content: str) -> None:
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


@contextmanager
def dataset_write_lock(dataset_dir: Path):
    import fcntl

    dataset_dir.mkdir(parents=True, exist_ok=True)
    lock_path = dataset_dir / ".dataset.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def dataset_read_lock(dataset_dir: Path):
    import fcntl

    lock_path = dataset_dir / ".dataset.lock"
    if not lock_path.is_file():
        yield
        return
    with lock_path.open("r", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def dataset_integrity_payload(
    dataset_dir: Path,
    *,
    generation_id: str,
    revision: int,
) -> dict[str, Any]:
    files: dict[str, Any] = {}
    required_paths = [dataset_dir / name for name in DATASET_FILES]
    missing_required = [path for path in required_paths if not path.is_file()]
    if missing_required:
        raise ValueError(f"required dataset file missing: {missing_required[0]}")
    dataset_paths = sorted(
        {
            *required_paths,
            *(path for path in dataset_dir.glob("*.jsonl") if path.is_file()),
        },
        key=lambda path: path.name,
    )
    for path in dataset_paths:
        name = path.name
        rows = read_jsonl(path)
        expected_schema = DATASET_FILE_SCHEMAS.get(name)
        if expected_schema:
            invalid_schemas = sorted(
                {
                    str(row.get("schema_version"))
                    for row in rows
                    if row.get("schema_version") is not None
                    and row.get("schema_version") != expected_schema
                }
            )
            if invalid_schemas:
                raise ValueError(
                    f"dataset schema mismatch: {name} expected "
                    f"{expected_schema}, got {', '.join(invalid_schemas)}"
                )
        raw = path.read_bytes()
        files[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "row_count": len(rows),
        }
    return {
        "schema_version": "shadow_replay_dataset_integrity.v1",
        "generation_id": generation_id,
        "revision": int(revision),
        "completed_at_utc": utc_now(),
        "files": files,
    }


def refresh_dataset_manifest(dataset_dir: Path) -> dict[str, Any]:
    manifest_path = dataset_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid dataset manifest JSON: {manifest_path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"dataset manifest must be an object: {manifest_path}")
        manifest = payload
    previous = manifest.get("integrity")
    previous = previous if isinstance(previous, dict) else {}
    generation_id = text(previous.get("generation_id")) or (
        f"generation:{hashlib.sha256(str(dataset_dir).encode('utf-8')).hexdigest()[:24]}"
    )
    revision = int(previous.get("revision") or 0) + 1
    manifest.setdefault("schema_version", DATASET_SCHEMA_VERSION)
    manifest.setdefault("dataset_id", dataset_dir.name)
    manifest.setdefault("dataset_dir", str(dataset_dir))
    manifest["integrity"] = dataset_integrity_payload(
        dataset_dir,
        generation_id=generation_id,
        revision=revision,
    )
    write_json(manifest_path, manifest)
    return manifest


def validate_dataset_integrity(
    dataset_dir: Path,
    *,
    require_manifest: bool = True,
) -> dict[str, Any]:
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        if require_manifest:
            raise ValueError(f"dataset manifest missing: {manifest_path}")
        return {"status": "legacy_unverified", "reason": "manifest_missing"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid dataset manifest JSON: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"dataset manifest must be an object: {manifest_path}")
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        if require_manifest:
            raise ValueError(f"dataset integrity receipt missing: {manifest_path}")
        return {"status": "legacy_unverified", "reason": "integrity_receipt_missing"}
    expected_files = integrity.get("files")
    if not isinstance(expected_files, dict):
        raise ValueError(f"dataset integrity files missing: {manifest_path}")
    actual = dataset_integrity_payload(
        dataset_dir,
        generation_id=text(integrity.get("generation_id")),
        revision=int(integrity.get("revision") or 0),
    )
    for name, actual_item in actual["files"].items():
        expected = expected_files.get(name)
        if not isinstance(expected, dict):
            raise ValueError(f"dataset integrity entry missing: {name}")
        for field in ("sha256", "bytes", "row_count"):
            if expected.get(field) != actual_item.get(field):
                raise ValueError(
                    f"dataset integrity mismatch: {name}.{field}"
                )
    extra = sorted(set(expected_files) - set(actual["files"]))
    if extra:
        raise ValueError(
            f"dataset integrity references missing file(s): {', '.join(extra)}"
        )
    return {
        "status": "verified",
        "generation_id": integrity.get("generation_id"),
        "revision": integrity.get("revision"),
        "files": expected_files,
    }


def safety_payload(*, writes_local_dataset: bool) -> dict[str, Any]:
    return {
        "offline_only": True,
        "read_only_sources": True,
        "writes_local_dataset_only": bool(writes_local_dataset),
        "writes_runtime_config": False,
        "writes_trade_state": False,
        "sends_notifications": False,
    }


def instrument_key(row: dict[str, Any]) -> str:
    """Return the reusable quote-subject identity.

    A contract quote may legitimately be reused by multiple decisions.  Do not
    use this key to join decision evidence; use ``decision_instance_key``.
    """

    contract = text(row.get("contract_symbol") or row.get("option_symbol"))
    if contract:
        return contract.upper()
    symbol = text(row.get("symbol") or row.get("underlying_symbol")).upper()
    option_type = text(row.get("option_type") or row.get("mode")).lower()
    expiration = text(row.get("expiration") or row.get("exp"))
    strike = text(row.get("strike"))
    if not all((symbol, option_type, expiration, strike)):
        return ""
    parts = [text(row.get("account")).lower(), symbol, option_type, expiration, strike]
    return "|".join(parts).strip("|")


def decision_instance_key(row: dict[str, Any], *, source_index: int | None = None) -> str:
    """Return a stable decision-occurrence identity.

    Explicit producer identities are authoritative.  Legacy rows are scoped by
    every stable occurrence field they carry.  Contract-only legacy evidence
    remains readable, but cannot match a newly captured scoped decision.
    """

    explicit = text(row.get("decision_instance_id"))
    if explicit:
        return explicit
    quote_key = instrument_key(row)
    if not quote_key:
        return ""
    run_id = text(row.get("run_id") or row.get("source_run_id"))
    account = text(row.get("account")).lower()
    source_path = text(row.get("source_path") or row.get("_source_path"))
    source_row = text(
        row.get("source_row_number")
        or row.get("_source_row_number")
        or source_index
    )
    observed_at = text(
        row.get("decision_at_utc")
        or row.get("observed_at_utc")
        or row.get("captured_at_utc")
    )
    stage = text(row.get("filter_stage") or row.get("stage")).lower()
    status = normal_status(row.get("status") or row.get("candidate_status"))
    group_id = text(row.get("strategy_group_id") or row.get("group_id"))
    occurrence = text(row.get("group_occurrence_id") or row.get("candidate_pair_id"))
    if not any((run_id, account, source_path, source_row, observed_at, stage, group_id, occurrence)):
        return f"legacy:{quote_key}"
    identity = {
        "run_id": run_id or None,
        "account": account or None,
        "quote_subject": quote_key,
        "decision_time": observed_at or None,
        "stage": stage or None,
        "status": status,
        "strategy_group_id": group_id or None,
        "group_occurrence_id": occurrence or None,
        "source_path": source_path or None,
        "source_row_number": source_row or None,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"decision:{digest}"


def group_occurrence_key(row: dict[str, Any]) -> str:
    """Return a run/account-scoped Combo occurrence key."""

    explicit = text(row.get("group_occurrence_id"))
    if explicit:
        return explicit
    group_id = text(row.get("strategy_group_id") or row.get("group_id"))
    if not group_id:
        return ""
    run_id = text(row.get("run_id") or row.get("source_run_id"))
    account = text(row.get("account")).lower()
    source_path = text(row.get("source_path") or row.get("_source_path"))
    source_parent = str(Path(source_path).parent) if source_path else ""
    pair_id = text(row.get("candidate_pair_id"))
    if not any((run_id, account, source_parent, pair_id)):
        return f"legacy-group:{group_id}"
    identity = {
        "run_id": run_id or None,
        "account": account or None,
        "strategy_group_id": group_id,
        "candidate_pair_id": pair_id or None,
        "source_parent": source_parent or None,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"group:{digest}"


def with_decision_identity(row: dict[str, Any], *, source_index: int | None = None) -> dict[str, Any]:
    """Copy a candidate row and freeze its decision/group occurrence ids."""

    payload = dict(row)
    decision_id = decision_instance_key(payload, source_index=source_index)
    if decision_id:
        payload["decision_instance_id"] = decision_id
    group_id = group_occurrence_key(payload)
    if group_id:
        payload["group_occurrence_id"] = group_id
    return payload


def bind_legacy_decision_evidence(
    candidates: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind legacy evidence only when one compatible decision is possible."""

    candidates_by_quote: dict[str, list[dict[str, Any]]] = {}
    for index, candidate in enumerate(candidates, start=1):
        quote_key = instrument_key(candidate)
        if not quote_key:
            continue
        scoped = (
            candidate
            if text(candidate.get("decision_instance_id"))
            else with_decision_identity(candidate, source_index=index)
        )
        candidates_by_quote.setdefault(quote_key, []).append(scoped)

    bound: list[dict[str, Any]] = []
    for row in rows:
        if text(row.get("decision_instance_id")):
            bound.append(dict(row))
            continue
        quote_key = instrument_key(row)
        matches = [
            candidate
            for candidate in candidates_by_quote.get(quote_key, [])
            if _legacy_scope_compatible(row, candidate)
        ]
        payload = dict(row)
        if len(matches) == 1:
            candidate = matches[0]
            payload["decision_instance_id"] = candidate.get("decision_instance_id")
            payload.setdefault("group_occurrence_id", candidate.get("group_occurrence_id"))
            payload.setdefault("run_id", candidate.get("run_id"))
            payload.setdefault("account", candidate.get("account"))
            payload["evidence_binding_status"] = "bound_unambiguous_legacy"
        elif len(matches) > 1:
            payload["evidence_binding_status"] = "ambiguous_legacy_decision"
        else:
            payload["evidence_binding_status"] = "unmatched_legacy_decision"
        bound.append(payload)
    return bound


def freeze_decision_identities(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        (
            dict(candidate)
            if text(candidate.get("decision_instance_id"))
            else with_decision_identity(candidate, source_index=index)
        )
        for index, candidate in enumerate(candidates, start=1)
    ]


def _legacy_scope_compatible(evidence: dict[str, Any], candidate: dict[str, Any]) -> bool:
    for key, normalize in (
        ("run_id", text),
        ("account", lambda value: text(value).lower()),
        ("strategy_group_id", text),
        ("group_occurrence_id", text),
    ):
        expected = normalize(evidence.get(key))
        actual = normalize(candidate.get(key))
        if expected and expected != actual:
            return False
    evidence_status = text(evidence.get("candidate_status")).lower()
    if evidence_status and evidence_status != normal_status(candidate.get("status")):
        return False
    return True


def first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def abs_first_float(row: dict[str, Any], *keys: str) -> float | None:
    value = first_float(row, *keys)
    return abs(value) if value is not None else None


def float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    text_value = raw.rstrip("%")
    if not text_value:
        return None
    try:
        parsed = float(text_value)
    except Exception:
        return None
    if raw.endswith("%"):
        return parsed / 100.0
    return parsed


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_date(value: str) -> Any:
    value_text = text(value)
    if not value_text:
        return None
    if len(value_text) >= 10 and value_text[4:5] == "-" and value_text[7:8] == "-":
        value_text = value_text[:10]
    try:
        return datetime.strptime(value_text, "%Y-%m-%d").date()
    except Exception:
        return None


def account_hint(path: Path) -> str | None:
    parts = list(path.parts)
    for marker in ("accounts", "output_accounts"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return str(parts[idx + 1]).strip().lower() or None
    return None


def strategy_hint(path: Path) -> str | None:
    name = path.name.lower()
    if "combo_yield" in name or "yield_enhancement" in name:
        return "combo_yield"
    if "sell_call" in name:
        return "sell_call"
    if "sell_put" in name:
        return "sell_put"
    return None


def strategy_mode(strategy: str | None) -> str | None:
    if strategy == "sell_put":
        return "put"
    if strategy == "sell_call":
        return "call"
    return None


def normal_status(value: Any) -> str:
    value_text = text(value).lower()
    if value_text in {"accepted", "rejected", "post_filtered", "ranked_below", "notified"}:
        return value_text
    return value_text or "unknown"
