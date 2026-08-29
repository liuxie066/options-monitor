from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RANKING_MODULE = ROOT / "src/application/strategy_lab/top1/ranking.py"
CONTRACTS_MODULE = ROOT / "src/application/strategy_lab/top1/contracts.py"
ECONOMICS_MODULE = ROOT / "src/application/strategy_lab/top1/economics.py"
STATISTICS_MODULE = ROOT / "src/application/strategy_lab/top1/statistics.py"
RESEARCH_MODULE = ROOT / "src/application/strategy_lab/top1/research.py"
RESEARCH_RUNNER_MODULE = (
    ROOT / "src/application/strategy_lab/top1/research_runner.py"
)
RESEARCH_ARTIFACTS_MODULE = (
    ROOT / "src/application/strategy_lab/top1/research_artifacts.py"
)
RECOMMENDATION_POINT_MODULE = ROOT / "src/application/recommendation_point.py"
CANDIDATE_ENGINE = ROOT / "domain/domain/engine/candidate_engine.py"
EXPERIMENT_STORE_MODULE = (
    ROOT / "src/infrastructure/strategy_lab/experiment_store.py"
)
LIFECYCLE_MODULE = ROOT / "src/application/strategy_lab/top1/lifecycle.py"
TERMINAL_PROJECTION_MODULE = (
    ROOT / "src/application/strategy_lab/top1/terminal_projection.py"
)
CORPUS_MODULE = ROOT / "src/application/strategy_lab/top1/corpus.py"
VALIDATION_MODULE = ROOT / "src/application/strategy_lab/top1/validation.py"
FILL_OBSERVATION_MODULE = (
    ROOT / "src/application/strategy_lab/top1/fill_observation.py"
)
OUTCOME_MODULE = ROOT / "src/application/strategy_lab/top1/outcome.py"
ADVANCE_MODULE = ROOT / "src/application/strategy_lab/top1/advance.py"
READINESS_MODULE = ROOT / "src/application/strategy_lab/top1/readiness.py"
PRODUCTION_TICK_MODULES = (
    ROOT / "src/application/multi_account_tick.py",
    ROOT / "src/application/tick_account_execution.py",
    ROOT / "src/application/tick_notification_flow.py",
    RECOMMENDATION_POINT_MODULE,
)


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
        "domain.domain.decision_state_fingerprint",
        "domain.domain.engine",
        "domain.domain.short_vol_assessment",
        "src.application.opening_candidate_snapshot",
        "src.application.shadow_replay.common",
        "src.application.strategy_lab.top1.economics",
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
        "domain.domain.short_vol_assessment",
        "src.application.opening_candidate_snapshot",
        "src.application.strategy_lab.top1.economics",
        "src.application.strategy_lab.top1.ranking",
        "src.application.strategy_lab.top1.statistics",
    }
    assert _imports(ECONOMICS_MODULE) <= {
        "__future__",
        "math",
        "collections.abc",
        "datetime",
        "decimal",
        "typing",
        "domain.domain.decision_state_fingerprint",
        "domain.domain.fee_calc",
        "domain.domain.performance.models",
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
    assert _imports(RESEARCH_MODULE) <= {
        "__future__",
        "hashlib",
        "math",
        "re",
        "collections.abc",
        "datetime",
        "typing",
        "domain.domain.decision_state_fingerprint",
        "domain.domain.engine",
        "domain.domain.fee_calc",
        "domain.domain.option_lifecycle",
        "src.application.shadow_replay.common",
        "src.application.strategy_lab.top1.contracts",
        "src.application.strategy_lab.top1.corpus",
        "src.application.strategy_lab.top1.economics",
        "src.application.strategy_lab.top1.ranking",
        "src.application.strategy_lab.top1.statistics",
    }


def test_recommendation_point_imports_only_producer_evidence_owners() -> None:
    assert _imports(RECOMMENDATION_POINT_MODULE) <= {
        "__future__",
        "collections.abc",
        "datetime",
        "hashlib",
        "json",
        "os",
        "pathlib",
        "re",
        "typing",
        "domain.domain.decision_state_fingerprint",
        "src.application.candidate_snapshot_contract",
        "src.application.candidate_snapshot_manifest",
        "src.application.opening_candidate_snapshot",
        "src.application.prepared_option_positions_context",
        "src.application.required_data_snapshot",
        "src.application.source_receipts",
        "src.application.tick_run_workspace",
    }


def test_top1_store_imports_only_stdlib_and_private_storage() -> None:
    assert _imports(EXPERIMENT_STORE_MODULE) <= {
        "__future__",
        "contextlib",
        "hashlib",
        "json",
        "pathlib",
        "sqlite3",
        "typing",
        "urllib.parse",
        "src.infrastructure.private_storage",
    }


def test_top1_lifecycle_and_terminal_projection_keep_dependency_direction() -> None:
    assert _imports(LIFECYCLE_MODULE) <= {
        "__future__",
        "datetime",
        "hashlib",
        "json",
        "pathlib",
        "re",
        "typing",
        "zoneinfo",
        "domain.domain.decision_state_fingerprint",
        "src.application.recommendation_point",
        "src.application.scan_scheduler",
        "src.application.shadow_replay.common",
        "src.application.strategy_lab.top1.contracts",
        "src.application.strategy_lab.top1.corpus",
        "src.application.strategy_lab.top1.research",
        "src.application.strategy_lab.top1.research_artifacts",
        "src.application.strategy_lab.top1.terminal_projection",
        "src.infrastructure.strategy_lab.experiment_store",
    }
    assert _imports(TERMINAL_PROJECTION_MODULE) <= {
        "__future__",
        "hashlib",
        "json",
        "os",
        "pathlib",
        "stat",
        "tempfile",
        "typing",
        "src.application.shadow_replay.common",
        "src.infrastructure.private_storage",
        "src.infrastructure.strategy_lab.experiment_store",
    }

    assert _imports(CORPUS_MODULE) <= {
        "__future__",
        "collections.abc",
        "datetime",
        "hashlib",
        "json",
        "pathlib",
        "re",
        "typing",
        "zoneinfo",
        "domain.domain.decision_state_fingerprint",
        "src.application.candidate_snapshot_contract",
        "src.application.opening_candidate_snapshot",
        "src.application.prepared_option_positions_context",
        "src.application.recommendation_point",
        "src.application.research.formal_corpus",
        "src.application.scan_scheduler",
        "src.application.shadow_replay.common",
        "src.application.strategy_lab.top1.contracts",
        "src.application.strategy_lab.top1.lifecycle",
        "src.application.strategy_lab.top1.ranking",
        "src.application.strategy_lab.top1.terminal_projection",
        "src.infrastructure.private_storage",
        "src.infrastructure.strategy_lab.experiment_store",
    }

    assert _imports(ADVANCE_MODULE) <= {
        "__future__",
        "collections.abc",
        "datetime",
        "hashlib",
        "pathlib",
        "typing",
        "zoneinfo",
        "src.application.recommendation_point",
        "src.application.research.formal_corpus",
        "src.application.strategy_lab.top1.corpus",
        "src.application.strategy_lab.top1.fill_observation",
        "src.application.strategy_lab.top1.lifecycle",
        "src.application.strategy_lab.top1.outcome",
        "src.application.strategy_lab.top1.validation",
        "src.infrastructure.strategy_lab.experiment_store",
    }
    assert _imports(READINESS_MODULE) <= {
        "__future__",
        "collections.abc",
        "datetime",
        "pathlib",
        "typing",
        "src.application.research.formal_corpus",
        "src.application.strategy_lab.top1.contracts",
    }

    assert _imports(RESEARCH_RUNNER_MODULE) <= {
        "__future__",
        "hashlib",
        "json",
        "math",
        "re",
        "collections.abc",
        "pathlib",
        "typing",
        "domain.domain.option_lifecycle",
        "src.application.opend_call_coordinator",
        "src.application.opend_fetch_config",
        "src.application.recommendation_point",
        "src.application.shadow_replay.common",
        "src.application.strategy_lab.top1.contracts",
        "src.application.strategy_lab.top1.lifecycle",
        "src.application.strategy_lab.top1.research",
        "src.application.strategy_lab.top1.research_artifacts",
        "src.application.strategy_lab.top1.terminal_projection",
        "src.infrastructure.futu_gateway",
        "src.infrastructure.private_storage",
        "src.infrastructure.strategy_lab.experiment_store",
    }
    assert _imports(RESEARCH_ARTIFACTS_MODULE) <= {
        "__future__",
        "hashlib",
        "json",
        "re",
        "collections.abc",
        "pathlib",
        "typing",
        "src.application.research.formal_corpus",
        "src.application.shadow_replay.common",
        "src.application.strategy_lab.top1.contracts",
        "src.application.strategy_lab.top1.ranking",
        "src.application.strategy_lab.top1.research",
        "src.infrastructure.private_storage",
    }


def test_w6_validation_modules_keep_narrow_dependency_direction() -> None:
    assert _imports(VALIDATION_MODULE) <= {
        "__future__",
        "hashlib",
        "json",
        "re",
        "collections.abc",
        "datetime",
        "pathlib",
        "typing",
        "zoneinfo",
        "domain.domain.decision_state_fingerprint",
        "domain.domain.fee_calc",
        "src.application.shadow_replay.common",
        "src.application.strategy_lab.top1.contracts",
        "src.application.strategy_lab.top1.corpus",
        "src.application.strategy_lab.top1.lifecycle",
        "src.application.strategy_lab.top1.ranking",
        "src.application.strategy_lab.top1.research_artifacts",
        "src.application.strategy_lab.top1.terminal_projection",
        "src.infrastructure.strategy_lab.experiment_store",
    }
    assert _imports(FILL_OBSERVATION_MODULE) <= {
        "__future__",
        "json",
        "math",
        "re",
        "collections.abc",
        "datetime",
        "pathlib",
        "typing",
        "domain.domain.decision_state_fingerprint",
        "domain.domain.option_lifecycle",
        "src.application.strategy_lab.top1.corpus",
        "src.application.strategy_lab.top1.lifecycle",
        "src.application.strategy_lab.top1.validation",
        "src.infrastructure.strategy_lab.experiment_store",
    }
    assert _imports(OUTCOME_MODULE) <= {
        "__future__",
        "json",
        "math",
        "collections.abc",
        "datetime",
        "pathlib",
        "typing",
        "domain.domain.decision_state_fingerprint",
        "src.application.strategy_lab.top1.contracts",
        "src.application.strategy_lab.top1.corpus",
        "src.application.strategy_lab.top1.economics",
        "src.application.strategy_lab.top1.lifecycle",
        "src.application.strategy_lab.top1.statistics",
        "src.application.strategy_lab.top1.terminal_projection",
        "src.application.strategy_lab.top1.validation",
        "src.infrastructure.futu_gateway",
        "src.infrastructure.strategy_lab.experiment_store",
    }


def test_production_tick_does_not_depend_on_strategy_lab() -> None:
    for path in PRODUCTION_TICK_MODULES:
        assert not any(
            module.startswith("src.application.strategy_lab")
            or module.startswith("src.infrastructure.strategy_lab")
            for module in _imports(path)
        )
