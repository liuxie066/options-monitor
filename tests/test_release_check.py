from __future__ import annotations

import pytest

from scripts.release_check import changelog_section
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
