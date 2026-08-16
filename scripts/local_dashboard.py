from __future__ import annotations

import html
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs


REPO_ROOT = Path(os.environ.get("OM_DASHBOARD_REPO_ROOT") or Path(__file__).resolve().parents[1]).expanduser().resolve()
MAX_OUTPUT_CHARS = 2_000_000
XUEQIU_COOKIE_ENV = "XUEQIU_COOKIE"
ROBINHOOD_TOKEN_ENV = "ROBINHOOD_AUTH_TOKEN"
DEFAULT_BLOGGER_URL = "https://xueqiu.com/u/1247347556#/stock"


def _dashboard_env_file() -> Path:
    raw = (
        os.environ.get("OM_DASHBOARD_ENV_FILE")
        or os.environ.get("OM_ENV_FILE")
        or "~/Library/Application Support/options-monitor/options-monitor.env"
    )
    return Path(raw).expanduser().resolve()


def _run(command: list[str], *, timeout: int = 180, env_overlay: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ)
    env["OM_ENV_FILE"] = str(_dashboard_env_file())
    if env_overlay:
        env.update(env_overlay)
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": f"Command timed out after {timeout} seconds.",
            "command": command,
        }
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-MAX_OUTPUT_CHARS:],
        "stderr": completed.stderr[-MAX_OUTPUT_CHARS:],
        "command": command,
    }


def _safe_symbol_list(raw: str) -> list[str]:
    return [item.strip().upper() for item in raw.replace("\n", ",").split(",") if item.strip()]


def _safe_cube_list(raw: str) -> list[str]:
    return [item.strip().upper() for item in raw.replace("\n", ",").split(",") if item.strip()]


def _env_file_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        values[key] = value
    return values


def _quote_env_value(value: str) -> str:
    escaped = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _save_env_setting(path: Path, key: str, value: str) -> dict[str, Any]:
    if not value.strip():
        label = {
            XUEQIU_COOKIE_ENV: "Xueqiu Cookie",
            ROBINHOOD_TOKEN_ENV: "Robinhood token",
        }.get(key, key)
        return {
            "ok": False,
            "message": f"{label} field is empty. Paste the value into the password box before saving.",
            "command": [],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        existing = [
            "# options-monitor local settings",
            "# Created by the local dashboard.",
        ]

    replacement = f"{key}={_quote_env_value(value)}"
    out: list[str] = []
    replaced = False
    for line in existing:
        stripped = line.strip()
        candidate = stripped[len("export ") :].strip() if stripped.startswith("export ") else stripped
        if candidate.startswith(f"{key}="):
            if not replaced:
                out.append(replacement)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(replacement)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    os.environ["OM_ENV_FILE"] = str(path)
    os.environ[key] = value
    return {
        "ok": True,
        "message": f"{key} saved locally.",
        "stdout": f"{key}=<saved locally>\nEnv file: {path}",
        "command": [],
    }


def _xueqiu_cookie_status() -> dict[str, Any]:
    env_file = _dashboard_env_file()
    values = _env_file_values(env_file)
    cookie = os.environ.get(XUEQIU_COOKIE_ENV) or values.get(XUEQIU_COOKIE_ENV) or ""
    return {
        "env_file": env_file,
        "env_file_exists": env_file.exists(),
        "configured": bool(cookie),
        "length": len(cookie),
    }


def _robinhood_token_status() -> dict[str, Any]:
    env_file = _dashboard_env_file()
    values = _env_file_values(env_file)
    token = os.environ.get(ROBINHOOD_TOKEN_ENV) or values.get(ROBINHOOD_TOKEN_ENV) or ""
    return {
        "env_file": env_file,
        "env_file_exists": env_file.exists(),
        "configured": bool(token),
        "length": len(token),
    }


def _render_output(result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    parsed = _result_payload(result)
    if parsed and parsed.get("schema_version") == "options_data_blogger_opportunities.v1":
        return _render_recommendations(result, parsed)
    command = " ".join(result.get("command") or [])
    status = "OK" if result.get("ok") else "Needs attention"
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    message = str(result.get("message") or "")
    return f"""
      <section class="output">
        <div class="status {html.escape(status.lower().replace(" ", "-"))}">{html.escape(status)}</div>
        {f'<div class="command">{html.escape(command)}</div>' if command else ""}
        <pre>{html.escape(stdout or stderr or message or "(no output)")}</pre>
        {f'<pre class="stderr">{html.escape(stderr)}</pre>' if stdout and stderr else ""}
      </section>
    """


def _result_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    text = str(result.get("stdout") or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _render_recommendations(result: dict[str, Any], payload: dict[str, Any]) -> str:
    status = "OK" if result.get("ok") and payload.get("ok") else "Needs attention"
    rows = payload.get("opportunities") if isinstance(payload.get("opportunities"), list) else []
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    summary = f"""
      <div class="summary">
        <div><strong>{html.escape(str(payload.get("us_stock_count") or 0))}</strong><span>US holdings</span></div>
        <div><strong>{html.escape(str(payload.get("quote_count") or 0))}</strong><span>quotes loaded</span></div>
        <div><strong>{html.escape(str(payload.get("returned_count") or payload.get("opportunity_count") or 0))}</strong><span>rows shown / {html.escape(str(payload.get("evaluated_count") or 0))} evaluated</span></div>
      </div>
    """
    if rows:
        body = "".join(_recommendation_row(row) for row in rows if isinstance(row, dict))
        table = f"""
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th class="decision-col">Decision</th>
                  <th>Score</th>
                  <th>Strategy</th>
                  <th>Stock</th>
                  <th>Contract</th>
                  <th>Price</th>
                  <th>Strike</th>
                  <th>Basis</th>
                  <th>Distance</th>
                  <th>IV</th>
                  <th>Mid</th>
                  <th>Yield</th>
                  <th>Liquidity</th>
                </tr>
              </thead>
              <tbody>{body}</tbody>
            </table>
          </div>
        """
    else:
        table = '<p class="empty">No candidates returned. Check Settings, reduce filters, or try again later.</p>'
    error_html = ""
    if errors:
        compact = html.escape(json.dumps(errors[:5], ensure_ascii=False, indent=2))
        error_html = f"<details class=\"debug\"><summary>Warnings</summary><pre>{compact}</pre></details>"
    truncation_html = ""
    if payload.get("truncated"):
        truncation_html = '<p class="note">Result set was capped for dashboard readability. Rows shown are still ranked by annualized yield from high to low.</p>'
    raw = html.escape(str(result.get("stdout") or result.get("stderr") or ""))
    return f"""
      <section class="output">
        <div class="status {html.escape(status.lower().replace(" ", "-"))}">{html.escape(status)}</div>
        {summary}
        {truncation_html}
        {table}
        {error_html}
        <details class="debug"><summary>Raw output</summary><pre>{raw}</pre></details>
      </section>
    """


def _recommendation_row(row: dict[str, Any]) -> str:
    strategy = "Sell Put" if row.get("strategy") == "sell_put" else "Covered Call"
    liquidity = f"Vol { _fmt_int(row.get('volume')) } / OI { _fmt_int(row.get('open_interest')) }"
    vetoes = row.get("hard_vetoes") if isinstance(row.get("hard_vetoes"), list) else []
    warnings = row.get("warnings") if isinstance(row.get("warnings"), list) else []
    risk_note = ", ".join(str(item) for item in (vetoes or warnings)[:2])
    return f"""
      <tr>
        <td class="decision-col"><strong>{html.escape(str(row.get("final_decision") or ""))}</strong><span>{html.escape(str(row.get("mature_band") or ""))}</span></td>
        <td>{html.escape(str(row.get("mature_score") or "-"))}<span>{html.escape(str(row.get("original_framework", {}).get("verdict") if isinstance(row.get("original_framework"), dict) else "-"))}</span></td>
        <td>{html.escape(strategy)}</td>
        <td><strong>{html.escape(str(row.get("symbol") or ""))}</strong><span>{html.escape(str(row.get("name") or ""))}</span></td>
        <td>{html.escape(str(row.get("contract_symbol") or ""))}<span>{html.escape(str(row.get("expiration") or ""))}</span></td>
        <td>{_fmt_money(row.get("underlying_price"))}</td>
        <td>{_fmt_money(row.get("strike"))}</td>
        <td>{_fmt_money(row.get("effective_basis"))}<span>target {_fmt_money(row.get("authorized_target_basis"))}</span></td>
        <td>{_fmt_pct(row.get("out_of_money_pct"))}<span>{html.escape(str(row.get("dte") or "-"))} DTE</span></td>
        <td>{_fmt_pct(row.get("iv"))}<span>delta {html.escape(str(row.get("delta") if row.get("delta") is not None else "-"))}</span></td>
        <td>{_fmt_money(row.get("mid"))}<span>{_fmt_money(row.get("bid_ask_spread"))} spread</span></td>
        <td>{_fmt_pct(row.get("annualized_return_on_cash"))}<span>{_fmt_pct(row.get("effective_discount_pct"))} basis discount</span></td>
        <td>{html.escape(liquidity)}<span>{html.escape(risk_note)}</span></td>
      </tr>
    """


def _fmt_money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:,.2f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "-"


def _page(result: dict[str, Any] | None = None) -> bytes:
    xueqiu_status = _xueqiu_cookie_status()
    robinhood_status = _robinhood_token_status()
    source_ready = bool(xueqiu_status["configured"] and robinhood_status["configured"])
    setup_note = "" if source_ready else '<p class="note">Set up Xueqiu Cookie and Robinhood token once in Settings before running recommendations.</p>'
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Options Monitor Local</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #111827;
      --muted: #5f6b7a;
      --line: #d8dee7;
      --bg: #f7f9fc;
      --panel: #ffffff;
      --accent: #155e75;
      --accent-2: #0f766e;
      --good: #14723c;
      --warn: #b45309;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    header {{
      padding: 24px 28px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{ margin: 0 0 6px; font-size: 28px; letter-spacing: 0; }}
    p {{ color: var(--muted); line-height: 1.5; margin: 0; }}
    nav {{ display: flex; gap: 14px; margin-top: 14px; }}
    nav a {{ color: var(--accent); font-weight: 650; text-decoration: none; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    h2 {{ margin: 0 0 10px; font-size: 20px; }}
    label {{ display: block; margin-top: 12px; font-size: 13px; color: var(--muted); }}
    input {{
      width: 100%;
      margin-top: 5px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font-size: 14px;
    }}
    button {{
      width: 100%;
      margin-top: 14px;
      padding: 12px 14px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: white;
      font-weight: 650;
      cursor: pointer;
    }}
    .pills {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 0; }}
    .pill {{ border-radius: 999px; padding: 6px 10px; font-size: 13px; border: 1px solid var(--line); }}
    .pill.good {{ color: var(--good); background: #ecfdf3; }}
    .pill.warn {{ color: var(--warn); background: #fff7ed; }}
    .row {{ display: grid; grid-template-columns: 1fr 180px; gap: 12px; }}
    .note {{ margin: 12px 0 0; padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; background: #f8fafc; color: var(--muted); font-size: 13px; line-height: 1.45; }}
    .output {{ margin-top: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    .status {{ padding: 10px 14px; font-weight: 700; background: #e0f2fe; }}
    .status.needs-attention {{ background: #ffedd5; }}
    .command {{ padding: 10px 14px; color: var(--muted); border-top: 1px solid var(--line); }}
    pre {{
      margin: 0;
      padding: 14px;
      overflow: auto;
      white-space: pre-wrap;
      border-top: 1px solid var(--line);
      max-height: 520px;
    }}
    pre.stderr {{ color: #991b1b; }}
    .summary {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--line); border-top: 1px solid var(--line); }}
    .summary div {{ background: #fff; padding: 14px; }}
    .summary strong {{ display: block; font-size: 22px; }}
    .summary span {{ color: var(--muted); font-size: 13px; }}
    .table-wrap {{ overflow-x: auto; border-top: 1px solid var(--line); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0; background: #f8fafc; }}
    td span {{ display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }}
    .decision-col {{ width: 72px; min-width: 72px; max-width: 72px; padding-left: 8px; padding-right: 8px; white-space: nowrap; }}
    td.decision-col strong {{ font-size: 12px; }}
    td.decision-col span {{ font-size: 11px; }}
    .empty {{ padding: 16px; }}
    .debug {{ border-top: 1px solid var(--line); padding: 10px 14px; }}
    .debug summary {{ cursor: pointer; color: var(--muted); }}
    .debug pre {{ border-top: 0; padding: 10px 0 0; max-height: 360px; }}
    @media (max-width: 720px) {{
      .row {{ grid-template-columns: 1fr; }}
      .summary {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Options Monitor Local</h1>
    <p>Use the Xueqiu blogger holdings as the stock list, then rank Robinhood put quotes through your personal cash-secured underwriting guardrails.</p>
    <nav><a href="/">Dashboard</a><a href="/settings">Settings</a></nav>
    <div class="pills">
      <div class="pill {"good" if xueqiu_status["configured"] else "warn"}">Xueqiu: {"ready" if xueqiu_status["configured"] else "needs setup"}</div>
      <div class="pill {"good" if robinhood_status["configured"] else "warn"}">Robinhood: {"ready" if robinhood_status["configured"] else "needs setup"}</div>
    </div>
  </header>
  <main>
    <section class="panel">
      <h2>Generate Option Recommendations</h2>
      <form method="post" action="/run">
        <label>Xueqiu blogger holdings link</label>
        <input name="xueqiu_user_url" value="{DEFAULT_BLOGGER_URL}">
        <p class="note">Default guardrails: sell puts only, no in-the-money puts, Tier-A target basis required, 21-60 DTE, at least 15% out-of-the-money, IV at least 40%, delta at or below 0.30, and liquidity/spread checks. Cash is assumed unlimited; GO and NO-GO rows inside the strategy universe are ranked by annualized yield.</p>
        <div class="row">
          <div>
            <label>Scan first N US holdings</label>
            <input name="symbols_limit" value="8">
          </div>
          <div>
            <label>Expiration, optional</label>
            <input name="options_expiration" placeholder="YYYY-MM-DD">
          </div>
        </div>
        {setup_note}
        <button name="action" value="blogger_opportunities">Generate Recommendations</button>
      </form>
    </section>
    {_render_output(result)}
  </main>
</body>
</html>"""
    return body.encode("utf-8")


def _settings_page(result: dict[str, Any] | None = None) -> bytes:
    status = _xueqiu_cookie_status()
    robinhood_status = _robinhood_token_status()
    cookie_label = "saved" if status["configured"] else "not saved"
    robinhood_label = "saved" if robinhood_status["configured"] else "not saved"
    env_file_label = str(status["env_file"])
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Options Monitor Settings</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17212b;
      --muted: #62707f;
      --line: #d6dde4;
      --bg: #f6f8fa;
      --panel: #ffffff;
      --accent: #176b87;
      --good: #166534;
      --warn: #9a3412;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }}
    header {{ padding: 24px 28px 12px; border-bottom: 1px solid var(--line); background: var(--panel); }}
    h1 {{ margin: 0 0 6px; font-size: 26px; letter-spacing: 0; }}
    p {{ color: var(--muted); line-height: 1.5; margin: 0; }}
    nav {{ display: flex; gap: 14px; margin-top: 14px; }}
    nav a {{ color: var(--accent); font-weight: 650; text-decoration: none; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 24px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    label {{ display: block; margin-top: 12px; font-size: 13px; color: var(--muted); }}
    input {{ width: 100%; margin-top: 5px; padding: 9px 10px; border: 1px solid var(--line); border-radius: 6px; font-size: 14px; }}
    button {{ width: 100%; margin-top: 14px; padding: 10px 12px; border: 0; border-radius: 6px; background: var(--accent); color: white; font-weight: 650; cursor: pointer; }}
    button.secondary {{ background: #334155; }}
    .pills {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 0; }}
    .pill {{ border-radius: 999px; padding: 6px 10px; font-size: 13px; border: 1px solid var(--line); }}
    .pill.good {{ color: var(--good); background: #ecfdf3; }}
    .pill.warn {{ color: var(--warn); background: #fff7ed; }}
    .small {{ margin-top: 10px; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }}
    .note {{ margin: 12px 0 0; padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; background: #f8fafc; color: var(--muted); font-size: 13px; line-height: 1.45; }}
    .output {{ margin-top: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
    .status {{ padding: 10px 14px; font-weight: 700; background: #e0f2fe; }}
    .status.needs-attention {{ background: #ffedd5; }}
    .command {{ padding: 10px 14px; color: var(--muted); border-top: 1px solid var(--line); }}
    pre {{ margin: 0; padding: 14px; overflow: auto; white-space: pre-wrap; border-top: 1px solid var(--line); max-height: 520px; }}
    pre.stderr {{ color: #991b1b; }}
  </style>
</head>
<body>
  <header>
    <h1>Options Monitor Settings</h1>
    <p>Local-only settings for read-only data sources.</p>
    <nav><a href="/">Dashboard</a><a href="/settings">Settings</a></nav>
    <div class="pills">
      <div class="pill {"good" if status["configured"] else "warn"}">Xueqiu cookie: {html.escape(cookie_label)}</div>
      <div class="pill {"good" if robinhood_status["configured"] else "warn"}">Robinhood token: {html.escape(robinhood_label)}</div>
      <div class="pill {"good" if status["env_file_exists"] else "warn"}">env file: {"found" if status["env_file_exists"] else "will be created"}</div>
    </div>
  </header>
  <main>
    <section class="panel">
      <h2>Xueqiu Login Cookie</h2>
      <form method="post" action="/settings">
        <label>Blogger stock page link</label>
        <input name="xueqiu_user_url" value="https://xueqiu.com/u/1247347556#/stock">
        <label>Xueqiu cookie</label>
        <input type="password" name="xueqiu_cookie" autocomplete="off" placeholder="Paste the Cookie value from your logged-in Xueqiu browser session">
        <p class="note">The Cookie box is cleared after every click. To avoid losing the pasted value, use Save And Test Cookie first.</p>
        <button name="action" value="save_and_test_xueqiu_cookie">Save And Test Cookie</button>
        <button class="secondary" name="action" value="test_xueqiu_cookie">Test Cookie Without Saving</button>
        <button class="secondary" name="action" value="save_xueqiu_cookie">Save Cookie Only</button>
      </form>
      <p class="small">Saved value is never displayed by this dashboard. It is stored as XUEQIU_COOKIE in {html.escape(env_file_label)} with file mode 600 when possible.</p>
    </section>
    <section class="panel" style="margin-top: 18px;">
      <h2>Robinhood Options Token</h2>
      <form method="post" action="/settings">
        <label>Symbol for test</label>
        <input name="options_symbol" value="NVDA">
        <label>Expiration for test</label>
        <input name="options_expiration" placeholder="YYYY-MM-DD, optional">
        <label>Robinhood auth token</label>
        <input type="password" name="robinhood_token" autocomplete="off" placeholder="Paste ROBINHOOD_AUTH_TOKEN">
        <p class="note">This dashboard only uses the token for read-only option quote requests. Do not paste your Robinhood password here.</p>
        <button name="action" value="save_and_test_robinhood_token">Save And Test Token</button>
        <button class="secondary" name="action" value="test_robinhood_token">Test Token Without Saving</button>
        <button class="secondary" name="action" value="save_robinhood_token">Save Token Only</button>
      </form>
      <p class="small">Saved value is never displayed by this dashboard. It is stored as ROBINHOOD_AUTH_TOKEN in {html.escape(env_file_label)}.</p>
    </section>
    {_render_output(result)}
  </main>
</body>
</html>"""
    return body.encode("utf-8")


def _command_for_action(action: str, fields: dict[str, list[str]]) -> tuple[list[str], int]:
    account = (fields.get("account") or ["christina"])[0].strip().lower() or "christina"
    futu_account_id = (fields.get("futu_account_id") or [""])[0].strip()
    if action == "setup":
        return ["./om", "setup", "check"], 180
    if action == "settings":
        return ["./om", "settings", "doctor"], 180
    if action == "validate_us":
        return ["./om", "config", "validate", "--source", "yaml", "--market", "us", "--config-yaml", "config.yaml"], 180
    if action == "validate_hk":
        return ["./om", "config", "validate", "--source", "yaml", "--market", "hk", "--config-yaml", "config.yaml"], 180
    if action == "scan_us_no_send":
        return ["./om", "run", "tick", "--config", "config.us.json", "--accounts", account, "--no-send"], 900
    if action == "preview_config":
        command = [
            "./om",
            "config",
            "init",
            "--output",
            "config.yaml",
            "--runtime-output-dir",
            ".",
            "--account-label",
            account,
            "--no-external-holdings",
            "--dry-run",
        ]
        if futu_account_id:
            command.extend(["--futu-acc-id", futu_account_id])
        for symbol in _safe_symbol_list((fields.get("us_symbols") or [""])[0]):
            command.extend(["--us-symbol", symbol])
        for symbol in _safe_symbol_list((fields.get("hk_symbols") or [""])[0]):
            command.extend(["--hk-symbol", symbol])
        return command, 180
    if action == "xueqiu_holdings":
        command = ["./om", "xueqiu", "holdings"]
        for cube in _safe_cube_list((fields.get("cube_symbols") or [""])[0]):
            command.extend(["--cube", cube])
        top = (fields.get("xueqiu_top") or ["20"])[0].strip()
        if top:
            command.extend(["--top", top])
        if len(command) == 3:
            command.extend(["--cube", "ZH123456"])
        return command, 180
    if action == "xueqiu_user_stocks":
        user_url = (fields.get("xueqiu_user_url") or [""])[0].strip()
        command = ["./om", "xueqiu", "user-stocks", "--user-url", user_url or "https://xueqiu.com/u/1247347556#/stock"]
        top = (fields.get("xueqiu_top") or ["20"])[0].strip()
        if top:
            command.extend(["--top", top])
        return command, 180
    if action == "robinhood_chain":
        symbol = (fields.get("options_symbol") or ["NVDA"])[0].strip().upper() or "NVDA"
        expiration = (fields.get("options_expiration") or [""])[0].strip()
        command = [
            "./om",
            "options-data",
            "chain",
            "--provider",
            "robinhood",
            "--symbol",
            symbol,
            "--limit",
            "10",
        ]
        if expiration:
            command.extend(["--expiration", expiration])
        return command, 180
    if action == "blogger_opportunities":
        user_url = (fields.get("xueqiu_user_url") or [""])[0].strip() or "https://xueqiu.com/u/1247347556#/stock"
        symbols_limit = (fields.get("symbols_limit") or ["5"])[0].strip() or "5"
        expiration = (fields.get("options_expiration") or [""])[0].strip()
        command = [
            "./om",
            "options-data",
            "blogger-opportunities",
            "--user-url",
            user_url,
            "--symbols-limit",
            symbols_limit,
            "--per-symbol-limit",
            "0",
            "--max-results",
            "250",
        ]
        if expiration:
            command.extend(["--expiration", expiration])
        return command, 900
    return ["./om", "setup", "check"], 180


def _run_robinhood_test(token: str, fields: dict[str, list[str]]) -> dict[str, Any]:
    symbol = (fields.get("options_symbol") or ["NVDA"])[0].strip().upper() or "NVDA"
    expiration = (fields.get("options_expiration") or [""])[0].strip()
    command = [
        "./om",
        "options-data",
        "chain",
        "--provider",
        "robinhood",
        "--symbol",
        symbol,
        "--limit",
        "3",
    ]
    if expiration:
        command.extend(["--expiration", expiration])
    return _run(
        command,
        timeout=180,
        env_overlay={ROBINHOOD_TOKEN_ENV: token},
    )


def _settings_result_for_action(action: str, fields: dict[str, list[str]]) -> dict[str, Any]:
    cookie = (fields.get("xueqiu_cookie") or [""])[0].strip()
    user_url = (fields.get("xueqiu_user_url") or [""])[0].strip() or "https://xueqiu.com/u/1247347556#/stock"
    robinhood_token = (fields.get("robinhood_token") or [""])[0].strip()
    if action == "save_and_test_xueqiu_cookie":
        saved = _save_env_setting(_dashboard_env_file(), XUEQIU_COOKIE_ENV, cookie)
        if not saved.get("ok"):
            return saved
        tested = _run(
            ["./om", "xueqiu", "user-stocks", "--user-url", user_url, "--top", "3"],
            timeout=180,
            env_overlay={XUEQIU_COOKIE_ENV: cookie},
        )
        return {
            "ok": bool(tested.get("ok")),
            "command": tested.get("command") or [],
            "stdout": f"{saved.get('stdout')}\n\nTest result:\n{tested.get('stdout') or tested.get('stderr') or '(no output)'}",
            "stderr": "",
        }
    if action == "save_xueqiu_cookie":
        return _save_env_setting(_dashboard_env_file(), XUEQIU_COOKIE_ENV, cookie)
    if action == "save_and_test_robinhood_token":
        saved = _save_env_setting(_dashboard_env_file(), ROBINHOOD_TOKEN_ENV, robinhood_token)
        if not saved.get("ok"):
            return saved
        tested = _run_robinhood_test(robinhood_token, fields)
        return {
            "ok": bool(tested.get("ok")),
            "command": tested.get("command") or [],
            "stdout": f"{saved.get('stdout')}\n\nTest result:\n{tested.get('stdout') or tested.get('stderr') or '(no output)'}",
            "stderr": "",
        }
    if action == "save_robinhood_token":
        return _save_env_setting(_dashboard_env_file(), ROBINHOOD_TOKEN_ENV, robinhood_token)
    if action == "test_robinhood_token":
        if not robinhood_token:
            robinhood_token = os.environ.get(ROBINHOOD_TOKEN_ENV) or _env_file_values(_dashboard_env_file()).get(ROBINHOOD_TOKEN_ENV, "")
        if not robinhood_token:
            return {"ok": False, "message": "Paste a Robinhood token first, or save one locally.", "command": []}
        return _run_robinhood_test(robinhood_token, fields)
    if action == "test_xueqiu_cookie":
        if not cookie:
            cookie = os.environ.get(XUEQIU_COOKIE_ENV) or _env_file_values(_dashboard_env_file()).get(XUEQIU_COOKIE_ENV, "")
        if not cookie:
            return {"ok": False, "message": "Paste a Xueqiu cookie first, or save one locally.", "command": []}
        return _run(
            ["./om", "xueqiu", "user-stocks", "--user-url", user_url, "--top", "3"],
            timeout=180,
            env_overlay={XUEQIU_COOKIE_ENV: cookie},
        )
    return {"ok": False, "message": f"Unsupported settings action: {action}", "command": []}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/settings":
            self._send(_settings_page())
        else:
            self._send(_page())

    def do_HEAD(self) -> None:
        content = _settings_page() if self.path.split("?", 1)[0] == "/settings" else _page()
        self._send_headers(len(content))

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0") or "0")
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        action = (fields.get("action") or ["setup"])[0]
        if self.path.split("?", 1)[0] == "/settings":
            result = _settings_result_for_action(action, fields)
            self._send(_settings_page(result))
            return
        command, timeout = _command_for_action(action, fields)
        result = _run(command, timeout=timeout)
        self._send(_page(result))

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_headers(self, content_length: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def _send(self, content: bytes) -> None:
        self._send_headers(len(content))
        self.wfile.write(content)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8501), Handler)
    print("Options Monitor Local dashboard: http://localhost:8501", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
