#!/usr/bin/env python3
"""Build and actually relocate a pinned, complete runtime; never publish it."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from src.application.runtime_bundle import (
    MANIFEST, PYTHON, REPOSITORY, RuntimeBundleError, asset_name, current_platform,
    download_verified, materialize_bundle, safe_extract, sha256, validate_runtime,
)

SOURCE_DIRS = {"src", "domain", "om_strategy_core", "scripts", "configs", "contracts", "services", "requirements", "constraints", "docs", "deploy"}
SOURCE_FILES = {"om", "om-agent", "VERSION", "LICENSE", "CHANGELOG.md", "README.md", "RUNBOOK.md", "DEPLOY.md", "CONFIGS.md", "CONFIGURATION_GUIDE.md", "SECURITY.md", "pyproject.toml", "requirements.txt", "constraints.txt"}


def run(argv, **kwargs):
    return subprocess.run([str(a) for a in argv], check=True, **kwargs)


def cached_download(record: dict, cache: Path) -> Path:
    target = cache / record["filename"]
    if target.is_file() and sha256(target) == record["sha256"]:
        return target
    return download_verified(record["url"], target, record["sha256"])


def copy_source(source: Path, target: Path) -> None:
    # Git is a build tool only. Include selected new files for isolated dirty-build QA.
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=source).decode().split("\0")
    for name in sorted(set(names)):
        if not name or (name not in SOURCE_FILES and name.split("/")[0] not in SOURCE_DIRS):
            continue
        if {"AGENTS.md", "CLAUDE.md", ".agents"}.intersection(Path(name).parts):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_./+@-]+", name) or ".." in Path(name).parts:
            raise RuntimeBundleError(f"source filename cannot be safely bootstrapped: {name}")
        src = source / name
        if src.is_symlink():
            raise RuntimeBundleError(f"source symlink is not a runtime resource: {name}")
        if not src.is_file():
            continue
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def build(*, source: Path, output: Path, cache: Path, require_clean: bool = False) -> dict:
    target_platform = current_platform()
    lock = json.loads((source / "scripts/runtime_bundle.lock.json").read_text())
    platform_lock = lock["platforms"][target_platform]
    version = (source / "VERSION").read_text().strip()
    tag = f"v{version}"
    name = asset_name(tag, target_platform)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=source))
    if require_clean and dirty:
        raise RuntimeBundleError("release build requires clean source")
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    archive = output / name
    python_lock = platform_lock["python"]
    interpreter_archive = cached_download(python_lock, cache)
    artifacts = {n: cached_download(d["artifacts"][target_platform], cache) for n, d in lock["dependencies"].items()}
    with tempfile.TemporaryDirectory(prefix="om-bundle-build-") as temporary:
        staging = Path(temporary)
        raw = staging / "upstream"
        raw.mkdir()
        safe_extract(interpreter_archive, raw, allow_links=True)
        root = staging / "release"
        root.mkdir()
        shutil.copytree(raw / "python", root / "runtime/python", symlinks=False)
        python = root / PYTHON
        (staging / "home").mkdir()
        (staging / "tmp").mkdir()
        env = dict(os.environ, HOME=str(staging / "home"), TMPDIR=str(staging / "tmp"), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "OM_PYTHON", "VIRTUAL_ENV", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
            env.pop(key, None)
        # Futu publishes an sdist. Its build runs offline with pinned build tools.
        build_files = [artifacts[n] for n in lock["build_dependencies"]]
        run([python, "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", *build_files], env=env)
        run([python, "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--no-build-isolation", *artifacts.values()], env=env)
        run([python, "-m", "pip", "--isolated", "check"], env=env)
        copy_source(source, root)
        licenses = root / "licenses/python"
        licenses.mkdir(parents=True)
        for record in lock["python_licenses"]:
            shutil.copy2(cached_download(record, cache), licenses / record["filename"])
        # Console-script shebangs encode staging paths. All supported entrypoints use python -m.
        for path in (root / "runtime/python/bin").iterdir():
            if not re.fullmatch(r"python(?:3(?:\.12)?)?", path.name):
                if path.is_file() or path.is_symlink():
                    path.unlink()
        for path in list(root.rglob("__pycache__")):
            shutil.rmtree(path)
        for path in list(root.rglob("*.pyc")):
            path.unlink()
        # Any package-created file symlinks become ordinary files in the relocatable artifact.
        for path in root.rglob("*"):
            if path.is_symlink():
                if not path.resolve().is_relative_to(root) or not path.is_file():
                    raise RuntimeBundleError(f"unsafe dependency symlink: {path}")
                payload = path.read_bytes()
                mode = path.stat().st_mode
                path.unlink()
                path.write_bytes(payload)
                path.chmod(mode)
        inventory = {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}
        installed = json.loads(subprocess.check_output([str(python), "-c", "import importlib.metadata as m,json;print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()}))"], env=env, text=True))
        expected = {n: d["version"] for n,d in lock["dependencies"].items()}
        normalized = {re.sub(r"[-_.]+", "-", n).lower(): v for n,v in installed.items()}
        if any(normalized.get(re.sub(r"[-_.]+", "-", n).lower()) != v for n,v in expected.items()):
            raise RuntimeBundleError("installed dependency versions differ from lock")
        manifest = {"schema_version": 1, "kind": "options-monitor-runtime", "repository": REPOSITORY,
                    "version": version, "tag": tag, "commit": commit, "source_dirty": dirty,
                    "platform": target_platform, "minimum_system": platform_lock["minimum_system"],
                    "python": {"version": python_lock["version"], "executable": PYTHON,
                               "source_url": python_lock["url"], "sha256": python_lock["sha256"]},
                    "dependencies": installed, "files": inventory,
                    "licenses": [n for n in inventory if re.search(r"(?:license|copying|notice)", n, re.I)],
                    "lock_sha256": sha256(source / "scripts/runtime_bundle.lock.json")}
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        (root / MANIFEST).write_text(manifest_text)
        with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT, compresslevel=6) as tar:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
        original_staging = str(root)
    # The original interpreter and source staging are gone before any qualification execution.
    assert not Path(original_staging).exists()
    with tempfile.TemporaryDirectory(prefix="om-bundle-relocation-") as temporary:
        relocated = Path(temporary) / "搬迁 验证" / "OM Runtime"
        materialize_bundle(archive, relocated, expected_tag=tag, expected_platform=target_platform)
        receipt = validate_runtime(relocated)
    digest = sha256(archive)
    archive.with_name(name + ".sha256").write_text(f"{digest}  {name}\n")
    archive.with_name(name + ".manifest.json").write_text(manifest_text)
    receipt.update({"archive": name, "sha256": digest, "commit": commit, "source_dirty": dirty,
                    "original_staging_removed": True, "relocated_path_contains_chinese_and_spaces": True,
                    "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "mac_ver": platform.mac_ver()[0]},
                    "qualification_limits": ["Only this host was executed; minimum OS is an artifact compatibility floor, not an executed receipt.", "No services, upgrades, OpenD, broker calls or notifications exercised."]})
    archive.with_name(name + ".qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(source=args.source.resolve(), output=args.output.resolve(), cache=args.cache.resolve(), require_clean=args.require_clean), indent=2))


if __name__ == "__main__":
    main()
