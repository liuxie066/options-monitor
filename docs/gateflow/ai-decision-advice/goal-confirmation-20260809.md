# Gateflow Goal Confirmation — ai-decision-advice

- Gate: `goal confirmation`
- Work unit: `ai-decision-advice`
- Design doc: `docs/AI_DECISION_ADVICE_DESIGN.md`（v1 已确认，2026-08-09）
- Branch: `feat/ai-decision-advice`
- Status: `confirmed by user 2026-08-09`

## 目标

在现有 Sell Put / Covered Call 监控工作流中加入固定、可审计的 AI 建议层：

1. External Evidence Collector：按 4 小时调度，用 DeepSeek Responses + 原生
   `web_search` 采集公开标的外部证据，追加写入共享 JSONL，不接触账户上下文；
2. AI Decision Advice：在简报形成时基于冻结的候选快照 + 组合分布 + 开放期权
   持仓 + 最新证据生成严格 JSON 建议，经确定性校验后由 renderer 渲染中文
   `AI建议` 区块进入既有 Daily Brief / 新增候选提醒。

## 动机

当前候选完全由确定性规则产生，无法反映监管/公司事件与账户当前暴露的叠加。
AI 层的价值定位是搜索并解读增量信息、关联组合暴露，而非重复策略计算。

## 成功信号

1. `ai_decision_advice.enabled: true` 时 Daily Brief 的 Sell Put / Covered Call
   模块内出现 `### AI建议` 聚合区块；动作限于
   `keep / switch / defer / needs_review / unavailable`；
2. 外部证据按调度采集、追加写入
   `output_shared/state/ai_decision_advice/external_evidence.jsonl`，失败/超时
   不阻断回执；
3. Advice 结果写入
   `output_runs/<run_id>/accounts/<account>/state/ai_decision_advice.jsonl`，
   输入 hash 未变时复用、不重复调用模型；
4. 校验降级规则生效（引用被拒候选 / CC 跨标的 switch → `needs_review`；
   证据过期 → `unavailable`，不能 `keep`）；
5. `enabled: false` 时完全不运行、不出现 AI 区块；`enabled: true` 缺
   `DEEPSEEK_API_KEY` 时配置校验失败；
6. 设计文档第 21 节验收矩阵逐条可测。

## 非目标

- 不改 Candidate Engine 规则、排序、资金口径；
- 不自动下单、不换汇、不写券商状态；
- 不做组合级 Greeks 实时风险引擎；
- 不引入 Pi SDK；
- 不新增 Agent 工具、独立通知渠道、手动搜索/刷新入口；
- 不预建 Combo Yield / Close Advice 适配器空壳；
- 不维护用户观点/意图数据库；
- 不输出 AI 置信度；
- 远端 systemd unit 的安装与升级不属于代码 work unit，遵循既有发布/升级边界。

## 直接代码证据

- 候选快照唯一真源：`src/application/opening_candidate_snapshot.py`
  （`opening_candidate_snapshot.v1`，seal 一次写入；`ranked_opening_candidates`
  投影封存顺序，含 `candidate_id` / `rank` / `facts`）；
- Daily Brief 组装已有冻结输入加载面：
  `src/application/daily_decision_brief_service.py`
  `_load_opening_candidate_families` / `_load_portfolio_context` /
  `option_positions_context.json`；
- 开放期权持仓权威投影：ledger `list_position_lots`
  （`src/application/ledger/commands.py`）；
- DeepSeek Responses 缺口：`src/application/llm_provider_registry.py` 把
  DeepSeek 标为 `chat_completions`；`src/infrastructure/openai_responses.py`
  无 `web_search` 工具投影与结构化输出合同；`copilot/model_client.py` 只处理
  function tools；
- 调度宿主：`src/application/service_deploy.py` 的 systemd timer render 机制
  （tick timer / auto-close timer 为既有先例）；
- Prompt 片段机制先例：`src/application/copilot/scene.py`
  （清单 + 有序 Markdown 片段 + 编译 SHA-256）；
- 通知渲染入口：`daily_decision_brief_renderer.py`
  `build_daily_brief_user_view` / `_render_user_view` /
  `render_candidate_alert`；持仓当前为 Markdown 表格（`_render_user_view`
  持仓段），候选当前主显示年化（需改为持有期净收益 + 门槛年化）。

## 本轮不做的过度设计

- 不建通用 LLM 编排框架；
- 不抽象策略适配器插件系统（v1 只有 SP/CC 两个具体适配点）；
- 不做证据全文网页归档；
- 不引入新数据库（证据与 Advice 均为 JSONL 追加日志 + 可重建索引）。

## Blocking open questions

无。用户已确认按完整 v1 范围推进，接受多 slice 拆分。

## DeepSeek 顾问复核（2026-08-09，用户核实）

两个被提出的“阻断问题”均不成立：

- 原生 `web_search` 缺失已在设计文档 4.1 明确列为待实现能力，DeepSeek 官方
  Responses API 支持该能力；
- Advice 格式修复与首次调用共享账户总计 30 秒预算（文档 10 / 13.1），不存在
  两个独立 30 秒。

采纳 6 点确定性边界补充，已写入设计文档：

1. “证据覆盖完成”明确定义（6.6.1）：范围/cutoff 可审计、无执行错误、快照
   不超过 8 小时；不要求必须搜到事件；
2. Advice 开始时冻结证据索引视图（7.5），运行途中 Collector 更新不影响当轮；
3. 两次刷新之间首次出现的新标的为 `unavailable: no_evidence`（6.6.2）；
4. `needs_review` → `keep` 属于需要通知的实质变化（14）；
5. 合法零候选不调用模型、不生成投资动作，仅确定性展示（9.8）；
6. 新候选提醒属于正常回执，可承载 `unavailable`（13.1）。

验收矩阵（21）已同步以上 6 条。
