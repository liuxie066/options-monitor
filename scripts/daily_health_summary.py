#!/usr/bin/env python3
"""Write a small daily health summary from tick/opend metrics.

Design goals:
- Scheduled/prod-safe: no stdout on success.
- Runtime state only: writes to output_shared/state/*.json.
- Beijing-date based summary (user-facing), but uses UTC timestamps internally.

Inputs:
- output_shared/state/tick_metrics_history.json (list)
- output_shared/state/opend_metrics.json (list)

Outputs (overwrite):
- output_shared/state/daily_health_hk.json
- output_shared/state/daily_health_us.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.trade_symbol_identity import symbol_market


def _read_json_list(path: Path) -> list[dict]:
    try:
        if path.exists() and path.stat().st_size > 0:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, list):
                return [x for x in obj if isinstance(x, dict)]
    except Exception:
        pass
    return []


def _iso_to_dt(s: str) -> datetime | None:
    try:
        if not s:
            return None
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 100:
        return float(sorted_vals[-1])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _summarize_ms(vals: list[int | float | None]) -> dict:
    xs = [float(v) for v in vals if v is not None]
    xs.sort()
    return {
        "count": int(len(xs)),
        "p50": _percentile(xs, 50),
        "p90": _percentile(xs, 90),
        "p99": _percentile(xs, 99),
        "max": (float(xs[-1]) if xs else None),
    }


def build_summary(
    market: str,
    date_bj: str,
    bj_tz: ZoneInfo,
    tick_rows: list[dict],
    opend_rows: list[dict],
) -> dict:
    m = str(market).upper()

    # tick metrics
    t_sched = []
    t_pipe = []
    sent_cnt = 0
    tick_cnt = 0
    long_tail = 0

    for r in tick_rows:
        # markets_to_run is [] or [HK]/[US]
        mk = r.get("markets_to_run")
        if isinstance(mk, list) and mk != [m]:
            continue

        dt = _iso_to_dt(str(r.get("as_of_utc") or ""))
        if not dt:
            continue
        if dt.astimezone(bj_tz).strftime("%Y-%m-%d") != date_bj:
            continue

        tick_cnt += 1
        if bool(r.get("sent")):
            sent_cnt += 1

        accts = r.get("accounts") or []
        if isinstance(accts, list):
            for a in accts:
                if not isinstance(a, dict):
                    continue
                t_sched.append(a.get("scheduler_ms"))
                t_pipe.append(a.get("pipeline_ms"))

        # long tail heuristic
        try:
            if any((isinstance(a, dict) and (a.get("pipeline_ms") or 0) and float(a.get("pipeline_ms")) >= 200000) for a in (accts or [])):
                long_tail += 1
        except Exception:
            pass

    # opend metrics
    # Filter by BJ date; HK symbol heuristic: endswith .HK
    sym_ms: dict[str, list[float]] = {}
    err_cnt: dict[str, int] = {}
    op_cnt = 0

    for r in opend_rows:
        dt = _iso_to_dt(str(r.get("as_of_utc") or ""))
        if not dt:
            continue
        if dt.astimezone(bj_tz).strftime("%Y-%m-%d") != date_bj:
            continue

        sym = str(r.get("symbol") or "")
        if not sym:
            continue
        if symbol_market(sym) != m:
            continue

        op_cnt += 1
        ms = r.get("ms")
        try:
            if ms is not None:
                sym_ms.setdefault(sym, []).append(float(ms))
        except Exception:
            pass

        err = str(r.get("error") or "").strip()
        if err:
            key = err[:120]
            err_cnt[key] = int(err_cnt.get(key, 0) + 1)

    slow = []
    for sym, xs in sym_ms.items():
        if not xs:
            continue
        avg = sum(xs) / len(xs)
        mx = max(xs)
        slow.append({"symbol": sym, "count": len(xs), "avg_ms": avg, "max_ms": mx})
    slow.sort(key=lambda x: (x.get("avg_ms") or 0), reverse=True)

    top_err = sorted(err_cnt.items(), key=lambda kv: kv[1], reverse=True)[:8]

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "date_bj": date_bj,
        "market": m,
        "tick": {
            "ticks": tick_cnt,
            "sent": sent_cnt,
            "sent_rate": (float(sent_cnt) / float(tick_cnt) if tick_cnt else None),
            "scheduler_ms": _summarize_ms(t_sched),
            "pipeline_ms": _summarize_ms(t_pipe),
            "long_tail_ticks_ge_200s": int(long_tail),
        },
        "opend": {
            "records": int(op_cnt),
            "top_errors": [{"error": k, "count": v} for k, v in top_err],
            "slow_symbols": slow[:8],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-bj", default=None, help="YYYY-MM-DD (Beijing). Default: today.")
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    state = (base / "output_shared" / "state").resolve()
    state.mkdir(parents=True, exist_ok=True)

    bj_tz = ZoneInfo("Asia/Shanghai")
    date_bj = args.date_bj or datetime.now(timezone.utc).astimezone(bj_tz).strftime("%Y-%m-%d")

    tick_hist = _read_json_list(state / "tick_metrics_history.json")
    opend_hist = _read_json_list(state / "opend_metrics.json")

    hk = build_summary("HK", date_bj, bj_tz, tick_hist, opend_hist)
    us = build_summary("US", date_bj, bj_tz, tick_hist, opend_hist)

    (state / "daily_health_hk.json").write_text(json.dumps(hk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (state / "daily_health_us.json").write_text(json.dumps(us, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
