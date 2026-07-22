# Gateflow Plan — Close Advice Bug-Only Safety Fixes

## 1. Work unit

- **Work unit**: `close-advice-bug-boundaries`
- **Gate**: `plan`
- **Plan date**: 2026-07-22
- **Repository baseline**: `feature/option-notification-experience@e47a4aa1` (`origin/main`)
- **Plan revision**: 2
- **Supersedes**: revision 1 and the failed review in `docs/reviews/plan-review-20260722-224800.md`
- **User boundary**: only fix demonstrable bugs; do not change strategy

## 2. Goal, motivation, and success signals

### Goal

Fix two correctness failures without changing Close Advice strategy:

1. use the current documented Futu option fee schedule and prevent an otherwise-actionable close from remaining actionable when its fee cannot be calculated or makes the applicable close economics non-positive;
2. decide option lifecycle from one run-level business date before quote planning, so expiry-day and already-expired open lots do not enter ordinary DTE/annualized-return evaluation, and expired lots do not trigger pointless quote acquisition.

Close Advice remains advisory and read-only. This work does not create actions beyond existing close actions and does not write orders, positions, ledger events, configuration, or notification state.

### Motivation and direct evidence

- `domain/domain/fee_calc.py` still uses US ORF `0.02915`, omits OCC/settlement/CAT, and uses stale SEC/TAF rates. Futu's current US option schedule documents ORF `0.013`, OCC `0.02` capped at `55/order`, settlement `0.18`, CAT `0.0003`, SEC `0.0000206` with `0.01` minimum, and stock-option TAF `0.00329` with `0.01` minimum.
- `calc_futu_option_fee()` silently maps missing or unsupported currency to USD.
- `src/application/close_advice_runner.py::_apply_buy_to_close_fee()` converts fee errors into a data-quality flag, while `_apply_fee_profitability_gate()` can leave the row actionable. It also skips every long position.
- `run_close_advice()` plans/fetches quotes before deriving lifecycle, `_calc_dte()` reads the clock independently, and event enrichment reads the clock again.
- `domain/domain/close_advice.py` rejects short positions at `dte <= 0`, but the runner has already paid the quote/fetch cost; long calls at `dte == 0` may still enter ordinary strategy evaluation.

### Success signals

- US and HK option fee tests match the dated official schedule assumptions and preserve the public float-returning facade without claiming account-level exactness.
- Missing or unsupported currency never silently becomes USD.
- Shared fee consumers retain their existing threshold/rank algorithms; tests explicitly accept only consequences caused by corrected fee inputs.
- An otherwise-actionable short close or long take-profit with missing/unsupported fee evidence becomes `not_evaluable`.
- An otherwise-actionable short close or long take-profit with non-positive net lifetime P&L is suppressed; long salvage is suppressed only when net liquidation proceeds are non-positive.
- A non-actionable hold is not converted into an action or notification merely because fee evidence is unavailable.
- HK calculations are explicitly identified as a Tier-1 conservative upper bound unless the price is exactly HKD `0.01`, when the exchange tariff is waived. They are not labeled exact.
- One `business_date` is obtained per run and passed to DTE calculation and event enrichment.
- `expiry_day` and `expired_open` rows are deterministic diagnostic `not_evaluable` rows with no active close action; `expired_open` positions are excluded from required-data planning, OpenD fallback, and event enrichment.
- Active positions keep their existing thresholds, ranking, action mapping, and notification selection.
- Focused tests, architecture checks, and the full offline suite pass without network, send, config, broker, Feishu, or ledger writes.

## 3. Non-goals and scope boundary

### In scope

- Correct the shared Futu US option fee formula.
- Preserve HK Tier 1 as a documented conservative upper bound and implement the exact-`0.01` tariff waiver.
- Reject missing/unsupported currencies in the shared option-fee facade.
- Cover every direct option-fee consumer with regression tests; do not change its threshold or ranking policy.
- Add Close Advice fee evidence/status and net-close-proceeds fields.
- Put fee-aware actionability in a pure domain function; runner only gathers inputs and projects results.
- Compute lifecycle once from canonical expiration plus a single run-level business date.
- Exclude expired-open positions from all quote/fetch/event paths.
- Route expiry-day and expired-open positions to explicit diagnostic `not_evaluable` output.
- Update public read/analysis allowlists and `docs/CLOSE_ADVICE_CONTRACT.md` for diagnostic fields.

### Explicitly deferred strategy/product work

- No changes to capture, DTE, remaining-annualized-return, spread, convexity, delta, IV/RV, stress, or event thresholds.
- No changes to tier priority, ranking, `notify_levels`, `max_items`, or Daily Decision Brief behavior.
- No `review_position`, P0/P1 lifecycle action, review selector, action diff, notification delivery state, or notification renderer changes.
- No change to `continued_willingness`, `assignment_acceptable`, or `called_away_acceptable` semantics.
- No bid/ask execution model, slippage model, replay calibration, candidate policy, or portfolio optimization.
- No fee configuration, runtime fee scraping, instrument-level HKEX tariff classification, database/schema migration, release, or deployment.
- No inference of exercise, assignment, called-away, expiry settlement, or ledger state.

### Accepted consequence, not a strategy change

Correcting a shared fee input may move a candidate across an existing net-return threshold or alter existing displayed economics. This is an accepted bug-fix effect. Threshold values, filter order, rank formula, and tie-breaking must remain byte-for-byte unchanged.

## 4. Architecture and contract decisions

### 4.1 Fee authority

`domain/domain/fee_calc.py` remains the only fee authority. No Close Advice-specific fee table is allowed.

US fixed-package calculation per order:

```text
commission = max((0.65 if premium > 0.10 else 0.15) * contracts, 1.99)
platform = 0.30 * contracts
ORF = 0.013 * contracts
OCC = min(0.02 * contracts, 55.00)
settlement = 0.18 * contracts
CAT = 0.0003 * contracts
SEC sell-only = max(0.0000206 * transaction_amount, 0.01)
TAF sell-only stock option = max(0.00329 * contracts, 0.01)
```

HK ordinary-package upper bound:

```text
commission = max(0.002 * transaction_amount, 3.00)
platform = 15.00/order
exchange tariff = 0 when premium == 0.01, otherwise Tier 1 upper bound 3.00 * contracts
```

The existing `calc_futu_option_fee(...) -> float` facade remains. It accepts only normalized USD/HKD aliases and raises `ValueError` for missing/unsupported currency rather than assuming USD.

### 4.2 Close Advice fee evidence

Add public diagnostic fields:

| Field | Values | Meaning |
|---|---|---|
| `fee_calc_status` | `schedule_estimate`, `conservative_estimate`, `not_required`, `unavailable`, `unsupported_broker`, `unsupported_currency` | decision-quality of the fee estimate |
| `fee_calc_basis` | dated stable token or null | identifies the formula/assumption used |
| `net_close_proceeds` | number or null | long liquidation value less sell-to-close fee |

Rules:

- normalized broker must be Futu for this calculator; missing broker is `unavailable`, explicit non-Futu is `unsupported_broker`;
- USD is `schedule_estimate` with basis `futu_us_fixed_package_2026-07-22`; the input contract has no account platform-package fact, so it must not be labeled exact;
- HKD is `conservative_estimate` with basis `futu_hk_tier1_upper_bound_2026-07-22`;
- invalid price, contracts, or multiplier is `unavailable`;
- `schedule_estimate` and `conservative_estimate` are the two usable fee statuses for this bug-fix gate; all other statuses are non-authoritative and fail closed only when the row would otherwise be actionable;
- the existing fee aliases remain for compatibility.

### 4.3 Fee-aware domain safety gate

Add one pure domain function in `domain/domain/close_advice.py`. It receives the evaluated row plus projected fee evidence and returns the final decision row.

The function must preserve existing thresholds and apply only these safety postconditions:

1. Existing non-actionable holds remain holds.
2. Otherwise-actionable short profit capture requires `estimated_pnl_if_close_net > 0`.
3. Otherwise-actionable long take-profit requires `estimated_pnl_if_close_net > 0`.
4. Otherwise-actionable long salvage requires `net_close_proceeds > 0`; it may retain a negative lifetime P&L.
5. Missing/unsupported fee evidence on an otherwise-actionable row becomes `tier=not_evaluable`, `exit_state=not_evaluable`, and later maps to the existing `close_action=not_evaluable`.
6. A calculated but non-positive applicable economic value suppresses the action to the existing hold state and adds a stable data-quality reason.

No new tier, action type, priority, selector, or notification state is introduced.

### 4.4 Lifecycle authority

At the start of `run_close_advice()`:

```text
business_date = expiration_business_today()  # exactly once
```

Derive lifecycle only from canonical expiration and this date:

| State | Rule | Quote/fetch behavior | Advice behavior |
|---|---|---|---|
| `active` | `dte > 0` | unchanged | unchanged |
| `expiry_day` | `dte == 0` | excluded from new fetch/fallback/event enrichment | diagnostic `not_evaluable`; no annualized-return or convexity action evaluation |
| `expired_open` | `dte < 0` | excluded from required-data planning, OpenD fallback, and event enrichment | diagnostic `not_evaluable`, `quote_status=not_required` |
| `unknown` | expiration cannot be parsed | coverage diagnostics may run, but quote DTE cannot make it active | deterministic existing `not_evaluable` data-gap contract plus lifecycle field |

Existing local quote artifacts may still be loaded as a batch implementation detail, but lifecycle rows must neither require nor consume those quotes for a decision. No broker outcome is inferred.

Add public diagnostic field `position_lifecycle_state` with the four values above. Lifecycle diagnostics use existing `not_evaluable` state/action contracts; they do not become notification actions.

### 4.5 Single-date invariant

- `_calc_dte(expiration, business_date)` does not receive a quote, call the clock, or fall back to quote-derived DTE.
- `_position_to_input()` receives `business_date`.
- `_merge_event_snapshot_for_short_vol_positions()` receives the same `business_date`.
- A regression test makes a second `expiration_business_today()` call fail.

## 5. Implementation slices

### S1 — Shared fee truth and Close Advice fee fail-closed

**Objective**: correct fee inputs at their shared owner and close the actionable fee failure path without changing strategy policy.

**Allowed files**:

- `domain/domain/fee_calc.py`
- `domain/domain/close_advice.py`
- `src/application/close_advice_runner.py`
- `src/application/agent_tools/close_advice_read_impl.py`
- `src/application/agent_tools/analysis.py`
- `docs/CLOSE_ADVICE_CONTRACT.md`
- directly relevant tests under `tests/`

**Exact changes**:

1. Update dated fee constants/formulas and docstrings.
2. Make the shared currency dispatch strict.
3. Add fee status/basis and net-close-proceeds projection in the runner.
4. Add and invoke the pure domain fee safety gate before close-action mapping and combo aggregation.
5. Add public/read/trace fields without changing notification selectors.
6. Add all-consumer regressions for Sell Put, Sell Call, Combo Yield, assigned-stock, positions reporting, and Close Advice.

**Non-goals**: no threshold/rank edits, no scanner refactor, no new fee object/config/provider.

**Focused validation**:

```bash
./.venv/bin/python -m pytest \
  tests/test_fee_calc.py \
  tests/test_close_advice_domain.py \
  tests/test_close_advice_runner.py \
  tests/test_sell_put_linked_call_helper.py \
  tests/test_sell_call_strategy_unification.py \
  tests/test_candidate_filter_trace.py \
  tests/test_assigned_stock_quotes.py \
  tests/test_positions_reporting.py
```

Expected assertions include exact US components, HK waiver/upper-bound labeling, unsupported currency rejection, fee-failure action suppression, long take-profit/salvage boundaries, unchanged hold behavior, and unchanged threshold/rank constants.

**Completion signal**: every direct consumer is covered; no action remains actionable with unknown applicable close fee.

**Stop condition**: official schedule evidence conflicts with the constants above, or a consumer requires a fee-package/config design not present in the approved scope.

### S2 — Lifecycle routing and single business date

**Prerequisite**: accepted S1 commit.

**Objective**: prevent lifecycle-invalid quote/evaluation work and eliminate intra-run date drift.

**Allowed files**:

- `src/application/close_advice_runner.py`
- `src/application/agent_tools/close_advice_read_impl.py`
- `src/application/agent_tools/analysis.py`
- `docs/CLOSE_ADVICE_CONTRACT.md`
- `tests/test_close_advice_runner.py`
- public-contract tests only if required by the new diagnostic field

**Exact changes**:

1. Add a small pure lifecycle classifier; no service or class hierarchy.
2. Obtain `business_date` once and thread it through DTE and event enrichment.
3. Partition quote-eligible positions before required-data planning and OpenD fallback.
4. Project expiry-day, expired-open, and unknown-lifecycle rows through the existing diagnostic `not_evaluable` contract without normal strategy evaluation; quote `dte` must never promote an unknown lifecycle to active.
5. Add lifecycle field to CSV/read/analysis allowlists and docs.
6. Test quote fetch exclusion, event exclusion, explicit lifecycle output, single date call, unchanged active behavior, and missing/malformed canonical expiration remaining `not_evaluable` even when quote `dte` is positive.

**Non-goals**: no manual-review action, auto-close, reconciliation workflow, notification change, or ledger write.

**Focused validation**:

```bash
./.venv/bin/python -m pytest \
  tests/test_close_advice_runner.py \
  tests/test_close_advice_domain.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py
```

**Completion signal**: expired-open lots cannot reach fetch/event/economic paths, and one date provider call owns the run.

**Stop condition**: lifecycle cannot be derived from canonical expiration without changing ledger schema or inferring broker outcome.

## 6. Validation and quality gates

After each slice:

- focused pytest command above;
- `git diff --check`;
- code review and re-review using `$deepreview`.

After both slices:

```bash
./.venv/bin/python -m pytest
./.venv/bin/python -m compileall -q domain src
git diff --check
```

Aggregate acceptance additionally proves:

- no changes in Daily Brief/notification/config/state-write modules;
- no threshold/rank constants changed;
- public diagnostic fields are documented and backward-compatible;
- no network or production mutation was used for validation.

## 7. Documentation decision

Update `docs/CLOSE_ADVICE_CONTRACT.md` because fee evidence and lifecycle state are public read contracts. No PRD, notification-experience, runtime config, VERSION, or CHANGELOG update belongs in this work unit.

## 8. Risks and residual-risk destinations

| Risk | Treatment |
|---|---|
| Fee schedules or account packages drift later | dated assumption tokens and official URLs; actual account-package integration and periodic review are later maintenance tasks |
| HK Tier 1 overstates Tier 2/3 fees | explicitly classified as conservative estimate; instrument classification is a later work unit |
| Corrected fees change candidate acceptance | accepted bug-fix consequence; threshold/rank policy must remain unchanged and tests cover boundaries |
| Midpoint is not executable price | assigned to later replay/strategy work; unchanged here |
| Expired lot still needs broker reconciliation | assigned to a later operations workflow; this work only stops incorrect quote/evaluation behavior |
| Expiry-day operator visibility | assigned to later strategy/product work; no new alert in this bug-only work unit |

There are no blocking open questions. The user has already selected bug-only scope and execution.

## 9. Why this is not over-designed

- It repairs two existing owners instead of adding a fee service, lifecycle service, config surface, database field, or notification lane.
- Four diagnostic fields are the minimum needed to avoid presenting estimates and lifecycle skips as exact priced decisions.
- Each slice has one primary invariant and can be independently reviewed and reverted.
- Strategy/product ideas from revision 1 are explicitly deferred rather than hidden inside the bug fix.

## 10. Completion report format

Final closeout must report:

- accepted plan/review artifacts and commits;
- per-slice changed files, tests, review findings, and accepted commits;
- aggregate deepreview and PR review status;
- full-suite/analyze results;
- public contract/doc changes;
- residual risks and their destination;
- draft PR URL and next entry point.
