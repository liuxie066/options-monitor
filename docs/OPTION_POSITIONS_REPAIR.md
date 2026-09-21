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

一般 `trade-events repair` 通过 void 加替代事件修正经济字段时，会保留原手续费及其
证据，并按替代事件的标识、金额、币种和时间重新生成现金换算。预览只读取已保存的
历史汇率；缺少匹配汇率时人民币金额标记为 pending，不沿用原换算或用当前汇率替代。
原事件保持不变，可继续追溯。仅绑定订单身份的下述原地修复仍遵循独立约束。

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

### 场景 D：历史 Futu 开仓缺少 OpenD 订单身份

只适用于经济事实已正确、fee 尚非 actual 的 active option open 事件。人工核实
OpenD 订单后先预览；`reason` 必须写明 OpenD 核对证据：

```bash
./om trade-events repair <open_event_id> \
  --futu-account-id <numeric_account_id> \
  --order-id <order_id> \
  --reason "OpenD manual evidence: <reference>" \
  --dry-run --format json
```

该命令不会自动创建数据库备份。生产执行前先另行创建并验证 SQLite 备份；确认 `event_id`、
绑定前后身份和 `expected_before_sha256` 后再单独写入：

```bash
./om trade-events repair <open_event_id> \
  --futu-account-id <numeric_account_id> \
  --order-id <order_id> \
  --reason "OpenD manual evidence: <reference>" \
  --confirm --format json
```

该路径原地补录 metadata，不生成 void/replacement event，不能与 strike、price、contracts
等其他 override 混用。写入后仍要单独 dry-run/apply `trade-events fees-sync`；本命令不连接 OpenD，
也不自动补 fee。

### 场景 E：历史 Futu 开仓时间与 OpenD 证据不一致

只适用于未 void 的 canonical Futu open 事件，包括已有下游 close 的已平仓 lot。事件必须已经保存
`opend_order_evidence.v1`；修正时间只能等于证据中最早一笔订单的 `trade_time_ms`，不能人工指定
其他时间。先核对目标、时间、订单 ID 和写前哈希：

```bash
./om trade-events repair <open_event_id> \
  --trade-time-ms <earliest_opend_trade_time_ms> \
  --reason "OpenD stored evidence: <order_ids>" \
  --dry-run --format json
```

生产执行前另行创建并验证 SQLite 备份，再单独确认写入：

```bash
./om trade-events repair <open_event_id> \
  --trade-time-ms <earliest_opend_trade_time_ms> \
  --reason "OpenD stored evidence: <order_ids>" \
  --confirm --format json
```

该路径保留 `event_id`、`ingest_seq`、lot identity 和 downstream lineage，只修正事件时间并强制全量
重建投影。旧时间形成的 `cash_conversions` 会被移除，避免把错误时点的汇率继续当作有效证据；完成
同一月份的时间修正后，必须先预览再回填该月份的历史换算：

```bash
./om option-performance cash-conversion backfill \
  --config-key us --account lx --start-date 2026-05-01 --end-date 2026-05-31
./om option-performance cash-conversion backfill \
  --config-key us --account lx --start-date 2026-05-01 --end-date 2026-05-31 --apply
```

只在 dry-run 选出的事件与本次时间修正目标一致时 apply，并对其他账户分别执行。任一事件缺少 OpenD 证据、订单数量与事件数量不一致、目标时间不是证据最早
成交时间、事件已 void 或投影出现额外变更时，命令都会失败且整个事务回滚。

### 场景 F：Futu 当前期权条款与账本不同

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

### 场景 G：你怀疑投影脏了，但账本本身没问题

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
./om option-performance report --config-key us --broker 富途 --account lx --period mtd
```

你要确认四件事：
- 事件链符合预期
- 当前 lot 状态符合预期
- replay projection 与当前 `position_lots` 一致
- MTD 期权净现金流、胜率和期权收益率与 ledger 事件一致

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

## 6. 订单统一：停写窗口、验收与回退

本节是窗口准备说明，不构成生产执行授权。合并源码、发布 R1、升级、执行 DDL / 数据
迁移、恢复服务是独立动作。R1 必须先证明能够读写旧、新两种表形状；只有生产只读副本
的全量核对通过后，才能另行交付收紧到单一新形状的 R2。窗口外
`LOT_IDENTITY_WINDOW_ENABLEMENT` 保持 `None`，不能为通过检查临时启用。

### 窗口前冻结证据

1. 记录主机、运行版本 / commit、runtime root、实际 SQLite 路径、配置路径、账户及市场。
   用 `option-positions store inspect` 核实 active store；不能根据文件名猜目标。
2. 记录全部 writer 的原状态：tick、trade-intake、auto-close、inbound Control、正在运行的
   service、timer 和人工 CLI。按目标环境的服务清单停写并确认没有正在执行的 writer；
   仅停 timer 不够。生产操作由获得授权的操作人执行。
3. 按 [部署文档 §6](DEPLOY_LINUX_MAC.md#6-切换旧数据) 的 SQLite `.backup` 流程制作
   一致备份并验证 `integrity_check = ok`，保留源库、备份哈希和原版本信息。禁止裸 `cp`
   主库文件，也不能覆盖已有备份。恢复演练使用独立目录和库，不启动任何真实服务。
4. 在确定的目标与版本上归档 inventory、verify、全量投影报告，以及 SQL registry 的检查
   结果。报告目录位于 runtime state 之外，权限限制为操作人可读。不得把生产账本或完整
   报告提交到 Git。

下面的 `RUNTIME`、`EVIDENCE` 均需操作人填入已核实的绝对路径；不能直接使用占位值。
命令能正常退出不代表验收成功，必须检查 JSON 内容：

```bash
./om option-positions lot-identity-migration inventory --runtime-root "$RUNTIME" --format json > "$EVIDENCE/inventory-before.json"
./om option-positions lot-identity-migration verify --runtime-root "$RUNTIME" --format json > "$EVIDENCE/identity-before.json"
./om option-positions verify-projection --runtime-root "$RUNTIME" --mode full --format json > "$EVIDENCE/projection-before.json"
```

迁移前 inventory 描述待办，不能要求它已经是迁移后的零待办。首次 R1 writer 开库可能
补列并改变 schema cookie；必须在这个转换之后重新生成冻结 manifest。若 apply 拒绝
manifest 漂移，重新调查并生成预览，不能修改旧 manifest 的哈希或绕过检查。

### 执行与验收

单独获准的窗口使用冻结 manifest 经公开 `lot-identity-migration apply` 入口执行；
保留原始 apply JSON receipt。此入口需要 `--manifest`、`--runtime-root`、`--apply --yes`，
并且构建自身必须已获该窗口的启用授权。不得用手写 SQL 代替，也不能把部分步骤的
`deferred` 当成 D1–D4 全部完成。该命令没有 `status` 子命令，后续状态由 inventory、
verify 和 store inspect 读取。

停写保持期间重新执行前述三个读取命令，分别保存为 `*-after.json`。逐项检查：

- apply 各步骤完成，重建 receipt 的行数、读回等值检查、外键与完整性检查通过；
  新表 identity 非空、唯一，退役列消失，四类保护仍生效。
- identity verify `ok = true`、无 readiness reasons；迁移后的 inventory 无目标待办。
- projection 报告 `mode_used = full_replay`、`ok = true`、`green = true`，
  `store_face.columns_read = true`；独立 `lot_parity_probe.green = true`，无 probe error。
  任一字段缺失均为未验证。重复 ID 即使内容相同也阻断；空 ID 单独阻断。
- 事件 fingerprint 保持不变。行数、ID 集合、payload 或派生列的差异都需要阻断并解释，
  不能由 rowid 判定覆盖。迁移前已存在差异也须保留，不能抹去基线后声称无损。

### rowid 前后核对（A4）

A3 的单次报告只提供 `store_rowids`；`verdicts.rowid_moved = null` 表示尚未比较。
下面只比较两份全量报告中的物理行号，不替代上面的业务验收。缺少快照时失败，不能
写成零差异。身份有重复或为空时快照不可用；行增减阻断。单纯 rowid 改变归档为
非阻断事实，不向普通消费者的 `items.status` 注入非阻断判定词。

```python
# Run: python /path/to/this-snippet.py projection-before.json projection-after.json
import json
import sys

before, after = [json.load(open(path, encoding="utf-8")) for path in sys.argv[1:]]
for report in (before, after):
    if report.get("mode_used") != "full_replay":
        raise SystemExit("unverified: a full replay report is required")
    rows = report.get("store_rowids")
    if not isinstance(rows, dict) or any(
        not key.strip() or type(value) is not int for key, value in rows.items()
    ):
        raise SystemExit("unverified: missing or invalid rowid snapshot")
    if any(report.get("summary", {}).get(key, 0) for key in ("duplicate_lot_id", "empty_lot_id")):
        raise SystemExit("blocked: invalid lot identity")
old, new = before["store_rowids"], after["store_rowids"]
if old.keys() != new.keys():
    raise SystemExit("blocked: lot identity set changed")
moved = [{"lot_id": key, "before": old[key], "after": new[key]}
         for key in sorted(old) if old[key] != new[key]]
print(json.dumps({"rowid_moved": len(moved), "items": moved}, ensure_ascii=False))
```

在放行 rowid 改变前，对待交付 commit 搜索 `src/`、`scripts/` 中的 `rowid` / `lastrowid`：
逐处确认 `position_lots` 的使用只影响快照、遍历或诊断，不作为业务身份、关联键或
持久游标。其他表（如 lifecycle evidence）的 rowid 不属于本次重建；不要一并调整。
将命中清单和判断放在窗口证据中。未来若出现依赖 position_lots.rowid 的业务消费者，
必须先迁移该消费者，不能沿用本节的非阻断结论。

### 任一验收失败时回退

保持所有 writer 停止，保存失败库、sidecar、日志和 receipt，不重复 apply 碰运气。
事务异常应核实已回滚；进程退出或网络中断不能当作回滚证据。不要单独删除 WAL/SHM。

本窗口采用**源库回退，保持 R1 二进制版本不变**；窗口结束前不得回滚二进制，代码问题
在 R1 上向前修复。使用部署文档 §6 的备份 API，在新的隔离 runtime 目录恢复整份备份
并验证，保留失败现场；不要把旧主库文件覆盖到可能仍有 WAL 的
路径上。只有核实没有窗口后的新事件写入，才可以恢复窗口前备份；若已有新事件，先
停写并核对差异，另行授权恢复方案，禁止丢弃新事件。

恢复后的 store inspect、完整性、事件 / lot 数量和全量投影验收都须与备份基线一致，
失败则继续停写。全部验收通过且操作人批准后，只恢复窗口前原本启用或运行的服务和
定时器，随后读回实际状态；不能把原本禁用的任务一起启动。
