# OM Agent Intelligence Upgrade Plan

本文档定义 OM Agent 下一轮智能化升级的 1-7 阶段方案。它不是新产品名，
也不是替换现有 AgentLoop 的并行架构；它是在
[OM_AGENT_COMPLETION_DESIGN.md](OM_AGENT_COMPLETION_DESIGN.md)、
[AGENT_RELIABILITY_P0_P2_DESIGN.md](AGENT_RELIABILITY_P0_P2_DESIGN.md) 和
[SQLITE_TOOL_OS_EXPANSION_DESIGN.md](SQLITE_TOOL_OS_EXPANSION_DESIGN.md)
之上继续收敛现有实现。

产品定位保持不变：

```text
OM Agent = 嵌入 OM 运行世界的运营与策略 Agent
```

核心升级方向：

```text
Planner 统一负责理解和计划，但必须输出可验证的 TaskContract；
系统提供受控通用调查环境，并用 Policy / Coverage / AnswerVerifier
保证安全和质量。
```

## 设计原则

1. 不新增一个独立的“意图理解模块”。Planner 本来就负责理解用户目标和制定计划。
2. 不让理解结果只隐含在工具调用里。Planner 必须显式输出 `task_contract` 和
   `tool_plan`。
3. 不围绕单个问句打补丁。收益、持仓、候选、配置、运行、升级和策略问题都走同一条
   AgentLoop。
4. 不给 Agent 任意 shell / Python / 文件写能力。通用调查能力先落在 read-only
   analysis workspace、artifact/runtime inspector、dry-run/replay 和 preview operation。
5. 安全裁决独立于 Planner。Planner 可以声明 `requested_effect`，但 PolicyGuard /
   ActionPolicy 才是权限权威。
6. Slash command 和固定报表保留确定性快路径；复杂开放问题进入 agentic investigation。

## 目标架构

```text
Inbound / CLI / API
  -> command_parser / deterministic aliases
  -> AgentLoop
     -> Planner
        -> TaskContract
        -> ToolPlan
     -> PolicyGuard / ActionPolicy
     -> ToolExecution / Investigation Runtime
     -> EvidenceBundle
     -> CoverageVerifier
     -> Follow-up Planner
     -> Composer
     -> AnswerVerifier
     -> Final response + AgentSession trace
```

`TaskContract` 是 Planner 的结构化输出，不是 Planner 前的第二个理解模块。

`ToolPlan` 描述要执行的工具动作。

`CoverageVerifier` 不重新理解用户，只根据 `TaskContract` 和 `EvidenceBundle`
判断证据够不够。

`AnswerVerifier` 不只查事实，还查回答是否完成了 `TaskContract` 要求的任务形态。

## Phase 1: Eval And Trace Baseline

目标：先固定当前智能化缺口，避免只修一个样例。

范围：

- 扩展 `assistant_agent_eval` 和 focused tests。
- 覆盖任务形态：`summarize`、`analyze`、`compare`、`diagnose`、`explain`、
  `recommend`、`preview_write`。
- 覆盖业务域：income、position、candidate、config、operation、runtime、
  strategy。
- Trace 中记录 Planner 的 `task_contract`、`tool_plan`、coverage gap、
  follow-up decision、final route 和 answer verifier 状态。

验收：

- 能稳定复现“只总结不分析”“证据不足仍完整回答”“跨账户范围缺失”“诊断无原因链”
  等弱点。
- 这阶段允许 shadow-only，不改变线上回答。

## Phase 2: Planner Schema Upgrade

目标：Planner 一次性输出任务理解和执行计划。

Planner 输出结构：

```json
{
  "schema_version": "om-tool-plan-v2",
  "task_contract": {
    "schema_version": "om-agent-task-contract-v1",
    "domain": "income",
    "task_mode": "analyze",
    "requested_effect": "read",
    "scope": {
      "accounts": ["lx", "sy"],
      "months": ["2026-06"],
      "symbols": [],
      "config_keys": ["us"]
    },
    "required_answer": ["summary", "main_drivers", "source_and_policy"],
    "required_evidence": [
      "summary_metrics",
      "driver_or_breakdown",
      "source_policy"
    ],
    "answer_shape": ["conclusion", "drivers", "scope_caveat"]
  },
  "goal": "分析 2026-06 收益来源",
  "required_capabilities": ["analysis_query", "read_only"],
  "steps": []
}
```

规则：

- `task_contract` 可选兼容旧 planner 输出；缺失时由现有 deterministic
  `build_task_contract` 反推。
- Planner 不能输出 renderer、canonical、synthesis 或 response mode。
- `requested_effect` 只表达意图，不授予权限。
- AgentLoop 对 Planner 合同做裁剪、归一化和 trace 记录。

验收：

- Provider schema 接受 `task_contract`。
- 旧测试和旧 planner fixture 继续可用。
- AgentSession 可看到 Planner 声明的任务域、任务形态、证据需求和回答形态。

## Phase 3: Coverage From TaskContract

目标：让“证据够不够”由系统判断。

Coverage 规则：

| task_mode | 最低证据要求 |
|---|---|
| `summarize` | summary metrics / status rows |
| `analyze` | summary + driver / breakdown / diagnostic evidence；纯指标计算使用 key facts / formula evidence |
| `compare` | same-scope comparable rows; 账户、月份、币种和口径一致 |
| `diagnose` | observed status + anomaly / missing / conflict + direct cause evidence |
| `explain` | rule / config / source / accounting policy evidence |
| `recommend` | current state + constraints + risk premise + options; 不直接应用 |
| `preview_write` | deterministic pending operation preview + permission request |

实现边界：

- 优先使用工具 manifest / analysis view manifest 的语义能力，而不是在
  `coverage_verifier.py` 里堆每个问句的 if/else。
- 对已有高价值 gap 继续保留专门规则：account comparison、breakdown、
  assigned-stock quote、upgrade operation/receipt/release status。
- 缺口必须说明 recoverability：`analysis_query`、`analysis_catalog`、
  `option_positions_read(refresh_quotes=true)`、`operation_timeline`、ask clarification
  或不可恢复。

验收：

- `analyze` 只有 summary 时不能被当作完整。
- `compare` 缺账户、月份或同口径数据时触发 gap。
- `diagnose` 不能把 partial / stale / conflict 证据包装成确定根因。

## Phase 4: Investigation Runtime

目标：提升通用工具组合能力，接近 Claude Code 的“理解目标后可持续调查”，但保持
OM 的生产边界。

第一批调查能力：

- `analysis_catalog` / `analysis_query`：read-only semantic SQL over whitelisted views。
- artifact/runtime inspector：读取 runtime status、operation timeline、assistant trace、
  scanned-run / notification / quote freshness 摘要。
- dry-run / replay：策略、筛选、配置建议先走离线证据和 dry-run；Agent 通过
  `analysis_query.strategy_replay_read_surface` 读取 Strategy Lab / Shadow Replay 只读产物摘要。
- preview operation：写操作只生成 pending preview。

非目标：

- 不给 Planner 任意 shell。
- 不给任意 Python/dataframe 写入生产路径。
- 不让 LLM 直接刷新生产报表、写账本、发通知、操作服务或 broker-facing 数据。

验收：

- 开放式分析问题能通过 analysis workspace 做聚合、排序、对比、补查。
- 运行/升级问题能从 status 补到 operation timeline 或明确缺口。
- 策略建议能连接 Strategy Lab / replay / dry-run evidence，而不是读一个配置就下结论。
  缺 replay / dry-run 时，CoverageVerifier 只能补 `strategy_replay_read_surface`，补不到则保持证据缺口。

## Phase 5: Controlled Follow-up

目标：多工具组合由 coverage gap 驱动，而不是模型自由乱查。

规则：

- 初始 plan 仍限制 1-3 个 read-only steps 或 1 个 preview-write step。
- Follow-up 只能由 `CoverageVerifier` 的 recoverable gap 触发。
- Follow-up 工具必须在 gap allowlist 内。
- 默认最多 1-2 轮，重复查询、无关查询、越权查询直接拒绝。
- 范围不明确时 ask clarification，不猜 account / month / symbol / market。

验收：

- 缺 breakdown 只补相关 breakdown view。
- 缺 sy 覆盖只补 sy 或同 scope comparable query。
- 缺 quote freshness 只补可授权 quote refresh，补不到则明确影响。
- follow-up 决策完整写入 `AgentSession.answer_trace`。

## Phase 6: Composer And AnswerVerifier Task Completion

目标：最终回答不仅事实正确，还必须完成任务。

Composer 规则：

| task_mode | 回答要求 |
|---|---|
| `summarize` | 直接结论 + 关键数字 / 状态 |
| `analyze` | 结论 + 主要驱动 / key facts + 结构解释或公式口径 + caveat |
| `compare` | 同口径结论 + 差异 + 差异来源 / 缺口 |
| `diagnose` | 直接观测 + 原因链 + 证据边界 + 下一步 |
| `explain` | 规则 / 口径 + 来源 + 影响 |
| `recommend` | 判断 + 选项 + 风险 + 前提 + 建议动作 |
| `preview_write` | preview 摘要 + 风险 + confirmation handle |

AnswerVerifier 规则：

- 数字、比例、日期、symbol、状态必须能回溯到 EvidenceBundle。
- `analyze` 在任务要求来源、构成、表现或复盘时缺 driver 必须说明缺失，不能只复述 summary；纯指标计算不强制写成 driver 形态。
- `compare` 必须覆盖双方同口径数据或说明为什么不能比。
- `diagnose` 必须有直接原因链或明确缺口，不能外推 upstream root cause。
- `recommend` 必须带风险和前提，不能伪装成已执行动作。
- 正常用户回复不能暴露 SQL、工具名、内部 id、artifact path、trace id。

验收：

- 数字正确但任务没完成，也会被 guard 拦截、重写或 fallback。
- 缺证据时回答转成“能确认什么 / 不能确认什么 / 下一步需要什么”。

## Phase 7: Action Lifecycle And Release Gate

目标：把 Agent 从“会答”推进到“能安全办事并证明办成”。

Action lifecycle：

```text
preview -> confirm -> execute -> verify -> audit
```

范围：

- 配置变更、手工成交记录、模型切换、升级、修复类操作继续走 pending operation。
- confirm / cancel / apply 只由确定性命令处理。
- 执行后必须有 readback / runtime_status / operation_timeline / config validate 等证据。
- 策略研究输出保持 advisory-only，不能直接修改生产配置。

Release gate：

- Focused tests 覆盖 planner schema、coverage、follow-up、answer verifier 和 eval。
- `python3 scripts/release_check.py`。
- Full `pytest` 视 release 风险执行。
- code review 先列发现，修复后提交和推送。

验收：

- 所有写-like 请求只产生 preview 和 permission request。
- 用户确认前不改配置、不写账本、不发通知。
- 执行后能通过 trace 解释：做了什么、为什么做、证据是否足够、最终是否验证通过。

## Implementation Slices

建议按以下 PR / commit 切片执行：

1. Eval + trace baseline。
2. Planner schema 接入 `task_contract`，旧输出兼容。
3. `TaskContract` 增加 `domain`、`task_mode`、`requested_effect`、
   `required_evidence`、`answer_shape`。
4. CoverageVerifier 消费 `required_evidence` 和 `task_mode`，扩展 summary-only
   analysis gap。
5. Follow-up contract 改为 gap allowlist 触发，不扩大安全面。
6. Composer / AnswerVerifier 按 `task_mode` 检查任务完成度。
7. Action lifecycle readback / release gate 验收。

本文档的短期实现目标是 1-6 的 read-only 智能化主线；Phase 7 保持现有
pending-operation 权限权威，并补足 trace / verify / eval，而不扩大写权限。

当前落地切片：

- `analysis_catalog` 暴露 `investigation_recipes`，让 Planner 能把任务契约和证据缺口映射到
  `analysis_query`、`operation_timeline`、`assistant_trace` 等通用调查工具。
- Planner schema / trace 已接入 `selected_recipe`；新 planner 可显式声明 recipe，旧 planner
  缺省时 runtime 会按 `task_contract` 推导，并写入 AgentSession plan revision。
- preview / confirm / cancel / readback 的 AgentSession trace 携带 `action_lifecycle`，
  `operation_timeline` 也输出 phase / verify status，方便执行后用通用读回证据解释闭环。
- CoverageVerifier 已消费 `selected_recipe.evidence_needs`：收益分析 recipe 会要求拆解/driver 证据，
  operation readback recipe 会触发受控 `operation_timeline` follow-up，策略 replay recipe 在缺少
  replay / dry-run 读面时保持不可恢复证据缺口。
- 仍保留受控 follow-up 和 pending-operation 权限边界；未给 Planner 任意 shell、任意 Python
  或直接生产写权限。
