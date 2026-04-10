#!/usr/bin/env python3
"""Policy gate for protected repositories and actions."""

from __future__ import annotations

import argparse
from pathlib import Path

PROD_REPO_NAME = "options-monitor-prod"
BLOCKED_ACTIONS = {"code-change", "build"}
ALLOWED_ACTIONS = {"code-change", "build", "deploy", "running"}


def _is_prod_repo(repo_path: Path) -> bool:
    resolved = repo_path.resolve()
    return resolved.name == PROD_REPO_NAME or str(resolved).endswith(f"/{PROD_REPO_NAME}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Policy check gate")
    parser.add_argument("--repo-path", required=True, help="Repository path being operated on")
    parser.add_argument("--action", required=True, help="Action type")
    args = parser.parse_args()

    action = args.action.strip()
    repo_path = Path(args.repo_path).expanduser()
    resolved_repo = repo_path.resolve()

    if action not in ALLOWED_ACTIONS:
        print(f"[DENY] 未知 action: {action}. 允许值: {sorted(ALLOWED_ACTIONS)}")
        return 2

    if _is_prod_repo(repo_path) and action in BLOCKED_ACTIONS:
        print(
            "[DENY] policy: 禁止在 options-monitor-prod 直接改代码或构建。"
            f" repo={resolved_repo} action={action}"
        )
        return 2

    print(f"[ALLOW] policy: repo={resolved_repo} action={action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
