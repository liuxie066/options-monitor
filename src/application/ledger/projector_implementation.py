from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any


PROJECTOR_IMPLEMENTATION_MANIFEST_SCHEMA = "projector_implementation_manifest.v1"

_ROOTS = (
    "domain/domain/ledger/position_fingerprint.py",
    "domain/domain/ledger/projection.py",
    "src/application/ledger/event_codec.py",
    "src/application/ledger/publisher.py",
)

_SEMANTIC_IMPORT_GRAPH: dict[str, tuple[str, ...]] = {
    "domain/domain/expiration_dates.py": (),
    "domain/domain/ledger/__init__.py": (
        "domain/domain/ledger/economics.py",
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/ledger/lots.py",
        "domain/domain/ledger/position_fields.py",
        "domain/domain/ledger/position_fingerprint.py",
        "domain/domain/ledger/projection.py",
    ),
    "domain/domain/ledger/economics.py": (
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/ledger/lots.py",
        "domain/domain/performance/models.py",
    ),
    "domain/domain/ledger/events.py": (
        "domain/domain/ledger/identity.py",
        "domain/domain/option_position_identity.py",
    ),
    "domain/domain/ledger/identity.py": (
        "domain/domain/option_position_identity.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/ledger/invariants.py": (
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/lots.py",
    ),
    "domain/domain/ledger/lots.py": (
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/ledger/position_fields.py",
        "domain/domain/option_position_identity.py",
    ),
    "domain/domain/ledger/position_fields.py": ("domain/domain/option_position_identity.py",),
    "domain/domain/ledger/position_fingerprint.py": (),
    "domain/domain/ledger/projection.py": (
        "domain/domain/ledger/economics.py",
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/ledger/invariants.py",
        "domain/domain/ledger/lots.py",
    ),
    "domain/domain/option_position_identity.py": (
        "domain/domain/expiration_dates.py",
        "domain/domain/symbol_identity.py",
    ),
    "domain/domain/performance/models.py": (
        "domain/domain/ledger/identity.py",
        "domain/domain/option_position_identity.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/symbol_identity.py": (),
    "domain/domain/trade_contract_identity.py": (
        "domain/domain/expiration_dates.py",
        "domain/domain/option_position_identity.py",
        "domain/domain/symbol_identity.py",
    ),
    "src/application/ledger/event_codec.py": (
        "domain/domain/ledger/__init__.py",
        "domain/domain/ledger/events.py",
    ),
    "src/application/ledger/position_records.py": (),
    "src/application/ledger/publisher.py": (
        "domain/domain/ledger/__init__.py",
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/lots.py",
        "domain/domain/ledger/position_fields.py",
        "domain/domain/option_position_identity.py",
        "domain/domain/trade_contract_identity.py",
        "src/application/ledger/event_codec.py",
        "src/application/ledger/position_records.py",
    ),
}

# Generated from the manifest and exact raw source bytes by
# compute_projector_implementation_fingerprint().
EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT = "78160e056fa5dc3828dd695f73041cbae6ec28f5530b0ae10f85e78bad795f9e"


class ProjectorImplementationUnavailable(RuntimeError):
    pass


def projector_implementation_manifest() -> dict[str, Any]:
    return {
        "schema": PROJECTOR_IMPLEMENTATION_MANIFEST_SCHEMA,
        "roots": list(_ROOTS),
        "files": {path: {"classification": "semantic-hashed"} for path in sorted(_SEMANTIC_IMPORT_GRAPH)},
        "imports": {path: list(imports) for path, imports in sorted(_SEMANTIC_IMPORT_GRAPH.items())},
    }


def resolve_projector_source_root(start: Path | None = None) -> Path:
    candidates = [Path(start).resolve()] if start is not None else list(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "domain" / "domain" / "ledger" / "projection.py").is_file() and (
            candidate / "src" / "application" / "ledger" / "publisher.py"
        ).is_file():
            return candidate
    raise ProjectorImplementationUnavailable(
        "projector source root could not be resolved from the installed source tree"
    )


def _canonical_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or path.suffix != ".py":
        raise ProjectorImplementationUnavailable(f"non-canonical projector manifest path: {value}")
    return value


def _module_path(root: Path, module: str) -> str | None:
    if not (module == "domain" or module.startswith("domain.") or module == "src" or module.startswith("src.")):
        return None
    base = Path(*module.split("."))
    candidates = (base.with_suffix(".py"), base / "__init__.py")
    for candidate in candidates:
        if (root / candidate).is_file():
            return candidate.as_posix()
    return None


def _module_name(path: str) -> tuple[str, bool]:
    relative = PurePosixPath(path)
    if relative.name == "__init__.py":
        return ".".join(relative.parent.parts), True
    return ".".join(relative.with_suffix("").parts), False


def _resolve_from_module(current_path: str, node: ast.ImportFrom) -> str:
    module = node.module or ""
    if node.level <= 0:
        return module
    current_module, is_package = _module_name(current_path)
    package_parts = current_module.split(".") if is_package else current_module.split(".")[:-1]
    keep = len(package_parts) - (node.level - 1)
    if keep < 0:
        return module
    prefix = package_parts[:keep]
    if module:
        prefix.extend(module.split("."))
    return ".".join(prefix)


def _discover_import_edges(root: Path, path: str) -> tuple[str, ...]:
    source_path = (root / _canonical_relative_path(path)).resolve()
    if source_path.parent != root and root not in source_path.parents:
        raise ProjectorImplementationUnavailable(f"projector source escaped root: {path}")
    try:
        raw = source_path.read_bytes()
        tree = ast.parse(raw, filename=path)
    except (OSError, SyntaxError) as exc:
        raise ProjectorImplementationUnavailable(f"projector source is unreadable: {path}") from exc

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            direct_dynamic = isinstance(node.func, ast.Name) and node.func.id == "__import__"
            importlib_dynamic = isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
            if direct_dynamic or importlib_dynamic:
                raise ProjectorImplementationUnavailable(f"dynamic import is not allowed in projector source: {path}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _module_path(root, alias.name)
                if resolved is not None:
                    imports.add(resolved)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_from_module(path, node)
            resolved = _module_path(root, module)
            if resolved is not None:
                imports.add(resolved)
            for alias in node.names:
                child = _module_path(root, f"{module}.{alias.name}")
                if child is not None:
                    imports.add(child)
    return tuple(sorted(imports))


def validate_projector_implementation_manifest(root: Path | None = None) -> None:
    source_root = resolve_projector_source_root(root)
    declared = set(_SEMANTIC_IMPORT_GRAPH)
    if not set(_ROOTS).issubset(declared):
        raise ProjectorImplementationUnavailable("projector roots are absent from the manifest")
    for path, expected_edges in _SEMANTIC_IMPORT_GRAPH.items():
        _canonical_relative_path(path)
        actual_edges = _discover_import_edges(source_root, path)
        if actual_edges != expected_edges:
            raise ProjectorImplementationUnavailable(
                f"projector import graph differs for {path}: expected={expected_edges}, actual={actual_edges}"
            )
        undeclared = set(actual_edges) - declared
        if undeclared:
            raise ProjectorImplementationUnavailable(
                f"projector imports are unclassified for {path}: {sorted(undeclared)}"
            )


def _frame(digest: Any, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, byteorder="big", signed=False))
    digest.update(payload)


def compute_projector_implementation_fingerprint(root: Path | None = None) -> str:
    source_root = resolve_projector_source_root(root)
    manifest_bytes = json.dumps(
        projector_implementation_manifest(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256()
    _frame(digest, manifest_bytes)
    for raw_path in sorted(_SEMANTIC_IMPORT_GRAPH):
        path = _canonical_relative_path(raw_path)
        path_bytes = path.encode("utf-8")
        source_path = (source_root / path).resolve()
        if source_root not in source_path.parents:
            raise ProjectorImplementationUnavailable(f"projector source escaped root: {path}")
        try:
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            raise ProjectorImplementationUnavailable(f"projector source is unreadable: {path}") from exc
        _frame(digest, path_bytes)
        _frame(digest, source_bytes)
    return digest.hexdigest()


def verify_expected_projector_implementation(
    root: Path | None = None,
) -> str:
    validate_projector_implementation_manifest(root)
    actual = compute_projector_implementation_fingerprint(root)
    if actual != EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT:
        raise ProjectorImplementationUnavailable(
            "loaded projector implementation differs from its checked-in fingerprint"
        )
    return actual


def _load_projector_implementation_result() -> tuple[str | None, str | None]:
    try:
        return verify_expected_projector_implementation(), None
    except ProjectorImplementationUnavailable as exc:
        return None, str(exc)


_LOADED_PROJECTOR_IMPLEMENTATION_RESULT = _load_projector_implementation_result()


def loaded_projector_implementation_fingerprint() -> str:
    """Return the implementation identity frozen when this module was loaded."""

    fingerprint, error = _LOADED_PROJECTOR_IMPLEMENTATION_RESULT
    if fingerprint is None:
        raise ProjectorImplementationUnavailable(error or "projector implementation is unavailable")
    return fingerprint


__all__ = [
    "EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT",
    "PROJECTOR_IMPLEMENTATION_MANIFEST_SCHEMA",
    "ProjectorImplementationUnavailable",
    "compute_projector_implementation_fingerprint",
    "loaded_projector_implementation_fingerprint",
    "projector_implementation_manifest",
    "resolve_projector_source_root",
    "validate_projector_implementation_manifest",
    "verify_expected_projector_implementation",
]
