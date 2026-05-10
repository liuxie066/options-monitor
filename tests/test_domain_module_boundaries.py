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


def test_domain_package_does_not_import_outer_layers() -> None:
    modules = sorted((ROOT / "domain").rglob("*.py"))
    offenders: list[str] = []
    for path in modules:
        for lineno, module in _imports_from(path):
            if module == "scripts" or module.startswith("scripts."):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}:{module}")
            if module == "src" or module.startswith("src."):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}:{module}")

    assert offenders == []


def test_legacy_identity_wrapper_modules_are_removed() -> None:
    assert not (ROOT / "scripts" / "trade_symbol_identity.py").exists()
    assert not (ROOT / "scripts" / "trade_contract_identity.py").exists()


def test_symbol_identity_has_no_runtime_config_file_io() -> None:
    text = (ROOT / "domain" / "domain" / "symbol_identity.py").read_text(encoding="utf-8")

    assert "config.us.json" not in text
    assert "config.hk.json" not in text
    assert "read_text" not in text
