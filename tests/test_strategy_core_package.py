"""Build and execute outside the checkout, using only offline build tooling."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
import zipfile

import pytest

from om_strategy_core import __version__

ROOT = Path(__file__).resolve().parents[1]


def run(python, code, *, cwd, env=None):
    result = subprocess.run([str(python), "-c", code], cwd=cwd, env=env, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    work = tmp_path_factory.mktemp("strategy-core-wheel")
    source = work / "source"
    source.mkdir()
    for name in ("domain", "om_strategy_core"):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT / "pyproject.toml", source / "pyproject.toml")
    # Build tools may live outside the project venv; no network installation.
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    try:
        import setuptools
    except ImportError:
        tooling = Path(sys.base_prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        assert (tooling / "setuptools/build_meta.py").is_file(), "offline setuptools>=77 is required"
        env["PYTHONPATH"] = str(tooling)
    else:
        env["PYTHONPATH"] = str(Path(setuptools.__file__).resolve().parent.parent)
    wheel_dir = work / "wheels"
    wheel_dir.mkdir()
    run(sys.executable, f"from setuptools.build_meta import build_wheel; build_wheel({str(wheel_dir)!r})", cwd=source, env=env)
    wheel = next(wheel_dir.glob("*.whl"))
    assert not (source / "om_strategy_core/_build_manifest.json").exists()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert all(name.startswith(("domain/", "om_strategy_core/", "om_strategy_core-")) for name in names)
        assert not any(name.startswith(("domain/storage/", "domain/services/", "src/", "scripts/")) for name in names)
        metadata = archive.read(next(name for name in names if name.endswith("/METADATA"))).decode()
        assert "Requires-Python: >=3.12" in metadata
        assert "Requires-Dist:" not in metadata
        first_manifest = archive.read("om_strategy_core/_build_manifest.json")
    # Repeated builds must seal the same bytes regardless of path or timestamps.
    second = work / "second"
    second.mkdir()
    other_source = work / "other-source"
    other_source.mkdir()
    for name in ("domain", "om_strategy_core"):
        shutil.copytree(source / name, other_source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(source / "pyproject.toml", other_source / "pyproject.toml")
    run(sys.executable, f"from setuptools.build_meta import build_wheel; build_wheel({str(second)!r})", cwd=other_source, env=env)
    with zipfile.ZipFile(next(second.glob("*.whl"))) as archive:
        assert archive.read("om_strategy_core/_build_manifest.json") == first_manifest
    target = work / "venv"
    venv.EnvBuilder(with_pip=True).create(target)
    python = target / "bin/python"
    clean_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
                            cwd=work, env=clean_env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    shutil.copyfile(ROOT / "tests/fixtures/strategy_core_baseline.json", work / "baseline.json")
    return work, source, python, clean_env, env


def test_installed_core_calculates_without_om_or_dependencies(installed):
    work, source, python, env, _ = installed
    output = run(python, '''
import importlib.util, json, sys
from datetime import datetime
from pathlib import Path
from om_strategy_core import *
from domain.domain.engine.candidate_engine import CandidateCalculationError
assert importlib.util.find_spec('pandas') is None
assert importlib.util.find_spec('futu') is None
assert importlib.util.find_spec('src') is None
baseline = json.loads(Path('baseline.json').read_text())
for c in baseline['cases']:
    try:
        result = calculate_metrics(c['row'], mode=c['mode'], avg_cost=c['avg_cost'],
            now_utc=datetime.fromisoformat(c['now_utc']), exchange_rates=ExchangeRates(**c['rates']))
    except CandidateCalculationError as exc:
        assert exc.to_payload() == c['error']
    else:
        assert result == {**c['metrics'], **c['cny_fields']}
        recorded = {**c['row'], 'decision_now_utc': c['now_utc']}
        assert calculate_metrics(recorded, mode=c['mode'], avg_cost=c['avg_cost'],
            exchange_rates=ExchangeRates(**c['rates'])) == result
        policy = resolve_strategy(mode=c['mode'], config=InsuranceUnderwritingConfig())
        assert evaluate_candidates([c['policy_row']], strategy=policy)['decisions'] == [c['decision']]
        constrained = calculate_constraints({**c['policy_row'], 'avg_cost': c['avg_cost']}, mode=c['mode'],
            risk_ctx=ShortVolPortfolioContext(None, {}, {}, None), exchange_rate_converter=CurrencyConverter(ExchangeRates(**c['rates'])))
        assert constrained['concentration_evaluable'] is False
assert not any(name.startswith(('pandas', 'futu', 'src.', 'domain.storage', 'domain.services')) for name in sys.modules)
case = next(c for c in baseline['cases'] if c['mode'] == 'call' and c.get('decision', {}).get('accepted'))
row = {k: v for k, v in case['policy_row'].items() if not k.startswith('policy_') and k != 'effective_min_strike'}
row['avg_cost'] = 130.0
policy = resolve_strategy(mode='call', config=InsuranceUnderwritingConfig())
try:
    evaluate_candidates([row], strategy=policy)
except ValueError as exc:
    assert 'calculate_constraints before evaluation' in str(exc)
else:
    raise AssertionError('unprepared CC candidate accepted')
prepared = calculate_constraints(row, mode='call', strategy=policy,
    risk_ctx=ShortVolPortfolioContext(None, {}, {}, None),
    exchange_rate_converter=CurrencyConverter(ExchangeRates(**case['rates'])))
decision = evaluate_candidates([prepared], strategy=policy)['decisions'][0]
assert not decision['accepted']
assert any(r['reason'] == 'hard_strike' and r['threshold'] == 132.6 for r in decision['opening_decision']['rejects'])
identity = runtime_identity(mode='installed')
assert identity['mode'] == 'installed' and len(identity['build_digest']) == 64
print(json.dumps(identity))
''', cwd=work, env=env)
    assert json.loads(output)["core_version"] == __version__


def test_source_shadow_and_mixed_imports_fail_installed_verification(installed):
    work, source, python, env, _ = installed
    code = '''
from om_strategy_core import runtime_identity
assert runtime_identity()['build_digest'] is None
try:
    runtime_identity(mode='installed')
except ValueError as exc:
    assert 'installed core verification failed' in str(exc)
else:
    raise AssertionError('source claimed installed identity')
'''
    run(python, code, cwd=source, env=env)
    run(python, f'''
import domain.domain.strategy_core as core
from om_strategy_core import runtime_identity
core.__file__ = {str(source / 'domain/domain/strategy_core.py')!r}
try:
    runtime_identity(mode='installed')
except ValueError as exc:
    assert 'shadowed' in str(exc)
else:
    raise AssertionError('mixed import claimed installed identity')
''', cwd=work, env=env)


@pytest.mark.parametrize("target", ["domain/domain/currency_conversion.py", "om_strategy_core/_build_manifest.json"])
def test_installed_tampering_fails_closed(installed, target):
    work, _, python, env, _ = installed
    run(python, f'''
from importlib.metadata import distribution
from pathlib import Path
from om_strategy_core import runtime_identity
path = Path(distribution('om-strategy-core').locate_file({target!r}))
original = path.read_bytes()
try:
    path.write_bytes(original + b'\\n# tampered\\n')
    try:
        runtime_identity(mode='installed')
    except ValueError:
        pass
    else:
        raise AssertionError('tampered installation accepted')
finally:
    path.write_bytes(original)
''', cwd=work, env=env)


def test_editable_has_no_artifact_digest(installed):
    work, source, _, clean_env, build_env = installed
    wheel_dir = work / "editable-wheel"
    wheel_dir.mkdir()
    run(sys.executable, f"from setuptools.build_meta import build_editable; build_editable({str(wheel_dir)!r})", cwd=source, env=build_env)
    assert not (source / "om_strategy_core/_build_manifest.json").exists()
    target = work / "editable-venv"
    venv.EnvBuilder(with_pip=True).create(target)
    python = target / "bin/python"
    wheel = next(wheel_dir.glob("*.whl"))
    result = subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
                            cwd=work, env=clean_env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    run(python, '''
from om_strategy_core import runtime_identity
assert runtime_identity()['build_digest'] is None
try:
    runtime_identity(mode='installed')
except ValueError:
    pass
else:
    raise AssertionError('editable claims installed identity')
''', cwd=work, env=clean_env)


def test_installed_core_period_cashflow_without_application_or_network(installed):
    work, _, python, env, _ = installed
    output = run(python, '''
import importlib.util, json, socket, sqlite3, sys
from decimal import Decimal
from om_strategy_core import (ContractKey, TradeEvent, PeriodWindow, calculate_option_performance,
                              cashflow_input_digest, runtime_identity)
def forbidden(*args, **kwargs):
    raise AssertionError('offline I/O')
socket.socket = forbidden
sqlite3.connect = forbidden
key = ContractKey.from_values(broker='futu', account='lx', underlying_symbol='NVDA',
                             option_type='put', strike=100, expiration_ymd='2026-09-30')
opening = TradeEvent(event_id='o', event_type='open', event_time_ms=1788220799000,
    contract_key=key, contracts=1, price=Decimal('2'), currency='USD', source='frozen', multiplier=100,
    lot_id='lot', raw_payload={'side':'sell','fee_provenance':{'basis':'actual','amount':0,'source':'test'}})
closing = TradeEvent(event_id='c', event_type='close', event_time_ms=1788307200000,
    contract_key=key, contracts=1, price=Decimal('3'), currency='USD', source='frozen', multiplier=100,
    target_lot_id='lot', raw_payload={'side':'buy','close_type':'buy_to_close',
    'fee_provenance':{'basis':'actual','amount':0,'source':'test'}})
period = PeriodWindow('month','Asia/Shanghai','2026-09-01','2026-09-30',1788220800000,
                      1790812800000,1788220799999,1790812799999,'complete_past')
result = calculate_option_performance([opening, closing],period=period,accounts=['LX'],broker='FUTU')
assert result.bundle['period_option_net_cashflow']['by_currency']['USD']['total']['amount'] == -300
assert result.bundle['option_net_cashflow']['by_currency'] == {}
assert len(cashflow_input_digest(opening)) == 64
assert importlib.util.find_spec('src') is None
assert not any(name.startswith(('pandas','futu','src.','domain.storage','domain.services')) for name in sys.modules)
identity = runtime_identity(mode='installed')
assert len(identity['build_digest']) == 64
print(json.dumps(identity))
''', cwd=work, env=env)
    assert json.loads(output)['mode'] == 'installed'
