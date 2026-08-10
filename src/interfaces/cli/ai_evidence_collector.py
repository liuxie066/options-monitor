from __future__ import annotations

import argparse
import json
import sys

from src.application.agent_tool_config import repo_base
from src.application.ai_decision_advice.managed_collector import (
    run_managed_collector,
)
from src.application.runtime_paths import resolve_runtime_root


def main(argv: list[str] | None = None) -> int:
    """Narrow argv/environment adapter for the managed systemd service."""

    parser = argparse.ArgumentParser(
        prog="options-monitor-ai-evidence-collector",
        description="managed AI Decision Advice evidence collector",
    )
    parser.add_argument(
        "--config-key",
        action="append",
        dest="config_keys",
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    runtime_root = resolve_runtime_root(repo_root=repo_base()).runtime_root
    try:
        result = run_managed_collector(
            config_keys=args.config_keys or ["us", "hk"],
            runtime_root=runtime_root,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "collector_error",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"completed", "partial", "skipped"} else 1


if __name__ == "__main__":
    sys.exit(main())
