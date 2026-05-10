# Option Positions Upgrade Migration

这份文档只回答一件事：

> 线上版本升级后，旧的 option positions 数据怎么安全迁到新方案。

适用前提：
- 旧线上环境已经有本地 `trade_events` / `position_lots`
- 新版本采用 canonical `trade_events -> projection -> position_lots`
- Feishu `option_positions` 只作为 bootstrap / 镜像，不再作为稳态主读源

---

## 1. 升级时不要先清旧数据

升级阶段先保留现有本地状态：

- 保留 `trade_events`
- 保留 `position_lots`
- 保留 Feishu 映射 / sync metadata

不要做这些事：

- 不要先删 SQLite 旧表内容
- 不要先清 Feishu 映射字段
- 不要为了“切新方案”手工重写历史 lot

原因：

- 新方案会先通过统一读路径接管旧数据
- canonical truth 已经固定为 `trade_events`
- `position_lots` 只是当前投影视图
- Feishu 只是 bootstrap / 镜像，不再定义本地业务状态

---

## 2. 让系统先自动接管旧数据

升级后，新版本会优先生成 / 刷新 `option_positions_v2` 状态。

接管逻辑：

- 如果历史里已有 bootstrap snapshot 事件，就直接把它当 baseline
- 如果没有 bootstrap snapshot，但已有旧 `position_lots`，系统会自动生成 `legacy_baseline`
- 然后把 legacy `trade_events` 和 v2 native events 合并
- 最后重放事件并投影出当前仓位

你不需要先做一次性全量数据重写。

---

## 3. 迁移第一步先跑 rebuild

先做一次只面向本地状态的重建：

```bash
./om option-positions rebuild
./om option-positions rebuild --format json
```

这一步的目标不是“修数据”，而是把 v2 当前状态正式落盘。

会生成 / 刷新的关键文件：

- `output_shared/state/option_positions_v2/current/projection.current.json`
- `output_shared/state/option_positions_v2/snapshots.jsonl`
- `output_shared/state/option_positions_v2/events.jsonl`

判断标准：

- 命令能正常完成
- baseline / event / projection 文件已经生成
- 没有意外 diagnostics 激增

---

## 4. rebuild 后立刻做 inspect 抽查

不要 rebuild 完就直接认为迁移结束。

先抽查：

- 每个账户至少抽 1~2 条关键仓位
- 最近做过 manual close / adjust 的仓位
- 你最担心会错账的仓位

示例：

```bash
./om option-positions inspect --record-id <record_id>
./om option-positions inspect --account lx --symbol TSLA --option-type put --strike 100 --exp 2026-06-19
```

重点确认：

- 当前 lot 是否正确
- projection 结果是否正确
- `diagnostics` 是否有异常
- baseline / verification / reconciliation 状态是否合理

迁移后的 `inspect` 不只是看 baseline / events / projection，也会显示：

- persisted baseline
- 当前 projection checkpoint
- latest verification
- latest reconciliation report

---

## 5. 确认真实仓位后，再做正式 reconcile

当你已经确认券商真实仓位后，再准备一份 verification snapshot 做正式对账。

示例：

```bash
./om option-positions reconcile --snapshot-file /path/to/verification.json
./om option-positions reconcile --snapshot-file /path/to/verification.json --format json
```

`verification.json` 可以是：

- 一个完整 verification snapshot 对象
- 或一个 `lots` 数组

这一步会：

- 持久化 verification snapshot
- 输出 reconciliation report
- 把这次 verification 作为后续 projection checkpoint / 运维基线

这才是迁移真正完成的标志。

---

## 6. reconcile 之后，把 verification 当作新基线

一旦正式 reconcile 完成，后续运维主要看的不再是“老 `position_lots` 长什么样”，而是：

- verification 后的 snapshot
- 之后继续追加的 event replay

也就是说：

- persisted baseline 负责保留历史导入起点
- latest verification 负责成为当前运维 checkpoint
- projection 负责表达当前仓位结果

后续再跑 `inspect` 时，重点就是看这三层是否一致。

---

## 7. 写路径迁移不需要停机重录

升级后不需要停机把仓位“重新录一遍”。

这些操作仍然可以继续使用：

- manual open
- manual close
- adjust lot
- void event

新版本会在这些动作后：

- 追加 v2 native event
- 在需要时写 verification checkpoint
- 再刷新 projection

所以迁移方式不是“停旧开新”，而是：

> 升级后继续正常写入，新增动作会自然落到 v2 轨道。

---

## 8. Feishu 只当镜像 / 对账对象

迁移完成后，Feishu 继续可以同步，但角色要收口：

- 可以作为 bootstrap 来源
- 可以作为远端镜像
- 可以作为人工核对对象

但不要再把 Feishu 当 steady-state 主读源。

如果线上旧流程有“本地读不到就回退 Feishu”的思路，升级后应改成：

- 本地 projection 为主
- Feishu 只做镜像和核对

---

## 9. 旧模型不要立刻清

迁移完成后先观察一段稳定期，再评估是否进一步弱化旧兼容层。

稳定期最低要求：

- rebuild 稳定
- inspect 抽查无异常
- reconcile 已经落正式基线
- Feishu sync / context / report 都围绕统一输出稳定

在这之前，不要急着清：

- 旧 `position_lots`
- legacy `trade_events` 重建链路
- 旧 bootstrap 兼容逻辑

---

## 10. 最小线上迁移顺序

推荐按下面顺序执行：

1. 升级版本，但不清旧数据
2. 跑一次 `./om option-positions rebuild`
3. 用 `inspect` 抽查关键仓位
4. 准备真实仓位 verification snapshot
5. 跑一次正式 `reconcile`
6. 之后按 verification checkpoint + event replay 进入新运维流程

一句话：

> 正确迁移方式不是先做大规模数据改写，而是“保留旧数据 -> 让 v2 自动接管 -> rebuild -> inspect -> reconcile -> 再进入新运维流程”。
