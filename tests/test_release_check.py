from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.release_check import (
    DependencyLockError,
    changelog_section,
    validate_dependency_lock,
    validate_installed_closure,
)
from src.application.release_notes import parse_version_categories, render_release_notes


def _changelog(version_section: str) -> str:
    return (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        f"{version_section.strip()}\n\n"
        "## 1.4.2 - 2026-07-20\n\n"
        "### Fixed\n"
        "- Historical fix.\n"
    )


def _write_dependency_fixture(base: Path, *, lock: str | None = None) -> None:
    (base / "requirements").mkdir()
    (base / "constraints").mkdir()
    (base / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
    (base / "requirements/runtime.txt").write_text("pandas>=2\npackaging\n", encoding="utf-8")
    (base / "requirements/dev.txt").write_text("-r runtime.txt\npytest\n", encoding="utf-8")
    (base / "requirements/server.txt").write_text("-r runtime.txt\nlark-oapi\n", encoding="utf-8")
    (base / "constraints/release.txt").write_text(
        lock
        or (
            "pandas==3.0.5\n"
            "packaging==26.3\n"
            "pytest==9.1.1\n"
            "lark-oapi==1.7.3\n"
            "six==1.17.0\n"
            "colorama==0.4.6 ; sys_platform == 'win32'\n"
        ),
        encoding="utf-8",
    )
    (base / "constraints.txt").write_text("-c constraints/release.txt\n", encoding="utf-8")
    for name in ("runtime", "dev", "server"):
        (base / f"constraints/{name}.txt").write_text("-c release.txt\n", encoding="utf-8")


def test_current_release_taxonomy_parses_and_renders_in_canonical_order() -> None:
    text = _changelog(
        """
## 1.5.0 - 2026-07-26

### Bug Fixes
- Fixed duplicate processing.

### New Features
- Added assignment analysis.

### Improvements
- Improved table readability.

### Breaking Changes
- Removed an obsolete public command.
"""
    )

    parsed = parse_version_categories(text, "1.5.0")
    rendered = render_release_notes(version="1.5.0", evidence=parsed["evidence"])

    assert parsed["status"] == "ok"
    assert parsed["taxonomy"] == "current"
    assert rendered == (
        "# options-monitor 1.5.0\n\n"
        "### Breaking Changes\n"
        "- Removed an obsolete public command.\n\n"
        "### New Features\n"
        "- Added assignment analysis.\n\n"
        "### Improvements\n"
        "- Improved table readability.\n\n"
        "### Bug Fixes\n"
        "- Fixed duplicate processing.\n"
    )


def test_empty_categories_are_omitted_from_rendered_notes() -> None:
    text = _changelog(
        """
## 1.4.3 - 2026-07-26

### Bug Fixes
- Fixed duplicate processing.
"""
    )

    parsed = parse_version_categories(text, "1.4.3")

    assert parsed["status"] == "ok"
    assert render_release_notes(version="1.4.3", evidence=parsed["evidence"]) == (
        "# options-monitor 1.4.3\n\n"
        "### Bug Fixes\n"
        "- Fixed duplicate processing.\n"
    )


def test_exact_version_match_does_not_confuse_prefix_versions() -> None:
    text = _changelog(
        """
## 1.4.30 - 2026-07-26

### Bug Fixes
- Fixed duplicate processing.
"""
    )

    parsed = parse_version_categories(text, "1.4.3")

    assert parsed["status"] == "missing"
    assert parsed["reason_code"] == "MISSING_RELEASE_SECTION"
    assert changelog_section(text, "1.4.3") == ""


def test_duplicate_exact_version_sections_are_rejected() -> None:
    text = (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "## 1.4.3 - 2026-07-26\n\n"
        "### Bug Fixes\n"
        "- First fix.\n\n"
        "## 1.4.3 - 2026-07-27\n\n"
        "### Bug Fixes\n"
        "- Second fix.\n"
    )

    parsed = parse_version_categories(text, "1.4.3")

    assert parsed["status"] == "malformed"
    assert parsed["reason_code"] == "DUPLICATE_RELEASE_SECTION"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "### Changed\n- Legacy category.",
        "### Documentation\n- Unknown category.",
        "- Heading-free item.",
        "### Improvements\n  - Nested item.",
        "### Improvements\nParagraph outside the bullet grammar.",
    ],
)
def test_current_release_section_rejects_empty_legacy_or_unowned_content(body: str) -> None:
    text = _changelog(f"## 1.4.3 - 2026-07-26\n\n{body}")

    parsed = parse_version_categories(text, "1.4.3")

    assert parsed["status"] != "ok"


def test_legacy_history_can_be_read_without_weakening_current_taxonomy() -> None:
    text = _changelog(
        """
## 1.4.3 - 2026-07-26

### Added
- Historical feature.

### Changed
- Historical improvement.

### Fixed
- Historical fix.
"""
    )

    strict = parse_version_categories(text, "1.4.3")
    compatible = parse_version_categories(text, "1.4.3", allow_legacy=True)

    assert strict["status"] == "unsupported"
    assert compatible["status"] == "ok"
    assert compatible["taxonomy"] == "legacy"
    assert render_release_notes(version="1.4.3", evidence=compatible["evidence"]) == (
        "# options-monitor 1.4.3\n\n"
        "### New Features\n"
        "- Historical feature.\n\n"
        "### Improvements\n"
        "- Historical improvement.\n\n"
        "### Bug Fixes\n"
        "- Historical fix.\n"
    )


def test_release_dependency_lock_validates_exact_intent_and_compatibility_entries(tmp_path: Path) -> None:
    _write_dependency_fixture(tmp_path)

    locked = validate_dependency_lock(tmp_path)

    assert set(locked) == {"colorama", "lark-oapi", "packaging", "pandas", "pytest", "six"}


@pytest.mark.parametrize(
    ("lock", "message"),
    [
        ("pandas>=2\n", "one exact == version"),
        ("pandas @ https://example.invalid/pandas.whl\n", "one exact == version"),
        ("-c other.txt\n", "cannot contain pip options"),
    ],
)
def test_release_dependency_lock_rejects_non_exact_entries(
    tmp_path: Path,
    lock: str,
    message: str,
) -> None:
    _write_dependency_fixture(tmp_path, lock=lock)

    with pytest.raises(DependencyLockError, match=message):
        validate_dependency_lock(tmp_path)


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("requirements.txt", "-r requirements/runtime.txt\nrogue-package\n"),
        ("constraints/dev.txt", "-c release.txt\npytest==9.1.1\n"),
    ],
)
def test_release_dependency_lock_rejects_compatibility_file_drift(
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    _write_dependency_fixture(tmp_path)
    (tmp_path / relative_path).write_text(content, encoding="utf-8")

    with pytest.raises(DependencyLockError, match="must contain only"):
        validate_dependency_lock(tmp_path)


def test_release_dependency_lock_covers_and_satisfies_direct_requirements(tmp_path: Path) -> None:
    _write_dependency_fixture(tmp_path)
    path = tmp_path / "constraints/release.txt"
    original = path.read_text(encoding="utf-8")

    path.write_text(original.replace("lark-oapi==1.7.3\n", ""), encoding="utf-8")
    with pytest.raises(DependencyLockError, match="missing from release lock: lark-oapi"):
        validate_dependency_lock(tmp_path)

    path.write_text(original.replace("pandas==3.0.5", "pandas==1.5.0"), encoding="utf-8")
    with pytest.raises(DependencyLockError, match="does not satisfy direct requirement: pandas>=2"):
        validate_dependency_lock(tmp_path)


def test_release_dependency_lock_rejects_overlapping_active_versions(tmp_path: Path) -> None:
    _write_dependency_fixture(tmp_path)
    path = tmp_path / "constraints/release.txt"
    path.write_text(path.read_text(encoding="utf-8") + "pandas==3.0.4\n", encoding="utf-8")

    with pytest.raises(DependencyLockError, match="multiple release lock entries apply to pandas"):
        validate_dependency_lock(tmp_path)


def test_installed_environment_must_equal_active_lock_closure(tmp_path: Path) -> None:
    _write_dependency_fixture(tmp_path)
    locked = validate_dependency_lock(tmp_path)
    distributions = [
        SimpleNamespace(metadata={"Name": name}, version=version)
        for name, version in {
            "pandas": "3.0.5",
            "packaging": "26.3",
            "pytest": "9.1.1",
            "lark-oapi": "1.7.3",
            "six": "1.17.0",
            "pip": "26.0",
        }.items()
    ]
    def ok(*_args, **_kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    validate_installed_closure(locked, distributions=distributions, run_cmd=ok)

    distributions[0] = SimpleNamespace(metadata={"Name": "pandas"}, version="3.0.4")
    with pytest.raises(DependencyLockError, match="mismatched=pandas:3.0.4!=3.0.5"):
        validate_installed_closure(locked, distributions=distributions, run_cmd=ok)

    distributions[0] = SimpleNamespace(metadata={"Name": "pandas"}, version="3.0.5")
    distributions.append(SimpleNamespace(metadata={"Name": "unexpected"}, version="1.0"))
    with pytest.raises(DependencyLockError, match="extra=unexpected"):
        validate_installed_closure(locked, distributions=distributions, run_cmd=ok)


def test_reusable_release_requires_cross_platform_lock_validation_before_publish() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/_release-reusable.yml"
    ).read_text(encoding="utf-8")

    assert "os: [ubuntu-latest, macos-latest]" in workflow
    assert "needs: dependency-lock" in workflow
    assert "scripts/release_check.py --dependency-lock-only" in workflow
    assert "pip install pytest" not in workflow


def test_dependency_lock_only_cli_skips_release_metadata(monkeypatch, tmp_path: Path) -> None:
    from scripts import release_check

    _write_dependency_fixture(tmp_path)
    verified: list[dict] = []
    monkeypatch.setattr(release_check, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(release_check, "validate_installed_closure", verified.append)
    monkeypatch.setattr(sys, "argv", ["release_check.py", "--dependency-lock-only"])

    assert release_check.main() == 0
    assert len(verified) == 1
