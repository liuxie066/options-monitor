from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import release_check
from src.application.release_delta_coverage import (
    ReleaseDeltaCoverageError,
    build_release_delta_manifest,
    default_manifest_path,
    validate_release_delta_coverage,
)
from src.application.release_notes import parse_version_categories


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_changelog(repo: Path, *, released: bool) -> None:
    if released:
        body = (
            "## Unreleased\n\n"
            "## 1.1.0 - 2026-07-28\n\n"
            "### New Features\n"
            "- Added a reviewed feature.\n\n"
            "## 1.0.0 - 2026-07-27\n\n"
            "### New Features\n"
            "- Baseline.\n"
        )
    else:
        body = (
            "## Unreleased\n\n"
            "### New Features\n"
            "- Added a reviewed feature.\n\n"
            "## 1.0.0 - 2026-07-27\n\n"
            "### New Features\n"
            "- Baseline.\n"
        )
    (repo / "CHANGELOG.md").write_text(f"# Changelog\n\n{body}", encoding="utf-8")


def _prepared_repo(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    _write_changelog(repo, released=False)
    _git(repo, "add", "VERSION", "CHANGELOG.md")
    _git(repo, "commit", "-m", "chore: baseline")
    _git(repo, "tag", "v1.0.0")

    (repo / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "reviewed-feature.md").write_text(
        "# Reviewed feature\n\n## Decision points\n\n- Keep one implementation.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "feature.py", "docs/reviewed-feature.md")
    _git(repo, "commit", "-m", "feat: add reviewed feature")
    (repo / "internal.txt").write_text("test fixture\n", encoding="utf-8")
    _git(repo, "add", "internal.txt")
    _git(repo, "commit", "-m", "test: add internal fixture")

    manifest = build_release_delta_manifest(base_dir=repo, target_version="1.1.0")
    feature_sha = manifest["commits"][0]["sha"]
    internal_sha = manifest["commits"][1]["sha"]
    manifest["release_notes"][0]["commits"] = [feature_sha]
    manifest["no_release_note"] = [
        {
            "commit": internal_sha,
            "reason": "Test-only fixture with no user or operator-visible behavior.",
        }
    ]
    manifest["design_evidence"] = [
        {
            "commit": feature_sha,
            "references": ["docs/reviewed-feature.md#decision-points"],
        },
        {
            "commit": internal_sha,
            "references": ["https://github.com/liuxie066/options-monitor/pull/123"],
        },
    ]
    _write_changelog(repo, released=True)
    (repo / "VERSION").write_text("1.1.0\n", encoding="utf-8")
    path = default_manifest_path(repo, "1.1.0")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return repo, manifest


def _release_evidence(repo: Path) -> dict[str, object]:
    parsed = parse_version_categories(
        (repo / "CHANGELOG.md").read_text(encoding="utf-8"),
        "1.1.0",
    )
    assert parsed["status"] == "ok"
    return parsed["evidence"]


def test_manifest_builder_inventory_and_unreleased_notes_are_deterministic(tmp_path: Path) -> None:
    repo, reviewed = _prepared_repo(tmp_path)

    regenerated = build_release_delta_manifest(
        base_dir=repo,
        target_version="1.1.0",
        existing=reviewed,
    )

    assert regenerated["base"]["tag"] == "v1.0.0"
    assert regenerated["commits"] == reviewed["commits"]
    assert regenerated["release_notes"] == reviewed["release_notes"]
    assert regenerated["no_release_note"] == reviewed["no_release_note"]
    assert regenerated["design_evidence"] == reviewed["design_evidence"]


def test_manifest_builder_rejects_target_older_than_latest_stable_ancestor(
    tmp_path: Path,
) -> None:
    repo, _reviewed = _prepared_repo(tmp_path)

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        build_release_delta_manifest(base_dir=repo, target_version="0.9.0")

    assert exc_info.value.reason_code == "RELEASE_DELTA_TARGET_NOT_AFTER_BASE"


def test_delta_coverage_accepts_complete_review_before_and_after_release_commit(
    tmp_path: Path,
) -> None:
    repo, manifest = _prepared_repo(tmp_path)

    summary = validate_release_delta_coverage(
        base_dir=repo,
        version="1.1.0",
        release_evidence=_release_evidence(repo),
    )

    assert summary["base_tag"] == "v1.0.0"
    assert summary["reviewed_head"] == manifest["reviewed_head"]
    assert summary["commit_count"] == 2
    assert summary["release_note_count"] == 1
    assert summary["no_release_note_count"] == 1
    assert summary["design_evidence_count"] == 2

    _git(repo, "add", "VERSION", "CHANGELOG.md", "release/coverage/v1.1.0.json")
    _git(repo, "commit", "-m", "chore: release 1.1.0")
    _git(repo, "tag", "v1.1.0")

    post_commit = validate_release_delta_coverage(
        base_dir=repo,
        version="1.1.0",
        release_evidence=_release_evidence(repo),
    )
    assert post_commit == summary


def test_release_check_cli_enforces_delta_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _manifest = _prepared_repo(tmp_path)
    monkeypatch.setattr(release_check, "repo_base", lambda: repo)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_check.py",
            "--tag",
            "v1.1.0",
            "--require-current-taxonomy",
            "--require-delta-coverage",
        ],
    )

    assert release_check.main() == 0
    output = capsys.readouterr().out
    assert "release delta coverage valid from v1.0.0" in output
    assert "2 design dispositions" in output
    assert "release metadata valid for 1.1.0" in output


def test_delta_coverage_rejects_unreviewed_commit(tmp_path: Path) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["no_release_note"] = []
    default_manifest_path(repo, "1.1.0").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_UNREVIEWED_COMMITS"


def test_delta_coverage_rejects_commit_without_design_evidence(tmp_path: Path) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["design_evidence"] = manifest["design_evidence"][:1]
    default_manifest_path(repo, "1.1.0").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_DESIGN_EVIDENCE_MISSING"


def test_delta_coverage_rejects_untracked_design_document(tmp_path: Path) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["design_evidence"][0]["references"] = ["docs/missing-design.md"]
    default_manifest_path(repo, "1.1.0").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_DESIGN_PATH_MISSING"


@pytest.mark.parametrize("references", [[], [""]])
def test_delta_coverage_rejects_empty_design_references(
    tmp_path: Path,
    references: list[str],
) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["design_evidence"][0]["references"] = references
    default_manifest_path(repo, "1.1.0").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_DESIGN_REFERENCE_REQUIRED"


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.com/design/123",
        "https://github.com/example/options-monitor/pull/123",
        "docs/reviews/code-review.md",
        "../outside.md",
    ],
)
def test_delta_coverage_rejects_invalid_design_reference(
    tmp_path: Path,
    reference: str,
) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["design_evidence"][0]["references"] = [reference]
    default_manifest_path(repo, "1.1.0").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_DESIGN_REFERENCE_INVALID"


def test_delta_coverage_accepts_legacy_manifest_before_design_cutover(tmp_path: Path) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["schema_version"] = "release_delta_coverage.v1"
    manifest.pop("design_evidence")
    default_manifest_path(repo, "1.1.0").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = validate_release_delta_coverage(
        base_dir=repo,
        version="1.1.0",
        release_evidence=_release_evidence(repo),
    )

    assert summary["schema_version"] == "release_delta_coverage.v1"
    assert summary["design_evidence_count"] == 0


@pytest.mark.parametrize("target_version", ["2.1.4", "2.1.4-beta.1"])
def test_delta_coverage_requires_design_schema_after_2_1_3(
    tmp_path: Path,
    target_version: str,
) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["schema_version"] = "release_delta_coverage.v1"
    manifest["target_version"] = target_version
    manifest.pop("design_evidence")
    path = default_manifest_path(repo, target_version)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version=target_version,
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_DESIGN_SCHEMA_REQUIRED"


def test_delta_coverage_rejects_changelog_note_without_exact_mapping(tmp_path: Path) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["release_notes"][0]["text"] = "A different release note."
    default_manifest_path(repo, "1.1.0").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_NOTES_MISMATCH"


def test_delta_coverage_rejects_empty_no_note_reason(tmp_path: Path) -> None:
    repo, manifest = _prepared_repo(tmp_path)
    manifest["no_release_note"][0]["reason"] = ""
    default_manifest_path(repo, "1.1.0").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_REASON_REQUIRED"


def test_delta_coverage_rejects_code_hidden_in_release_metadata_commit(tmp_path: Path) -> None:
    repo, _manifest = _prepared_repo(tmp_path)
    (repo / "hidden.py").write_text("HIDDEN = True\n", encoding="utf-8")
    _git(
        repo,
        "add",
        "VERSION",
        "CHANGELOG.md",
        "release/coverage/v1.1.0.json",
        "hidden.py",
    )
    _git(repo, "commit", "-m", "chore: release 1.1.0")

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_RELEASE_COMMIT_SCOPE"


def test_delta_coverage_rejects_any_second_commit_after_review(tmp_path: Path) -> None:
    repo, _manifest = _prepared_repo(tmp_path)
    _git(repo, "add", "VERSION", "CHANGELOG.md", "release/coverage/v1.1.0.json")
    _git(repo, "commit", "-m", "chore: release 1.1.0")
    (repo / "late.py").write_text("LATE = True\n", encoding="utf-8")
    _git(repo, "add", "late.py")
    _git(repo, "commit", "-m", "feat: late unreviewed change")

    with pytest.raises(ReleaseDeltaCoverageError) as exc_info:
        validate_release_delta_coverage(
            base_dir=repo,
            version="1.1.0",
            release_evidence=_release_evidence(repo),
        )

    assert exc_info.value.reason_code == "RELEASE_DELTA_POST_REVIEW_COMMITS"


def _run_commit_msg_hook(tmp_path: Path, subject: str) -> subprocess.CompletedProcess[str]:
    message_path = tmp_path / "COMMIT_EDITMSG"
    message_path.write_text(f"{subject}\n", encoding="utf-8")
    hook_path = Path(__file__).resolve().parents[1] / ".githooks" / "commit-msg"
    return subprocess.run(
        ["bash", str(hook_path), str(message_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_commit_msg_hook_accepts_canonical_release_subject(tmp_path: Path) -> None:
    result = _run_commit_msg_hook(tmp_path, "chore: release 1.13.0")

    assert result.returncode == 0


def test_commit_msg_hook_still_rejects_other_unscoped_subjects(tmp_path: Path) -> None:
    result = _run_commit_msg_hook(tmp_path, "fix: unscoped change")

    assert result.returncode == 1
    assert "<type>(<scope>): <subject> or chore: release <version>" in result.stderr
