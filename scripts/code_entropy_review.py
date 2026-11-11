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
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tarfile
import time
import uuid

if __package__:
    from . import code_entropy_assessment as assessment
    from . import code_entropy_scan as scan
else:
    import code_entropy_assessment as assessment
    import code_entropy_scan as scan

DEFAULT_STATE = Path.home() / "Library/Application Support/options-monitor/code-entropy-review"
BUDGETS = {"daily": 900, "weekly": 5400}
LABEL = "code-entropy"
VULTURE_UNUSED = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+): unused (?P<kind>[a-z]+) '(?P<name>[^']*)' \((?P<confidence>\d+)% confidence\)$")


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


def command(argv, cwd, log, timeout, *, input_text=None, env=None):
    if timeout <= 0:
        raise TimeoutError("scan budget exhausted")
    with log.open("w") as stdout, log.with_suffix(".stderr").open("w") as stderr:
        process = subprocess.Popen(argv, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=True,
                                   stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                   text=True, env=env)
        try:
            process.communicate(input=input_text, timeout=timeout)
            code = process.returncode
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    return {"command": argv, "exit_code": code, "output": log.name,
            "stderr": log.with_suffix(".stderr").name}


def coverage_entries(evidence, source_root=None):
    """Validate a coverage JSON report and index it by repository-relative path."""
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
            if source_root is None:
                raise ValueError("coverage paths must be relative to the scanned source")
            file = file.relative_to(source_root)
        if ".." in file.parts:
            raise ValueError("invalid coverage source path")
        result[file.as_posix()] = entry
    return result


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
    source_root = manifest.get("source_root")
    return coverage_entries(read_json(path), Path(source_root) if source_root else None)


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


def automatic_review(source, findings, state, config, output, sha, coverage, execute, evidence=None):
    evidence = evidence or {}
    options = config.get("auto_review", {})
    if not isinstance(options, dict):
        raise ValueError("auto_review must be an object")
    if options.get("enabled") is not True:
        return findings, {"enabled": False}
    client = Path(options.get("client", ""))
    timeout = options.get("timeout_seconds", 240)
    model = options.get("model", "opus")
    if not client.is_absolute() or not client.is_file():
        raise ValueError("auto_review.client must name an existing absolute Claude executable; no installation attempted")
    if type(timeout) is not int or not 1 <= timeout <= 300 or not isinstance(model, str) or not model.strip():
        raise ValueError("invalid auto_review model or timeout (1..300 seconds)")
    deliver([], state, config, sha, execute)
    decisions = read_json(state / "decisions.json", {})
    ledger = read_json(state / "issues.json", {})
    queue = read_json(state / "review-queue.json", {})
    assessment.enqueue(queue, findings, decisions, ledger)
    write_json(state / "review-queue.json", queue)
    before = json.loads(json.dumps(decisions))

    def invoke(packet, schema):
        write_json(output / "review-request.json", packet)
        write_json(output / "review-schema.json", schema)
        work = output / "review-work"
        work.mkdir()
        argv = [str(client), "--print", "--safe-mode", "--tools", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--no-session-persistence", "--permission-mode", "dontAsk",
                "--output-format", "json", "--json-schema", json.dumps(schema),
                "--system-prompt", assessment.SYSTEM, "--model", model]
        result = execute(argv, cwd=work, input_text=json.dumps(packet, ensure_ascii=False), timeout=timeout)
        if result["exit_code"]:
            raise RuntimeError("automatic entropy reviewer failed; queue retained and daily cursor not advanced")
        envelope = json.loads(result["text"])
        write_json(output / "review-response.json", envelope)
        if not isinstance(envelope, dict) or envelope.get("is_error") is not False or envelope.get("subtype") != "success":
            raise ValueError("automatic reviewer did not return a successful structured result")
        return envelope.get("structured_output")

    ready, summary = assessment.assess(source, findings, queue, decisions, ledger, options, invoke,
                                      sha=sha, coverage=coverage,
                                      vulture_unused=evidence.get("vulture"),
                                      radon_complexity=evidence.get("radon"))
    for key, decision in decisions.items():
        if decision != before.get(key):
            decision["at"] = now()
            decision.setdefault("history", []).append({"action": "auto-" + decision["review_decision"],
                "at": decision["at"], "run_id": output.name, "evidence_digest": decision["reviewed_evidence"],
                "reason": decision["reason"], "model": model, "policy_version": assessment.POLICY_VERSION})
    write_json(state / "decisions.json", decisions)
    write_json(state / "review-queue.json", queue)
    write_json(output / "reviewed-candidates.json", ready)
    summary.update(enabled=True, model=model, timeout_seconds=timeout)
    write_json(output / "assessment.json", summary)
    return list({row["key"]: row for row in [*findings, *ready]}.values()), summary


def tool_command(name):
    import importlib.util
    executable = shutil.which(name)
    if executable:
        return [executable]
    if name != "jscpd" and importlib.util.find_spec(name) is not None:
        return [sys.executable, "-I", "-m", name]
    return None


def _radon_blocks(block):
    yield block
    for child in [*(block.get("closures") or []), *(block.get("methods") or [])]:
        yield from _radon_blocks(child)


def tool_runs(source, selected, execute):
    """Probe and run the fixed analysis tools.

    Tools are evidence sources, never the decision itself.  Vulture can only
    corroborate or veto a dead-code candidate produced by the built-in
    whole-tree reference index; radon supplies authoritative complexity values
    when it is available, with the built-in counter as fallback.
    """
    tools, findings = {}, []
    evidence = {"vulture": None, "radon": None}
    selected = [path for path in selected if (source / path).is_file() and not (source / path).is_symlink()]
    for name in ("ruff", "vulture", "radon", "coverage"):
        prefix = tool_command(name)
        if not prefix:
            tools[name] = {"available": False,
                           "reason": "not installed; install requirements/dev.txt with constraints/dev.txt"}
            continue
        version = execute([*prefix, "--version"])
        if version["exit_code"]:
            raise RuntimeError(f"{name} version probe failed; see raw tool output")
        tools[name] = {"available": True, "version": version["text"].strip(), "command_prefix": prefix}
        if not selected:
            tools[name]["execution"] = "not run; no scoped files"
            continue
        if name == "ruff":
            result = execute([*prefix, "check", "--isolated", "--target-version", "py312",
                              "--select", "F401,F841,ARG", "--output-format", "json", *selected], cwd=source)
            tools[name]["result"] = {key: value for key, value in result.items() if key != "text"}
            if result["exit_code"] not in {0, 1}:
                raise RuntimeError("ruff failed; see raw tool output")
            for row in json.loads(result["text"]):
                path = Path(row["filename"]).relative_to(source).as_posix()
                line = row["location"]["row"]
                source_line = (source / path).read_text().splitlines()[line - 1].strip()
                findings.append(scan.candidate(f"ruff-{row['code']}", path, f"{row['code']}:{source_line}", line,
                    json.dumps(row, ensure_ascii=False), tool=f"ruff {tools[name]['version']}"))
            continue
        if name == "vulture":
            result = execute([*prefix, "--min-confidence", "60", *selected], cwd=source)
            tools[name]["result"] = {key: value for key, value in result.items() if key != "text"}
            if result["exit_code"] not in {0, 3}:
                raise RuntimeError("vulture failed; see raw tool output")
            unused = {}
            for line in result["text"].splitlines():
                match = VULTURE_UNUSED.match(line.strip())
                if not match or match.group("kind") not in {"function", "method"}:
                    continue
                unused.setdefault(Path(match.group("path")).as_posix(), set()).add(int(match.group("line")))
            evidence["vulture"] = {"analyzed": sorted(set(selected)), "unused": unused}
            tools[name]["execution"] = "corroborates or vetoes zero-reference dead-code candidates at 60% confidence"
            tools[name]["unused_symbols"] = sum(len(lines) for lines in unused.values())
            continue
        if name == "radon":
            result = execute([*prefix, "cc", "--json", *selected], cwd=source)
            tools[name]["result"] = {key: value for key, value in result.items() if key != "text"}
            if result["exit_code"]:
                raise RuntimeError("radon failed; see raw tool output")
            measured = {}
            for path, blocks in json.loads(result["text"]).items():
                rows = measured.setdefault(Path(path).as_posix(), {})
                for block in blocks:
                    for item in _radon_blocks(block):
                        if (item.get("type") in {"function", "method"}
                                and type(item.get("lineno")) is int and type(item.get("complexity")) is int):
                            rows[item["lineno"]] = item["complexity"]
            evidence["radon"] = measured
            tools[name]["execution"] = "authoritative cyclomatic complexity when available; built-in counter is fallback"
            tools[name]["measured_functions"] = sum(len(rows) for rows in measured.values())
            continue
        tools[name]["execution"] = "probed; weekly self-measurement decides whether to run the workload"
    tools["jscpd"] = {"available": False, "adopted": False,
                      "reason": "Node-only tool; this Python-only repository keeps the versioned entropy-token-v1 duplication metric"}
    tools["builtin"] = {"available": True, "version": scan.VERSION,
                        "complexity": "entropy-ast-v1 (fallback)", "duplication": "entropy-token-v1"}
    return tools, findings, evidence


def coverage_sources(source):
    """Source roots coverage must measure; plugin packages live under */src."""
    roots = [name for name in ("src", "domain", "om_strategy_core", "scripts") if (source / name).is_dir()]
    roots += sorted(path.relative_to(source).as_posix()
                    for path in (source / "plugins").glob("*/src") if path.is_dir())
    return roots


def coverage_environment(output):
    """Ambient-neutral environment; the workload's own venv supplies its imports.

    Plugin sources must never be injected through PYTHONPATH: pip installs them
    into the disposable venv, and shadowing those distributions with the source
    tree makes package-bound tests resolve a different module than the one the
    environment declares.
    """
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
                                  "COVERAGE_FILE", "COVERAGE_RCFILE", "COVERAGE_PROCESS_START"}}
    environment.update(PYTHONDONTWRITEBYTECODE="1",
                       COVERAGE_FILE=str(output / "coverage-raw.data"),
                       PIP_DISABLE_PIP_VERSION_CHECK="1")
    return environment


def coverage_plugin_mappings(source, venv):
    """Map each installed plugin package back to the snapshot source it was built from.

    Coverage traces the imported distribution in the venv's site-packages.  The
    ``[paths]`` remapping folds those traces back onto the snapshot's
    ``plugins/*/src`` tree so the report speaks the repository's own paths.
    """
    site_packages = Path(sysconfig.get_path("purelib", vars={"base": str(venv), "platbase": str(venv)}))
    measured, mappings = [], {}
    for root in sorted((source / "plugins").glob("*/src")):
        for package in sorted(root.iterdir()):
            installed = site_packages / package.name
            if not (package / "__init__.py").is_file() or not installed.is_dir():
                continue
            measured.append(str(installed.resolve()))
            mappings[package.name] = (str(package.resolve()), str(installed.resolve()))
    return measured, mappings


def coverage_configuration(output, measured, mappings):
    """Write the coverage rc file: branch mode, measured roots, and path remapping."""
    lines = ["[run]", "branch = True", "source ="]
    lines.extend(f"    {path}" for path in measured)
    if mappings:
        lines.extend(["", "[paths]"])
        for name, (canonical, installed) in sorted(mappings.items()):
            # The first pattern is the canonical name coverage reports; the
            # installed distribution is the path the workload actually imports.
            lines.extend([f"{name} =", f"    {canonical}", f"    {installed}"])
    path = output / "coverage.rc"
    path.write_text("\n".join(lines) + "\n")
    return path


def self_coverage(source, output, execute, *, scratch, time_left):
    """Run the snapshot's own tests under branch coverage.

    The ambient interpreter is not trusted to match the snapshot's dependency
    declaration, so a disposable virtual environment is built from the snapshot
    itself.  Any workload failure degrades to an explicit record; only the outer
    budget alarm (TimeoutError) may abort the scan.
    """
    started = time.monotonic()
    record = {"source": "self-run", "status": "skipped", "reason": None}
    if not (source / "tests").is_dir() or not (source / "requirements/dev.txt").is_file():
        record["reason"] = "snapshot has no tests/ and requirements/dev.txt workload"
        return None, record
    sources = coverage_sources(source)
    if not sources:
        record["reason"] = "snapshot has no coverage source roots"
        return None, record
    try:
        if time_left() < 600:
            record["reason"] = "remaining budget is too small for a coverage workload"
            return None, record
        venv = Path(scratch) / "coverage-venv"
        environment = coverage_environment(output)
        result = execute([sys.executable, "-m", "venv", str(venv)], timeout=120, env=environment)
        record["venv_command"] = {key: value for key, value in result.items() if key != "text"}
        if result["exit_code"]:
            record.update(status="failed", reason="could not create the disposable coverage environment")
            return None, record
        pip = [str(venv / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check", "-q",
               "-r", "requirements/dev.txt", "-c", "constraints/dev.txt"]
        result = execute(pip, cwd=source, timeout=min(600, max(60, time_left() - 420)), env=environment)
        record["install_command"] = {key: value for key, value in result.items() if key != "text"}
        if result["exit_code"]:
            record.update(status="failed", reason="snapshot development requirements could not be installed")
            return None, record
        # Building the local plugin packages writes untracked build/ and egg-info
        # directories into the snapshot; remove them so they never become metrics.
        for artifact in [*sorted((source / "plugins").glob("*/build")),
                         *sorted((source / "plugins").glob("*/*.egg-info")),
                         *sorted((source / "plugins").glob("*/src/*.egg-info"))]:
            shutil.rmtree(artifact)
        installed_plugins, mappings = coverage_plugin_mappings(source, venv)
        measured = [str((source / name).resolve()) for name in sources if not name.startswith("plugins/")]
        configuration = coverage_configuration(output, [*measured, *installed_plugins], mappings)
        environment["COVERAGE_RCFILE"] = str(configuration)
        record["configuration"] = {"path": str(configuration), "measured_roots": len(measured),
                                   "mapped_plugins": sorted(mappings)}
        workload = [str(venv / "bin/python"), "-m", "coverage", "run", "--branch",
                    "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "--basetemp", str(Path(scratch) / "pytest-tmp")]
        result = execute(workload, cwd=source, timeout=max(60, time_left() - 420), env=environment)
        record["workload"] = {"command": workload, "exit_code": result["exit_code"],
                              "output": result.get("output"), "stderr": result.get("stderr")}
        if result["exit_code"]:
            record.update(status="failed", reason=f"snapshot test workload exited {result['exit_code']}")
            return None, record
        combined_environment = {**environment, "COVERAGE_FILE": str(output / "coverage.data")}
        combine = [str(venv / "bin/python"), "-m", "coverage", "combine", "--keep",
                   str(output / "coverage-raw.data")]
        result = execute(combine, cwd=source, timeout=min(120, max(30, time_left() - 360)),
                         env=combined_environment)
        record["combine_command"] = {key: value for key, value in result.items() if key != "text"}
        if result["exit_code"]:
            record.update(status="failed", reason="coverage data could not be combined")
            return None, record
        report = output / "coverage-self.json"
        result = execute([str(venv / "bin/python"), "-m", "coverage", "json", "-o", str(report)],
                         cwd=source, timeout=min(120, max(30, time_left() - 300)), env=combined_environment)
        record["json_command"] = {key: value for key, value in result.items() if key != "text"}
        if result["exit_code"]:
            record.update(status="failed", reason="coverage JSON generation failed")
            return None, record
        entries = coverage_entries(read_json(report), source_root=source)
        record.update(status="complete", reason=None, files=len(entries),
                      coverage_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
                      elapsed_seconds=round(time.monotonic() - started, 3))
        return entries, record
    except TimeoutError:
        raise
    except (subprocess.TimeoutExpired, OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as error:
        record.update(status="failed", reason=f"{type(error).__name__}: {error}")
        return None, record


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
    source_files = [Path(__file__), Path(scan.__file__), Path(assessment.__file__), Path(scan.__file__).with_name("guardrails_check.py")]
    record["scanner_digest"] = scan.digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files})
    record["python_version"] = sys.version.split()[0]
    write_json(output / "progress.json", record)

    def execute(argv, cwd=None, *, input_text=None, timeout=300, env=None):
        log = output / f"{len(record['commands']):03d}.stdout"
        remaining = budget - (time.monotonic() - started)
        options = {"input_text": input_text} if input_text is not None else {}
        if env is not None:
            options["env"] = env
        result = command(argv, cwd or state, log, min(remaining, timeout), **options)
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
                version = re.search(r"版本[：:]\s*(\d+)", policy.read_text())
                if not version or int(version.group(1)) not in {1, 2}:
                    raise ValueError("main baseline version is unsupported; update the scanner before resuming")
                record["baseline_version"] = int(version.group(1))
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
            tools, tool_findings, evidence = tool_runs(source, selected, execute)
            if coverage is None and mode == "weekly" and tools.get("coverage", {}).get("available") is True:
                record["stage"] = "coverage-workload"
                write_json(output / "progress.json", record)
                coverage, workload = self_coverage(
                    source, output, execute, scratch=scratch,
                    time_left=lambda: budget - (time.monotonic() - started))
                record["coverage_workload"] = workload
                if coverage is not None:
                    write_json(output / "coverage-evidence.json", coverage)
                    tools["coverage"]["execution"] = "self-run branch coverage of the snapshot test suite"
                else:
                    tools["coverage"]["execution"] = workload["reason"]
            elif coverage is not None:
                tools.setdefault("coverage", {})["execution"] = "external SHA-bound coverage manifest"
            elif tools.get("coverage", {}).get("available") is True:
                tools["coverage"]["execution"] = "not run; branch coverage is measured in weekly deep scans"
            record["stage"] = "static-measurement"
            write_json(output / "progress.json", record)
            metrics, findings = scan.scan(source, selected, coverage,
                                           vulture_unused=evidence.get("vulture"),
                                           radon_complexity=evidence.get("radon"))
            findings.extend(tool_findings)
            if mode == "daily" and cursor and selected:
                previous = snapshot(cursor["sha"], "previous", scratch)
                _, old_tool_findings, old_evidence = tool_runs(previous, selected, execute)
                old_measured = scan.scan(previous, selected,
                                         vulture_unused=old_evidence.get("vulture"),
                                         radon_complexity=old_evidence.get("radon"))[1]
                old = {row["key"]: row for row in [*old_measured, *old_tool_findings]}
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
                if mode == "weekly":
                    reason = (record.get("coverage_workload") or {}).get("reason") or "coverage is not installed"
                    record["gaps"].append(f"Branch coverage is unavailable for this weekly run: {reason}.")
                else:
                    record["gaps"].append("Branch coverage is measured only in weekly deep scans; daily increments leave dead-code confirmation and untested branches null.")
            if coverage is not None and metrics["untested_branches"] is None:
                record["gaps"].append("Coverage did not report branch data for every scoped file: untested branches and dead-code confirmation stay null.")
            if coverage is not None and metrics["dead_code_symbols"] is None:
                record["gaps"].append("Dead-code gates remain unproven (partial coverage, public API or dynamic entry): confirmation is null.")
            if not tools.get("vulture", {}).get("available"):
                record["gaps"].append("vulture is not installed: dead-code candidates rely on the built-in reference index alone.")
            if not tools.get("radon", {}).get("available"):
                record["gaps"].append("radon is not installed: complexity uses the built-in entropy-ast-v1 counter.")
            record["gaps"].append("Duplicate ratio uses entropy-token-v1 (a conservative lower bound); jscpd is not adopted for this Python-only repository.")
            record["gaps"].append("Dynamic config consumers are not excluded: confirmed unused config is null.")
            delivery_findings = findings
            if publish:
                record["stage"] = "candidate-assessment"
                record["auto_review"] = {"status": "running"}
                write_json(output / "progress.json", record)
                delivery_findings, record["auto_review"] = automatic_review(
                    source, findings, state, config, output, sha, coverage, execute, evidence=evidence)
                record["auto_review"]["status"] = "complete"
            record["published_issues"] = deliver(delivery_findings, state, config, sha, execute) if publish else []
            ledger = read_json(state / "issues.json", {})
            record["pending_issues"] = [key for key, value in ledger.items() if value["status"] == "pending"]
            record["status"] = "complete"
    except Exception as error:
        record["status"] = "failed"
        if record.get("auto_review", {}).get("status") == "running":
            record["auto_review"]["status"] = "failed"
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
            decision.pop("reviewed_evidence", None)
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
