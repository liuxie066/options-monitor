#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class Candidate:
    path: Path
    kind: str
    size_bytes: int


def parse_args() -> argparse.Namespace:
    default_repo_root = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(
        description="Cleanup old runtime artifacts under output_runs (and optionally output_accounts/*/raw)."
    )
    parser.add_argument(
        "--repo-root",
        default=str(default_repo_root),
        help="Repository root path (default: script-inferred repo root)",
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=7,
        help="Keep artifacts newer than N days (default: 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview deletions only (default behavior)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute deletion operations",
    )
    parser.add_argument(
        "--cleanup-account-raw",
        action="store_true",
        help="Also clean old JSON files under output_accounts/*/raw (disabled by default)",
    )
    args = parser.parse_args()

    # --apply wins. Without --apply we remain in dry-run mode.
    args.dry_run = not bool(args.apply)

    if args.keep_days < 0:
        raise SystemExit("[ERROR] --keep-days must be >= 0")

    return args


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def dir_size_bytes(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def file_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def detect_success_run_dirs(repo_root: Path, runs_root: Path) -> tuple[Path | None, Path | None]:
    # Explicit pointer maintained by runtime pipeline.
    last_run_dir_txt = (repo_root / "output_shared" / "state" / "last_run_dir.txt").resolve()
    pointer_dir: Path | None = None
    if last_run_dir_txt.exists():
        try:
            pointed = Path(last_run_dir_txt.read_text(encoding="utf-8").strip()).resolve()
            if pointed.exists() and pointed.is_dir() and is_within(pointed, runs_root):
                pointer_dir = pointed
        except Exception:
            pointer_dir = None

    success_candidates: list[tuple[datetime, Path]] = []
    for run_dir in runs_root.iterdir() if runs_root.exists() else []:
        if not run_dir.is_dir():
            continue

        state_dir = (run_dir / "state").resolve()
        if not state_dir.exists() or not state_dir.is_dir():
            continue

        success = False

        tick_metrics = state_dir / "tick_metrics.json"
        if tick_metrics.exists():
            data = read_json(tick_metrics)
            # Completed runs in this repo persist tick_metrics with sent/reason.
            if isinstance(data, dict) and ("sent" in data or "reason" in data):
                success = True

        if not success:
            last_run = state_dir / "last_run.json"
            if last_run.exists():
                data = read_json(last_run)
                status = str(data.get("status") or "").lower() if isinstance(data, dict) else ""
                if status == "ok":
                    success = True

        if success:
            success_candidates.append((file_mtime(run_dir), run_dir.resolve()))

    latest_success_dir: Path | None = None
    if success_candidates:
        success_candidates.sort(key=lambda x: x[0], reverse=True)
        latest_success_dir = success_candidates[0][1]

    return latest_success_dir, pointer_dir


def build_run_dir_candidates(
    runs_root: Path,
    cutoff: datetime,
    protected: set[Path],
    whitelist_roots: list[Path],
) -> tuple[list[Candidate], int]:
    candidates: list[Candidate] = []
    scanned = 0

    if not runs_root.exists():
        return candidates, scanned

    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir():
            continue

        scanned += 1
        run_dir = run_dir.resolve()

        if run_dir in protected:
            continue

        if file_mtime(run_dir) >= cutoff:
            continue

        if run_dir.parent != runs_root.resolve():
            continue

        if not any(is_within(run_dir, root) for root in whitelist_roots):
            continue

        candidates.append(Candidate(path=run_dir, kind="run_dir", size_bytes=dir_size_bytes(run_dir)))

    return candidates, scanned


def build_account_raw_candidates(
    accounts_root: Path,
    cutoff: datetime,
    whitelist_roots: list[Path],
) -> tuple[list[Candidate], int]:
    candidates: list[Candidate] = []
    scanned_dirs = 0

    if not accounts_root.exists() or not accounts_root.is_dir():
        return candidates, scanned_dirs

    for raw_dir in accounts_root.glob("*/raw"):
        if not raw_dir.is_dir():
            continue
        scanned_dirs += 1

        for item in raw_dir.iterdir():
            if not item.is_file():
                continue
            if item.suffix.lower() != ".json":
                continue
            if file_mtime(item) >= cutoff:
                continue
            if not any(is_within(item, root) for root in whitelist_roots):
                continue

            try:
                size = item.stat().st_size
            except OSError:
                size = 0
            candidates.append(Candidate(path=item.resolve(), kind="account_raw_file", size_bytes=size))

    return candidates, scanned_dirs


def bytes_human(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.2f}{u}" if u != "B" else f"{int(size)}B"
        size /= 1024.0
    return f"{n}B"


def delete_candidates(candidates: list[Candidate], dry_run: bool) -> tuple[int, int]:
    deleted_dirs = 0
    deleted_files = 0

    for c in candidates:
        if dry_run:
            continue

        if c.kind == "run_dir":
            shutil.rmtree(c.path)
            deleted_dirs += 1
        elif c.kind == "account_raw_file":
            c.path.unlink()
            deleted_files += 1

    return deleted_dirs, deleted_files


def main() -> int:
    args = parse_args()

    repo_root = Path(args.repo_root).resolve()
    runs_root = (repo_root / "output_runs").resolve()
    accounts_root = (repo_root / "output_accounts").resolve()

    if not repo_root.exists() or not repo_root.is_dir():
        raise SystemExit(f"[ERROR] repo root does not exist: {repo_root}")

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(args.keep_days))

    whitelist_roots = [runs_root]
    if args.cleanup_account_raw:
        whitelist_roots.append(accounts_root)

    latest_success_run_dir, pointer_run_dir = detect_success_run_dirs(repo_root, runs_root)
    protected: set[Path] = set()
    if latest_success_run_dir is not None:
        protected.add(latest_success_run_dir.resolve())
    if pointer_run_dir is not None:
        protected.add(pointer_run_dir.resolve())

    displayed_latest_success = latest_success_run_dir
    if pointer_run_dir is not None:
        if displayed_latest_success is None:
            displayed_latest_success = pointer_run_dir
        else:
            if file_mtime(pointer_run_dir) > file_mtime(displayed_latest_success):
                displayed_latest_success = pointer_run_dir

    run_candidates, scanned_run_dirs = build_run_dir_candidates(
        runs_root=runs_root,
        cutoff=cutoff,
        protected=protected,
        whitelist_roots=whitelist_roots,
    )

    raw_candidates: list[Candidate] = []
    scanned_raw_dirs = 0
    if args.cleanup_account_raw:
        raw_candidates, scanned_raw_dirs = build_account_raw_candidates(
            accounts_root=accounts_root,
            cutoff=cutoff,
            whitelist_roots=whitelist_roots,
        )

    all_candidates = run_candidates + raw_candidates
    estimated_bytes = sum(c.size_bytes for c in all_candidates)

    deleted_dirs, deleted_files = delete_candidates(all_candidates, dry_run=args.dry_run)

    mode = "dry-run" if args.dry_run else "apply"
    print(f"[MODE] {mode}")
    print(f"[REPO] {repo_root}")
    print(f"[CUTOFF_UTC] {cutoff.isoformat()}")
    print(f"[KEEP_DAYS] {args.keep_days}")
    print(f"[SAFE] latest_success_run_dir={displayed_latest_success}")
    print(f"[SCAN] run_dirs={scanned_run_dirs} account_raw_dirs={scanned_raw_dirs}")
    print(f"[PLAN] delete_run_dirs={len(run_candidates)} delete_account_raw_files={len(raw_candidates)}")
    print(f"[ESTIMATE] reclaim={estimated_bytes} ({bytes_human(estimated_bytes)})")

    for c in all_candidates:
        print(f"[CANDIDATE] {c.kind} size={c.size_bytes} path={c.path}")

    print(f"[RESULT] deleted_run_dirs={deleted_dirs} deleted_account_raw_files={deleted_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
