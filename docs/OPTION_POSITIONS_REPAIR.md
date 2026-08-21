# Option Positions Repair Playbook

这份文档只回答一件事：

> 本地 `option_positions` / `position_lots` 发现错账后，应该怎么安全修。

适用前提：
- canonical model 仍然是 `trade_events -> projection -> position_lots`
- 不直接手改 SQLite 行
- Feishu `option_positions` 已退休，不是修账入口

如果问题来自旧环境升级、多库并行或历史 Feishu 表，先用 `store inspect`
确认当前 active SQLite；旧表只能作为人工历史证据，不能重新接成运行时事实源。
生产环境中的每条命令都应显式传入正确的 `--runtime-root`；下面省略该参数只是为了
突出修复语义，不代表可以依赖当前目录猜测目标 store。

---

## 1. 先看清再下手

先看当前 lot：

```bash
./om option-positions list --broker 富途 --account lx --status all
```

如果已经知道 `record_id`，先看这条 lot 的事件链：

```bash
./om option-positions history --record-id <record_id>
./om option-positions history --record-id <record_id> --format json
```

如果你需要看整个 canonical 账本：

```bash
./om option-positions events --account lx
./om option-positions events --account lx --format json
```

判断原则：
- 先用 `history` 看单条 lot
- 再用 `events` 看全局账本
- 不确定是哪条 event 错时，不要直接修

---

## 2. 常见场景对应动作

### 场景 A：这笔开仓本来就不该存在

先预览：

```bash
./om option-positions void-event --event-id <open_event_id> --dry-run
```

确认 event、lot 和影响范围后再写入：

```bash
./om option-positions void-event --event-id <open_event_id> --apply --confirm
```

效果：
- 该开仓不会再投影到 `position_lots`
- 月收益 / premium 收入也不会再计入

---

### 场景 B：这笔平仓记错了，应该撤销

先预览，确认后再写入：

```bash
./om option-positions void-event --event-id <close_event_id> --dry-run
./om option-positions void-event --event-id <close_event_id> --apply --confirm
```

效果：
- 已实现收益不再计入
- 对应 lot 会恢复到平仓前状态
- 原本开仓收到的 premium 仍保留

---

### 场景 C：开仓存在，但字段录错了

适合修这些字段：
- `contracts`
- `strike`
- `exp`
- `premium_per_share`
- `multiplier`
- `opened_at_ms`

先 dry-run：

```bash
./om option-positions adjust-lot --record-id <record_id> --premium-per-share 3.1 --dry-run
```

确认后再 apply：

```bash
./om option-positions adjust-lot --record-id <record_id> --premium-per-share 3.1 --apply --confirm
./om option-positions adjust-lot --record-id <record_id> --exp 2026-07-17 --strike 105 --apply --confirm
```

效果：
- 会追加 `adjust` 事件
- 会重算相关派生字段，例如 `position_id` / `cash_secured_amount`
- 月收益 / premium 统计会按修正后的投影生效

---

### 场景 D：Futu 当前期权条款与账本不同

分红、拆并股等公司行动可能调整存量合约的 strike、multiplier 或其他交割条款。
此时 Futu 的期权代码只用于定位合约；当前经济条款必须以该代码对应的 market
snapshot 为准，不能继续从代码文本反解析 strike。

质量检查先用 broker position code 中的原始合约身份精确关联 canonical lot，再比较
同一 code 对应 snapshot 的当前 strike / multiplier。只有 code lineage、方向和数量均
能唯一对应时，才会以 `POSITION_CONTRACT_TERMS_DRIFT` 立即阻断
`option_position_report`、`lifecycle` 和 `close_advice`。Scheduled Tick 不会把被阻断
市场的旧 strike 加入 Close Advice 预取计划。目标账户/市场没有唯一、当前的
`om.option_positions` 数据集时同样 fail closed；其他市场的 snapshot 缺失不会污染该
市场的判断。

相同标的、方向、到期日和数量本身不能证明公司行动。如果 broker code 不能与原 lot
精确对应，例如刚平掉旧 strike 又新开相同数量的新 strike，系统只报告普通 position
divergence，不会建议 `adjust-lot`。系统不会做模糊 strike 匹配，也不会自动改账。

先核对 lot 事件链和券商公司行动通知，确认调整后的 strike、multiplier、到期日、
方向及交割物。证据不足，或特殊交割物无法由当前 lot 模型表达时，应停止修复并人工
处理；不能仅凭价格接近就认定是同一合约。

确认当前模型能够完整表达调整条款后，先预览：

```bash
./om option-positions adjust-lot --record-id <record_id> --strike <adjusted_strike> --multiplier <adjusted_multiplier> --dry-run
```

核对新增 `adjust` 事件、`position_id` 以及 Put 的 `cash_secured_amount` 或 Call 的
`underlying_share_locked` 后，再单独授权写入：

```bash
./om option-positions adjust-lot --record-id <record_id> --strike <adjusted_strike> --multiplier <adjusted_multiplier> --apply --confirm
```

修复后按第 3 节验证，并在下一次质量刷新中确认 `OM-POS-002` 恢复通过。

---

### 场景 E：你怀疑投影脏了，但账本本身没问题

默认只预览投影差异：

```bash
./om option-positions rebuild
```

确认目标 store 和差异后才 apply：

```bash
./om option-positions rebuild --apply
```

apply 后的效果：
- 从 `trade_events` 全量重建 `position_lots`

这个命令适合：
- 手工修复后做一次确认
- 怀疑本地投影与账本不一致

---

## 3. 修完后怎么验

最小验证顺序：

```bash
./om option-positions history --record-id <record_id>
./om option-positions list --broker 富途 --account lx --status all
./om option-positions verify-projection
./om option-performance report --config-key us --broker 富途 --account lx --period month --month 2026-04 --no-refresh-quotes
```

你要确认四件事：
- 事件链符合预期
- 当前 lot 状态符合预期
- replay projection 与当前 `position_lots` 一致
- 当月 PnL、现金和 premium activity 没有被错误污染

---

## 4. 远端镜像已退休

期权持仓不再同步到 Feishu 多维表。修复流程只收口本地 SQLite ledger：

- `trade_events` 是写入事实。
- `position_lots` 是本地 projection。
- `./om option-positions rebuild` 默认预览从 `trade_events` 重建 projection 的差异；
  只有 `--apply` 才写入。

普通 Feishu holdings 读取仍然保留，但它不参与期权持仓 ledger 修复。

---

## 5. 不要这么做

- 不要直接手改 `position_lots`
- 不要直接把 Feishu 表当主表修
- 不要手工改 `trade_events.event_json`
- 不确定哪条 event 错时，不要先 `void`

如果你已经直接改了投影表，先跑：

```bash
./om option-positions rebuild
./om option-positions verify-projection --mode full
```

解释清楚差异后，才执行 `./om option-positions rebuild --apply` 并重新检查结果。
