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


def test_r1_exceptions_are_exact_live_statements(tmp_path, monkeypatch) -> None:
    from src.application.ledger import lot_identity_migration as migration

    registry = scan()["src"]
    live = registry["detail"] + registry["dynamic_sql"]
    path = tmp_path / "registry.json"
    monkeypatch.setattr(migration, "RETIRED_COLUMN_REGISTRY_PATH", path)
    for module, signatures in migration.R1_POSITION_SQL_EXCEPTIONS.items():
        for kind, digest, occurrences in signatures:
            matches = [hit for hit in live if (
                hit["module"], hit["kind"], hit["digest"], hit["occurrences"]
            ) == (module, kind, digest, occurrences)]
            assert len(matches) == 1, (module, kind, digest, occurrences)
            hit = matches[0]
            # Each exception is independently usable; changing any component
            # must close it, even when --write has accepted the new registry.
            for changed in (None, {"module": "src/unreviewed.py"}, {"kind": "unreviewed"},
                            {"digest": "sha256:unreviewed"}, {"occurrences": occurrences + 1}):
                candidate = hit if changed is None else {**hit, **changed}
                path.write_text(json.dumps({"src": {"detail": [candidate], "dynamic_sql": []}}))
                assert bool(migration._live_sql_naming_retired_columns()) is (changed is not None)


@pytest.mark.parametrize("rebuilt", [False, True], ids=["legacy", "rebuilt"])
def test_r1_exception_shape_regression(tmp_path, monkeypatch, rebuilt) -> None:
    from tests import test_lot_identity_migration as regression

    # Exercise real SQL gate and repository owners. Only the window token is
    # enabled for the isolated fixture; the shipped token remains absent.
    monkeypatch.setattr(regression.module, "LOT_IDENTITY_WINDOW_ENABLEMENT", "test-window-token")
    assert regression.module._live_sql_naming_retired_columns() == ()
    regression.test_r1_rebuilt_store_reopens_and_preserves_projection(tmp_path, None, rebuilt)
