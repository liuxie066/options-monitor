# Daily Decision Brief 用户通知投影优化计划

- **Work unit**: `daily-decision-notification-projection`
- **Date**: 2026-07-20
- **Revision**: 6
- **Status**: final-a-plus-product-plan-with-strategy-attribution
- **Implementation baseline**: `origin/main=bee60f201e1b` (`v1.3.4`)
- **Parent capability**: `daily-decision-brief`
- **Motivation source**: 2026-07-20 用户对“每日决策简报 / 日内决策恢复”可读性、术语、内部字段、时间格式和实际发送节奏的反馈

## 1. 决策摘要

本 work unit **不回滚 Daily Brief 后台生命周期**，也不恢复 legacy 文本链路为通知权威。

锁定为以下方案：

1. 保留 Daily Brief 的 structured snapshot、revision、material diff、blocked/recovered、last-confirmed delivery pointer、retry/idempotency、CLI/Agent read model 和 audit artifacts。
2. 锁定 opening candidate 与 execution action 的用户语义：canonical v1 继续保留 `open_candidate / open_combo_yield` action 作为稳定 lifecycle carrier，避免同日兼容和 rollback 风险；但 diff 和用户投影必须把它们分类为“候选”，永不称为执行行动。
3. 现有 `daily_decision_brief_renderer.py` 改为**结构化、allowlist、紧凑型用户投影**；旧 `notify_symbols.py` 仅作为视觉参考，不被 Daily Brief 调用，不做 Markdown/text round-trip。
4. backend diff 继续决定是否通知；initial/material/recovered 通知正文均是当前完整 compact snapshot，material/recovered 只在顶部增加一行“较上一轮”摘要。整体 blocked 使用短告警。
5. 内部 delivery 仍使用现有 `full / delta / none`、delivery key 和 confirmation pointer；“delta”描述触发事件，不再意味着用户只能收到 revision-only 文本。
6. 用户消息只显示人类可理解的业务字段；内部 ID、raw enum、raw ISO、broker contract code、revision、拒绝统计不进入展示 DTO。
7. 采用用户确认的 **A+ delivery policy**：保留既有 US 调度（美东 09:40 首轮，之后 10:00 / 11:00 / 12:00 / 13:00…整点检查），09:40 首次成功固定发送完整简报，后续整点仅在 material change 时发送。消息明确区分“检查批次”和“数据截至”，不修改 `run_points`。

## 2. 用户问题与根因

### 2.1 用户问题

当前消息同时存在以下问题：

- 把审计结构直接展示给用户，信息层次过重；
- `LIVE / READY / revision / action_added` 等系统术语无法指导决策；
- `position_lot_id / strategy_group_id / leg_role` 泄漏内部身份；
- `2026-07-20T20:00:00+00:00` 是机器时间而非用户时间；
- `US.TCOM260821P40000` 是券商编码而非可读合约；
- opening candidates 同时进入 `actions` 与 `candidates`，恢复消息把两个 strike 说成两个“新增行动”；
- delta/recovery 只给变化，不给当前完整上下文，必须回看旧消息才能理解；
- US 调度本身已经是用户需要的 09:40 首轮 + 后续整点；混乱来自消息只展示实际数据时间、没有展示所属检查批次，且 material-only 会让某些整点没有消息。

### 2.2 直接根因

- `src/application/daily_decision_brief_service.py:117-158` 将 selected candidates 同时放入 `candidate_payloads` 和 `actions`。
- `domain/domain/daily_decision_brief.py:194-280` 只对 `actions` 做 material lifecycle diff，因此 opening candidates 被迫借用 action 语义。
- `src/application/daily_decision_brief_renderer.py` 直接渲染 canonical mapping，并展示 revision、raw timestamps、raw enums、identity suffix、候选证据和拒绝诊断。
- `src/application/daily_decision_brief_repository.py:159-185` 正确保留 last-confirmed baseline 和 `full/delta/none` envelope；问题在 payload projection，不在 repository lifecycle。
- `src/application/config_defaults.py:208-275` 的 US `run_points` 已是 `10 / 0 / 10`；`src/application/scan_scheduler.py:296-330` 生成 09:40 + 后续整点，但当前 decision/rendering 没有把 due target 作为结构化“批次”传给用户消息。
- legacy compact path `multi_tick/notify_format.py:421-467` 依赖 `notification_text` 分段和字符串解析，不适合作为 structured Daily Brief 的输入协议。

## 3. Goal / Non-goals

### 3.1 Goal

交付一个用户可直接理解、最新消息自包含、后台状态机不回退的 Daily Brief 通知投影：

- 用户首先看见候选、持仓建议、资金容量和变化原因；
- opening opportunities 始终称为“候选”，不称为“行动”；
- 同标的多个 strike 明确是按排名排列的备选，不暗示全部同时执行；
- 资金容量明确到具体 candidate/symbol scope，并说明共享资金时数量不可相加；
- blocked/recovered/material change 可从单条最新消息理解；
- 结构化 audit 仍保留完整内部 identity 和 diagnostics；
- US 用户明确知道本轮属于 09:40 首轮或某个整点批次，并能区分 scheduled batch、数据截至时间和实际送达时间。

### 3.2 Non-goals

- 不新增自动下单、quantity allocation、组合优化器或执行计划器；
- 不改变候选过滤、排序、收益或风险算法；
- 不删除 canonical JSON 中的内部 ID、source provenance、rejections 或 data gaps；
- 不新增数据库、状态文件、发送栈、模板 DSL 或新的配置开关；
- 不修改 quiet hours、provider routing、transport idempotency 或通知确认语义；
- 不把 legacy Markdown/text 变成 Daily Brief 的中间协议；
- 不保证显示价格在整段交易时段持续可成交；
- 不修改 US/HK `run_points`、午休/收盘节奏、gate 或通用缺省 fallback；现有调度只做回归验证；
- 不新增独立 notification scheduler、cooldown、pending-message 状态或固定收盘总结。

## 4. 锁定术语与业务语义

| Canonical fact | 用户术语 | 规则 |
|---|---|---|
| Sell Put / Covered Call / Combo Yield opening row | 候选 / 备选 | 未形成 quantity + limit + allocation 的执行计划前，永不称为行动 |
| `close_position` active P0/P1 | 持仓建议 | 可显示“建议平仓/调整/继续观察”等人类动作 |
| position-specific not evaluable | 暂无法评估 | 只影响对应持仓，不把整个 brief 说成 blocked |
| overall `actionability=blocked` | 本轮暂无法更新 | 不展示旧候选；系统下一轮自动重试 |
| capacity | 条件容量 | Sell Put 按具体 strike 估算且共享现金；Covered Call 按具体标的持股覆盖；不同候选数量不可直接相加 |
| `delivery_kind=delta` | 较上一轮有变化 | 只用于触发/审计；正文仍展示当前完整状态 |
| revision | 不展示 | 仅保留在 artifact、delivery 和 read API |
| `valid_until_utc` | 默认不展示 | 仅参与后台 actionability；不暗示报价有效期 |

## 5. Architecture boundaries

### 5.1 必须保留的 canonical backend

以下边界不因通知改版而改变：

- `domain/domain/daily_decision_brief.py`
  - brief identity、action identity、normalization、effective actionability、material diff；
- `src/application/daily_decision_brief_service.py`
  - 从 structured artifacts 组装 brief；
- `src/application/daily_decision_brief_repository.py`
  - immutable revisions、current pointer、last-confirmed delivery pointer、delivery key、confirmation validation；
- `src/application/tick_notification_flow.py`
  - quiet hours、provider selection、send、confirmation、retry；
- CLI / Agent read tool 和 run-scoped/shared audit artifacts。

**禁止实现方式**：

- 设置 `notifications.daily_brief.enabled=false` 作为最终修复；
- 让 legacy notification preparation 重新成为 active authority；
- 绕过 repository 直接发送；
- 发送成功前推进 delivery pointer；
- 从最终 Markdown 解析候选或持仓事实。

### 5.2 User projection ownership

`src/application/daily_decision_brief_renderer.py` 继续作为现有 public import facade，但职责收窄为两个纯函数边界：

1. `build_daily_decision_notification_view(brief, diff=None, delivery_kind="full", *, market_tz, user_tz) -> Mapping`
2. `render_daily_decision_notification(view, *, limits=None) -> str`

现有 public entry points 保留，避免无关 command/API churn：

- `render_full_brief(...)`：构建无变化 banner 的当前 compact view；
- `render_daily_brief_lifecycle(...)`：依据 lifecycle 构建 initial/material/recovered/blocked view；
- `render_delta_brief` / `render_recovery_brief` 如无外部调用则删除；如测试/调用仍依赖，则变为薄 facade，不再输出 revision-only 消息。

Projection view 是**内存中的私有展示契约，不持久化**。仅允许以下字段类别：

- account / market display labels；
- localized data-as-of；
- optional change banner；
- candidate cards；
- position advice cards；
- account capacity summary；
- blocked summary。

禁止把 canonical mapping、任意 `metrics` mapping、任意 `reason` 或未知字段整体传入 renderer；builder 必须逐字段构造 allowlist scalar values。

### 5.3 Legacy renderer boundary

- `notify_symbols.py` 和 `multi_tick/notify_format.py::build_account_message_compact` 不作为新 Daily Brief renderer 的依赖；
- 不复制其 text parser，不先序列化再解析；
- 只复用其用户信息层次作为 acceptance reference：标题 → 候选 → 持仓 → 资金；
- 更新 ownership 注释/文档，明确 legacy formatter 只负责 legacy alert-text path，Daily Brief structured formatter 负责 enabled Daily Brief path。

## 6. Candidate / Action 语义修复

### 6.1 锁定兼容策略

本 work unit **不从 persisted `actions` 删除 opening candidate records**。原因：`daily_decision_brief.v1` 已使用 action identity 作为同日 diff 和 delivery 基线；直接删除会改变 Agent/read semantics，并使同日 rollback 到旧代码时把所有 opening candidates 再次报成 `action_added`。

锁定为：

- `actions` 在 canonical v1 中是内部 decision lifecycle records，不等同于用户执行指令；
- `action_type in {open_candidate, open_combo_yield}` 的记录是 candidate lifecycle carrier；
- `candidates` 仍是用户候选卡片的唯一内容来源；renderer 不从 opening action 渲染候选正文；
- `close_position`、overall blocker 等才进入用户“持仓建议 / 系统阻塞”语义；
- 若未来要从 canonical `actions` 彻底移除 candidates，必须作为独立 versioned schema/migration work unit，不塞入本次通知 UX 修复。

修改 `src/application/daily_decision_brief_service.py` 仅做以下结构化补强：

- 保留现有 candidate → action lifecycle assembly 和 stable identity，避免历史/rollback version skew；
- `_position_view` 补齐已有上游字段 `evaluation_status` 和 `quote_status`，供 projection 做确定性的人类状态映射；不得通过解析 `reason` 猜测“价格不可用”还是“覆盖不足”；
- Combo Yield candidate 补齐 `priority`，默认沿用当前 P1 语义；
- candidate/action change view 所需的 expiration、strike、option_type 必须来自 structured fields，不解析 broker symbol。

### 6.2 Domain diff 分类

修改 `domain/domain/daily_decision_brief.py`，继续复用现有 stable action identity 和一个 diff engine，不新增第二套 candidate diff：

- 增加 `_is_opening_candidate_action(action)`，识别 `open_candidate / open_combo_yield`；
- opening action 的 material transition 使用 candidate vocabulary：
  - 新进入 active P0/P1：`candidate_added`；
  - 从 active P0/P1 离开、消失或失去条件：`candidate_invalidated`；
  - P1 → P0：`candidate_priority_upgraded_to_p0`；
  - P0 → P1/P2：`candidate_priority_downgraded`；
- true actions 继续使用 `p0_added / action_added / action_invalidated / priority_*`；
- candidate/action change view 必须携带 `strategy_family / symbol / option_type / expiration / strike / priority` 这些可读事实；`contract_symbol`、lot/group identity 可保留在 audit view，但 banner 不使用；
- 同一 opening action 的 mid/bid/ask、收益率、delta、DTE 或 rank 变化继续不触发 material notification；
- 同一 opening action 仍 active P0/P1 且 `metrics.capacity.contracts_available` 改变时，发出 `candidate_capacity_changed`，携带 before/after；
- eligibility/state 或 priority transition 优先于 capacity change，同一 candidate 同一 diff 不重复计数；
- 现有 top-level `capacity_changed` 不再用于 Sell Put / Covered Call 通知触发，因为它只是 first-known candidate 的投影，不是稳定账户聚合；若未来存在真正 aggregate capacity，须另有明确 schema。

现有 `_has_active_high_priority_actions` 仍能覆盖 opening lifecycle carriers，因此 actionability transition 的 material gate 不因本次改动失效。

### 6.3 Same-day and rollback compatibility

- 历史 revision 和新 revision 都保留 opening action identity，不产生批量 false invalidation；
- 不重写历史 artifact、不重置 delivery pointer、不生成 migration 文件；
- 新代码回滚到旧版本时，旧 action diff 仍看到相同 opening actions，不会仅因 schema 语义变化批量报“新增行动”；
- `daily_decision_brief_diff.v1` 顶层 shape 不变；新增 candidate change types 是 additive enum extension；
- 增加 old-diff-label → new-diff-label、new release → old-code-compatible fixture/scenario，验证 stable action IDs、delivery digest 和 confirmation pointer。

### 6.4 Consumer compatibility

- 实施前使用 repo-wide search 再确认 `open_candidate`、`open_combo_yield` 和 diff change types 的所有 runtime consumers；当前 `origin/main` 证据显示 diff change type 的 runtime consumer 仅为 Daily Brief renderer，其余命中为 tests/docs；
- `src/application/agent_tools/daily_brief.py::_OUTPUT_CONTRACT` 明确 `brief.actions[]` 是 internal lifecycle records，opening opportunities 的用户内容来源为 `brief.candidates`；read output 顶层 shape 不变；
- Agent/CLI structured output 继续返回完整 audit facts；`rendered_markdown` 使用 user-safe projection；
- release note 记录新增 candidate diff vocabulary，但不声称 persisted brief schema 已迁移；
- 若 preflight 发现 repo 外 consumer 对 diff change type 使用 closed enum，则停止并先设计 versioned read/diff contract，不静默扩 enum。

## 7. User-facing projection contract

### 7.1 Header and time

默认格式：

```text
# OM · lx · 美股
> 盘中更新 · 10:00 批次
数据截至：美东 10:03 / 北京 22:03
```

规则：

- market timezone：从 `base_cfg.markets.<market>.schedule.timezone` 取得；US fallback `America/New_York`，HK fallback `Asia/Hong_Kong`；
- user timezone 使用 request 已有 `bj_tz`，fallback `Asia/Shanghai`；
- 使用 `zoneinfo` 处理 DST；
- 同一自然日可省略日期；market/user local date 不同或跨交易日时显示 `MM-DD HH:mm`；
- malformed/missing `data_as_of_utc` 显示“数据时间未知”，不得输出 raw value；
- 默认不展示 `generated_at_utc`、`valid_until_utc`、revision 或 ISO 字符串。

### 7.2 Contract display

仅从 explicit structured fields 构造：

- `TCOM · 2026-08-21 · $40 Put`
- `TCOM · 2026-08-21 · $35 Put（备选 2）`

规则：

- 不解析 `US.TCOM260821P40000`；
- 不展示 `contract_symbol`；
- strike currency 按 market 明确：US 使用 `$40`，HK 使用 `HK$40`；未知市场只显示 `40 Put`，不猜币种；
- expiration/strike/option_type 缺失时显示“合约信息不完整”，不回退到 broker code；
- Combo Yield 分别显示 Put/Call 两条腿的人类字段，不展示 group/leg IDs。
- 候选区与持仓区独立：`candidates.combo_yield` 为空只表示本轮没有新的组合增强候选，不得抹掉已有持仓的组合增强归属；已有组合持仓使用 `PDD · 组合增强（Put 侧）` / `（Call 侧）` 等人类标签，不展示 raw `leg_role`。

### 7.3 Candidate cards

按 family 和既有 rank 排列；同一 symbol 多 strike 明确标注“首选 / 备选 N”。每项最多展示：

- symbol、strategy label、expiration、strike、option type；
- price：优先 mid，缺失时显示 bid/ask；
- annualized return 使用现有 strategy authority：Sell Put=`annualized_net_return_on_cash_basis`，Covered Call=`annualized_net_premium_return`，Combo Yield=`annualized_net_credit_yield`；对应字段缺失则不显示，禁止跨策略猜用其它 return；
- delta、DTE、预计净收入；
- 一条人类风险/可用性提示；
- 不展示 raw priority enum、source path、identity 或完整 metrics dump。

消息使用“候选”，不得使用“建议下单”“新增行动”或把 account capacity 复制成该候选 quantity。

### 7.4 Position advice cards

- active close advice：显示 symbol、人类可读合约、建议动作、最重要的一条理由/指标；
- observe：显示“继续观察”；
- not evaluable：显示“暂无法评估（价格不可用）”或“暂无法评估（行情覆盖不足）”；
- 持仓策略归属必须保留：即使本轮 Combo Yield 候选为 0，已有 Combo Yield lot 仍显示为“组合增强”；候选是否为空不得改变持仓分类；
- 只依据新增保留的 structured status 映射：`coverage_missing → 行情覆盖不足`，`quote_unusable / unavailable → 价格不可用`，`not_evaluable / error / blocked` 且无更具体状态时 → 数据暂不可用，`priced` 再使用 close tier/action；不得解析自由文本 reason 猜状态；
- 未知 internal status 统一显示“暂无法评估（数据暂不可用）”；
- 不透出 `position_lot_id`、`strategy_group_id`、`leg_role`、raw `close_action`。

### 7.5 Capacity

示例：

```text
## 资金
- TCOM 08-21 $40 Put：按当前现金最多 8 手
- 备选方案共享同一现金额度，数量不可相加
```

- 不把 top-level `capacity.sell_put` 的 first-known row 误称为全账户统一“最多 8 手”；Sell Put 数量随 strike 变化，使用 candidate 自带 capacity，明确为“按该候选估算”；
- Covered Call capacity 按 underlying/symbol 展示，不把某一标的的可覆盖股数推广到其它标的；
- 多个 Sell Put 候选共享现金，数量不可相加；不同 Covered Call 标的按各自持股覆盖，不做跨标的合计；
- top-level `capacity` 继续保留在 audit 以兼容当前 brief shape，但不再作为 Sell Put / Covered Call material trigger，renderer 也不把 first-known value 当成无条件账户上限；
- 如 available cash 已有可靠 currency，则可附加；无 currency 不猜；
- 不把 capacity reason code 直接展示。

### 7.6 Rejections and diagnostics

- 主动通知不展示 rejection category/count/sample、source artifact、raw data gap；
- canonical JSON、CLI/Agent structured facts 和 audit artifacts 继续保留；
- overall blocked message 可使用 data gaps/action reason 生成一条 allowlisted 人类摘要，但不得 dump 原始对象。

## 8. Lifecycle rendering and delivery semantics

| Backend lifecycle | 是否发送 | 用户 payload |
|---|---:|---|
| `full`, live/planning | 是 | 当前完整 compact snapshot；无 revision |
| `full`, blocked | 是 | 短 blocked 告警；不展示候选 |
| `delta`, material, current live/planning | 是 | 一行“较上一轮”摘要 + 当前完整 compact snapshot |
| `delta`, recovered | 是 | “数据已恢复”摘要 + 当前完整 compact snapshot |
| `delta`, current blocked | 是 | 短 blocked 告警 + 本轮影响摘要 |
| `none` | 否 | 空字符串；发送链路和 provider 不应被解析 |

### 8.1 Change banner

只展示用户可理解的聚合变化，不逐条 dump diff：

- recovered：`数据已恢复，以下为当前结果`；
- candidate changes：`较上一轮：新增 2 个 Sell Put 候选`；
- position changes：`较上一轮：PDD 持仓建议已变化`；
- capacity：`较上一轮：TCOM 08-21 $40 Put 条件容量 8 → 6 手`；
- 多类变化：展示最多两项，剩余显示“另有 N 项变化”。

Banner 不显示 revision、action_id、contract_symbol、lot/group/leg identity 或 raw change type。

若 material change 指向的 candidate/position 超出常规 top-N，projection 必须优先保留本轮 changed item，再用既有 rank 填满剩余名额；对已经移除的 candidate，banner 使用 diff change view 中的 explicit expiration/strike 显示，不允许出现“有变化但正文/摘要都找不到对象”的消息。

确定性预算顺序：changed active close advice → changed eligible candidates → unchanged active close advice（priority）→ unchanged candidates（family + rank）→ observe/not-evaluable positions。changed items 可以突破 section soft limit，但不能突破现有 global item/character hard cap；超过 hard cap 时 banner 展示前两项并汇总“另有 N 项变化”，对应 section 明确显示“另有 N 项未展开”，不得静默截断。

### 8.2 Delivery invariants

不修改 repository envelope 语义：

- initial 仍为 `delivery_kind=full`；
- 后续 material 仍为 `delivery_kind=delta`；
- no material 仍为 `none`；
- delivery key 继续绑定 last delivered digest + material diff digest；
- provider 成功且本地 completion 成功后才 confirm；
- send/confirm 失败不得推进 pointer；
- 同一 prepared lifecycle retry 复用原 delivery key；
- message 是否包含完整 snapshot 不参与 delivery kind 判定。

### 8.3 Sending cadence contract

用户锁定的 US 节奏就是现有 canonical schedule，不做时间迁移：

```yaml
run_points:
  start_plus_min: 10
  hourly_minute: 0
  end_minus_min: 10
```

现有 `_scheduled_run_targets()` 由此产生：

- **US 夏令时（包括 2026-07-20）**：09:40 / 10:00 / 11:00 / 12:00 / 13:00 ET；14:00 ET 因现有“北京时间 02:00 前”gate 不执行；
- **US 冬令时**：09:40 / 10:00 / 11:00 / 12:00 ET；13:00 ET 已达到 gate，不执行；
- **HK**：保持现状，不属于本 work unit。

09:40 与 10:00 相隔 20 分钟是**明确保留的产品节奏**：09:40 用于开盘后首轮判断，10:00 起回到整点检查。

这些是**检查批次，不是精确送达秒数，也不是每轮必发**：

1. 当日首个成功 scheduled scan 对应 `09:40 批次`，发送 current full snapshot；
2. 后续整点检查只有 material change 才发送；无变化静默；
3. 扫描、行情获取和渲染需要时间，09:40 批次可能在 09:43 等时间送达；消息不得把 09:43 误写成调度时间；
4. 消息同时展示人类化的批次和数据截至，例如 `今日首次 · 09:40 批次`、`数据截至：美东 09:43 / 北京 21:43`；不展示 raw ISO；
5. catch-up run 必须继续标记其原 scheduled batch，并展示真实 data-as-of，不伪装成准点完成；
6. 首轮已发送 blocked 告警时，下一检查点 recovered 可以发送；若 blocked 未形成/未送达，下一成功扫描属于 initial full，不得称“恢复”；
7. manual/force 运行没有 scheduled batch，必须通过 explicit structured trigger context 标注“手动触发”，不得解析 scheduler `reason` 文本；默认诊断继续使用 `--no-send`；
8. 不发送 heartbeat，不新增固定整点空消息或收盘总结；如果未来要求“每个整点都必须收到”，应作为独立 delivery-policy 决策，不在本次暗中改变 material-only 语义。

用户可见 trigger 示例：

- `今日首次 · 09:40 批次`；
- `盘中更新 · 10:00 批次：候选/持仓/条件容量变化`；
- `数据恢复 · 11:00 批次`；
- `本轮暂无法更新 · 12:00 批次`；
- `手动触发`。

### 8.4 Timing ownership and structured batch context

- scheduler target 保持不变；本 work unit 不修改 `src/application/config_defaults.py`、`configs/system.json`、`config.yaml` 或 generated runtime config 的 `run_points`；
- 为避免从实际完成时间或 free-text reason 猜批次，scheduler decision/notification context 必须提供 optional structured `scheduled_target_market`（或等价字段）；scheduled run 有值，manual/force 为 `null`；
- `scheduled_target_market` 是运行上下文，不写入 persisted brief identity，不参与 brief digest、material diff、delivery key 或 confirmation pointer；
- renderer 只把 structured target 格式化为 `HH:mm 批次`；字段缺失/非法时省略批次，不输出 raw value，也不从 `now_market`、`data_as_of` 或 reason 反推；
- `data_as_of_utc` 继续表示实际数据截至时间，localized 到 market/user timezone；actual provider send timestamp 默认不展示，因为它受扫描耗时、重试和渠道延迟影响；
- manual/force 标签使用 flow-local explicit bool/trigger kind；scheduled/manual 分类不得依赖文案；
- 现有 `last_run_utc_by_account`、target 去重、catch-up grace 和 gate 语义保持不变，不做 state migration。

### 8.5 A+ notification policy and operational separation

A+ 的产品语义锁定为：

- **决策通知**回答“现在是否值得重新看一眼”：09:40 首轮固定发送；后续整点仅 material change、blocked 或 recovered 时发送；
- **运行状态**回答“系统是否正常运行”：继续由 scheduler/runtime status、healthcheck、run audit 和既有 failure alert surfaces 承担，不用每小时重复简报充当 heartbeat；
- 用户没有收到某个整点的决策消息，只能表示“没有形成需要发送的 material delivery”，不能单独证明任务成功或失败；运维判断必须读取 structured runtime evidence；
- 本 work unit 不新增 heartbeat、固定空消息或第二套 health notification state machine；如现有 pipeline/transport failure alert 已存在，必须保持，不得被 Daily Brief 的 material-only early return 吞掉；
- no-material run 仍生成必要的 canonical brief/diff/audit artifacts，但在 provider route resolution 前结束，不发送用户消息、不推进 confirmed delivery pointer；
- material change 的用户范围锁定为：候选新增/失效或高优先级语义变化、持仓建议变化、整体 blocked/recovered、条件容量跨整手变化；普通价格/收益率/Delta/文案/时间/revision 波动不单独触发。

## 9. Example acceptance messages

### 9.1 Initial full snapshot

```text
# OM · lx · 美股
> 今日首次 · 09:40 批次
数据截至：美东 09:43 / 北京 21:43

## 候选
1. TCOM · Sell Put · 08-21 $40 Put（首选）
   - 权利金 $… · 年化 … · Delta … · 32天
2. TCOM · Sell Put · 08-21 $35 Put（备选 2）
   - 权利金 $… · 年化 … · Delta … · 32天

## 持仓
- FUTU：暂无法评估（价格不可用）
- PDD · 组合增强（Put 侧）：暂无法评估（行情覆盖不足）

## 资金
- TCOM 08-21 $40 Put：按当前现金最多 8 手
- 备选方案共享同一现金额度，数量不可相加
```

### 9.2 Material update — self-contained current snapshot

```text
# OM · lx · 美股
> 盘中更新 · 10:00 批次：新增 2 个 Sell Put 候选
数据截至：美东 10:03 / 北京 22:03

## 候选
1. TCOM · Sell Put · 08-21 $40 Put（首选）
   - 权利金 $… · 年化 … · Delta … · 32天
2. TCOM · Sell Put · 08-21 $35 Put（备选 2）
   - 权利金 $… · 年化 … · Delta … · 32天

## 持仓
- FUTU：暂无法评估（价格不可用）
- PDD · 组合增强（Put 侧）：暂无法评估（行情覆盖不足）

## 资金
- TCOM 08-21 $40 Put：按当前现金最多 8 手
- 备选方案共享同一现金额度，数量不可相加
```

### 9.3 Overall blocked

```text
# OM · lx · 美股
> 本轮暂无法更新 · 10:00 批次
数据截至：美东 10:03 / 北京 22:03
- 原因：关键行情暂不可用，本轮不提供新候选。
- 系统会在下一轮监控自动重试。
```

## 10. Implementation slices

### Slice A — Candidate/action semantic correction

Files:

- `src/application/daily_decision_brief_service.py`
- `domain/domain/daily_decision_brief.py`
- focused service/domain/scenario tests

Acceptance:

- opening candidate action IDs 保持稳定，但 diff 使用 candidate vocabulary；
- `candidates` 是用户卡片唯一内容源，opening actions 不被 renderer 重复展示；
- close/blocker actions 保留原 action vocabulary 和 identity；
- same-day upgrade/rollback 无批量 false add/invalidation；
- no schema/file migration。

### Slice B — Structured compact projection

Files:

- `src/application/daily_decision_brief_renderer.py`
- `src/application/agent_tools/daily_brief.py`（仅适配 safe rendered preview，如需要）
- renderer/CLI/Agent tests

Acceptance:

- allowlist view 与 renderer 分离；
- latest message 自包含；
- human contract/time/status；
- materially changed item 在 section limit 下仍可见，removed item 在 banner 中可读；
- no internal identity/raw enums/raw ISO/broker code/rejection dump；
- output length/top-N 仍有确定性上限。

### Slice C — A+ notification and trigger context wiring

Files:

- `src/application/tick_notification_flow.py`
- notification flow/scenario tests

Acceptance:

- flow 将 market/user timezone 和 explicit `force_mode`/trigger kind 传给 projection；
- scheduled/manual 分类不得解析 scheduler reason；
- `full/delta/none` 和 delivery key 不变；
- material/recovered payload 为 change banner + full current snapshot；
- blocked payload 短且不展示旧候选；
- no-material 在 route/provider resolution 前结束；
- 09:40 initial full、后续 material-only、blocked/recovered 和 manual 语义符合 A+；
- existing operational failure alert path 不被 no-material early return 吞掉。

### Slice D — Structured scheduled-batch context

Files:

- `src/application/scan_scheduler.py`
- scheduler DTO/normalization boundary if required
- `src/application/multi_account_tick.py`
- `src/application/tick_notification_flow.py`
- focused scheduler/notification tests

Acceptance:

- existing US target list remains 09:40 + allowed hourly top-of-hour targets；
- scheduled decision exposes optional structured target time；manual/force does not fabricate one；
- renderer shows `HH:mm 批次` only from structured context；
- catch-up displays original batch plus real data-as-of；
- target context does not affect brief digest、diff、delivery identity or persisted schema；
- no config or scheduler-state migration。

### Slice E — Documentation and release contract

Files:

- user-facing notification docs/examples；
- `CHANGELOG.md`（实施 release 时）；
- relevant ownership comments。

Acceptance:

- 说明“候选不是订单”“Sell Put 候选共享现金且各候选条件容量不可相加”；
- 说明 structured audit 仍保留内部 identity；
- 不新增 config migration。

## 11. Test plan

### 11.1 Domain / service

- two TCOM strikes → `candidates.sell_put` 两项、canonical lifecycle actions 保持两项 stable identity，但用户 renderer 只展示两张候选卡且无“行动”措辞；
- close position → action identity/lot/group/leg 仍存在于 canonical JSON；
- candidate add/remove/eligibility/priority changes 的 material semantics；
- price/rank-only change 静默；
- same-candidate conditional capacity change material；first-ranked candidate 变化不得通过 top-level first-known capacity 制造伪 aggregate change；
- old diff vocabulary → new candidate vocabulary 保持 stable identity；upgrade/rollback 无批量 false add/invalidation；
- blocked→recovered 仍 material。

### 11.2 Renderer contract

必须断言消息不包含：

- `position_lot_id`、`strategy_group_id`、`leg_role`、`action_id`；
- `revision`；
- `LIVE`、`READY`、`BLOCKED`、`PLANNING`；
- raw ISO UTC；
- `US.TCOM260821P40000` 或其它 broker contract code；
- rejection category/count/sample；
- raw `action_added` / `candidate_added` / reason codes。

必须断言：

- `TCOM · 08-21 $40 Put`、`备选 2`；
- capacity 带“候选共享”；
- capacity 按 candidate/symbol scope 展示，不把 first-known capacity 当全账户统一上限；
- Sell Put/Covered Call/Combo Yield 分别使用 canonical annualized metric；
- Combo Yield candidate-empty / existing-position-present fixture 保留“组合增强（Put 侧/Call 侧）”用户归属，同时不泄漏 `strategy_group_id` / `leg_role`；
- US DST 与跨日时间正确；
- missing expiration/strike 不回退 broker code；
- unknown reason 使用安全中文 fallback；
- Agent tool 的 `rendered_markdown` 同样 user-safe，而 structured `brief` 仍保留 audit identity。

### 11.3 Lifecycle / delivery

- first full successful send → confirm revision；
- no material → no message、no provider route；
- material candidate change → delta envelope + self-contained full payload；
- blocked → short message；
- blocked→recovered → recovered banner + full payload；
- provider failure / quiet hours / no-send → pointer 不推进；
- provider success then repeated completion → already-confirmed/no duplicate；
- message hash/length audit 仍记录，且不记录 message body；
- 09:40 首次成功即使无候选也发送完整状态；
- 10:00 后无 material 不发送，但 canonical run/diff/audit evidence 仍存在；
- no-material early return 不掩盖 scheduler/pipeline/provider failure。

### 11.4 Scheduler / send cadence

- US summer target list保持 09:40/10:00/11:00/12:00/13:00 ET，14:00 被 02:00 BJ gate 排除；
- US winter target list保持 09:40/10:00/11:00/12:00 ET；
- HK target list保持现状；
- 09:40 scheduled batch 与 09:43 data-as-of 可同时正确展示；
- 10:00 catch-up run 仍标记 10:00 批次，不使用实际启动时间冒充 target；
- initial full / material / recovered / blocked / manual trigger label 正确；
- blocked 未送达时，下一成功 run 不得误标 recovered；
- delayed catch-up 展示 original scheduled batch + real data-as-of，不展示 raw cron/gate；
- no-material target 不解析 provider route、不发送；
- manual/force authorized send 基于 explicit trigger context 标记“手动触发”，不得解析 reason；no-send 不推进 pointer。

### 11.5 Quality gates

实施后至少运行：

```bash
./.venv/bin/python -m pytest \
  tests/test_daily_decision_brief_domain.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_repository.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_daily_decision_brief_scenarios.py \
  tests/test_daily_decision_brief_agent_tool.py \
  tests/test_daily_decision_brief_cli.py

./.venv/bin/python -m pytest \
  tests/test_multi_tick_notify_format.py \
  tests/test_notify_symbols_markdown.py \
  tests/test_unified_tick_entrypoint.py \
  tests/test_scan_scheduler_notify_semantics.py \
  tests/test_layered_config.py
```

并对 representative fixtures 做 rendered text snapshot inspection，不发送真实通知。

## 12. Rollout and rollback

### 12.1 Preflight

- 当前 worktree `HEAD=v1.2.419` 早于目标实现；实施必须先在基于最新 `origin/main` 的独立/干净 branch 或 worktree 中进行；
- 不覆盖 `paseo.json` 等用户未跟踪改动；
- 读取实施时最新 `AGENTS.md`、VERSION、tests 和 runtime contract。

### 12.2 Rollout

1. 完成 focused tests 和 broader notification regression；
2. 生成本地 fixture 消息并人工检查；
3. 在隔离的非生产 output root 运行 `--no-send` canary，允许生成测试 artifacts，但不得发送、不得推进 confirmed pointer；远端上线前的 no-write 检查使用现有 read-only status/config surfaces，不把 `--no-send` 误称为只读；
4. 按正常 VERSION release 流程发布 projection/trigger-context 代码；
5. 远端升级前单独取得明确批准；不修改 production schedule config；
6. 升级后验证 scheduler 仍为 09:40 + 整点，并观察一轮真实 scheduled message 的“批次 / 数据截至”展示。

### 12.3 Rollback

- projection、diff vocabulary、structured-status 和 trigger-context 补强作为同一 code release unit 整体回滚，不允许只回退 renderer 或只回退 diff；
- production schedule 未修改，无 schedule rollback；
- 不删除 revision/current/delivery artifacts；
- 不重置 confirmed pointer；
- rollback 后 normalizer 仍接受已有 brief v1；
- 如必须临时关闭发送，使用现有运维通知总开关并单独批准，不把 `daily_brief.enabled=false` 作为长期产品回退。

## 13. Done criteria

以下全部成立才算完成：

1. 用户最新一条消息无需回看旧消息即可理解当前状态；
2. 用户消息中的 opening rows 只叫候选，两个 TCOM strike 显示为首选/备选；canonical audit 可继续保留 lifecycle action records；
3. capacity 按 candidate/symbol scope 表达，Sell Put 共享现金且数量不可相加，不再误称 first-known value 为全账户统一上限；
4. 消息不存在内部 identity、raw enums、raw ISO、revision、broker code 和 rejection dump；
5. blocked/recovered/material/no-change 行为符合 lifecycle table；
6. last-confirmed pointer、delivery key、retry/idempotency 证明未回归；
7. canonical JSON 和 Agent structured facts 仍保留完整 audit identity/diagnostics；
8. 未新增执行 planner、发送栈、持久化模型或不必要配置；
9. US scheduled targets 保持 09:40 + 整点；消息能区分 scheduled batch 与实际 data-as-of，US DST/gate 和 HK target list 未改变；
10. scheduled batch/manual trigger 来自 explicit structured context，并在消息中有明确的人类标签；未送达 blocked 不产生伪 recovery；
11. A+ 成立：09:40 首次成功固定发送，后续整点仅 material/blocked/recovered 发送，无变化静默且运行证据可查；
12. focused + notification regression tests 全部通过；
13. 隔离 output root 的 no-send canary 可生成测试 artifacts，但不发送、不推进 pointer；远端 no-write preflight 不运行会写 artifacts 的 tick。
14. 组合增强候选为 0 时，已有组合增强持仓仍保留人类可读策略归属，候选空状态不得将其降级为无策略普通持仓。
