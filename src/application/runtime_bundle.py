"""Published runtime artifacts: identity, verified downloads and isolated materialization.

This owner never switches current, installs dependencies, or manages services.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

REPOSITORY = "liuxie066/options-monitor"
MANIFEST = "om.runtime.json"
PYTHON = "runtime/python/bin/python3"
PLATFORMS = ("macos-arm64", "macos-x86_64", "linux-x86_64")
MINIMUM_SYSTEMS = {
    "macos-arm64": "macOS 14.0", "macos-x86_64": "macOS 14.0",
    "linux-x86_64": "glibc 2.28; optional services require systemd and systemd-creds",
}
TAG_RE = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")
SHA_RE = re.compile(r"[0-9a-f]{64}")


class RuntimeBundleError(ValueError):
    pass


def current_platform() -> str:
    system = {"Darwin": "macos", "Linux": "linux"}.get(platform.system(), "unsupported")
    arch = {"aarch64": "arm64", "arm64": "arm64", "AMD64": "x86_64", "x86_64": "x86_64"}.get(platform.machine())
    result = f"{system}-{arch}"
    if result not in PLATFORMS:
        raise RuntimeBundleError(f"unsupported runtime platform: {result}; source installation is explicit only")
    return result


def asset_name(tag: str, platform_name: str) -> str:
    if not TAG_RE.fullmatch(tag) or platform_name not in PLATFORMS:
        raise RuntimeBundleError("invalid bundle tag/platform")
    return f"options-monitor-{tag}-{platform_name}.tar.gz"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _relative(name: str) -> bool:
    return (isinstance(name, str) and bool(name) and not name.startswith("/")
            and "\\" not in name and all(p not in ("", ".", "..") for p in name.split("/"))
            and not any(ord(c) < 32 for c in name))


def load_manifest(root: Path, *, expected_tag: str | None = None,
                  expected_platform: str | None = None, verify_files: bool = False) -> dict:
    root = Path(root).resolve()
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("kind") != "options-monitor-runtime":
            raise RuntimeBundleError("invalid runtime manifest schema")
        tag = data.get("tag", "")
        if (not TAG_RE.fullmatch(tag) or data.get("version") != tag[1:]
                or (expected_tag is not None and tag != expected_tag)
                or data.get("repository") != REPOSITORY
                or not re.fullmatch(r"[0-9a-f]{40}", data.get("commit", ""))
                or data.get("platform") not in PLATFORMS
                or (expected_platform is not None and data["platform"] != expected_platform)):
            raise RuntimeBundleError("runtime manifest identity mismatch")
        if data.get("minimum_system") != MINIMUM_SYSTEMS[data["platform"]]:
            raise RuntimeBundleError("runtime minimum_system declaration mismatch")
        python = data["python"]
        if (python["executable"] != PYTHON or not re.fullmatch(r"3\.(?:1[2-9]|[2-9][0-9])\.[0-9]+", python["version"])
                or not SHA_RE.fullmatch(python["sha256"]) or not python["source_url"].startswith("https://github.com/astral-sh/python-build-standalone/releases/download/")):
            raise RuntimeBundleError("invalid bundled Python declaration")
        executable = root / PYTHON
        if not executable.is_file() or not os.access(executable, os.X_OK) or executable.is_symlink():
            raise RuntimeBundleError("declared bundled Python is missing or not executable")
        if (root / "VERSION").read_text().strip() != data["version"]:
            raise RuntimeBundleError("bundle VERSION mismatch")
        if not isinstance(data["dependencies"], dict) or not data["dependencies"]:
            raise RuntimeBundleError("missing pinned dependency inventory")
        files = data["files"]
        if not isinstance(files, dict) or not {PYTHON, "VERSION", "om", "om-agent"}.issubset(files):
            raise RuntimeBundleError("incomplete bundle file inventory")
        for name, digest in files.items():
            if not _relative(name) or not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
                raise RuntimeBundleError("invalid bundle file inventory")
            if verify_files:
                path = root / name
                if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file() or sha256(path) != digest:
                    raise RuntimeBundleError(f"bundle file checksum mismatch: {name}")
        if verify_files:
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise RuntimeBundleError("bundle links are forbidden")
                if path.is_file():
                    name = path.relative_to(root).as_posix()
                    source = path.parent.parent / (path.name.split(".")[0] + ".py")
                    cached_source = (path.parent.name == "__pycache__" and path.suffix == ".pyc"
                                     and source.relative_to(root).as_posix() in files)
                    if name != MANIFEST and name not in files and not cached_source:
                        raise RuntimeBundleError(f"unlisted bundle file: {name}")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeBundleError(f"invalid or missing runtime manifest: {exc}") from exc
    return data


def _read_json(url: str) -> dict:
    with urlopen(Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "options-monitor"}), timeout=30) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeBundleError("release metadata too large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise RuntimeBundleError("invalid release metadata")
    return result


def resolve_release(tag: str = "latest", *, repository: str = REPOSITORY) -> dict:
    if repository != REPOSITORY or (tag != "latest" and not TAG_RE.fullmatch(tag)):
        raise RuntimeBundleError("invalid release identity")
    suffix = "latest" if tag == "latest" else f"tags/{tag}"
    result = _read_json(f"https://api.github.com/repos/{repository}/releases/{suffix}")
    actual = result.get("tag_name", "")
    if (not TAG_RE.fullmatch(actual) or result.get("draft") is not False
            or result.get("prerelease") is not False or "-" in actual
            or (tag != "latest" and actual != tag)):
        raise RuntimeBundleError("expected a published stable release")
    return result


def download_verified(url: str, destination: Path, digest: str) -> Path:
    """Download a public HTTPS artifact, publish the file only after SHA256 matches."""
    if not url.startswith("https://") or not SHA_RE.fullmatch(digest):
        raise RuntimeBundleError("HTTPS URL and SHA256 are required")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".part", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with urlopen(Request(url, headers={"User-Agent": "options-monitor"}), timeout=60) as response, temporary.open("wb") as out:
            if not response.geturl().startswith("https://"):
                raise RuntimeBundleError("artifact redirected outside HTTPS")
            shutil.copyfileobj(response, out)
        if sha256(temporary) != digest:
            raise RuntimeBundleError("artifact SHA256 mismatch")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def download_bundle(release: dict, destination: Path, *, platform_name: str | None = None) -> Path:
    tag = release.get("tag_name", "")
    name = asset_name(tag, platform_name or current_platform())
    assets = release.get("assets", [])
    def asset(named):
        matches = [a for a in assets if a.get("name") == named and a.get("state") == "uploaded"]
        if len(matches) != 1:
            raise RuntimeBundleError(f"published runtime asset unavailable: {named}")
        found = matches[0]
        expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{named}"
        if found.get("browser_download_url") != expected:
            raise RuntimeBundleError("runtime asset URL identity mismatch")
        return found
    target = asset(name)
    digest = target.get("digest", "")
    if isinstance(digest, str) and digest.startswith("sha256:") and SHA_RE.fullmatch(digest[7:]):
        digest = digest[7:]
    else:
        sidecar = asset(name + ".sha256")
        with urlopen(sidecar["browser_download_url"], timeout=30) as response:
            checksum = response.read(1024).decode("ascii").strip().split()
        if len(checksum) != 2 or checksum[1].lstrip("*") != name or not SHA_RE.fullmatch(checksum[0]):
            raise RuntimeBundleError("invalid runtime asset checksum")
        digest = checksum[0]
    return download_verified(target["browser_download_url"], destination, digest)


def safe_extract(archive: Path, destination: Path, *, allow_links: bool = False) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        seen = set()
        for member in members:
            name = member.name.rstrip("/")
            if name in ("", ".") and member.isdir():
                continue
            if not _relative(name) or name in seen:
                raise RuntimeBundleError("unsafe or duplicate archive path")
            seen.add(name)
            if not (member.isfile() or member.isdir() or (allow_links and (member.issym() or member.islnk()))):
                raise RuntimeBundleError("archive links/special files are forbidden")
        tar.extractall(destination, members=members, filter="data")


def materialize_bundle(archive: Path, destination: Path, *, expected_tag: str,
                       expected_platform: str | None = None) -> dict:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        safe_extract(archive, destination)
        return load_manifest(destination, expected_tag=expected_tag,
                             expected_platform=expected_platform or current_platform(), verify_files=True)
    except Exception:
        shutil.rmtree(destination)
        raise


def validate_runtime(root: Path) -> dict:
    """Offline qualification only; run native/Bot imports and public entrypoints."""
    root = Path(root).resolve()
    manifest = load_manifest(root, expected_platform=current_platform(), verify_files=True)
    with tempfile.TemporaryDirectory(prefix="om-bundle-check-") as temporary:
        env = {"PATH": "/usr/bin:/bin", "HOME": temporary, "OM_RUNTIME_ROOT": temporary + "/instance",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"}
        commands = [
            [str(root / PYTHON), "-c", "import sys,ssl,sqlite3,ctypes,subprocess,importlib.metadata as m; "
             "import pandas,numpy,yaml,futu,jsonschema; "
             "import src.application.bot.host,src.application.bot.runtime; "
             "assert sys.version.split()[0] == " + repr(manifest["python"]["version"]) + "; "
             "expected=" + repr(manifest["dependencies"]) + "; "
             "assert all(m.version(n)==v for n,v in expected.items()); "
             "assert int(pandas.Series([1,2]).sum())==3; "
             "assert sqlite3.connect(':memory:').execute('select 1').fetchone()==(1,); "
             "subprocess.run([sys.executable,'-c','import ssl,sqlite3,pandas,futu'],check=True)"],
            [str(root / "om"), "help"], [str(root / "om-agent"), "spec"],
        ]
        for command in commands:
            proc = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=180)
            if proc.returncode:
                raise RuntimeBundleError(f"runtime self-check failed ({Path(command[0]).name}): {proc.stderr[-4000:]}")
            if command[-1] == "spec":
                json.loads(proc.stdout)
    return {"ok": True, "platform": manifest["platform"], "version": manifest["version"],
            "python": manifest["python"]["version"], "checks": ["native/core/server/Bot imports", "subprocess", "om help", "om-agent spec"]}



def release_platforms(base: Path) -> list[str]:
    lock = json.loads((Path(base) / "scripts/runtime_bundle.lock.json").read_text())
    names = lock["release_platforms"]
    if not names or len(set(names)) != len(names) or any(n not in PLATFORMS for n in names):
        raise RuntimeBundleError("invalid qualified release platform policy")
    return names


def publication_assets(base: Path, tag: str) -> set[str]:
    return {asset_name(tag, platform_name) + suffix
            for platform_name in release_platforms(base)
            for suffix in ("", ".sha256", ".manifest.json", ".qualification.json")}


def validate_publication_set(base: Path, directory: Path, *, tag: str, commit: str) -> None:
    """Bind each native execution receipt and manifest to the exact archive and source."""
    expected = publication_assets(base, tag)
    actual = {p.name for p in directory.iterdir() if p.is_file()}
    if actual != expected:
        raise RuntimeBundleError(f"runtime publication set mismatch; missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")
    for platform_name in release_platforms(base):
        name = asset_name(tag, platform_name)
        archive = directory / name
        digest = sha256(archive)
        if (directory / (name + ".sha256")).read_text().strip() != f"{digest}  {name}":
            raise RuntimeBundleError("runtime publication checksum mismatch")
        manifest = json.loads((directory / (name + ".manifest.json")).read_text())
        receipt = json.loads((directory / (name + ".qualification.json")).read_text())
        with tarfile.open(archive, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.name == MANIFEST]
            if len(members) != 1 or not members[0].isfile():
                raise RuntimeBundleError("missing/duplicate archive manifest")
            archived = json.load(tar.extractfile(members[0]))
        if (archived != manifest or manifest.get("commit") != commit or manifest.get("tag") != tag
                or manifest.get("platform") != platform_name or manifest.get("source_dirty") is not False
                or receipt.get("ok") is not True or receipt.get("sha256") != digest
                or receipt.get("commit") != commit or receipt.get("version") != tag[1:]
                or receipt.get("platform") != platform_name or receipt.get("source_dirty") is not False
                or receipt.get("original_staging_removed") is not True
                or receipt.get("relocated_path_contains_chinese_and_spaces") is not True):
            raise RuntimeBundleError("runtime qualification/source/archive identity mismatch")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate_runtime(args.root), ensure_ascii=False))
