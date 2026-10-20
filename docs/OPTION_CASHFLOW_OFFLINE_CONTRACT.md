# Option Cashflow Offline Contract

The canonical metric definitions live in [Option Performance Design](OPTION_PERFORMANCE_DESIGN.md).
OM owns both live and offline calculation; consumers must not implement another cash or return reducer.

## Public pure entry

```python
from domain.domain.performance import (
    OptionFeeCashEvent,
    calculate_option_performance,
    cashflow_input_digest,
)
```

```python
result = calculate_option_performance(
    events, period=period, accounts=accounts, broker=None, fee_cash_events=(),
    fee_mode="actual", knowledge_cutoff_ms=None, input_evidence=None,
)
```

The installed `om-strategy-core` wheel also exports the public types and functions directly:

```python
from om_strategy_core import (
    ContractKey, TradeEvent, OptionFeeCashEvent, PeriodRequest, PeriodWindow,
    normalize_performance_period, calculate_option_performance, cashflow_input_digest,
)
```

In the call above, `events` contains typed canonical `TradeEvent` objects,
including prior openings and controls needed to project the requested cash. `period` is an explicit
`PeriodWindow` with a nonnegative start and positive-duration half-open effective interval `[t0,t1)`. Supply a nonempty collection
of nonblank string accounts, not a bare string; account names normalize to lowercase. A supplied
broker must be a nonblank string and uses canonical broker normalization (`FUTU` and `富途` agree).
Both metrics use the same normalized scope. A valid unmatched scope is empty, not unavailable.

The result is `PerformanceReduction`, the same domain result used by the live application. Its
`bundle['period_option_net_cashflow']` contains the new metric; `cashflow_facts` is a separate tuple
of detailed cash facts and attribution. Normal application summaries do not serialize that tuple.
Existing `facts` and `breakdowns` remain opening-cohort facts, not period-cash details.
There is no config, database, quote, network, filesystem or clock lookup in this entry.

Exact repeated event/fee identities deduplicate. Conflicting versions of one identity in a snapshot
reject; callers supply one frozen version, not a revision log. Canonical projection resolves controls
once. Live ingestion retains stricter duplicate-row rejection. Public/offline parity requires the
same admitted canonical facts, scope and policy; ingestion safeguards are not relaxed for parity.

## Period metric and original rates

`period_option_net_cashflow.v1` selects each option cash fact by effective time. It publishes
`selection='cash_event_effective_time'`,
`time_policy='trade_event_time_with_explicit_fee_effective_time'`, `fee_mode`, `status`, `missing`,
`fact_count`, `by_currency[currency]` with `total/gross/fees` metric objects, and `cny_total`.
Each metric has `amount/status/missing`. Fees are signed cash: negative charge, positive refund;
total equals gross plus fees when both are complete. Missing native cash, actual fees or required
conversion evidence is partial, never invented zero. Native currencies stay separate. Nonzero
non-CNY cash requires validated persisted conversion evidence for CNY; reads never fetch current FX.

A prior-month opening of +200 and this-month close of -300 produces this-month gross cash -300,
even with no this-month opening cohort. An unclosed +200 sale is cash, not realized profit.
Expiry does not rebook premium. Stock principal, settlement fees, price changes, dividends and
marks are excluded; this physical-settlement contract does not infer intrinsic option cash.

`option_net_cashflow`, cohort rows/breakdowns, returns and wins preserve their original numerical
and shape contracts. The return numerator remains **opening-cohort `option_net_cashflow`**;
denominators, sample membership, capital-time and win eligibility are unchanged. In particular,
`fee_cash_events` affect **only the new period metric**, including replacement fees. Old cohort
cash and returns continue using ledger-attached fees. Do not assume full-cycle cohort cash equals
period cash when supplemental/replacement fees are supplied. The two metrics overlap and cannot
be added together. Actual daily occupied capital remains independent of the return denominator.

## Fee facts and restatement

`OptionFeeCashEvent` has `fee_id`, `source_event_id`, `effective_at_ms`, `currency`, signed Decimal
`amount`, `basis` (`actual|estimated|missing`), `source`, `replaces_attached_fee`, and optional
persisted `cash_conversion`. Missing basis requires `amount=None`. IDs and source must be nonempty;
effective time must be a positive strict integer. The linked trade must be an effective option trade;
orphan/voided links reject. Account/broker scope comes from that trade.

A replacement is the **full corrected attached fee cash**, not a delta. At most one replacement
per trade is allowed, and its effective time must equal the linked trade time. It suppresses the
whole original attached option fee for period cash. Observing a -2 fee corrected to -3 later
restates the original month in a new snapshot; it does not move -3 to the observation month.
Changing the version of a fee uses a new frozen snapshot with the same fee identity.

A genuinely new -1 debit or +1 refund uses a distinct `fee_id`,
`replaces_attached_fee=False`, and its independently evidenced effective time. It adds cash without
suppressing the original attached fee. No independent fee time is inferred. This interface does
not add production fee capture, ledger tables or fee-write APIs.

`fee_mode='actual'` treats estimated fees as incomplete. Explicit `simulated` mode permits labelled
estimated fees and marks affected period results estimated, never observed actual cash. This mode
does not turn estimated ledger fees into actual cohort evidence or change original rates.

## Frozen evidence and visibility

Without `knowledge_cutoff_ms`, visibility is `corrected_snapshot`. Current mutable rows, even with
old trade dates, cannot establish what was known historically. Later fee/FX/correction/void evidence
may restate earlier effective periods in a new snapshot.

With a positive strict-integer cutoff, every input needs an evidence entry keyed as
`trade:<event_id>` or `fee:<fee_id>`, with exactly these fields:

```python
input_evidence = {
    "trade:" + event.event_id: {
        "available_at_ms": archived_available_at_ms,
        "source": archived_source_reference,
        "payload_sha256": cashflow_input_digest(event),
    },
}
```

`cashflow_input_digest(record)` hashes the exact `to_dict()` payload using UTF-8 canonical JSON:
`ensure_ascii=False`, sorted keys, compact `(',', ':')` separators, `allow_nan=False`, SHA256.
Binding includes all serialized input contents, not a selected subset. Every effective time and
embedded fee-provider/FX observation and quote time must be no later than declared availability, and availability
must be no later than cutoff. The period end must be no later than `cutoff+1`. Missing, mismatched
or future evidence rejects instead of silently discarding records and breaking projection context.
Canonical effective cash times after adjustments must also be no later than cutoff.
Earlier snapshots must omit revisions not yet known; the function does not reconstruct history.

The result adds `bundle['cashflow_source']`:

```text
schema_version = option_performance.inputs.v1
snapshot_hash  = hash of canonical records and input_evidence
visibility     = corrected_snapshot | point_in_time_archive
knowledge_cutoff_ms = supplied cutoff or null
input_count    = count of unique admitted inputs
```

Record order and exact repeats do not change snapshot identity. Archive evidence is part of that
identity. The hash binds content; it does **not independently authenticate** the caller's source,
availability timestamp or archive completeness. `point_in_time_archive` means the supplied archive
passed binding/time checks, not independently verified historical knowledge. Record period, scope,
fee mode, cutoff, source commit and wheel/source content hash alongside a research result. Compare
adjacent-window additivity only under the same frozen facts and policy. Later restatements cannot
mutate earlier inputs or results.

## Consumer compatibility

Application bodies use `schema_version='option_performance.report.v3'` and `metric_semantics`;
the HTTP envelope remains `option_performance.api.v1`. No request or command rename is needed.
Rows/breakdown pages carry the schema and cohort semantics in both payload and content-hash binding,
but never attach full-account period totals to symbol-filtered pages. The Tool model contract retains
the new field and its missing-data reasons. Bot shows period cash separately from opening-cohort cash;
an old response lacking the new field renders period cash unavailable. Per-contract decision history
uses “合约期权现金结果”, not period profit. No renderer recalculates cash or repurposes legacy values.
