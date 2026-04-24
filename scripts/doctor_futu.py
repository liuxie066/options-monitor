#!/usr/bin/env python3
from __future__ import annotations

"""User-facing doctor for the Futu data source.

This wraps the lower-level OpenD checks so users can think in terms of one
source: fetch.source=futu.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _find_python() -> str:
    vpy = REPO_ROOT / ".venv" / "bin" / "python"
    if vpy.exists():
        return str(vpy)
    return sys.executable


def _extract_json_obj(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    if not text:
        return None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj = json.loads(text[i:])
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _run_json(cmd: list[str], *, timeout_sec: int) -> tuple[int, dict[str, Any] | None, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=int(timeout_sec),
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return int(proc.returncode), _extract_json_obj(out), (out or err)
    except subprocess.TimeoutExpired as exc:
        return 124, None, f"timeout after {timeout_sec}s: {exc}"
    except Exception as exc:
        return 1, None, f"{type(exc).__name__}: {exc}"


def _sdk_status() -> dict[str, Any]:
    futu_found = importlib.util.find_spec("futu") is not None
    return {
        "futu_sdk_importable": futu_found,
        "ok": bool(futu_found),
    }


def _print_human(result: dict[str, Any]) -> None:
    print("# Futu Data Source Doctor")
    print("")
    print(f"endpoint: {result.get('host')}:{result.get('port')}")
    print("")

    sdk = result.get("sdk") if isinstance(result.get("sdk"), dict) else {}
    if sdk.get("ok"):
        print("[OK] SDK importable: futu")
    else:
        print("[FAIL] SDK not importable: install futu-api")

    wd = result.get("watchdog") if isinstance(result.get("watchdog"), dict) else {}
    if wd.get("ok"):
        print("[OK] Futu/OpenD gateway healthy")
    else:
        code = wd.get("error_code") or "FUTU_GATEWAY_UNHEALTHY"
        msg = wd.get("message") or wd.get("error") or result.get("watchdog_raw") or "unknown error"
        print(f"[FAIL] Futu/OpenD gateway unhealthy: {code}: {msg}")
        action = wd.get("action_taken")
        if action:
            print(f"  action: {action}")

    fields = result.get("required_fields") if isinstance(result.get("required_fields"), dict) else None
    if fields is not None:
        rows = fields.get("results") if isinstance(fields.get("results"), list) else []
        if not rows:
            print("[WARN] Symbol field check returned no rows")
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = "OK" if row.get("ok") else "FAIL"
            print(
                f"[{status}] {row.get('symbol')} underlier={row.get('underlier_code')} "
                f"chain={row.get('chain_rows')} snap={row.get('snap_rows')} spot={row.get('spot')}"
            )
            missing = row.get("missing_snapshot_cols") if isinstance(row.get("missing_snapshot_cols"), list) else []
            if missing:
                print(f"  missing: {', '.join(str(x) for x in missing)}")
            if row.get("note"):
                print(f"  note: {row.get('note')}")
            if row.get("error"):
                print(f"  error: {row.get('error')}")

    print("")
    if result.get("ok"):
        print("[OK] 富途数据源可用。配置里可使用 fetch.source = \"futu\"。")
    else:
        print("[FAIL] 富途数据源尚不可用。请按上面的失败项处理后重试。")


def main() -> int:
    ap = argparse.ArgumentParser(description="Doctor Futu data source (fetch.source=futu)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11111)
    ap.add_argument("--symbols", nargs="*", default=[], help="Option symbols to validate, e.g. NVDA 0700.HK")
    ap.add_argument("--ensure", action="store_true", help="Try to start OpenD using OPEND_START_SCRIPT if port is closed")
    ap.add_argument("--timeout-sec", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    py = _find_python()
    sdk = _sdk_status()

    watchdog_cmd = [
        py,
        "scripts/opend_watchdog.py",
        "--host",
        str(args.host),
        "--port",
        str(int(args.port)),
        "--json",
    ]
    if args.ensure:
        watchdog_cmd.append("--ensure")
    wd_rc, wd_json, wd_raw = _run_json(watchdog_cmd, timeout_sec=args.timeout_sec)

    required_fields = None
    fields_rc = 0
    fields_raw = ""
    if wd_json and wd_json.get("ok") and args.symbols:
        fields_cmd = [
            py,
            "scripts/doctor_opend_required_fields.py",
            "--host",
            str(args.host),
            "--port",
            str(int(args.port)),
            "--json",
            "--symbols",
            *[str(s) for s in args.symbols],
        ]
        fields_rc, required_fields, fields_raw = _run_json(fields_cmd, timeout_sec=args.timeout_sec)

    ok = bool(sdk.get("ok")) and bool(wd_json and wd_json.get("ok")) and int(fields_rc) == 0
    result = {
        "ok": ok,
        "host": str(args.host),
        "port": int(args.port),
        "source": "futu",
        "sdk": sdk,
        "watchdog_returncode": wd_rc,
        "watchdog": wd_json,
        "watchdog_raw": wd_raw,
        "required_fields_returncode": fields_rc,
        "required_fields": required_fields,
        "required_fields_raw": fields_raw,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
