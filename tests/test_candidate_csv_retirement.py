from __future__ import annotations

import inspect
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "src", ROOT / "scripts")
LEGACY_METADATA_CLASSIFIERS = {
    ROOT / "src" / "application" / "candidate_evidence_history.py",
    ROOT / "src" / "application" / "research" / "archive.py",
}
RETIRED_CANDIDATE_CSV_FRAGMENTS = (
    "_candidates.csv",
    "_candidates_labeled.csv",
    "_candidates_reject_log.csv",
    "_reject_log.csv",
    "_pair_diagnostics.csv",
    "_rank_shadow.csv",
    "_put_universe.csv",
    "_put_universe_labeled.csv",
    "_put_universe_cash_filtered.csv",
    "_put_universe_underwritten.csv",
    "sell_put_linked_calls.csv",
)


def _production_python_files() -> list[Path]:
    return sorted(
        path
        for root in PRODUCTION_ROOTS
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_retired_candidate_csv_names_exist_only_in_metadata_classifiers() -> None:
    violations: list[str] = []
    for path in _production_python_files():
        text = path.read_text(encoding="utf-8").lower()
        matches = [item for item in RETIRED_CANDIDATE_CSV_FRAGMENTS if item in text]
        if matches and path not in LEGACY_METADATA_CLASSIFIERS:
            violations.append(f"{path.relative_to(ROOT)}: {', '.join(matches)}")

    assert violations == []


def test_legacy_candidate_metadata_classifiers_never_parse_csv_bytes() -> None:
    for path in LEGACY_METADATA_CLASSIFIERS:
        text = path.read_text(encoding="utf-8")
        assert "read_csv(" not in text
        assert "to_csv(" not in text


def test_candidate_producers_have_no_csv_output_adapter_parameters() -> None:
    from src.application.cc_lp_steps import run_cc_lp_scan
    from src.application.combo_yield_steps import (
        run_cc_lp_variant,
        run_combo_yield_scan_and_summarize,
    )
    from src.application.scan_sell_call import run_sell_call_scan
    from src.application.scan_sell_put import run_sell_put_scan
    from src.application.sell_call_steps import run_sell_call_scan_and_summarize
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs
    from src.application.sell_put_cash import enrich_sell_put_candidates_with_cash
    from src.application.sell_put_steps import run_sell_put_scan_and_summarize

    forbidden_by_callable = {
        run_sell_put_scan: {
            "output",
            "output_path",
            "reject_log",
            "reject_log_output",
            "reject_log_path",
        },
        run_sell_call_scan: {
            "output",
            "output_path",
            "reject_log",
            "reject_log_output",
            "reject_log_path",
        },
        run_sell_put_scan_and_summarize: {
            "base",
            "report_dir",
            "symbol_lower",
            "yield_enhancement_sell_put_cfg",
        },
        run_sell_call_scan_and_summarize: {"base", "report_dir", "symbol_lower"},
        run_combo_yield_scan_and_summarize: {
            "sell_put_labeled_path",
            "label_put_candidates_fn",
            "attach_calls_fn",
        },
        run_cc_lp_variant: {"report_dir", "output", "output_path"},
        run_cc_lp_scan: {"report_dir", "output", "output_path"},
        find_sell_put_yield_enhancement_pairs: {"output", "output_path"},
        enrich_sell_put_candidates_with_cash: {"out_path", "output", "output_path"},
    }
    for callable_obj, forbidden in forbidden_by_callable.items():
        parameters = set(inspect.signature(callable_obj).parameters)
        assert parameters.isdisjoint(forbidden), callable_obj.__qualname__


@pytest.mark.parametrize(
    ("module_name", "argv"),
    (
        (
            "src.application.scan_sell_put",
            ["--symbols", "NVDA", "--min-annualized-net-return", "0.1"],
        ),
        (
            "src.application.scan_sell_call",
            [
                "--symbols",
                "NVDA",
                "--avg-cost",
                "100",
                "--shares",
                "100",
                "--min-annualized-net-return",
                "0.1",
            ],
        ),
    ),
)
@pytest.mark.parametrize(
    "retired_args",
    (
        ("--output", "retired.csv"),
        ("--reject-log-output", "retired.csv"),
        ("--quiet",),
    ),
)
def test_scanner_cli_rejects_retired_candidate_csv_flags(
    module_name: str,
    argv: list[str],
    retired_args: tuple[str, ...],
) -> None:
    module = __import__(module_name, fromlist=["parse_args"])
    with pytest.raises(SystemExit):
        module.parse_args([*argv, *retired_args])


def test_removed_csv_only_adapters_stay_absent() -> None:
    for relative in (
        "src/application/render_sell_put_alerts.py",
        "src/application/render_sell_call_alerts.py",
        "src/application/portfolio_capacity_shadow.py",
    ):
        assert not (ROOT / relative).exists()


def test_combo_output_mode_exists_only_as_a_targeted_validation_error() -> None:
    matches = []
    for path in (ROOT / "src" / "application").rglob("*.py"):
        if "output_mode" in path.read_text(encoding="utf-8"):
            matches.append(path.relative_to(ROOT).as_posix())
    assert matches == ["src/application/config_validator.py"]


def test_allow_stale_config_cannot_revive_removed_combo_output_mode() -> None:
    from src.application.config_validator import (
        validate_resolved_watchlist_item_runtime_config,
    )

    resolved = {
        "symbol": "NVDA",
        "sell_put": {"enabled": True},
        "sell_call": {"enabled": False},
        "combo_yield": {"enabled": True, "output_mode": "separate"},
    }
    with pytest.raises(SystemExit, match="output_mode has been removed"):
        validate_resolved_watchlist_item_runtime_config(resolved)


def test_v1_strategy_status_name_exists_only_in_history_classifiers() -> None:
    matches = []
    for path in (ROOT / "src" / "application").rglob("*.py"):
        if "strategy_scan_status_index.v1" in path.read_text(encoding="utf-8"):
            matches.append(path)
    assert set(matches) == LEGACY_METADATA_CLASSIFIERS
