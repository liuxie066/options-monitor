# Run-scoped Required-data Snapshot Fix Plan

## 1. Problem statement

2026-07-28 13:00 的同一轮 HK 扫描中，`lx` 与 `sy` 对同一 Sell Put 市场机会得到了不同结果。直接证据显示账户流水线先后重复执行 required-data prefetch：

- `lx` 读取 3690.HK 合约时 ask/IV 对应 `IV/RV=1.092702`，通过 `1.08` 门槛；
- `sy` 数秒后重新取数，同一合约对应 `IV/RV=1.068102`，被同一门槛过滤；
- 两个账户的 Sell Put 市场配置一致，且 `apply_prefilters()` 对 Sell Put 保持账户不变量；
- 当第一次 prefetch 有单 symbol 错误时，`account_run.py` 只在 `errors == 0` 时标记完成，第二个账户因此重新执行整轮 prefetch。

根因不是账户资金或排名策略，而是同一 `run_id` 没有冻结统一的 market-data planning facts 与 payload。当前共享目录只是可复用路径，不是不可变快照契约。

## 2. Goal

对一次需要扫描的 tick：

1. 在任何账户 scan pipeline 启动前，只构造一次 run-scoped required-data plan；
2. 每个 symbol 的 spot、expirations、fetch bounds、raw/CSV bytes 和 quote receipt 作为同一个冻结事实集合发布；
3. 所有扫描账户只消费该集合，不重新发现行情、不补取、不改写；
4. Sell Put 等账户不变的市场候选/拒绝事实对账户顺序和账户并发保持一致；
5. 资金、持仓、容量和 Covered Call 成本线等账户事实仍可造成有类型、可解释的账户差异；
6. 单 symbol 行情失败只抑制该 symbol 的动作并形成 data gap；全部 market snapshot 不可用时不发送 normal actionable brief。

## 3. Non-goals

- 不稳定不同 `run_id` 之间的报价；跨 run 候选变化仍是正常市场行为。
- 不调整 `IV/RV`、收益率、流动性、事件风险、排名或容量阈值。
- 不修改调度时间、material-change threshold、通知路由或重试策略；允许为“证据不可用/恢复”补齐既有 diff 状态语义。
- 不修改 `config.yaml`、`config.us.json`、`config.hk.json` 或新增生产配置键。
- 不建立数据库、通用事件总线或跨服务 snapshot framework。
- 不发布版本、不部署、不发送真实通知。

## 4. Authoritative boundaries

### 4.1 Ownership

- `tick_account_execution.py` 持有 run barrier：在共享绝对时限内准备账户 planning context、构造并执行一次 market prefetch、seal manifest，然后才允许提交账户 futures。
- 新的窄模块 `src/application/prepared_portfolio_context.py` 持有可终止的 context worker、临时产物校验和 parent-only 原子发布；不得包含 market planning 或策略判断。
- `required_data_prefetch.py` 持有 run-scoped market plan、fetch、multiplier enrichment 和 per-symbol quote receipt。
- 新的窄模块 `src/application/required_data_snapshot.py` 只持有 manifest schema、原子发布和只读校验；不得发起 OpenD 请求或包含扫描策略。
- `symbol_monitoring.py` 持有 symbol 级消费与策略产物降级；有 manifest 时不得重新 planning/fetch。
- `daily_decision_brief_service.py` 只把 manifest/prefetch failure 投影为 data gap，不推导不存在的候选或拒绝原因。
- 原有 `--shared-required-data` 继续只表示共享目录；冻结语义只由新的显式 manifest 参数触发。

### 4.2 Import direction

```text
tick orchestration
    -> account planning-context preparation
    -> required-data prefetch
    -> required-data snapshot manifest
    -> account pipeline consumer
    -> strategy scans / Daily Brief
```

`domain/domain/` 不依赖 `src/`；manifest 和 orchestration 都留在 application 层。

## 5. Frozen inputs contract

### 5.1 Account planning context

Sell Put 的 market plan 已由配置决定且账户不变，但 direct Sell Call 的有效 `min_strike` 会使用账户持仓 `avg_cost`。因此 run plan 必须覆盖账户有效需求并集，不能只使用第一个账户或 base config。

在 prefetch 前：

1. 从 `account_run.py` 提取纯函数 `build_account_runtime_config(...)`，复用现有 account、market 和 symbol whitelist 过滤逻辑；`run_one_account()` 与 barrier 都必须调用同一个函数。
2. 对本轮 `scan_should_run=true` 的账户，在一个 run-scoped `portfolio_timeout_sec` 绝对时限内并发准备 portfolio context；该时限是本轮全部 scanning accounts 共享的 wall-clock budget，不得按账户串行重置。
3. 由于当前 OpenD/Futu 调用没有可取消的 application deadline，context preparation 使用每账户一个窄 subprocess，而不是不可终止的 thread：
   - child 只读取 portfolio context，使用 `write_cache=false`，且不得调用 `_persist_source_snapshot()` 或写正式 run/account state；
   - child 只写 parent 指定的 account-scoped 临时目录；
   - parent 同时启动全部 scanning accounts 的 child，使用同一个 monotonic absolute deadline；context preparation 不复用可能为 1 的 account pipeline worker pool，避免慢账户因顺序饿死后续账户；
   - deadline 到达后 parent 先 terminate，短 grace 后 kill 未完成 child，并丢弃其临时目录；
   - 只有 parent 能校验 completed child 的 `run_id/account/payload_sha256`，再把 context/source snapshot 与 manifest 原子提升到正式 account state；
   - 迟到、被杀或 token 不匹配的 child 不能改写已发布结果。
4. 为每个账户原子写入 `prepared_portfolio_context.v1.json`，至少包含：
   - `schema_version`
   - `run_id`
   - `account`
   - `status: ready | unavailable`
   - `portfolio_context_relpath`（ready 时）
   - `payload_sha256`（ready 时）
   - `source_as_of_utc`（有上游时间时）
   - typed `reason`（unavailable 时）
5. deadline 后尚未完成的账户写 `status=unavailable`、`reason=portfolio_context_deadline_exceeded`；child 自身异常使用稳定的 typed reason。任何单账户失败都不能延长共享 deadline。
6. account pipeline 通过新增的显式内部参数 `--prepared-portfolio-context-manifest` 读取该 artifact；提供参数后只验证和读取，不刷新 portfolio context。
7. option-position context 与 FX 不参与 market fetch plan，保持现有加载流程；本 work unit 不冻结它们。

Portfolio context unavailable 时：

- 对该账户 direct Sell Call 按现有语义禁用；
- Sell Put 和 Combo Yield 的配置需求仍进入 run plan；
- 该账户后续 brief 按现有 account-context authority 规则降级或 blocked；
- 不因为一个账户 context 失败而让另一个账户重新取行情。

### 5.2 Cross-account plan union

`required_data_prefetch_planning.py` 新增纯 planning helper，输入：

- 过滤后的 base symbol configs；
- 每个 scanning account 的 prepared portfolio context（ready 或 unavailable）；
- 已解析 profiles/templates。

对每个 `symbol + source + host + port`：

1. 先保留 base config 的策略需求，确保 context unavailable 不会移除 Sell Put/Combo 的市场需求；
2. 对每个 ready account 应用与 `symbol_monitoring.py` 相同的 direct Sell Call eligibility 和 `resolve_effective_sell_call_min_strike()`；
3. 复用现有 `merge_prefetch_symbol_configs()` / side-plan merge，取：
   - 最小的 `min_dte`
   - 最大的 `max_dte`
   - expirations 并集
   - 最小的 side `min_strike`
   - 最大的 side `max_strike`
   - 任一策略需要时包含 realized volatility
4. 结果必须与账户顺序无关，并写入现有 `global_required_data_plan`；不新增第二套 ranking 或 candidate planning。

账户 pipeline 不再构造 market fetch plan；prepared context 只用于策略过滤和容量计算。

## 6. Snapshot state machine and manifest

### 6.1 State transitions

```text
planning
  -> fetching
  -> enriching
  -> sealing
  -> complete | partial | failed
```

- `planning` 失败：不发布可消费 manifest，不启动账户 futures。
- `fetching` 允许 existing retry/rate-limit policy 在单次 prefetch invocation 内工作。
- `enriching` 在 receipt 发布前完成 multiplier 补齐。
- `sealing` 验证每个 ready symbol 的 exact current bytes 与本 `run_id` receipt。
- 只有 terminal manifest 才能被账户 pipeline 消费。
- manifest 原子写入是 commit marker；中途残留的 raw/CSV/receipt 没有 terminal manifest 时均不可消费。

### 6.2 Manifest path

```text
output_runs/<run_id>/state/required_data_snapshot_manifest.json
```

### 6.3 Manifest v1

```json
{
  "schema_version": "required_data_snapshot_manifest.v1",
  "run_id": "<run_id>",
  "status": "complete|partial|failed",
  "plan_id": "<sha256>",
  "sealed_at_utc": "<timestamp>",
  "required_data_root_relpath": "../required_data",
  "symbols": {
    "3690.HK": {
      "status": "ready",
      "fetch_plan": {},
      "fetch_policy_hash": "<sha256>",
      "receipt_relpath": "position_advice_sources/quotes/.../receipt.json",
      "receipt_hash": "<sha256>",
      "snapshot_id": "<id>",
      "raw_json_relpath": "raw/3690.HK_required_data.json",
      "required_data_csv_relpath": "parsed/3690.HK_required_data.csv"
    },
    "9898.HK": {
      "status": "failed",
      "reason": "empty_chain",
      "error_type": "RequiredDataFetchError"
    }
  },
  "summary": {
    "symbols_total": 5,
    "ready": 4,
    "failed": 1
  }
}
```

约束：

- `symbols` 使用 canonical uppercase symbol key；
- ready entry 必须有本 `run_id` 的 receipt；不得用“最新 receipt”代替精确 producer binding；
- receipt 必须绑定 manifest 所指向的当前 raw/CSV bytes；
- failed entry 不得指向旧文件；
- `status=complete` 当且仅当所有计划 symbol ready；
- `status=partial` 当且仅当至少一个 ready 且至少一个 failed；
- `status=failed` 当且仅当零 ready；
- manifest 不复制 raw/CSV payload，只引用并校验现有 immutable receipt，避免第二份事实源。

### 6.4 Multiplier ordering

当前 `symbol_monitoring.py` 会在账户阶段调用 `apply_multiplier_cache_to_required_data_csv()`，即使没有真实变化也可能改写 CSV。修复后：

1. multiplier enrichment 移到 prefetch 的每个成功 symbol 内；
2. 顺序固定为 `save_outputs -> multiplier enrichment -> publish quote receipt`；
3. receipt/manifest 发布后，账户消费者不得调用 multiplier writer；
4. 账户前后 raw/CSV bytes、hash 和 mtime 均保持不变。

## 7. Account execution barrier

`run_tick_account_execution()` 顺序改为：

1. 计算本轮真正需要扫描的账户；
2. 为这些账户准备相同逻辑生成的 runtime config 与 prepared portfolio context；
3. 构造 cross-account plan union；
4. 调用 `prefetch_required_data()` **恰好一次**；
5. 完成 multiplier enrichment、receipt 校验和 terminal manifest 原子发布；
6. 把同一份 prefetch summary 字节复制到每个 scanning account state，并记录相同的 `manifest_sha256`；
7. `status=complete|partial` 时才提交 account futures；
8. `status=failed` 或 manifest 发布失败时，不运行 normal scan pipeline；barrier 为每个 scanning account 构造显式 synthetic `AccountResult` 和 account metrics，再交给现有 notification flow 生成 blocked/operational outcome；不得发送 normal actionable brief。

删除 `account_run.py` 中 `_run_required_data_prefetch()`、共享 prefetch lock 和 `errors == 0` 才算 done 的状态。新的成功信号是 `manifest sealed`，不是 `prefetch errors == 0`：

- partial manifest 也已完成本 run 的唯一 prefetch，账户不得重跑；
- force mode 只影响 barrier 中这一次 fetch；
- serial / parallel account mode 的 prefetch 次数都为 1；
- 新 tick 使用新 `run_id`，可正常重新取数；
- event prefetch 保持现有独立语义，不与 required-data manifest 合并。

### 7.1 Pre-pipeline terminal outcome contract

barrier 不得以“无 account outcome”代表失败，否则 `tick_notification_flow` 没有可装配的账户输入。状态表固定为：

| Snapshot outcome | Account pipeline | `results` | `ran_pipeline_accounts` | `scheduled_scan_targets_by_account` | `prefetch_done` | Brief / delivery |
|---|---|---|---|---|---|---|
| `complete` | 每个 scanning account 启动 | 使用真实 `AccountResult` | 成功启动并完成 pipeline 的账户 | 从 precomputed scan decision 复制 | `true` | 现有 normal policy |
| `partial` | 每个 scanning account 启动 | 使用真实 `AccountResult` | 成功启动并完成 pipeline 的账户 | 从 precomputed scan decision 复制 | `true` | 可靠 action + typed gaps |
| terminal `failed` | 不启动 | 每个 scanning account 一个 synthetic result：`ran_scan=false`、`notification_text=""`、`decision_reason=required_data_snapshot_failed`，`should_notify` 保留 scheduler/notify decision | `[]` | 必须仍从 precomputed scan decision 填充 | `true` | assembler 以 `pipeline_succeeded=false` 生成 blocked；fixed due 可走既有 fixed-failure 通知，禁止 normal actionable |
| manifest publish/validation failure | 不启动 | 同上，但 reason 为 `required_data_snapshot_manifest_unavailable` | `[]` | 必须仍填充 | `false` | 同 terminal failed |

补充约束：

- account metrics 必须记录 `run_id/account/ran_pipeline=false/snapshot_status/typed_reason`，不得伪装成成功扫描；
- notification flow 继续用 `scheduled_scan_targets_by_account` 使 fixed-due 失败可见，但 `ran_pipeline_accounts=[]` 保证 `pipeline_succeeded=false`；
- scheduled non-fixed、manual 和 `--no-send` 继续遵守现有路由/发送授权，不因该 failure contract 获得新的发送权限；
- run finalization、重试和进程退出码复用现有 pipeline-failure 语义；本 work unit 不新建第二套 retry policy。

## 8. Frozen consumer contract

### 8.1 Additive CLI/API

新增内部 pipeline 参数：

```text
--required-data-snapshot-manifest <absolute path>
--prepared-portfolio-context-manifest <absolute path>
```

`run_pipeline_script()` 仅在 tick account execution 传递它们。未传参数时：

- `--shared-required-data` 和 `ensure_required_data()` 保持现有兼容行为；
- dev/manual pipeline 仍可在 coverage 不足时 fetch；
- 不改变仓库外既有调用的默认语义。

传入 required-data manifest 时：

- 验证路径位于当前 `output_runs/<run_id>`；
- 验证 schema、run id、plan id、terminal status、symbol entry 和 exact receipt；
- ready symbol 直接返回 manifest quote evidence；
- 禁止调用 `get_underlier_spot()`、`list_option_expirations()`、`execute_required_data_opend()`、`save_outputs()` 或 multiplier writer；
- 禁止基于账户配置重新校验/扩展 market fetch plan；
- manifest 或 receipt 被篡改时 fail closed。

### 8.2 Symbol-level failure handling

新增 typed `FrozenRequiredDataUnavailable`，只表达以下原因：

- symbol entry missing；
- symbol status failed；
- manifest/run/schema mismatch；
- receipt missing/expired/mismatched；
- bound raw/CSV bytes mismatch。

`symbol_monitoring.py` 在 required-data acquisition 边界捕获该类型，不让它落入 `pipeline_watchlist._failure_rows()` 的通用异常：

1. 对该 symbol 所有已启用 strategy family 写 `strategy_scan_failure`；
2. 物化 canonical empty candidate/reject artifacts；
3. summary row 为 `candidate_count=0`，但 note 必须是“行情快照不可用”，不得表述为“无符合候选”；
4. Position Advice candidate capture 对每个已启用 family 写：
   - `status=failed`
   - `reason=required_data_snapshot_unavailable`
   - `snapshot_id/receipt_relpath` 在可获得时保留；
5. 其它 ready symbols 继续扫描。

普通策略异常仍由现有各 strategy `try/except` 处理；不得把所有错误泛化成 frozen-data failure。

为避免 Daily Brief 用“candidate rows 是否为空”猜测 source availability，每个已启用的 `symbol + strategy_family` 必须原子写一个独占文件：

```text
<account-report-dir>/<canonical-symbol>_<strategy-family>_scan_status.json
```

内容使用 `strategy_scan_status.v1`，至少包含：

- `run_id/account/market/symbol/strategy_family`
- `status: completed | unavailable | failed`
- `candidate_count`（仅 `completed` 必填，允许为 0）
- typed `reason`（`unavailable|failed` 必填）
- `snapshot_id/receipt_relpath`（可获得时）
- canonical candidate/reject artifact 的相对路径与 SHA-256

语义固定：

- ready snapshot 上策略正常返回，即使 `candidate_count=0` 也写 `completed`；
- `FrozenRequiredDataUnavailable` 写 `unavailable`；
- 非行情的策略执行异常写 `failed`；
- 每个组合有独占文件，避免并行 symbol worker 竞争追加同一 JSONL；
- 未产生 status 的已启用组合视为 `failed: strategy_scan_status_missing`，不得视为成功的 0 candidates。
- status file 是该组合的 commit marker：必须在 canonical candidate/reject artifacts 写入并通过 schema/hash 校验后最后发布；`completed` 不得引用 missing/mismatched artifact。

pipeline coordinator 使用与 symbol dispatch 相同的 effective runtime config 先构造 expected `symbol + strategy_family` 集合；所有 symbol workers join 后：

1. 校验每个 expected status file 的 `run_id/account/market/symbol/family` 及其 canonical artifact hashes；
2. 对缺失或 invalid 文件补成 `failed: strategy_scan_status_missing|invalid`；
3. 原子发布 `strategy_scan_status_index.v1.json`，包含完整 expected matrix、每项 status/path 和 aggregate counts；
4. Daily Brief 只消费该 index 判断 frozen path availability，不用 glob 结果或 runtime config 再猜 expected 集合。

## 9. Daily Brief and notification semantics

### 9.1 Prefetch summary normalization

run barrier 生成的 canonical summary 沿用当前 `required_data_prefetch.v1` 顶层结构：

- 顶层 `errors`
- `symbols` list
- `results`
- `global_required_data_plan`
- `quote_receipts`
- 新增 `snapshot_manifest_relpath` 和 `snapshot_manifest_sha256`

`daily_decision_brief_service._append_prefetch_gaps()`：

- 首先解析当前顶层 `errors` 与 `symbols` list；
- 兼容 legacy `symbols` mapping / nested `summary.errors`，但不再把 legacy shape 当唯一来源；
- 每个 failed symbol 只生成一个 canonical `scope=symbol` gap；
- 不把同一失败重复投影成 symbol gap、prefetch gap 和多个 `source_artifact_missing`。

`strategy_scan_status_index.v1` 再把 availability 投影到可关联 action identity 的 family scope：

- 每个 `unavailable|failed|missing` status 生成一个 gap，必须包含 `market/symbol/strategy_family/reason/source_status_path`；
- prefetch symbol gap 与 family gap 按 `(market, symbol, strategy_family, reason)` 去重，symbol-only gap 不直接参与 candidate evidence lifecycle；
- evidence-hold reconcile 只消费同时具有 `symbol + strategy_family` 的 typed gap，不从自由文本或 candidate rows 反推。

### 9.2 Decision policy

- Daily Brief 以 `strategy_scan_status_index.v1` 而不是 candidate row count 判断 source availability：
  - 任一已启用组合为 `completed`（即使 candidate count 为 0），候选 source 仍可用；
  - 只有全部已启用组合均为 `unavailable|failed|missing` 时才加 candidate-source blocker；
  - “一个失败组合 + 其它 completed 但 0 candidates”只能 `degraded`，不得触发 `candidate_strategy_execution_failed` blocked；
  - legacy run 缺少 status artifact 时保留现有兼容读取，但 frozen-manifest path 必须有 status，缺失即 typed failed。
- **partial**：brief 为 `degraded`；受影响 symbol/family 的新 action 被抑制并显示明确 data gap；其它可信 action 仍可 `live_actionable`，符合现有“单 symbol failure 不阻断其它行动”规则。
- **failed / no ready symbols**：不发送 normal actionable brief；输出 account/run blocked operational outcome。
- 数据不可用不等价于“0 候选”或“候选失效”。data gap 与 candidate action 的 canonical correlation key 为 `(market, symbol, strategy_family)`。

### 9.3 Evidence-unavailable lifecycle

为避免本轮错误发出 `candidate_invalidated`，又避免下一轮丢失上一候选身份，使用既有 action identity 做一个最小 evidence-hold 状态，不建立第二套候选数据库：

1. 新增纯 domain 函数 `reconcile_daily_decision_brief_evidence(previous, current)`。
2. `persist_daily_decision_brief_success()` 在同一 account/market lock 内读取 previous 后、写 revision 前调用该函数，保证 reconcile 与 current advancement 原子化。
3. 对 previous 中 active 的 P0/P1 opening candidate：若 current 缺少同 `action_id`，但 current data gaps 覆盖其 correlation key，则复制该 action 到 current：
   - 保持原 `action_id` identity、priority 和候选证据；
   - `state=observe`；
   - `evidence_state=unavailable`；
   - `evidence_gap_key` 和 typed reason 指向当前 gap；
   - renderer 只展示“行情证据不可用/待恢复”，不得把 held action 渲染为当前推荐。
4. diff 状态机固定为：

| Previous | Current | Material change |
|---|---|---|
| active candidate | matching evidence hold | `candidate_evidence_unavailable` |
| evidence hold | same evidence hold | none |
| evidence hold | same action active | `candidate_evidence_recovered` |
| evidence hold | action missing 且当前无 matching gap | `candidate_invalidated` |
| active candidate | action missing 且当前无 matching gap | 保持既有 `candidate_invalidated` |

5. `candidate_evidence_unavailable` 和 `candidate_evidence_recovered` 沿用原 action priority 并属于 material change；不修改 material threshold、fixed/candidate route、账户特例或发送授权。
6. held action 不计入 active candidate identity/pending-candidate alert，不参与容量执行；它只保存跨 degraded run 的证据生命周期。

## 10. Implementation slices

### S1 — Characterization and pure planning

Files:

- `src/application/account_run.py`
- `src/application/required_data_prefetch_planning.py`
- `src/application/prefilters.py`（仅复用，不改变 Sell Put 语义）
- `tests/test_prefilters_cash_limits.py`
- `tests/test_required_data_prefetch_inprocess.py`
- `tests/test_pipeline_fetch_read_model_boundary.py`

Work:

- 提取纯 account runtime config helper；
- 构造 base + prepared-account effective configs 的 cross-account union；
- 证明 Sell Put 计划账户不变、direct Sell Call 覆盖不同 `avg_cost` 的需求并集；
- 证明 account order 不影响 `plan_id`。

Exit:

- 不改 fetch 或 pipeline 行为；
- planning tests 全绿；
- 同一输入的 canonical plan hash 稳定。

### S2 — Manifest producer and immutable consumer

Files:

- `src/application/required_data_snapshot.py`（新增窄模块）
- `src/application/multi_tick/required_data_prefetch.py`
- `src/application/opend_symbol_outputs.py`
- `src/application/multiplier_steps.py`（只复用/必要时提供“有变化才写”能力）
- `src/application/required_data_steps.py`
- `src/application/symbol_monitoring.py`
- `src/application/pipeline_symbol.py`
- `src/application/pipeline_runtime.py`
- `src/infrastructure/external_services.py`
- focused tests

Work:

- 定义/验证 manifest v1；
- multiplier 前移到 receipt 之前；
- exact receipt 验证增加 expected producer `run_id`；
- 增加显式 frozen consumer path；
- 保持未传 manifest 的 legacy path 不变。

Exit:

- frozen consumer 的网络/discovery/write spies 均为 0；
- 篡改任一 raw/CSV/receipt/manifest 都 fail closed；
- legacy shared-required-data tests 保持通过。

### S3 — Run barrier and prepared portfolio context

Files:

- `src/application/tick_account_execution.py`
- `src/application/account_run.py`
- `src/application/prepared_portfolio_context.py`（新增窄 coordinator/worker 模块）
- `src/application/pipeline_context.py`
- `src/application/portfolio_context_service.py`
- `src/application/pipeline_runtime.py`
- `src/infrastructure/external_services.py`
- `domain/storage/repositories/state_repo.py`（仅在现有原子 JSON helper 不足时）
- tick/account orchestration tests

Work:

- 把 `portfolio_timeout_sec` 定义为全部 scanning accounts 共享的 monotonic absolute deadline；
- 用可 terminate/kill 的 account-scoped subprocess 并发准备只读 context，parent-only 原子提升产物；
- 在 account futures 之前完成 bounded context preparation、union plan、唯一 prefetch 和 manifest seal；
- 移除 account-owned required-data prefetch/lock；
- 传递两个显式 manifest；
- 将同一 canonical summary 复制到 scanning account state；
- 为 terminal failed / manifest unavailable 构造完整 synthetic account outcomes。

Exit:

- serial / parallel / reversed order / force / partial failure 均只调用一次 prefetch；
- account future 的提交事件一定发生在 manifest seal 之后；
- partial error 不触发第二次 prefetch；
- 两个 blocking context workers 的总 wall clock 不超过 `portfolio_timeout_sec + kill_grace + test_tolerance`，而不是 `N * portfolio_timeout_sec`；
- 超时 worker 无法在 parent 发布 unavailable 后迟到改写 account state；
- full failure 时 notification flow 收到每个 scanning account 的 typed result、scan target 和 metrics。

### S4 — Failure projection and Daily Brief

Files:

- `src/application/symbol_monitoring.py`
- `src/application/pipeline_watchlist.py`（只保留 generic fallback；typed failure 不应到达这里）
- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_repository.py`
- `src/application/daily_decision_brief_renderer.py`
- `src/application/tick_notification_flow.py`
- `domain/domain/daily_decision_brief.py`
- Daily Brief / Position Advice capture / notification tests

Work:

- 每个已启用 symbol/family 原子物化 canonical `strategy_scan_status.v1`，coordinator 发布完整 expected `strategy_scan_status_index.v1`；
- typed symbol failure 物化 canonical artifacts；
- 修复 canonical + legacy prefetch summary shape；
- 以 status aggregation 明确 partial/failed 的 brief authority，移除 row-count blocker proxy；
- 在 repository lock 内 reconcile evidence hold，并扩展 domain diff/renderer；
- 防止 data unavailable 被表述为 zero candidates 或 invalidated condition。

Exit:

- partial failure 只抑制失败 symbol；
- full failure 不产生 normal actionable brief；
- ready symbol/family 正常返回 0 candidates 时仍算 source available；
- active -> unavailable -> recovered/absent 的三轮 diff 状态机无 false invalidation 且不丢候选身份；
- Position Advice capture 与 Daily Brief 引用同一 snapshot/gap reason。

### S5 — Regression and documentation

Files:

- `docs/AGENT_WIKI.md`（仅在公共操作说明需要时）
- focused tests listed below

Work:

- 执行 focused + broader tick/notification tests；
- 记录 no-send dry-run 验证方法；
- 不修改 VERSION/CHANGELOG，除非后续用户单独授权 release。

## 11. Acceptance tests

### 11.1 Root-cause regression

模拟同一 3690.HK 合约在第二次行情调用会从 `IV/RV > 1.08` 漂移到 `< 1.08`：

- prefetch 只返回第一次快照；
- `lx`、`sy` 的 market candidate/rejection facts 完全相同；
- 第二次 spot、expiration、chain、snapshot 调用均不存在；
- 两账户资金容量允许不同，最终 `contracts/headroom` 可不同。

### 11.2 Account-plan union

- `lx` / `sy` direct Sell Call 使用不同 `avg_cost`；
- union fetch plan 覆盖两者最大有效 strike；
- 交换账户顺序，plan payload 和 `plan_id` 不变；
- 一个账户 portfolio context unavailable 时，该账户 call disabled，但 Sell Put plan 与另一个账户不受影响；
- 两个 context child 同时阻塞时，总 wall clock 受一个共享 absolute deadline 约束；
- 一个 child 提前完成、另一个超时时，前者 parent-published 为 ready，后者为 `portfolio_context_deadline_exceeded`，超时 child 之后不能改写正式产物。

### 11.3 Concurrency and retry

参数化：

- `account_workers=1`
- `account_workers=2`
- account order `lx,sy` / `sy,lx`
- `force_mode=false/true`
- prefetch complete/partial

统一断言：

- `prefetch_required_data` 调用数为 1；
- manifest seal 早于第一个 account future submit；
- partial manifest 不重跑；
- 新 `run_id` 可以再次 prefetch。

### 11.4 Immutability and provenance

- seal 前完成 multiplier enrichment；
- seal 后两个账户执行前后 raw/CSV/receipt 的 bytes、SHA-256 和 mtime 不变；
- manifest receipt 的 `producer_run_id` 精确匹配当前 run；
- spot/expiration discovery、fetch、save 和 multiplier writer spy 均为 0；
- 使用别的 run receipt、过期 receipt、修改 CSV 一字节或缺 manifest 均 fail closed。

### 11.5 Failure semantics

- 单 symbol `empty_chain`：
  - manifest `partial`
  - 两账户看到相同 gap
  - 其它 symbols 正常扫描
  - coordinator index 覆盖全部 expected symbol/family
  - 其它 ready symbol/family 正常返回 0 candidates 时 status 为 `completed`
  - Daily Brief 为 degraded 而不是 `candidate_strategy_execution_failed` blocked
  - 失败 symbol 有 canonical empty/error artifacts
  - brief 不写成“0 候选”
  - candidate capture 为 typed failed
- 所有 symbols failed：
  - manifest `failed`
  - account scan pipeline 不启动
  - 每个 scanning account 都有 synthetic `AccountResult`、metrics 和 scheduled target
  - `ran_pipeline_accounts=[]` 且 assembler 收到 `pipeline_succeeded=false`
  - normal actionable brief 不发送
  - fixed due 只允许既有 fixed-failure delivery，operational blocked outcome 可审计
- manifest 原子发布失败：
  - account futures 不提交
  - synthetic reason 为 `required_data_snapshot_manifest_unavailable`
  - 残留文件不可消费

### 11.6 Evidence lifecycle and diff

同一 candidate identity 连续执行：

1. run A：active P1 candidate；
2. run B：matching required-data gap、无新 candidate；
3. run C1：证据恢复且同 candidate active；或 run C2：证据恢复且 candidate absent。

断言：

- run B persisted brief 含 `state=observe/evidence_state=unavailable` 的同 identity hold；
- A -> B 只有 `candidate_evidence_unavailable`，没有 `candidate_invalidated`；
- B -> B 不重复 material change；
- B -> C1 为 `candidate_evidence_recovered`；
- B -> C2 为 `candidate_invalidated`；
- renderer 不把 hold 表述成当前 Sell Put/Covered Call 推荐；
- hold 不进入 pending candidate identities 或容量执行。

### 11.7 Compatibility

- 未传 `--required-data-snapshot-manifest` 时，manual/dev pipeline 保持 coverage 不足可补取的现有行为；
- 原 `--shared-required-data` help/behavior 不被重新定义；
- existing Position Advice quote receipt consumers 继续通过；
- event prefetch 行为和测试不变。

## 12. Validation commands

优先使用项目 Python 3.12 环境：

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_prefilters_cash_limits.py \
  tests/test_required_data_fetch_planning.py \
  tests/test_required_data_prefetch_inprocess.py \
  tests/test_pipeline_fetch_read_model_boundary.py \
  tests/test_symbol_monitoring_fetch_spec_merge.py
```

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_prepared_portfolio_context.py \
  tests/test_required_data_snapshot.py \
  tests/test_multi_tick_*.py \
  tests/test_unified_tick_entrypoint.py \
  tests/test_daily_decision_brief_domain.py \
  tests/test_daily_decision_brief_repository.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_position_advice_candidate_capture.py
```

其中 `tests/test_prepared_portfolio_context.py` 与 `tests/test_required_data_snapshot.py` 是本方案计划新增的 focused test 文件；若其它测试文件实际名称已变化，先用 `rg --files tests` 解析精确目标，不得跳过同等覆盖。

最终执行：

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider
```

全仓已知基线噪声必须与本 work unit 新回归分开报告；不得通过放宽 assertion 掩盖失败。

## 13. Observability and handoff evidence

每个 run 至少可审计：

- `run_id`
- `plan_id`
- manifest path/hash/status
- portfolio context absolute deadline、per-account child exit/timeout reason 和 parent promotion time
- per-symbol ready/failed、snapshot id、receipt path/hash
- per-symbol/family `strategy_scan_status` 和完整 expected index/hash
- prefetch invocation count
- account pipeline start time
- frozen consumer validation result
- per-account summary hash
- data gaps、evidence hold 和被抑制的 symbol/strategy

实施完成后的 handoff 必须给出：

1. 修改文件；
2. focused/full tests；
3. synthetic quote-drift regression；
4. no-send run artifact 路径；
5. 未执行发布/部署/真实通知的明确边界。

## 14. Prior review resolution

对 `docs/reviews/plan-review-20260728-131431.md`：

- **PR-01**：用 prepared portfolio context + base/account plan union 关闭；
- **PR-02**：用 terminal manifest 冻结 planning facts 与 payload，consumer 禁止 discovery 关闭；
- **PR-03**：用 typed symbol failure、canonical artifacts、capture status 和 Daily Brief policy 关闭；
- **PR-04**：保留 `--shared-required-data` 兼容语义，仅新增显式 frozen manifest 参数关闭。

对 `docs/reviews/plan-review-20260728-132638.md`：

- **PR2-01**：用共享 monotonic absolute deadline、可终止 account subprocess、child 临时目录和 parent-only promotion 关闭；
- **PR2-02**：用 per-symbol/family `strategy_scan_status.v1` 和 availability aggregation 关闭，completed/0 candidates 不再被 row-count proxy 判为 blocked；
- **PR2-03**：用 repository-lock 内的 evidence-hold reconcile 和四态 diff lifecycle 关闭，数据缺口不再误报 invalidated，也不丢下一轮候选身份；
- **PR2-04**：用 pre-pipeline terminal outcome 状态表关闭，full failure 仍向 notification flow 提供完整 account results、metrics 和 scheduled targets。

## 15. Implementation readiness rule

只有新的 planreview 结论为 `pass` 或所有 material findings 已在本文中明确裁决并补齐后，才进入代码实施。当前文档修改不授权 commit、release、deployment 或生产通知。
