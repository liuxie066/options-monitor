"""Local release rehearsal: core downloads precede the build, then the same Release gains qualified assets."""
import copy
import hashlib
import importlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from plugin_release_helpers import publication
from scripts import release_check
from src.application import runtime_bundle as bundle
from test_runtime_bundle import archive_root, miniature

ROOT = Path(__file__).resolve().parents[1]
TAG = "v1.2.3"
SHA = "a" * 40
RUN_URL = "https://github.com/liuxie066/options-monitor/actions/runs/123"


@pytest.fixture
def publisher(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / ".github/ci"))
    return importlib.import_module("runtime_release")


def qualified_runtime(out, root):
    out.mkdir()
    manifest = miniature(root)
    name = bundle.asset_name(TAG, "macos-arm64")
    archive_root(root, out / name)
    digest = bundle.sha256(out / name)
    (out / (name + ".manifest.json")).write_text(json.dumps(manifest))
    (out / (name + ".sha256")).write_text(f"{digest}  {name}\n")
    receipt = dict(ok=True, sha256=digest, commit=SHA, version=TAG[1:], platform="macos-arm64",
                   source_dirty=False, original_staging_removed=True, relocated_path_contains_chinese_and_spaces=True)
    (out / (name + ".qualification.json")).write_text(json.dumps(receipt))
    return out


@pytest.fixture
def bundle_dir(tmp_path):
    return qualified_runtime(tmp_path / "bundles", tmp_path / "runtime")


@pytest.fixture
def metadata_repo(tmp_path, monkeypatch):
    base = tmp_path / "metadata"
    base.mkdir()
    (base / "VERSION").write_text(TAG[1:] + "\n")
    (base / "scripts").mkdir()
    (base / "scripts/runtime_bundle.lock.json").write_bytes((ROOT / "scripts/runtime_bundle.lock.json").read_bytes())
    (base / "CHANGELOG.md").write_text(f"## {TAG[1:]} - 2026-11-06\n\n### Bug Fixes\n- Fixed release delivery.\n")
    monkeypatch.setattr(release_check, "repo_base", lambda: base)

    def git_head(command, **kwargs):
        assert command == ["git", "rev-parse", "HEAD"]
        return SHA + "\n"

    monkeypatch.setattr(subprocess, "check_output", git_head)
    return base


def run_release_check(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["release_check.py", "--tag", TAG, *map(str, arguments)])
    return release_check.main()


def test_release_check_pending_is_explicit_and_does_not_claim_bundle_validation(metadata_repo, monkeypatch, capsys):
    assert run_release_check(monkeypatch, "--defer-runtime-bundles") == 0
    output = capsys.readouterr().out
    assert "[RELEASE_CORE_OK]" in output and "runtime_bundles=pending (not validated)" in output
    assert "[RELEASE_OK]" not in output and "runtime_bundles=ready" not in output


def test_release_check_complete_set_is_fully_validated(metadata_repo, bundle_dir, monkeypatch, capsys):
    assert run_release_check(monkeypatch, "--bundle-dir", bundle_dir) == 0
    assert "[RELEASE_OK]" in capsys.readouterr().out
    receipt = next(bundle_dir.glob("*.qualification.json"))
    bad = json.loads(receipt.read_text())
    bad["commit"] = "b" * 40
    receipt.write_text(json.dumps(bad))
    with pytest.raises(bundle.RuntimeBundleError, match="identity"):
        run_release_check(monkeypatch, "--bundle-dir", bundle_dir)


@pytest.mark.parametrize("suffix", ["", ".sha256", ".manifest.json", ".qualification.json"])
def test_release_check_full_mode_rejects_each_missing_asset(metadata_repo, bundle_dir, monkeypatch, suffix):
    (bundle_dir / (bundle.asset_name(TAG, "macos-arm64") + suffix)).unlink()
    with pytest.raises(bundle.RuntimeBundleError, match="set mismatch"):
        run_release_check(monkeypatch, "--bundle-dir", bundle_dir)


@pytest.mark.parametrize("damage", ["tag", "version", "changelog"])
def test_release_check_pending_still_rejects_bad_metadata(metadata_repo, monkeypatch, damage):
    if damage == "version":
        (metadata_repo / "VERSION").write_text("broken")
    elif damage == "changelog":
        (metadata_repo / "CHANGELOG.md").write_text("## Unreleased\n")
    monkeypatch.setattr(sys, "argv", ["release_check.py", "--tag", "v9.9.9" if damage == "tag" else TAG,
                                      "--defer-runtime-bundles"])
    with pytest.raises(SystemExit, match="RELEASE_ERROR"):
        release_check.main()


@pytest.mark.parametrize("mode", ["pending", "complete"])
def test_both_release_modes_preserve_required_coverage_and_taxonomy(metadata_repo, bundle_dir, monkeypatch, mode):
    arguments = ["--defer-runtime-bundles"] if mode == "pending" else ["--bundle-dir", bundle_dir]
    with pytest.raises(SystemExit, match="RELEASE_ERROR"):
        run_release_check(monkeypatch, *arguments, "--require-delta-coverage")
    changelog = metadata_repo / "CHANGELOG.md"
    changelog.write_text(changelog.read_text().replace("### Bug Fixes", "### Fixed"))
    with pytest.raises(SystemExit, match="RELEASE_ERROR"):
        run_release_check(monkeypatch, *arguments, "--require-current-taxonomy")


def test_release_check_cannot_defer_and_verify_at_once(metadata_repo, bundle_dir, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        run_release_check(monkeypatch, "--defer-runtime-bundles", "--bundle-dir", bundle_dir)
    assert exc.value.code == 2


class ReleaseAPI:
    """Only in-memory Release data; mutation stand-in accepts the exact PATCH used by gh."""
    def __init__(self, publisher):
        self.release = dict(id=42, tag_name=TAG, draft=False,
                            body=publisher.render_status("# Release notes\n\nOriginal notes.\n",
                                                        publisher.status_block(TAG, SHA, "building", RUN_URL)))
        self.assets = []
        self.writes = []
        self.commands = []
        self.commit = SHA

    def get(self, path, **kwargs):
        if path == f"/releases/tags/{TAG}":
            return copy.deepcopy(self.release)
        if path == "/releases/42/assets?per_page=100&page=1":
            return copy.deepcopy(self.assets)
        if path == f"/git/ref/tags/{TAG}":
            return dict(ref=f"refs/tags/{TAG}", object=dict(type="commit", sha=self.commit))
        pytest.fail(f"unexpected API read: {path}")

    def upload(self, name, data):
        self.assets.append(dict(id=len(self.assets) + 1, name=name, state="uploaded", size=len(data),
                                digest="sha256:" + hashlib.sha256(data).hexdigest()))

    def patch(self, command, **kwargs):
        self.commands.append(command)
        assert command == ["gh", "api", "--method", "PATCH", "repos/liuxie066/options-monitor/releases/42", "--input", "-"]
        data = json.loads(kwargs["input"])
        assert set(data) == {"body"}
        self.release.update(data)
        self.writes.append(command)


def test_local_rehearsal_core_downloads_then_qualified_assets_on_same_release(tmp_path, publisher):
    api = ReleaseAPI(publisher)
    reads = []
    origin = f"https://api.github.com/repos/{publisher.checks.REPOSITORY}"

    def open_url(request, **kwargs):
        reads.append((request.get_method(), request.full_url))
        assert request.get_method() == "GET" and request.full_url.startswith(origin + "/")
        return io.BytesIO(json.dumps(api.get(request.full_url.removeprefix(origin))).encode())

    http = publisher.checks.GitHubReads("synthetic", open_url=open_url)
    plugins = tmp_path / "plugins"
    catalog = publication(plugins)
    for name in ["om-plugins.json", "om-plugins.md"] + [p["asset"] for p in catalog["plugins"]]:
        api.upload(name, (plugins / name).read_bytes())
    for name in (f"options-monitor-{TAG}.tar.gz", "om-agent-spec.json", "VERSION"):
        api.upload(name, b"core product fixture")

    # At this point no runtime has been built and the core assets already pass readback.
    publisher.checks.readback(http, TAG, SHA, plugin_dir=plugins, runtime_pending=True)
    assert "Status: **building**" in api.release["body"]
    assert not bundle.publication_assets(ROOT, TAG) & {a["name"] for a in api.assets}
    with pytest.raises(publisher.checks.EvidenceError, match="incomplete"):
        publisher.checks.readback(http, TAG, SHA, plugin_dir=plugins)

    # The build happens only after core availability has been established.
    out = qualified_runtime(tmp_path / "built", tmp_path / "runtime")
    bundle.validate_publication_set(ROOT, out, tag=TAG, commit=SHA)
    for path in out.iterdir():
        api.upload(path.name, path.read_bytes())
    core_assets = copy.deepcopy([a for a in api.assets if a["name"] not in bundle.publication_assets(ROOT, TAG)])
    publisher.update_status(http, TAG, SHA, "ready", RUN_URL, bundle_dir=out, run=api.patch)
    assert api.release["id"] == 42 and api.release["tag_name"] == TAG and api.commit == SHA
    assert len(api.writes) == 1 and api.commands == api.writes
    assert reads and all(method == "GET" and url.startswith(origin + "/") for method, url in reads)
    assert "Status: **ready**" in api.release["body"]
    assert "Original notes." in api.release["body"] and ".qualification.json" in api.release["body"]
    assert ".sha256" in api.release["body"]
    assert core_assets == [a for a in api.assets if a["name"] not in bundle.publication_assets(ROOT, TAG)]
    publisher.checks.readback(http, TAG, SHA, plugin_dir=plugins)


@pytest.mark.parametrize("damage", ["missing", "digest", "size", "uploading", "duplicate", "tag", "receipt"])
def test_unqualified_or_incomplete_upload_cannot_be_marked_ready(publisher, bundle_dir, damage):
    api = ReleaseAPI(publisher)
    for path in bundle_dir.iterdir():
        api.upload(path.name, path.read_bytes())
    if damage == "missing":
        api.assets.pop()
    elif damage == "digest":
        api.assets[0]["digest"] = "sha256:" + "b" * 64
    elif damage == "size":
        api.assets[0]["size"] += 1
    elif damage == "uploading":
        api.assets[0]["state"] = "new"
    elif damage == "duplicate":
        api.assets.append(dict(api.assets[0], id=100))
    elif damage == "tag":
        api.commit = "b" * 40
    else:
        receipt = next(bundle_dir.glob("*.qualification.json"))
        bad = json.loads(receipt.read_text())
        bad["ok"] = False
        receipt.write_text(json.dumps(bad))
    with pytest.raises((publisher.checks.EvidenceError, bundle.RuntimeBundleError)):
        publisher.update_status(api, TAG, SHA, "ready", RUN_URL, bundle_dir=bundle_dir, run=api.patch)
    assert not api.writes and "Status: **building**" in api.release["body"]


def test_build_failure_or_cancel_updates_existing_release_and_preserves_notes(publisher):
    api = ReleaseAPI(publisher)
    publisher.update_status(api, TAG, SHA, "failed", RUN_URL, run=api.patch)
    assert "Status: **failed**" in api.release["body"] and "含取消" in api.release["body"]
    assert "Original notes." in api.release["body"] and RUN_URL in api.release["body"]
    assert api.release["id"] == 42 and api.release["body"].count(publisher.START) == 1
    publisher.update_status(api, TAG, SHA, "failed", RUN_URL, run=api.patch)
    assert api.release["body"].count(publisher.START) == 1


def test_full_retry_resets_failed_to_building_before_runtime_ready(publisher, metadata_repo, bundle_dir, monkeypatch):
    api = ReleaseAPI(publisher)
    publisher.update_status(api, TAG, SHA, "failed", RUN_URL, run=api.patch)
    assert "Re-run all jobs" in api.release["body"]
    assert "Status: **failed**" in api.release["body"]
    notes = metadata_repo / "notes.md"
    notes.write_text(api.release["body"])
    monkeypatch.chdir(metadata_repo)
    for key, value in dict(RELEASE_TAG=TAG, GITHUB_SHA=SHA, GITHUB_REPOSITORY=publisher.checks.REPOSITORY,
                           GITHUB_RUN_ID="123").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["runtime_release.py", "prepare-notes", "--notes", str(notes)])
    assert publisher.main() == 0
    api.release["body"] = notes.read_text()
    assert "Status: **building**" in api.release["body"]
    assert "Status: **failed**" not in api.release["body"]
    for path in bundle_dir.iterdir():
        api.upload(path.name, path.read_bytes())
    publisher.update_status(api, TAG, SHA, "ready", RUN_URL, bundle_dir=bundle_dir, run=api.patch)
    assert "Status: **ready**" in api.release["body"]
    assert "Original notes." in api.release["body"] and api.release["body"].count(publisher.START) == 1


def test_existing_public_release_can_mark_failed_after_core_upload_error(publisher):
    api = ReleaseAPI(publisher)
    publisher.update_status(api, TAG, SHA, "failed", RUN_URL, run=api.patch)
    assert len(api.writes) == 1 and "Status: **building**" not in api.release["body"]
    assert "Status: **failed**" in api.release["body"] and "Original notes." in api.release["body"]


def test_ready_requires_artifacts_and_body_update_requires_readback(publisher):
    api = ReleaseAPI(publisher)
    with pytest.raises(publisher.checks.EvidenceError, match="requires verified"):
        publisher.update_status(api, TAG, SHA, "ready", RUN_URL, run=api.patch)
    with pytest.raises(publisher.checks.EvidenceError, match="readback mismatch"):
        publisher.update_status(api, TAG, SHA, "failed", RUN_URL, run=lambda *a, **k: None)
