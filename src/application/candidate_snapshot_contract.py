from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Iterable, Mapping


OPENING_BROKER_SNAPSHOT_SCHEMA = "opening_candidate_snapshot.v4"
BENIGN_OPENING_NOT_APPLICABLE_REASONS = frozenset(
    {"covered_call_underlying_not_held"}
)


REQUIRED_CANDIDATE_DEPENDENCIES = frozenset(
    {"required_data", "portfolio", "ledger", "fx", "earnings_rv"}
)
EXPERIENCE_CANDIDATE_DEPENDENCIES = frozenset(
    {"required_data", "fx", "earnings_rv"}
)
CANDIDATE_RUN_MODE_FIELDS = frozenset(
    {"scan_mode", "capacity_source", "account_display_name", "executable"}
)
CANDIDATE_CAPTURE_STATUSES = frozenset(
    {"completed", "not_applicable", "failed", "incomplete", "unavailable"}
)
_CURRENT_CANDIDATE_OWNER_FILES = {
    "opening": "opening_candidate_snapshot.json",
    "sp_lc": "combo_yield_candidate_snapshot.json",
    "cc_lp": "cc_lp_candidate_snapshot.json",
    "wheel": "wheel_candidate_snapshot.v3.json",
}
_CURRENT_CANDIDATE_OWNER_SCHEMAS = {
    "opening": "opening_candidate_snapshot.v3",
    "sp_lc": "combo_yield_candidate_snapshot.v4",
    "cc_lp": "cc_lp_candidate_snapshot.v4",
    "wheel": "wheel_candidate_snapshot.v3",
}
_LEGACY_CANDIDATE_ROOT_FILES = (
    "strategy_scan_status_index.v1.json",
    "strategy_scan_status_index.v2.json",
    "strategy_scan_status_index.v3.json",
    "strategy_scan_status_index.v4.json",
)
_LEGACY_CANDIDATE_STATE_FILES = (
    "candidate_snapshot_manifest.v1.json",
    "candidate_snapshot_manifest.v2.json",
    "candidate_snapshot_manifest.v3.json",
    "wheel_candidate_snapshot.json",
    "wheel_candidate_snapshot.v2.json",
)
_LEGACY_CANDIDATE_STATUS_PATTERNS = (
    "*_scan_status.json",
    "*_scan_status.v2.json",
)
_CURRENT_CANDIDATE_MANIFEST_FILE = "candidate_snapshot_manifest.v4.json"


class CandidateSnapshotContractError(ValueError):
    """Raised when shared candidate-snapshot evidence is not canonical."""


def assert_current_candidate_artifact_boundary(
    *,
    account_dir: Path,
    target: Path | None = None,
) -> None:
    """Reject legacy or post-seal additions before a current artifact write."""

    root = Path(account_dir)
    state_dir = root / "state"
    try:
        if root.is_symlink() or state_dir.is_symlink():
            raise CandidateSnapshotContractError("artifact_version_mismatch")
        legacy_paths = [root / name for name in _LEGACY_CANDIDATE_ROOT_FILES]
        legacy_paths.extend(
            state_dir / name for name in _LEGACY_CANDIDATE_STATE_FILES
        )
        for pattern in _LEGACY_CANDIDATE_STATUS_PATTERNS:
            legacy_paths.extend(root.glob(pattern))
        if any(path.exists() or path.is_symlink() for path in legacy_paths):
            raise CandidateSnapshotContractError("artifact_version_mismatch")

        for owner, filename in _CURRENT_CANDIDATE_OWNER_FILES.items():
            path = state_dir / filename
            if not (path.exists() or path.is_symlink()):
                continue
            if path.is_symlink() or not path.is_file():
                raise CandidateSnapshotContractError("artifact_version_mismatch")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateSnapshotContractError(
                    "artifact_version_mismatch"
                ) from exc
            if not isinstance(payload, Mapping) or not is_current_candidate_owner_schema(
                owner, payload.get("schema_version")
            ):
                raise CandidateSnapshotContractError("artifact_version_mismatch")

        manifest_path = state_dir / _CURRENT_CANDIDATE_MANIFEST_FILE
        if manifest_path.exists() or manifest_path.is_symlink():
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise CandidateSnapshotContractError("artifact_version_mismatch")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateSnapshotContractError(
                    "artifact_version_mismatch"
                ) from exc
            if not isinstance(manifest, Mapping) or manifest.get(
                "schema_version"
            ) != "candidate_snapshot_manifest.v4":
                raise CandidateSnapshotContractError("artifact_version_mismatch")
            if target is not None:
                target_path = Path(target)
                if not (target_path.exists() or target_path.is_symlink()):
                    raise CandidateSnapshotContractError("artifact_version_mismatch")
    except CandidateSnapshotContractError:
        raise
    except OSError as exc:
        raise CandidateSnapshotContractError("artifact_version_mismatch") from exc


def candidate_run_mode_fields(
    *,
    experience: bool,
    account_display_name: str | None = None,
) -> dict[str, Any]:
    """Build the single persisted mode discriminator for candidate evidence."""

    if not experience:
        if str(account_display_name or "").strip():
            raise CandidateSnapshotContractError(
                "standard candidate mode cannot have an account display name"
            )
        return {"scan_mode": "standard", "executable": True}
    display_name = required_text(
        account_display_name,
        "experience account display name",
    )
    return {
        "scan_mode": "experience",
        "capacity_source": "demo_scenario",
        "account_display_name": display_name,
        "executable": False,
    }


def current_candidate_owner_schema(owner: Any) -> str:
    """Return the default schema (including experience mode) for one owner."""

    owner_norm = required_text(owner, "candidate owner").lower()
    try:
        return _CURRENT_CANDIDATE_OWNER_SCHEMAS[owner_norm]
    except KeyError as exc:
        raise CandidateSnapshotContractError("candidate owner is invalid") from exc


def is_current_candidate_owner_schema(owner: str, schema: Any) -> bool:
    """Opening v4 adds explicit broker identity; existing owner schemas remain frozen."""
    return schema == current_candidate_owner_schema(owner) or (
        owner == "opening" and schema == OPENING_BROKER_SNAPSHOT_SCHEMA
    )


def validate_candidate_run_mode(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and project the current candidate run-mode discriminator."""

    scan_mode = str(payload.get("scan_mode") or "").strip().lower()
    if scan_mode == "standard":
        expected = candidate_run_mode_fields(experience=False)
    elif scan_mode == "experience":
        expected = candidate_run_mode_fields(
            experience=True,
            account_display_name=str(payload.get("account_display_name") or ""),
        )
    else:
        raise CandidateSnapshotContractError("candidate scan mode is invalid")
    present = {
        key: payload.get(key)
        for key in CANDIDATE_RUN_MODE_FIELDS
        if key in payload
    }
    if present != expected:
        raise CandidateSnapshotContractError("candidate run mode contract is invalid")
    return expected


def required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CandidateSnapshotContractError(f"{field} is required")
    return text


def sha256_text(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CandidateSnapshotContractError(f"{field} is invalid")
    return text


def utc_timestamp(value: datetime | str | Any, field: str = "sealed_at_utc") -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = required_text(value, field)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CandidateSnapshotContractError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise CandidateSnapshotContractError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_json_value(value: Any, *, field: str = "value") -> Any:
    """Return canonical JSON-safe values without stringifying unknown objects."""

    if value is None or isinstance(value, (str, bool)):
        return value
    # Handle pandas.NA/NaT before the date/numeric branches.  ``NaT`` is a
    # datetime subclass and ``NA`` exposes ``item`` but neither is a value that
    # may be serialized into immutable candidate evidence.
    type_name = f"{type(value).__module__}.{type(value).__name__}"
    if type(value).__name__ in {"NAType", "NaTType"} and type_name.startswith(
        "pandas."
    ):
        return None
    if isinstance(value, datetime):
        return utc_timestamp(value, field)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CandidateSnapshotContractError(f"{field} is non-finite")
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        numeric = float(value)
        if math.isnan(numeric):
            return None
        if not math.isfinite(numeric):
            raise CandidateSnapshotContractError(f"{field} is non-finite")
        return numeric
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise CandidateSnapshotContractError(f"{field} contains a non-string key")
            out[raw_key] = normalize_json_value(
                raw_value,
                field=f"{field}.{raw_key}",
            )
        return out
    if isinstance(value, (list, tuple)):
        return [
            normalize_json_value(item, field=f"{field}[{index}]")
            for index, item in enumerate(value)
        ]

    # NumPy scalar values expose ``item``.  Pandas missing scalars commonly
    # raise from bool conversion but are represented by this same scalar path.
    item_fn = getattr(value, "item", None)
    if callable(item_fn):
        try:
            item = item_fn()
        except (TypeError, ValueError) as exc:
            raise CandidateSnapshotContractError(
                f"{field} cannot be normalized"
            ) from exc
        if item is value:
            raise CandidateSnapshotContractError(f"{field} has an unsupported type")
        return normalize_json_value(item, field=field)

    raise CandidateSnapshotContractError(f"{field} has an unsupported type")


def normalize_dependencies(
    rows: Iterable[Mapping[str, Any]],
    *,
    verify_root: Path | None = None,
    required_kinds: frozenset[str] = REQUIRED_CANDIDATE_DEPENDENCIES,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise CandidateSnapshotContractError("candidate dependency must be an object")
        kind = required_text(raw.get("kind"), "dependency kind")
        if kind in seen:
            raise CandidateSnapshotContractError(f"duplicate dependency kind: {kind}")
        seen.add(kind)
        relpath_value = raw.get("relpath")
        relpath: str | None
        if relpath_value in (None, ""):
            relpath = None
        else:
            relpath = str(relpath_value)
            candidate = Path(relpath)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise CandidateSnapshotContractError(f"invalid dependency path: {kind}")
        digest = sha256_text(raw.get("sha256"), f"{kind} dependency hash")
        if verify_root is not None and relpath is not None:
            root = Path(verify_root).resolve()
            target = (root / relpath).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise CandidateSnapshotContractError(
                    f"candidate dependency escapes runtime root: {kind}"
                ) from exc
            if not target.is_file() or target.is_symlink():
                raise CandidateSnapshotContractError(
                    f"candidate dependency is missing: {kind}"
                )
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise CandidateSnapshotContractError(
                    f"candidate dependency hash mismatch: {kind}"
                )
        out.append({"kind": kind, "relpath": relpath, "sha256": digest})
    missing = sorted(required_kinds - seen)
    if missing:
        raise CandidateSnapshotContractError(
            "candidate dependencies are incomplete: " + ",".join(missing)
        )
    extra = sorted(seen - required_kinds)
    if extra:
        raise CandidateSnapshotContractError(
            "candidate dependencies contain unknown kinds: " + ",".join(extra)
        )
    return sorted(out, key=lambda row: str(row["kind"]))


def dependency_hash(rows: Iterable[Mapping[str, Any]], kind: str) -> str:
    for item in rows:
        if str(item.get("kind") or "") == kind:
            return sha256_text(item.get("sha256"), f"{kind} dependency hash")
    raise CandidateSnapshotContractError(f"candidate dependency is missing: {kind}")


def required_data_coverage_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve optional expiration coverage evidence across candidate projections."""

    if "required_data_coverage" not in row:
        return {}
    raw = row["required_data_coverage"]
    if not isinstance(raw, Mapping):
        raise CandidateSnapshotContractError("required-data coverage must be an object")
    coverage = normalize_json_value(dict(raw), field="required_data_coverage")
    if (
        coverage.get("status") != "partial"
        or coverage.get("reason_code") != "required_data_partial_coverage"
    ):
        raise CandidateSnapshotContractError("required-data partial coverage status is invalid")

    def expiration_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise CandidateSnapshotContractError("required-data coverage expirations are invalid")
        for expiration in value:
            try:
                valid = isinstance(expiration, str) and date.fromisoformat(expiration).isoformat() == expiration
            except ValueError:
                valid = False
            if not valid:
                raise CandidateSnapshotContractError("required-data coverage expiration is invalid")
        if len(set(value)) != len(value):
            raise CandidateSnapshotContractError("required-data coverage expiration is duplicated")
        return value

    covered = expiration_list(coverage.get("covered_expirations"))
    incomplete = expiration_list(coverage.get("incomplete_expirations"))
    if set(covered) & set(incomplete):
        raise CandidateSnapshotContractError("required-data coverage expiration states overlap")
    rows = coverage.get("per_expiration")
    if not isinstance(rows, list) or not rows or any(not isinstance(item, Mapping) for item in rows):
        raise CandidateSnapshotContractError("required-data per-expiration coverage is invalid")
    expirations = expiration_list([item.get("expiration") for item in rows])
    if set(expirations) != set(covered) | set(incomplete):
        raise CandidateSnapshotContractError("required-data coverage expiration summary mismatch")
    allowed_missing = {"lower_bound", "upper_bound", "strike_window", "exact_strike", "rows"}
    for item in rows:
        is_covered = item["expiration"] in covered
        if item.get("status") not in ({"covered"} if is_covered else {"incomplete", "missing"}):
            raise CandidateSnapshotContractError("required-data expiration coverage status mismatch")
        missing = item.get("missing_requirements")
        if not isinstance(missing, list) or bool(missing) == is_covered:
            raise CandidateSnapshotContractError("required-data missing requirements are invalid")
        for requirement in missing:
            if not isinstance(requirement, Mapping):
                raise CandidateSnapshotContractError("required-data missing requirement is invalid")
            index = requirement.get("request_index")
            gaps = requirement.get("missing")
            if (
                isinstance(index, bool) or not isinstance(index, int) or index < 0
                or requirement.get("option_type") not in {"put", "call"}
                or not isinstance(gaps, list) or not gaps
                or any(not isinstance(gap, str) or gap not in allowed_missing for gap in gaps)
            ):
                raise CandidateSnapshotContractError("required-data missing requirement scope is invalid")
            if "missing_exact_strikes" in requirement:
                strikes = requirement["missing_exact_strikes"]
                if (
                    "exact_strike" not in gaps or not isinstance(strikes, list)
                    or any(isinstance(strike, bool) or not isinstance(strike, (int, float))
                           or not math.isfinite(strike) for strike in strikes)
                ):
                    raise CandidateSnapshotContractError("required-data missing exact strikes are invalid")
    range_evidence = any("strike_coverage" in item for item in rows)
    if range_evidence:
        for item in rows:
            _validate_strike_coverage(item)
        if coverage.get("ranking_complete") is not False or coverage.get("ranking_scope") != "observed_contracts":
            raise CandidateSnapshotContractError("partial required-data ranking must remain incomplete")
        if coverage.get("partial_candidate_policy") != "retain_and_mark":
            raise CandidateSnapshotContractError("partial required-data candidate policy is invalid")
    elif not covered or not incomplete:
        raise CandidateSnapshotContractError("legacy required-data partial coverage needs both date states")
    expiration = row.get("expiration")
    if expiration is not None:
        detail = next((item for item in rows if item["expiration"] == expiration), None)
        if detail is None or detail["status"] == "missing":
            raise CandidateSnapshotContractError("candidate belongs to an unavailable expiration")
        if detail["status"] == "incomplete":
            strike = row.get("strike")
            side = row.get("option_type")
            scopes = detail.get("strike_coverage", [])
            if not any(
                scope.get("option_type") == side
                and strike in scope.get("covered_strikes", [])
                and any(lower <= strike <= upper for lower, upper in scope.get("covered_ranges", []))
                for scope in scopes
            ) or any("exact_strike" in gap["missing"] for gap in detail["missing_requirements"]):
                raise CandidateSnapshotContractError("candidate is outside observed required-data coverage")
        if row.get("required_data_expiration_status", detail["status"]) != detail["status"]:
            raise CandidateSnapshotContractError("candidate required-data expiration status mismatch")
    if range_evidence and any(
        key in row and row[key] != expected for key, expected in (
            ("ranking_complete", False), ("ranking_scope", "observed_contracts"),
            ("partial_candidate_policy", "retain_and_mark"),
        )
    ):
        raise CandidateSnapshotContractError("candidate contradicts partial required-data ranking")
    out = {"required_data_coverage": coverage}
    for key in ("required_data_expiration_status", "ranking_complete", "ranking_scope", "partial_candidate_policy"):
        if key in row:
            out[key] = row[key]
    return out


def _validate_strike_coverage(detail: Mapping[str, Any]) -> None:
    """Validate stored range evidence at the existing snapshot contract boundary."""
    scopes = detail.get("strike_coverage")
    if not isinstance(scopes, list) or not scopes:
        raise CandidateSnapshotContractError("required-data strike coverage scopes are invalid")
    for scope in scopes:
        if not isinstance(scope, Mapping):
            raise CandidateSnapshotContractError("required-data strike coverage scope is invalid")
        index = scope.get("request_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or scope.get("option_type") not in {"put", "call"}:
            raise CandidateSnapshotContractError("required-data strike coverage identity is invalid")
        observed = scope.get("covered_strikes")
        if not isinstance(observed, list) or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
            for value in observed
        ) or observed != sorted(set(observed)):
            raise CandidateSnapshotContractError("required-data observed strikes are invalid")
        expected = [[observed[0], observed[-1]]] if observed else []
        if scope.get("covered_ranges") != expected:
            raise CandidateSnapshotContractError("required-data covered range does not match observations")
        window = scope.get("strike_window")
        if not isinstance(window, list) or len(window) != 2:
            raise CandidateSnapshotContractError("required-data strike window is invalid")
        lower, upper = window
        if any(value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
        ) for value in window) or (lower is not None and upper is not None and lower > upper):
            raise CandidateSnapshotContractError("required-data strike window is invalid")
        if any((lower is not None and value < lower) or (upper is not None and value > upper) for value in observed):
            raise CandidateSnapshotContractError("required-data observed strike is outside its window")
        expected_missing = []
        inclusive = []
        if not observed:
            expected_missing.append(window)
            inclusive.append([lower is not None, upper is not None])
        else:
            if lower is not None and lower < observed[0]:
                expected_missing.append([lower, observed[0]])
                inclusive.append([True, False])
            if upper is not None and upper > observed[-1]:
                expected_missing.append([observed[-1], upper])
                inclusive.append([False, True])
        if scope.get("missing_ranges") != expected_missing or scope.get("missing_range_inclusive") != inclusive:
            raise CandidateSnapshotContractError("required-data missing range endpoints are invalid")
    for key in ("covered_ranges", "missing_ranges", "missing_range_inclusive"):
        if detail.get(key) != [value for scope in scopes for value in scope[key]]:
            raise CandidateSnapshotContractError("required-data range summary mismatch")
    if detail.get("covered_strikes") != sorted({value for scope in scopes for value in scope["covered_strikes"]}):
        raise CandidateSnapshotContractError("required-data observed strikes summary mismatch")


def normalize_combo_scope_results(
    rows: Iterable[Mapping[str, Any]],
    *,
    owner: str,
) -> list[dict[str, Any]]:
    owner_norm = required_text(owner, "candidate owner").lower()
    if owner_norm not in {"sp_lc", "cc_lp"}:
        raise CandidateSnapshotContractError("candidate owner is invalid")
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise CandidateSnapshotContractError("candidate scope result must be an object")
        symbol = required_text(raw.get("symbol"), "scope symbol").upper()
        mode = required_text(raw.get("strategy_mode"), "scope strategy mode").lower()
        if mode != "combo_yield":
            raise CandidateSnapshotContractError("combo scope strategy mode is invalid")
        raw_owner = str(raw.get("owner") or raw.get("variant") or owner_norm).strip().lower()
        if raw_owner != owner_norm:
            raise CandidateSnapshotContractError("combo scope owner mismatch")
        key = (symbol, mode)
        if key in seen:
            raise CandidateSnapshotContractError("combo candidate scope is duplicated")
        seen.add(key)
        status = required_text(raw.get("status"), "scope status").lower()
        if status not in CANDIDATE_CAPTURE_STATUSES:
            raise CandidateSnapshotContractError("combo candidate scope status is invalid")
        detail = raw.get("reason_detail", raw.get("detail"))
        out.append(
            {
                "scope": "strategy",
                "symbol": symbol,
                "strategy_mode": mode,
                "candidate_owner": owner_norm,
                "status": status,
                "reason_code": str(raw.get("reason") or raw.get("reason_code") or "").strip() or None,
                **({"reason_detail": detail.strip()} if isinstance(detail, str) and detail.strip() else {}),
                "quote_snapshot_id": str(raw.get("quote_snapshot_id") or "").strip() or None,
                "quote_receipt_relpath": str(raw.get("quote_receipt_relpath") or "").strip() or None,
                **required_data_coverage_fields(raw),
            }
        )
    return sorted(out, key=lambda row: (str(row["symbol"]), str(row["strategy_mode"])))


def combo_opening_status(
    scopes: Iterable[Mapping[str, Any]],
    *,
    selected_count: int,
) -> str:
    rows = [dict(item) for item in scopes]
    if not rows:
        raise CandidateSnapshotContractError("combo candidate scopes are missing")
    observed = [row for row in rows if row.get("status") != "not_applicable"]
    if observed and all(row.get("reason_code") == "market_closed" for row in observed):
        return "market_closed"
    states = {str(row.get("status") or "") for row in rows}
    if states == {"completed"}:
        if any(row.get("reason_code") == "partial_data" for row in rows):
            return "partial_data"
        if selected_count > 0:
            return "candidates_found"
        return "no_candidate"
    if states == {"not_applicable"}:
        return "not_applicable"
    if "completed" in states or "not_applicable" in states:
        return "partial_data"
    return "data_unavailable"


__all__ = [
    "BENIGN_OPENING_NOT_APPLICABLE_REASONS",
    "CANDIDATE_RUN_MODE_FIELDS",
    "CANDIDATE_CAPTURE_STATUSES",
    "CandidateSnapshotContractError",
    "EXPERIENCE_CANDIDATE_DEPENDENCIES",
    "REQUIRED_CANDIDATE_DEPENDENCIES",
    "assert_current_candidate_artifact_boundary",
    "candidate_run_mode_fields",
    "combo_opening_status",
    "current_candidate_owner_schema",
    "dependency_hash",
    "normalize_combo_scope_results",
    "normalize_dependencies",
    "normalize_json_value",
    "required_text",
    "sha256_text",
    "utc_timestamp",
    "validate_candidate_run_mode",
]
