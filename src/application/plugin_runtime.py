"""Load the fixed official add-ons outside the immutable OM release.

This module is stdlib-only: registering the finder reads no plugin state and
imports no plugin. Install selection belongs to the code release, not an account.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import sys

PLUGINS = {
    "pm": {"distribution": "om-pm-plugin", "module": "om_pm_plugin", "source": "portfolio-management"},
    "position-advice": {"distribution": "om-position-advice-plugin", "module": "om_position_advice_plugin", "source": "position-advice"},
    "feishu": {"distribution": "om-feishu-channel-plugin", "module": "om_feishu_channel", "source": "feishu-channel"},
    "wechat": {"distribution": "om-wechat-channel-plugin", "module": "om_wechat_channel", "source": "wechat-channel"},
}

# A running process must not rebind to another release when current is switched.
_CODE_ROOT = Path(__file__).resolve().parents[2]


def code_root() -> Path:
    return _CODE_ROOT


def plugin_store(repo_root: Path) -> Path:
    root = Path(repo_root).resolve()
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:24]
    return root.parent / ".om-plugins" / identity


def _validate_inventory(site: Path, files: dict) -> None:
    if site.is_symlink() or not site.is_dir() or not isinstance(files, dict) or not files:
        raise ValueError("plugin package directory or inventory missing")
    for name, digest in files.items():
        if (not isinstance(name, str) or "\\" in name or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("plugin file inventory mismatch")
        path = site / name
        if (not path.resolve().is_relative_to(site.resolve()) or path.is_symlink()
                or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("plugin file inventory mismatch")
    for path in site.rglob("*"):
        if path.is_symlink():
            raise ValueError("plugin links are forbidden")
        if path.is_file() and path.relative_to(site).as_posix() not in files:
            source = path.parent.parent / (path.name.split(".")[0] + ".py")
            if not (path.parent.name == "__pycache__" and path.suffix == ".pyc"
                    and source.relative_to(site).as_posix() in files):
                raise ValueError("unlisted plugin file")


def validate_feishu_sdk() -> None:
    """Check the SDK's actual consumer APIs and effective dependencies, without connecting."""
    import lark_oapi as lark
    from lark_oapi.core.enum import LogLevel

    if not (callable(lark.ws.Client) and callable(lark.EventDispatcherHandler.builder)
            and hasattr(LogLevel, "INFO")):
        raise ImportError("Feishu SDK APIs are unavailable")
    pending, checked = ["lark-oapi"], set()
    while pending:
        name = pending.pop()
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in checked:
            continue
        checked.add(normalized)
        dist = importlib.metadata.distribution(name)
        for declared in dist.requires or []:
            try:
                from packaging.requirements import Requirement
            except ImportError:
                # Source installations already require pip; bundled runtimes include packaging.
                from pip._vendor.packaging.requirements import Requirement
            req = Requirement(declared)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            dependency = importlib.metadata.distribution(req.name)
            if req.url or not req.specifier.contains(dependency.version, prereleases=True):
                raise ImportError(f"Feishu SDK dependency mismatch: {req.name}")
            pending.append(req.name)


def validate_plugin_import(plugin_id: str) -> None:
    """Exercise the same package exports as consumers, with no network or service effects."""
    module = PLUGINS[plugin_id]["module"]
    plugin = importlib.import_module(module)
    if plugin_id == "pm":
        assert callable(plugin.PortfolioManagementClient)
    elif plugin_id == "position-advice":
        assert callable(plugin.evaluate_close_advice) and callable(plugin.format_close_advice_reason)
    elif plugin_id == "feishu":
        assert callable(importlib.import_module(module + ".bot").send_message)
        assert callable(importlib.import_module(module + ".ws_client").start_feishu_ws_client)
        validate_feishu_sdk()
    elif plugin_id == "wechat":
        assert callable(importlib.import_module(module + ".ilink_client").WechatClawbotClient)


def read_managed_plugin(repo_root: Path, plugin_id: str) -> dict | None:
    if plugin_id not in PLUGINS:
        raise ValueError("unknown official plugin")
    root = Path(repo_root).resolve()
    store = plugin_store(root)
    target = store / plugin_id
    if not target.exists() and not target.is_symlink():
        return None
    try:
        if store.parent.is_symlink() or store.is_symlink() or target.is_symlink() or not target.is_dir():
            raise ValueError("plugin directory must not be a symlink")
        if (target / "receipt.json").is_symlink():
            raise ValueError("plugin receipt must not be a symlink")
        receipt = json.loads((target / "receipt.json").read_text())
        if (receipt.get("schema_version") != 1 or receipt.get("plugin_id") != plugin_id
                or receipt.get("repo_root") != str(root)
                or receipt.get("om_version") != (root / "VERSION").read_text().strip()
                or receipt.get("version") != receipt.get("om_version")
                or receipt.get("distribution") != PLUGINS[plugin_id]["distribution"]
                or receipt.get("module") != PLUGINS[plugin_id]["module"]
                or not isinstance(receipt.get("version"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("wheel_sha256", ""))):
            raise ValueError("plugin receipt identity or OM version mismatch")
        files = receipt.get("files")
        module = PLUGINS[plugin_id]["module"]
        if not isinstance(files, dict) or f"{module}/__init__.py" not in files:
            raise ValueError("plugin file inventory missing")
        _validate_inventory(target / "site", files)
        dependencies = receipt.get("dependency_files", {})
        if dependencies:
            if plugin_id != "feishu":
                raise ValueError("only Feishu has managed dependencies")
            _validate_inventory(target / "dependencies", dependencies)
        elif (target / "dependencies").exists() or (target / "dependencies").is_symlink():
            raise ValueError("unlisted plugin dependencies")
        return receipt
    except (OSError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid managed plugin installation") from exc


class _OfficialPluginFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        match = next((key for key, item in PLUGINS.items() if item["module"] == fullname), None)
        if match is None:
            return None
        root = code_root()
        try:
            receipt = read_managed_plugin(root, match)
        except ValueError as exc:
            raise ImportError(f"Official plugin {match} is invalid; use om plugins list") from exc
        if receipt is None:
            return None
        if receipt.get("dependency_files"):
            dependencies = str(plugin_store(root) / match / "dependencies")
            if dependencies not in sys.path:
                sys.path.append(dependencies)
        package = plugin_store(root) / match / "site" / fullname
        return importlib.util.spec_from_file_location(fullname, package / "__init__.py",
                                                      submodule_search_locations=[str(package)])


def register_plugin_finder() -> None:
    if not any(isinstance(finder, _OfficialPluginFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _OfficialPluginFinder())
