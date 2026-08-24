from __future__ import annotations

from pathlib import Path
import subprocess

from src.application.source_identity import source_commit_sha


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_release_identity_compares_with_its_tag_not_cache_head(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    original = {
        "VERSION": "1.0.0\n",
        "domain/value.py": "VALUE = 1\n",
        "src/value.py": "VALUE = 1\n",
        "scripts/value.py": "VALUE = 1\n",
    }
    for relative, content in original.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "release")
    release_commit = _git(source, "rev-parse", "HEAD")
    _git(source, "tag", "v1.0.0")
    (source / "src/value.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(source, "commit", "-am", "advance cache head")

    apps = tmp_path / "apps"
    cache = apps / "_cache/git/options-monitor.git"
    cache.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "--bare", str(source), str(cache))
    release = apps / "releases/1.0.0"
    for relative, content in original.items():
        path = release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    assert source_commit_sha(release) == release_commit
    (release / "src/value.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert source_commit_sha(release) is None
