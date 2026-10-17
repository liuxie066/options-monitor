"""Adapter-level distribution verification, never part of financial arithmetic."""
from __future__ import annotations

import base64
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
import json
from pathlib import Path
import sys

from domain.domain.strategy_core import __version__


def runtime_identity(*, mode: str = "source") -> dict[str, str | None]:
    """Source/editable has no artifact claim; installed is explicitly fail-closed."""
    if mode == "source":
        return {"mode": "source", "core_version": __version__, "build_digest": None}
    if mode != "installed":
        raise ValueError("mode must be source or installed")
    try:
        dist = distribution("om-strategy-core")
        root = Path(dist.locate_file("")).resolve()
        direct_url = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct_url.get("dir_info", {}).get("editable"):
            raise ValueError("editable installation is source")
        records = {str(entry): entry for entry in dist.files or ()}
        manifest_name = "om_strategy_core/_build_manifest.json"
        manifest_path = root / manifest_name
        raw = manifest_path.read_bytes()
        record = records[manifest_name]
        if record.hash is None or record.hash.mode != "sha256" or record.hash.value != base64.urlsafe_b64encode(
            hashlib.sha256(raw).digest()
        ).rstrip(b"=").decode():
            raise ValueError("build manifest does not match distribution RECORD")
        manifest = json.loads(raw)
        files = manifest["files"]
        declared = {name for name in records if name.endswith(".py") and name.startswith(("domain/", "om_strategy_core/"))}
        if not files or set(files) != declared:
            raise ValueError("manifest file inventory differs from distribution")
        if manifest["version"] != dist.version or dist.version != __version__:
            raise ValueError("core version differs from distribution")
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != manifest["build_digest"]:
            raise ValueError("invalid build digest")
        for name, expected in files.items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"installed content differs: {name}")
        for name, module in tuple(sys.modules.items()):
            if name not in ("domain", "om_strategy_core") and not name.startswith(("domain.", "om_strategy_core.")):
                continue
            if module is None or not getattr(module, "__file__", None):
                raise ValueError(f"unverifiable module origin: {name}")
            path = Path(module.__file__).resolve()
            if not path.is_relative_to(root) or path.relative_to(root).as_posix() not in files:
                raise ValueError(f"shadowed module origin: {name}")
            expected_package = path.parent
            if hasattr(module, "__path__") and any(Path(p).resolve() != expected_package for p in module.__path__):
                raise ValueError(f"mixed package search path: {name}")
        if Path(__file__).resolve() != root / "om_strategy_core/identity.py":
            raise ValueError("identity adapter is shadowed")
        return {"mode": "installed", "core_version": __version__, "build_digest": digest}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, PackageNotFoundError) as exc:
        raise ValueError(f"installed core verification failed: {exc}") from exc
