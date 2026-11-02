"""Small, valid wheel fixtures: offline and never imported or installed."""
import base64
import csv
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import shutil
from zipfile import ZipFile

from src.application import plugin_release as release


def copy_provider_core(source, target):
    """Use real transport imports while keeping SDK calls forbidden in fixtures."""
    names = ["requirements/longbridge.txt", "src/infrastructure/futu_gateway.py",
             "src/infrastructure/futu_dataframe.py", "src/infrastructure/opend_retcodes.py",
             "src/infrastructure/net_port.py", "src/infrastructure/longbridge_gateway.py"]
    for package in ("src/application/settings", "src/application/secret_store"):
        names.extend(path.relative_to(source).as_posix() for path in (source / package).rglob("*.py"))
    for name in names:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)


def provider_test_dependencies(site):
    """Expose only core numeric dependencies, never ambient installed plugin owners."""
    for name in ("numpy", "pandas", "dateutil", "six", "pytz", "tzdata"):
        spec = importlib.util.find_spec(name)
        if spec is None:
            continue
        source = Path(spec.origin)
        source = source.parent if spec.submodule_search_locations else source
        destination = site / (name if spec.submodule_search_locations else source.name)
        if not destination.exists():
            destination.symlink_to(source, target_is_directory=source.is_dir())
    for name, data in {
        "longbridge/__init__.py": "",
        "longbridge/openapi.py": "class Config:\n    def __init__(self,*a,**k): raise AssertionError('SDK construction forbidden')\nclass TradeContext:\n    def __init__(self,*a,**k): raise AssertionError('SDK connection forbidden')\n",
        "longbridge-5.3.0.dist-info/METADATA": "Name: longbridge\nVersion: 5.3.0\n",
    }.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)


def copy_channel_core(source, target):
    """Copy the actual channel facades and their core imports into an isolated release."""
    for name in (
        "src/infrastructure/__init__.py",
        "src/infrastructure/feishu_bot.py",
        "src/infrastructure/feishu_bitable.py",
        "src/infrastructure/feishu_ws_client.py",
        "src/application/agent_tool_contracts.py",
        "src/application/channels/__init__.py",
        "src/application/channels/contracts.py",
        "src/application/channels/service.py",
        "src/application/channels/plugins.py",
        "src/application/channels/wechat_clawbot/__init__.py",
        "src/application/channels/wechat_clawbot/ilink_client.py",
        "constraints/runtime-bundle.txt",
    ):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)


def wheel_files(distribution, module, version):
    info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    dependencies = "Requires-Dist: lark-oapi\n" if distribution == "om-feishu-channel-plugin" else ""
    return {
        f"{module}/__init__.py": b'"""Offline test package."""\n',
        f"{info}/METADATA": (f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n"
                             f"Requires-Python: >=3.12\n{dependencies}\n").encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
    }


def write_wheel(path, files):
    files = dict(files)
    info = next(n.rsplit("/", 1)[0] for n in files if n.endswith(".dist-info/WHEEL"))
    record = info + "/RECORD"
    files.pop(record, None)
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, str(len(data))])
    writer.writerow([record, "", ""])
    files[record] = output.getvalue().encode()
    with ZipFile(path, "w") as wheel:
        for name, data in files.items():
            wheel.writestr(name, data)


def refresh_entry(path, entry):
    entry.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=path.stat().st_size)


def write_catalog(directory, catalog):
    (directory / release.CATALOG).write_text(json.dumps(catalog))
    (directory / release.NOTES).write_text(release.compatibility_notes(catalog))


def publication(directory, version="1.2.3", commit="a" * 40):
    directory.mkdir(parents=True, exist_ok=True)
    catalog = dict(schema_version=1, repository=release.REPOSITORY, om_version=version,
                   tag="v" + version, commit=commit, plugins=[])
    for plugin_id, distribution, module, _ in release.OFFICIAL_PLUGINS:
        filename = f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
        path = directory / filename
        write_wheel(path, wheel_files(distribution, module, version))
        entry = dict(id=plugin_id, distribution=distribution, module=module, version=version,
                     requires_python=">=3.12", asset=filename)
        refresh_entry(path, entry)
        catalog["plugins"].append(entry)
    write_catalog(directory, catalog)
    return catalog
