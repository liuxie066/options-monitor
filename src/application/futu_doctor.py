from __future__ import annotations

"""Futu/OpenD doctor checks for application and agent runtime."""

import importlib.util
import json
from dataclasses import asdict, dataclass
from typing import Any

from src.application.opend_utils import normalize_underlier
from src.infrastructure.opend_watchdog import port_open, run_watchdog_check


REQUIRED_SNAPSHOT_COLS = [
    "code",
    "last_price",
    "bid_price",
    "ask_price",
    "volume",
    "option_open_interest",
    "option_implied_volatility",
    "option_delta",
    "option_contract_multiplier",
]


@dataclass(frozen=True)
class SymbolFieldResult:
    symbol: str
    underlier_code: str | None
    ok: bool
    chain_rows: int = 0
    snap_rows: int = 0
    missing_snapshot_cols: list[str] | None = None
    spot: float | None = None
    note: str | None = None
    error: str | None = None


def sdk_status() -> dict[str, Any]:
    futu_found = importlib.util.find_spec("futu") is not None
    return {
        "futu_sdk_importable": futu_found,
        "ok": bool(futu_found),
    }


def telnet_status(*, host: str = "127.0.0.1", port: int = 22222) -> dict[str, Any]:
    open_ok = port_open(str(host), int(port), timeout=0.8)
    return {
        "host": str(host),
        "port": int(port),
        "listening": bool(open_ok),
        "ok": bool(open_ok),
        "message": (
            "OpenD Telnet is listening"
            if open_ok
            else "OpenD Telnet is not listening; set telnet_ip=127.0.0.1 and telnet_port=22222 in FutuOpenD.xml"
        ),
    }


def check_required_option_fields(
    *,
    symbols: list[str],
    host: str,
    port: int,
    limit: int = 10,
) -> dict[str, Any]:
    from futu import OpenQuoteContext, RET_OK

    results: list[SymbolFieldResult] = []

    for sym in symbols:
        underlier = None
        ctx = None
        try:
            ctx = OpenQuoteContext(host=host, port=int(port))
            underlier = normalize_underlier(sym)
            ret, chain = ctx.get_option_chain(underlier.code)
            if ret != RET_OK or chain is None or chain.empty:
                results.append(
                    SymbolFieldResult(
                        symbol=sym,
                        underlier_code=underlier.code,
                        ok=False,
                        error=f"get_option_chain ret={ret} empty",
                    )
                )
                continue

            codes = [str(x) for x in chain["code"].astype(str).head(int(limit)).tolist() if x]
            ret2, snap = ctx.get_market_snapshot(codes)
            if ret2 != RET_OK or snap is None or snap.empty:
                results.append(
                    SymbolFieldResult(
                        symbol=sym,
                        underlier_code=underlier.code,
                        ok=False,
                        chain_rows=int(len(chain)),
                        error=f"get_market_snapshot ret={ret2} empty",
                    )
                )
                continue

            missing = [col for col in REQUIRED_SNAPSHOT_COLS if col not in snap.columns]
            spot = None
            try:
                if underlier.market != "US":
                    ret3, spot_df = ctx.get_market_snapshot([underlier.code])
                    if ret3 == RET_OK and spot_df is not None and not spot_df.empty:
                        raw_spot = spot_df.iloc[0].get("last_price")
                        spot = float(raw_spot) if raw_spot is not None else None
            except Exception:
                spot = None

            note = None
            if spot is None and underlier.market != "US":
                note = "spot missing via OpenD snapshot; consider spot override/fallback"
            if underlier.market == "US":
                note = "US spot is not required from OpenD (often no quote right); use spot override/fallback if needed"

            results.append(
                SymbolFieldResult(
                    symbol=sym,
                    underlier_code=underlier.code,
                    ok=(len(missing) == 0),
                    chain_rows=int(len(chain)),
                    snap_rows=int(len(snap)),
                    missing_snapshot_cols=missing,
                    spot=spot,
                    note=note,
                )
            )
        except Exception as exc:
            results.append(
                SymbolFieldResult(
                    symbol=sym,
                    underlier_code=(underlier.code if underlier else None),
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass

    return {
        "host": str(host),
        "port": int(port),
        "results": [asdict(row) for row in results],
    }


def required_fields_ok(required_fields: dict[str, Any] | None, *, symbols: list[str]) -> bool:
    if not symbols:
        return True
    if not isinstance(required_fields, dict):
        return False
    rows = required_fields.get("results") if isinstance(required_fields.get("results"), list) else []
    if not rows:
        return False
    return all(bool(isinstance(row, dict) and row.get("ok")) for row in rows)


def run_futu_doctor_checks(
    *,
    host: str,
    port: int,
    telnet_host: str = "127.0.0.1",
    telnet_port: int = 22222,
    symbols: list[str] | None = None,
    ensure: bool = False,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    del timeout_sec
    symbol_list = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    sdk = sdk_status()

    watchdog = run_watchdog_check(host=str(host), port=int(port), ensure=bool(ensure)).to_payload()
    watchdog_ok = bool(watchdog.get("ok"))
    telnet = telnet_status(host=str(telnet_host), port=int(telnet_port))

    required_fields = None
    required_fields_raw = ""
    if bool(sdk.get("ok")) and watchdog_ok and symbol_list:
        try:
            required_fields = check_required_option_fields(
                symbols=symbol_list,
                host=str(host),
                port=int(port),
            )
        except Exception as exc:
            required_fields_raw = f"{type(exc).__name__}: {exc}"
            required_fields = {
                "host": str(host),
                "port": int(port),
                "results": [
                    {
                        "symbol": symbol,
                        "underlier_code": None,
                        "ok": False,
                        "error": required_fields_raw,
                    }
                    for symbol in symbol_list
                ],
            }
    fields_ok = required_fields_ok(required_fields, symbols=symbol_list)
    ok = bool(sdk.get("ok")) and watchdog_ok and fields_ok

    return {
        "ok": ok,
        "host": str(host),
        "port": int(port),
        "telnet_host": str(telnet_host),
        "telnet_port": int(telnet_port),
        "source": "futu",
        "sdk": sdk,
        "telnet": telnet,
        "watchdog_ok": watchdog_ok,
        "watchdog_returncode": (0 if watchdog_ok else 2),
        "watchdog": watchdog,
        "watchdog_raw": json.dumps(watchdog, ensure_ascii=False),
        "required_fields_ok": fields_ok,
        "required_fields_returncode": (0 if fields_ok else 2),
        "required_fields": required_fields,
        "required_fields_raw": required_fields_raw,
    }


def build_human_text(result: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Futu Data Source Doctor",
        "",
        f"endpoint: {result.get('host')}:{result.get('port')}",
        "",
    ]

    sdk = result.get("sdk") if isinstance(result.get("sdk"), dict) else {}
    lines.append("[OK] SDK importable: futu" if sdk.get("ok") else "[FAIL] SDK not importable: install futu-api")

    wd = result.get("watchdog") if isinstance(result.get("watchdog"), dict) else {}
    if wd.get("ok"):
        lines.append("[OK] Futu/OpenD gateway healthy")
    else:
        code = wd.get("error_code") or "FUTU_GATEWAY_UNHEALTHY"
        msg = wd.get("message") or wd.get("error") or result.get("watchdog_raw") or "unknown error"
        lines.append(f"[FAIL] Futu/OpenD gateway unhealthy: {code}: {msg}")
        action = wd.get("action_taken")
        if action:
            lines.append(f"  action: {action}")

    telnet = result.get("telnet") if isinstance(result.get("telnet"), dict) else {}
    if telnet:
        endpoint = f"{telnet.get('host')}:{telnet.get('port')}"
        if telnet.get("ok"):
            lines.append(f"[OK] OpenD Telnet listening: {endpoint}")
        else:
            lines.append(f"[WARN] OpenD Telnet unavailable: {endpoint}")
            lines.append("  enable telnet_ip=127.0.0.1 and telnet_port=22222 in FutuOpenD.xml, then use Telnet to submit phone verification codes.")

    fields = result.get("required_fields") if isinstance(result.get("required_fields"), dict) else None
    if fields is not None:
        rows = fields.get("results") if isinstance(fields.get("results"), list) else []
        if not rows:
            lines.append("[WARN] Symbol field check returned no rows")
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = "OK" if row.get("ok") else "FAIL"
            lines.append(
                f"[{status}] {row.get('symbol')} underlier={row.get('underlier_code')} "
                f"chain={row.get('chain_rows')} snap={row.get('snap_rows')} spot={row.get('spot')}"
            )
            missing = row.get("missing_snapshot_cols") if isinstance(row.get("missing_snapshot_cols"), list) else []
            if missing:
                lines.append(f"  missing: {', '.join(str(x) for x in missing)}")
            if row.get("note"):
                lines.append(f"  note: {row.get('note')}")
            if row.get("error"):
                lines.append(f"  error: {row.get('error')}")

    lines.append("")
    if result.get("ok"):
        lines.append('[OK] 富途数据源可用。配置里可使用 fetch.source = "futu"。')
    else:
        lines.append("[FAIL] 富途数据源尚不可用。请按上面的失败项处理后重试。")
    return "\n".join(lines)


__all__ = [
    "REQUIRED_SNAPSHOT_COLS",
    "SymbolFieldResult",
    "build_human_text",
    "check_required_option_fields",
    "required_fields_ok",
    "run_futu_doctor_checks",
    "sdk_status",
    "telnet_status",
]
