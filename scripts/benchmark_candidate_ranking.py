"""Offline CSP/CC ranking benchmark; synthetic facts, no broker or runtime I/O.

Run with --source-root for both baseline and candidate checkouts. Compare order
hashes as well as timings; these are ranking costs, not whole-Tick latency.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import random
import statistics
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    from domain.domain.engine.candidate_engine import rank_candidate_rows

    cases = []
    for size in (200, 1000, 3000):
        for shape in ("near_returns", "many_bands", "many_symbols"):
            rng = random.Random(37)
            rows = [{
                "symbol": f"S{i}" if shape == "many_symbols" else "NVDA",
                "contract_symbol": f"C{i}",
                "period_net_return": rng.random() * (0.003 if shape == "near_returns" else 0.2),
                "net_assignment_discount_pct": rng.random() * 0.2,
                "strike_upside_margin_pct": rng.random() * 0.2,
                "spread_ratio": 0.02, "open_interest": 100,
                "symbol_concentration_after": 0.1,
            } for i in range(size)]
            for mode in ("put", "call"):
                result = rank_candidate_rows(rows, mode=mode)
                digest = hashlib.sha256(json.dumps(
                    [row["contract_symbol"] for row in result],
                ).encode()).hexdigest()
                samples = []
                for _ in range(7):
                    start = time.perf_counter()
                    rank_candidate_rows(rows, mode=mode)
                    samples.append((time.perf_counter() - start) * 1000)
                cases.append(dict(size=size, shape=shape, mode=mode, order_sha256=digest,
                                  median_ms=statistics.median(samples), samples_ms=samples))
    print(json.dumps(dict(python=platform.python_version(), source_root=str(args.source_root),
                          cases=cases), indent=2))


if __name__ == "__main__":
    main()
