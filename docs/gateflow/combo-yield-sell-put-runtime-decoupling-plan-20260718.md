# Gateflow Plan — Combo Yield 与 Sell Put 运行解耦

- **Gate**: plan
- **Work unit**: Combo Yield 与 Sell Put 的运行耦合审计与解耦
- **Date**: 2026-07-18
- **Goal artifact**: `docs/gateflow/combo-yield-sell-put-runtime-decoupling-goal-confirmation-20260718.md`
- **Status**: proposed

## Design decision

在 `run_symbol_monitoring()` 建立两个显式策略 step：Sell Put step 和 Combo Yield step。二者共享 required-data fetch 和 funding-put 配置，但调用、summary、异常结果与 enablement 独立。`run_sell_put_scan_and_summarize()` 恢复为只负责 Sell Put；`run_combo_yield_scan_and_summarize()` 由 symbol orchestrator 直接调用。

不引入 registry、base class 或通用 pipeline framework。仅增加一个明确 dependency 和一个窄 adapter/context preparation boundary。

## Behavioral contract

1. `sell_put.enabled` 只控制 Sell Put recommendation step。
2. `combo_yield.enabled` 只控制 Combo Yield recommendation step。
3. Combo Yield 使用 Sell Put 配置作为 funding-put underwriting 参数；Sell Put 配置对象存在但 `enabled=false` 时，其他参数仍可作为 Combo Yield 输入。
4. required-data `want_put/want_call` 是所有启用策略需求的并集。
5. Sell Put empty result 不传递为 Combo Yield input gate；Combo Yield独立扫描 funding-put universe。
6. Sell Put exception 不阻止 Combo Yield。策略 step exception 在 symbol orchestration 层转换为该策略的 fail-closed empty summary，并保留日志；基础 required-data 获取失败仍属于共享前置失败，不在本 work unit 隔离。
7. canonical `output_mode=separate` 完全独立。legacy `inline|both` 仅在 Sell Put labeled artifact存在时执行 attachment；Sell Put disabled/failed 时 separate Combo Yield 仍正常，inline attachment best-effort/fail-closed，不反向阻断 Combo Yield summary。

## Implementation slices

### S1 — Independent orchestration and runner ownership

**Files expected**

- `src/application/symbol_monitoring.py`
- `src/application/pipeline_symbol.py`
- `src/application/sell_put_steps.py`
- optionally a narrow adapter/helper in `src/application/` only if signature size demands it
- `tests/test_symbol_monitoring_fetch_spec_merge.py`
- focused Sell Put/Combo Yield tests

**Changes**

1. Extend `SymbolMonitoringDependencies` with `run_combo_yield_scan_fn` and an empty/failure summary surface if required.
2. Compute `want_combo_yield = yield_policy.enabled` independently of `market_want_put`.
3. Invoke Sell Put only when post-prefilter `want_put`; invoke Combo Yield independently when `want_combo_yield`.
4. Move Combo Yield invocation out of `run_sell_put_scan_and_summarize()`.
5. Preserve summary ordering as Sell Put, Combo Yield, Sell Call.
6. Add narrow per-strategy fail-closed exception isolation around Sell Put and Combo Yield invocations, with `log.exception` and explicit empty summary rows where existing report contract expects a row.
7. Pass original market Sell Put config to Combo Yield as `yield_sp`, while passing artifact paths/context explicitly.

**Tests**

- Sell Put disabled + Combo Yield enabled invokes only Combo Yield.
- Sell Put raises + Combo Yield enabled still invokes Combo Yield.
- Sell Put returns empty + Combo Yield enabled still invokes Combo Yield.
- Combo Yield raises does not erase Sell Put result and fails closed.
- existing account-prefilter behavior remains valid.
- summary order remains stable.

### S2 — Required-data and scheduled prefetch decoupling

**Files expected**

- `src/application/symbol_monitoring.py`
- `src/application/required_data_prefetch_planning.py`
- `src/application/multi_tick/required_data_prefetch.py`
- `src/application/required_data_planning.py` only if current canonical planner still couples enablement
- related required-data/prefetch tests

**Changes**

1. Replace `want_put and combo_enabled` gates with independent Combo Yield enablement.
2. Required data union:
   - put data when Sell Put or Combo Yield enabled;
   - call data when Sell Call or Combo Yield enabled;
   - RV only according to the independent strategy policies.
3. Preserve funding-put strike/DTE config for Combo Yield even when Sell Put recommendation is disabled.
4. Ensure both direct symbol fetch and scheduled multi-tick prefetch follow the same contract.

**Tests**

- disabled Sell Put + enabled Combo Yield requests put and call.
- disabled Sell Put + disabled Combo Yield does not request Combo Yield data.
- diagonal call DTE window remains intact.
- existing merged multi-account prefetch behavior remains unchanged.

### S3 — Compatibility, diagnostics, and documentation

**Files expected**

- `src/application/combo_yield_steps.py`
- tests for `output_mode` compatibility if needed
- `docs/STRATEGY_ARCHITECTURE.md`
- possibly `docs/DEPENDENCY_GRAPH.md` / `docs/dependency_graph.mmd` only if their runtime edge is authoritative and currently wrong

**Changes**

1. Remove unnecessary hard dependency on live Sell Put dataframe for separate mode; allow an empty/missing Sell Put artifact input.
2. Keep inline attachment best-effort and explicit; do not let compatibility behavior gate separate Combo Yield execution.
3. Add/retain trace reasons for Combo Yield’s own empty/failure states.
4. Update architecture docs to state independent runtime ownership and shared capability/config reuse.

**Tests**

- no Sell Put candidates still allows Combo Yield independent universe and recommendation.
- no Sell Put artifact with separate mode works.
- inline/both behavior is deterministic and fail-closed.

## Validation plan

Focused per slice, then aggregate:

```bash
python3 -m pytest \
  tests/test_symbol_monitoring_fetch_spec_merge.py \
  tests/test_combo_yield_steps.py \
  tests/test_sell_put_yield_enhancement_required_data_planning.py \
  tests/test_required_data_prefetch_inprocess.py

python3 -m pytest \
  tests/test_multi_tick_*.py \
  tests/test_unified_tick_entrypoint.py \
  tests/test_notify_symbols_markdown.py \
  tests/test_multi_tick_notify_format.py

python3 -m pytest tests/test_architecture_guards.py
```

Run broader affected-area tests if focused failures reveal shared contracts. Do not run production tick or notification commands.

## Docs decision

Update strategy architecture because the public conceptual contract changes from “product independent, runtime nested” to “product and runtime independent.” Update dependency graph only where it explicitly models the old `Sell Put -> Combo Yield` execution edge.

## Rollback strategy

Changes are facade-preserving and local to orchestration. Each slice receives its own accepted commit. Rollback can revert the independent orchestration commit(s) without modifying config or persisted state. No migration is required.

## Residual risks

- **Legacy inline/both output**: covered by S3; compatibility attachment may not exist when Sell Put is disabled, but separate result must remain valid.
- **Shared required-data failure**: explicitly outside strategy-step isolation and remains a symbol-level failure; assigned to a later work unit if product requires per-strategy fetch isolation.
- **Duplicate funding-put scans when both strategies run**: accepted current cost; eliminating it would couple results or require caching and is out of scope.
- **Dirty worktree overlap**: controlled by precise staging and diff review before every commit.

## Completion criteria

All slices accepted; plan/code/deepreview artifacts complete; focused and aggregate validation passes; docs match runtime; only work-unit files are committed; draft PR opened and reviewed through Gateflow final closeout.

## Plan review fixes

### Resolution of PR-1 — facade ownership

**Accepted.** Add a symbol-level facade in `src/application/combo_yield_steps.py`, tentatively `run_combo_yield_for_symbol_and_summarize(...)`. Its public application inputs are limited to raw symbol/config/runtime context already available to `run_symbol_monitoring`: `base`, symbol identifiers, `symbol_cfg`, original Sell Put funding config, top-N, required/report directories, schedule flag, converter, portfolio context, and global Sell Put liquidity/event-risk defaults. The facade owns:

- resolving Combo Yield config and policy;
- resolving funding-put candidate window, liquidity and event-risk defaults;
- constructing optional Sell Put labeled artifact path for legacy inline attachment;
- wiring the low-level scan/filter/pairing helpers;
- materializing Combo Yield empty artifacts on disabled/failure paths.

`run_symbol_monitoring` does not import or construct `CandidateWindowDefaults` or other low-level scan details. `run_sell_put_scan_and_summarize` no longer calls or prepares Combo Yield.

### Resolution of PR-2 — fail-closed artifacts

**Accepted.** Each independently invoked strategy step must clear/materialize its current report artifacts before work that may throw, or its symbol-level facade must do so in the exception path. For Combo Yield this includes candidates and alerts plus any current-run diagnostic outputs that could otherwise remain stale. For Sell Put, preserve existing empty artifact behavior and add an orchestration failure helper only where injected exceptions bypass it.

The symbol orchestrator logs the exception with strategy and symbol, appends the strategy’s explicit empty summary, and continues to the next independent strategy. Tests seed stale artifacts, inject a failure, and assert the failing strategy’s current artifacts are empty/replaced while the other strategy still executes.

Programmer/config errors are still visible through `log.exception` and diagnostics; they are not reported as successful scans. Shared required-data acquisition remains outside this isolation.

### Resolution of PR-3 — planner scope

**Accepted.** `src/application/required_data_planning.py` is a protected non-change target unless a failing test proves a defect. It already models Combo Yield independently. S2 changes only caller-level gates in:

- `src/application/symbol_monitoring.py`;
- `src/application/required_data_prefetch_planning.py`;
- `src/application/multi_tick/required_data_prefetch.py`.

Existing canonical planner tests will be used as regression evidence, especially diagonal DTE behavior.

## Revised slice boundaries

- **S1**: introduce the Combo Yield symbol facade, remove nested invocation, add independent orchestration and strategy failure artifacts/tests.
- **S2**: repair only caller/prefetch enablement gates; leave canonical planner unchanged.
- **S3**: finalize inline/both compatibility, diagnostics and docs after independent separate-mode behavior is proven.
