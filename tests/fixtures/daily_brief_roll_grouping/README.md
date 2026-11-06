# Daily brief roll grouping fixtures

`lx.json` and `sy.json` are minimal projections of the HK daily decision briefs
from run `20261106T033016Z-acf396`, produced by release `4.6.17` on 2026-11-06.
The source files were `accounts/{lx,sy}/state/daily_decision_brief.HK.json` in
that run. Their use as committed regression fixtures was explicitly authorized.

The fixtures preserve position order, timestamps, old contract identity, the first
roll candidate, contract counts and unrounded net credits. Real lot IDs are
replaced consistently with `fixture-lot-NN`. Broker/account metadata inside roll
results, paths, notes, ledger identifiers and unrelated brief sections are omitted.

At each brief's `generated_at_utc`, lx has six current roll lots forming three
actions. Its three-lot group totals HKD 1,926.00; the two-lot 420 Put group totals
HKD 952.708 before display rounding to HKD 952.71. sy has five current roll lots
forming three actions. Two sy actions share the same target contract but have
different old expirations (2026-11-27 and 2026-12-30), so they must stay separate.
Uniqueness is therefore checked by the full action key, with the same-target
exception limited to distinct old positions.
