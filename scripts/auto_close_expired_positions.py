#!/usr/bin/env python3
"""Auto-close expired option_positions records in Feishu Bitable.

Design goals:
- Do NOT add extra list/scan calls: consume the already-generated option_positions_context.json
  (produced by fetch_option_positions_context.py), which includes open_positions_min with record_id.
- Close rule: status=open AND (as_of >= expiration + grace_days).
  expiration is taken from table field `expiration` (ms) when available, else from note `exp=YYYY-MM-DD`.
- Safety:
  - Default apply (per user requirement) but can run --dry-run.
  - max-close-per-run guardrail.
  - Skip and report positions missing expiration.

This is *table maintenance*, not trade execution.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    data = None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
    res = http_json("POST", url, {"app_id": app_id, "app_secret": app_secret})
    if res.get("code") != 0:
        raise RuntimeError(f"feishu auth failed: {res}")
    return res["tenant_access_token"]


def bitable_update_record(tenant_token: str, app_token: str, table_id: str, record_id: str, fields: dict) -> dict:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    res = http_json("PUT", url, {"fields": fields}, headers=headers)
    if res.get("code") != 0:
        raise RuntimeError(f"bitable update record failed: {res}")
    return res.get("data") or {}


def parse_note_kv(note: str, key: str) -> str:
    if not note:
        return ''
    s = str(note)
    for part in s.replace(',', ';').split(';'):
        part = part.strip()
        if not part:
            continue
        if part.startswith(key + '='):
            return part.split('=', 1)[1].strip()
    return ''


def merge_note(note: str | None, kv: dict[str, str]) -> str:
    base = (note or '').strip()
    parts = []
    if base:
        parts.append(base)
    for k, v in kv.items():
        if v is None or v == '':
            continue
        parts.append(f"{k}={v}")
    return ';'.join(parts)


def parse_exp_to_ms(exp_ymd: str) -> int | None:
    try:
        y, m, d = map(int, exp_ymd.split('-'))
        return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None


@dataclass
class Decision:
    record_id: str
    position_id: str
    expiration_ms: int | None
    effective_exp_source: str
    should_close: bool
    reason: str


def main():
    ap = argparse.ArgumentParser(description='Auto-close expired option_positions (table maintenance)')
    ap.add_argument('--pm-config', default='../portfolio-management/config.json')
    ap.add_argument('--context', default='output/state/option_positions_context.json')
    ap.add_argument('--as-of-utc', default=None, help='ISO time; default now UTC')
    ap.add_argument('--grace-days', type=int, default=1)
    ap.add_argument('--max-close', type=int, default=20)
    ap.add_argument('--dry-run', action='store_true', help='do not write updates')
    ap.add_argument('--summary-out', default='output/reports/auto_close_summary.txt')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]

    ctx_path = Path(args.context)
    if not ctx_path.is_absolute():
        ctx_path = (base / ctx_path).resolve()
    ctx = json.loads(ctx_path.read_text(encoding='utf-8')) if ctx_path.exists() else {}

    positions = ctx.get('open_positions_min') or []

    # as_of
    if args.as_of_utc:
        as_of = datetime.fromisoformat(args.as_of_utc)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        else:
            as_of = as_of.astimezone(timezone.utc)
    else:
        as_of = datetime.now(timezone.utc)

    cutoff = as_of - timedelta(days=int(args.grace_days))

    decisions: list[Decision] = []
    for p in positions:
        rid = (p.get('record_id') or '').strip()
        pid = (p.get('position_id') or '').strip() or '(no position_id)'
        if not rid:
            decisions.append(Decision('', pid, None, 'none', False, 'missing record_id'))
            continue

        exp_ms = p.get('expiration')
        exp_src = 'expiration'
        exp_dt = None
        if exp_ms not in (None, '', 0):
            try:
                exp_dt = datetime.fromtimestamp(int(exp_ms)/1000, tz=timezone.utc)
            except Exception:
                exp_dt = None
        if exp_dt is None:
            exp_ymd = parse_note_kv(p.get('note') or '', 'exp')
            exp_ms2 = parse_exp_to_ms(exp_ymd) if exp_ymd else None
            if exp_ms2 is not None:
                exp_ms = exp_ms2
                exp_src = 'note.exp'
                exp_dt = datetime.fromtimestamp(int(exp_ms)/1000, tz=timezone.utc)

        if exp_dt is None:
            decisions.append(Decision(rid, pid, None, 'none', False, 'missing expiration (field and note)'))
            continue

        should_close = exp_dt <= cutoff
        reason = f"expired: exp={exp_dt.date().isoformat()} grace_days={args.grace_days} as_of={as_of.date().isoformat()}"
        decisions.append(Decision(rid, pid, int(exp_ms), exp_src, should_close, reason))

    to_close = [d for d in decisions if d.should_close and d.record_id]
    skipped = [d for d in decisions if (not d.should_close) or (not d.record_id)]

    # guardrail
    applied: list[Decision] = []
    errors: list[str] = []

    if len(to_close) > int(args.max_close):
        errors.append(f"too many to close: {len(to_close)} > max_close={args.max_close}; abort")
        to_apply: list[Decision] = []
    else:
        to_apply = to_close

    # Apply updates
    if to_apply and not args.dry_run and not errors:
        pm_config = Path(args.pm_config)
        if not pm_config.is_absolute():
            pm_config = (base / pm_config).resolve()
        cfg = json.loads(pm_config.read_text(encoding='utf-8'))
        feishu_cfg = cfg.get('feishu', {}) or {}
        app_id = feishu_cfg.get('app_id')
        app_secret = feishu_cfg.get('app_secret')
        ref = (feishu_cfg.get('tables', {}) or {}).get('option_positions')
        if not (app_id and app_secret and ref and '/' in ref):
            raise SystemExit('pm config missing feishu app_id/app_secret/option_positions')
        app_token, table_id = ref.split('/', 1)

        token = get_tenant_access_token(app_id, app_secret)

        for d in to_apply:
            try:
                now_iso = as_of.isoformat()
                patch = {
                    'status': 'close',
                    'note': merge_note(None, {}),
                }
                # Keep existing note by setting note to existing+append.
                # We don't have full existing note here reliably; use the note from context.
                # (Context is produced by the same scan, so it's up-to-date enough.)
                existing_note = None
                for p in positions:
                    if (p.get('record_id') or '').strip() == d.record_id:
                        existing_note = p.get('note')
                        break
                patch['note'] = merge_note(existing_note, {
                    'auto_close_at': now_iso,
                    'auto_close_reason': 'expired',
                    'auto_close_grace_days': str(args.grace_days),
                })
                bitable_update_record(token, app_token, table_id, d.record_id, patch)
                applied.append(d)
            except Exception as e:
                errors.append(f"{d.record_id} {d.position_id}: {e}")

    # Summary
    lines: list[str] = []
    lines.append(f"Auto-close expired positions (grace_days={args.grace_days})")
    lines.append(f"as_of_utc: {as_of.isoformat()}")
    lines.append(f"context: {ctx_path}")
    lines.append("")

    if errors:
        lines.append(f"ERRORS: {len(errors)}")
        for e in errors[:20]:
            lines.append(f"- {e}")
        lines.append("")

    if args.dry_run:
        lines.append(f"MODE: DRY_RUN")
    else:
        lines.append(f"MODE: APPLY")

    lines.append(f"candidates_should_close: {len(to_close)}")
    lines.append(f"applied_closed: {len(applied)}")
    lines.append(f"skipped_or_not_due: {len(decisions) - len(to_close)}")
    lines.append("")

    if to_close:
        lines.append("Closed / To close list:")
        for d in (applied if (applied and not args.dry_run) else to_close)[:50]:
            lines.append(f"- {d.record_id} | {d.position_id} | exp_src={d.effective_exp_source} | exp_ms={d.expiration_ms}")
        lines.append("")

    # skipped highlights
    missing_exp = [d for d in decisions if 'missing expiration' in d.reason]
    missing_rid = [d for d in decisions if d.reason == 'missing record_id']
    if missing_exp or missing_rid:
        lines.append("Skipped:")
        if missing_rid:
            lines.append(f"- missing record_id: {len(missing_rid)}")
        if missing_exp:
            lines.append(f"- missing expiration (field and note): {len(missing_exp)}")

    summary_out = Path(args.summary_out)
    if not summary_out.is_absolute():
        summary_out = (base / summary_out).resolve()
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text("\n".join(lines).strip() + "\n", encoding='utf-8')

    print(f"[DONE] auto_close summary -> {summary_out}")
    print(f"should_close={len(to_close)} applied={len(applied)} errors={len(errors)} dry_run={bool(args.dry_run)}")


if __name__ == '__main__':
    main()
