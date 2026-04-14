#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

try:
    from scripts.deploy_observability import ROOT_DEV, classify_lag, load_state
except ModuleNotFoundError:
    from deploy_observability import ROOT_DEV, classify_lag, load_state

ROOT_PROD = Path("/home/node/.openclaw/workspace/options-monitor-prod")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def _head_short(repo: Path) -> str:
    res = _git(repo, "rev-parse", "--short", "HEAD")
    if res.returncode != 0:
        return "unknown"
    return (res.stdout or "").strip() or "unknown"


def _is_ancestor(repo: Path, old_ref: str, new_ref: str) -> bool:
    if not old_ref or not new_ref or "unknown" in {old_ref, new_ref}:
        return False
    res = _git(repo, "merge-base", "--is-ancestor", old_ref, new_ref)
    return res.returncode == 0


def _count_range(repo: Path, rev_range: str) -> int:
    res = _git(repo, "rev-list", "--count", rev_range)
    if res.returncode != 0:
        return 0
    txt = (res.stdout or "").strip()
    try:
        return int(txt)
    except Exception:
        return 0


def build_status() -> dict[str, Any]:
    dev_head = _head_short(ROOT_DEV)
    prod_head = _head_short(ROOT_PROD)
    prod_is_ancestor = _is_ancestor(ROOT_DEV, prod_head, dev_head)
    dev_is_ancestor = _is_ancestor(ROOT_DEV, dev_head, prod_head)
    ahead_by = _count_range(ROOT_DEV, f"{prod_head}..{dev_head}")
    behind_by = _count_range(ROOT_DEV, f"{dev_head}..{prod_head}")
    lag = classify_lag(
        dev_head=dev_head,
        prod_head=prod_head,
        prod_is_ancestor_of_dev=prod_is_ancestor,
        dev_is_ancestor_of_prod=dev_is_ancestor,
        ahead_by=ahead_by,
        behind_by=behind_by,
    )
    state = load_state()
    last_event = state.get("last_event") if isinstance(state.get("last_event"), dict) else None
    last_failure = state.get("last_failure") if isinstance(state.get("last_failure"), dict) else None
    out = {
        "lag": lag,
        "last_sync": last_event,
        "last_failure": last_failure,
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-only", action="store_true", help="Print status JSON only")
    args = ap.parse_args()

    result = build_status()
    if not args.json_only:
        lag = result.get("lag") if isinstance(result.get("lag"), dict) else {}
        last_sync = result.get("last_sync") if isinstance(result.get("last_sync"), dict) else {}
        last_failure = result.get("last_failure") if isinstance(result.get("last_failure"), dict) else {}
        print(
            "[status] lag "
            f"is_lagging={lag.get('is_lagging')} "
            f"is_diverged={lag.get('is_diverged')} "
            f"ahead_by={lag.get('ahead_by')} "
            f"dev={lag.get('dev_head')} "
            f"prod={lag.get('prod_head')}"
        )
        if last_sync:
            print(
                "[status] last_sync "
                f"ts={last_sync.get('timestamp_utc')} "
                f"op={last_sync.get('operation')} "
                f"status={last_sync.get('status')} "
                f"merged={last_sync.get('merged_to_target')}"
            )
        else:
            print("[status] last_sync unavailable")
        if last_failure:
            print(
                "[status] recent_failure "
                f"ts={last_failure.get('timestamp_utc')} "
                f"reason={last_failure.get('failure_reason')}"
            )
        else:
            print("[status] recent_failure none")
    print(f"[status][json] {json.dumps(result, ensure_ascii=False, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
