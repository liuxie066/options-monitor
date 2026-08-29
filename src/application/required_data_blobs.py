from __future__ import annotations

import base64
import binascii
import csv
from datetime import datetime, timezone
from functools import partial
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping, Sequence
from uuid import uuid4

import pandas as pd

from src.application.payload_helpers import required_text


REQUIRED_DATA_SCAN_BLOB_SCHEMA = "required_data_scan_blob.v1"
REQUIRED_DATA_SCAN_BLOB_REF_SCHEMA = "required_data_scan_blob_ref.v1"
REQUIRED_DATA_SCAN_BLOB_CODEC = "gzip"
REQUIRED_DATA_SCAN_BLOB_CODEC_VERSION = 1
_BLOB_CHUNK_BYTES = 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class RequiredDataBlobError(RuntimeError):
    """Raised when canonical required-data blob evidence is invalid."""


_required_text = partial(required_text, error=RequiredDataBlobError)


def publish_required_data_scan_blob(
    *,
    runtime_root: Path,
    symbol: str,
    market: str,
    raw_json_bytes: bytes,
    required_data_csv_bytes: bytes,
    columns: Sequence[str],
) -> dict[str, Any]:
    """Publish or adopt one deterministic canonical required-data blob."""

    root = _runtime_root(runtime_root)
    payload = build_required_data_scan_blob_payload(
        symbol=symbol,
        market=market,
        raw_json_bytes=raw_json_bytes,
        required_data_csv_bytes=required_data_csv_bytes,
        columns=columns,
    )
    canonical = _canonical_scan_blob_bytes_unchecked(payload)
    digest = hashlib.sha256(canonical).hexdigest()
    compressed = _gzip_bytes(canonical)
    relpath = _blob_relpath(digest)
    target = _write_once_or_adopt_blob(
        runtime_root=root,
        digest=digest,
        compressed=compressed,
    )
    metadata = target.stat(follow_symlinks=False)
    published_at = (
        datetime.fromtimestamp(
            metadata.st_ctime,
            tz=timezone.utc,
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    ref = {
        "schema_version": REQUIRED_DATA_SCAN_BLOB_REF_SCHEMA,
        "blob_schema_version": REQUIRED_DATA_SCAN_BLOB_SCHEMA,
        "logical_roles": ["raw_json", "required_data_csv"],
        "codec": REQUIRED_DATA_SCAN_BLOB_CODEC,
        "codec_version": REQUIRED_DATA_SCAN_BLOB_CODEC_VERSION,
        "blob_sha256": digest,
        "uncompressed_size_bytes": len(canonical),
        "compressed_size_bytes": len(compressed),
        "blob_relpath": relpath,
        "published_at_utc": published_at,
    }
    validate_required_data_scan_blob_ref(ref)
    return ref


def build_required_data_scan_blob_payload(
    *,
    symbol: str,
    market: str,
    raw_json_bytes: bytes,
    required_data_csv_bytes: bytes,
    columns: Sequence[str],
) -> dict[str, Any]:
    """Build the single canonical provider payload plus sparse CSV overrides."""

    symbol_norm = _required_text(symbol, "symbol").upper()
    market_norm = _required_text(market, "market").upper()
    if market_norm not in {"US", "HK"}:
        raise RequiredDataBlobError("required-data blob market is invalid")
    column_names = [_required_text(item, "column") for item in columns]
    if not column_names or len(column_names) != len(set(column_names)):
        raise RequiredDataBlobError("required-data blob columns are invalid")
    try:
        provider_payload = json.loads(
            bytes(raw_json_bytes).decode("utf-8"),
            parse_constant=_reject_nonfinite_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RequiredDataBlobError("required-data raw JSON is unreadable") from exc
    if not isinstance(provider_payload, dict):
        raise RequiredDataBlobError("required-data raw JSON must be an object")
    if str(provider_payload.get("symbol") or "").strip().upper() != symbol_norm:
        raise RequiredDataBlobError("required-data raw JSON symbol mismatch")
    provider_payload = _json_safe(provider_payload)
    rows = provider_payload.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RequiredDataBlobError("required-data provider rows are invalid")

    base_csv = _render_provider_csv(rows=rows, columns=column_names)
    overrides = _projection_overrides(
        provider_csv_bytes=base_csv,
        final_csv_bytes=bytes(required_data_csv_bytes),
        columns=column_names,
    )
    payload = {
        "schema_version": REQUIRED_DATA_SCAN_BLOB_SCHEMA,
        "symbol": symbol_norm,
        "market": market_norm,
        "provider_payload": provider_payload,
        "projection": {
            "columns": column_names,
            "multiplier_overrides": overrides,
        },
        "legacy_integrity": {
            "raw_json_sha256": hashlib.sha256(raw_json_bytes).hexdigest(),
            "raw_json_size_bytes": len(raw_json_bytes),
            "required_data_csv_sha256": hashlib.sha256(required_data_csv_bytes).hexdigest(),
            "required_data_csv_size_bytes": len(required_data_csv_bytes),
        },
    }
    if _materialize_raw_json_unchecked(payload) != bytes(raw_json_bytes):
        raise RequiredDataBlobError("canonical required-data blob cannot exactly materialize raw JSON")
    materialized = _materialize_csv_unchecked(payload)
    if materialized != bytes(required_data_csv_bytes):
        raise RequiredDataBlobError("canonical required-data blob cannot exactly materialize final CSV")
    return payload


def canonical_scan_blob_bytes(payload: Mapping[str, Any]) -> bytes:
    validated, _csv_bytes, _raw_bytes = _validate_required_data_scan_blob_payload(payload)
    return _canonical_scan_blob_bytes_unchecked(validated)


def _canonical_scan_blob_bytes_unchecked(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def load_required_data_scan_blob(
    *,
    runtime_root: Path,
    blob_ref: Mapping[str, Any],
) -> dict[str, Any]:
    """Read, decompress, hash, and validate one exact canonical blob."""

    root = _runtime_root(runtime_root)
    ref = validate_required_data_scan_blob_ref(blob_ref)
    descriptor = _open_blob_for_read(runtime_root=root, ref=ref)
    try:
        compressed_size = os.fstat(descriptor).st_size
        if compressed_size != ref["compressed_size_bytes"]:
            raise RequiredDataBlobError("required-data blob compressed size mismatch")
        with os.fdopen(descriptor, "rb", closefd=False) as compressed_file:
            with gzip.GzipFile(fileobj=compressed_file, mode="rb") as handle:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = handle.read(_BLOB_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > ref["uncompressed_size_bytes"]:
                        raise RequiredDataBlobError("required-data blob exceeds declared uncompressed size")
                    chunks.append(chunk)
                canonical = b"".join(chunks)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise RequiredDataBlobError("required-data blob is unreadable") from exc
    finally:
        os.close(descriptor)
    if len(canonical) != ref["uncompressed_size_bytes"]:
        raise RequiredDataBlobError("required-data blob uncompressed size mismatch")
    if hashlib.sha256(canonical).hexdigest() != ref["blob_sha256"]:
        raise RequiredDataBlobError("required-data blob content hash mismatch")
    try:
        payload = json.loads(canonical.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequiredDataBlobError("required-data blob JSON is unreadable") from exc
    validated, csv_bytes, raw_bytes = _validate_required_data_scan_blob_payload(payload)
    if _canonical_scan_blob_bytes_unchecked(validated) != canonical:
        raise RequiredDataBlobError("required-data blob bytes are not canonical")
    return {
        "payload": validated,
        "canonical_bytes": canonical,
        "raw_json_bytes": raw_bytes,
        "required_data_csv_bytes": csv_bytes,
    }


def validate_required_data_scan_blob_ref(
    blob_ref: Mapping[str, Any],
) -> dict[str, Any]:
    value = dict(blob_ref or {})
    if set(value) != {
        "schema_version",
        "blob_schema_version",
        "logical_roles",
        "codec",
        "codec_version",
        "blob_sha256",
        "uncompressed_size_bytes",
        "compressed_size_bytes",
        "blob_relpath",
        "published_at_utc",
    }:
        raise RequiredDataBlobError("required-data blob ref fields do not match schema")
    if (
        value.get("schema_version") != REQUIRED_DATA_SCAN_BLOB_REF_SCHEMA
        or value.get("blob_schema_version") != REQUIRED_DATA_SCAN_BLOB_SCHEMA
        or value.get("logical_roles") != ["raw_json", "required_data_csv"]
        or value.get("codec") != REQUIRED_DATA_SCAN_BLOB_CODEC
        or value.get("codec_version") != REQUIRED_DATA_SCAN_BLOB_CODEC_VERSION
    ):
        raise RequiredDataBlobError("required-data blob ref contract mismatch")
    digest = _sha256(value.get("blob_sha256"), "blob_sha256")
    uncompressed_size = _nonnegative_int(
        value.get("uncompressed_size_bytes"),
        "uncompressed_size_bytes",
    )
    compressed_size = _nonnegative_int(
        value.get("compressed_size_bytes"),
        "compressed_size_bytes",
    )
    if uncompressed_size <= 0 or compressed_size <= 0:
        raise RequiredDataBlobError("required-data blob sizes must be positive")
    if value.get("blob_relpath") != _blob_relpath(digest):
        raise RequiredDataBlobError("required-data blob ref path mismatch")
    published_at = _utc_timestamp(value.get("published_at_utc"))
    return {
        **value,
        "blob_sha256": digest,
        "uncompressed_size_bytes": uncompressed_size,
        "compressed_size_bytes": compressed_size,
        "published_at_utc": published_at,
    }


def required_data_scan_blob_ref_identity(
    blob_ref: Mapping[str, Any],
) -> dict[str, Any]:
    """Return content/location identity; publication time is runtime-local."""

    ref = validate_required_data_scan_blob_ref(blob_ref)
    return {key: value for key, value in ref.items() if key != "published_at_utc"}


def required_data_shadow_base64_matches(
    encoded: Any,
    expected_bytes: bytes,
) -> bool:
    """Compare legacy inline bytes without materializing a decoded copy."""

    if not isinstance(encoded, str):
        return False
    try:
        ascii_bytes = encoded.encode("ascii")
    except UnicodeEncodeError:
        return False
    if len(ascii_bytes) % 4:
        return False
    digest = hashlib.sha256()
    decoded_size = 0
    chunk_size = 4 * (_BLOB_CHUNK_BYTES // 4)
    try:
        for offset in range(0, len(ascii_bytes), chunk_size):
            decoded = base64.b64decode(
                ascii_bytes[offset : offset + chunk_size],
                validate=True,
            )
            decoded_size += len(decoded)
            digest.update(decoded)
    except (ValueError, binascii.Error):
        return False
    return decoded_size == len(expected_bytes) and digest.digest() == hashlib.sha256(expected_bytes).digest()


def required_data_shadow_file_matches(
    path: Path,
    expected_bytes: bytes,
) -> bool:
    """Hash-compare a legacy shadow file through a no-follow descriptor."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            Path(path),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(expected_bytes):
            return False
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _BLOB_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        return digest.digest() == hashlib.sha256(expected_bytes).digest()
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def fsync_required_data_bound_file(
    *,
    root: Path,
    relpath: str,
    expected_bytes: bytes,
) -> None:
    """Flush one exact bound artifact and every directory needed to reach it."""

    opened = _open_bound_leaf(root=root, relpath=relpath, allow_absent=False)
    assert opened is not None
    directories, descriptor, _name = opened
    try:
        if not _descriptor_matches(descriptor, expected_bytes):
            raise RequiredDataBlobError(
                "required-data bound artifact changed before cleanup"
            )
        os.fsync(descriptor)
        for directory in reversed(directories):
            os.fsync(directory)
    except RequiredDataBlobError:
        raise
    except OSError as exc:
        raise RequiredDataBlobError(
            "required-data bound artifact durability failed"
        ) from exc
    finally:
        os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def retire_required_data_shadow_file(
    *,
    root: Path,
    relpath: str,
    expected_bytes: bytes,
) -> str:
    """Remove one verified legacy shadow without following path symlinks."""

    opened = _open_bound_leaf(root=root, relpath=relpath, allow_absent=True)
    if opened is None:
        return "absent"
    directories, descriptor, name = opened
    try:
        opened_stat = os.fstat(descriptor)
        if not _descriptor_matches(descriptor, expected_bytes):
            raise RequiredDataBlobError("required-data shadow content mismatch")
        current_stat = os.stat(
            name,
            dir_fd=directories[-1],
            follow_symlinks=False,
        )
        opened_identity = (
            opened_stat.st_dev,
            opened_stat.st_ino,
            opened_stat.st_size,
            opened_stat.st_nlink,
        )
        current_identity = (
            current_stat.st_dev,
            current_stat.st_ino,
            current_stat.st_size,
            current_stat.st_nlink,
        )
        if opened_identity != current_identity:
            raise RequiredDataBlobError(
                "required-data shadow identity changed before cleanup"
            )
        os.unlink(name, dir_fd=directories[-1])
        return "removed"
    except RequiredDataBlobError:
        raise
    except OSError as exc:
        raise RequiredDataBlobError("required-data shadow cleanup failed") from exc
    finally:
        os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def _open_bound_leaf(
    *,
    root: Path,
    relpath: str,
    allow_absent: bool,
) -> tuple[list[int], int, str] | None:
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or os.unlink not in os.supports_dir_fd
    ):
        raise RequiredDataBlobError(
            "required-data bound artifact primitives are unavailable"
        )
    base = _runtime_root(root)
    parts = _bound_relpath_parts(relpath)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directories: list[int] = []
    try:
        directories.append(os.open(base, flags))
        for component in parts[:-1]:
            directories.append(
                os.open(component, flags, dir_fd=directories[-1])
            )
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directories[-1],
        )
        return directories, descriptor, parts[-1]
    except FileNotFoundError:
        for directory in reversed(directories):
            os.close(directory)
        if allow_absent:
            return None
        raise RequiredDataBlobError(
            "required-data bound artifact is unavailable"
        ) from None
    except OSError as exc:
        for directory in reversed(directories):
            os.close(directory)
        raise RequiredDataBlobError(
            "required-data bound artifact path is unsafe"
        ) from exc


def _bound_relpath_parts(relpath: str) -> tuple[str, ...]:
    text = str(relpath or "").strip()
    pure = PurePosixPath(text)
    if (
        not text
        or pure.is_absolute()
        or text != pure.as_posix()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise RequiredDataBlobError(
            "required-data bound artifact path is invalid"
        )
    return tuple(pure.parts)


def _descriptor_matches(descriptor: int, expected_bytes: bytes) -> bool:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != len(expected_bytes)
    ):
        return False
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, _BLOB_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.digest() == hashlib.sha256(expected_bytes).digest()


def _validate_required_data_scan_blob_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes, bytes]:
    value = dict(payload or {})
    if set(value) != {
        "schema_version",
        "symbol",
        "market",
        "provider_payload",
        "projection",
        "legacy_integrity",
    }:
        raise RequiredDataBlobError("required-data scan blob fields do not match schema")
    if value.get("schema_version") != REQUIRED_DATA_SCAN_BLOB_SCHEMA:
        raise RequiredDataBlobError("required-data scan blob schema mismatch")
    symbol = _required_text(value.get("symbol"), "symbol").upper()
    market = _required_text(value.get("market"), "market").upper()
    if market not in {"US", "HK"}:
        raise RequiredDataBlobError("required-data scan blob market is invalid")
    provider = value.get("provider_payload")
    if not isinstance(provider, dict):
        raise RequiredDataBlobError("required-data provider payload is invalid")
    if str(provider.get("symbol") or "").strip().upper() != symbol:
        raise RequiredDataBlobError("required-data provider symbol mismatch")
    rows = provider.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RequiredDataBlobError("required-data provider rows are invalid")
    projection = value.get("projection")
    if not isinstance(projection, dict) or set(projection) != {
        "columns",
        "multiplier_overrides",
    }:
        raise RequiredDataBlobError("required-data projection is invalid")
    columns_raw = projection.get("columns")
    if not isinstance(columns_raw, list):
        raise RequiredDataBlobError("required-data projection columns are invalid")
    columns = [_required_text(item, "column") for item in columns_raw]
    if not columns or len(columns) != len(set(columns)) or "multiplier" not in columns:
        raise RequiredDataBlobError("required-data projection columns are invalid")
    overrides_raw = projection.get("multiplier_overrides")
    if not isinstance(overrides_raw, list):
        raise RequiredDataBlobError("required-data multiplier overrides are invalid")
    overrides: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for item in overrides_raw:
        if not isinstance(item, dict) or set(item) != {
            "row_index",
            "row_identity",
            "multiplier",
        }:
            raise RequiredDataBlobError("required-data multiplier override is invalid")
        index = _nonnegative_int(item.get("row_index"), "row_index")
        if index >= len(rows) or index in seen_indexes:
            raise RequiredDataBlobError("required-data multiplier override index is invalid")
        seen_indexes.add(index)
        expected_identity = _row_identity(rows[index])
        if item.get("row_identity") != expected_identity:
            raise RequiredDataBlobError("required-data multiplier row identity mismatch")
        try:
            multiplier = float(item.get("multiplier"))
        except (TypeError, ValueError) as exc:
            raise RequiredDataBlobError("required-data multiplier is invalid") from exc
        if not math.isfinite(multiplier) or multiplier <= 0:
            raise RequiredDataBlobError("required-data multiplier is invalid")
        raw_multiplier = rows[index].get("multiplier")
        try:
            raw_number = float(raw_multiplier)
        except (TypeError, ValueError):
            raw_number = math.nan
        if math.isfinite(raw_number) and raw_number > 0:
            raise RequiredDataBlobError("required-data multiplier override cannot replace a valid provider value")
        overrides.append(
            {
                "row_index": index,
                "row_identity": expected_identity,
                "multiplier": multiplier,
            }
        )
    if [item["row_index"] for item in overrides] != sorted(seen_indexes):
        raise RequiredDataBlobError("required-data multiplier overrides are not ordered")

    integrity = value.get("legacy_integrity")
    if not isinstance(integrity, dict) or set(integrity) != {
        "raw_json_sha256",
        "raw_json_size_bytes",
        "required_data_csv_sha256",
        "required_data_csv_size_bytes",
    }:
        raise RequiredDataBlobError("required-data legacy integrity is invalid")
    normalized_integrity = {
        "raw_json_sha256": _sha256(integrity.get("raw_json_sha256"), "raw_json_sha256"),
        "raw_json_size_bytes": _nonnegative_int(integrity.get("raw_json_size_bytes"), "raw_json_size_bytes"),
        "required_data_csv_sha256": _sha256(
            integrity.get("required_data_csv_sha256"),
            "required_data_csv_sha256",
        ),
        "required_data_csv_size_bytes": _nonnegative_int(
            integrity.get("required_data_csv_size_bytes"),
            "required_data_csv_size_bytes",
        ),
    }
    normalized = {
        "schema_version": REQUIRED_DATA_SCAN_BLOB_SCHEMA,
        "symbol": symbol,
        "market": market,
        "provider_payload": _json_safe(provider),
        "projection": {
            "columns": columns,
            "multiplier_overrides": overrides,
        },
        "legacy_integrity": normalized_integrity,
    }
    csv_bytes = _materialize_csv_unchecked(normalized)
    if (
        len(csv_bytes) != normalized_integrity["required_data_csv_size_bytes"]
        or hashlib.sha256(csv_bytes).hexdigest() != normalized_integrity["required_data_csv_sha256"]
    ):
        raise RequiredDataBlobError("required-data CSV materialization hash mismatch")
    raw_bytes = _materialize_raw_json_unchecked(normalized)
    if (
        len(raw_bytes) != normalized_integrity["raw_json_size_bytes"]
        or hashlib.sha256(raw_bytes).hexdigest() != normalized_integrity["raw_json_sha256"]
    ):
        raise RequiredDataBlobError("required-data raw JSON materialization hash mismatch")
    return normalized, csv_bytes, raw_bytes


def _materialize_raw_json_unchecked(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload["provider_payload"],
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _materialize_csv_unchecked(payload: Mapping[str, Any]) -> bytes:
    projection = payload["projection"]
    columns = list(projection["columns"])
    rows = list(payload["provider_payload"]["rows"])
    initial = _render_provider_csv(rows=rows, columns=columns)
    overrides = list(projection["multiplier_overrides"])
    if not overrides:
        return initial
    frame = pd.read_csv(io.BytesIO(initial))
    if len(frame.index) != len(rows) or list(frame.columns) != columns:
        raise RequiredDataBlobError("required-data materialized frame shape mismatch")
    for item in overrides:
        index = int(item["row_index"])
        if _row_identity(frame.iloc[index].to_dict()) != item["row_identity"]:
            raise RequiredDataBlobError("required-data materialized row identity mismatch")
        frame.loc[index, "multiplier"] = float(item["multiplier"])
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def _render_provider_csv(*, rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    frame = pd.DataFrame.from_records(rows, columns=columns)
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def _projection_overrides(
    *,
    provider_csv_bytes: bytes,
    final_csv_bytes: bytes,
    columns: list[str],
) -> list[dict[str, Any]]:
    provider_rows = _csv_rows(provider_csv_bytes, columns=columns)
    final_rows = _csv_rows(final_csv_bytes, columns=columns)
    if len(provider_rows) != len(final_rows):
        raise RequiredDataBlobError("required-data CSV row count mismatch")
    overrides: list[dict[str, Any]] = []
    for index, (provider, final) in enumerate(zip(provider_rows, final_rows)):
        provider_identity = _row_identity(provider)
        if _row_identity(final) != provider_identity:
            raise RequiredDataBlobError("required-data CSV row order or identity mismatch")
        changed = [
            column
            for column in columns
            if provider[column] != final[column]
            and not _equivalent_csv_number(provider[column], final[column])
        ]
        if not changed:
            continue
        if changed != ["multiplier"]:
            raise RequiredDataBlobError("required-data CSV differs outside multiplier enrichment")
        try:
            multiplier = float(final["multiplier"])
            provider_multiplier = float(provider["multiplier"])
        except (TypeError, ValueError):
            provider_multiplier = math.nan
            try:
                multiplier = float(final["multiplier"])
            except (TypeError, ValueError) as exc:
                raise RequiredDataBlobError("required-data CSV multiplier enrichment is invalid") from exc
        if (
            not math.isfinite(multiplier)
            or multiplier <= 0
            or (math.isfinite(provider_multiplier) and provider_multiplier > 0)
        ):
            raise RequiredDataBlobError("required-data CSV multiplier enrichment is invalid")
        overrides.append(
            {
                "row_index": index,
                "row_identity": provider_identity,
                "multiplier": multiplier,
            }
        )
    return overrides


def _equivalent_csv_number(left: str, right: str) -> bool:
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(left_number) or not math.isfinite(right_number):
        return False
    if left_number == right_number:
        return True
    # ponytail: two derivation paths (provider projection vs pipeline CSV) can
    # reorder float ops and land a few ULP apart (production case: 3 ULP on
    # otm_pct). Relative tolerance is far tighter than any financial meaning.
    scale = max(abs(left_number), abs(right_number), 1.0)
    return abs(left_number - right_number) <= 1e-12 * scale


def _csv_rows(payload: bytes, *, columns: list[str]) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        if reader.fieldnames != columns:
            raise RequiredDataBlobError("required-data CSV columns mismatch")
        rows = [dict(row) for row in reader]
    except (UnicodeDecodeError, csv.Error) as exc:
        raise RequiredDataBlobError("required-data CSV is unreadable") from exc
    if any(set(row) != set(columns) or any(value is None for value in row.values()) for row in rows):
        raise RequiredDataBlobError("required-data CSV row shape mismatch")
    return rows


def _row_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    symbol = _required_text(row.get("symbol"), "row symbol").upper()
    option_type = _required_text(row.get("option_type"), "row option_type").lower()
    expiration = _required_text(row.get("expiration"), "row expiration")[:10]
    contract_symbol = _required_text(row.get("contract_symbol"), "row contract_symbol")
    try:
        strike = float(row.get("strike"))
    except (TypeError, ValueError) as exc:
        raise RequiredDataBlobError("required-data row strike is invalid") from exc
    if option_type not in {"put", "call"} or len(expiration) != 10 or not math.isfinite(strike):
        raise RequiredDataBlobError("required-data row identity is invalid")
    return {
        "symbol": symbol,
        "option_type": option_type,
        "expiration": expiration,
        "contract_symbol": contract_symbol,
        "strike": format(strike, ".17g"),
    }


def _gzip_bytes(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=buffer,
        mtime=0,
    ) as handle:
        handle.write(payload)
    return buffer.getvalue()


def _blob_relpath(digest: str) -> str:
    return f"output_shared/blobs/sha256/{digest[:2]}/{digest}.json.gz"


def _write_once_or_adopt_blob(
    *,
    runtime_root: Path,
    digest: str,
    compressed: bytes,
) -> Path:
    components = ("output_shared", "blobs", "sha256", digest[:2])
    directory = _open_directory_chain(runtime_root, components, create=True)
    name = f"{digest}.json.gz"
    temporary = f".{name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
            dir_fd=directory,
        )
        view = memoryview(compressed)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("required-data blob write made no progress")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_regular_at(directory, name)
            if existing != compressed:
                raise RequiredDataBlobError("required-data blob destination conflicts")
        os.fsync(directory)
        if _read_regular_at(directory, name) != compressed:
            raise RequiredDataBlobError("required-data blob publication mismatch")
    except RequiredDataBlobError:
        raise
    except OSError as exc:
        raise RequiredDataBlobError("required-data blob publication failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(directory)
    return runtime_root / PurePosixPath(_blob_relpath(digest))


def _open_blob_for_read(*, runtime_root: Path, ref: Mapping[str, Any]) -> int:
    digest = str(ref["blob_sha256"])
    directory = _open_directory_chain(
        runtime_root,
        ("output_shared", "blobs", "sha256", digest[:2]),
        create=False,
    )
    try:
        descriptor = os.open(
            f"{digest}.json.gz",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RequiredDataBlobError("required-data blob is not a regular file")
        return descriptor
    except RequiredDataBlobError:
        raise
    except OSError as exc:
        raise RequiredDataBlobError("required-data blob is unavailable") from exc
    finally:
        os.close(directory)


def _open_directory_chain(
    root: Path,
    components: tuple[str, ...],
    *,
    create: bool,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(root, flags | getattr(os, "O_NOFOLLOW", 0))
        for component in components:
            _safe_component(component)
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                os.fsync(descriptor)
            child = os.open(
                component,
                flags | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except RequiredDataBlobError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise RequiredDataBlobError("required-data blob path is unavailable or unsafe") from exc


def _read_regular_at(directory: int, name: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RequiredDataBlobError("required-data blob is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, _BLOB_CHUNK_BYTES)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except RequiredDataBlobError:
        raise
    except OSError as exc:
        raise RequiredDataBlobError("required-data blob is unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _runtime_root(value: Path) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise RequiredDataBlobError("runtime root must not be a symlink")
    try:
        root = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RequiredDataBlobError("runtime root is unavailable") from exc
    if not root.is_dir():
        raise RequiredDataBlobError("runtime root is not a directory")
    return root


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    raise RequiredDataBlobError(f"required-data payload contains unsupported {type(value).__name__}")


def _reject_nonfinite_json_number(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _sha256(value: Any, field: str) -> str:
    digest = str(value or "").strip()
    if len(digest) != 64 or digest != digest.lower() or any(char not in _HEX for char in digest):
        raise RequiredDataBlobError(f"required-data {field} is invalid")
    return digest


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RequiredDataBlobError(f"required-data {field} is invalid")
    return value


def _utc_timestamp(value: Any) -> str:
    text = _required_text(value, "published_at_utc")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RequiredDataBlobError("required-data published_at_utc is invalid") from exc
    if parsed.tzinfo is None:
        raise RequiredDataBlobError("required-data published_at_utc lacks timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_component(value: str) -> str:
    if not value or value in {".", ".."} or Path(value).name != value or "/" in value or "\\" in value:
        raise RequiredDataBlobError("required-data blob path component is invalid")
    return value


__all__ = [
    "REQUIRED_DATA_SCAN_BLOB_REF_SCHEMA",
    "REQUIRED_DATA_SCAN_BLOB_SCHEMA",
    "RequiredDataBlobError",
    "build_required_data_scan_blob_payload",
    "canonical_scan_blob_bytes",
    "fsync_required_data_bound_file",
    "load_required_data_scan_blob",
    "publish_required_data_scan_blob",
    "required_data_scan_blob_ref_identity",
    "required_data_shadow_base64_matches",
    "required_data_shadow_file_matches",
    "retire_required_data_shadow_file",
    "validate_required_data_scan_blob_ref",
]
