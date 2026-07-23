# Gateflow Goal Confirmation — Copilot Option Performance MTD

- Work unit: `copilot-option-performance-mtd`
- Gate: `goal confirmation`
- Date: 2026-07-23
- Status: confirmed by user
- Branch: `fix/copilot-option-performance-mtd`
- Isolated worktree: `/private/tmp/options-monitor-copilot-option-performance-mtd`
- Base: `origin/main@0db40d50` (`v1.4.14`)

## User problem

线上飞书 Copilot 回答“7月 MTD 的期权收益”时，主报告工具在执行前因隐藏的
`null` 默认值和互斥期间参数冲突而失败，随后回退到通用 SQL，只返回单账户的
`realized_gross`。结果没有完整说明：

- MTD 是否存在以及截止时点；
- premium activity、期权现金、指派结算本金、指派股票卖出现金；
- 实现利润是否混合了纯期权与指派股票；
- 哪些数字包括指派、哪些不包括；
- 查询账户范围和证据缺口。

## Confirmed goal

优化 canonical `option_performance_report` 与 Copilot 适配边界，使期权收益查询首先
得到合法、可审计、口径清楚的 MTD 报告，并在回答中显式拆分纯期权和指派股票的
利润与现金流。

## Success criteria

1. “7月 MTD 的期权收益”首个 `option_performance_report` 调用可执行成功。
2. MTD 请求只携带合法相关字段，不带 YTD/month/year/range 冲突字段，也不带隐藏
   `config_path:null`、`data_config:null`。
3. canonical 报告同时保留总实现利润，并新增纯期权与指派股票实现利润拆分；不靠
   展示层反推。
4. 回答明确展示 premium、option cash、assignment settlement principal、
   assigned-stock sale cash、相关费用和缺失证据。
5. 指派是否计入、计入哪个利润/现金字段、账户范围、期间状态均显式可见。
6. 未指定账户时聚合全部可用账户；只有当前请求或明确会话范围给出账户时才缩小，
   且回答必须展示实际范围。
7. 线上失败对话被固定为 deterministic regression/eval，聚焦测试与相关基线通过。

## Non-goals

- 不回填历史 FX、费用、报价或其他缺失证据；
- 不改变会计确认原则、ledger 事件、持仓投影或收益期间定义；
- 不修改生产 config、通知行为、Feishu 数据、broker-facing 数据或真实持仓；
- 不恢复已退役的 monthly income/capital bridge 工具；
- 不新增平行期权收益模块，不用关键词硬编码替代工具选择；
- 本 work unit 不包含 release、部署或生产写入。

## Preflight decision

原工作区存在与通知体验相关的未提交改动。用户确认使用隔离 worktree，因此本工作
单元从 `origin/main@0db40d50` 建立独立分支；原工作区不修改、不暂存、不清理。
