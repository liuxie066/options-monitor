#!/usr/bin/env python3
"""Fetch fresh read-only report inputs, then invoke the existing report CLI."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from uuid import uuid4

SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT))

from domain.domain.symbol_identity import canonical_symbol
from src.application.agent_tool_contracts import AgentToolError
from src.application.reports.prices import load_prices_file
from src.application.reports.strike_metrics import unavailable_evidence
from src.interfaces.cli.main import parse_args

FETCH_TIMEOUT_SECONDS = 480
REPORT_TIMEOUT_SECONDS = 600


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _configured_symbols(configs_root: Path) -> list[str]:
    symbols = set()
    for market in ("hk", "us"):
        payload = json.loads((configs_root / f"user.{market}.json").read_text(encoding="utf-8"))
        for row in payload.get("symbols", []):
            symbol = canonical_symbol(row.get("symbol"))
            if symbol is None:
                raise ValueError("configured symbol is invalid")
            symbols.add(symbol)
    return sorted(symbols)


def _fetch_json(script: Path, arguments: list[str], *, host: str, repo_root: str) -> dict:
    if not host or not repo_root:
        raise ValueError("remote host and repository root are required")
    command = (
        f"cd {shlex.quote(repo_root)} && exec env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 "
        ".venv/bin/python - " + shlex.join(arguments)
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
         "-o", "ServerAliveCountMax=2", host, command],
        input=script.read_text(encoding="utf-8"), text=True, capture_output=True,
        timeout=FETCH_TIMEOUT_SECONDS, check=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("remote output is not a JSON object")
    return payload


def run_weekly(argv: list[str], *, source_root: Path = SOURCE_ROOT, inputs_root: Path,
               remote_host: str, remote_repo_root: str, remote_runtime_root: str) -> int:
    args = parse_args(argv)
    if argv[:3] != ["report", "run", "config_review"] or args.prices_file or args.candidate_evidence_file:
        raise ValueError("weekly runner owns fresh inputs for report run config_review")
    now = datetime.now(timezone.utc)
    as_of = now.isoformat()
    input_dir = inputs_root / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    input_dir.mkdir(parents=True, exist_ok=False)
    evidence_file = input_dir / "candidate_evidence.json"
    prices_file = input_dir / "prices.json"
    receipt_file = input_dir / "prefetch.json"
    receipt = {"started_at_utc": as_of, "source_root": str(source_root),
               "evidence": {"status": "pending"}, "prices": {"status": "pending"}}
    _write_json(receipt_file, receipt)

    try:
        if not remote_runtime_root:
            raise ValueError("remote runtime root is required")
        payload = _fetch_json(source_root / "src/application/reports/strike_metrics.py",
                              ["--export-root", remote_runtime_root, "--as-of", as_of],
                              host=remote_host, repo_root=remote_repo_root)
        _write_json(evidence_file, payload)
        receipt["evidence"] = {"status": "fetched", "path": str(evidence_file)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = f"candidate_fetch_failed:{type(exc).__name__}"
        _write_json(evidence_file, unavailable_evidence(now, reason))
        receipt["evidence"] = {"status": "failed", "reason": reason, "path": str(evidence_file)}
        print(reason, file=sys.stderr)
    _write_json(receipt_file, receipt)

    extra_args = ["--candidate-evidence-file", str(evidence_file)]
    try:
        symbols = _configured_symbols(Path(args.configs_root).expanduser().resolve())
        if not symbols:
            raise ValueError("no configured symbols for price fetch")
        payload = _fetch_json(source_root / "deploy/launchd/fetch-config-review-prices.py",
                              [json.dumps(symbols)], host=remote_host, repo_root=remote_repo_root)
        _write_json(prices_file, payload)
        snapshot = load_prices_file(prices_file)
        receipt["prices"] = {"status": "fetched", "path": str(prices_file),
                             "symbol_count": len(snapshot.observations)}
        extra_args.extend(["--prices-file", str(prices_file)])
    except (AgentToolError, OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = f"price_fetch_failed:{type(exc).__name__}"
        receipt["prices"] = {"status": "failed", "reason": reason}
        print(f"{reason}; report uses timestamped runtime price observations if available", file=sys.stderr)
    _write_json(receipt_file, receipt)

    command = [sys.executable, "-m", "src.interfaces.cli.main", *argv, *extra_args]
    report_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    report_env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(command, cwd=source_root, env=report_env, timeout=REPORT_TIMEOUT_SECONDS)
        receipt["report"] = {"status": "completed", "exit_code": result.returncode, "command": command}
        code = result.returncode
    except subprocess.TimeoutExpired:
        receipt["report"] = {"status": "timeout", "command": command}
        code = 124
    receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    _write_json(receipt_file, receipt)
    return code


def main() -> int:
    return run_weekly(
        sys.argv[1:], inputs_root=Path(os.environ["OM_REPORT_INPUTS_ROOT"]).expanduser(),
        remote_host=os.environ.get("OM_REPORT_REMOTE_HOST", ""),
        remote_repo_root=os.environ.get("OM_REPORT_REMOTE_REPO_ROOT", ""),
        remote_runtime_root=os.environ.get("OM_REPORT_REMOTE_RUNTIME_ROOT", ""),
    )


if __name__ == "__main__":
    raise SystemExit(main())
