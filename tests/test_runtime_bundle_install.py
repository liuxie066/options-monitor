"""Actual bundle installer qualification (opt in with OM_TEST_RUNTIME_BUNDLE).

The ordinary archive-rejection tests need no downloads or assembled interpreter.
"""
import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def installer_env(tmp_path, archive, *, tag, corrupt_checksum=False, missing_asset=""):
    home = tmp_path / 'home'
    home.mkdir()
    tools = tmp_path / 'tools'
    tools.mkdir()
    log = tmp_path / 'forbidden-tools.log'
    for name in ('git', 'python', 'python3', 'python3.12', 'pip', 'pip3', 'brew'):
        p = tools / name
        p.write_text(f'#!/bin/sh\necho {name} >> {shlex.quote(str(log))}\nexit 98\n')
        p.chmod(0o755)
    digest = '0'*64 if corrupt_checksum else hashlib.sha256(archive.read_bytes()).hexdigest()
    curl = tools / 'curl'
    curl.write_text('''#!/bin/bash
set -eu
url=''
out=''
while [ "$#" -gt 0 ]; do
 case "$1" in
   -o) out="$2"; shift 2 ;;
   https://*) url="$1"; shift ;;
   *) shift ;;
 esac
done
printf '%s\\n' "$url" >> "$TEST_REQUEST_LOG"
case "$url" in
 */releases/latest) printf '{"tag_name":"%s"}\\n' "$TEST_TAG" ;;
 *.sha256) [ "$TEST_MISSING_ASSET" != checksum ] || exit 22
   filename="${url##*/}"; printf '%s  %s\\n' "$TEST_DIGEST" "${filename%.sha256}" > "$out" ;;
 *.tar.gz) [ "$TEST_MISSING_ASSET" != archive ] || exit 22
   /bin/cp "$TEST_ARCHIVE" "$out" ;;
 *) exit 97 ;;
esac
''')
    curl.chmod(0o755)
    env = {k: v for k,v in os.environ.items() if not k.startswith(('OM_', 'PYTHON', 'VIRTUAL_ENV'))}
    snapshot = tmp_path / 'install.sh'
    snapshot.write_bytes((ROOT / 'scripts/install.sh').read_bytes())
    env.update(HOME=str(home), PATH=f'{tools}:/usr/bin:/bin', TEST_DIGEST=digest, TEST_ARCHIVE=str(archive),
               TEST_INSTALLER=str(snapshot), TEST_TAG=tag, TEST_MISSING_ASSET=missing_asset,
               TEST_REQUEST_LOG=str(tmp_path / 'requests.log'))
    return env, log


def install(tmp_path, env, tag):
    return subprocess.run(['/bin/bash', env['TEST_INSTALLER'], '--version', tag,
                           '--prefix', str(tmp_path / "中文 安装 'quoted' $prefix"),
                           '--bin-dir', str(tmp_path / '中文 命令')],
                          env=env, capture_output=True, text=True, timeout=600)


@pytest.mark.parametrize('latest', [False, True])
@pytest.mark.parametrize('missing_asset', ['checksum', 'archive'])
def test_published_release_without_runtime_asset_refuses_install_and_preserves_current(tmp_path, latest, missing_asset):
    archive = tmp_path / 'unused.tar.gz'
    archive.write_bytes(b'not reached')
    tag = 'v1.2.3'
    env, forbidden = installer_env(tmp_path, archive, tag=tag, missing_asset=missing_asset)
    prefix, bin_dir = tmp_path / 'installation', tmp_path / 'commands'
    old = prefix / 'releases/v1.2.2'
    old.mkdir(parents=True)
    (prefix / 'current').symlink_to(old)
    bin_dir.mkdir()
    for name in ('om', 'om-agent'):
        (bin_dir / name).write_text('# options-monitor managed wrapper\nexisting wrapper\n')
    command = ['/bin/bash', env['TEST_INSTALLER'], '--prefix', str(prefix), '--bin-dir', str(bin_dir)]
    if not latest:
        command += ['--version', tag]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    expected = 'no source fallback' if missing_asset == 'checksum' else 'runtime bundle download failed'
    assert expected in result.stderr
    requests = Path(env['TEST_REQUEST_LOG']).read_text().splitlines()
    downloads = [url for url in requests if '/releases/download/' in url]
    assert downloads and all(f'/releases/download/{tag}/options-monitor-{tag}-' in url for url in downloads)
    assert len(downloads) == (1 if missing_asset == 'checksum' else 2)
    assert not forbidden.exists()
    assert (prefix / 'current').resolve() == old.resolve()
    assert not (prefix / f'releases/{tag}').exists()
    assert not list((prefix / 'releases').glob(f'.{tag}.tmp.*'))
    assert all((bin_dir / name).read_text() == '# options-monitor managed wrapper\nexisting wrapper\n' for name in ('om', 'om-agent'))


@pytest.mark.parametrize('bad_kind', ['traversal', 'symlink', 'hardlink', 'fifo', 'duplicate'])
def test_shell_bootstrap_rejects_unsafe_archive_without_interpreter(tmp_path, bad_kind):
    archive = tmp_path / 'unsafe.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        item = tarfile.TarInfo('../escaped' if bad_kind == 'traversal' else 'file')
        if bad_kind == 'symlink': item.type, item.linkname = tarfile.SYMTYPE, '../escaped'
        elif bad_kind == 'hardlink': item.type, item.linkname = tarfile.LNKTYPE, '../escaped'
        elif bad_kind == 'fifo': item.type = tarfile.FIFOTYPE
        else: item.size = 1
        tar.addfile(item, io.BytesIO(b'x') if item.isfile() else None)
        if bad_kind == 'duplicate': tar.addfile(item, io.BytesIO(b'x'))
    env, log = installer_env(tmp_path, archive, tag='v1.2.3')
    result = install(tmp_path, env, 'v1.2.3')
    assert result.returncode != 0 and ('unsafe runtime archive' in result.stderr or 'links/special files' in result.stderr)
    assert "awk:" not in result.stderr
    assert not log.exists() and not (tmp_path / 'escaped').exists()
    assert not (tmp_path / "中文 安装 'quoted' $prefix/current").exists()


def test_shell_bootstrap_checksum_failure_has_no_runtime_or_wrapper_effect(tmp_path):
    archive = tmp_path / 'wrong.tar.gz'
    archive.write_bytes(b'not an archive')
    env, log = installer_env(tmp_path, archive, tag='v1.2.3', corrupt_checksum=True)
    result = install(tmp_path, env, 'v1.2.3')
    assert result.returncode != 0 and 'SHA256 mismatch' in result.stderr
    assert not log.exists() and not (tmp_path / '中文 命令/om').exists()



def offline_plugin_assets(tmp_path, archive, manifest, monkeypatch):
    """Build real wheels offline with the artifact's own pinned build dependencies."""
    from types import SimpleNamespace
    from src.application import plugin_release
    build_host = tmp_path / 'wheel-build-host'
    build_host.mkdir()
    with tarfile.open(archive, 'r:gz') as tar:
        members = [m for m in tar.getmembers() if m.name.startswith('runtime/python/')]
        tar.extractall(build_host, members=members, filter='data')
    python = build_host / 'runtime/python/bin/python3'
    assert python.is_file()
    def build_wheel(command, **kwargs):
        assert command[1:5] == ['-m', 'pip', 'wheel', '--no-deps']
        return subprocess.run([str(python), *command[1:], '--no-build-isolation', '--no-index'], **kwargs)
    assets = tmp_path / 'official-plugin-assets'
    with monkeypatch.context() as patch:
        patch.setattr(plugin_release, 'subprocess', SimpleNamespace(run=build_wheel, check_output=subprocess.check_output))
        catalog = plugin_release.build(source=ROOT, output=assets)
    assert catalog['om_version'] == manifest['version'] and catalog['commit'] == manifest['commit']
    base = f"https://github.com/{catalog['repository']}/releases/download/{manifest['tag']}/"
    published = [p for p in assets.iterdir() if p.name != plugin_release.NOTES]
    release = dict(tag_name=manifest['tag'], draft=False, prerelease=False, assets=[
        dict(name=p.name, state='uploaded', size=p.stat().st_size,
             digest='sha256:'+hashlib.sha256(p.read_bytes()).hexdigest(), browser_download_url=base+p.name)
        for p in published
    ])
    release_path = assets / 'release.json'
    release_path.write_text(json.dumps(release))
    routes = {base+p.name: str(p) for p in published}
    routes[f"https://api.github.com/repos/{catalog['repository']}/releases/tags/{manifest['tag']}"] = str(release_path)
    sandbox = tmp_path / 'offline-transport'
    sandbox.mkdir()
    (sandbox / 'routes.json').write_text(json.dumps(routes))
    (sandbox / 'sitecustomize.py').write_text("""
import io,json,socket,urllib.request
from pathlib import Path
base=Path(__file__).parent
routes=json.loads((base/'routes.json').read_text())
class Response(io.BytesIO):
    def __init__(self,url,data):
        super().__init__(data)
        self.url=url
    def geturl(self): return self.url
def offline(request,**kwargs):
    url=request if isinstance(request,str) else request.full_url
    assert url in routes, 'unexpected network request: '+url
    with (base/'requests.jsonl').open('a') as log: log.write(json.dumps(url)+'\\n')
    return Response(url,Path(routes[url]).read_bytes())
def forbidden(*args,**kwargs): raise AssertionError('real network forbidden during bundle qualification')
urllib.request.urlopen=offline
socket.socket.connect=socket.socket.connect_ex=forbidden
""")
    return sandbox


def test_actual_runtime_bundle_installs_and_runs_without_git_python_pip_or_homebrew(tmp_path, monkeypatch):
    value = os.environ.get('OM_TEST_RUNTIME_BUNDLE')
    if not value:
        pytest.skip('requires actual assembled native bundle; ordinary tests do not fabricate qualification')
    archive = Path(value).resolve()
    manifest = json.loads(archive.with_name(archive.name + '.manifest.json').read_text())
    tag = manifest['tag']
    sandbox = offline_plugin_assets(tmp_path, archive, manifest, monkeypatch)
    env, log = installer_env(tmp_path, archive, tag=tag)
    env['PYTHONPATH'] = str(sandbox)
    state = Path(env['HOME']) / 'existing instance'
    state.mkdir()
    (state / 'config.yaml').write_text('# untouched configuration\n')
    (state / 'ledger.sqlite').write_bytes(b'untouched ledger fixture')
    record = Path(env['HOME']) / '.config/options-monitor/runtime-root'
    record.parent.mkdir(parents=True)
    record.write_text(str(state) + '\n')
    before_state = {p.name: p.read_bytes() for p in state.iterdir()}
    before_record = record.read_bytes()
    result = install(tmp_path, env, tag)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count('运行 OM（首次配置或继续使用）') == 1
    assert 'setup init' not in result.stdout and 'settings doctor' not in result.stdout
    prefix = tmp_path / "中文 安装 'quoted' $prefix"
    assert (prefix / 'current').resolve() == prefix / 'releases' / tag
    assert not (prefix / 'current/.venv').exists()
    assert not (prefix / 'current/config.yaml').exists()
    from src.application.plugin_runtime import read_managed_plugin
    receipt = read_managed_plugin(prefix / 'current', 'market-opend')
    assert receipt and receipt['repo_root'] == str((prefix / 'current').resolve())
    assert receipt['version'] == manifest['version']
    assert read_managed_plugin(prefix / 'current', 'broker-futu') is None
    assert read_managed_plugin(prefix / 'current', 'broker-longbridge') is None
    requests = [json.loads(row) for row in (sandbox / 'requests.jsonl').read_text().splitlines()]
    assert len(requests) == 3 and requests[-1].endswith('.whl')
    assert '/om_market_opend_plugin-' in requests[-1]
    for entry, command in [('om', 'help'), ('om-agent', 'spec')]:
        proc = subprocess.run([str(tmp_path / '中文 命令' / entry), command], env=env,
                              capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        if command == 'spec': json.loads(proc.stdout)
    child = subprocess.run([str(prefix / 'current/runtime/python/bin/python3'), '-c',
                            'import sys,subprocess; subprocess.run([sys.executable,"-c","import ssl,sqlite3,pandas,numpy; print(42)"],check=True)'],
                           cwd=prefix / 'current', env=env, capture_output=True, text=True, timeout=180)
    assert child.returncode == 0 and child.stdout.strip() == '42', child.stderr
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before_state
    assert record.read_bytes() == before_record
    assert not log.exists()
    before = os.readlink(prefix / 'current')
    repeated = install(tmp_path, env, tag)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert os.readlink(prefix / 'current') == before and not log.exists()
    assert len((sandbox / 'requests.jsonl').read_text().splitlines()) == 3
    force = subprocess.run(['/bin/bash', env['TEST_INSTALLER'], '--version', tag, '--prefix', str(prefix),
                            '--bin-dir', str(tmp_path / '中文 命令'), '--force'], env=env, capture_output=True, text=True)
    assert force.returncode != 0 and 'active current release' in force.stderr
    assert os.readlink(prefix / 'current') == before and not log.exists()


def test_shell_bootstrap_accepts_regular_resource_names_with_spaces(tmp_path):
    archive = tmp_path / 'ordinary.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        item = tarfile.TarInfo('runtime/python/lib/site-packages/script (dev).tmpl')
        item.size = 1
        tar.addfile(item, io.BytesIO(b'x'))
        from src.application.runtime_bundle import current_platform
        target = current_platform()
        minimum = 'macOS 14.0' if target.startswith('macos') else 'glibc 2.28; optional services require systemd and systemd-creds'
        payload = json.dumps({'platform': target, 'minimum_system': minimum}, indent=2).encode()
        header = tarfile.TarInfo('om.runtime.json')
        header.size = len(payload)
        tar.addfile(header, io.BytesIO(payload))
    env, log = installer_env(tmp_path, archive, tag='v1.2.3')
    result = install(tmp_path, env, 'v1.2.3')
    assert result.returncode != 0 and 'declared bundled Python is missing' in result.stderr
    assert 'unsafe runtime archive' not in result.stderr and not log.exists()


@pytest.mark.parametrize('system,observed', [('Darwin', '13.6'), ('Linux', 'glibc 2.27')])
def test_minimum_system_rejected_before_download_or_bundled_execution(tmp_path, system, observed):
    archive = tmp_path / 'unused.tar.gz'
    archive.write_bytes(b'unused')
    env, log = installer_env(tmp_path, archive, tag='v1.2.3')
    tools = tmp_path / 'tools'
    for name, output in [('uname', system), ('sw_vers', observed), ('getconf', observed)]:
        path = tools / name
        if name == 'uname':
            path.write_text(f'#!/bin/sh\nif [ "$1" = -m ]; then echo x86_64; else echo {system}; fi\n')
        else:
            path.write_text(f'#!/bin/sh\necho {shlex.quote(output)}\n')
        path.chmod(0o755)
    (tools / 'curl').write_text('#!/bin/sh\necho download-attempted >&2\nexit 91\n')
    result = install(tmp_path, env, 'v1.2.3')
    assert result.returncode != 0 and 'requires' in result.stderr and observed.split()[-1] in result.stderr
    assert 'download-attempted' not in result.stderr and not log.exists()
