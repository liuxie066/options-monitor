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


def test_legacy_wrapper_modules_are_removed() -> None:
    assert not (ROOT / "scripts" / "trade_symbol_identity.py").exists()
    assert not (ROOT / "scripts" / "trade_contract_identity.py").exists()
    assert not (ROOT / "scripts" / "option_positions_core" / "domain.py").exists()
    assert not (ROOT / "scripts" / "option_positions_core" / "ledger.py").exists()
    assert not (ROOT / "scripts" / "option_positions_core" / "service.py").exists()
    assert not (ROOT / "scripts" / "option_positions_core" / "reporting.py").exists()
    assert not (ROOT / "scripts" / "option_positions_core" / "__init__.py").exists()
    assert not (ROOT / "scripts" / "feishu_bitable.py").exists()
    assert not (ROOT / "scripts" / "exchange_rates.py").exists()
    assert not (ROOT / "scripts" / "multiplier_cache.py").exists()
    assert not (ROOT / "scripts" / "sync_option_positions_to_feishu.py").exists()
    assert not (ROOT / "scripts" / "trade_account_identity.py").exists()
    assert not (ROOT / "scripts" / "trade_account_mapping.py").exists()
    assert not (ROOT / "scripts" / "trade_event_normalizer.py").exists()
    assert not (ROOT / "scripts" / "config_loader.py").exists()
    assert not (ROOT / "scripts" / "account_config.py").exists()
    assert not (ROOT / "scripts" / "validate_config.py").exists()
    assert not (ROOT / "scripts" / "io_utils.py").exists()
    assert not (ROOT / "scripts" / "run_log.py").exists()
    assert not (ROOT / "scripts" / "subprocess_utils.py").exists()
    assert not (ROOT / "scripts" / "logging_config.py").exists()
    assert not (ROOT / "scripts" / "futu_gateway.py").exists()
    assert not (ROOT / "scripts" / "opend_utils.py").exists()
    assert not (ROOT / "scripts" / "opend_normalize.py").exists()
    assert not (ROOT / "scripts" / "required_data_validate.py").exists()
    assert not (ROOT / "scripts" / "candidate_defaults.py").exists()
    assert not (ROOT / "scripts" / "report_builders.py").exists()
    assert not (ROOT / "scripts" / "summary_formatting.py").exists()
    assert not (ROOT / "scripts" / "report_formatting.py").exists()
    assert not (ROOT / "scripts" / "alert_policy.py").exists()
    assert not (ROOT / "scripts" / "alert_rules.py").exists()
    assert not (ROOT / "scripts" / "scan_scheduler.py").exists()
    assert not (ROOT / "scripts" / "cash_secured_utils.py").exists()
    assert not (ROOT / "scripts" / "fee_calc.py").exists()
    assert not (ROOT / "scripts" / "close_advice").exists()
    assert not (ROOT / "scripts" / "fetch_market_data_opend.py").exists()
    assert not (ROOT / "scripts" / "query_sell_put_cash.py").exists()
    assert not (ROOT / "scripts" / "fetch_option_positions_context.py").exists()
    assert not (ROOT / "scripts" / "fetch_portfolio_context.py").exists()
    assert not (ROOT / "scripts" / "portfolio_context_service.py").exists()
    assert not (ROOT / "scripts" / "futu_portfolio_context.py").exists()
    assert not (ROOT / "scripts" / "pipeline_context.py").exists()
    assert not (ROOT / "scripts" / "pipeline_alert_steps.py").exists()
    assert not (ROOT / "scripts" / "multi_tick").exists()
    assert not (ROOT / "scripts" / "infra" / "service.py").exists()
    assert not (ROOT / "scripts" / "infra" / "entry_external.py").exists()
    assert not (ROOT / "scripts" / "infra").exists()
    assert not (ROOT / "scripts" / "domain" / "storage").exists()


def test_symbol_identity_has_no_runtime_config_file_io() -> None:
    text = (ROOT / "domain" / "domain" / "symbol_identity.py").read_text(encoding="utf-8")

    assert "config.us.json" not in text
    assert "config.hk.json" not in text
    assert "read_text" not in text
