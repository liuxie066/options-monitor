"""Independent, opt-in acceptance with built official wheels and real offline SDK.

OM_CHANNEL_DISTRIBUTION_QA=1 enables local artifacts; ordinary CI skips explicitly.
No production runtime, credentials, platform endpoint, or package index is used.
"""
from __future__ import annotations

import hashlib
from importlib.metadata import Distribution
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

from src.application import plugin_release, plugin_runtime, runtime_bundle
from tests.plugin_release_helpers import refresh_entry, write_wheel

pytestmark = pytest.mark.skipif(os.environ.get("OM_CHANNEL_DISTRIBUTION_QA") != "1",
                                reason="requires local official wheels and offline real SDK wheelhouse")
ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path("/tmp/om-channel-dist-official-wheels")
WHEELHOUSE = Path("/tmp/om-channel-dist-sdk-wheels")
SDK = Path("/tmp/om-channel-dist-real-sdk")

# This counter survives swallowed exceptions and all validation subprocesses.
GUARD = r'''
import builtins, json, os, socket, sys
from pathlib import Path
def forbidden(label):
    def fail(*args, **kwargs):
        with open(os.environ['QA_FORBIDDEN'], 'a') as out:
            out.write(json.dumps({'attempt': label, 'pid': os.getpid()})+'\n')
        raise AssertionError('forbidden QA effect: '+label)
    return fail
socket.socket.connect = forbidden('socket.connect')
socket.socket.connect_ex = forbidden('socket.connect_ex')
socket.socket.sendto = forbidden('socket.sendto')
socket.create_connection = forbidden('socket.create_connection')
socket.getaddrinfo = forbidden('socket.getaddrinfo')
original_import = builtins.__import__
def guarded_import(*args, **kwargs):
    result = original_import(*args, **kwargs)
    sdk = sys.modules.get('lark_oapi')
    ws = sys.modules.get('lark_oapi.ws')
    client = getattr(ws, 'Client', None)
    if isinstance(client, type) and not getattr(client, '_qa_guard', False):
        client.__init__ = forbidden('SDK.Client.__init__')
        client.start = forbidden('SDK.Client.start')
        client._qa_guard = True
    dispatcher = getattr(sdk, 'EventDispatcherHandler', None)
    if isinstance(dispatcher, type) and not getattr(dispatcher, '_qa_guard', False):
        dispatcher.builder = staticmethod(forbidden('SDK.dispatcher.builder'))
        dispatcher._qa_guard = True
    return result
builtins.__import__ = guarded_import
if os.environ.get('QA_AMBIENT'):
    sys.path.append(os.environ['QA_AMBIENT'])
'''

DRIVER = r'''
import io, runpy, subprocess
root, assets, action = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3])
sys.path.insert(0, str(root))
from src.application import plugin_distribution as distribution, plugin_runtime as runtime
from src.interfaces.cli import main as cli
catalog = json.loads((assets/'om-plugins.json').read_text())
tag = catalog['tag']
base = 'https://github.com/liuxie066/options-monitor/releases/download/'+tag+'/'
release = {'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': [
    {'name': p.name, 'state': 'uploaded', 'size': p.stat().st_size,
     'digest': 'sha256:'+__import__('hashlib').sha256(p.read_bytes()).hexdigest(),
     'browser_download_url': base+p.name} for p in assets.iterdir() if p.is_file()]}
fault = action.get('asset_fault')
if fault:
    selected = next(p for p in release['assets'] if p['name'] == action['asset'])
    if fault == 'duplicate': release['assets'].append(dict(selected))
    elif fault == 'url': selected['browser_download_url'] = 'https://example.invalid/wheel'
    elif fault == 'state': selected['state'] = 'new'
    elif fault == 'size': selected['size'] += 1
    elif fault == 'digest': selected['digest'] = 'sha256:'+'0'*64
    elif fault == 'boolean_size': selected['size'] = True
class Response(io.BytesIO):
    def __init__(self, value, url): super().__init__(value); self.url = url
    def geturl(self): return self.url
def fetch(request, **kwargs):
    url = request.full_url
    with open(os.environ['QA_DOWNLOADS'], 'a') as out: out.write(url+'\n')
    if url == 'https://api.github.com/repos/liuxie066/options-monitor/releases/tags/'+tag:
        return Response(json.dumps(release).encode(), url)
    if url.startswith(base) and '/' not in url[len(base):]:
        return Response((assets/url[len(base):]).read_bytes(), url)
    return forbidden('unexpected HTTPS '+url)()
distribution.runtime_bundle.urlopen = fetch
actual_run = subprocess.run
def offline_run(command, **kwargs):
    command = list(command)
    if command[1:3] == ['-I', '-c']:
        command[3] = os.environ['QA_GUARD']+'\n'+command[3]
    elif command[1:4] == ['-I', '-m', 'pip']:
        staging = Path(command[command.index('--target')+1])
        with open(os.environ['QA_PIP'], 'a') as out: out.write(json.dumps(command)+'\n')
        command = [command[0], '-I', '-c', os.environ['QA_GUARD']+
                   "\nimport runpy;sys.argv=['pip',*sys.argv[1:]];runpy.run_module('pip',run_name='__main__')",
                   *command[4:], '--no-index', '--find-links', os.environ['QA_WHEELHOUSE']]
    else:
        return forbidden('unexpected subprocess '+repr(command))()
    result = actual_run(command, **kwargs)
    if 'staging' in locals() and result.returncode == 0:
        if os.environ.get('QA_SDK_FAULT') == 'broken_api':
            (staging/'lark_oapi/core/enum.py').unlink()
        elif os.environ.get('QA_SDK_FAULT') == 'incompatible_dependency':
            metadata = next(staging.glob('lark_oapi-*.dist-info/METADATA'))
            metadata.write_text(metadata.read_text().replace('\n\n', '\nRequires-Dist: requests>=999\n\n', 1))
            from importlib.metadata import Distribution
            assert 'requests>=999' in (Distribution.at(metadata.parent).requires or [])
    if result.returncode:
        with open(os.environ['QA_PIP']+'.failures', 'a') as out:
            out.write(json.dumps({'argv': command, 'stdout': result.stdout, 'stderr': result.stderr})+'\n')
    return result
distribution.subprocess.run = offline_run
if 'cli' in action:
    raise SystemExit(cli.main(action['cli']))
if action.get('probe'):
    from src.infrastructure.feishu_ws_client import is_feishu_ws_sdk_available
    available = is_feishu_ws_sdk_available()
    result = {'available': available}
    if available:
        runtime.validate_plugin_import('feishu')
        import lark_oapi, importlib.metadata
        result.update(sdk=lark_oapi.__file__, version=importlib.metadata.version('lark-oapi'))
    print(json.dumps(result))
if action.get('imports'):
    for key in action['imports']: runtime.validate_plugin_import(key)
    print(json.dumps({'imported': action['imports']}))
'''


def inventory(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts}


@pytest.fixture(scope='module')
def qa_python(tmp_path_factory):
    environment = tmp_path_factory.mktemp('channel-qa-python')
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
    python = environment/'bin/python'
    site = next((environment/'lib').glob('python*/site-packages'))
    # Reuse core dependencies, while retaining genuine absence of all plugin owners/SDK.
    excluded = {v['module'] for v in plugin_runtime.PLUGINS.values()}
    excluded |= {v['distribution'].replace('-', '_') for v in plugin_runtime.PLUGINS.values()}
    # Prefer the supplied runtime closure to the controller's older dev dependencies.
    # lark itself is still excluded: missing SDK acquisition remains a real pip path.
    dependency_sites = [SDK, *(Path(entry) for entry in sys.path if entry.endswith('/site-packages'))]
    packages = (package for entry in dependency_sites for package in entry.iterdir())
    distributions = {p.name.removesuffix('.dist-info').rsplit('-', 1)[0].lower() for p in site.glob('*.dist-info')}
    for package in packages:
        if package.name.endswith('.dist-info'):
            name = package.name.removesuffix('.dist-info').rsplit('-', 1)[0].lower()
            if name in distributions:
                continue
            distributions.add(name)
        if (package.name.endswith('.pth') or package.name.startswith(('__editable__', 'lark_oapi'))
                or any(package.name == name or package.name.startswith(name+'-') for name in excluded)
                or (site/package.name).exists()):
            continue
        (site/package.name).symlink_to(package, target_is_directory=package.is_dir())
    result = subprocess.run([str(python), '-I', '-c',
        "import importlib.util,importlib.metadata;assert importlib.util.find_spec('lark_oapi') is None;"
        "assert not any(d.metadata['Name'] in " + repr([v['distribution'] for v in plugin_runtime.PLUGINS.values()]) +
        " for d in importlib.metadata.distributions())"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'include-system-site-packages = false' in (environment/'pyvenv.cfg').read_text()
    return python


@pytest.fixture
def qa(tmp_path, qa_python):
    assert ASSETS.is_dir() and WHEELHOUSE.is_dir() and SDK.is_dir(), 'QA input artifacts required'
    root = tmp_path/'release'
    root.mkdir()
    for directory in ('src', 'domain', 'om_strategy_core', 'constraints'):
        shutil.copytree(ROOT/directory, root/directory, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(ROOT/'VERSION', root/'VERSION')
    for name in ('om', 'om-agent'):
        shutil.copy2(ROOT/name, root/name)
    for item in plugin_runtime.PLUGINS.values():
        target = root/'plugins'/item['source']/'pyproject.toml'
        target.parent.mkdir(parents=True)
        shutil.copy2(ROOT/'plugins'/item['source']/'pyproject.toml', target)
    assets = tmp_path/'assets'
    shutil.copytree(ASSETS, assets)
    catalog = plugin_release.verify_publication(assets, tag='v'+(root/'VERSION').read_text().strip())
    protected = tmp_path/'runtime'
    for name in ('config.yaml', 'config.us.json', 'config.hk.json', 'services/sentinel',
                 'ledger/ledger.sqlite', 'reports/history.md'):
        path = protected/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('independent QA sentinel\n')
    before = inventory(protected)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('OM_', 'PIP_', 'PYTHON', 'QA_'))}
    environment.update(HOME=str(tmp_path/'home'), OM_RUNTIME_ROOT=str(protected), OM_SECRET_BACKEND='env',
                       PYTHONDONTWRITEBYTECODE='1', QA_GUARD=GUARD, QA_WHEELHOUSE=str(WHEELHOUSE),
                       QA_FORBIDDEN=str(tmp_path/'forbidden.jsonl'), QA_PIP=str(tmp_path/'pip.jsonl'),
                       QA_DOWNLOADS=str(tmp_path/'downloads.txt'))
    (tmp_path/'home').mkdir()
    def run(action, expected=0):
        result = subprocess.run([str(qa_python), '-I', '-B', '-c', GUARD+DRIVER,
                                 str(root), str(assets), json.dumps(action)],
                                capture_output=True, text=True, env=environment, cwd=tmp_path, timeout=120)
        assert result.returncode == expected, result.stdout+'\n'+result.stderr
        assert not Path(environment['QA_FORBIDDEN']).exists(), Path(environment['QA_FORBIDDEN']).read_text()
        assert inventory(protected) == before
        return json.loads(result.stdout)
    yield root, assets, catalog, environment, run
    assert inventory(protected) == before
    assert not inventory(tmp_path/'home'), 'CLI must not save a runtime binding or credentials'
    assert not Path(environment['QA_FORBIDDEN']).exists(), 'forbidden attempts cannot be swallowed'


def command(run, verb, key=None, confirm=False, expected=0, **fault):
    args = ['plugins', verb] + ([key] if key else []) + (['--confirm'] if confirm else []) + ['--format', 'json']
    return run({'cli': args, **fault}, expected)


@pytest.mark.parametrize('key', tuple(plugin_runtime.PLUGINS))
def test_real_cli_install_preview_confirm_idempotency_uninstall(qa, key):
    root, _, catalog, env, run = qa
    before = inventory(root)
    assert all(p['status'] == 'not_installed' for p in command(run, 'list')['plugins'])
    assert not Path(env['QA_DOWNLOADS']).exists()
    assert command(run, 'install', key)['status'] == 'preview'
    assert not plugin_runtime.plugin_store(root).exists()
    assert not Path(env['QA_PIP']).exists()
    assert command(run, 'install', key, True)['status'] == 'installed'
    receipt = plugin_runtime.read_managed_plugin(root, key)
    entry = next(p for p in catalog['plugins'] if p['id'] == key)
    assert receipt['wheel_sha256'] == entry['sha256']
    assert receipt['version'] == catalog['om_version']
    downloads = Path(env['QA_DOWNLOADS']).read_text()
    assert command(run, 'install', key, True)['status'] == 'already_installed'
    assert Path(env['QA_DOWNLOADS']).read_text() == downloads
    assert run({'imports': [key]})['imported'] == [key]
    if key == 'feishu':
        assert receipt['dependency_files']
        assert len(Path(env['QA_PIP']).read_text().splitlines()) == 1
        assert run({'probe': True})['available']
    assert command(run, 'uninstall', key)['status'] == 'preview'
    assert plugin_runtime.read_managed_plugin(root, key)
    assert command(run, 'uninstall', key, True)['status'] == 'uninstalled'
    assert command(run, 'uninstall', key, True)['status'] == 'not_installed'
    assert run({'probe': True})['available'] is False
    assert inventory(root) == before


@pytest.mark.parametrize('selection', [(), ('feishu',), ('wechat',), ('feishu', 'wechat')])
def test_channel_selection_is_independent_and_uninstall_keeps_other_channel(qa, selection):
    root, _, _, _, run = qa
    for key in selection:
        command(run, 'install', key, True)
    assert {p['id'] for p in command(run, 'list')['plugins'] if p['installed']} == set(selection)
    assert run({'probe': True})['available'] == ('feishu' in selection)
    if selection:
        assert run({'imports': list(selection)})['imported'] == list(selection)
        command(run, 'uninstall', selection[0], True)
        assert {p['id'] for p in command(run, 'list')['plugins'] if p['installed']} == set(selection[1:])
        assert all(plugin_runtime.read_managed_plugin(root, key) for key in selection[1:])


@pytest.mark.parametrize('fault', ['duplicate', 'url', 'state', 'size', 'digest', 'boolean_size'])
def test_release_asset_identity_rejected_before_sdk_acquisition(qa, fault):
    root, _, catalog, env, run = qa
    entry = next(p for p in catalog['plugins'] if p['id'] == 'feishu')
    assert command(run, 'install', 'feishu', True, expected=2,
                   asset_fault=fault, asset=entry['asset'])['error']['code'] == 'PLUGIN_ERROR'
    assert plugin_runtime.read_managed_plugin(root, 'feishu') is None
    assert not Path(env['QA_PIP']).exists()


@pytest.mark.parametrize('fault', ['extra_requirement', 'missing_sdk_requirement', 'wrong_version'])
def test_actual_wheel_metadata_rejected_even_with_matching_https_digests(qa, fault):
    root, assets, catalog, env, run = qa
    import zipfile
    entry = next(p for p in catalog['plugins'] if p['id'] == 'feishu')
    path = assets/entry['asset']
    with zipfile.ZipFile(path) as wheel:
        files = {name: wheel.read(name) for name in wheel.namelist()}
    name = next(name for name in files if name.endswith('/METADATA'))
    if fault == 'extra_requirement': files[name] += b'Requires-Dist: arbitrary-sdk\n'
    elif fault == 'missing_sdk_requirement': files[name] = files[name].replace(b'Requires-Dist: lark-oapi\n', b'')
    else: files[name] = files[name].replace(('Version: '+entry['version']).encode(), b'Version: 0.0.1')
    write_wheel(path, files)
    refresh_entry(path, entry)
    (assets/'om-plugins.json').write_text(json.dumps(catalog))
    assert command(run, 'install', 'feishu', True, expected=2)['error']['code'] == 'PLUGIN_ERROR'
    assert plugin_runtime.read_managed_plugin(root, 'feishu') is None
    assert not Path(env['QA_PIP']).exists()


@pytest.mark.parametrize('fault', ['working', 'working_reported_newer', 'broken_api', 'missing_dependency', 'incompatible_dependency'])
def test_existing_real_sdk_reuse_and_incompatible_environment_rejection(qa, fault):
    root, _, _, env, run = qa
    ambient = root.parent/'ambient-sdk'
    shutil.copytree(SDK, ambient, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    if fault == 'working_reported_newer':
        # Same real 1.6.5 APIs; only metadata is newer. This tests no downgrade,
        # and deliberately makes no claim about actual 1.6.8 API compatibility.
        metadata = next(ambient.glob('lark_oapi-*.dist-info/METADATA'))
        metadata.write_text(metadata.read_text().replace('Version: 1.6.5\n', 'Version: 1.6.8\n'))
        metadata.parent.rename(ambient/'lark_oapi-1.6.8.dist-info')
    elif fault == 'broken_api':
        (ambient/'lark_oapi/core/enum.py').write_text("raise ImportError('QA broken SDK API')\n")
    elif fault in {'missing_dependency', 'incompatible_dependency'}:
        metadata = next(ambient.glob('lark_oapi-*.dist-info/METADATA'))
        requirement = 'qa-nonexistent-sdk-dep>=1' if fault == 'missing_dependency' else 'requests>=999'
        metadata.write_text(metadata.read_text().replace('\n\n', '\nRequires-Dist: '+requirement+'\n\n', 1))
        assert requirement in (Distribution.at(metadata.parent).requires or [])
    before = inventory(ambient)
    env['QA_AMBIENT'] = str(ambient)
    working = fault.startswith('working')
    result = command(run, 'install', 'feishu', True, expected=0 if working else 2)
    assert not Path(env['QA_PIP']).exists()
    assert inventory(ambient) == before
    if working:
        assert result['status'] == 'installed'
        assert not plugin_runtime.read_managed_plugin(root, 'feishu').get('dependency_files')
        probe = run({'probe': True})
        assert probe['available'] and probe['version'] == ('1.6.8' if fault == 'working_reported_newer' else '1.6.5')
        assert str(ambient) in probe['sdk']
        assert inventory(ambient) == before
    else:
        assert result['error']['code'] == 'PLUGIN_ERROR'
        assert plugin_runtime.read_managed_plugin(root, 'feishu') is None


@pytest.mark.parametrize('fault', ['missing_wheels', 'broken_api', 'incompatible_dependency'])
def test_real_pip_and_staged_sdk_failures_never_activate_plugin(qa, fault):
    root, _, _, env, run = qa
    if fault == 'missing_wheels':
        empty = root.parent/'empty-wheelhouse'
        empty.mkdir()
        env['QA_WHEELHOUSE'] = str(empty)
    else:
        env['QA_SDK_FAULT'] = fault
    before = inventory(root)
    assert command(run, 'install', 'feishu', True, expected=2)['error']['code'] == 'PLUGIN_ERROR'
    assert len(Path(env['QA_PIP']).read_text().splitlines()) == 1
    assert plugin_runtime.read_managed_plugin(root, 'feishu') is None
    assert not (plugin_runtime.plugin_store(root)/'feishu').exists()
    assert inventory(root) == before


@pytest.mark.parametrize('has_sdk', [False, True])
def test_immutable_bundle_never_stages_or_mutates_sdk(qa, has_sdk):
    root, _, catalog, env, run = qa
    from tests.test_pm_package_upgrade import _bundled_runtime
    if has_sdk:
        sdk = root/'runtime/sdk'
        shutil.copytree(SDK, sdk, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        env['QA_AMBIENT'] = str(sdk)
    _bundled_runtime(root)
    manifest = json.loads((root/'om.runtime.json').read_text())
    manifest['commit'] = catalog['commit']
    (root/'om.runtime.json').write_text(json.dumps(manifest))
    before = inventory(root)
    runtime_bundle.load_manifest(root, verify_files=True)
    result = command(run, 'install', 'feishu', True, expected=0 if has_sdk else 2)
    assert not Path(env['QA_PIP']).exists()
    assert inventory(root) == before
    runtime_bundle.load_manifest(root, verify_files=True)
    if has_sdk:
        assert result['status'] == 'installed'
        assert run({'probe': True})['available']
        assert not plugin_runtime.read_managed_plugin(root, 'feishu').get('dependency_files')
    else:
        assert 'bundled Feishu SDK' in result['error']['message']
        assert plugin_runtime.read_managed_plugin(root, 'feishu') is None


def test_effect_guard_records_swallowed_network_and_sdk_attempts(tmp_path, qa_python):
    counter = tmp_path/'guard-selfcheck.jsonl'
    environment = dict(os.environ, QA_FORBIDDEN=str(counter), QA_AMBIENT=str(SDK))
    script = GUARD + r'''
import lark_oapi
attempts = [lambda: socket.create_connection(('example.invalid', 443)),
            lambda: lark_oapi.ws.Client('qa', 'qa'),
            lambda: lark_oapi.EventDispatcherHandler.builder('qa', 'qa')]
for attempt in attempts:
    try: attempt()
    except AssertionError: pass
'''
    result = subprocess.run([str(qa_python), '-I', '-B', '-c', script], env=environment,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert {json.loads(line)['attempt'] for line in counter.read_text().splitlines()} == {
        'socket.create_connection', 'SDK.Client.__init__', 'SDK.dispatcher.builder'}
