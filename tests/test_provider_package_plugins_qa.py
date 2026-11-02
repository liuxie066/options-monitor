"""Independent provider acceptance; pre-freeze runs are preparation evidence only.

No provider is imported from plugins/*/src. Wheels are built from temporary
source copies, installed with pip --target --no-deps, and consumed in fresh
isolated interpreters. Missing artifacts/tooling are failures, never skips.
OM_PROVIDER_QA_BUILD_SITES may name local cached build-backend installations.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import subprocess
import sys
import sysconfig
import tarfile
import textwrap
import urllib.request
import venv
from types import SimpleNamespace
import zipfile

import pytest

from tests.test_managed_plugin_upgrade import managed_python, managed_transition  # noqa: F401
from tests.test_pm_package_upgrade import transition  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = {
    "market-opend": ("om-market-opend-plugin", "om_market_opend"),
    "broker-futu": ("om-broker-futu-plugin", "om_broker_futu"),
    "broker-longbridge": ("om-broker-longbridge-plugin", "om_broker_longbridge"),
}
BASE = "86f05821ce8e9cc99aed566d7ae919a9e04917c6"


def dependency_site(work):
    """Expand already-processed .pth roots without copying their startup hooks."""
    runtime = work / "runtime-site"
    runtime.mkdir()
    roots = dict.fromkeys([sysconfig.get_paths()["purelib"], *(
        entry for entry in sys.path if Path(entry).name in {"site-packages", "dist-packages"}
    )])
    for source in map(Path, roots):
        assert source.is_dir(), f"runtime dependency root unavailable: {source}"
        for item in source.iterdir():
            name = item.name.lower().replace("-", "_")
            if (name.startswith(("om_", "futu", "longbridge"))
                    or item.suffix == ".pth" or name in {"__pycache__", "_virtualenv.py"}
                    or (runtime / item.name).exists()):
                continue
            (runtime / item.name).symlink_to(item, target_is_directory=item.is_dir())
    return runtime

# This prelude runs before importing OM or pip. The audit log survives caught
# exceptions, so a swallowed forbidden operation still fails the parent test.
GUARD = r'''
import importlib.abc, json, os, socket, sys, urllib.request
from pathlib import Path
scratch = Path(sys.argv[1]).resolve()
evidence_path = scratch / "attempts.jsonl"
def record(kind, detail):
    with evidence_path.open("a") as stream:
        stream.write(json.dumps({"kind": kind, "detail": str(detail)}) + "\n")
def forbidden(name):
    def reject(*args, **kwargs):
        record("forbidden", name)
        raise AssertionError("forbidden QA operation: " + name)
    return reject
socket.socket.connect = forbidden("socket.connect")
socket.socket.connect_ex = forbidden("socket.connect_ex")
socket.socket.sendto = forbidden("socket.sendto")
socket.create_connection = forbidden("socket.create_connection")
socket.getaddrinfo = forbidden("socket.getaddrinfo")
# pip/urllib3 otherwise probes IPv6 by binding ::1 even for a local wheel.
socket.has_ipv6 = False
urllib.request.urlopen = forbidden("urlopen")
def audit(event, args):
    if event.startswith("socket.") and event not in {"socket.__new__"}:
        forbidden(event)()
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "pty.spawn"}:
        forbidden(event)()
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path == evidence_path:
            return
        mode = args[1] or ""
        flags = args[2] or 0
        writing = any(c in mode for c in "wax+") or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        if writing and not path.is_relative_to(scratch):
            forbidden("write outside scratch: " + str(path))()
        if not path.is_relative_to(scratch) and (
            path.name in {"config.yaml", "config.us.json", "config.hk.json", ".env", "options-monitor.env"}
            or path.suffix in {".sqlite", ".sqlite3", ".db", ".key", ".pem"}
            or any(part in {"output", "output_accounts", "output_shared", "output_runs", ".om-plugins", ".ssh", ".aws"} for part in path.parts)
        ):
            forbidden("production/secret read: " + str(path))()
    if event in {"os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.symlink", "os.link", "os.chmod"}:
        paths = args[:2] if event in {"os.rename", "os.symlink", "os.link"} else args[:1]
        for raw in paths:
            if isinstance(raw, (str, bytes, os.PathLike)) and not Path(os.fsdecode(raw)).resolve().is_relative_to(scratch):
                forbidden("filesystem mutation outside scratch: " + str(raw))()
sys.addaudithook(audit)
# Do not inherit editable installs, .pth processing, original venv plugin
# inventories, user site, or a source checkout as an accidental import root.
sys.path[:] = [p for p in sys.path if p and "site-packages" not in p and "dist-packages" not in p]
sys.path[:0] = json.loads(sys.argv[2])
class NoRealSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"futu", "longbridge"}:
            record("sdk_unavailable", fullname)
            raise ModuleNotFoundError("QA excludes real SDK: " + fullname, name=fullname)
sys.meta_path.insert(0, NoRealSDK())
# Plugin state belongs to a temporary code-instance identity, never a store
# adjacent to the shared worktree. Core modules still come from frozen sources.
import shutil
from src.application import plugin_runtime as qa_runtime
qa_instance = scratch / "instance"
qa_instance.mkdir()
qa_source = Path(qa_runtime.__file__).resolve().parents[2]
for relative in [Path("VERSION"), Path("requirements/longbridge.txt"), *(
    Path("plugins") / item["source"] / "pyproject.toml" for item in qa_runtime.PLUGINS.values()
)]:
    source = qa_source / relative
    if source.is_file():
        destination = qa_instance / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
qa_runtime._CODE_ROOT = qa_instance
'''


@pytest.fixture(autouse=True)
def no_host_effects(monkeypatch, tmp_path):
    """Also guard reused fixture subprocesses; never run their unguarded probes."""
    from tests import test_managed_plugin_upgrade as support

    attempts = []

    def deny(name):
        def reject(*args, **kwargs):
            attempts.append(name)
            (tmp_path / "host-forbidden.json").write_text(json.dumps(attempts))
            raise AssertionError("forbidden QA host effect: " + name)
        return reject

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny("socket." + name))
    monkeypatch.setattr(socket, "create_connection", deny("socket.create_connection"))
    monkeypatch.setattr(urllib.request, "urlopen", deny("urlopen"))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    for key in tuple(os.environ):
        if key.startswith(("FUTU_", "LONGBRIDGE_", "FEISHU_", "OM_LB_")) or key == "OM_ENV_FILE":
            monkeypatch.delenv(key, raising=False)

    original_run = subprocess.run

    def checked_import(command, **kwargs):
        assert command[1:3] == ["-I", "-c"], command
        assert Path(command[0]).is_relative_to(tmp_path), command
        # The reused fixture has its own fresh dependency-only interpreter.
        # Keep its argv and import roots intact; inject the same effect guard.
        prelude = GUARD.split("# Do not inherit", 1)[0].replace(
            "scratch = Path(sys.argv[1]).resolve()", f"scratch = Path({str(tmp_path)!r}).resolve()"
        )
        env = {"PATH": os.defpath, "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
               "OM_SECRET_BACKEND": "env", "OM_RUNTIME_ROOT": str(tmp_path / "runtime"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        kwargs = {**kwargs, "env": env}
        result = original_run([command[0], "-B", *command[1:3], prelude + command[3], *command[4:]], **kwargs)
        log = tmp_path / "attempts.jsonl"
        entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        assert not [entry for entry in entries if entry["kind"] == "forbidden"], entries
        return result

    monkeypatch.setattr(support, "_offline_import", checked_import)
    yield
    assert attempts == [], attempts


def inventory(path):
    return {item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in path.rglob("*") if item.is_file() and "__pycache__" not in item.parts}


@pytest.fixture(scope="module")
def four_package_archive():
    result = subprocess.run(
        ["git", "archive", BASE, "src", "domain", "plugins", "requirements", "constraints", "pyproject.toml"],
        cwd=ROOT, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        return {member.name: archive.extractfile(member).read()
                for member in archive.getmembers() if member.isfile()}


def restore_four_package_release(ctx, files):
    """Synthetic VERSION/runtime wrappers; every historical code byte is original."""
    from tests.test_managed_plugin_upgrade import _python_proxy
    from src.application import runtime_bundle
    from src.application.plugin_runtime import plugin_store
    import ast

    shutil.rmtree(plugin_store(ctx.previous) / "market-opend")
    for name in ("src", "domain", "plugins", "requirements", "constraints"):
        if (ctx.previous / name).exists():
            shutil.rmtree(ctx.previous / name)
    for name, data in files.items():
        path = ctx.previous / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    path = ctx.previous / "src/application/plugin_runtime.py"
    tree = ast.parse(path.read_text())
    catalog = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "PLUGINS" for target in node.targets))
    assert set(catalog) == {"pm", "position-advice", "feishu", "wechat"}
    assert {p.name for p in (ctx.previous / "plugins").iterdir()} == {
        "portfolio-management", "position-advice", "feishu-channel", "wechat-channel"}
    if ctx.bundled:
        shutil.rmtree(ctx.previous / "plugins")
        manifest = json.loads((ctx.previous / runtime_bundle.MANIFEST).read_text())
        manifest["files"] = {name: runtime_bundle.sha256(ctx.previous / name)
                             for name in inventory(ctx.previous) if name != runtime_bundle.MANIFEST}
        (ctx.previous / runtime_bundle.MANIFEST).write_text(json.dumps(manifest))
    else:
        _python_proxy(ctx.previous / ".venv/bin/python", Path(ctx.target_interpreter["python"]))
    for name, data in files.items():
        if not (ctx.bundled and name.startswith("plugins/")):
            assert (ctx.previous / name).read_bytes() == data, f"historical fixture changed: {name}"


def cli_json(capsys, argv):
    from src.interfaces.cli.main import main

    status = main(argv)
    output = capsys.readouterr()
    return status, json.loads(output.out)


def bind_managed_cli(monkeypatch, root):
    from src.application import plugin_distribution as distribution, plugin_runtime
    from src.application.service_upgrade import _release_python
    from src.interfaces.cli import main as cli

    monkeypatch.setattr(distribution, "code_root", lambda: root)
    monkeypatch.setattr(plugin_runtime, "code_root", lambda: root)
    monkeypatch.setattr(cli, "repo_base", lambda: root)
    original_run = distribution.subprocess.run

    def bound_run(command, **kwargs):
        if command[0] == sys.executable:
            command = [str(_release_python(root)), *command[1:]]
        return original_run(command, **kwargs)

    monkeypatch.setattr(distribution, "subprocess", SimpleNamespace(run=bound_run, TimeoutExpired=subprocess.TimeoutExpired))


@dataclass
class OfflineProcess:
    work: Path
    runtime_site: Path
    calls: int = 0

    def run(self, code: str, *, sites=(), arguments=(), success=True):
        self.calls += 1
        case = self.work / f"process-{self.calls}"
        case.mkdir()
        paths = [str(p) for p in (*sites, self.runtime_site, ROOT)]
        env = {
            "PATH": os.defpath,
            "HOME": str(case),
            "TMPDIR": str(case),
            "PYTHONDONTWRITEBYTECODE": "1",
            "OM_SECRET_BACKEND": "env",
            "OM_RUNTIME_ROOT": str(case / "runtime"),
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "LC_ALL": "en_US.UTF-8",
        }
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", GUARD + "\n" + textwrap.dedent(code),
             str(case), json.dumps(paths), *map(str, arguments)],
            cwd=case, env=env, text=True, capture_output=True, timeout=90,
        )
        (case / "stdout.txt").write_text(result.stdout)
        (case / "stderr.txt").write_text(result.stderr)
        log = case / "attempts.jsonl"
        attempts = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        evidence = {"returncode": result.returncode, "attempts": attempts, "sites": paths}
        (case / "evidence.json").write_text(json.dumps(evidence, indent=2))
        if success:
            assert not [a for a in attempts if a["kind"] == "forbidden"], (case, attempts)
            assert result.returncode == 0, f"{case}\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
        return result, evidence


@pytest.fixture
def sandbox(tmp_path):
    return OfflineProcess(tmp_path, dependency_site(tmp_path))


@pytest.fixture(scope="module")
def provider_wheels(tmp_path_factory):
    """Use a local backend explicitly; never trigger pip's online build isolation."""
    work = tmp_path_factory.mktemp("provider-qa-wheels")
    # A separate sandbox per build avoids touching source egg-info/build dirs.
    process = OfflineProcess(work, dependency_site(work))
    build_sites = tuple(Path(p).resolve() for p in os.environ.get("OM_PROVIDER_QA_BUILD_SITES", "").split(os.pathsep) if p)
    for site in build_sites:
        assert site.is_dir(), f"local build backend missing: {site}"
    wheels = {}
    for plugin_id, (distribution, module) in PROVIDERS.items():
        source = ROOT / "plugins" / plugin_id
        assert (source / "pyproject.toml").is_file(), f"required package missing: {source}"
        output = work / "wheels" / plugin_id
        output.mkdir(parents=True)
        process.run('''
            import importlib.metadata, shutil
            shutil.copytree(sys.argv[3], scratch / "source",
                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build", "dist"))
            os.chdir(scratch / "source")
            import setuptools.build_meta as backend
            assert int(importlib.metadata.version("setuptools").split(".")[0]) >= 77
            result = backend.build_wheel(str(scratch / "wheels"))
            print("QA_WHEEL=" + str(scratch / "wheels" / result))
        ''', sites=build_sites, arguments=(source,))
        built = list((work / f"process-{process.calls}" / "wheels").glob("*.whl"))
        assert len(built) == 1, f"required wheel missing for {plugin_id}: {built}"
        wheel = output / built[0].name
        shutil.copyfile(built[0], wheel)
        with zipfile.ZipFile(wheel) as archive:
            meta = BytesParser().parsebytes(archive.read(next(n for n in archive.namelist() if n.endswith("/METADATA"))))
            assert meta["Name"] == distribution
            assert meta["Requires-Python"] == ">=3.12"
            assert not meta.get_all("Requires-Dist"), f"unexpected dependencies: {plugin_id}"
            assert f"{module}/__init__.py" in archive.namelist()
        wheels[plugin_id] = wheel
    (work / "wheel-evidence.json").write_text(json.dumps({
        key: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for key, path in wheels.items()
    }, indent=2))
    return wheels


def install(sandbox, wheels, *selected):
    # pip's target must belong to this process's scratch, too.
    target = sandbox.work / f"process-{sandbox.calls + 1}" / "installed"
    sandbox.run('''
        from pip._internal.cli.main import main
        assert main(["install", "--no-index", "--no-deps", "--no-compile",
                     "--disable-pip-version-check", "--target", sys.argv[3], *sys.argv[4:]]) == 0
    ''', arguments=(target, *(wheels[key] for key in selected)))
    for key in selected:
        distribution, module = PROVIDERS[key]
        matches = list(importlib.metadata.Distribution.discover(name=distribution, path=[str(target)]))
        assert len(matches) == 1, f"real installed distribution missing/ambiguous: {key}"
        dist = matches[0]
        assert f"{module}/__init__.py" in {str(p) for p in dist.files or ()}
        assert (target / module / "__init__.py").read_bytes() == (ROOT / "plugins" / key / "src" / module / "__init__.py").read_bytes(), "installed bytes stale; rebuild frozen sources"
    return target


def test_qa_harness_records_caught_network_and_write_attempts(sandbox):
    _, evidence = sandbox.run('''
        for fn in (lambda: socket.create_connection(("offline.invalid", 1)),
                   lambda: urllib.request.urlopen("https://offline.invalid"),
                   lambda: Path(sys.argv[3]).write_text("must never be written")):
            try: fn()
            except AssertionError: pass
    ''', arguments=(sandbox.work.parent / "qa-must-not-write",), success=False)
    attempts = [a["detail"] for a in evidence["attempts"] if a["kind"] == "forbidden"]
    assert len(attempts) == 3, evidence
    assert attempts[:2] == ["socket.create_connection", "urlopen"]
    assert attempts[2].startswith("write outside scratch:")
    assert not (sandbox.work.parent / "qa-must-not-write").exists()


def test_qa_harness_removes_environment_plugins_and_real_sdk(sandbox):
    sandbox.run('''
        import importlib.metadata as metadata, importlib.util
        import pandas, pytest
        assert callable(pandas.DataFrame) and callable(pytest.fail)
        for name, module in [("om-market-opend-plugin", "om_market_opend"),
                             ("om-broker-futu-plugin", "om_broker_futu"),
                             ("om-broker-longbridge-plugin", "om_broker_longbridge")]:
            try: metadata.distribution(name)
            except metadata.PackageNotFoundError: pass
            else: raise AssertionError("inherited provider distribution: " + name)
            assert importlib.util.find_spec(module) is None
        for sdk in ("futu", "longbridge"):
            try: __import__(sdk)
            except ModuleNotFoundError: pass
            else: raise AssertionError("real SDK reachable: " + sdk)
    ''')


def test_qa_harness_builds_and_pip_installs_an_offline_synthetic_wheel(sandbox):
    sandbox.run('''
        source = scratch / "source"
        source.mkdir()
        (source / "qa_provider_harness.py").write_text("VALUE = 17\\n")
        (source / "pyproject.toml").write_text(
            '[build-system]\\nrequires = ["setuptools>=77"]\\n'
            'build-backend = "setuptools.build_meta"\\n'
            '[project]\\nname = "qa-provider-harness"\\nversion = "0.0.1"\\n'
            '[tool.setuptools]\\npy-modules = ["qa_provider_harness"]\\n')
        os.chdir(source)
        import setuptools.build_meta as backend
        wheel = scratch / "wheels" / backend.build_wheel(str(scratch / "wheels"))
        from pip._internal.cli.main import main
        target = scratch / "installed"
        assert main(["install", "--no-index", "--no-deps", "--no-compile",
                     "--disable-pip-version-check", "--target", str(target), str(wheel)]) == 0
        sys.path.insert(0, str(target))
        import importlib.metadata as metadata, qa_provider_harness as installed
        assert installed.VALUE == 17
        assert metadata.version("qa-provider-harness") == "0.0.1"
        assert Path(installed.__file__).resolve().is_relative_to(target)
    ''')


@pytest.mark.parametrize("plugin_id", tuple(PROVIDERS))
def test_real_wheel_installs_with_metadata_and_no_source_fallback(sandbox, provider_wheels, plugin_id):
    target = install(sandbox, provider_wheels, plugin_id)
    distribution, module = PROVIDERS[plugin_id]
    sandbox.run('''
        import importlib, importlib.metadata as metadata
        site, distribution, module, plugin_id = sys.argv[3:]
        dist = metadata.distribution(distribution)
        assert not dist.requires
        assert Path(dist.locate_file("")).resolve() == Path(site).resolve()
        loaded = importlib.import_module(module)
        assert Path(loaded.__file__).resolve().is_relative_to(Path(site).resolve())
        from src.application.plugin_runtime import plugin_installed, require_plugin
        assert plugin_installed(plugin_id)
        assert require_plugin(plugin_id) is loaded
    ''', sites=(target,), arguments=(target, distribution, module, plugin_id))


def test_no_providers_help_spec_and_local_history_are_readable(sandbox):
    sandbox.run('''
        from src.interfaces.cli.main import main
        assert main(["help"]) == 0
        assert main(["tools", "spec"]) == 0
        # Seed a real sealed historical report; absence of history is a separate
        # DEPENDENCY_MISSING outcome, not an empty successful report.
        sys.path.insert(0, str(Path(__import__("src").__file__).resolve().parents[1] / "tests"))
        from test_position_advice_history import report, position
        path = report(scratch / "history", position())
        before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
        from src.application.tool_execution import execute_tool
        result = execute_tool("close_advice_read", {"report_path": str(path)})
        assert result["ok"], result
        assert result["data"]["rows"][0]["symbol"] == "NVDA"
        assert result["data"]["freshness"]["status"] == "historical"
        assert {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()} == before
        from src.application.market_data_provider import open_quote_gateway
        from src.application.plugin_runtime import PluginUnavailableError
        try: open_quote_gateway(host="offline.invalid", port=1)
        except PluginUnavailableError as exc: assert "om plugins install market-opend" in str(exc)
        else: raise AssertionError("missing market admitted a live call")
    ''')


def test_market_installed_without_brokers_performs_quote_only_analysis(sandbox, provider_wheels):
    target = install(sandbox, provider_wheels, "market-opend")
    sandbox.run('''
        from types import SimpleNamespace
        from src.application.market_data_provider import open_quote_gateway, get_market_state
        from src.application.plugin_runtime import plugin_installed, PluginUnavailableError
        from src.infrastructure.futu_gateway import FutuGateway
        assert not plugin_installed("broker-futu") and not plugin_installed("broker-longbridge")
        events = []
        class Backend:
            def __init__(self, **kwargs):
                self._quote_client = SimpleNamespace(get_global_state=lambda: (0, {
                    "program_status_type": "READY", "qot_logined": True}))
            def _ensure_quote_client(self): return self._quote_client
            def _ensure_trade_client(self): forbidden("broker probe during quote analysis")()
        gateway = open_quote_gateway(backend_cls=Backend, client_cls=lambda *a, **kw: None)
        assert gateway.ensure_quote_ready()["qot_logined"] is True
        source = SimpleNamespace(get_market_state=lambda codes: events.append(codes) or {"US.NVDA": "MORNING"})
        assert get_market_state(source, ["US.NVDA"]) == {"US.NVDA": "MORNING"}
        assert events == [["US.NVDA"]]
        guarded = FutuGateway(client=SimpleNamespace(get_positions=forbidden("account transport")),
                              backend=SimpleNamespace(_ensure_trade_client=forbidden("trade context")),
                              host="offline.invalid", port=1)
        try: guarded.get_positions()
        except PluginUnavailableError as exc: assert "broker-futu" in str(exc)
        else: raise AssertionError("market package granted broker access")
    ''', sites=(target,))


@pytest.mark.parametrize("missing", [None, "broker-futu", "broker-longbridge"])
def test_broker_selection_is_account_isolated_without_implicit_fallback(sandbox, provider_wheels, missing):
    selected = [key for key in ("broker-futu", "broker-longbridge") if key != missing]
    target = install(sandbox, provider_wheels, *selected)
    sandbox.run('''
        from src.application.broker_provider import resolve_broker_portfolio_provider
        from src.application.plugin_runtime import PluginUnavailableError
        cfg = {"accounts": ["alpha", "beta"], "portfolio": {"source": "auto"},
               "account_settings": {"alpha": {"type": "futu", "futu": {"account_id": "QA_A", "trd_env": "REAL"}},
                                    "beta": {"type": "longbridge", "longbridge": {"account_id": "QA_B", "region": "ap", "trd_env": "REAL", "credentials_env_prefix": "QA_LB"}}}}
        missing = sys.argv[3]
        futu = lambda **kw: {"account": "alpha", "source": "futu"}
        for account, broker in (("alpha", "futu"), ("beta", "longbridge")):
            try: provider = resolve_broker_portfolio_provider(cfg, account=account, futu_provider=futu)
            except PluginUnavailableError as exc:
                assert "broker-" + broker == missing
                assert "om plugins install " + missing in str(exc)
            else:
                assert "broker-" + broker != missing
                if broker == "futu": assert provider is futu
                else:
                    from src.application.longbridge_portfolio_context import fetch_longbridge_portfolio_context
                    assert provider is fetch_longbridge_portfolio_context
    ''', sites=(target,), arguments=(missing or "",))


@pytest.mark.parametrize("broker", ["futu", "longbridge"])
def test_missing_broker_cannot_consume_cache_or_fabricate_cash_holdings(sandbox, broker):
    sandbox.run('''
        from src.application.portfolio_context_service import load_account_portfolio_context
        broker = sys.argv[3]
        cfg = {"accounts": ["alpha"], "_resolved": {"market": "us"},
               "portfolio": {"source": "auto", "base_currency": "CNY"},
               "account_settings": {"alpha": {"type": broker, broker: {
                   "account_id": "QA_A", "trd_env": "REAL", "region": "ap", "credentials_env_prefix": "QA_LB"}}}}
        # Any cache read is forbidden: absence must be decided before accepting
        # even fresh evidence. Positive valid-cache qualification is a separate case.
        result = load_account_portfolio_context(market="富途" if broker == "futu" else "长桥",
            account="alpha", state_dir=scratch, log=lambda _: None, runtime_config=cfg,
            portfolio_source="auto", write_cache=False,
            load_json_fn=forbidden("cache read before capability admission"),
            fetch_futu_portfolio_context_fn=forbidden("broker fallback"))
        assert result["context_source"] == "unavailable", result
        assert result["cash_snapshot"]["status"] == "unknown", result
        assert result["cash_provider_error"] == "PluginUnavailableError", result
        assert "cash_by_currency" not in result and "stocks_by_symbol" not in result
        assert "om plugins install broker-" + broker in result["errors"][0]
        assert not list(scratch.rglob("*.sqlite*"))
    ''', arguments=(broker,))


def test_longbridge_missing_sdk_validation_fails_without_real_context(sandbox, provider_wheels):
    target = install(sandbox, provider_wheels, "broker-longbridge")
    sandbox.run('''
        from src.application.plugin_runtime import validate_plugin_import
        try: validate_plugin_import("broker-longbridge")
        except ImportError as exc:
            assert "requirements/longbridge.txt" in str(exc)
            assert "immutable bundles" in str(exc)
        else: raise AssertionError("missing SDK accepted")
    ''', sites=(target,))


@pytest.mark.parametrize("brokers", [("futu",), ("longbridge",), ("futu", "longbridge")])
def test_qa_new_controller_migrates_unmodified_four_package_release_and_managed_pm(
    managed_transition, four_package_archive, monkeypatch, capsys, brokers,
):
    from src.application import plugin_distribution as distribution, service_upgrade as upgrade
    from src.application.plugin_runtime import plugin_store, read_managed_plugin
    from src.interfaces.cli import main as cli

    ctx = managed_transition
    ctx.install("pm")
    restore_four_package_release(ctx, four_package_archive)
    assert read_managed_plugin(ctx.previous, "pm"), "legacy managed PM prerequisite missing"
    assert not upgrade._provider_catalog_supported(ctx.previous)
    ctx.config.write_text(json.dumps({"accounts": list(brokers), "portfolio": {"source": "auto"},
        "account_settings": {name: {"type": name, name: {
            "account_id": "QA_" + name, "trd_env": "REAL", "region": "ap",
            "credentials_env_prefix": "QA_LB"}} for name in brokers}}))
    before = inventory(ctx.previous), inventory(plugin_store(ctx.previous)), ctx.config.read_bytes(), ctx.profile.read_bytes()
    ctx.events.clear()
    ctx.requests.clear()
    entries = []
    staged = []
    original_validation = distribution.subprocess.run

    def validation(command, **kwargs):
        if command[4] == str(ctx.target) and "validate_plugin_import" in command[3]:
            assert command[0] == str(upgrade._release_python(ctx.target))
            staged.append(command)
            ctx.events.append("qa_staged_validate")
        return original_validation(command, **kwargs)

    monkeypatch.setattr(distribution, "subprocess", SimpleNamespace(run=validation, TimeoutExpired=subprocess.TimeoutExpired))

    def controller(**kwargs):
        entries.append(kwargs)
        assert Path(kwargs["repo_root"]) == ctx.current
        assert Path(kwargs["runtime_root"]) == ctx.runtime
        return upgrade.service_upgrade(**kwargs, run_cmd=ctx.runner)

    monkeypatch.setattr(cli, "service_upgrade", controller)
    argv = ["update", "apply", "--repo-root", str(ctx.current), "--runtime-root", str(ctx.runtime),
            "--releases-root", str(ctx.target.parent), "--cache-root", str(ctx.current.parent / "cache"),
            "--target-version", "1.0.1"]
    status, preview = cli_json(capsys, argv)
    assert status == 0 and preview["data"]["status"] == "dry_run", preview
    assert ctx.current.resolve() == ctx.previous and not ctx.requests
    assert (inventory(ctx.previous), inventory(plugin_store(ctx.previous)), ctx.config.read_bytes(), ctx.profile.read_bytes()) == before
    status, result = cli_json(capsys, [*argv, "--confirm"])
    assert status == 0 and result["ok"], result
    assert len(entries) == 2 and [entry["confirm"] for entry in entries] == [False, True]
    assert ctx.current.resolve() == ctx.target
    expected = {"pm", "market-opend", *("broker-" + name for name in brokers)}
    assert set(upgrade._managed_plugin_selection(ctx.target)) == expected
    for key in expected:
        receipt = read_managed_plugin(ctx.target, key)
        assert receipt["om_version"] == receipt["version"] == "1.0.1"
        assert receipt["repo_root"] == str(ctx.target.resolve())
        assert (plugin_store(ctx.target) / key / "site" / receipt["module"] / "__init__.py").is_file()
    assert (inventory(ctx.previous), inventory(plugin_store(ctx.previous)), ctx.config.read_bytes(), ctx.profile.read_bytes()) == before
    assert len(staged) == len(expected)
    assert ctx.events.index("qa_staged_validate") < ctx.events.index("managed_validate") < ctx.events.index("configs") < ctx.events.index("switch")
    wheels = [url.rsplit("/", 1)[1] for url in ctx.requests if url.endswith(".whl")]
    assert len(wheels) == len(expected)
    assert not any("om_broker_" in wheel and not any(name in wheel for name in brokers) for wheel in wheels)


def test_qa_actual_broker_cli_uninstall_is_not_resurrected_by_config_or_sdk(
    managed_transition, monkeypatch, capsys,
):
    from src.application import service_upgrade as upgrade
    from src.application.plugin_runtime import plugin_store, read_managed_plugin
    from tests import test_managed_plugin_upgrade as support

    ctx = managed_transition
    bind_managed_cli(monkeypatch, ctx.previous)
    for key in ("broker-futu", "broker-longbridge"):
        ctx.install(key)
        assert read_managed_plugin(ctx.previous, key)
        status, preview = cli_json(capsys, ["plugins", "uninstall", key, "--format", "json"])
        assert status == 0 and preview["status"] == "preview"
        assert read_managed_plugin(ctx.previous, key), "preview removed the broker"
        status, receipt = cli_json(capsys, ["plugins", "uninstall", key, "--confirm", "--format", "json"])
        assert status == 0 and receipt["status"] == "uninstalled"
        assert not (plugin_store(ctx.previous) / key).exists()
    code = (
        "import sys,importlib.util,importlib.metadata as m;sys.path.insert(0,sys.argv[1]);import src;"
        "assert m.version('longbridge')=='5.3.0';"
        "assert importlib.util.find_spec('om_broker_futu') is None;"
        "assert importlib.util.find_spec('om_broker_longbridge') is None"
    )
    checked = support._offline_import([str(upgrade._release_python(ctx.previous)), "-I", "-c", code, str(ctx.previous)],
                                      capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    ctx.config.write_text(json.dumps({"accounts": ["alpha", "beta"], "portfolio": {"source": "auto"},
        "account_settings": {"alpha": {"type": "futu"}, "beta": {"type": "longbridge"}}}))
    if not ctx.bundled:
        # The source runtime really retains a separately installed SDK.
        ctx.offline.probe_output = json.dumps([False, True, False])
    before = inventory(plugin_store(ctx.previous)), ctx.config.read_bytes(), ctx.profile.read_bytes()
    ctx.events.clear()
    ctx.requests.clear()
    result = ctx.run()
    assert result["ok"] and ctx.current.resolve() == ctx.target, result
    assert upgrade._managed_plugin_selection(ctx.target) == ["market-opend"]
    assert (inventory(plugin_store(ctx.previous)), ctx.config.read_bytes(), ctx.profile.read_bytes()) == before
    assert not any("om_broker_" in url for url in ctx.requests)
    assert not any("./plugins/broker-" in " ".join(command) for command in ctx.offline.commands)
    checked = support._offline_import([str(upgrade._release_python(ctx.target)), "-I", "-c", code, str(ctx.target)],
                                      capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_qa_historical_four_package_rollback_never_downloads_or_installs(
    managed_transition, four_package_archive, monkeypatch,
):
    from src.application import plugin_distribution as distribution, runtime_bundle, service_upgrade as upgrade
    from src.application.plugin_runtime import plugin_store
    from tests import test_managed_plugin_upgrade as support

    ctx = managed_transition
    ctx.install("pm")
    restore_four_package_release(ctx, four_package_archive)
    # Simulate a running newer release while keeping the historical target exact.
    ctx.current.unlink()
    ctx.current.symlink_to(ctx.target, target_is_directory=True)
    historical = inventory(ctx.previous), inventory(plugin_store(ctx.previous))
    target_before = inventory(ctx.target), inventory(plugin_store(ctx.target))
    runtime_before = ctx.config.read_bytes(), ctx.profile.read_bytes()
    ctx.events.clear()
    ctx.requests.clear()
    attempts = []

    def deny(name):
        def reject(*args, **kwargs):
            attempts.append(name)
            raise AssertionError("rollback must not " + name)
        return reject

    monkeypatch.setattr(runtime_bundle, "urlopen", deny("download"))
    monkeypatch.setattr(runtime_bundle, "download_verified", deny("download_verified"))
    monkeypatch.setattr(distribution, "install_plugin", deny("install_plugin"))
    monkeypatch.setattr(upgrade, "_ensure_release_runtime", deny("prepare_or_install_runtime"))
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        if "install" in command or any(word in command for word in ("curl", "wget", "pip", "uv")):
            return deny("install_or_download_command")()
        assert command[0] == str(upgrade._release_python(ctx.previous)), command
        assert "read_managed_plugin" in command[3], command
        assert json.loads(command[-1]) == ["pm"], "historical rollback was given provider selections"
        return support._offline_import(command, **kwargs)

    kwargs = dict(repo_root=ctx.current, runtime_root=ctx.runtime, releases_root=ctx.target.parent,
                  to_version="1.0.0", restart_services=False, run_cmd=runner)
    preview = upgrade.service_rollback(**kwargs)
    assert preview["ok"] and ctx.current.resolve() == ctx.target, preview
    assert not commands and not attempts
    result = upgrade.service_rollback(**kwargs, confirm=True)
    assert result["ok"] and result["status"] == "rolled_back", result
    assert ctx.current.resolve() == ctx.previous
    assert len(commands) == 1 and not attempts and not ctx.requests
    assert (inventory(ctx.previous), inventory(plugin_store(ctx.previous))) == historical
    assert (inventory(ctx.target), inventory(plugin_store(ctx.target))) == target_before
    assert (ctx.config.read_bytes(), ctx.profile.read_bytes()) == runtime_before
    assert set(upgrade._managed_plugin_selection(ctx.previous)) == {"pm"}
    assert not upgrade._provider_catalog_supported(ctx.previous)


def test_qa_managed_cli_last_market_refusal_preserves_inventory(
    managed_transition, monkeypatch, capsys,
):
    from src.application.plugin_runtime import plugin_store, read_managed_plugin

    ctx = managed_transition
    bind_managed_cli(monkeypatch, ctx.previous)
    before = inventory(ctx.previous), inventory(plugin_store(ctx.previous)), ctx.config.read_bytes(), ctx.profile.read_bytes()
    ctx.requests.clear()
    for confirmation in ([], ["--confirm"]):
        status, result = cli_json(capsys, ["plugins", "uninstall", "market-opend", *confirmation, "--format", "json"])
        assert status == 2 and not result["ok"], result
        assert result["error"]["code"] == "PLUGIN_ERROR"
        assert "last valid market" in result["error"]["message"]
        assert read_managed_plugin(ctx.previous, "market-opend")
        assert (inventory(ctx.previous), inventory(plugin_store(ctx.previous)), ctx.config.read_bytes(), ctx.profile.read_bytes()) == before
        assert not ctx.requests


@pytest.fixture
def without_longbridge_python(tmp_path):
    environment = tmp_path / "no-longbridge-python"
    venv.EnvBuilder(with_pip=False).create(environment)
    site = next((environment / "lib").glob("python*/site-packages"))
    dependencies = dependency_site(environment)
    for entry in dependencies.iterdir():
        (site / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    return environment / "bin/python"


def test_qa_managed_cli_longbridge_missing_sdk_refuses_without_mutating_runtime(
    managed_transition, without_longbridge_python, monkeypatch, capsys,
):
    from src.application import plugin_distribution as distribution
    from src.application.plugin_runtime import plugin_store, read_managed_plugin
    from tests import test_managed_plugin_upgrade as support

    ctx = managed_transition
    bind_managed_cli(monkeypatch, ctx.previous)
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        assert "pip" not in command and "--target" not in command, "SDK acquisition was implicit"
        assert "validate_plugin_import" in command[3], command
        return support._offline_import([str(without_longbridge_python), *command[1:]], **kwargs)

    monkeypatch.setattr(distribution, "subprocess", SimpleNamespace(run=runner, TimeoutExpired=subprocess.TimeoutExpired))
    before = inventory(ctx.previous), ctx.config.read_bytes(), ctx.profile.read_bytes()
    store_before = inventory(plugin_store(ctx.previous))
    status, preview = cli_json(capsys, ["plugins", "install", "broker-longbridge", "--format", "json"])
    assert status == 0 and preview["status"] == "preview"
    assert not commands and inventory(plugin_store(ctx.previous)) == store_before
    status, result = cli_json(capsys, ["plugins", "install", "broker-longbridge", "--confirm", "--format", "json"])
    assert status == 2 and not result["ok"], result
    assert "requirements/longbridge.txt" in result["error"]["message"]
    assert len(commands) == 1
    assert read_managed_plugin(ctx.previous, "broker-longbridge") is None
    assert (inventory(ctx.previous), ctx.config.read_bytes(), ctx.profile.read_bytes()) == before
    assert inventory(plugin_store(ctx.previous)) == store_before
    assert not any("longbridge" in str(command).lower() and "install" in command for command in commands)


def test_qa_longbridge_missing_target_sdk_stops_before_configs_and_switch(
    managed_transition, without_longbridge_python,
):
    from src.application import runtime_bundle, service_upgrade as upgrade
    from src.application.plugin_runtime import plugin_store
    from tests.test_managed_plugin_upgrade import _python_proxy

    ctx = managed_transition
    ctx.install("broker-longbridge")
    ctx.target_interpreter["python"] = without_longbridge_python
    if ctx.bundled:
        _python_proxy(upgrade._release_python(ctx.target), without_longbridge_python)
        path = ctx.target / runtime_bundle.MANIFEST
        manifest = json.loads(path.read_text())
        manifest["files"][runtime_bundle.PYTHON] = runtime_bundle.sha256(upgrade._release_python(ctx.target))
        path.write_text(json.dumps(manifest))
    before = inventory(plugin_store(ctx.previous)), inventory(ctx.previous), ctx.config.read_bytes(), ctx.profile.read_bytes()
    immutable = inventory(ctx.target) if ctx.bundled else None
    ctx.events.clear()
    result = ctx.run()
    assert not result["ok"] and not result["symlink_switched"], result
    assert ctx.current.resolve() == ctx.previous
    assert "requirements/longbridge.txt" in json.dumps(result), result
    assert (inventory(plugin_store(ctx.previous)), inventory(ctx.previous), ctx.config.read_bytes(), ctx.profile.read_bytes()) == before
    assert all(event not in ctx.events for event in ("configs", "switch", "services", "restart", "health"))
    assert not (plugin_store(ctx.target) / "broker-longbridge").exists()
    if ctx.bundled:
        assert inventory(ctx.target) == immutable


def test_qa_missing_market_blocks_public_candidate_scan_and_evaluation(sandbox):
    sandbox.run('''
        from unittest.mock import patch
        from src.application import option_candidates as candidates
        from src.application.multi_tick.required_data_prefetch import prefetch_required_data
        from src.application.plugin_runtime import PluginUnavailableError, plugin_installed
        from src.application.tool_execution import execute_tool
        assert not plugin_installed("market-opend")
        cfg = {"market_data": {"provider": "opend"}, "symbols": [{"symbol": "NVDA"}]}
        with patch.object(candidates, "load_runtime_config", return_value=(scratch / "config.us.json", cfg)), \
             patch.object(candidates, "ensure_runtime_config_freshness", return_value=None), \
             patch.object(candidates, "current_exchange_rate_snapshot", forbidden("FX before market admission")):
            with candidates.option_candidates_context(runtime_root=scratch):
                response = execute_tool("option_candidates", {"queries": [{"symbol": "NVDA", "strategy": "short_put"}]})
        assert "om plugins install market-opend" in json.dumps(response), response
        assert not response.get("data", {}).get("results", [{}])[0].get("candidates"), response
        try:
            prefetch_required_data(vpy=scratch / "unused-python", base=scratch,
                cfg=cfg, shared_required=scratch / "required")
        except PluginUnavailableError as exc:
            assert "om plugins install market-opend" in str(exc)
        else:
            raise AssertionError("scheduled scan admitted missing market")
        assert not list(scratch.rglob("*.csv")), "missing market wrote evaluated candidates"
    ''')


def test_qa_real_broker_removal_invalidates_fresh_cache_prepared_and_capacity(sandbox, provider_wheels):
    sandbox.run('''
        from pip._internal.cli.main import main
        from unittest.mock import patch
        from datetime import datetime, timezone
        import importlib
        target = scratch / "broker-site"
        assert main(["install", "--no-index", "--no-deps", "--no-compile", "--target", str(target), sys.argv[3]]) == 0
        sys.path.insert(0, str(target))
        from src.application.plugin_runtime import plugin_installed
        assert plugin_installed("broker-futu")
        from tests.cash_evidence_helpers import cash_config, cash_portfolio
        from tests.test_prepared_portfolio_context import _state_dirs, _config_authorities, _prepare, _generation_kwargs
        from src.application import prepared_portfolio_context as prepared
        from src.application.portfolio_context_service import load_account_portfolio_context, cash_snapshot_is_usable
        from domain.domain.risk_capacity import compute_sell_put_effective_cash, compute_sell_put_cash_capacity
        now = datetime.now(timezone.utc)
        cfg = cash_config()
        cached = cash_portfolio({"cash_by_currency": {"USD": 20000}, "cash_source_observed_at": now.isoformat()}, evaluated_at=now)
        common = dict(market="富途", account="lx", state_dir=scratch, log=lambda _: None,
            runtime_config=cfg, portfolio_source="auto", write_cache=False, now_utc=now,
            fetch_futu_portfolio_context_fn=forbidden("unexpected broker/cache fallback"))
        positive = load_account_portfolio_context(**common, load_json_fn=lambda _: cached)
        assert positive["context_source"] == "account_cache" and cash_snapshot_is_usable(positive), positive
        effective = compute_sell_put_effective_cash(cash_by_currency=positive["cash_by_currency"],
            cash_secured_by_currency={}, native_currency="USD", convert_currency=lambda value, *a: value)
        assert effective.available and effective.cash_free == 20000
        assert compute_sell_put_cash_capacity(cash_required_native=10000,
            cash_free_effective_native=effective.cash_free, cash_native_currency="USD").accepted
        shared, states = _state_dirs(scratch, "qa-prepared")
        authorities = _config_authorities(scratch, "qa-prepared")
        manifests = _prepare(scratch, shared, states, "qa-prepared", authorities=authorities)
        identity = _generation_kwargs(scratch, Path(manifests["lx"]["manifest_path"]), run_id="qa-prepared",
            authority=authorities["lx"], manifest_sha256=manifests["lx"]["manifest_sha256"])
        payload = prepared.load_prepared_portfolio_context(**identity)
        assert payload["stocks_by_symbol"]["NVDA"]["account"] == "lx"
        frozen = {str(p): p.read_bytes() for p in (scratch / "output_runs").rglob("*") if p.is_file()}
        # Remove actual installed files, retaining any imported module/cache state.
        shutil.rmtree(target)
        importlib.invalidate_caches()
        assert not plugin_installed("broker-futu")
        negative = load_account_portfolio_context(**common, load_json_fn=forbidden("fresh cache bypass"))
        assert negative["context_source"] == "unavailable" and not cash_snapshot_is_usable(negative), negative
        assert negative["cash_provider_error"] == "PluginUnavailableError"
        assert "cash_by_currency" not in negative and "stocks_by_symbol" not in negative
        effective = compute_sell_put_effective_cash(cash_by_currency=negative.get("cash_by_currency"),
            cash_secured_by_currency={}, native_currency="USD", convert_currency=forbidden("FX on missing broker"))
        assert not effective.available and effective.cash_free is None
        capacity = compute_sell_put_cash_capacity(cash_required_native=10000,
            cash_free_effective_native=effective.cash_free, cash_native_currency="USD")
        assert not capacity.accepted and capacity.cash_free is None
        for load in (prepared.load_prepared_portfolio_context, prepared.load_prepared_portfolio_context_receipt):
            try: load(**identity)
            except prepared.PreparedPortfolioContextError as exc: assert "broker-futu" in str(exc)
            else: raise AssertionError("current prepared receipt bypassed actual broker removal")
        identity.pop("expected_runtime_config")
        assert prepared.load_prepared_portfolio_context(**identity) == payload
        assert prepared.load_prepared_portfolio_context_receipt(**identity)["payload"] == payload
        assert {str(p): p.read_bytes() for p in (scratch / "output_runs").rglob("*") if p.is_file()} == frozen
    ''', arguments=(provider_wheels["broker-futu"],))


def test_qa_positive_managed_cli_install_receipt_and_idempotency(managed_transition, monkeypatch, capsys):
    from src.application.plugin_runtime import plugin_store, read_managed_plugin

    ctx = managed_transition
    bind_managed_cli(monkeypatch, ctx.previous)
    before = inventory(ctx.previous), ctx.config.read_bytes(), ctx.profile.read_bytes()
    store_before = inventory(plugin_store(ctx.previous))
    ctx.requests.clear()
    argv = ["plugins", "install", "broker-futu", "--format", "json"]
    status, preview = cli_json(capsys, argv)
    assert status == 0 and preview["status"] == "preview", preview
    assert inventory(plugin_store(ctx.previous)) == store_before
    assert not any(url.endswith(".whl") for url in ctx.requests), "preview acquired a wheel"
    status, installed = cli_json(capsys, [*argv, "--confirm"])
    assert status == 0 and installed["ok"], installed
    receipt = read_managed_plugin(ctx.previous, "broker-futu")
    assert receipt and receipt["repo_root"] == str(ctx.previous.resolve())
    assert receipt["version"] == receipt["om_version"] == "1.0.0"
    assert (plugin_store(ctx.previous) / "broker-futu/site/om_broker_futu/__init__.py").read_bytes() == (
        ROOT / "plugins/broker-futu/src/om_broker_futu/__init__.py").read_bytes()
    after_install = inventory(plugin_store(ctx.previous))
    wheel_requests = [url for url in ctx.requests if url.endswith(".whl")]
    assert len(wheel_requests) == 1 and "om_broker_futu" in wheel_requests[0]
    status, again = cli_json(capsys, [*argv, "--confirm"])
    assert status == 0 and again["ok"], again
    assert inventory(plugin_store(ctx.previous)) == after_install
    assert [url for url in ctx.requests if url.endswith(".whl")] == wheel_requests
    assert (inventory(ctx.previous), ctx.config.read_bytes(), ctx.profile.read_bytes()) == before


def test_qa_pip_installed_source_rollback_missing_sdk_rejected_before_switch(
    transition, without_longbridge_python, provider_wheels, monkeypatch, tmp_path,
):
    from src.application import plugin_distribution as distribution, runtime_bundle, service_upgrade as upgrade
    from src.application.plugin_runtime import plugin_store
    from tests import test_managed_plugin_upgrade as support

    previous, active, current, events = transition
    for root in (previous, active):
        support._release_code(root)
    support._python_proxy(upgrade._release_python(previous), without_longbridge_python)
    site = next((without_longbridge_python.parent.parent / "lib").glob("python*/site-packages"))
    # Use setuptools-built wheels and actual pip ownership; no managed receipt.
    command = [str(without_longbridge_python), "-I", "-c",
        "from pip._internal.cli.main import main; import sys; raise SystemExit(main(['install','--no-index','--no-deps','--no-compile',*sys.argv[1:]]))",
        str(provider_wheels["market-opend"]), str(provider_wheels["broker-longbridge"])]
    installed = support._offline_import(command, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert (site / "om_market_opend").is_dir() and (site / "om_broker_longbridge").is_dir()
    assert not (site / "longbridge").exists() and not plugin_store(previous).exists()
    current.unlink()
    current.symlink_to(active, target_is_directory=True)
    runtime = tmp_path / "rollback-runtime"
    runtime.mkdir()
    config = runtime / "config.us.json"
    config.write_text(json.dumps({"accounts": ["unrelated"], "account_settings": {"unrelated": {"type": "futu"}}}))
    before = inventory(previous), inventory(active), config.read_bytes(), inventory(without_longbridge_python.parent.parent)
    attempts, commands = [], []

    def deny(name):
        def reject(*args, **kwargs):
            attempts.append(name)
            (tmp_path / "rollback-forbidden.json").write_text(json.dumps(attempts))
            raise AssertionError(name)
        return reject

    monkeypatch.setattr(distribution, "install_plugin", deny("rollback installation"))
    monkeypatch.setattr(runtime_bundle, "urlopen", deny("rollback download"))
    monkeypatch.setattr(upgrade, "_ensure_release_runtime", deny("rollback runtime preparation"))
    monkeypatch.setattr(upgrade, "_provider_upgrade_selection", deny("rollback migration"))

    def runner(command, **kwargs):
        commands.append(command)
        assert command[0] == str(upgrade._release_python(previous)) and command[1:3] == ["-I", "-c"]
        assert "install" not in command and "pip" not in command
        return support._offline_import(command, **kwargs)

    result = upgrade.service_rollback(repo_root=current, runtime_root=runtime, releases_root=previous.parent,
        to_version="1.0.0", confirm=True, restart_services=False, run_cmd=runner)
    assert not result["ok"] and not result["changed"] and not result["symlink_switched"], result
    assert "requirements/longbridge.txt" in json.dumps(result), result
    assert commands and not attempts and current.resolve() == active
    assert not any(event in events for event in ("configs", "switch", "services", "restart", "health"))
    assert (inventory(previous), inventory(active), config.read_bytes(), inventory(without_longbridge_python.parent.parent)) == before
    assert set(inventory(runtime)) <= {"config.us.json", "upgrade_status.json", "locks/upgrade.lock.guard"}


def test_qa_default_installer_really_installs_market_without_brokers(managed_transition, tmp_path):
    """Real shell/managed installer; bootstrap dependencies/native runtime are fixtures."""
    from src.application import runtime_bundle
    from src.application.plugin_runtime import plugin_store, read_managed_plugin
    from tests.test_install_script import _installer_env, _write_executable
    from tests.test_runtime_bundle_install import installer_env

    ctx = managed_transition
    template = tmp_path / "installer-template"
    shutil.copytree(ctx.previous, template)
    # The fixture template models an uninstalled release; core stays real.
    for name in ("src", "domain", "om_strategy_core"):
        shutil.copytree(ROOT / name, template / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    dependencies = dependency_site(tmp_path)
    routes = {}
    assets = tmp_path / "installer-assets"
    assets.mkdir()
    for index, (url, data) in enumerate(ctx.downloads.items()):
        path = assets / str(index)
        path.write_bytes(data)
        routes[url] = str(path)
    for tag, release in ctx.releases.items():
        path = assets / tag
        path.write_text(json.dumps(release))
        routes[f"https://api.github.com/repos/{runtime_bundle.REPOSITORY}/releases/tags/{tag}"] = str(path)
    (assets / "routes.json").write_text(json.dumps(routes))
    guard = GUARD.split("# Do not inherit", 1)[0].replace(
        "scratch = Path(sys.argv[1]).resolve()", f"scratch = Path({str(tmp_path)!r}).resolve()"
    ).replace('if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "pty.spawn"}:',
              'if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "pty.spawn"} and not globals().get("_qa_validation_spawn", False):')
    guard += f'\nsys.path[:] = [p for p in sys.path if p and "site-packages" not in p and "dist-packages" not in p]\nsys.path[:0] = [str(Path.cwd()), {str(dependencies)!r}]\n'
    driver = tmp_path / "installer-python"
    body = '''
import io, subprocess
from src.application import runtime_bundle
routes = json.loads(Path(ASSETS, "routes.json").read_text())
class Response(io.BytesIO):
    def __init__(self, url): super().__init__(Path(routes[url]).read_bytes()); self.url = url
    def geturl(self): return self.url
def offline(request, **kwargs):
    url = request if isinstance(request, str) else request.full_url
    if url not in routes: forbidden("unmapped installer transport: " + url)()
    with Path(ASSETS, "requests.jsonl").open("a") as stream: stream.write(json.dumps(url) + "\\n")
    return Response(url)
runtime_bundle.urlopen = offline
# Native interpreter/dependency qualification belongs to the bundle fixture;
# this test exercises the shell's default provider installation transaction.
runtime_bundle.validate_runtime = lambda root: {"ok": True}
original_run = subprocess.run
def validation(command, **kwargs):
    global _qa_validation_spawn
    assert command[1:3] == ["-I", "-c"] and "validate_plugin_import" in command[3], command
    record("installer_validation", command[4:])
    _qa_validation_spawn = True
    try:
        return original_run([sys.executable, "-I", "-B", "-c", CHILD_GUARD + command[3], *command[4:]], **kwargs)
    finally: _qa_validation_spawn = False
subprocess.run = validation
assert sys.argv[1] == "-c", sys.argv
code = sys.argv[2]
sys.argv = ["-c", *sys.argv[3:]]
exec(compile(code, "<real installer Python entry>", "exec"))
'''
    _write_executable(driver, f"#!{sys.executable}\n" + guard +
        f"\nASSETS = {str(assets)!r}\nCHILD_GUARD = {guard!r}\n" + textwrap.dedent(body))
    prefix = tmp_path / "fresh-install"
    if ctx.bundled:
        _write_executable(template / runtime_bundle.PYTHON, f"#!/bin/sh\nexec {shlex.quote(str(driver))} \"$@\"\n")
        manifest = json.loads((template / runtime_bundle.MANIFEST).read_text())
        manifest["files"] = {p: runtime_bundle.sha256(template / p) for p in inventory(template) if p != runtime_bundle.MANIFEST}
        (template / runtime_bundle.MANIFEST).write_text(json.dumps(manifest, indent=2))
        archive = tmp_path / "installer-runtime.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(template.rglob("*")):
                if path.is_file(): tar.add(path, arcname=path.relative_to(template).as_posix(), recursive=False)
        env, forbidden_tools = installer_env(tmp_path, archive, tag="v1.0.0")
        args = []
    else:
        env = _installer_env(tmp_path)
        env["FAKE_REAL_PYTHON"] = str(driver)
        _write_executable(tmp_path / "fake-bin/git", '#!/bin/bash\nset -eu\n[[ "$1" == clone ]]\ndest="${!#}"\n/bin/cp -R ' + shlex.quote(str(template)) + ' "$dest"\n')
        forbidden_tools = tmp_path / "forbidden-tools.log"
        args = ["--source"]
    env.update(TMPDIR=str(tmp_path), PYTHONDONTWRITEBYTECODE="1", OM_SECRET_BACKEND="env",
               OM_RUNTIME_ROOT=str(tmp_path / "isolated-runtime"))
    result = subprocess.run(["/bin/bash", str(ROOT / "scripts/install.sh"), *args,
        "--version", "v1.0.0", "--prefix", str(prefix), "--no-install-cli"],
        env=env, capture_output=True, text=True, timeout=90)
    (tmp_path / "installer.stdout").write_text(result.stdout)
    (tmp_path / "installer.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    installed = prefix / "releases/v1.0.0"
    assert (prefix / "current").resolve() == installed
    receipt = read_managed_plugin(installed, "market-opend")
    assert receipt and receipt["repo_root"] == str(installed.resolve())
    assert receipt["version"] == receipt["om_version"] == "1.0.0"
    assert {p.name for p in plugin_store(installed).iterdir() if p.is_dir()} == {"market-opend"}
    assert read_managed_plugin(installed, "broker-futu") is None
    assert read_managed_plugin(installed, "broker-longbridge") is None
    assert not (installed / "config.yaml").exists() and not (installed / "config.us.json").exists()
    requests = [json.loads(line) for line in (assets / "requests.jsonl").read_text().splitlines()]
    assert len([url for url in requests if url.endswith(".whl")]) == 1
    assert "om_market_opend_plugin" in requests[-1]
    attempts = [json.loads(line) for line in (tmp_path / "attempts.jsonl").read_text().splitlines()]
    assert any(entry["kind"] == "installer_validation" for entry in attempts)
    assert not any(entry["kind"] == "forbidden" for entry in attempts), attempts
    assert not forbidden_tools.exists()


def test_qa_position_evaluate_missing_market_rejects_before_live_io(sandbox):
    # Copy only the actually installed advice distribution, preserving metadata;
    # the provider-free dependency site cannot inherit the controller's market.
    dist = importlib.metadata.distribution("om-position-advice-plugin")
    target = sandbox.work / "installed-advice"
    for entry in dist.files or ():
        if ".." in entry.parts or entry.suffix == ".pyc":
            continue
        source = Path(dist.locate_file(entry))
        if source.is_file():
            path = target / entry
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
    assert (target / "om_position_advice").is_dir() or (target / "om_position_advice_plugin").is_dir()
    sandbox.run('''
        from unittest.mock import patch
        from types import SimpleNamespace
        from src.application.agent_tools import close_advice
        from src.application import position_advice as service
        from src.application.tool_execution import execute_tool
        from src.application.plugin_runtime import plugin_installed
        assert plugin_installed("position-advice") and not plugin_installed("market-opend")
        cfg = {"accounts": ["sample"], "portfolio": {"account": "sample", "broker": "富途"},
            "close_advice": {"enabled": True}, "symbols": [{"symbol": "NVDA", "broker": "US",
            "fetch": {"source": "opend", "host": "offline.invalid", "port": 1}}]}
        with patch.object(close_advice, "load_runtime_config", return_value=(scratch / "config.us.json", cfg)), \
             patch.object(close_advice, "repo_base", return_value=scratch), \
             patch("src.application.runtime_paths.resolve_runtime_root", return_value=SimpleNamespace(runtime_root=scratch)), \
             patch.object(service, "_read_holdings", forbidden("manual evaluate ledger access")), \
             patch.object(service, "_run_current_assessment", forbidden("evaluate before market admission")):
            result = execute_tool("position_advice_evaluate", {"account": "sample", "positions": [{
                "input_id": "qa-1", "input_as_of_utc": "2026-11-01T00:00:00Z", "symbol": "NVDA",
                "option_type": "put", "expiration": "2026-12-18", "strike": 100,
                "contracts_open": 1, "premium": 2, "currency": "USD", "multiplier": 100}]})
        assert not result["ok"] and "om plugins install market-opend" in json.dumps(result), result
        assert not list(scratch.rglob("position_advice.json"))
    ''', sites=(target,))


@pytest.mark.parametrize("wrapped", [False, True], ids=["raw", "window"])
def test_qa_raw_and_window_missing_market_never_claim_provider_request(sandbox, wrapped):
    sandbox.run('''
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace
        from src.infrastructure.futu_gateway import FutuGateway
        from src.application.opend_market_snapshot_fetching import _record_snapshot_call
        from src.application.research.observation_series import _WindowGateway
        from src.application.plugin_runtime import PluginUnavailableError, plugin_installed
        assert not plugin_installed("market-opend")
        gateway = FutuGateway(client=SimpleNamespace(get_snapshot=forbidden("missing-market raw transport")),
            backend=SimpleNamespace(_ensure_quote_client=forbidden("missing-market context")),
            host="offline.invalid", port=1)
        calls = []
        now = datetime.now(timezone.utc)
        if sys.argv[3] == "True":
            gateway = _WindowGateway(gateway, start=now - timedelta(seconds=1),
                end=now + timedelta(seconds=30), clock=lambda: now, calls=calls)
        receipts, responses = [], []
        try:
            _record_snapshot_call(gateway, ["US.NVDA"], receipts,
                stage="primary", batch_start=0, provider_responses=responses)
        except PluginUnavailableError as exc:
            assert "om plugins install market-opend" in str(exc)
        else:
            raise AssertionError("missing market reached snapshot provider")
        assert not responses and not calls, (responses, calls)
        assert all(receipt["provider_call_started"] is False
            and receipt["requested_at_utc"] is None and receipt["received_at_utc"] is None
            for receipt in receipts), receipts
        assert not list(scratch.rglob("*receipt*.json"))
    ''', arguments=(wrapped,))
