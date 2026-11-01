"""Small, valid wheel fixtures: offline and never imported or installed."""
import base64
import csv
import hashlib
import io
import json
from zipfile import ZipFile

from src.application import plugin_release as release


def wheel_files(distribution, module, version):
    info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    return {
        f"{module}/__init__.py": b'"""Offline test package."""\n',
        f"{info}/METADATA": (f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n"
                             "Requires-Python: >=3.12\n\n").encode(),
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
