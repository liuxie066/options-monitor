#!/usr/bin/env python3
"""Lightweight OpenD doctor via telnet (no futu-api dependency).

This script validates:
- telnet port reachability
- basic command responsiveness
- current login/ready state hints

It does NOT validate quote fetching (that needs futu-api).

Usage:
  python3 scripts/doctor_opend_telnet.py --host 127.0.0.1 --port 22222

Exit codes:
  0: OK
  2: connect failed
  3: telnet not responsive / unexpected output
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time


def read_all(sock: socket.socket, total_sec: float) -> bytes:
    end = time.time() + total_sec
    chunks: list[bytes] = []
    while time.time() < end:
        try:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
            # extend a bit when data arrives
            end = time.time() + 0.8
        except socket.timeout:
            pass
    return b"".join(chunks)


def run(host: str, port: int, timeout_sec: float) -> dict:
    res: dict = {
        "ok": False,
        "host": host,
        "port": port,
        "ts": time.time(),
        "banner": None,
        "ping": None,
        "help": None,
        "hints": [],
    }

    try:
        sock = socket.create_connection((host, port), timeout=timeout_sec)
    except Exception as e:
        res["error"] = f"connect_failed: {e!r}"
        return res

    try:
        sock.settimeout(0.3)
        banner = read_all(sock, 1.0).decode("utf-8", "ignore").strip()
        res["banner"] = banner

        sock.sendall(b"ping\r\n")
        ping_out = read_all(sock, 2.0).decode("utf-8", "ignore").strip()
        res["ping"] = ping_out

        sock.sendall(b"help\r\n")
        help_out = read_all(sock, 3.0).decode("utf-8", "ignore").strip()
        res["help"] = help_out

        text = (banner + "\n" + ping_out + "\n" + help_out).lower()

        # crude state hints
        if "登录成功" in (banner + ping_out + help_out):
            res["hints"].append("logged_in")
        if "需要图形验证码" in (banner + ping_out + help_out):
            res["hints"].append("need_pic_verify")
        if "需要手机验证码" in (banner + ping_out + help_out):
            res["hints"].append("need_phone_verify")
        if "命令列表" in (banner + ping_out + help_out):
            res["hints"].append("telnet_ok")

        # We consider OK if telnet is responsive and shows command list.
        res["ok"] = ("telnet_ok" in res["hints"])
        return res
    finally:
        try:
            sock.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=22222)
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = run(args.host, args.port, args.timeout)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"OpenD telnet @ {args.host}:{args.port}")
        print(f"ok={res['ok']} hints={res.get('hints')}")
        if res.get("error"):
            print(f"error: {res['error']}")
        if res.get("banner"):
            print("--- banner ---")
            print(res["banner"])
        if res.get("ping"):
            print("--- ping ---")
            print(res["ping"])

    if not res["ok"]:
        return 2 if str(res.get("error", "")).startswith("connect_failed") else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
