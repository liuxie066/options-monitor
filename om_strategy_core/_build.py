"""Setuptools hook: seal copied code, never write a manifest into the checkout."""
import hashlib
import json
from pathlib import Path

from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        if self.editable_mode:
            return
        root = Path(self.build_lib)
        files = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for package in ("domain", "om_strategy_core")
            for path in sorted((root / package).rglob("*.py"))
        }
        manifest = {
            "version": self.distribution.get_version(),
            "files": files,
            "build_digest": hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        (root / "om_strategy_core/_build_manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8",
        )
