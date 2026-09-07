from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from domain.domain.trade_execution import EXECUTION_INPUT_VERSION, normalize_execution_input


MAX_EXECUTION_FILE_BYTES = 10 * 1024 * 1024
MAX_EXECUTION_FILE_ROWS = 10_000


def run_execution_file(
    path: str | Path,
    *,
    process_payload_fn: Callable[..., dict[str, Any]],
    configured_accounts: Iterable[Mapping[str, Any]],
    dry_run: bool = True,
    max_bytes: int = MAX_EXECUTION_FILE_BYTES,
    max_rows: int = MAX_EXECUTION_FILE_ROWS,
) -> dict[str, Any]:
    """Read explicit UTF-8 execution JSONL through the shared intake boundary."""
    if max_bytes <= 0 or max_rows <= 0:
        raise ValueError("execution file limits must be positive")
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError("execution input must be a regular file")
    with file_path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"execution file exceeds {max_bytes} bytes")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("execution file must be UTF-8") from exc
    if len(lines) > max_rows:
        raise ValueError(f"execution file exceeds {max_rows} rows")
    if not lines:
        raise ValueError("execution file must contain at least one execution")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        try:
            payload = json.loads(line, object_pairs_hook=_unique_json_object,
                                 parse_constant=_reject_json_constant)
        except ValueError as exc:
            raise ValueError(f"execution file line {line_number} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"execution file line {line_number} must be a JSON object")
        rows.append(payload)

    bindings: dict[tuple[str, str, str], dict[str, Any]] = {}
    for configured in configured_accounts:
        binding = dict(configured)
        key = _account_key(binding)
        label = str(binding.get("account_label") or "").strip()
        if not all(key) or not label or label != label.lower() or key in bindings:
            raise ValueError("execution file requires unambiguous configured physical accounts")
        bindings[key] = binding

    file_sha256 = hashlib.sha256(raw).hexdigest()
    results: list[dict[str, Any]] = []
    for line_number, (payload, raw_line) in enumerate(zip(rows, lines), 1):
        validation_payload, errors = _bounded_decimal_payload(payload)
        execution = normalize_execution_input(validation_payload)
        errors.extend(execution["errors"])
        if payload.get("schema_version") != EXECUTION_INPUT_VERSION:
            errors.append("unsupported:file_schema_version")
        if "execution_input" in payload:
            errors.append("unsupported:file_nested_execution_input")
        ref = execution["broker_account_ref"]
        binding = bindings.get(_account_key(ref))
        identity_unbound = binding is None or "execution_input" in payload
        if binding is None:
            errors.append("unbound:broker_account_ref")
        else:
            supplied_label = ref.get("account_label")
            if supplied_label and supplied_label != binding["account_label"]:
                errors.append("conflict:broker_account_ref.account_label")
                identity_unbound = True
            configured_id = binding.get("broker_account_id")
            if configured_id and ref.get("broker_account_id") != configured_id:
                errors.append("conflict:broker_account_ref.broker_account_id")
                identity_unbound = True
        incoming = dict(validation_payload)
        if binding is not None and not identity_unbound:
            incoming["broker_account_ref"] = {
                **dict(payload.get("broker_account_ref") or {}),
                "account_label": binding["account_label"],
            }
        incoming["_trade_intake_file_evidence"] = {
            "file_sha256": file_sha256, "line_number": line_number, "raw_line": raw_line,
        }
        incoming["_trade_intake_file_errors"] = sorted(set(errors))
        incoming["_trade_intake_file_identity_unbound"] = identity_unbound
        result = process_payload_fn(
            incoming, source="file", allow_external_lookup=False, apply_changes=not dry_run,
        )
        results.append({**result, "line_number": line_number})
    counts = Counter(str(result.get("status") or "unknown") for result in results)
    return {"status": "previewed" if dry_run else "completed", "dry_run": dry_run,
            "file_sha256": file_sha256, "row_count": len(rows),
            "result_counts": dict(counts), "results": results}


def _account_key(ref: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(ref.get("broker_id") or "").strip().lower(),
        str(ref.get("external_account_id") or "").strip(),
        str(ref.get("environment") or "").strip().upper(),
    )


def _bounded_decimal_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    candidate = deepcopy(payload)
    errors: list[str] = []
    fields = [(candidate, "quantity"), (candidate, "price")]
    instrument = candidate.get("instrument_ref")
    if isinstance(instrument, dict):
        fields.extend((instrument, key) for key in ("strike", "multiplier") if key in instrument)
        deliverable = instrument.get("deliverable")
        if isinstance(deliverable, dict):
            fields.extend((deliverable, key) for key in ("quantity", "amount", "multiplier", "ratio") if key in deliverable)
    for owner, key in fields:
        value = owner.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 128
                                  or re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value) is None):
            errors.append(f"invalid:file_decimal:{key}")
            owner[key] = None
    return candidate, errors


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")
