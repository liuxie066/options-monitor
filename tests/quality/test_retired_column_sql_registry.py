"""Pin the retired-column SQL registry to the tree (slice 3 gate, step 1).

The registry (docs/retired_column_sql_registry.json) enumerates every live
statement that names a retired lot column. Any drift -- a statement added,
repointed, or removed -- must turn these tests red rather than pass silently.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts.retired_column_scan import (
    EXEMPT_MODULES,
    REGISTRY_PATH,
    ROOT,
    scan,
)


def test_the_committed_registry_matches_the_tree() -> None:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert scan() == registry


def test_every_exempt_module_still_names_retired_columns() -> None:
    """A renamed exempt module must fail here, not hollow out the exemption."""
    src = scan()["src"]
    named = {h["module"] for h in src["exempt_hits"] + src["exempt_dynamic"]}
    assert set(EXEMPT_MODULES) <= named
    assert any(
        h["module"] == "src/application/ledger/lot_identity_migration.py"
        and h["kind"] == "read"
        and h["columns"] == ["record_id"]
        for h in src["exempt_hits"]
    )


def test_a_planted_statement_naming_a_retired_column_is_flagged(tmp_path) -> None:
    planted = tmp_path / "src" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        'SQL_OLD = "SELECT record_id, expiration FROM position_lots WHERE account = ?"\n'
        'SQL_OTHER_TABLE = "SELECT expiration FROM option_contracts"\n'
        'INDEX_NAME = "idx_position_lots_account_record"\n'
        'def read(conn, column):\n'
        '    return conn.execute(f"SELECT {column} FROM position_lots")\n',
        encoding="utf-8",
    )
    detail = scan(root=tmp_path)["src"]["detail"]
    flagged = {h["digest"]: h for h in detail}

    kinds = {h["kind"] for h in flagged.values() if h["module"] == "src/planted.py"}
    # The position_lots statement is a read hit; the index-name constant is an
    # index_name hit. The option_contracts expiration is a different table's
    # domain concept and must NOT be flagged.
    assert kinds == {"read", "index_name"}
    dynamic = scan(root=tmp_path)["src"]["dynamic_sql"]
    assert [(h["module"], h["columns"]) for h in dynamic] == [
        ("src/planted.py", []),
    ]


def test_check_mode_round_trips() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "retired_column_scan.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_check_rejects_metadata_only_drift(tmp_path, monkeypatch, capsys) -> None:
    from copy import deepcopy
    from scripts import retired_column_scan as scanner

    document = scan()
    path = tmp_path / "registry.json"
    monkeypatch.setattr(scanner, "REGISTRY_PATH", path)
    monkeypatch.setattr(scanner, "scan", lambda: document)
    for key in ("detail", "dynamic_sql", "exempt_hits", "exempt_dynamic"):
        stale = deepcopy(document)
        stale["src"][key] = [] if document["src"][key] else [{"unexpected": "metadata drift"}]
        assert stale != document, key
        path.write_text(json.dumps(stale), encoding="utf-8")
        assert scanner.main(["--check"]) == 1, key
        assert "registry is stale" in capsys.readouterr().err
    path.write_text(json.dumps(document), encoding="utf-8")
    assert scanner.main(["--check"]) == 0


def test_runtime_sql_no_longer_names_retired_columns() -> None:
    registry = scan()["src"]
    assert registry["detail"] == []
    assert {(h["module"], h["digest"]) for h in registry["dynamic_sql"]} == {
        ("src/application/ledger/repository_projection_schema.py", "sha256:00304c6023cc01e1"),
        ("src/application/ledger/repository_projection_schema.py", "sha256:10ccc631371b73ad"),
        ("src/application/ledger/repository_projection_schema.py", "sha256:11efe2ef73a5d799"),
        ("src/application/ledger/repository_projection_schema.py", "sha256:3db215a46c8ea0c6"),
        ("src/application/ledger/repository_projection_schema.py", "sha256:471fd62ac2cbdaf6"),
        ("src/application/ledger/repository_projection_schema.py", "sha256:d899e724f091dcce"),
        ("src/application/ledger/repository_projection_schema.py", "sha256:e65279505d598965"),
        ("src/application/ledger/trade_attribution_migration.py", "sha256:2aa2f9d38a6a4493"),
    }
