# Gateflow Fix Artifact — Combo Yield / Sell Put Runtime Decoupling

- **Gate**: fix
- **Review**: `docs/reviews/code-review-20260718-164805.md`
- **Status**: fixes implemented

## Finding resolutions

| Finding | Decision | Fix |
|---|---|---|
| CR-1 | accepted | Removed obsolete `yield_window/yield_sp` reference and unused market gate; Sell Put runner now owns only Sell Put config. |
| CR-2 | accepted | Disabled and exception branches materialize empty Sell Put/Combo artifacts; Combo facade does not read Sell Put artifact when Sell Put is disabled. |
| CR-3 | accepted | Replaced nested-ownership tests with explicit Sell Put-only regression; retained Combo-owned cash/underwriting coverage. |

## Validation

- affected strategy/data tests: `83 passed`
- multi-tick/notification/architecture tests: `106 passed`
- `git diff --check`: pass
- `python3 -m compileall -q src/application`: pass

## Residual risks

- Shared required-data failure: assigned to later work unit.
- Duplicate funding-put scans: accepted current cost.
- Commit isolation: requires explicit user decision because pre-existing staged hunks overlap touched files.
