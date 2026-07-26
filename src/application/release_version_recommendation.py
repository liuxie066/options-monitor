from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from src.application.release_target import (
    VERSION_RE,
    RemoteStableTagIdentity,
    bump_version,
    parse_remote_stable_tag_identities,
    parse_version,
)
from src.application.release_notes import parse_unreleased_categories

SCHEMA_VERSION = "release_version_recommendation.v1"
DIGEST_SCHEMA_VERSION = "release_version_recommendation.digest.v1"
SENSITIVE_PATH_PATTERNS = (
    "src/interfaces/**",
    "src/application/agent_tools/**",
    "src/application/config*.py",
    "src/application/layered_config.py",
    "domain/domain/ledger/**",
    "src/application/positions/**",
    "src/application/trades/**",
    "src/application/service_upgrade.py",
    "src/application/service_deploy.py",
    "scripts/install.sh",
    "scripts/python_runtime.sh",
    "src/__init__.py",
    ".github/workflows/**",
)

RunCommand = Callable[..., Any]


class RecommendationFailure(Exception):
    def __init__(self, *, status: str, reason_code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.reason_code = reason_code
        self.message = message
        self.details = dict(details or {})


def recommend_release_version(
    *,
    base_dir: Path,
    remote_name: str = "origin",
    run_cmd: RunCommand = subprocess.run,
    max_untracked_files: int = 256,
    max_untracked_file_bytes: int = 1024 * 1024,
    max_untracked_total_bytes: int = 10 * 1024 * 1024,
) -> dict[str, Any]:
    base = base_dir.resolve()
    base_data: dict[str, Any] | None = None
    workspace: dict[str, Any] | None = None
    evidence = _empty_evidence()
    try:
        current_version = _read_current_version(base)
        if parse_version(current_version).prerelease:
            raise RecommendationFailure(
                status="blocked",
                reason_code="UNSUPPORTED_PRERELEASE_VERSION",
                message="automatic recommendation requires a stable current VERSION",
            )

        remote = _resolve_remote(base=base, remote_name=remote_name, run_cmd=run_cmd)
        identities = _remote_tag_identities(
            base=base,
            remote_name=remote["name"],
            raw_url=remote["_raw_url"],
            display_url=remote["display"],
            run_cmd=run_cmd,
        )
        if not identities:
            raise RecommendationFailure(
                status="blocked",
                reason_code="NO_STABLE_RELEASE_TAG",
                message="no canonical stable release tag was found on the configured remote",
            )
        latest = identities[-1]
        local_commit = _local_tag_commit(base=base, identity=latest, run_cmd=run_cmd)
        head = _git_stdout(
            base,
            ["git", "rev-parse", "HEAD"],
            run_cmd=run_cmd,
            reason="BASE_TAG_NOT_AVAILABLE_LOCALLY",
        ).strip().lower()
        ancestor = _run_git(base, ["git", "merge-base", "--is-ancestor", local_commit, head], run_cmd=run_cmd)
        if ancestor["returncode"] != 0:
            raise RecommendationFailure(
                status="blocked",
                reason_code="BASE_TAG_NOT_ANCESTOR",
                message=f"remote baseline {latest.tag} is not an ancestor of current HEAD",
            )
        if current_version != latest.version:
            raise RecommendationFailure(
                status="blocked",
                reason_code="VERSION_BASE_MISMATCH",
                message=f"current VERSION {current_version} does not match remote baseline {latest.version}",
            )

        base_data = {
            "remote_name": remote["name"],
            "remote_endpoint_display": remote["display"],
            "remote_endpoint_fingerprint": remote["fingerprint"],
            "tag": latest.tag,
            "version": latest.version,
            "remote_tag_object_sha": latest.remote_tag_object_sha,
            "remote_commit_sha": latest.remote_commit_sha,
            "local_commit_sha": local_commit,
        }
        workspace, untracked_evidence = _collect_workspace_evidence(
            base=base,
            baseline_commit=local_commit,
            head=head,
            run_cmd=run_cmd,
            max_untracked_files=max_untracked_files,
            max_untracked_file_bytes=max_untracked_file_bytes,
            max_untracked_total_bytes=max_untracked_total_bytes,
        )
        parsed = parse_unreleased((base / "CHANGELOG.md").read_text(encoding="utf-8"))
        evidence.update(parsed["evidence"])
        if parsed["status"] != "ok":
            raise RecommendationFailure(
                status="needs_input",
                reason_code=parsed["reason_code"],
                message=parsed["message"],
                details={"evidence": evidence, "workspace": workspace, "base": base_data},
            )

        bump = _classify(evidence)
        target_version = bump_version(latest.version, bump)
        if any(identity.version == target_version for identity in identities):
            raise RecommendationFailure(
                status="blocked",
                reason_code="TARGET_TAG_ALREADY_EXISTS",
                message=f"target tag v{target_version} already exists on remote",
            )

        sensitive_paths = sorted(path for path in workspace["changed_files"] if _is_sensitive_path(path))
        evidence["sensitive_paths"] = sensitive_paths
        review_flags: list[str] = []
        if sensitive_paths:
            review_flags.append("COMPATIBILITY_SENSITIVE_PATH_CHANGED")
        if workspace["detached"]:
            review_flags.append("DETACHED_HEAD")

        recommendation = {
            "bump": bump,
            "target_version": target_version,
            "classification_basis": "changelog_unreleased",
            "declaration_status": "complete",
            "manual_review_required": True,
        }
        digest_payload = {
            "schema_version": DIGEST_SCHEMA_VERSION,
            "remote_name": remote["name"],
            "remote_endpoint_fingerprint": remote["fingerprint"],
            "remote_stable_tag": latest.tag,
            "remote_stable_version": latest.version,
            "remote_tag_object_sha": latest.remote_tag_object_sha,
            "remote_commit_sha": latest.remote_commit_sha,
            "local_commit_sha": local_commit,
            "head": head,
            "current_version": current_version,
            "tracked_content_digest": workspace["tracked_content_digest"],
            "untracked_files": untracked_evidence,
            "unreleased": parsed["canonical_text"],
            "recommended_bump": bump,
            "target_version": target_version,
        }
        recommendation_digest = _json_digest(digest_payload)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "recommended",
            "mode": "dry_run",
            "reason_code": None,
            "message": f"recommended {bump} version bump: {latest.version} -> {target_version}",
            "base": base_data,
            "workspace": workspace,
            "recommendation": recommendation,
            "evidence": evidence,
            "review_flags": review_flags,
            "recommendation_digest": recommendation_digest,
            "write": {"changed": False, "already_at_target": False},
        }
    except RecommendationFailure as exc:
        details = dict(exc.details)
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": exc.status,
            "mode": "dry_run",
            "reason_code": exc.reason_code,
            "message": exc.message,
            "review_flags": [],
            "write": {"changed": False, "already_at_target": False},
        }
        if base_data is not None:
            result["base"] = base_data
        if workspace is not None:
            result["workspace"] = workspace
        if any(evidence.values()):
            result["evidence"] = evidence
        result.update(details)
        return result
    except FileNotFoundError as exc:
        return _blocked("MALFORMED_UNRELEASED_SECTION", f"required release metadata file is missing: {exc.filename}")
    except UnicodeDecodeError:
        return _blocked("UNSUPPORTED_UNRELEASED_CONTENT", "CHANGELOG.md must be valid UTF-8 text")


def parse_unreleased(text: str) -> dict[str, Any]:
    parsed = parse_unreleased_categories(text)
    evidence = _empty_evidence()
    evidence.update(parsed["evidence"])
    if parsed["status"] != "ok":
        return {
            "status": "needs_input",
            "reason_code": parsed["reason_code"],
            "message": parsed["message"],
            "canonical_text": parsed["canonical_text"],
            "evidence": evidence,
        }
    return {
        "status": "ok",
        "reason_code": None,
        "message": "release intent parsed",
        "canonical_text": parsed["canonical_text"],
        "evidence": evidence,
    }


def recommendation_warnings(data: dict[str, Any]) -> list[str]:
    flags = set(data.get("review_flags") or [])
    warnings: list[str] = []
    if "COMPATIBILITY_SENSITIVE_PATH_CHANGED" in flags:
        warnings.append("compatibility-sensitive files changed; confirm Unreleased impact classification")
    if "DETACHED_HEAD" in flags:
        warnings.append("repository is on a detached HEAD; choose the intended release branch before commit/push")
    if data.get("status") == "recommended":
        warnings.append("recommendation only; confirm before writing VERSION")
    return warnings


def _resolve_remote(*, base: Path, remote_name: str, run_cmd: RunCommand) -> dict[str, str]:
    name = str(remote_name or "origin").strip()
    remotes = _git_stdout(base, ["git", "remote"], run_cmd=run_cmd, reason="REMOTE_NOT_CONFIGURED").splitlines()
    if name not in {item.strip() for item in remotes}:
        raise RecommendationFailure(
            status="blocked",
            reason_code="REMOTE_NOT_CONFIGURED",
            message=f"git remote is not configured: {name}",
        )
    raw_url = _git_stdout(
        base,
        ["git", "remote", "get-url", name],
        run_cmd=run_cmd,
        reason="REMOTE_ENDPOINT_LOOKUP_FAILED",
    ).strip()
    if not raw_url:
        raise RecommendationFailure(
            status="blocked",
            reason_code="REMOTE_ENDPOINT_LOOKUP_FAILED",
            message=f"git remote has no fetch URL: {name}",
        )
    return {
        "name": name,
        "display": _redact_remote_url(raw_url),
        "fingerprint": _sha256_text(raw_url),
        "_raw_url": raw_url,
    }


def _remote_tag_identities(
    *,
    base: Path,
    remote_name: str,
    raw_url: str,
    display_url: str,
    run_cmd: RunCommand,
) -> list[RemoteStableTagIdentity]:
    try:
        stdout = _git_stdout(
            base,
            ["git", "ls-remote", "--tags", remote_name],
            run_cmd=run_cmd,
            reason="REMOTE_TAG_LOOKUP_FAILED",
        )
    except RecommendationFailure as exc:
        raise RecommendationFailure(
            status=exc.status,
            reason_code=exc.reason_code,
            message=exc.message.replace(raw_url, display_url),
            details=exc.details,
        ) from exc
    try:
        return parse_remote_stable_tag_identities(stdout)
    except ValueError as exc:
        raise RecommendationFailure(
            status="blocked",
            reason_code="REMOTE_TAG_IDENTITY_INVALID",
            message=str(exc),
        ) from exc


def _local_tag_commit(*, base: Path, identity: RemoteStableTagIdentity, run_cmd: RunCommand) -> str:
    result = _run_git(base, ["git", "rev-parse", f"refs/tags/{identity.tag}^{{commit}}"], run_cmd=run_cmd)
    if result["returncode"] != 0:
        raise RecommendationFailure(
            status="blocked",
            reason_code="BASE_TAG_NOT_AVAILABLE_LOCALLY",
            message=f"remote baseline tag is not available locally: {identity.tag}",
        )
    local_commit = result["stdout"].strip().lower()
    if local_commit != identity.remote_commit_sha.lower():
        raise RecommendationFailure(
            status="blocked",
            reason_code="REMOTE_LOCAL_TAG_MISMATCH",
            message=f"local tag {identity.tag} does not resolve to the remote commit",
        )
    return local_commit


def _collect_workspace_evidence(
    *,
    base: Path,
    baseline_commit: str,
    head: str,
    run_cmd: RunCommand,
    max_untracked_files: int,
    max_untracked_file_bytes: int,
    max_untracked_total_bytes: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    branch_result = _run_git(base, ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], run_cmd=run_cmd)
    branch = branch_result["stdout"].strip() if branch_result["returncode"] == 0 else None
    name_status = _git_stdout(
        base,
        ["git", "diff", "--name-status", "-M", baseline_commit, "--"],
        run_cmd=run_cmd,
        reason="BASE_TAG_NOT_AVAILABLE_LOCALLY",
    )
    changed_files = _paths_from_name_status(name_status)
    tracked_diff = _git_stdout(
        base,
        ["git", "diff", "--no-ext-diff", "--binary", baseline_commit, "--", ".", ":(exclude)VERSION"],
        run_cmd=run_cmd,
        reason="BASE_TAG_NOT_AVAILABLE_LOCALLY",
    )
    staged_files = _git_name_list(base, ["git", "diff", "--name-only", "--cached"], run_cmd=run_cmd)
    unstaged_files = _git_name_list(base, ["git", "diff", "--name-only"], run_cmd=run_cmd)
    untracked_raw = _git_stdout(
        base,
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        run_cmd=run_cmd,
        reason="BASE_TAG_NOT_AVAILABLE_LOCALLY",
    )
    untracked_paths = sorted(path for path in untracked_raw.split("\0") if path)
    untracked_evidence = _hash_untracked_files(
        base=base,
        paths=untracked_paths,
        max_files=max_untracked_files,
        max_file_bytes=max_untracked_file_bytes,
        max_total_bytes=max_untracked_total_bytes,
    )
    all_changed = sorted(set(changed_files) | set(untracked_paths))
    return (
        {
            "head": head,
            "branch": branch,
            "detached": branch is None,
            "dirty": bool(all_changed),
            "changed_files": all_changed,
            "tracked_content_digest": _sha256_text(tracked_diff),
            "staged_files": staged_files,
            "unstaged_files": unstaged_files,
        },
        untracked_evidence,
    )


def _hash_untracked_files(
    *,
    base: Path,
    paths: list[str],
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[dict[str, Any]]:
    if len(paths) > max_files:
        raise RecommendationFailure(
            status="blocked",
            reason_code="EVIDENCE_LIMIT_EXCEEDED",
            message=f"untracked file count exceeds evidence limit ({max_files})",
        )
    root = base.resolve()
    total = 0
    out: list[dict[str, Any]] = []
    for rel in paths:
        path = base / rel
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RecommendationFailure(
                status="blocked",
                reason_code="EVIDENCE_UNSUPPORTED_FILE_TYPE",
                message=f"cannot inspect untracked evidence file {rel}: {exc}",
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise RecommendationFailure(
                status="blocked",
                reason_code="EVIDENCE_UNSUPPORTED_FILE_TYPE",
                message=f"untracked evidence is not a regular file: {rel}",
            )
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RecommendationFailure(
                status="blocked",
                reason_code="EVIDENCE_UNSUPPORTED_FILE_TYPE",
                message=f"untracked evidence escapes repository root: {rel}",
            ) from exc
        if metadata.st_size > max_file_bytes or total + metadata.st_size > max_total_bytes:
            raise RecommendationFailure(
                status="blocked",
                reason_code="EVIDENCE_LIMIT_EXCEEDED",
                message=f"untracked evidence exceeds content limit: {rel}",
            )
        content = path.read_bytes()
        total += len(content)
        out.append({"path": rel, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    return out


def _run_git(base: Path, command: list[str], *, run_cmd: RunCommand) -> dict[str, Any]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = run_cmd(
            command,
            cwd=str(base),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
    except Exception as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": int(getattr(proc, "returncode", 1)),
        "stdout": str(getattr(proc, "stdout", "") or ""),
        "stderr": str(getattr(proc, "stderr", "") or ""),
    }


def _git_stdout(base: Path, command: list[str], *, run_cmd: RunCommand, reason: str) -> str:
    result = _run_git(base, command, run_cmd=run_cmd)
    if result["returncode"] != 0:
        message = result["stderr"].strip() or result["stdout"].strip() or "git command failed"
        raise RecommendationFailure(status="blocked", reason_code=reason, message=message)
    return result["stdout"]


def _git_name_list(base: Path, command: list[str], *, run_cmd: RunCommand) -> list[str]:
    return sorted(path.strip() for path in _git_stdout(base, command, run_cmd=run_cmd, reason="BASE_TAG_NOT_AVAILABLE_LOCALLY").splitlines() if path.strip())


def _paths_from_name_status(stdout: str) -> list[str]:
    paths: set[str] = set()
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        for path in parts[1:]:
            if path:
                paths.add(path)
    return sorted(paths)


def _read_current_version(base: Path) -> str:
    value = (base / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_RE.match(value):
        raise RecommendationFailure(
            status="blocked",
            reason_code="VERSION_BASE_MISMATCH",
            message=f"invalid current VERSION: {value}",
        )
    return value


def _classify(evidence: dict[str, Any]) -> str:
    if evidence["breaking_changes"]:
        return "major"
    if evidence["added"]:
        return "minor"
    return "patch"


def _empty_evidence() -> dict[str, list[str]]:
    return {
        "breaking_changes": [],
        "added": [],
        "changed": [],
        "fixed": [],
        "unsupported": [],
        "sensitive_paths": [],
    }


def _is_sensitive_path(path: str) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(path, pattern) for pattern in SENSITIVE_PATH_PATTERNS)


def _json_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _redact_remote_url(raw_url: str) -> str:
    value = raw_url.strip()
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or "remote"
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path or ""
        return urlunsplit((parsed.scheme, f"{host}{port}", path, "", ""))
    if "@" in value and ":" in value.split("@", 1)[1]:
        host_path = value.split("@", 1)[1]
        host, path = host_path.split(":", 1)
        return f"{host}:{path}"
    path = Path(value)
    return f"<local>/{path.name}" if path.name else "<local>"


def _blocked(reason_code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "mode": "dry_run",
        "reason_code": reason_code,
        "message": message,
        "review_flags": [],
        "write": {"changed": False, "already_at_target": False},
    }


__all__ = [
    "DIGEST_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "RecommendationFailure",
    "parse_unreleased",
    "recommend_release_version",
    "recommendation_warnings",
]
