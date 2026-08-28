#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".csv",
    ".env",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".plist",
    ".service",
    ".txt",
    ".rst",
    ".py",
    ".sh",
    ".toml",
    ".tsv",
    ".xml",
    ".yml",
    ".yaml",
    ".mk",
}
TEXT_FILENAMES = {"Makefile"}
LineReader = Callable[[Path], list[str]]
PathExists = Callable[[Path], bool]

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
    "禁止",
    "forbid",
    "forbidden",
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

LIVING_DOC_HISTORY_HEADING = "## 迁移与历史兼容"
REPO_PATH_PREFIXES = (
    "src/",
    "domain/",
    "scripts/",
    "tests/",
    ".github/",
    "configs/",
    "agent-runtime/",
)
_MARKDOWN_LINK_RE = re.compile(r"\[[^]]+\]\(([^)]+)\)")
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_PATH_LIFECYCLE_PREFIX_RE = re.compile(
    r"(?:\b(?:historical|removed|deleted|retired|deprecated|proposed|planned|example)\b"
    r"|历史(?:路径|文件)?|已删除|已移除|已退役|已弃用|拟新增|计划新增|待新增|示例)"
    r"\s*[:：]?\s*$",
    re.IGNORECASE,
)
_NONDETERMINISTIC_PATH_CHARS = frozenset("*?[]{}<>")

ROOT_RUNTIME_CONFIG_EXACT = {
    "config.assistant.json",
    "config.json",
    "config.us.json",
    "config.hk.json",
    "config.scheduled.json",
}

# SHA-256 fingerprints avoid re-publishing the private values this check blocks.
# The token digest corresponds to a known broker identifier that previously
# entered tracked files. Full-line fingerprints cover production snapshots
# without blocking ordinary test numbers that happen to share one value.
KNOWN_PRIVATE_TOKEN_SHA256 = {
    "01a806970fce6590f0af7cd15d336a96810218f0147e70ba665e32fefb9feb71",
}

# Lower-cased SHA-256 fingerprints of personal commit email addresses that
# entered this public repository. The original addresses must not be copied
# into tracked policy or test fixtures.
KNOWN_PRIVATE_EMAIL_SHA256 = {
    "25656499f9f41c50ab3f80ba30aa23d635f6a7c251804491afd99c32d519a65a",
    "976e5655efa1fd82592a512b235a385027e006ea07ce050bcd71d45fa4d9c70c",
}

KNOWN_PRIVATE_LINE_SHA256 = {
    "9b19f299f62267c705ea86cc4eb849e26f6a02b1578f20a9ac74ffce8d30b6f2",
    "2a7a08a8b323faea1cb3144509ed99bff332964c1c40a4d4cbb0f9f9635afd14",
    "c07135572e8789d3f7174f85e5542487cac1cd700e6bd64e80883bd80b1f8898",
    "2d3aba7adc7213770b12004eece3132390413aaf6dc7ceeb8d29cc33ce41662b",
    "01d2f0535d856c424a4214e9aff069ce3440cfe907cc68d1618a85fe62b6a267",
    "ce2feed25ba7244bd6dd00efdfb1fc172afd7aa430325e308149e2d3a389d7c2",
    "90fc60d9f64e135b14d41b36ff3fd988fc5b16fbb712c24fd35fc47a5fa3f682",
    "e8379c40054669b7dc387ec19a2594d1479c0ecda15287cb57c752acc87c4d3f",
    "d3f52111abd0cc4d9e656bac3fc96c3f65eaa090207707b18e0c59ba00f68c32",
    "3f43289b9200c28e69a3cf2dad3e3ab0ec9c9578dd9f7f93ae4ffdd9f05be197",
    "f4b84e64b1111efa4db644ee5454a56918e73ce6b1a4c71b00c5524be5cd4d5c",
    "1535deeb01fb464b700f709770d9f8138ae93a7ecf0d2a86430dffa2f292f308",
    "a36901de3b1d2cadda16f450ca5d1d3359744b5e8d11ac43e775cf06f37c2f60",
    "f49c4053151818de855302ce3248b4f7e53544bb72c011d2f28c8fb5c0fba721",
    "c810e7d8c82dc94208eb82c1368d3c596c42bb35c91e459e2920c59cd0a4dc42",
    "a6f94e8d099ddf20e07991116cdfa102f420196c7b25f1162062e5dbf0a81c65",
    "1c66818855aba589b0b6425ede42ebdcc9e514e830b6c06950a266986d9b3868",
    "331cb588fa20e449578865ae09cc3841bf08dedafc8023f817e52a305e302a67",
}

# Exact-line hashes for deliberate invalid security fixtures. Keep this narrow:
# paths are not allowlisted, so moving a real credential into a test still fails.
KNOWN_SAFE_SECRET_FIXTURE_LINE_SHA256 = {
    "365c29c8194eaf2f2faaae0a39458ad91dcedf424588e64a62925db45cd094cf",
    "975e4c7110a231ade2bea4e298bf77ced793b902cef07d72a89065e62f80969c",
}

_SENSITIVE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9])")
_EMAIL_RE = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9.-])"
)
_HIGH_CONFIDENCE_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])"),
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}(?![A-Za-z0-9-])"),
    re.compile(r"(?<![A-Za-z0-9])sk_live_[A-Za-z0-9]{20,}(?![A-Za-z0-9])"),
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@"),
    re.compile(
        r"(?i)(?:facebook|meta)[A-Za-z0-9_.-]{0,40}"
        r"(?:app[_-]?secret|secret[_-]?id)\s*[\"']?\s*[:=]\s*[\"']?[0-9a-f]{32}"
    ),
)
_PATH_COMPONENT = r"([A-Za-z0-9._\-\u3400-\u9fff]+)"
_ABSOLUTE_HOME_RE = re.compile(rf"/(?:Users|home)/{_PATH_COMPONENT}/")
_ABSOLUTE_VOLUME_RE = re.compile(rf"/Volumes/{_PATH_COMPONENT}/")
_GENERIC_PATH_COMPONENTS = frozenset(
    {"alice", "bob", "me", "om", "user", "users", "test", "example", "runner", "workspace"}
)
ROOT_RUNTIME_CONFIG_PATTERNS = (
    "config.market_*.json",
    "config.market_*.json.deprecated",
    "config.local*.json",
    "config.*.bak.*",
)


class Violation:
    def __init__(self, path: Path, line_no: int, reason: str, line: str) -> None:
        self.path = path
        self.line_no = line_no
        self.reason = reason
        self.line = line

    def render(self) -> str:
        return f"{self.path}:{self.line_no}: {self.reason}\n  {self.line.strip()}"


def git_index_paths() -> list[Path]:
    try:
        out = subprocess.check_output(["git", "ls-files", "-z"], cwd=str(ROOT))
    except Exception as exc:
        raise SystemExit(f"[guardrails] failed to list files: {exc}")

    return [
        Path(raw.decode("utf-8", errors="surrogateescape"))
        for raw in out.split(b"\0")
        if raw
    ]


def tracked_file_paths() -> list[Path]:
    paths = git_index_paths()

    files: list[Path] = []
    for rel in paths:
        p = ROOT / rel
        if not p.is_file():
            continue
        files.append(rel)
    return files


def staged_file_paths() -> list[Path]:
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
            cwd=str(ROOT),
        )
    except Exception as exc:
        raise SystemExit(f"[guardrails] failed to list staged files: {exc}") from exc
    return [
        Path(raw.decode("utf-8", errors="surrogateescape"))
        for raw in out.split(b"\0")
        if raw
    ]


def text_tracked_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for rel in paths:
        p = ROOT / rel
        if p.name in TEXT_FILENAMES or p.suffix in TEXT_SUFFIXES:
            files.append(p)
    return files


def text_staged_files(paths: list[Path]) -> list[Path]:
    return [
        ROOT / rel
        for rel in paths
        if rel.name in TEXT_FILENAMES or rel.suffix in TEXT_SUFFIXES
    ]


def is_root_runtime_config_path(path: Path) -> bool:
    if len(path.parts) != 1:
        return False
    name = path.name
    if name in ROOT_RUNTIME_CONFIG_EXACT:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in ROOT_RUNTIME_CONFIG_PATTERNS)


def is_doc_file(path: Path) -> bool:
    return path.suffix in {".md", ".txt", ".rst"}


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def read_staged_lines(path: Path) -> list[str]:
    rel = path.relative_to(ROOT).as_posix()
    try:
        out = subprocess.check_output(["git", "show", f":{rel}"], cwd=str(ROOT))
    except Exception as exc:
        raise SystemExit(f"[guardrails] failed to read staged file {rel}: {exc}") from exc
    return out.decode("utf-8", errors="ignore").splitlines()


def working_tree_path_exists(path: Path) -> bool:
    root = ROOT.resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.exists()


def index_path_exists(path: Path, index_paths: set[str]) -> bool:
    key = path.as_posix().rstrip("/")
    if not key or key == "." or ".." in path.parts:
        return False
    return key in index_paths or any(item.startswith(f"{key}/") for item in index_paths)


def _local_markdown_targets(
    document: Path,
    lines: list[str],
    *,
    path_exists: PathExists,
) -> tuple[list[Path], list[Violation]]:
    root = ROOT.resolve()
    targets: list[Path] = []
    issues: list[Violation] = []
    for idx, line in enumerate(lines, start=1):
        for raw_target in _MARKDOWN_LINK_RE.findall(line):
            parsed = urlsplit(raw_target.strip())
            if parsed.scheme or parsed.netloc or not parsed.path.lower().endswith(".md"):
                continue
            candidate = (document.parent / parsed.path).resolve()
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if path_exists(relative):
                targets.append(candidate)
            else:
                issues.append(
                    Violation(
                        document.relative_to(root),
                        idx,
                        "indexed living-doc target does not exist",
                        line,
                    )
                )
    return targets, issues


def living_document_paths(
    *,
    line_reader: LineReader = read_lines,
    path_exists: PathExists = working_tree_path_exists,
) -> tuple[list[Path], list[Violation]]:
    root = ROOT.resolve()
    index = root / "docs" / "INDEX.md"
    if not path_exists(Path("docs/INDEX.md")):
        return [], [
            Violation(
                Path("docs/INDEX.md"),
                1,
                "living-doc authority index does not exist",
                "<missing docs/INDEX.md>",
            )
        ]

    index_lines: list[str] = []
    for line in line_reader(index):
        if line.strip() == LIVING_DOC_HISTORY_HEADING:
            break
        index_lines.append(line)

    direct, issues = _local_markdown_targets(index, index_lines, path_exists=path_exists)
    documents = [index, *direct]
    for document in direct:
        relative = document.relative_to(root)
        if document.name != "README.md" or relative.parts[:1] != ("docs",) or len(relative.parts) < 3:
            continue
        nested, nested_issues = _local_markdown_targets(
            document,
            line_reader(document),
            path_exists=path_exists,
        )
        documents.extend(nested)
        issues.extend(nested_issues)
    return list(dict.fromkeys(documents)), issues


def _normalized_repo_path(token: str) -> Path | None:
    value = token.strip()
    if not value.startswith(REPO_PATH_PREFIXES):
        return None
    if (
        any(character.isspace() for character in value)
        or any(character in _NONDETERMINISTIC_PATH_CHARS for character in value)
        or "..." in value
    ):
        return None
    value = value.split("::", 1)[0]
    value = re.sub(r":\d+(?:-\d+)?$", "", value)
    return Path(value)


def check_living_doc_repo_paths(
    files: list[Path],
    *,
    line_reader: LineReader = read_lines,
    path_exists: PathExists = working_tree_path_exists,
) -> list[Violation]:
    issues: list[Violation] = []
    for path in files:
        for idx, line in enumerate(line_reader(path), start=1):
            for match in _INLINE_CODE_RE.finditer(line):
                relative = _normalized_repo_path(match.group(1))
                if relative is None:
                    continue
                if _PATH_LIFECYCLE_PREFIX_RE.search(line[: match.start()]):
                    continue
                if path_exists(relative):
                    continue
                issues.append(
                    Violation(
                        path.relative_to(ROOT.resolve()),
                        idx,
                        "indexed living-doc repository path does not exist",
                        line,
                    )
                )
    return issues


def check_runtime_entry_wording(
    files: list[Path],
    *,
    line_reader: LineReader = read_lines,
) -> list[Violation]:
    issues: list[Violation] = []
    for path in files:
        if not is_doc_file(path):
            continue
        for idx, line in enumerate(line_reader(path), start=1):
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


def check_runtime_config_tracking(files: list[Path]) -> list[Violation]:
    issues: list[Violation] = []
    for path in files:
        if not is_root_runtime_config_path(path):
            continue
        issues.append(
            Violation(
                path,
                1,
                "root runtime config must stay untracked; commit templates under configs/examples/ instead",
                "<tracked runtime config>",
            )
        )
    return issues


def check_sensitive_repository_artifacts(
    files: list[Path],
    *,
    line_reader: LineReader = read_lines,
) -> list[Violation]:
    """Reject known private fingerprints and literal personal host layouts."""

    issues: list[Violation] = []
    for raw_path in files:
        path = raw_path if raw_path.is_absolute() else ROOT / raw_path
        if path.name not in TEXT_FILENAMES and path.suffix not in TEXT_SUFFIXES:
            continue
        for idx, line in enumerate(line_reader(path), start=1):
            normalized_line = " ".join(line.split())
            line_sha256 = hashlib.sha256(normalized_line.encode("utf-8")).hexdigest()
            if (
                line_sha256 in KNOWN_PRIVATE_LINE_SHA256
            ):
                issues.append(
                    Violation(
                        path.relative_to(ROOT),
                        idx,
                        "known production-derived repository line must not be tracked",
                        "<redacted private fingerprint>",
                    )
                )
            if (
                line_sha256 not in KNOWN_SAFE_SECRET_FIXTURE_LINE_SHA256
                and any(pattern.search(line) for pattern in _HIGH_CONFIDENCE_CREDENTIAL_PATTERNS)
            ):
                issues.append(
                    Violation(
                        path.relative_to(ROOT),
                        idx,
                        "high-confidence credential pattern must not be tracked",
                        "<redacted credential pattern>",
                    )
                )
            for match in _SENSITIVE_TOKEN_RE.finditer(line):
                token = match.group(0)
                if hashlib.sha256(token.encode("utf-8")).hexdigest() in KNOWN_PRIVATE_TOKEN_SHA256:
                    issues.append(
                        Violation(
                            path.relative_to(ROOT),
                            idx,
                            "known private runtime or financial fingerprint must not be tracked",
                            "<redacted private fingerprint>",
                        )
                    )
                    break
            for match in _EMAIL_RE.finditer(line):
                email = match.group(0).lower()
                if hashlib.sha256(email.encode("utf-8")).hexdigest() in KNOWN_PRIVATE_EMAIL_SHA256:
                    issues.append(
                        Violation(
                            path.relative_to(ROOT),
                            idx,
                            "known personal email fingerprint must not be tracked",
                            "<redacted private email>",
                        )
                    )
                    break
            for match in _ABSOLUTE_HOME_RE.finditer(line):
                if match.group(1).lower() not in _GENERIC_PATH_COMPONENTS:
                    issues.append(
                        Violation(
                            path.relative_to(ROOT),
                            idx,
                            "literal personal home path must use a generic placeholder",
                            "<redacted personal path>",
                        )
                    )
                    break
            for match in _ABSOLUTE_VOLUME_RE.finditer(line):
                if match.group(1).lower() not in _GENERIC_PATH_COMPONENTS:
                    issues.append(
                        Violation(
                            path.relative_to(ROOT),
                            idx,
                            "literal personal volume path must use a generic placeholder",
                            "<redacted personal path>",
                        )
                    )
                    break
    return issues


def check_git_identity_privacy() -> list[Violation]:
    """Reject a repository-effective author email already classified private."""

    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            cwd=str(ROOT),
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        raise SystemExit(f"[guardrails] failed to read git author identity: {exc}") from exc
    email = result.stdout.strip().lower()
    if not email:
        return []
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    if digest not in KNOWN_PRIVATE_EMAIL_SHA256:
        return []
    return [
        Violation(
            Path(".git/config"),
            1,
            "repository-effective git author email is a known private address; use a noreply identity",
            "<redacted private email>",
        )
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Guardrails checks for docs and runtime config tracking")
    parser.add_argument("--check-doc-wording", action="store_true", help="check docs wording for runtime entry")
    parser.add_argument(
        "--check-runtime-config-tracking",
        action="store_true",
        help="check that root runtime configs are not tracked by git",
    )
    parser.add_argument(
        "--check-sensitive-artifacts",
        action="store_true",
        help="check tracked text for known private fingerprints and personal paths",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check the staged index content of changed files instead of the working tree",
    )
    args = parser.parse_args()

    run_doc = args.check_doc_wording
    run_tracking = args.check_runtime_config_tracking
    run_sensitive = args.check_sensitive_artifacts
    if not run_doc and not run_tracking and not run_sensitive:
        run_doc = True
        run_tracking = True
        run_sensitive = True

    if args.staged:
        tracked_paths = staged_file_paths()
        files = text_staged_files(tracked_paths)
        line_reader = read_staged_lines
        index_paths = {path.as_posix() for path in git_index_paths()}
        path_exists = lambda path: index_path_exists(path, index_paths)
    else:
        tracked_paths = tracked_file_paths()
        files = text_tracked_files(tracked_paths)
        line_reader = read_lines
        path_exists = working_tree_path_exists
    issues: list[Violation] = []

    if run_doc:
        issues.extend(check_runtime_entry_wording(files, line_reader=line_reader))
        living_docs, living_doc_issues = living_document_paths(
            line_reader=line_reader,
            path_exists=path_exists,
        )
        issues.extend(living_doc_issues)
        issues.extend(
            check_living_doc_repo_paths(
                living_docs,
                line_reader=line_reader,
                path_exists=path_exists,
            )
        )
    if run_tracking:
        issues.extend(check_runtime_config_tracking(tracked_paths))
    if run_sensitive:
        issues.extend(check_sensitive_repository_artifacts(files, line_reader=line_reader))
        issues.extend(check_git_identity_privacy())

    if issues:
        print(f"[guardrails] FAILED ({len(issues)} issue(s))")
        for item in issues:
            print(item.render())
        sys.exit(1)

    print("[guardrails] OK")


if __name__ == "__main__":
    main()
