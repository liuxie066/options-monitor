# Futu 成交与 PM 持仓同步

## 边界

Futu OpenD deal push 是实时成交入口。OM 标准化成交后维护期权和生命周期
账本；股票或 ETF 成交只向异步调度器投递账户刷新意图。调度器调用本机
portfolio-management 服务，PM 再读取 Futu 完整持仓快照并更新绝对
`quantity` 和 `average_cost`。

同步意图不携带推算持仓、成交数量或成本。OM 不直接写 PM/Feishu，PM
也不写 OM ledger 或 Feishu `transactions`。

## 启用

权威 `config.yaml` 可增加：

```yaml
trade_intake:
  holdings_sync:
    enabled: true
    debounce_sec: 2
    request_timeout_sec: 120
    max_attempts: 3
    retry_backoff_sec: 2
    queue_capacity: 100
    recent_deal_limit: 2000
    state_dir: output_shared/state/trade_intake/stock_holdings_sync
```

默认关闭。只有 `trade-intake` 处于 `apply` 且已经经过写入确认时才会启动。
YAML 只接受 `holdings_sync` 子树；`mode`、确认和其他写入权限仍由
CLI、服务定义和环境文件控制。
目标服务地址沿用 `PORTFOLIO_SERVICE_URL`，默认
`http://127.0.0.1:8765`，并强制为 loopback origin。

## 运行语义

- 期权成交返回 `option_deal`，不会调用 PM。
- 股票或 ETF 使用 `deal_id` 去重，短时间内同账户成交合并为一次同步。
- 每个账户有独立队列和工作线程；一个账户超时或失败不会阻塞其他账户。
- PM 调用失败按配置有限重试；失败不会回滚 OM 已记录的期权/生命周期事实。
- 推送和 history backfill 使用同一个标准化回调；成功的 `deal_id` 会持久化，
  进程重启或回补再次看到该成交时不会重复同步。
- PM 的既有早晚全量同步仍是最终对账兜底。

## Push 来源身份

Futu deal push 通过 OM 主动连接的 OpenD TCP 端口进入，不是独立 webhook。
每个 push 必须在写入 durable inbox 之前绑定可信的 source 配置，并记录：

```text
source_id
account
futu_account_id
opend_process=FutuOpenD
opend_host
opend_port
received_at_utc
deal_id
```

`source_id + opend_host + opend_port` 是稳定的逻辑进程身份；操作系统 PID
会在 OpenD 重启后变化，只用于实时诊断，不进入业务幂等键。若 push payload
缺少账户 ID，只允许从绑定到单一 Futu 账户的 source 补齐；若 payload 身份
与 source 配置冲突，必须在入箱前拒绝并写
`push_source_identity_rejected` 审计，不能退化为裸 `deal_id`。

Push 与 history backfill 随后统一使用账户级 broker deal key：

```text
futu:<account>:<futu_account_id>:<deal_id>
```

因此无论 push 或 backfill 谁先到，后到者都只能命中同一业务成交，不能因
传输顺序生成第二条 inbox 记录。

## 审计

每个账户独立保存：

```text
<state_dir>/<account>/state.json
<state_dir>/<account>/audit.jsonl
```

`state.json` 记录最近成功 deal、高水位批次和最后状态；`audit.jsonl`
记录 started、attempt_failed、succeeded、failed。trade-intake 自身
audit 另外记录 `stock_holdings_sync_intent`，用于证明成交是否成功入队、
被合并、拒绝或已同步。

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

## 通知 Outbox

业务事务只写冻结的通知意图，不在 ledger 事务内调用飞书。dispatcher 使用
CAS 状态流转：

```text
pending -> claimed -> send_started
send_started -> confirmed | accepted | explicit_failed | unknown
```

- `claimed` 在发送前租约过期可安全退回 `pending`。
- `send_started` 后进程失联必须冻结为 `unknown`，不能自动重发。
- `explicit_failed` 只按有限退避重试，达到上限后保持可观测。
- `accepted` 表示 provider 已接受但尚无强确认；不能伪装成 `confirmed`。
- `unknown` 只能由操作员依据 provider 证据标记确认，或创建增加
  `delivery_revision` 的补偿发送；原记录不重开。

trade-intake status 将 Inbox、生命周期原因状态和 Outbox 状态分开显示，并
保留 source 的 `pid`、`source_id`、OpenD host/port、账户以及启动时间。

## 运维命令

以下命令均以 dry-run 为默认。这里只列只读或预览形式；实际写入必须同时给出
`--apply` 和 `--confirm`（或 `--yes`），发送通知还需要明确授权真实发送。

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

# 查看或预览处理通知
./om option-positions lifecycle receipts inspect \
  --outbox-id <outbox-id>
./om option-positions lifecycle receipts dispatch \
  --once --account lx --config config.us.json --dry-run
./om option-positions lifecycle receipts reconcile \
  --outbox-id <outbox-id> --mark confirmed \
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

`accepted` 只能人工收敛为 `confirmed` 或 `unknown`，不能直接 resend；
进入 `unknown` 后才允许显式 `--mark resend`。人工收敛会保留原始
provider receipt。

`lifecycle confirm-expired` 已退役。禁止用人工按钮直接制造
`expiration_no_settlement`；该结论必须来自完整且冻结的 broker settlement
observation。

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
