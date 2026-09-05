#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.application.release_delta_coverage import (
    ReleaseDeltaCoverageError,
    validate_release_delta_coverage,
)
from src.application.release_notes import parse_version_categories, render_release_notes
from src.application.release_target import VERSION_RE


RELEASE_LOCK = Path("constraints/release.txt")
REQUIREMENT_ROOTS = (Path("requirements/dev.txt"), Path("requirements/server.txt"))
COMPATIBILITY_INPUTS = {
    Path("requirements.txt"): "-r requirements/runtime.txt",
    Path("constraints.txt"): "-c constraints/release.txt",
    Path("constraints/runtime.txt"): "-c release.txt",
    Path("constraints/dev.txt"): "-c release.txt",
    Path("constraints/server.txt"): "-c release.txt",
}
BOOTSTRAP_PACKAGES = frozenset({"pip", "setuptools", "wheel"})


class DependencyLockError(ValueError):
    pass


def repo_base() -> Path:
    return BASE_DIR


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def current_version(base: Path) -> str:
    version = read_text((base / "VERSION").resolve()).strip()
    if not VERSION_RE.match(version):
        raise SystemExit(f"[RELEASE_ERROR] invalid VERSION format: {version}")
    return version


def changelog_section(changelog_text: str, version: str) -> str:
    parsed = parse_version_categories(changelog_text, version, allow_legacy=True)
    if parsed["status"] != "ok":
        return ""
    return "\n".join([parsed["section_heading"], parsed["canonical_text"]]).strip()


def _content_lines(path: Path) -> list[str]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DependencyLockError(f"cannot read {path}: {exc}") from exc
    return [line.strip() for line in raw_lines if line.strip() and not line.lstrip().startswith("#")]


def _parse_requirement(line: str, *, source: Path) -> Requirement:
    try:
        return Requirement(line)
    except InvalidRequirement as exc:
        raise DependencyLockError(f"invalid requirement in {source}: {line}") from exc


def _locked_version(requirement: Requirement, *, source: Path) -> Version:
    specifiers = tuple(requirement.specifier)
    if (
        requirement.url is not None
        or len(specifiers) != 1
        or specifiers[0].operator != "=="
        or "*" in specifiers[0].version
    ):
        raise DependencyLockError(
            f"release lock entries must use one exact == version in {source}: {requirement}"
        )
    try:
        return Version(specifiers[0].version)
    except InvalidVersion as exc:
        raise DependencyLockError(f"invalid locked version in {source}: {requirement}") from exc


def _load_release_lock(base: Path) -> dict[str, list[tuple[Requirement, Version]]]:
    path = base / RELEASE_LOCK
    locked: dict[str, list[tuple[Requirement, Version]]] = {}
    for line in _content_lines(path):
        if line.startswith("-"):
            raise DependencyLockError(f"release lock cannot contain pip options or includes: {line}")
        requirement = _parse_requirement(line, source=path)
        version = _locked_version(requirement, source=path)
        locked.setdefault(canonicalize_name(requirement.name), []).append((requirement, version))
    if not locked:
        raise DependencyLockError(f"release lock is empty: {path}")
    return locked


def _load_requirement_intent(path: Path, *, seen: set[Path] | None = None) -> list[Requirement]:
    resolved = path.resolve()
    visited = seen if seen is not None else set()
    if resolved in visited:
        return []
    visited.add(resolved)
    requirements: list[Requirement] = []
    for line in _content_lines(resolved):
        parts = line.split(maxsplit=1)
        if parts[0] in {"-r", "--requirement"} and len(parts) == 2:
            requirements.extend(_load_requirement_intent(resolved.parent / parts[1], seen=visited))
            continue
        if line.startswith("-"):
            raise DependencyLockError(f"unsupported requirement option in {resolved}: {line}")
        requirement = _parse_requirement(line, source=resolved)
        if requirement.url is not None:
            raise DependencyLockError(f"release requirements cannot use URL dependencies: {requirement}")
        requirements.append(requirement)
    return requirements


def _validate_compatibility_inputs(base: Path) -> None:
    for relative_path, expected in COMPATIBILITY_INPUTS.items():
        path = base / relative_path
        if _content_lines(path) != [expected]:
            raise DependencyLockError(f"{relative_path} must contain only: {expected}")


def _active_lock(
    locked: Mapping[str, list[tuple[Requirement, Version]]],
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Version]:
    marker_environment = dict(environment or default_environment())
    active: dict[str, Version] = {}
    for name, entries in locked.items():
        matching = [
            version
            for requirement, version in entries
            if requirement.marker is None or requirement.marker.evaluate(environment=marker_environment)
        ]
        if len(matching) > 1:
            raise DependencyLockError(f"multiple release lock entries apply to {name}")
        if matching:
            active[name] = matching[0]
    return active


def validate_dependency_lock(base: Path) -> dict[str, list[tuple[Requirement, Version]]]:
    _validate_compatibility_inputs(base)
    locked = _load_release_lock(base)
    _active_lock(locked)
    direct_requirements = [
        requirement
        for root in REQUIREMENT_ROOTS
        for requirement in _load_requirement_intent(base / root)
    ]
    for requirement in direct_requirements:
        name = canonicalize_name(requirement.name)
        versions = [version for _entry, version in locked.get(name, ())]
        if not versions:
            raise DependencyLockError(f"direct requirement is missing from release lock: {requirement.name}")
        if requirement.specifier and not any(version in requirement.specifier for version in versions):
            raise DependencyLockError(
                f"release lock version does not satisfy direct requirement: {requirement}"
            )
    return locked


def validate_installed_closure(
    locked: Mapping[str, list[tuple[Requirement, Version]]],
    *,
    distributions: Iterable[Any] | None = None,
    run_cmd: Any = subprocess.run,
) -> None:
    expected = _active_lock(locked)
    installed: dict[str, Version] = {}
    for distribution in distributions if distributions is not None else importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = canonicalize_name(raw_name)
        try:
            version = Version(distribution.version)
        except InvalidVersion as exc:
            raise DependencyLockError(f"installed package has an invalid version: {raw_name}") from exc
        if name in installed:
            raise DependencyLockError(f"installed package appears more than once: {name}")
        installed[name] = version

    missing = sorted(set(expected) - set(installed))
    extra = sorted(set(installed) - set(expected) - BOOTSTRAP_PACKAGES)
    mismatched = sorted(
        name for name in expected.keys() & installed.keys() if expected[name] != installed[name]
    )
    if missing or extra or mismatched:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        if mismatched:
            details.append(
                "mismatched="
                + ",".join(f"{name}:{installed[name]}!={expected[name]}" for name in mismatched)
            )
        raise DependencyLockError("installed environment differs from release lock: " + "; ".join(details))

    proc = run_cmd(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    if int(getattr(proc, "returncode", 1)) != 0:
        output = str(getattr(proc, "stdout", "") or getattr(proc, "stderr", "") or "dependency conflict")
        raise DependencyLockError("pip check failed: " + output.strip()[:500])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="validate release metadata and optionally render release notes")
    parser.add_argument("--tag", default=None, help="optional git tag such as v0.1.0-beta.1")
    parser.add_argument("--render-notes-out", default=None, help="optional output markdown path for release notes")
    parser.add_argument(
        "--require-current-taxonomy",
        action="store_true",
        help="reject legacy Added/Changed/Fixed headings in the target release section",
    )
    parser.add_argument(
        "--require-delta-coverage",
        action="store_true",
        help="require an auditable previous-tag-to-release commit coverage manifest",
    )
    parser.add_argument(
        "--delta-coverage-file",
        default=None,
        help="optional release delta manifest path; requires --require-delta-coverage",
    )
    parser.add_argument(
        "--dependency-lock-only",
        action="store_true",
        help="validate the release lock and the complete installed environment, then exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base = repo_base()
    try:
        locked = validate_dependency_lock(base)
        if args.dependency_lock_only:
            validate_installed_closure(locked)
    except DependencyLockError as exc:
        raise SystemExit(f"[RELEASE_ERROR] {exc}") from exc
    if args.dependency_lock_only:
        print("[OK] release dependency lock and installed environment valid")
        return 0
    version = current_version(base)
    tag = str(args.tag or "").strip()
    if tag:
        tag_version = tag[1:] if tag.startswith("v") else tag
        if tag_version != version:
            raise SystemExit(f"[RELEASE_ERROR] tag {tag} does not match VERSION {version}")

    changelog_path = (base / "CHANGELOG.md").resolve()
    parsed = parse_version_categories(
        read_text(changelog_path),
        version,
        allow_legacy=not args.require_current_taxonomy,
    )
    if parsed["status"] != "ok":
        raise SystemExit(f"[RELEASE_ERROR] {parsed['reason_code']}: {parsed['message']}")

    if args.delta_coverage_file and not args.require_delta_coverage:
        raise SystemExit("[RELEASE_ERROR] --delta-coverage-file requires --require-delta-coverage")
    coverage_summary = None
    if args.require_delta_coverage:
        try:
            coverage_summary = validate_release_delta_coverage(
                base_dir=base,
                version=version,
                release_evidence=parsed["evidence"],
                manifest_path=(
                    Path(args.delta_coverage_file).expanduser().resolve()
                    if args.delta_coverage_file
                    else None
                ),
            )
        except ReleaseDeltaCoverageError as exc:
            raise SystemExit(f"[RELEASE_ERROR] {exc.reason_code}: {exc.message}") from exc

    if args.render_notes_out:
        out_path = Path(args.render_notes_out).expanduser().resolve()
        out_path.write_text(render_release_notes(version=version, evidence=parsed["evidence"]), encoding="utf-8")

    if coverage_summary is not None:
        print(
            "[OK] release delta coverage valid "
            f"from {coverage_summary['base_tag']}: "
            f"{coverage_summary['commit_count']} commits, "
            f"{coverage_summary['release_note_count']} notes, "
            f"{coverage_summary['no_release_note_count']} no-note dispositions, "
            f"{coverage_summary['design_evidence_count']} design dispositions",
        )
    print(f"[OK] release metadata valid for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
