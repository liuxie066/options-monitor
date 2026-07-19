# Gateflow Goal Confirmation — Daily Decision Brief 与后续增量监控

- **Gate**: goal confirmation
- **Work unit**: `daily-decision-brief`
- **Date**: 2026-07-19
- **Branch**: `codex/v1.3.0-daily-decision-brief`
- **Status**: confirmed by user on 2026-07-19
- **Artifact path**: `docs/gateflow/daily-decision-brief-goal-confirmation-20260719.md`

## 目标

为每个市场交易日、每个账号建立一份确定性的 Daily Decision Brief，作为当天后续监控的唯一决策基线：

1. 首轮成功扫描生成并持久化完整日报；
2. 后续 scheduler run 使用同一份结构化 read model 重算并比较；
3. 无重大变化时静默，有重大变化时只发送相对日报的增量提醒；
4. 市场收盘或整日休市时把行动能力降级为 `planning_only`；
5. 数据或关键运行依赖不完整时输出 `blocked`，不得把旧候选包装成当前可执行建议；
6. Feishu/WeChat 主动通知、CLI 和 Copilot/Tool Gateway 读取同一个 canonical Daily Brief read model；
7. 全程 advisory-only，不自动下单、不从消息点击推断成交。

## 动机

当前 tick 已能在开盘后和后续运行点扫描候选、计算现金、生成 Close Advice 并按账号通知，但每一轮通知仍是独立文本：系统没有“当天基线”、稳定 action identity、material-change diff 或日报/监控生命周期。因此用户会看到候选和平仓摘要，却不能稳定回答“这相对今天早上的计划发生了什么变化”。

日报 work unit 成立，因为它补的是现有扫描与通知之间缺失的决策生命周期，而不是新增一套策略算法。

## 直接代码证据

1. `src/application/scan_scheduler.py` 与 `src/application/config_defaults.py`
   - US/HK 都已有 `start_plus_min=10`、整点监控和 `end_minus_min=10` 运行点；首轮目标天然是市场时间 09:40。
   - scheduler 已维护 `last_run_utc_by_account` / `last_notify_utc_by_account`，可复用交易日与账号级去重语义。

2. `src/application/multi_tick/misc.py::AccountResult`
   - 只有 `account / ran_scan / should_notify / decision_reason / notification_text`；没有结构化日报、actionability、action ID、revision 或有效期。

3. `src/application/account_run.py`
   - 每个账号已产出 candidate CSV、`candidate_filter_trace.jsonl`、`close_advice.csv` / `.txt`、持仓 context 和通知文本。
   - 日报应从这些结构化运行产物及 canonical services 组装，不应解析最终 Markdown 来反推业务事实。

4. `src/application/scheduled_notification.py` 与 `src/application/tick_notification_flow.py`
   - 当前每个 notify window 都重新准备完整账号消息并发送；没有区分“首份完整日报”和“后续增量”。
   - delivery routing、quiet hours、retry、delivery confirmation 和 provider idempotency 已存在，应复用而不是建立第二套发送栈。

5. `domain/domain/engine/candidate_engine.py` 与 `domain/domain/close_advice.py`
   - 候选过滤/排序和持仓平仓分层已有 canonical authority。
   - 日报只做风险优先的行动编排、解释与 diff，不增加隐藏的 LLM 排名或平行 optimizer。

6. `domain/storage/repositories/state_repo.py`
   - 已有 shared/account current read-model 写入和通用 idempotency record，可承载日报 current snapshot、历史 revision 与发送去重，无需引入新数据库。

7. `src/application/multi_tick/assistant_perception_event.py`、`notification_perception_read.py` 和 Agent Tool registry
   - 已有安全的主动通知审计/read-only tool 模式；日报 read tool 可沿用相同 registry 和只读边界。

8. `src/application/tick_scheduler_context.py`
   - 已有 trading-day guard。整日休市应是正常 `planning_only/market_closed`，而不是系统故障或旧行情 LIVE 日报。

## 期望产品行为

### 首轮完整日报

- 粒度：`market + market_trading_date + account`。
- 首个完整成功 scheduled scan 生成 `revision=0` 的 Daily Brief。
- 内容包括：状态/有效期、一句话策略、P0/P1/P2 行动、已有仓位、资金与覆盖容量、Sell Put/Covered Call/Combo Yield 候选、拒绝摘要、数据缺口和事件。
- 同一市场交易日、同一账号只发送一次完整日报；重试不得重复发送。

### 后续监控

- 后续 scheduled scan 生成新 revision，并与上一 revision 比较。
- 只有 material change 才发送增量：新增/升级 P0、日报主行动失效、blocked/recovered、成交或持仓变化导致容量变化、跨越明确阈值的新候选。
- 价格小波动、同 tier 排名互换、无决策影响的现金变化和未变化 heartbeat 均静默。
- 增量消息必须引用日报基线时间/ID，并说明“什么变化、为什么、影响什么行动、现在是什么状态”。

### Actionability

- `live_actionable`: 数据完整且市场交易中。
- `planning_only`: 收盘、整日休市或报价超过有效期；只能用于下一交易时段计划。
- `blocked`: 关键行情、持仓、现金、事件或运行依赖不可用；只陈述已知事实和阻塞原因。

### 账号和策略边界

- `lx`、`sy` 独立计算、独立持久化、独立发送，不共享现金、股份或行动容量。
- Existing position risk / Close Advice 优先于新开仓。
- Sell Put、Covered Call、Combo Yield 使用各自现有 canonical evidence；Combo Yield 必须保持 group/leg identity，不伪造组合。
- Candidate ranking 委托 `candidate_engine.rank_candidate_rows()`；日报不建立第二套 ranking。

## 成功信号

1. 首个成功 09:40 附近 scheduled scan 为每个成功账号写入可验证的 `daily_decision_brief.v1` current snapshot 和 run-scoped artifact。
2. 完整日报有稳定 identity、market trading date、account、revision、data as-of、valid-until 和 actionability。
3. 同一交易日重复 tick 不重复发送完整日报；发送成功/失败/重试均有 idempotency 和审计证据。
4. 后续无 material change 的 run 静默；有变化时只发送结构化 diff 对应的增量消息。
5. blocker → recovery、LIVE → planning、候选失效、P0 升级、容量变化均有 scenario tests。
6. 非交易日不发送伪 LIVE 日报；旧行情不能继续作为当前建议。
7. CLI 与只读 Agent Tool 能读取 latest/day-specific brief 和 revisions，Copilot 无需解析飞书文本。
8. Markdown 输出保持账号隔离、中文、紧凑并显式展示缺失数据。
9. 现有普通 notification、candidate ranking、Close Advice、trade intake 和 scheduler public behavior 不回归。
10. focused tests、tick/notification regressions、Agent contract tests、architecture/dependency checks 和完整发布级测试通过。

## Scope boundary

### 本 work unit 包含

- Daily Brief domain contract、action identity、actionability、revision 和 material-change diff；
- 从当前 run 的结构化候选、trace、Close Advice、现金和持仓事实组装账号级 brief；
- shared/account/run-scoped JSON 持久化与每日发送去重；
- 首份完整日报、blocked brief、recovery update 和后续增量消息渲染；
- 集成现有 tick notification flow 和现有 provider route；
- CLI/read-only Agent Tool read surface；
- 配置 schema/default/example、操作文档和场景测试。

### 本 work unit 不包含

- 自动下单、订单草稿、消息按钮执行或点击即成交；
- 新的候选评分算法、LLM re-ranking、平行 optimizer 或自动配置调参；
- 根据通知是否被阅读推断 action 完成；成交事实仍以 canonical trade events / projection 为准；
- 完整绩效日报、月度收益归因或新的新闻聚合系统；
- 修改真实生产 config、发送真实通知、部署或升级远端服务；
- 合并 PR、发布 `v1.3.0`、打 tag 或远端上线；这些在 Draft PR 通过后另行授权；
- 为满足 09:45 精确 SLA 而新增 daemon、队列或第二套 scheduler。

## 最小实现原则

- 复用现有 scheduler、run artifacts、state repository、delivery route 和 canonical strategy services。
- 不从 Markdown 反解析事实；Markdown 只是同一 read model 的 renderer。
- 不新增数据库或任务管理系统。
- 新功能默认 opt-in；本 PR 不改变生产通知行为，生产启用需后续显式配置/部署确认。

## Resolved open question

现有 systemd/launchd timer 每 10 分钟唤醒，业务运行点为 09:40、10:00、11:00……，无法天然在 09:45 再判断一次。

建议 v1.3.0 采用最小且可验证的语义：

- 09:40 scheduled run 成功：立即发送完整日报；
- 09:40 run 能明确判定关键依赖失败：立即发送 blocked brief；
- 09:40 run 没有形成可发送结果或进程级失败：10:00 下一次 scheduled run 再发送 blocked/recovery brief；
- 不修改 timer cadence，也不承诺 09:45 精确 SLA。

若必须严格保证 09:45，需扩大 scope 修改 timer 或加入受控重试机制，会显著增加生产调度和重复发送风险。

## Residual risks classification at this gate

| Risk / area | Classification |
|---|---|
| 09:45 精确 blocker SLA 与现有 10 分钟 timer 不一致 | fixed at goal gate；用户接受 09:40/10:00 语义 |
| 生产启用后消息长度和真实噪声 | covered by current work unit 的 renderer 限额、scenario tests 和 default-off；真实 canary 属于后续 deployment gate |
| 历史 run 缺少新 schema | current work unit 只保证新 run；历史迁移 assigned to later work unit，read tool 对缺失状态显式返回 unavailable |
| 成交与日报 action 的一一任务完成映射 | assigned to later work unit；本轮只基于 canonical position/trade facts 重算，不建立任务管理系统 |

## Confirmation requested

请确认：

1. 接受上述目标、成功信号、scope 与 non-goals；
2. 接受 09:40 成功即发、明确失败即 blocked、进程级失败最晚由 10:00 下一轮恢复/阻塞处理，不承诺精确 09:45；
3. 接受新功能 default-off，Draft PR 完成后再单独决定生产配置启用、发布和远端升级。


## Gate decision

- **Decision**: pass
- **User confirmation**: 2026-07-19；接受目标/scope/non-goals、09:40/10:00 时间语义以及 default-off 边界。
- **Validation**: branch 为 `codex/v1.3.0-daily-decision-brief`，base 为 release `v1.2.420` / `5aecee73b3e4ace39b0c38ce9a98d18180020d1b`。
- **Current gate**: plan
- **Next entry point**: 编写 code-generation-ready plan artifact。
