from __future__ import annotations

import copy
import io
import json
import subprocess
from urllib.error import HTTPError, URLError

import pytest

from src.application import release_target as rt

API = "https://api.github.com/repos/example/options-monitor"
REMOTE = "https://github.com/example/options-monitor.git"


def release(tag="v1.0.1"):
    return {"id": 7, "tag_name": tag, "draft": False, "published_at": "2026-10-25T00:00:00Z"}


def assets(tag="v1.0.1"):
    return [{"name": n, "state": "uploaded", "size": 1} for n in
            ("VERSION", "om-agent-spec.json", f"options-monitor-{tag}.tar.gz")]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected HTTP transport")
    monkeypatch.setattr(rt, "build_opener", unexpected, raising=False)


def resolve(tmp_path, monkeypatch, *, metadata=None, uploaded=None, explicit="1.0.1", remote=REMOTE, fetch=False):
    calls = []
    responses = {f"{API}/releases/tags/v1.0.1": release() if metadata is None else metadata,
                 f"{API}/releases/7/assets?per_page=100&page=1": assets() if uploaded is None else uploaded}
    def get(url, *, timeout):
        calls.append((url, timeout))
        value = responses[url]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)
    monkeypatch.setattr(rt, "_github_get_json", get, raising=False)
    git_calls = []
    def git(command, **kwargs):
        git_calls.append(command)
        if "for-each-ref" in command:
            return subprocess.CompletedProcess(command, 0, "a refs/tags/v1.0.0\nb refs/tags/v1.0.1\n", "")
        if "config" in command:
            return subprocess.CompletedProcess(command, 0, REMOTE, "")
        return subprocess.CompletedProcess(command, 0, "", "")
    out = rt.resolve_upgrade_target(current_version="1.0.0", repo_root=tmp_path, cache_root=tmp_path / "cache",
                                    remote_url=remote, explicit_target=explicit, fetch=fetch, run_cmd=git)
    return out, calls, git_calls


@pytest.mark.parametrize("explicit", ["1.0.1", "v1.0.1", None])
def test_eligible_target(tmp_path, monkeypatch, explicit):
    out, calls, git = resolve(tmp_path, monkeypatch, explicit=explicit)
    assert out["ok"] is True
    assert out["release_tag"] == out["selected_tag"] == "v1.0.1"
    assert out["latest_version"] == "1.0.1" and out["update_available"] is True
    assert out["target_source"] == ("explicit" if explicit else "latest_from_cache")
    assert len(calls) == 2
    assert (not git) if explicit else any("for-each-ref" in c for c in git)


@pytest.mark.parametrize("change", [
    {"draft": True}, {"published_at": None}, {"tag_name": "v1.0.2"}, {"draft": "false"},
    {"published_at": "bad"}, {"published_at": "2026-10-25T00:00:00"}, {"id": True}, {"id": 0},
    {"published_at": ""}, {"draft": None},
])
@pytest.mark.parametrize("explicit", [None, "1.0.1"])
def test_release_metadata_fail_closed(tmp_path, monkeypatch, change, explicit):
    out, calls, _ = resolve(tmp_path, monkeypatch, metadata={**release(), **change}, explicit=explicit)
    assert_failed(out)
    assert len(calls) == 1


def assert_failed(out):
    assert out["ok"] is False and out["update_available"] is False
    assert out["latest_version"] is out["release_tag"] is out["selected_tag"] is None


@pytest.mark.parametrize("uploaded", [[], assets()[:2], assets()+[assets()[0]], {},
    [{**a, "size": 0} for a in assets()], [{**a, "size": True} for a in assets()],
    [{**a, "state": "new"} for a in assets()], [{"name": "VERSION"}], [None]])
@pytest.mark.parametrize("explicit", [None, "1.0.1"])
def test_partial_or_invalid_assets_fail_closed(tmp_path, monkeypatch, uploaded, explicit):
    out, _, _ = resolve(tmp_path, monkeypatch, uploaded=uploaded, explicit=explicit)
    assert_failed(out)


@pytest.mark.parametrize("remote", [REMOTE, "https://github.com/example/options-monitor/",
    "git@github.com:example/options-monitor.git", "ssh://git@github.com:2222/example/options-monitor.git/"])
def test_github_source_forms(tmp_path, monkeypatch, remote):
    out, calls, _ = resolve(tmp_path, monkeypatch, remote=remote)
    assert out["ok"] and calls[0][0].startswith(API)
    assert "fake-secret" not in str(out)


def test_synthetic_password_userinfo_is_not_forwarded(tmp_path, monkeypatch):
    from urllib.parse import urlsplit

    password = "not-a-real-credential"
    remote = urlsplit(REMOTE)._replace(netloc=f"fixture-user:{password}@github.com").geturl()
    out, calls, _ = resolve(tmp_path, monkeypatch, remote=remote)
    assert out["ok"] and calls[0][0].startswith(API)
    assert password not in str(out) and password not in str(calls)
    assert "fixture-user" not in str(out) and "fixture-user" not in str(calls)


@pytest.mark.parametrize("remote", ["https://example.invalid/o/r", "http://github.com/o/r", "git@alias:o/r",
    "https://github.com/o/r?token=secret", "https://github.com/o/r#x", "https://github.com/o/r/extra",
    "/tmp/repo", "", "https://github.com/o/..", "https://github.com/o/r%2fx"])
def test_unverifiable_source_fails_closed(tmp_path, monkeypatch, remote):
    out, calls, _ = resolve(tmp_path, monkeypatch, remote=remote)
    assert_failed(out)
    assert calls == []


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())
    def getcode(self):
        return 200


@pytest.mark.parametrize("tokens,expected", [({}, None), ({"GH_TOKEN": "fake-gh", "GITHUB_TOKEN": "fake-github"}, "fake-gh"),
    ({"GH_TOKEN": "  ", "GITHUB_TOKEN": "fake-github"}, "fake-github")])
def test_http_auth_and_fixed_request(monkeypatch, tokens, expected):
    for k, v in tokens.items():
        monkeypatch.setenv(k, v)
    requests = []
    class Opener:
        def open(self, request, *, timeout):
            requests.append((request, timeout))
            return Response(release())
    monkeypatch.setattr(rt, "build_opener", lambda *handlers: Opener())
    assert rt._github_get_json(f"{API}/releases/tags/v1.0.1", timeout=4) == release()
    request, timeout = requests[0]
    assert request.full_url == f"{API}/releases/tags/v1.0.1" and timeout == 4
    assert request.get_header("Authorization") == (f"Bearer {expected}" if expected else None)
    assert request.get_header("X-github-api-version") == "2026-03-10"
    assert request.get_header("User-agent")


@pytest.mark.parametrize("code", [301, 302, 401, 403, 404, 429, 500, 503])
def test_http_errors_are_safe(monkeypatch, code):
    class Opener:
        def open(self, request, **kwargs):
            raise HTTPError(request.full_url, code, "fake-secret", {}, None)
    monkeypatch.setattr(rt, "build_opener", lambda *handlers: Opener())
    with pytest.raises(ValueError) as exc:
        rt._github_get_json(f"{API}/releases/tags/v1.0.1", timeout=10)
    assert "fake-secret" not in str(exc.value)
    assert ("release_not_found_or_inaccessible" if code == 404 else "release_metadata_unavailable") in str(exc.value)


@pytest.mark.parametrize("error", [TimeoutError("fake-secret"), URLError("fake-secret"), ValueError("fake-secret")])
def test_http_transport_and_parse_errors_are_safe(monkeypatch, error):
    class Opener:
        def open(self, request, **kwargs):
            raise error
    monkeypatch.setattr(rt, "build_opener", lambda *handlers: Opener())
    with pytest.raises(ValueError, match="release_metadata_unavailable") as exc:
        rt._github_get_json(f"{API}/releases/tags/v1.0.1", timeout=10)
    assert "fake-secret" not in str(exc.value)


def test_http_bad_json_and_redirect(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(b"invalid json fake-secret")
    monkeypatch.setattr(rt, "build_opener", lambda *handlers: Opener())
    with pytest.raises(ValueError, match="release_metadata_unavailable"):
        rt._github_get_json(f"{API}/releases/tags/v1.0.1", timeout=10)
    assert rt._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.invalid") is None


def test_paginated_assets_and_later_page_failure(tmp_path, monkeypatch):
    calls = []
    def get(url, *, timeout):
        calls.append(url)
        if "/tags/" in url:
            return release()
        if url.endswith("page=1"):
            return [{"name": f"extra{i}", "state": "uploaded", "size": 1} for i in range(100)]
        assert url.endswith("page=2")
        return assets()
    monkeypatch.setattr(rt, "_github_get_json", get, raising=False)
    kwargs = dict(current_version="1.0.0", repo_root=tmp_path, cache_root=tmp_path / "cache", remote_url=REMOTE,
                  explicit_target="1.0.1", fetch=False)
    assert rt.resolve_upgrade_target(**kwargs)["ok"]
    assert len(calls) == 3
    original = get
    def unavailable(url, *, timeout):
        if url.endswith("page=2"):
            raise ValueError("release_metadata_unavailable: HTTP 503")
        return original(url, timeout=timeout)
    monkeypatch.setattr(rt, "_github_get_json", unavailable)
    assert_failed(rt.resolve_upgrade_target(**kwargs))


def test_budget_and_request_timeout(tmp_path, monkeypatch):
    clock = [0.0]
    timeouts = []
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock[0])
    def get(url, *, timeout):
        timeouts.append(timeout)
        if "/tags/" in url:
            clock[0] += 25
            return release()
        clock[0] += 6
        return assets()
    monkeypatch.setattr(rt, "_github_get_json", get, raising=False)
    out = rt.resolve_upgrade_target(current_version="1.0.0", repo_root=tmp_path, cache_root=tmp_path / "cache",
                                    remote_url=REMOTE, explicit_target="1.0.1")
    assert_failed(out)
    assert timeouts == [10, 5]


@pytest.mark.parametrize("field", ["id", "tag_name", "draft", "published_at"])
def test_missing_release_field_is_unverifiable(tmp_path, monkeypatch, field):
    metadata = release()
    del metadata[field]
    out, _, _ = resolve(tmp_path, monkeypatch, metadata=metadata)
    assert_failed(out)
    assert "release_metadata_unavailable" in out["error"]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_each_required_asset_is_mandatory(tmp_path, monkeypatch, index):
    uploaded = assets()
    del uploaded[index]
    out, _, _ = resolve(tmp_path, monkeypatch, uploaded=uploaded)
    assert_failed(out)


def test_complete_first_page_does_not_skip_later_errors(tmp_path, monkeypatch):
    def get(url, *, timeout):
        if "/tags/" in url:
            return release()
        if url.endswith("page=1"):
            return assets() + [{"name": f"other{i}"} for i in range(97)]
        assert url.endswith("page=2")
        raise ValueError("release_metadata_unavailable: HTTP 403")
    monkeypatch.setattr(rt, "_github_get_json", get)
    out = rt.resolve_upgrade_target(current_version="1.0.0", repo_root=tmp_path, cache_root=tmp_path / "cache",
                                    remote_url=REMOTE, explicit_target="1.0.1", fetch=False)
    assert_failed(out)


def test_expired_budget_prevents_next_request(tmp_path, monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock[0])
    def get(url, *, timeout):
        calls.append(url)
        clock[0] = 30
        return release()
    monkeypatch.setattr(rt, "_github_get_json", get)
    out = rt.resolve_upgrade_target(current_version="1.0.0", repo_root=tmp_path, cache_root=tmp_path / "cache",
                                    remote_url=REMOTE, explicit_target="1.0.1", fetch=False)
    assert_failed(out)
    assert len(calls) == 1


def test_prerelease_eligibility_does_not_add_stable_only_policy(tmp_path, monkeypatch):
    tag = "v1.0.1-rc.1"
    calls = []
    def get(url, *, timeout):
        calls.append(url)
        if url == f"{API}/releases/tags/{tag}":
            return {**release(tag), "prerelease": True}
        assert url == f"{API}/releases/7/assets?per_page=100&page=1"
        return assets(tag)
    monkeypatch.setattr(rt, "_github_get_json", get)
    out = rt.resolve_upgrade_target(current_version="1.0.0", repo_root=tmp_path, cache_root=tmp_path / "cache",
                                    remote_url=REMOTE, explicit_target=tag, fetch=False)
    assert out["ok"] and out["update_available"] and out["release_tag"] == tag
    assert len(calls) == 2


def test_automatic_highest_failure_does_not_query_eligible_lower_release(tmp_path, monkeypatch):
    calls = []
    def get(url, *, timeout):
        calls.append(url)
        if url == f"{API}/releases/tags/v1.0.2":
            raise ValueError("release_not_found_or_inaccessible: HTTP 404")
        if url == f"{API}/releases/tags/v1.0.1":
            return release()
        assert url == f"{API}/releases/7/assets?per_page=100&page=1"
        return assets()
    monkeypatch.setattr(rt, "_github_get_json", get)
    def git(command, **kwargs):
        assert "for-each-ref" in command
        return subprocess.CompletedProcess(command, 0, "a refs/tags/v1.0.1\nb refs/tags/v1.0.2\n", "")
    kwargs = dict(current_version="1.0.0", repo_root=tmp_path, cache_root=tmp_path / "cache", remote_url=REMOTE,
                  fetch=False, run_cmd=git)
    out = rt.resolve_upgrade_target(**kwargs)
    assert_failed(out)
    assert out["tag_count"] == 2
    assert calls == [f"{API}/releases/tags/v1.0.2"]
    assert rt.resolve_upgrade_target(**kwargs, explicit_target="1.0.1")["ok"]


def test_transport_never_sends_token_to_non_api_host(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake-secret")
    with pytest.raises(ValueError, match="invalid API destination") as exc:
        rt._github_get_json("https://example.invalid/release", timeout=10)
    assert "fake-secret" not in str(exc.value)
