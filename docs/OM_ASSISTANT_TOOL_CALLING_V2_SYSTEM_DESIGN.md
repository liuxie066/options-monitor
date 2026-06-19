# OM Assistant Tool Calling v2 System Design

本文档是 `./om assistant` 工具调用升级的系统分析和详细设计。
当前命名、维度边界仍以
[OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md) 为准；
能力、风险和可见工具边界以
[OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md) 为准。

## 1. 背景

当前 `./om assistant` 已经有 `AgentLoop`、`TaskContract`、
`EvidenceBundle`、`AgentSessionSnapshot`、read-only tool policy、
answer guard、answer verifier 和 `assistant_trace`。问题不是缺少基础
安全骨架，而是工具调用仍偏向一次性计划执行，模型没有充分承担
"观察工具结果后决定下一步" 的智能工作。

本次升级目标是让 `./om assistant` 从 "选一个能力/工具回答" 升级为：

```text
理解用户目标
-> 选择一个或多个只读工具
-> 观察工具结果
-> 模型判断是否继续查证
-> 从证据组织答案
-> 由代码做安全、可靠性和可追溯护栏
```

核心判断：

- 智能化主要放在模型侧：理解意图、选工具、读结果、决定继续或回答。
- 代码主要解决准确性、可靠性、安全性：协议、scope、风险、预算、
  去重、证据、answer verification、trace。
- 不新增一套项目 Agent、不新增第二工具注册表、不新增全局对话状态机。
- 不保留旧 planner 输出、旧 answer path 或旧 fixture 的并行路径；
  旧形态必须迁移到当前 contract。

## 2. 目标和非目标

### 2.1 目标

1. 让模型可以在同一 turn 内多次调用 `READ_AUTO` 工具，并基于观察结果
   决定是否继续。
2. 把 pure-read 工具池适度放宽，但每次调用都必须被 scope、risk、
   budget、duplicate 和 trace 约束。
3. 让 capability selection 可解释：为什么选这个工具、来自用户文本、
   上下文、task contract 还是证据缺口。
4. 让答案可追溯：用户可见事实来自 `EvidenceBundle` 中的 tool fact、
   dataset、missing_data、conflict 或 calculation。
5. 保持写路径严格：交易、持仓、ledger、config、model、通知、服务、
   release、broker-facing 操作不能进入自动工具循环。
6. 让 clarification 只在高风险缺槽或 unsafe scope 时触发，避免为了
   "智能化" 增加误澄清。

### 2.2 非目标

- 不把 `./om-agent` 改造成项目自己的 Agent。
- 不暴露完整 `./om-agent spec` 给远端 Inbound Assistant。
- 不允许模型运行 shell、Python、SQL 写入、broker 操作或通知发送。
- 不让 `CoverageVerifier` 成为主调度器。
- 不添加 mode flag 来保留旧 planner 输出或旧 answer route。
- 不为单个业务问句新增专用 tool path；优先复用 `analysis_query`、
  现有 read tools、output contracts 和 evidence extraction。

## 3. 当前 owner 边界

| 责任 | 当前 owner | v2 设计中的角色 |
|---|---|---|
| message entry | `src/application/assistant/runtime.py`, `router.py` | 接收请求、选择 deterministic route 或 AgentLoop |
| capability catalog | `src/application/assistant/capability_catalog.py`, `tool_bindings.py` | 生成 planner-visible 能力视图 |
| planner loop | `src/application/assistant/agent_loop.py` | 承载模型驱动 read-tool loop |
| task scope | `src/application/assistant/task_contract.py` | 生成和校验 `TaskContract` |
| read authorization | `src/application/assistant/tool_policy.py`, `src/application/tool_allowlist.py` | 只允许 `READ_AUTO` 工具自动执行 |
| tool execution | `src/application/tool_execution.py`, `src/application/agent_tool_registry.py` | 统一执行工具和返回 deterministic result |
| evidence | `src/application/assistant/evidence.py` | 从 observation 构建 `EvidenceBundle` |
| verification | `coverage_verifier.py`, `answer_verifier.py`, `answer_guard.py` | 做 post-check、repair 和 fallback |
| session trace | `src/application/assistant/session.py`, `session_store.py` | 写入 `AgentSessionSnapshot` 和 `assistant_trace` |
| preview/confirm | `operation_lifecycle.py`, `operation_store.py`, operation handlers | 写路径退出 read loop 后进入 pending operation |

## 4. 目标架构

```text
Inbound message
  -> AssistantRequest
  -> ContextProjection
  -> model planner
     -> TaskContract
     -> first tool call
  -> ToolLoopGuard
     -> scope check
     -> risk check
     -> budget check
     -> duplicate check
  -> tool_execution
  -> ToolObservation
  -> EvidenceBundle
  -> model continuation decision
     -> next READ_AUTO tool
     -> answer
     -> high-risk clarification
     -> preview request
  -> AnswerVerifier / AnswerGuard
  -> final response
  -> AgentSessionSnapshot / assistant_trace
```

`AgentLoop` 仍然是 `./om assistant` 内部实现；`./om-agent` 只是本地
Tool Gateway。所有工具执行继续走：

```text
AgentLoop -> tool_execution -> agent_tool_registry -> deterministic tool
```

## 5. 核心设计原则

### 5.1 模型负责智能

模型负责：

- 判断用户目标、任务形态和分析角度。
- 从 planner-visible capability view 中选择工具。
- 阅读 observation，决定是否还需要另一个只读工具。
- 识别证据不足、冲突或需要向用户说明的边界。
- 用中文组织面向用户的答案。

模型不是事实源。凡是金额、数量、日期、持仓、状态、候选过滤原因、
配置值、运行状态，都必须来自工具 observation 或明确 missing-data
声明。

### 5.2 代码负责护栏

代码负责：

- schema validation 和 payload normalization；
- `TaskContract` scope 归一化；
- `READ_AUTO` / `SOFT_WRITE_PREVIEW` / `LEDGER_WRITE_CONFIRM` /
  `ADMIN_CONFIRM` 风险裁决；
- tool budget、turn budget、time budget、context budget；
- duplicate call detection；
- output contract 到 `EvidenceBundle` 的转换；
- answer claim verification；
- session trace 和 audit。

### 5.3 单一路径原则

当前 contract 是唯一实现目标：

- planner 必须输出当前 `TaskContract` 需要的字段；
- fixture 必须迁移到当前 schema；
- answer source 收敛到 `EvidenceBundle`；
- 旧 planner 输出和旧 answer route 不保留为并行路径。

## 6. 风险模型

| 风险类 | 自动循环 | 说明 |
|---|---|---|
| `READ_AUTO` | 允许 | 纯读、无 side effect、无 confirm 要求、scope 可控 |
| `SOFT_WRITE_PREVIEW` | 不进入 read loop | 只能生成 pending preview，不能 apply |
| `LEDGER_WRITE_CONFIRM` | 禁止 | 交易、持仓、ledger、projection、config、model 写入必须显式 confirm |
| `ADMIN_CONFIRM` | 禁止 | 通知、服务、release、broker-facing、live tick 只能由 operator 明确触发 |

执行规则：

1. 自动循环只执行 `READ_AUTO`。
2. 任何 preview/write/admin intent 都退出 read loop。
3. confirm/cancel/apply 必须 deterministic-only，并绑定已有 pending operation。
4. 工具 metadata 是风险事实来源，模型不能通过语言声明降低风险等级。

## 7. Planner-Visible Capability View

planner 不应看到完整 `./om-agent spec`。它只能看到经过 Inbound
capability catalog 过滤后的能力：

```json
{
  "intent_name": "candidate_filter_explain",
  "tool_name": "candidate_filter_explain",
  "risk_class": "READ_AUTO",
  "scope_policy": "symbol_market_config_optional",
  "summary": "explain a single symbol's candidate filter trace",
  "required_arguments": ["symbol"],
  "answer_capabilities": ["filter_explain", "candidate_filter_trace"],
  "not_promised": ["rerunning scans", "market data refresh"]
}
```

每次 selection trace 至少记录：

- `selected_tool`
- `selected_intent`
- `selection_reason`
- `selection_source`: `message_text`、`context_projection`、
  `task_contract`、`evidence_gap`
- `scope_source`: `request`、`context`、`planner_declared`、
  `system_injected`
- `risk_class`

不要求 planner 对所有未选工具写 rejected 列表；只要求 selected route
可审计。

## 8. TaskContract

`TaskContract` 是模型计划和代码护栏之间的合同。planner 输出的
`task_contract` 只允许当前 schema 字段；`question`、`planner_declared`
这类字段由 runtime `TaskContract.public_payload()` 补入 session trace，
不是 planner 可写字段。

Planner 输出字段：

```json
{
  "schema_version": "om-agent-task-contract-v1",
  "goal": "诊断 NVDA 候选过滤原因",
  "domain": "candidate",
  "task_mode": "diagnose",
  "requested_effect": "read",
  "intent_families": ["candidate_filter"],
  "scope": {
    "requested_accounts": ["lx"],
    "requested_symbols": ["NVDA"],
    "requested_months": [],
    "config_keys": ["us"]
  },
  "required_answer": ["summary", "cause"],
  "required_evidence": ["diagnostic_evidence"],
  "answer_shape": ["observation", "cause_chain", "evidence_boundary"]
}
```

规则：

- `requested_effect=read` 才能进入 read loop。
- `requested_effect=preview_write` 只能进入 preview operation。
- `requested_effect=prohibited` 必须 deny 或 ask。
- requested scope 是 action safety 和 tool payload injection 的优先依据。
- planner 声明的 scope 必须被代码裁剪、归一化、补默认值。
- 缺少高风险 scope 时才澄清；普通 read scope 缺失应优先用安全默认值
  或回答 missing data。

## 9. Tool Loop 状态机

### 9.1 状态

| 状态 | 含义 | 下一步 |
|---|---|---|
| `planning` | 模型生成 task contract 和首个工具调用 | `guarding` |
| `guarding` | 代码检查 scope/risk/budget/duplicate | `executing` / `blocked` |
| `executing` | deterministic tool 执行 | `observing` |
| `observing` | 写入 observation 和 evidence | `deciding` |
| `deciding` | 模型决定继续或停止 | `guarding` / `answering` / `clarifying` / `previewing` |
| `answering` | 生成并验证最终回答 | `done` / `repair_once` / `fallback` |
| `repair_once` | 明显可恢复缺口的一次补查 | `guarding` / `fallback` |
| `previewing` | 创建 pending preview | `done` |
| `clarifying` | 输出 clarification_request | `done` |
| `blocked` | 拒绝、预算耗尽或风险越界 | `done` |

### 9.2 继续条件

继续下一次工具调用必须同时满足：

1. 模型显式请求下一个工具。
2. 工具属于 `READ_AUTO`。
3. payload 在 normalized requested scope 内。
4. 未超过 `MAX_AGENT_LOOP_TOOL_CALLS`。
5. 未超过 loop iteration / time / context budget。
6. tool + normalized payload 没有重复。
7. 上一次 observation 没有产生不可恢复 policy error。
8. 请求没有跨入 write/admin 边界。

### 9.3 停止条件

任一条件满足即停止自动循环：

- 模型选择回答。
- 模型请求 preview/write/admin。
- 模型请求高风险澄清。
- guard 拒绝工具调用。
- 预算耗尽。
- 工具重复。
- evidence gap 已经对同一 scope 补查一次。
- answer verifier 仍无法通过且没有 deterministic fallback。

## 10. Loop Guard

建议把 guard 作为 `agent_loop.py` 内部清晰函数或小对象收敛，而不是新建
第二控制平面。

输入：

```json
{
  "task_contract": {},
  "tool_name": "analysis_query",
  "payload": {},
  "prior_calls": [],
  "risk_class": "READ_AUTO",
  "budget": {
    "max_tool_calls": 5,
    "max_iterations": 3
  }
}
```

输出：

```json
{
  "allowed": true,
  "decision": "allow",
  "reason": "read_auto_in_scope",
  "normalized_payload": {},
  "trace": {
    "scope_source": "task_contract",
    "risk_class": "READ_AUTO",
    "duplicate_signature": "analysis_query:..."
  }
}
```

拒绝 reason 建议固定枚举：

- `unknown_tool`
- `not_read_auto`
- `scope_violation`
- `duplicate_call`
- `tool_budget_exhausted`
- `iteration_budget_exhausted`
- `write_boundary`
- `admin_boundary`
- `missing_high_risk_scope`

## 11. Observation 和 Evidence

每次工具执行后生成三层结果：

1. raw `tool_result`: deterministic tool response envelope。
2. `ToolObservation`: 给模型看的压缩 observation。
3. `EvidenceBundle`: 给 verifier、trace、answer provenance 用的结构化证据。

`ToolObservation` 建议包含：

```json
{
  "index": 1,
  "tool_name": "candidate_filter_explain",
  "payload": {"symbol": "NVDA", "account": "lx"},
  "ok": true,
  "summary": "NVDA has rejected rows in candidate_filter_trace",
  "data": {},
  "output_contract": {},
  "missing_data": [],
  "conflicts": []
}
```

`EvidenceBundle` 继续作为最终事实源：

- `facts`: 金额、数量、日期、状态、symbol、account 等可验证事实。
- `datasets`: tool output 的结构化摘要。
- `diagnostics`: 诊断类证据。
- `calculations`: 代码认可的衍生计算。
- `missing_data`: 明确缺失项及影响。
- `conflicts`: 工具之间或同工具多次观察的冲突。
- `guard_contracts`: answer verifier 可用的输出合同。

## 12. Answer Verification

Answer verification 是 post-check，不是主循环 driver。

流程：

```text
model answer
-> claim extraction
-> check against EvidenceBundle
-> pass
   or one repair prompt/tool if obvious recoverable gap
   or deterministic fallback
   or answer with missing data
```

规则：

- 金额、百分比、数量、日期、symbol、status 必须能在 evidence 中找到。
- 不能把 missing data 写成事实。
- 不能暴露默认不面向用户的内部 id、SQL、artifact path。
- 如果 verifier 发现一个明显可恢复缺口，最多触发一次 bounded repair。
- repair 后仍失败，输出 deterministic fallback 或明确无法完成。

## 13. Clarification Gate

澄清只用于高风险或 unsafe scope，不用于普通信息不足。

应澄清：

- 用户要求 confirm/cancel/apply，但 operation scope 不唯一。
- 用户要求改交易、持仓、ledger、config、model，但关键字段缺失。
- 用户要求服务、通知、release、broker-facing 操作，但目标不明确。
- 继续读工具会越过用户请求 scope。

不应澄清：

- 普通 read 问题缺少可选 account；可用安全默认或说明范围。
- 某个只读 artifact 不存在；应回答 missing data。
- 工具结果为空；应说明未观察到，而不是强行追问。
- 低风险 follow-up term 可从 `ContextProjection` 安全继承。

`clarification_request` 继续使用现有 response schema，并在 trace 中记录：

- `clarification_reason`
- `scope_source`
- `blocking_fields`
- `risk_class`

## 14. Preview / Write Exit

模型可以识别 preview-write intent，但不能在 read loop 中执行写。

```text
model detects preview_write
-> action_safety
-> deterministic operation handler
-> PendingOperation
-> permission_request
-> final response waiting_for_permission
```

confirm/cancel/apply：

- 不由模型执行。
- 必须匹配已有 pending operation。
- 必须通过 sender/env/HMAC/TTL 等现有 gates。
- 结果进入 `operation_lifecycle` 和 `assistant_trace`。

## 15. Trace 和审计

`AgentSessionSnapshot` 需要覆盖工具循环全链路：

```json
{
  "schema_version": "om-agent-session-v1",
  "task_contract": {},
  "capability_selection": {
    "selected": [],
    "selection_sources": [],
    "risk_classes": []
  },
  "progress": {
    "task_state": "done",
    "tool_calls_used": 2,
    "next_action": "answer"
  },
  "tool_transcript": [],
  "evidence_bundle": {},
  "coverage": {},
  "answer_trace": {
    "answer_route": "llm_from_evidence",
    "scope_source": "task_contract",
    "clarification_reason": null,
    "verification": {}
  }
}
```

新增或确认记录字段：

- `answer_route`
- `scope_source`
- `selection_source`
- `risk_class`
- `loop_stop_reason`
- `duplicate_signature`
- `repair_attempted`
- `clarification_reason`

`assistant_trace` 只读展示这些字段，不执行工具、不修改状态。

## 16. Error Handling

| 错误 | 处理 |
|---|---|
| unknown tool | 拒绝该 tool call，模型可改选 allowed tool；若无可用工具则回答 unsupported |
| permission denied | 作为 tool error observation 给模型，但不继续越权 |
| tool budget exhausted | 回答已取得证据和剩余缺口 |
| duplicate call | 拒绝并停止或要求模型回答 |
| missing artifact | 记录 missing_data，必要时建议 operator refresh |
| stale data | 标明 as_of/freshness，不自动运行 live refresh |
| answer verification fail | 一次 repair；失败后 fallback |
| high-risk missing scope | 输出 clarification_request |

## 17. Implementation Slices

当前落地状态：

| Slice | 状态 | 当前边界 |
|---|---|---|
| Slice 1: Contract 和 trace 收敛 | 已落地 | planner 必须输出当前 `task_contract`；session trace 记录 selection/risk/stop/repair |
| Slice 2: READ_AUTO Tool Loop Guard | 已落地 | `AgentLoop` 内部 guard 统一记录 risk、scope source、duplicate signature；重复 read payload 执行前拒绝 |
| Slice 3: Model-Driven Continuation | 待落地 | 继续沿用 bounded follow-up，不引入无限循环或全局状态机 |
| Slice 4: Evidence 和 Answer Verification 加强 | 待落地 | 先补高价值 read tools 的 fact extraction，不做大而全规则引擎 |
| Slice 5: Regression 和发布门槛 | 待落地 | 以现有 eval/tests 为基线，发现覆盖缺口时再补 case |

### Slice 1: Contract 和 trace 收敛

- planner schema 必须输出当前 `task_contract`。
- 删除旧 planner shape 的并行路径。
- `AgentSessionSnapshot` 记录 `selection_source`、`risk_class`、
  `loop_stop_reason`、`repair_attempted`。
- 更新 eval fixtures 到当前 schema。

验收：

- `./om assistant eval-context --mode scenarios` 通过。
- trace 可解释一次 read answer 的 scope、工具选择和 answer route。

### Slice 2: READ_AUTO Tool Loop Guard

- 从现有 `ToolPolicyEngine` 和 `PURE_READ_TOOLS` 推导 `READ_AUTO`。
- 在 `AgentLoop` 内收敛 scope/risk/budget/duplicate guard。
- duplicate signature 使用 normalized tool name + normalized payload。
- preview/write/admin intent 直接退出 read loop。

验收：

- read tools 可多步。
- write/admin 工具无法进入 loop。
- 重复 tool payload 被拒绝并可 trace。

### Slice 3: Model-Driven Continuation

- prompt/schema 支持模型基于 observation 输出下一步：
  `call_tool`、`answer`、`ask_clarification`、`preview_request`。
- continuation 只能请求 `READ_AUTO`。
- `CoverageVerifier` 仅提供 post-check gap，不主导每次 replan。
- 明显可恢复 gap 最多一次 repair。

验收：

- 多工具问题可由模型连续查询后回答。
- 普通证据缺口不会误触发澄清。
- 一次 repair 后不会形成循环。

### Slice 4: Evidence 和 Answer Verification 加强

- 补齐高价值 read tools 的 output contract / fact extraction。
- `EvidenceBundle` 覆盖 missing_data、conflicts、calculations。
- answer verifier 覆盖金额、数量、日期、symbol、status、rate。
- fallback 优先用 canonical renderer。

验收：

- 用户可见事实可追溯到 evidence。
- unsupported claim 能被拦截或 fallback。
- missing data 被明确说明。

### Slice 5: Regression 和发布门槛

- scenarios 覆盖 preview、read-only、follow-up、scope expansion、
  lowercase symbol、中文 alias、多工具 synthesis。
- 增加 tool-loop tests：预算、重复、risk 越界、一次 repair。
- 保持 release 前 focused tests + docs check。

验收：

- 不引入误澄清。
- planner 不会多查无关工具。
- write/admin 边界不回退。

## 18. Test Plan

最小测试面：

```bash
./om assistant eval-context --mode scenarios
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
```

新增/扩展 focused tests：

- pure read auto allowed。
- non-read tool rejected in AgentLoop。
- duplicate read call rejected。
- tool budget exhausted returns partial evidence。
- high-risk missing scope returns clarification_request。
- low-risk missing artifact returns missing_data answer。
- answer verifier rejects unsupported amount/status/date.
- preview intent creates pending operation, not a read-loop write.

## 19. Acceptance Criteria

用户层面：

- 用户可以用自然语言问复杂运营问题，不需要知道具体工具名。
- assistant 能自动查多个只读工具并组织成一个答案。
- 答案能说明证据边界、缺失数据和不同 accounting view。
- 低风险 read 问题不频繁追问。
- 写入、通知、服务、release 仍需要明确人工确认。

工程层面：

- 所有 tool calls 都经过 `tool_execution` 和 registry metadata。
- 所有自动 tool calls 都是 `READ_AUTO`。
- 所有用户可见事实都来自 `EvidenceBundle` 或明确 missing-data。
- trace 能解释 capability selection、scope、risk、tool transcript、
  loop stop reason 和 answer route。
- 没有旧 planner/output/answer 的并行实现路径。

## 20. Open Questions

1. `READ_AUTO` 初始工具池是否先限于当前 Inbound planner allowlist，
   再逐步扩到更多 pure-read tools？
2. continuation schema 是复用当前 planner schema，还是拆出轻量
   `om-agent-loop-decision-v1`？
3. `analysis_query` 的模型生成 SQL 是否需要更强的 view-level evidence
   hint，以减少无效 query？
4. answer verifier 的 rate/percentage 支持是否先覆盖 monthly income 和
   assigned stock，再扩展到 candidate/risk 领域？

建议默认答案：

- 工具池先从当前 Inbound planner allowlist 扩，不一次性开放全部 pure-read。
- continuation 用轻量 decision schema，避免每轮重建完整 plan。
- `analysis_query` 优先加强 view manifest 和 examples，而不是加业务分支。
- verifier 分领域逐步补 fact extraction，不做大而全的规则引擎。
