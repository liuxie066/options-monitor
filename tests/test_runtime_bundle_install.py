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


def installer_env(tmp_path, archive, *, tag, corrupt_checksum=False):
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
case "$url" in
 *.sha256) filename="${url##*/}"; printf '%s  %s\\n' "$TEST_DIGEST" "${filename%.sha256}" > "$out" ;;
 *.tar.gz) /bin/cp "$TEST_ARCHIVE" "$out" ;;
 *) exit 97 ;;
esac
''')
    curl.chmod(0o755)
    env = {k: v for k,v in os.environ.items() if not k.startswith(('OM_', 'PYTHON', 'VIRTUAL_ENV'))}
    snapshot = tmp_path / 'install.sh'
    snapshot.write_bytes((ROOT / 'scripts/install.sh').read_bytes())
    env.update(HOME=str(home), PATH=f'{tools}:/usr/bin:/bin', TEST_DIGEST=digest, TEST_ARCHIVE=str(archive), TEST_INSTALLER=str(snapshot))
    return env, log


def install(tmp_path, env, tag):
    return subprocess.run(['/bin/bash', env['TEST_INSTALLER'], '--version', tag,
                           '--prefix', str(tmp_path / "中文 安装 'quoted' $prefix"),
                           '--bin-dir', str(tmp_path / '中文 命令')],
                          env=env, capture_output=True, text=True, timeout=600)


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


def test_actual_runtime_bundle_installs_and_runs_without_git_python_pip_or_homebrew(tmp_path):
    value = os.environ.get('OM_TEST_RUNTIME_BUNDLE')
    if not value:
        pytest.skip('requires actual assembled native bundle; ordinary tests do not fabricate qualification')
    archive = Path(value).resolve()
    manifest = json.loads(archive.with_name(archive.name + '.manifest.json').read_text())
    tag = manifest['tag']
    env, log = installer_env(tmp_path, archive, tag=tag)
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
