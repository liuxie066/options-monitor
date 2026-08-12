# AI Decision Advice 退役记录

> 状态：已从当前产品与源码中退役（2026-08-12）

AI Decision Advice 的模型生成、联网资讯采集、组合分布准备、配置入口、托管 Collector
service/timer，以及 Daily Brief / Agent 当前展示面均已删除。配置中出现
`ai_decision_advice` 会被明确拒绝，防止旧配置静默恢复已退役能力。

## 保留边界

- Candidate Engine 继续拥有 Sell Put、Covered Call 与 Combo Yield 的资格判断和排序。
- Daily Brief 继续使用确定性的候选、持仓、资金、事件、拒绝原因和 Close Advice 事实。
- `prepared_option_positions_context`、opening candidate snapshot、SQLite ledger 和通用
  Assistant LLM provider/credential 保持不变；它们不是 AI Decision Advice 的替代实现。
- 历史 Brief、正式 Advice、外部证据和审计文件不在本次源码变更中删除或重写。
- 尚未确认送达、且冻结内容包含旧 AI Advice 的历史通知会 fail closed，不会在模型功能
  退役后继续发送；干净账户仍可独立发送当前确定性报告。

## 生产切换边界

源码合并或发布不会自动改变已安装服务或删除运行数据。生产切换必须另行取得授权，并使用
受控 service drift/reconcile 流程停用和移除旧 Collector unit；历史数据清理也必须作为单独、
可恢复、明确指定目标的操作执行。DeepSeek 逻辑凭据仍可被通用 Assistant 使用，本次退役不
删除任何密钥。
