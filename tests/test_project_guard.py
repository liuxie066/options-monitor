from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.multi_tick.project_guard import (  # noqa: E402
    admit_project_run,
    apply_project_load_shed,
    project_guard_state_path,
    record_project_failure,
    record_project_success,
)


def _cfg(**kwargs):
    base = {
        'project_guard': {
            'enabled': True,
            'min_run_interval_sec': 1,
            'circuit_failure_threshold': 2,
            'circuit_window_sec': 600,
            'circuit_open_sec': 120,
            'probe_max_accounts': 1,
        }
    }
    base['project_guard'].update(kwargs)
    return base


def test_project_guard_rate_limit() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        cfg = _cfg(min_run_interval_sec=10)
        a1 = admit_project_run(root, cfg)
        assert a1['allowed'] is True
        a2 = admit_project_run(root, cfg)
        assert a2['allowed'] is False
        assert a2['error_code'] == 'PROJECT_RATE_LIMIT'


def test_project_guard_circuit_and_half_open_probe_load_shed() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        cfg = _cfg(min_run_interval_sec=0, circuit_failure_threshold=2, circuit_open_sec=120, probe_max_accounts=1)

        assert admit_project_run(root, cfg)['allowed'] is True
        f1 = record_project_failure(root, cfg, error_code='OPEND_NOT_READY', stage='watchdog')
        assert f1['opened'] is False
        f2 = record_project_failure(root, cfg, error_code='OPEND_NOT_READY', stage='watchdog')
        assert f2['opened'] is True
        assert admit_project_run(root, cfg)['allowed'] is False

        p = project_guard_state_path(root)
        st = json.loads(p.read_text(encoding='utf-8'))
        st['circuit_open_until_utc'] = '2000-01-01T00:00:00+00:00'
        p.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')

        probe = admit_project_run(root, cfg)
        assert probe['allowed'] is True
        assert probe['mode'] == 'probe'
        assert apply_project_load_shed(['a', 'b', 'c'], probe) == ['a']


def test_project_guard_success_closes_circuit() -> None:
    with TemporaryDirectory() as td:
        root = Path(td)
        cfg = _cfg(min_run_interval_sec=0, circuit_failure_threshold=1)
        assert admit_project_run(root, cfg)['allowed'] is True
        assert record_project_failure(root, cfg, error_code='SCHEDULER_FAILED', stage='scheduler')['opened'] is True
        out = record_project_success(root, cfg)
        assert out['state'] == 'closed'
        assert admit_project_run(root, cfg)['allowed'] is True
