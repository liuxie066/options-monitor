from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RANKING_MODULE = ROOT / "src/application/strategy_lab/top1/ranking.py"
CONTRACTS_MODULE = ROOT / "src/application/strategy_lab/top1/contracts.py"
ECONOMICS_MODULE = ROOT / "src/application/strategy_lab/top1/economics.py"
STATISTICS_MODULE = ROOT / "src/application/strategy_lab/top1/statistics.py"
CANDIDATE_ENGINE = ROOT / "domain/domain/engine/candidate_engine.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_top1_ranking_imports_only_pure_approved_owners() -> None:
    assert _imports(RANKING_MODULE) <= {
        "__future__",
        "math",
        "re",
        "collections.abc",
        "datetime",
        "typing",
        "domain.domain.engine",
        "src.application.opening_candidate_snapshot",
        "src.application.shadow_replay.common",
    }


def test_candidate_engine_does_not_depend_on_strategy_lab() -> None:
    assert not any(
        module.startswith("src.application.strategy_lab")
        for module in _imports(CANDIDATE_ENGINE)
    )


def test_top1_core_imports_only_approved_pure_owners() -> None:
    assert _imports(CONTRACTS_MODULE) <= {
        "__future__",
        "math",
        "re",
        "collections.abc",
        "copy",
        "datetime",
        "typing",
        "domain.domain.decision_state_fingerprint",
        "domain.domain.engine",
        "domain.domain.fee_calc",
        "src.application.opening_candidate_snapshot",
        "src.application.strategy_lab.top1.ranking",
    }
    assert _imports(ECONOMICS_MODULE) <= {
        "__future__",
        "math",
        "collections.abc",
        "datetime",
        "typing",
        "domain.domain.fee_calc",
    }
    assert _imports(STATISTICS_MODULE) <= {
        "__future__",
        "math",
        "statistics",
        "collections",
        "collections.abc",
        "datetime",
        "typing",
        "scipy.stats",
    }
