# Gateflow Goal Confirmation — ai-decision-advice-account-context

- Gate: `goal confirmation`
- Work unit: `ai-decision-advice-account-context`
- Branch: `fix/ai-decision-advice-account-context`
- Base: `origin/main@0d635e116ab46c1caafb2a93f325fc23a992c27d`
- Design authority: `docs/AI_DECISION_ADVICE_DESIGN.md`
- Status: `confirmed; design contract updated; ready for plan`

## Confirmed goal

把 AI Decision Advice 的账户上下文收紧为一条可审计的数据拼接路径，并验证实现没有把
不同职责或不同账户的数据混在一起：

1. Candidate Engine 继续按当前 OM 账户配置使用 Futu 或 holdings 运营上下文，独立负责
   合格候选、现金、持股和 Covered Call 容量；
2. AI 战略组合分布无论账户运营类型为何，都只使用显式启用、按
   `holdings_account` 映射后单账户读取的 portfolio-management distribution；
3. 开放期权只使用 OM SQLite ledger 对当前 OM 账户生成的 prepared projection；共享
   SQLite 文件不意味着共享逻辑持仓；
4. 每张确定性投影只拼接同一 run、同一 OM 账户的候选、PM 分布和 prepared option
   context；Sell Put 总市值分母、Covered Call 持股分母来自 PM，已有期权叠加来自 ledger；
5. PM、期权或 binding 不完整时显式失败关闭，不使用 Futu、默认账户、其他账户或 legacy
   文件补齐，但 Candidate Engine 和原始监控回执继续运行。

用户已确认先更新设计文档，再更新 Gateflow 实施方案，最后才进入代码阶段；并已确认
从 `origin/main` 创建独立 worktree/分支，保留原 main 工作区的既有未提交改动不动。

## Success signals

1. 设计文档有唯一、集中且不互相矛盾的账户级来源矩阵和投影分母/范围规则；
2. Futu 运营账户启用 PM 时，测试证明 Candidate 运营来源与 Advice 战略组合来源不互相
   替代，PM 请求只使用当前账户映射后的 `holdings_account`；
3. 两个 OM 账户共用一个 SQLite ledger 时，测试证明各自 Advice 只看到本账户的开放期权，
   汇总和投影都没有跨账户张数；
4. 同一候选分别配不同账户 PM 总市值、持股数量和期权仓位时，Sell Put 暴露比例、
   Covered Call 叫走比例及到期/义务叠加只使用当前账户事实；
5. run/account/config binding 不匹配、PM unavailable 或 option unavailable 时，不产生
   fallback，投影产生明确 gap，动作上限保持 `needs_review`；
6. 聚焦测试、相关账户/Tick/Advice 集成测试、静态检查和最终 deepreview 通过；
7. 不触发真实 PM、OpenD、DeepSeek、通知、生产配置写入、发布或远端升级。

## Non-goals

- 不修改 Sell Put / Covered Call 的召回、硬门槛、排序、收益率、限价、资金或容量口径；
- 不改变用户已经确认的一张合约投影公式，不新增行业、Greeks、综合风险分或数量建议；
- 不把 portfolio-management 变成 OM 的必装依赖，也不修改 PM 仓库、数据库或 OpenAPI；
- 不把不同 OM 账户合成一个综合 Advice；
- 不修改 External Evidence Collector、DeepSeek schema、搜索归因、Prompt 或来源渲染；
- 不修改 Combo Yield、Close Advice、option ledger 事件或真实持仓；
- 不修改生产配置，不调用真实外部服务，不发布、不升级远端。

## Current code evidence

- `src/application/prepared_portfolio_distribution.py`
  - `prepare_portfolio_distributions()` 已逐 OM 账户处理；
  - `_mapped_pm_account()` 复用 `resolve_holdings_account()`；
  - PM 响应与 prepared envelope 已绑定 OM account、mapped PM account、run、config hash。
- `src/application/prepared_option_positions_context.py`
  - `prepare_option_positions_contexts()` 可对共享 ledger 一次读取后生成逐账户 context；
  - `load_prepared_option_positions_context()` 校验 manifest、payload hash、run、account 和
    account config hash；
  - `_validate_option_context_account()` 拒绝其他账户的期权行。
- `src/application/ai_decision_advice/contexts.py`
  - `build_frozen_inputs()` 从 candidate snapshot 取得 run/account/config authority；
  - PM 和 option freeze 都以同一 authority 校验；
  - 投影只接收 PM 派生总市值/持股与当前账户 option projection rows。
- `src/application/ai_decision_advice/projection.py`
  - Sell Put 使用 `strike * multiplier * CNY FX / PM total value`；
  - Covered Call 使用 `multiplier / PM held shares`；
  - 已有义务和期限叠加只读取传入的账户级 option rows。
- `src/application/tick_notification_flow.py` 与
  `src/application/daily_decision_brief_service.py` 已按当前 account 从 authority maps 选择对象，
  再交给 Advice；delivery-only 不消费这些输入。

## First-principles assessment

当前主路径已经基本符合刚确认的策略，缺口首先是合同分散、缺少一个把“Futu 运营来源、
PM 战略分布、共享 ledger 的账户切片、投影分母”同时钉死的端到端回归。最高 ROI 是补齐
这个跨边界回归，并只在测试证明存在真实实现漂移时修复 owning boundary；不新增第二套
账户实体、context assembler、fallback 或配置项。

## Blocking open questions

无。下一 gate 为形成可执行 plan，并由 `planreview` 审查范围、反例和最小实现。
