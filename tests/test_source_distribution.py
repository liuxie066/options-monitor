"""Product archives exercise Git export rules and the real source upgrade adapter."""
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts import build_runtime_bundle
from src.application import service_upgrade

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_FILES = {
    "om", "om-agent", "VERSION", "LICENSE", "README.md", "CHANGELOG.md",
    "CONFIGS.md", "CONFIGURATION_GUIDE.md", "DEPLOY.md", "RUNBOOK.md", "SECURITY.md",
    "pyproject.toml", "requirements.txt", "constraints.txt",
    "src/application/example.py", "domain/domain/example.py", "om_strategy_core/_build.py",
    "configs/system.json", "configs/examples/config.yaml.example",
    "contracts/portfolio-management/v1.openapi.json",
    "deploy/logrotate/options-monitor.conf.in", "services/systemd/example.service",
    "requirements/runtime.txt", "requirements/server.txt", "requirements/longbridge.txt",
    "constraints/runtime.txt", "constraints/server.txt", "constraints/runtime-bundle.txt",
    "plugins/feishu-channel/pyproject.toml", "plugins/feishu-channel/README.md",
    "plugins/feishu-channel/src/om_feishu_channel/__init__.py",
    "docs/INSTALL.md", "docs/TOOL_REFERENCE.md", "docs/future_user_guide.md",
    "docs/DECISION_HISTORY.md", "docs/DECISION_HISTORY_PRD.md",
    "docs/architecture/data-storage-runtime-source-inventory.v1.json",
    "scripts/install.sh", "scripts/install_agent_plugin.sh", "scripts/python_runtime.sh",
    "scripts/auto_close_schedule.sh", "scripts/release_check.py",
}
DEVELOPER_FILES = {
    ".gitattributes", ".gitignore", ".github/workflows/future.yml", ".githooks/pre-commit",
    ".devflow/scope.md", ".agents/skills/private/SKILL.md", "AGENTS.md", "CLAUDE.md",
    "Makefile", "LESSONS.md", "future_developer_tool.py", "future_tools/example.py",
    "reports/example.json", "agent-runtime/example.py", ".tick-tools/example.py",
    "memory/decisions/example.md", "codex/example.md", "release/coverage/example.json",
    "tests/test_example.py", "plugins/feishu-channel/tests/test_example.py",
    "src/nested/AGENTS.md", "src/CLAUDE.md", "src/.agents/skills/example.md",
    "src/__pycache__/example.pyc", "requirements/dev.txt", "constraints/dev.txt",
    "scripts/release_acceptance.py", "scripts/release_environment.py", "scripts/release_preflight.sh",
    "scripts/build_runtime_bundle.py", "scripts/future_developer_tool.py", "scripts/nested/install.sh",
    "docs/AGENT_WIKI.md", "docs/CODE_HEALTH_REVIEW.md", "docs/CODE_ENTROPY_REVIEW.md",
    "docs/RELEASE_PROCESS.md", "docs/GUARDRAILS.md", "docs/DEPENDENCY_GRAPH.md",
    "docs/dependency_graph.mmd", "docs/reviews/example.md", "docs/reviews/nested/example.md",
    "docs/plans/example.md", "docs/gateflow/example/state.json",
}


def source_fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    for name in PRODUCT_FILES | DEVELOPER_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    shutil.copyfile(ROOT / ".gitattributes", source / ".gitattributes")
    (source / "VERSION").write_text("1.2.3\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    tree = subprocess.check_output(["git", "-C", str(source), "write-tree"], text=True).strip()
    return source, tree


def file_inventory(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_source_archive_exports_only_product_files(tmp_path):
    source, tree = source_fixture(tmp_path)
    payload = subprocess.check_output(["git", "-C", str(source), "archive", "--format=tar", tree])
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        assert {member.name for member in archive if member.isfile()} == PRODUCT_FILES


def test_runtime_source_copy_respects_product_export_policy(tmp_path):
    source, _ = source_fixture(tmp_path)
    target = tmp_path / "runtime"
    build_runtime_bundle.copy_source(source, target)
    # Plugins ship separately as wheels; release_check is used by source upgrades only.
    expected = {name for name in PRODUCT_FILES
                if not name.startswith("plugins/") and name != "scripts/release_check.py"}
    assert file_inventory(target) == expected


def test_git_cache_upgrade_materializes_only_product_files(tmp_path):
    source, tree = source_fixture(tmp_path)
    tag = "v1.2.3"
    subprocess.run(["git", "-C", str(source), "update-ref", f"refs/tags/{tag}", tree], check=True)
    target = tmp_path / "releases" / tag
    cache = tmp_path / "cache"
    operations = []
    calls = []

    def local_commands(command, **kwargs):
        calls.append(command)
        if command[:2] == ["git", "clone"]:
            assert command[2:6] == ["--mirror", "--origin", "origin", str(source)]
        elif command[0] == "git":
            assert command[1].startswith(f"--git-dir={cache}/") and command[2] == "archive"
            assert command[-1] == tag
        else:
            assert command[:2] == ["tar", "-xf"]
        return subprocess.run(command, **kwargs)

    result = service_upgrade._materialize_release_from_git_cache(
        remote_url=str(source), tag=tag, target_dir=target, cache_root=cache,
        run_cmd=local_commands, operations=operations,
    )
    assert result["method"] == "git_cache_archive" and result["cache_initialized"] is True
    assert len(calls) == 3
    assert file_inventory(target) == PRODUCT_FILES


@pytest.fixture(scope="module")
def extracted_product(tmp_path_factory):
    root = tmp_path_factory.mktemp("om-source-archive")
    archive = root / "source.tar.gz"
    # Snapshot current product sources in disposable Git objects/index, preserving the real index.
    objects = root / "objects"
    objects.mkdir()
    source_objects = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-path", "objects"],
        text=True,
    ).strip()
    env = {**os.environ, "GIT_INDEX_FILE": str(root / "index"),
           "GIT_OBJECT_DIRECTORY": str(objects), "GIT_ALTERNATE_OBJECT_DIRECTORIES": source_objects}
    subprocess.run(["git", "-C", str(ROOT), "read-tree", "HEAD"], env=env, check=True)
    subprocess.run(["git", "-C", str(ROOT), "add", "--update"], env=env, check=True)
    # Like dirty runtime-build QA, include new product inputs. Git archive's
    # export-ignore contract also applies to every parent directory.
    untracked = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "--others", "--exclude-standard", "-z"], env=env,
    ).decode().split("\0")
    paths = {path for name in untracked if name for path in (Path(name), *Path(name).parents)}
    paths.discard(Path("."))
    attributes = subprocess.check_output(
        ["git", "-C", str(ROOT), "check-attr", "--cached", "-z", "--stdin", "export-ignore"],
        env=env, input=("\0".join(sorted(path.as_posix() for path in paths)) + "\0").encode() if paths else b"",
    ).decode().split("\0")
    export_ignored = {attributes[i] for i in range(0, len(attributes) - 1, 3)
                      if attributes[i + 2] == "set"}
    additions = [name for name in untracked if name and not any(
        path.as_posix() in export_ignored for path in (Path(name), *Path(name).parents)
    )]
    if additions:
        subprocess.run(["git", "-C", str(ROOT), "add", "--", *additions], env=env, check=True)
    tree = subprocess.check_output(["git", "-C", str(ROOT), "write-tree"], env=env, text=True).strip()
    subprocess.run([
        "git", "-C", str(ROOT), "archive", "--format=tar.gz", "--output", str(archive), tree,
    ], env=env, check=True)
    extracted = root / "extracted"
    extracted.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(extracted, filter="data")
    return extracted


def test_extracted_product_dependency_inputs_are_complete(extracted_product):
    root = extracted_product
    for manifest in [root / "requirements.txt", root / "constraints.txt",
                     *root.glob("requirements/*.txt"), *root.glob("constraints/*.txt")]:
        for row in manifest.read_text().splitlines():
            if row.startswith(("-r ", "-c ")):
                assert (manifest.parent / row[3:].strip()).is_file(), (manifest, row)
    projects = list(root.glob("plugins/*/pyproject.toml"))
    assert len(projects) == 8
    for project in projects:
        assert (project.parent / "README.md").is_file()
        assert list((project.parent / "src").rglob("*.py"))
    assert (root / "docs/architecture/data-storage-runtime-source-inventory.v1.json").is_file()
    assert not any(name in file_inventory(root) for name in DEVELOPER_FILES)
    for directory in ("reports", "agent-runtime", ".tick-tools", ".devflow",
                      "docs/gateflow", "docs/reviews", "docs/plans"):
        assert not (root / directory).exists(), directory


def local_document_link_issues(root):
    issues = []
    for document in sorted(root.rglob("*.md")):
        for href in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", document.read_text()):
            if "://" not in href and not (document.parent / href).is_file():
                issues.append((document.relative_to(root).as_posix(), href))
    return issues


def test_extracted_product_document_entrypoints_keep_local_links_resolvable(extracted_product):
    assert local_document_link_issues(extracted_product) == []


def test_runtime_product_keeps_user_guides_and_local_links(tmp_path):
    target = tmp_path / "runtime"
    build_runtime_bundle.copy_source(ROOT, target)
    assert (target / "docs/DECISION_HISTORY.md").is_file()
    assert (target / "docs/DECISION_HISTORY_PRD.md").is_file()
    assert local_document_link_issues(target) == []


def test_extracted_product_runs_public_launchers_and_upgrade_metadata_check(extracted_product, tmp_path):
    root = extracted_product
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(OM_PYTHON=sys.executable, HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    commands = [
        [str(root / "om"), "--help"],
        [str(root / "om-agent"), "spec"],
        [sys.executable, "-I", str(root / "scripts/release_check.py"),
         "--tag", f"v{(root / 'VERSION').read_text().strip()}"],
        [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
         "import src.application.bot.host; import src.application.bot.runtime", str(root)],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, (command, result.stdout, result.stderr)
