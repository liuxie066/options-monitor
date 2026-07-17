# Gateflow Implementation Plan — Option Performance Refactor

- **Gate**: plan
- **Work unit**: `option-performance-refactor`
- **Created at**: 2026-07-17 22:40:48（本机时钟）
- **Status**: accepted-plan
- **Artifact path**: `docs/gateflow/option-performance-refactor-plan-20260717-224048.md`
- **Goal confirmation**: `docs/gateflow/option-performance-refactor-goal-confirmation-20260717-222825.md`
- **Prior adversarial review**: `docs/reviews/plan-review-20260717-221908.md`

## 1. Goal, Motivation, Success Signal

### Goal

把当前分散且语义冲突的期权收益统计重构成一条可重放、可审计的 pipeline：

```text
canonical trade facts
  -> single ledger lifecycle allocation
  -> normalized performance facts
  + assigned-stock canonical facts
  + versioned valuation/FX evidence
  -> period performance engine
  -> Agent / CLI / Analysis / Portfolio bridges / legacy adapters
```

统一支持：

- MTD、YTD、自然月、自然年和任意日期范围；
- premium activity、完整 cash movement、realized/unrealized/period-total PnL；
- actual/estimated/missing fee evidence；
- Sell Put assignment -> assigned stock -> mark/sale -> covered-call lifecycle；
- 可解释的 notional-days capital efficiency；
- 历史 mark/FX 的 deterministic selection；
- 新公共工具和 staged legacy migration；
- total-assets PnL bridge 与 cash-balance bridge 的独立恒等式。

### Motivation

直接代码证据表明：

- `src/application/positions/reporting.py:32-50` 当前按 UTC 归月；capital bridge 按北京时间 EOD 截止；
- `src/application/positions/reporting.py:733-838` 用 current collateral 计算历史 return；
- `src/application/positions/reporting.py:2480-2547` 和 `domain/domain/ledger/lots.py:124-131` 重复计算 realized PnL；
- `domain/domain/ledger/events.py:36-66` 把 missing fee 与 actual zero 都归为 `0.0`；
- `docs/ASSIGNED_STOCK_RETURN_DESIGN.md:37,166-184` 只证明 Sell Put assigned-stock lifecycle；
- `src/application/portfolio_capital_bridge.py:113-149` 用 option cash carve out portfolio period PnL；
- Agent、analysis、CLI、Copilot 和 bridge 都消费 legacy `monthly_income_report` contract。

### Success Signal

1. 单一 ledger owner 生成 close allocation；performance 不再自行匹配 option lots。
2. 所有 period 使用 `Asia/Shanghai` reporting timezone 和 `[start,end_exclusive)` UTC 区间。
3. native-currency activity/cash/gross PnL 可从 immutable events 重放；缺失不当零。
4. realized/unrealized/period-total net PnL 仅在 incurred fee coverage 完整时有值。
5. 历史 opening/end valuation 和 FX 使用 versioned evidence，并返回 selected fact IDs。
6. period total PnL 使用 `realized + end unrealized - start unrealized`，可用于 PnL bridge。
7. Sell Put lifecycle 不重复计算 premium、stock cost、covered-call PnL 或 fees。
8. capital efficiency 明确 denominator 与 capital-days；不输出无依据的 generic return。
9. 新 `option_performance_report`/CLI/analysis 为 primary；legacy adapter 有 deprecation warning。
10. PnL/cash bridge 分别满足 total-assets/cash-balance 恒等式；缺上游 cash facts 时 unavailable。
11. old/new reconciliation、consumer inventory、tests、deepreview、draft PR review 和 final closeout 全部通过。

## 2. Confirmed Scope and Non-goals

### In scope

- Period、money、metric、quality contracts；
- ledger close economic allocation 和 fee allocation；
- performance facts 与 period aggregation；
- option/stock valuation mark 与 FX evidence persistence/selectors/import；
- option open-position unrealized PnL；
- Sell Put assigned-stock lifecycle；
- notional-days capital efficiency；
- Agent/CLI/analysis/public migration；
- PnL bridge、cash bridge；
- legacy adapters、shadow reconciliation、docs/tests。

### Non-goals

- 不建设通用 stock inventory ledger；
- Short Call assignment、Long Call/Put exercise 缺股票 basis 时只记录 option close/settlement cash，stock PnL explicit incomplete；
- 不接管普通股票买卖、dividend、tax、split 的完整会计；
- 不推导 broker margin return 或 NAV return；
- 不改写 immutable `trade_events`/`assigned_stock_events`；
- 不修改生产 config、Feishu、broker-facing data 或真实持仓状态；
- 不修改外部 portfolio-management 服务；
- 不预先引入 performance materialized projection/cache；只有 correctness 完成且 benchmark 证明需要时另开 work unit。

## 3. Design Alignment and Architecture Boundaries

### 3.1 Ownership

```text
trade_events
  -> domain.ledger.project_trade_events
     -> PositionLot state
     -> OptionEconomicAllocation[]   # option lifecycle allocation 唯一 owner

assignment allocations + assigned_stock_events
  -> domain.assigned_stock.project_assigned_stock_lifecycle
     -> AssignedStockLotState[]
     -> AssignedStockEconomicFact[]  # assigned stock lifecycle 唯一 owner

allocations + direct event cash/activity facts + assigned-stock facts
  -> domain.performance.engine
     -> native-currency PeriodPerformance

valuation_marks + fx_rate_facts
  -> deterministic selectors
  -> opening/end valuations + translated CNY metrics

application.performance.service
  -> loads facts/evidence
  -> invokes pure domain projections/engine
  -> builds public report
```

Rules:

- Performance engine may filter/aggregate but may not choose target option lot or re-run independent matching.
- `OptionEconomicAllocation` is returned by canonical ledger projection; it is not a second persisted state machine.
- Assigned-stock projection owns only stock lots born from supported Sell Put assignment facts.
- Mark/FX are evidence facts, not trade facts; they never enter `trade_events`.
- Domain imports no `src/` or `scripts/`.

### 3.2 Minimal planned module set

New domain files:

```text
domain/domain/performance/__init__.py
domain/domain/performance/period.py
domain/domain/performance/models.py
domain/domain/performance/engine.py
domain/domain/ledger/economics.py
domain/domain/assigned_stock.py
```

New application/infrastructure/interface files:

```text
src/application/performance/__init__.py
src/application/performance/service.py
src/application/performance/adapters.py
src/application/performance/reconciliation.py
src/infrastructure/performance_evidence_sqlite.py
src/interfaces/cli/option_performance.py
```

No separate builders/loaders/presenters/fx/money/cash/pnl modules are created unless a reviewed slice proves a concrete ownership need.

## 4. Core Contracts

### 4.1 Period request

Public input:

```json
{"period":"mtd","as_of_date":"2026-07-17"}
{"period":"ytd","as_of_date":"2026-07-17"}
{"period":"month","month":"2026-06"}
{"period":"year","year":2025}
{"period":"range","start_date":"2026-04-01","end_date":"2026-06-30"}
```

Validation:

- `period` required and one of `mtd|ytd|month|year|range`；
- MTD/YTD require `as_of_date` or default to local today；future `as_of_date` rejected；
- month requires `YYYY-MM`，future month rejected；current month is partial through current instant；
- year requires four digits，future year rejected；current year is partial through current instant；
- range requires both dates, `start <= end`, future end rejected；
- public reporting timezone fixed to `Asia/Shanghai` in v1；not configurable per request；
- internal interval is `[start_at_ms, end_exclusive_at_ms)`；
- past period ends at local next-day/month/year midnight exclusive；current-day period ends at `now_ms + 1` exclusive；
- legacy adapter may pass an exact `cutoff_ms_override`; new public tool does not expose raw milliseconds。

Normalized output:

```json
{
  "kind":"ytd",
  "reporting_timezone":"Asia/Shanghai",
  "requested_start_date":"2026-01-01",
  "requested_end_date":"2026-07-17",
  "effective_start_at_ms":1767196800000,
  "effective_end_exclusive_at_ms":0,
  "valuation_open_at_ms":1767196800000,
  "valuation_end_at_ms":0,
  "status":"partial_current"
}
```

`valuation_open_at_ms` is the instant immediately before period activity starts; `valuation_end_at_ms` is `effective_end_exclusive_at_ms - 1`.

### 4.2 Money and precision

- Internal money/rates use `Decimal(str(input))`；
- option/stock amounts and PnL quantize to `0.000001`；
- bridge presentation quantizes to `0.01`；
- JSON numbers remain numeric floats converted only at the boundary；
- native currency maps are authoritative；CNY is derived；
- no cross-currency sum without complete selected FX evidence。

Metric amount envelope:

```json
{
  "by_currency":{"USD":123.45,"HKD":67.0},
  "cny":950.12,
  "status":"observed|partial|not_observed|not_applicable",
  "missing":[],
  "fx_fact_ids":["..."]
}
```

### 4.3 Fee facts

```text
FeeFact:
  amount: Decimal | None
  basis: actual | estimated | missing
  component: option_open | option_close | assignment_option | stock_settlement | stock_sale
  source: str | None
  reason: str | None
  source_event_id: str
```

Rules:

- bare legacy `fees=0` without provenance is `missing`, not actual zero；
- provenance `basis=actual` + `amount=0` is actual zero；
- gross metrics ignore fees；net subtracts incurred fees；
- if any required incurred fee component is missing, corresponding net metric is null/partial；
- open fee allocated pro rata by opened contracts；each close gets `open_fee_total * closed_qty / opened_qty`，last close absorbs Decimal remainder；
- remaining open quantity retains unallocated open fee for unrealized net；
- option and stock settlement fees are distinct and never double counted；
- estimated closing fee belongs only to `estimated_*` close advice, never realized/unrealized production PnL。

### 4.4 Option economic allocation

Each canonical close event yields one allocation per target lot segment:

```text
OptionEconomicAllocation:
  allocation_id = open_event_id + close_event_id + sequence
  contract_key
  open_event_id / close_event_id / target_lot_id
  opened_at_ms / closed_at_ms
  contracts
  multiplier / currency
  position_side / close_type
  open_price / close_price
  open_amount_gross / close_amount_gross
  realized_pnl_gross
  allocated_open_fee / close_fee
  realized_pnl_net | None
  fee_quality
  strategy / leg_role / strategy_group_id
  settlement_ref | None
```

Invariants:

- sum allocation contracts per close == close event contracts；
- sum closed + remaining == opened contracts；
- gross short = open proceeds - close cost；gross long = close proceeds - open cost；
- assignment/expire option close price zero is legal；
- void/repair replay yields allocations only for effective events；
- same event stream produces stable allocation IDs/order。

### 4.5 Performance facts and report

Direct facts:

- `premium_collected`: short open gross activity；
- `premium_paid`: long open gross activity；
- `option_trade_cash`: signed open/close option cash；
- `option_fee_cash`: incurred option fees；
- `stock_settlement_cash`: assignment/exercise settlement signed cash；
- `assigned_stock_sale_cash`: supported assigned-stock sale signed cash；
- `realized_option_pnl`: from ledger allocation only；
- `assigned_stock_pnl`: from assigned-stock projection only；
- `option_unrealized_pnl`: open lot basis versus selected option mark；
- `assigned_stock_unrealized_pnl`: supported stock lot basis versus selected stock mark。

Period PnL:

```text
period_realized_pnl
  = option realized allocations in period
  + assigned-stock realized allocations in period

period_total_pnl
  = period_realized_pnl
  + ending_unrealized_pnl
  - opening_unrealized_pnl
```

Gross and net are parallel. Net requires fee coverage at opening, realized allocations and ending state.

Public report v1:

```json
{
  "schema_version":"option_performance_report.output.v1",
  "period":{},
  "scope":{},
  "activity":{
    "premium_collected_gross":{},
    "premium_paid_gross":{},
    "contracts_opened":0,
    "contracts_closed":0
  },
  "cash":{
    "option_trade_cash_gross":{},
    "option_fee_cash":{},
    "stock_settlement_cash_gross":{},
    "assigned_stock_sale_cash_gross":{},
    "total_cash_change_net":{}
  },
  "pnl":{
    "realized_gross":{},
    "realized_net":{},
    "opening_unrealized_gross":{},
    "opening_unrealized_net":{},
    "ending_unrealized_gross":{},
    "ending_unrealized_net":{},
    "period_total_gross":{},
    "period_total_net":{}
  },
  "capital":{},
  "assignment_lifecycle":{},
  "breakdowns":{"monthly":[],"accounts":[],"symbols":[]},
  "quality":{},
  "rows":{}
}
```

Rows are included only with `include_rows=true`。

### 4.6 FX translation

- native currency amounts are the source of truth；
- realized/cash fact CNY translation uses FX selected at event effective time；
- opening/end unrealized uses FX selected at corresponding valuation instant；
- period total CNY is computed from translated realized + translated end unrealized - translated opening unrealized；
- method named `effective_time_translation`; it is not claimed as separately decomposed FX return；
- missing any required FX makes CNY envelope partial/null but preserves native values；
- CNY facts use identity rate 1 and no stored FX fact required。

### 4.7 Valuation and FX evidence

#### Instrument identity

Valuation identity is market-instrument identity and is deliberately independent from account, broker and position side:

```text
OptionInstrumentKey:
  symbol                  canonical underlier, e.g. NVDA / 0700.HK
  option_type             put | call
  strike                  canonical Decimal text, no exponent/trailing zero
  expiration_ymd          YYYY-MM-DD
  currency                uppercase ISO-like code
  multiplier              canonical Decimal text

StockInstrumentKey:
  symbol                  canonical symbol
  currency                uppercase ISO-like code
```

Deterministic codecs:

```text
option:v1|<pct(symbol)>|<option_type>|<strike>|<expiration_ymd>|<currency>|<multiplier>
stock:v1|<pct(symbol)>|<currency>
```

- values are UTF-8 percent-encoded with RFC 3986 unreserved characters left literal；
- Decimal codec uses fixed notation, strips insignificant trailing zero and normalizes `-0` to `0`；
- decode rejects unknown versions, field counts, invalid enums/dates/currency and non-positive multiplier；
- `ContractKey -> OptionInstrumentKey` is an explicit conversion requiring currency/multiplier；`ContractKey.position_key` is never accepted as valuation identity；
- option/stock codecs have round-trip, stability and cross-account/cross-side reuse tests；
- adjusted/non-standard option deliverables are accepted only when a unique market-native contract code and authoritative multiplier prove identity；otherwise mark selection returns `unsupported_adjusted_contract` rather than matching a standard contract by symbol/strike/expiry。

SQLite tables live in the same resolved local ledger DB but are owned by a separate infrastructure repository:

```sql
performance_evidence_schema(
  component TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  migrated_at_ms INTEGER NOT NULL
)

performance_valuation_marks(
  fact_id TEXT PRIMARY KEY,
  instrument_key TEXT NOT NULL,
  key_version INTEGER NOT NULL,
  instrument_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  option_type TEXT,
  strike_text TEXT,
  expiration_ymd TEXT,
  multiplier_text TEXT,
  currency TEXT NOT NULL,
  price_text TEXT NOT NULL,
  mark_kind TEXT NOT NULL,
  effective_at_ms INTEGER NOT NULL,
  observed_at_ms INTEGER NOT NULL,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  supersedes_fact_id TEXT,
  quality_json TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  UNIQUE(source, source_id, revision)
)

performance_fx_rate_facts(
  fact_id TEXT PRIMARY KEY,
  base_currency TEXT NOT NULL,
  quote_currency TEXT NOT NULL,
  rate_text TEXT NOT NULL,
  rate_kind TEXT NOT NULL,
  effective_at_ms INTEGER NOT NULL,
  observed_at_ms INTEGER NOT NULL,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  supersedes_fact_id TEXT,
  quality_json TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  UNIQUE(source, source_id, revision)
)
```

Indexes cover `(instrument_key,effective_at_ms)` and `(base_currency,quote_currency,effective_at_ms)`。Structured identity columns must decode to exactly the same `instrument_key`; mismatch rejects the fact。

Schema state machine:

```text
not_initialized --explicit migrate/apply--> initialized_v1
initialized_v1 --idempotent migrate--> initialized_v1
unknown/newer version --> unsupported_schema (no write)
```

- explicit `migrate_evidence_schema(conn)` owns idempotent DDL inside one transaction；
- repository construction and all report/read/select paths never execute DDL；
- absent schema/tables return `not_initialized` plus empty evidence, without mutating the DB；
- import/capture `--apply` first runs migration in its write transaction；dry-run validates the same transition without writing。

Selector rules:

1. remove facts superseded by a valid correction chain；
2. only `effective_at_ms <= valuation/event instant`；
3. choose latest effective time；
4. tie priority: `manual_correction > official_close > broker_snapshot > realtime_snapshot > cache_snapshot`；
5. tie after priority: highest revision then fact_id；
6. maximum staleness 7 calendar days；older returns missing；
7. output fact ID, effective time, observed time, source and staleness days。

Import/capture envelope:

```json
{
  "schema_version":"option_performance_evidence.v1",
  "valuation_marks":[
    {
      "fact_id":"optional-on-import",
      "instrument":{"type":"option","symbol":"NVDA","option_type":"put","strike":"100","expiration_ymd":"2026-08-21","currency":"USD","multiplier":"100"},
      "price":"2.35","mark_kind":"midpoint","effective_at_ms":0,"observed_at_ms":0,
      "source":"manual_correction","source_id":"operator-supplied-id","revision":1,
      "supersedes_fact_id":null,"quality":{},"raw":{}
    }
  ],
  "fx_rates":[
    {
      "fact_id":"optional-on-import","base_currency":"USD","quote_currency":"CNY","rate":"7.12",
      "rate_kind":"spot","effective_at_ms":0,"observed_at_ms":0,"source":"manual_correction",
      "source_id":"operator-supplied-id","revision":1,"supersedes_fact_id":null,"quality":{},"raw":{}
    }
  ]
}
```

Write state machine and invariants:

- parse and validate the complete envelope before opening the write transaction；
- dry-run executes schema-version, identity, Decimal, duplicate, correction and cycle validation but commits nothing；
- apply runs migration plus the entire batch in one transaction；any conflict rolls back migration and all facts in that batch；
- duplicate `(source,source_id,revision)` with byte-semantic equivalent normalized payload is idempotent；different payload is conflict；
- `supersedes_fact_id` must exist either before the batch or earlier in the validated batch, have the same evidence kind and exact identity/pair, and must not create self-reference or a cycle；
- append-only insert or explicit correction only；no update/delete surface；
- no report/read path writes evidence。

#### Current read-only collection and explicit capture

`collect_current_performance_evidence(...)` is an application adapter, not a domain or persistence concern:

- only runs when the normalized period has `status=partial_current` and `refresh_quotes=true`；historical/full past periods return `skipped_historical` and never call OpenD/live FX；
- derives ending open option identities from canonical projection and ending assigned-stock identities from the shared projector；
- resolves each option market code from a stored canonical `contract_symbol` when present；otherwise fetches only the required expiration chain via existing `opend_symbol_fetching` and matches symbol/type/strike/expiry/multiplier, rejecting zero/multiple matches；
- batches exact option codes through `fetch_option_snapshots` in `opend_market_snapshot_fetching.py`；stock marks reuse `get_spot_opend`/assigned-stock quote adapter；
- option mark is positive bid/ask midpoint when both are valid, otherwise positive `last_price` with `mark_kind=last_fallback`; crossed/zero/NaN markets are missing with diagnostics；
- one injected `now_ms` is used as `observed_at_ms`; broker snapshot time is used as `effective_at_ms` only when present and valid, otherwise the same `now_ms` is used and quality records `timestamp_fallback=true`；
- current FX uses `get_exchange_rates_or_fetch_latest(..., write_cache=False)`；the adapter preserves payload source/timestamp, labels stale-cache fallback as `cache_snapshot` rather than realtime, applies the same staleness rules, and performs no cache/evidence write from report generation；
- report service merges `live_unpersisted` facts only for current ending valuation and returns collection diagnostics/provenance。

`./om option-performance evidence capture` calls the same collector, converts successful live facts to the v1 envelope, then uses the same import transaction:

- dry-run is the default；`--apply` is required to migrate/write local evidence；
- capture accepts `--config-key`, `--config-path`, `--data-config`, optional `--account`/`--broker`, and optional `--output` for the validated envelope；
- capture is current-only；historical timestamps cannot be supplied；
- report refresh never persists, while capture apply produces future replayable historical facts；
- tests prove current fetch without DB writes, capture dry-run, capture apply, and a later historical query selecting the captured fact IDs。

### 4.8 Assigned-stock lifecycle

Supported complete state machine:

```text
Sell Put option assignment allocation
  -> AssignedStockLot(open)
  -> zero or more stock_sale events
  -> open | partially_closed | closed
  -> opening/end mark valuation when shares remain
```

Rules:

- stock cost basis remains settlement price * shares + stock settlement fee；premium stays option side；
- lifecycle gross = attributed option PnL + stock realized/unrealized gross + covered-call realized/unrealized gross；
- lifecycle net subtracts each incurred fee exactly once；
- stock sale must target a unique assigned-stock lot；ambiguous remains unresolved；
- covered-call attribution uses explicit strategy/lot link first；existing heuristic may be retained only with non-complete quality；
- Short Call assignment / long exercise settlement without stock basis outputs settlement cash and `stock_lifecycle_status=incomplete_inventory_basis`；
- ordinary stock holdings are reconciliation evidence only。

### 4.9 Capital efficiency

Capital uses continuous time integration, not inclusive calendar-day counting:

```text
overlap_ms = max(0, min(exposure_end, period_end_exclusive) - max(exposure_start, period_start))
capital_days = notional * overlap_ms / 86_400_000
```

- all exposure intervals are `[open_at_ms, close_at_ms)`；current open exposure ends at `period_end_exclusive`；
- short put notional is `strike * multiplier * open_contracts`；
- long option notional is allocated open premium debit for remaining contracts；
- assigned stock notional is remaining stock cost basis；
- partial close/sale reduces quantity exactly at the close/sale instant；
- Sell Put assignment ends put capital and starts assigned-stock capital at the same assignment instant, so neither gap nor overlap exists；
- covered call attributed to existing assigned stock has zero incremental capital-days；
- standalone/naked short call remains unavailable without authoritative stock/margin basis；
- Decimal milliseconds/day calculation is retained through aggregation；presentation may round but never truncates to integer days。

Output:

```text
capital_basis = notional_days_v1
capital_days_by_currency
period_total_pnl_net / capital_days * 365
period_realized_pnl_net / capital_days * 365
coverage
```

Required boundary tests cover same-day open/close, intraday partial close, exact local/UTC midnight, cross-period lots and assignment transition。No unqualified `return_rate` field is created。

### 4.10 Portfolio bridges

PnL bridge:

```text
ending_assets
  = opening_assets
  + external_cash_flow
  + option_period_total_pnl_net
  + portfolio_non_option_pnl
  + reconciliation_residual
```

- Requires aligned period end date, CNY, valuation/FX and net fee coverage；
- missing option period total PnL produces partial/unavailable, not zero；
- assigned-stock lifecycle belongs to option strategy component in v1 and is excluded from residual once；
- residual tolerance remains explicit and does not hide basis mismatch。

Cash bridge:

```text
ending_cash
  = opening_cash
  + external_cash_flow
  + option_trade_cash_net
  + stock_settlement_cash_net
  + assigned_stock_sale_cash_net
  + non_option_cash_change
  + reconciliation_residual
```

- Public tool queries `/analysis/cash-facts`；404/unavailable/missing opening or ending cash returns structured unavailable；
- no reuse of opening/ending assets；
- pure builder is fully tested with injected cash facts。

### 4.11 Public compatibility

New primary Agent tool:

```text
option_performance_report
```

Exact Agent input contract:

```json
{
  "config_key":"us",
  "config_path":null,
  "data_config":null,
  "account":null,
  "broker":null,
  "period":"mtd",
  "as_of_date":null,
  "month":null,
  "year":null,
  "start_date":null,
  "end_date":null,
  "include_rows":false,
  "refresh_quotes":true
}
```

- `config_key` is `us|hk`, defaults to `us`; `config_path` overrides config-key resolution and `data_config` overrides portfolio data-config resolution using existing public helpers；
- `account` is normalized lowercase；omitted means no account filter over the resolved ledger, while `scope.accounts` is the sorted union of configured accounts and accounts actually observed in selected facts；it never silently chooses the first account and breakdowns/rows retain account；
- `broker` is normalized with existing `normalize_broker`; omitted means all brokers in the resolved ledger；
- `period` defaults to `mtd` and uses the exact conditional fields in §4.1; extraneous period-specific fields are validation errors rather than ignored ambiguity；
- `include_rows=false` by default；when true rows sort by `(effective_at_ms, fact_kind, source_event_id, allocation_id)` ascending, are capped at 1000, and expose `rows_truncated` diagnostics；
- `refresh_quotes=true` by default only for `partial_current`; historical/full past requests force `skipped_historical` regardless of input and never call live adapters；
- aggregate reports preserve native currency maps；CNY aggregate is available only with complete selected FX evidence；
- Agent and CLI call the same request parser/service and must have parity for period/scope/core metric/quality fields。

New CLI:

```bash
./om option-performance report --period ytd --as-of-date 2026-07-17
./om option-performance report --period month --month 2026-06
./om option-performance report --period year --year 2025
./om option-performance report --period range --start-date 2026-04-01 --end-date 2026-06-30
./om option-performance evidence import --file facts.json          # dry-run default
./om option-performance evidence import --file facts.json --apply
./om option-performance evidence capture --config-key us --account lx      # dry-run default
./om option-performance evidence capture --config-key us --account lx --apply
```

CLI report exposes the same config/scope/period/include-rows/refresh-quotes fields；`--no-refresh-quotes` maps to false。Evidence import accepts only the v1 envelope in §4.7。Evidence capture accepts only current collection scope and produces the same envelope。`--dry-run` and `--apply` are mutually exclusive and omission means dry-run；there is no Agent write tool for evidence in v1。

New portfolio tools:

```text
portfolio_pnl_bridge
portfolio_cash_bridge
```

Legacy:

- `monthly_income_report` remains registered for one deprecation window；
- it calls the new service, maps monthly breakdown/rows to old schema, sets unsupported legacy rates to null, and emits deprecation/semantic warnings；
- `portfolio_capital_bridge` remains deprecated and unchanged unless required to call the PnL bridge adapter without semantic lying；internal consumers migrate to new bridges；
- candidate `net_income` is out of this report migration and remains candidate-domain naming；
- close advice adds `estimated_pnl_if_close_gross`, `estimated_close_fee`, `estimated_pnl_if_close_net`; old `realized_if_close` remains deprecated alias for one window。

## 5. Error Handling and Quality Invariants

- invalid period input => validation error before data load；
- projection diagnostics with severity error => affected metric partial, rows retained in diagnostics, no guessed values；
- no events in a proven scope => observed zero；scope cannot be proven => not_observed；
- missing mark => realized still observed, unrealized/period total partial/null；
- missing FX => native observed, CNY partial/null；
- missing fee => gross observed, net partial/null；
- ambiguous assigned-stock attribution => lifecycle incomplete, no stock sale inference；
- historical query never calls realtime quote/FX fetch；
- current query may use injected live evidence but labels it `live_unpersisted`；
- report generation is pure read；evidence import is explicit local write；
- all outputs include schema version, period, scope, quality and evidence provenance。

## 6. Implementation Slices

### Slice S1 — Period, Money, Instrument, Metric and Quality Contracts

- **Objective**: establish pure domain period, instrument identity, money and quality contracts。
- **Allowed files**:
  - new `domain/domain/performance/__init__.py`
  - new `domain/domain/performance/period.py`
  - new `domain/domain/performance/models.py`
  - new `tests/test_performance_period.py`
  - new `tests/test_performance_models.py`
  - new `tests/test_performance_instrument_identity.py`
  - new `docs/OPTION_PERFORMANCE_DESIGN.md`
- **Exact changes**:
  - implement `PeriodRequest`, `PeriodWindow`, `normalize_period`；
  - implement `OptionInstrumentKey`, `StockInstrumentKey`, deterministic v1 encode/decode and explicit `ContractKey` conversion requiring currency/multiplier；
  - implement Decimal amount envelope, `MetricStatus`, `MetricQuality`, `FeeFact`；
  - document contracts in design doc。
- **Non-goals**: no event projection, no public tool, no DB。
- **Validation**:
  - `python3 -m pytest tests/test_performance_period.py tests/test_performance_models.py tests/test_performance_instrument_identity.py -q`
  - `python3 -m ruff check domain/domain/performance tests/test_performance_period.py tests/test_performance_models.py tests/test_performance_instrument_identity.py`
- **Expected assertions**: month/year/range/MTD/YTD boundaries, future rejection, current partial cutoff, Decimal/quality/null semantics, identity round-trip/stable Decimal codec, account/broker/side exclusion, option/stock invalid decode。
- **Completion signal**: contracts can be consumed without importing `src` and evidence identity is schema-ready。

### Slice S2 — Canonical Ledger Economic Allocations and Fees

- **Objective**: make ledger projection the only option close allocation/PnL owner。
- **Allowed files**:
  - new `domain/domain/ledger/economics.py`
  - modify `domain/domain/ledger/events.py`
  - modify `domain/domain/ledger/lots.py`
  - modify `domain/domain/ledger/projection.py`
  - modify `domain/domain/ledger/__init__.py`
  - modify `src/application/ledger/event_codec.py`
  - modify `src/application/ledger/queries.py`
  - modify `src/application/ledger/api.py`
  - modify `src/application/ledger/writer.py` only to preserve new fee provenance on newly written events
  - new `tests/test_ledger_economics.py`
  - modify `tests/test_ledger_projection.py`
  - modify `tests/test_ledger_sqlite_workflows.py` only for projection/codec assertions
- **Exact changes**:
  - add stable `OptionEconomicAllocation` output to `ProjectionResult`；
  - allocate open/close fee facts on partial closes；
  - expose projection allocations through ledger API；
  - preserve existing PositionLot/risk behavior and immutable event JSON compatibility。
- **Non-goals**: no performance aggregation, no stock lifecycle。
- **Validation**:
  - `python3 -m pytest tests/test_ledger_projection.py tests/test_ledger_economics.py tests/test_ledger_sqlite_workflows.py -q`
  - `python3 -m ruff check domain/domain/ledger src/application/ledger tests/test_ledger_economics.py`
- **Expected assertions**: fee unknown vs zero, partial close remainder, void/repair, stable IDs, quantity/PnL invariants, old DB/event decode。
- **Completion signal**: reporting no longer needs an independent option lot matcher for new engine。

### Slice S3 — Core Period Performance Engine: Activity, Cash, Realized PnL

- **Objective**: build native-currency period engine without valuation dependencies。
- **Allowed files**:
  - new `domain/domain/performance/engine.py`
  - new `src/application/performance/__init__.py`
  - new `src/application/performance/adapters.py`
  - new `src/application/performance/service.py`
  - new `tests/test_performance_engine.py`
  - new `tests/test_performance_service.py`
  - modify `docs/OPTION_PERFORMANCE_DESIGN.md`
- **Exact changes**:
  - normalize canonical events and ledger allocations into activity/cash/realized facts；
  - signed cash formulas for short/long open/close and settlement；
  - period/month/account/symbol breakdowns；
  - gross/net quality envelopes；
  - service loads events only through ledger API。
- **Non-goals**: unrealized/FX/assigned-stock/capital/public tool。
- **Validation**:
  - `python3 -m pytest tests/test_performance_engine.py tests/test_performance_service.py -q`
  - existing ledger tests from S2。
- **Expected assertions**: premium not added to PnL, assignment principal only in cash, realized assigned to close period, missing fees null only net, observed-zero vs not-observed。
- **Completion signal**: pure service returns v1 core sections for all period kinds。

### Slice S4 — Valuation/FX Evidence, Current Collection and Capture Core

- **Objective**: make current and historical valuation operationally closed while preserving pure reads and deterministic replay。
- **Allowed files**:
  - new `src/infrastructure/performance_evidence_sqlite.py`
  - new `src/application/performance/evidence_collection.py`
  - modify `src/application/performance/adapters.py`
  - modify `src/application/performance/service.py`
  - modify `domain/domain/performance/models.py`
  - modify `domain/domain/performance/engine.py`
  - use `src/application/opend_market_snapshot_fetching.py`, `src/application/opend_symbol_fetching.py`, `src/application/positions/assigned_stock_quotes.py`, and `src/infrastructure/exchange_rates.py` through existing public functions；modify them only if a focused test proves a missing reusable timestamp/code-resolution seam
  - new `tests/test_performance_evidence_sqlite.py`
  - new `tests/test_performance_evidence_collection.py`
  - new `tests/test_performance_valuation.py`
  - modify `tests/test_performance_engine.py`
  - modify `tests/test_performance_service.py`
  - modify `docs/OPTION_PERFORMANCE_DESIGN.md`
- **Exact changes**:
  - explicit idempotent evidence migration; repository reads return `not_initialized` without DDL/write；
  - v1 envelope parsing, full dry-run validation and one-transaction import/capture apply；
  - deterministic mark/FX selector and correction-chain validation；
  - current-only read-through collection for open option/assigned-stock identities and live FX with no persistence；
  - exact option market-code resolution, batch snapshots, midpoint/last fallback/timestamp/staleness diagnostics；
  - reusable capture payload generation over the same collector/import core；
  - opening/end option mark valuation, effective-time CNY translation and `period_total = realized + end unrealized - opening unrealized`；
  - historical path rejects live evidence。
- **Non-goals**: CLI command wiring occurs S7；no report/read/cache writes。
- **Validation**:
  - `python3 -m pytest tests/test_performance_evidence_sqlite.py tests/test_performance_evidence_collection.py tests/test_performance_valuation.py tests/test_performance_engine.py tests/test_performance_service.py -q`
- **Expected assertions**: read on missing tables has no DB mutation, migration idempotency, whole-batch rollback, dry-run no write, correction identity/cycle rejection, cross-account instrument reuse, option code unique resolution, batch midpoint/last fallback, current live-unpersisted, weekend previous close, 7-day stale, missing mark/FX, historical no-live, capture then historical fact-ID replay, start/end formula。
- **Completion signal**: current report can value open positions read-only, explicit capture can persist replayable facts, and repeated historical query is byte-semantically stable after JSON normalization。

### Slice S5 — Sell Put Assigned-Stock Lifecycle

- **Objective**: move supported stock lifecycle to one reusable projection and integrate with performance through the legal ledger API。
- **Allowed files**:
  - new `domain/domain/assigned_stock.py`
  - modify `src/application/ledger/queries.py`
  - modify `src/application/ledger/api.py`
  - modify `src/application/performance/adapters.py`
  - modify `src/application/performance/service.py`
  - modify `domain/domain/performance/engine.py`
  - modify `src/application/positions/reporting.py` only to delegate assigned-stock calculations for legacy output
  - modify `src/application/agent_tools/operations_impl.py` only if assigned-stock read path needs the shared projector
  - modify `src/application/ledger/read_model.py`, `src/application/positions/workflows.py`, `src/application/trades/state_reconcile.py`, and `src/application/agent_tools/materialization_impl.py` only where this slice touches an assigned-stock read so it uses the new API instead of repository introspection
  - new `tests/test_assigned_stock_projection.py`
  - new `tests/test_ledger_assigned_stock_queries.py`
  - new `tests/test_performance_assignment.py`
  - modify `tests/test_positions_reporting.py`
  - modify `tests/test_assigned_stock_sale_intake.py`
  - modify `docs/ASSIGNED_STOCK_RETURN_DESIGN.md`
  - modify `docs/OPTION_PERFORMANCE_DESIGN.md`
- **Exact changes**:
  - add `assigned_stock_event_log(repo)` to `ledger/queries.py`, re-export it from `ledger/api.py`, and keep repository capability probing inside that boundary；
  - project Sell Put assignment stock lots/sales/marks/fees；
  - explicit lifecycle formula and quality；
  - covered-call explicit-first attribution and heuristic quality downgrade；
  - unsupported inventory basis status for other assignment/exercise；
  - performance and touched legacy consumers use the public ledger API, never direct `getattr(repo, "list_assigned_stock_events")`；
  - legacy report delegates rather than independently recomputes lifecycle。
- **Non-goals**: ordinary stock ledger, dividends/tax/split；unrelated pre-existing introspection paths need not be mass-refactored unless touched。
- **Validation**:
  - `python3 -m pytest tests/test_assigned_stock_projection.py tests/test_ledger_assigned_stock_queries.py tests/test_performance_assignment.py tests/test_assigned_stock_sale_intake.py tests/test_positions_reporting.py -q`
- **Expected assertions**: API capability absent returns empty with explicit diagnostics, no premium double count, partial sale, missing quote, fee coverage, covered-call no double attribution, unsupported inventory basis, touched paths contain no direct assigned-stock repository getattr。
- **Completion signal**: option positions assigned-stock read and new performance share the same lifecycle results through `ledger.api`。

### Slice S6 — Capital Exposure and Efficiency

- **Objective**: replace misleading return rates with precisely integrated notional-days metrics。
- **Allowed files**:
  - modify `domain/domain/performance/models.py`
  - modify `domain/domain/performance/engine.py`
  - modify `src/application/performance/service.py`
  - new `tests/test_performance_capital.py`
  - modify `tests/test_performance_engine.py`
  - modify `docs/OPTION_PERFORMANCE_DESIGN.md`
- **Exact changes**:
  - implement `capital_days = notional * overlap_ms / 86_400_000` with Decimal arithmetic；
  - use `[open_at,close_at)` intervals and period intersection；
  - short-put, long-option and assigned-stock exposure segments；
  - partial close/sale quantity reduction at event instant；
  - assignment atomically ends put exposure and starts stock exposure without overlap；
  - zero incremental covered-call capital and unavailable naked/unknown basis；
  - realized/period-total net annualized efficiency with explicit basis/coverage。
- **Non-goals**: NAV/margin return, integer calendar-day approximation。
- **Validation**:
  - `python3 -m pytest tests/test_performance_capital.py tests/test_performance_engine.py -q`
- **Expected assertions**: same-day open/close nonzero fractional days, intraday partial close weighted segments, exact midnight boundary, cross-period lots, assignment no gap/double count, overlapping covered call, zero denominator, missing net PnL。
- **Completion signal**: no public unqualified return metric in new schema and all capital segments conserve quantity/time。

### Slice S7 — New Agent Tool, CLI, Evidence Import/Capture and Legacy Adapter

- **Objective**: make the new report primary, expose explicit evidence writes and preserve staged compatibility。
- **Allowed files**:
  - new `src/interfaces/cli/option_performance.py`
  - modify `src/interfaces/cli/main.py`
  - modify `src/application/agent_tools/positions.py`
  - modify `src/application/agent_tools/materialization_impl.py`
  - modify `src/application/ledger/read_model.py`
  - modify `src/application/ledger/queries.py`
  - modify `src/application/ledger/api.py`
  - modify `src/interfaces/cli/option_positions_report.py`
  - modify `src/application/close_advice_runner.py`
  - modify `domain/domain/close_advice.py` only if gross field must be exposed directly
  - new `tests/test_option_performance_cli.py`
  - new `tests/test_option_performance_agent_tool.py`
  - modify `tests/test_agent_plugin_contract.py`
  - modify `tests/test_agent_plugin_smoke.py`
  - modify `tests/test_option_positions_cli.py`
  - modify exact close-advice tests: `tests/test_close_advice_contract.py`, `tests/test_close_advice_domain.py`, `tests/test_close_advice_reallocation_shadow.py`, `tests/test_close_advice_runner.py`, `tests/test_notification_compact.py`
- **Exact changes**:
  - register `option_performance_report` with the exact schema/defaults/aggregation/current-vs-historical behavior in §4.11；
  - add CLI report using the same parser/service and an Agent/CLI parity test；
  - add guarded evidence import and current evidence capture；both dry-run by default, mutually exclusive `--dry-run/--apply`, v1 envelope only, no Agent write tool；
  - make monthly report a deprecated adapter over new service；
  - add close-advice `estimated_*` fields and deprecated alias；
  - publish output contract/schema and validation errors。
- **Non-goals**: analysis/portfolio consumers remain S8/S9。
- **Validation**:
  - `python3 -m pytest tests/test_option_performance_cli.py tests/test_option_performance_agent_tool.py tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py tests/test_option_positions_cli.py tests/test_close_advice_contract.py tests/test_close_advice_domain.py tests/test_close_advice_reallocation_shadow.py tests/test_close_advice_runner.py tests/test_notification_compact.py -q`
- **Expected assertions**: all period inputs and extraneous-field rejection, account aggregate semantics, broker normalization, include_rows cap, current refresh default, historical forced skip, import/capture dry-run and apply gate, CLI/Agent parity, deprecation warning, old tool still callable。
- **Completion signal**: new public surfaces and evidence lifecycle work without legacy implementation dependency。

### Slice S8 — Analysis Views and Consumer Migration

- **Objective**: migrate every identified internal analytical/assistant/CLI consumer and remove legacy semantic dependence。
- **Allowed files**:
  - modify `src/application/agent_tools/analysis.py`
  - modify `src/application/assistant/command_parser.py`
  - modify `src/application/assistant/inbound_control.py`
  - modify `src/application/assistant/renderer.py`
  - modify `src/application/assistant/tool_bindings.py`
  - modify `src/application/copilot/eval_fixtures.py`
  - modify `src/interfaces/cli/option_positions_report.py`
  - modify additional Copilot routing/answer files only where a direct `monthly_income_report` preference is proven by search
  - modify `tests/test_analysis_tools.py`
  - modify `tests/test_assistant_runtime.py`
  - modify `tests/test_inbound_control.py`
  - modify `tests/test_assistant_position_query.py`
  - modify `tests/test_copilot_phase1.py`
  - modify `tests/test_copilot_p1_eval.py`
  - modify `tests/copilot_eval/test_answer_quality.py`
  - modify `tests/test_option_positions_cli.py`
  - new `docs/migrations/OPTION_PERFORMANCE_V1_MIGRATION.md`
- **Exact changes**:
  - new period/monthly performance, cash and PnL component views；
  - old aliases source new report and are marked deprecated；
  - route profit questions to PnL, cash questions to cash and premium questions to activity；
  - remove non-additive residual logic that subtracts premium and realized from cash；
  - inventory all direct consumer references and document old-to-new metric matrix；
  - retain candidate ranking `net_income` as candidate-domain naming only。
- **Non-goals**: no speculative rewrite of assistant/Copilot files without a direct legacy contract dependency。
- **Validation**:
  - `python3 -m pytest tests/test_analysis_tools.py tests/test_assistant_runtime.py tests/test_inbound_control.py tests/test_assistant_position_query.py tests/test_copilot_phase1.py tests/test_copilot_p1_eval.py tests/copilot_eval/test_answer_quality.py tests/test_option_positions_cli.py -q`
  - any additional Copilot file admitted by the proven-dependency allowance must add its exact existing/new test path to the implementation artifact before edit；the slice may not finish with an untested modified consumer。
- **Expected assertions**: profit questions select PnL, cash questions select cash, premium questions select activity, no premium+PnL sum, namespaces preserved, every listed consumer has a migration assertion。
- **Completion signal**: repository analysis/assistant/Copilot/CLI consumers no longer require legacy semantics outside the deprecated adapter。

### Slice S9 — Portfolio PnL and Cash Bridges

- **Objective**: replace mixed capital bridge with two valid bridges。
- **Allowed files**:
  - new `src/application/portfolio_pnl_bridge.py`
  - new `src/application/portfolio_cash_bridge.py`
  - modify `src/application/agent_tools/portfolio.py`
  - modify `src/application/portfolio_capital_bridge.py` only for deprecation metadata/adapter if safe
  - new `tests/test_portfolio_pnl_bridge.py`
  - new `tests/test_portfolio_cash_bridge.py`
  - modify `tests/test_portfolio_agent_tool.py`
  - modify `tests/test_portfolio_capital_bridge.py` only for deprecation compatibility
  - modify `docs/OM_AGENT_CAPABILITY_MAP.md`
  - modify `docs/AGENT_INTEGRATION.md`
- **Exact changes**:
  - PnL bridge consumes option period total net CNY and PM capital facts；
  - cash bridge consumes PM cash facts endpoint and performance cash components；
  - unavailable/partial/reconciliation semantics；
  - register `portfolio_pnl_bridge` and `portfolio_cash_bridge`；
  - keep old bridge deprecated, not primary。
- **Non-goals**: no external PM code changes。
- **Validation**:
  - `python3 -m pytest tests/test_portfolio_pnl_bridge.py tests/test_portfolio_cash_bridge.py tests/test_portfolio_agent_tool.py tests/test_portfolio_capital_bridge.py -q`
- **Expected assertions**: assignment internal asset conversion absent from PnL except economic PnL, cash bridge never uses assets, 404 cash facts unavailable, aligned cutoff/FX/fee basis, no missing-as-zero。
- **Completion signal**: both bridges have independent, tested equations and contracts。

### Slice S10 — Shadow Reconciliation, Cutover, Docs and Legacy Isolation

- **Objective**: prove migration, finish internal cutover and document deprecation/rollback。
- **Allowed files**:
  - new `src/application/performance/reconciliation.py`
  - modify `src/application/performance/service.py`
  - modify legacy report/bridge files only to finalize adapters
  - modify `tests/test_positions_reporting.py`
  - new `tests/test_performance_reconciliation.py`
  - modify `tests/test_research.py` only if public capability inventory changes
  - modify `docs/migrations/OPTION_PERFORMANCE_V1_MIGRATION.md`
  - modify `docs/AGENT_WIKI.md`
  - modify `README.md` if public CLI listing is present
  - modify `CHANGELOG.md` only if repo convention requires unreleased changes; otherwise document no changelog change
- **Exact changes**:
  - reconciliation matrix implementation for exact and expected-delta metrics；
  - replay determinism and null/coverage gates；
  - search-based consumer zero check for legacy semantic fields outside adapters/tests/docs；
  - rollback instructions and deprecation removal entry point。
- **Non-goals**: actual legacy removal/release version bump。
- **Validation**:
  - focused suite from all slices；
  - `python3 -m pytest -q`；
  - `python3 -m ruff check .`；
  - `git diff --check`；
  - `rg -n "monthly_income_report|net_income_cny|realized_return_rate" src` reviewed against explicit allowlist。
- **Expected assertions**: exact native cash/activity/gross reconciliation, expected fee/FX deltas classified, no unowned consumer, rollback can re-enable adapters without data migration rollback。
- **Completion signal**: implementation ready for aggregate deepreview。

## 7. Review, Commit and Gateflow Execution

For each slice:

```text
implementation artifact
-> deep code review artifact
-> accepted findings fixed
-> re-review artifact
-> gateflow: accept option-performance-refactor <slice-id> commit
```

After S10:

```text
aggregate deepreview
-> fix/re-review
-> gateflow: accept deepreview for option-performance-refactor
-> ready-to-open-draft-PR
-> push
-> create draft PR
-> PR review/fix/re-review
-> gateflow: accept PR review for option-performance-refactor
-> final push
-> draft-PR-pass
-> final closeout
```

Only Gateflow artifacts and files in the active slice may be staged in each protected commit。

## 8. Tests and Validation Strategy

### Contract tests

- period schema/boundaries/timezone；
- output schema and public tool manifest；
- legacy adapter deprecation。

### Property/invariant tests

- event quantity conservation；
- cash sign and cash reconciliation；
- fee allocation conservation；
- realized + end unrealized - opening unrealized；
- assignment premium/cost/fee no double count；
- bridge equations。

### Failure-path tests

- missing fee/mark/FX/cash facts；
- ambiguous stock sale/covered call；
- evidence schema `not_initialized` read without DDL, idempotent migration, batch rollback and supersede cycle/identity conflict；
- current option-code ambiguity, crossed/empty snapshots and current collection without persistence；
- stale evidence/correction conflict；
- historical query with live evidence；
- unsupported inventory basis；
- mixed account cutoff and external service unavailable。

### Regression tests

- existing ledger projection and write workflows；
- assigned-stock intake；
- agent plugin contract/smoke；
- analysis/Copilot；
- CLI；
- old monthly report/bridge adapters；
- exact close-advice suite: `tests/test_agent_plugin_smoke.py`, `tests/test_close_advice_contract.py`, `tests/test_close_advice_domain.py`, `tests/test_close_advice_reallocation_shadow.py`, `tests/test_close_advice_runner.py`, `tests/test_notification_compact.py`；
- assistant consumers: command parser, inbound control, renderer and tool bindings。

### Final validation

```bash
python3 -m ruff check .
python3 -m pytest -q
git diff --check
```

Full-suite failures unrelated to the branch must be evidence-classified; no weakening assertions to fit implementation。

## 9. Docs Decision

Required updates:

- `docs/OPTION_PERFORMANCE_DESIGN.md` — authoritative contracts/formulas；
- `docs/ASSIGNED_STOCK_RETURN_DESIGN.md` — shared projector and supported scope；
- `docs/migrations/OPTION_PERFORMANCE_V1_MIGRATION.md` — metric matrix, consumers, rollback/deprecation；
- `docs/AGENT_INTEGRATION.md` — new tools/examples；
- `docs/OM_AGENT_CAPABILITY_MAP.md` — capabilities/bridge semantics；
- `docs/AGENT_WIKI.md` and README public CLI references where applicable；
- Gateflow artifacts under `docs/gateflow/` and review artifacts under `docs/reviews/`。

No production config documentation change is planned because no new runtime config key is introduced。

## 10. Risks and Classified Residuals

| Risk | Classification / owner |
|---|---|
| General stock inventory for Short Call/Long Put | assigned to later work unit；current output explicit incomplete |
| External opening/ending cash facts absent | covered by S9 unavailable semantics；external integration later work unit |
| Immediate legacy deletion | assigned to later removal work unit after deprecation window |
| Historical evidence coverage initially sparse | covered by S4 quality/null semantics plus explicit capture/import；data backfill remains operational follow-up |
| FX attribution not separately decomposed | accepted v1 design limitation, documented as effective-time translation；separate FX attribution later if needed |
| Performance on large ledgers | test in final suite; materialization assigned to later work unit only if benchmark proves need |
| Current UTC month results change | covered by migration matrix and explicit reporting timezone change |

No unclassified residual risk remains for plan review entry。

## 11. Why This Is Not Overengineered

- It removes duplicate state/PnL logic instead of layering another independent projector。
- Domain contracts remain in the existing six planned domain files；one focused evidence-collection application adapter is added because live collection has distinct external-I/O ownership, rather than splitting money/FX/cash/PnL into speculative layers。
- General stock ledger, corporate actions, NAV/margin return, external PM changes and performance materialization are explicitly excluded。
- Historical facts are necessary because the user explicitly requires historical deterministic PnL/FX；they are not speculative abstraction。
- Separate PnL/cash bridges are necessary because their balance-sheet equations differ；this is semantic separation, not optional layering。
- Ten slices are sequencing/checkpoint boundaries for an operations-sensitive breaking refactor; they do not imply ten runtime services or state machines。

## 12. Completion Report Format

Final closeout artifact and user summary will contain:

1. work unit and draft PR URL；
2. changed architecture/contracts and public commands；
3. each slice commit and artifact path；
4. planreview/code-review/deepreview/PR-review finding status；
5. focused/full test and ruff results；
6. migration/compatibility status；
7. docs updated；
8. remaining risks with owner/destination；
9. explicit statement that no production config, Feishu, broker-facing or live position data was mutated；
10. next entry point: user reviews/merges draft PR, then legacy removal after deprecation window。

## 13. Plan Gate Decision

- **Completion status**: accepted-plan
- **Accepted at**: 2026-07-17 22:58:36 CST（本机时钟）
- **Plan re-review**: `docs/reviews/plan-review-20260717-225752.md` — `pass-with-risks`
- **Accepted findings addressed**: PR2-01 through PR2-06（全部 `已修复`）
- **Blocking open questions**: none
- **Residual risks**: classified in plan re-review；none blocks implementation
- **Next entry point**: implementation Slice S1
