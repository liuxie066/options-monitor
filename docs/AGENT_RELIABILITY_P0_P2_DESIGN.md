# OM Agent Reliability P0-P2 Design

本文档定义 OM Agent 从当前实现继续演进的 P0-P2 方案。目标不是照搬
Claude Code 的完整产品形态，而是把 OM 现有 `AgentLoop`、Tool OS、
`EvidenceBundle`、answer verifier 和 preview/confirm 路径整理成一条更可靠的
主线。

相关文档：

- [OM Agent Completion Design](OM_AGENT_COMPLETION_DESIGN.md)
- [SQLite Tool OS Expansion Design](SQLITE_TOOL_OS_EXPANSION_DESIGN.md)
- [Tool Reference](TOOL_REFERENCE.md)
- [Inbound Control](INBOUND_CONTROL.md)
- [OM Agent Capability Map](OM_AGENT_CAPABILITY_MAP.md)

## 1. 结论

OM Agent 后续按四段主线组织：

```text
User
-> Plan
-> Act
-> Verify
-> Answer
```

其中：

| 阶段 | 责任 | 内部机制 |
|---|---|---|
| Plan | 明确用户问题、范围和需要回答的点 | `TaskContract`、planner、工具计划 |
| Act | 安全执行工具 | `ActionPolicy`、pre-check、tool execution、post-check |
| Verify | 判断证据是否足够且可信 | `EvidenceBundle`、coverage verifier、fact verifier、gap |
| Answer | 生成并验收用户回复 | LLM synthesis、answer verifier、rewrite、fallback、ask |

P0-P2 的切分：

| 阶段 | 目标 | 主要收益 |
|---|---|---|
| P0 | 任务契约 + 覆盖验证 + 回答验收 | 减少答非所问和无证据总结 |
| P1 | 统一工具执行边界 + ActionPolicy + manifest 扩展 | 减少工具路径分叉和权限/回执不一致 |
| P2 | 诊断推理、动作安全分类、可读 trace 和 eval 扩展 | 让 Agent 更聪明，但仍可审计、可回退 |

不做：

- 不增加用户可见的 `Permission Profile` 模式。
- 不新增第二套工具注册表。
- 不新增第二套 pending operation store。
- 不让 LLM 拥有账本计算、写入确认、服务操作或 broker-facing 权限。
- 不把 `canonical` / `synthesis` / SQL mode 暴露给用户。

权限控制使用每次工具调用的即时 `ActionPolicy` 判定，而不是会话级模式分叉。

## 2. 当前基线

当前 OM 已经具备以下能力：

- `AgentTool` 声明 `read_only`、`side_effects`、`risk_level`、
  `requires_confirm`、`answer_policy`、`output_contract`。
- `AgentLoop` 有 bounded plan / tool call / iteration 预算。
- `analysis_query` 是 SELECT-only SQLite Tool OS，只能访问白名单 view，并输出
  `query_explain`、`evidence`、`cell_refs`、`fallback_text`。
- `EvidenceBundle` 可以从工具 observation 和 output contract 提取 facts、
  datasets、calculations、missing_data、conflicts、guard_contracts。
- `answer_verifier` 可以检查金额、比例、数量、日期、symbol、status 是否有证据。
- answer guard 会拦截内部 id、SQL、tool name、artifact path、强制
  `事实/分析` 分裂和与工具覆盖矛盾的回答。
- preview/write/admin 路径已有 pending operation、confirm/cancel/apply 和
  `permission_request` 对象。

主要缺口：

1. Planner 有工具计划，但“用户必须得到哪些答案”还不够结构化。
2. Coverage verifier 还不够通用，容易出现用户问“对比 A/B”，回答却只给全部摘要。
3. 工具执行路径尚未形成统一 `ToolExecutor`，pre/post 校验分散在工具、policy、
   answer guard 和 deterministic handler 中。
4. `output_contract` 仍偏工具自定义，缺统一 annotations、schema、verifier 声明。
5. Trace 已经存在，但还不能稳定解释每次回答的 plan、evidence gap、verifier 和
   fallback 原因。

## 2.1 Relationship To Existing Agent Design

本文档不是替代 [OM Agent Completion Design](OM_AGENT_COMPLETION_DESIGN.md)，而是它的
P0-P2 执行路线图。

文档分工：

| 文档 | 职责 |
|---|---|
| `OM_AGENT_COMPLETION_DESIGN.md` | 完整受控 Agent 的目标架构、Session、Evidence、loop 和权限边界 |
| `SQLITE_TOOL_OS_EXPANSION_DESIGN.md` | `analysis_catalog` / `analysis_query` 的语义 view、SQL 安全和 evidence 扩展 |
| 本文档 | 下一步 P0-P2 的开发顺序、兼容策略、验收门槛和裁剪边界 |

执行原则：

1. 源码事实优先于文档描述。
2. 本文档只定义 P0-P2 的落地顺序，不新增第二套 Agent 架构。
3. 与现有设计重叠时，保留现有公共入口和数据存储，只在内部模型上增量扩展。
4. `AgentSession`、`EvidenceBundle`、pending operation store、tool registry 继续沿用
   现有实现。
5. `Plan / Act / Verify / Answer` 是实现切分，不是用户可见模式。

## 2.2 Compatibility And Rollout

P0-P2 必须渐进发布。任何阶段失败时，都应能退回当前 AgentLoop 行为。

兼容规则：

| 组件 | 兼容策略 |
|---|---|
| Planner schema | `task_contract` 是 additive；旧 `om-tool-plan-v1` 仍可解析 |
| `response_mode` | 迁移期保留内部兼容，不作为产品概念继续扩展 |
| Tool manifest | 新增 annotations / schema / evidence contract 时保留旧字段 |
| Tool output | 新 verifier 缺失时按当前 output contract 和 renderer 逻辑处理 |
| AgentSession | 新增字段写入 snapshot，但旧 trace reader 必须能忽略未知字段 |
| Answer verifier | P0 增量加入 required-answer 检查；失败时仍可走现有 fallback |

建议开关：

| 开关 | 默认 | 作用 |
|---|---|---|
| `OM_AGENT_TASK_CONTRACT_ENABLED` | off during dev, on after P0 tests | 启用 TaskContract 和 coverage verifier |
| `OM_AGENT_TOOL_EXECUTOR_ENABLED` | off during dev, on after P1 tests | AgentLoop 工具调用走统一 ToolExecutor |
| `OM_AGENT_ACTION_SAFETY_ENABLED` | off | 启用 P2 action safety classifier |

开关不代表用户模式，只是 rollout / rollback 保护。线上稳定后可移除或默认开启。

发布顺序：

1. P0 先以 trace-only 方式生成 `task_contract` 和 coverage result，不改变用户回复。
2. P0 trace 稳定后，把 coverage verifier 接入 synthesis 前置判断。
3. P1 先让 ToolExecutor 包 read-only 工具，再迁移 preview 工具。
4. P2 先只增加 trace / eval，再让 action safety classifier 影响执行。

回退策略：

- 新模型解析失败：忽略新字段，继续现有 tool plan。
- Coverage verifier 抛错：记录 trace warning，走现有 answer guard。
- ToolExecutor 抛出非业务异常：记录 trace warning，回退直接调用现有 handler。
- Action safety classifier 异常：按保守 deny 或关闭开关，不能放宽写权限。

## 3. 成功标准

完成 P0-P2 后，OM Agent 应做到：

1. 用户问开放式问题时，Agent 能明确 task scope 和 required answer。
2. 任何最终回答都能说明：答了哪些 required answer，哪些因为证据缺失没有答。
3. 金额、比例、数量、日期、symbol、状态、quote freshness 和 root-cause 结论都能
   回溯到 evidence 或明确标记为缺失。
4. 工具调用前有统一的 action decision，工具调用后有统一的 result verification。
5. 写操作只产生 preview / permission request；apply 仍由确定性 confirm/apply
   路径执行。
6. LLM 回答失败时，fallback 保留用户问题的任务形状，而不是退回某个原始长工具回执。
7. 线上问题可通过 `assistant_trace` 看到 plan、tool transcript、evidence gap、
   answer verifier 和 final route。

## 4. P0: Task Contract And Coverage Verification

### 4.1 目标

P0 解决“答不准”和“答非所问”。

重点不是增加新工具，而是让系统在回答前知道：

- 用户真正问什么。
- 需要覆盖哪些账户、月份、标的、指标、数据新鲜度。
- 最终回答必须包含哪些结论。
- 缺失证据时应该继续查、追问，还是 fallback。

### 4.2 TaskContract

新增内部模型 `TaskContract`，由 planner 生成，随 `AgentSession` 记录。

建议结构：

```json
{
  "schema_version": "om-agent-task-contract-v1",
  "goal": "对比 lx 和 sy 的账户收益差异",
  "intent_family": "analysis.compare",
  "scope": {
    "config_key": "us",
    "accounts": ["lx", "sy"],
    "symbols": [],
    "period": "all_available",
    "currencies": []
  },
  "required_answer": [
    {
      "key": "comparison_winner",
      "description": "说明 lx 和 sy 谁的收益更高",
      "required": true
    },
    {
      "key": "amount_difference",
      "description": "给出收益金额差额",
      "required": true
    },
    {
      "key": "rate_difference",
      "description": "给出收益率差异；如口径不支持，说明原因",
      "required": true
    },
    {
      "key": "main_drivers",
      "description": "说明主要差异来源；证据不足时明确缺失",
      "required": false
    },
    {
      "key": "source_and_policy",
      "description": "追加数据来源和口径",
      "required": true
    }
  ],
  "required_evidence": [
    "account_monthly_metrics",
    "same_period_coverage",
    "currency_policy",
    "calculation_formula"
  ],
  "requested_effect": "read",
  "ambiguities": []
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `goal` | 用户可读目标，不是内部工具名 |
| `intent_family` | 粗粒度意图，例如 `analysis.compare`、`position.pnl`、`ops.status` |
| `scope` | 账户、月份、标的、配置 key、币种等范围 |
| `required_answer` | 最终回答必须覆盖的点 |
| `required_evidence` | 覆盖验证需要看到的证据类别 |
| `requested_effect` | `read`、`preview`、`apply_confirmation` 等动作意图，不是 permission profile |
| `ambiguities` | 无法安全推断的范围，用于 ask clarification |

### 4.3 Contract 生成策略

P0 不需要一次性支持所有自然语言。先覆盖高价值问题族：

| 问题族 | 示例 | Required answer |
|---|---|---|
| 账户收益对比 | `对比 lx 和 sy 的账户收益，有什么不同` | 双方指标、谁更高、差额、收益率差、来源 |
| 收益组成 | `6 月收益主要来自哪里` | 汇总、组成、top contributors、口径 |
| 指派正股盈亏 | `sy FUTU 指派正股亏多少` | 剩余股数、成本、spot/freshness、浮盈亏、生命周期 PnL |
| 缺失/异常解释 | `为什么 0700 没有浮盈亏` | 缺失字段、影响、恢复路径 |
| 升级状态 | `刚才升级成功了吗` | command id、状态、当前版本、目标版本、日志/缺失 |

Contract 来源优先级：

1. Deterministic parser 从显式账户、标的、日期、动词提取 scope。
2. Planner LLM 补充 `goal` 和 `required_answer`。
3. Host 校验并裁剪 planner 输出，只保留允许字段。
4. 无法确定 account/period/symbol 时记录 `ambiguities`，不让 LLM 猜。

### 4.4 Coverage Verifier

新增 `CoverageVerifier`，输入 `TaskContract + EvidenceBundle`，输出：

```json
{
  "schema_version": "om-agent-coverage-v1",
  "status": "complete",
  "satisfied": ["comparison_winner", "amount_difference", "source_and_policy"],
  "missing": [],
  "gaps": [],
  "next_action": "answer"
}
```

状态：

| 状态 | 含义 |
|---|---|
| `complete` | 证据足够回答 |
| `recoverable_gap` | 当前证据不足，但可用只读工具补齐 |
| `need_user` | 缺用户范围或确认 |
| `unrecoverable_gap` | 当前工具无法补齐，回答时必须说明缺失 |
| `unsafe` | 证据冲突或口径不允许回答 |

典型 gap：

| Gap | 场景 | 后续动作 |
|---|---|---|
| `missing_account_coverage` | 用户问 lx/sy，但 evidence 只有 lx | follow-up query 补 sy |
| `missing_period_coverage` | 用户问 2026-06，但 evidence 无该月 | follow-up 或说明缺失 |
| `missing_breakdown` | 用户问来源，但只有汇总 | follow-up 查 components / attribution |
| `missing_quote` | 需要实时浮盈亏但 quote 缺失 | refresh quote 或说明不能算 |
| `invalid_rate_aggregation` | 平均收益率口径无效 | 改用合计分子/分母或说明 |
| `missing_command_status` | 升级回执缺 command id 或日志 | 查 operation/audit 状态 |

### 4.4.1 Accounting And Freshness Coverage Rules

Coverage verifier 必须理解 OM 的核心口径，避免看似有数据但实际不可比。

账户收益 / 现金流：

| 规则 | 说明 |
|---|---|
| 同币种金额可直接比较 | `HKD` 对 `HKD`、`USD` 对 `USD` 可比较 |
| 跨币种必须有归一口径 | 没有 `CNY` / `HKD` 归一字段或汇率 evidence 时，不能直接说总额谁高 |
| rate 不能简单平均 | 账户收益率必须来自同一分子/分母口径，不能平均月收益率或账户收益率 |
| cashflow 和 realized PnL 分开 | 净现金流、已实现 PnL、权利金、生命周期 PnL 是不同指标 |
| 生命周期归因要说明来源 | 指派正股、权利金归因、正股浮盈亏必须分清楚 |

持仓 / 指派正股：

| 规则 | 说明 |
|---|---|
| 正股成本按交割价 | 不扣 Sell Put 权利金；权利金只进入生命周期 PnL |
| 当前浮盈亏必须有 spot | 缺 `spot` 或 quote freshness 不可用时，不能计算实时浮盈亏 |
| realized / unrealized 分开 | 已卖部分进入 realized，剩余持仓进入 unrealized |
| 剩余股数必须可追溯 | `assigned_qty - sold_qty` 必须来自 event / lot evidence |

时间和新鲜度：

| 规则 | 说明 |
|---|---|
| 当前问题必须带 `as_of` | 用户问“现在/当前/实时”时，必须展示或内部验证 quote/report 时间 |
| 月度收益按交易发生月 | cashflow 按交易发生月，realized PnL 按平仓/到期月 |
| 相对日期要归一 | `今天/昨天/本月` 在 Agent 层归一到具体日期或月份 |
| 港股/美股市场分开 | 市场日、交易时段和 quote freshness 不混用 |

Coverage gap 应该带 impact：

```json
{
  "kind": "missing_quote",
  "symbol": "0700.HK",
  "required_answer_key": "assigned_stock_unrealized_pnl",
  "impact": "当前正股浮盈亏和生命周期 PnL 无法计算",
  "recoverable_by": "refresh_quotes"
}
```

### 4.5 Answer Verifier 接入 Contract

当前 answer verifier 主要校验 claim 是否有 evidence。P0 增加 answer shape 校验：

```text
TaskContract.required_answer
-> Answer shape extractor
-> required keys satisfied?
-> pass / rewrite / fallback / ask
```

示例：

- 用户问“对比 lx 和 sy”，回答必须包含两个账户同一口径的对比。
- 用户问“有什么不同”，回答必须包含差额或说明缺少可比较口径。
- 用户问“为什么”，回答必须有 driver evidence；没有就不能编 root cause。
- 用户问“当前”，回答必须有 freshness；缺失就不能说实时结论。

### 4.6 P0 改动范围

建议新增：

- `src/application/assistant/task_contract.py`
- `src/application/assistant/coverage_verifier.py`

建议修改：

- `src/application/assistant/agent_loop.py`
  - planner 输出解析加入 `task_contract`
  - follow-up gap 由 `CoverageVerifier` 统一生成
  - synthesis 前检查 coverage
- `src/application/assistant/session.py`
  - `AgentSessionSnapshot` 记录 `task_contract` 和 `coverage`
- `src/application/assistant/answer_verifier.py`
  - 加 answer shape verification
- `src/application/assistant/evidence.py`
  - 补齐 coverage 所需的 scope summary 和 source summary

### 4.7 P0 验收

必须通过的场景：

| 场景 | 期望 |
|---|---|
| 对比 lx/sy 收益 | 回答谁高、差额、收益率差；不能只输出全部账户摘要 |
| 用户问来源 | 如果首查只有汇总，触发 follow-up 查组成 |
| 指派正股缺 quote | 不计算浮盈亏，说明缺失 quote 和影响 |
| 错误金额 synthesis | rewrite 一次；仍错则 fallback |
| 缺账户覆盖 | 能补查则补查，不能补查则明确缺哪个账户 |
| LLM 不可用 | fallback 仍保留问题形状 |

建议测试：

```bash
python3 -m pytest tests/test_assistant_runtime.py tests/test_analysis_tools.py tests/test_assistant_evidence_session.py
```

## 5. P1: Unified Tool Execution And ActionPolicy

### 5.1 目标

P1 解决“工具边界不统一”和“执行/回执/权限分散”。

P1 必须补上，但不做成 `Permission Profile`。设计原则：

```text
每次工具调用即时判定，不引入用户可见模式。
```

也就是：

```text
ToolCallProposal
-> ActionPolicy
-> PreToolCheck
-> Execute
-> PostToolCheck
-> Observation
```

### 5.2 ActionPolicy

`ActionPolicy` 是一次工具调用的决策，不是会话模式。

输入：

| 输入 | 说明 |
|---|---|
| `tool_manifest` | 工具是否只读、是否写入、风险等级、是否需要确认 |
| `request_context` | channel、sender、conversation、config_key |
| `task_contract` | 用户原始意图和 requested effect |
| `tool_call` | planner 提出的工具名和参数 |
| `pending_operation_state` | 是否已有待确认操作 |

输出：

```json
{
  "schema_version": "om-agent-action-policy-v1",
  "decision": "allow_read",
  "tool_name": "analysis_query",
  "risk_level": "read_only",
  "allowed_effect": "read",
  "reason": "pure_read_tool_within_task_scope",
  "requires_confirmation": false,
  "denied_reason": null
}
```

决策值：

| Decision | 含义 |
|---|---|
| `allow_read` | 可执行只读工具 |
| `allow_preview` | 可生成 preview/pending operation，但不能 apply |
| `need_confirm` | 已有 preview，需要用户确认后由 deterministic path apply |
| `deny` | 越权、工具不允许、参数不安全或与用户意图不符 |

核心规则：

1. Pure-read 工具默认可执行，但参数不能包含 path/config/env/system 越权字段。
2. Preview 工具只有在用户明确要求创建/修改/补录时可生成 preview。
3. Apply/confirm 不由 LLM planner 直接执行，只能走 deterministic confirm/apply。
4. 工具请求不能把 read 和 write preview 混在一个 plan 中。
5. planner 不能扩大用户 scope，例如用户问 `sy`，不能擅自查全部账户后给结论。

### 5.2.1 ActionPolicy Integration With Existing Policies

`ActionPolicy` 不重新实现权限系统，只编排现有边界。

现有边界继续作为 authority：

| 边界 | 现有 owner | P1 用法 |
|---|---|---|
| Read-only tool allowlist | `src/application/assistant/tool_policy.py` | `allow_read` 分支复用 `ToolPolicyEngine.authorize_read_tool` |
| Planner tool composition | `src/application/assistant/agent_loop.py` | 保留 plan step 数、read/preview 不混用、banned args 校验 |
| Inbound write/admin gate | `src/application/assistant/operation_policy.py` | preview/write/admin 分支复用 `enforce_*_write_allowed` |
| Pending operation | `src/application/assistant/operation_store.py` | preview 仍写入现有 store |
| Permission request | `src/application/assistant/permission_request.py` | preview 回执仍由现有 builder 生成 |
| Confirm/apply | deterministic command handlers | AgentLoop 不直接执行 apply |

集成规则：

1. `ActionPolicy` 先解析工具风险，不直接读取 env 决策写权限。
2. read-only 工具调用 `ToolPolicyEngine.authorize_read_tool`，失败即 `deny`。
3. preview 工具先检查用户原始意图，再调用对应 deterministic preview handler。
4. preview handler 内部继续调用 `operation_policy`，P1 不绕过现有 write gate。
5. apply/confirm 工具计划一律 `deny`，提示用户使用确认命令。
6. 所有 policy 结果写入 observation trace，但普通用户回执只展示必要确认信息。

ActionPolicy 输出应保留原始 authority：

```json
{
  "decision": "allow_preview",
  "tool_name": "manual_trade_open_preview",
  "authority": "operation_policy.enforce_trade_write_allowed",
  "allowed_effect": "preview",
  "apply_allowed": false,
  "requires_confirmation": true
}
```

### 5.2.2 Confirm/Apply Boundary

`need_confirm` 不是让 AgentLoop 进入长时间等待状态。它只表示当前 turn 应停止并返回
确认指引。

流程：

```text
User asks write/upgrade/model change
-> AgentLoop may create preview
-> preview returns pending operation + permission_request
-> Agent replies with summary + confirm/cancel hint
-> turn stops
-> user sends confirm command
-> deterministic confirm/apply handler executes
-> read-back / status receipt is returned
```

边界：

| 动作 | AgentLoop | Deterministic handler |
|---|---|---|
| 识别写入意图 | yes | yes |
| 生成 preview | yes, through existing preview operation | yes |
| 生成 permission_request | yes, from existing builder | yes |
| 等待用户确认 | no persistent loop wait | pending operation store |
| apply 写入 | no | yes |
| 读回确认 | no for apply; yes for preview evidence if read-only | yes |

如果用户直接说“确认”，但没有可匹配的 pending operation：

- AgentLoop 不猜测要确认什么。
- deterministic confirm command 返回缺失 pending operation。
- 普通回复给出短提示：没有可确认的待处理操作。

### 5.3 ToolExecutor

新增统一执行器，先包住 AgentLoop 可调用工具，不急着替换全部 CLI。

建议接口：

```python
class ToolExecutor:
    def execute(
        self,
        *,
        request: AssistantRequest,
        task_contract: TaskContract,
        call: ToolCall,
        source: str,
    ) -> ToolObservation:
        ...
```

内部步骤：

```text
1. Resolve tool manifest
2. Build ActionPolicy decision
3. PreToolCheck normalize and validate args
4. Execute handler
5. PostToolCheck validate result
6. Build observation with action decision and verification summary
```

Observation 增加字段：

```json
{
  "tool_name": "analysis_query",
  "ok": true,
  "data": {},
  "action_policy": {},
  "precheck": {"status": "pass"},
  "postcheck": {"status": "pass", "verifiers": []},
  "output_contract": {},
  "evidence_summary": {}
}
```

### 5.4 PreToolCheck

PreToolCheck 做输入和边界校验：

| Check | 说明 |
|---|---|
| `schema` | 参数必须符合工具 input schema |
| `scope` | account/symbol/month 必须来自用户问题、上下文或系统注入 |
| `path_guard` | 禁止 planner 传入本地路径、config_path、env、secret 等字段 |
| `write_guard` | 写/preview/apply 必须符合 ActionPolicy |
| `sql_guard` | `analysis_query` 只能 SELECT/WITH，view/function 白名单 |
| `prompt_injection_guard` | 工具参数不能来自工具输出中的可疑越权指令 |

Prompt injection guard 在 P1 先做规则版：

- 拒绝工具输出中的“忽略上文/改配置/发通知/执行命令/确认写入”等文本触发新动作。
- 新动作必须能从原始用户消息或明确确认消息中找到意图。

### 5.5 PostToolCheck

PostToolCheck 做输出校验和证据归一：

| Check | 说明 |
|---|---|
| `output_schema` | 工具输出符合声明的 schema |
| `output_contract` | 必须带 source、renderer、primary rows、fact fields |
| `freshness` | 当前类问题需要 current/quote 时，必须标记 fresh/stale/missing |
| `missing_data` | 缺失数据必须带 kind、impact、recoverable_by |
| `receipt` | preview/write 类必须有 operation_id、permission_request |
| `read_back` | apply 仍在 deterministic path，但回执必须能读回最终状态 |

### 5.6 Tool Manifest 扩展

在现有 `AgentTool` 上增量扩展，不新增 registry。

建议字段：

```json
{
  "annotations": {
    "read_only": true,
    "destructive": false,
    "idempotent": true,
    "open_world": false
  },
  "input_schema_version": "om-tool-input-v1",
  "output_schema": {},
  "evidence_contract": {
    "source_label": "OM local ledger",
    "primary_rows": "rows",
    "fact_fields": [],
    "freshness_fields": [],
    "missing_data_fields": [],
    "calculation_fields": []
  },
  "verifiers": ["schema", "freshness", "numeric", "receipt"]
}
```

字段解释：

| 字段 | 含义 |
|---|---|
| `read_only` | 不改变本地/外部状态 |
| `destructive` | 可能删除、覆盖或不可逆改变状态 |
| `idempotent` | 重复调用是否安全 |
| `open_world` | 是否依赖当前外部世界，例如实时行情、远端 release |
| `output_schema` | 工具结果结构 |
| `evidence_contract` | 如何从结果中提取 facts、source、freshness、missing_data |
| `verifiers` | 需要运行哪些 post-check |

优先迁移工具：

| 工具 | 原因 |
|---|---|
| `analysis_query` | 开放式分析主工具 |
| `analysis_catalog` | planner 的字段事实来源 |
| `option_positions_read` / assigned-stock read | spot、quote freshness、生命周期 PnL 高风险 |
| monthly income tools | 收益统计、现金流/已实现口径容易混淆 |
| `assistant_trace` | 诊断 Agent 自身行为 |
| upgrade preview/status 工具 | 用户强依赖回执和版本状态 |
| manual trade preview 工具 | 写入预览和 permission request 需要统一回执 |

### 5.7 Trace

P1 把 action policy 和 pre/post checks 写入 session trace：

```json
{
  "tool_transcript": [
    {
      "tool_name": "analysis_query",
      "action_policy": {"decision": "allow_read"},
      "precheck": {"status": "pass"},
      "postcheck": {"status": "pass"},
      "evidence_summary": {"fact_count": 24, "missing_data_count": 0}
    }
  ]
}
```

用户默认不看到这些字段。运维排障通过 `assistant_trace` 查看。

### 5.8 P1 验收

必须通过的场景：

| 场景 | 期望 |
|---|---|
| read-only 分析 | `allow_read`，执行并进入 evidence |
| 用户要求补录交易 | 只生成 preview 和 permission request，不 apply |
| LLM 计划 confirm/apply | `deny`，提示必须走确认命令 |
| LLM 传 path/config/env 参数 | `deny` |
| 工具输出缺 source/freshness | postcheck warning 或 fail，最终回答说明影响 |
| preview 回执 | 包含 operation_id、风险、confirm/cancel hint |

建议测试：

```bash
python3 -m pytest tests/test_assistant_runtime.py tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
```

## 6. P2: Diagnostics, Action Safety, Trace UX, And Evals

### 6.1 目标

P2 让 Agent 更智能，但不牺牲可控性。

P2 不追求大范围自主写操作，重点是：

- 更好地回答“为什么”。
- 更好地判断下一步工具是否仍符合用户意图。
- 更好地解释 fallback/ask/deny。
- 更好地用 eval 防止回归。

P2 的最小闭环：

```text
TaskContract
-> Diagnostic evidence adapter
-> EvidenceBundle
-> Hook results
-> Root-cause / coverage verifier
-> LLM synthesis
-> Answer verifier
-> compact trace
```

这个闭环有两个约束：

1. LLM 只能组织表达和提出候选解释，不能把缺失 evidence 变成事实。
2. 所有 pass / fail / deny / ask 仍由 deterministic verifier 或 action policy 决定。

### 6.1.1 P2 Runtime Route

P2 进入运行时后，仍然保持一条 AgentLoop 主线：

```text
AssistantRequest
-> TaskContract
-> PlannerPlan
-> ToolExecutor
-> DiagnosticEvidenceAdapter
-> HookRunner
-> EvidenceBundle
-> AnswerVerifier
-> ResponseRoute
```

各层只做自己的事：

| 层 | 输入 | 输出 | 不做 |
|---|---|---|---|
| `TaskContract` | 用户原话、上下文 | intent、scope、required answer shape | 不决定工具 |
| Planner | contract、tool catalog | bounded tool plan | 不执行、不授权 |
| `ToolExecutor` | tool call、policy、precheck | observation、postcheck | 不总结最终答案 |
| `DiagnosticEvidenceAdapter` | observation、output contract | `diagnostics[]` | 不重新计算业务数据 |
| `HookRunner` | tool/evidence/answer 快照 | `hook_results[]`、route hint | 不修改输入 |
| `AnswerVerifier` | draft、evidence、coverage、hooks | pass/rewrite/fallback/ask | 不补事实 |

Response route 初始只保留五类：

| Route | 条件 | 用户体验 |
|---|---|---|
| `pass` | 证据完整，回答验收通过 | 直接给自然语言答案 |
| `rewrite` | 回答形状或披露不足，但证据足够 | 内部重写一次，用户只看到最终答案 |
| `fallback` | LLM 仍不合格，但有确定性 renderer | 给结构化但可读的保底答案 |
| `ask` | 用户意图或 scope 不足，继续工具调用不安全 | 问一个最小澄清问题 |
| `deny` | 工具动作越权或 prompt injection 链路成立 | 简短拒绝并说明安全边界 |

P2 不新增用户可见模式。`route` 只进 trace，用来解释为什么最终回答是自然答案、
fallback、ask 或 deny。

### 6.2 诊断推理层

P2 优先扩展只读诊断 view 和 evidence：

| 诊断域 | 典型问题 | 数据面 |
|---|---|---|
| candidate filter | `为什么 NVDA 没出现在候选里` | `candidate_filter_diagnostics` |
| close advice | `为什么建议/不建议平仓` | `close_advice_snapshot` |
| runtime tick | `今天为什么没推送` | `runtime_tick_status` |
| quote freshness | `为什么浮盈亏没有 spot` | `quote_freshness` |
| upgrade/deploy | `升级为什么没回执` | operation audit、command log、release status |

规则：

1. 诊断只读，不启动 broker、OpenD、cron、service 或通知。
2. artifact 缺失必须是 evidence gap，不能回答成“没有问题”。
3. root cause 必须来自 observed diagnostic evidence。
4. 多个可能原因并存时，回答“最直接证据显示 X；Y 仍缺数据”，不要编唯一原因。

诊断层优先复用现有只读入口，不新增并行事实源：

| 诊断域 | 优先入口 | 缺失时的回答边界 |
|---|---|---|
| 收益/归因 | `analysis_query` semantic views | 只能说明已有汇总或明细缺失，不能补算未查询字段 |
| 指派正股 | assigned-stock read tools + quote evidence | 缺 quote 时不能计算当前浮盈亏 |
| candidate filter | `candidate_filter_explain` / diagnostics view | 缺 trace/artifact 时不能断定“未被过滤” |
| close advice | close-advice read surface | 缺快照时不能解释当时策略判断 |
| runtime tick | `runtime_status` / scheduler status | 缺日志时只能解释当前状态，不能解释历史原因 |
| upgrade/deploy | command audit / release status | 缺 command log 时不能宣称成功或失败 |

诊断 evidence adapter 的职责是把这些入口的输出归一到同一种 `diagnostics[]`，
而不是新建一套计算逻辑。已有工具已经能给出业务事实时，只补 metadata、scope、
freshness、missing_data 和 answer boundary。

### 6.2.1 诊断 Evidence Contract

P2 诊断不是让 LLM 自由推理，而是把各诊断域输出成统一 evidence，再让 LLM
负责可读表达。

诊断 evidence 最小结构：

```json
{
  "schema_version": "om-agent-diagnostic-evidence-v1",
  "domain": "candidate_filter",
  "status": "observed_rejection",
  "severity": "info",
  "scope": {
    "accounts": ["lx"],
    "symbols": ["NVDA"],
    "market": "us",
    "period": "latest_run"
  },
  "source": {
    "tool": "analysis_query",
    "view": "candidate_filter_diagnostics",
    "source_label": "OM read-only analysis workspace",
    "as_of": "2026-06-14T10:00:00+08:00"
  },
  "observed_reason": "liquidity rule rejected the row",
  "answer_boundary": "observed_filter_evidence_only",
  "missing_data": [],
  "confidence": "direct"
}
```

字段约定：

| 字段 | 说明 |
|---|---|
| `domain` | 诊断域，不等于工具名 |
| `status` | 观测状态，例如 rejected、skipped、held、missing、stale |
| `scope` | 账户、标的、市场、月份、run 范围 |
| `source` | 只读来源和时间，不暴露 artifact path |
| `observed_reason` | 直接证据里的原因，不能由 LLM 扩写成新事实 |
| `answer_boundary` | 回复边界，例如只能解释本次 run，不能泛化到全部历史 |
| `missing_data` | 缺哪些证据，以及缺失对结论的影响 |
| `confidence` | `direct`、`partial`、`missing`、`conflict` |

`status` 初始枚举：

| Status | 含义 | 回答方式 |
|---|---|---|
| `observed_rejection` | 有明确过滤/拒绝记录 | 可以说“记录显示被 X 规则过滤” |
| `observed_skip` | 有明确跳过记录 | 可以说“本次跳过原因是 X” |
| `observed_hold` | 有明确 hold / no-action 记录 | 可以解释已记录策略原因 |
| `diagnostic_missing` | 诊断 view 无对应行 | 只能说没有诊断记录，不能说没有问题 |
| `artifact_missing` | 上游 artifact 缺失 | 说明缺 artifact 和影响 |
| `stale_artifact` | artifact 过旧 | 说明只能作为旧快照，不能代表当前 |
| `conflicting_evidence` | 多个来源冲突 | 不能给单一确定结论 |

### 6.2.2 Why 类问题回答规则

“为什么”类问题的回答必须先定级：

| 级别 | 条件 | 允许回答 |
|---|---|---|
| Direct | 有同 scope 的诊断 evidence 和直接原因 | `直接记录显示...` |
| Partial | 有部分 scope 或间接证据 | `能看到...，但还缺...` |
| Missing | 没有诊断记录或 artifact | `当前证据不足，缺...` |
| Conflict | 证据冲突 | `不能给确定原因，冲突在...` |

禁止回答：

- artifact 缺失时回答“没有触发/没有问题”。
- 只有汇总状态时编具体 root cause。
- 把策略建议重新解释成交易建议。
- 把历史 run 的诊断外推为当前实时原因。
- 把 LLM 自己的推测写成“系统判断”。

回答模板：

```text
直接证据：...
影响：...
还缺：...
数据来源：...
```

如果证据完整，可以省略“还缺”。如果证据不完整，“还缺”和影响必须出现。

Root-cause verifier 需要额外检查三类错误：

| 错误 | 例子 | 处理 |
|---|---|---|
| `unsupported_root_cause` | 回答说“因为 OpenD 断开”，但 evidence 只有 missing quote | rewrite；仍失败则 fallback |
| `scope_overclaim` | 用 `lx` 的诊断解释 `sy` | ask 或说明缺 sy 证据 |
| `freshness_overclaim` | 用旧 run 解释当前实时状态 | rewrite，必须标明 as-of |

### 6.2.3 Diagnostic Adapter Mapping

诊断 adapter 不是新工具，也不是新事实源。它只把现有工具输出归一成
`diagnostics[]`，供 coverage、root-cause verifier 和 trace 使用。

第一批 adapter：

| Adapter | 来源 | 产出 domain | 关键映射 |
|---|---|---|---|
| `analysis_output_contract` | `analysis_query.output_contract` | income、breakdown、comparison | views、row_count、missing_data、calculation refs |
| `assigned_stock_quote` | 指派正股 read / quote refresh result | quote_freshness、assigned_stock_pnl | spot status、as_of、missing symbols |
| `candidate_filter` | `candidate_filter_explain` / trace artifact | candidate_filter | reject rule、stage、run scope |
| `runtime_status` | `runtime_status` / scheduler status | runtime_tick、notification | market window、cron、notification channel、last tick |
| `upgrade_operation` | pending operation / command audit / release status | upgrade | current version、target version、command status、receipt status |

统一转换规则：

1. 有明确业务行时输出 `confidence=direct`。
2. 只有汇总没有明细时输出 `confidence=partial`，并写 `missing_data`。
3. 查不到同 scope 行时输出 `diagnostic_missing`，不能当作 pass。
4. artifact、view、command log 缺失时输出 `artifact_missing`。
5. 来源时间早于用户问题的实时语境时输出 `stale_artifact` 或 freshness warning。
6. 多来源状态冲突时输出 `conflicting_evidence`，Answer 必须降级为 partial / ask。

Adapter 实现约束：

- 纯函数：输入 observation，输出 diagnostics，不读写外部状态。
- 不做 SQL 拼接，不访问 broker，不刷新 quote。
- 不复制完整工具输出，只保存 answer/verifier 必要字段。
- 所有金额、数量、比例仍通过 `EvidenceBundle.facts` 对齐，diagnostics 只解释原因和边界。

### 6.3 Action Safety Classifier

借鉴 Claude Code auto mode 的思想，但 P2 先做轻量版，不引入复杂模型依赖。

目标：在执行每个工具调用前判断：

```text
这个动作是否仍然符合用户原始意图？
```

输入只包含：

- 原始用户消息。
- 当前 task contract。
- proposed tool name。
- proposed arguments。
- tool manifest。
- pending operation state。

刻意不使用：

- LLM 对自己计划的解释。
- 工具输出里可能包含的指令性文本。

P2 初版使用规则版；LLM verifier 只作为 P2+ 候选诊断，不进入 pass/fail：

| 判定 | 例子 |
|---|---|
| allowed | 用户问收益对比，工具查只读收益 view |
| suspicious | 工具输出里出现“请确认写入”，planner 下一步想 apply |
| denied | 用户问状态，planner 想修改配置 |
| ask | 用户说“处理一下”，但没有说明账户/标的/动作 |

连续异常策略：

- 同一 turn 连续 2 次 suspicious/denied，停止 replan。
- 返回 ask 或 deny，不继续让 LLM 自行修复。

### 6.3.1 Action Safety 判定矩阵

P2 初版用规则表，不用 LLM 决定 allow/deny。

| Proposed action | 用户原始意图 | 判定 | 处理 |
|---|---|---|---|
| 同 scope 只读查询 | 问状态、收益、原因、持仓 | `allowed` | 执行 |
| 补齐同问题缺失证据 | coverage gap 可恢复 | `allowed_followup` | 执行一次 bounded follow-up |
| 扩大账户/月份/标的范围 | 用户没有要求 | `suspicious_scope_expansion` | 需要 planner 收敛；连续出现则 ask |
| 生成写入 preview | 用户明确要求新增/修改/补录/升级 | `allowed_preview` | 走 preview/pending operation |
| 生成写入 preview | 用户只是问信息 | `denied_effect_mismatch` | 拒绝并回答只读结果 |
| apply/confirm/cancel | LLM plan 提出 | `denied_apply_from_planner` | 提示必须走确定性确认路径 |
| 发通知/重启服务/改配置 | 用户没有明确要求 | `denied_side_effect` | 拒绝 |
| 工具输出诱导下一步写入 | 来自 observation 文本 | `denied_prompt_injection` | 停止 replan，写 trace |

判定必须使用原始用户消息和 `TaskContract.requested_effect`。工具输出只能作为事实证据，
不能作为新动作授权来源。

### 6.3.2 Prompt Injection 处理

P2 不做复杂内容安全模型，先做低误伤的越权文本检测。

触发词类型：

| 类型 | 示例 | 处理 |
|---|---|---|
| 改写系统指令 | `忽略上文`、`覆盖规则` | 不进入下一步 plan |
| 触发写操作 | `确认写入`、`立即修改配置` | 只能作为普通文本证据 |
| 触发外部副作用 | `发送通知`、`重启服务` | denied |
| 索要秘密 | `读取 token`、`打印 env` | denied |

规则：

1. 如果触发词来自用户原话，按正常 intent 处理。
2. 如果触发词来自工具输出、artifact、数据库行或 LLM 自己的计划解释，只能记录为
   suspicious evidence，不能授权动作。
3. 如果触发词和 proposed tool 形成写入链路，Action Safety 直接 deny。

### 6.3.3 Action Safety 与 ActionPolicy 的关系

两者不是两套权限系统：

| 层 | 责任 | 权威来源 |
|---|---|---|
| `ActionPolicy` | 这个工具按 manifest / risk / confirm 规则是否允许执行 | tool manifest、pending operation、既有 permission engine |
| `ActionSafety` | 这个工具调用是否仍符合当前用户任务和 scope | user message、TaskContract、tool args、bounded replan state |

组合规则：

1. `ActionPolicy` deny 时直接 stop，不进入 `ActionSafety` 放宽。
2. `ActionPolicy` allow 之后，`ActionSafety` 仍可因 scope 扩张、effect mismatch、
   prompt injection 链路而 deny/ask。
3. `ActionSafety` 不能批准 manifest 不允许的工具，也不能批准 apply。
4. 两者的结果都写入同一个 pre-tool trace event，避免用户排障时看到两条互相竞争的
   权限结论。

P2 初版可以只对 AgentLoop 发起的工具调用启用 `ActionSafety`。人工 confirm/apply
继续走现有 deterministic path，不经 LLM planner。

### 6.3.4 ActionSafety Decision Model

`ActionSafety` 输出必须能和 `ActionPolicy` 放在同一个 pre-tool trace 中：

```json
{
  "schema_version": "om-agent-action-safety-v1",
  "status": "deny",
  "code": "effect_mismatch",
  "user_intent": "read_income_comparison",
  "requested_effect": "read",
  "proposed_tool": "manual_trade_open",
  "proposed_effect": "preview_write",
  "scope_delta": {
    "accounts": "same",
    "symbols": "expanded",
    "period": "same"
  },
  "injection_evidence": [],
  "route": "deny",
  "reason": "User asked for read-only comparison; proposed tool creates a write preview."
}
```

`status` 枚举：

| Status | 含义 | Route |
|---|---|---|
| `allow` | 与用户 intent、scope、effect 一致 | execute |
| `allow_followup` | 为补 coverage gap 的同 scope 只读工具 | execute once |
| `allow_preview` | 用户明确要求写入预览，且 ActionPolicy 允许 preview | preview |
| `ask` | 用户意图可写但缺账户/标的/动作等关键字段 | ask |
| `suspicious` | scope 或 effect 可疑但不是明确越权 | replan once / then ask |
| `deny` | 越权、apply、外部副作用或注入链路成立 | deny |

Scope 比较规则：

| 字段 | 允许 | 可疑 | 拒绝 |
|---|---|---|---|
| account | 用户指定账户内 | 追加未指定账户 | 写入未指定账户 |
| symbol | 用户指定标的或 evidence gap 同标的 | 扩大到同市场其他标的 | 写入其他标的 |
| period | 用户指定月份/当前默认月份 | 扩到全历史 | 用旧数据解释实时问题且不披露 |
| effect | read -> read，write request -> preview | read -> preview | planner -> apply/confirm/service/config |

实现时先使用规则表。只有当规则表给出 `allow` 或 `allow_followup` 时，工具才会执行；
LLM 对“为什么要执行”的解释不能改变结果。

### 6.4 Verifier Hooks

P2 可以把 P0/P1 的验证器做成 hook pipeline：

```text
pre_tool_hooks:
  - action_policy
  - scope_check
  - prompt_injection_check

post_tool_hooks:
  - output_schema
  - evidence_contract
  - freshness
  - receipt

answer_hooks:
  - coverage
  - numeric_claim
  - policy
  - missing_data
  - root_cause
```

Hook 是内部插件点，不是用户配置系统。先由代码注册，不开放动态脚本。

P2 初版只允许 deterministic / rule-based hooks 参与最终 pass/fail。LLM verifier
最多作为 P2+ 的候选诊断生成器，输出仍必须通过 deterministic verifier，不能直接决定
`pass`、`allow`、`apply` 或 `root_cause confirmed`。

### 6.4.1 Hook Result Model

Hook 输出必须统一，避免每个 verifier 自己决定 final route。

```json
{
  "hook": "freshness",
  "stage": "post_tool",
  "status": "warning",
  "code": "quote_stale",
  "message": "FUTU quote is stale",
  "impact": "current assigned-stock unrealized PnL cannot be treated as realtime",
  "recoverable": true,
  "recoverable_by": "refresh_quotes",
  "evidence_refs": ["tool_transcript[1].data.rows[0].spot"]
}
```

`status`：

| Status | 含义 | 默认 route 影响 |
|---|---|---|
| `pass` | 通过 | 不影响 |
| `warning` | 有缺口但可说明 | answer 必须披露 impact |
| `recoverable_gap` | 可补证据 | bounded follow-up |
| `fail` | 不能安全回答该结论 | fallback / ask |
| `deny` | 工具动作越权 | stop + deny |

Route 归并规则：

1. 任一 pre-tool hook `deny`，不执行工具。
2. 任一 pre-tool hook `fail`，不执行工具，返回 ask 或 fallback。
3. post-tool `recoverable_gap` 优先进入 bounded follow-up。
4. answer hook `fail` 优先 rewrite；rewrite 后仍 fail 才 fallback。
5. `warning` 不阻断回答，但必须出现在 source / missing data / 口径里。

### 6.4.2 Hook 注册和裁剪

Hook 是代码内注册，不做动态配置：

| Stage | P2 初始 hooks | 裁剪线 |
|---|---|---|
| `pre_tool` | action_policy、scope_check、path_guard、prompt_injection_guard | 只检查 AgentLoop 工具 |
| `post_tool` | output_contract、freshness、missing_data、receipt | 先覆盖 analysis/positions/income/upgrade |
| `answer` | coverage、numeric_claim、root_cause、ux_leak | 沿用现有 answer verifier |

不做：

- 不允许用户配置 hook。
- 不允许工具输出携带可执行 hook。
- 不让 LLM verifier 直接改变 hook status。

### 6.4.3 Hook Pipeline 运行模型

Hook pipeline 只负责“汇总判定”，不替代工具实现：

```text
for proposed_call in tool_plan:
  pre_tool_hooks -> allow / ask / deny
  execute existing AgentTool
  post_tool_hooks -> evidence / warnings / recoverable gaps

after evidence bundle:
  answer_hooks -> pass / rewrite / fallback / ask
```

实现要求：

1. Hook 输入必须是不可变快照，不能修改 tool args、tool output 或 evidence。
2. Hook 输出只追加 `hook_results[]`，由 `AgentLoop` 汇总 route。
3. 同一个 stage 内的 hook 全部执行，除非出现 `deny`；这样 trace 能看到所有 warning。
4. route 汇总采用最保守状态：`deny > fail > recoverable_gap > warning > pass`。
5. bounded follow-up 次数继续由现有 loop budget 控制，hook 不能自己重入循环。

第一阶段不需要把所有 verifier 迁移成 hook。先把已有结果包成统一 `HookResult`
记录到 trace；等 trace 稳定后，再把 route 汇总迁入 hook pipeline。

### 6.4.4 Hook Aggregation Algorithm

Hook 汇总必须简单、可预测，避免出现多个 verifier 互相覆盖。

建议实现：

```text
stage_result = pass
for hook_result in hook_results:
  if hook_result.status == deny:
    return deny
  stage_result = max_severity(stage_result, hook_result.status)

route:
  deny -> deny
  fail -> rewrite if answer stage else fallback/ask
  recoverable_gap -> bounded follow-up if budget remains else fallback/ask
  warning -> pass with disclosure requirement
  pass -> continue
```

严重级别固定为：

```text
deny > fail > recoverable_gap > warning > pass
```

Bounded follow-up 规则：

1. 同一个 `gap.code + scope` 最多补一次。
2. follow-up 只能调用同 effect 的 read-only 工具。
3. follow-up 不能扩大用户未要求的账户、标的、月份。
4. follow-up 后仍缺数据时，answer 必须说明缺口和影响。
5. 如果缺口来自 action deny、prompt injection 或 write confirm，不允许 follow-up。

Hook code 需要稳定，便于 eval 和 trace 断言。第一批 code：

| Code | Stage | 含义 |
|---|---|---|
| `scope_missing_account` | coverage / answer | 用户要求账户对比但缺某账户证据 |
| `required_difference_missing` | answer | 对比问题缺差额或收益率差 |
| `unsupported_root_cause` | answer | 原因没有 diagnostic evidence 支撑 |
| `quote_missing_or_stale` | post_tool / answer | spot 缺失或过旧 |
| `output_contract_missing` | post_tool | 工具输出缺 evidence/source/freshness contract |
| `effect_mismatch` | pre_tool | proposed action 与用户 effect 不一致 |
| `planner_apply_denied` | pre_tool | planner 试图 confirm/apply/cancel |
| `prompt_injection_chain` | pre_tool | 工具输出诱导后续写操作 |

### 6.5 Trace UX

`assistant_trace` 增加 compact diagnosis：

```text
Agent trace：1 条
- command_id=in_xxx status=fallback route=analysis_result_renderer
  goal: 对比 lx 和 sy 的账户收益
  plan: analysis_query -> analysis_query(follow-up)
  evidence: facts=42 missing=0 views=account_monthly_performance,symbol_income_attribution
  coverage: complete
  verifier: first answer failed unsupported_amount; rewrite passed
```

原则：

- 默认展示摘要，不泄露 SQL、internal id、artifact path。
- `include_snapshot=true` 才输出结构化 snapshot。
- trace 是排障工具，不进入普通用户回执。

### 6.5.1 Trace 数据落点

Trace 继续使用现有 `agent_sessions` snapshot，不新增独立存储。

`AgentSessionSnapshot` P2 建议字段：

| 字段 | 内容 |
|---|---|
| `task_contract` | 用户问题、scope、required answer |
| `tool_transcript` | 工具名、action decision、pre/post check 摘要 |
| `evidence_bundle` | facts/datasets/missing/conflicts/guard contracts 摘要 |
| `coverage` | status、satisfied、missing、gaps、next_action |
| `hook_results` | pre/post/answer hook 的 compact 结果 |
| `answer_verification` | numeric/policy/shape/root-cause 检查结果 |
| `final_route` | pass、rewrite、fallback、ask、deny |

普通 trace 默认只展示 compact 字段。`include_snapshot=true` 才展示结构化 snapshot，
并继续走 redaction。

### 6.5.2 Redaction 规则

Trace UX 必须遵守普通 answer guard 的泄露边界：

| 内容 | 默认 trace | snapshot |
|---|---|---|
| SQL 文本 | 不展示 | 可展示 redacted / truncated |
| local path | 不展示 | redacted |
| internal id / lot id | 不展示 | 可展示 hash 或 redacted |
| account / symbol | 可展示 | 可展示 |
| amount / status / freshness | 可展示 | 可展示 |
| pending operation id | 可展示短 id | 可展示完整 id |

如果 trace 需要解释“为什么 fallback”，优先展示 verifier code 和 impact，不展示原始
LLM 输出全文。

### 6.5.3 Trace 用户体验

普通用户问题不自动附带 trace。只有用户明确问“为什么你这么答”“刚才为什么 fallback”
或调用 `assistant_trace` 时才展示 compact trace。

推荐展示格式：

```text
刚才这次回答走了 fallback。
- 任务：对比 lx 和 sy 的账户收益
- 工具：读取收益汇总；缺少 sy 明细 follow-up
- 证据：lx/sy 月度汇总完整，组成明细缺 1 个 view
- 校验：首版回答缺少差额，rewrite 后仍缺收益率差
- 最终：用确定性 fallback 回答，并标明缺失项
```

展示规则：

1. 面向用户的 trace 只说业务动作，不说 `analysis_query`、SQL、内部 path 或 lot id。
2. 如果用户是排障语境，可以显示 verifier code，例如 `missing_rate_difference`。
3. trace 不替代最终答案；它解释过程，不重新生成业务结论。

### 6.5.4 Trace Examples

Trace 要能解释线上最常见的失败，不要求用户读懂内部类名。

收益对比 fallback：

```text
刚才这次回答使用了保底结果。
- 任务：对比 lx 和 sy 的账户收益
- 证据：lx/sy 月度汇总完整；组成明细只覆盖 lx
- 校验：首版回答缺少收益率差；重写后仍缺 sy 明细归因
- 最终：返回确定性汇总，并标明 sy 明细缺失
```

候选过滤 why 缺 artifact：

```text
当前不能确定 NVDA 为什么没进候选。
- 任务：解释候选缺失原因
- 证据：没有找到同 run 的 candidate filter trace
- 影响：只能说明缺诊断记录，不能断定没有触发过滤
- 下一步：需要指定 run_id，或等下一次扫描生成 trace
```

升级无回执：

```text
升级流程没有足够证据证明完成。
- 任务：检查升级回执
- 证据：pending operation 存在，缺 command completion log
- 校验：当前版本和目标版本为空，不能宣称成功
- 最终：要求补 command_id 或重新查询 operation audit
```

工具输出诱导写入：

```text
刚才的工具计划被拒绝。
- 任务：只读查看收益
- 风险：工具输出文本包含确认写入提示，planner 试图生成写入预览
- 校验：ActionSafety 判定为 effect_mismatch
- 最终：停止执行写入类动作
```

### 6.6 Eval Suite

P2 必须建立 golden eval，覆盖自然语言问题而不是只测工具函数。

建议 eval 维度：

| Eval | 必须检查 |
|---|---|
| Account comparison | 双方、差额、收益率差、口径 |
| Assigned stock PnL | quote freshness、生命周期 PnL、missing quote |
| Income breakdown | follow-up 查组成，不能只给汇总 |
| Candidate why missing | artifact missing vs observed rejection 区分 |
| Runtime no notification | scheduler/market/notification 状态分开 |
| Upgrade receipt | current version、target version、command status、缺失日志 |
| Prompt injection | 工具输出诱导写入时被拒绝 |
| Write preview | preview 生成但不 apply |
| Answer UX | 不出现 SQL、tool name、internal id、强制事实/分析 |
| Fallback shape | fallback 仍回答原问题形状 |

Eval fixture 最小格式：

```json
{
  "name": "compare_lx_sy_income",
  "input": "对比 lx 和 sy 的账户收益，有什么不同？",
  "context": {
    "channel": "local",
    "config_key": "us",
    "sender": "test"
  },
  "mocked_tools": [
    {
      "tool_name": "analysis_query",
      "ok": true,
      "data": {}
    }
  ],
  "expected": {
    "route": "pass",
    "required_answer_keys": ["comparison_winner", "amount_difference", "rate_difference"],
    "required_text": ["lx", "sy", "差额"],
    "forbidden_text": ["analysis_query", "select ", "stock_lot_id"],
    "required_trace": {
      "coverage_status": "complete",
      "final_route": "pass"
    }
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `input` | 用户原话 |
| `context` | channel/config/sender 等最小运行上下文 |
| `mocked_tools` | 稳定工具 observation，避免 eval 依赖实时行情或外部服务 |
| `expected.route` | `pass`、`rewrite`、`fallback`、`ask`、`deny` |
| `required_answer_keys` | 必须满足的 TaskContract keys |
| `required_text` | 用户可见回答必须包含的业务词 |
| `forbidden_text` | 不允许泄露的内部词、SQL、路径、id |
| `required_trace` | coverage/verifier/fallback 的关键 trace 断言 |

Eval 运行原则：

1. 默认使用 mocked tool observations，不触发 broker、OpenD、Feishu 或远端 release。
2. 每个线上事故都应沉淀一个 regression fixture。
3. eval 断言业务形状和安全红线，不追求自然语言逐字相同。
4. 涉及金额时断言 evidence 中的值和最终回答里的值一致。
5. 涉及缺失数据时断言回答必须说明 impact。

### 6.6.1 Eval Harness 落点

沿用现有 eval 风格，优先扩展：

- `tests/fixtures/assistant_agent_eval.jsonl`
- `tests/test_assistant_agent_eval.py`
- `tests/test_assistant_runtime.py`
- `tests/test_analysis_tools.py`

新增 fixture 不直接访问 OpenD、Feishu、GitHub 或 broker。实时行情、升级状态、远端
release 统一 mock 成工具 observation。

建议新增断言：

| 断言 | 目的 |
|---|---|
| `required_answer_keys` | 验证 TaskContract 必答项 |
| `required_coverage_status` | 验证 coverage complete / gap |
| `required_hook_codes` | 验证关键 verifier 被触发 |
| `forbidden_route` | 防止本应 ask/deny 的场景被 pass |
| `forbidden_text` | 防止 SQL、tool name、internal id 泄露 |
| `evidence_value_match` | 金额/比例/数量必须来自 evidence |

### 6.6.2 P2 Golden Case 清单

P2 最少补齐这些 case：

| Case | 目标 |
|---|---|
| `compare_lx_sy_income_difference` | 收益对比必须有 winner/diff/rate/source |
| `compare_lx_sy_missing_sy` | 缺 sy 覆盖时触发 follow-up 或说明缺失 |
| `assigned_stock_missing_quote` | 缺 spot 不算当前浮盈亏 |
| `assigned_stock_fresh_quote` | fresh quote 下能回答持仓/spot/PnL |
| `income_breakdown_followup` | 用户问来源时不能只给汇总 |
| `candidate_missing_artifact` | artifact 缺失不能编 root cause |
| `candidate_observed_rejection` | 有 observed rejection 才能给直接原因 |
| `runtime_no_notification` | market/scheduler/notification 状态分开 |
| `upgrade_receipt_missing_version` | 当前/目标版本缺失时说明缺口 |
| `prompt_injection_from_tool_output` | 工具输出诱导写入时 deny |
| `write_preview_no_apply` | preview 可以生成，apply 不能由 planner 执行 |
| `trace_compact_no_internal_leak` | trace compact 不泄露 SQL/path/id |

### 6.7 P2 实现落点

P2 不需要一次性重写 AgentLoop。落点应沿现有边界增量进入：

| 能力 | 文件/边界 | 说明 |
|---|---|---|
| 诊断 evidence 归一 | `src/application/assistant/evidence.py` | 从 analysis diagnostics / output contract 提取 |
| action safety | `src/application/assistant/action_safety.py` | 新文件，规则版 classifier |
| hook result model | `src/application/assistant/verifier_hooks.py` | 新文件，只定义 pipeline 和 result |
| trace compact | `src/application/assistant/session.py`、`session_store.py` | snapshot additive 字段 |
| assistant_trace 展示 | `src/application/agent_tools/diagnostics.py` | 只读 compact renderer |
| golden eval | `tests/fixtures/assistant_agent_eval.jsonl` | mock observations |

执行顺序：

1. 先扩展 diagnostic evidence 和 answer root-cause verifier。
2. 再加 action safety classifier，仅影响 pre-tool deny/ask。
3. 再把现有 verifier 包成 hook result，不改变实际判定。
4. 最后扩展 trace compact 和 eval fixture。

### 6.7.1 P2 Vertical Slices

P2 不按“先把框架全搭完”推进，而按可验收的业务切片落地。

| Slice | 目标问题 | 涉及能力 | 验收 |
|---|---|---|---|
| S1 income comparison | `对比 lx 和 sy 收益差异` | coverage、answer shape、trace | 有 winner、差额、收益率差；trace 不泄露 SQL |
| S2 assigned stock quote | `为什么指派正股没有浮盈亏` | quote diagnostics、missing impact | missing quote 不计算 PnL；fresh quote 能计算 |
| S3 candidate why | `为什么某标的没进候选` | diagnostic adapter、root-cause verifier | artifact missing 与 observed rejection 分开回答 |
| S4 runtime / notification | `今天为什么没推送` | runtime diagnostics、scope/freshness | market、scheduler、notification 状态分开 |
| S5 upgrade receipt | `为什么升级没回执` | command audit、version evidence | 当前/目标版本缺失时不能宣称完成 |
| S6 prompt injection / preview | 工具输出诱导写入、用户要求补录 | action safety、preview trace | read-only 不写；write request 只 preview 不 apply |

每个 slice 的实现顺序：

1. 加 mocked observation fixture。
2. 加 diagnostic adapter 或补 output contract。
3. 加 hook / verifier code。
4. 加 runtime eval。
5. 加 compact trace 断言。

这样每个 slice 都能独立合并和回滚，不需要等 P2 全部完成。

### 6.8 P2 验收

必须通过：

1. 至少 10 个 end-to-end assistant runtime golden cases。
2. `assistant_trace` 能解释 fallback、rewrite、ask、deny。
3. `why` 类诊断回答不能在 artifact 缺失时编 root cause。
4. prompt injection 场景不能把工具输出中的指令升级为写操作。
5. 全量 release gate 仍通过。

建议测试：

```bash
python3 -m pytest tests/test_assistant_agent_eval.py
python3 -m pytest tests/test_assistant_runtime.py tests/test_analysis_tools.py tests/test_assistant_evidence_session.py
python3 scripts/release_check.py
```

### 6.9 P2 裁剪后的最终方案

P2 按最小可交付版本收敛为五件事：

| 事项 | 做 | 暂不做 |
|---|---|---|
| 诊断 evidence | 把已有只读诊断输出归一成 `diagnostics[]` | 新建独立诊断数据库 |
| why 回答 | 增加 root-cause verifier 和 missing impact | 让 LLM 自由推理原因 |
| action safety | 规则版 scope/effect/injection 检查 | LLM 自动权限判断 |
| trace | compact trace + snapshot additive 字段 | 面向用户展示完整内部链路 |
| eval | 10+ golden assistant runtime cases | 逐字匹配自然语言 |

因此 P2-MVP 的完成定义是：

1. 至少 candidate、runtime、quote、upgrade 四类 why 问题能区分 direct / partial /
   missing / conflict。
2. 工具输出中的指令性文本不能触发 planner 写操作。
3. `assistant_trace` 能解释 pass、rewrite、fallback、ask、deny 的主要原因。
4. golden eval 锁定收益对比、指派正股、why 诊断、prompt injection 和 preview no apply。
5. 没有新增用户可见模式，没有新增第二套 registry/store/permission profile。

### 6.10 P2 落地状态对照

第 6 节后续执行时，必须先对照源码事实，避免文档里的目标被误读成已经完成。
状态以当前源码和测试为准，本文只记录 P2 主线的落地边界：

| 能力 | 当前状态 | 已有事实 | 下一步 |
|---|---|---|---|
| Diagnostic evidence | M2 本地部分验收通过 | `EvidenceBundle` 已有 `diagnostics[]`、`diagnostic_count`、`diagnostic_domains`；已覆盖 analysis diagnostics、assigned-stock quote gap、upgrade operation/receipt/version/release publication gap；analysis rows 可归一出 candidate/runtime/quote/upgrade direct、missing、conflict/stale 缺口诊断；`upgrade_operation_status` 已接入真实 operation timeline read surface；`command_log_missing` / command audit 缺失会归入 `artifact_missing`；`release_tag` 不能证明 GitHub Release 已发布；`release_status` / `release_published_at` / `github_release_url` 可表达发布成功或失败证据 | 继续补更多 release status 线上样本 |
| Coverage verifier | M2.5 本地验收通过 | 已覆盖 account comparison、breakdown、assigned-stock missing quote、upgrade status missing version/receipt；coverage result 已进入 session trace / hook trace；不可补 upgrade gap 不会进入 follow-up planner | 继续补更多线上 stale/conflict 样本 |
| Root-cause verifier | 部分落地 | answer verifier 已能拦截无证据 root cause、quote upstream overclaim、analysis quote freshness gap 的上游根因外推，以及 unresolved diagnostics 下直接宣称成功/失败/完成的确定性状态结论；normalized diagnostics 已能区分 direct / partial / missing / conflict 的主要状态 | 把更多业务域的 conflict/stale 语义纳入统一规则 |
| ActionPolicy | 已落地 P1 | read path 和 preview path 已有 policy decision，planner 不能直接 apply | 保持为权限权威，P2 不另建权限系统 |
| ActionSafety | 已落地 M1 | 规则版 classifier 已检查 effect、scope、prompt injection chain；AgentLoop read tool 和 preview plan trace 已接入；同 scope read follow-up allow、跨账户 read ask、跨账户 write preview deny 已有 golden eval | 继续沉淀更多线上误判 golden case |
| Hook pipeline | M3 本地验收通过 | `HookResult` 包装模型已接入 pre-tool、post-tool、coverage、answer trace；当前只做 trace，不接管 route；M3 聚焦测试和相关回归已通过 | 保持 route authority 在 AgentLoop，不继续扩大 hook pipeline |
| Trace compact | M4 本地验收通过 | session snapshot 已能记录 task contract、coverage、evidence 摘要、diagnostics count/domains 和 hook code 摘要；`assistant_trace.response_text` 已按任务/工具/证据/缺口/校验/最终展示，并覆盖 ask、preview、rewrite、fallback、denied route 断言和 redaction 断言；`assistant_trace_route_samples` 已沉淀 5 条脱敏 route fixture | 继续沉淀真实线上 route 样本 |
| Golden eval | M4 本地验收通过 | `assistant_agent_eval` 已有 25 条 fixture，覆盖收益对比、指派正股、stale quote freshness、candidate why、runtime why、runtime conflict/stale、upgrade receipt missing version、upgrade conflict / command log missing、release tag not enough、release published/failed、old operation timeline、scope expansion、prompt injection from tool output、write preview no apply；harness 已支持 final response、answer guard、diagnostic domain/status、action safety 和 preview no apply 断言 | 后续只追加线上回归样本，不为单一问题写专用模式 |

这个状态表的意义是控制开发顺序：

1. 已落地的能力只补缺口，不重写。
2. 未落地的能力先做最小垂直切片，不先搭完整框架。
3. 部分落地的能力优先补 trace 和 eval，避免功能存在但线上无法解释。
4. evidence 层完成不等于 coverage 层完成。只有 `CoverageVerifier` 能把缺口转成
   `followup_tool`、`answer_with_missing_data` 或 `ask`，才算进入 AgentLoop 闭环。

### 6.11 P2 下一步最小实现包

P2 历史上不按“抽象层”推进，而按四个可合并的小包推进。当前 M1-M5 已有本地
验收结果，下一步不要重写它们；只继续追加线上样本和小缺口。

#### M5 Coverage/Upgrade 收口包

当前状态：本地验收通过。

目标：用户追问“为什么升级回执没有当前版本、目标版本，或没有成功回执”时，Agent
不能因为已有 operation timeline evidence 就误判为可完整回答，也不能触发任何升级、
重启或通知动作。

改动：

- 扩展 `CoverageVerifier`，读取 `EvidenceBundle.diagnostics` 和顶层
  `missing_data`。
- 当 `TaskContract.intent_families` 包含 `upgrade_status` 时，检查：
  `command_status`、`current_version`、`target_version`、`receipt_status`。
- 把 `current_version_missing`、`target_version_missing`、`receipt_not_observed`、
  `final_receipt_missing` 映射成 coverage gap。
- gap 必须带 `required_answer_key`、`impact`、`recoverable` 和 `recoverable_by`。
- 如果缺口只能通过 command audit / operation timeline 再读一次补齐，且当前 evidence
  还没有查询过对应只读 view，可以标记 `recoverable=true`。
- 如果已经查过 `upgrade_operation_status` / operation timeline 后仍缺版本或回执，
  必须标记 `recoverable=false`，`next_action=answer_with_missing_data`。
- AgentLoop follow-up gate 必须只对 `recoverable=true` 且 `suggested_tool` 为只读工具的
  gap 继续补证据。

首批 gap：

| Gap | 触发 | Route | 禁止动作 |
|---|---|---|---|
| `upgrade_current_version_missing` | 缺当前版本 | `answer_with_missing_data` 或只读 follow-up | 不执行升级、不重启服务 |
| `upgrade_target_version_missing` | 缺目标版本 | `answer_with_missing_data` 或只读 follow-up | 不猜 release tag |
| `upgrade_receipt_missing` | `receipt_not_observed` / `final_receipt_missing` | `answer_with_missing_data` | 不补发通知 |
| `upgrade_status_conflict` | command / operation / release 状态冲突 | `fallback` / `ask` | 不给单一成功结论 |

验收测试：

- 缺 `current_version` / `target_version` 时，coverage 不能是 `complete`。
- 已查询 `upgrade_operation_status` 后仍缺版本或回执时，不触发 follow-up 循环。
- 未查询 operation timeline 且用户明确给了 `command_id` 时，可以生成一次同 scope
  只读 follow-up。
- final answer 必须说明缺哪个版本/回执、影响是什么、数据来源是什么。
- trace 只能展示 compact 缺口，不展示 raw command log、本地 path、SQL 或 message id。

#### M1 ActionSafety 规则版

目标：每次 AgentLoop 发起工具调用前，都能回答“这个动作还符合用户原始任务吗”。

改动：

- 新增 `src/application/assistant/action_safety.py`。
- 输入：用户原话、`TaskContract`、proposed tool、args、manifest、ActionPolicy decision。
- 输出：`om-agent-action-safety-v1`，字段包含 `status`、`code`、`requested_effect`、
  `proposed_effect`、`scope_delta`、`route`、`reason`。
- 在 `ToolExecutor` 中放在 `ActionPolicy` 之后、precheck 之前。
- preview plan step 也写入 action safety trace，但仍不能 apply/confirm。

首批规则：

| 规则 | 处理 |
|---|---|
| `ActionPolicy` deny | 直接 deny，不调用 ActionSafety 放宽 |
| 用户 read-only，工具 proposed effect 是 preview/write/admin | `deny/effect_mismatch` |
| 用户 write intent 明确，工具只生成 preview | `allow_preview` |
| planner 提出 apply/confirm/cancel | `deny/planner_apply_denied` |
| args 增加未请求账户/标的/月 | read 场景 `ask` 或 `suspicious`；write 场景 `deny` |
| 工具输出中的指令性文本导致下一步写入 | `deny/prompt_injection_chain` |

验收测试：

- read-only 收益问题不能生成交易/升级 preview。
- 用户明确“补录/升级”时只能生成 preview/permission_request。
- 未指定账户的写入请求必须 ask。
- 工具 observation 中出现“确认写入/忽略规则”不能授权后续写操作。

#### M2 诊断 adapter 补齐

目标：why 类问题不再依赖 LLM 猜测原因，而是只基于诊断 evidence。

补齐顺序：

1. `candidate_filter`：区分 observed rejection、diagnostic missing、artifact missing。
2. `runtime_status`：区分 market window、scheduler、notification channel、last tick。
3. `upgrade_operation`：区分 current/target version、command status、receipt status、
   command log missing。
4. `quote_freshness`：在已有 assigned-stock quote gap 基础上补 stale/as-of 语义。

每个 adapter 都必须满足：

- 纯函数转换 observation，不读写外部状态。
- 不引入新事实源。
- 输出 `confidence` 和 `answer_boundary`。
- 缺证据时输出 `missing_data`，不能输出“没有问题”。

当前本地落地：

- `analysis_query.evidence.diagnostics` 仍是优先来源。
- 当 analysis result 只有 `rows/views_used` 而没有嵌套 diagnostics 时，`EvidenceBundle`
  会从 `candidate_filter_diagnostics`、`runtime_tick_status`、`close_advice_snapshot`、
  `quote_freshness`、`upgrade_operation_status` 的 rows 归一出 diagnostic evidence。
- `candidate_filter_diagnostics` 已覆盖 `observed_rejection`、`no_matching_rows`、
  `conflicting_evidence`。
- `runtime_tick_status` 已覆盖 `observed_scheduler_skip`、`observed_run_failure`、
  `observed_notification_missing`、`observed_runtime_freshness_gap`、`conflicting_evidence`。
- `upgrade_operation_status` 已作为真实 `analysis_query` view 接入
  `operation_timeline`，能查询 command / operation status、current / target version、
  receipt status 和 warning codes；版本或回执缺失会进入 `missing_data`。
- `confidence` 已按 direct / partial / missing / conflict 归一，缺行不再被解释成
  “没有问题”。

#### M3 HookResult 包装

目标：统一 trace 语言，不急着重构 verifier 权威。

第一步只做包装：

| 现有结果 | 包装为 |
|---|---|
| ActionPolicy decision | `pre_tool/action_policy` |
| ActionSafety decision | `pre_tool/action_safety` |
| planner args guard | `pre_tool/scope_or_args_guard` |
| output contract check | `post_tool/output_contract` |
| freshness/missing data warning | `post_tool/freshness`、`post_tool/missing_data` |
| coverage verifier | `answer/coverage` |
| answer verifier | `answer/numeric_claim`、`answer/root_cause`、`answer/ux_leak` |

裁剪线：

- 不开放用户自定义 hook。
- 不让 hook 修改 args/output/evidence。
- 不让 LLM verifier 直接写 `pass` / `deny`。
- 不在 M3 就替换现有 route 汇总逻辑。

#### M4 Trace 和 Eval

目标：线上失败后能解释，并用测试防止回归。

Trace compact 最少展示：

```text
任务：...
工具：...
证据：...
缺口：...
校验：...
最终：pass/rewrite/fallback/ask/deny
```

默认不展示：

- SQL。
- local path。
- internal id / lot id。
- 原始 LLM draft。
- 完整 tool output。

Golden eval 至少覆盖：

1. 收益对比必须有双方、winner、差额、收益率差。
2. 指派正股 missing quote 不能计算当前浮盈亏。
3. candidate artifact missing 不能编过滤原因。
4. runtime no notification 必须分开 scheduler / market / notification。
5. upgrade missing version 不能宣称升级完成。
6. prompt injection from tool output 必须 deny。
7. write preview no apply。
8. trace compact 不泄露 SQL/path/id。

本地落地状态：

- `assistant_agent_eval` 已扩展为 25 条 fixture。
- `prompt_injection_from_tool_output_denied` 使用 action safety 模式，断言工具输出里的
  `忽略上文/确认写入/修改配置` 不能形成写入授权链路。
- `write_preview_no_apply_manual_trade_open` 走真实 AgentLoop preview path，断言只生成
  `permission_request` 和预览回执，不调用 tool executor，不写入 `trade_events`。
- eval harness 支持普通回答、action safety、planner preview 三类 fixture，但这些只是测试入口，
  不是用户可见模式。

### 6.12 P2 失败处理策略

P2 的失败处理要比回答本身更确定。任何不确定都必须落到固定 route：

| 失败点 | 例子 | Route | 用户可见行为 |
|---|---|---|---|
| Task scope 不足 | “帮我处理一下”但没账户/标的/动作 | `ask` | 问一个最小澄清问题 |
| Coverage gap 可补 | 对比 lx/sy 但只查到 lx | bounded follow-up | 内部补一次同 scope 只读证据 |
| Coverage gap 不可补 | 缺历史 artifact 或 command log | `fallback` / `ask` | 说明缺什么和影响 |
| ActionPolicy deny | 工具风险或 manifest 不允许 | `deny` | 说明工具动作不被允许 |
| ActionSafety deny | read-only 问题触发 preview/write | `deny` | 说明与当前任务不匹配 |
| Root cause unsupported | 只有 missing quote 却说 OpenD 故障 | `rewrite`，失败后 `fallback` | 去掉无证据原因，只保留可证事实 |
| Trace leak | 回答出现 SQL/path/internal id | `rewrite`，失败后 `fallback` | 用确定性 renderer |
| Hook conflict | 多来源证据冲突 | `fallback` / `ask` | 说明冲突，不给单一结论 |

Bounded follow-up 只允许补同一用户问题所需的只读证据。以下情况不允许 follow-up：

- planner 想 apply/confirm/cancel。
- 缺口来自写入确认。
- 缺口需要启动服务、通知、broker、OpenD 或远端 release。
- follow-up 会扩大到用户未要求的账户、标的、月份。

### 6.13 P2 代码验收矩阵

每个 P2 切片合并前，至少要有“模型、运行时、trace/eval”三类证据。

| Slice | 模型/纯函数测试 | 运行时测试 | Trace/Eval 断言 |
|---|---|---|---|
| ActionSafety | classifier 对 read/preview/apply/scope/injection 的判定 | AgentLoop pre-tool deny/ask/pass | trace 含 action safety code |
| Quote diagnostics | EvidenceBundle 提取 quote gap/stale | assigned-stock 回答 missing quote fallback | 回答不编 upstream cause |
| Candidate why | adapter 区分 observed/missing/artifact | why 问题缺 artifact 不 pass | trace 说明缺 candidate trace |
| Runtime why | adapter 区分 market/scheduler/notification | no notification 问题不编单一原因 | trace 展示状态拆分 |
| Upgrade receipt | adapter 区分 version/command/receipt | version 为空时不能成功回执 | trace 展示 command log 缺口 |
| Coverage / upgrade status | coverage gap 区分可补/不可补 | 已查 operation timeline 后不继续 unsafe follow-up | 回答展示缺版本/回执的影响 |
| Hook wrapper | HookResult severity 排序和 redaction | 不改变现有 pass/fail 行为 | trace 不泄露内部字段 |

推荐执行顺序：

1. `python3 -m pytest tests/test_assistant_evidence_session.py -k diagnostic`
2. `python3 -m pytest tests/test_assistant_runtime.py -k "action_policy or action_safety or unsupported_quote or agent_loop"`
3. `python3 -m pytest tests/test_assistant_agent_eval.py`
4. `python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py`

发布前仍以第 9 节的 release gate 为准。

P2+ 再考虑：

- LLM verifier 作为候选诊断生成器，但不直接决定 pass/fail。
- 更多业务域 diagnostic adapters，例如 strategy replay、close optimizer、config diff。
- 更精细的 trace drill-down，但仍默认 redacted。

### 6.14 P2 Route Authority 补充

P2 的关键风险不是少一个 hook，而是 route 权威被拆散。补充规则：

| 决策点 | 权威 | LLM 角色 | 失败时 |
|---|---|---|---|
| 是否允许工具动作 | `ActionPolicy` + `ActionSafety` | 不能放宽，只能解释候选计划 | `deny` / `ask` |
| 是否需要补证据 | `CoverageVerifier` + deterministic gaps | 可以提出候选缺口，但不能自行扩大 scope | bounded follow-up / `fallback` |
| 是否能给 root cause | diagnostic evidence + answer verifier | 组织表达，不能创造原因 | `rewrite` 后仍失败则 `fallback` |
| 是否可展示 trace | compact renderer + redaction rule | 不直接输出内部 trace | redacted fallback |
| 是否 apply/confirm/cancel | 现有 pending operation confirm path | 无权限 | `deny` |

Route 汇总只保留一个入口：`AgentLoop`。Hook、coverage、answer verifier 都只输出
`HookResult` / diagnostic / verification result，由 `AgentLoop` 选择最终 route。这样
P2 不会形成第二套调度器或第二套权限系统。

Route 选择优先级：

```text
pre_tool deny
-> ask when user scope is insufficient
-> bounded read-only follow-up when evidence gap is recoverable
-> answer rewrite when evidence is enough but draft is unsafe/incomplete
-> deterministic fallback when rewrite still fails
-> pass
```

如果多个结果同时存在，采用最保守 route：

```text
deny > ask > fallback > rewrite > pass
```

注意：`recoverable_gap` 不是最终 route。它只是在 budget 允许时触发一次同 scope 只读
follow-up；follow-up 后仍缺证据，必须转成 `fallback` 或 `ask`。

### 6.15 P2 Evidence And Trace Ownership

P2 必须分清“业务事实”和“过程解释”，否则 trace 容易污染最终答案。

| 数据 | 存放位置 | 用途 | 用户默认可见 |
|---|---|---|---|
| 账户、标的、金额、比例、spot | `EvidenceBundle.facts` / datasets | 最终答案事实 | 可见 |
| 缺数据、stale、conflict、artifact missing | `EvidenceBundle.diagnostics` | 限制回答边界 | 可见摘要 |
| action policy / safety decision | tool authorization event | 排障和安全解释 | 默认不可见 |
| pre/post/answer hook result | session trace compact | 解释 fallback/ask/deny | 用户问 trace 时可见摘要 |
| LLM draft / retry draft | synthesis trace | 内部调试 | 不可见 |
| 原始 SQL、本地 path、lot id、command full log | raw tool output / artifacts | 只供工具和测试 | 不可见 |

实现约束：

1. 最终回答只从 `EvidenceBundle` 和 deterministic renderer 取事实。
2. `hook_results` 只说明“为什么能答/不能答”，不能新增业务事实。
3. `assistant_trace` 展示的是 compact trace，不展示 raw observation。
4. session store 只能持久化必要摘要；敏感内部字段必须在写入 compact trace 前裁剪。
5. 测试要同时断言“答案有必要业务字段”和“答案没有内部字段”。

### 6.16 P2 Current Implementation Audit

截至本节记录时，P2 不应被理解为整体完成。当前更准确的状态是：

| 包 | 状态 | 可合并前还缺 |
|---|---|---|
| M1 ActionSafety | 本地回归部分通过 | prompt injection chain、preview no apply、scope expansion 已有 golden 覆盖；同 scope read follow-up 允许，跨账户 read 要求澄清，跨账户 write preview 拒绝 |
| M2 Diagnostic adapter | 本地部分验收通过 | candidate/runtime/quote row-derived diagnostics 已覆盖 direct/missing/conflict/stale；upgrade missing version 已从 fixture 扩展为真实 `upgrade_operation_status` view，并保留 partial/missing_data 语义；runtime conflict/stale、stale quote、old operation timeline、upgrade conflict / command log missing、release_tag-only publication gap、release published/failed 已进入 golden eval | 仍需更多线上 release status 样本 |
| M2.5 Coverage/Upgrade | 本地验收通过 | `CoverageVerifier` 已把 upgrade diagnostics 转成 current/target version、receipt、command status gap；已查 operation timeline 后仍缺版本/回执会 `answer_with_missing_data`，不会触发 follow-up；未查 timeline 且有 operation id 时允许一次只读 follow-up |
| M3 HookResult | 本地验收通过 | hook code 已进入 compact trace；后续只补真实样本，不扩大 hook pipeline |
| M4 Trace/Eval | 本地验收通过 | compact trace renderer、redaction、ask/preview/rewrite/fallback/denied route 断言已落地；`assistant_trace_route_samples` 已有 5 条脱敏 route fixture；`assistant_agent_eval` 已扩到 25 条，包含 stale quote、runtime conflict/stale、old operation timeline、upgrade conflict / command log missing、release tag not enough、release published/failed、scope expansion、upgrade receipt missing version、prompt injection from tool output、write preview no apply | 可继续补真实线上 route 样本 |

M3 已完成本地验收，后续不要继续扩大 hook pipeline。M3 已证明三件事：

1. `HookResult` 不改变现有 pass/fail/deny 行为。
2. session snapshot 和 persisted trace 都能看到同一套 hook code。
3. public `final_response` 形状保持兼容，不把 hook 细节塞给普通用户。

M3 最小验证命令：

```bash
python3 -m py_compile src/application/assistant/verifier_hooks.py src/application/assistant/agent_loop.py src/application/assistant/session.py src/application/assistant/session_store.py
python3 -m pytest tests/test_assistant_runtime.py -k "hook_results or precheck_rejects or postcheck_marks or plans_manual_trade_open_preview or action_safety"
python3 -m pytest tests/test_assistant_evidence_session.py -k "assistant_trace or agent_session or hook"
```

本地已追加通过的相关回归：

```bash
python3 -m pytest tests/test_assistant_runtime.py tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
python3 -m pytest tests/test_analysis_tools.py -k "analysis_query or evidence or answer_guard"
```

下一步继续 P2 收尾：

1. 如线上排障需要，再补真实 session 的 ask / deny / rewrite trace 样本。
2. 补更多 `runtime_tick_status` / `upgrade_operation_status` 线上样本，覆盖 stale、conflict、
   command log missing 等细分诊断。
3. 再跑 release gate，决定是否发布。

### 6.17 P2 不做清单

为了防止 P2 膨胀，以下内容明确不进本轮：

- 不做 Permission Profile。
- 不做用户可配置 hook。
- 不做第二套 tool registry、session store、planner 或权限系统。
- 不让 LLM 直接决定 `allow`、`deny`、`apply`、`confirm`。
- 不把 tool output 中的指令当作下一步授权。
- 不在普通回答里展示 SQL、path、internal id、lot id、raw command log。
- 不为了一个线上问题新增专用工具；优先增强通用 evidence、coverage、trace 和 eval。

如果后续确实需要新增工具，必须满足三个条件：

1. 现有 Tool OS 无法稳定表达同一类问题。
2. 新工具输出是可复用的 structured evidence，而不是某个问题的 hard-coded answer。
3. 有对应 eval 能证明 LLM 可以基于这个 evidence 回答多个相邻问题。

### 6.18 P2 收口执行顺序

第 6 节后续开工只按下面顺序推进，避免在 action safety、hook、trace 已经够用时继续
加层。

#### Step 1: Coverage gap 模型补字段

先保持 `CoverageResult` 公共结构兼容，只给 `gaps[]` 增加字段：

| 字段 | 说明 |
|---|---|
| `required_answer_key` | 对应 `TaskContract.required_answer` 的缺口 |
| `impact` | 这个缺口导致最终答案不能回答什么 |
| `recoverable` | 是否允许 bounded read-only follow-up |
| `recoverable_by` | 可补时的只读 evidence 来源 |
| `suggested_tool` | 可补时的只读工具名；不可补时为空 |

兼容规则：

- 旧 gap 没有 `recoverable` 时，按 `recoverable=true` 处理，保持现有 follow-up 行为。
- 新增不可补 gap 时，必须显式写 `recoverable=false`。
- 不引入新的 `CoverageResult.status` 枚举，先用 `next_action` 表达：
  `followup_tool` 或 `answer_with_missing_data`。

#### Step 2: Upgrade coverage rule

在 `CoverageVerifier` 增加 `_upgrade_status_gaps`，输入只使用：

- `task_contract.intent_families`
- `task_contract.required_answer`
- `EvidenceBundle.facts`
- `EvidenceBundle.datasets`
- `EvidenceBundle.missing_data`
- `EvidenceBundle.diagnostics`

规则：

| 缺口来源 | Coverage gap | recoverable |
|---|---|---|
| `current_version_missing` | `upgrade_current_version_missing` | 已查 operation timeline 后为 false |
| `target_version_missing` | `upgrade_target_version_missing` | 已查 operation timeline 后为 false |
| `receipt_not_observed` | `upgrade_receipt_missing` | false |
| `final_receipt_missing` | `upgrade_receipt_missing` | false |
| `conflicting_evidence` | `upgrade_status_conflict` | false |

如果用户给了 `command_id`，但 evidence 没有 `upgrade_operation_status` 或 operation timeline
dataset，可以生成一次 `recoverable=true` 的只读 follow-up。这个 follow-up 只能读
operation/audit 状态，不能执行升级、重启、补发通知或修改 runtime state。

#### Step 3: AgentLoop follow-up gate

`AgentLoop` 合并 evidence gaps 后必须过滤不可补缺口：

1. 只有 `recoverable=true` 的 gap 能进入 `_should_replan_read_only`。
2. `suggested_tool` 必须是 manifest 上的 read-only 工具。
3. `recoverable_by` 不能是 service、notification、broker、OpenD refresh 或 apply/confirm。
4. 同一 `gap.kind + scope` 只补一次。
5. 不可补缺口进入 final answer，要求 answer verifier 检查是否披露缺口。

这样可以解决两个问题：

- 避免“缺升级成功回执”时 Agent 试图补发回执。
- 避免“版本为空”时 Agent 继续循环查同一条已经查过的 operation timeline。

#### Step 4: 回答和 trace

面向用户的回答只保留三块：

```text
结论：当前证据能确认什么 / 不能确认什么
缺口：缺当前版本、目标版本或最终回执，以及影响
数据来源：operation timeline / command audit / release status
```

不要展示：

- raw command log。
- SQL。
- local artifact path。
- message id / operation id 以外的内部 id。
- hook 名称、route 名称、canonical/synthesis 名称。

compact trace 可以记录：

- task contract required answer。
- coverage gaps。
- 是否尝试过 bounded follow-up。
- final route。

#### Step 5: 验证清单

最小测试包：

```bash
python3 -m pytest tests/test_assistant_evidence_session.py -k "coverage or upgrade"
python3 -m pytest tests/test_assistant_runtime.py -k "coverage or upgrade or followup"
python3 -m pytest tests/test_assistant_agent_eval.py
python3 -m pytest tests/test_analysis_tools.py -k "upgrade_operation_status or analysis_query"
```

发布前再跑：

```bash
python3 scripts/release_check.py
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
```

完成判定：

1. upgrade missing version / receipt 不再被 coverage 判为 complete。
2. 不可补缺口不会触发 follow-up 工具循环。
3. 可补缺口只会触发一次同 scope 只读查询。
4. final answer 能自然说明缺口和影响，不展示内部 trace。
5. golden eval 覆盖“缺版本/缺回执”和“证据完整”两类 upgrade status 问题。

### 6.19 P2 发布前缺口补齐

第 6 节里的 M1-M5 “本地验收通过”只代表核心模型和回归测试已经成立，不代表 P2
已经达到线上可发布状态。发布前还需要把真实线上失败沉淀为稳定样本，确保 Agent
不是只在单元测试里安全。

发布前只补六类缺口：

| 缺口 | 当前状态 | 需要补什么 | 完成判定 |
|---|---|---|---|
| 真实 session route 样本 | 已补 `assistant_trace_route_samples` 5 条脱敏 route fixture，覆盖 ask / preview / rewrite / fallback / deny；compact trace 能解释 route 且不展示 SQL/path/session id/lot id/raw log | 继续从真实线上回执补充同格式样本 | trace 能解释 route，不泄露 SQL/path/internal id |
| runtime stale / conflict | 已补 `runtime_why_conflict_stale_answer` golden fixture；`runtime_tick_status` 已能归一 direct / missing / conflict | 继续从线上补更多 runtime stale / notification audit 样本 | why 回答不能给单一确定原因 |
| upgrade conflict / command log missing / release status | 已补 `operation_upgrade_conflict_command_log_missing_answer`、`operation_upgrade_release_tag_not_enough_answer`、`operation_upgrade_release_published_answer`、`operation_upgrade_release_failed_answer` golden fixture；`command_log_missing` 会归入 `artifact_missing`；只有 `release_tag` 时会产生 `release_publication_status_missing` | 继续从线上补真实 release success / failure 样本 | 不能宣称升级或 release 成功；必须说明冲突、缺日志或缺发布证据的影响 |
| scope expansion 误判 | 已补 `action_safety_read_followup_same_scope_allowed`、`action_safety_read_scope_expansion_asks`、`action_safety_cross_account_write_denied` golden fixture | 继续从线上补更多误判样本 | 同 scope read follow-up 允许；未请求写入或跨账户写入拒绝 |
| answer source/freshness | Answer verifier 已覆盖金额、quote、diagnostic root cause、analysis quote gap 上游根因外推、unresolved diagnostics 下的确定性状态结论；已补旧 runtime 快照、stale quote、old operation timeline final answer 断言 | 继续沉淀更多线上 freshness 样本 | 最终回答必须披露 as-of / stale 影响 |
| 发布 gate | 已补 release checklist 对应命令和失败回退说明 | 每次 release 前按 6.21 执行并记录证据 | release 前能一眼判断是否可发 |

不补：

- 不新增 mode。
- 不新增工具层级。
- 不为单个线上问题写 hard-coded answer。
- 不扩大 hook pipeline 权限。
- 不把真实 command log、SQL、local path 放进 fixture 明文。

### 6.20 P2 线上样本进入 Eval 的流程

每个线上问题必须按同一流程进入 eval，避免把临时修复变成长期复杂度：

```text
线上问题
-> 收集 compact trace / tool observation 摘要
-> 脱敏成 mocked observation fixture
-> 写 expected route / required text / forbidden text
-> 修 evidence / coverage / verifier
-> 跑 focused tests
-> 更新第 6.16 状态表
```

样本脱敏规则：

| 原始内容 | fixture 中保留 | fixture 中移除 |
|---|---|---|
| account / symbol / month | 保留，作为业务 scope | 不移除 |
| command_id | 可保留短 id 或稳定假 id | 真实 message id / audit path |
| 金额 / ratio / status | 保留，用来验算 answer | 不移除 |
| SQL | 尽量只保留 view 和 rows；必要时保留短 SELECT | path、PRAGMA、内部调试 SQL |
| tool output | 保留最小 data / evidence / diagnostics | raw command log、完整 LLM draft |

每条 eval 至少要断言四件事：

1. `expected.route` 或等价 final route。
2. 必须出现的业务结论。
3. 必须披露的缺口和影响。
4. 不允许出现的内部词、SQL、path、id 或 unsupported root cause。

fixture 命名规则：

| 类型 | 命名 |
|---|---|
| 收益 / 持仓 | `analysis_<domain>_<behavior>` |
| why 诊断 | `<domain>_why_<status>` |
| 升级 / 操作 | `operation_<domain>_<gap_or_conflict>` |
| 安全 | `action_safety_<risk>` |
| trace | `trace_<route>_<redaction>` |

线上样本如果暴露的是已有规则缺失，优先修 verifier / coverage；如果暴露的是 evidence
缺字段，优先修 adapter / output contract；只有现有工具无法表达同类问题时，才考虑新增
通用 read-only view。

route 样本使用 `tests/fixtures/assistant_trace_route_samples.jsonl`，每行保留 compact trace
结构和 `expect_contains` / `expect_not_contains`，禁止放入真实 SQL、message id、session id、
raw command log、local path 或真实成交提醒正文。

### 6.21 P2 发布 gate 和回退

P2 发布前必须把“本地验收通过”提升为“可发布”。最小 gate：

```bash
python3 -m pytest tests/test_assistant_evidence_session.py
python3 -m pytest tests/test_assistant_agent_eval.py
python3 -m pytest tests/test_assistant_runtime.py
python3 -m pytest tests/test_analysis_tools.py
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
python3 scripts/release_check.py
```

执行清单：

| Gate | 命令 | 通过证据 | 失败回退 |
|---|---|---|---|
| fixture 格式 | `jq -c . tests/fixtures/assistant_agent_eval.jsonl` | 25 条 JSONL 都能逐行解析 | 停止发布，先修 fixture；不要删除失败样本 |
| trace route fixture | `jq -c . tests/fixtures/assistant_trace_route_samples.jsonl` + `python3 -m pytest tests/test_assistant_evidence_session.py::test_format_assistant_trace_route_samples_from_fixture` | ask/preview/rewrite/fallback/denied 都能解释 route 且不泄露内部细节 | 停止发布，先修 compact renderer 或脱敏 fixture |
| agent eval | `python3 -m pytest tests/test_assistant_agent_eval.py` | stale/conflict/release_tag/scope 等 fixture 全部通过 | 回到 evidence / coverage / verifier 修根因，不加固定文案 |
| verifier / runtime / analysis | `python3 -m pytest tests/test_assistant_evidence_session.py tests/test_assistant_runtime.py tests/test_analysis_tools.py` | diagnostics、follow-up、rewrite/fallback、analysis view 回归全部通过 | 保留失败 trace，缩小到对应模块修复 |
| agent contract | `python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py` | public tool contract 和 smoke 全部通过 | 不发布；先同步 tool metadata / output contract |
| release metadata | `python3 scripts/release_check.py --tag v<VERSION>` | `VERSION`、`CHANGELOG.md`、tag 名称一致 | 修版本元数据；不要手动绕过 VERSION workflow |
| diff hygiene | `git diff --check` | 无 whitespace / conflict marker 问题 | 修当前 diff；不要把格式噪声混入 release |

可发布判定：

1. eval 覆盖第 6.19 的六类缺口，且没有只靠固定文案通过的测试。
2. `assistant_trace` 能解释最新一次线上失败的 route。
3. final answer 不展示 `analysis_query`、SQL、local path、internal id、lot id、raw command
   log、`canonical`、`synthesis`。
4. 读请求不会生成 write preview；write 请求不会被 planner 直接 apply。
5. upgrade / runtime / quote / candidate why 的 missing、stale、conflict 都能降级回答。

回退策略：

| 问题 | 回退 |
|---|---|
| coverage 误判导致答非所问 | 关闭 coverage 对 final route 的影响，保留 trace-only |
| ActionSafety 误拒绝只读工具 | 仅禁用 suspicious scope 规则，保留 effect mismatch / injection deny |
| trace 泄露内部字段 | 禁用 snapshot 展示，只保留 compact renderer |
| eval flaky | 移除实时依赖，改成 mocked observation |
| release 后线上回答质量下降 | 回退到上一版本，同时保留失败 trace 作为新 fixture |

回退不能放宽写权限。所有回退都只能减少新验证器对用户回复的影响，不能让 planner 获得
apply/confirm/service/config/broker-facing 权限。

### 6.22 P2 完成后的裁剪

P2 发布稳定后，需要反向裁剪，避免文档和代码继续堆层：

| 可裁剪对象 | 条件 | 处理 |
|---|---|---|
| 临时 feature flag | 连续两个版本无回退 | 默认开启或删除 |
| 重复 fallback 文案 | eval 已覆盖 route shape | 合并到 deterministic renderer |
| 只为调试存在的 trace 字段 | compact trace 不再使用 | 从 public trace 删除，snapshot 可保留 redacted 摘要 |
| 过窄 fixture | 已有更通用 fixture 覆盖 | 合并，保留事故说明 |
| 文档中过时状态 | 源码和测试已经变更 | 更新第 6.16，删除不符合事实的信息 |

裁剪原则：

1. 权限边界只收紧不放宽。
2. 用户可见体验只保留一个 Agent 回复，不暴露内部 route / mode。
3. eval 保留行为覆盖，删除重复的实现细节断言。
4. 文档状态必须以源码和测试为准，不把目标状态写成已完成事实。

## 7. 分阶段开发计划

### P0 开发顺序

1. 新增 `TaskContract` 数据模型和 trace payload。
2. Planner 输出 `task_contract`，host 校验并裁剪。
3. 新增 `CoverageVerifier`，先覆盖 compare、breakdown、assigned-stock、upgrade status。
4. AgentLoop synthesis 前运行 coverage verifier。
5. Answer verifier 增加 required-answer shape 检查。
6. 补 runtime tests 和 analysis Tool OS tests。

P0 完成定义：

- 账户收益对比不会退回全部账户摘要。
- 指派正股缺 quote 不会编浮盈亏。
- coverage gap 能触发 bounded follow-up 或明确缺失。
- LLM synthesis 错误仍能 rewrite/fallback。

### P1 开发顺序

1. 新增 `ActionPolicy` 数据模型。
2. 新增 `ToolExecutor`，先在 AgentLoop 内部使用。
3. 把现有 `ToolPolicyEngine.authorize_read_tool` 作为 P1 policy 的 read-only 分支。
4. 增加 precheck/postcheck 结果模型。
5. 扩展 `AgentTool` manifest annotations 和 evidence contract。
6. 迁移 `analysis_query`、assigned-stock read、monthly income read、assistant_trace、
   preview trade/upgrade 相关工具。
7. 把 action policy 和 checks 写入 `AgentSessionSnapshot`。

P1 完成定义：

- AgentLoop 所有工具调用都有 action decision。
- 写入类请求只能生成 preview/permission_request。
- planner 不能直接 apply/confirm。
- 工具输出缺 source/freshness/receipt 时，postcheck 能拦截或标记 warning。

### P2 开发顺序

1. 扩展 P2 诊断 view 的 evidence diagnostics。
2. 新增 action safety classifier 规则版。
3. 把 verifier 收敛成内部 hook pipeline。
4. 扩展 `assistant_trace` compact diagnosis。
5. 建立 golden eval fixtures。
6. P2+ 可选加入只读 LLM verifier 候选诊断，但所有结论仍需 deterministic verifier 通过。

P2 完成定义：

- `为什么` 类问题能基于诊断 evidence 回答。
- 工具输出诱导越权动作会被拒绝。
- trace 能解释主要 Agent 决策。
- golden eval 能防止 UX 和安全回归。

## 8. 风险和裁剪

| 风险 | 裁剪策略 |
|---|---|
| 抽象过多 | 只保留 `Plan / Act / Verify / Answer` 四段，其他都是内部组件 |
| P1 变成新权限系统 | 不做 Permission Profile，只做每次工具调用的 ActionPolicy |
| Verifier 过度复杂 | P0 先覆盖收益/持仓/升级状态，P2 再 hook 化 |
| Trace 泄露内部细节 | 默认 compact，不展示 SQL、internal id、artifact path |
| LLM verifier 反而引入幻觉 | LLM verifier 只能产生候选，最终仍由 deterministic verifier 判定 |
| 影响现有命令 | 先包 AgentLoop 可见工具，不替换 human CLI 和 deterministic handlers |

## 9. 验证门槛

阶段内验收：

```bash
python3 -m pytest tests/test_assistant_runtime.py tests/test_analysis_tools.py tests/test_assistant_evidence_session.py
```

涉及工具 manifest / agent plugin contract：

```bash
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
```

发布前：

```bash
python3 scripts/release_check.py
python3 -m pytest
```

## 10. 最终形态

用户看到的是一个自然 Agent 回复：

```text
lx 和 sy 的 2026-06 收益差异主要在 HKD 指派正股和 USD 权利金归因。
同口径下 sy 的净现金流更高，差额为 ...

数据来源：OM 本地账本 ...
口径：...
缺失数据：...
```

系统内部保留完整、可审计的结构：

```text
TaskContract
-> Tool calls with ActionPolicy
-> Observations with postcheck
-> EvidenceBundle
-> Coverage result
-> Answer verifier result
-> final route
```

这就是 OM 需要借鉴 Claude Code 的部分：不是更多模式，而是让每一步都由 host
验证、记录、回退。
