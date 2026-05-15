# Tool Reference

这份文档只回答两件事：

1. `om-agent` 目前有哪些公开工具
2. 它们和人工 CLI `om` 的关系是什么

如果你只想跑产品，先看根目录 [README.md](../README.md)。

---

## 1. 两套入口的区别

| 入口 | 面向对象 | 典型用途 |
|---|---|---|
| `./om` | 人工操作 | 手动跑 pipeline、分阶段运行、命令行查询 |
| `./om-agent` | 程序 / Agent | JSON manifest、结构化 tool 调用 |

一句话：

- `om` 是人类 CLI
- `om-agent` 是程序化工具入口

---

## 2. 如何查看工具清单

```bash
./om-agent spec
```

它会输出当前环境下可用的工具 manifest。

注意：
- `spec` 不是绝对静态的
- 某些默认值会受环境变量影响，例如写工具门禁

---

## 3. 常见调用方式

```bash
./om-agent run --tool <tool-name> --input-json '<json>'
```

也支持：

```bash
./om-agent run --tool <tool-name> --input-file payload.json
```

`--input-file` 会覆盖 `--input-json`。

---

## 4. Tool 与 `om` CLI 的关系

有些能力同时存在于：

- `om-agent` 的 tool
- `om` 的命令行入口

但名字不一定一样。

### 常见映射

| `om-agent` tool | `om` / CLI 对应能力 |
|---|---|
| `healthcheck` | `./om healthcheck` |
| `version_check` | `./om version` |
| `version_update` | Agent-only local `VERSION` update helper |
| `config_validate` | `./om config validate` |
| `scheduler_status` | `./om scheduler` 的只读判定部分 |
| `scan_opportunities` | `./om scan` / `./om scan-pipeline` |
| `candidate_rank_explain` | Agent-only read existing candidate CSV ranking explanations |
| `strategy_replay_analyze` | `./om strategy-replay analyze` |
| `preview_notification` | `./om notify preview` |
| `get_close_advice` | `./om close-advice` |
| `query_cash_headroom` | `./om sell-put-cash` / `src.application.cash_headroom_query::query_sell_put_cash(...)` |
| `monthly_income_report` | `./om option-positions report monthly-income` |
| `option_positions_read` | `src.application.option_positions_facade` / `src.application.option_positions_inspection` 的只读部分 |

说明：
- `om-agent` 更适合给程序调
- `om` 更适合人工操作
- `om-agent` 的 CLI 由 `src/interfaces/agent/cli.py` 维护；manifest 由 `src/application/agent_tool_registry.py` 维护，handler 由 `src/application/agent_tool_handlers.py` 维护，runtime config helper 由 `src/application/agent_tool_config.py` / `src/application/agent_tool_init_local.py` 维护。

配置优先级和 `config_validate` / `healthcheck` / `runtime_status` / `openclaw_readiness` 的正式边界，请以根目录 `CONFIGURATION_GUIDE.md` 为准。这里只保留工具说明，不再重复完整配置规则。

### Tick 入口关系

`om-agent` 当前不提供“直接发送通知”的 tool。实时 tick / 扫描 / 通知运行使用人工 CLI：

```bash
./om run tick --config config.us.json --accounts lx
./om run tick --config config.us.json --accounts lx sy
```

这是一条统一链路，单账户只是传一个账户的特例。旧脚本
`scripts/send_if_needed.py` 和 `scripts/send_if_needed_multi.py` 已移除。人工执行可直接调用
`./om run tick`；生产 cron 建议使用带锁和 timeout 诊断的包装入口：

```bash
./om run tick-cron --market hk --accounts lx sy --timeout 600
./om run tick-cron --market us --accounts lx sy --timeout 600
```

`tick-cron` 会按 market 推导 canonical config、lock path 和 `OM_TRIGGER_*`
诊断环境变量；`--dry-run-command` 可只查看将执行的 tick 命令。返回码语义：
`SKIP_LOCKED` 返回 `0`，表示上一轮还在跑；真实执行失败返回原始非零码并输出
`EXEC_FAILED_RC_<rc>`；超时返回 `124` 并输出 `EXEC_TIMEOUT_RC_124`。

---

## 5. 当前公开工具列表

## 5.1 `healthcheck`

用途：
- 校验 runtime config
- 检查账户路径
- 检查 OpenD / SQLite / 通知前置条件

示例：

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

---

## 5.2 `version_check`

用途：
- 检查本地 `VERSION` 与 git 远端发布 tag
- 不运行监控流程

示例：

```bash
./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'
```

---

## 5.2.1 `version_update`

用途：
- 预览或更新本地 `VERSION`
- 默认 dry-run；写入需要 `apply=true`、`confirm=true` 和 `OM_AGENT_ENABLE_WRITE_TOOLS=true`
- 不创建 git tag、不 commit、不 push、不运行发布流程

示例：

```bash
./om-agent run --tool version_update --input-json '{"bump":"patch"}'
OM_AGENT_ENABLE_WRITE_TOOLS=true ./om-agent run --tool version_update --input-json '{"version":"1.2.3","apply":true,"confirm":true}'
```

`apply=true` 是本地写入动作，还需要 `confirm=true` 和
`OM_AGENT_ENABLE_WRITE_TOOLS=true`。固定频率任务只应使用 dry-run 预览或版本检查。

---

## 5.3 `config_validate`

用途：
- 只校验 runtime config
- 不检查 OpenD
- 不运行 pipeline

示例：

```bash
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
```

---

## 5.4 `scheduler_status`

用途：
- 读取现有 scheduler state
- 返回当前调度判定、下次运行时间、是否处于通知窗口
- 不执行 `run-if-due`
- 不写 `mark-scanned` / `mark-notified`

示例：

```bash
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
```

---

## 5.5 `scan_opportunities`

用途：
- 跑扫描流程
- 返回候选摘要

示例：

```bash
./om-agent run --tool scan_opportunities --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":3}'
```

---

## 5.5.1 `candidate_rank_explain`

用途：
- 读取已有候选 CSV
- 返回 Top N 的排序分数、分数组件、输入指标、主要排序原因和风险提示
- 可用 `compare_baseline=true` 对比“收益率优先”的基线排序

示例：

```bash
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'
./om-agent run --tool candidate_rank_explain --input-json '{"candidate_path":"output/reports/sell_call_candidates.csv","mode":"call","top_n":5}'
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","score_weights":{"liquidity":0.02},"compare_baseline":true}'
```

注意：
- 该工具只读本地 CSV，不重新扫描、不发通知、不写 Feishu、不写报告。
- 默认先找 `output/reports`，再找 `output/agent_plugin/reports`；也可传 `report_dir`、`output_dir` 或 `candidate_path`。
- `score_weights` 只影响本次解释输出，不修改配置，也不改变生产排序默认值。

---

## 5.5.2 `strategy_replay_analyze`

用途：
- 读取离线策略复盘 CSV / JSON / JSONL
- 回答哪些 DTE、Delta 区间更有效，哪些标的收益高但回撤差，哪些过滤条件最有价值
- 输出 `dry_run_config_suggestions`，但不修改生产配置

示例：

```bash
./om-agent run --tool strategy_replay_analyze --input-json '{"replay_path":"output/reports/strategy_replay.csv","min_sample":5}'
./om strategy-replay analyze --replay-path output/reports/strategy_replay.csv --min-sample 5
```

注意：
- 该工具只分析已存在的复盘记录，不重新扫描、不发通知、不写 Feishu。
- 复盘记录应覆盖通过和被拒绝候选，否则过滤条件价值会缺少 shadow outcome 依据。
- 详细字段约定见 [STRATEGY_REPLAY.md](STRATEGY_REPLAY.md)。

---

## 5.6 `query_cash_headroom`

用途：
- Agent 查询 Sell Put 现金占用与余量的标准入口
- 包装 `src.application.cash_headroom_query` 的 `query_sell_put_cash(...)`
- 返回账户现金、Sell Put 担保占用、剩余可用现金
- 支持按账户筛选，并按可用汇率折算到 CNY

示例：

```bash
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"sy"}'
```

注意：
- Agent payload 使用 `broker` 表示券商口径；未传时读取 runtime config 的 `portfolio.broker`
- Agent 工具输入不再公开 `market` 别名；新调用统一使用 `broker`
- 该工具不会发送通知或写 Feishu；它会把查询产物写到本地 agent 输出目录

---

## 5.7 `monthly_income_report`

用途：
- 读取本地 option positions
- 返回月度期权收益的三类统计口径
- 默认只返回 summary；`include_rows=true` 时返回资金流、实现收益、开仓归因明细

核心字段：
- `net_cashflow_gross`：资金流口径，按交易发生月统计；short 开仓收款为正，
  long 开仓成本和平仓买回支出为负，long 平仓卖出为正。
- `realized_pnl_gross`：已实现口径，按平仓/到期月统计；short 为开仓权利金减平仓成本，
  long 为平仓卖出减开仓成本。
- `open_basis_lifecycle_pnl_gross`：开仓归因口径，按开仓月回填生命周期收益，
  公式为：
  `sell_open_premium - sell_close_cost_actual - enhancement_call_buy_cost + enhancement_call_sell_proceeds_actual`。
- `yield_enhancement_realized_pnl_gross`：收益增强 call 腿按实现口径统计，
  只有带 `yield_enhancement` / `enhancement_call` 标记的 long call 平仓收益进入该字段。
- `premium_received_gross` / `realized_gross`：兼容字段，分别对应 short 开仓权利金和已实现收益；
  新消费方优先使用上面的明确口径字段。

示例：

```bash
./om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"lx","month":"2026-04"}'
```

---

## 5.8 `option_positions_read`

用途：
- `action=list`：读取 position lots
- `action=events`：读取 canonical trade events
- `action=history`：读取单个 lot 的事件链
- `action=inspect`：读取投影诊断状态

示例：

```bash
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"history","record_id":"rec_xxx"}'
```

注意：
- 这个工具只开放读和诊断动作
- `add` / `buy-close` / `void-event` / `adjust-lot` / `rebuild` 不在此工具中开放

---

## 5.9 `get_portfolio_context`

用途：
- 获取账户持仓 / 现金 context

示例：

```bash
./om-agent run --tool get_portfolio_context --input-json '{"config_key":"us","account":"lx"}'
```

---

## 5.10 `prepare_close_advice_inputs`

用途：
- 预先刷新 close advice 依赖的本地输入

通常与 `close_advice` 搭配使用。

---

## 5.11 `close_advice`

用途：
- 基于本地 context 和 quotes 构建平仓建议

示例：

```bash
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

---

## 5.12 `get_close_advice`

用途：
- 一次性执行 close advice 推荐路径

示例：

```bash
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
```

这是更推荐的 Agent 入口。

---

## 5.13 `manage_symbols`

用途：
- 读取或修改 `symbols[]`

示例：

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

注意：
- `list` 永远是只读
- 真正写操作需要：
  - `OM_AGENT_ENABLE_WRITE_TOOLS=true`
  - `confirm=true`

---

## 5.14 `preview_notification`

用途：
- 只生成通知内容，不发送

示例：

```bash
./om-agent run --tool preview_notification --input-json '{"alerts_path":"output/reports/symbols_alerts.txt","changes_path":"output/reports/symbols_changes.txt","account_label":"lx"}'
```

---

## 5.15 `runtime_status`

用途：
- 只读汇总现有 runtime / OpenClaw 输出文件
- 不运行 pipeline
- 不发送通知
- 可读取 `openclaw.profile.json` / `.openclaw-profile.json` 或 payload 里的
  `profile_path` 作为 OpenClaw 路径、账户和 freshness 阈值
- 可读取可选的外层任务上下文，例如 `trigger_source`、`trigger_job_id`、
  `delivery.mode` / `delivery_mode`、`timeoutSeconds`，用于区分“代码没有发送”
  和“外层任务没有 announce”

示例：

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"profile_path":"openclaw.profile.json"}'
```

---

## 5.16 `openclaw_readiness`

用途：
- 面向 OpenClaw 的一站式 readiness 摘要
- 组合 `runtime_status`、`healthcheck` 和本地 `openclaw` 命令可用性
- 读取可选 OpenClaw profile，输出 `next_actions.safe_next_actions` 和
  `next_actions.blocked_actions`
- profile 或 payload 提供 `cron_jobs` / `include_cron_status=true` 时，会运行只读
  `openclaw cron list` / `openclaw cron runs`
- 检查通知 route 是否已配置，且不会返回完整通知 target

示例：

```bash
./om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
./om-agent run --tool openclaw_readiness --input-json '{"profile_path":"openclaw.profile.json"}'
```

---

## 6. 人工 CLI：版本检查

`./om version` 仍然保留为人工 CLI 能力。Agent 使用 `version_check`，二者读取同一个本地 `VERSION` 和远端 `v*` tags。

示例：

```bash
./om version
```

---

## 7. WebUI 能调用哪些工具

这点要特别说明：

> `om-agent` 有很多工具，但 **WebUI 并不会全部开放。**

当前 WebUI 实现 owner 是 `src/interfaces/webui/server.py`。`/api/tools/run` 只允许：

- `healthcheck`
- `scan_opportunities`
- `get_close_advice`

所以：
- Tool Reference 是面向 **agent/tool 调用方** 的
- 不是 WebUI 可调用范围的完整镜像

---

## 8. 字段口径

### 工具输入
- `broker`

### 历史数据字段
- `market`

Agent 工具输入统一使用 `broker`。`market` 只可能出现在历史数据表或迁移说明里，不作为新的工具 payload 字段。

---

## 9. 相关文档

- Agent 合同：[`AGENT_INTEGRATION.md`](AGENT_INTEGRATION.md)
- 快速开始：[`GETTING_STARTED.md`](GETTING_STARTED.md)
- 配置说明：[`../CONFIGURATION_GUIDE.md`](../CONFIGURATION_GUIDE.md)
