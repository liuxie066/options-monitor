"""Artifact boundary regression tests; all inputs and effects are isolated."""
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from src.application import runtime_bundle as bundle

ROOT = Path(__file__).resolve().parents[1]
TAG = "v1.2.3"
SHA = "a" * 40


def test_bundle_source_excludes_developer_instructions(tmp_path):
    from scripts.build_runtime_bundle import copy_source

    source = tmp_path / 'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    private = ['AGENTS.md', 'CLAUDE.md', '.agents/skills/private/SKILL.md',
               'src/nested/AGENTS.md', 'docs/CLAUDE.md', 'src/.agents/private/SKILL.md']
    product = ['om', 'om-agent', 'VERSION', 'src/application/bot/runtime.py', 'docs/INSTALL.md']
    for name in private + product:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture\n')
    target = tmp_path / 'product'
    copy_source(source, target)
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()) == sorted(product)


def miniature(root, platform_name="macos-arm64"):
    root.mkdir(parents=True)
    for name, content in [(bundle.PYTHON, "#!/bin/sh\nexit 0\n"), ("om", "#!/bin/sh\n"), ("om-agent", "#!/bin/sh\n"), ("VERSION", "1.2.3\n")]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
    lock = json.loads((ROOT / "scripts/runtime_bundle.lock.json").read_text())
    python = lock["platforms"][platform_name]["python"]
    manifest = dict(schema_version=1, kind="options-monitor-runtime", version="1.2.3", tag=TAG,
                    repository=bundle.REPOSITORY, commit=SHA, platform=platform_name, source_dirty=False,
                    python=dict(version=python["version"], executable=bundle.PYTHON, source_url=python["url"], sha256=python["sha256"]),
                    minimum_system=bundle.MINIMUM_SYSTEMS[platform_name], dependencies={"pandas": "3.0.2"},
                    files={p.relative_to(root).as_posix(): bundle.sha256(p) for p in root.rglob("*") if p.is_file()})
    (root / bundle.MANIFEST).write_text(json.dumps(manifest))
    return manifest


def test_core_bundle_selfcheck_does_not_require_message_sdk(tmp_path, monkeypatch):
    root = tmp_path / "core-bundle"
    miniature(root, platform_name=bundle.current_platform())
    commands = []

    def offline_runner(command, **kwargs):
        commands.append(command)
        assert "lark_oapi" not in " ".join(command)
        assert "om_feishu_channel" not in " ".join(command)
        assert "om_wechat_channel" not in " ".join(command)
        return subprocess.CompletedProcess(command, 0, "{}" if command[-1] == "spec" else "", "")

    monkeypatch.setattr(bundle.subprocess, "run", offline_runner)
    result = bundle.validate_runtime(root)
    assert result["ok"]
    assert len(commands) == 3
    assert "import pandas,numpy,yaml,futu,jsonschema" in commands[0][2]
    assert commands[1:] == [[str(root / "om"), "help"], [str(root / "om-agent"), "spec"]]


def archive_root(root, archive):
    with tarfile.open(archive, "w:gz") as tar:
        for p in root.rglob("*"):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(root).as_posix())


def test_materialization_checks_identity_and_every_file_before_publication(tmp_path):
    source = tmp_path / "source"
    miniature(source)
    archive = tmp_path / "bundle.tar.gz"
    archive_root(source, archive)
    target = tmp_path / "中文 空格" / "target"
    manifest = bundle.materialize_bundle(archive, target, expected_tag=TAG, expected_platform="macos-arm64")
    assert manifest["commit"] == SHA
    with pytest.raises(FileExistsError):
        bundle.materialize_bundle(archive, target, expected_tag=TAG, expected_platform="macos-arm64")
    (source / "VERSION").write_text("9.9.9\n")
    archive_root(source, archive)
    failed = tmp_path / "failed"
    with pytest.raises(bundle.RuntimeBundleError, match="VERSION"):
        bundle.materialize_bundle(archive, failed, expected_tag=TAG, expected_platform="macos-arm64")
    assert not failed.exists() and target.exists()


@pytest.mark.parametrize("damage", ["tag", "platform", "checksum", "python", "inventory"])
def test_manifest_rejects_broken_declared_bundle(tmp_path, damage):
    manifest = miniature(tmp_path / "source")
    root = tmp_path / "source"
    kwargs = dict(expected_tag=TAG, expected_platform="macos-arm64", verify_files=True)
    if damage == "tag": kwargs["expected_tag"] = "v1.2.4"
    if damage == "platform": kwargs["expected_platform"] = "linux-x86_64"
    if damage == "checksum": (root / "om").write_text("tampered")
    if damage == "python": (root / bundle.PYTHON).unlink()
    if damage == "inventory":
        manifest["files"]["../outside"] = "0" * 64
        (root / bundle.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(bundle.RuntimeBundleError):
        bundle.load_manifest(root, **kwargs)


@pytest.mark.parametrize("name,kind", [("../escaped", "file"), ("/absolute", "file"), ("link", "symlink"), ("hard", "hardlink"), ("fifo", "fifo"), ("duplicate", "duplicate")])
def test_archive_paths_links_devices_duplicates_never_materialize(tmp_path, name, kind):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        m = tarfile.TarInfo(name)
        if kind == "symlink": m.type, m.linkname = tarfile.SYMTYPE, "../escaped"
        elif kind == "hardlink": m.type, m.linkname = tarfile.LNKTYPE, "../escaped"
        elif kind == "fifo": m.type = tarfile.FIFOTYPE
        else: m.size = 1
        tar.addfile(m, io.BytesIO(b"x") if m.isfile() else None)
        if kind == "duplicate": tar.addfile(m, io.BytesIO(b"x"))
    with pytest.raises(bundle.RuntimeBundleError):
        bundle.materialize_bundle(archive, tmp_path / "target", expected_tag=TAG, expected_platform="macos-arm64")
    assert not (tmp_path / "target").exists() and not (tmp_path / "escaped").exists()


class Response(io.BytesIO):
    def geturl(self): return "https://github.com/public/download"


def test_download_mismatch_preserves_destination_and_cleans_partial(tmp_path, monkeypatch):
    dest = tmp_path / "archive"
    dest.write_bytes(b"previous")
    monkeypatch.setattr(bundle, "urlopen", lambda *a, **k: Response(b"bad"))
    with pytest.raises(bundle.RuntimeBundleError, match="SHA256"):
        bundle.download_verified("https://github.com/public/download", dest, "0" * 64)
    assert dest.read_bytes() == b"previous" and not dest.with_name("archive.part").exists()


def test_release_resolution_only_published_stable_exact_identity(monkeypatch):
    monkeypatch.setattr(bundle, "_read_json", lambda url: dict(tag_name=TAG, draft=False, prerelease=False))
    assert bundle.resolve_release()["tag_name"] == TAG
    with pytest.raises(bundle.RuntimeBundleError): bundle.resolve_release("v1.2.4")
    monkeypatch.setattr(bundle, "_read_json", lambda url: dict(tag_name=TAG, draft=True, prerelease=False))
    with pytest.raises(bundle.RuntimeBundleError): bundle.resolve_release()


def test_bundle_download_binds_asset_and_digest(monkeypatch, tmp_path):
    name = bundle.asset_name(TAG, "macos-arm64")
    url = f"https://github.com/{bundle.REPOSITORY}/releases/download/{TAG}/{name}"
    release = dict(tag_name=TAG, assets=[dict(name=name, state="uploaded", browser_download_url=url, digest="sha256:" + "a"*64)])
    calls = []
    monkeypatch.setattr(bundle, "download_verified", lambda *args: calls.append(args) or args[1])
    assert bundle.download_bundle(release, tmp_path / "archive", platform_name="macos-arm64") == tmp_path / "archive"
    assert calls[0][2] == "a"*64
    release["assets"][0]["browser_download_url"] = "https://wrong.invalid/bundle"
    with pytest.raises(bundle.RuntimeBundleError): bundle.download_bundle(release, tmp_path / "other", platform_name="macos-arm64")


def test_real_lock_is_complete_pinned_and_has_no_qualification_fiction():
    lock = json.loads((ROOT / "scripts/runtime_bundle.lock.json").read_text())
    assert lock["release_platforms"] == ["macos-arm64"]
    for value in lock["dependencies"].values():
        assert value["version"] and value["license"]
        for platform_name in bundle.PLATFORMS:
            artifact = value["artifacts"][platform_name]
            assert artifact["url"].startswith("https://files.pythonhosted.org/")
            assert bundle.SHA_RE.fullmatch(artifact["sha256"])
    for platform_name in bundle.PLATFORMS:
        assert bundle.SHA_RE.fullmatch(lock["platforms"][platform_name]["python"]["sha256"])
        assert lock["platforms"][platform_name]["minimum_system"] == bundle.MINIMUM_SYSTEMS[platform_name]
    assert len(lock["python_licenses"]) >= 20


def test_publication_binds_clean_source_receipt_and_archive(tmp_path):
    root = tmp_path / "root"
    manifest = miniature(root)
    out = tmp_path / "out"
    out.mkdir()
    name = bundle.asset_name(TAG, "macos-arm64")
    archive_root(root, out / name)
    digest = bundle.sha256(out / name)
    (out / (name + ".manifest.json")).write_text(json.dumps(manifest))
    (out / (name + ".sha256")).write_text(f"{digest}  {name}\n")
    receipt = dict(ok=True, sha256=digest, commit=SHA, version="1.2.3", platform="macos-arm64", source_dirty=False,
                   original_staging_removed=True, relocated_path_contains_chinese_and_spaces=True)
    (out / (name + ".qualification.json")).write_text(json.dumps(receipt))
    bundle.validate_publication_set(ROOT, out, tag=TAG, commit=SHA)
    receipt["commit"] = "b"*40
    (out / (name + ".qualification.json")).write_text(json.dumps(receipt))
    with pytest.raises(bundle.RuntimeBundleError, match="identity"):
        bundle.validate_publication_set(ROOT, out, tag=TAG, commit=SHA)
    (out / (name + ".qualification.json")).unlink()
    with pytest.raises(bundle.RuntimeBundleError, match="set mismatch"):
        bundle.validate_publication_set(ROOT, out, tag=TAG, commit=SHA)


@pytest.mark.parametrize('name', ['src/unlisted.py', 'src/unlisted.pyc', 'src/__pycache__/unlisted.cpython-312.pyc', 'config.yaml'])
def test_full_inventory_rejects_unlisted_code_and_state(tmp_path, name):
    root = tmp_path / 'root'
    miniature(root)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'not declared')
    with pytest.raises(bundle.RuntimeBundleError, match='unlisted bundle file'):
        bundle.load_manifest(root, verify_files=True)


def test_full_inventory_accepts_only_cache_for_declared_source(tmp_path):
    root = tmp_path / 'root'
    manifest = miniature(root)
    source = root / 'src/module.py'
    source.parent.mkdir()
    source.write_text('value = 1\n')
    manifest['files']['src/module.py'] = bundle.sha256(source)
    (root / bundle.MANIFEST).write_text(json.dumps(manifest))
    cache = root / 'src/__pycache__/module.cpython-312.pyc'
    cache.parent.mkdir()
    cache.write_bytes(b'cache')
    assert bundle.load_manifest(root, verify_files=True)['tag'] == TAG
