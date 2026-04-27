from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.io_utils import money_cny
from scripts.query_sell_put_cash import query_sell_put_cash


def snapshot_fresh(payload: dict, max_age_sec: int) -> bool:
    if not payload or max_age_sec <= 0:
        return False
    try:
        as_of = payload.get('as_of_utc')
        if not as_of:
            return False
        dt = datetime.fromisoformat(str(as_of))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age.total_seconds() <= float(max_age_sec)
    except Exception:
        return False


def _format_cash_line(acct_u: str, payload: dict) -> str:
    base_available = payload.get('cash_available_cny')
    base_free = payload.get('cash_free_cny')
    if base_available is not None or base_free is not None:
        return f"- **{acct_u}** CNY 持有 {money_cny(base_available)} | CNY 可用 {money_cny(base_free)}"

    total_available = payload.get('cash_available_total_cny')
    total_free = payload.get('cash_free_total_cny')
    if total_available is not None or total_free is not None:
        return f"- **{acct_u}** 总现金折算 {money_cny(total_available)} | 总可用折算 {money_cny(total_free)}"

    return f"- **{acct_u}** CNY 持有 {money_cny(None)} | CNY 可用 {money_cny(None)}"


def query_cash_footer(
    base: Path,
    *,
    config_path: str | Path | None = None,
    market: str,
    accounts: list[str],
    timeout_sec: int = 180,
    snapshot_max_age_sec: int = 900,
) -> list[str]:
    lines: list[str] = []
    payloads: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def _run_one(acct_l: str) -> tuple[str, dict | None, str | None]:
        state_dir = (base / 'output_accounts' / acct_l / 'state').resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        snap_path = state_dir / 'cash_snapshot.json'

        try:
            if snap_path.exists() and snap_path.stat().st_size > 0:
                snap = json.loads(snap_path.read_text(encoding='utf-8'))
                if isinstance(snap, dict) and snapshot_fresh(snap, snapshot_max_age_sec):
                    return acct_l, snap, None
        except Exception:
            pass

        try:
            payload = query_sell_put_cash(
                config=(str(config_path) if config_path is not None and str(config_path).strip() else None),
                market=str(market),
                account=acct_l,
                output_format='json',
                out_dir=str(state_dir),
                base_dir=base,
            )
        except Exception as e:
            return acct_l, None, f"exec_error: {e}"
        try:
            if isinstance(payload, dict) and payload:
                snap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        except Exception:
            pass

        return acct_l, payload, None

    acct_list = [str(a).strip().lower() for a in accounts if str(a).strip()]

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(acct_list)))) as ex:
        futs = {ex.submit(_run_one, acct_l): acct_l for acct_l in acct_list}
        for fut in as_completed(futs):
            acct_l, payload, err = fut.result()
            if err:
                errors[acct_l] = err
            elif payload is not None:
                payloads[acct_l] = payload

    if not payloads and not errors:
        return []

    def asof_bj(payload: dict) -> str:
        try:
            s = payload.get('as_of_utc')
            if not s:
                return ''
            dt = datetime.fromisoformat(str(s))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            bj = dt.astimezone(ZoneInfo('Asia/Shanghai'))
            return bj.strftime('%Y-%m-%d %H:%M')
        except Exception:
            return ''

    lines.append('**💰 现金 CNY**')
    latest_asof = ''
    for acct in accounts:
        acct_l = str(acct).strip().lower()
        acct_u = acct_l.upper()
        if acct_l in payloads:
            payload = payloads[acct_l] or {}
            t = asof_bj(payload)
            if t and (not latest_asof or t > latest_asof):
                latest_asof = t
            lines.append(_format_cash_line(acct_u, payload))
        elif acct_l in errors:
            lines.append(f"- **{acct_u}**: (查询失败) {errors[acct_l]}")

    if latest_asof:
        lines.append('')
        lines.append(f"> 截至 {latest_asof}")

    return lines
