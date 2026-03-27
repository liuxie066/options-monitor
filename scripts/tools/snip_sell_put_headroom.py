#!/usr/bin/env python3
"""Snip sell-put cash/headroom fields into short, non-truncated output.

Why this exists:
- Feishu / OpenClaw often truncates long exec stdout in the system 'Exec completed' line.
- The full per-candidate alert blocks are useful but too verbose for chat notifications.

This script reads the already-rendered text report:
  output/reports/{symbol_lower}_sell_put_alerts.txt
and prints a compact per-contract cash summary for the first N candidates.

It does NOT depend on pandas.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


TITLE_RE = re.compile(r"^\[Sell Put 候选\]\s+(?P<symbol>\S+)\s+(?P<exp>\d{4}-\d{2}-\d{2})\s+(?P<strike>\d+(?:\.\d+)?)P\s*$")
MONEY_RE = re.compile(r"\$([0-9][0-9,]*)(?:\.[0-9]+)?")


def money_or_dash(line: str) -> str:
    # Keep '-' as is; normalize $12,345.* to $12,345
    line = line.strip()
    if line.endswith(": -") or line == "-":
        return "-"
    m = MONEY_RE.search(line)
    if not m:
        return "-"
    return f"${m.group(1)}"


def parse_blocks(text: str):
    lines = text.splitlines()
    blocks = []

    i = 0
    while i < len(lines):
        m = TITLE_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue

        symbol = m.group("symbol")
        exp = m.group("exp")
        strike = m.group("strike")

        cash_req = cash_avail_est = cash_free_est = headroom_est = "-"
        cash_avail = cash_free = headroom = "-"

        # scan forward until next title or EOF
        j = i + 1
        while j < len(lines) and not TITLE_RE.match(lines[j].strip()):
            s = lines[j].strip()
            if s.startswith("担保现金需求(1张):"):
                cash_req = money_or_dash(s)
            elif s.startswith("富途USD现金(折算"):
                cash_avail_est = money_or_dash(s)
            elif s.startswith("现金余量(折算):"):
                cash_free_est = money_or_dash(s)
            elif s.startswith("加仓后余量(折算):"):
                headroom_est = money_or_dash(s)
            elif s.startswith("富途USD现金:"):
                cash_avail = money_or_dash(s)
            elif s.startswith("现金余量(扣占用):"):
                cash_free = money_or_dash(s)
            elif s.startswith("加仓后余量:"):
                headroom = money_or_dash(s)
            j += 1

        blocks.append(
            {
                "symbol": symbol,
                "exp": exp,
                "strike": strike,
                "cash_req": cash_req,
                "cash_avail": cash_avail,
                "cash_free": cash_free,
                "headroom": headroom,
                "cash_avail_est": cash_avail_est,
                "cash_free_est": cash_free_est,
                "headroom_est": headroom_est,
            }
        )
        i = j

    return blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="e.g. PDD")
    ap.add_argument("--n", type=int, default=3, help="how many candidates to show")
    ap.add_argument(
        "--input",
        default=None,
        help="optional explicit path; default: output/reports/{symbol}_sell_put_alerts.txt",
    )
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    symbol_lower = args.symbol.lower()
    input_path = Path(args.input) if args.input else (base / "output" / "reports" / f"{symbol_lower}_sell_put_alerts.txt")

    if not input_path.exists() or input_path.stat().st_size <= 0:
        raise SystemExit(f"No alert report found: {input_path}")

    text = input_path.read_text(encoding="utf-8", errors="replace")
    blocks = parse_blocks(text)

    if not blocks:
        print(f"{args.symbol} sell_put: 无可解析候选（报告格式可能变化）。")
        return

    print(f"{args.symbol} sell_put 现金/余量摘要（每张）")

    for b in blocks[: max(args.n, 1)]:
        contract = f"{b['exp']} {b['strike']}P"

        # Prefer showing real cash if present, else show estimated.
        has_real = b["cash_avail"] != "-" or b["cash_free"] != "-" or b["headroom"] != "-"
        if has_real:
            print(
                f"- {contract} | req {b['cash_req']} | avail {b['cash_avail']} | free {b['cash_free']} | headroom {b['headroom']}"
            )
        else:
            print(
                f"- {contract} | req {b['cash_req']} | cash_avail_eq {b['cash_avail_est']} | cash_free_eq {b['cash_free_est']} | headroom_eq {b['headroom_est']}"
            )


if __name__ == "__main__":
    main()
