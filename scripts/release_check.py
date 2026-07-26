#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.application.release_notes import parse_version_categories, render_release_notes
from src.application.release_target import VERSION_RE


def repo_base() -> Path:
    return BASE_DIR


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def current_version(base: Path) -> str:
    version = read_text((base / "VERSION").resolve()).strip()
    if not VERSION_RE.match(version):
        raise SystemExit(f"[RELEASE_ERROR] invalid VERSION format: {version}")
    return version


def changelog_section(changelog_text: str, version: str) -> str:
    parsed = parse_version_categories(changelog_text, version, allow_legacy=True)
    if parsed["status"] != "ok":
        return ""
    return "\n".join([parsed["section_heading"], parsed["canonical_text"]]).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="validate release metadata and optionally render release notes")
    parser.add_argument("--tag", default=None, help="optional git tag such as v0.1.0-beta.1")
    parser.add_argument("--render-notes-out", default=None, help="optional output markdown path for release notes")
    parser.add_argument(
        "--require-current-taxonomy",
        action="store_true",
        help="reject legacy Added/Changed/Fixed headings in the target release section",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base = repo_base()
    version = current_version(base)
    tag = str(args.tag or "").strip()
    if tag:
        tag_version = tag[1:] if tag.startswith("v") else tag
        if tag_version != version:
            raise SystemExit(f"[RELEASE_ERROR] tag {tag} does not match VERSION {version}")

    changelog_path = (base / "CHANGELOG.md").resolve()
    parsed = parse_version_categories(
        read_text(changelog_path),
        version,
        allow_legacy=not args.require_current_taxonomy,
    )
    if parsed["status"] != "ok":
        raise SystemExit(f"[RELEASE_ERROR] {parsed['reason_code']}: {parsed['message']}")

    if args.render_notes_out:
        out_path = Path(args.render_notes_out).expanduser().resolve()
        out_path.write_text(render_release_notes(version=version, evidence=parsed["evidence"]), encoding="utf-8")

    print(f"[OK] release metadata valid for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
