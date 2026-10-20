"""Native-service clock gate; business effects stay in the ordinary auto-close CLI."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from domain.storage.json_io import atomic_write_private_json
from src.application.account_config import normalize_account_label
from src.application.write_contract import write_control

MARKET_CLOCKS = {"us": ("America/New_York", 7), "hk": ("Asia/Hong_Kong", 5)}
MAX_ATTEMPTS = 3
ATTEMPT_SECONDS = 600
RETRY_SECONDS = 300
RETRY = 75
TERMINAL = 78


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def latest_occurrence(market: str, now: datetime) -> date:
    zone, minute = MARKET_CLOCKS[market]
    local = now.astimezone(ZoneInfo(zone))
    today = local.date()
    return today if local >= datetime.combine(today, time(0, minute), ZoneInfo(zone)) else today - timedelta(days=1)


def schedule_scope(market: str, config: Path, runtime: Path, accounts: list[str]) -> dict[str, Any]:
    labels = sorted({normalize_account_label(value) for value in accounts})
    if market not in MARKET_CLOCKS or not labels or not all(labels):
        raise ValueError("invalid schedule scope")
    return {"version": 1, "market": market, "config": str(config.resolve()),
            "runtime": str(runtime.resolve()), "accounts": labels}


def schedule_paths(scope: dict[str, Any]) -> tuple[Path, Path]:
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    root = Path(scope["runtime"])
    return (root / "output_shared/state/auto_close_schedule" / f"{digest}.json",
            root / "locks" / f"auto-close-schedule-{digest}.lock")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid checkpoint timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("checkpoint timestamp must be UTC")
    return result


def _read_checkpoint(path: Path, scope: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("checkpoint must be a regular file")
        state = json.load(handle)
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or state.get("scope") != scope:
        raise ValueError("checkpoint identity mismatch")
    occurrence = date.fromisoformat(state["occurrence_date"])
    local_today = now.astimezone(ZoneInfo(MARKET_CLOCKS[scope["market"]][0])).date()
    if occurrence > local_today:
        raise ValueError("checkpoint occurrence is in the future")
    attempts, status = state["attempts"], state["status"]
    if type(attempts) is not int or not 1 <= attempts <= MAX_ATTEMPTS:
        raise ValueError("invalid attempt count")
    started = _timestamp(state["started_at_utc"])
    finished = state["finished_at_utc"]
    retry_at = state["retry_not_before_utc"]
    code = state["child_exit_code"]
    if code is not None and type(code) is not int:
        raise ValueError("invalid child exit code")
    if status == "running":
        if finished is not None or code is not None or _timestamp(retry_at) != started + timedelta(seconds=ATTEMPT_SECONDS + RETRY_SECONDS):
            raise ValueError("invalid running checkpoint")
    elif status in {"failed", "succeeded", "exhausted"}:
        finish = _timestamp(finished)
        if status == "succeeded":
            if code != 0 or retry_at is not None:
                raise ValueError("invalid success checkpoint")
        elif code == 0 or code is None or _timestamp(retry_at) != finish + timedelta(seconds=RETRY_SECONDS):
            raise ValueError("invalid failure checkpoint")
        if status == "failed" and attempts >= MAX_ATTEMPTS or status == "exhausted" and attempts != MAX_ATTEMPTS:
            raise ValueError("invalid exhausted checkpoint")
    else:
        raise ValueError("unknown checkpoint status")
    return state


def _save_checkpoint(path: Path, state: dict[str, Any]) -> None:
    # Retry a failed directory sync too: existence does not prove durability.
    runtime = Path(state["scope"]["runtime"])
    path.relative_to(runtime)
    sync_directories = [path.parent]
    ancestor = path.parent
    while ancestor != runtime.parent:
        ancestor = ancestor.parent
        sync_directories.append(ancestor)
    atomic_write_private_json(path, state)
    for directory in sync_directories:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _run_child(argv: list[str]) -> int:
    child: subprocess.Popen | None = None
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def cancel(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(signum)

    try:
        for sig in previous:
            signal.signal(sig, cancel)
        # Inherit the native job's group so launchd can clean up descendants.
        child = subprocess.Popen(argv)
        try:
            return child.wait(timeout=ATTEMPT_SECONDS)
        except subprocess.TimeoutExpired:
            return 124
        except KeyboardInterrupt:
            return 130
    except OSError:
        return 127
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if child is not None:
                if child.poll() is None:
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            child.kill()
                        except ProcessLookupError:
                            pass
                child.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def run_scheduled(
    *, scope: dict[str, Any], repo: Path,
    now_fn: Callable[[], datetime] = utc_now,
    run_child: Callable[[list[str]], int] = _run_child,
) -> int:
    path, lock_path = schedule_paths(scope)
    if not Path(scope["runtime"]).parent.is_dir():
        raise ValueError("runtime parent must already exist")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "a") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("invalid schedule lock")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = now_fn()
        occurrence = latest_occurrence(scope["market"], now)
        state = _read_checkpoint(path, scope, now)
        if state is not None:
            stored = date.fromisoformat(state["occurrence_date"])
            if stored > occurrence:
                return 0
            if stored < occurrence:
                state = None
            elif state["status"] == "succeeded":
                return 0
            elif state["attempts"] >= MAX_ATTEMPTS:
                print("auto-close schedule: occurrence exhausted", file=sys.stderr)
                return TERMINAL
            elif now < _timestamp(state["retry_not_before_utc"]):
                return RETRY

        # Read only the explicit config after the clock gate; normal CLI owns validation.
        config = json.loads(Path(scope["config"]).read_text(encoding="utf-8"))
        generated = config.get("_generated") if isinstance(config, dict) else None
        if not isinstance(generated, dict) or str(generated.get("market", "")).lower() != scope["market"]:
            raise ValueError("schedule/config market mismatch")
        attempt = int(state["attempts"]) + 1 if state else 1
        state = {"schema_version": 1, "scope": scope, "occurrence_date": occurrence.isoformat(),
                 "attempts": attempt, "status": "running", "started_at_utc": now.isoformat(),
                 "finished_at_utc": None, "child_exit_code": None,
                 "retry_not_before_utc": (now + timedelta(seconds=ATTEMPT_SECONDS + RETRY_SECONDS)).isoformat()}
        _save_checkpoint(path, state)
        argv = [str(repo / "om"), "option-positions", "auto-close-expired", "--config", scope["config"],
                "--accounts", *scope["accounts"], "--runtime-root", scope["runtime"], "--apply", "--yes", "--quiet"]
        code = run_child(argv)
        finished = now_fn()
        state.update(status="succeeded" if code == 0 else "exhausted" if attempt == MAX_ATTEMPTS else "failed",
                     child_exit_code=code, finished_at_utc=finished.isoformat(),
                     retry_not_before_utc=None if code == 0 else (finished + timedelta(seconds=RETRY_SECONDS)).isoformat())
        _save_checkpoint(path, state)
        return 0 if code == 0 else TERMINAL if attempt == MAX_ATTEMPTS else RETRY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native-service daily expiry maintenance clock")
    parser.add_argument("--market", choices=tuple(MARKET_CLOCKS), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--accounts", nargs="+", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    control = write_control(apply=args.apply, yes=args.yes, high_risk=True)
    if not args.apply or not control["explicitly_confirmed"]:
        parser.error("scheduled maintenance requires --apply --yes")
    try:
        scope = schedule_scope(args.market, args.config, args.runtime_root, args.accounts)
        return run_scheduled(scope=scope, repo=Path(__file__).resolve().parents[3])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Config and checkpoint contents can be sensitive; preserve only the failure class.
        print(f"auto-close schedule: stopped ({type(exc).__name__}); inspect local checkpoint/config", file=sys.stderr)
        return TERMINAL


if __name__ == "__main__":
    raise SystemExit(main())
