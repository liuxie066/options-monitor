from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.application.release_target import (
    bump_version,
    parse_release_tags,
    parse_remote_stable_tag_identities,
)
from src.application.release_version_recommendation import parse_unreleased, recommend_release_version


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path, *, version: str = "1.0.0") -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "VERSION").write_text(version + "\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## Unreleased\n\n## {version} - 2026-07-20\n\n### Added\n- Baseline.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "VERSION", "CHANGELOG.md")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "tag", f"v{version}")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "HEAD:main", "--tags")
    return repo


def _write_unreleased(repo: Path, body: str, *, version: str = "1.0.0") -> None:
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## Unreleased\n\n{body.strip()}\n\n## {version} - 2026-07-20\n\n### Added\n- Baseline.\n",
        encoding="utf-8",
    )


def test_remote_stable_identity_parser_supports_lightweight_and_annotated_tags() -> None:
    rows = parse_remote_stable_tag_identities(
        "\n".join(
            [
                f"{'1' * 40} refs/tags/v1.2.9",
                f"{'2' * 40} refs/tags/v1.2.10",
                f"{'3' * 40} refs/tags/v1.2.10^{{}}",
                f"{'4' * 40} refs/tags/v1.3.0-rc.1",
                f"{'5' * 40} refs/tags/v01.3.0",
            ]
        )
    )

    assert [row.version for row in rows] == ["1.2.9", "1.2.10"]
    assert rows[0].remote_commit_sha == "1" * 40
    assert rows[1].remote_tag_object_sha == "2" * 40
    assert rows[1].remote_commit_sha == "3" * 40


def test_remote_stable_identity_parser_rejects_orphan_peeled_ref() -> None:
    with pytest.raises(ValueError, match="orphan peeled"):
        parse_remote_stable_tag_identities(f"{'1' * 40} refs/tags/v1.2.3^{{}}")


def test_existing_prerelease_parser_facade_is_unchanged() -> None:
    tags = parse_release_tags(
        f"{'1' * 40} refs/tags/v1.2.3-beta.1\n{'2' * 40} refs/tags/v1.2.3\n"
    )
    assert tags == [("1.2.3-beta.1", "v1.2.3-beta.1"), ("1.2.3", "v1.2.3")]
    assert bump_version("1.2.3-beta.1", "minor") == "1.3.0"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("### Breaking Changes\n- Removed an API.\n\n### Added\n- New feature.", "major"),
        ("### Added\n- New feature.\n\n### Fixed\n- Bug fix.", "minor"),
        ("### Changed\n- Internal behavior.\n\n### Fixed\n- Bug fix.", "patch"),
    ],
)
def test_recommendation_classification(tmp_path: Path, body: str, expected: str) -> None:
    repo = _repo(tmp_path)
    _write_unreleased(repo, body)

    result = recommend_release_version(base_dir=repo)

    assert result["status"] == "recommended"
    assert result["recommendation"]["bump"] == expected
    assert result["recommendation"]["manual_review_required"] is True
    assert result["recommendation_digest"].startswith("sha256:")
    assert result["write"] == {"changed": False, "already_at_target": False}


def test_empty_unreleased_requires_input(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = recommend_release_version(base_dir=repo)

    assert result["status"] == "needs_input"
    assert result["reason_code"] == "UNRELEASED_IMPACT_REQUIRED"


def test_unknown_unreleased_content_is_not_silently_classified(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_unreleased(repo, "### Added\n- New feature.\n\nRemoved legacy behavior.")

    result = recommend_release_version(base_dir=repo)

    assert result["status"] == "needs_input"
    assert result["reason_code"] == "UNSUPPORTED_UNRELEASED_CONTENT"
    assert result["evidence"]["unsupported"] == ["Removed legacy behavior."]


def test_duplicate_unreleased_is_malformed() -> None:
    parsed = parse_unreleased("# Changelog\n\n## Unreleased\n\n## Unreleased\n")
    assert parsed["status"] == "needs_input"
    assert parsed["reason_code"] == "MALFORMED_UNRELEASED_SECTION"


def test_sensitive_path_sets_review_flag(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_unreleased(repo, "### Added\n- New tool behavior.")
    path = repo / "src" / "application" / "agent_tools" / "new_tool.py"
    path.parent.mkdir(parents=True)
    path.write_text("VALUE = 1\n", encoding="utf-8")

    result = recommend_release_version(base_dir=repo)

    assert result["status"] == "recommended"
    assert result["review_flags"] == ["COMPATIBILITY_SENSITIVE_PATH_CHANGED"]
    assert result["evidence"]["sensitive_paths"] == ["src/application/agent_tools/new_tool.py"]


def test_recommendation_digest_changes_with_untracked_content(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_unreleased(repo, "### Fixed\n- Bug fix.")
    extra = repo / "notes.txt"
    extra.write_text("one\n", encoding="utf-8")
    first = recommend_release_version(base_dir=repo)

    extra.write_text("two\n", encoding="utf-8")
    second = recommend_release_version(base_dir=repo)

    assert first["status"] == second["status"] == "recommended"
    assert first["recommendation_digest"] != second["recommendation_digest"]


def test_remote_baseline_must_match_local_version(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_unreleased(repo, "### Fixed\n- Bug fix.")
    (repo / "VERSION").write_text("1.0.1\n", encoding="utf-8")

    result = recommend_release_version(base_dir=repo)

    assert result["status"] == "blocked"
    assert result["reason_code"] == "VERSION_BASE_MISMATCH"


def test_untracked_symlink_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_unreleased(repo, "### Fixed\n- Bug fix.")
    (repo / "target.txt").write_text("target\n", encoding="utf-8")
    (repo / "link.txt").symlink_to("target.txt")

    result = recommend_release_version(base_dir=repo)

    assert result["status"] == "blocked"
    assert result["reason_code"] == "EVIDENCE_UNSUPPORTED_FILE_TYPE"


def test_auto_preview_then_confirm_apply_end_to_end(tmp_path: Path) -> None:
    from src.application.version_check import update_local_version

    repo = _repo(tmp_path)
    _write_unreleased(repo, "### Added\n- New feature.")
    before_changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    preview = update_local_version(base_dir=repo, bump="auto", apply=False)
    applied = update_local_version(
        base_dir=repo,
        bump="auto",
        apply=True,
        recommendation_digest=preview["recommendation_digest"],
        expected_base_version=preview["base"]["version"],
        expected_target_version=preview["recommendation"]["target_version"],
    )

    assert preview["status"] == "recommended"
    assert applied["status"] == "applied"
    assert applied["write"]["changed"] is True
    assert (repo / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == before_changelog

    retried = update_local_version(
        base_dir=repo,
        bump="auto",
        apply=True,
        recommendation_digest=preview["recommendation_digest"],
        expected_base_version="1.0.0",
        expected_target_version="1.1.0",
    )
    assert retried["status"] == "already_at_target"
    assert retried["write"]["changed"] is False


def test_auto_apply_fails_stale_when_workspace_changes_after_preview(tmp_path: Path) -> None:
    from src.application.version_check import update_local_version

    repo = _repo(tmp_path)
    _write_unreleased(repo, "### Fixed\n- Bug fix.")
    preview = update_local_version(base_dir=repo, bump="auto", apply=False)
    (repo / "notes.txt").write_text("changed after preview\n", encoding="utf-8")

    applied = update_local_version(
        base_dir=repo,
        bump="auto",
        apply=True,
        recommendation_digest=preview["recommendation_digest"],
        expected_base_version=preview["base"]["version"],
        expected_target_version=preview["recommendation"]["target_version"],
    )

    assert applied["status"] == "stale"
    assert applied["write"]["changed"] is False
    assert (repo / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"
