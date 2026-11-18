"""The public module entry keeps the supervisor lean without changing CLI behavior."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _run(body: str, tmp_path: Path) -> None:
    bootstrap = '''
import sys
sys.path.insert(0, sys.argv[1])
def no_external_effects(event, args):
    if event in {"socket.connect", "socket.bind", "subprocess.Popen"}:
        raise AssertionError("unexpected external effect: " + event)
sys.addaudithook(no_external_effects)
'''
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", bootstrap + body, str(ROOT), str(tmp_path)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "OM_SECRET_BACKEND": "env"},
        text=True, capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_module_trade_entry_keeps_scan_dependencies_out_of_parent(tmp_path):
    _run('''
import importlib.abc, os, runpy
from pathlib import Path
from src.application.trades import process_supervisor
blocked = {"pandas", "src.application.pipeline_runtime", "src.application.multi_account_tick",
           "src.application.trades.auto_intake", "src.interfaces.cli.run_ops"}
class DenyHeavy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in blocked:
            raise AssertionError("trade supervisor loaded unrelated dependency: " + fullname)
sys.meta_path.insert(0, DenyHeavy())
root = Path(sys.argv[2])
calls = []
def supervised(argv):
    calls.append(argv)
    assert os.environ["OM_RUNTIME_ROOT"] == str(root)
    assert not blocked.intersection(sys.modules)
    return 78
process_supervisor.run_trade_intake_process = supervised
sys.argv = ["om", "run", "trade-intake", "--config", str(root / "fixture.json"),
            "--runtime-root", str(root), "--mode", "apply", "--yes"]
try:
    runpy.run_module("src.interfaces.cli.main", run_name="__main__")
except SystemExit as exc:
    assert exc.code == 78
else:
    raise AssertionError("module entry did not propagate supervisor status")
assert calls == [["--config", str(root / "fixture.json"), "--runtime-root", str(root),
                  "--mode", "apply", "--yes"]]
assert "OM_RUNTIME_ROOT" not in os.environ
''', tmp_path)


def test_module_trade_entry_matches_full_cli_and_bootstraps_once(tmp_path):
    _run('''
import contextlib, io, json, os, runpy
from pathlib import Path
from src.application.agent_tool_contracts import AgentToolError
from src.application.trades import process_supervisor
from src.interfaces.cli import command_environment as environment
from src.interfaces.cli.main import main
root = Path(sys.argv[2])
config = str(root / "missing-fixture.json")
(root / "options-monitor.env").write_text("OM_CLI_TEST_MARKER=fixture\\n")
bootstrap = environment.bootstrap_process_env
calls, boots = [], []
failure = None
bootstrap_failure = False
def observed_bootstrap(**kwargs):
    boots.append(str(kwargs.get("env_file")))
    if bootstrap_failure:
        raise ValueError("fixture environment error")
    return bootstrap(**kwargs)
environment.bootstrap_process_env = observed_bootstrap
def supervised(argv):
    calls.append((argv, os.environ.get("OM_RUNTIME_ROOT"), os.environ.get("OM_CLI_TEST_MARKER")))
    if failure == "agent":
        raise AgentToolError(code="FIXTURE_ERROR", message="fixture failure")
    if failure == "value":
        raise ValueError("fixture business value error")
    if failure == "exit":
        raise SystemExit(78)
    return 78
process_supervisor.run_trade_intake_process = supervised

def invoke(argv, module):
    calls.clear(); boots.clear()
    before = dict(os.environ)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            if module:
                sys.modules.pop("src.interfaces.cli.main", None)
                sys.argv = ["om", *argv]
                runpy.run_module("src.interfaces.cli.main", run_name="__main__")
                code = 0
            else:
                code = main(argv, discover_local=True)
        except SystemExit as exc:
            code = exc.code
    assert dict(os.environ) == before
    assert len(calls) <= 1 and len(boots) <= 1
    return code, out.getvalue(), err.getvalue(), list(calls), list(boots)

cases = [
    ["--help"], [], ["--once"], ["--config", config, "--unknown"],
    ["--config", config, "--port", "invalid"], ["--config", config, "--mode", "bad"],
    ["--config", config], ["--config", config, "--hel"],
    ["--config", config, "--mode", "apply", "--yes"],
    ["attribution-migrate", "--config", config, "--effective-from-ms", "123", "--actor", "fixture",
     "--manifest", "plan.json", "--backup-path", "backup", "--writers-stopped"],
    ["--config", config, "--account", "lx", "--recover-skipped", "--expected-recovery-hash", "hash",
     "--retry-failed", "--reconcile-state", "--compensate-receipts", "--compensation-reason", "fixture",
     "--expected-payload-hash", "payload", "--deal-id", "a", "--deal-id", "b", "--apply"],
    ["--config", config, "--runtime-root", str(root / "explicit"), "--data-config", "data.json",
     "--state-path", "state.json", "--audit-path", "audit.json", "--status-path", "status.json",
     "--host", "fixture", "--port", "11111", "--once", "--deal-json", "deal.json",
     "--execution-file", "execution.json", "--inbox-id", "id", "--dry-run", "--confirm"],
]
for args in cases:
    argv = ["run", "trade-intake", *args]
    expected = invoke(argv, False)
    actual = invoke(argv, True)
    assert actual == expected, (args, actual, expected)
for failure in ("agent", "value", "exit"):
    argv = ["run", "trade-intake", "--config", config]
    expected = invoke(argv, False)
    assert len(expected[3]) == 1 and len(expected[4]) == 1
    assert invoke(argv, True) == expected, failure
failure = None
bootstrap_failure = True
for args in (["--config", config], ["--config", config, "--unknown"], ["--config", config, "--hel"]):
    argv = ["run", "trade-intake", *args]
    expected = invoke(argv, False)
    assert expected[0] == 2 and not expected[3] and len(expected[4]) == 1
    assert invoke(argv, True) == expected
bootstrap_failure = False
# Other commands and an incomplete prefix remain on the ordinary full parser.
for argv in (["run", "--help"], ["run", "tick", "--help"], ["run", "trade"]):
    assert invoke(list(argv), True) == invoke(list(argv), False)
''', tmp_path)
