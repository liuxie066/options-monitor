from __future__ import annotations

import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from scripts import release_environment as check
from src.application.plugin_runtime import PLUGINS


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installed_wheel(tmp_path, monkeypatch):
    repo, prefix = tmp_path / "checkout", tmp_path / "python"
    item = {"source": "portfolio-management", "module": "om_environment_fixture",
            "distribution": "om-environment-fixture-plugin"}
    project = repo / "plugins" / item["source"]
    source = project / "src" / item["module"]
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("# same source\n")
    (source / "feature.py").write_text("VALUE = 42\n")
    (project / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    site = prefix / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    metadata = "om_environment_fixture_plugin-0.1.0.dist-info"
    # Extract an actual wheel layout, including distribution metadata. Package
    # discovery and metadata lookup below are real, without importing its code.
    wheel = tmp_path / "om_environment_fixture_plugin-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in source.glob("*.py"):
            archive.write(path, f"{item['module']}/{path.name}")
        archive.writestr(f"{metadata}/METADATA", "Metadata-Version: 2.1\nName: om-environment-fixture-plugin\nVersion: 0.1.0\n")
        archive.writestr(f"{metadata}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{metadata}/RECORD", "")
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(check, "PLUGINS", {"pm": item})
    return repo, source, site / item["module"], site / metadata, item


def test_correct_installed_wheel_and_current_editable_pass(installed_wheel, monkeypatch):
    repo, source, loaded, _, item = installed_wheel
    row = check._check_plugin(repo, "pm", item)
    assert row["ok"] and row["source_file_count"] == 2
    assert row["loaded_path"] == str(loaded)
    assert row["source_digest"] == row["loaded_digest"]
    monkeypatch.syspath_prepend(str(source.parent))
    importlib.invalidate_caches()
    editable = check._check_plugin(repo, "pm", item)
    assert editable["ok"] and editable["loaded_path"] == str(source)


@pytest.mark.parametrize("mutation,diagnostic", [
    ("changed", "changed Python files: feature.py"),
    ("missing", "missing Python files: feature.py"),
    ("unexpected", "unexpected Python files: deleted_module.py"),
    ("version", "distribution version"),
    ("distribution", "PackageNotFoundError"),
    ("package", "exactly one directory"),
    ("foreign", "outside selected Python prefix"),
])
def test_stale_or_wrong_environment_is_rejected(installed_wheel, tmp_path, monkeypatch, mutation, diagnostic):
    repo, source, loaded, metadata, item = installed_wheel
    if mutation == "changed":
        (loaded / "feature.py").write_text("VALUE = 0\n")
    elif mutation == "missing":
        (loaded / "feature.py").unlink()
    elif mutation == "unexpected":
        (loaded / "deleted_module.py").write_text("OLD = True\n")
    elif mutation == "version":
        (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: om-environment-fixture-plugin\nVersion: 0.2.0\n")
    elif mutation == "distribution":
        shutil.rmtree(metadata)
    elif mutation == "package":
        shutil.rmtree(loaded)
    else:
        foreign = tmp_path / "other-checkout" / item["module"]
        shutil.copytree(source, foreign)
        monkeypatch.syspath_prepend(str(foreign.parent))
    importlib.invalidate_caches()
    row = check._check_plugin(repo, "pm", item)
    assert not row["ok"]
    assert diagnostic in " ".join(row["errors"])
    assert "Reinstall" in row["remediation"]


def test_all_canonical_projects_follow_supported_source_layout():
    for item in PLUGINS.values():
        package = ROOT / "plugins" / item["source"] / "src" / item["module"]
        assert (package / "__init__.py").is_file(), package


def test_cli_binds_actual_head_and_selected_python_from_other_cwd(tmp_path):
    proc = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/release_environment.py")],
                          cwd=tmp_path, capture_output=True, text=True, check=False)
    result = json.loads(proc.stdout)
    assert proc.returncode == 0, result
    assert result["ok"] and len(result["head"]) == 40
    assert result["python"] == sys.executable
    assert result["prefix"] == sys.prefix
    assert {row["id"] for row in result["plugins"]} == set(PLUGINS)
    assert all(row["source_digest"] == row["loaded_digest"] for row in result["plugins"])


def test_cli_failure_is_json_and_nonzero(tmp_path, capsys):
    assert check.main(["--repo-root", str(tmp_path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert not payload["ok"] and payload["errors"]
    assert all(not row["ok"] for row in payload["plugins"])
