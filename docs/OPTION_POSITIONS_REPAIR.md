# Option Positions Repair Playbook

这份文档只回答一件事：

> 本地 `option_positions` / `position_lots` 发现错账后，应该怎么安全修。

如果你现在问的是“线上升级后怎么从旧方案迁到新方案”，先看：

- `docs/OPTION_POSITIONS_MIGRATION.md`

适用前提：
- canonical model 仍然是 `trade_events -> projection -> position_lots`
- 不直接手改 SQLite 行
- Feishu `option_positions` 已退休，不是修账入口

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

动作：

```bash
./om option-positions void-event --event-id <open_event_id> --confirm
```

效果：
- 该开仓不会再投影到 `position_lots`
- 月收益 / premium 收入也不会再计入

---

### 场景 B：这笔平仓记错了，应该撤销

动作：

```bash
./om option-positions void-event --event-id <close_event_id> --confirm
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
./om option-positions adjust-lot --record-id <record_id> --premium-per-share 3.1 --confirm
./om option-positions adjust-lot --record-id <record_id> --exp 2026-07-17 --strike 105 --confirm
```

效果：
- 会追加 `adjust` 事件
- 会重算相关派生字段，例如 `position_id` / `cash_secured_amount`
- 月收益 / premium 统计会按修正后的投影生效

---

### 场景 D：你怀疑投影脏了，但账本本身没问题

动作：

```bash
./om option-positions rebuild
```

效果：
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
./om option-positions report monthly-income --broker 富途 --account lx --month 2026-04
```

你要确认三件事：
- 事件链符合预期
- 当前 lot 状态符合预期
- 月收益 / premium 收入没有被错误污染

---

## 4. 远端镜像已退休

期权持仓不再同步到 Feishu 多维表。修复流程只收口本地 SQLite ledger：

- `trade_events` 是写入事实。
- `position_lots` 是本地 projection。
- `./om option-positions rebuild` 从 `trade_events` 重建 projection。

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
```

再重新检查结果。
