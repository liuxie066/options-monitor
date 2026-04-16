from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.close_advice import (
    CloseAdviceConfig,
    CloseAdviceInput,
    evaluate_close_advice,
    safe_float,
    safe_int,
    sort_advice_rows,
)
from scripts.fee_calc import calc_futu_option_fee
from scripts.io_utils import atomic_write_text, read_json, safe_read_csv


OUTPUT_COLUMNS = [
    "account",
    "symbol",
    "option_type",
    "expiration",
    "strike",
    "contracts_open",
    "premium",
    "close_mid",
    "bid",
    "ask",
    "dte",
    "multiplier",
    "capture_ratio",
    "remaining_premium",
    "realized_if_close",
    "remaining_annualized_return",
    "tier",
    "reason",
    "data_quality_flags",
]


def _norm_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _norm_option_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _date_from_ms(value: Any) -> str | None:
    try:
        if value in (None, ""):
            return None
        n = int(float(value))
        if n <= 0:
            return None
        if n > 10_000_000_000:
            return datetime.fromtimestamp(n / 1000, tz=timezone.utc).date().isoformat()
        return datetime.fromtimestamp(n, tz=timezone.utc).date().isoformat()
    except Exception:
        return None


def normalize_expiration(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
            return s[:10]
        parsed = _date_from_ms(s)
        return parsed or s
    return _date_from_ms(value)


def _strike_key(value: Any) -> str:
    v = safe_float(value)
    if v is None:
        return ""
    return f"{v:.6f}"


def _quote_key(symbol: Any, option_type: Any, expiration: Any, strike: Any) -> tuple[str, str, str, str]:
    return (
        _norm_symbol(symbol),
        _norm_option_type(option_type),
        normalize_expiration(expiration) or "",
        _strike_key(strike),
    )


def load_required_data_quotes(required_data_root: Path, symbols: set[str] | None = None) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    root = Path(required_data_root)
    parsed = root / "parsed"
    quotes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if not parsed.exists():
        return quotes

    for path in sorted(parsed.glob("*_required_data.csv")):
        sym_from_name = path.name.removesuffix("_required_data.csv").upper()
        if symbols and sym_from_name not in symbols:
            continue
        df = safe_read_csv(path)
        if df.empty:
            continue
        for _, row0 in df.iterrows():
            row = row0.to_dict()
            key = _quote_key(
                row.get("symbol") or sym_from_name,
                row.get("option_type"),
                row.get("expiration"),
                row.get("strike"),
            )
            if not all(key):
                continue
            quotes[key] = row
    return quotes


def _symbol_config_by_symbol(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = config.get("symbols") if isinstance(config, dict) else []
    if not isinstance(items, list):
        items = config.get("watchlist") if isinstance(config, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sym = _norm_symbol(item.get("symbol"))
        if sym:
            out[sym] = item
    return out


def _merge_quote_rows(quotes: dict[tuple[str, str, str, str], dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = _quote_key(row.get("symbol"), row.get("option_type"), row.get("expiration"), row.get("strike"))
        if all(key):
            quotes[key] = row


def _fetch_missing_quotes_via_opend(
    *,
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    quotes: dict[tuple[str, str, str, str], dict[str, Any]],
    base_dir: Path,
) -> None:
    advice_cfg = config.get("close_advice") if isinstance(config, dict) else {}
    if isinstance(advice_cfg, dict) and str(advice_cfg.get("quote_source") or "auto").strip().lower() == "required_data":
        return

    symbol_cfgs = _symbol_config_by_symbol(config)
    missing_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"))
        if all(key) and key not in quotes:
            missing_by_symbol.setdefault(key[0], []).append(pos)

    if not missing_by_symbol:
        return

    try:
        from scripts.fetch_market_data_opend import fetch_symbol
    except Exception:
        return

    for symbol, missing_positions in missing_by_symbol.items():
        symbol_cfg = symbol_cfgs.get(symbol) or {}
        fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg, dict) else {}
        fetch_cfg = fetch_cfg if isinstance(fetch_cfg, dict) else {}
        if str(fetch_cfg.get("source") or "").strip().lower() != "opend":
            continue
        strikes = [safe_float(p.get("strike")) for p in missing_positions]
        strikes = [s for s in strikes if s is not None]
        if not strikes:
            continue
        option_types = sorted({_norm_option_type(p.get("option_type")) for p in missing_positions if p.get("option_type")})
        try:
            payload = fetch_symbol(
                symbol,
                limit_expirations=safe_int(fetch_cfg.get("limit_expirations")) or 8,
                host=str(fetch_cfg.get("host") or "127.0.0.1"),
                port=safe_int(fetch_cfg.get("port")) or 11111,
                spot_from_pm=bool(fetch_cfg.get("spot_from_portfolio_management")),
                base_dir=base_dir,
                option_types=",".join(option_types or ["put", "call"]),
                min_strike=min(strikes),
                max_strike=max(strikes),
                chain_cache=True,
            )
        except Exception:
            continue
        rows = payload.get("rows") if isinstance(payload, dict) else []
        _merge_quote_rows(quotes, rows if isinstance(rows, list) else [])


def _position_expiration(pos: dict[str, Any]) -> str | None:
    exp = normalize_expiration(pos.get("expiration"))
    if exp:
        return exp
    note = str(pos.get("note") or "")
    for token in note.replace(";", " ").split():
        if token.startswith("exp="):
            return normalize_expiration(token.split("=", 1)[1])
    return None


def _position_premium(pos: dict[str, Any]) -> float | None:
    premium = safe_float(pos.get("premium"))
    if premium is not None:
        return premium
    note = str(pos.get("note") or "")
    for token in note.replace(";", " ").split():
        if token.startswith("premium_per_share="):
            return safe_float(token.split("=", 1)[1])
    return None


def _calc_dte(expiration: str | None, quote: dict[str, Any] | None) -> int | None:
    q_dte = safe_int((quote or {}).get("dte"))
    if q_dte is not None:
        return q_dte
    if not expiration:
        return None
    try:
        exp_date = datetime.strptime(expiration[:10], "%Y-%m-%d").date()
        return (exp_date - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


def _mid_from_quote(quote: dict[str, Any] | None) -> tuple[float | None, list[str]]:
    if not isinstance(quote, dict):
        return None, ["missing_quote"]
    mid = safe_float(quote.get("mid"))
    if mid is not None:
        return mid, []
    last_price = safe_float(quote.get("last_price"))
    if last_price is not None:
        return last_price, ["mid_fallback_last_price"]
    return None, ["missing_mid"]


def _position_to_input(pos: dict[str, Any], quote: dict[str, Any] | None) -> tuple[CloseAdviceInput, list[str]]:
    expiration = _position_expiration(pos)
    mid, quote_flags = _mid_from_quote(quote)
    return (
        CloseAdviceInput(
            account=str(pos.get("account") or "").strip().lower(),
            symbol=_norm_symbol(pos.get("symbol")),
            option_type=_norm_option_type(pos.get("option_type")),
            side=str(pos.get("side") or "").strip().lower(),
            expiration=expiration,
            strike=safe_float(pos.get("strike")),
            contracts_open=safe_int(pos.get("contracts_open")),
            premium=_position_premium(pos),
            close_mid=mid,
            bid=safe_float((quote or {}).get("bid")),
            ask=safe_float((quote or {}).get("ask")),
            dte=_calc_dte(expiration, quote),
            multiplier=safe_float(pos.get("multiplier")) or safe_float((quote or {}).get("multiplier")),
            spot=safe_float((quote or {}).get("spot")),
            currency=str(pos.get("currency") or (quote or {}).get("currency") or "").strip().upper(),
        ),
        quote_flags,
    )


def _apply_buy_to_close_fee(row: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    mid = safe_float(row.get("close_mid"))
    contracts = safe_int(row.get("contracts_open")) or 1
    if mid is None:
        return row
    try:
        fee = calc_futu_option_fee(row.get("currency"), mid, contracts=contracts, is_sell=False, base_dir=base_dir)
    except Exception:
        fee = 0.0
    realized = safe_float(row.get("realized_if_close"))
    if realized is not None:
        row["realized_if_close"] = realized - float(fee)
    row["buy_to_close_fee"] = float(fee)
    return row


def _with_extra_flags(row: dict[str, Any], flags: list[str]) -> dict[str, Any]:
    cur = [x for x in str(row.get("data_quality_flags") or "").split(";") if x]
    for flag in flags:
        if flag and flag not in cur:
            cur.append(flag)
    row["data_quality_flags"] = ";".join(cur)
    return row


def _money(value: Any, currency: Any) -> str:
    v = safe_float(value)
    if v is None:
        return "-"
    ccy = str(currency or "").strip().upper()
    prefix = "$" if ccy == "USD" else ("HK$" if ccy == "HKD" else "")
    if prefix:
        return f"{prefix}{v:,.0f}"
    return f"{v:,.0f} {ccy}".strip()


def _pct(value: Any) -> str:
    v = safe_float(value)
    if v is None:
        return "-"
    return f"{v * 100:.1f}%"


def _num(value: Any) -> str:
    v = safe_float(value)
    if v is None:
        return "-"
    return f"{v:.2f}"


def render_markdown(rows: list[dict[str, Any]], *, notify_levels: set[str], max_items: int) -> str:
    selected = [r for r in sort_advice_rows(rows) if str(r.get("tier") or "") in notify_levels]
    if max_items > 0:
        selected = selected[:max_items]
    if not selected:
        return ""

    acct = str(selected[0].get("account") or "当前账户").strip().lower()
    lines = [f"### [{acct}] 平仓建议"]
    for row in selected:
        opt = "Put" if str(row.get("option_type")) == "put" else "Call"
        exp = row.get("expiration") or "-"
        strike = _num(row.get("strike"))
        suffix = "P" if opt == "Put" else "C"
        currency = row.get("currency")
        lines.extend(
            [
                f"- {row.get('symbol')} {opt} {exp} {strike}{suffix} · {row.get('tier_label')}",
                (
                    f"- 已锁定: {_pct(row.get('capture_ratio'))} | "
                    f"剩余DTE={row.get('dte') if row.get('dte') is not None else '-'} | "
                    f"剩余收益年化={_pct(row.get('remaining_annualized_return'))}"
                ),
                f"- 价格: 开仓权利金={_num(row.get('premium'))} | 平仓 mid={_num(row.get('close_mid'))}",
                (
                    f"- 估算: 平仓后锁定收益 {_money(row.get('realized_if_close'), currency)} | "
                    f"剩余权利金 {_money(row.get('remaining_premium'), currency)}"
                ),
                f"- 理由: {row.get('reason') or '-'}",
                "---",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from io import StringIO

    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    atomic_write_text(path, buf.getvalue(), encoding="utf-8")


def _load_context(context_path: Path) -> dict[str, Any]:
    obj = read_json(context_path, default={})
    return obj if isinstance(obj, dict) else {}


def run_close_advice(
    *,
    config: dict[str, Any],
    context_path: Path,
    required_data_root: Path,
    output_dir: Path,
    base_dir: Path,
) -> dict[str, Any]:
    advice_cfg_raw = config.get("close_advice") if isinstance(config, dict) else {}
    advice_cfg = advice_cfg_raw if isinstance(advice_cfg_raw, dict) else {}
    output_dir = Path(output_dir).resolve()
    csv_path = output_dir / "close_advice.csv"
    text_path = output_dir / "close_advice.txt"

    if not bool(advice_cfg.get("enabled", False)):
        _write_csv(csv_path, [])
        atomic_write_text(text_path, "", encoding="utf-8")
        return {"enabled": False, "rows": 0, "notify_rows": 0, "csv": str(csv_path), "text": str(text_path)}

    ctx = _load_context(context_path)
    positions = ctx.get("open_positions_min") if isinstance(ctx, dict) else []
    positions = positions if isinstance(positions, list) else []
    symbols = {_norm_symbol(p.get("symbol")) for p in positions if isinstance(p, dict) and p.get("symbol")}
    quotes = load_required_data_quotes(Path(required_data_root), symbols=symbols)
    _fetch_missing_quotes_via_opend(
        config=config,
        positions=positions,
        quotes=quotes,
        base_dir=Path(base_dir),
    )

    cfg = CloseAdviceConfig.from_mapping(advice_cfg)
    rows: list[dict[str, Any]] = []
    for pos0 in positions:
        if not isinstance(pos0, dict):
            continue
        exp = _position_expiration(pos0)
        key = _quote_key(pos0.get("symbol"), pos0.get("option_type"), exp, pos0.get("strike"))
        quote = quotes.get(key)
        inp, quote_flags = _position_to_input(pos0, quote)
        row = evaluate_close_advice(inp, cfg)
        row = _with_extra_flags(row, quote_flags)
        row = _apply_buy_to_close_fee(row, base_dir=Path(base_dir))
        rows.append(row)

    rows = sort_advice_rows(rows)
    notify_levels = advice_cfg.get("notify_levels") or ["strong", "medium"]
    notify_level_set = {str(x).strip().lower() for x in notify_levels if str(x).strip()}
    max_items = safe_int(advice_cfg.get("max_items_per_account")) or 5
    text = render_markdown(rows, notify_levels=notify_level_set, max_items=max_items)

    _write_csv(csv_path, rows)
    atomic_write_text(text_path, text, encoding="utf-8")

    return {
        "enabled": True,
        "rows": len(rows),
        "notify_rows": len([r for r in rows if str(r.get("tier") or "") in notify_level_set]),
        "csv": str(csv_path),
        "text": str(text_path),
    }


def load_config(path: Path) -> dict[str, Any]:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    return obj if isinstance(obj, dict) else {}


def run_from_paths(
    *,
    config_path: Path,
    context_path: Path,
    required_data_root: Path,
    output_dir: Path,
    base_dir: Path,
) -> dict[str, Any]:
    return run_close_advice(
        config=load_config(config_path),
        context_path=context_path,
        required_data_root=required_data_root,
        output_dir=output_dir,
        base_dir=base_dir,
    )
