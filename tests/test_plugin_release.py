"""Official plugin trust boundaries and isolated build orchestration, all offline."""
import copy
from contextlib import nullcontext
from email.parser import BytesParser
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tomllib
from zipfile import ZipFile, ZipInfo

import pytest

from plugin_release_helpers import copy_channel_core, publication, refresh_entry, wheel_files, write_catalog, write_wheel
from src.application import plugin_release as release

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def artifacts(tmp_path):
    return tmp_path, publication(tmp_path)


def test_channel_core_fixture_copies_real_sources_and_imports_offline(tmp_path):
    for name in ("src/__init__.py", "src/application/__init__.py", "src/application/plugin_runtime.py"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    copy_channel_core(ROOT, tmp_path)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (ROOT / path.relative_to(tmp_path)).read_bytes()
    assert (tmp_path / "constraints/runtime-bundle.txt").is_file()
    result = subprocess.run([
        sys.executable, "-I", "-B", "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import src.infrastructure.feishu_bot; import src.infrastructure.feishu_ws_client; "
        "import src.application.channels.wechat_clawbot.ilink_client; "
        "assert src.infrastructure.feishu_bot.__file__.startswith(sys.argv[1])",
        str(tmp_path),
    ], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_exact_four_official_plugins_and_dependency_metadata(artifacts):
    directory, catalog = artifacts
    assert {e["id"]: (e["distribution"], e["module"]) for e in catalog["plugins"]} == {
        "pm": ("om-pm-plugin", "om_pm_plugin"),
        "position-advice": ("om-position-advice-plugin", "om_position_advice_plugin"),
        "feishu": ("om-feishu-channel-plugin", "om_feishu_channel"),
        "wechat": ("om-wechat-channel-plugin", "om_wechat_channel"),
    }
    for entry in catalog["plugins"]:
        path = directory / entry["asset"]
        with ZipFile(path) as wheel:
            metadata = BytesParser().parsebytes(wheel.read(next(n for n in wheel.namelist() if n.endswith("/METADATA"))))
        assert metadata.get_all("Requires-Dist", []) == (["lark-oapi"] if entry["id"] == "feishu" else [])
        assert not metadata.get_all("Provides-Extra")
        release.validate_wheel(path, entry)
    notes = (directory / release.NOTES).read_text()
    assert "lark-oapi" in notes and "no package dependencies" not in notes
    assert release.verify_publication(directory, tag="v1.2.3") == catalog


@pytest.mark.parametrize("plugin_id", ["pm", "position-advice", "feishu", "wechat"])
def test_catalog_requires_every_official_plugin(artifacts, plugin_id):
    directory, catalog = artifacts
    entry = next(e for e in catalog["plugins"] if e["id"] == plugin_id)
    (directory / entry["asset"]).unlink()
    with pytest.raises(ValueError, match="exactly four wheels"):
        release.verify_publication(directory, tag="v1.2.3")
    catalog["plugins"].remove(entry)
    with pytest.raises(ValueError, match="exactly the four official plugins"):
        release.validate_catalog(catalog, om_version="1.2.3")


def test_publication_and_independent_selected_wheel(artifacts):
    directory, catalog = artifacts
    assert release.verify_publication(directory, tag="v1.2.3", commit="a" * 40) == catalog
    entry = catalog["plugins"][0]
    (directory / catalog["plugins"][1]["asset"]).unlink()
    (directory / release.NOTES).unlink()
    assert release.validate_catalog(catalog, om_version="1.2.3") == catalog
    assert release.validate_wheel(directory / entry["asset"], entry) is None
    with pytest.raises(ValueError):
        release.verify_publication(directory, tag="v1.2.3")


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("repository", "other/repository"),
    ("om_version", "1.2.4"), ("tag", "v1.2.4"), ("commit", "a" * 39),
    ("commit", "g" * 40), ("plugins", []), ("plugins", None),
])
def test_catalog_identity(artifacts, field, value):
    _, catalog = artifacts
    catalog[field] = value
    with pytest.raises(ValueError):
        release.validate_catalog(catalog, om_version="1.2.3")


@pytest.mark.parametrize("field,value", [
    ("id", "thirdparty"), ("distribution", "thirdparty"), ("module", "src"),
    ("version", "0.1.0"), ("requires_python", ">=3.11"),
    ("asset", "../escape.whl"), ("asset", "om_pm_plugin-1.2.3-cp312-none-any.whl"),
    ("sha256", "g" * 64), ("sha256", None), ("size", True), ("size", 0), ("size", -1),
])
def test_catalog_entry_contract(artifacts, field, value):
    _, catalog = artifacts
    catalog["plugins"][0][field] = value
    with pytest.raises(ValueError):
        release.validate_catalog(catalog, om_version="1.2.3")


def test_catalog_no_duplicates_cross_identity_or_wrong_host(artifacts):
    _, catalog = artifacts
    with pytest.raises(ValueError):
        release.validate_catalog(catalog, om_version="1.2.4")
    changed = copy.deepcopy(catalog)
    changed["plugins"][0]["module"] = catalog["plugins"][1]["module"]
    with pytest.raises(ValueError):
        release.validate_catalog(changed, om_version="1.2.3")
    catalog["plugins"][1] = dict(catalog["plugins"][0])
    with pytest.raises(ValueError):
        release.validate_catalog(catalog, om_version="1.2.3")


@pytest.mark.parametrize("damage", ["missing", "extra", "digest", "size", "notes", "commit", "tag", "catalog"])
def test_publication_integrity(artifacts, damage):
    directory, catalog = artifacts
    path = directory / catalog["plugins"][0]["asset"]
    kwargs = dict(tag="v1.2.3", commit="a" * 40)
    if damage == "missing":
        path.unlink()
    elif damage == "extra":
        (directory / "extra.whl").write_bytes(b"")
    elif damage == "digest":
        path.write_bytes(b"x" * path.stat().st_size)
    elif damage == "size":
        catalog["plugins"][0]["size"] += 1
        write_catalog(directory, catalog)
    elif damage == "notes":
        (directory / release.NOTES).write_text("incompatible notes")
    elif damage in {"commit", "tag"}:
        kwargs[damage] = "b" * 40 if damage == "commit" else "v1.2.4"
    else:
        (directory / release.CATALOG).write_text("[]")
    with pytest.raises(ValueError):
        release.verify_publication(directory, **kwargs)


@pytest.mark.parametrize("filename", [
    "../escape.py", "/absolute.py", "om_pm_plugin/../escape.py", "om_pm_plugin//escape.py",
    "om_pm_plugin\\escape.py", "other/__init__.py", "om_pm_plugin/native.so",
    "om_pm_plugin/startup.pth", "om_pm_plugin/__pycache__/x.pyc",
    "om_pm_plugin-1.2.3.data/scripts/run", "om_pm_plugin-1.2.3.dist-info/entry_points.txt",
])
def test_wheel_unsafe_members(artifacts, filename):
    directory, catalog = artifacts
    entry = catalog["plugins"][0]
    path = directory / entry["asset"]
    files = wheel_files(entry["distribution"], entry["module"], entry["version"])
    files[filename] = b"bad"
    write_wheel(path, files)
    refresh_entry(path, entry)
    with pytest.raises(ValueError):
        release.validate_wheel(path, entry)


@pytest.mark.parametrize("filename,old,new", [
    ("METADATA", b"Name: om-pm-plugin", b"Name: om-other-plugin"),
    ("METADATA", b"Version: 1.2.3", b"Version: 0.1.0"),
    ("METADATA", b"Requires-Python: >=3.12", b"Requires-Python: >=3.13"),
    ("METADATA", b"Metadata-Version: 2.4", b"Metadata-Version: 2.4\nRequires-Dist: pandas"),
    ("METADATA", b"Metadata-Version: 2.4", b"Metadata-Version: 2.4\nName: om-pm-plugin"),
    ("WHEEL", b"Root-Is-Purelib: true", b"Root-Is-Purelib: false"),
    ("WHEEL", b"Tag: py3-none-any", b"Tag: cp312-none-any"),
    ("WHEEL", b"Tag: py3-none-any", b"Tag: py3-none-any\nTag: py2-none-any"),
    ("WHEEL", b"Wheel-Version: 1.0", b"Wheel-Version: 2.0"),
])
def test_wheel_metadata_is_authoritative(artifacts, filename, old, new):
    directory, catalog = artifacts
    entry = catalog["plugins"][0]
    path = directory / entry["asset"]
    files = wheel_files(entry["distribution"], entry["module"], entry["version"])
    key = next(n for n in files if n.endswith("/" + filename))
    files[key] = files[key].replace(old, new)
    write_wheel(path, files)
    refresh_entry(path, entry)
    with pytest.raises(ValueError):
        release.validate_wheel(path, entry)


@pytest.mark.parametrize("plugin_id", ["pm", "position-advice", "feishu", "wechat"])
@pytest.mark.parametrize("headers", [
    "", "Requires-Dist: lark-oapi\n", "Requires-Dist: pandas\n",
    "Requires-Dist: lark-oapi==1.6.5\n", "Requires-Dist: lark-oapi[extra]\n",
    "Requires-Dist: lark-oapi; python_version >= '3.12'\n",
    "Requires-Dist: lark-oapi @ https://example.invalid/sdk.whl\n",
    "Requires-Dist: lark-oapi\nRequires-Dist: pandas\n",
    "Requires-Dist: lark-oapi\nRequires-Dist: lark-oapi\n",
    "Provides-Extra: extra\n", "Requires-Dist: lark-oapi\nProvides-Extra: extra\n",
])
def test_only_feishu_declares_exact_source_dependency(artifacts, plugin_id, headers):
    directory, catalog = artifacts
    entry = next(e for e in catalog["plugins"] if e["id"] == plugin_id)
    path = directory / entry["asset"]
    files = wheel_files(entry["distribution"], entry["module"], entry["version"])
    metadata = next(n for n in files if n.endswith("/METADATA"))
    files[metadata] = (f"Metadata-Version: 2.4\nName: {entry['distribution']}\n"
                       f"Version: {entry['version']}\nRequires-Python: >=3.12\n{headers}\n").encode()
    write_wheel(path, files)
    refresh_entry(path, entry)
    if headers == ("Requires-Dist: lark-oapi\n" if plugin_id == "feishu" else ""):
        release.validate_wheel(path, entry)
    else:
        with pytest.raises(ValueError, match="metadata or dependency mismatch"):
            release.validate_wheel(path, entry)


def test_wheel_record_digest_and_missing_package(artifacts):
    directory, catalog = artifacts
    entry = catalog["plugins"][0]
    path = directory / entry["asset"]
    with ZipFile(path) as wheel:
        files = {n: wheel.read(n) for n in wheel.namelist()}
    files["om_pm_plugin/__init__.py"] = b"modified without updating RECORD"
    with ZipFile(path, "w") as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)
    refresh_entry(path, entry)
    with pytest.raises(ValueError, match="RECORD"):
        release.validate_wheel(path, entry)
    del files["om_pm_plugin/__init__.py"]
    write_wheel(path, files)
    refresh_entry(path, entry)
    with pytest.raises(ValueError, match="package is missing"):
        release.validate_wheel(path, entry)


@pytest.mark.parametrize("damage", ["duplicate", "symlink", "filelink", "zip", "compression", "count", "size", "host"])
def test_wheel_links_corruption_and_limits(artifacts, monkeypatch, damage):
    directory, catalog = artifacts
    entry = catalog["plugins"][0]
    path = directory / entry["asset"]
    if damage in {"duplicate", "symlink"}:
        with ZipFile(path, "a") as wheel:
            member = ZipInfo("om_pm_plugin/linked.py" if damage == "symlink" else "om_pm_plugin/__init__.py")
            if damage == "symlink":
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
            with pytest.warns(UserWarning) if damage == "duplicate" else nullcontext():
                wheel.writestr(member, b"../outside")
    elif damage == "filelink":
        target = directory / "actual-wheel"
        path.rename(target)
        path.symlink_to(target)
    elif damage == "zip":
        path.write_bytes(b"not a zip")
    elif damage == "compression":
        content = bytearray(path.read_bytes())
        central = content.index(b"PK\x01\x02")
        content[8:10] = (99).to_bytes(2, "little")
        content[central + 10:central + 12] = (99).to_bytes(2, "little")
        path.write_bytes(content)
    elif damage in {"count", "size"}:
        monkeypatch.setattr(release, "MAX_WHEEL_MEMBERS" if damage == "count" else "MAX_WHEEL_BYTES", 1)
        monkeypatch.setattr(ZipFile, "read", lambda *a, **k: pytest.fail("must bound archive before reading members"))
    else:
        monkeypatch.setattr(release.sys, "version_info", (3, 11))
    refresh_entry(path, entry)
    with pytest.raises(ValueError):
        release.validate_wheel(path, entry)


def test_isolated_build_versions_source_copy_without_network(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "VERSION").write_text("1.2.3\n")
    shutil.copytree(ROOT / "plugins", source / "plugins")
    originals = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    monkeypatch.setattr(release.subprocess, "check_output", lambda *a, **k: "a" * 40)
    calls = []

    def pip_wheel(argv, **kwargs):
        assert argv[:6] == [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir"]
        assert "--no-build-isolation" not in argv
        package = Path(argv[-1])
        assert not package.is_relative_to(source)
        project = tomllib.loads((package / "pyproject.toml").read_text())["project"]
        assert project["version"] == "1.2.3"
        original = tomllib.loads((source / "plugins" / package.name / "pyproject.toml").read_text())["project"]
        assert project == dict(original, version="1.2.3")
        assert project["dependencies"] == (["lark-oapi"] if project["name"] == "om-feishu-channel-plugin" else [])
        assert kwargs["env"]["PIP_CACHE_DIR"].startswith(str(package.parent))
        plugin = next(p for p in release.OFFICIAL_PLUGINS if p[1] == project["name"])
        filename = f"{plugin[1].replace('-', '_')}-1.2.3-py3-none-any.whl"
        write_wheel(Path(argv[6]) / filename, wheel_files(plugin[1], plugin[2], "1.2.3"))
        calls.append(package)

    monkeypatch.setattr(release.subprocess, "run", pip_wheel)
    output = tmp_path / "output"
    catalog = release.build(source=source, output=output)
    assert len(calls) == 4 and all(not p.exists() for p in calls)
    assert release.verify_publication(output, tag="v1.2.3", commit="a" * 40) == catalog
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == originals
    assert "Source/pip" in (output / release.NOTES).read_text()
    with pytest.raises(ValueError, match="empty"):
        release.build(source=source, output=output)


@pytest.mark.parametrize("plugin_id", ["pm", "position-advice", "feishu", "wechat"])
@pytest.mark.parametrize("damage", ["dependency", "extra"])
def test_build_rejects_source_dependency_or_extras_changes(tmp_path, monkeypatch, plugin_id, damage):
    source = tmp_path / "source"
    source.mkdir()
    (source / "VERSION").write_text("1.2.3")
    plugin = next(p for p in release.OFFICIAL_PLUGINS if p[0] == plugin_id)
    shutil.copytree(ROOT / "plugins" / plugin[3], source / "plugins" / plugin[3])
    path = source / "plugins" / plugin[3] / "pyproject.toml"
    text = path.read_text()
    if damage == "dependency":
        dependencies = '["lark-oapi"]' if plugin_id == "feishu" else '[]'
        text = text.replace(f"dependencies = {dependencies}", 'dependencies = ["pandas"]')
    else:
        text += '\n[project.optional-dependencies]\nextra = ["pandas"]\n'
    path.write_text(text)
    monkeypatch.setattr(release, "OFFICIAL_PLUGINS", (plugin,))
    monkeypatch.setattr(release.subprocess, "check_output", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: pytest.fail("invalid source must fail before build"))
    with pytest.raises(ValueError, match="source metadata differs"):
        release.build(source=source, output=tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_build_failure_does_not_emit_partial_publication(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "VERSION").write_text("1.2.3")
    shutil.copytree(ROOT / "plugins", source / "plugins")
    monkeypatch.setattr(release.subprocess, "check_output", lambda *a, **k: "a" * 40)
    temporary = []

    def fail(argv, **kwargs):
        temporary.append(Path(argv[-1]))
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(release.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        release.build(source=source, output=tmp_path / "output")
    assert not (tmp_path / "output").exists()
    assert all(not p.exists() for p in temporary)


def test_cli_verify_local_assets_only(tmp_path):
    version = (ROOT / "VERSION").read_text().strip()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    catalog = publication(tmp_path, version, commit)
    command = [sys.executable, str(ROOT / "scripts/build_plugin_wheels.py"), "--verify-only", "--output", str(tmp_path)]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert catalog["commit"] in result.stdout
    (tmp_path / catalog["plugins"][0]["asset"]).unlink()
    assert subprocess.run(command, cwd=tmp_path, capture_output=True).returncode != 0


def test_release_and_pr_qualification_preserve_existing_gates():
    import yaml
    load = lambda name: yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
    workflow = load("guardrails.yml")
    steps = workflow["jobs"]["checks"]["steps"]
    assert any('scripts/build_plugin_wheels.py --output "$RUNNER_TEMP/plugin-wheels"' in s.get("run", "") for s in steps)
    assert workflow["jobs"]["guardrails"]["needs"] == ["plan", "checks", "regression"]
    assert len(workflow["jobs"]["regression"]["strategy"]["matrix"]["shard"]) == 8
    steps = load("_release-reusable.yml")["jobs"]["release"]["steps"]
    publish = next(i for i, s in enumerate(steps) if s.get("id") == "publish")
    assert any("build_plugin_wheels.py --output plugin-wheels" in s.get("run", "") for s in steps[:publish])
    assert any("--verify-only" in s.get("run", "") for s in steps[:publish])
    assert "plugin-wheels/*" in steps[publish]["with"]["files"]
    assert "readback" in steps[-1]["run"]
