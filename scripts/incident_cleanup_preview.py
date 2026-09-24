"""Read-only 2026-09-23 incident candidate inventory; no delete option."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.application.incident_cleanup_preview import preview_incident_cleanup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=Path("/var/lib/options-monitor"))
    parser.add_argument("--tmp-root", type=Path, default=Path("/tmp"))
    parser.add_argument("--apps-root", type=Path, default=Path.home() / "apps")
    parser.add_argument("--unit-root", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--opend-log-root", type=Path,
                        default=Path.home() / ".com.futunn.FutuOpenD" / "Log")
    args = parser.parse_args()
    print(json.dumps(preview_incident_cleanup(**vars(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
