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
