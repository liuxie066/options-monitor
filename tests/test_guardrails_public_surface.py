from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts import guardrails_check


def _bare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "--quiet", "--initial-branch=main")
    return repo


def _run(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=guardrails test", "-c", "user.email=test@example.invalid", *arguments],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _run(repo, "add", "--all")
    _run(repo, "commit", "--quiet", "-m", message)
    return _run(repo, "rev-parse", "HEAD")


def _write(repo: Path, relative: str, text: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_ledger(repo: Path, entries: list[dict[str, str]]) -> Path:
    return _write(
        repo,
        guardrails_check.PUBLIC_SURFACE_LEDGER.as_posix(),
        json.dumps({"retirements": entries}, ensure_ascii=False, indent=2) + "\n",
    )


def _reason() -> str:
    return "covered by the shared helper extracted in this change"


# --- declared surface -------------------------------------------------------


def test_explicit_all_wins_over_definitions_that_are_not_exported() -> None:
    source = "def exported():\n    pass\n\ndef internal():\n    pass\n\n__all__ = ['exported']\n"

    assert guardrails_check.declared_public_names(source) == frozenset({"exported"})


def test_module_without_all_declares_its_own_public_definitions_only() -> None:
    source = (
        "import json\n"
        "from typing import Any\n"
        "CONSTANT = 1\n"
        "TYPED: int = 2\n"
        "def public_function():\n    pass\n"
        "async def public_coroutine():\n    pass\n"
        "class PublicClass:\n    pass\n"
        "def _private():\n    pass\n"
        "_HIDDEN = 3\n"
        "if True:\n"
        "    NESTED = 4\n"
    )

    assert guardrails_check.declared_public_names(source) == frozenset(
        {"CONSTANT", "TYPED", "public_function", "public_coroutine", "PublicClass"}
    )


def test_all_resolves_starred_references_to_module_level_literals() -> None:
    source = (
        "_EXPORTS = {'first': 'a', 'second': 'b'}\n"
        "_MODULES = {'third'}\n"
        "__all__ = [*_EXPORTS.keys(), *_MODULES]\n"
    )

    assert guardrails_check.declared_public_names(source) == frozenset({"first", "second", "third"})


def test_all_resolves_annotated_module_level_bindings() -> None:
    source = (
        "_EXPORTS: dict[str, str] = {'first': '.a'}\n"
        "_MODULES: set[str] = {'second'}\n"
        "__all__: list[str] = [*_EXPORTS.keys(), *_MODULES]\n"
    )

    assert guardrails_check.declared_public_names(source) == frozenset({"first", "second"})


def test_all_resolves_sorted_and_concatenated_sequences() -> None:
    source = "_MODULES = {'a', 'b'}\n__all__ = sorted(_MODULES) + ['c']\n"

    assert guardrails_check.declared_public_names(source) == frozenset({"a", "b", "c"})


def test_unresolvable_all_is_reported_instead_of_guessed() -> None:
    source = "__all__ = compute_exports()\n"

    with pytest.raises(guardrails_check.PublicSurfaceUnavailable):
        guardrails_check.declared_public_names(source)


def test_unparsable_module_is_reported_instead_of_skipped() -> None:
    with pytest.raises(guardrails_check.PublicSurfaceUnavailable):
        guardrails_check.declared_public_names("def broken(:\n")


def test_present_names_accepts_an_alias_import_as_still_exposed() -> None:
    source = "from src.infrastructure.io_utils import utc_now as utc_now_iso\n"

    assert guardrails_check.declared_public_names(source) == frozenset()
    assert guardrails_check.present_public_names(source) == frozenset({"utc_now_iso"})


def test_present_names_still_honours_all_when_a_module_declares_one() -> None:
    source = "__all__ = ['kept']\n\ndef dropped():\n    pass\n"

    assert guardrails_check.present_public_names(source) == frozenset({"kept"})


# --- retirement ledger ------------------------------------------------------


def test_ledger_accepts_a_well_formed_entry() -> None:
    source = json.dumps(
        {
            "retirements": [
                {"module": "src/pkg/mod.py", "name": "gone", "reason": _reason()},
                {"module": "src/pkg/retired.py", "name": "*", "reason": _reason()},
            ]
        }
    )

    entries, issues = guardrails_check._retirement_entries(source, label="head")

    assert issues == []
    assert entries == [
        {"module": "src/pkg/mod.py", "name": "gone", "reason": _reason()},
        {"module": "src/pkg/retired.py", "name": "*", "reason": _reason()},
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"module": "src/pkg/mod.py", "name": "gone"},
        {"module": "src/pkg/mod.py", "name": "gone", "reason": _reason(), "extra": "x"},
        {"module": "src/pkg/mod.py", "name": "gone", "reason": 3},
        {"module": "src/pkg/mod.py.txt", "name": "gone", "reason": _reason()},
        {"module": "tests/test_mod.py", "name": "gone", "reason": _reason()},
        {"module": "src/pkg/mod.py", "name": "not an identifier", "reason": _reason()},
        {"module": "src/pkg/mod.py", "name": "gone", "reason": "merged"},
    ],
)
def test_ledger_rejects_malformed_entries(entry: dict[str, object]) -> None:
    source = json.dumps({"retirements": [entry]})

    entries, issues = guardrails_check._retirement_entries(source, label="head")

    assert entries == []
    assert len(issues) == 1
    assert "retirement entry #1" in issues[0].reason


def test_ledger_reports_invalid_json_without_crashing() -> None:
    entries, issues = guardrails_check._retirement_entries("{", label="head")

    assert entries == []
    assert "not valid JSON" in issues[0].reason


def test_ledger_reports_a_missing_retirements_list() -> None:
    entries, issues = guardrails_check._retirement_entries('{"other": []}', label="head")

    assert entries == []
    assert "retirements" in issues[0].reason


def test_absent_ledger_reads_as_no_entries() -> None:
    assert guardrails_check._retirement_entries(None, label="base") == ([], [])


# --- cross-revision behaviour ----------------------------------------------


def test_missing_base_revision_is_refused_loudly(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n")
    _commit(repo, "base")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    with pytest.raises(SystemExit, match="not available locally"):
        guardrails_check.check_public_surface("does-not-exist")


def test_declared_name_removal_requires_a_retirement_record(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n\ndef dropped():\n    pass\n\ndef _private():\n    pass\n")
    base = _commit(repo, "base")
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n\ndef _private():\n    pass\n")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    rendered = issues[0].render()
    assert "src/pkg/mod.py" in rendered
    assert "dropped" in rendered
    assert "_private" not in rendered


def test_recorded_retirement_clears_the_removal(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n\ndef dropped():\n    pass\n")
    base = _commit(repo, "base")
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n")
    _write_ledger(repo, [{"module": "src/pkg/mod.py", "name": "dropped", "reason": _reason()}])
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_public_surface(base) == []


def test_retired_module_is_covered_by_one_wildcard_entry(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/retired.py", "def first():\n    pass\n\ndef second():\n    pass\n")
    base = _commit(repo, "base")
    (repo / "src/pkg/retired.py").unlink()
    _write(repo, "src/pkg/replacement.py", "def first():\n    pass\n\ndef second():\n    pass\n")
    _write_ledger(repo, [{"module": "src/pkg/retired.py", "name": "*", "reason": _reason()}])
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_public_surface(base) == []


def test_wildcard_entry_is_refused_while_the_module_still_exists(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n")
    base = _commit(repo, "base")
    _write_ledger(repo, [{"module": "src/pkg/mod.py", "name": "*", "reason": _reason()}])
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "only valid when the module itself is gone" in issues[0].reason


def test_ledger_entries_are_append_only_across_revisions(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n")
    _write_ledger(repo, [{"module": "src/pkg/retired.py", "name": "*", "reason": _reason()}])
    base = _commit(repo, "base")
    _write_ledger(repo, [])
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "append-only" in issues[0].reason


def test_renaming_a_module_removes_the_declared_surface_of_the_old_path(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/old.py", "def moved():\n    pass\n")
    base = _commit(repo, "base")
    (repo / "src/pkg/old.py").rename(repo / "src/pkg/new.py")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "src/pkg/old.py" in issues[0].render()
    assert "moved" in issues[0].render()


def test_staged_content_is_compared_when_the_index_is_selected(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n\ndef dropped():\n    pass\n")
    base = _commit(repo, "base")
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_public_surface(base, staged=True) == []

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "dropped" in issues[0].render()


def test_changes_outside_the_checked_roots_are_not_examined(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "tests/test_mod.py", "def dropped():\n    pass\n")
    base = _commit(repo, "base")
    _write(repo, "tests/test_mod.py", "\n")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_public_surface(base) == []


def test_every_checked_module_has_a_statically_readable_declared_surface() -> None:
    """A module whose surface cannot be read would fail every later change that touches it."""
    root = Path(__file__).resolve().parents[1]
    unreadable: list[str] = []
    for directory in guardrails_check.PUBLIC_SURFACE_ROOTS:
        for path in sorted((root / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8", errors="ignore")
            try:
                guardrails_check.declared_public_names(source)
            except guardrails_check.PublicSurfaceUnavailable as exc:
                unreadable.append(f"{path.relative_to(root)}: {exc}")

    assert unreadable == []
