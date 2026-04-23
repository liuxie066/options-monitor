#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def repo_base() -> Path:
    return Path(__file__).resolve().parents[1]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def current_version(base: Path) -> str:
    version = read_text((base / "VERSION").resolve()).strip()
    if not VERSION_RE.match(version):
        raise SystemExit(f"[RELEASE_ERROR] invalid VERSION format: {version}")
    return version


def changelog_section(changelog_text: str, version: str) -> str:
    target = f"## {version}"
    lines = changelog_text.splitlines()
    capture = False
    out: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if capture:
                break
            if line.strip().startswith(target):
                capture = True
                out.append(line)
                continue
        elif capture:
            out.append(line)
    return "\n".join(out).strip()


def render_release_notes(*, version: str, section: str) -> str:
    body = section.splitlines()
    if body and body[0].startswith("## "):
        body = body[1:]
    cleaned = "\n".join(body).strip()
    return f"# options-monitor {version}\n\n{cleaned}\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="validate release metadata and optionally render release notes")
    parser.add_argument("--tag", default=None, help="optional git tag such as v0.1.0-beta.1")
    parser.add_argument("--render-notes-out", default=None, help="optional output markdown path for release notes")
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
    section = changelog_section(read_text(changelog_path), version)
    if not section:
        raise SystemExit(f"[RELEASE_ERROR] CHANGELOG.md missing section for {version}")

    if args.render_notes_out:
        out_path = Path(args.render_notes_out).expanduser().resolve()
        out_path.write_text(render_release_notes(version=version, section=section), encoding="utf-8")

    print(f"[OK] release metadata valid for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
