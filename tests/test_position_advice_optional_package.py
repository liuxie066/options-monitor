"""Optional package boundary; run with the dev packages explicitly installed."""
from __future__ import annotations

import ast
from dataclasses import replace
from importlib.metadata import distribution
import os
from pathlib import Path
import subprocess
import sys

import pytest

from domain.domain.close_advice import evaluate_close_advice, select_close_advice_notification_rows
from tests.test_strict_close_advice import _input


ROOT = Path(__file__).resolve().parents[1]
ABSENT_PLUGIN = '''
import sys
from importlib.abc import MetaPathFinder
class AbsentAdvice(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "om_position_advice_plugin" or fullname.startswith("om_position_advice_plugin."):
            raise ModuleNotFoundError("advice deliberately absent", name=fullname)
sys.meta_path.insert(0, AbsentAdvice())
'''


def _run(code: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
        timeout=60, env={**os.environ, "OM_SECRET_BACKEND": "env"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("entry,args", [
    ("src.interfaces.cli.main", ["help"]),
    ("src.interfaces.agent.cli", ["spec"]),
])
def test_core_public_startup_without_advice(entry, args):
    _run(ABSENT_PLUGIN + f'''
from importlib import import_module
assert import_module({entry!r}).main({args!r}) == 0
assert "om_position_advice_plugin" not in sys.modules
''')


def test_absent_advice_preserves_core_inputs_helpers_and_history_import():
    _run(ABSENT_PLUGIN + '''
from domain.domain.close_advice import (
    CloseAdviceInput, PositionAdviceUnavailableError, evaluate_close_advice,
    safe_float, safe_int, sort_advice_rows, select_close_advice_notification_rows,
)
from src.application.position_advice_plugin import position_advice_plugin_status
import src.application.agent_tools.close_advice_read_impl
from tests.test_strict_close_advice import _input
inp = _input()
assert isinstance(inp, CloseAdviceInput) and inp.input_source == "ledger"
assert inp.input_id is None and inp.input_as_of_utc is None
assert safe_float("nan") is None and safe_int(True) is None
assert sort_advice_rows([]) == [] and select_close_advice_notification_rows([]) == []
assert position_advice_plugin_status({"close_advice": {"enabled": True}}) == "unavailable"
try:
    evaluate_close_advice(inp)
except PositionAdviceUnavailableError as exc:
    assert "POSITION_ADVICE_UNAVAILABLE" in str(exc) and "om-position-advice-plugin" in str(exc)
else:
    raise AssertionError("absent policy was used")
assert "om_position_advice_plugin" not in sys.modules
''')


def test_disabled_status_never_probes_or_loads_even_if_installed():
    _run('''
import sys
from importlib.metadata import version
import src.application.position_advice_plugin as boundary
assert version("om-position-advice-plugin")
def forbidden(*args, **kwargs): raise AssertionError("disabled package was probed")
boundary.find_spec = forbidden
for config in (None, {}, {"close_advice": {"enabled": False}}):
    assert boundary.position_advice_plugin_status(config) == "disabled"
assert "om_position_advice_plugin" not in sys.modules
''')


def test_installed_status_checks_spec_without_importing_policy():
    _run('''
import sys
from src.application.position_advice_plugin import position_advice_plugin_status, require_position_advice_plugin
assert position_advice_plugin_status({"close_advice": {"enabled": True}}) == "available"
assert "om_position_advice_plugin" not in sys.modules
plugin = require_position_advice_plugin()
assert callable(plugin.evaluate_close_advice) and callable(plugin.format_close_advice_reason)
''')


def test_broken_installed_policy_raises_explicit_unavailable(tmp_path):
    package = tmp_path / "om_position_advice_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("raise ImportError('incompatible optional package')\n")
    _run(f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n" + '''
from src.application.position_advice_plugin import require_position_advice_plugin, PositionAdviceUnavailableError
try:
    require_position_advice_plugin()
except PositionAdviceUnavailableError as exc:
    assert isinstance(exc.__cause__, ImportError)
else:
    raise AssertionError("broken policy was accepted")
''')


@pytest.mark.parametrize("option_type,spot", [("put", 120), ("call", 80)])
def test_manual_identity_has_same_economics_without_ledger_lot(option_type, spot):
    ledger = evaluate_close_advice(_input(option_type=option_type, spot=spot))
    manual = evaluate_close_advice(_input(
        option_type=option_type, spot=spot, position_lot_id=None,
        input_source="user_provided", input_id="holding-1", input_as_of_utc="2026-06-01T14:30:00Z",
    ))
    identity = {"position_lot_id", "input_source", "input_id", "input_as_of_utc"}
    assert {k: v for k, v in manual.items() if k not in identity} == {
        k: v for k, v in ledger.items() if k not in identity
    }
    assert manual["position_lot_id"] is None
    assert manual["input_source"] == "user_provided"
    assert manual["input_id"] == "holding-1"
    assert manual["input_as_of_utc"] == "2026-06-01T14:30:00Z"


@pytest.mark.parametrize("overrides,flag", [
    ({"position_lot_id": None}, "missing_position_lot_id"),
    ({"position_lot_id": None, "input_source": "user_provided"}, "missing_input_id"),
    ({"input_source": "user_provided", "input_id": "manual-1"}, "manual_position_has_ledger_lot"),
    ({"input_source": "unknown"}, "invalid_input_source"),
])
def test_identity_errors_are_not_evaluable(overrides, flag):
    row = evaluate_close_advice(_input(**overrides))
    assert row["recommendation_state"] == "not_evaluable"
    assert flag in row["data_quality_flags"]


@pytest.mark.parametrize("option_type,spot,otm", [("put", 120, True), ("call", 120, False)])
@pytest.mark.parametrize("missing", [
    "premium", "estimated_open_fee", "estimated_close_fee", "fee_calc_status", "fee_calc_basis",
])
def test_missing_cost_or_fees_keeps_independent_diagnostics_only(option_type, spot, otm, missing):
    inp = replace(_input(option_type=option_type, spot=spot), **{missing: None})
    row = evaluate_close_advice(inp)
    assert row["recommendation_state"] == "not_evaluable"
    assert row["is_otm"] is otm
    assert row["spread_ratio"] == pytest.approx((0.08 - 0.07) / 0.075)
    assert row["bid"] == 0.07 and row["ask"] == 0.08 and row["dte"] == 30
    for metric in ("net_capture_ratio", "estimated_pnl_if_close_net", "remaining_max_annualized_return"):
        assert row[metric] is None


def test_invalid_quote_does_not_acquire_plausible_spread():
    row = evaluate_close_advice(_input(bid=0.09, ask=0.08, estimated_close_fee=None))
    assert row["recommendation_state"] == "not_evaluable"
    assert row["is_otm"] is True
    assert row["spread_ratio"] is None


@pytest.mark.parametrize("mode", ["historical", "unavailable", "", None, "CURRENT"])
def test_explicit_noncurrent_rows_are_never_notifiable(mode):
    row = evaluate_close_advice(_input())
    assert select_close_advice_notification_rows([row]) == [row]  # legacy report compatibility
    assert select_close_advice_notification_rows([{**row, "assessment_mode": "current"}])
    assert select_close_advice_notification_rows([{**row, "assessment_mode": mode}]) == []


def test_installed_distribution_contains_policy_and_presentation_only():
    import om_position_advice_plugin

    dist = distribution("om-position-advice-plugin")
    assert not any(str(path).startswith(("domain/", "src/", "scripts/")) for path in dist.files)
    for source in Path(om_position_advice_plugin.__file__).parent.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            modules = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                       [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(m.split(".")[0] in {"src", "scripts"} for m in modules)
    assert "./plugins/position-advice" in (ROOT / "requirements/dev.txt").read_text()
    assert "position-advice" not in (ROOT / "requirements/runtime.txt").read_text()
