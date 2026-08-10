# Gateflow Goal Confirmation — ai-decision-advice-drift-remediation

- Gate: `goal confirmation`
- Work unit: `ai-decision-advice-drift-remediation`
- Design doc: `docs/AI_DECISION_ADVICE_DESIGN.md`
- Predecessor: `docs/gateflow/ai-decision-advice/`（已验收历史工件，不回写）
- Branch: `feat/ai-decision-advice`
- Status: `confirmed by user 2026-08-09`

## 目标

修复 AI Decision Advice 已实现代码与已确认策略之间的漂移，使每个账户的建议只消费
该账户、同一 run 的权威输入，并把一张合约的确定性边际影响真正纳入模型输入和事实
引用：

1. 组合分布：可选接入 portfolio-management（PM）的账户级资产分布；PM 未安装、
   未配置、失败或不可信时显式降级，绝不由 Futu 账户持仓冒充全组合分布；
2. 开放期权：只消费 OM SQLite ledger 生成并验证的同 run、同账户 prepared context；
   合法空持仓与数据不可用严格区分；
3. 风险投影：固定按新增一张合约计算 Sell Put 指派资金暴露、Covered Call 可被叫走
   比例、同义务叠加和到期日集中，并作为 Advice 可引用的确定事实；
4. 外部证据、动作校验和通知：补齐观察集合、账户相关证据冻结、完整 scope、事实引用、
   变化通知及内部 collector 入口等已经确认但尚未完整落地的合同。

## 动机

当前实现可能把错误形态或空对象当作完整上下文；期权载荷字段不匹配时甚至会把真实
开放仓位投影成空列表。一张合约投影尚未接入 Advice，Collector 的生产观察集合也只
包含配置标的。这些问题会让模型在缺少真实账户暴露的情况下输出看似完整的建议，违背
本功能“资讯 + 组合 + 期权持仓共同决策”的核心价值。

## 成功信号

1. PM provider 只有显式配置为 `portfolio_management` 才启用；按 OM 账户映射后的
   `holdings_account` 单账户查询，产出同 run、账户绑定且可校验的 prepared distribution；
2. PM `fresh + trusted` 可完整使用；`stale + trusted` 或 `partial` 最多
   `needs_review`；`unknown / untrusted / unavailable` 不向模型发送资产行且最多
   `needs_review`；合法零资产仍是完整空组合；
3. Advice 不再重读或猜测 legacy context 文件，只消费已经验证的 prepared portfolio
   distribution 与 `prepared_option_positions_context`；账户、run、配置 hash 或 manifest
   不匹配均 fail closed；
4. 有效 `open_positions_min: []` 表示真实空期权持仓；字段缺失、ledger 失败或不可信
   不能被解释为空；所有期权事实严格限于当前 Advice 账户；
5. 所有开放期权参与确定性汇总；候选标的获得合约明细及新增一张后的事实，其他标的只
   进入账户汇总；可靠 combo identity 才能形成结构关系，不能猜配对；
6. 每个候选都产生可引用投影：Sell Put 的一张指派资金占当前组合市值比例、Covered
   Call 的一张可叫走股数占当前持股比例、同义务张数、同到期及前后 7 日张数；
7. `keep` 仅在候选、PM 组合、期权持仓和相关外部证据全部完整时成立；内部事实可独立
   支持 `switch / defer`；输出 scope 缺失、重复或多余时整账户 Advice unavailable；
8. Collector 实际观察配置标的、PM 持仓、开放期权底层和最近候选，公开证据跨账户按
   symbol 去重，Advice 只冻结本账户相关标的；公共 `./om ai-evidence-collector` 被移除，
   managed service 仍可调用内部入口；
9. 设计、配置、运行工件、通知和 Agent 读取面一致；聚焦测试、全量测试与 Gateflow
   review 均通过，不修改 Candidate Engine 的正式策略结果。

## 已确认的账户与来源边界

- Advice 与监控均按 OM 账户分别运行；PM 分布和期权持仓禁止跨账户合并后再传模型。
- Futu 是券商账户的现金、股票和可卖容量来源，继续服务 Candidate Engine；它不是
  AI Advice 的全组合战略分布真源。
- PM 属于独立项目，不是安装必需项。配置默认 provider 为 `none`，不得因本机恰好存在
  PM 服务就自动启用。
- PM 地址复用 loopback-only `PORTFOLIO_SERVICE_URL`；账户映射复用
  `account_settings.<account>.holdings_account`，未配置时使用 OM 账户标签。
- PM 查询固定为单账户：
  `/api/v1/distribution?account=<mapped>&by_asset=true&include_value=true&group_cash=false`。
- `group_cash=false` 用于保留现金与货币基金的币种结构；OM 在校验后本地计算
  `asset_weights`、`currency_weights` 和 `cash_and_mmf_weight`。
- 开放期权唯一真源是 OM SQLite ledger 的账户级 projection；Feishu、Futu 和 legacy
  JSON 均不是 Advice 的 fallback。

## 非目标

- 不修改 Sell Put / Covered Call 的召回、硬门槛、排序、收益、限价、容量或资金口径；
- 不把投影升级为 Candidate Engine 新硬门槛，不推荐数量，不自动下单；
- 不伪造指派或叫走后的组合权重，不建设组合级 Greeks 或统一风险分数；
- 不引入行业集中度；
- 不做跨账户综合 Advice；
- 不要求所有 OM 用户安装 PM，也不在 OM 内复制 PM 数据库；
- 不把 PM 的账户、broker、绝对 NAV、成本或 breakdown 发送给模型；
- 不新增手动搜索/刷新入口或 Agent 写工具；
- 不改 Combo Yield / Close Advice 行为；
- 不发布、不升级远端、不修改生产配置。

## 直接代码证据

- `src/application/ai_decision_advice/orchestration.py` 仍重读
  `portfolio_context.json` / `option_positions_context.json`，并用 `bool(dict)` 判断完整性；
- 同文件 `_option_lots_from_context()` 不识别权威字段 `open_positions_min`；
- `src/application/ai_decision_advice/contexts.py` 的期权冻结只识别嵌套
  `contract_key`，与 prepared context 的扁平行合同不一致；
- `src/application/ai_decision_advice/projection.py` 的投影没有生产调用点，且到期和同义务
  叠加按行计数，只检查完全相同到期日；
- `src/application/prepared_option_positions_context.py` 已提供同 run、账户、配置 hash、
  manifest hash、`context_status=available`、`decision_snapshot_status=trusted` 和
  `open_positions_min` 的严格验证边界；
- `src/infrastructure/portfolio_management_client.py` 已有 loopback-only、API version-aware
  PM client 和 `distribution` view，但尚无 Advice 专用严格响应校验；
- PM `/api/v1/distribution` 已返回账户列表、`by_asset`、freshness/trust 与观察时间；
  OM 必须校验并重新计算权重，不能直接相信上游 ratio；
- `src/interfaces/cli/ai_evidence_collector.py` 当前将普通持仓、开放期权和最近候选均传
  空列表，且 `src/interfaces/cli/main.py` 仍公开分发 `ai-evidence-collector`；
- `src/application/ai_decision_advice/validation.py` 当前没有完整 scope 集合与严格 frozen
  fact registry 合同。

## 工作区所有权说明

目标确认期间，先前的 `tests/run_smoke.py`、`src/application/service_deploy.py` 与
`tests/test_service_deploy.py` 改动已由其所有者分别独立提交为 `22d8ac9d` 和
`437505a3`。当前工作区除本 work unit 的设计与 Gateflow 工件外已清洁。后续若移除
public Collector CLI 需要继续修改 service deployment，仍应只提交本 work unit 的
可审计改动，并在每个 slice 提交前核对 staged diff。

## Blocking open questions

无。以上边界均已由用户逐项确认；下一 gate 为更新设计文档，再形成 remediation plan。
