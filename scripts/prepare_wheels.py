"""Prepare Python wheels and publish them to a same-host wheel directory."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _digest(path: Path) -> bytes:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--allow-index", action="store_true", help="Allow the configured package index on a cache miss")
    parser.add_argument("pip_args", nargs=argparse.REMAINDER, metavar="-- PIP_WHEEL_ARGS")
    args = parser.parse_args()
    if args.pip_args[:1] != ["--"] or len(args.pip_args) < 2:
        parser.error("pass dependency inputs after --")
    wheel_dir = args.wheel_dir.resolve()
    wheel_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=wheel_dir) as temporary:
        command = [
            sys.executable, "-m", "pip", "--disable-pip-version-check", "wheel",
            "--wheel-dir", temporary, "--find-links", str(wheel_dir), *args.pip_args[1:],
        ]
        offline = subprocess.run(
            [*command, "--no-index", "--isolated"],
            env={**os.environ, "PIP_CONFIG_FILE": os.devnull},
            check=False,
        )
        if offline.returncode:
            if not args.allow_index:
                return offline.returncode
            for wheel in Path(temporary).glob("*.whl"):
                wheel.unlink()
            online = subprocess.run(command, check=False)
            if online.returncode:
                return online.returncode
        wheels = sorted(Path(temporary).glob("*.whl"))
        if not wheels:
            parser.exit(1, "No dependency wheels were prepared.\n")
        for wheel in wheels:
            destination = wheel_dir / wheel.name
            digest = _digest(wheel)
            try:
                wheel.chmod(0o644)
                os.link(wheel, destination)
            except FileExistsError:
                if destination.is_symlink() or not destination.is_file() or _digest(destination) != digest:
                    parser.exit(1, f"Wheel content conflict: {destination}\n")
                source = "reused"
            else:
                source = "published"
            print(f"{source} {digest.hex()} {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
