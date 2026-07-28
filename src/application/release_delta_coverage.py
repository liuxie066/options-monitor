from __future__ import annotations

from collections import Counter
import json
from functools import cmp_to_key
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from src.application.release_notes import (
    RELEASE_CATEGORY_ORDER,
    parse_unreleased_categories,
    parse_version_categories,
)
from src.application.release_target import TAG_RE, VERSION_RE, compare_versions, parse_version


SCHEMA_VERSION = "release_delta_coverage.v1"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_METADATA_PATHS = {"VERSION", "CHANGELOG.md"}
RunCommand = Callable[..., Any]


class ReleaseDeltaCoverageError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def default_manifest_path(base_dir: Path, version: str) -> Path:
    target_version = _validated_version(version)
    return base_dir.resolve() / "release" / "coverage" / f"v{target_version}.json"


def build_release_delta_manifest(
    *,
    base_dir: Path,
    target_version: str,
    existing: dict[str, Any] | None = None,
    run_cmd: RunCommand = subprocess.run,
) -> dict[str, Any]:
    base = base_dir.resolve()
    version = _validated_version(target_version)
    reviewed_head = _git_stdout(base, ["rev-parse", "HEAD"], run_cmd=run_cmd).lower()
    base_tag, base_commit = _resolve_base_tag(
        base=base,
        target_version=version,
        reviewed_head=reviewed_head,
        run_cmd=run_cmd,
    )
    commits = _git_commits(
        base=base,
        base_commit=base_commit,
        reviewed_head=reviewed_head,
        run_cmd=run_cmd,
    )
    release_notes = _declared_release_notes(base=base, target_version=version)

    preserved_note_commits, preserved_no_note = _preserved_assignments(existing)
    commit_shas = {item["sha"] for item in commits}
    for note in release_notes:
        key = (note["category"], note["text"])
        note["commits"] = [
            sha for sha in preserved_note_commits.get(key, []) if sha in commit_shas
        ]

    no_release_note = [
        item for item in preserved_no_note if item["commit"] in commit_shas
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "target_version": version,
        "base": {"tag": base_tag, "commit": base_commit},
        "reviewed_head": reviewed_head,
        "commits": commits,
        "release_notes": release_notes,
        "no_release_note": no_release_note,
    }


def validate_release_delta_coverage(
    *,
    base_dir: Path,
    version: str,
    release_evidence: dict[str, Any],
    manifest_path: Path | None = None,
    run_cmd: RunCommand = subprocess.run,
) -> dict[str, Any]:
    base = base_dir.resolve()
    target_version = _validated_version(version)
    path = (manifest_path or default_manifest_path(base, target_version)).resolve()
    relative_manifest = _relative_manifest_path(base=base, manifest_path=path)
    manifest = _load_manifest(path)
    _require_exact_keys(
        manifest,
        {
            "schema_version",
            "target_version",
            "base",
            "reviewed_head",
            "commits",
            "release_notes",
            "no_release_note",
        },
        context="manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        _fail(
            "UNSUPPORTED_RELEASE_DELTA_SCHEMA",
            f"expected schema_version {SCHEMA_VERSION}",
        )
    if manifest["target_version"] != target_version:
        _fail(
            "RELEASE_DELTA_VERSION_MISMATCH",
            f"manifest target_version {manifest['target_version']!r} does not match VERSION {target_version}",
        )

    reviewed_head = _full_sha(manifest["reviewed_head"], field="reviewed_head")
    current_head = _git_stdout(base, ["rev-parse", "HEAD"], run_cmd=run_cmd).lower()
    _validate_reviewed_head_boundary(
        base=base,
        version=target_version,
        reviewed_head=reviewed_head,
        current_head=current_head,
        relative_manifest=relative_manifest,
        run_cmd=run_cmd,
    )

    base_data = manifest["base"]
    if not isinstance(base_data, dict):
        _fail("MALFORMED_RELEASE_DELTA", "base must be an object")
    _require_exact_keys(base_data, {"tag", "commit"}, context="base")
    expected_base_tag, expected_base_commit = _resolve_base_tag(
        base=base,
        target_version=target_version,
        reviewed_head=reviewed_head,
        run_cmd=run_cmd,
    )
    if base_data["tag"] != expected_base_tag:
        _fail(
            "RELEASE_DELTA_BASE_MISMATCH",
            f"manifest base tag {base_data['tag']!r} does not match {expected_base_tag}",
        )
    manifest_base_commit = _full_sha(base_data["commit"], field="base.commit")
    if manifest_base_commit != expected_base_commit:
        _fail(
            "RELEASE_DELTA_BASE_MISMATCH",
            f"manifest base commit {manifest_base_commit} does not match {expected_base_commit}",
        )

    actual_commits = _git_commits(
        base=base,
        base_commit=expected_base_commit,
        reviewed_head=reviewed_head,
        run_cmd=run_cmd,
    )
    manifest_commits = _validate_commit_inventory(manifest["commits"])
    if manifest_commits != actual_commits:
        _fail(
            "RELEASE_DELTA_COMMIT_INVENTORY_MISMATCH",
            "manifest commit inventory is stale; regenerate it from the previous stable tag",
        )

    expected_notes = _notes_from_evidence(release_evidence)
    note_commit_refs = _validate_release_note_mappings(
        manifest["release_notes"],
        expected_notes=expected_notes,
        commit_shas={item["sha"] for item in actual_commits},
    )
    no_note_refs = _validate_no_release_note(
        manifest["no_release_note"],
        commit_shas={item["sha"] for item in actual_commits},
    )
    overlap = sorted(note_commit_refs & no_note_refs)
    if overlap:
        _fail(
            "RELEASE_DELTA_CONFLICTING_DISPOSITION",
            f"commits cannot have both release-note and no-release-note dispositions: {_short_shas(overlap)}",
        )
    all_commit_shas = {item["sha"] for item in actual_commits}
    uncovered = sorted(all_commit_shas - note_commit_refs - no_note_refs)
    if uncovered:
        _fail(
            "RELEASE_DELTA_UNREVIEWED_COMMITS",
            f"commits without a release-note mapping or explicit no-release-note reason: {_short_shas(uncovered)}",
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_path": relative_manifest,
        "base_tag": expected_base_tag,
        "base_commit": expected_base_commit,
        "reviewed_head": reviewed_head,
        "commit_count": len(actual_commits),
        "release_note_count": len(expected_notes),
        "no_release_note_count": len(no_note_refs),
    }


def _declared_release_notes(*, base: Path, target_version: str) -> list[dict[str, Any]]:
    changelog = (base / "CHANGELOG.md").read_text(encoding="utf-8")
    version_section = parse_version_categories(changelog, target_version)
    if version_section["status"] == "ok":
        evidence = version_section["evidence"]
    else:
        unreleased = parse_unreleased_categories(changelog)
        if unreleased["status"] != "ok":
            _fail(
                "RELEASE_DELTA_NOTES_REQUIRED",
                "target release section is unavailable and Unreleased is not ready for review",
            )
        evidence = unreleased["evidence"]
    return [
        {**item, "commits": []}
        for item in _notes_from_evidence(evidence)
    ]


def _notes_from_evidence(evidence: dict[str, Any]) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    for category, key in RELEASE_CATEGORY_ORDER:
        for raw_item in evidence.get(key, []):
            text = str(raw_item).strip()
            if text:
                notes.append({"category": category, "text": text})
    return notes


def _preserved_assignments(
    existing: dict[str, Any] | None,
) -> tuple[dict[tuple[str, str], list[str]], list[dict[str, str]]]:
    if existing is None:
        return {}, []
    if not isinstance(existing, dict):
        _fail("MALFORMED_RELEASE_DELTA", "existing manifest must be an object")

    by_note: dict[tuple[str, str], list[str]] = {}
    raw_notes = existing.get("release_notes", [])
    if not isinstance(raw_notes, list):
        _fail("MALFORMED_RELEASE_DELTA", "existing release_notes must be an array")
    for item in raw_notes:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        text = str(item.get("text") or "").strip()
        commits = item.get("commits")
        if not category or not text or not isinstance(commits, list):
            continue
        by_note[(category, text)] = [
            str(sha).strip().lower()
            for sha in commits
            if FULL_SHA_RE.fullmatch(str(sha).strip().lower())
        ]

    no_note: list[dict[str, str]] = []
    raw_no_note = existing.get("no_release_note", [])
    if not isinstance(raw_no_note, list):
        _fail("MALFORMED_RELEASE_DELTA", "existing no_release_note must be an array")
    for item in raw_no_note:
        if not isinstance(item, dict):
            continue
        commit = str(item.get("commit") or "").strip().lower()
        reason = str(item.get("reason") or "").strip()
        if FULL_SHA_RE.fullmatch(commit) and reason:
            no_note.append({"commit": commit, "reason": reason})
    return by_note, no_note


def _validate_commit_inventory(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        _fail("MALFORMED_RELEASE_DELTA", "commits must be an array")
    commits: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail("MALFORMED_RELEASE_DELTA", f"commits[{index}] must be an object")
        _require_exact_keys(item, {"sha", "subject"}, context=f"commits[{index}]")
        sha = _full_sha(item["sha"], field=f"commits[{index}].sha")
        subject = str(item["subject"])
        if not subject.strip() or "\n" in subject or "\r" in subject:
            _fail(
                "MALFORMED_RELEASE_DELTA",
                f"commits[{index}].subject must be one non-empty line",
            )
        if sha in seen:
            _fail("MALFORMED_RELEASE_DELTA", f"duplicate commit inventory entry: {sha}")
        seen.add(sha)
        commits.append({"sha": sha, "subject": subject})
    return commits


def _validate_release_note_mappings(
    value: Any,
    *,
    expected_notes: list[dict[str, str]],
    commit_shas: set[str],
) -> set[str]:
    if not isinstance(value, list):
        _fail("MALFORMED_RELEASE_DELTA", "release_notes must be an array")
    actual_notes: list[dict[str, str]] = []
    referenced: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail("MALFORMED_RELEASE_DELTA", f"release_notes[{index}] must be an object")
        _require_exact_keys(
            item,
            {"category", "text", "commits"},
            context=f"release_notes[{index}]",
        )
        category = str(item["category"]).strip()
        text = str(item["text"]).strip()
        commits = item["commits"]
        if not category or not text:
            _fail(
                "MALFORMED_RELEASE_DELTA",
                f"release_notes[{index}] category and text are required",
            )
        if not isinstance(commits, list) or not commits:
            _fail(
                "RELEASE_DELTA_NOTE_WITHOUT_COMMIT",
                f"release note {category!r}: {text!r} must reference at least one commit",
            )
        note_seen: set[str] = set()
        for raw_sha in commits:
            sha = _full_sha(raw_sha, field=f"release_notes[{index}].commits")
            if sha not in commit_shas:
                _fail(
                    "RELEASE_DELTA_UNKNOWN_COMMIT",
                    f"release note references a commit outside the reviewed delta: {sha}",
                )
            if sha in note_seen:
                _fail(
                    "MALFORMED_RELEASE_DELTA",
                    f"release_notes[{index}] contains duplicate commit {sha}",
                )
            note_seen.add(sha)
            referenced.add(sha)
        actual_notes.append({"category": category, "text": text})

    expected_counter = Counter((item["category"], item["text"]) for item in expected_notes)
    actual_counter = Counter((item["category"], item["text"]) for item in actual_notes)
    if actual_counter != expected_counter:
        missing = list((expected_counter - actual_counter).elements())
        extra = list((actual_counter - expected_counter).elements())
        details: list[str] = []
        if missing:
            details.append(f"missing={missing!r}")
        if extra:
            details.append(f"extra={extra!r}")
        _fail(
            "RELEASE_DELTA_NOTES_MISMATCH",
            "manifest release notes do not exactly match the target Changelog section"
            + (f": {'; '.join(details)}" if details else ""),
        )
    return referenced


def _validate_no_release_note(value: Any, *, commit_shas: set[str]) -> set[str]:
    if not isinstance(value, list):
        _fail("MALFORMED_RELEASE_DELTA", "no_release_note must be an array")
    referenced: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail("MALFORMED_RELEASE_DELTA", f"no_release_note[{index}] must be an object")
        _require_exact_keys(
            item,
            {"commit", "reason"},
            context=f"no_release_note[{index}]",
        )
        sha = _full_sha(item["commit"], field=f"no_release_note[{index}].commit")
        reason = str(item["reason"]).strip()
        if not reason:
            _fail(
                "RELEASE_DELTA_REASON_REQUIRED",
                f"no_release_note[{index}] requires a non-empty reason",
            )
        if sha not in commit_shas:
            _fail(
                "RELEASE_DELTA_UNKNOWN_COMMIT",
                f"no-release-note disposition references a commit outside the reviewed delta: {sha}",
            )
        if sha in referenced:
            _fail("MALFORMED_RELEASE_DELTA", f"duplicate no_release_note entry: {sha}")
        referenced.add(sha)
    return referenced


def _validate_reviewed_head_boundary(
    *,
    base: Path,
    version: str,
    reviewed_head: str,
    current_head: str,
    relative_manifest: str,
    run_cmd: RunCommand,
) -> None:
    ancestor = _git_result(
        base,
        ["merge-base", "--is-ancestor", reviewed_head, current_head],
        run_cmd=run_cmd,
    )
    if ancestor.returncode != 0:
        _fail(
            "RELEASE_DELTA_REVIEWED_HEAD_NOT_ANCESTOR",
            f"reviewed_head {reviewed_head} is not an ancestor of current HEAD",
        )
    if current_head == reviewed_head:
        return

    count_text = _git_stdout(
        base,
        ["rev-list", "--count", f"{reviewed_head}..{current_head}"],
        run_cmd=run_cmd,
    )
    if count_text != "1":
        _fail(
            "RELEASE_DELTA_POST_REVIEW_COMMITS",
            "more than the single release-metadata commit exists after reviewed_head",
        )
    parent = _git_stdout(base, ["rev-parse", f"{current_head}^"], run_cmd=run_cmd).lower()
    subject = _git_stdout(
        base,
        ["show", "-s", "--format=%s", current_head],
        run_cmd=run_cmd,
    )
    if parent != reviewed_head or subject != f"chore: release {version}":
        _fail(
            "RELEASE_DELTA_INVALID_RELEASE_COMMIT",
            f"the only commit after reviewed_head must be 'chore: release {version}' with reviewed_head as its parent",
        )

    changed = set(
        _git_lines(
            base,
            ["diff-tree", "--no-commit-id", "--name-only", "-r", current_head],
            run_cmd=run_cmd,
        )
    )
    allowed = RELEASE_METADATA_PATHS | {relative_manifest}
    unexpected = sorted(changed - allowed)
    if unexpected:
        _fail(
            "RELEASE_DELTA_RELEASE_COMMIT_SCOPE",
            f"release metadata commit contains unexpected paths: {unexpected}",
        )


def _resolve_base_tag(
    *,
    base: Path,
    target_version: str,
    reviewed_head: str,
    run_cmd: RunCommand,
) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    for tag in _git_lines(
        base,
        ["tag", "--merged", reviewed_head, "--list", "v*"],
        run_cmd=run_cmd,
    ):
        match = TAG_RE.fullmatch(tag)
        if match is None:
            continue
        version = match.group("version")
        if parse_version(version).prerelease:
            continue
        if version == target_version:
            continue
        candidates.append((version, tag))
    if not candidates:
        _fail(
            "RELEASE_DELTA_BASE_TAG_MISSING",
            f"no stable ancestor tag exists before target version {target_version}",
        )
    candidates.sort(key=cmp_to_key(lambda left, right: compare_versions(left[0], right[0])))
    base_version, base_tag = candidates[-1]
    if compare_versions(base_version, target_version) >= 0:
        _fail(
            "RELEASE_DELTA_TARGET_NOT_AFTER_BASE",
            f"target version {target_version} must be newer than latest stable ancestor {base_version}",
        )
    base_commit = _git_stdout(
        base,
        ["rev-parse", f"{base_tag}^{{commit}}"],
        run_cmd=run_cmd,
    ).lower()
    return base_tag, _full_sha(base_commit, field="resolved base commit")


def _git_commits(
    *,
    base: Path,
    base_commit: str,
    reviewed_head: str,
    run_cmd: RunCommand,
) -> list[dict[str, str]]:
    output = _git_stdout(
        base,
        ["log", "--reverse", "--format=%H%x00%s", f"{base_commit}..{reviewed_head}"],
        run_cmd=run_cmd,
    )
    if not output:
        return []
    commits: list[dict[str, str]] = []
    for line in output.splitlines():
        sha, separator, subject = line.partition("\x00")
        if not separator:
            _fail("RELEASE_DELTA_GIT_ERROR", "git log returned a malformed commit row")
        commits.append(
            {
                "sha": _full_sha(sha.lower(), field="git commit"),
                "subject": subject,
            }
        )
    return commits


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(
            "RELEASE_DELTA_MANIFEST_MISSING",
            f"release delta manifest is missing: {path}",
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(
            "MALFORMED_RELEASE_DELTA",
            f"release delta manifest is not valid UTF-8 JSON: {exc}",
        )
    if not isinstance(value, dict):
        _fail("MALFORMED_RELEASE_DELTA", "release delta manifest must be a JSON object")
    return value


def _relative_manifest_path(*, base: Path, manifest_path: Path) -> str:
    try:
        return manifest_path.relative_to(base).as_posix()
    except ValueError:
        _fail(
            "RELEASE_DELTA_MANIFEST_OUTSIDE_REPO",
            "release delta manifest must be stored inside the repository",
        )


def _validated_version(value: Any) -> str:
    version = str(value or "").strip()
    if not VERSION_RE.fullmatch(version):
        _fail("INVALID_RELEASE_VERSION", f"invalid release version: {version}")
    return version


def _full_sha(value: Any, *, field: str) -> str:
    sha = str(value or "").strip().lower()
    if not FULL_SHA_RE.fullmatch(sha):
        _fail("MALFORMED_RELEASE_DELTA", f"{field} must be a full 40-character commit SHA")
    return sha


def _require_exact_keys(value: dict[str, Any], expected: set[str], *, context: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing={missing}")
    if extra:
        details.append(f"extra={extra}")
    _fail(
        "MALFORMED_RELEASE_DELTA",
        f"{context} has invalid fields: {', '.join(details)}",
    )


def _git_lines(
    base: Path,
    args: list[str],
    *,
    run_cmd: RunCommand,
) -> list[str]:
    output = _git_stdout(base, args, run_cmd=run_cmd)
    return [line.strip() for line in output.splitlines() if line.strip()]


def _git_stdout(
    base: Path,
    args: list[str],
    *,
    run_cmd: RunCommand,
) -> str:
    result = _git_result(base, args, run_cmd=run_cmd)
    if result.returncode != 0:
        stderr = str(result.stderr or "").strip()
        _fail(
            "RELEASE_DELTA_GIT_ERROR",
            stderr or f"git {' '.join(args)} failed with exit code {result.returncode}",
        )
    return str(result.stdout or "").strip()


def _git_result(
    base: Path,
    args: list[str],
    *,
    run_cmd: RunCommand,
) -> Any:
    try:
        return run_cmd(
            ["git", *args],
            cwd=base,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        _fail("RELEASE_DELTA_GIT_ERROR", f"unable to execute git: {exc}")


def _short_shas(values: list[str]) -> str:
    return ", ".join(sha[:12] for sha in values)


def _fail(reason_code: str, message: str) -> None:
    raise ReleaseDeltaCoverageError(reason_code, message)


__all__ = [
    "SCHEMA_VERSION",
    "ReleaseDeltaCoverageError",
    "build_release_delta_manifest",
    "default_manifest_path",
    "validate_release_delta_coverage",
]
