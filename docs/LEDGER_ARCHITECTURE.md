# Ledger Architecture

本文记录当前交易与期权持仓账本的运行契约。它描述已经落地的边界，不是迁移计划。

## 权威链路

```text
trade_events -> deterministic projection -> position_lots
```

- `trade_events` 是业务事实。
- `position_lots` 是可重放投影，不是第二套事实源。
- `lot_id` / 当前 `record_id` 是写入目标身份。
- `position_key` 只用于聚合、展示和风险查询，不能代替精确 lot 写入目标。
- Feishu `option_positions`、旧 v2 snapshot 和历史兼容文件不参与稳态读取或写入。

默认 SQLite 位于：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

不要直接修改 SQLite 行。修复必须表达为可审计的语义事件，或通过受控 projection rebuild / verify 恢复派生状态。

订单费用是同一成交事件的延迟证据，不另建费用事实账本。受控 fee enrichment 是唯一
允许更新既有事件费用 provenance 的路径：按完整订单组执行 CAS、审计、读回并在同一
事务重放受影响投影。其他模块不得直接改 `event_json`。下文“Futu 订单身份
补录”定义一条同样受控的 metadata-only 例外。

## 模块所有权

| 边界 | 当前 owner |
|---|---|
| 领域事件与投影规则 | `domain/domain/ledger/` |
| 非 ledger 模块的公共应用入口 | `src/application/ledger/api.py` |
| 命令与维护动作 | `src/application/ledger/commands.py` |
| 查询与读模型 | `src/application/ledger/queries.py`、`read_model.py` |
| 事件写入与投影发布 | `src/application/ledger/writer.py` 公共 facade；实现按职责位于 `writer_*.py` |
| 订单费用迁移与审计 | `src/application/ledger/order_fee_migration.py`；公共入口仍由 `ledger/api.py` 导出 |
| SQLite repository | `src/application/ledger/repository.py` 公共 facade；schema、trade events、position projection、assigned stock、lifecycle 与 strategy identity 分别由 `repository_*.py` 持有 |
| 当前决策投影 | `src/application/ledger/current_decision_projection.py` 公共 facade；事实域、migration 与 runtime 分别由 `current_decision_*.py` 持有 |
| lot 目标解析与 preflight | `src/application/ledger/lot_resolver.py`、`preflight.py` |
| 人工持仓工作流 | `src/application/positions/` |
| broker trade intake | `src/application/trades/` |
| 人工 CLI | `src/interfaces/cli/option_positions.py`、`trade_events.py` |

`positions`、`trades`、Agent tools、CLI 和 pipeline 不应绕过 `ledger.api` 导入内部写入原语。领域层不得反向导入 `src/`。

## 单次投影刷新与 SQLite 写连接生命周期锁

### 当前合同

- 每个非 dry-run 的到期维护账户只重建一次 `trade_events -> position_lots` 投影，并在这次刷新后确定本轮 account/broker/market lot 成员集。
- 同一 ledger SQLite 文件的 repository writer 和公开 migration writer，从打开、WAL 配置、事务到关闭和 SQLite/WAL/SHM 安全属性加固，全部位于同一 db-path writer lock 内。
- 保留 auto-close 的当前 fail-closed 语义、fresh-lot 校验、对外结果字段和回执行为。
- 纯读查询不纳入 writer lock；不用 retry、sleep 或扩大 `busy_timeout` 代替锁边界。

### 运行合同

Auto-close 数据流：

```text
positions maintenance
  -> 复用同次 repository open 的 startup recovery 回执，否则重建一次 trade_events -> position_lots
  -> 读取并过滤本轮 account/broker/market open lots
  -> 刷新行情/交割证据
  -> ledger.auto_close_expired_positions(projection_refresh=<typed result>)
       -> 不再重复刷新
       -> 重读本轮已选 record IDs 的 fresh lots 并合并上游证据
       -> decision -> preflight -> optional expire-close write -> readback
  -> 将同一次 projection refresh 结果暴露为既有 projection_refresh 字段
```

本轮成员集在唯一刷新后冻结。刷新恢复的 lot 会参与本轮；成员集冻结后新增的 lot 留给下一次定时维护，不扩大本轮 account/broker/market 范围。已选 lot 在写前仍重读当前字段并执行原有 preflight。fresh-read 必须用 close-candidate identity 复核 account、broker、symbol、option type、side、strike、expiration 和 currency；身份改变时不得合并先前行情，并以 `position_lot_identity_changed` fail closed。

`load_option_positions_repo()` 在 startup recovery 时保存同库的 typed `ProjectionRefreshResult`；positions orchestrator 单次消费该回执，避免恢复后立即重复 full rebuild。没有 recovery 回执时，positions orchestrator 自行刷新并传入同库本轮结果。`auto_close_expired_positions()` 收到该输入时不再刷新；其他调用方未传入时继续默认自行刷新，保留公共 ledger 安全语义。typed 结果只作为 ledger 输入；positions 层继续通过既有顶层 `projection_refresh` 字段输出。`ExpiredCloseRunResult` 仍只携带 decisions、applied 和 errors，不增加字段、不改变 `to_payload()` key set。无 trade event 或 dry-run 时不伪造刷新结果；刷新失败时保留现有 `projection refresh failed before auto-close` 错误和整次零写入行为。

SQLite writer 状态转换：

```text
acquire <db>.writer.lock
  -> connect + connection invariants + busy_timeout
  -> PRAGMA journal_mode=WAL + synchronous=NORMAL
  -> optional BEGIN IMMEDIATE
  -> write body
  -> commit | rollback
  -> conn.close()
  -> validate and harden SQLite/WAL/SHM artifact permissions
release <db>.writer.lock
```

`_writer_connection()` 在调用现有 `_connect()` 前取得外层 writer lock。`connect_private_sqlite()` 继续作为共享连接工厂；如果它在 `sqlite3.connect()` 成功后执行的 artifact 加固失败，工厂在返回前关闭已创建连接并重新抛出该错误。repository 和 migration writer 都在外层锁内调用这个工厂；其他已有调用方只获得相同的失败清理，不新增 writer lock。

工厂成功返回后，`_connect()` 保持重入取锁和 PRAGMA 顺序，并校验 `journal_mode=WAL` 的实际返回值为 `wal`。如果 connection invariant、PRAGMA、WAL 返回值校验或后续初始化 artifact 加固任一步失败，`_connect()` 在外层锁内尝试关闭连接、完成可行的 artifact 加固，并重新抛出初始化错误。这个外层锁一直持有到正常路径或失败路径的 `close()` 和 `secure_sqlite_artifacts()` 完成；连接不能因任一 helper 尚未成功返回而逃出清理边界。

position-projection migration 的 `_write_connection()` 持有同一 `<db>.writer.lock`。它只负责 db-path lock、连接打开与初始化、WAL 实际返回值校验、连接关闭和 artifact 加固；apply/activate/deactivate 调用方继续负责 `BEGIN`、commit、rollback 和 `write_applied` 时序，不复用 repository 的事务 owner。current-decision migration 复用该 context manager 并遵守同一边界。读只诊断连接不受这个 writer lock 合同限制。`secure_sqlite_artifacts()` 只验证普通文件并收紧权限，不 checkpoint、不删除 WAL/SHM；活跃 reader 存在时 sidecar 可以继续存在。

失败语义：

- shared factory 在连接创建后的首次 artifact 加固失败：工厂在返回前尝试 close，并重新抛出该加固错误；writer 调用时整个分支仍位于 db-path lock 内。
- factory 返回后的 invariant、PRAGMA、WAL 校验或后续初始化 artifact 加固失败：不开始业务事务；`_connect()` 仍在 writer lock 内尝试 close 和可行的 artifact 加固，重新抛出初始化错误后释放锁。
- 写入体或 commit 失败：在同一锁内 rollback、close 并加固 artifact 权限，向上抛出错误。
- close 或 artifact 安全属性加固失败：锁仍由 context manager 释放，错误不得被改写为成功。
- 不新增多异常优先级状态机；保持当前 Python context-manager 的异常传播。任何 auto-close 失败都不自动 retry；操作员通过现有定时重试和读回回执区分“未写入”与“已应用”。
- auto-close 投影刷新失败时整次零写入；进入逐 lot 写入循环后，后续 lot 失败不回滚先前已成功的 durable close，响应同时保留 `applied` 和 `errors`。

## 写入语义

账本动作按业务事实区分，不能互相替代：

- open
- buy-close
- expire-close
- assignment
- exercise
- assigned-stock sale
- adjustment
- void / repair

每次写入都必须满足：

1. account、broker、symbol、option type、side 和 contract identity 明确；
2. close / assignment / exercise 解析到确定 lot；
3. 数量不会超过当前可用 lot；
4. 幂等身份足以防止 broker deal 或人工请求重复落账；
5. 写前 preview 与写后 projection 使用同一事实；
6. 身份冲突、projection drift 或关键证据缺失时 fail closed。

手工入口默认先 dry-run。例如：

```bash
./om option-positions add \
  --request-id manual-open-<stable-id> \
  --account lx \
  --symbol NVDA \
  --option-type put \
  --side short \
  --contracts 1 \
  --currency USD \
  --strike 100 \
  --multiplier 100 \
  --exp <future-expiry> \
  --dry-run
```

`add`、`assign`、`exercise` 的 preview、apply 和响应丢失后的重试必须复用同一个
`--request-id`。相同 request ID 与相同 intent 返回原结果；同一 ID 绑定不同 intent
会 fail closed。确认前检查响应中的目标 SQLite、account、lot/event identity、数量和写入合同。

## 批量 adjustment 投影成本

### 目标、边界与成功信号

`record_manual_position_adjustments()` 在一次原子批量写入前复用同一份 current projection，
并把全部候选 adjustment 作为一个集合只预览一次，不按 adjustment 数量重复读取和投影完整
`trade_events` 历史。

成功信号：

- 两项及更多 adjustment 的预检固定为一次 current preview 和一次 combined candidate preview；
  全历史读取次数不随 batch cardinality 线性增长；
- trusted checkpoint 下完整入口至多两次 full-prefix read：一次 combined candidate fallback 和一次
  事务内最终 projection；checkpoint disabled 或 untrusted 时至多三次：current preview、combined candidate
  preview 和事务内最终 projection 各一次；两种模式都不随 batch cardinality 增长；
- preflight 返回字段、patch、event time 和写入结果保持兼容，forced-full 与优化路径的最终 lot
  fingerprint 完全一致；
- 任一 target、current fields、contract identity、patch 或 combined projection 无效时，整批零写入；
  事务内仍重读当前 lot，并要求重读字段与 preflight advisory 字段逐字典相等，再完成最终 projection、
  current-decision finalize 和 commit；
- 完整 projection 缺少可恢复 state 时继续 fail closed，不发布 read model、不提交事务，也不新建
  checkpoint；warning-only/no-state 行为只增加 characterization regression，不修改 runtime 源码；
- Phase 3A 的 special Combo fixture 使用晚于既有历史且早于 expiration 的开仓时间，完整场景不再因
  fixture 自身的 `economic_adjust_invalid` warning 中断。

该实现没有让 adjustment 支持 tail projection，也没有达到 Phase 3A 冻结的 `500 ms`、`64 MiB` 和零
full-prefix-read 门槛。本地 `1` warmup / `3` repetitions non-acceptance smoke 在 10,000 events / 100
open lots 的两项 batch 上，fast 路径 wall/CPU P95 为 `0.880 s` / `0.863 s`、两次 full-prefix read，
forced-full 路径为 `1.158 s` / `1.137 s`、三次 full-prefix read；最终 lot fingerprint 完全一致，fast
路径 Python peak allocation 为 `79,110,743` bytes。该 smoke 只证明当前主机上的结构性收益，不是
跨主机 acceptance 结论。

当前仓库中该 batch facade 的唯一调用者是 Phase 3A benchmark；本分片只改善 admission/benchmark
路径，不宣称生产 tick、通知或交易入口会因此加速。若未来接入生产 caller，必须先解决下述响应丢失
后的批量重试风险。

不修改 schema、公开 facade、CLI、config、正常 read path 或单笔 close path，不发布、部署或写入生产
账本和运行环境。不处理通用事件 JSON 解码/复制成本，也不增加 cache、后台任务或新依赖。

### 当前事实与选定方案

原 batch command 对每个输入分别调用 `_preflight_lot_adjust()`。该函数先读取 trusted current
projection，再对单个 `adjust` 候选调用 `preview_position_projection_append()`；领域 projector 把
`adjust` 视为 control event，candidate tail 因此回退 full replay。事务内
`persist_manual_adjust_events()` 又必须对最终事件集合重算一次投影。批量项越多，写前 full replay
越多；单笔 close 和 current projection read 不经过这条重复链路，保持不动。

当前实现保留 singular preflight 和事务 writer 的职责，只抽出一段共享的
“基于已读取 current preview 构造一个 adjustment preflight result 与候选 event”逻辑：

1. singular `_preflight_lot_adjust()` 读取一次 current preview，构造一个候选并预览一次；外部行为不变；
2. 新的 batch preflight 读取一次 current preview，按既有顺序验证每个唯一 `record_id`、current
   fields、open 状态和 contract identity，并构造各自 patch、event time 与候选 event；
3. batch preflight 将全部候选 events 交给一次 `_preview_append_projection()`，沿用既有 error/ineligible
   阻断规则；warning 保留在 preview 结果中，但不放宽事务内最终完整 projection 的发布条件；
4. command 把每项 preflight 的 advisory current fields 和 event time 交给现有
   `persist_manual_adjust_events()`；后者在同一 SQLite transaction 内重读目标，并在构造任何正式 event
   前要求 `current_fields == fields`；任一字段漂移都 rollback，随后才检查 group collision、构造正式
   events、发布最终 projection 和 current-decision，并一次 commit 或 rollback；
5. 不把 advisory preview 对象或投影状态带入写事务，不降低 transaction-time revalidation。

`ensure_projection_publishable()` 的 error/ineligible 规则只负责 preview 资格；事务内 `_run_full_path()`
仍要求完整 resumable state。warning 导致 state 缺失时继续抛错并由 transaction rollback，不发布
position lots，也不推进 trusted head。Phase 3A 当前中断来自 synthetic special Combo 开仓时间晚于
expiration；只把该 fixture 的时间改为 `1_850_000_000_500` ms，使其晚于基线历史且早于
`2028-12-15`，并用 regression 证明 warning-only/no-state 的 fail-closed 行为没有漂移。

拒绝的替代方案：

- 允许所有 `adjust` 直接 tail-resume：会扩大 domain control-event 契约，经济字段、合约身份和策略
  metadata 的安全边界需要另行设计；没有当前收益证据时不做；
- 修改 generic projector 为 streaming 或重写事件 codec：改动面远大于已定位的 batch 重复调用；
- 为 warning 新增 allowlist、持久化 head diagnostics 或改变全局发布语义：当前 fixture 修正后没有必要；
- 新增 repository latest-event query 以省掉 disabled/untrusted 下的 current full read：只少一次固定读取，
  却扩大 persistence API，当前 benchmark-only 收益不成立；
- 新增 batch projector、cache 或 checkpoint 类型：现有 preview 与 transaction runtime 足够表达本分片。

### Owner、数据流与失败语义

受影响 owner：

- `src/application/ledger/commands.py`：batch 输入归一化、调用 batch preflight、组装兼容结果；
- `src/application/ledger/preflight.py`：共享 adjustment 构造校验和一次 combined preview；
- `src/application/ledger/manual_trades.py`：事务内 advisory/current exact-equality fence；
- `scripts/benchmark_data_storage_projection.py`：只修正 synthetic special Combo event time；
- `tests/test_position_projection_runtime.py`、`tests/test_research_performance_baseline.py`：行为、计数、
  原子性、runtime characterization 和 fixture 回归。

```text
record_manual_position_adjustments(adjustments)
  -> normalize batch + reject missing/duplicate record_id
  -> current preview once
  -> per target: current fields + identity + patch + candidate event
  -> combined candidate preview once
  -> persist_manual_adjust_events(existing transaction owner)
       -> BEGIN IMMEDIATE + reread all targets
       -> exact advisory/current fields fence
       -> group collision checks + rebuild official events
       -> final projection + current-decision finalize
       -> commit | rollback
```

preflight 仍是 advisory；正式事务内的 `current_fields == fields` 是 batch 专属并发 fence，不新增 hash、
版本列或通用比较 helper。combined preview 比逐项 preview 多检查 batch 内相互作用，只可能把失败提前到
写事务前，不允许原先被拒绝的写入。重复 target、缺失 target、closed lot、identity mismatch、invalid
patch、projection error、任一字段漂移和最终 projection/finalize 失败都保持整批零写入。

批量写入在提交成功但响应丢失后重试，当前可能因相同 event ID 携带不同 `event_time` 而冲突；本分片不
扩张为幂等协议改造。owner 为 `src/application/ledger/manual_trades.py`，影响是该 facade 暂不适合新增生产
caller；在任何生产接入前，必须复用单笔 adjust 的 existing-event 语义并补响应丢失回归。

### 实现与验证

1. 在 `preflight.py` 复用现有校验构造共享 helper，增加 batch preflight；在 `commands.py` 仅替换
   per-item preflight loop；在 `manual_trades.py` 增加 exact-equality fence。参数化验证一、二、三项 batch
   在 trusted 模式分别保持至多两次 full-prefix read，在 disabled/untrusted 模式分别保持至多三次；
   同时覆盖相同 `as_of_ms` 下的结果顺序，以及 trusted/disabled 模式的最终 fingerprint parity。
2. 增加 duplicate target、单个 invalid item、transaction-time 任一字段漂移和 final projection/finalize
   late failure 的 public batch facade 回归，全部断言 event、lot 和 current-decision 零写入；保留一条
   warning-only/no-state forced-full 继续 fail closed 的 runtime characterization。只修正 special Combo
   fixture 时间，并验证完整 fixture 零 diagnostics、可完成。
3. 运行 focused tests、Ruff、dependency-graph check、guardrails、`git diff --check` 和完整 pytest；
   再运行 Phase 3A `1` warmup / `3` repetitions smoke，比较 wall/CPU、allocation、full-prefix reads、
   parity 和原子性。该 smoke 必须标记为 non-acceptance，不替代指定 reference host 上的正式 `5/30`。

验收首先看结构性计数和正确性：full-prefix reads 对 batch cardinality 有界、fingerprint parity、任一
失败零写入、warning/no-state 继续 fail closed。优化后绝对耗时和 peak allocation 如仍超过冻结门槛，
只记录实测差距，不把结构性计数达标表述成 Phase 3A acceptance；只有出现真实生产调用压力或正式
admission 需求，才重新评估 metadata-only tail support。

开放项：无。批量响应丢失重试作为明确 deferred risk，在生产接入前解决。commit、push、merge、
release、deploy 和生产写入继续是独立授权边界。

## Futu 订单身份补录

### 目标、边界与成功信号

历史手工 Futu 期权 open 事件可能已保留完整成交经济事实，但缺少 OpenD 费用查询所需的
`raw_payload.futu_account_id` 和 `raw_payload.order_id`。本变更让操作员在人工核实 OpenD
历史订单后，用现有 `trade-events repair` 原地绑定这两个身份，然后仍由现有
`fees-sync` 查询并持久化 actual fee。

成功必须同时满足：

- dry-run 展示唯一目标、绑定前后身份和 `expected_before_sha256`，不写 SQLite；
- apply 后仍是同一 `event_id` 和 `ingest_seq`，事件数量、合约、金额、数量、时间、
  cash-conversion fact IDs、lot identity 和下游 close/adjust lineage 不变；
- 原地更新造成的全局 position source generation 变化在同事务中发布，不留下新的
  position 或 current-decision dirty state；
- 带有有效下游 close/adjust 的 open 事件可补身份；经济或 lot-target override 仍走原有
  void/replacement 路径及 downstream dependency 阻断；
- 同一绑定重试为零 DML no-op；部分身份、冲突身份、目标已 void 或 CAS 冲突时零写入并失败；
- 绑定后的同范围 `fees-sync` dry-run 选中该订单，actual fee 写入后再次 dry-run 收敛为 no-op。

本变更不支持 close、expire-close、assignment、exercise 或 assigned-stock sale；不自动匹配 OpenD
历史订单，不进行批量映射、部分身份补齐、事件时间纠正、经济事实更改、下游链重写、
schema/config 变更或生产回填。账本与 OpenD 时间相差 12 小时等情况仍需操作员独立确认；
近似时间、合约、数量或价格不会被代码提升成持久订单身份。

### 当前事实与选定方案

当前 `repair` 已接受 `--futu-account-id` 和 `--order-id`，但总是 void 原事件并追加
replacement。这会被有效下游 close/adjust 正确拒绝，而强行 replacement 又会改变 event
identity 并破坏指向原 event ID 的证据。另一个已验证的约束是：任何 `event_json` 更新都会推进
全局 position source generation，所以只重建 position lots 不足以保持 current-decision 可信。

选定的最小方案保留现有 CLI 和 application facade，在 ledger command 入口优先识别
identity-only override，不进入 append-repair preflight：

1. 对 `futu_account_id` 和 `order_id` 按原值校验且不静默 trim；前者必须是无前导零的正整数字符串，
   后者必须非空且不含空白或控制字符。两字段必须同时显式提供。
2. 有效 override key set 精确等于这两个身份字段时才走 identity-only；与任何经济、合约、
   时间或 lot-target override 混用时继续原 void/replacement 语义。
3. 目标必须是 active canonical Futu `open` 期权事件；首次绑定时 fee basis 必须尚非 actual。
   相同 `(futu_account_id, order_id)` 被其他 active event 使用时首版直接拒绝，不实现多事件订单分配。
4. 目标两个身份都缺失时允许绑定；两者都与输入相同时即使 fee 后来已为 actual 也返回 no-op；部分存在或
   任一值冲突时 fail closed。目标已有相同身份但没有本功能 provenance 时仍是 no-op，不为补审计数据改写历史。
5. identity-only 要求显式、非默认 `reason`引用人工 OpenD 核对证据。绑定自身不连接 provider；
   REAL 账户范围、终态、币种、成交数量和 actual fee 仍由后续 `fees-sync` 验证。

不新增 `bind-order-identity` 命令，因为它会重复现有参数、写入门禁和 facade。不让
`fees-sync` 自动匹配缺身份事件，也不增加通用 event mutator 或新审计表。

### 数据流、状态与失败语义

```text
om trade-events repair --futu-account-id ... --order-id ... --reason ...
  -> trades.review / ledger.api（现有签名不变）
  -> ledger.commands 优先选择 identity-only 或原 append-repair
  -> dry-run: 构造 advisory preview，零写入
  -> apply: db-path writer lock -> BEGIN IMMEDIATE
            -> 重读原始 JSON 及有效 void 集，重做全部验证
            -> capture global current-decision fence
            -> old-JSON CAS + exact readback
            -> forced-full position projection，要求 lot diff 为零
            -> finalize current-decision projection -> commit
  -> 现有 fees-sync 独立 dry-run/apply
```

apply 不使用 dry-run 结果作为事实；preview 明确标记为 advisory。事务内验证从原始存储 JSON 做
copy-on-write，只允许以下路径变化：

```text
raw_payload.futu_account_id
raw_payload.order_id
raw_payload.order_identity_provenance
```

`order_identity_provenance` 不得覆盖已有同名数据，它记录 schema version、以
`event_id + normalized futu_account_id + order_id` 确定性生成的 binding ID、来源
`manual_trade_event_repair`、reason、绑定前两个空身份、`expected_before_sha256` 和首次
`bound_at_ms`。apply 回执另返回实际 `after_sha256`；避免把时间字段包含在 dry-run 的预期 after hash 中。

原始 SQL 由 trade-event repository 内一个窄 CAS 方法持有：
`WHERE event_id = ? AND event_json = ?` 必须更新且只更新一行。intervention 持有策略、原始 JSON
深度 diff、事务编排和读回校验；canonical codec 只用于验证结果，不用来重序列化无关的历史字段。
CAS、读回、lot 零差异、current-decision finalize、foreign-key 检查或 commit 任一失败都回滚；不 retry。

响应状态为 `dry_run`、`applied` 或 `no_op`。`no_op` 退出码为 0、`write_applied=false`、不推进
source generation；验证失败或冲突退出码为 2。普通 void/replacement repair 的现有 JSON 响应保持不变，
identity-only 的文本回执、help 和 rollback hint 必须明确不会生成 void/replacement event。

### Owner 与实现分片

- `src/interfaces/cli/trade_events.py`：保留现有参数和高风险写入门禁，修正 help、文本回执和真实
  `write_applied` / rollback hint。
- `src/application/trades/review.py` 和 `src/application/ledger/api.py`：保留现有公开签名与分层，不新增 facade。
- `src/application/ledger/commands.py`：优先分流 identity-only 与原 append-repair，组装稳定响应。
- `src/application/ledger/interventions.py`：持有目标状态机、allowlist diff、current-decision fence 和事务编排。
- `src/application/ledger/repository_trade_events.py`：只新增目标化的 raw event JSON CAS，不提供通用 mutator。
- `src/application/trades/order_fee_sync.py` 继续持有 provider 验证；
  `src/application/ledger/order_fee_migration.py` 以原始 JSON 做 CAS 与 fee-only copy-on-write，并维护全局
  trade-event current-decision fence。

代码、CLI 回执、[Option Positions Repair](OPTION_POSITIONS_REPAIR.md) 和测试作为一个最小纵向分片交付，
避免 ledger 已分流但 CLI 仍报 `void=None repair=None` 的中间状态。

### 验证计划、风险与开放项

最小回归覆盖：

- 有 downstream close 的 open 事件：dry-run 零写入；apply 后只有三条 allowlist JSON 路径变化，
  event count/ID/ingest sequence、完整 lot fingerprint、cash-conversion IDs 和 downstream lineage 不变；
- 两个已初始化账户的 current-decision 在绑定后仍 clean，业务 payload 不变；强制 projection/finalize
  失败时原 JSON、source generation 和读模型整体回滚；
- 同一绑定再执行为零 DML no-op；单字段、空白、部分或冲突身份、重复 active identity、
  已 void 目标、非 open、已有 actual fee 和 CAS 冲突都失败且零写入；
- 身份参数与 strike 等任一经济 override 混用时，原有 void/replacement 行为和 downstream 阻断不变；
- 原始 JSON 含 legacy top-level `fee_provenance` 或未知 raw keys 时不会被 codec 顺带规范化；
- fake OpenD provider 验证绑定后 `fees-sync` 选中订单并写 actual fee，保留无关历史 JSON，且同范围
  第二次 dry-run 与相同身份重试均为 no-op。

实现验证运行 focused pytest、受影响文件 Ruff、`git diff --check`、文档/敏感产物 guardrails 和
`./.venv/bin/python scripts/generate_dependency_graph.py --check`。

残余风险：

- identity-only 不加载 runtime config，所以不在绑定阶段证明 Futu account ID 属于账本 account；
  owner 仍是 `order_fee_sync`，影响是错误但格式合法的身份可以被持久化。本分片用显式人工证据、
  高风险确认、重复 identity 阻断和后续 `fees-sync` 缩小风险；如要由系统证明合约/方向/价格，
  应作为 provider admission 的独立 hardening，不塞入本 repair。
- 首版不允许覆盖或解绑，错误绑定不能用同一命令修正。owner 是后续 ledger repair 设计，影响是
  提交后只能使用写前备份或另行授权的前向修复；本命令不会自动创建备份。生产使用必须逐笔 preview、
  另行创建并验证备份、写后读回和
  `fees-sync` dry-run，任一不一致立即停止。
- 首版拒绝多事件共用订单身份；如未来出现经证明的合法 split-fill/order 组，owner 是
  `order_fee_sync` 和 ledger identity contract，需单独设计分配与纠错，不放宽本分片。

开放项：无。当前能力不需要新 schema、public command 或 provider capability。发布/升级和
生产回填继续是独立授权边界。

## 读取语义

运行时风险、Close Advice、Performance 和 Agent tools 从 canonical read model 读取：

- 单 lot 查询保留真实开仓、费用、策略快照和生命周期字段；
- 聚合持仓只用于展示或风险计算；
- 历史 `as_of` 查询不能用当前报价回填历史缺口；
- 当前时点报价刷新失败时返回明确 quote status；
- 缺少费用、汇率、行情或 lifecycle evidence 时保留 partial / missing，不把未知值写成零。

常用只读入口：

```bash
./om option-positions list --account lx --status open
./om option-positions inspect --record-id <lot-id>
./om trade-events list --account lx
./om trade-events fees-sync \
  --config-key us --account lx \
  --start-date 2026-08-01 --end-date 2026-08-23

./om-agent run --tool option_positions_read \
  --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
```

具体子命令以 `./om option-positions --help`、`./om trade-events --help` 和 `./om-agent spec` 为准。

### 稳定事件分页

Agent 通过 `option_positions_read action=events` 分页读取 canonical `trade_events`。SQLite 为每个
事件分配单调且不复用的 `ingest_seq`；首次查询记录最大序号，后续页使用
`trade_time_ms DESC, event_id DESC` 的 keyset cursor，并始终限制在该序号边界内。因此新增事件
不会插入正在进行的结果流，分页条数可以在 1–20 之间变化，也不会导致已返回成员重复。

这个 snapshot 冻结的是成员集合、筛选字段和排序字段，不是整行 JSON 的历史版本。事件成员
不可删除，`ingest_seq`、事件身份、交易时间、账户、市场、position effect 和合约筛选字段不可
修改；价格等不参与查询的补充字段仍可按现有账本语义更新。完整 TradeEvent 的编码与验证继续
由 Python canonical codec 负责，SQLite 不实现第二套领域 JSON 校验器。

旧库只声明新增列，不在普通启动时扫描回填。必须通过受控 position-projection migration 分批
填充分页投影并发布索引与约束；完成前 `action=events` 明确返回 pagination unavailable。

## 到期与交割生命周期

到期短仓不能仅凭“过了 expiry”自动写成 worthless：

- 价外自动关闭需要符合市场时区和报价证据；
- 价内、平值或缺少 spot 时进入 review；
- option leg 与 stock settlement leg 可以异步到达；
- assignment / exercise 必须有匹配的交割事实；
- `external_holdings` 账户缺少 broker lifecycle evidence 时默认要求人工复核。

到期维护由独立 `auto-close-expired` 服务/定时入口负责，不是普通 `account_run` 或扫描 pipeline 的隐式步骤。

## Projection 验证与恢复

`verify-projection` 默认是纯只读诊断：它可以读取已有 checkpoint 加速比较，但不会创建目录、
覆盖 latest report 或发布新 checkpoint。只有明确需要留下运维证据时才使用
`--publish-evidence`：

```bash
./om option-positions verify-projection --mode auto
./om option-positions verify-projection --mode auto --publish-evidence
```

生产定时验证显式使用 `--publish-evidence`；临时排查保持默认只读。

发现 read model、report 或 lot 状态异常时，按顺序处理：

1. 用只读 inspect/history/verify 确认 active runtime root 和 SQLite；
2. 检查 trade event 是否完整、重复或存在目标歧义；
3. dry-run projection rebuild；
4. 只有在差异可解释且目标准确时才 apply；
5. 用相同 runtime root 复查 lot、event history、Close Advice 和 Performance。

不得用以下方式“修好显示”：

- 直接更新 `position_lots`；
- 重新接回 Feishu / v2 兼容状态；
- 用聚合 `position_key` 猜测 close lot；
- 为缺失历史事实填入当前价格、当前汇率或零费用。

完整修复步骤见 [Option Positions Repair](OPTION_POSITIONS_REPAIR.md)。

## 下游合同

- [Close Advice Contract](CLOSE_ADVICE_CONTRACT.md)：如何消费 lot、行情和策略快照。
- [Option Performance Design](OPTION_PERFORMANCE_DESIGN.md)：利润、现金、activity 和组合桥接。
- [Assigned Stock Return Design](ASSIGNED_STOCK_RETURN_DESIGN.md)：assignment 后的正股成本与收益。
- [Architecture](ARCHITECTURE.md)：ledger 与 interfaces/application/domain/infrastructure 的整体边界。
