# Options Monitor — Project Map

This repo is meant to run as a repeatable monitoring pipeline.

## Core entrypoints

- Runtime config entry (OM only): `config.us.json` / `config.hk.json`
- Main pipeline: `./run_watchlist.sh` → `scripts/run_pipeline.py`
- Scheduler: `scripts/cli/scan_scheduler_cli.py`
- Alert engine: `scripts/alert_engine.py`
- Dev mainline unified entry: `scripts/send_if_needed_multi.py` (thin wrapper -> `scripts.multi_tick.main.main`)
- Production scheduler entry (unchanged): `scripts/send_if_needed.py`

## Data + state

- Per-account state: `output_accounts/<account>/state/`
  - `last_run.json`, `scheduler_state.json`, `cash_snapshot.json`, etc.

## Option positions (write-back)

- Parse message → normalized params: `scripts/parse_option_message.py`
- Parse + write (safe by default): `scripts/option_intake.py` (default `--dry-run`)
- CRUD: `scripts/option_positions.py`

## Non-negotiable invariants

- Intake safety: `option_intake.py` MUST default to dry-run.
- Idempotency: `position_id` should be stable for the same contract tuple.
- State isolation: each account writes only under its own state dir.

## Diagnostics

- Config: `python scripts/validate_config.py --config config.us.json`
- Health: `python scripts/healthcheck.py --config config.us.json`
- Health + notify (dry-run): `python scripts/healthcheck_and_notify.py --config config.us.json --dry-run`
- Tests (no pytest): `./.venv/bin/python tests/run_tests.py`
