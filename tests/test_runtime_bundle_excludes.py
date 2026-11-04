"""Keep maintainer resources out of the runtime artifact's file inventory."""
import io
import json
import re
import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import build_runtime_bundle as builder

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPTS = {
    "scripts/install.sh",
    "scripts/install_agent_plugin.sh",
    "scripts/python_runtime.sh",
}
INTERNAL_DOCS = {
    "docs/CODE_HEALTH_REVIEW.md",
    "docs/DECISION_HISTORY.md",
    "docs/DECISION_HISTORY_PRD.md",
    "docs/AGENT_GETTING_STARTED.md",
    "docs/AGENT_INTEGRATION.md",
    "docs/AGENT_WIKI.md",
}
PRIVATE_FILES = INTERNAL_DOCS | {
    "AGENTS.md", "CLAUDE.md", ".agents/skills/private/SKILL.md",
    "src/nested/AGENTS.md", "docs/CLAUDE.md", "src/.agents/private/SKILL.md",
    "scripts/code_health_review.py", "scripts/code_health_issues.py",
    "scripts/code_health_repair.py", "scripts/benchmark_future.py",
    "scripts/bot_p1_eval.py", "scripts/build_runtime_bundle.py",
    "scripts/build_future.py", "scripts/release_future.py",
    "scripts/generate_dependency_graph.py", "scripts/guardrails_check.py",
    "scripts/retired_column_scan.py", "scripts/incident_cleanup_preview.py",
    "scripts/setup_git_hooks.sh", "scripts/__init__.py",
    "scripts/runtime_bundle.lock.json", "scripts/future_maintenance.sh",
    "scripts/nested/install.sh", "tests/test_internal.py",
    "release/v1.2.3/coverage.json", "memory/decisions/internal.md",
    "codex/internal.md", ".devflow/scope.md",
    "docs/reviews/internal.md", "docs/reviews/nested/internal.md",
}
PRODUCT_FILES = INSTALL_SCRIPTS | {
    "om", "om-agent", "VERSION", "README.md", "LICENSE",
    "src/application/example.py", "domain/example.py",
    "docs/INSTALL.md", "docs/STRATEGY_ARCHITECTURE.md",
    "docs/TOOL_REFERENCE.md", "docs/WHEEL_STRATEGY_PRD.md",
    "docs/future_user_guide.md",
}


@pytest.mark.parametrize("tracked", [True, False], ids=["tracked", "untracked"])
def test_runtime_bundle_manifest_excludes_maintainer_files(tmp_path, monkeypatch, tracked):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run([
        "git", "-C", str(source), "-c", "core.hooksPath=/dev/null",
        "-c", "user.name=Bundle Test", "-c", "user.email=bundle-test@example.com",
        "commit", "--allow-empty", "-qm", "test(bundle): initialize source fixture",
    ], check=True)
    for name in PRIVATE_FILES | PRODUCT_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    (source / "VERSION").write_text("1.2.3\n")
    (source / "scripts/runtime_bundle.lock.json").write_bytes(
        (ROOT / "scripts/runtime_bundle.lock.json").read_bytes()
    )
    if tracked:
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)

    # Only interpreter/dependency provisioning is stubbed. Source selection,
    # manifest generation, archive creation and relocated checksum checks are real.
    lock = json.loads((source / "scripts/runtime_bundle.lock.json").read_text())
    dependencies = {name: record["version"] for name, record in lock["dependencies"].items()}
    python_archive = tmp_path / "python.tar.gz"
    with tarfile.open(python_archive, "w:gz") as archive:
        interpreter = tarfile.TarInfo("python/bin/python3")
        content = f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(json.dumps(dependencies))}\n".encode()
        interpreter.size = len(content)
        interpreter.mode = 0o755
        archive.addfile(interpreter, io.BytesIO(content))
    artifact = tmp_path / "dependency"
    artifact.write_text("fixture dependency/license\n")
    python_filename = lock["platforms"]["macos-arm64"]["python"]["filename"]
    monkeypatch.setattr(builder, "current_platform", lambda: "macos-arm64")
    monkeypatch.setattr(
        builder, "cached_download",
        lambda record, cache: python_archive if record["filename"] == python_filename else artifact,
    )

    def provision_dependencies(argv, **kwargs):
        runtime = Path(argv[0]).parents[2]
        (runtime / "lib/python3.12/site-packages").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, stdout="fixture dependency metadata\n")

    monkeypatch.setattr(builder, "run", provision_dependencies)
    monkeypatch.setattr(builder, "validate_runtime", lambda root: {"ok": True})
    output = tmp_path / "output"
    report = builder.build(source=source, output=output, cache=tmp_path / "cache")
    manifest = json.loads((output / (report["archive"] + ".manifest.json")).read_text())
    files = set(manifest["files"])
    with tarfile.open(output / report["archive"]) as archive:
        archived_files = {member.name for member in archive if member.isfile()}

    assert archived_files == files | {builder.MANIFEST}
    assert files.isdisjoint(PRIVATE_FILES), sorted(files & PRIVATE_FILES)
    assert PRODUCT_FILES <= files
    assert {name for name in files if name.startswith("scripts/")} == INSTALL_SCRIPTS
    assert not any(
        name.startswith(("tests/", "release/", "memory/", "codex/", ".devflow/", "docs/reviews/"))
        for name in files
    )


def test_runtime_bundle_source_keeps_readme_user_docs(tmp_path):
    target = tmp_path / "product"
    builder.copy_source(ROOT, target)
    files = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
    readme_docs = set(re.findall(r"\]\((docs/[^)#]+\.md)(?:#[^)]*)?\)", (ROOT / "README.md").read_text()))
    user_docs = readme_docs - INTERNAL_DOCS

    assert user_docs
    assert user_docs | INSTALL_SCRIPTS | {"README.md", "om", "om-agent"} <= files
    assert files.isdisjoint(INTERNAL_DOCS)
    assert {name for name in files if name.startswith("scripts/")} == INSTALL_SCRIPTS
