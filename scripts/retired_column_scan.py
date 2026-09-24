#!/usr/bin/env python3
"""Pin the inventory of live SQL that still names the retired lot columns.

Slice 3's gate (window adjudication 甲, 2026-09-20) is a per-statement
registry: the set of live statements naming a retired column is enumerated,
committed, and pinned by test, so any drift -- a statement added, repointed,
or removed -- turns the pinning test red instead of passing silently.

Three hit shapes are scanned, because the manual inventory
(docs/gateflow retired-column-sql-inventory.md, superseded by this tool's
output as the canonical count) found all three:

1. SQL text: a string literal that names a retired column's table and an
   SQL keyword, and contains the retired column as a word run (``record_id``
   matches inside neither ``idx_position_lots_record`` nor a longer name,
   because ``_`` is a word character for ``\\b``).
2. Schema-helper calls: ``_add_column_if_missing(conn, "position_lots",
   "expiration", ...)`` carries the table and the column in separate
   arguments, so no single literal names both.
3. Index-name constants: ``idx_position_lots_account_expiration`` is not
   SQL text, but dropping that index is part of retiring the column.

Dynamically assembled SQL (f-strings) naming a retired table cannot be
judged from literals alone. Record each in ``dynamic_sql``, even if its
retired-column list is empty because the column could be interpolated.

The read-only lot-identity inventory and the projection migration still need
old-column reads; they are scanned but recorded in the ``exempt`` section.
The exemption is asserted non-empty so a renamed module cannot hollow it out.

Usage:
    ./.venv/bin/python scripts/retired_column_scan.py --write
    ./.venv/bin/python scripts/retired_column_scan.py --check
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "docs" / "retired_column_sql_registry.json"

#: The retired SQL columns, keyed by the table they are retired from. The
#: payload-level ``record_id`` key (fields_json wrapper) is explicitly NOT
#: part of this scan: it is a different retirement with a different owner.
RETIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "position_lots": ("expiration", "record_id"),
    "wheel_events": ("stock_lot_id",),
}

#: Modules whose job requires naming the retired columns. Recorded, counted,
#: and asserted present -- exempt is not invisible. R2 removed the parity
#: probe's old-shape branch; the read-only lot-identity inventory still reads
#: the old shape to diagnose it.
EXEMPT_MODULES: tuple[str, ...] = (
    "src/application/ledger/lot_identity_migration.py",
    "src/application/ledger/position_projection_migration.py",
)

_SCOPES = ("src", "scripts", "tests")
_KEYWORD_RE = re.compile(
    r"\b(select|insert|update|delete|replace|create|drop|alter|pragma)\b",
    re.IGNORECASE,
)
_DDL_RE = re.compile(r"\b(create|drop|alter)\b", re.IGNORECASE)
_WRITE_RE = re.compile(r"\b(insert|update|delete|replace)\b", re.IGNORECASE)
_READ_RE = re.compile(r"\bselect\b", re.IGNORECASE)


@dataclass(frozen=True)
class Hit:
    module: str
    kind: str  # read | write | ddl | schema_helper | index_name | dynamic
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    digest: str
    occurrences: int


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def _classify(text: str) -> str | None:
    if _DDL_RE.search(text):
        return "ddl"
    if _WRITE_RE.search(text):
        return "write"
    if _READ_RE.search(text):
        return "read"
    return None


def _mentioned_tables(text: str) -> tuple[str, ...]:
    return tuple(table for table in sorted(RETIRED_COLUMNS) if _word(table).search(text))


def _retired_in(text: str, tables: tuple[str, ...]) -> tuple[str, ...]:
    names = [name for table in tables for name in RETIRED_COLUMNS[table]]
    return tuple(name for name in sorted(set(names)) if _word(name).search(text))


def _digest(text: str) -> str:
    normalized = " ".join(text.split())
    return "sha256:" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _index_name_columns(text: str) -> tuple[str, ...]:
    """Retired columns an index-name constant depends on.

    Index names drop the ``_id`` suffix of the column they are built from
    (``idx_position_lots_account_record`` indexes ``record_id``), so the
    segment match accepts the stripped alias. Requiring the index's table
    as a consecutive segment run keeps unrelated ``record`` segments out.
    """
    if not re.fullmatch(r"idx_[a-z0-9_]+", text):
        return ()
    segments = text.split("_")

    def _run_present(run: list[str]) -> bool:
        size = len(run)
        return any(segments[i : i + size] == run for i in range(len(segments) - size + 1))

    names = [name for columns in RETIRED_COLUMNS.values() for name in columns]
    return tuple(
        sorted(
            {
                name
                for table, columns in RETIRED_COLUMNS.items()
                if _run_present(table.split("_"))
                for name in names
                if name in columns and (name in segments or name.removesuffix("_id") in segments)
            }
        )
    )


def _string_nodes(tree: ast.AST) -> list[ast.Constant | ast.JoinedStr]:
    return [node for node in ast.walk(tree) if isinstance(node, (ast.Constant, ast.JoinedStr))]


def _literal_text(node: ast.Constant | ast.JoinedStr) -> str:
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    return "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))


def _is_dynamic(node: ast.Constant | ast.JoinedStr) -> bool:
    return isinstance(node, ast.JoinedStr) and any(isinstance(part, ast.FormattedValue) for part in node.values)


def _scan_module(path: Path, module: str) -> tuple[list[Hit], list[Hit]]:
    """Return (hits, dynamic_sql) for one module file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    recorded: dict[tuple[str, str, str], Hit] = {}
    dynamic: dict[tuple[str, str], Hit] = {}

    def _record(bucket: dict, kind: str, text: str) -> None:
        tables = _mentioned_tables(text)
        columns = _retired_in(text, tables) or _index_name_columns(text)
        if not columns and kind != "dynamic":
            return
        digest = _digest(text)
        key = (kind, digest, module)
        if key in bucket:
            bucket[key] = Hit(
                module=module,
                kind=kind,
                tables=tables or bucket[key].tables,
                columns=columns,
                digest=digest,
                occurrences=bucket[key].occurrences + 1,
            )
        else:
            bucket[key] = Hit(
                module=module,
                kind=kind,
                tables=tables,
                columns=columns,
                digest=digest,
                occurrences=1,
            )

    for node in _string_nodes(tree):
        text = _literal_text(node)
        if not text:
            continue
        if _is_dynamic(node):
            if _mentioned_tables(text) and _KEYWORD_RE.search(text):
                _record(dynamic, "dynamic", text)
            continue
        if _mentioned_tables(text) and _KEYWORD_RE.search(text):
            kind = _classify(text)
            if kind is not None:
                _record(recorded, kind, text)
        elif _index_name_columns(text):
            # A required-index-name constant names a retired column without
            # being SQL text: no table, no keyword, still a live dependency
            # on the column the index is built from.
            _record(recorded, "index_name", text)

    # Rule 2: schema-helper calls carry table and column in separate args.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None) or ""
        if not re.search(r"add_column|create_index|drop_index|drop_column", name):
            continue
        args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        text = " ".join(args)
        tables = _mentioned_tables(text)
        if _retired_in(text, tables):
            _record(recorded, "schema_helper", text)

    hits = sorted(recorded.values(), key=lambda h: (h.module, h.kind, h.digest))
    dyn = sorted(dynamic.values(), key=lambda h: (h.module, h.digest))
    return hits, dyn


def _scope_digest(hits: list[Hit]) -> str:
    payload = json.dumps([list(h.__dict__.values()) for h in hits], default=list)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def scan(root: Path = ROOT) -> dict[str, object]:
    """Scan every scope and build the registry document."""
    document: dict[str, object] = {
        "retired_columns": {table: list(names) for table, names in sorted(RETIRED_COLUMNS.items())},
        "exempt_modules": list(EXEMPT_MODULES),
    }
    exempt_paths = {str(ROOT / m) for m in EXEMPT_MODULES}
    for scope in _SCOPES:
        live: list[Hit] = []
        exempt: list[Hit] = []
        exempt_dynamic: list[Hit] = []
        dynamic: list[Hit] = []
        for path in sorted((root / scope).rglob("*.py")):
            module = path.relative_to(root).as_posix()
            hits, dyn = _scan_module(path, module)
            if str(path) in exempt_paths:
                exempt.extend(hits)
                exempt_dynamic.extend(dyn)
            else:
                live.extend(hits)
                dynamic.extend(dyn)
        section: dict[str, object] = {
            "statements": len(live),
            "by_kind": _count_by(live, "kind"),
            "by_module": _count_by(live, "module"),
            "digest": _scope_digest(live),
        }
        if scope == "src":
            section["detail"] = [h.__dict__ | {"tables": list(h.tables), "columns": list(h.columns)} for h in live]
            section["exempt_hits"] = [
                h.__dict__ | {"tables": list(h.tables), "columns": list(h.columns)} for h in exempt
            ]
            section["exempt_dynamic"] = [
                h.__dict__ | {"tables": list(h.tables), "columns": list(h.columns)} for h in exempt_dynamic
            ]
            section["dynamic_sql"] = [
                h.__dict__ | {"tables": list(h.tables), "columns": list(h.columns)} for h in dynamic
            ]
        document[scope] = section
    return document


def _count_by(hits: list[Hit], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for hit in hits:
        key = getattr(hit, field)
        counts[key] = counts.get(key, 0) + hit.occurrences
    return dict(sorted(counts.items()))


def _check(document: dict[str, object], registry: dict[str, object]) -> list[str]:
    if document == registry:
        return []
    diffs: list[str] = []
    for scope in _SCOPES:
        new = document.get(scope, {})
        old = registry.get(scope, {})
        for key in ("statements", "digest", "by_kind", "by_module"):
            if new.get(key) != old.get(key):
                diffs.append(f"{scope}.{key}: registry={old.get(key)!r} tree={new.get(key)!r}")
    # Detail, dynamic SQL and exemptions are part of the pin, even when the
    # aggregate statement counts and digests are unchanged.
    return diffs or ["registry content differs from the tree"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write", action="store_true", help="rewrite docs/retired_column_sql_registry.json from the tree"
    )
    parser.add_argument("--check", action="store_true", help="fail if the committed registry does not match the tree")
    args = parser.parse_args(argv)

    document = scan()
    if args.write:
        REGISTRY_PATH.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {REGISTRY_PATH.relative_to(ROOT)}")
        return 0
    if args.check:
        if not REGISTRY_PATH.exists():
            print(f"missing {REGISTRY_PATH.relative_to(ROOT)}; run --write first", file=sys.stderr)
            return 1
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        diffs = _check(document, registry)
        if diffs:
            print("retired-column registry is stale:", file=sys.stderr)
            for diff in diffs:
                print(f"  {diff}", file=sys.stderr)
            return 1
        print("retired-column registry matches the tree")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
