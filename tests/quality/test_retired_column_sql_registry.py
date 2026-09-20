"""Pin the retired-column SQL registry to the tree (slice 3 gate, step 1).

The registry (docs/retired_column_sql_registry.json) enumerates every live
statement that names a retired lot column. Any drift -- a statement added,
repointed, or removed -- must turn these tests red rather than pass silently.
"""

from __future__ import annotations

import json
import subprocess
import sys

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


def test_a_planted_statement_naming_a_retired_column_is_flagged(tmp_path) -> None:
    planted = tmp_path / "src" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        'SQL_OLD = "SELECT record_id, expiration FROM position_lots WHERE account = ?"\n'
        'SQL_OTHER_TABLE = "SELECT expiration FROM option_contracts"\n'
        'INDEX_NAME = "idx_position_lots_account_record"\n',
        encoding="utf-8",
    )
    detail = scan(root=tmp_path)["src"]["detail"]
    flagged = {h["digest"]: h for h in detail}

    kinds = {h["kind"] for h in flagged.values() if h["module"] == "src/planted.py"}
    # The position_lots statement is a read hit; the index-name constant is an
    # index_name hit. The option_contracts expiration is a different table's
    # domain concept and must NOT be flagged.
    assert kinds == {"read", "index_name"}


def test_check_mode_round_trips() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "retired_column_scan.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
