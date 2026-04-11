#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".rst",
    ".py",
    ".sh",
    ".yml",
    ".yaml",
    ".mk",
}
TEXT_FILENAMES = {"Makefile"}

RUNTIME_TERMS = (
    "运行入口",
    "入口配置",
    "主运行入口",
    "runtime entry",
    "run entry",
    "entry config",
)
NEGATION_TERMS = (
    "非运行入口",
    "不是",
    "不要将",
    "not",
    "historical",
    "history",
    "deprecated",
    "示例",
    "example",
)
FORBIDDEN_CONFIG_MARKERS = (
    "config.json",
    "config.scheduled",
    "config.market_us",
    "config.market_hk",
)

EXPLICIT_TERMS = (
    "显式",
    "explicit",
    "如需",
    "allowlist",
)


class Violation:
    def __init__(self, path: Path, line_no: int, reason: str, line: str) -> None:
        self.path = path
        self.line_no = line_no
        self.reason = reason
        self.line = line

    def render(self) -> str:
        return f"{self.path}:{self.line_no}: {self.reason}\n  {self.line.strip()}"


def tracked_files() -> list[Path]:
    try:
        out = subprocess.check_output(["git", "ls-files"], cwd=str(ROOT), text=True)
    except Exception as exc:
        raise SystemExit(f"[guardrails] failed to list files: {exc}")

    files: list[Path] = []
    for rel in out.splitlines():
        if not rel:
            continue
        p = ROOT / rel
        if not p.is_file():
            continue
        if p.name in TEXT_FILENAMES or p.suffix in TEXT_SUFFIXES:
            files.append(p)
    return files


def is_doc_file(path: Path) -> bool:
    return path.suffix in {".md", ".txt", ".rst"}


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def check_runtime_entry_wording(files: list[Path]) -> list[Violation]:
    issues: list[Violation] = []
    for path in files:
        if not is_doc_file(path):
            continue
        for idx, line in enumerate(read_lines(path), start=1):
            lowered = line.lower()
            if not any(marker in lowered for marker in FORBIDDEN_CONFIG_MARKERS):
                continue
            if not any(term in lowered for term in RUNTIME_TERMS):
                continue
            if any(term in lowered for term in NEGATION_TERMS):
                continue
            issues.append(
                Violation(
                    path.relative_to(ROOT),
                    idx,
                    "forbidden runtime-entry wording for config.json/config.scheduled/config.market_*",
                    line,
                )
            )
    return issues


def check_deploy_include_runtime_default(files: list[Path]) -> list[Violation]:
    issues: list[Violation] = []
    for path in files:
        lines = read_lines(path)
        for idx, line in enumerate(lines, start=1):
            lowered = line.lower()
            if "deploy_to_prod.py" not in lowered or "--include-runtime-config" not in lowered:
                continue
            if "--runtime-config-allowlist" in lowered:
                continue

            context = " ".join(lines[max(0, idx - 3) : min(len(lines), idx + 2)]).lower()
            if any(term in context for term in EXPLICIT_TERMS):
                continue

            issues.append(
                Violation(
                    path.relative_to(ROOT),
                    idx,
                    "deploy_to_prod.py --include-runtime-config cannot be used as default path",
                    line,
                )
            )
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description="Guardrails checks for docs and deploy examples")
    parser.add_argument("--check-doc-wording", action="store_true", help="check docs wording for runtime entry")
    parser.add_argument("--check-deploy-args", action="store_true", help="check deploy examples/scripts")
    args = parser.parse_args()

    run_doc = args.check_doc_wording
    run_deploy = args.check_deploy_args
    if not run_doc and not run_deploy:
        run_doc = True
        run_deploy = True

    files = tracked_files()
    issues: list[Violation] = []

    if run_doc:
        issues.extend(check_runtime_entry_wording(files))
    if run_deploy:
        issues.extend(check_deploy_include_runtime_default(files))

    if issues:
        print(f"[guardrails] FAILED ({len(issues)} issue(s))")
        for item in issues:
            print(item.render())
        sys.exit(1)

    print("[guardrails] OK")


if __name__ == "__main__":
    main()
