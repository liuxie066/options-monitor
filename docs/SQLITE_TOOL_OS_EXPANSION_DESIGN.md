# SQLite Tool OS 扩展设计

本文档是 OM Agent 继续扩展 SQLite Tool OS 的权威方案。目标不是再加一个
`account_income_compare` 这样的窄工具，而是把现有 `analysis_catalog` /
`analysis_query` 打造成 Agent 的受控只读分析工作区。

这条原则针对“按每个开放式分析形状新增 API”。它不否定已有或必要的任务形
诊断入口：例如单标的候选过滤原因应由 `candidate_filter_explain` 承担，
`candidate_filter_diagnostics` 则作为同一 trace 事实源上的聚合/对比/趋势 view。

相关文档：

- [OM Agent Completion Design](OM_AGENT_COMPLETION_DESIGN.md)
- [Tool Reference](TOOL_REFERENCE.md)
- [OM Agent Capability Map](OM_AGENT_CAPABILITY_MAP.md)
- [Inbound Control](INBOUND_CONTROL.md)

## 1. 结论

继续扩展 SQLite Tool OS，最应该做的是这五件事：

| 优先级 | 能力 | 作用 |
|---|---|---|
| P0 | 语义 catalog 和稳定业务 view | 让 LLM 知道有哪些业务对象、粒度、单位、公式和聚合规则 |
| P0 | SELECT-only SQLite 查询层 | 让 LLM 能通用地比较、分组、排行、归因和 join，而不是依赖窄工具 |
| P0 | Evidence v2 和 answer guard | 允许 LLM 做总结，但所有金额、比例、覆盖范围、freshness 和根因说法必须可验证 |
| P1 | bounded follow-up Agent loop | 让 Agent 观察首个查询结果后，能安全补查缺失粒度、缺失账户、空结果或 preflight 错误 |
| P1 | P2 诊断解释层 | 让候选过滤、平仓建议、runtime 推送、quote freshness 这类“为什么”问题能从证据回答 |

裁剪后的最终方案：

```text
用户问题
-> Agent 选择 analysis_catalog / analysis_query
-> Host 暴露语义 view 和安全 SQL 规则
-> Agent 写一条 SELECT/CTE
-> Host materialize 白名单 view 到内存 SQLite
-> Host preflight / execute / explain / evidence
-> Agent 观察 gap，必要时做 bounded follow-up
-> LLM 基于 evidence 写自然语言答案
-> Host 校验 claims，不安全则重试或 deterministic fallback
-> 追加稳定的数据来源、口径和缺失数据说明
```

不做：

- 不给 LLM 任意 Python、shell、物理 SQLite 表、文件系统、broker、service 或通知权限。
- 不把 `canonical`、`synthesis`、SQL mode、fact mode 做成用户可见模式。
- 不用一堆窄工具覆盖每个问题形状。
- 不让 LLM 成为数据源或账本计算源。

## 2. 背景

用户对 Agent 的期待不是“固定命令输出”，而是能处理开放式问题：

- `对比 lx 和 sy 的账户收益，有什么不同？`
- `sy 被指派正股现在亏在哪里？`
- `为什么 NVDA 没出现在候选里？`
- `今天为什么没推送？`
- `FUTU 在 lx 和 sy 的策略配置有什么差异？`

这些问题有几个共同点：

1. 不是单一工具固定格式能覆盖。
2. 需要 LLM 选择分析路径和表达结论。
3. 需要 host 保持账本、权限、计算口径和验证权威。
4. 经常需要第二步补查，比如首个结果只有账户汇总，但用户问的是来源或原因。

因此主线应该是“受控通用分析工作区”，而不是“为每个问题写一个工具”。

## 3. 成功标准

本扩展完成后，OM Agent 应该做到：

1. 对开放式分析问题，优先通过语义 view 查询，而不是关键词命中窄工具。
2. `analysis_catalog` 能解释 view 的业务含义、行粒度、主键、单位、公式、聚合规则、freshness、safe join keys。
3. `analysis_query` 只接受一条 bounded `SELECT` / `WITH`，只读白名单语义 view。
4. 查询输出包含 rows，也包含 coverage、freshness、aggregation policy、formula evidence、diagnostic evidence。
5. Agent 能在安全预算内做只读 follow-up，修复空结果、错误粒度、缺失覆盖和 repairable preflight 错误。
6. Answer guard 能拒绝不被 evidence 支持的金额、比例、覆盖范围、latest/current、root-cause 和无效聚合说法。
7. 用户看到的是一个自然 Agent 回复，不是工具回执、SQL、内部 id 或模式名。
8. LLM 不可用或答案不安全时，fallback 仍保留用户问题的任务形状。

## 4. 当前基线

当前工作区已经具备的基础能力：

- `analysis_catalog` / `analysis_query` 是公开工具名。
- `analysis_query` 是内存 SQLite SELECT-only 查询层。
- 输出保留兼容字段：`columns`、`rows`、`row_count`、`truncated`、
  `views_used`、`cell_refs`、`fallback_text`。
- AgentLoop 已经有受控 tool policy、LLM synthesis、answer guard 和 deterministic fallback。
- 写操作仍走 existing preview / confirm / apply 路径。

当前工作区已有的扩展方向：

- semantic catalog v2 元数据。
- P0/P1/P2 语义 view。
- lazy materialization。
- preflight / explain。
- first-scope evidence v2。
- bounded read-only follow-up trace。
- 部分 fallback warning 和 answer guard 校验。

仍需补齐：

- P2 诊断 view 的领域解释能力。
- 正常答案 UX 的 golden eval 锁定。
- 更完整的 formula / cross-row math verifier。
- 独立 follow-up decision schema 可作为后续兼容增强，不作为当前主阻塞。

## 5. 设计空间

### 5.1 候选方案

| 方案 | 优点 | 问题 |
|---|---|---|
| 增加窄工具，如 `account_income_compare` | 快速解决一个已知问题 | 每种问题都要新工具，无法利用 LLM 的通用规划能力 |
| 给 LLM Python sandbox | 分析能力最强 | 权限、包、IO、资源、审计和 evidence 面太大 |
| 直接开放物理 SQLite 表 | 灵活 | 暴露内部 schema，容易绕过业务口径和安全边界 |
| enrich SQLite semantic catalog | 高 ROI，能指导 LLM 正确查询 | 需要维护字段语义和聚合策略 |
| 增加稳定业务 view | 通用，且保留 host 计算权威 | 需要明确 view owner 和测试 |
| bounded follow-up loop | 更像真实 Agent，可观察后补查 | 需要预算、去重和 scope 校验 |
| evidence v2 + verifier | 让 LLM 自由表达同时保持正确性 | 需要持续扩展 verifier 模板 |

### 5.2 裁剪决策

接受：

- 继续使用 `analysis_catalog` / `analysis_query`，不另起 public tool 名。
- 用 semantic business views 作为 LLM 可见数据面。
- 用 SQLite SELECT 作为通用计算面。
- 增加 preflight、explain、coverage、freshness、formula evidence。
- 增加 bounded read-only follow-up。
- 增加 P2 诊断解释，但只读、lazy、warning-tolerant。

推迟：

- `analysis_python`：只有当 SQL 不能覆盖明确问题族，比如回归、仿真、参数扫描时再讨论。
- cross-row 复杂自然语言公式解析：先覆盖高频金额差、金额和、比例、贡献率、生命周期 PnL。
- write/apply Tool OS：继续复用现有 preview / confirm / apply。

拒绝：

- 以一堆窄工具作为主路径。
- 允许 LLM 访问物理表、任意 SQL、shell、Python、文件路径或 broker/service。
- 用户可见多模式。

## 6. 总体架构

```text
Tool registry
-> analysis_catalog
   -> semantic view specs
   -> field specs
   -> formulas
   -> aggregation policies
   -> join policies
   -> freshness/source metadata
-> analysis_query
   -> SQL validation
   -> view detection
   -> lazy materialization
   -> SQLite authorizer
   -> preflight diagnostics
   -> bounded execution
   -> query_explain + evidence
-> AgentLoop
   -> observe result
   -> detect evidence gap
   -> final / follow-up / clarification / stop_with_gap
-> EvidenceBundle
   -> facts
   -> datasets
   -> calculations
   -> coverage
   -> freshness
   -> diagnostics
-> Answer guard
   -> verify cells and derived calculations
   -> reject unsupported policy-sensitive claims
-> User answer
   -> concise natural answer
   -> deterministic source and policy lines
```

LLM 的角色：

- 选择 view 和查询策略。
- 写 SELECT/CTE。
- 观察结果并决定是否补查。
- 把 verified evidence 写成自然语言答案。

Host 的角色：

- 决定有哪些 view。
- materialize 数据。
- 维护 SQL sandbox。
- 解释 preflight 错误。
- 生成 evidence。
- 校验答案。
- 记录 audit trace。

## 7. Tool 合同

### 7.1 `analysis_catalog`

目的：告诉 planner 安全分析宇宙里有哪些业务 view、字段和规则。

输入示例：

```json
{
  "config_key": "us",
  "views": ["account_monthly_performance"]
}
```

输出必须包含：

- `schema_version`: `analysis.catalog.v2`
- `source_label`
- `views`
- `field_types`
- `aggregation_policies`
- `join_policies`
- `query_patterns`
- `anti_patterns`
- `sql_rules`

单个 view 的语义：

```json
{
  "description": "account-level monthly performance view",
  "row_grain": "month + account",
  "primary_keys": ["month", "account"],
  "time_grain": "month",
  "source_tools": ["monthly_income_report"],
  "semantic_source": "monthly_income_report.return_summary",
  "freshness": "snapshot",
  "recommended_filters": ["month", "account"],
  "safe_join_keys": ["month", "account"],
  "field_semantics": {
    "net_income_cny": {
      "type": "money",
      "unit": "CNY",
      "currency": "CNY",
      "aggregation": "sum",
      "formula": "host-owned monthly income calculation",
      "null_meaning": "source amount or FX missing"
    },
    "net_return_rate": {
      "type": "rate",
      "unit": "percent",
      "aggregation": "weighted_recompute",
      "do_not": ["avg", "sum"],
      "formula": "sum(net_income_cny) / sum(cash_secured_cny)"
    }
  }
}
```

兼容要求：

- 保留 `fields` 简单列表。
- v2 字段只 additive。
- unknown view 返回 actionable error。

### 7.2 `analysis_query`

目的：执行一条受控只读 SQL，并返回数据和验证证据。

输入示例：

```json
{
  "config_key": "us",
  "sql": "select month, account, net_income_cny from account_monthly_performance order by month, account",
  "limit": 80
}
```

约束：

- 只允许一条语句。
- 只允许 `SELECT` 或 `WITH`。
- SQLite authorizer 只能读取白名单 view。
- 拒绝 DDL、DML、`PRAGMA`、`ATTACH`、`DETACH`、非白名单函数。
- SQL 长度、输出行数、materialized 行数都有限制。

输出必须包含：

```json
{
  "schema_version": "analysis.query.output.v2",
  "source_label": "OM read-only analysis workspace",
  "query": {"sql": "...", "limit": 80},
  "preflight": {"ok": true, "warnings": []},
  "columns": [],
  "rows": [],
  "row_count": 0,
  "truncated": false,
  "views_used": [],
  "query_explain": {},
  "evidence": {},
  "cell_refs": {},
  "fallback_text": ""
}
```

`evidence` 至少包含：

- `coverage`: 查询覆盖的账户、月份、symbol、view。
- `freshness`: snapshot/realtime/artifact/stale/missing。
- `aggregation_policy`: 使用了哪些聚合，是否违反 view policy。
- `formulas` 或 `calculations`: host 可验证的派生计算。
- `diagnostics`: P2 诊断解释证据。

## 8. 语义 View 设计

### 8.1 P0：收益和被指派正股

| View | Grain | 主要用途 |
|---|---|---|
| `account_monthly_performance` | `month + account` | 账户收益、收益率、premium、realized PnL 对比 |
| `account_monthly_income_components` | `month + account + component` | 解释收益组成 |
| `assigned_stock_position_pnl` | `account + symbol + stock_lot_id` | 指派正股持仓、未实现、已实现和 lifecycle PnL |
| `assigned_stock_sale_events` | `account + symbol + stock_lot_id + sale_event` | 指派正股卖出记录和已实现 PnL |

关键原则：

- 正股成本记录真实交割价，不扣 Sell Put 权利金。
- Sell Put 权利金只进入 lifecycle PnL 归因，避免双算。
- spot 缺失时，未实现 PnL 和 lifecycle PnL 必须显式缺失。

### 8.2 P1：风险、归因、配置

| View | Grain | 主要用途 |
|---|---|---|
| `open_option_exposure` | `account + symbol + option_type + side + strike + expiration` | 当前 open option 风险、现金担保、到期暴露 |
| `expiration_risk_buckets` | `account + expiration_bucket + currency` | 到期风险桶 |
| `symbol_income_attribution` | `month + account + symbol + component` | 标的收益贡献和归因 |
| `strategy_config_by_symbol_account` | `symbol + account + strategy_family` | 比较账户/标的策略配置 |

关键原则：

- 归因 view 应由 host 整合明细，不让 LLM 手工拼 raw rows。
- rate 字段不允许直接 `avg` 表示组合收益率。
- 配置 view 只读 runtime config/watchlist，不写配置。

### 8.3 P2：诊断

| View | Grain | 主要用途 |
|---|---|---|
| `candidate_filter_diagnostics` | `run_id + account + symbol + option_type + rule` | 解释候选为何被过滤或未出现 |
| `close_advice_snapshot` | `account + position + advice_run` | 解释 recorded close advice 和 policy inputs |
| `runtime_tick_status` | `market + account + latest_run` | 解释扫描/推送是否运行、跳过、失败、无候选 |
| `quote_freshness` | `symbol + market + source` | 解释行情缺失或 stale 对计算的影响 |

P2 规则：

- P2 source 必须 lazy materialize。
- artifact 缺失不是 tool failure，应返回空 schema + warning。
- 空 rows 不等于“没有问题”；必须区分 missing source、empty artifact、no matching evidence。
- 不启动 OpenD、cron、service，不刷新 broker 数据。
- 正常用户答案不显示 artifact path、internal run id 或 trace id。

## 9. Materialization 策略

`analysis_query` 必须根据 SQL 引用的 view 加载数据，不能全量预载。

| Source family | Views | 加载规则 |
|---|---|---|
| Monthly/report | 收益、收益组成、assigned stock、symbol attribution | 仅引用相关 view 时调用 monthly report/read model |
| Position | `open_option_exposure`, `expiration_risk_buckets` | 仅引用 position view 时读取 position projection |
| Config | `strategy_config_by_symbol_account` | 仅引用 config view 时读取 runtime config |
| Artifact/runtime | P2 诊断 view | 仅引用 P2 view 时读取本地 artifact 或只读状态面 |

验收点：

- `select 1` 不 materialize 业务数据。
- 查询收益 view 不加载 P2 artifact。
- 查询 P2 view 不加载 monthly/position/config，除非 SQL 显式 join。
- meta 记录 `requested_views` 和 `materialized_views`。

## 10. Preflight And Explain

Preflight 需要把常见 SQL 错误变成可修复诊断：

| Error | Host 输出 |
|---|---|
| unknown column | `UNKNOWN_COLUMN` + suggested fields + available fields |
| unknown view | `UNKNOWN_VIEW` + suggested views |
| forbidden table | `PERMISSION_DENIED` |
| forbidden function | `PERMISSION_DENIED` |
| invalid aggregation | warning 或 reject，取决于 policy |

聚合风险：

- `avg(net_return_rate)` 默认不安全。
- `sum(rate)` 默认不安全。
- 跨账户比较收益率时，必须有 denominator context。
- rate 类组合值应重算：`sum(numerator) / sum(denominator)`。

`query_explain` 应记录：

- views used
- effective grain
- filters / coverage
- aggregations
- warnings
- truncation

## 11. Follow-Up Agent Loop

### 11.1 决策类型

| Decision | 含义 |
|---|---|
| `final_answer` | evidence 足够，进入答案合成 |
| `call_tool` | 再调用一次 allowlisted read-only tool |
| `ask_clarification` | 需要用户补范围 |
| `stop_with_gap` | gap 存在但无法安全补查 |

当前 Tool OS follow-up 只允许：

- `analysis_catalog`
- `analysis_query`

### 11.2 触发条件

| Gap | 例子 | 允许补查 |
|---|---|---|
| Empty result | 查询候选诊断无 rows | 查 `runtime_tick_status` 或问 run/symbol 范围 |
| Wrong grain | 首查只有账户汇总，但用户问来源 | 查 component / symbol attribution view |
| Missing coverage | 用户问 lx vs sy，但 rows 只有 lx | 补查缺失账户或 stop_with_gap |
| Missing freshness | assigned-stock PnL 缺 quote | 查 `quote_freshness` 或说明缺失 |
| Repairable preflight | unknown column/view 有 suggestions | 用 suggested field/view 重新查询 |

### 11.3 停止条件

- evidence 足够。
- 需要 clarification。
- gap 不可安全恢复。
- 下一步会扩大到未请求账户、symbol、月份或 market。
- 下一步是 write、service、broker、notification 或 config 操作。
- SQL 重复或语义重复。
- max tool calls / max iterations / wall-clock budget 达到。

### 11.4 Audit

Follow-up 决策需要进入 operator trace：

- schema version
- decision
- reason
- accepted/rejected/stopped
- rejected reason
- related evidence gap
- SQL fingerprint
- source observation id

正常用户答案不展示这些调试字段。

## 12. Evidence v2 And Verifier

### 12.1 Evidence 类型

`EvidenceBundle` 应从 `analysis_query` 输出里提取：

- facts：单元格事实，带 account/symbol/month/currency/source。
- datasets：查询数据集摘要。
- calculations：可验证派生计算。
- coverage：范围。
- freshness：数据新鲜度。
- aggregation policy：聚合是否安全。
- diagnostics：P2 诊断解释。
- missing data：缺失数据和影响。

### 12.2 需要优先验证的计算

| 计算 | 规则 |
|---|---|
| amount difference | 同币种、同 scope、容忍小额 rounding |
| amount sum | 同币种加总 |
| ratio | numerator / denominator，denominator 必须存在 |
| rate difference | percentage point 需要明确两个 rate |
| contribution share | component / total，total 必须存在 |
| assigned-stock lifecycle | unrealized + realized + option premium attribution |

### 12.3 必须拒绝或 fallback 的说法

- coverage 只有 lx，却说“全部账户”。
- freshness missing/stale，却说“当前/latest 已确认”。
- 只查 summary，却声称具体 root cause。
- 用 `avg(rate)` 得出组合收益率。
- 贡献率没有 denominator evidence。
- P2 rows 为空，却说“没有被过滤/没有问题”。

## 13. P2 诊断解释设计

这是下一阶段最值得做的实现切片。

### 13.1 Candidate diagnostics

问题例子：

- `为什么 NVDA 没出现在候选里？`
- `FUTU 这轮为什么没推送？`

解释状态：

| 状态 | 含义 | 用户表达 |
|---|---|---|
| `observed_rejection` | 有直接 reject/filter rule | “记录显示被 X 规则过滤” |
| `no_matching_rows` | 诊断源存在但没有匹配 symbol/account/run | “没有匹配诊断记录，不能断定没被过滤” |
| `diagnostic_missing` | artifact 或 read surface 缺失 | “缺少候选诊断源，无法判断过滤原因” |
| `read_error` | 诊断源读取失败 | “候选诊断读取失败，只能说明证据不完整” |

安全边界：

- 不能从行情或配置猜原因。
- 没有直接 rule 时，不说“原因是”。
- 可以建议用户补 run scope 或检查最新 runtime status。

### 13.2 Runtime / notification diagnostics

问题例子：

- `今天为什么没推送？`
- `扫描是不是没跑？`

需要区分：

- scheduler skip
- run failed
- no candidates
- notification disabled/skipped
- quote missing/stale
- artifact missing

表达规则：

- 有 direct status 才说“原因”。
- 只有 symptom 时说“观察到”。
- 不从这个路径启动服务或重跑 tick。

### 13.3 Close advice

问题例子：

- `哪些仓位该平，为什么？`
- `为什么 FUTU 没建议平仓？`

回答边界：

- 只解释 recorded close advice 和 policy inputs。
- 不生成新的 broker advice。
- 需要当前 exposure 时，follow-up `open_option_exposure`。
- 缺 close-advice artifact 时，明确“没有快照证据”。

### 13.4 Quote freshness

问题例子：

- `为什么指派正股 PnL 算不出来？`
- `spot 为什么缺失？`

回答边界：

- 解释哪个 symbol/source 缺 quote 或 stale。
- 解释受影响字段，例如 unrealized PnL、market value、lifecycle PnL。
- 不直接调用 broker refresh，除非用户走已有显式刷新/确认路径。

## 14. 用户答案 UX

默认格式：

```text
一句话结论。

关键依据：
- ...
- ...

数据来源：...
口径：...
缺失数据：...  # 仅必要时出现
```

规则：

- 先回答用户问题，不先展示工具回执。
- 不显示 SQL，除非用户明确要求。
- 不显示 internal id、lot id、artifact path、trace id、mode 名。
- 对收益/风险问题，始终保留账户、币种、月份、symbol scope。
- 对“为什么”问题，区分 observed cause 和 insufficient evidence。
- Fallback 也要保留问题形状，例如比较、归因、诊断，而不是退回原始长报表。

## 15. 实施阶段

### Phase 0：保留安全合同

状态：已实现，后续每次改动都必须保持。

范围：

- 保留 `analysis_catalog` / `analysis_query`。
- 保留 SELECT-only、authorizer、row cap、fallback。
- 保留 read-only tool policy。

验收：

- 现有 analysis safety tests 通过。
- 禁止 DDL/DML/PRAGMA/ATTACH/非白名单表。

### Phase 1：Semantic Catalog v2

状态：当前工作区已实现主要安全范围。

范围：

- view grain / primary keys / freshness / source / safe join keys。
- field semantics：type、unit、currency、formula、aggregation、null meaning。
- planner manifest 暴露裁剪后的语义。

验收：

- rate 字段标记 `weighted_recompute`。
- money 字段有 currency 和 aggregation。
- unknown view 有 actionable error。

### Phase 2：P0/P1 Semantic Views

状态：当前工作区已实现主要安全范围。

范围：

- account performance。
- income components。
- assigned-stock PnL / sale events。
- open option exposure。
- expiration risk buckets。
- symbol attribution。
- strategy config。

验收：

- 每个 view 有 row grain 和 source。
- 每个 view 有正常、空数据、缺失数据测试。
- planner 对开放式分析问题优先使用 semantic views。

### Phase 3：Lazy Materialization And Preflight

状态：当前工作区已实现主要安全范围。

范围：

- SQL 引用哪些 view，只加载哪些 source family。
- `select 1` 不加载业务数据。
- unknown column/view 分类并给 suggestions。
- aggregation warnings。

验收：

- lazy materialization 测试覆盖 monthly/position/config/P2。
- preflight repairable errors 被 follow-up loop 使用。

### Phase 4：Bounded Follow-Up Loop

状态：当前工作区已实现当前安全范围。

范围：

- 空结果、summary-vs-breakdown、missing coverage、preflight unknown column/view。
- duplicate/no-progress detection。
- `ask_clarification` stop。
- structured follow-up decision trace。

验收：

- 缺账户 coverage 能补查或明确 gap。
- duplicate SQL 被拒绝。
- 非 analysis tool follow-up 被拒绝。
- write/scope expanding follow-up 被拒绝。

### Phase 5：Evidence v2 And Formula Guard

状态：当前工作区已实现 first-scope 主要安全范围；后续继续扩展。

范围：

- coverage / freshness / aggregation policy。
- amount sum/difference。
- ratio。
- percentage-point difference。
- contribution share。
- assigned-stock lifecycle sum。

验收：

- 正确派生金额/比例可通过。
- 错误派生金额/比例 fallback。
- contribution claim 必须有 denominator。
- unsupported all/latest/root-cause claims 被拒绝。

### Phase 6：P2 Diagnostic Interpretation

状态：当前安全范围已实现。

范围：

- `analysis_query.evidence.diagnostics`。
- candidate/runtick/close-advice/quote 的 interpretation records。
- renderer compact warning。
- answer verifier root-cause wording guard。
- golden runtime tests。

验收：

- 候选缺失能区分 observed rejection、no matching evidence、diagnostic missing。
- 没推送能区分 skip、failure、no candidates、quote missing、artifact missing。
- close advice 只解释 recorded policy，不生成新建议。
- P2 artifact 缺失不会让 Agent 编造原因。

当前实现说明：

- `analysis_query` 会为 P2 view 输出 `evidence.diagnostics`，区分
  `observed_rejection`、`observed_close_advice`、`observed_runtime_status`、
  `observed_quote_freshness`、`diagnostic_missing`、`empty_artifact`、
  `read_error`、`no_matching_rows` 等状态。
- `EvidenceBundle` 会把 diagnostics 提升到 dataset 的 `analysis_evidence`。
- Answer guard 会在 diagnostics 缺失、空、读取失败或无匹配时，拒绝未带证据不足
  caveat 的“原因是/根因”断言。
- Deterministic `analysis_result` fallback 会用中文短提示展示候选诊断缺失、无匹配、
  读取失败、runtime skip/failure、quote freshness gap 等诊断状态。

### Phase 7：Normal Answer UX Eval Lock

状态：当前安全范围已实现。

范围：

- golden prompts：
  - account income compare
  - assigned-stock PnL
  - candidate diagnostics
  - close advice
  - runtime diagnostics
  - strategy config
- 正常答案隐藏内部细节。
- fallback table 保留任务形状。

验收：

- 不出现 `canonical`、`synthesis`、fact/analysis mode。
- 不暴露 SQL/internal ids/artifact paths。
- 结论、依据、数据来源、口径清晰。

当前实现说明：

- `_SYNTHESIS_INSTRUCTIONS` 和 `assistant.answer_evidence` composition instruction 已收敛为
  “直接结论 + 必要关键依据”，并明确禁止 SQL、tool name、raw tool receipt、
  artifact path、trace id、internal id 和 canonical/synthesis/fact/analysis mode。
- `_verify_answer_guard` 增加 normal answer UX guard。LLM 正常合成答案如果泄漏
  internal mode、`analysis_query` / `analysis_catalog`、SQL、`stock_lot_id` /
  `record_id` / `event_id` / `source_deal_id` / `position_key` / `trace_id`、
  artifact path 或强制 `事实` / `分析` 标题，会进入一次 rewrite；rewrite 仍不安全
  时走 deterministic fallback。
- `tests/fixtures/assistant_agent_eval.jsonl` 已增加六类 golden prompts：
  account income compare、assigned-stock PnL、candidate diagnostics、close advice、
  runtime diagnostics、strategy config。
- `tests/test_assistant_runtime.py` 增加内部 UX 泄漏重写测试，覆盖 LLM 首答暴露
  `analysis_query`、SQL、internal id 和 `事实` / `分析` 标题后的重写路径。
- Evidence unit inference 已修正 `stock_cost_per_share` 这类 per-share 成本字段，
  使 `USD 117.45/股` 这类正常用户表达可以被 verifier 当作货币事实校验。

## 16. 下一切片详细任务

Phase 7 已进入当前安全实现范围。后续重点从 UX 锁定转为扩展更完整的
formula / cross-row math verifier，并决定开放问题中的 rate 聚合和 debug 展示策略。

| 任务 | 文件 | 说明 |
|---|---|---|
| Golden prompts | `tests/fixtures/assistant_agent_eval.jsonl` | 已覆盖收益对比、指派正股、候选诊断、平仓建议、runtime 诊断、策略配置 |
| Normal answer policy tests | `tests/test_assistant_runtime.py` | 已断言正常合成答案不出现 SQL、internal id、mode 名、冗长工具回执 |
| Fallback UX tests | `tests/test_assistant_runtime.py` | 已覆盖 synthesis 不可用或 unsafe 时保留 analysis task-shaped fallback |
| Composer prompt tightening | `src/application/assistant/agent_loop.py` | 已强化直接结论、关键依据、隐藏内部实现的输出约束 |
| Renderer/source line review | `src/application/assistant/renderer.py` | 已保留 analysis_result fallback 的 source/policy/warning 简短输出 |

Phase 6 已实现的 diagnostics record 示例：

```json
{
  "view": "candidate_filter_diagnostics",
  "status": "observed_rejection",
  "severity": "info",
  "accounts": ["lx"],
  "symbols": ["NVDA"],
  "summary": "observed rejection by liquidity rule",
  "observed_rules": ["liquidity"],
  "answer_boundary": "observed_filter_evidence_only"
}
```

缺失时：

```json
{
  "view": "candidate_filter_diagnostics",
  "status": "diagnostic_missing",
  "severity": "warning",
  "summary": "candidate filter trace artifact is missing",
  "answer_boundary": "cannot infer filter root cause"
}
```

## 17. 测试计划

文档或纯设计改动：

```bash
git diff --check
```

Tool OS 行为改动：

```bash
python3 -m ruff check src/application/agent_tools/analysis.py src/application/assistant/evidence.py src/application/assistant/agent_loop.py src/application/assistant/answer_verifier.py src/application/assistant/renderer.py tests/test_analysis_tools.py tests/test_assistant_runtime.py
python3 -m compileall src/application/agent_tools/analysis.py src/application/assistant/evidence.py src/application/assistant/agent_loop.py src/application/assistant/answer_verifier.py src/application/assistant/renderer.py
python3 -m pytest tests/test_analysis_tools.py tests/test_assistant_runtime.py tests/test_assistant_evidence_session.py -q
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q
```

Release baseline：

```bash
python3 -m pytest
python3 scripts/release_check.py --tag v<VERSION>
python3 scripts/generate_dependency_graph.py --check
```

## 18. 风险和缓解

| 风险 | 缓解 |
|---|---|
| LLM 编造 root cause | P2 diagnostics evidence + root-cause verifier + fallback |
| SQL 灵活性导致错误聚合 | catalog aggregation policy + preflight warnings + verifier |
| P2 artifact 缺失被误解为没有问题 | diagnostics status 区分 missing/empty/no match |
| 用户看到太多内部细节 | normal answer UX policy + golden eval |
| follow-up 失控 | read-only allowlist、scope check、duplicate check、预算 |
| view 变成另一个业务计算源 | domain/read model 继续拥有计算，Tool OS 只暴露语义 view |

## 19. 开放问题

1. `avg(rate)` 先保持 warning 还是升级为 hard reject？
2. `candidate_filter_diagnostics` 在多个 run artifact 存在时，默认 latest run 还是要求用户指定 run？
3. `runtime_tick_status` 是否只来自 `runtime_status`，还是合并 scheduler/latest-run artifact？
4. `quote_freshness` 是否需要独立 quote-status read surface？
5. 正常答案中是否允许在 debug 请求下显示 SQL 和 trace id？如果允许，应只在显式 debug intent 下展示。
