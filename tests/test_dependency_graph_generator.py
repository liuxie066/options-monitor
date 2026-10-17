from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.generate_dependency_graph import resolve_internal_imports


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_graph_generator_check_passes() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/generate_dependency_graph.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "dependency graph current" in proc.stdout


def test_dependency_graph_resolves_relative_imports_from_package_init() -> None:
    import ast

    node = ast.parse("from .tool_execution_service import ToolExecutionService").body[0]
    modules = {
        "domain.services",
        "domain.services.tool_execution_service",
    }

    assert resolve_internal_imports("domain.services", node, modules) == [
        "domain.services.tool_execution_service"
    ]

def test_strategy_core_facade_is_included_and_cannot_import_runtime(tmp_path):
    from scripts.generate_dependency_graph import analyze_repo
    facade = tmp_path / "om_strategy_core"
    facade.mkdir()
    (facade / "__init__.py").write_text("from src.application.owner import compute\n")
    application = tmp_path / "src/application"
    application.mkdir(parents=True)
    (application / "owner.py").write_text("compute = None\n")
    graph = analyze_repo(tmp_path)
    assert "om_strategy_core" in graph.modules
    assert len(graph.boundary_violations) == 1
    assert graph.boundary_violations[0].importer == "om_strategy_core"
