#!/usr/bin/env python3
"""Read-only binding of development plugin packages to the selected checkout."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.application.plugin_runtime import PLUGINS, expected_plugin_version


def _sources(package: Path) -> dict[str, str]:
    return {
        path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob("*.py")) if path.is_file()
    }


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _check_plugin(repo: Path, plugin_id: str, item: dict[str, str]) -> dict:
    # All official development projects use this src layout. The inventory owns
    # the names; the all-entry test makes a future layout change fail explicitly.
    source = repo / "plugins" / item["source"] / "src" / item["module"]
    row = {"id": plugin_id, "distribution": item["distribution"],
           "source_path": str(source), "loaded_path": None, "version": None,
           "source_digest": None, "loaded_digest": None, "errors": []}
    errors = row["errors"]
    try:
        expected = expected_plugin_version(plugin_id, repo_root=repo)
        row["expected_version"] = expected
        row["version"] = importlib.metadata.version(item["distribution"])
        if row["version"] != expected:
            errors.append(f"distribution version {row['version']!r} != {expected!r}")
        spec = importlib.util.find_spec(item["module"])
        locations = list(spec.submodule_search_locations or ()) if spec else []
        if len(locations) != 1:
            errors.append("package must resolve to exactly one directory")
        else:
            loaded = Path(locations[0]).resolve()
            row["loaded_path"] = str(loaded)
            if loaded != source.resolve() and not loaded.is_relative_to(Path(sys.prefix).resolve()):
                errors.append("package is outside selected Python prefix and this checkout")
            if not source.is_dir() or not loaded.is_dir():
                errors.append("source or loaded package directory is missing")
            else:
                wanted, actual = _sources(source), _sources(loaded)
                row["source_digest"], row["loaded_digest"] = _digest(wanted), _digest(actual)
                row["source_file_count"] = len(wanted)
                if not wanted:
                    errors.append("source package contains no Python files")
                for label, names in (
                    ("missing", wanted.keys() - actual.keys()),
                    ("unexpected", actual.keys() - wanted.keys()),
                    ("changed", {name for name in wanted.keys() & actual.keys()
                                 if wanted[name] != actual[name]}),
                ):
                    if names:
                        errors.append(f"{label} Python files: {', '.join(sorted(names))}")
    except (OSError, ValueError, ImportError, KeyError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    row["ok"] = not errors
    if errors:
        row["remediation"] = "Reinstall this local plugin from the confirmed checkout into the selected Python environment."
    return row


def check_environment(repo_root: Path) -> dict:
    repo = Path(repo_root).resolve()
    result = {"ok": False, "repo_root": str(repo), "head": None,
              "python": sys.executable, "prefix": sys.prefix, "errors": [], "plugins": []}
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=False, timeout=10)
        if head.returncode or not head.stdout.strip():
            result["errors"].append("Cannot read checkout Git HEAD: " + head.stderr.strip())
        else:
            result["head"] = head.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["errors"].append(f"Cannot read checkout Git HEAD: {exc}")
    result["plugins"] = [_check_plugin(repo, key, item) for key, item in PLUGINS.items()]
    result["ok"] = not result["errors"] and all(row["ok"] for row in result["plugins"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    result = check_environment(args.repo_root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
