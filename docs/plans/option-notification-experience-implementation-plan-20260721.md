# 期权监控通知体验升级 Implementation Plan

## 0. Plan 元信息

- 日期：2026-07-21
- 状态：accepted after second adversarial review，进入 implementation gate
- 产品真源：`docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md`
- 实施范围：scheduler、成功扫描快照、通知决策与确认状态、用户投影、只读查询、状态迁移
- 当前边界：本地实施与验证；不自动改线上配置、不触发真实通知、不迁移生产状态

## 1. 目标、非目标与成功信号

### 1.1 目标

在不增加第二套扫描器、第二套候选权威或第二套 sender 的前提下，把当前 Daily Decision Brief 的“material change 决定发不发”改为：

```text
一套 canonical 策略扫描
  -> 每次可靠成功都更新最近成功快照
  -> 扫描后统一判断：固定完整报告 / 新增候选通知 / 静默
  -> 用户随时查询最近成功快照
```

### 1.2 成功信号

1. 市场当地时间 `09:40 + 有效整点 + 15:50`，每个账户、每个市场稳定发送完整报告；无候选也发送。
2. 交易时段内有效半点的成功扫描发现未送达的新候选身份时，当轮发送一条新增候选通知。
3. 固定报告点同时有新增候选时只发送一条完整报告。
4. 每次可靠成功扫描更新 current；pipeline/关键数据失败不覆盖 current。
5. 查询读取最近成功 current，而不是最后一次发送内容。
6. 报告包含现金总额、可用于期权开仓的资金、候选容量；不显示总资产/NAV/证券市值。
7. provider 未确认、`--no-send`、quiet hours、确认写入失败均不推进 confirmed 状态。
8. 同一固定批次不重复；同一候选身份同一市场交易日不重复。
9. 所有用户 Markdown 隐藏 revision、digest、pointer、内部 ID 和原始状态枚举。
10. 候选过滤、排名、Close Advice、ledger、broker fetch 和 sender 保持单一现有权威。
11. 非 scan target 的 10 分钟唤醒不运行 pipeline，但可精确重试已经持久化的 delivery envelope。
12. scheduler 以每账户已处理的 scheduled scan target 去重；pipeline 完成时间只用于观测，不得吞掉下一计划点。

### 1.3 非目标

- 不新增候选专用扫描器、行情流或分钟级实时系统。
- 不新增候选通知专用 timer、scheduler 或频率配置。
- 不改变候选过滤/排名/资金风控业务规则。
- 不把通知状态写回交易、持仓、broker 或 ledger。
- 不为固定报告、候选通知、查询建立三份快照。
- 不重写 provider adapter、retry 或 delivery confirmation 状态机。
- 不删除 immutable revision；revision 继续用于审计和恢复，但不再决定普通通知类型。

## 2. 本计划锁定的产品与技术决策

PRD 第 17 节仍列出推荐项。为使 plan 可直接实施，本计划按以下推荐值设计；如 CEO 不同意，应在 `/execute` 前修改计划，而不是由实现者临场决定。

1. **扫描目标**：复用现有 10 分钟 scheduler 唤醒，但实际 canonical pipeline 只在固定报告点与交易时段内有效 `HH:30` 执行；`09:30` 不扫描，由 `09:40` 开盘完整报告替代。
2. **固定报告点**：继续由现有 `schedule.run_points` 表达 `09:40 + 有效整点 + 15:50`，它只决定完整报告，不再决定是否扫描。
3. **候选身份**：`账户 + 市场 + 标的 + 策略族`；换到期日、行权价或具体合约不形成新身份。
4. **候选通知展开**：最多展开 3 个；同轮其余候选以数量提示，确认成功后整批视为送达。
5. **默认查询范围**：未指定账户/市场时读取全部启用账户与市场，按账户、市场分节；明确条件时只查指定范围。
6. **无新增配置键**：复用 `cron_interval_min` 作为 timer/catch-up 语义，复用 `run_points` 表达固定报告点；半点检查是产品固定合同，不增加可配置频率。
7. **同一 tick 每账户每市场最多一条普通监控消息**：固定报告优先于候选通知；故障/恢复和业务回执仍按既有独立合同运行。
8. **delivery-only retry**：非 scan target 只允许处理已经持久化的 pending/ambiguous envelope；不运行 assembler、不刷新内容、不增加 revision，也不新建 scheduler/sender。

## 3. 当前真实链路与根因

### 3.1 当前 scheduler 把扫描和通知绑在一起

`src/application/scan_scheduler.py::decide()` 当前只生成 `run_points` 目标。在目标到期时同时设置：

```text
should_run_scan = true
is_notify_window_open = true
```

非目标时两者均为 false。因此 `cron_interval_min=10` 当前主要用于 catch-up grace/timer cadence，不代表每 10 分钟执行策略扫描。

直接后果：如果 13:30 没有 canonical 扫描，就不可能在该半点发现并发送新增候选。

### 3.2 当前 brief 快照只在 `should_notify != false` 时生成

`src/application/tick_notification_flow.py::_prepare_daily_brief_notification()` 遇到 `should_notify is False` 会跳过 assembler 和 repository。

直接后果：

- 非固定扫描即使未来执行，也不会更新 current；
- 查询无法保证读到最近一次成功扫描；
- “是否通知”错误地控制“是否形成业务快照”。

### 3.3 当前 repository 是 material-change delivery pointer

`src/application/daily_decision_brief_repository.py::prepare_daily_decision_brief()` 当前：

1. 写 immutable revision 和 current；
2. 与最后确认送达 revision 比较；
3. 产生 `full / delta / none`；
4. 用单一 `daily_decision_brief_delivery.v1` pointer 确认。

这与 PRD 冲突：固定点无变化也必须发完整报告；非固定点只关心新增普通候选；固定报告点和候选身份需要独立确认。

### 3.4 pipeline 失败可能成为 current

当前 assembler 会把 pipeline failure 组装为 `blocked` brief，repository 仍会写 revision/current。

直接后果：最近成功快照可能被失败产物覆盖，查询和下一轮比较可能把“数据缺失”误解为“没有候选”。

### 3.5 当前 action identity 过细

`build_daily_brief_action_id()` 包含 expiration、strike、contract symbol、strategy group/leg 等字段，不能直接用于“标的 + 策略族”的提醒去重。

### 3.6 当前快照缺少稳定的账户资金区块

候选容量已有，但账户级资金没有进入 brief。pipeline 同轮已写出：

- run-scoped `portfolio_context.json` 的 `cash_by_currency`；
- run-scoped `option_positions_context.json` 的 cash-secured 使用情况；
- candidate CSV 的 required/free/capacity 字段。

通知阶段不应再次请求 broker。

### 3.7 当前 scheduler 把完成时间当作计划点水位

`scan_scheduler.mark_scheduler_accounts()` 在 account pipeline 返回后把实际完成时刻写入 `last_run_utc_by_account`；`decide()` 又用该值判断 scheduled target 是否已经处理。若 `09:50` catch-up 执行 `09:40`，但到 `10:00` 之后才完成，完成时间会错误覆盖 target identity，导致真正的 `10:00` target 被跳过。`15:30 -> 15:50` 同理。

### 3.8 当前 no-scan 分支也跳过了持久化 delivery

`multi_account_tick` 当前在 `_has_scan_to_run() == false` 时直接完成 tick，不进入 notification flow；即使进入下层，`tick_notification_flow._prepare_daily_brief_notification()` 遇到 `should_notify is False` 也会跳过账户。若 `15:50` pipeline 成功但 provider definite failure，`16:00` no-scan tick 根本不会重试，最终批次可能直接过期。扫描 cadence 与已经准备好的 delivery 恢复被错误绑定。

## 4. 目标架构

```text
system timer / tick-cron（现有）
  -> scan_scheduler
       - should_run_scan：fixed 或有效半点 scan target 是否到期
       - is_notify_window_open：本扫描点是否为固定完整报告点
       - scheduled_scan_target_market：本次扫描计划点
       - scheduled_target_market：固定报告批次；非固定点为 null
  -> should_run_scan?
       ├─ yes
       │   -> account_run / canonical pipeline（只执行一次）
       │   -> account execution terminal result
       │   -> reliable success?
       │        ├─ yes
       │        │   -> assemble one canonical brief
       │        │   -> persist immutable revision + successful current
       │        │   -> update candidate detection state
       │        │   -> durably prepare fixed/candidate envelope or quiet outcome
       │        └─ no
       │            -> do not advance successful current
       │            -> fixed point: persist failure artifact + failure envelope
       │            -> non-fixed point: persist terminal failure audit
       │   -> commit exact processed scheduled target + actual completion time
       │   -> dispatch prepared envelope, if any
       └─ no
           -> do not run pipeline/assembler or create snapshot/revision
           -> optionally load one persisted retryable delivery envelope
  -> existing scheduled_notification / provider adapter
  -> confirmed result updates fixed/candidate confirmation state

read tool / CLI
  -> read successful current only
  -> render query projection
  -> no write to notification state
```

核心边界：

- scheduler 只回答“扫不扫”“是哪一个 scheduled scan target”和“是不是固定报告点”；
- service 只组装同轮业务事实；
- repository 负责 revision/current、通知状态、锁、原子写和迁移；
- domain pure functions 负责候选身份与四格决策；
- account execution 只返回“实际尝试的账户 + scheduled target”候选映射，不直接推进水位；
- flow 先持久化本 target 的成功快照/失败证据及应发送 envelope，再通过现有 scheduler writer 提交 processed-target 水位，最后才调用 sender；
- flow 同时负责 no-scan delivery-only retry 的 I/O 编排；
- renderer 只做用户投影；
- sender 保持不变。

## 5. 模块级修改方案

| 模块 | 当前责任 | 修改方案 | 明确不做 |
|---|---|---|---|
| `src/application/scan_scheduler.py` | 生成固定 run-point，并把扫描/通知同时打开；用实际完成时间去重 | 分离 scan/report targets；新增 `last_processed_scan_target_utc_by_account` 作为 target 水位，`last_run_utc_by_account` 只记录实际完成时间；legacy state 缺少新字段时兼容读取旧水位 | 不增加第二个 scheduler、timer 或新配置 |
| `domain/domain/tool_boundary.py` | scheduler decision v1 normalization allowlist | 把 `scheduled_scan_target_market` 作为 v1 optional additive field 纳入 normalization/validation；旧 payload 缺失时归一为 `None` | 不建立 v2 schema 或平行 boundary |
| `domain/domain/engine/decision_engine.py` | scheduler DTO/view 与通知窗口兼容 | `SchedulerDecisionView` 透传/规范化 fixed-report 语义和 optional scan target；保留 `is_notify_window_open` 兼容字段；account execution 继续复用完整 normalized scheduler payload，不新建平行 DTO | 不承载候选业务判断 |
| `domain/domain/multi_tick.py` | account notify/dispatch 纯判断 | 仅适配“should_notify 表示固定报告点”的输入语义；不再把 false 等同于无需生成快照 | 不实现 snapshot/repository I/O |
| `src/application/multi_tick_scheduler.py` | 全局/账户 scheduler 决策 | 将 normalized decision 原样保存在 `scan_decision_by_account[account]["scheduler_decision"]`，向 account execution/tick flow 透传 scan target、fixed target；账户仍独立去重 | 不增加第二条 pipeline 路径 |
| `src/application/multi_account_tick.py` | 无扫描时在 notification flow 之前直接结束；scan 路径在 account execution 后直接进入发送 flow | no-scan 改为显式 delivery-only 分支；scan 路径把 per-account target map 和 scheduler commit callback 交给 notification flow，确保 durable prepare -> watermark commit -> send | 不创建第二个 flow/sender；no-scan 不创建 pipeline workspace |
| `src/application/tick_account_execution.py` | 并发执行账户 pipeline，结束后批量标记 scanned | 从每账户 normalized scheduler decision 提取 `scheduled_scan_target_market`，在 outcome 中返回 `account -> target`；移除这里的提前 mark 和静默吞错；force/manual target 为 `None` | 不用当前完成时间冒充 scheduled target；不在 durable outcome 准备前推进水位 |
| `src/application/tick_scheduler_context.py` | 非 scheduled 场景默认 scheduler context | 补齐新字段的安全默认值 | 不制造固定报告批次 |
| `src/application/account_run.py` | 执行单账户 pipeline，返回 `AccountResult` | 保持一次 pipeline；`ran_scan` 表示本轮是否真实尝试；`should_notify` 继续作为 fixed-report compatibility bool；失败原因结构化透传 | 不在这里做候选差异或发送 |
| `domain/domain/daily_decision_brief.py` | brief normalization、action identity、material diff | 增加可选 `funds`、compact `candidate_index` normalization；增加 candidate alert identity、eligible identity extraction、四格通知 pure decision | 不改 canonical candidate rank/filter；不复用细粒度 action_id 作为提醒身份 |
| `src/application/daily_decision_brief_service.py` | 从 run artifacts 组装 brief | 每次可靠成功组装；从同轮 context 构造 funds；从全部合格候选生成 compact candidate index；失败不在此伪装成成功快照 | 不调用 `query_cash_footer()` 或 broker；不重排 Combo Yield |
| `src/application/daily_decision_brief_repository.py` | revision/current + v1 material delivery pointer | 拆分“持久化成功快照”和“准备/确认通知”；升级 delivery state v2；envelope 持久化 exact rendered payload、source/hash/key 和 pending/ambiguous/confirmed；提供只读加载 retryable envelope 与显式 v1->v2 迁移/校验 | 不再用 last-delivered diff 决定普通通知；不新增数据库/outbox 服务 |
| `src/application/tick_notification_flow.py` | brief assemble/render/send/confirm 编排 | request 增加 `delivery_only/account_ids/processed_target_by_account` 与窄 commit callback；scan 分支执行 durable prepare -> scheduler watermark commit -> existing sender；no-scan 只读取 persisted exact envelope 做 retry/核验 | 不复制 sender/retry；不从 Markdown 反推候选；不在 no-scan 分支生成新内容 |
| `src/application/daily_decision_brief_renderer.py` | full/delta lifecycle Markdown | 提供 fixed report、candidate alert、fixed failure、query 四种投影；复用候选/持仓/资金子渲染；隐藏内部字段 | 不输出普通 material-delta 文案 |
| `src/application/scheduled_notification.py` | route、quiet/no-send、send execution、result classification | 原则上不改；只有现有 execution result 缺少 flow 所需的 definite/ambiguous 分类时才做窄透传 | 不建立第二套发送状态机 |
| `src/application/agent_tools/daily_brief.py` | 单账户/单市场 current/revision 只读工具 | latest 查询支持可选账户/市场聚合；Markdown 使用 query context；普通不可用文案隐藏 revision；精确 revision 保留为结构化运维能力 | 不刷新、不发送、不推进状态；不硬编码中文触发词 |
| `src/interfaces/cli/daily_brief_ops.py` | `daily-brief latest/day` | latest 支持 all/single 范围；保留 day/revision 运维读取；增加显式 delivery-state inspect/migrate 子命令时必须 dry-run 默认 | 不把迁移埋进普通 latest 查询 |
| `src/application/agent_tools/operations_impl.py` | scheduler/runtime 状态展示 | additive 展示 processed scheduled target 与 actual last run；旧 state 仍可读 | 不改变 readiness 判定权威 |
| `src/application/config_defaults.py` / `config_validator.py` | schedule/daily brief 默认与校验 | 预计只更新语义文档和现有字段测试；不增加键 | 不改变 `daily_brief.enabled` 默认值 |
| docs/tests | 合同与回归 | 更新 scheduler 语义、通知矩阵、迁移 runbook、查询示例 | 不改无关文档或旧 review artifact |

### 5.1 预计无需修改的模块

- `domain/domain/engine/candidate_engine.py`
- candidate filter/rank adapters
- `domain/domain/close_advice.py`
- ledger/projection/trades/positions 写路径
- `src/application/notification_delivery_adapter.py`
- provider sender（Feishu/WeChat 等）
- broker fetch / OpenD 查询实现

## 6. Scheduler 设计

### 6.1 两组目标点

把当前 `_scheduled_run_targets()` 拆为两个窄概念：

1. `report_targets`：现有 `run_points` 产生的 `09:40 + 整点 + 15:50`；
2. `candidate_check_targets`：交易时段内的 `HH:30`，但排除 `09:30`；
3. `scan_targets`：`report_targets ∪ candidate_check_targets`。

两组目标都复用：

- market timezone；
- `run_window`；
- breaks（HK 午休）；
- gates；
- market trading-day guard；
- account-scoped processed scheduled-target watermark；
- catch-up grace。

默认 HK 扫描示意：

```text
09:40, 10:00, 10:30, 11:00, 11:30,
[12:00-12:59 午休无扫描],
13:00, 13:30, 14:00, 14:30, 15:00, 15:30, 15:50
```

现有定时任务仍可在 `09:50`、`10:10`、`10:20` 等时刻唤醒。scheduler 对扫描返回 no-op，不启动 pipeline；通知 flow 可以在既有发送窗口内只处理一个已经持久化的 retryable envelope。

### 6.2 Scheduled-target 水位

scheduler state 拆分两个语义：

```text
last_processed_scan_target_utc_by_account   权威去重水位：账户已处理的 scheduled target
last_run_utc_by_account                     观测字段：账户 pipeline 实际完成时间
```

规则：

- `decide()` 优先使用 `last_processed_scan_target_utc_by_account[account]` 判断 due target 是否已处理；
- 旧 state 没有新字段时，暂以 legacy `last_run_utc_by_account[account]` 作为兼容 seed；该账户第一次处理新版 scheduled target 后写入新字段并永久切换到 target watermark；
- `tick_account_execution` 必须返回 `account -> scheduled_scan_target_market`，但不能在 account pipeline 返回后立即写 scheduler state；
- notification flow 必须先把该 target 对应的 durable outcome 准备完成：可靠成功需持久化 successful current、candidate state 及应发送 envelope/quiet 决策；fixed pipeline failure 需持久化 failure artifact + failure envelope；non-fixed failure 至少需落 terminal audit；
- durable prepare 成功后、调用 provider 之前，才由窄 commit callback 把 exact target 写入 processed watermark；可靠成功与 pipeline failure 都算“该 target 已处理”，避免同一计划点每 10 分钟重复扫描；
- 同一次 scheduler state write 记录实际完成时间到 `last_run_utc_by_account`，但完成时间不得参与新版 target 去重；
- force/manual 没有 scheduled target，只允许更新实际完成观测，不推进 processed watermark；
- target 缺失、格式错误或账户映射不一致时不猜测、不推进水位，并写结构化 audit；
- processed-target state write 失败不得调用 provider；tick fail closed，durable envelope 保持 pending，避免“消息已发但 scheduler 仍认为 target 未处理”；
- scheduler writer 的新输入是 `processed_scan_targets_by_account: account -> scheduled_target|null`，替代只有 accounts list 的模糊 `mark_scanned` 调用；多账户映射和 state 更新保持 account isolation/原子写。

因此 `09:50` catch-up 的 `09:40` 即使在 `10:01` 才完成，processed watermark 仍是 `09:40`，下一轮 `10:00` target 仍可执行；`15:30 -> 15:50` 同理。

### 6.3 SchedulerDecision 合同

保持 scheduler decision schema v1，并把新字段定义为 optional additive field：

```text
should_run_scan                 本次 scan target 是否未处理
is_notify_window_open           当前 scan target 是否也是 fixed report target
scheduled_scan_target_market    本次 scan target ISO；force 时可为空
scheduled_target_market         fixed report target ISO；非固定点为空
next_run_*                      下一 fixed/half-hour canonical scan target，不再只是下一 report target
```

`domain/domain/tool_boundary.py::normalize_scheduler_decision_payload()` 必须显式 allowlist `scheduled_scan_target_market`；`SchedulerDecisionView`、`scan_decision_by_account[account]["scheduler_decision"]` 和 account execution 全程保留该值。旧 producer/fixture 不提供字段时视为 `None`，不升级 schema version；force/manual 也为 `None`。`payload.should_notify` 继续是 `is_notify_window_open` 的兼容 alias，但新通知 flow 不再用它决定是否持久化成功快照。

必须有完整 round-trip 断言：raw scheduler payload -> tool-boundary normalize -> `SchedulerDecisionView` -> per-account `scan_decision` -> account execution target map，值不变且不会被静默丢弃。

### 6.4 force/manual 边界

本 PRD 定义的是 scheduled notification。为避免手工扫描伪装成固定批次或意外改变当日候选送达状态，本计划锁定：

- `trigger_kind != scheduled` 的扫描仍可更新 successful current；
- 不生成 fixed report，不写 `fixed_reports`；
- 不自动发送普通新增候选通知，不推进 `alerted_candidates`；
- 系统故障、恢复和业务回执继续按各自合同运行；
- 生产验证仍优先 `--no-send`。

这是对当前 force 可通知语义的有意收紧，必须更新 CLI help 和回归测试。若未来需要“手工发送当前完整报告”，应增加显式、需确认的独立 operator 命令，而不是复用 `--force` 的扫描副作用。

### 6.5 资源影响

这是同一 scheduler、同一 pipeline。真实 canonical 扫描从约 7-8 个固定点提高为固定点与半点的并集：

- HK 默认约 12 个有效扫描点/交易日/账户；
- US 默认约 14 个理论扫描点/交易日/账户，仍受现有 gates/DST 限制。

实施前必须用远端运行时指标确认：

- 单轮 pipeline P95 小于最短 20 分钟 scan-target 间隔（`09:40 -> 10:00`、`15:30 -> 15:50`）并留有安全余量；
- `15:50` scan-to-first-send-attempt 通常应在 `16:00` 前完成，才能保留同日 `16:00` delivery-only recovery slot；若容量证据不满足，必须作为 rollout gate 返回 CEO，而不是假设 final retry 一定存在；
- OpenD/行情限频可承受；
- 两账户不会因上一轮超时叠加；
- `tick-cron --timeout 600` 和 watchdog 能阻止重入。

若容量不满足，不能通过增加第二套轻扫描绕过；应先降低同轮数据成本，或由 CEO 决定是否减少半点检查。

## 7. 成功快照合同

### 7.1 什么可以推进 current

仅以下结果可写 immutable revision 和 current：

- account pipeline 确认完成；
- brief `status in {ready, degraded}`；
- 关键候选/持仓/资金来源足以区分“无候选”和“数据不可用”；
- account、market、market trading date、run_id 完整。

以下结果不得推进 current：

- pipeline subprocess failure；
- all structured decision sources unavailable；
- 关键上下文完全缺失导致 actionability blocked；
- malformed/schema mismatch；
- no-op account result（`ran_scan=false`）。

失败可以写 run-scoped audit/failure artifact，但不能成为 latest successful current。

### 7.2 Repository API 拆分

把当前一个 `prepare_daily_decision_brief()` 同时承担的职责拆为最小的两个边界：

```text
persist_daily_decision_brief_success(...)
  -> validate old current/revision
  -> allocate immutable revision
  -> return previous successful brief/current identities
  -> write revision + current atomically under account+market lock

prepare_daily_decision_brief_delivery(...)
  -> read v2 notification state
  -> choose fixed/candidate/none
  -> persist durable exact envelope in account delivery state
  -> persist matching run-scoped audit plan
  -> return stable delivery envelope
```

命名可在实现时保持 facade 兼容，但职责必须分开；失败 brief 不允许调用 success persistence。

### 7.3 brief 的 additive fields

为避免改写旧 immutable revision，保留 `daily_decision_brief.v1`，只增加向后兼容的可选字段并在 normalizer 中规范化：

```json
{
  "funds": {
    "as_of_utc": "...",
    "cash_total_by_currency": {"HKD": 480000, "USD": 18000},
    "option_opening_available_by_currency": {"HKD": 225000},
    "available": true,
    "reason": "ok"
  },
  "candidate_index": [
    {
      "identity": "candidate:v1:lx:HK:9992.HK:sell_put",
      "symbol": "9992.HK",
      "strategy_family": "sell_put",
      "representative": {"...": "bounded candidate view"},
      "contract_count": 4
    }
  ]
}
```

约束：

- `candidate_index` 每个提醒身份只有一项；representative 使用 canonical rank 后的首个合格合约；
- `candidate_index` 来自全部合格候选，不受报告展开上限 3 的影响；
- 原 `candidates/actions/capacity` 继续服务详细报告和审计；
- 旧 revision 没有 `funds/candidate_index` 时可读，显示不可用并可从现有 action/candidate best-effort 派生身份用于迁移；
- unknown 不能写成 0。

## 8. 资金构造方案

### 8.1 数据来源

只读取本轮 run-scoped state：

- `portfolio_context.json.cash_by_currency` -> 现金总额；
- `option_positions_context.json.cash_secured_total_by_ccy` 和 reliability flags -> 已占用担保；
- candidate row 的 canonical capacity -> 每候选最多整手数。

不调用 `query_cash_footer()`，因为它会重新进入 context/broker 查询路径。

### 8.2 计算口径

```text
cash_total_by_currency[ccy] = portfolio cash_by_currency[ccy]
option_opening_available_by_currency[ccy]
  = cash_total_by_currency[ccy] - reliable cash_secured_total_by_ccy[ccy]
```

- 多币种按原币种分别展示；不为展示目的强行折算合计；
- option positions 的担保使用不可靠时，opening available 整体标记 unavailable；
- 现金总额可靠、opening available 不可靠时，允许只显示现金总额，并明确后者暂不可用；
- 候选容量继续使用 `compute_sell_put_cash_capacity`、`compute_sell_call_share_capacity` 和 Combo canonical 输出；
- renderer 不把多个候选容量相加。

## 9. 候选身份与检测状态

### 9.1 身份

在 `domain/domain/daily_decision_brief.py` 增加 pure function，返回版本化、确定性、可审计的 canonical string：

```text
identity = "candidate:v1:<account>:<market>:<canonical_symbol>:<strategy_family>"
# example: candidate:v1:lx:HK:9992.HK:sell_put
```

- 禁止使用 Python 内置 `hash()` 或任何进程级随机 hash；identity 必须跨进程、跨重启稳定；
- account lowercase；market/symbol uppercase；strategy family canonical lowercase；各组件先通过现有 canonical validator，非法组件 fail closed，不进入 eligible index；
- identity 不包含 expiration、strike、contract symbol、rank、price、yield、Delta、capacity；
- 同一 symbol 的 Sell Put 与 Covered Call 是两个身份；
- Combo Yield 使用 `combo_yield` 策略族，不按腿拆分提醒身份；
- candidate delivery key 中的 identities digest 使用现有 canonical digest helper；若无可复用 helper，则用标准库 SHA-256 计算 canonical JSON `sorted(identity strings)`，不得依赖集合迭代顺序。

### 9.2 合格身份

只有以下 candidate index item 进入检测：

- 来自正式结构化、canonical accepted/labeled candidate；
- brief 是可靠成功且当前可行动；
- representative 合约字段完整且数据未过期；
- Sell Put / Covered Call / Combo Yield canonical capacity >= 1；
- 不是 rejected/raw-only/malformed/data-blocked。

### 9.3 为什么需要 pending，而不能只比较相邻快照

若 13:30 新候选发送失败，14:00 的 previous successful snapshot 已经包含该候选。只做：

```text
current - previous
```

会把它误判为“不再新增”，违反“provider 失败后续仍可重试”。

因此状态必须区分：

- `newly_detected`：本次相对上次成功快照新增；
- `pending_candidates`：已经检测到、仍当前有效、尚未确认送达；
- `alerted_candidates`：本交易日已确认送达。

成功扫描后的集合更新：

```text
newly_detected = current_ids - previous_success_ids   # 只用于审计/指标
pending = current_ids - alerted_ids                     # 当前有效且尚未确认送达
alerted 保持不变到市场交易日切换
```

- pending 直接由“当前有效且未 alerted”推导，避免 current 已写但 delivery state 写失败、或 force/manual 更新 current 后，后续 scheduled scan 永久漏掉候选；
- pending 未送达后消失：从 pending 移除；同日重新出现且从未确认，可再次进入 pending；
- alerted 后消失再出现：因为 alerted 仍在，不重复；
- 新交易日创建新日状态；首个成功扫描相对当日空基线，当前身份全部进入 pending，通常由 09:40 完整报告确认清除。

## 10. Delivery state v2

### 10.1 文件与锁

继续复用每账户、每市场一个 delivery 文件和现有 account+market lock：

```text
output_accounts/<account>/state/daily_decision_brief.<MARKET>.delivery.json
schema_version = daily_decision_brief_delivery.v2
```

不新增数据库或第二套 read model。

### 10.2 最小 schema

```json
{
  "schema_version": "daily_decision_brief_delivery.v2",
  "account": "lx",
  "market": "HK",
  "days": {
    "2026-07-21": {
      "fixed_reports": {
        "2026-07-21T14:00:00+08:00": {
          "status": "pending|ambiguous|confirmed|expired_unconfirmed",
          "delivery_kind": "fixed_report|fixed_failure",
          "source_kind": "successful_brief|scan_failure",
          "revision": "12 or null for scan_failure",
          "source_digest": "brief_digest or failure_artifact_digest",
          "delivery_key": "option-report:HK:2026-07-21:lx:2026-07-21T14:00:00+08:00",
          "rendered_message": "<exact Markdown payload>",
          "message_sha256": "...",
          "candidate_identities": ["candidate-..."],
          "first_prepared_at_utc": "...",
          "last_attempt_at_utc": "...",
          "confirmed_at_utc": "..."
        }
      },
      "pending_candidates": {
        "candidate-...": {
          "first_seen_revision": 11,
          "first_seen_at_utc": "..."
        }
      },
      "alerted_candidates": {
        "candidate-...": {
          "revision": 12,
          "brief_digest": "...",
          "delivery_key": "...",
          "confirmed_at_utc": "...",
          "via": "fixed_report|candidate_alert|legacy_delivery"
        }
      },
      "candidate_delivery": null
    }
  },
  "legacy_last_confirmation": null
}
```

`candidate_delivery` 与 successful fixed record 使用同样的 envelope 字段，另含整批 `candidate_identities`。同一账户+市场同一时刻只准备一条 candidate delivery；新身份可继续加入 `pending_candidates`。`fixed_failure` 不伪造 revision/brief digest，而是引用 run-scoped failure artifact 及其 digest。`rendered_message`、source、delivery key、identity set 和 message hash 共同构成可跨 tick 精确重放的 durable envelope；重试读取该 envelope，不依赖可能被 cleanup 的旧 `output_runs`。source/hash 校验失败时 fail closed。

按 `days[market_trading_date]` 隔离状态，避免新交易日覆盖仍需审计的前一日记录。交易日切换时：

- 前一日 pending fixed/candidate 不跨日自动发送，fixed 标记 `expired_unconfirmed`，candidate pending 留作审计后清空；
- 前一日 ambiguous envelope 保留并要求 operator 处理，不得在新交易日自动换 key 补发；
- 新交易日建立新的 pending/alerted 集合；
- v2 normalizer 至少保留当前日、存在 ambiguous 的日期和 `legacy_last_confirmation` 引用日期；历史清理不在本 work unit 实施。

### 10.3 Stable delivery keys

固定报告：

```text
option-report:<market>:<market-date>:<account>:<scheduled-target-market>
```

候选通知：

```text
option-candidates:<market>:<market-date>:<account>:<digest(sorted candidate identities))>
```

约束：

- fixed key 与 revision/content 无关，同一批次重试稳定；
- candidate key 对同一待送达身份集合稳定；
- definite failure 或从未调用 provider 时，pending 集合变化可以准备新 envelope；
- ambiguous send 后不得自动换 key 或改消息内容，只能按原 revision、原 identity set、原 message hash 重试/核验；新候选继续留在 pending，等待 ambiguous envelope 解决；
- 不允许为了“补发”生成新 key 绕过 provider idempotency。

### 10.4 Run-scoped delivery plan

每次真正准备消息时写：

```text
output_runs/<run_id>/accounts/<account>/state/daily_decision_brief_delivery_plan.<MARKET>.json
```

包含：kind、account、market/date、revision、brief digest、fixed target 或 candidate identities、delivery key、完整 rendered message、message hash、render context。它是本次准备/发送的 run-scoped audit copy；durable authority 是 account delivery state 中的 envelope。delivery-only retry 读取 durable envelope，不依赖旧 run artifact，也不创建新的 plan 或 revision。

confirmation 必须同时校验：

- send result transport key；
- persisted plan identity；
- successful delivery 的 immutable revision digest，或 fixed failure 的 run-scoped failure artifact digest；
- message hash；
- v2 current state 未被不兼容状态覆盖。

## 11. 扫描后统一通知状态机

### 11.1 输入分类

| ran_scan | pipeline reliable | fixed point | 行为 |
|---:|---:|---:|---|
| 否 | - | - | scanning no-op；不组装、不写 revision、不做 candidate diff、不准备新消息；可读取并处理一个已持久化 retryable envelope |
| 是 | 否 | 否 | 不推进 current；不做候选差异；普通监控静默 |
| 是 | 否 | 是 | 不推进 current；登记/准备 fixed failure report |
| 是 | 是 | 否 | 写成功快照；更新 pending；有 pending candidate 才发候选通知 |
| 是 | 是 | 是 | 写成功快照；登记 fixed report；只发完整报告 |

### 11.2 消息选择优先级

每个账户+市场每轮最多选择一条。scan tick 可按完整优先级准备/选择；no-scan tick 只能从已经持久化的 envelope 中选择：

1. 已存在 ambiguous envelope：只允许原 envelope 的幂等核验，或在现有 provider/idempotency 合同支持时精确重试；不得换 key；
2. 当前市场交易日最早未确认 fixed report（包括本轮 fixed target）：发送完整报告或 failure report；
3. 没有 fixed backlog 且存在 pending candidates：发送新增候选通知；
4. 否则静默。

no-scan tick 不得根据 `pending_candidates` 临时创建 candidate envelope，也不得把尚未持久化的 fixed due 转成消息；它只能重放 account delivery state 中已经持久化的 exact envelope。`--no-send` 不发布新的 retryable envelope，已有生产 envelope byte-for-byte 保留；quiet hours 保留已有 envelope，等允许发送时再处理。

当 provider 在同一市场交易日内长时间不可用导致多个 fixed point 排队时，按计划点从早到晚处理。消息继续显示原批次和实际数据截至时间；未解决 backlog 不跨交易日自动补发，跨日后转为可审计的 `expired_unconfirmed`，避免次日发送过期报告洪峰。

### 11.3 pending fixed failure 的后续成功

同一固定批次在 pipeline failure 后、failure message 尚未 confirmed 时，后续成功扫描按以下规则处理：

- 尚未调用 provider、quiet/no-send 或 provider 明确拒绝：可把 pending `fixed_failure` 升级为引用最新 successful revision 的 `fixed_report`，delivery key 保持该固定批次不变；
- failure message 已 confirmed：该批次已完成，不再补发 full report；
- failure message ambiguous：冻结原 failure envelope，只允许原 key/原 message 的精确重试或 operator 核验，不得自动改成 full report；
- 升级只改变 unconfirmed envelope，不修改 successful current 之外的业务事实。

这样 14:00 扫描失败但 failure envelope 未送达时，14:10/14:20 no-scan tick 只能精确重试原失败说明；若到 14:30 仍未确认且本轮扫描恢复成功，才可升级为标记“14:00 批次、数据截至 14:30”的完整报告。若 14:00 失败说明已经送达，则不重复占用该批次。

### 11.4 四格矩阵

在没有 backlog/ambiguous 的正常状态下：

| fixed point | pending new candidates | 输出 |
|---:|---:|---|
| 否 | 否 | 静默，只更新快照 |
| 是 | 否 | 完整报告 |
| 否 | 是 | 新增候选通知 |
| 是 | 是 | 只发完整报告 |

### 11.5 确认更新

固定完整报告 confirmed：

- fixed target -> confirmed；
- delivery plan 中的全部 current eligible candidate identities -> alerted；
- 对应 pending candidates 删除。

这里确认的是本次完整报告代表的整份候选集合，而不只是在 Markdown 中展开的前三项，保证“fixed + new 只发一条”不会在下一非固定点补发被折叠的同轮候选。

候选通知 confirmed：

- delivery plan 中全部 candidate identities -> alerted；
- 全部从 pending 删除；
- 即使 Markdown 只展开 3 个，整批都视为送达，剩余项通过数量提示和查询入口承接。

fixed failure confirmed：

- fixed target -> confirmed failure report；
- 不修改 candidate pending/alerted；
- 不推进 successful current。

以下均不确认：

- `--no-send`；
- quiet hours；
- provider definite failure；
- ambiguous send；
- local confirmation write failure。

ambiguous send 只记录 attempt/freeze envelope，不写 confirmed fixed/alerted candidate。

### 11.6 Delivery-only retry 合同

非 scan target 的 tick 复用同一个 `tick_notification_flow` 和 `scheduled_notification` sender：

1. `multi_account_tick` 在 no-scan 决策下不再无条件 early return，而是用配置中的 account IDs、scheduler markets 和 `delivery_only=true` 调用同一个 notification flow；
2. repository 在 account+market lock 下读取最优先的 persisted pending/ambiguous exact envelope；
3. flow 校验 envelope source digest、revision/failure artifact、delivery key 和 rendered message hash；
4. 校验通过才按原 route 处理；provider definite failure 保持 pending，ambiguous 继续冻结，confirmed 才推进 fixed/candidate 状态；
5. 整个分支不得创建 pipeline workspace、执行 prefetch/account pipeline，也不得调用 broker、brief assembler、candidate detector 或 current persistence；
6. 不改变 revision、candidate identities、delivery key、rendered message、message hash 或 source；只追加 attempt/audit 时间；
7. 只处理当前 scheduler market、当前 market trading date、仍在既有发送窗口内的 envelope；`--market-config all` 继续按现有多市场 fail-closed 合同不主动 dispatch；
8. 没有 retryable envelope 时按原 no-scan 语义完成 skipped；`15:50` full-report definite failure 可在 `16:00` 现有 timer 唤醒中精确重试；这不是第二套扫描频率或第二 sender。

## 12. Renderer 与用户消息

### 12.1 Renderer API

在现有 renderer 内增加窄入口，不建 renderer framework：

```text
render_fixed_report(brief, context)
render_candidate_alert(brief, candidate_identities, context)
render_fixed_failure(failure, context)
render_query_brief(brief, context)
```

共享现有候选、持仓、时间、容量格式 helper。

### 12.2 固定完整报告

固定结构：

1. 标题：账户 + 市场期权监控；
2. 原计划批次；
3. 数据截至（市场当地 + 北京）；
4. 当前候选；
5. 持仓；
6. 资金；
7. 必要提醒。

无候选也保留持仓和资金，不退化为心跳。

### 12.3 新增候选通知

- 标题明确“新增候选”；
- 显示发现于本轮 scan target 和 data as-of；
- identity 对应的 representative contract；
- 最多展开 3 个；其余显示数量并提示查询；
- 显示账户资金；
- 不显示普通排序/指标变化摘要。

### 12.4 固定点失败

只显示：批次失败、可靠结果未形成、下一 canonical scan 会继续；不得渲染旧 current 为“本轮候选”，也不得写“本轮无候选”。

### 12.5 查询

- 标题与固定报告复用主要投影；
- context 改为“当前查询 · 查询时间”；
- 显示 data as-of、今日最新/已过期、今日扫描暂不可用；
- 旧成功 current 仍可读，但明确过期；
- Markdown 不显示 revision；精确 revision 仅保留在 CLI `--json`/operator contract。

## 13. 查询入口方案

### 13.1 复用现有能力

保留：

- `daily_decision_brief_read`；
- `./om daily-brief latest`；
- repository current/revision read。

不新增 query service 或查询快照。

### 13.2 聚合读取

`read_daily_brief_view()` 扩展为：

- account+market 都给定：单份；
- 只给 account：该账户全部启用市场；
- 只给 market：该市场全部启用账户；
- 都不提供：全部启用账户和市场。

启用范围来自 canonical runtime config，不从历史 state 目录猜测，避免返回已停用账户的陈旧文件。

聚合结果：

- structured output 按 sections 返回；
- Markdown 按 `account -> market` 分节；
- 每节资金独立，不跨账户/市场/币种合并；
- 个别 section unavailable 不阻断其他 section，但整体 warning 明确部分缺失。

### 13.3 自然语言路由

不在业务代码硬编码 `期权监控` 等中文字符串。通过：

- tool description；
- optional account/market schema；
- Copilot 场景测试；
- read-only capability metadata；

让现有 Copilot 选择该工具。

## 14. v1 delivery pointer 迁移

### 14.1 迁移原则

生产 v1 pointer 不能由 normal tick 隐式猜测迁移。提供显式、dry-run 默认、锁内原子迁移：

1. 备份原 pointer；
2. 严格读取 account/market/date/revision/delivery key/confirmed time；
3. 读取其指向的 immutable revision；
4. 验证 identity 和 revision；
5. 仅依据 immutable revision 重新计算 `brief_digest`；
6. 写 v2；
7. 重新读取并校验；
8. 不手动触发 tick，等待下一正常 scan target。

### 14.2 v1 -> v2 映射

v1 能可靠保留：

```text
legacy_last_confirmation = {
  market_trading_date,
  revision,
  delivery_kind,
  delivery_key,
  recomputed brief_digest,
  confirmed_at_utc
}
```

- v1 没有 fixed scheduled target，不允许猜测某个 fixed_reports key 已确认；
- 仅迁移有证据证明已送达的候选身份：v1 `delivery_kind=full` 可从指向 revision 派生整份候选身份；v1 `delta` 只迁移 persisted diff 中可验证的 `candidate_added` 身份；diff 缺失或无法证明时不猜测、不写 alerted；
- 写入的 legacy 候选记录使用 `via=legacy_delivery`；保守少迁移可能产生一次后续提醒，但不得通过过度迁移静默压掉未确认候选；
- 迁移后下一真实 fixed target 正常发送；
- 如果 revision 缺失、digest/identity 不一致或 state malformed，fail closed，不覆盖原文件。

### 14.3 迁移工具边界

优先在现有 `./om daily-brief` 下增加：

```text
./om daily-brief delivery-inspect --account lx --market HK
./om daily-brief delivery-migrate --account lx --market HK --dry-run
./om daily-brief delivery-migrate --account lx --market HK --confirm
```

- `--dry-run` 默认；
- `--confirm` 前输出 source/backup/target 摘要；
- 不扫描、不发送、不写 current/revision；
- 先迁移 `lx`、`sy` HK，再按实际启用市场处理 US；
- 生产执行需单独批准。

## 15. 实施切片

### Slice 0：Characterization 与容量基线

- 固化当前 scheduler、no-op、pipeline failure、repository pointer 和 query 测试；
- 采集远端单轮 pipeline 时长/OpenD 请求量，只读评估固定点 + 半点容量；
- 不改行为。

完成标准：明确最短 20 分钟目标间隔可运行，或把容量风险返回 CEO 决策。

### Slice 1：Scheduler 解耦

修改：

- `scan_scheduler.py` 的 processed-target watermark；
- `domain/domain/tool_boundary.py` 与 scheduler DTO/view/propagation；
- `multi_tick_scheduler.py`、`tick_account_execution.py` 的 per-account target round-trip/outcome map，并移除提前 mark 与静默吞错；
- runtime status additive 展示和 scheduler focused tests。

完成标准：scheduled target identity 能完整到达 account outcome，但 Slice 1 不在 durable notification outcome 之前提交 watermark；09:40 catch-up 晚完成不吞 10:00，15:30 晚完成不吞 15:50；legacy state、午休/非交易日/gate/account dedupe 不回归。

### Slice 2：成功快照、funds 与 candidate index

修改：

- domain brief additive fields/identity；
- brief service；
- service/domain/renderer characterization tests。

完成标准：每次可靠成功都能形成结构化 snapshot；资金按币种且 unknown 非 0；全部候选身份可检测；失败尚不推进 current。

### Slice 3：Repository v2 与迁移

修改：

- repository success persistence；
- delivery v2 normalize/read/write；
- durable exact envelope + 正确 `output_runs/<run_id>/accounts/<account>/state/` 路径下的 run-scoped delivery audit plan；
- inspect/migrate CLI；
- repository/migration tests。

完成标准：v1 dry-run、backup、recomputed digest、v2 round-trip、rollback backup 均可验证；normal runtime 对 malformed/mixed state fail closed。

### Slice 4：统一通知决策和确认

修改：

- domain pure decision；
- `multi_account_tick.py` 的 no-scan delivery-only 入口和 scan target commit callback；
- `tick_notification_flow.py` 的 durable prepare -> target commit -> send 顺序，以及 delivery-only 窄分支；
- repository retryable-envelope read；
- 必要时只窄扩 `scheduled_notification.py` result 透传；
- notification-flow tests。

完成标准：四格矩阵、fixed backlog、跨 tick exact retry、ambiguous freeze、pipeline failure、no-send/quiet/provider failure 全部通过；scan 路径严格 durable prepare -> watermark commit -> send，delivery-only 分支零 broker/pipeline/revision 写入。

### Slice 5：用户投影与查询

修改：

- renderer；
- agent tool；
- CLI latest；
- Copilot scenario tests；
- 用户文档。

完成标准：固定/候选/失败/查询 Markdown 可读，内部字段不泄漏，聚合范围正确且查询零写入。

### Slice 6：全链路回归、发布与 canary

- focused tests + broader tick/config/agent tests；
- `--no-send` 四种矩阵；
- delivery migration dry-run；
- 单账户/单市场真实发送 canary（单独批准）；
- 等待正常调度点观察，不手动 tick 制造结果。

## 16. 测试矩阵

### 16.1 Scheduler

- 09:35：不扫描，next scan 09:40；
- 09:40：扫描 + fixed；
- 09:50：若 09:40 已处理则 scheduler no-op；若 09:40 tick 缺失则按现有 catch-up grace 补跑 09:40 target；
- 10:00：扫描 + fixed；
- 10:10 / 10:20：scheduler no-op；
- 10:30：扫描 + 非 fixed；
- HK 12:00-12:50：不扫描；13:00 恢复且 fixed；
- 15:50：扫描 + fixed；16:00 不扫描，但可 delivery-only retry；既有发送窗口结束后停止普通 delivery；
- non-trading day：不扫描/不准备新的正常报告；
- per-account `last_processed_scan_target_utc_by_account` 去重，`last_run_utc_by_account` 仅观测；
- 09:50 catch-up 09:40 且 10:01 完成，10:00 target 仍未处理并可运行；
- 15:40 catch-up 15:30 且 15:51 完成，15:50 target 仍未处理并可运行；
- lx/sy processed target 独立；单账户晚完成不推进另一账户水位；
- legacy state 缺少 processed-target 时先兼容 `last_run`，首个新版 target 后切换；
- force/manual target 为 `None` 且不推进 processed-target；
- raw payload -> tool boundary -> SchedulerDecisionView -> per-account scan decision -> account execution target map 完整 round-trip；
- successful/failure durable outcome 尚未落盘时不推进 processed-target；
- processed-target state write 失败时 tick fail closed、provider 不调用、pending envelope 保留且有 audit；
- watermark commit 成功后进程在 send 前崩溃，下一 no-scan tick 可从 durable envelope 精确恢复；
- DST/gate/catch-up；
- pipeline 超时无重入。

### 16.2 Snapshot/service

- ready/degraded 推进 current；
- pipeline failed/blocked/no-op 不推进 current；
- current 指向 immutable revision 且 digest 一致；
- cash total 多币种；
- secured usage 不可靠 -> opening funds unavailable；
- unknown 不输出 0；
- contract change 不改变 candidate identity；
- strategy family change 产生新 identity；
- 相同输入在独立 Python 进程和不同 `PYTHONHASHSEED` 下产生完全相同 identity/delivery digest；
- identity normalization、排序和 repository round-trip 稳定；
- force/manual 已更新 current 但未 alerted 的候选，在下一 scheduled success 仍进入 pending；
- current 写入后 delivery state 写失败，下一 scheduled success 仍从 `current_ids - alerted_ids` 恢复 pending；
- top-N 之外的 identity 仍进入 candidate index。

### 16.3 通知矩阵与重试

- fixed/no-new -> full；
- fixed/new -> full only；
- non-fixed/new -> candidate only；
- non-fixed/no-new -> quiet；
- 14:00 provider definite failure，14:10 no-scan tick 使用相同 fixed envelope/message 精确重试；
- 15:50 full-report definite failure，16:00 delivery-only retry；
- candidate provider definite failure，下一 no-scan tick 精确重试原 candidate envelope；后续 scan 仍保留未确认身份；
- 顶层 no-scan tick 确实进入 delivery-only flow；无 envelope 时 skipped，有 envelope 时才调用 sender；
- delivery-only 只读当前 scheduler market/date/window；multi-market `all` 继续 fail closed 不 dispatch；
- delivery-only retry 不创建 pipeline workspace，不运行 prefetch/pipeline/broker/assembler，不新增 revision/current/diff/plan；
- delivery-only retry 不改变 source revision、candidate set、delivery key、rendered message 或 message hash；
- no-send 不确认、不发布新的 auto-retry envelope；已有生产 envelope 不变；quiet 不确认且已有 envelope 保留；
- ambiguous send 不旋转 key、不改变 envelope，只走核验或 provider 幂等合同允许的 exact retry；
- fixed full confirmed 后全部同轮 candidate identities 标记 alerted；
- >3 candidate alert 只展开 3，但整批 confirmed；
- 已确认候选同日消失再出现不重复；
- 新交易日重建集合；
- 多账户/多市场隔离；
- 一个 tick 每账户市场最多一条普通消息。

### 16.4 Failure

- fixed pipeline failure -> explicit failure report；
- non-fixed pipeline failure -> ordinary quiet；
- failure 不覆盖 successful current；
- failure 不参与 candidate diff；
- failure report provider 未确认 -> fixed target 未确认；
- OpenD/coverage/provider/pipeline 在 audit 中可区分。

### 16.5 Query

- 后续静默成功扫描更新 current，查询读到新 snapshot；
- 查询不读取 last delivered pointer；
- stale previous-day current 明确标过期；
- aggregate sections 隔离；
- latest Markdown 不显示 revision；
- exact revision JSON/CLI 仍可用于运维；
- query 前后 delivery state byte-for-byte 不变。

### 16.6 迁移

- valid v1 -> v2；
- digest 只由 immutable revision 重算；
- missing revision/state mismatch -> fail closed；
- backup 存在且可恢复；
- lx/sy/US/HK 互不覆盖；
- migration 不写 current/revision、不触发 tick、不发送。

## 17. 建议验证命令

实现阶段按 slice 运行 focused checks，最终至少：

```bash
./.venv/bin/python -m pytest \
  tests/test_scan_scheduler_notify_semantics.py \
  tests/test_scan_scheduler_scan_per_account.py \
  tests/test_multi_tick_scheduler_application.py

./.venv/bin/python -m pytest \
  tests/test_daily_decision_brief_domain.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_repository.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_daily_decision_brief_scenarios.py \
  tests/test_daily_decision_brief_agent_tool.py \
  tests/test_daily_decision_brief_cli.py

./.venv/bin/python -m pytest tests/test_multi_tick_*.py tests/test_unified_tick_entrypoint.py
./.venv/bin/python -m pytest tests/test_layered_config.py tests/test_validate_config_notifications.py

./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example

git diff --check
```

禁止用真实 provider 发送替代自动化测试。

## 18. Rollout

### 18.1 发布前

1. 确认远端实际 pipeline P95/资源容量可承受固定点 + 半点扫描，尤其是 20 分钟最短间隔；
2. 本地/隔离 runtime 完成 no-send 四格矩阵；
3. 对 production v1 pointers 做 read-only inspect 和迁移 dry-run；
4. 明确 Compact 旧心跳/旧 Daily Brief material lifecycle 不会并行发送。

### 18.2 生产切换

生产配置、服务、状态迁移和真实发送均需单独批准。建议顺序：

1. 选择固定报告点之间的维护窗口；
2. 备份 `lx`、`sy` 各启用市场 delivery pointer；
3. 部署支持 v2 的版本；
4. 显式迁移并校验 delivery state；
5. 恢复正常 scheduler；
6. 不手动触发 tick，等待下一正常 scan target；
7. 先观察单账户/单市场 canary，再扩大。

### 18.3 观察指标

- canonical scan duration/P95/timeout/重入抑制；
- fixed report due/confirmed/backlog；
- candidate detected/pending/confirmed/duplicate-suppressed；
- scan-complete 到 send-start 延时；
- query snapshot age；
- pipeline/provider/OpenD/coverage failure 分类；
- current revision 是否只由 reliable success 推进；
- `15:50` scan-complete/send-attempt 是否在 `16:00` recovery slot 前完成；
- per-account processed scheduled target 与 actual completion time 是否一致可审计；
- delivery-only retry attempt/confirmed/ambiguous、source envelope 校验失败。

## 19. Rollback

### 19.1 代码回滚

- 停止新版本普通通知路径；
- 保留 immutable revisions、run-scoped plans 和 audit；
- 不删除 v2 state；
- 若必须运行旧代码，先在停调度条件下恢复迁移前 v1 backup，再校验 pointer；旧代码不能直接读取 v2。

### 19.2 行为回退

最小紧急回退使用现有 `notifications.daily_brief.enabled=false`，避免新旧普通通知并行；系统故障/业务回执按独立合同保留。该配置变更需生产批准。

### 19.3 数据恢复

- successful current 可从 immutable revision 重建；
- confirmed 状态只能从 validated v2/backup/audit 恢复，不根据聊天记录猜测；
- ambiguous send 不自动标 confirmed，也不生成新 delivery key 补发。

## 20. Residual risks / execution gate

### 必须在 execute 前确认

1. 已确认产品节奏：现有 10 分钟 timer 保持不变，真实 pipeline 仅在固定报告点和有效半点执行。实施前只需验证远端容量，不再把扫描频率作为产品待决项。
2. 候选身份正式采用 `账户 + 市场 + 标的 + 策略族`。
3. 默认“期权监控”聚合全部启用账户和市场。

### 已接受的 residual risks

- production scheduler 正常按单市场运行；显式 `--market-config all` 继续只生成各市场快照并 fail closed 不主动发送，避免一次 tick 把两个市场合并。若要支持 multi-market 主动 dispatch，应另做路由 work unit。
- 固定点与半点的每次成功扫描都会增加 immutable revision；本 work unit 先监控每日 revision 数量和磁盘增量，不顺带设计 retention。超过运行预算后再由现有 cleanup ownership 增加“保留 current、delivery 引用和审计所需 revision”的安全清理策略。
- provider 同日长时间故障仍会形成 fixed backlog；现有 10 分钟唤醒会在发送允许窗口内精确重试持久化 envelope，但不会额外扫描或刷新内容。窗口结束后仍未确认的批次跨交易日转为 `expired_unconfirmed` 而非次日补发，需通过审计/告警暴露。
- v1 pointer 无固定计划点，迁移不能诚实恢复历史 fixed confirmation；只保留 legacy confirmation 和可验证的候选送达证据。
- 旧 revision 缺少完整 candidate index，迁移只能从当时保存的 actions/candidates 派生已送达身份，不能虚构当时未持久化的候选。
- 半点检查仍不是实时行情；正常连续交易时段内通常最晚在下一个半点或 fixed target 发现，跨 HK 午休则等待下一个有效目标点，再加 pipeline/send latency。

## 21. 完成定义

只有以下全部成立，work unit 才可 close：

- PRD A-L 验收场景全部自动化或有受控 canary 证据；
- scheduler 单一扫描合同和资源容量验证通过；
- pipeline failure 不再覆盖 successful current；
- delivery v2 迁移、备份、校验和 rollback 演练通过；
- no-send/quiet/provider failure/ambiguous confirmation 不错误推进状态；delivery-only retry 不产生扫描或新 revision；
- 用户 Markdown 达到固定可预期、简单、可读，不泄漏内部生命周期；
- focused + broader quality gates 通过；
- 生产观察至少覆盖一个正常 fixed point、一个半点静默 scan、一个 timer scanning no-op、一个查询、一个受控新增候选场景，以及一次受控 delivery-only retry。
