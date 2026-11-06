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
import tomllib

PLUGINS = {
    "pm": {"distribution": "om-pm-plugin", "module": "om_pm_plugin", "source": "portfolio-management"},
    "position-advice": {"distribution": "om-position-advice-plugin", "module": "om_position_advice_plugin", "source": "position-advice"},
    "feishu": {"distribution": "om-feishu-channel-plugin", "module": "om_feishu_channel", "source": "feishu-channel"},
    "wechat": {"distribution": "om-wechat-channel-plugin", "module": "om_wechat_channel", "source": "wechat-channel"},
    "market-opend": {"distribution": "om-market-opend-plugin", "module": "om_market_opend", "source": "market-opend", "kind": "market"},
    "broker-futu": {"distribution": "om-broker-futu-plugin", "module": "om_broker_futu", "source": "broker-futu"},
    "broker-longbridge": {"distribution": "om-broker-longbridge-plugin", "module": "om_broker_longbridge", "source": "broker-longbridge"},
}

# A running process must not rebind to another release when current is switched.
_CODE_ROOT = Path(__file__).resolve().parents[2]


def code_root() -> Path:
    return _CODE_ROOT


def plugin_store(repo_root: Path) -> Path:
    root = Path(repo_root).resolve()
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:24]
    return root.parent / ".om-plugins" / identity


class PluginUnavailableError(RuntimeError):
    code = "PLUGIN_UNAVAILABLE"


def expected_plugin_version(plugin_id: str, *, repo_root: Path | None = None) -> str:
    root = Path(repo_root).resolve() if repo_root is not None else code_root()
    project = root / "plugins" / PLUGINS[plugin_id]["source"] / "pyproject.toml"
    return (tomllib.loads(project.read_text())["project"]["version"] if project.is_file()
            else (root / "VERSION").read_text().strip())


def _environment_version_matches(plugin_id: str) -> bool:
    return importlib.metadata.version(PLUGINS[plugin_id]["distribution"]) == expected_plugin_version(plugin_id)


def plugin_installed(plugin_id: str) -> bool:
    """Inspect installation identity without importing the package or any SDK."""
    item = PLUGINS[plugin_id]
    root = code_root()
    try:
        if read_managed_plugin(root, plugin_id) is not None:
            cached = sys.modules.get(item["module"])
            selected = plugin_store(root) / plugin_id / "site" / item["module"] / "__init__.py"
            # A cached pip owner must not silently impersonate a managed selection.
            return cached is None or getattr(cached, "__file__", None) == str(selected)
        if read_source_plugin(root, plugin_id) is not None:
            cached = sys.modules.get(item["module"])
            selected = source_plugin_store(root) / plugin_id / "site" / item["module"] / "__init__.py"
            return cached is None or getattr(cached, "__file__", None) == str(selected)
        return _environment_version_matches(plugin_id) and importlib.util.find_spec(item["module"]) is not None
    except (OSError, ValueError, ImportError, KeyError):
        return False


def require_plugin(plugin_id: str):
    """Load an installed capability lazily; never fall back to repository sources."""
    if plugin_installed(plugin_id):
        try:
            return importlib.import_module(PLUGINS[plugin_id]["module"])
        except (ImportError, OSError, RuntimeError) as exc:
            raise PluginUnavailableError(
                f"Plugin {plugin_id} cannot load; check om plugins list and use om plugins install {plugin_id} --confirm"
            ) from exc
    raise PluginUnavailableError(
        f"Plugin {plugin_id} is unavailable; use om plugins install {plugin_id} --confirm"
    )


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
    from packaging.requirements import Requirement

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
    elif plugin_id == "market-opend":
        assert callable(plugin.OpenDMarketDataProvider)
    elif plugin_id == "broker-futu":
        assert callable(plugin.resolve_portfolio_provider) and callable(plugin.build_ready_broker_gateway)
    elif plugin_id == "broker-longbridge":
        assert callable(plugin.LongbridgeGateway) and callable(plugin.resolve_portfolio_provider)
        try:
            from longbridge.openapi import Config, TradeContext
            requirement = (code_root() / "requirements/longbridge.txt").read_text()
            pin = next(line.strip().split("==", 1)[1] for line in requirement.splitlines()
                       if line.strip().startswith("longbridge=="))
            if importlib.metadata.version("longbridge") != pin or not (callable(Config) and callable(TradeContext)):
                raise ImportError("incompatible Longbridge SDK")
        except (ImportError, OSError, StopIteration) as exc:
            raise ImportError("Longbridge SDK unavailable or incompatible; install requirements/longbridge.txt in the source runtime; immutable bundles are not modified") from exc


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
            if receipt is None and read_source_plugin(root, match) is not None:
                package = source_plugin_store(root) / match / "site" / fullname
                return importlib.util.spec_from_file_location(fullname, package / "__init__.py",
                                                              submodule_search_locations=[str(package)])
        except (ValueError, OSError) as exc:
            raise ImportError(f"Official plugin {match} is invalid; use om plugins list") from exc
        if receipt is None:
            # Ask the remaining importers, preserving absent and editable-package discovery.
            spec = None
            for finder in sys.meta_path:
                if isinstance(finder, _OfficialPluginFinder):
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is not None:
                    break
            if spec is None:
                return None
            try:
                if not _environment_version_matches(match):
                    raise ValueError("plugin version mismatch")
            except (OSError, ValueError, ImportError, KeyError) as exc:
                raise ImportError(f"Official plugin {match} is incompatible; use om plugins list") from exc
            return spec
        if receipt.get("dependency_files"):
            dependencies = str(plugin_store(root) / match / "dependencies")
            if dependencies not in sys.path:
                sys.path.append(dependencies)
        package = plugin_store(root) / match / "site" / fullname
        return importlib.util.spec_from_file_location(fullname, package / "__init__.py",
                                                      submodule_search_locations=[str(package)])


def source_plugin_store(repo_root: Path) -> Path:
    return Path(repo_root) / ".om-source-plugins"


def read_source_plugin(repo_root: Path, plugin_id: str) -> dict | None:
    """Validate an upgrade-installed source package independently of shared dependencies."""
    target = source_plugin_store(repo_root) / plugin_id
    if not target.exists():
        return None
    receipt = json.loads((target / "receipt.json").read_text())
    item = PLUGINS[plugin_id]
    if (source_plugin_store(repo_root).is_symlink() or target.is_symlink()
            or not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("plugin_id") != plugin_id
            or receipt.get("distribution") != item["distribution"]
            or receipt.get("version") != expected_plugin_version(plugin_id, repo_root=repo_root)
            or not isinstance(receipt.get("source_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["source_sha256"])):
        raise ValueError("source plugin receipt identity mismatch")
    files = receipt.get("files")
    if not isinstance(files, dict) or f"{item['module']}/__init__.py" not in files:
        raise ValueError("source plugin file inventory missing")
    _validate_inventory(target / "site", files)
    distributions = list(importlib.metadata.distributions(path=[str(target / "site")]))
    if (len(distributions) != 1 or distributions[0].metadata["Name"] != item["distribution"]
            or distributions[0].version != receipt["version"]):
        raise ValueError("source plugin installed distribution mismatch")
    return receipt


def register_plugin_finder() -> None:
    # Each release owns its plugin code; shared venvs contain only dependencies.
    # Metadata probes and normal imports see the same release-specific packages.
    for plugin_id in PLUGINS:
        try:
            receipt = read_source_plugin(code_root(), plugin_id)
        except (OSError, ValueError, KeyError):
            # Keep core commands usable to inspect/repair an invalid add-on.
            # The finder and plugin_installed still reject it on actual use.
            continue
        if receipt is not None:
            site = str(source_plugin_store(code_root()) / plugin_id / "site")
            if site not in sys.path:
                sys.path.insert(0, site)
    if not any(isinstance(finder, _OfficialPluginFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _OfficialPluginFinder())
