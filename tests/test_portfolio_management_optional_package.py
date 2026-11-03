"""Run with om-pm-plugin explicitly installed in the test environment."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ABSENT_PLUGIN = '''
import sys
from importlib.abc import MetaPathFinder
class AbsentPM(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "om_pm_plugin" or fullname.startswith("om_pm_plugin."):
            raise ModuleNotFoundError("PM intentionally absent", name=fullname)
sys.meta_path.insert(0, AbsentPM())
'''


def _run(code: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True,
        text=True, timeout=60, env={**os.environ, "OM_SECRET_BACKEND": "env"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("entry,args", [
    ("src.interfaces.cli.main", ["help"]),
    ("src.interfaces.agent.cli", ["spec"]),
])
def test_core_public_startup_without_plugin(entry, args):
    _run(ABSENT_PLUGIN + f'''
from importlib import import_module
assert import_module({entry!r}).main({args!r}) == 0
assert "om_pm_plugin" not in sys.modules
''')


def test_absent_plugin_preserves_core_exports_disabled_and_unavailable():
    _run(ABSENT_PLUGIN + '''
from src.application.portfolio_management import (
    resolve_portfolio_management_client, portfolio_management_failure_code,
    PORTFOLIO_MANAGEMENT_DISABLED, PORTFOLIO_MANAGEMENT_UNAVAILABLE,
)
from src.infrastructure.portfolio_management_client import (
    PortfolioManagementClient, PortfolioManagementError,
    PortfolioManagementUnavailableError, PortfolioManagementProtocolError,
    resolve_portfolio_service_origin, validate_holdings_refresh_response,
)
assert resolve_portfolio_service_origin("http://localhost:8765") == "http://localhost:8765"
assert resolve_portfolio_management_client({}) == PORTFOLIO_MANAGEMENT_DISABLED
try:
    validate_holdings_refresh_response({}, requested_account="sample", requested_request_id="r1")
except PortfolioManagementProtocolError:
    pass
else:
    raise AssertionError("validation was bypassed")
for construct in (
    PortfolioManagementClient,
    lambda: resolve_portfolio_management_client({"portfolio_management": {"enabled": True}}),
):
    try:
        construct()
    except PortfolioManagementUnavailableError as exc:
        assert isinstance(exc, PortfolioManagementError)
        assert portfolio_management_failure_code(exc) == PORTFOLIO_MANAGEMENT_UNAVAILABLE
        assert "om-pm-plugin" in str(exc)
    else:
        raise AssertionError("missing plugin was accepted")
assert "om_pm_plugin" not in sys.modules
''')


def test_installed_plugin_remains_unloaded_when_disabled():
    _run('''
import sys
from importlib.metadata import version
assert version("om-pm-plugin")
from src.application.portfolio_management import resolve_portfolio_management_client, PORTFOLIO_MANAGEMENT_DISABLED
from src.infrastructure.portfolio_management_client import PortfolioManagementClient
assert resolve_portfolio_management_client({"portfolio_management": {"enabled": False}}) == PORTFOLIO_MANAGEMENT_DISABLED
assert "om_pm_plugin" not in sys.modules
''')


def test_installed_plugin_uses_shared_errors_and_compatibility_constructor():
    from importlib.metadata import distribution

    import om_pm_plugin
    from src.application.portfolio_management import resolve_portfolio_management_client
    from src.application.portfolio_management_contract import PortfolioManagementConfigError
    from src.infrastructure.portfolio_management_client import PortfolioManagementClient

    dist = distribution("om-pm-plugin")
    assert not any(str(path).startswith("src/application") for path in dist.files)
    client = resolve_portfolio_management_client({"portfolio_management": {"enabled": True}})
    assert isinstance(client, om_pm_plugin.PortfolioManagementClient)
    assert isinstance(PortfolioManagementClient(), om_pm_plugin.PortfolioManagementClient)
    with pytest.raises(PortfolioManagementConfigError):
        PortfolioManagementClient(service_url="https://remote.invalid")


def test_injected_client_still_bypasses_optional_package():
    _run(ABSENT_PLUGIN + '''
from src.application.portfolio_management import resolve_portfolio_management_client
client = object()
assert resolve_portfolio_management_client({}, client=client) is client
''')
