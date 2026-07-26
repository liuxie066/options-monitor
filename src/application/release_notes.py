from __future__ import annotations

import re
from typing import Any

from src.application.release_target import VERSION_RE


CURRENT_RELEASE_HEADINGS = {
    "Breaking Changes": "breaking_changes",
    "New Features": "added",
    "Improvements": "changed",
    "Bug Fixes": "fixed",
}
LEGACY_RELEASE_HEADINGS = {
    "Breaking Changes": "breaking_changes",
    "Added": "added",
    "Changed": "changed",
    "Fixed": "fixed",
}
RELEASE_CATEGORY_ORDER = (
    ("Breaking Changes", "breaking_changes"),
    ("New Features", "added"),
    ("Improvements", "changed"),
    ("Bug Fixes", "fixed"),
)
_VERSION_HEADING_RE = re.compile(
    r"^## (?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?) - (?P<date>\d{4}-\d{2}-\d{2})$"
)


def empty_release_evidence() -> dict[str, list[str]]:
    return {
        "breaking_changes": [],
        "added": [],
        "changed": [],
        "fixed": [],
        "unsupported": [],
    }


def parse_unreleased_categories(changelog_text: str) -> dict[str, Any]:
    lines = _normalized_lines(changelog_text)
    indexes = [index for index, line in enumerate(lines) if line == "## Unreleased"]
    if len(indexes) != 1:
        return _result(
            status="malformed",
            reason_code="MALFORMED_UNRELEASED_SECTION",
            message="CHANGELOG.md must contain exactly one exact '## Unreleased' heading",
        )

    start = indexes[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.startswith("## "):
            continue
        if not _VERSION_HEADING_RE.fullmatch(line):
            evidence = empty_release_evidence()
            evidence["unsupported"] = [line]
            return _result(
                status="unsupported",
                reason_code="UNSUPPORTED_UNRELEASED_CONTENT",
                message="Unreleased must be followed by a dated release-version heading",
                canonical_text="\n".join(lines[start : index + 1]).strip(),
                evidence=evidence,
            )
        end = index
        break

    return _parse_category_lines(
        lines[start:end],
        headings=CURRENT_RELEASE_HEADINGS,
        unsupported_reason_code="UNSUPPORTED_UNRELEASED_CONTENT",
        unsupported_message="Unreleased contains content outside the supported release-intent grammar",
        empty_reason_code="UNRELEASED_IMPACT_REQUIRED",
        empty_message="Unreleased contains no declared release impact",
        taxonomy="current",
    )


def parse_version_categories(
    changelog_text: str,
    version: str,
    *,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    if not VERSION_RE.fullmatch(str(version or "").strip()):
        return _result(
            status="malformed",
            reason_code="INVALID_RELEASE_VERSION",
            message=f"invalid release version: {version}",
        )

    lines = _normalized_lines(changelog_text)
    indexes = [
        index
        for index, line in enumerate(lines)
        if (match := _VERSION_HEADING_RE.fullmatch(line)) and match.group("version") == version
    ]
    if not indexes:
        return _result(
            status="missing",
            reason_code="MISSING_RELEASE_SECTION",
            message=f"CHANGELOG.md missing exact dated section for {version}",
        )
    if len(indexes) != 1:
        return _result(
            status="malformed",
            reason_code="DUPLICATE_RELEASE_SECTION",
            message=f"CHANGELOG.md contains duplicate sections for {version}",
        )

    start = indexes[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    section_lines = lines[start + 1 : end]
    headings, taxonomy = _headings_for_version_section(section_lines, allow_legacy=allow_legacy)
    parsed = _parse_category_lines(
        section_lines,
        headings=headings,
        unsupported_reason_code="UNSUPPORTED_RELEASE_CONTENT",
        unsupported_message=f"release section {version} contains unsupported content",
        empty_reason_code="RELEASE_CONTENT_REQUIRED",
        empty_message=f"release section {version} contains no release items",
        taxonomy=taxonomy,
    )
    parsed["section_heading"] = lines[start]
    return parsed


def render_release_notes(*, version: str, evidence: dict[str, Any]) -> str:
    body: list[str] = []
    for heading, key in RELEASE_CATEGORY_ORDER:
        items = [str(item).strip() for item in evidence.get(key, []) if str(item).strip()]
        if not items:
            continue
        if body:
            body.append("")
        body.append(f"### {heading}")
        body.extend(f"- {item}" for item in items)
    rendered = "\n".join(body)
    return f"# options-monitor {version}\n\n{rendered}\n"


def _headings_for_version_section(
    section_lines: list[str],
    *,
    allow_legacy: bool,
) -> tuple[dict[str, str], str]:
    headings = {line[4:] for line in section_lines if line.startswith("### ")}
    current_only = set(CURRENT_RELEASE_HEADINGS) - set(LEGACY_RELEASE_HEADINGS)
    legacy_only = set(LEGACY_RELEASE_HEADINGS) - set(CURRENT_RELEASE_HEADINGS)
    has_current = bool(headings & current_only)
    has_legacy = bool(headings & legacy_only)
    if allow_legacy and has_legacy and not has_current:
        return LEGACY_RELEASE_HEADINGS, "legacy"
    return CURRENT_RELEASE_HEADINGS, "current"


def _parse_category_lines(
    lines: list[str],
    *,
    headings: dict[str, str],
    unsupported_reason_code: str,
    unsupported_message: str,
    empty_reason_code: str,
    empty_message: str,
    taxonomy: str,
) -> dict[str, Any]:
    evidence = empty_release_evidence()
    unsupported: list[str] = []
    current_key: str | None = None
    seen_headings: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("### "):
            heading = line[4:]
            current_key = None
            if heading not in headings or heading in seen_headings:
                unsupported.append(line)
                continue
            seen_headings.add(heading)
            current_key = headings[heading]
            continue
        if line.startswith("- ") and line[2:].strip() and current_key is not None:
            evidence[current_key].append(line[2:].strip())
            continue
        unsupported.append(line)

    canonical_text = "\n".join(lines).strip()
    if unsupported:
        evidence["unsupported"] = unsupported
        return _result(
            status="unsupported",
            reason_code=unsupported_reason_code,
            message=unsupported_message,
            canonical_text=canonical_text,
            evidence=evidence,
            taxonomy=taxonomy,
        )
    if not any(evidence[key] for key in ("breaking_changes", "added", "changed", "fixed")):
        return _result(
            status="empty",
            reason_code=empty_reason_code,
            message=empty_message,
            canonical_text=canonical_text,
            evidence=evidence,
            taxonomy=taxonomy,
        )
    return _result(
        status="ok",
        reason_code=None,
        message="release categories parsed",
        canonical_text=canonical_text,
        evidence=evidence,
        taxonomy=taxonomy,
    )


def _normalized_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _result(
    *,
    status: str,
    reason_code: str | None,
    message: str,
    canonical_text: str = "",
    evidence: dict[str, list[str]] | None = None,
    taxonomy: str = "current",
) -> dict[str, Any]:
    return {
        "status": status,
        "reason_code": reason_code,
        "message": message,
        "canonical_text": canonical_text,
        "evidence": evidence or empty_release_evidence(),
        "taxonomy": taxonomy,
    }


__all__ = [
    "CURRENT_RELEASE_HEADINGS",
    "LEGACY_RELEASE_HEADINGS",
    "RELEASE_CATEGORY_ORDER",
    "empty_release_evidence",
    "parse_unreleased_categories",
    "parse_version_categories",
    "render_release_notes",
]
