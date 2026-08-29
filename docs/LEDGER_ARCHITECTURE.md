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
事务重放受影响投影。其他模块不得直接改 `event_json`。

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
