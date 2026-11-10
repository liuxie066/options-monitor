"""Official release add-ons: inspect, preview, install and remove without pip.

Only the fixed official wheels from the exact installed OM release are
accepted. No configuration, service or broker operation belongs to this owner.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

from src.application import runtime_bundle
from src.application.plugin_runtime import (
    PLUGINS, code_root, expected_plugin_version, plugin_store, read_managed_plugin,
    read_source_plugin, source_plugin_store,
)
from src.infrastructure.private_storage import ensure_private_directory, exclusive_private_file_lock


def _root(repo_root: Path | None) -> Path:
    return Path(repo_root or code_root()).resolve()


def _version(root: Path) -> str:
    version = (root / "VERSION").read_text().strip()
    if not runtime_bundle.TAG_RE.fullmatch("v" + version) or "-" in version:
        raise ValueError("plugins require a stable OM release version")
    return version


def _environment_version(plugin_id: str) -> str | None:
    try:
        return importlib.metadata.version(PLUGINS[plugin_id]["distribution"])
    except importlib.metadata.PackageNotFoundError:
        return None


def list_plugins(*, repo_root: Path | None = None) -> dict:
    root = _root(repo_root)
    rows = []
    for key, item in PLUGINS.items():
        try:
            receipt = read_managed_plugin(root, key)
            source_receipt = read_source_plugin(root, key) if receipt is None else None
            environment_version = source_receipt["version"] if source_receipt else _environment_version(key)
            expected = _version(root) if receipt else expected_plugin_version(key, repo_root=root)
            installed_version = receipt["version"] if receipt else environment_version
            rows.append({"id": key, "distribution": item["distribution"], "installed": installed_version is not None,
                         "version": installed_version, "compatible": installed_version == expected if installed_version else None,
                         "source": "managed" if receipt else "release_source" if source_receipt else "python_environment" if environment_version else None,
                         "status": "installed" if installed_version == expected else "incompatible" if installed_version else "not_installed"})
        except (ValueError, OSError, KeyError) as exc:
            rows.append({"id": key, "distribution": item["distribution"], "installed": None,
                         "compatible": False, "status": "invalid", "reason": str(exc)})
    return {"ok": all(row["status"] not in {"invalid", "incompatible"} for row in rows),
            "om_version": _version(root), "repo_root": str(root), "plugin_directory": str(plugin_store(root)),
            "plugins": rows, "note": "安装不等于启用；包选择属于此 OM 代码实例，配置开关保持原样"}


def _published_asset(release: dict, name: str) -> tuple[str, str, int]:
    assets = [item for item in release.get("assets", []) if item.get("name") == name]
    if len(assets) != 1:
        raise ValueError(f"official plugin asset unavailable: {name}")
    item = assets[0]
    expected_url = f"https://github.com/{runtime_bundle.REPOSITORY}/releases/download/{release['tag_name']}/{name}"
    digest = item.get("digest", "")
    if (item.get("state") != "uploaded" or item.get("browser_download_url") != expected_url
            or not isinstance(digest, str) or not digest.startswith("sha256:")
            or not runtime_bundle.SHA_RE.fullmatch(digest[7:])
            or type(item.get("size")) is not int or not 0 < item["size"] <= 10 * 1024 * 1024):
        raise ValueError(f"official plugin asset identity/digest invalid: {name}")
    return expected_url, digest[7:], item["size"]


def _catalog(root: Path, temporary: Path) -> tuple[dict, dict]:
    from src.application.plugin_release import validate_catalog

    version = _version(root)
    release = runtime_bundle.resolve_release("v" + version)
    url, digest, size = _published_asset(release, "om-plugins.json")
    path = runtime_bundle.download_verified(url, temporary / "om-plugins.json", digest)
    if path.stat().st_size != size:
        raise ValueError("plugin catalog size mismatch")
    # An older release orchestrates the upgrade and installs its managed
    # plugins from the target release's catalog, which may list official
    # plugins that release does not know yet. Validate the known entries and
    # ignore the rest instead of blocking the upgrade.
    catalog = validate_catalog(json.loads(path.read_text()), om_version=version, allow_additional=True)
    if (root / "om.runtime.json").exists():
        manifest = runtime_bundle.load_manifest(root)
        if manifest["commit"] != catalog["commit"]:
            raise ValueError("plugin catalog commit differs from installed runtime bundle")
    return catalog, release


def _extract_wheel(wheel: Path, site: Path, *, module: str) -> dict[str, str]:
    files = {}
    with zipfile.ZipFile(wheel) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(item.file_size for item in entries) > 20 * 1024 * 1024:
            raise ValueError("plugin wheel exceeds pure-Python package limits")
        for item in entries:
            name = item.filename
            if item.is_dir():
                continue
            parts = name.split("/")
            if (name in files or "\\" in name or any(part in {"", ".", ".."} for part in parts)
                    or (parts[0] != module and not parts[0].endswith(".dist-info"))
                    or (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("plugin wheel contains an unsafe or unrelated file")
            data = archive.read(item)
            destination = site / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            files[name] = hashlib.sha256(data).hexdigest()
    if f"{module}/__init__.py" not in files:
        raise ValueError("plugin wheel package entry missing")
    return files


def _prepare_feishu_sdk(root: Path, staged: Path, *, python_executable: Path | None = None) -> dict[str, str]:
    python = str(python_executable or sys.executable)
    probe = (
        "import importlib.util,sys\n"
        "sys.dont_write_bytecode=True\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "if importlib.util.find_spec('lark_oapi') is None: sys.exit(42)\n"
        "from src.application.plugin_runtime import validate_feishu_sdk\n"
        "validate_feishu_sdk()\n"
    )
    try:
        result = subprocess.run([python, "-I", "-c", probe, str(root)], capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return {}
        if result.returncode != 42:
            raise ValueError("existing Feishu SDK is incompatible; installation was not changed")
        if (root / "om.runtime.json").exists():
            raise ValueError("bundled Feishu SDK is unavailable; repair the runtime bundle before installing")
        constraints = root / "constraints/runtime-bundle.txt"
        if not constraints.is_file():
            raise ValueError("Feishu SDK installation requires the release dependency constraints")
        dependencies = staged / "dependencies"
        env = dict(os.environ, PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        for name in list(env):
            if name in {"PYTHONPATH", "PYTHONHOME"} or name.startswith(("PIP_", "UV_")):
                env.pop(name)
        uv = shutil.which("uv")
        installer = ([uv, "--no-config", "--no-cache", "--no-python-downloads", "pip", "install", "--python", python,
                      "--only-binary", ":all:", "--link-mode", "copy"] if uv else
                     [python, "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
                      "--no-input", "--no-compile", "--only-binary=:all:"])
        result = subprocess.run(
            [*installer, "--target", str(dependencies),
             "-c", str(constraints), "lark-oapi"],
            capture_output=True, text=True, timeout=300, env=env,
        )
        if result.returncode:
            raise ValueError("Feishu SDK dependency installation failed; installation was not changed")
        files = {}
        for path in dependencies.rglob("*"):
            if path.is_symlink():
                raise ValueError("Feishu SDK dependencies must not contain links")
            if path.is_file():
                files[path.relative_to(dependencies).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if "lark_oapi/__init__.py" not in files:
            raise ValueError("Feishu SDK dependency installation is incomplete")
        return files
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Feishu SDK preparation timed out; installation was not changed") from exc


def _validate_import(root: Path, staged: Path, plugin_id: str, *, python_executable: Path | None = None) -> None:
    # A fresh process verifies the actual adapter without calls to PM/OpenD.
    module = PLUGINS[plugin_id]["module"]
    script = (
        "import importlib.util,importlib.metadata,sys\n"
        "from pathlib import Path\n"
        "root,site,module,distribution,check_environment=sys.argv[1:]\n"
        "if check_environment=='yes':\n"
        "    try: importlib.metadata.distribution(distribution)\n"
        "    except importlib.metadata.PackageNotFoundError: pass\n"
        "    else: raise RuntimeError('target Python environment already owns this plugin')\n"
        "sys.dont_write_bytecode=True\n"
        "sys.path.insert(0,root)\n"
        "dependencies=Path(site).parent/'dependencies'\n"
        "if dependencies.is_dir(): sys.path.append(str(dependencies))\n"
        "package=Path(site)/module\n"
        "spec=importlib.util.spec_from_file_location(module,package/'__init__.py',submodule_search_locations=[str(package)])\n"
        "obj=importlib.util.module_from_spec(spec);sys.modules[module]=obj;spec.loader.exec_module(obj)\n"
        "from src.application.plugin_runtime import PLUGINS,validate_plugin_import\n"
        "validate_plugin_import(next(key for key,item in PLUGINS.items() if item['module']==module))\n"
    )
    try:
        result = subprocess.run([str(python_executable or sys.executable), "-I", "-c", script, str(root), str(staged / "site"), module,
                                 PLUGINS[plugin_id]["distribution"], "yes" if python_executable else "no"],
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("plugin import validation timed out; installation was not changed") from exc
    if result.returncode:
        guidance = ("; install requirements/longbridge.txt in the source runtime first; immutable bundles are not modified"
                    if plugin_id == "broker-longbridge" else "")
        raise ValueError("plugin import validation failed; installation was not changed" + guidance)


def install_plugin(plugin_id: str, *, repo_root: Path | None = None, confirm: bool = False,
                   python_executable: Path | None = None) -> dict:
    from src.application.plugin_release import validate_wheel

    if plugin_id not in PLUGINS:
        raise ValueError("unknown official plugin")
    root = _root(repo_root)
    version = _version(root)
    receipt = read_managed_plugin(root, plugin_id)
    if receipt:
        return {"ok": True, "status": "already_installed", "changed": False, "plugin": plugin_id,
                "om_version": version, "directory": str(plugin_store(root) / plugin_id)}
    # Upgrade controllers can run in a different environment. In that case the
    # staged subprocess checks target ownership instead of inspecting this one.
    if read_source_plugin(root, plugin_id) is not None:
        raise ValueError("plugin belongs to this release's source installation; use om plugins uninstall before replacing it")
    environment_version = _environment_version(plugin_id) if python_executable is None else None
    if environment_version is not None:
        raise ValueError("plugin is installed in Python environment; manage it with that environment's pip before using om plugins")
    with tempfile.TemporaryDirectory(prefix="om-plugin-download-") as temporary:
        work = Path(temporary)
        catalog, release = _catalog(root, work)
        entry = next(item for item in catalog["plugins"] if item["id"] == plugin_id)
        preview = {"ok": True, "status": "preview", "changed": False, "plugin": plugin_id,
                   "version": entry["version"], "om_version": version, "release_tag": catalog["tag"],
                   "asset": entry["asset"], "sha256": entry["sha256"],
                   "directory": str(plugin_store(root) / plugin_id), "enabled_changed": False,
                   "note": "安装不启用功能，不重启服务；确认后供新进程加载"}
        if plugin_id == "feishu":
            preview["dependencies"] = ["lark-oapi"]
            preview["note"] += "；复用可用飞书 SDK，源码版缺 SDK 时按发布约束下载至插件专属目录"
        elif plugin_id == "broker-longbridge":
            preview["note"] += "；须预先安装 requirements/longbridge.txt 的 SDK，不修改运行包内置环境"
        if not confirm:
            return preview
        url, digest, size = _published_asset(release, entry["asset"])
        if digest != entry["sha256"] or size != entry["size"]:
            raise ValueError("plugin catalog and published asset differ")
        wheel = runtime_bundle.download_verified(url, work / entry["asset"], digest)
        validate_wheel(wheel, entry)
        store = plugin_store(root)
        ensure_private_directory(store.parent)
        ensure_private_directory(store)
        with exclusive_private_file_lock(store / ".lock"):
            existing = read_managed_plugin(root, plugin_id)
            if existing:
                return {**preview, "status": "already_installed"}
            target = store / plugin_id
            with tempfile.TemporaryDirectory(prefix=f".{plugin_id}-", dir=store) as staging:
                staged = Path(staging) / "package"
                site = staged / "site"
                site.mkdir(parents=True)
                files = _extract_wheel(work / entry["asset"], site, module=entry["module"])
                dependency_files = (_prepare_feishu_sdk(root, staged, python_executable=python_executable)
                                    if plugin_id == "feishu" else {})
                record = {"schema_version": 1, "plugin_id": plugin_id, "repo_root": str(root),
                          "om_version": version, "version": entry["version"], "distribution": entry["distribution"],
                          "module": entry["module"], "wheel_sha256": entry["sha256"], "files": files,
                          "release_tag": catalog["tag"], "commit": catalog["commit"],
                          "installed_at_utc": datetime.now(timezone.utc).isoformat()}
                if dependency_files:
                    record["dependency_files"] = dependency_files
                (staged / "receipt.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
                _validate_import(root, staged, plugin_id, python_executable=python_executable)
                staged.rename(target)
            read_managed_plugin(root, plugin_id)
        return {**preview, "status": "installed", "changed": True, "restart_required": True}


def _check_market_removal(root: Path, plugin_id: str) -> None:
    if PLUGINS[plugin_id].get("kind") != "market":
        return
    valid = []
    for row in list_plugins(repo_root=root)["plugins"]:
        candidate = row["id"]
        if PLUGINS[candidate].get("kind") != "market" or row["status"] != "installed":
            continue
        try:
            if read_managed_plugin(root, candidate):
                _validate_import(root, plugin_store(root) / candidate, candidate)
            valid.append(candidate)
        except ValueError:
            continue
    if valid == [plugin_id]:
        raise ValueError("cannot remove the last valid market provider; install another market plugin first")


def uninstall_plugin(plugin_id: str, *, repo_root: Path | None = None, confirm: bool = False) -> dict:
    if plugin_id not in PLUGINS:
        raise ValueError("unknown official plugin")
    root = _root(repo_root)
    store = plugin_store(root)
    target = store / plugin_id
    # An explicit uninstall also repairs a damaged managed installation; never
    # follow a link or touch a package owned by the interpreter environment.
    if store.parent.is_symlink() or store.is_symlink() or target.is_symlink():
        raise ValueError("plugin directory must not be a symlink")
    source_target = source_plugin_store(root) / plugin_id
    if source_target.exists() and not target.exists():
        if (source_plugin_store(root).is_symlink() or source_target.is_symlink()
                or not source_target.is_dir() or source_target.resolve().parent != source_plugin_store(root).resolve()):
            raise ValueError("source plugin directory must be a direct non-symlink child of the release plugin store")
        _check_market_removal(root, plugin_id)
        preview = {"ok": True, "status": "preview", "changed": False, "plugin": plugin_id,
                   "directory": str(source_target), "enabled_changed": False,
                   "note": "停用此 release 的源码插件；配置与依赖保留，已有进程需单独重启"}
        if not confirm:
            return preview
        with exclusive_private_file_lock(source_plugin_store(root) / ".lock"):
            _check_market_removal(root, plugin_id)
            source_target.rename(source_target.with_name(f".removed-{plugin_id}-{uuid.uuid4().hex}"))
        return {**preview, "status": "removed", "changed": True, "restart_required": True}
    if _environment_version(plugin_id) is not None:
        raise ValueError("plugin also belongs to Python environment; use that environment's pip uninstall first")
    if not target.exists():
        return {"ok": True, "status": "not_installed", "changed": False, "plugin": plugin_id}
    if not target.is_dir():
        raise ValueError("plugin path is not a directory; installation was not changed")
    _check_market_removal(root, plugin_id)
    preview = {"ok": True, "status": "preview", "changed": False, "plugin": plugin_id,
               "directory": str(target), "enabled_changed": False,
               "note": "仅移除插件文件；配置、账本及历史报告保留，已有进程需单独重启"}
    if not confirm:
        return preview
    with exclusive_private_file_lock(store / ".lock"):
        _check_market_removal(root, plugin_id)
        if target.is_dir() and not target.is_symlink():
            removed = store / f".removed-{plugin_id}-{uuid.uuid4().hex}"
            # Rename is the uninstall commit point; cleanup cannot leave a
            # partially deleted installation visible to new processes.
            target.rename(removed)
            try:
                shutil.rmtree(removed)
            except OSError:
                return {**preview, "status": "uninstalled", "changed": True, "restart_required": True,
                        "cleanup_pending": True, "retained_directory": str(removed),
                        "note": f"插件已移除；部分文件未清理：{removed}。配置和历史保留，已有进程需单独重启"}
    return {**preview, "status": "uninstalled", "changed": True, "restart_required": True}
