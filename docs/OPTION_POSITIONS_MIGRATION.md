# Option Positions Upgrade Migration

这份文档只回答一件事：

> 线上版本升级后，旧的 option positions 数据怎么安全迁到新方案。

适用前提：
- 旧线上环境已经有本地 `trade_events` / `position_lots`
- 新版本采用 canonical `trade_events -> projection -> position_lots`
- Feishu `option_positions` 已退休，不再作为线上策略、持仓仓库 bootstrap 输入或镜像同步目标
- legacy v2 代码已删除，不再参与默认读写路径

---

## Legacy v2 state

旧 `option_positions_v2` 状态目录只作为历史文件留存：

- 位置：`output_shared/state/option_positions_v2/`
- 允许用途：人工历史备查。
- 禁止用途：作为风控输入、close target resolver、facade fallback、默认 rebuild / verify-projection / inspect 依据。

默认系统现在只从 canonical `trade_events -> position_lots` 推导仓位；代码库已经没有 v2 domain / repo / service 读取入口。不要把旧 v2 输出再写回 canonical 状态。

---

## 1. 升级时不要先清旧数据

升级阶段先保留现有本地状态：

- 保留 `trade_events`
- 保留 `position_lots`

不要做这些事：

- 不要先删 SQLite 旧表内容
- 不要为了“切新方案”手工重写历史 lot

原因：

- 新方案会先通过统一读路径接管旧数据
- canonical truth 已经固定为 `trade_events`
- `position_lots` 只是当前投影视图
- Feishu 不再定义本地业务状态

---

## 2. 让系统先接管本地数据

升级后，新版本会优先从本地 `trade_events` 重放并刷新 `position_lots`。

接管逻辑：

- 如果历史里已有 `trade_events`，直接重放为当前 `position_lots`
- 如果没有 `trade_events`，但已有旧 `position_lots`，系统只报告 legacy state；不会在默认打开 repo 时自动生成 bootstrap snapshot
- 如果没有本地状态，系统保持 SQLite 空状态；不会从 Feishu `option_positions` 做 bootstrap
- 旧 SQLite `trade_events` / `position_lots` / `option_positions` 表不再默认迁移；只有显式执行 `./om option-positions store migrate-legacy --confirm` 时，才会作为一次性迁移输入读取
- 最后统一重放事件并投影出当前仓位

默认不会因为配置了 Feishu table 或历史 SQLite `option_positions` 表，就自动把外部/旧状态变成本地事实。

SQLite 存储路径固定为：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

如果线上怀疑存在多库并行，先跑只读诊断：

```bash
./om option-positions store inspect --config config.us.json
./om option-positions store inspect --config config.us.json --format text
```

---

## 3. 迁移第一步先跑 rebuild

先做一次只面向本地状态的重建：

```bash
./om option-positions rebuild
./om option-positions rebuild --format json
```

这一步的目标不是“修数据”，而是从 canonical `trade_events` 重放并刷新本地 `position_lots` 投影视图。

会生成 / 刷新的关键文件：

- 本地 SQLite `position_lots`

判断标准：

- 命令能正常完成
- 没有意外 diagnostics 激增
- `trade_event_count` / `position_lot_count` 与预期一致

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
- latest projection verify 状态是否合理

迁移后的 `inspect` 不只是看当前 lot / events / projection，也会显示：

- latest projection verify report
- latest projection verify checkpoint

---

## 5. 验证事件表与持仓投影

外部 verification snapshot 对账已退休。当前默认只验证本地 canonical 链路：

```text
trade_events -> shadow position_lots
vs
persisted position_lots
```

示例：

```bash
./om option-positions verify-projection
./om option-positions verify-projection --format json
./om option-positions verify-projection --mode full
```

这一步会：

- 从 `trade_events` 重放出 shadow projection
- 与当前持久化 `position_lots` 比较
- 输出 projection verify report
- 成功时写入 checkpoint；下一次 `--mode auto` 可在事件和持仓 fingerprint 不变时复用 checkpoint

如果出现 mismatch，下一步应该是 rebuild / repair / adjust / void 明确修账，而不是直接手改 `position_lots`。

---

## 6. verify 之后，按差异决定 repair

一旦正式 verify 完成，后续运维主要看两层：

- canonical `trade_events` 事件流
- projection verify report 的差异

也就是说：

- `trade_events` 仍是事实源
- `position_lots` 仍是当前状态投影
- mismatch 必须通过 explicit repair / adjust / void 进入账本

后续再跑 `inspect` 时，重点就是看当前 lot、相关 events、latest projection verify 是否一致。

---

## 7. 写路径迁移不需要停机重录

升级后不需要停机把仓位“重新录一遍”。

这些操作仍然可以继续使用：

- manual open
- manual close
- adjust lot
- void event

新版本会在这些动作后：

- 追加 canonical `trade_events`
- 通过 ledger preflight 校验目标 lot 和 contract identity
- 重放并刷新 `position_lots`

所以迁移方式不是“停旧开新”，而是：

> 升级后继续正常写入，新增动作会自然落到 canonical ledger 轨道。

---

## 8. Feishu 只当镜像 / 对账对象

迁移完成后，Feishu 继续可以同步，但角色要收口：

- 可以作为远端镜像
- 可以作为人工核对对象

但不要再把 Feishu 当 bootstrap 输入或 steady-state 主读源。

如果线上旧流程有“本地读不到就回退 Feishu”的思路，升级后应改成：

- 本地 projection 为主
- Feishu 只做镜像和核对

---

## 9. 旧模型不要立刻清

迁移完成后先观察一段稳定期，再评估是否进一步弱化旧兼容层。

稳定期最低要求：

- rebuild 稳定
- inspect 抽查无异常
- verify-projection 已经能稳定输出 projection verify 报告
- risk context / report 都围绕统一输出稳定

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
4. 跑一次 `./om option-positions verify-projection`
5. 对 mismatch 执行 explicit repair / adjust / void
6. 之后按 `trade_events -> position_lots` 进入新运维流程

一句话：

> 正确迁移方式不是先做大规模数据改写，而是“保留旧数据 -> canonical rebuild -> inspect -> verify-projection -> repair 差异 -> 再进入新运维流程”。
