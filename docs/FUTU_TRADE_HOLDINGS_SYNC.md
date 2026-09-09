# Futu 成交、PM 持仓同步与期权生命周期

## 边界

Futu OpenD deal push 是实时成交入口。OM 标准化成交后维护期权和生命周期
账本；股票或 ETF 成交结算完成后，OM 只向本机 portfolio-management 服务
发送账户刷新提示。PM 自行读取 Futu 完整持仓快照并更新绝对
`quantity` 和 `average_cost`。

同步意图不携带推算持仓、成交数量或成本。OM 不直接写 PM/Feishu，PM
也不写 OM ledger 或 Feishu `transactions`。

## 启用

权威 `config.yaml` 可增加：

```yaml
portfolio_management:
  enabled: true
```

该全局开关默认关闭，同时控制 PM 只读工具、指派场景证据和成交后的刷新提示。
只有 `trade-intake` 处于 `apply`、成交首次出现且来源是 Futu push 或 history
backfill 时才会产生提示。旧 `trade_intake.holdings_sync.enabled` 只保留一个版本的
迁移读取；旧队列、重试和状态目录参数会被忽略并输出迁移诊断。
目标服务地址沿用 `PORTFOLIO_SERVICE_URL`，默认
`http://127.0.0.1:8765`，并强制为 loopback origin。

## 运行语义

- 期权成交返回 `option_deal`，不会调用 PM。
- 股票或 ETF 使用 canonical broker deal key 去重；提示只含 `account` 和该 key
  的 SHA-256 `request_id`，不传成交增量或券商身份。
- Push 在该成交 Inbox 结算且账户锁释放后发送一次；结算失败时，同一 Inbox
  可在后续重复 push 或 backfill 结算后认领该意图一次。history backfill 完整处理
  本批成交后，每账户最多发送一次。
- OM 不维护独立刷新队列、工作线程或 PM 同步状态；现有成交 Inbox 仅保存一次性
  刷新意图和认领标记，不重试 PM 请求。请求固定超时 2 秒；PM 返回
  `202 Accepted` 只表示接受提示，不表示持仓已经同步。
- PM 调用失败不会回滚或改写 OM 已记录的成交、期权或生命周期事实；PM 的既有
  早晚全量同步仍是最终对账兜底。
- 成交写入时冻结公式费用；持久化成功后按该成交的 canonical
  `(broker, account, futu_account_id, order_id)` 精确查询终态订单和实际费用。
  当次尚未取得 actual 时，recent history backfill 只重试本次窗口内成交携带的同一订单。

## 生命周期观察隔离与一次性历史修复

`trade_intake.settlement_observation.enabled` 是 lifecycle settlement provider
的唯一开关，默认值为 `true`。它不改变成交身份、Inbox 幂等、费用补全、投影、
通知或 lifecycle 证据规则。

开关为 `false` 时：

- applied push、Inbox retry 和 recent history backfill 在 canonical ledger 写入前
  仍必须完成启动 checkpoint seal；seal 失败时 payload 保持可重试且不写 ledger；
- 新成交继续完成 durable Inbox、canonical ledger、readback 和现有回执，但不创建
  lifecycle settlement broker/quote gateway，也不查询 lifecycle timing；
- recent history backfill 继续按现有 6 小时窗口和 5 分钟间隔恢复 listener 漏推，
  不扩展为长期历史扫描；
- lifecycle due tick 继续执行本地计划。provider-required case 写入
  `collector_disabled`，空集和本地可决 case 保持原结果，provider claim 与 attempt
  均为零；
- 新 lifecycle case 可以保持 `cause_pending` / `needs_review` 且暂时没有 timing；
  不猜测到期、指派或行权终态，也不发送相应终态通知。

该开关只隔离 lifecycle settlement 观察。Push 成交补全、recent history backfill
和费用同步仍使用 Futu native SDK，不能据此宣称所有 native 崩溃风险均已消除。
Python `except Exception` 也无法捕获 `SIGBUS` 等进程级信号。

### 生产启用顺序

发布、远端升级、生产配置修改、历史账本写入和恢复服务是独立授权边界。
恢复 trade-intake 前必须：

1. 升级时使用 `--no-restart-services --preserve-activation-state`，并读回确认
   trade-intake 仍为 inactive。
2. 在权威 `config.yaml` 显式设置
   `trade_intake.settlement_observation.enabled=false`，重建并验证运行配置，
   分别读回 lx 和 sy 的有效值为 `false`。
3. 完成一次性历史修复的 preview、apply 和 readback，并确认不存在待发历史通知。
4. 记录 `NRestarts` 基线后再恢复服务。第一次增加或出现状态 `135` 时立即停服，
   核对备份、账本 readback、integrity、投影数量、notification pending 和
   lifecycle claim，不等待重复崩溃。

服务恢复后的最小 canary 是连续跨过至少两个 lifecycle tick，期间
`NRestarts` 不增加，账本 integrity、投影数量和 notification pending
没有非预期变化。该 canary 不是对所有 Futu SDK 路径的长期稳定性证明。

### 一次性历史修复边界

历史批量修复是独立的一次性运维工作单元，不加入 listener、定时器、配置生成器、
公开 CLI 或长期脚本，也不增加 `force-provider` 类产品选项。它必须：

- 显式接收账户、包含边界的日期范围、时区和目标类型；
- 先执行只读 OpenD inventory 与 ledger diff，由操作员确认 frozen manifest；
- 在 apply 前备份精确 SQLite 与 source Inbox，并取得独立的账本写入确认；
- 只通过现有 canonical trade/lifecycle owner 应用精确 broker identity，不按
  symbol、数量或推测成交时间模糊匹配；
- 在常驻 collector 开关为 `false` 时使用任务自身的明确 provider 入口；
- 抑制历史通知，写后执行 ledger readback、完整投影重放和 integrity check；
- 在回执中记录 runner 代码哈希、manifest 哈希、通知抑制证据和每个 identity
  的应用结果；重跑只跳过已经读回确认的同一 identity；
- 遇到 OpenD 崩溃、超时或证据不完整时失败并保留备份与回执，不把未知结果
  标记为已完成；执行后删除临时 runner。

apply 前还需从生产只读盘点 effective flag、缺少 timing 的 case，以及
pending、provider-finished、ambiguous 基线。具体账户、范围、时区、目标类型和
通知抑制方法属于该一次性工作单元的运行输入，不固化为产品默认值。

## 订单费用同步

OpenD 适配器只负责查询与规范化。账本应用层选择完整订单组，并在查询费用前校验
REAL 账户、终态、币种和成交数量。`order_fee_query` 返回的订单总费用按同合约成交
数量确定性分配；Combo、跨类型订单、日期边界不完整、冲突 actual 或无法证明的股票
sale 订单均不写入。actual zero 是完整证据。

```bash
# 默认只预览；日期按 Asia/Shanghai，结束日期包含当日
./om trade-events fees-sync \
  --config-key us --account lx \
  --start-date 2026-08-01 --end-date 2026-08-23

# 高风险账本写入，仍需统一写入确认
./om trade-events fees-sync \
  --config-key us --account lx \
  --start-date 2026-08-01 --end-date 2026-08-23 \
  --apply --confirm
```

写入以订单组为事务单位，使用旧 JSON 比较交换、写后读回、投影重放和审计哈希。
dry-run 不创建审计表。历史回补负责补 actual；仍取不到 actual 的旧裸费用只冻结一次
当前公式版本，后续读取不随费率变化重算。OpenD 不可用、查询失败或证据不一致时保留
estimated/missing 和显式诊断，不回退到 raw deal fee 字段。

### 自动单订单费用补全

自动路径的目标是让每笔 broker 成交在 durable ledger 写入后，以该成交携带的特定
`order_id` 补全 actual fee；成功信号是终态、账户、币种和完整成交数量均可证明，
`order_fee_query([order_id])` 返回该订单费用，ledger enrichment transaction 写后读回为
actual。费用补全失败不得回滚、重复或改写已经持久化的成交，也不得把 estimated/missing
解释成零费用。

自动路径不扫描 2018 年以来的 ledger 候选，不维护 round-robin cursor，也不把历史
`fees-sync` 作为成交后的默认动作。缺少 canonical order identity 的手工事件、超过 recent
backfill 覆盖期的历史修复、费用公式调整、绩效口径变更和新增持久重试队列不在该路径内；
人工 `./om trade-events fees-sync` 继续负责显式日期范围的历史预览与受控写入。

```text
trusted push or recent history deal
  -> durable inbox and canonical ledger write, then settle intake result
  -> exact account + futu_account_id + order_id target
  -> source-local in-memory target queue
  -> exact current-order query; narrow history-order fallback when needed
  -> terminal / currency / complete dealt quantity admission
  -> order_fee_query([order_id])
  -> exact-only ledger enrichment transaction and readback
```

`src/application/trades/auto_intake.py` 为每个 source 持有一个进程内队列和 enqueue helper。
Push callback 只在成交 Inbox 已 settle 后，从可信 `_trade_intake_source` 和 canonical ledger
结果构造完整 `(broker, account, futu_account_id, order_id)` target，并调用该 helper；费用失败
不会把已成功处理的 Inbox 改回 retryable。source loop 顺序消费并去重 target，provider 查询
不持有共享 `process_lock`，也不与 backfill 并发使用同一个可变 OpenD client。进程退出时未
消费的 target 不单独持久化，由 recent history backfill 恢复。

当前订单接口以 `refresh_cache=true` 按 `order_id` 精确过滤，覆盖全部未完成订单以及 24 小时内
已成交或已撤订单。当前查询未命中或返回受控 provider 错误，且目标来自更早的有效 backfill
payload 时，按该订单完整
ledger 组的最早和最晚成交时间生成不超过 provider 360 天上限的窄 history-order 窗口，再在
本地只接受同一 `order_id`；不会退回无目标的长时间历史扫描。两种查询都失败或限流时 fail
closed，保留 estimated/missing。

首次尝试发生在成交持久化和 Inbox settle 之后；`FILLED_PART` 等非终态只记录
`terminal_pending`，不查询或写入最终费用。`FILLED_ALL` 或有成交数量的部分撤单只有在
provider `dealt_qty` 与 canonical ledger 订单组数量完全一致、币种一致时才进入费用查询。
这样同一订单的多笔成交不会在订单尚未结束时冻结不完整费用。provider 已终态但暂未返回
费用行时记录 `fee_pending`，后续 recent backfill 继续尝试，不能解释成零费用。

Recent history backfill 先从本轮 broker payload 派生明确携带的 canonical order identity；
只有新结果为 `applied`，或 duplicate 分支能由完整 ledger key 或 `applied/reconciled` processed
state 证明此前已经持久化时才收集 target。普通股票、failed、unresolved 和缺少持久化证据的
重复项不会触发 fee 查询。完成本轮业务处理并释放 `process_lock` 后，同一订单去重并调用由
auto intake 注入的 enqueue helper。
`run_history_backfill()` 不再查询费用，也不在 diagnostics 返回 raw target identity。已经具备
一致 actual fee 的订单在 provider 调用前跳过。该重试使用既有 lookback/checkpoint 覆盖，
不增加持久队列或状态表；旧 checkpoint 中的 fee cursor 自然保留但不再读取或更新。

`src/application/trades/order_fee_sync.py` 继续拥有完整订单组选择、terminal admission 和 fee
enrichment 编排。现有内部 `sync_order_fees` 增加可选 exact target 模式：按完整 canonical
identity 读取跨日期边界的完整订单组；该模式的 `start_ms`/`end_exclusive_ms` 可省略且不参与
选择或写入，history-order fallback 日期只从目标组的最早和最晚 event time 生成，也不使用
日期 cursor 或 `max_orders`。缺省模式仍要求有效日期范围并保持人工 `fees-sync` 语义，不新增
平行公开入口。当前全量 ledger 读取保持 O(n)，只有实测成为瓶颈后才增加索引。

`src/application/ledger/order_fee_migration.py` 是 enrichment implementation owner；
`src/application/ledger/api.py` 继续作为应用层公开边界。现有 `enrich_order_fees` 增加相同的
exact target 约束：该模式可省略日期范围，只构建该订单的 actual unit，禁止运行范围内 legacy
formula freeze pass，也不为无关订单产生 audit 或 unresolved outcome；写入仍复用旧 JSON
比较交换、SQLite transaction、投影重放和写后读回。
`src/infrastructure/futu_history_deals.py` 复用现有 gateway 和状态规范化，负责 exact
current-order、窄 history-order fallback 和 fee query。无需新增 domain entity、配置键、数据库
schema 或依赖；若实现产生新的 import edge，按仓库约定重新生成两份 dependency graph。

自动尝试结果写入既有 trade-intake audit 和 listener `last_fee_sync` 子状态，至少区分
`actual`、`already_actual`、`terminal_pending`、`terminal_order_missing`、`fee_pending`、
`provider_rate_limited`、provider 查询失败和 evidence conflict。成功数以 ledger
`committed`/`no_op` 结果为准，不以 provider observation 数冒充写入成功。audit/status 只保存
identity hash、reason、error type 和计数，不透传 raw `order_id`、provider 异常文本或 missing
列表。

顶层 backfill `ok` 仍只证明 history、durable inbox 与 lifecycle 完整，不会因费用暂缺而谎称
成交失败。`last_fee_sync` 表示该 source 最近一次完成的非空 queue drain cycle，包含 attempt
时间和仅属于该 cycle 的 actual、pending、failed 计数，不表示当前 backlog 或进程累计值。
无 target 的空 cycle 不覆盖旧状态；新的无失败 cycle 清除上一次 fee error。
`runtime_status.fee_failed_source_count` 只统计各 source 最近 cycle 仍有失败的数量，并同时暴露
脱敏的 fee actual、pending、failed 计数，避免仅凭 `listener_status=listening` 推断 actual fee
已补齐。

实现先给现有 fee sync 和 ledger enrichment 增加 exact target 模式，并用跨窗口完整订单组、
无关订单字节及 audit 不变、终态、数量、币种和 already-actual 用例锁定写入门；再增加 exact
current-order 和窄 history-order fallback，由 auto intake 的 enqueue helper 把 push 与 backfill
target 接到 source-local 队列，并删除 backfill 自动全历史 fee scan 与 cursor；最后补充 queue
drain cycle status 和 runtime status 安全聚合。

验证覆盖单笔成交成功、部分成交不写、部分撤单数量一致、超过 24 小时的 checkpoint 恢复、
duplicate push/backfill 重试、双账户相同 order ID 隔离、无关 estimated 订单不查询不改写、
provider 失败不回滚成交或改变 settled Inbox、同一订单并发重试幂等、成功清除旧 fee error、
空 backfill 不覆盖旧状态和双账户状态聚合。实现后运行 fee sync、trade intake、agent facade
相关测试、guardrails 与 `git diff --check`。

订单费用接口同一账户每 30 秒最多调用 10 次。复用的 source provider client 跨调用维护该
`order_fee_query` 预算；自动路径顺序处理特定订单，第 11 次及 provider 自身限流均 fail closed，
不 sleep 持锁，也不以并发、盲重试或批量历史扫描绕过。current-order 强制刷新若被独立限流，
同样保留 estimated/missing 等待 backfill。

保留风险是 provider 不可用超过现有回补覆盖期，或订单长时间保持部分成交且在变为终态前已
离开 backfill 窗口时，不会自动补齐。该情况由 partial 指标和历史 `fees-sync` 显式处理；只有
观测到持续积压后才由 trade-intake owner 设计 durable fee retry，不为当前缺陷预建状态机。

## Push 来源身份

Futu deal push 经 OM 主动连接的 OpenD TCP 端口进入。transport 保留响应头与成交行的
来源证据，source binder 校验允许的物理账户、环境与内部账户映射。PID 只用于诊断，
不进入业务幂等键。缺少真实物理身份、环境不符或来源冲突不得凭单账户配置猜测归属。

当前 canonical execution identity 由 `domain/domain/trade_execution.py` 拥有，应用经
`src/application/ledger/api.py` 和 `src/application/trades/deal_identity.py` 复用，键为
`execution:v1:<hash>`。`futu:<account>:<physical_account_id>:<deal_id>` 保留为兼容或
外部事件键，不替代 canonical identity。`trade_account_identity.py` 只负责账户字段提取。

推送头字段保留、错误门修复与 push/history 收敛要求见下节；不得通过放松来源校验修复
SDK 转换丢字段。无身份记录只保留证据，关联必须经既有证据导入机制证明。

## 成交录入与回执恢复

### 目标与边界

普通成交尽量在首次推送时完成录入并发送成功回执。首次失败时说明具体原因、是否自动重试及
用户下一步；重试录入成功后发送独立的成功回执。交易已成交、OM 已记录、通知已送达是三个
不同事实，不以其中一个推断另一个。由 trade-intake 与 private-storage owners 负责。

验收要求：可信推送无需等待 history 才获得账户身份；SQLite 权限维护不释放活动事务的锁；
重复推送、回补、崩溃恢复均只产生一次经济效果；失败、恢复、重试耗尽都可解释；通知恢复不
重放成交。外部发送结果不明时不能保证恰好一次，不允许用盲目重发制造这一保证。

范围限于推送身份传递、共享 SQLite 文件权限维护，以及普通 intake 回执与现有重试循环的
衔接。不改变策略、费用计算或 lifecycle outbox 的业务归属，不引入通用消息平台或历史全量
扫描。本设计不授权生产重放、账本补录、真实回执补发、配置变更、发布或升级。

### 已修复缺陷与约束

- 修复前推送入口调用 SDK `TradeDealHandlerBase.on_recv_rsp` 后只转发 DataFrame 行。SDK 的列清单
  不包含物理账户 ID；原始响应 `s2c.header` 则携带交易环境和账户标识。下游先设置
  `missing:push_physical_account`，导致应用层已有 source 身份绑定被跳过。
- history 可以提供完整身份并重新进入录入。现有 canonical identity owner 负责命名空间、
  账户映射与 broker execution 幂等，不能以配置账户标签或裸 deal ID 替代。
- 修复前 `secure_sqlite_artifacts` 打开并关闭数据库与 sidecar 文件以执行 `fchmod`。
  Linux 上额外的 `close` 会释放本进程在同一文件上的 POSIX 锁；已用临时 SQLite WAL 数据库
  和另一进程的锁探针复现。`ensure_private_file` 对已存在数据库的 open/close 也必须覆盖。
  这是已证实的共享存储缺陷；缺少故障 SQL 堆栈，不能断言它是个别 `locking protocol`
  异常的唯一原因。
- 修复前普通回执在 Inbox 中保存一次 `receipt_json`；只要已有回执，后续业务结果就被
  `durable_receipt_*` 拦截。失败通知送达因此会阻止后来成功通知。
- 修复前 retry 列表默认按 60 秒筛选、以 `attempt_count < 20` 过滤；claim 本身不检查到期
  时间、不递增计数，计数实际在结算时增加。因此崩溃重领没有统一预算保证，现有 claim 将计数与到期检查收敛在同一事务。通知失败、provider 接受但未确认、业务耗尽不能都表示为“未记录”。
- lifecycle outbox 已有独立投递所有权；有可读回 outbox 的 lifecycle 结果不得转为普通直发。

### 方案与责任边界

沿用现有入口、Inbox、ledger facade 与重试循环，只修复它们之间的契约。

| Owner | 责任 |
|---|---|
| `src/infrastructure/futu_trade_push.py` | 在 SDK 行转换丢字段前保留可信响应头身份；验证行与头的冲突 |
| `domain/domain/trade_execution.py` 经 ledger facade/deal_identity；现有 source binder | 复用执行身份生成；binder 校验 source 与映射，transport 不生成平行主键 |
| `src/infrastructure/private_storage.py` | 所有 SQLite caller 共用的私有权限、文件类型与锁保护 |
| `src/application/trades/intake.py` | 返回真实持久化结果、结构化失败分类与可重试性 |
| `src/application/trades/inbox.py` | 持久化业务 claim、实际重试政策和有版本的回执记录，使用事务与 CAS |
| `src/application/trades/receipt.py` 与 `receipt_compensation.py` | 共用 Inbox 发送资格；保留人工补偿的预览与确认边界 |
| `src/application/trades/auto_intake.py` | 复用当前循环协调业务重试和仅通知恢复，保持账户/source 隔离 |

不以延长 timeout 或对全部 `OperationalError` 盲目重试掩盖锁问题；不让历史回补承担本可从
推送头获得的身份；不清空旧 `receipt_json` 来补发成功；不新建并行的交易状态库或通用队列。

### 推送身份与首次录入

数据流为原始 SDK 响应头与成交行 → transport 保留来源证据 → source/account identity 验证
→ canonical execution key → durable Inbox → 现有录入 facade。响应头中的账户 ID 只有通过
当前 source、交易环境、订阅/可见账户与映射校验后才可作为物理身份。

行与头同时有身份时必须一致；缺少、格式不合法、环境不匹配或多账户歧义继续拒绝录入并
保留可诊断证据，不能清除真实错误来绕过校验。配置只约束允许的账户，不凭配置推断成交
属于谁。移除或收紧 source binder 中无证据的单账户推断分支；单账户 source 但无响应头/
可验证行身份也必须拒绝。直接注入 listener 的测试/兼容入口仍经过同一身份验证。

同一有效成交的 push 与 backfill 必须收敛到现有 canonical key。已有身份缺失行不按裸
`deal_id` 强制合并；只有现有证据导入机制可证明等价时才关联，未证明的继续保留待复核。
无法证明账户归属的入口错误只进入既有本地 audit/status，不借单账户通知 route 猜测
归属；已有可信账户的规范化/录入错误才可产生该账户的失败回执。这是来源安全下的明确
限制，不承诺所有缺身份推送都能即时发出账户回执。

### SQLite 权限与锁

连接建立前仅在目标不存在时创建私有空文件；发现已有文件不额外 open/close。
新建复用 `tempfile.mkstemp` 在同一私有目录创建唯一临时 inode，设置 0600 并先关闭 fd，
然后以不覆盖目标的 `os.link` 原子发布到数据库路径，最后删除临时名称。目标已存在则
丢弃自己的临时文件并验证已有目标，禁止 replace 已有数据库。发布前没有可由普通 DB
读写入口连接的目标 inode；发布后权限维护不再打开它，从而避免“创建者关闭 fd 释放
并发连接锁”的窗口。临时文件清理使用 finally，失败不能留下非私有目标。

全部 SQLite 创建入口经共享 helper，不对数据库调用通用 `ensure_private_file`。
不要求只读 `mode=ro` 入口执行 chmod、创建或获得业务锁；它们可能在发布后立即连接，
仍不会被 helper 的额外 close 破坏锁。初始化采用私有临时文件而非扩大连接锁覆盖范围，
以保留只读边界；验证中加入发布前后并发只读连接与创建竞争。
数据库与 journal/WAL/SHM 的权限维护使用不打开文件内容的 metadata 操作，并明确禁止
跟随符号链接。优先使用标准库提供的 no-follow 操作；不支持的平台应明确失败，不能
回退到跟随 symlink 的 chmod。检查路径类型、父目录私有性和操作后的状态，保留既有
0600 文件、0700 目录及特殊文件拒绝契约；sidecar 正常消失可容忍，权限异常不可吞掉。

全面核对共享 helper 的调用者，包括同进程多连接、初始化、事务内调用及关闭后的维护。
新文件初始化也不得对另一个活动连接的同名 inode 执行危险 open/close。连接建立后若
权限维护失败必须关闭新连接。检查与 chmod 之间的路径替换风险纳入安全测试；采用受控
私有目录与 no-follow 元数据操作，不引入会再次释放 SQLite 锁的文件描述符校验。

不新增连接准备锁；现有 ledger writer lock 和 SQLite transaction 继续负责业务事务串行性。metadata no-follow 调用必须通过实际 Linux 锁探针验证，不能仅凭 API 名称
假定其实现没有额外 open/close。信任已配置 runtime 根及既有祖先目录；直接父目录须为受控私有目录，
对直接父目录和数据库文件执行 no-follow 类型、inode 与权限复核；
不把同 UID 恶意进程修改其自身私有目录宣称为已隔离的安全边界。
只对已明确分类且确认可安全重试的临时故障沿用 durable 重试；本轮不增加进程内 sleep
或第二套快速重试政策。首发成功率主要通过修复身份与锁缺陷提升。

### 普通成交回执与恢复

以现有 Inbox 为唯一持久化 owner。扩展 `receipt_json` 为显式版本的记录，按语义保存
`pending_retry`、`recorded`、`manual_required`、`verification_pending` 回执，而不是每次异常建立新通知。
这些是普通回执的结果版本，不是新的交易生命周期状态。JSON envelope 包含 schema_version、
current_result_key 与按语义键索引的 receipts；`verification_pending` 专指经济状态待核对，
与投递 unknown 分离。键由 Inbox identity 与结果语义构成，不随
传输来源、重试次数或无经济变化的关联补充变化。同一语义只冻结一份内容，发送尝试用独立
attempt ID；保留各结果的 route/message、送达证据、次数、下次可尝试时间与停止原因。

普通即时发送、重复入口和通知恢复都先走 Inbox 的同一结果/attempt claim。state.json
只保留兼容投影，不得以旧 delivery_confirmed/unknown 否决新结果、复活旧结果或绕过
预算。没有 Inbox 的旧兼容路径保留原有抑制规则，不能用其状态猜测迁移后的发送资格。

所有正常、normalize 失败、resolve 失败及已知辅助异常出口统一先收敛经济事实，再以
claim/payload CAS 写入结果、重试决定和回执意图，最后进行外部发送。禁止异常分支绕过
该持久化步骤，禁止发送回调失败把已记录业务改回 pending。ledger 与 Inbox 的提交窗口
由 canonical readback 恢复；无法可靠读回时只进入待核对，不再执行经济写入。
恢复的账本读回与 Inbox 结果更新共用现有 ledger writer lock，等待仍在提交的过期 claim。
沿用 compensation → ledger → Inbox 的加锁顺序，外部发送前释放锁，不能把未提交快照中的缺失当成最终未记录。
Inbox 不可写时不发送无法持久化防重的通知；记录现有日志/status，存储恢复后继续协调。

成功回执的内容截止于收敛时已知的 ledger 及 before-receipt 结果（如 combo）；不等待后续
lifecycle timing、费用或 PM 刷新完成。这些后续结果继续由各自现有 status/audit 呈现，
不会追改冻结消息或新增辅助工作流通知。发送完成只更新投递记录与兼容投影。

| 业务事实 | 用户回执 | 后续动作 |
|---|---|---|
| 首次持久化并读回成功 | ✅ 已记录 | 一次成功回执 |
| 未持久化、可安全重试且未耗尽 | ⚠️ 暂未记录；原因；至少 60 秒后自动重试 | 同一失败阶段不重复刷屏；恢复后独立成功回执 |
| 不可自动重试或达到上限 | ❌ 暂未记录；原因；不会继续自动重试；处理建议 | 一次需处理回执；等待经授权的复核/修复 |
| 持久化成功，回执截止前已知辅助步骤失败 | ✅ 已记录；注明当时已知的未完成步骤 | 后续辅助任务由原有状态入口呈现，不重复写交易 |
| 未获得可靠持久化结论 | ⚠️ 记录状态待核对 | 先按 canonical key 读回；不能猜测成功或再次执行经济效果 |

业务重试政策集中在 Inbox claim 边界：首次可立即认领；以后所有处理入口均在同一 CAS
中检查 due time、lease、attempt_count 与可重试结论。成功 claim 原子消费一次现有计数，
最多 20 次；settle 不再递增。正常失败从结算时间起至少 60 秒后到期；claim 中断须先等
租约到期，不能以直接 processor 调用跳过期限。存储恢复读回与通知协调不消费业务预算。
旧计数按已结算次数保留，不虚构此前崩溃次数；迁移后每次新 claim 都按新政策消费。

第 20 次 claim 后崩溃也先执行无经济写入的 readback：确认已记录则恢复成功结果；明确
未记录则耗尽；读回不可用则“记录状态待核对”，不可写成“未记录”。人工 resume 保留
既有显式 operator 门与审计，重新开启预算不代表授权重发已确认或 unknown 的相同回执。

intake 分类稳定原因码；仅确认可安全恢复的暂态 SQLite busy/locked/protocol 等错误可
自动重试，权限、磁盘空间、schema、身份冲突等须给出对应处理建议，不能只凭异常类型。
已提交或提交状态不明的异常先读回，禁止绕过唯一事件/lot 约束。渲染读取持久化的
attempt/remaining/retryable、到期下限和调度条件，不自行计数或承诺精确执行时间。
用户看到可执行的原因和动作，原始异常/堆栈只进诊断，不泄漏路径或凭据。

`pending_retry` → `recorded` 或 `manual_required` 是不同可发送结果。重复的失败与重复
成功都不能新增经济效果或重复已确认回执。成功必须来自 ledger facade 的持久化与读回
证据；历史已处理但无新失败的成交继续保持历史抑制，不能因升级批量补发旧成交成功消息。

提交边界不明或最后一次 claim 中断且 ledger 暂不可读时，冻结 `verification_pending`：
“记录状态待核对；暂不重复录入；系统将继续核对”。每轮到期协调仅经 ledger 只读 facade
按 canonical key 核验事件/lot，不调用可能补关联的 resolver；本地读取可每 60 秒再次到期，
每批有上限，不消费经济或发送预算，也不重复发已确认的待核对通知。读取仍失败则保留
该结果；确认已记录则推进到 recorded；确认未记录且有预算则 pending_retry；确认未记录
且无预算/不可重试则 manual_required。每次推进按同一 supersession/CAS 规则废止旧未发
消息，允许最终明确结果独立发送；未知不能当有效零结果。核对与真实投递各自 unknown
字段不得混用。

保留当前 `begin/finish` attempt fence，扩展到结果版本、payload version/hash、账户及
冻结 route/message。claim 和 finish 必须核对当前 attempt ID 与版本；过期 worker 的
完成回调不能覆盖新结果。旧结果已在发送中时，不能宣称撤销该外部发送；新成功内容应
清楚说明为恢复结果，不能因为旧失败的已发送标记而跳过。结果推进时，原子撤销被替代
失败或待核对结果未来的发送资格；尚未发送、无 route 或明确未接受的旧结果只保留审计，不能在
成功或需处理结果之后再发送。claim 再核对 current_result_key。已经开始的旧 attempt
允许按自身 ID 完成记录，但不能修改当前结果或恢复自身重试资格。

投递状态沿用现有 `sent`、`failed`、`unknown` 语义：只有 delivery confirmation 才为
sent；明确未接受为 failed，允许在既有循环中按持久化节奏仅重试通知；accepted/unconfirmed、
超时或发送中崩溃为 unknown，禁止自动重发同一结果，状态诊断说明需核对投递。没有可用
通知路由时保留待发送与原因，不伪造已送达。新业务成功属于新结果，不被旧失败通知的
unknown 阻止；若同一成功通知 unknown 则继续防止重复发送。

通知恢复独立于业务 pending 查询，从相同 Inbox 选择有新结果或明确未接受的待发送回执；
已处理成交不重新进入 pipeline。用当前账户/source enabled、receipt enabled、route 与
身份校验约束选择；保留 notify_applied、notify_failed、notify_unresolved 等现有子开关：
recorded 属 applied，暂态/耗尽失败属 failed，身份明确的待复核属 unresolved；
verification_pending 沿用产生该结果的原通知类别并持久化，不借恢复绕过用户抑制。
停用时不发送，恢复启用后才继续。每个发送阶段必须有持久化意图，
业务结果提交后、意图冻结前崩溃，可由 Inbox 结果与 ledger readback 恢复，不依赖新 push。
source 拥有同一个到期协调函数，在正常循环、启动前与 reconnect 等待片段中调用。
transport 的 `start` 复用已有 SDK 初始化线程，在当前 `queue.get(timeout=0.1)` 等待点
提供可选 on_wait 回调；只暴露等待机会，不包含业务查询或发送逻辑。source 注入到期函数，
因此初始化长时间未完成也能恢复通知；回调每次先检查 stop/due，实际工作受批量与发送
超时约束，异常只进 status，不逃逸中断 SDK 等待。取消后不开始新的通知尝试。

同步 health 查询须有 SDK 连接等待与请求超时：transport 使用现有
`set_sync_query_connect_timeout` 设置有限连接等待，并沿用 SDK 请求超时；不能只依靠
_queue 轮询超时。恢复最早到期为 60 秒，实际还受本轮有界 I/O 延迟影响，不承诺精确周期。
测试初始化线程保持未完成、首轮通知明确失败、时钟到期后再次发送及 stop，不能只用
立即抛错的 listener stub。沿用现有线程与协调，不新建常驻服务或通用 supervisor。

达到上限时，即使下一轮业务查询已排除该行，也必须由现有循环的状态协调产生最后的需
处理意图。通知重试使用独立计数，不能消耗或复活业务 retry budget；同样有上限和终止
原因，以免永久刷接口。沿用 60 秒/20 次默认政策，不增加配置键或服务；通知 attempt
认领时原子消费其独立预算，未知发送不续发，无路由只是等待条件、不消费发送次数。
路由首次有效时冻结，重试前验证当前账户与 route 仍匹配；路由变更则停下待核对，不能
把冻结消息自动发往另一个目标。通知预算耗尽/unknown 由现有 intake status 的 Inbox
摘要展示结果、原因、最后 attempt 和处理建议；只读核对后另行授权补发，不自动发送
一张“发送失败”的通知来递归通知失败。

人工 `receipt_compensation` 必须先检查该成交是否由新版 Inbox 管理：是则 fail closed，
返回 Inbox 当前投递状态和恢复策略，禁止通过独立补偿 JSON 再次发送。旧补偿仍保留其
preview/hash/confirm 门。接管旧无路由记录前，在现有 compensation lock 内核对同一
account/canonical deal 的补偿证据并完成 Inbox 接管；旧补偿 apply 在同一锁内重新检查
Inbox 归属，避免先预览后并发发送。已有 send_started/unknown 视为歧义，confirmed 视为
已送达；多成交合并补偿的任一成员都不能再次自动发送。缺失/无法归属的证据 fail closed。
通知网络调用不持该迁移锁；新记录认领后由 Inbox 独立 CAS 防重。

旧单回执读取必须兼容：sent/unknown 保留其原状态；只有保存了明确失败业务结果且当前
已持久化成功，才建立恢复成功的新结果。无法证明旧回执对应何种结果时 fail closed 并
显示诊断，禁止猜测补发。旧失败 sent/unknown 只约束其旧语义，不能挡住有证据的新成功。
已有历史成功和空旧 receipt 不因升级批量补发；仅 live、可证明为本流程尚待完成的回执
进入恢复，保留 delivery_purpose 与 receipt_recovery_allowed 的历史保护。

扩展现有 `trade_inbox_writer_version()` 与 SQLite writer triggers：迁移在单一事务中
检查版本并替换旧 guard，新写入要求新 writer version；先安装 guard 再写新格式。
旧连接、旧二进制及不兼容降级必须在写入前失败，不得覆盖新版 JSON。新 reader 兼容旧
单回执；损坏/未知版本 fail closed。测试旧连接在迁移前已打开、迁移并发和降级写入，
不通过新增部署锁或文档提醒代替持久化兼容门。

### 实现切片与验证

1. **可信推送首次录入**：以真实 SDK 形状的响应头进入公开 listener，验证转换、source
   绑定、入箱与同一成交的 history 收敛；覆盖缺头、多账户、账户/环境冲突，不伪造默认账户。
2. **权限维护保持事务锁**：临时目录中复现 Linux SQLite WAL 写锁；独立进程在 helper
   前后都应无法取得锁，事务提交正确。覆盖同进程第二连接、权限修复、新建竞态、symlink、
   特殊文件、sidecar 消失和异常连接释放；macOS 通过不能替代 Linux 锁验收。
3. **失败到恢复的完整回执**：用真实 Inbox/ledger 与 fake sender 经过 auto-intake facade，
   验证首次成功、失败到成功、不可重试、耗尽、仅通知重试、无路由、unknown、进程重启、
   旧回执兼容、旧 worker 迟到、账户停用和 lifecycle outbox 交接。内部顺序为统一结果与
   原子预算 → 版本回执/兼容 guard → 外层通知恢复及补偿互斥，整体交付前不启用半成品发送。
   覆盖失败待发→成功后旧失败失效，失败已送达→耗尽仍发新结果；claim 后/最后一次
   claim 后崩溃、直接未到期认领、异常出口发送前后崩溃；OpenD 持续失败时仍恢复；
   补偿先完成或并发自动恢复、无 route 后恢复、route 变更、旧 writer 及两存储状态冲突；
   第20次 claim 崩溃→ledger 暂不可读→最终已记录/明确未记录，待核对通知失败/送达后
   最终结果仍正确发送，且核对阶段经济写入次数为零；初始化一直未完成时再次到期发送
   和取消可达；子开关抑制沿原类别生效。
   断言唯一事件/lot，
   冻结内容、发送次数及持久化确认；不以函数返回成功替代投递事实。

验证以现有 `test_trades_push_listener.py`、`test_private_storage.py`、
`test_trade_receipt_recovery.py`、`test_trade_receipt_claim_fence.py`、
`test_trade_receipt_concurrency.py`、`test_trades_inbox.py` 与 auto-intake 对应测试为基础，
按入口补最小回归，不复制实现做镜像测试。执行相关 import/依赖边界、文案与敏感信息 guardrails，
测试/import 变化时重新生成 `docs/DEPENDENCY_GRAPH.md`。

待落实的风险由对应 owner 处理：transport 需核对运行 SDK header 契约；private-storage
需证明无符号链接跟随和 Linux 锁保持；Inbox 需验证旧格式迁移、compensation 证据归属与混合 writer 保护；
status 的只读诊断不得把 unknown 提示成可直接安全重发。任何需要生产动作的验证先使用临时数据和 fake provider，
真实补发与部署另行取得授权。

业务到期时间由 Inbox 的 `next_attempt_at_ms` 保存，避免新推送补充关联信息时更新
`updated_at_ms` 而推迟或绕过重试窗口。既有非零次数按原更新时间迁移到期时间；只有真实
claim/settle 才推进业务重试到期时间，核对已确认缺失不会继续推迟录入。无法核对的结果
每 60 秒再次只读核对，显式操作员恢复才重置预算。迁移和 writer-version guard 在同一
事务内完成，调用方复用该事务，不再嵌套 `BEGIN`。

## 审计

trade-intake audit 只记录 `portfolio_refresh_hint_accepted` 或
`portfolio_refresh_hint_failed`。前者证明 PM 接受了提示，不能作为持仓已刷新、
已落库或已对账的证据。OM 不再保存 PM 刷新状态文件。

## 期权平仓两阶段状态

期权生命周期不再用一条状态同时表达“已经平仓”和“为什么平仓”：

1. 第一阶段确认平仓事实。Futu 零价期权成交进入 durable Inbox，以
   `futu:<account>:<futu_account_id>:<deal_id>` 占用唯一 broker source，
   冻结受影响 lot 和合约数量，并生成一次 `option_leg_closed` Outbox 意图。
2. 第二阶段确认平仓原因。原因未确认时为 `cause_pending`；证据完整后写入
   canonical terminal event 和 allocation，成为 `resolved`；缺证、来源冲突、
   数量冲突或投影漂移进入 `needs_review` 或 `conflict`，不得猜测原因。

平仓事实不会因为原因尚未确认而消失；原因确认也不能再次消费同一 broker
成交。`resolution_revision` 只随业务结论变化，通知重发只增加
`delivery_revision`。

Lifecycle discovery 只冻结到期 lot 并创建 immutable case，不刷新已有 case 的
`status` 或 `derived_summary`。既有 case 的派生状态由 canonical lifecycle read model
计算，并只由 account-scoped `reconcile-due` 通过 ledger 原子 transition writer 推进。
无 option-close anchor 的 case 在 canonical deadline 后仍 fail closed 为人工复核，但不因
legacy discovery 重放而改写 broker timing policy 口径。

History backfill 只从本次查询的 Futu account IDs 与 canonical account mapping 导出
显式账户范围，并对每个账户分别执行 discovery；不向 discovery 传
`account=None`。任一 configured Futu account ID 缺少 mapping 时，该轮 lifecycle discovery
整体 fail closed，不部分扫描其他账户。Legacy multi-account source 仍可用，但也必须逐账户
隔离执行。

## 平仓原因判定

按冻结的合约截止时间先分流：

- 截止时间前，正价格且存在同一正常订单成交，判定 `trade_close`。
- 截止时间前，零价格并有唯一、数量匹配的股票交收，short option 判定
  `assignment`，long option 判定 `exercise`。
- 截止时间后，存在唯一、数量匹配的股票交收，short option 判定
  `assignment`，long option 判定 `exercise`。
- 截止时间后，只有在第二个后续 broker business day 结束后，完整观察同时
  证明期权仓位已消失、没有股票交收、没有现金交收、没有正常平仓订单、
  projection 与冻结余量一致、source reservation 唯一时，才判定
  `expiration_no_settlement`。

结算观察必须冻结并校验历史成交、历史订单、fresh positions、逐 clearing
date cash flow、交易日历和合约元数据的查询输入、返回码、覆盖范围、行及
payload hash。任一来源不完整、日历 hash 变化、零价锚点无法在历史成交中
唯一复核、source claim 不匹配或数量超出冻结余量，统一进入人工复核。

## 通知 Outbox 与批量回执

### Combo Yield 自动归组

自动归组按账户显式开启；默认仍关闭：

```yaml
trade_intake:
  combo_reconciliation:
    default_mode: off
    accounts:
      sy: auto
```

`auto` 只自动采用 `exact_delivered_candidate`、没有候选替代项且属于唯一最优解的
Put + Call 配对；缺证据或多解继续停在待确认状态。第二腿归组成功后，成交回执显示
`组合｜✅ 已自动归入 Combo Yield（Funding Put + Participation Call）`。

`observe` 只记录提案，`confirm` 生成提案并要求人工执行 `confirm-combo`；`auto` 下仍可
手工确认未被自动采用的提案。

成交先持久化，再执行每组独立事务的 adoption，最后渲染回执。单组 adoption 失败不会回滚已记录
成交，也不会把失败提案标成成功；错误保留在 reconciliation 结果中，提案继续等待后续自动重试或
人工确认。把账户改回 `confirm` 或 `off` 只影响后续自动采用，不拆除既有组合；既有误归组使用
`supersede-combo` 的 append-only 回滚路径。

普通开仓成交保留逐 broker deal 的 intake 回执；已处理的 deal 在
history backfill 中会于 pipeline 之前跳过，不会因回执未确认而重放交易。
provider 命令已成功但缺少 delivery confirmation 时记为
`unconfirmed`，后续 duplicate 不自动重发；
`retry_unconfirmed_duplicate` 只对没有 provider acceptance 或歧义发送证据的
缺失/`failed` 回执生效。

已形成 lifecycle 状态变更的平仓及其他 lifecycle 通知不走普通
intake 直发；写入前的普通 intake `unresolved`/`failed` 仍是成交操作回执。只有
ledger 结果携带 `notification_outbox_id`，且同一 SQLite 仓库能立即读回
该 outbox row 时，intake 才记录 `receipt.status=outbox_managed`，并保存
`outbox_id` 与 `outbox_readback_confirmed=true`。声称了 ID 但读回失败，
或已完成的 lifecycle 结果没有 outbox ID，都会 fail closed，不会
回退成一次可能重复的直发。

业务事务仍然一条状态变化写一条冻结通知意图，用于案件级审计；它不在 ledger
事务内调用飞书。外部发送单位改为 delivery batch：同一
provider/channel/target 的 `lx`、`sy` 等账户意图可进入同一批次，一条意图不会
因批量发送而丢失或改写。只有 enabled source 且 receipt enabled 的账户可被
绑定；禁用账户的历史意图保持可见、pending、unbound。

planner 等待最新意图安静 10 秒，但最老意图最多等待 60 秒；到点后把当时所有
符合条件的意图一次性冻结到一个批次，不按成员数拆分。批次只保存目标指纹，
不保存或输出原始 target。绑定后的成员状态为 `batched`，旧版逐行 dispatcher
不会重新认领这些行。

批次使用 CAS 状态流转：

```text
pending -> claimed -> send_started
send_started -> confirmed | accepted | explicit_failed | unknown
```

- `claimed` 在发送前租约过期可安全退回 `pending`；成员保持 `batched`。
- `send_started` 后进程失联、超时、瞬时错误或 fallback 歧义必须把整个批次
  冻结为 `unknown`，不能自动重发。
- 明确的发送前失败、HTTP 4xx 或无歧义的 provider 拒绝才进入
  `explicit_failed`；最多尝试三次，退避 60 秒、5 分钟。
- 每次尝试都以稳定 `batch_id` 作为 transport idempotency key；同一路由
  60 秒内最多开始一次发送。
- `accepted` 表示 provider 已接受但尚无强确认；不能伪装成 `confirmed`。
- `confirmed`、`accepted`、`unknown` 或耗尽重试的失败会原子投影到全部成员。
- `unknown` 只能由操作员依据 provider 证据确认，或为每个原成员创建增加
  `delivery_revision` 的补偿意图；原批次和原记录都不重开。

单成员批次沿用原有回执文本。多成员批次按案件选代表，最多展开 12 个案件，
其余只显示数量；展示截断不改变批次完整成员集合。trade-intake status 将
Inbox、生命周期原因、逐意图 Outbox 与 delivery batch 分开显示，并提供未绑定
意图、未知批次、批次成员数及已减少消息数，同时保留 source 的 `pid`、
`source_id`、OpenD host/port、账户和启动时间。

监听进程只创建一个全局 `LifecycleReceiptBatchDispatcher`，统一领取全部启用账户
的同路由批次；source listener 不再按账户发送回执。dispatcher 每秒进行一次可
取消轮询，每轮最多尝试一个批次，并在所有 source listener 停止后、运行时资源
关闭前退出。provider I/O 位于 `process_lock` 和 SQLite 事务之外，慢发送不会
阻塞新的成交、Inbox 或生命周期事实写入。

每个 source 的 status 在 `lifecycle_delivery.dispatcher` 下显示全局调度器状态、
允许账户、最近一次批次结果或错误及 provider/channel/route 指纹；这里不会显示
原始 target。`dry-run`、所有 receipt 均禁用或路由不可用时不会启动 dispatcher，
状态分别显示 `dry_run`、`receipt_disabled` 或 `route_unavailable`。

## 运维命令

以下命令均以 dry-run 为默认。示例同时列出预览和显式 one-shot applied 形式；
实际写入必须同时给出 `--apply` 和 `--confirm`（或 `--yes`），发送通知还需要
明确授权真实发送。

`lifecycle reconcile-due` 的默认模式和显式 `--dry-run` 都只计算本地计划：
不要求 broker/quote 路由 ready，也不会构造或查询 provider gateway。只有显式
`--apply --confirm`（或 `--apply --yes`）才会访问 provider 并写入结算结果。
apply 会在创建 gateway 前把当前账户的 lifecycle audit heads 持久化到 intake
audit JSONL，并在有实际 attempt 时追加 touched-head seal。任一 seal 写入失败都
返回非零；已提交的 attempt 不会因此重调 provider，下一次 apply 会先补写当前
账户 checkpoint。

```bash
# 查看 case、证据和当前 revision
./om option-positions lifecycle list --account lx --include-evidence
./om option-positions lifecycle inspect --case-id <case-id>

# 到期结算观察与原因 reconciliation 预览
./om option-positions lifecycle reconcile-due \
  --account lx --config config.us.json --dry-run

# 使用已持久化 broker 证据人工确认；先预览
./om option-positions lifecycle resolve \
  --case-id <case-id> --expected-revision <revision> \
  --reason assignment --broker-ref <canonical-broker-ref> \
  --note "<operator evidence>" --dry-run

# 更正既有终态；只追加 void 与 replacement，不删除历史
./om option-positions lifecycle correct \
  --case-id <case-id> --expected-revision <revision> \
  --void-terminal-event-id <event-id> --reason assignment \
  --broker-ref <canonical-broker-ref> \
  --note "<correction evidence>" --dry-run

# 查看逐意图及其所属批次，或直接查看完整批次
./om option-positions lifecycle receipts inspect \
  --outbox-id <outbox-id>
./om option-positions lifecycle receipts inspect \
  --batch-id <batch-id>

# 发送预览可按账户观察，但不会绑定或发送
./om option-positions lifecycle receipts dispatch \
  --once --account lx --config config.us.json --dry-run

# applied dispatch 必须是全局的，不能带 --account
./om option-positions lifecycle receipts dispatch \
  --once --config config.us.json --apply --confirm

# 多成员批次只能用 batch-id 整体收敛
./om option-positions lifecycle receipts reconcile \
  --batch-id <batch-id> --mark confirmed \
  --broker-ref <provider-ref> --note "<verification>" --dry-run

# 历史切换：先 inventory，再显式选择 exact target
./om option-positions lifecycle migration inventory
./om option-positions lifecycle migration inventory \
  --mapping-manifest <lifecycle-explicit-mapping.json>
./om option-positions lifecycle migration inventory \
  --mapping-manifest <lifecycle-explicit-mapping.json> \
  --select-target <target-key>
./om option-positions lifecycle migration apply \
  --manifest <frozen-manifest.json> --dry-run
```

`--outbox-id` 仍可处理 legacy 未绑定记录和单成员批次；如果成员属于多成员
批次，命令会拒绝并提示准确的 `--batch-id`。`accepted` 只能人工收敛为
`confirmed` 或 `unknown`，不能直接 resend；进入 `unknown` 后才允许显式
`--mark resend`。人工收敛会保留原始 provider receipt。

`lifecycle confirm-expired` 已退役。禁止用人工按钮直接制造
`expiration_no_settlement`；该结论必须来自完整且冻结的 broker settlement
observation。

## 当前决策投影迁移（shadow-only）

Phase 3B 只增加影子读面，legacy 决策仍是唯一业务权威。先在停止 trade-intake
的离线副本上执行只读命令；本阶段没有自动 apply、服务切换或历史删除。

```bash
./om option-positions decision-projection inventory > current-decision-inventory.json
./om option-positions decision-projection verify
./om option-positions decision-projection status

# 仅对同一个未漂移 ledger 使用刚冻结的 inventory；这是本地高风险写入
./om option-positions decision-projection apply \
  --manifest current-decision-inventory.json --apply --confirm
```

- `inventory`、`verify`、`status` 均为只读，并校验 SQLite 文件尺寸不变。
  `status=absent` 表示尚未建立投影；`dirty` 表示源、schema 或 generation
  不可信；`mismatch` 表示只有部分账户缺失或与 oracle 不一致；只有 `clean`
  才允许 shadow readiness 继续评估。
- `apply` 在 `BEGIN IMMEDIATE` 内重新核对 store identity、实现指纹和 authority
  fingerprint。manifest 过期、目标 ledger 不同或任何校验失败都会整笔回滚；
  相同 manifest 对 clean 状态重放返回 `write_applied=false`，不会产生 SQLite
  DML 或 WAL/SHM 增长。
- 修复流程不是手改 JSON 或单表补行：重新停止 writer、重新生成 inventory，
  核对 readiness/reasons 后再执行一次 manifest-bound apply，最后重新运行
  `verify` 和 `status`。
- schema 启用后，旧版本 writer 的无账户 assigned-stock 写入会被 guard 拒绝，
  其它未适配写入会使 generation 变脏并令新读面失败关闭。因此升级窗口内不得
  混跑旧、新 writer；降级只恢复 legacy 读权威，不删除 additive 表，也不猜测
  或回写旧状态。

## 历史切换安全顺序

保持 trade-intake 停止，先做 WAL-safe ledger 快照，再生成 inventory。
`needs_review` 行不得 apply；只显式选择 `exact` 行，核对 manifest hash 和
数量后先 dry-run。apply 每行在单事务内写 source claim、历史通知 suppression
和 migration receipt；重复 apply 相同 manifest 为 no-op，源状态漂移或 claim
owner 冲突则失败关闭。切换完成后仍需独立验证 projection、Outbox、状态文件
和重复消息计数；启动服务与真实发送属于另一次明确授权。

普通平仓的历史通知迁移只接受完整且一致的 canonical broker deal key：
`futu:<account>:<futu-account-id>:<deal-id>`。旧事件顶层 account 缺失时，
只有 contract key 和 raw close target 等候选账户唯一且一致才可恢复；账户
冲突、部分券商标识或未知来源继续进入 `needs_review`。完全没有券商标识的
`manual_trade_event` / `system_trade_event` 是内部账本历史，不属于 broker
deal replay；已被有效 void 的 close 也不是迁移目标，两者均不得生成历史通知
回执。

旧 lifecycle case 只能通过 operator-curated
`lifecycle_explicit_mapping.v1` 进入自动迁移。每行必须给出：

- `legacy_case_id` 和 `disposition`：`terminal_frozen` 或
  `bridge_to_v2`；
- 完整 `canonical_contract`、逐 lot 的
  `target_contracts_by_lot`；
- 每条 broker 证据的 `evidence_id`、canonical
  `futu:<account>:<futu-account-id>:<deal-id>` source key 和 role；
- `terminal_frozen` 必须引用已经存在且未 void 的 terminal event；
  assignment/exercise 还必须引用同账户、同 Futu 账户、同标的、正确方向、
  数量和执行价的股票交割证据，并冻结 `settlement_window`；
- legacy case 的 multiplier 与 canonical terminal event/lot 不一致时，只允许
  用 `legacy_case_exceptions.multiplier` 精确冻结 legacy 值、canonical 值和
  operator reason；其它 case 合约字段不接受豁免；
- `bridge_to_v2` 必须引用已经存在的 v2 case 和完整
  `lifecycle_timing_policy.v1`。

迁移器逐项核对 case、broker source、terminal event、lot projection 和
账户/合约身份。`terminal_frozen` 只绑定既有证据、写 source claim、suppression
与 migration receipt；不会新增或改写经济 terminal event，也不会改动仓位。
`bridge_to_v2` 只绑定 Futu account、timing policy、非 allocating bridge
evidence 和 supersession；不会生成 terminal event。

运行期 `reconcile-due` 只调度 active v2 case：superseded legacy case
不会进入采集，单个 malformed active case 会按 case 返回人工复核原因，
不会中断同批其它 case。v2 case 可以只读解析经过完整校验的 migration
bridge 和 legacy zero-price broker anchor，用于采集一份新的、独立冻结的
settlement observation；legacy source claim 始终保留原 owner，不得释放、
转移或复制到 v2，bridge 本身也始终不参与 allocation。
