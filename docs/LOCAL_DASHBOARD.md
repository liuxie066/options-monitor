# Local Dashboard

The local dashboard is a personal, read-only workflow for scanning Xueqiu blogger
US holdings against Robinhood option quotes. It is designed for local research
and recommendation review only. It does not place trades.

## What It Does

The dashboard:

- loads a Xueqiu blogger stock-holdings page;
- keeps only US-listed holdings;
- fetches Robinhood stock and option quote data;
- evaluates sell-put candidates through the personal underwriting guardrails;
- shows both `GO` and `NO_GO` rows inside the eligible strategy universe;
- ranks returned rows by annualized cash-secured yield.

The default dashboard route is:

```bash
./.venv/bin/python scripts/local_dashboard.py
```

Then open:

```text
http://localhost:8501
```

## Local Secrets

The dashboard can save local data-source credentials into:

```text
~/Library/Application Support/options-monitor/options-monitor.env
```

Supported values:

```text
XUEQIU_COOKIE=<your browser cookie>
ROBINHOOD_AUTH_TOKEN=<your Robinhood session token>
```

These values are never displayed by the dashboard after saving. They must remain
local. Do not commit this env file, browser cookies, Robinhood tokens, screenshots
containing tokens, or downloaded runtime output.

The repository `.gitignore` excludes local runtime files such as:

- `options-monitor.env`
- `config.yaml`
- `config.us.json`
- `config.hk.json`
- `cache/`
- `output/`
- `output_accounts/`
- `output_shared/`
- `output_runs/`

## Sell-Put Guardrails

The local dashboard applies the personal sell-put underwriting strategy as
guardrails before showing recommendations.

Default requirements:

- option type must be `put`;
- strike must be below the current stock price;
- strike must be at least 15% out of the money;
- implied volatility must be at least 40%;
- DTE must be 21 to 60 days;
- absolute delta must be at or below 0.30 when available;
- open interest, volume, and bid/ask spread must pass liquidity checks;
- unknown single stocks are marked `NO_GO` unless an authorized target basis is
  configured;
- cash is assumed unlimited, so NAV is not required as a dashboard input.

The dashboard still shows `NO_GO` rows after the strategy-universe filter so the
user can see high-yield candidates that failed a hard guardrail. Rows outside the
strategy universe, such as in-the-money puts or near-the-money puts, are excluded
before ranking.

## Current Data Sources

Xueqiu is used only to discover the blogger's visible holdings. Robinhood is used
only for read-only stock and options market data. The dashboard does not need a
brokerage account ID and does not send orders.

## Safety Model

Treat all output as advisory-only. Before any real trade:

- confirm the contract manually in the brokerage UI;
- use limit orders, not market orders;
- confirm the strike, expiration, premium, and effective basis;
- confirm the option does not cross an event that changes the thesis;
- decide whether assignment would still be acceptable.

This local dashboard is intentionally separate from production notification,
ledger, and broker-facing workflows.
