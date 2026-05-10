from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _imports_from(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
    return imports


def test_domain_storage_does_not_import_scripts_layer() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "domain" / "storage").rglob("*.py")):
        for lineno, module in _imports_from(path):
            if module == "scripts" or module.startswith("scripts."):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}:{module}")

    assert offenders == []


def test_domain_storage_json_io_round_trip_and_default(tmp_path: Path) -> None:
    from domain.storage.json_io import atomic_write_json, read_json

    path = tmp_path / "nested" / "state.json"
    atomic_write_json(path, {"ok": True})

    assert read_json(path) == {"ok": True}
    assert path.read_text(encoding="utf-8").endswith("\n")

    path.write_text("{", encoding="utf-8")
    assert read_json(path, default={"fallback": True}) == {"fallback": True}
