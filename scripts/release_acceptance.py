#!/usr/bin/env python3
"""Collect read-only acceptance evidence for a Linux/systemd source upgrade.

Run the reviewed, staged script with the target release Python and -I. Imports of
application owners occur only after selecting and validating that target.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
import uuid


SCHEMA = "om.release-acceptance/v1"
SERVICE_PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "Result",
                      "ExecMainStatus", "MainPID", "NRestarts", "WorkingDirectory")
TIMER_PROPERTIES = ("Id", "LoadState", "UnitFileState", "ActiveState", "SubState",
                    "Result", "NextElapseUSecRealtime", "NextElapseUSecMonotonic",
                    "LastTriggerUSec", "Triggers")
_SECRET = re.compile(r"token|secret|password|authorization|cookie|credential|api.?key", re.I)
_ASSIGNMENT = re.compile(
    r'''(\b[\w-]*(?:token|secret|password|authorization|api[_-]?key)[\w-]*["']?\s*[:=]\s*)("[^"\n]*"|'[^'\n]*'|Bearer\s+[^\s,;}]+|[^\s,;}]+)''', re.I)
_URL = re.compile(r'''(?:https?|wss?)://[^\s<>"']+''', re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")


def _safe_url(match) -> str:
    try:
        value = urlsplit(match.group())
        return urlunsplit((value.scheme, value.netloc.rsplit("@", 1)[-1], value.path,
                           "REDACTED" if value.query else "", "REDACTED" if value.fragment else ""))
    except ValueError:
        return "[REDACTED_URL]"


def _redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SECRET.search(str(key)) else _redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = _URL.sub(_safe_url, value)
        return _ASSIGNMENT.sub(lambda match: match[1] + '"[REDACTED]"', value)
    return value


def _check(evidence: dict, name: str, status: str, detail: str = "") -> None:
    evidence["checks"].append({"name": name, "status": status, "detail": detail})


def _overall(checks: list[dict]) -> str:
    states = [item["status"] for item in checks]
    if "failed" in states:
        return "failed"
    if not states or any(state != "passed" for state in states):
        return "missing_data"
    return "passed"


class _Commands:
    """Record the subprocess interface already consumed by the health owner."""
    def __init__(self, evidence, runner, env, python):
        self.evidence, self.runner, self.env, self.python = evidence, runner, env, python

    def __call__(self, command, **kwargs):
        env = dict(self.env)
        env.update(kwargs.pop("env", None) or {})
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["OM_PYTHON"] = self.python
        kwargs.update(env=env)
        kwargs.setdefault("timeout", 120)
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("check", False)
        started = time.monotonic()
        row = {"command": list(command), "cwd": str(kwargs.get("cwd") or ""),
               "started_at": _now(), "returncode": None, "stdout": "", "stderr": ""}
        try:
            proc = self.runner(command, **kwargs)
            row.update(returncode=proc.returncode, stdout=_text(proc.stdout), stderr=_text(proc.stderr))
            return proc
        except BaseException as exc:
            row.update(stdout=_text(getattr(exc, "output", "")),
                       stderr=_text(getattr(exc, "stderr", "")) or f"{type(exc).__name__}: {exc}",
                       timed_out=isinstance(exc, subprocess.TimeoutExpired),
                       interrupted=isinstance(exc, KeyboardInterrupt))
            raise
        finally:
            row.update(ended_at=_now(), duration_seconds=round(time.monotonic() - started, 3))
            # Neither command output nor an exception fragment reaches disk raw.
            self.evidence["commands"].append(_redact(row))


def _probe(evidence, recorder, name, command, *, cwd=None, timeout=120):
    try:
        proc = recorder(command, cwd=str(cwd) if cwd else None, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        _check(evidence, name, "failed", f"{type(exc).__name__}: {exc}")
        return None
    _check(evidence, name, "passed" if proc.returncode == 0 else "failed",
           f"exit={proc.returncode}")
    return proc if proc.returncode == 0 else None


def _bind_target(repo: Path):
    if (repo / "om.runtime.json").exists():
        raise ValueError("This entry supports source installs, not runtime bundles")
    if not (repo / ".venv/bin/python").is_file() or Path(sys.prefix).resolve() != (repo / ".venv").resolve():
        raise ValueError("Run with the selected target release .venv/bin/python -I")
    loaded = sys.modules.get("src")
    if loaded and not Path(loaded.__file__).resolve().is_relative_to(repo):
        raise ValueError("Application owners are already bound to another checkout")
    sys.path.insert(0, str(repo))
    upgrade = importlib.import_module("src.application.service_upgrade")
    distribution = importlib.import_module("src.application.plugin_distribution")
    deploy = importlib.import_module("src.application.service_deploy")
    runtime = importlib.import_module("src.application.plugin_runtime")
    contracts = importlib.import_module("src.application.agent_tool_contracts")
    for module in (upgrade, distribution, deploy, runtime, contracts):
        if not Path(module.__file__).resolve().is_relative_to(repo):
            raise ValueError("Application owner resolved outside the selected release")
    return SimpleNamespace(upgrade=upgrade, distribution=distribution, deploy=deploy, runtime=runtime, contracts=contracts)


def _properties(output: str, names: list[str], required: tuple[str, ...]) -> list[dict]:
    rows = []
    for block in output.strip().split("\n\n"):
        if not block.strip():
            continue
        row = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if not sep or key in row:
                raise ValueError("Malformed or duplicate systemctl property")
            row[key] = value
        if any(key not in row for key in required):
            raise ValueError("Missing systemctl properties")
        rows.append({key: row[key] for key in required})
    ids = [row["Id"] for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(names):
        raise ValueError("Systemctl rows do not correspond uniquely to requested units")
    return rows


def _plugins(payload, expected: dict[str, str]) -> list[dict]:
    if not expected or not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
        raise ValueError("Missing canonical plugin inventory result")
    rows = payload.get("plugins")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Missing plugin rows")
    ids = [row.get("id") for row in rows]
    if any(not isinstance(key, str) for key in ids) or len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError("Missing, duplicate or unknown plugin IDs")
    for row in rows:
        if row.get("distribution") != expected[row["id"]] or type(row.get("installed")) is not bool:
            raise ValueError("Missing plugin identity/selection facts")
        if row["installed"]:
            if (type(row.get("compatible")) is not bool or not isinstance(row.get("version"), str)
                    or not row["version"] or row.get("source") not in {"managed", "release_source", "python_environment"}
                    or row.get("status") not in {"installed", "incompatible"}):
                raise ValueError("Missing installed plugin facts")
        elif row.get("status") != "not_installed" or any(row.get(key) is not None for key in ("version", "source", "compatible")):
            raise ValueError("Invalid uninstalled plugin facts")
    return [{key: row.get(key) for key in ("id", "distribution", "installed", "compatible", "version", "source", "status")}
            for row in rows]


def _collect_timers(evidence, recorder):
    proc = _probe(evidence, recorder, "timer_enumeration", [
        "systemctl", "list-unit-files", "--type=timer", "--no-legend", "--no-pager", "options-monitor*"])
    if proc is None:
        return
    try:
        names = [line.split()[0] for line in proc.stdout.splitlines() if line.strip()]
        if len(names) != len(set(names)) or any(not name.startswith("options-monitor") or not name.endswith(".timer") for name in names):
            raise ValueError("Invalid or duplicate timer unit-file enumeration")
        rows = []
        if names:
            proc = _probe(evidence, recorder, "timer_properties", [
                "systemctl", "show", "--no-pager", "--property=" + ",".join(TIMER_PROPERTIES), *names])
            if proc is None:
                return
            rows = _properties(proc.stdout, names, TIMER_PROPERTIES)
        if any(not row["UnitFileState"] or not row["ActiveState"] for row in rows):
            raise ValueError("Missing timer activation facts")
        evidence["timers"] = {"names": names, "rows": rows}
        _check(evidence, "timers", "passed" if all(row["LoadState"] == "loaded" for row in rows) else "failed")
    except (TypeError, ValueError) as exc:
        _check(evidence, "timers", "missing_data", str(exc))


def _upgrade_snapshot(payload, path: Path):
    if not isinstance(payload, dict):
        return {"source": str(path), "runtime_prepare": None}
    result = {key: payload.get(key) for key in ("ok", "status", "current_version", "target_version", "previous_version", "updated_at")}
    raw = payload.get("runtime_prepare")
    result["runtime_prepare"] = ({key: raw.get(key) for key in
        ("duration_seconds", "dependency_hash", "installer", "venv_reused")} if isinstance(raw, dict) else None)
    return {**result, "source": str(path)}


def _metrics(snapshot, version):
    if not isinstance(snapshot, dict) or snapshot.get("target_version") != version:
        return None
    raw = snapshot.get("runtime_prepare")
    if not isinstance(raw, dict):
        return None
    duration, digest = raw.get("duration_seconds"), raw.get("dependency_hash")
    if (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0
            or not isinstance(digest, str) or not digest.strip()):
        return None
    return raw


def _cli_check(evidence, recorder, name, command, *, repo, timeout=120):
    proc = _probe(evidence, recorder, name + "_command", command, cwd=repo, timeout=timeout)
    if proc is None:
        return
    try:
        payload = json.loads(proc.stdout)
        if (not isinstance(payload, dict) or payload.get("schema_version") != evidence["cli_schema_version"]
                or payload.get("tool_name") != name or type(payload.get("ok")) is not bool):
            raise ValueError("Missing or wrong CLI envelope identity/result")
        if payload["ok"] is False:
            _check(evidence, name, "failed", "CLI error envelope")
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("Missing CLI envelope data")
        if name == "update.verify":
            if type(data.get("ok")) is not bool or not isinstance(data.get("status"), str):
                raise ValueError("Missing update.verify result")
            passed = data["ok"] is True and data["status"] == "ok"
        else:
            summary = data.get("summary")
            if (type(data.get("checked")) is not bool or type(data.get("supported")) is not bool
                    or not isinstance(summary, dict) or type(summary.get("ok")) is not bool
                    or type(summary.get("error_count")) is not int):
                raise ValueError("Missing service.drift result")
            passed = data["checked"] and data["supported"] and summary["ok"] and summary["error_count"] == 0
            evidence["drift_summary"] = {key: summary.get(key) for key in ("ok", "status", "error_count", "warning_count")}
            evidence["warnings"] = payload.get("warnings", []) + data.get("compatibility_warnings", [])
        _check(evidence, name, "passed" if passed else "failed")
    except (TypeError, ValueError) as exc:
        _check(evidence, name, "missing_data", str(exc))


def _collect_services(evidence, recorder, repo, services, proc_cwd):
    proc = _probe(evidence, recorder, "service_properties", [
        "systemctl", "show", "--no-pager", "--property=" + ",".join(SERVICE_PROPERTIES), *services])
    if proc is None:
        return
    try:
        rows = _properties(proc.stdout, services, SERVICE_PROPERTIES)
        evidence["services"] = rows
        for row in rows:
            name = row["Id"]
            passed = (row["LoadState"] == "loaded" and row["ActiveState"] == "active"
                      and row["SubState"] == "running" and row["Result"] == "success" and row["ExecMainStatus"] == "0")
            _check(evidence, "service:" + name, "passed" if passed else "failed")
            if not row["MainPID"].isdigit():
                raise ValueError("Missing MainPID")
            if int(row["MainPID"]) > 0 and row["WorkingDirectory"]:
                try:
                    actual = Path(proc_cwd(int(row["MainPID"]))).resolve()
                    configured = Path(row["WorkingDirectory"]).resolve()
                    row["actual_cwd"] = str(actual)
                    _check(evidence, "process_cwd:" + name, "passed" if actual == configured else "failed")
                except OSError as exc:
                    _check(evidence, "process_cwd:" + name, "missing_data", str(exc))
    except (TypeError, ValueError) as exc:
        _check(evidence, "service_properties", "missing_data", str(exc))


def _collect(repo_root, runtime_root, *, post, expected_version=None, runner=subprocess.run,
             owners=None, proc_cwd=None, evidence=None):
    evidence = evidence if evidence is not None else {}
    stable, runtime = Path(repo_root).expanduser().absolute(), Path(runtime_root).expanduser().absolute()
    repo = stable.resolve()
    evidence.update(schema=SCHEMA, kind="acceptance" if post else "baseline", collection_id=str(uuid.uuid4()),
        started_at=_now(), host=socket.gethostname(), repo_root=str(stable), runtime_root=str(runtime),
        resolved_repo_root=str(repo), profile_path=str(runtime / "service.profile.json"),
        interpreter={"executable": sys.executable, "prefix": sys.prefix},
        expected_version=expected_version, checks=[], commands=[], complete=False)
    try:
        owners = owners or _bind_target(repo)
        version = (repo / "VERSION").read_text().strip()
        if not version:
            raise ValueError("Empty target VERSION")
        evidence["version"] = version
        _check(evidence, "target", "passed" if not post or version == expected_version else "failed")
        # Capture before verify/health commands can affect any other runtime evidence.
        evidence["upgrade"] = _upgrade_snapshot(
            owners.upgrade.load_upgrade_status(runtime_root=runtime), owners.upgrade.upgrade_status_path(runtime))
        profile = owners.deploy.load_service_profile(runtime / "service.profile.json")
        services = owners.upgrade._restart_service_names(profile)
        evidence["profile"] = {"service_provider": profile.get("service_provider"),
                               "runtime_root": profile.get("runtime_root"), "restart_services": services}
        if not profile or profile.get("service_provider") != "systemd" or not services:
            raise ValueError("Need a systemd source profile with nonempty canonical restart services")
        _check(evidence, "profile", "passed")
        env = dict(owners.upgrade._child_env_from_profile(profile) or {})
        recorder = _Commands(evidence, runner, env, sys.executable)
        expected = {key: item["distribution"] for key, item in owners.runtime.PLUGINS.items()}
        evidence["canonical_plugins"] = expected
        evidence["cli_schema_version"] = owners.contracts.SCHEMA_VERSION
        try:
            payload = owners.distribution.list_plugins(repo_root=repo)
            if isinstance(payload, dict) and payload.get("ok") is False:
                _check(evidence, "plugin_owner", "failed", "Canonical owner rejected inventory")
            rows = _plugins(payload, expected)
            evidence["plugins"] = {"ok": payload["ok"], "plugins": rows}
            _check(evidence, "plugins", "passed" if payload["ok"] and all(
                not row["installed"] or row["compatible"] is True for row in rows) else "failed")
        except (OSError, TypeError, ValueError, ImportError, KeyError) as exc:
            _check(evidence, "plugins", "missing_data", str(exc))
        _collect_timers(evidence, recorder)
        if post:
            _probe(evidence, recorder, "pip_check", [sys.executable, "-m", "pip", "check"], cwd=repo)
            operations = []
            try:
                health = owners.upgrade._post_upgrade_service_health(
                    profile=profile, repo_root=repo, run_cmd=recorder, operations=operations)
                evidence["health"] = health
                if (not isinstance(health, dict) or health.get("status") in {"skipped", "unsupported"}
                        or not isinstance(health.get("checks"), list) or not health["checks"]
                        or type(health.get("ok")) is not bool):
                    _check(evidence, "service_health", "missing_data", "No actual supported health checks")
                else:
                    _check(evidence, "service_health", "passed" if health["ok"] is True else "failed")
            except (OSError, TypeError, ValueError, ImportError, AttributeError) as exc:
                _check(evidence, "service_health", "missing_data", str(exc))
            evidence["health_operations"] = operations
            _collect_services(evidence, recorder, repo, services,
                              proc_cwd or (lambda pid: os.readlink(f"/proc/{pid}/cwd")))
            om = str(stable / "om")
            roots = ["--repo-root", str(stable), "--runtime-root", str(runtime)]
            _cli_check(evidence, recorder, "update.verify", [om, "update", "verify", *roots, "--no-check-latest"], repo=repo, timeout=180)
            _cli_check(evidence, recorder, "service.drift", [om, "service", "drift", *roots,
                       "--profile-path", str(runtime / "service.profile.json")], repo=repo)
            failed = _probe(evidence, recorder, "failed_units_command", ["systemctl", "--failed", "--no-legend", "--no-pager", "--plain"])
            if failed is not None:
                evidence["failed_units"] = [line for line in failed.stdout.splitlines() if line.strip()]
                _check(evidence, "failed_units", "failed" if evidence["failed_units"] else "passed")
            snapshot = evidence["upgrade"]
            _check(evidence, "runtime_prepare", "failed" if snapshot.get("ok") is False else
                   "passed" if _metrics(snapshot, expected_version) is not None else "missing_data")
        evidence["complete"] = True
    except (OSError, TypeError, ValueError, ImportError, AttributeError, KeyError) as exc:
        _check(evidence, "collection", "missing_data", f"{type(exc).__name__}: {exc}")
    evidence["ended_at"] = _now()
    evidence["status"] = _overall(evidence["checks"])
    return evidence


def collect_baseline(repo_root, runtime_root, **kwargs):
    return _collect(repo_root, runtime_root, post=False, **kwargs)


def collect_acceptance(repo_root, runtime_root, *, expected_version, **kwargs):
    return _collect(repo_root, runtime_root, post=True, expected_version=expected_version, **kwargs)


def _timer_rows(snapshot):
    timers = snapshot["timers"]
    names, rows = timers["names"], timers["rows"]
    if not isinstance(names, list) or not isinstance(rows, list) or any(not isinstance(name, str) for name in names):
        raise ValueError("Missing timer collection")
    ids = [row["Id"] for row in rows]
    if len(names) != len(set(names)) or len(ids) != len(set(ids)) or set(names) != set(ids):
        raise ValueError("Missing or duplicate timer rows")
    if any(any(key not in row for key in TIMER_PROPERTIES) for row in rows):
        raise ValueError("Missing timer facts")
    return {row["Id"]: row for row in rows}


def summarize(evidence: dict, baseline: dict) -> dict:
    """Derive status/comparison from one collection; never run another probe."""
    checks = list(evidence.get("checks", []))
    if evidence.get("schema") != SCHEMA or evidence.get("kind") != "acceptance" or evidence.get("complete") is not True:
        checks.append({"name": "collection", "status": "missing_data", "detail": "Incomplete acceptance collection"})
    comparison = {"prior_version": baseline.get("version"), "current_version": evidence.get("version"),
                  "prior_duration_seconds": None, "current_duration_seconds": None,
                  "prior_dependency_hash": None, "current_dependency_hash": None,
                  "dependency_hash_unchanged": None, "duration_difference_seconds": None}
    added = []
    baseline_matches = False
    try:
        if (baseline.get("schema") != SCHEMA or baseline.get("kind") != "baseline"
                or baseline.get("complete") is not True or baseline.get("status") != "passed"
                or _overall(baseline["checks"]) != "passed"):
            raise ValueError("Baseline is not a complete successful collection")
        for key in ("host", "repo_root", "runtime_root", "profile_path"):
            if not baseline.get(key) or baseline[key] != evidence.get(key):
                raise ValueError(f"Baseline target mismatch: {key}")
        required = {"target", "profile", "plugins", "timers"}
        if not required.issubset({item["name"] for item in baseline["checks"]}):
            raise ValueError("Baseline lacks required collection checks")
        baseline_matches = True
        before = _plugins(baseline["plugins"], baseline["canonical_plugins"])
        after = _plugins(evidence["plugins"], evidence["canonical_plugins"])
        selected = lambda rows: {row["id"] for row in rows if row["installed"]}
        managed = lambda rows: {row["id"] for row in rows if row["source"] == "managed"}
        checks.append({"name": "plugin_selection_preserved", "status":
                       "passed" if selected(before) == selected(after) and managed(before) == managed(after) else "failed"})
        old, new = _timer_rows(baseline), _timer_rows(evidence)
        preserved = all(name in new and all(new[name][key] == row[key] for key in ("UnitFileState", "ActiveState"))
                        for name, row in old.items())
        checks.append({"name": "timer_activation_preserved", "status": "passed" if preserved else "failed"})
        added = sorted(new.keys() - old.keys())
    except (KeyError, TypeError, ValueError) as exc:
        checks.append({"name": "baseline_comparison", "status": "missing_data", "detail": str(exc)})
    prior = _metrics(baseline.get("upgrade"), baseline.get("version")) if baseline_matches else None
    current = _metrics(evidence.get("upgrade"), evidence.get("expected_version"))
    for label, metrics in (("prior", prior), ("current", current)):
        if metrics:
            for key in ("duration_seconds", "dependency_hash"):
                comparison[f"{label}_{key}"] = metrics[key]
    if current:
        comparison.update(current_installer=current.get("installer"), current_venv_reused=current.get("venv_reused"))
    if prior and current:
        comparison["dependency_hash_unchanged"] = prior["dependency_hash"] == current["dependency_hash"]
        comparison["duration_difference_seconds"] = round(current["duration_seconds"] - prior["duration_seconds"], 6)
    return {"schema": SCHEMA, "collection_id": evidence.get("collection_id"), "status": _overall(checks),
            "checks": checks, "added_timers": added, "warnings": evidence.get("warnings", []), "comparison": comparison}


def _prepare_output(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("Output directory must be absent or empty; existing evidence is never overwritten")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def write_outputs(output_dir, evidence, summary=None):
    folder = Path(output_dir)
    _prepare_output(folder)
    values = ({"baseline.json": evidence} if summary is None else {
        "acceptance-evidence.json": {**evidence, "checks": summary["checks"], "status": summary["status"]},
        "acceptance-summary.json": {key: value for key, value in summary.items() if key != "comparison"},
        "upgrade-comparison.json": {"schema": SCHEMA, "collection_id": evidence["collection_id"],
                                    "status": summary["status"], **summary["comparison"]}})
    entries = []
    for name, value in values.items():
        data = (json.dumps(_redact(value), indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
        with os.fdopen(os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(data)
        entries.append({"file": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    if summary is not None:
        index = {"schema": SCHEMA, "collection_id": evidence["collection_id"], "status": summary["status"], "host": evidence["host"],
                 "repo_root": evidence["repo_root"], "resolved_repo_root": evidence["resolved_repo_root"],
                 "runtime_root": evidence["runtime_root"], "started_at": evidence["started_at"],
                 "ended_at": evidence.get("ended_at"), "files": entries}
        data = (json.dumps(_redact(index), indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
        with os.fdopen(os.open(folder / "evidence-index.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(data)
    return [entry["file"] for entry in entries] + (["evidence-index.json"] if summary is not None else [])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("baseline", "accept"))
    for name in ("repo-root", "runtime-root", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    evidence = {}
    baseline = {}
    try:
        if args.mode == "accept":
            if not args.expected_version or args.baseline is None:
                raise ValueError("accept requires --expected-version and --baseline")
            baseline = json.loads(args.baseline.read_text())
            if not isinstance(baseline, dict):
                raise ValueError("Baseline must be a JSON object")
        _prepare_output(args.output_dir)
        try:
            if args.mode == "baseline":
                collect_baseline(args.repo_root, args.runtime_root, evidence=evidence)
            else:
                collect_acceptance(args.repo_root, args.runtime_root, expected_version=args.expected_version, evidence=evidence)
        except (KeyboardInterrupt, Exception) as exc:
            evidence.update(complete=False, ended_at=_now())
            evidence.setdefault("checks", [])
            _check(evidence, "interrupted_collection", "missing_data", f"{type(exc).__name__}: {exc}")
            evidence["status"] = _overall(evidence["checks"])
        summary = summarize(evidence, baseline) if args.mode == "accept" else None
        status = summary["status"] if summary else evidence["status"]
        files = write_outputs(args.output_dir, evidence, summary)
        print(json.dumps({"status": status, "collection_id": evidence.get("collection_id"), "files": files}))
        return {"passed": 0, "failed": 1, "missing_data": 2}[status]
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(json.dumps(_redact({"status": "missing_data", "error": str(exc)})), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
