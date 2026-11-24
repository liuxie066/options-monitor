from __future__ import annotations

import ast
from collections.abc import Callable
from functools import cache, partial
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
        "domain/domain/ledger/position_fingerprint.py",
        "domain/domain/ledger/projection.py",
        "domain/domain/ledger/projection_state.py",
    ),
    "domain/domain/ledger/economics.py": (
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/fees.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/ledger/lots.py",
        "domain/domain/money.py",
        "domain/domain/strategy_membership.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/ledger/events.py": (
        "domain/domain/ledger/identity.py",
        "domain/domain/money.py",
        "domain/domain/option_position_identity.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/ledger/fees.py": (
        "domain/domain/ledger/events.py",
        "domain/domain/money.py",
    ),
    "domain/domain/ledger/identity.py": (
        "domain/domain/money.py",
        "domain/domain/option_position_identity.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/ledger/invariants.py": (
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/lots.py",
    ),
    "domain/domain/ledger/lots.py": (
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/fees.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/ledger/position_fields.py",
        "domain/domain/money.py",
        "domain/domain/option_position_identity.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/ledger/position_fields.py": (
        "domain/domain/option_position_identity.py",
        "domain/domain/strategy_membership.py",
        "domain/domain/strategy_vocab.py",
        "domain/domain/trade_contract_identity.py",
        "domain/domain/wheel_call_allocation.py",
    ),
    "domain/domain/ledger/position_fingerprint.py": (),
    "domain/domain/ledger/projection.py": (
        "domain/domain/ledger/economics.py",
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/ledger/invariants.py",
        "domain/domain/ledger/lots.py",
        "domain/domain/ledger/position_fields.py",
        "domain/domain/ledger/projection_state.py",
        "domain/domain/strategy_membership.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/ledger/projection_state.py": (
        "domain/domain/ledger/economics.py",
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/ledger/lots.py",
        "domain/domain/ledger/position_fields.py",
        "domain/domain/money.py",
        "domain/domain/strategy_membership.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/money.py": (),
    "domain/domain/option_position_identity.py": (
        "domain/domain/expiration_dates.py",
        "domain/domain/symbol_identity.py",
    ),
    "domain/domain/strategy_membership.py": (
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/identity.py",
        "domain/domain/strategy_vocab.py",
        "domain/domain/trade_execution.py",
        "domain/domain/wheel_call_allocation.py",
    ),
    "domain/domain/strategy_vocab.py": (),
    "domain/domain/symbol_identity.py": (),
    "domain/domain/trade_account_identity.py": (),
    "domain/domain/trade_contract_identity.py": (
        "domain/domain/expiration_dates.py",
        "domain/domain/money.py",
        "domain/domain/option_position_identity.py",
        "domain/domain/symbol_identity.py",
    ),
    "domain/domain/trade_execution.py": (
        "domain/domain/option_position_identity.py",
        "domain/domain/symbol_identity.py",
        "domain/domain/trade_account_identity.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "domain/domain/wheel_call_allocation.py": (),
    "src/application/ledger/event_codec.py": (
        "domain/domain/ledger/__init__.py",
        "domain/domain/ledger/events.py",
        "domain/domain/trade_contract_identity.py",
    ),
    "src/application/ledger/position_records.py": (),
    "src/application/ledger/publisher.py": (
        "domain/domain/ledger/__init__.py",
        "domain/domain/ledger/events.py",
        "domain/domain/ledger/lots.py",
        "src/application/ledger/event_codec.py",
        "src/application/ledger/position_records.py",
    ),
}

# Generated from the manifest and exact raw source bytes by
# compute_projector_implementation_fingerprint().
EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT = "a52747e7999a1597165546b8ef69a420c7f7f53f42921bacce90fa45a3bc5325"


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


def _read_projector_source(root: Path, path: str) -> bytes:
    source_path = (root / _canonical_relative_path(path)).resolve()
    if root not in source_path.parents:
        raise ProjectorImplementationUnavailable(f"projector source escaped root: {path}")
    try:
        return source_path.read_bytes()
    except OSError as exc:
        raise ProjectorImplementationUnavailable(f"projector source is unreadable: {path}") from exc


def _discover_import_edges(
    path: str, raw: bytes, resolve_module: Callable[[str], str | None],
    *, check_dynamic_imports: bool,
) -> tuple[str, ...]:
    try:
        tree = ast.parse(raw, filename=path)
    except SyntaxError as exc:
        raise ProjectorImplementationUnavailable(f"projector source is unreadable: {path}") from exc

    imports: set[str] = set()
    pending = [tree]
    while pending:
        node = pending.pop()
        kind = type(node)
        if kind is ast.Call:
            direct_dynamic = isinstance(node.func, ast.Name) and node.func.id == "__import__"
            importlib_dynamic = isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
            if direct_dynamic or importlib_dynamic:
                raise ProjectorImplementationUnavailable(f"dynamic import is not allowed in projector source: {path}")
        elif kind is ast.Import:
            for alias in node.names:
                resolved = resolve_module(alias.name)
                if resolved is not None:
                    imports.add(resolved)
        elif kind is ast.ImportFrom:
            module = _resolve_from_module(path, node)
            resolved = resolve_module(module)
            if resolved is not None:
                imports.add(resolved)
            for alias in node.names:
                child = resolve_module(f"{module}.{alias.name}")
                if child is not None:
                    imports.add(child)
        if not check_dynamic_imports:
            # Imports are statements. Statement bodies and case/exception
            # handlers are the only AST fields that can contain them.
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                if (children := getattr(node, field, None)) is not None:
                    pending.extend(children)
        elif isinstance(node, ast.AST):
            # Parser fields can contain scalars or list placeholders.
            for field in node._fields:
                value = getattr(node, field)
                if type(value) is list:
                    pending.extend(value)
                elif isinstance(value, ast.AST):
                    pending.append(value)
    return tuple(sorted(imports))


def validate_projector_implementation_manifest(root: Path | None = None) -> None:
    source_root = resolve_projector_source_root(root)
    _validate_import_graph(source_root, None, check_dynamic_imports=True)


def _validate_import_graph(
    source_root: Path, sources: dict[str, bytes] | None, *, check_dynamic_imports: bool,
) -> None:
    declared = set(_SEMANTIC_IMPORT_GRAPH)
    if not set(_ROOTS).issubset(declared):
        raise ProjectorImplementationUnavailable("projector roots are absent from the manifest")
    resolve_module = cache(partial(_module_path, source_root))
    for path, expected_edges in _SEMANTIC_IMPORT_GRAPH.items():
        raw = _read_projector_source(source_root, path) if sources is None else sources[path]
        actual_edges = _discover_import_edges(
            path, raw, resolve_module, check_dynamic_imports=check_dynamic_imports,
        )
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
    return _fingerprint_and_sources(source_root)[0]


def _fingerprint_and_sources(source_root: Path) -> tuple[str, dict[str, bytes]]:
    manifest_bytes = json.dumps(
        projector_implementation_manifest(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256()
    _frame(digest, manifest_bytes)
    sources = {}
    for path in sorted(_SEMANTIC_IMPORT_GRAPH):
        source_bytes = _read_projector_source(source_root, path)
        _frame(digest, path.encode("utf-8"))
        _frame(digest, source_bytes)
        sources[path] = source_bytes
    return digest.hexdigest(), sources


def verify_expected_projector_implementation(
    root: Path | None = None,
) -> str:
    return _verify_projector_sources(resolve_projector_source_root(root), exhaustive=True)


def _verify_projector_sources(source_root: Path, *, exhaustive: bool) -> str:
    actual, sources = _fingerprint_and_sources(source_root)
    exact_sources = actual == EXPECTED_PROJECTOR_IMPLEMENTATION_FINGERPRINT
    # The checked-in digest binds the source and manifest audited by the full
    # validator. Each process still resolves all imports against its own tree.
    _validate_import_graph(source_root, sources, check_dynamic_imports=exhaustive or not exact_sources)
    if not exact_sources:
        raise ProjectorImplementationUnavailable(
            "loaded projector implementation differs from its checked-in fingerprint"
        )
    return actual


def _load_projector_implementation_result() -> tuple[str | None, str | None]:
    try:
        return _verify_projector_sources(resolve_projector_source_root(), exhaustive=False), None
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
