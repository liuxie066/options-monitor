#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.application.release_delta_coverage import (
    ReleaseDeltaCoverageError,
    build_release_delta_manifest,
    default_manifest_path,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="prepare the auditable commit-to-release-note coverage manifest",
    )
    parser.add_argument(
        "--target-version",
        required=True,
        help="target release version without the v prefix",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="optional manifest path; defaults to release/coverage/v<version>.json",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="refresh commit/note inventory while preserving still-valid dispositions",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    version = str(args.target_version).strip()
    try:
        output_path = (
            Path(args.output).expanduser().resolve()
            if args.output
            else default_manifest_path(BASE_DIR, version)
        )
        output_path.relative_to(BASE_DIR)
    except ReleaseDeltaCoverageError as exc:
        raise SystemExit(f"[RELEASE_DELTA_ERROR] {exc.reason_code}: {exc.message}") from exc
    except ValueError as exc:
        raise SystemExit(
            "[RELEASE_DELTA_ERROR] output manifest must be stored inside the repository",
        ) from exc
    existing = None
    if output_path.exists():
        if not args.refresh:
            raise SystemExit(
                f"[RELEASE_DELTA_ERROR] manifest already exists: {output_path}; pass --refresh to update it",
            )
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"[RELEASE_DELTA_ERROR] cannot refresh malformed manifest: {exc}") from exc

    try:
        manifest = build_release_delta_manifest(
            base_dir=BASE_DIR,
            target_version=version,
            existing=existing,
        )
    except ReleaseDeltaCoverageError as exc:
        raise SystemExit(f"[RELEASE_DELTA_ERROR] {exc.reason_code}: {exc.message}") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assigned = {
        sha
        for note in manifest["release_notes"]
        for sha in note["commits"]
    } | {item["commit"] for item in manifest["no_release_note"]}
    total = len(manifest["commits"])
    print(f"[OK] wrote {output_path}")
    print(
        f"[RELEASE_DELTA] commits={total} reviewed={len(assigned)} "
        f"unreviewed={total - len(assigned)} notes={len(manifest['release_notes'])}",
    )
    print(
        "[NEXT] map every release note to commit SHA(s), and give every remaining commit "
        "a no_release_note reason",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
