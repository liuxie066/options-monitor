from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from om.storage import paths
from om.storage.repositories import run_repo
from scripts.io_utils import atomic_write_json as write_json
from scripts.io_utils import read_json


def shared_state_dir(base: Path) -> Path:
    p = paths.shared_state_dir(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_state_dir(base: Path, run_id: str) -> Path:
    return run_repo.ensure_run_state_dir(base, run_id)


def account_state_dir(base: Path, account: str) -> Path:
    p = paths.account_state_dir(base, account)
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_account_state_dir(base: Path, run_id: str, account: str) -> Path:
    return run_repo.ensure_run_account_state_dir(base, run_id, account)


def write_scheduler_decision(base: Path, run_id: str, payload: dict[str, Any]) -> Path:
    out = run_state_dir(base, run_id) / "scheduler_decision.json"
    write_json(out, payload)
    return out


def write_tick_metrics(base: Path, run_id: str, payload: dict[str, Any]) -> dict[str, Path]:
    sdir = shared_state_dir(base)
    rdir = run_state_dir(base, run_id)
    p_shared = (sdir / "tick_metrics.json").resolve()
    p_run = (rdir / "tick_metrics.json").resolve()
    write_json(p_shared, payload)
    write_json(p_run, payload)
    return {"shared": p_shared, "run": p_run}


def append_tick_metrics_history(base: Path, run_id: str, payload: dict[str, Any]) -> dict[str, Path]:
    sdir = shared_state_dir(base)
    rdir = run_state_dir(base, run_id)
    p_shared = (sdir / "tick_metrics_history.json").resolve()
    p_run = (rdir / "tick_metrics_history.json").resolve()

    def _append(path: Path) -> None:
        cur = read_json(path, [])
        if not isinstance(cur, list):
            cur = []
        cur.append(payload)
        write_json(path, cur)

    _append(p_shared)
    _append(p_run)
    return {"shared": p_shared, "run": p_run}


def write_shared_last_run(base: Path, payload: dict[str, Any]) -> Path:
    out = (shared_state_dir(base) / "last_run.json").resolve()
    write_json(out, payload)
    return out


def write_shared_state(base: Path, name: str, payload: dict[str, Any]) -> Path:
    out = (shared_state_dir(base) / str(name)).resolve()
    write_json(out, payload)
    return out


def write_account_last_run(base: Path, account: str, payload: dict[str, Any]) -> Path:
    out = (account_state_dir(base, account) / "last_run.json").resolve()
    write_json(out, payload)
    return out


def write_account_state_json_text(base: Path, account: str, name: str, payload: dict[str, Any]) -> Path:
    out = (account_state_dir(base, account) / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def write_run_account_last_run(base: Path, run_id: str, account: str, payload: dict[str, Any]) -> Path:
    out = (run_account_state_dir(base, run_id, account) / "last_run.json").resolve()
    write_json(out, payload)
    return out


def write_account_run_state(base: Path, run_id: str, account: str, name: str, payload: dict[str, Any]) -> Path:
    out = (run_account_state_dir(base, run_id, account) / str(name)).resolve()
    write_json(out, payload)
    return out


def write_last_run_dir_pointer(base: Path, run_id: str) -> Path:
    p = (shared_state_dir(base) / "last_run_dir.txt").resolve()
    p.write_text(str(run_repo.get_run_dir(base, run_id)) + "\n", encoding="utf-8")
    return p


def append_run_audit_jsonl(base: Path, run_id: str, name: str, payload: dict[str, Any]) -> Path:
    out = (run_state_dir(base, run_id) / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return out
