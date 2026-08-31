from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable


RunCommand = Callable[..., Any]


def source_commit_sha(
    root: Path | None = None,
    *,
    run_cmd: RunCommand | None = None,
) -> str | None:
    """Resolve the clean production source commit for a worktree or release."""

    root = (root or Path(__file__).resolve().parents[2]).resolve()
    runner = run_cmd or subprocess.run
    git_prefix = ["git"]
    git_env: dict[str, str] | None = None
    commit_ref = "HEAD"
    temp_index: tempfile.TemporaryDirectory[str] | None = None
    if not (root / ".git").exists():
        configured_cache = str(os.environ.get("OM_UPGRADE_CACHE_ROOT") or "").strip()
        cache_root = (
            Path(configured_cache).expanduser()
            if configured_cache
            else root.parent.parent / "_cache"
        )
        cache_repo = cache_root / "git" / "options-monitor.git"
        try:
            version = (root / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if root.parent.name != "releases" or not cache_repo.is_dir() or not version:
            return None
        git_prefix = [
            "git",
            f"--git-dir={cache_repo}",
            f"--work-tree={root}",
        ]
        commit_ref = f"refs/tags/v{version}^{{commit}}"
        temp_index = tempfile.TemporaryDirectory(prefix="om-source-identity-")
        git_env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp_index.name) / "index")}
    try:
        result = runner(
            [*git_prefix, "rev-parse", "--verify", commit_ref],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=git_env,
        )
        commit = result.stdout.strip()
        if temp_index is not None:
            runner(
                [*git_prefix, "read-tree", commit],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
                env=git_env,
            )
            runner(
                [*git_prefix, "update-index", "-q", "--refresh"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
                env=git_env,
            )
            runner(
                [
                    *git_prefix,
                    "diff-index",
                    "--quiet",
                    commit,
                    "--",
                    "domain",
                    "src",
                    "scripts",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
                env=git_env,
            )
            untracked = runner(
                [
                    *git_prefix,
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "--",
                    "domain",
                    "src",
                    "scripts",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
                env=git_env,
            )
            if untracked.stdout.strip():
                return None
        else:
            source_status = runner(
                [
                    *git_prefix,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--",
                    "domain",
                    "src",
                    "scripts",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
                env=git_env,
            )
            if source_status.stdout.strip():
                return None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if temp_index is not None:
            temp_index.cleanup()
    return commit or None
