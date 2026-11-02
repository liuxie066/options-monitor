"""Build and validate the fixed official plugin Release assets; never publish/install."""
from __future__ import annotations

import base64
import csv
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from zipfile import BadZipFile, ZipFile
import zlib

from src.application.plugin_runtime import PLUGINS

REPOSITORY = "liuxie066/options-monitor"
CATALOG = "om-plugins.json"
NOTES = "om-plugins.md"
MAX_WHEEL_MEMBERS = 1000
MAX_WHEEL_BYTES = 20 * 1024 * 1024
OFFICIAL_PLUGINS = tuple((key, item["distribution"], item["module"], item["source"])
                         for key, item in PLUGINS.items())
_DEPENDENCIES = {"feishu": ["lark-oapi"]}


def _package_version(version: str) -> str:
    # OM uses SemVer; wheel metadata uses PEP 440 for supported prereleases.
    match = re.fullmatch(r"((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))(?:-(alpha|beta|rc)[.-]?([0-9]+))?", version)
    if not match:
        raise ValueError("plugin release requires a Python-compatible OM version")
    core, pre, number = match.groups()
    return core + ({"alpha": "a", "beta": "b", "rc": "rc"}[pre] + str(int(number)) if pre else "")


def _asset(distribution: str, version: str) -> str:
    return f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"


def _validate_entry(entry: dict, version: str) -> None:
    if not isinstance(entry, dict):
        raise ValueError("invalid plugin catalog entry")
    identity = (entry.get("id"), entry.get("distribution"), entry.get("module"))
    if identity not in [p[:3] for p in OFFICIAL_PLUGINS]:
        raise ValueError("unknown official plugin identity")
    if (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?", version)
            or entry.get("version") != version or entry.get("requires_python") != ">=3.12"
            or entry.get("asset") != _asset(entry["distribution"], version)
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or type(entry.get("size")) is not int or entry["size"] <= 0):
        raise ValueError("invalid plugin version, compatibility or asset identity")


def validate_catalog(data, *, om_version: str) -> dict:
    """Validate the official catalog against the exact host OM version, without I/O."""
    version = _package_version(om_version)
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1 or data.get("repository") != REPOSITORY
            or data.get("om_version") != om_version or data.get("tag") != f"v{om_version}"
            or not isinstance(data.get("commit"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", data["commit"])):
        raise ValueError("invalid official plugin catalog identity or OM version mismatch")
    entries = data.get("plugins")
    if not isinstance(entries, list) or len(entries) != len(OFFICIAL_PLUGINS):
        raise ValueError("catalog must contain exactly the four official plugins")
    for entry in entries:
        _validate_entry(entry, version)
    if {e["id"] for e in entries} != {p[0] for p in OFFICIAL_PLUGINS}:
        raise ValueError("duplicate or missing official plugin")
    return data


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_wheel(path: Path, entry: dict) -> None:
    """Validate bytes, metadata, purelib and safe package members without extracting/importing."""
    _validate_entry(entry, entry.get("version") if isinstance(entry, dict) else None)
    if sys.version_info < (3, 12):
        raise ValueError("official plugins require Python >=3.12")
    if (path.name != entry["asset"] or path.is_symlink() or not path.is_file()
            or path.stat().st_size != entry["size"] or _digest(path) != entry["sha256"]):
        raise ValueError("plugin wheel asset, size or sha256 mismatch")
    module = entry["module"]
    dist_info = f"{entry['distribution'].replace('-', '_')}-{entry['version']}.dist-info"
    try:
        with ZipFile(path) as wheel:
            members = wheel.infolist()
            if (len(members) > MAX_WHEEL_MEMBERS
                    or sum(m.file_size for m in members) > MAX_WHEEL_BYTES):
                raise ValueError("plugin wheel exceeds member or uncompressed size limit")
            files = {}
            seen = set()
            for member in members:
                name = member.filename
                parts = PurePosixPath(name).parts
                mode = member.external_attr >> 16
                if (name in seen or not parts or "\\" in name or name.startswith("/")
                        or any(p in {"", ".", ".."} for p in name.rstrip("/").split("/"))
                        or not re.fullmatch(r"[A-Za-z0-9_./+-]+", name)
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or (stat.S_ISDIR(mode) and not member.is_dir())
                        or member.flag_bits & 1):
                    raise ValueError("unsafe or duplicate plugin wheel member")
                seen.add(name)
                if parts[0] not in {module, dist_info}:
                    raise ValueError("plugin wheel member outside its package")
                if member.is_dir():
                    continue
                if parts[0] == module:
                    if (not name.endswith(".py") or "__pycache__" in parts
                            or any(not p.isidentifier() for p in parts[1:-1])
                            or not PurePosixPath(name).stem.isidentifier()):
                        raise ValueError("plugin wheel must contain only Python package code")
                elif (parts[1:] not in [("METADATA",), ("WHEEL",), ("RECORD",), ("top_level.txt",)]
                      and not (len(parts) >= 3 and parts[1] == "licenses")):
                    raise ValueError("unsupported plugin wheel metadata member")
                files[name] = wheel.read(member)
            if f"{module}/__init__.py" not in files:
                raise ValueError("plugin wheel package is missing")
            metadata = BytesParser().parsebytes(files[f"{dist_info}/METADATA"])
            headers = BytesParser().parsebytes(files[f"{dist_info}/WHEEL"])
            normalized_name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
            if (len(metadata.get_all("Name", [])) != 1 or normalized_name != entry["distribution"]
                    or metadata.get_all("Version") != [entry["version"]]
                    or metadata.get_all("Requires-Python") != [entry["requires_python"]]
                    or metadata.get_all("Requires-Dist", []) != _DEPENDENCIES.get(entry["id"], [])
                    or metadata.get_all("Provides-Extra")):
                raise ValueError("plugin wheel metadata or dependency mismatch")
            if (headers.get_all("Wheel-Version") != ["1.0"]
                    or headers.get_all("Root-Is-Purelib") != ["true"]
                    or headers.get_all("Tag") != ["py3-none-any"]):
                raise ValueError("plugin wheel must be purelib py3-none-any")
            record_name = f"{dist_info}/RECORD"
            records = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
            if any(len(r) != 3 for r in records) or len({r[0] for r in records}) != len(records):
                raise ValueError("invalid plugin wheel RECORD")
            if {r[0] for r in records} != set(files):
                raise ValueError("plugin wheel RECORD inventory mismatch")
            for name, digest, size in records:
                if name == record_name:
                    expected = ("", "")
                else:
                    encoded = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=").decode()
                    expected = ("sha256=" + encoded, str(len(files[name])))
                if (digest, size) != expected:
                    raise ValueError("plugin wheel RECORD digest mismatch")
    except (BadZipFile, KeyError, UnicodeError, csv.Error, OSError, EOFError, NotImplementedError, zlib.error) as exc:
        raise ValueError("invalid plugin wheel content") from exc


def compatibility_notes(catalog: dict) -> str:
    return (f"# Official plugins for options-monitor {catalog['om_version']}\n\n"
            f"Requires exactly OM {catalog['om_version']} (tag `{catalog['tag']}`, "
            f"commit `{catalog['commit']}`) and Python >=3.12.\n"
            "Four pure Python wheels (`py3-none-any`). Feishu declares `lark-oapi`; "
            "PM, position-advice and WeChat have no dependencies. "
            "Install only with the matching OM release; other OM versions are incompatible.\n\n"
            "Official Release wheel versions follow OM VERSION. Source/pip packages may retain "
            "their own project version (currently 0.1.0); that source version is distinct from "
            "these official Release wheels and is not their compatibility identity.\n\n"
            + "\n".join(f"- `{e['id']}`: `{e['asset']}` (SHA-256 `{e['sha256']}`)" for e in catalog["plugins"])
            + "\n")


def verify_publication(directory: Path, *, tag: str, commit: str | None = None) -> dict:
    """Read back a local publication set; verify catalog, wheels and compatibility notes."""
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("invalid plugin publication tag")
    try:
        catalog = validate_catalog(json.loads((directory / CATALOG).read_text()), om_version=tag[1:])
        if commit is not None and catalog["commit"] != commit:
            raise ValueError("plugin publication commit mismatch")
        if {p.name for p in directory.glob("*.whl")} != {e["asset"] for e in catalog["plugins"]}:
            raise ValueError("plugin publication must contain exactly four wheels")
        for entry in catalog["plugins"]:
            validate_wheel(directory / entry["asset"], entry)
        if (directory / NOTES).read_text() != compatibility_notes(catalog):
            raise ValueError("plugin compatibility notes mismatch")
        return catalog
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("plugin publication missing or unreadable") from exc


def build(*, source: Path, output: Path) -> dict:
    """Build in disposable source copies with pip's default build isolation."""
    om_version = (source / "VERSION").read_text().strip()
    version = _package_version(om_version)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    catalog = {"schema_version": 1, "repository": REPOSITORY, "om_version": om_version,
               "tag": f"v{om_version}", "commit": commit, "plugins": []}
    if output.exists() and any(output.iterdir()):
        raise ValueError("plugin output directory must be empty")
    with tempfile.TemporaryDirectory(prefix="om-plugin-build-") as temporary:
        staging = Path(temporary)
        wheels = staging / "wheels"
        wheels.mkdir()
        env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONNOUSERSITE="1",
                   PYTHONDONTWRITEBYTECODE="1", PIP_CACHE_DIR=str(staging / "pip-cache"),
                   TMPDIR=str(staging))
        for key in ("PYTHONPATH", "PYTHONHOME", "PIP_TARGET", "PIP_PREFIX", "PIP_USER", "PIP_NO_BUILD_ISOLATION"):
            env.pop(key, None)
        for plugin_id, distribution, module, folder in OFFICIAL_PLUGINS:
            original = source / "plugins" / folder
            package = staging / folder
            package.mkdir()
            text = (original / "pyproject.toml").read_text()
            project = tomllib.loads(text)["project"]
            if (project["name"] != distribution or project["requires-python"] != ">=3.12"
                    or project.get("dependencies") != _DEPENDENCIES.get(plugin_id, [])
                    or project.get("optional-dependencies")):
                raise ValueError("plugin source metadata differs from official contract")
            text, count = re.subn(r'(?m)^version\s*=\s*"[^"]+"\s*$', f'version = "{version}"', text)
            if count != 1 or tomllib.loads(text)["project"]["version"] != version:
                raise ValueError("plugin source must have one static project version")
            (package / "pyproject.toml").write_text(text)
            shutil.copytree(original / "src", package / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
            subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheels), str(package)],
                           check=True, cwd=staging, env=env, timeout=300)
            path = wheels / _asset(distribution, version)
            entry = dict(id=plugin_id, distribution=distribution, module=module, version=version,
                         requires_python=">=3.12", asset=path.name, sha256=_digest(path), size=path.stat().st_size)
            validate_wheel(path, entry)
            catalog["plugins"].append(entry)
        validate_catalog(catalog, om_version=om_version)
        (wheels / CATALOG).write_text(json.dumps(catalog, indent=2) + "\n")
        (wheels / NOTES).write_text(compatibility_notes(catalog))
        verify_publication(wheels, tag=catalog["tag"], commit=commit)
        output.mkdir(parents=True, exist_ok=True)
        for path in wheels.iterdir():
            shutil.copy2(path, output / path.name)
    return verify_publication(output, tag=catalog["tag"], commit=commit)
