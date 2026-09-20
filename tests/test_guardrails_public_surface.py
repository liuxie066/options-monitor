from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts import guardrails_check

_GIT = ["git", "-c", "user.name=guardrails test", "-c", "user.email=test@example.invalid"]


def _bare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "--quiet", "--initial-branch=main")
    return repo


def _try_run(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_GIT, *arguments],
        cwd=str(repo),
        check=False,
        capture_output=True,
        text=True,
    )


def _run(repo: Path, *arguments: str) -> str:
    completed = _try_run(repo, *arguments)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _drop_object(repo: Path, revision: str, relative: str) -> None:
    """Unlink a blob from the local object store, as a partial clone or a pruned repo would."""
    sha = _run(repo, "rev-parse", f"{revision}:{relative}")
    loose = repo / ".git" / "objects" / sha[:2] / sha[2:]
    if not loose.is_file():
        raise AssertionError(f"{relative} is not a loose object; cannot make it unreadable")
    loose.unlink()


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


@pytest.mark.parametrize(
    "source",
    [
        '__all__ = ["a"]\n__all__ += ["b"]\n',
        '__all__ = ["a"]\n__all__.append("b")\n',
        '__all__ = ["a"]\n__all__.extend(["b"])\n',
        '__all__ = ["a"]\n__all__[0] = "b"\n',
        "if True:\n    __all__ = ['a']\n",
        "try:\n    __all__ = ['a']\nexcept Exception:\n    __all__ = []\n",
        '__all__ = ["a"]\n__all__ = ["a", "b"]\n',
        "del __all__\n",
    ],
)
def test_an_all_that_cannot_be_followed_is_reported_not_read_as_empty(source: str) -> None:
    """Reading these as "no ``__all__``" would drop the protection of every exported name."""
    with pytest.raises(guardrails_check.PublicSurfaceUnavailable):
        guardrails_check.declared_public_names(source)


@pytest.mark.parametrize(
    "source",
    [
        "if sys.version_info >= (3, 12):\n    def modern():\n        pass\n",
        "try:\n    def modern():\n        pass\nexcept Exception:\n    pass\n",
        "try:\n    def modern():\n        pass\nexcept* ValueError:\n    pass\n",
        "match FLAG:\n    case 1:\n        def modern():\n            pass\n",
        "for _ in ():\n    def modern():\n        pass\n",
        "while False:\n    def modern():\n        pass\n",
        "with contextlib.suppress(Exception):\n    def modern():\n        pass\n",
    ],
)
def test_definition_behind_a_guard_is_still_declared(source: str) -> None:
    """Every container a module may legally use has to be walked.

    A definition inside a container the reading does not enter is a name this
    check never protects, and nothing else would report it.
    """
    assert guardrails_check.declared_public_names(source) == frozenset({"modern"})


def test_assignment_behind_a_guard_is_not_treated_as_an_exported_name() -> None:
    """``except ImportError: fcntl = None`` is a fallback, not a name the module promises."""
    source = "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n"

    assert guardrails_check.declared_public_names(source) == frozenset()


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
    padded = f" {_reason()} "
    pointer = {"module": "src/pkg/mod.py", "name": "gone", "reason": padded}
    wildcard = {"module": "src/pkg/retired.py", "name": "*", "reason": _reason().upper()}
    source = json.dumps({"retirements": [pointer, wildcard]})

    entries, issues = guardrails_check._retirement_entries(source, label="head")

    assert issues == []
    # Returned verbatim and in order: the record is evidence, so nothing about it
    # is normalised on the way through -- ``reason.strip()`` is only used to
    # measure the explanation, never to rewrite it.
    assert entries == [pointer, wildcard]
    assert entries[0]["reason"] == padded


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
    # The message lists what disappeared, so a name that stayed must not appear:
    # reporting every public name would pass a check that only looked for "dropped".
    assert "kept" not in rendered


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
    # Tracked on purpose: an untracked file is invisible to ``git diff``, which
    # would make this line a fixture the check never sees.
    _run(repo, "add", "--all")
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
    # Tracked, so the rename is what the check sees -- a deletion and an addition
    # -- instead of a deletion with the new path invisible to ``git diff``.
    _run(repo, "add", "--all")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "src/pkg/old.py" in issues[0].render()
    assert "moved" in issues[0].render()
    assert "src/pkg/new.py" not in issues[0].render()


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


def test_staged_comparison_reads_the_index_not_the_working_tree(monkeypatch, tmp_path: Path) -> None:
    """The index and the working tree disagree here, so each mode has one right answer.

    Dropping ``--cached`` would diff the working tree (which still has the name),
    and reading the file instead of ``:path`` would compare the same content, so
    both halves of the staged path are pinned by this test.
    """
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n\ndef dropped():\n    pass\n")
    base = _commit(repo, "base")
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n")
    _run(repo, "add", "src/pkg/mod.py")
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n\ndef dropped():\n    pass\n")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    staged_issues = guardrails_check.check_public_surface(base, staged=True)

    assert len(staged_issues) == 1
    assert "dropped" in staged_issues[0].render()
    assert guardrails_check.check_public_surface(base) == []


def test_definition_moved_behind_a_guard_is_not_reported(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def kept():\n    pass\n\ndef guarded():\n    pass\n")
    base = _commit(repo, "base")
    _write(
        repo,
        "src/pkg/mod.py",
        "def kept():\n    pass\n\nif True:\n    def guarded():\n        pass\n",
    )
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_public_surface(base) == []


def test_constant_moved_behind_a_guard_is_not_reported(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "VERSION = '1'\n")
    base = _commit(repo, "base")
    _write(
        repo,
        "src/pkg/mod.py",
        "import os\n\nif os.environ.get('OM_VERSION'):\n    VERSION = '1'\nelse:\n    VERSION = '2'\n",
    )
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_public_surface(base) == []


def test_restoring_a_retired_module_allows_dropping_its_wildcard_entry(
    monkeypatch, tmp_path: Path
) -> None:
    """A revert puts the module back; the entry that recorded its retirement is then false."""
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/kept.py", "def kept():\n    pass\n")
    _write_ledger(repo, [{"module": "src/pkg/retired.py", "name": "*", "reason": _reason()}])
    base = _commit(repo, "base")
    _write(repo, "src/pkg/retired.py", "def first():\n    pass\n")
    _write_ledger(repo, [])
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_public_surface(base) == []


def test_a_wildcard_entry_cannot_be_rewritten_into_another_name(monkeypatch, tmp_path: Path) -> None:
    """Rewriting it would leave an entry for a module that is back, exempting it again."""
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/kept.py", "def kept():\n    pass\n")
    _write_ledger(repo, [{"module": "src/pkg/retired.py", "name": "*", "reason": _reason()}])
    base = _commit(repo, "base")
    _write(repo, "src/pkg/retired.py", "def first():\n    pass\n")
    _write_ledger(repo, [{"module": "src/pkg/retired.py", "name": "first", "reason": _reason()}])
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "append-only" in issues[0].reason


def test_unmerged_index_entry_is_reported_instead_of_skipped(monkeypatch, tmp_path: Path) -> None:
    """``U`` is not a state this check can compare, so it must not read as "nothing to see"."""
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def alpha():\n    pass\n\ndef beta():\n    pass\n")
    base = _commit(repo, "base")
    _run(repo, "checkout", "--quiet", "-b", "theirs")
    _write(repo, "src/pkg/mod.py", "def alpha():\n    pass\n")
    _commit(repo, "theirs")
    _run(repo, "checkout", "--quiet", "main")
    _write(repo, "src/pkg/mod.py", "def gamma():\n    pass\n")
    _commit(repo, "ours")
    conflict = _try_run(repo, "merge", "theirs")
    assert "CONFLICT" in conflict.stdout, "the merge was expected to conflict"
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base, staged=True)

    assert len(issues) == 1
    assert "cannot interpret" in issues[0].reason


def test_unreadable_base_module_content_is_reported(monkeypatch, tmp_path: Path) -> None:
    """A blob missing from the local store is not the same thing as a module that is gone."""
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "def alpha():\n    pass\n")
    base = _commit(repo, "base")
    _drop_object(repo, base, "src/pkg/mod.py")
    (repo / "src/pkg/mod.py").unlink()
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "could not be read" in issues[0].reason


def test_unreadable_base_ledger_is_reported(monkeypatch, tmp_path: Path) -> None:
    """Reading the unreadable ledger as "no entries" would void append-only for this change."""
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/kept.py", "def kept():\n    pass\n")
    _write_ledger(repo, [{"module": "src/pkg/retired.py", "name": "*", "reason": _reason()}])
    base = _commit(repo, "base")
    _drop_object(repo, base, guardrails_check.PUBLIC_SURFACE_LEDGER.as_posix())
    _write_ledger(repo, [])
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "could not be read" in issues[0].reason
    assert "append-only" in issues[0].reason


def test_symlinked_module_at_base_is_reported(monkeypatch, tmp_path: Path) -> None:
    """A symlink's blob is the link target's path, which parses as an empty module."""
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/impl.py", '__all__ = ["alpha", "beta"]\n')
    (repo / "src/pkg/foo.py").symlink_to("impl.py")
    base = _commit(repo, "base")
    (repo / "src/pkg/foo.py").unlink()
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "symbolic link at the base revision" in issues[0].reason


def test_symlinked_module_at_head_is_reported(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/foo.py", '__all__ = ["alpha", "beta"]\n')
    base = _commit(repo, "base")
    outside = _write(tmp_path, "outside.py", '__all__ = ["alpha"]\n')
    (repo / "src/pkg/foo.py").unlink()
    (repo / "src/pkg/foo.py").symlink_to(outside)
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert len(issues) == 1
    assert "symbolic link at the revision under review" in issues[0].reason


def test_wildcard_retirement_is_refused_for_a_symlinked_module(monkeypatch, tmp_path: Path) -> None:
    """Otherwise a link standing in for the module exempts every later removal from it."""
    repo = _bare_repo(tmp_path)
    _write(repo, "src/pkg/foo.py", '__all__ = ["alpha", "beta"]\n')
    base = _commit(repo, "base")
    outside = _write(tmp_path, "outside.py", '__all__ = ["alpha"]\n')
    (repo / "src/pkg/foo.py").unlink()
    (repo / "src/pkg/foo.py").symlink_to(outside)
    _write_ledger(repo, [{"module": "src/pkg/foo.py", "name": "*", "reason": _reason()}])
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    issues = guardrails_check.check_public_surface(base)

    assert any("only valid when the module itself is gone" in issue.reason for issue in issues)


def test_changes_outside_the_checked_roots_are_not_examined(monkeypatch, tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write(repo, "tests/test_mod.py", "def dropped():\n    pass\n")
    base = _commit(repo, "base")
    _write(repo, "tests/test_mod.py", "\n")
    monkeypatch.setattr(guardrails_check, "ROOT", repo)

    assert guardrails_check.check_public_surface(base) == []


def test_the_checked_roots_are_pinned() -> None:
    """Narrowing this tuple would unprotect a whole tree, and nothing else would notice."""
    assert guardrails_check.PUBLIC_SURFACE_ROOTS == ("src", "domain", "scripts")


def test_every_checked_module_has_a_statically_readable_declared_surface() -> None:
    """A module whose surface cannot be read would fail every later change that touches it."""
    root = Path(__file__).resolve().parents[1]
    unreadable: list[str] = []
    counted = {directory: 0 for directory in guardrails_check.PUBLIC_SURFACE_ROOTS}
    for directory in guardrails_check.PUBLIC_SURFACE_ROOTS:
        for path in sorted((root / directory).rglob("*.py")):
            counted[directory] += 1
            source = path.read_text(encoding="utf-8", errors="ignore")
            try:
                guardrails_check.declared_public_names(source)
            except guardrails_check.PublicSurfaceUnavailable as exc:
                unreadable.append(f"{path.relative_to(root)}: {exc}")

    assert unreadable == []
    # A root that stopped matching would leave this test silently checking nothing.
    for directory, count in counted.items():
        assert count > 0, f"{directory}/ holds no module, so nothing under it is checked"
