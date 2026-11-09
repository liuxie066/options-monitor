#!/usr/bin/env python3
"""Independent daily/weekly entropy scans and conservative issue delivery."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time
import uuid

if __package__:
    from . import code_entropy_scan as scan
else:
    import code_entropy_scan as scan

DEFAULT_STATE = Path.home() / "Library/Application Support/options-monitor/code-entropy-review"
BUDGETS = {"daily": 900, "weekly": 5400}
LABEL = "code-entropy"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_path(value):
    path = Path(value).expanduser().resolve()
    source = Path(__file__).resolve().parents[1]
    in_source = (source / ".git").exists() and (source == path or source in path.parents)
    if path.name != "code-entropy-review" or "code-health-review" in path.parts or in_source:
        raise ValueError("use an independent directory named code-entropy-review outside source")
    return path


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


@contextmanager
def run_lock(state):
    with (state / "run.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def command(argv, cwd, log, timeout):
    if timeout <= 0:
        raise TimeoutError("scan budget exhausted")
    with log.open("w") as stdout, log.with_suffix(".stderr").open("w") as stderr:
        process = subprocess.Popen(argv, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    return {"command": argv, "exit_code": code, "output": log.name,
            "stderr": log.with_suffix(".stderr").name}


def coverage_evidence(manifest_path, sha):
    if not manifest_path:
        return None
    manifest = read_json(Path(manifest_path))
    workload = manifest["command"]
    if (manifest["main_sha"] != sha or type(manifest["exit_code"]) is not int or manifest["exit_code"] != 0
            or not isinstance(workload, list) or not workload or not all(isinstance(arg, str) and arg for arg in workload)):
        raise ValueError("coverage must bind the exact SHA and a successful workload")
    path = Path(manifest["coverage_json"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["coverage_sha256"]:
        raise ValueError("coverage digest mismatch")
    evidence = read_json(path)
    if evidence.get("meta", {}).get("branch_coverage") is not True:
        raise ValueError("coverage must include branch data")
    result = {}
    for name, entry in evidence["files"].items():
        if any(not isinstance(entry.get(field), list) for field in ("executed_lines", "missing_lines", "missing_branches")):
            raise ValueError("coverage requires measured lines and branches for every file")
        if any(type(line) is not int for field in ("executed_lines", "missing_lines") for line in entry[field]):
            raise ValueError("coverage line numbers must be integers")
        if any(not isinstance(arc, list) or len(arc) != 2 or any(type(line) is not int for line in arc)
               for arc in entry["missing_branches"]):
            raise ValueError("coverage branch arcs must be integer pairs")
        file = Path(name)
        if file.is_absolute():
            file = file.relative_to(manifest["source_root"])
        if ".." in file.parts:
            raise ValueError("invalid coverage source path")
        result[file.as_posix()] = entry
    return result


def changed_files(git, base, sha, execute):
    if execute([*git, "merge-base", "--is-ancestor", base, sha])["exit_code"]:
        raise ValueError("daily cursor is not an ancestor of main")
    result = execute([*git, "diff", "--name-only", "--no-renames", "--diff-filter=AM", "-z", base, sha])
    if result["exit_code"]:
        raise RuntimeError("failed to discover changed files")
    return [name for name in result["text"].split("\0") if name]


def eligible(row, decisions):
    decision = decisions.get(row["key"], {})
    return (row["high_confidence"] and not decision.get("muted")
            and decision.get("accepted_evidence") == row["evidence_digest"]
            and bool(decision.get("reason")))


def deliver(candidates, state, config, sha, execute):
    decisions = read_json(state / "decisions.json", {})
    ledger = read_json(state / "issues.json", {})
    selected = [row for row in candidates if eligible(row, decisions)]
    pending = [key for key, value in ledger.items() if value["status"] == "pending" and not decisions.get(key, {}).get("muted")]
    if not selected and not pending:
        return []
    repository = config["repository"]
    listing = execute(["gh", "api", f"repos/{repository}/issues?state=all&labels={LABEL}&per_page=100",
                       "--paginate", "--slurp"])
    if listing["exit_code"]:
        raise RuntimeError("cannot verify existing entropy issues")
    existing = [issue for page in json.loads(listing["text"]) for issue in page if "pull_request" not in issue]
    for key in pending:
        marker = f"<!-- code-entropy:{key} -->"
        found = next((issue for issue in existing if marker in (issue.get("body") or "")), None)
        if not found:
            raise RuntimeError("pending entropy issue is not confirmed by remote readback; no retry was sent")
        ledger[key] = {"status": "published", "url": found["html_url"]}
        write_json(state / "issues.json", ledger)
    published = []
    for row in selected:
        key = row["key"]
        marker = f"<!-- code-entropy:{key} -->"
        found = next((issue for issue in existing if marker in (issue.get("body") or "")), None)
        if found:
            ledger[key] = {"status": "published", "url": found["html_url"]}
            write_json(state / "issues.json", ledger)
        if key in ledger:
            continue
        if len(published) >= 3:
            raise RuntimeError("entropy issue allowance exhausted; unsubmitted findings remain for the next run")
        label = execute(["gh", "api", f"repos/{repository}/labels/{LABEL}"])
        if label["exit_code"]:
            raise RuntimeError("code-entropy label must already exist; no label was created")
        body = (f"{marker}\n\n{row['rule']}: `{row['path']}:{row['line']}`\n\n"
                f"Source: https://github.com/{repository}/blob/{sha}/{row['path']}#L{row['line']}\n\n"
                f"Tool: {row['tool']}; baseline {scan.VERSION}; main {sha}\n\n"
                f"```text\n{row['raw'][:6000]}\n```\n\n"
                f"Decision: {decisions[key]['reason']}\n\nEvidence: {row['evidence_digest']}\n"
                "Discovery only. No repair or PR is authorized by this issue.\n")
        body_path = state / "issue-body.md"
        body_path.write_text(body)
        ledger[key] = {"status": "pending", "at": now(), "sha": sha}
        write_json(state / "issues.json", ledger)
        result = execute(["gh", "issue", "create", "--repo", repository, "--label", LABEL,
                          "--title", f"[code-entropy] {row['rule']}: {row['path']} {row['identity'][:60]}",
                          "--body-file", str(body_path)])
        if result["exit_code"]:
            raise RuntimeError("entropy issue creation failed; pending outcome requires remote readback")
        import re
        url = result["text"].strip()
        match = re.fullmatch(rf"https://github\.com/{re.escape(repository)}/issues/(\d+)", url)
        if not match:
            raise RuntimeError("entropy issue URL is unconfirmed; pending outcome requires remote readback")
        readback = execute(["gh", "api", f"repos/{repository}/issues/{match.group(1)}"])
        if readback["exit_code"]:
            raise RuntimeError("entropy issue readback failed; pending outcome was preserved")
        issue = json.loads(readback["text"])
        if marker not in (issue.get("body") or "") or LABEL not in {item["name"] for item in issue.get("labels", [])}:
            raise RuntimeError("entropy issue readback did not confirm marker and label; pending outcome was preserved")
        ledger[key] = {"status": "published", "url": url}
        write_json(state / "issues.json", ledger)
        published.append(url)
    return published


def tool_command(name):
    import importlib.util
    import shutil
    executable = shutil.which(name)
    if executable:
        return [executable]
    if name != "jscpd" and importlib.util.find_spec(name) is not None:
        return [sys.executable, "-I", "-m", name]
    return None


def tool_runs(source, selected, execute):
    tools, findings = {}, []
    selected = [path for path in selected if (source / path).is_file() and not (source / path).is_symlink()]
    for name in ("ruff", "vulture", "coverage", "radon", "jscpd"):
        prefix = tool_command(name)
        if not prefix:
            tools[name] = {"available": False, "reason": "not installed; no automatic installation"}
            continue
        version = execute([*prefix, "--version"])
        if version["exit_code"]:
            raise RuntimeError(f"{name} version probe failed; see raw tool output")
        tools[name] = {"available": True, "version": version["text"].strip(), "command_prefix": prefix}
        if not selected or name not in {"ruff", "vulture"}:
            tools[name]["execution"] = "not run; built-in metrics or bound coverage evidence used"
            continue
        options = (["check", "--isolated", "--target-version", "py312", "--select", "F401,F841,ARG", "--output-format", "json"]
                   if name == "ruff" else ["--min-confidence", "100"])
        result = execute([*prefix, *options, *selected], cwd=source)
        tools[name]["result"] = {key: value for key, value in result.items() if key != "text"}
        if result["exit_code"] not in ({0, 1} if name == "ruff" else {0, 3}):
            raise RuntimeError(f"{name} failed; see raw tool output")
        if name == "ruff":
            for row in json.loads(result["text"]):
                path = Path(row["filename"]).relative_to(source).as_posix()
                line = row["location"]["row"]
                source_line = (source / path).read_text().splitlines()[line - 1].strip()
                findings.append(scan.candidate(f"ruff-{row['code']}", path, f"{row['code']}:{source_line}", line,
                    json.dumps(row, ensure_ascii=False), tool=f"ruff {tools[name]['version']}"))
    tools["builtin"] = {"available": True, "version": scan.VERSION,
                         "complexity": "entropy-ast-v1", "duplication": "entropy-token-v1"}
    return tools, findings


@contextmanager
def deadline(seconds):
    def expired(signum, frame):
        raise TimeoutError("entropy scan budget exhausted")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def run_scan(state, mode, *, publish=False, coverage_manifest=None, timeout_seconds=None):
    """Caller holds this scanner's lock; no other scheduler state is consulted."""
    import tempfile
    budget = BUDGETS[mode] if timeout_seconds is None else timeout_seconds
    if not 1 <= budget <= BUDGETS[mode]:
        raise ValueError("timeout must be positive and within the independent mode budget")
    config = read_json(state / "config.json")
    if not config:
        raise ValueError("initialize the independent entropy directory first")
    baseline = hashlib.sha256((state / "baseline.md").read_bytes()).hexdigest()
    cursor = read_json(state / "daily-cursor.json") if mode == "daily" else None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    output = state / "runs" / run_id
    output.mkdir(parents=True)
    started = time.monotonic()
    record = {"schema": 1, "run_id": run_id, "started_at": now(), "mode": mode, "status": "running",
              "baseline_version": 1, "baseline_digest": baseline, "algorithm": scan.VERSION,
              "budget_seconds": budget, "base_sha": cursor["sha"] if cursor else None,
              "metrics": None, "gaps": [], "python": sys.executable, "commands": []}
    source_files = [Path(__file__), Path(scan.__file__), Path(scan.__file__).with_name("guardrails_check.py")]
    record["scanner_digest"] = scan.digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files})
    record["python_version"] = sys.version.split()[0]
    write_json(output / "progress.json", record)

    def execute(argv, cwd=None):
        log = output / f"{len(record['commands']):03d}.stdout"
        remaining = budget - (time.monotonic() - started)
        result = command(argv, cwd or state, log, min(remaining, 300))
        record["commands"].append(result)
        write_json(output / "progress.json", record)
        return {**result, "text": log.read_text(errors="replace")}

    def checked(argv):
        result = execute(argv)
        if result["exit_code"]:
            raise RuntimeError(f"command failed: {argv[0]} (see {result['stderr']})")
        return result["text"].strip()

    mirror = state / "mirror.git"
    git = ["git", "-C", str(mirror)]

    def snapshot(sha, name, scratch):
        archive = Path(scratch) / f"{name}.tar"
        checked([*git, "archive", "--format=tar", sha, "--output", str(archive)])
        destination = Path(scratch) / name
        destination.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")
        return destination
    try:
        with deadline(budget), tempfile.TemporaryDirectory(prefix="source-", dir=output) as scratch:
            if not mirror.exists():
                checked(["git", "clone", "--bare", "--no-hardlinks", "--single-branch", "--branch", "main",
                         "--filter=blob:none", "--no-tags", config["remote"], str(mirror)])
            checked([*git, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/heads/main"])
            sha = checked([*git, "rev-parse", "refs/heads/main^{commit}"])
            record["main_sha"] = sha
            source = snapshot(sha, "current", scratch)
            policy = source / "docs/CODE_ENTROPY_BASELINE.md"
            if policy.is_file():
                import re
                version = re.search(r"版本[：:]\s*(\d+)", policy.read_text())
                if not version or int(version.group(1)) != 1:
                    raise ValueError("main baseline version is unsupported; update the scanner before resuming")
                baseline = hashlib.sha256(policy.read_bytes()).hexdigest()
                record["baseline_digest"] = baseline
                record["baseline_source"] = "main:docs/CODE_ENTROPY_BASELINE.md"
            else:
                record["baseline_source"] = "initialized baseline copy (target has no entropy baseline)"
            if cursor and cursor["baseline_digest"] != baseline:
                raise ValueError("baseline changed; explicitly reset the daily cursor")
            if mode == "weekly":
                selected = sorted(path.relative_to(source).as_posix() for path in source.rglob("*.py")
                                  if path.relative_to(source).parts[0] in scan.ROOTS and not path.is_symlink())
            elif cursor:
                selected = changed_files(git, cursor["sha"], sha, execute)
            else:
                selected = []
                record["initialized_daily_cursor"] = True
            selected = [path for path in selected if Path(path).suffix == ".py" and Path(path).parts[0] in scan.ROOTS]
            record["selected_files"] = selected
            record["stage"] = "static-measurement"
            write_json(output / "progress.json", record)
            coverage = coverage_evidence(coverage_manifest, sha)
            if coverage is not None:
                write_json(output / "coverage-evidence.json", coverage)
            metrics, findings = scan.scan(source, selected, coverage)
            tools, tool_findings = tool_runs(source, selected, execute)
            findings.extend(tool_findings)
            if mode == "daily" and cursor and selected:
                previous = snapshot(cursor["sha"], "previous", scratch)
                _, old_findings = scan.scan(previous, selected)
                _, old_tools = tool_runs(previous, selected, execute)
                old = {row["key"]: row for row in [*old_findings, *old_tools]}
                findings = [row for row in findings if row["key"] not in old or
                            (row["rule"] == "complexity" and row["raw"] != old[row["key"]]["raw"])]
            decisions = read_json(state / "decisions.json", {})
            findings = list({row["key"]: row for row in findings}.values())
            muted = [row for row in findings if decisions.get(row["key"], {}).get("muted")]
            findings = [row for row in findings if not decisions.get(row["key"], {}).get("muted")]
            record.update(metrics=metrics, tools=tools, candidates=len(findings), muted_candidates=len(muted))
            write_json(output / "candidates.json", findings)
            write_json(output / "muted.json", muted)
            if coverage is None:
                record["gaps"].append("No SHA-bound branch coverage: dead-code confirmation and untested branches are null.")
            if coverage is not None and metrics["dead_code_symbols"] is None:
                record["gaps"].append("Dead-code gates remain unproven (partial coverage, public API or dynamic entry): confirmation is null.")
            record["gaps"].append("Dynamic config consumers are not excluded: confirmed unused config is null.")
            record["published_issues"] = deliver(findings, state, config, sha, execute) if publish else []
            ledger = read_json(state / "issues.json", {})
            record["pending_issues"] = [key for key, value in ledger.items() if value["status"] == "pending"]
            record["status"] = "complete"
    except Exception as error:
        record["status"] = "failed"
        record["gaps"].append(f"{type(error).__name__}: {error}")
    record["finished_at"] = now()
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    write_json(output / "scorecard.json", record)
    write_json(output / "progress.json", record)
    report = ["# 代码熵评分卡", "", f"状态：{record['status']}；模式：{mode}；main：{record.get('main_sha', 'unknown')}", "",
              "```json", json.dumps(record["metrics"], ensure_ascii=False, indent=2), "```", "", "证据缺口："]
    report.extend(f"- {gap}" for gap in record["gaps"])
    report.extend(["", "候选及工具原件见同目录 candidates.json 和编号 stdout/stderr。"])
    (output / "report.md").write_text("\n".join(report) + "\n")
    with (state / "scorecards.jsonl").open("a") as history:
        history.write(json.dumps({key: value for key, value in record.items() if key != "commands"}, ensure_ascii=False) + "\n")
    if mode == "daily" and record["status"] == "complete":
        write_json(state / "daily-cursor.json", {"sha": record["main_sha"], "baseline_digest": baseline, "at": now()})
    return record


def initialize(state, repository, remote=None, baseline=None):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be OWNER/NAME")
    if (state / "config.json").exists():
        raise ValueError("entropy state already initialized; existing records were preserved")
    baseline = baseline or Path(__file__).resolve().parents[1] / "docs/CODE_ENTROPY_BASELINE.md"
    (state / "baseline.md").write_bytes(Path(baseline).read_bytes())
    write_json(state / "config.json", {"schema": 1, "repository": repository,
               "remote": remote or f"https://github.com/{repository}.git"})


def record_decision(state, action, key, reason, run_id=None):
    if not reason.strip():
        raise ValueError("a decision reason is required")
    decisions = read_json(state / "decisions.json", {})
    decision = decisions.setdefault(key, {})
    if action == "accept":
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("accept requires an existing entropy run ID")
        rows = read_json(state / "runs" / run_id / "candidates.json", [])
        row = next((row for row in rows if row["key"] == key), None)
        if not row or not row["high_confidence"]:
            raise ValueError("only a tool-confirmed high-confidence candidate can be accepted")
        decision["accepted_evidence"] = row["evidence_digest"]
    else:
        decision["muted"] = action == "mute"
        if action == "unmute":
            decision.pop("accepted_evidence", None)
    decision.update(reason=reason, at=now())
    decision.setdefault("history", []).append({"action": action, "reason": reason, "at": now()})
    write_json(state / "decisions.json", decisions)


def render_plists(state, python=None):
    from xml.sax.saxutils import escape
    import plistlib
    python = str(Path(python or sys.executable).absolute())
    values = {"@PYTHON@": python, "@RUNNER@": str(state / "installed/code_entropy_review.py"),
              "@STATE@": str(state), "@HOME@": str(Path.home()),
              "@PATH@": f"{Path(python).parent}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    output = state / "plist-preview"
    output.mkdir(exist_ok=True)
    result = []
    for mode in BUDGETS:
        name = f"com.options-monitor.code-entropy.{mode}.plist"
        text = (Path(__file__).parent / "launchd" / name).read_text()
        for token, value in values.items():
            text = text.replace(token, escape(value))
        plistlib.loads(text.encode())
        path = output / name
        path.write_text(text)
        result.append(str(path))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE))
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init")
    init.add_argument("--repository", required=True)
    init.add_argument("--remote")
    init.add_argument("--baseline")
    run = commands.add_parser("run")
    run.add_argument("--mode", required=True, choices=tuple(BUDGETS))
    run.add_argument("--timeout-seconds", type=int)
    run.add_argument("--publish", action="store_true")
    run.add_argument("--coverage-manifest")
    history = commands.add_parser("history")
    history.add_argument("--limit", type=int, default=12)
    for action in ("mute", "unmute", "accept"):
        decision = commands.add_parser(action)
        decision.add_argument("key")
        decision.add_argument("--reason", required=True)
        if action == "accept":
            decision.add_argument("--run-id", required=True)
    reset = commands.add_parser("reset-cursor")
    reset.add_argument("--reason", required=True)
    preview = commands.add_parser("render-plists")
    preview.add_argument("--python")
    args = parser.parse_args(argv)
    state = state_path(args.state_dir)
    if args.action == "history":
        path = state / "scorecards.jsonl"
        rows = path.read_text().splitlines() if path.exists() else []
        print("\n".join(rows[-max(0, args.limit):]) if args.limit > 0 else "")
        return 0
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with run_lock(state):
            if args.action == "init":
                initialize(state, args.repository, args.remote, args.baseline)
                print(json.dumps({"status": "initialized", "state_dir": str(state)}))
            elif args.action == "run":
                record = run_scan(state, args.mode, publish=args.publish, coverage_manifest=args.coverage_manifest,
                                  timeout_seconds=args.timeout_seconds)
                result = {key: record.get(key) for key in ("run_id", "status", "main_sha", "metrics", "gaps", "tools",
                                                           "budget_seconds", "elapsed_seconds")}
                result["report"] = str(state / "runs" / record["run_id"] / "report.md")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if record["status"] == "complete" else 1
            elif args.action == "render-plists":
                print(json.dumps({"preview_files": render_plists(state, args.python), "loaded": False}, indent=2))
            elif args.action == "reset-cursor":
                cursor = state / "daily-cursor.json"
                if cursor.exists():
                    backup = state / "cursor-resets"
                    backup.mkdir(exist_ok=True)
                    write_json(backup / f"{uuid.uuid4().hex}.json", {"reason": args.reason, "cursor": read_json(cursor), "at": now()})
                    cursor.unlink()
            else:
                record_decision(state, args.action, args.key, args.reason, getattr(args, "run_id", None))
    except BlockingIOError:
        print(json.dumps({"status": "skipped", "reason": "independent entropy lock busy"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
