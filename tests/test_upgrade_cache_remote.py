from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest

from tests.service_deploy_test_support import _mock_published_releases

from src.application.service_upgrade import (
    _materialize_release_from_git_cache,
    _resolve_upgrade_remote_url,
    service_upgrade_check,
)


SOURCE_URL = "https://github.com/cache-fixture/options-monitor.git"
SSH_SOURCE_URL = "git@github.com:cache-fixture/options-monitor.git"


@pytest.fixture
def published_releases(monkeypatch):
    return _mock_published_releases(
        monkeypatch, tags=("v1.0.0", "v1.0.1", "v1.0.2"), repository="cache-fixture/options-monitor",
    )


@pytest.fixture
def git_fixture(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    config = tmp_path / "gitconfig"
    config.write_text("[clone]\n\tdefaultRemoteName = alternate\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _publish(source, "1.0.0")
    # Real Git transport stays on disk; stored remote URLs retain GitHub identity.
    for url in (SOURCE_URL, SSH_SOURCE_URL):
        _git(tmp_path, "config", "--global", "--add", f"url.{source}.insteadOf", url)
    return source


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _publish(source: Path, version: str) -> None:
    (source / "VERSION").write_text(version + "\n")
    _git(source, "add", "VERSION")
    _git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-m", version)
    _git(source, "tag", "v" + version)


@pytest.mark.parametrize("source_url", [SOURCE_URL, SSH_SOURCE_URL])
@pytest.mark.parametrize("remote_name", ["upstream", "origin"])
def test_custom_checkout_remote_survives_refresh_archive_and_gitless_check(
    git_fixture, tmp_path, remote_name, source_url, published_releases,
):
    source = git_fixture
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "--origin", remote_name, source_url, str(checkout))
    runtime, cache = tmp_path / "runtime", tmp_path / "cache"

    def check(root):
        out = service_upgrade_check(
            repo_root=root, runtime_root=runtime, cache_root=cache, remote_name=remote_name,
        )
        assert out["ok"], out
        assert out["remote_name"] == remote_name
        return out["latest_version"]

    assert check(checkout) == "1.0.0"
    mirror = cache / "git/options-monitor.git"
    assert _git(mirror, "remote") == "origin"
    _publish(source, "1.0.1")
    assert check(checkout) == "1.0.1"
    release = tmp_path / "releases/1.0.1"
    _materialize_release_from_git_cache(
        remote_url=source_url, tag="v1.0.1", target_dir=release,
        cache_root=cache, run_cmd=subprocess.run, operations=[],
    )
    assert (release / "VERSION").read_text() == "1.0.1\n"
    assert not (release / ".git").exists()
    calls = []

    def run(command, **kwargs):
        result = subprocess.run(command, **kwargs)
        calls.append((command, result.returncode))
        return result

    assert _resolve_upgrade_remote_url(
        repo_root=release, cache_root=cache, remote_name=remote_name, run_cmd=run,
    ) == source_url
    assert calls[0][1] != 0
    assert calls[-1][0][-1] == "remote.origin.url"
    _publish(source, "1.0.2")
    assert check(release) == "1.0.2"
    assert _git(mirror, "config", "--get", "remote.origin.url") == source_url
    _, http_calls = published_releases
    assert {url for url, _ in http_calls if "/releases/tags/" in url} == {
        f"https://api.github.com/repos/cache-fixture/options-monitor/releases/tags/v{version}"
        for version in ("1.0.0", "1.0.1", "1.0.2")
    }


def test_materializer_initializes_origin_despite_clone_default(git_fixture, tmp_path):
    cache, release = tmp_path / "cache", tmp_path / "releases/1.0.0"
    _materialize_release_from_git_cache(
        remote_url=str(git_fixture), tag="v1.0.0", target_dir=release,
        cache_root=cache, run_cmd=subprocess.run, operations=[],
    )
    assert _git(cache / "git/options-monitor.git", "remote") == "origin"
    assert (release / "VERSION").read_text() == "1.0.0\n"


@pytest.mark.parametrize("origin_state", ["missing", "empty"])
def test_missing_origin_refuses_decoy_before_fetch_or_archive(git_fixture, tmp_path, origin_state):
    source = git_fixture
    cache = tmp_path / "cache"
    mirror = cache / "git/options-monitor.git"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "--mirror", "--origin", "upstream", str(source), str(mirror))
    if origin_state == "empty":
        _git(mirror, "config", "remote.origin.url", " ")
    decoy = tmp_path / "origin"
    _git(tmp_path, "clone", str(source), str(decoy))
    _publish(decoy, "9.0.0")
    before = (mirror / "config").read_bytes()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.run(command, **kwargs)

    out = service_upgrade_check(
        repo_root=source, runtime_root=tmp_path / "runtime", cache_root=cache,
        remote_name="upstream", run_cmd=run,
    )
    assert not out["ok"]
    assert "origin" in out["version_check"]["error"]
    release = tmp_path / "releases/1.0.0"
    with pytest.raises(RuntimeError, match="origin"):
        _materialize_release_from_git_cache(
            remote_url=str(source), tag="v1.0.0", target_dir=release,
            cache_root=cache, run_cmd=run, operations=[],
        )
    assert not any("fetch" in c or "archive" in c or "for-each-ref" in c for c in calls)
    assert _git(mirror, "tag") == "v1.0.0"
    assert not release.exists()
    assert (mirror / "config").read_bytes() == before


def test_cache_uses_origin_when_conflicting_upstream_exists(git_fixture, tmp_path, published_releases):
    source = git_fixture
    cache, mirror = tmp_path / "cache", tmp_path / "cache/git/options-monitor.git"
    mirror.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "--mirror", "--origin", "origin", SOURCE_URL, str(mirror))
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(source), str(other))
    _publish(other, "9.0.0")
    _git(mirror, "remote", "add", "upstream", str(other))
    _publish(source, "1.0.1")
    out = service_upgrade_check(
        repo_root=source, runtime_root=tmp_path / "runtime", cache_root=cache,
        remote_name="upstream",
    )
    assert out["ok"], out
    assert out["latest_version"] == "1.0.1"
    assert "v9.0.0" not in _git(mirror, "tag")


@pytest.mark.parametrize("target_version", [None, "1.0.1"])
@pytest.mark.parametrize("ineligible", ["tag_only", "draft", "partial", "unavailable", "malformed", "published"])
def test_custom_remote_gitless_release_eligibility_uses_mirror_origin(
    git_fixture, tmp_path, published_releases, target_version, ineligible,
):
    from src.application.service_upgrade import service_upgrade

    source = git_fixture
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "--origin", "upstream", SOURCE_URL, str(checkout))
    cache = tmp_path / "cache"
    runtime = tmp_path / "runtime"
    initial = service_upgrade_check(
        repo_root=checkout, runtime_root=runtime, cache_root=cache, remote_name="upstream",
    )
    assert initial["ok"], initial
    release = tmp_path / "releases/1.0.0"
    _materialize_release_from_git_cache(
        remote_url=SOURCE_URL, tag="v1.0.0", target_dir=release,
        cache_root=cache, run_cmd=subprocess.run, operations=[],
    )
    assert (release / "VERSION").read_text() == "1.0.0\n"
    assert not (release / ".git").exists()
    current = tmp_path / "current"
    current.symlink_to(release, target_is_directory=True)
    mirror = cache / "git/options-monitor.git"
    decoy = tmp_path / "decoy"
    _git(tmp_path, "clone", str(source), str(decoy))
    _publish(decoy, "9.0.0")
    _git(mirror, "remote", "add", "upstream", str(decoy))
    _publish(source, "1.0.1")

    responses, http_calls = published_releases
    base = "https://api.github.com/repos/cache-fixture/options-monitor"
    metadata_url = base + "/releases/tags/v1.0.1"
    assets_url = base + "/releases/2/assets?per_page=100&page=1"
    if ineligible == "tag_only":
        responses[metadata_url] = ValueError("release_not_eligible: release not found")
    elif ineligible == "draft":
        responses[metadata_url]["draft"] = True
    elif ineligible == "partial":
        responses[assets_url].pop()
    elif ineligible == "unavailable":
        responses[metadata_url] = ValueError("release_metadata_unavailable: request failed")
    elif ineligible == "malformed":
        responses[metadata_url] = []
    http_calls.clear()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.run(command, **kwargs)

    result = service_upgrade_check(
        repo_root=current, runtime_root=runtime, cache_root=cache,
        remote_name="upstream", target_version=target_version, run_cmd=run,
    )
    eligible = ineligible == "published"
    assert result["ok"] is eligible, result
    assert result["upgrade_available"] is eligible
    assert result["remote_name"] == "upstream"
    assert (metadata_url, 10) in http_calls
    assert all(url.startswith(base + "/releases/") for url, _ in http_calls)
    assert not any("remote.upstream.url" in command for command in commands)
    assert "v9.0.0" not in _git(mirror, "tag")
    assert _git(mirror, "config", "--get", "remote.origin.url") == SOURCE_URL
    if eligible:
        assert result["latest_version"] == "1.0.1"
        target = tmp_path / "releases/1.0.1"
        _materialize_release_from_git_cache(
            remote_url=SOURCE_URL, tag=result["release_tag"], target_dir=target,
            cache_root=cache, run_cmd=run, operations=[],
        )
        assert (target / "VERSION").read_text() == "1.0.1\n"
        assert _git(mirror, "rev-parse", result["release_tag"]) == _git(source, "rev-parse", "v1.0.1")
        assert any("archive" in command for command in commands)
        assert any("fetch" in command and command[-1] == "origin" for command in commands)
    else:
        assert result["latest_version"] is None
        assert result["selected_tag"] is None
        commands.clear()
        applied = service_upgrade(
            repo_root=current, runtime_root=runtime, cache_root=cache,
            releases_root=tmp_path / "releases", remote_name="upstream",
            target_version=target_version, confirm=True, restart_services=False, run_cmd=run,
        )
        assert applied["ok"] is False
        assert applied["status"] == "no_target_version"
        assert applied["check"]["status"] == "upgrade_check_failed"
        assert applied["changed"] is False
        assert current.resolve() == release
        assert not (tmp_path / "releases/1.0.1").exists()
        assert not any("archive" in command or "venv" in command for command in commands)
        assert not any(command[0] in {"npm", "systemctl", "launchctl"} for command in commands)
