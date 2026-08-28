from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import guardrails_check


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_repo_path_check_accepts_current_paths_suffixes_and_nondeterministic_tokens(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(guardrails_check, "ROOT", tmp_path)
    _write(tmp_path / "src/current.py", "")
    _write(tmp_path / "src/package/member.py", "")
    document = _write(
        tmp_path / "docs/current.md",
        "\n".join(
            (
                "`src/current.py`",
                "`src/package/`",
                "`src/current.py::handler`",
                "`src/current.py:42`",
                "`tests/*/test_service.py`",
                "`src/<module>.py`",
                "`src/[account]/state.py`",
                "`src/...`",
                "`scripts/tool.py --check`",
            )
        ),
    )

    assert guardrails_check.check_living_doc_repo_paths([document]) == []


def test_repo_path_check_exempts_only_explicit_path_lifecycle_markers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(guardrails_check, "ROOT", tmp_path)
    document = _write(
        tmp_path / "docs/current.md",
        "\n".join(
            (
                "Removed: `src/removed.py`",
                "Historical: `src/historical.py`",
                "Proposed: `src/proposed.py`",
                "示例：`src/example.py`",
                "Must not import `src/missing.py`.",
                "不得写入 `src/missing-cn.py`。",
                "Current owner: `src/missing-current.py`.",
            )
        ),
    )

    issues = guardrails_check.check_living_doc_repo_paths([document])

    assert [issue.line_no for issue in issues] == [5, 6, 7]
    assert all(issue.reason == "indexed living-doc repository path does not exist" for issue in issues)


def test_living_document_resolver_stops_before_history_and_follows_nested_subindex(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(guardrails_check, "ROOT", tmp_path)
    _write(
        tmp_path / "docs/INDEX.md",
        "\n".join(
            (
                "# Docs",
                "- [Current](CURRENT.md)",
                "- [Quality](quality/README.md)",
                "## 迁移与历史兼容",
                "- [Legacy](legacy.md)",
            )
        ),
    )
    _write(tmp_path / "docs/CURRENT.md", "current\n")
    _write(tmp_path / "docs/quality/README.md", "- [Implementation](implementation.md)\n")
    _write(tmp_path / "docs/quality/implementation.md", "implementation\n")
    _write(tmp_path / "docs/legacy.md", "legacy\n")

    documents, issues = guardrails_check.living_document_paths()

    assert issues == []
    assert [path.relative_to(tmp_path).as_posix() for path in documents] == [
        "docs/INDEX.md",
        "docs/CURRENT.md",
        "docs/quality/README.md",
        "docs/quality/implementation.md",
    ]


def test_staged_check_uses_index_bytes_and_path_authority(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(guardrails_check, "ROOT", tmp_path)
    _write(tmp_path / "docs/INDEX.md", "- [Current](CURRENT.md)\n")
    _write(
        tmp_path / "docs/CURRENT.md",
        "\n".join(
            (
                "`src/removed.py`",
                "`src/staged.py`",
                "`src/package/`",
            )
        ),
    )
    removed = _write(tmp_path / "src/removed.py", "")
    staged = _write(tmp_path / "src/staged.py", "")
    _write(tmp_path / "src/package/member.py", "")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "update-index", "--force-remove", "src/removed.py"],
        cwd=tmp_path,
        check=True,
    )
    staged.unlink()

    index_paths = {path.as_posix() for path in guardrails_check.git_index_paths()}

    def staged_path_exists(path: Path) -> bool:
        return guardrails_check.index_path_exists(path, index_paths)

    documents, target_issues = guardrails_check.living_document_paths(
        line_reader=guardrails_check.read_staged_lines,
        path_exists=staged_path_exists,
    )
    issues = guardrails_check.check_living_doc_repo_paths(
        documents,
        line_reader=guardrails_check.read_staged_lines,
        path_exists=staged_path_exists,
    )

    assert removed.exists()
    assert not staged.exists()
    assert target_issues == []
    assert [issue.line_no for issue in issues] == [1]


def test_staged_resolver_fails_closed_when_authority_or_target_is_deleted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(guardrails_check, "ROOT", tmp_path)
    index = _write(tmp_path / "docs/INDEX.md", "- [Current](CURRENT.md)\n")
    current = _write(tmp_path / "docs/CURRENT.md", "current\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    index_paths = {path.as_posix() for path in guardrails_check.git_index_paths()}

    def staged_path_exists(path: Path) -> bool:
        return guardrails_check.index_path_exists(path, index_paths)

    subprocess.run(
        ["git", "update-index", "--force-remove", "docs/CURRENT.md"],
        cwd=tmp_path,
        check=True,
    )
    index_paths = {path.as_posix() for path in guardrails_check.git_index_paths()}
    documents, issues = guardrails_check.living_document_paths(
        line_reader=guardrails_check.read_staged_lines,
        path_exists=staged_path_exists,
    )

    assert current.exists()
    assert documents == [index]
    assert [(issue.path.as_posix(), issue.line_no, issue.reason) for issue in issues] == [
        ("docs/INDEX.md", 1, "indexed living-doc target does not exist")
    ]

    subprocess.run(["git", "add", "docs/CURRENT.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "update-index", "--force-remove", "docs/INDEX.md"],
        cwd=tmp_path,
        check=True,
    )
    index_paths = {path.as_posix() for path in guardrails_check.git_index_paths()}
    documents, issues = guardrails_check.living_document_paths(
        line_reader=guardrails_check.read_staged_lines,
        path_exists=staged_path_exists,
    )

    assert index.exists()
    assert documents == []
    assert [(issue.path.as_posix(), issue.line_no, issue.reason) for issue in issues] == [
        ("docs/INDEX.md", 1, "living-doc authority index does not exist")
    ]
