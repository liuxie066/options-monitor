from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIDE_LANE_PREFIXES = (
    "src.application.research",
    "src.application.shadow_replay",
    "src.application.strategy_lab",
)


def _run_python(script: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _loaded_side_lane_modules(imports: str) -> list[str]:
    return json.loads(_run_python(f"""
import json
import sys
{imports}
prefixes = {SIDE_LANE_PREFIXES!r}
print(json.dumps(sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + '.') for prefix in prefixes)
)))
"""))


def test_tick_entry_imports_do_not_load_research_side_lanes() -> None:
    loaded = _loaded_side_lane_modules(
        "import src.application.multi_account_tick\n"
        "import src.application.tick_notification_flow\n"
        "import src.application.tick_cron"
    )

    assert loaded == []


def test_ordinary_cli_parser_loads_no_forbidden_side_lanes() -> None:
    loaded = _loaded_side_lane_modules(
        "from contextlib import redirect_stdout\n"
        "from io import StringIO\n"
        "from src.interfaces.cli.main import parse_args\n"
        "with redirect_stdout(StringIO()):\n"
        "    try:\n"
        "        parse_args(['--help'])\n"
        "    except SystemExit as exc:\n"
        "        assert exc.code == 0\n"
        "assert parse_args(['version']).command == 'version'"
    )

    assert set(loaded) <= {
        "src.application.research",
        "src.application.research.redaction",
    }


def test_research_package_exports_are_bound_lazy_forwarders() -> None:
    result = json.loads(_run_python("""
import inspect
import json
import sys

import src.application.research as research

owners = {
    'src.application.research.facade',
    'src.application.research.service',
}
initial_owners = sorted(owners.intersection(sys.modules))

import src.application.research.facade as facade

collect_signature = inspect.signature(research.run_research_collect) == inspect.signature(facade.run_research_collect)
facade.run_research_collect = lambda payload, *, repo_base_fn: {'owner': 'collect', 'payload': payload}
collect = research.run_research_collect({'scope': 'full'}, repo_base_fn=lambda: None)

import src.application.research.service as service

tool_signature = inspect.signature(research.research_tool) == inspect.signature(service.research_tool)
service.research_tool = lambda payload, **kwargs: ({'owner': 'tool', 'payload': payload}, [], {})
tool = research.research_tool(
    {'scope': 'full'},
    runtime_status_tool_fn=lambda payload: ({}, [], {}),
    load_runtime_config=lambda **kwargs: (None, {}),
    repo_base=lambda: None,
    mask_path=lambda value: None,
)

print(json.dumps({
    'initial_owners': initial_owners,
    'bound': all(name in vars(research) for name in research.__all__),
    'collect_signature': collect_signature,
    'tool_signature': tool_signature,
    'collect_owner': collect['owner'],
    'tool_owner': tool[0]['owner'],
}))
"""))

    assert result == {
        "initial_owners": [],
        "bound": True,
        "collect_signature": True,
        "tool_signature": True,
        "collect_owner": "collect",
        "tool_owner": "tool",
    }


def test_strategy_lab_handler_loads_after_command_selection() -> None:
    result = json.loads(_run_python("""
from contextlib import redirect_stdout
from io import StringIO
import json
import sys

from src.interfaces.cli.main import main

owner = 'src.application.strategy_lab.service'
before = owner in sys.modules
with redirect_stdout(StringIO()):
    rc = main(['strategy-lab', 'canary', '--profile-path', '/path/that/does/not/exist'])
print(json.dumps({'before': before, 'after': owner in sys.modules, 'rc': rc}))
"""))

    assert result == {"before": False, "after": True, "rc": 2}
