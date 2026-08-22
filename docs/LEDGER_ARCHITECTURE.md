# Ledger Architecture

本文记录当前交易与期权持仓账本的运行契约。它描述已经落地的边界，不是迁移计划。

## 权威链路

```text
trade_events -> deterministic projection -> position_lots
```

- `trade_events` 是业务事实。
- `position_lots` 是可重放投影，不是第二套事实源。
- `lot_id` / 当前 `record_id` 是写入目标身份。
- `position_key` 只用于聚合、展示和风险查询，不能代替精确 lot 写入目标。
- Feishu `option_positions`、旧 v2 snapshot 和历史兼容文件不参与稳态读取或写入。

默认 SQLite 位于：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

不要直接修改 SQLite 行。修复必须表达为可审计的语义事件，或通过受控 projection rebuild / verify 恢复派生状态。

## 模块所有权

| 边界 | 当前 owner |
|---|---|
| 领域事件与投影规则 | `domain/domain/ledger/` |
| 非 ledger 模块的公共应用入口 | `src/application/ledger/api.py` |
| 命令与维护动作 | `src/application/ledger/commands.py` |
| 查询与读模型 | `src/application/ledger/queries.py`、`read_model.py` |
| 事件写入与投影发布 | `src/application/ledger/writer.py` |
| SQLite repository | `src/application/ledger/repository.py` |
| lot 目标解析与 preflight | `src/application/ledger/lot_resolver.py`、`preflight.py` |
| 人工持仓工作流 | `src/application/positions/` |
| broker trade intake | `src/application/trades/` |
| 人工 CLI | `src/interfaces/cli/option_positions.py`、`trade_events.py` |

`positions`、`trades`、Agent tools、CLI 和 pipeline 不应绕过 `ledger.api` 导入内部写入原语。领域层不得反向导入 `src/`。

## 写入语义

账本动作按业务事实区分，不能互相替代：

- open
- buy-close
- expire-close
- assignment
- exercise
- assigned-stock sale
- adjustment
- void / repair

每次写入都必须满足：

1. account、broker、symbol、option type、side 和 contract identity 明确；
2. close / assignment / exercise 解析到确定 lot；
3. 数量不会超过当前可用 lot；
4. 幂等身份足以防止 broker deal 或人工请求重复落账；
5. 写前 preview 与写后 projection 使用同一事实；
6. 身份冲突、projection drift 或关键证据缺失时 fail closed。

手工入口默认先 dry-run。例如：

```bash
./om option-positions add \
  --request-id manual-open-<stable-id> \
  --account lx \
  --symbol NVDA \
  --option-type put \
  --side short \
  --contracts 1 \
  --currency USD \
  --strike 100 \
  --multiplier 100 \
  --exp <future-expiry> \
  --dry-run
```

`add`、`assign`、`exercise` 的 preview、apply 和响应丢失后的重试必须复用同一个
`--request-id`。相同 request ID 与相同 intent 返回原结果；同一 ID 绑定不同 intent
会 fail closed。确认前检查响应中的目标 SQLite、account、lot/event identity、数量和写入合同。

## 读取语义

运行时风险、Close Advice、Performance 和 Agent tools 从 canonical read model 读取：

- 单 lot 查询保留真实开仓、费用、策略快照和生命周期字段；
- 聚合持仓只用于展示或风险计算；
- 历史 `as_of` 查询不能用当前报价回填历史缺口；
- 当前时点报价刷新失败时返回明确 quote status；
- 缺少费用、汇率、行情或 lifecycle evidence 时保留 partial / missing，不把未知值写成零。

常用只读入口：

```bash
./om option-positions list --account lx --status open
./om option-positions inspect --record-id <lot-id>
./om trade-events list --account lx

./om-agent run --tool option_positions_read \
  --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
```

具体子命令以 `./om option-positions --help`、`./om trade-events --help` 和 `./om-agent spec` 为准。

### 稳定事件分页

Agent 通过 `option_positions_read action=events` 分页读取 canonical `trade_events`。SQLite 为每个
事件分配单调且不复用的 `ingest_seq`；首次查询记录最大序号，后续页使用
`trade_time_ms DESC, event_id DESC` 的 keyset cursor，并始终限制在该序号边界内。因此新增事件
不会插入正在进行的结果流，分页条数可以在 1–20 之间变化，也不会导致已返回成员重复。

这个 snapshot 冻结的是成员集合、筛选字段和排序字段，不是整行 JSON 的历史版本。事件成员
不可删除，`ingest_seq`、事件身份、交易时间、账户、市场、position effect 和合约筛选字段不可
修改；价格等不参与查询的补充字段仍可按现有账本语义更新。完整 TradeEvent 的编码与验证继续
由 Python canonical codec 负责，SQLite 不实现第二套领域 JSON 校验器。

旧库只声明新增列，不在普通启动时扫描回填。必须通过受控 position-projection migration 分批
填充分页投影并发布索引与约束；完成前 `action=events` 明确返回 pagination unavailable。

## 到期与交割生命周期

到期短仓不能仅凭“过了 expiry”自动写成 worthless：

- 价外自动关闭需要符合市场时区和报价证据；
- 价内、平值或缺少 spot 时进入 review；
- option leg 与 stock settlement leg 可以异步到达；
- assignment / exercise 必须有匹配的交割事实；
- `external_holdings` 账户缺少 broker lifecycle evidence 时默认要求人工复核。

到期维护由独立 `auto-close-expired` 服务/定时入口负责，不是普通 `account_run` 或扫描 pipeline 的隐式步骤。

## Projection 验证与恢复

`verify-projection` 默认是纯只读诊断：它可以读取已有 checkpoint 加速比较，但不会创建目录、
覆盖 latest report 或发布新 checkpoint。只有明确需要留下运维证据时才使用
`--publish-evidence`：

```bash
./om option-positions verify-projection --mode auto
./om option-positions verify-projection --mode auto --publish-evidence
```

生产定时验证显式使用 `--publish-evidence`；临时排查保持默认只读。

发现 read model、report 或 lot 状态异常时，按顺序处理：

1. 用只读 inspect/history/verify 确认 active runtime root 和 SQLite；
2. 检查 trade event 是否完整、重复或存在目标歧义；
3. dry-run projection rebuild；
4. 只有在差异可解释且目标准确时才 apply；
5. 用相同 runtime root 复查 lot、event history、Close Advice 和 Performance。

不得用以下方式“修好显示”：

- 直接更新 `position_lots`；
- 重新接回 Feishu / v2 兼容状态；
- 用聚合 `position_key` 猜测 close lot；
- 为缺失历史事实填入当前价格、当前汇率或零费用。

完整修复步骤见 [Option Positions Repair](OPTION_POSITIONS_REPAIR.md)。

## 下游合同

- [Close Advice Contract](CLOSE_ADVICE_CONTRACT.md)：如何消费 lot、行情和策略快照。
- [Option Performance Design](OPTION_PERFORMANCE_DESIGN.md)：利润、现金、activity 和组合桥接。
- [Assigned Stock Return Design](ASSIGNED_STOCK_RETURN_DESIGN.md)：assignment 后的正股成本与收益。
- [Architecture](ARCHITECTURE.md)：ledger 与 interfaces/application/domain/infrastructure 的整体边界。
