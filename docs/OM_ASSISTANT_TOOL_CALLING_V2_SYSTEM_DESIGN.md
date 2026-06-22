# OM Assistant Tool Calling Event Model

本文档是 `./om assistant` 工具调用升级的系统分析和详细设计。
当前命名、维度边界仍以
[OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md) 为准；
能力、风险和可见工具边界以
[OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md) 为准。
实施状态和验收记录见
[OM_ASSISTANT_TOOL_LOOP_COMPLETION_PLAN.md](OM_ASSISTANT_TOOL_LOOP_COMPLETION_PLAN.md)。

本方案替换旧的 "LLM 输出完整 JSON plan，再由系统解析执行" 方向。
后续实现以事件式 Tool Calling Loop 为主路径，不保留旧 JSON plan
作为兼容开关。
文件名保留 `V2` 仅为保持已有链接稳定；本文档的当前语义是事件模型。

## 0. 当前实现现实和边界

当前源码已经完成 model-turn loop cutover：默认 provider 规划入口使用结构化
tool/function call，不再把 provider 的普通 `output_text` JSON 当作生产
成功路径解析执行。当前 provider structured tool-call 主路径已经不再经过
下面这条旧桥：

```text
provider structured tool call
-> ModelToolCallEvent
-> PlannerPlan
-> execute_tool_plan(plan_payload)
-> evidence / answer / trace
```

当前 provider path 已改为：

```text
initial provider model turn
-> ModelToolCallEvent / preview_request / clarification / final answer
-> EventNativePlanningResult
-> run_read_only_agent_loop(...)
-> run_assistant_tool_event_loop(...)
-> guarded tool result / guard-denial observation
-> optional continuation model turn
-> final answer / preview_request / clarification / bounded stop
```

为了保持外层 action/response API 稳定，`run_read_only_agent_loop(...)` 在
执行完 event loop 后仍以 `PerceptionResult(tool_loop)` 承接，但会把
precomputed loop result 交给 `assistant.tool_loop` action wrapper，避免二次
执行。这个 `tool_loop` 是 loop terminal/result envelope，不再是旧式 JSON
plan 执行桥。若 event loop terminal 是 preview operation，最终
inbound response 必须暴露 concrete operation perception/reasoning
（例如 `manual_assignment`、`manual_expiry`、`symbol_edit`），`tool_loop`
只保留在 trace/assistant meta 中解释模型循环来源。

因此本文档后续阶段的真实目标是：

- provider 普通文本 JSON plan 不再被解析或执行；
- provider structured tool calls 不再映射成 `PlannerPlan` 作为生产执行合同；
- `execute_tool_plan(plan_payload)` 不再是 provider structured tool-call 主路径；
- `tool_plan_json_schema()`、`parse_tool_plan_payload()`、legacy JSON planner
  不再回到 assistant 主包或 provider runtime path；
- 可恢复的 schema/scope/safety/tool 错误以 event/tool-result observation
  回喂模型，而不是在 plan 外层追加专用 repair 分支。
- `run_read_only_agent_loop(...)` 直接驱动 model-turn loop；兼容
  `tool_loop` perception 只承接已执行结果。

## 1. 背景

当前 `./om assistant` 已经有 `AgentLoop`、`TaskContract`、
`EvidenceBundle`、`AgentSessionSnapshot`、read-only tool policy、
answer guard、answer verifier 和 `assistant_trace`。基础安全骨架已经存在，
旧的 legacy/custom plan 执行桥曾偏向：

```text
UserMessage
-> legacy JSON fixture or custom planner result
-> PlannerPlan
-> execute_tool_plan(plan_payload)
-> build evidence
-> synthesize answer
```

这个形态有三个问题：

1. 智能性被压成一次性计划，模型不能像 Claude Code 一样观察工具结果后
   自然决定下一步。
2. 可靠性绑定在普通文本 JSON 上，模型只要在 JSON 前后加解释文字，
   就会暴露 `LLM planner returned invalid JSON.`。
3. `TaskContract`、coverage、follow-up、answer synthesis 混在一次性 plan
   合同里，容易把系统护栏误读成模型必须一次性填写的复杂表单。

新的方向是：

```text
UserMessage
-> ModelEvent
-> Host Guard
-> ToolResultEvent
-> ModelEvent
-> ...
-> FinalAnswerEvent
```

模型继续负责理解意图、选择工具、阅读结果、判断是否继续和组织答案；
代码负责 schema、权限、scope、预算、去重、证据和 verifier。

## 2. 核心决策

### 2.1 废弃普通文本 JSON plan

废弃的是这条主路径：

```text
LLM output_text -> JSON object with task_contract + steps -> parse -> execute
```

保留的是底层结构化数据：

- tool input 仍然是结构化对象；
- tool result 仍然是结构化对象；
- trace / audit / session snapshot 仍然是结构化对象；
- provider API 底层可以使用 JSON 传输。

边界是：**模型不再通过普通文本 JSON plan 表达工具计划**。
模型应通过 provider 的结构化 tool call 能力，或内部等价的
`ModelEvent` 通道，表达下一步动作。

进一步的废弃边界是：**生产主路径也不再把结构化 tool call 重新包装成
`PlannerPlan`**。`PlannerPlan` 现在只应作为历史名词出现在文档或负向测试
说明中，不能承担 `AgentLoop` 的 runtime execution contract。

### 2.2 引入事件式 Tool Calling Loop

新的 AgentLoop 主体是事件流，而不是 plan 执行器：

```text
user_message
model_tool_call
tool_guard_decision
tool_result
model_tool_call
tool_guard_decision
tool_result
model_final_answer
loop_stopped
```

每一轮模型只决定下一步，不要求一次性输出完整执行计划。

### 2.3 TaskContract 改为 Host 派生护栏

`TaskContract` 不再是模型每次必须输出的 JSON 字段。

它改为 host 根据以下输入派生和归一化：

- 当前用户消息；
- `ContextProjection`；
- 已接受的 model tool call；
- 已执行的 `ToolResultEvent`；
- 已知 request context，例如 channel、sender、config_key、current_date。

模型仍可通过 tool call 的 `purpose`、`intent`、`scope_hint` 表达理解，
但权限和 scope 权威在 host 派生的 `TaskContract`。

### 2.4 Provider Tool Arguments 不是 JSON Plan

废弃 JSON plan 不等于废弃 provider 协议里的结构化参数。

Chat Completions 兼容 provider 的 function/tool call 通常把
`arguments` 作为 JSON 字符串返回；Responses / tool-use provider 也可能在
wire protocol 中使用 JSON 传输工具参数。OM 的边界是：

- 不从普通文本、Markdown 或 `output_text` 中提取 `steps` plan；
- 只接受 provider 的结构化 tool/function call block；
- tool arguments 仍必须能解析为对象，解析失败属于 provider protocol
  error，不属于旧 JSON plan fallback；
- 对可恢复的 provider protocol error，host 应优先保留 tool name / call id，
  形成 `protocol_error` event 和 model-visible error observation，让模型重发
  结构化 tool call；只有无法形成可绑定 event 时，才允许高置信 deterministic
  explicit command / preview candidate 兜底。

因此用户不应再看到 `LLM planner returned invalid JSON.`；如果底层
tool-call arguments malformed，用户也不应看到 `json.loads` 或
`provider tool call arguments are not valid JSON`，而应进入
`protocol_error -> tool_result observation -> continuation`，或在无法形成
可绑定事件时进入 clarification / 安全 preview fallback / 清晰的无法完成说明。

### 2.5 Tool-Call Path 的 Context Authority

provider structured tool-call 参数是当前模型轮次的执行意图，不是旧 JSON
plan，也不是历史上下文来源证明。host 仍然派生 `TaskContract` 和
`context_use`，但这个派生只用于 schema/scope/risk/budget/duplicate/
hidden-arg/tool-policy guard，以及识别真正继承的上下文。

具体边界：

- 当前消息给出必需 scope 时，模型把自然语言规范化为 tool argument
  是允许的，例如 `sell put` -> `function="sell_put"`。
- `ContextProjection.safe_slots` 只说明历史里可见过哪些安全槽位；不能因为
  历史里也有同名 enum，就把当前 tool argument 判成 inherited。
- `context_use.inherited_slots` 只记录从 referenced turn/evidence ref 继承
  的值；真正继承的 `run_id`、账户、月份、标的等仍需通过 validator。

## 3. 目标和非目标

### 3.1 目标

1. 自然语言请求走模型工具调用路径，而不是新增业务专用 deterministic
   shortcut。
2. 模型可以在同一 turn 内多次调用 `READ_AUTO` 工具，并基于结果决定继续、
   回答、澄清或退出到 preview。
3. 读工具适度放宽；写入、交易、持仓、config、通知、服务、release
   仍由 host 风险模型强约束。
4. 普通文本 JSON 解析失败不再外泄给用户。
5. 所有用户可见事实可追溯到 `EvidenceBundle` 或明确 missing-data。
6. trace 能解释工具选择、scope、risk、guard、tool transcript、
   loop stop reason 和 answer route。
7. provider structured tool-call arguments malformed 时，不阻断已经可安全
   处理的 explicit preview intent，也不把底层解析错误暴露给用户。

### 3.2 非目标

- 不把 `./om-agent` 改造成项目自己的 Agent。
- 不新增第二工具注册表。
- 不把完整 `./om-agent spec` 暴露给远端 Inbound Assistant。
- 不允许模型运行 shell、Python、SQL 写入、broker 操作或通知发送。
- 不照搬 Claude Code 的完整 MCP、文件权限 UI 或多 agent 系统。
- 不保留旧 JSON plan 主路径或旧 planner mode 开关。
- 不把 deterministic natural-language parser 变成普通业务问答的主路由；
  它只能处理显式命令、confirm/cancel/apply、以及 explicit preview 的安全兜底。

## 4. Owner 边界

| 责任 | 当前 owner | 事件模型角色 |
|---|---|---|
| message entry | `src/application/assistant/runtime.py`, `router.py` | 接收请求，选择 deterministic command 或 AgentLoop |
| event loop | `src/application/assistant/agent_loop.py` | 承载 ModelEvent / ToolResultEvent 循环 |
| capability catalog | `capability_catalog.py`, `tool_bindings.py` | 生成模型可见 tool schema 和能力说明 |
| task scope | `task_contract.py` | host 派生、归一化、校验 scope 和 requested effect |
| risk / permission | `tool_policy.py`, `action_policy.py`, `action_safety.py` | 每次 tool call 前裁决 |
| tool execution | `src/application/tool_execution.py`, `agent_tool_registry.py` | 执行 deterministic tool |
| evidence | `evidence.py` | 从 tool result 生成事实和数据集证据 |
| verification | `coverage_verifier.py`, `answer_verifier.py`, `answer_guard.py` | post-check、一次 repair、fallback |
| session trace | `session.py`, `session_store.py` | 记录 event transcript 和 answer trace |
| preview / confirm | `operation_lifecycle.py`, `operation_store.py` | 写路径退出 read loop 后进入 pending operation |

`AgentLoop` 仍然是 `./om assistant` 内部实现；`./om-agent` 只是本地
Tool Gateway。所有工具执行继续走：

```text
AgentLoop -> tool_execution -> agent_tool_registry -> deterministic tool
```

## 5. 事件协议

### 5.1 Event Types

主循环只认识固定事件类型：

| Event | 来源 | 含义 |
|---|---|---|
| `user_message` | host | 当前用户输入和 request context |
| `context_projected` | host | 给模型看的 bounded context |
| `model_tool_call` | model | 模型请求调用一个工具 |
| `tool_guard_decision` | host | host 对工具调用的 allow/deny/ask 裁决 |
| `tool_result` | host | deterministic tool 执行结果 |
| `evidence_updated` | host | EvidenceBundle 增量更新 |
| `model_final_answer` | model | 模型基于证据给出最终回答 |
| `clarification_request` | model or host | 需要用户补充高风险 scope |
| `preview_request` | model | 模型识别到 preview-write intent，退出 read loop |
| `loop_stopped` | host | 预算、重复、权限、完成或失败导致循环停止 |

### 5.2 ModelToolCall

模型请求工具调用的最小结构：

```text
type: model_tool_call
id: call_1
tool_name: monthly_income_report
arguments:
  month: 2026-06
  include_rows: true
purpose: 分析 6 月收益来源
```

这是内部事件结构，不是要求模型在普通文本里输出 YAML 或 JSON。
如果 provider 支持 native tool/function calling，`ModelToolCall` 从
provider 的 tool call block 映射而来。

### 5.3 ToolResultEvent

每次工具执行后生成：

```text
type: tool_result
tool_call_id: call_1
tool_name: monthly_income_report
ok: true
observation: 给模型看的压缩结果
evidence_delta: 给 EvidenceBundle 的结构化事实
missing_data: []
conflicts: []
```

模型只能依据 `observation` 和已有上下文继续判断；最终答案的事实校验依据
`EvidenceBundle`。

## 6. 目标架构

```text
Inbound message
  -> AssistantRequest
  -> ContextProjection
  -> user_message event
  -> model turn
     -> model_tool_call
     -> model_final_answer
     -> clarification_request
     -> preview_request
  -> ToolCallGuard
     -> schema check
     -> risk check
     -> scope check
     -> budget check
     -> duplicate check
  -> tool_execution
  -> tool_result event
  -> EvidenceBundle update
  -> next model turn
  -> AnswerVerifier / AnswerGuard
  -> final response
  -> AgentSessionSnapshot / assistant_trace
```

`6月收益分析` 的期望路径：

```text
User: 6月收益分析
-> model_tool_call monthly_income_report(month=2026-06, include_rows=true)
-> tool_result
-> model_tool_call analysis_query(...)       # 仅当收益来源证据缺口可恢复
-> tool_result
-> model_final_answer
```

如果模型没有发出合法 `model_tool_call` 或 `model_final_answer`，host 记录
`invalid_model_event`，最多做一次 repair；仍失败则安全停止，不执行工具。

## 7. Capability View

模型不看完整 `./om-agent spec`。它只看 Inbound Assistant 过滤后的工具：

```text
tool_name: monthly_income_report
risk_class: READ_AUTO
summary: read monthly option income report from OM local ledger
required_arguments: []
optional_arguments: [account, month, include_rows]
model_notes:
  - Set include_rows=true for analysis, breakdown, source, composition.
not_promised:
  - broker refresh
  - ledger mutation
  - notification send
```

每个工具必须提供：

- `tool_name`
- input schema
- `risk_class`
- read/write classification
- scope policy
- user-facing capability summary
- model-facing notes
- output contract / evidence extraction hints

## 8. ToolCallGuard

每个 `model_tool_call` 必须先过 host guard。

输入：

```text
model_tool_call
host_task_contract
capability_catalog
prior_tool_calls
budget
request_context
```

检查顺序：

1. tool exists。
2. arguments match input schema。
3. tool risk class allowed in current loop。
4. requested effect remains `read` for automatic loop。
5. normalized payload stays within host task scope。
6. no system-scoped arguments from model, such as `config_key` when injected by host。
7. not duplicate tool + normalized payload。
8. tool / iteration / time budget not exhausted。

输出：

```text
type: tool_guard_decision
tool_call_id: call_1
allowed: true
decision: allow
reason: read_auto_in_scope
risk_class: READ_AUTO
scope_source: host_task_contract
duplicate_signature: monthly_income_report:...
normalized_payload: ...
```

拒绝 reason 固定枚举：

- `unknown_tool`
- `schema_invalid`
- `not_read_auto`
- `scope_violation`
- `duplicate_call`
- `tool_budget_exhausted`
- `iteration_budget_exhausted`
- `write_boundary`
- `admin_boundary`
- `missing_high_risk_scope`
- `system_argument_rejected`

## 9. 风险模型

| 风险类 | 模型可请求 | 自动执行 | 说明 |
|---|---|---|---|
| `READ_AUTO` | 是 | 是 | 纯读、无 side effect、scope 可控 |
| `SOFT_WRITE_PREVIEW` | 是 | 否 | 只能退出 read loop，创建 pending preview |
| `LEDGER_WRITE_CONFIRM` | 否 | 否 | 交易、持仓、ledger、projection 写入必须 explicit confirm |
| `CONFIG_WRITE_CONFIRM` | 否 | 否 | config/model 修改必须 explicit confirm |
| `ADMIN_CONFIRM` | 否 | 否 | 通知、服务、release、broker-facing、live tick |

规则：

1. 自动循环只执行 `READ_AUTO`。
2. 模型识别到 preview-write intent 时只能产生 `preview_request`，由现有
   deterministic operation handler 创建 pending operation。
3. confirm/cancel/apply 必须 deterministic-only，并绑定已有 pending operation。
4. 工具 metadata 是风险事实来源，模型不能通过语言声明降低风险等级。

## 10. Loop 状态机

| 状态 | 含义 | 下一步 |
|---|---|---|
| `model_turn` | 模型读取上下文和结果，产生下一事件 | `guarding` / `answering` / `clarifying` / `previewing` |
| `guarding` | host 检查 model tool call | `executing` / `model_turn` / `blocked` |
| `executing` | deterministic tool 执行 | `observing` |
| `observing` | 生成 observation 和 evidence delta | `model_turn` |
| `answering` | 最终回答和 verifier | `done` / `repair_once` / `fallback` |
| `repair_once` | 明显可恢复缺口的一次补查或 answer repair | `model_turn` / `fallback` |
| `previewing` | 创建 pending preview | `done` |
| `clarifying` | 输出 clarification_request | `done` |
| `blocked` | 拒绝、预算耗尽或风险越界 | `done` |

继续下一轮必须同时满足：

1. 模型发出合法 `model_tool_call`。
2. 工具属于 `READ_AUTO`。
3. payload 在 host task scope 内。
4. 未超过 `MAX_AGENT_LOOP_TOOL_CALLS`。
5. 未超过 iteration / time / context budget。
6. tool + normalized payload 没有重复。
7. 上一个错误不是不可恢复 policy error。
8. 请求没有跨入 write/admin 边界。

停止条件：

- 模型给出 `model_final_answer`。
- 模型给出 `preview_request`。
- 模型或 host 给出高风险 `clarification_request`。
- guard 拒绝不可恢复工具调用，或可恢复错误已回喂一次仍失败。
- 预算耗尽。
- 工具重复。
- repair 已经尝试一次。
- verifier 失败且没有 deterministic fallback。

## 11. Evidence 和 Answer

每次工具结果必须经过 Tool Result Adapter，拆成四层：

1. raw tool result: deterministic tool response envelope。
2. model observation: 给模型看的压缩结果。
3. evidence delta: 给 `EvidenceBundle` 和 verifier 的结构化事实。
4. trace payload: 给 `assistant_trace`、审计和 operator debug 的执行收据。

Adapter 合同：

```text
raw_result
  -> model_observation
  -> evidence_delta
  -> trace_payload
```

规则：

- raw result 保留工具原始 envelope，不直接暴露给模型或用户。
- model observation 必须短、稳定、面向下一步判断；不能包含内部 SQL、
  artifact path、异常堆栈或不可见配置。
- evidence delta 必须只包含 deterministic tool 可证实的事实、数据集摘要、
  calculation、missing_data 和 conflict。
- trace payload 记录 tool name、normalized payload、duration、guard decision、
  result size、error code、evidence ids 和 observation summary。
- Adapter 可以从现有 tool metadata 的 output contract /
  evidence extraction hints 派生，但不能让模型文本决定 evidence 结构。

`EvidenceBundle` 是最终事实源：

- `facts`: 金额、数量、日期、状态、symbol、account。
- `datasets`: tool output 的结构化摘要。
- `diagnostics`: 诊断类证据。
- `calculations`: 代码认可的衍生计算。
- `missing_data`: 明确缺失项及影响。
- `conflicts`: 工具之间或同工具多次观察的冲突。
- `guard_contracts`: answer verifier 可用的输出合同。

Answer verification 是 post-check，不是主循环 driver：

```text
model_final_answer
-> claim extraction
-> check against EvidenceBundle
-> pass
   or one repair
   or deterministic fallback
   or answer with missing data
```

规则：

- 金额、百分比、数量、日期、symbol、status 必须能在 evidence 中找到。
- 不能把 missing data 写成事实。
- 不能暴露默认不面向用户的内部 id、SQL、artifact path。
- repair 后仍失败，输出 deterministic fallback 或明确无法完成。

## 12. Clarification Gate

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

## 13. Provider Strategy

### 13.1 主路径

主路径使用 provider 原生 structured tool calling：

- OpenAI: function/tool call 或等价 Responses tool event。
- DeepSeek: Chat Completions `tool_calls`。

host 向 provider 传：

- system instructions；
- context projection；
- allowed tools；
- each tool input schema；
- prior tool results；
- event budget。

host 从 provider 取：

- `model_tool_call`；
- `model_final_answer`；
- `clarification_request`；
- `preview_request`。

### 13.2 Provider Transcript Mapping

Provider transcript 和 OM 内部事件一一映射，不从 assistant
`output_text` 中解析 JSON plan。

| Provider block / message | OM internal event | 说明 |
|---|---|---|
| user text + request context | `user_message` | host 先注入 request metadata、current_date、channel、sender |
| bounded context payload | `context_projected` | host 生成给模型看的上下文，不暴露完整 session/store |
| provider `tool_use` / `tool_calls` | `model_tool_call` | 只接受 provider 结构化工具调用字段 |
| assistant text without tool call | `model_final_answer` or `invalid_model_event` | 必须通过 answer guard/verifier；不能当 JSON plan 解析 |
| provider tool call arguments | `ModelToolCall.arguments` | 结构化对象，进入 schema/scope/risk guard |
| guard allow/deny/ask | `tool_guard_decision` | host 权威，不由模型覆盖 |
| deterministic tool output | `tool_result` + `evidence_updated` | 经过 Tool Result Adapter 后再进入 transcript |
| provider `tool_result` message | next model turn input | 包含 observation 或 error observation，绑定 tool call id |
| model asks user a question | `clarification_request` | 使用现有 response schema |
| model identifies preview-write intent | `preview_request` | 退出 read loop，交给 operation lifecycle |

映射规则：

- provider 的文本块可以和 `tool_use` 同时出现；host 只用结构化
  `tool_use` 执行工具，文本块不参与工具解析。
- provider 如果只返回普通文本，host 只能把它当候选 final answer
  校验，或标记 `invalid_model_event`；不能从文本中提取 JSON plan。
- provider tool-call `arguments` 必须解析为对象。Chat Completions 兼容
  provider 的 `arguments` 是 JSON 字符串时，解析失败应记录为
  `provider_arguments_malformed` / `invalid_model_event`，并在仍能识别
  tool name / call id 时形成 `protocol_error` tool-call event，进入
  model-visible `tool_result` observation continuation；不能把异常文案直接给用户。
- 每个 `tool_result` 回 provider transcript 时必须绑定原始 tool call id，
  让模型能把 observation 或 error 对应到具体调用。
- provider adapter 是窄模块，负责 block/event 转换；schema、scope、risk、
  evidence 和 trace 仍由 OM host 侧模块负责。

### 13.3 不再保留 JSON Plan Fallback

如果 provider 不能稳定提供结构化 tool call，本轮 AgentLoop 应返回
`LLM_UNAVAILABLE` 或 `invalid_model_event`，而不是回退到普通文本 JSON plan。

允许的短期迁移辅助：

- 测试 fixture 可构造 `ModelEvent`；
- 本地 adapter 可把旧 plan fixture 转换成事件，用于迁移旧测试；
- adapter 不作为生产 fallback，不提供 runtime mode flag。

允许的安全 fallback 与旧 JSON plan fallback 不同，且不是 malformed
tool-call 的默认路径：

- 当 provider response 无法形成可绑定 call id / tool name 的 model-visible
  event，且 deterministic router 已经得到同一用户消息的高置信 explicit
  command / preview intent，host 可以选择 deterministic candidate；
- fallback 只允许走现有 operation preview / confirm / cancel 生命周期；
- fallback 不从 provider 普通文本提取工具计划，不执行模型文本中的参数；
- fallback 必须在 trace 中标记 `llm_protocol_error_fallback`，并保留 LLM
  error candidate 供诊断。

### 13.4 Host-owned Preview Payload

Preview-write 类能力不应要求模型复制完整用户原文。

对于 `manual_trade_open`、`manual_trade_close`、`manual_assignment`、
`manual_expiry`，模型只需要表达：

- intent / preview capability；
- 显式出现的低风险 slot，例如 `account`；
- 必要时的澄清原因。

host 负责把当前 `AssistantRequest.text` 注入为 `raw_text`，再交给现有
manual trade parser / operation lifecycle 生成 pending preview。原因：

1. `raw_text` 是 host 已拥有的 request context，不需要模型复述。
2. 长 broker notice 放进 tool-call `arguments` 会增加 provider JSON 字符串
   转义和截断风险，尤其是 Chat Completions 兼容 provider。
3. manual trade parser 已经是 preview payload 的解析边界；模型重复抽取
   symbol、expiration、strike、contracts 只会扩大幻觉面。

Schema 约束：

- provider-visible preview schema 中 `raw_text` 应标记为 host-owned，或从
  model-writable arguments 中移除；
- `account` 可由模型填写，但 host 仍以原文和 request scope 做校验；
- confirm/cancel/apply 仍不能由模型产生，只能由 deterministic command
  绑定 pending operation。

### 13.5 Model-Driven Manifest Budget

provider 一次输入仍需要预算控制，但预算控制不能退回到“host 先替模型选择
具体业务工具”。

当前边界：

- 普通自然语言进入模型驱动 tool loop，provider-visible manifest 同时包含
  按消息裁剪后的 read tools 和 preview capabilities；
- host 可以给出 `preview_authority` 这类 effect-level policy hint，说明当前
  消息是否具备创建 pending preview 的用户授权；
- `preview_authority` 的判定先看解释、分析、收益、状态等 read intent；只有
  explicit record/write/补录 或完整 broker lifecycle/fill notice 才允许 preview；
- host 不再把 broker lifecycle notice 预先收窄成唯一
  `manual_assignment` / `manual_expiry` 工具；
- confirm/cancel/apply 仍跳过 LLM，走 deterministic command；
- `raw_text` 仍由 host 注入，不进入 model-writable arguments。
- 初始 planner manifest 保留 analysis view 字段清单和关键聚合警示；完整字段
  语义按需通过 `analysis_catalog` 获取，避免 provider 输入重新膨胀。

这样让模型负责 capability selection，同时保留 host 对 scope、effect、权限、
预算和 trace 的硬边界。

## 14. Error Handling

| 错误 | 处理 |
|---|---|
| invalid model event | 区分 provider protocol error、无 tool call、非法 control event；可恢复时一次 repair 或 explicit preview fallback，仍失败则安全停止 |
| provider arguments malformed | 不暴露 JSON 解析错误；压缩 manifest/context 后一次重试，或使用高置信 deterministic preview candidate |
| unknown tool | 作为 guard denial 反馈给模型，若无合法下一步则 unsupported |
| schema invalid | 要求模型修正参数；不猜测高风险参数 |
| scope violation | read scope 可回喂模型收窄一次；write/admin scope 直接停止或澄清 |
| permission denied | 记录 `tool_guard_decision`；mutation denial 不继续越权 |
| tool budget exhausted | 回答已取得证据和剩余缺口 |
| duplicate call | 回喂已有 observation 摘要一次；重复仍发生则停止 |
| tool runtime error | read error 记录 missing_data 并可回喂一次；mutation error 停止 |
| missing artifact | 记录 missing_data，必要时建议 operator refresh |
| stale data | 标明 as_of/freshness，不自动运行 live refresh |
| answer verification fail | 一次 repair；失败后 fallback |
| high-risk missing scope | 输出 clarification_request |

### 14.1 Pre-tool Safety Failure Feedback

`planned tool call failed pre-tool safety checks` 这类错误不应直接暴露给用户。
它说明 host 已经拿到模型的工具意图，但在 schema、scope、risk 或 requested
effect 上无法安全执行。

事件模型下的处理方式：

```text
model_tool_call
-> tool_guard_decision allowed=false reason=scope_or_risk_mismatch
-> tool_result ok=false is_error=true observation=...
-> model continuation
-> corrected model_tool_call / clarification_request / unsupported answer
```

这不是让模型绕过安全检查。host 仍然拥有最终权限；模型只获得一次机会，
基于结构化 denial observation 选择更合适的只读工具、退出到 preview，
或向用户提出高风险缺槽澄清。

### 14.2 Recoverable Tool Error Feedback

参考 Claude Code 的 `tool_result is_error` 形态，OM 不应把可恢复工具错误直接
暴露给用户，也不应把它们转成 Python exception。可恢复错误应作为
`tool_result` 风格的 error observation 回喂模型一次，让模型选择合法下一步：

```text
model_tool_call
-> tool_guard_decision allowed=false reason=schema_invalid
-> tool_result ok=false error_code=schema_invalid observation=...
-> next model turn
```

可恢复错误：

| Error | 回喂内容 | 模型可做 |
|---|---|---|
| `unknown_tool` | 工具不可用、可见工具名摘要 | 换成 catalog 内工具或回答 unsupported |
| `schema_invalid` | schema 错误摘要、缺失/错误字段 | 修正参数；不能让 host 猜高风险字段 |
| `scope_violation` for read | 当前 normalized scope 和越界字段 | 收窄查询或输出 scope 不足 |
| `duplicate_call` | duplicate signature 和已有 observation 摘要 | 停止重复，基于已有证据回答 |
| `tool_runtime_error` for read | 稳定错误码和 missing_data 影响 | 换一个 read evidence path 或说明缺口 |

不可恢复边界：

| Error | 处理 |
|---|---|
| `write_boundary` | 停止 read loop；如适用转 preview_request，否则说明不能自动执行 |
| `admin_boundary` | 停止；提示需要 operator 显式操作或 read-only preflight |
| `missing_high_risk_scope` | 输出 clarification_request |
| mutation permission denied | 停止；记录 guard decision，不继续越权 |
| budget exhausted | 回答已取得证据和剩余缺口 |
| repeated recoverable error | 停止；避免无限 repair loop |

限制：

- 同一个 tool call id 只能产生一次 error feedback。
- 同一类 recoverable error 最多给模型一次修正机会。
- error observation 不能暴露 raw provider payload、异常堆栈、内部 SQL、
  secrets、artifact path 或完整 runtime config。
- error feedback 只能帮助模型选择下一步，不能降低风险等级、扩大 scope、
  或绕过 preview/confirm。

用户不应看到：

- `json.loads`；
- `invalid JSON`；
- raw provider payload；
- Python exception；
- internal planner schema error。

用户应看到：

- 没有执行工具时：明确“本次没有生成可执行工具调用，未执行工具”。
- 部分完成时：已取得证据和剩余缺口。
- 需要澄清时：一个具体问题。

### 14.3 Provider Argument Malformation Recovery

`provider tool call arguments are not valid JSON` 是 provider protocol 层错误，
不是用户意图错误，也不是 JSON plan 回退入口。

处理顺序：

1. 记录 compact diagnostic：
   `provider`、`api_kind`、`tool_name`、`error_code`、`argument_shape`、
   `planner_input_chars`、`manifest_chars`、`max_output_tokens`。
   不记录 raw provider payload。
2. 如果 provider response 仍包含 tool name / call id，创建
   `ModelToolCallEvent(arguments={}, protocol_error=...)`，再由 tool loop 生成
   `tool_result is_error` observation，并做一次 bounded continuation。
3. 只有无法形成可绑定 model-visible event，且已有 deterministic candidate
   满足以下条件时，才走 deterministic fallback：
   - candidate 是 explicit command / preview intent；
   - intent 属于现有 preview lifecycle，例如 manual trade / assignment /
     expiry / symbol edit / model use / upgrade preview；
   - deterministic confidence 为 1.0，且 sender / operation policy 允许
     preview；
   - fallback 只创建 pending preview，不 apply。
4. continuation 后仍 malformed 或重复同类错误，则停止，并给用户中文说明：
   “模型没有生成可执行工具调用，未执行任何写入；请重试或改用明确命令。”

禁止：

- 从 malformed `arguments` 字符串里做括号补全、截断修复或正则抽参；
- 从 provider 普通文本中恢复 `tool_name` / `arguments`；
- 在 preview-write 上把缺失高风险字段由 host 猜出来并直接 apply。

### 14.4 Requested Effect Repair

`requested_effect` 是 safety 的核心输入，必须由 host 从用户原文优先派生。

规则：

- `记录开仓`、`记录平仓`、Futu 成交提醒、成功卖出/买入 option fill：
  `preview_write`。
- `期权被指派通知`、`已被指派` 且文本呈现 broker lifecycle notice，或用户
  明确说“记录...被指派/到期被指派平仓”：`preview_write`。
- `期权到期失效通知`、`已到期失效` 且文本呈现 broker lifecycle notice：
  `preview_write`。
- “被指派正股收益/持仓/浮盈/成本/分析”这类查询：`read`。

当模型选择 preview capability 而 host-derived effect 是 `read` 时，优先
检查 host effect inference 是否漏识别 explicit preview；不能立即把它当成
模型越权。只有在原文确实是普通查询时，才拒绝 preview capability。

### 14.5 Candidate Arbitration

LLM-first 不表示 LLM-error-first。

候选选择顺序应满足：

1. deterministic confirm/cancel/apply 永远优先，LLM 不参与。
2. LLM 产出合法 event 且通过 host guard 时，优先走 AgentLoop。
3. LLM 产出 preview intent 但缺少 host-owned payload，由 host 补 request
   context 后进入 preview lifecycle。
4. LLM provider protocol error 若仍能形成 tool call event，先进入
   `protocol_error -> tool_result observation -> continuation`；只有无法形成
   model-visible event 且 deterministic 有同 intent 或高置信 explicit
   preview candidate 时，才选 deterministic fallback，并在 trace 中标记
   `llm_protocol_error_fallback`。
5. LLM safety/permission 明确拒绝且 deterministic 只是低置信猜测时，不
   fallback，返回 clarification 或 unsupported。

这条规则避免“模型协议失败覆盖正确 deterministic 候选”，同时不把
deterministic parser 提升为普通业务问答主路径。

## 15. Trace 和审计

`AgentSessionSnapshot` 需要覆盖事件循环全链路：

```text
schema_version: om-agent-session-v1
event_transcript:
  - user_message
  - context_projected
  - model_tool_call
  - tool_guard_decision
  - tool_result
  - evidence_updated
  - model_final_answer
task_contract:
  source: host_derived
capability_selection:
  selected_tools: [...]
  selection_sources: [...]
  risk_classes: [...]
progress:
  task_state: done
  tool_calls_used: 2
  loop_stop_reason: model_final_answer
answer_trace:
  answer_route: llm_from_evidence
  scope_source: host_task_contract
  verification: ...
```

新增或确认记录字段：

- `event_type`
- `event_id`
- `tool_call_id`
- `answer_route`
- `scope_source`
- `selection_source`
- `risk_class`
- `loop_stop_reason`
- `duplicate_signature`
- `repair_attempted`
- `clarification_reason`
- `invalid_model_event_reason`

`assistant_trace` 只读展示这些字段，不执行工具、不修改状态。

## 16. Implementation Slices

当前落地状态：

| Slice | 状态 | 当前边界 |
|---|---|---|
| Slice 0: 方案切换 | 本文档 | 废弃 JSON plan 目标，确立事件模型 |
| Slice 1: Event Contract | 已落地 | `model_events.py` 定义 model/tool/result/final-answer event |
| Slice 2: Provider Tool Calling | 已落地，外层默认入口已切换 | provider structured tool-call/schema adapter；不解析 output_text JSON plan |
| Slice 3: Guarded Event Executor | 已切入生产 | provider structured read calls 走 `assistant.tool_loop` guarded event executor |
| Slice 4: Model Continuation | 已切入生产默认 event loop | runtime 可将 tool result / recoverable denial 回灌 provider，并解析下一事件 |
| Slice 5: Evidence / Answer Verification | 已切入生产默认 event loop | provider structured read path 从 event tool result 构造 evidence；answer guard 可验证 continuation final answer |
| Slice 6: Remove Output Text JSON Plan Path | 已落地于默认 provider path | provider 普通文本 JSON 不解析；provider tool-call 不再映射到 `PlannerPlan` |
| Slice 7: Regression / Release Gate | 已落地于外层 tool-call cutover | 补自然语言工具调用、lowercase/中文 alias 和 invalid model event 回归 |
| Slice 8: Remove PlannerPlan Runtime Bridge | 已落地于 assistant 主包 | provider event 主路径是 event-native result；legacy JSON plan bridge 不再回到 runtime path |
| Slice 9: Provider Argument / Preview Hardening | 已落地 | malformed tool arguments、host-owned preview payload、requested_effect 和 deterministic preview fallback |
| Slice 10: Model-Driven Capability Selection | 已落地 | 自然语言不再 preview-only 收窄；模型在 read + preview manifest 中选择能力，host 只做 effect/scope/safety guard |
| Slice 11: Model-Turn Loop Cutover | 已落地 | 主入口直接运行 bounded model-turn loop；`planner_repair` 已移出生产成功路径；兼容 `tool_loop` perception 只承接 precomputed result |
| Slice 12: Intelligence Quality Hardening | 计划中 | 不再切架构；围绕工具选择质量、证据补全、低误澄清和 trace/eval 闭环做可量化改进 |

### Slice 1: Event Contract

- 新增内部事件 dataclass / schema。
- 事件 id、tool_call_id、parent_event_id 可追踪。
- `TaskContract` 标记为 `source=host_derived`。
- 旧 fixture 通过测试 adapter 转为 event fixture。

验收：

- 单测可构造 `model_tool_call -> tool_result -> model_final_answer`。
- trace 能按事件顺序展示一次工具调用。

### Slice 2: Provider Tool Calling

- OpenAI / DeepSeek provider 走原生 tool call。
- capability catalog 转为 provider tools schema。
- provider 输出普通文本 JSON plan 不再被接受为生产成功路径。
- provider 没有 tool call 能力时 fail closed。

验收：

- `6月收益分析` 生成 `monthly_income_report(month=2026-06, include_rows=true)`。
- 模型多说一句解释不会导致 invalid JSON，因为不再 parse output_text JSON plan。

### Slice 3: Guarded Event Executor

- 每个 `model_tool_call` 执行前通过 ToolCallGuard。
- `READ_AUTO` 可自动执行。
- preview/write/admin 退出 loop。
- duplicate、budget、scope violation 形成 guard event。

验收：

- read tool 可执行。
- write/admin 工具无法进入自动循环。
- 重复 payload 被拒绝并可 trace。

### Slice 4: Model Continuation

- tool observation 回灌给模型。
- 模型选择 next tool / answer / clarification / preview。
- 明显可恢复缺口最多一次 repair。

当前落地边界：

- `ToolResultEvent.provider_tool_result_payload()` 可转换为 provider
  continuation transcript。
- OpenAI Responses adapter 使用原始 `function_call.call_id` 绑定
  `function_call_output`。
- Chat Completions adapter 使用 assistant `tool_calls` + `tool`
  message 绑定同一个 `tool_call_id`。
- continuation response 的解析顺序固定为：structured tool call 优先，
  然后 structured clarification/preview，最后才是 final answer。
- 普通文本 JSON plan 不会被解析为工具计划，也不会被当作有效
  continuation final answer。
- `continue_model_after_tool_result(...)` 只调用 provider 一次，不形成循环；
  后续是否执行 next tool 交给 host guard / budget 决策。

验收：

- 收益分析可以先查 summary，再按证据缺口补查组成或归因。
- 普通缺数据不会误澄清。
- 不形成无限循环。

### Slice 5: Evidence / Answer Verification

- tool result 生成 evidence delta。
- `EvidenceBundle` 覆盖 missing_data、conflicts、calculations。
- answer verifier 覆盖金额、数量、日期、symbol、status、rate。
- fallback 优先用 canonical renderer。

当前落地边界：

- `ToolResultAdapterOutput` 可转换为现有 `build_evidence_bundle(...)`
  接受的 observation。
- output contract 从 tool registry 解析；payload-dependent contract 使用
  host-normalized payload，不使用模型文本。
- `ModelFinalAnswerEvent` 可通过现有 `verify_answer_guard(...)` 校验。
- unsupported amount / quantity / date / symbol / status / rate 仍由既有
  `answer_verifier.py` 规则判断。
- canonical fallback 从 tool output contract 的 `canonical_renderer` 渲染；
  verifier 失败时可提供 fallback 文本。
- event-level `missing_data` / `conflicts` 会并入 `EvidenceBundle`。
- 当前阶段不改变生产 `AgentLoop` 的 answer path。

验收：

- 用户可见事实可追溯到 evidence。
- unsupported claim 能被拦截或 fallback。
- missing data 被明确说明。

### Slice 6: Remove Output Text JSON Plan Path

- 生产路径不再调用 `parse_tool_plan_payload` 作为 AgentLoop 主入口。
- 删除 `LLM planner returned invalid JSON.` 用户可见错误。
- 旧 planner tests 迁移到 event tests 或负向拒绝测试。
- 不添加 `legacy_json_plan_enabled` 之类开关。

当前落地边界：

- `create_model_turn_events(...)` 默认调用 provider structured tool/function
  calling，不再向 provider 请求 `tool_plan_json_schema()`。
- OpenAI Responses 默认请求使用 `tools` / `tool_choice=auto`。
- Chat Completions 默认请求使用 `tools` / `tool_choice=auto`，不使用
  `response_format={"type":"json_object"}` 作为工具规划路径。
- provider 返回普通 `output_text` JSON plan 时，不解析、不执行，记录为
  `invalid_model_event`。
- legacy JSON planner/parser/schema/executor 不再作为 assistant 主包能力暴露，
  也不是 runtime 兼容开关。
- 当前阶段把同一 provider 响应中的 1-3 个 `ModelToolCallEvent`
  映射到 `EventNativePlanningResult`，并通过 `assistant.tool_loop`
  执行 read tool；不再创建生产 `PlannerPlan`。
- model tool-call path 仍由 host 派生 `context_use` 并执行
  `context_validation`；该派生只用于识别真正继承的上下文，不能把
  provider structured tool-call `arguments` 反向当成需要逐字槽位匹配
  的 NLU 输出。模糊追问不会因为没有 JSON plan 而绕过 scope authority，
  但显式当前请求也不会因为历史 `safe_slots` 多源而被误判为继承上下文。
- event execution / continuation 已由后续 Slice 接入默认 tool loop。

验收：

- 自然语言 read 请求不依赖 output_text JSON。
- invalid model output 被记录为 `invalid_model_event`。
- 用户只看到安全、可恢复的中文提示。

### Slice 7: Regression / Release Gate

覆盖：

- `6月收益分析`
- `6月收益来源`
- `对比 lx sy 6月收益`
- `继续拆收益来源`
- lowercase symbol
- 中文 alias
- preview-write 退出 loop
- confirm/cancel deterministic-only
- invalid model event repair
- duplicate read call
- tool budget exhausted

最小 gate：

```bash
./om assistant eval-context --mode scenarios
python3 -m pytest tests/test_assistant_runtime.py tests/test_assistant_agent_eval.py tests/test_assistant_evidence_session.py
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
```

当前落地边界：

- 补充 `6月收益来源`、`对比 lx sy 6月收益` 的 provider structured
  tool-call 回归，确认不走 `output_text` JSON plan。
- 补充中文 alias（如 `泡泡玛特`）和 lowercase symbol（如 `nvda`）
  经 model tool-call 进入 `TaskContract` scope 的回归。
- 补充 provider 同轮多个 tool call 的桥接回归，避免只执行第一个
  tool call。
- 补充 model tool-call path 的 follow-up context 回归，避免
  `context_use=none` 绕过模糊追问澄清。
- 补充显式当前 scope 压过历史上下文的回归，避免 `sell put` 等自然语言
  被模型规范化为 `sell_put` 后又被 host 误判成 inherited slot。
- `TaskContract` 仅在 planner 的 `symbol` / `symbols` 参数值内允许
  lowercase ticker 归一化，普通英文短语仍不会被当作 symbol。
- 已跑最小 gate，并额外跑 `./om assistant eval-context --format json`、
  `py_compile`、`git diff --check`。

### Slice 8: Remove PlannerPlan Runtime Bridge

目标：

- `create_model_turn_events(...)` 的 provider structured path 返回 event-native
  planning result，而不是 `PlannerPlan`。
- provider `ModelToolCallEvent` 直接进入 guarded event executor。
- `ToolGuardDecisionEvent`、`ToolResultEvent`、`EvidenceUpdatedEvent`
  进入同一 transcript，并可作为 provider continuation 输入。
- pre-tool safety denial、schema invalid、read scope violation、duplicate
  read call、read tool runtime error 都作为 bounded error observation
  回灌模型一次。
- preview/write/admin 边界退出 event loop，进入现有
  operation lifecycle；confirm/cancel/apply 仍是 deterministic command。
- `execute_tool_plan(plan_payload)` 从 provider structured tool-call path
  移除；legacy/custom planner results 只能被拒绝或作为历史/负向测试背景。

最小迁移步骤：

1. 在 `agent_loop.py` 增加 event-loop executor facade，与现有 plan bridge
   并存，但 provider structured 入口只接收 `ModelEvent`。（已落地）
2. 将只读 tool call 执行改为
   `ModelToolCallEvent -> guard -> tool_execution -> ToolResultEvent`。（已落地）
3. 把 evidence / answer verifier 接到 event transcript，而不是 plan step
   transcript。
4. 将 recoverable guard/tool error 作为 provider continuation observation
   回灌一次；超过预算或重复失败则停止并说明缺口。
5. 删除 production path 对 `PlannerPlan`、`parse_tool_plan_payload()`、
   `tool_plan_json_schema()` 的依赖；旧测试完成迁移后不保留 runtime adapter。

验收：

- `sy ... 期权被指派通知...` 这类自然语言写意图不会被错误降级为只读；
  如果缺少安全字段，应得到 preview 或 clarification，而不是
  pre-tool safety raw error。
- `6月收益分析`、`6月收益来源`、`对比 lx sy 6月收益` 可以由模型选择一个
  或多个只读工具并基于 observation 回答。
- provider 返回普通 JSON 文本时仍不解析、不执行。
- provider structured tool call 不再转成 `PlannerPlan`。
- recoverable read/scope/schema 错误最多 repair 一次；不形成无限 loop。
- trace 可展示完整
  `model_tool_call -> guard -> tool_result -> continuation -> final_answer`
  事件链。

### Slice 9: Provider Argument / Preview Hardening

触发背景：

- 远端 ClawBot `deepseek` / Chat Completions 兼容 provider 返回 malformed
  tool-call `arguments`，用户看到
  `provider tool call arguments are not valid JSON`。
- 同一条消息 deterministic candidate 已经正确识别
  `manual_assignment(account=sy, raw_text=...)`，但候选仲裁选择了 LLM
  error，导致正确 preview fallback 没有生效。
- 初始 pre-tool safety 还把 explicit “记录...期权被指派通知”误判为
  `requested_effect=read`，把 `manual_assignment` 当成越权 preview。
- planner input 超过 60k chars，manifest 超过 56k chars，却仍要求模型在
  `arguments` 中复述完整 broker notice，增加 provider JSON 字符串损坏风险。

目标：

1. provider tool-call arguments malformed 不再直接成为用户可见错误。
2. preview-write 能力采用 host-owned `raw_text`，模型不再复制长 broker
   notice。
3. explicit broker lifecycle notice 的 `requested_effect` 由 host 派生为
   `preview_write`。
4. LLM provider protocol error 不覆盖高置信 deterministic preview candidate。
5. 不恢复普通文本 JSON plan，不新增兼容开关。

最小实施步骤：

1. `model_events.py`
   - 将 `_provider_arguments(...)` 的 JSON decode failure 标记为稳定
     details，例如 `reason=provider_arguments_malformed`、`argument_type=str`。
   - 保留 tool name / call id，生成带 `protocol_error` 的
     `ModelToolCallEvent(arguments={})`；不尝试修复字符串，不解析普通文本。
2. `agent_loop.py` / planner manifest
   - preview capability schema 中移除 model-writable `raw_text`，或标注为
     host-owned 且不要求模型填写。
   - preview notes 改为“select preview capability; host injects original
     user message as raw_text”。
   - Slice 9 曾先用 preview-only 子集降低 provider malformed 风险；
     Slice 10 已将该策略升级为模型驱动 manifest：read tools 和 preview
     capabilities 同时暴露，由模型选择，host 用 `preview_authority` 和
     pre-tool guard 做 effect/scope 安全控制。
3. `preview_request.py`
   - 保持从 `question` 注入 `raw_text` 的逻辑，并把它作为唯一权威来源。
   - 如果模型传了 `raw_text`，host 可忽略或覆盖为 request text。
4. `task_contract.py`
   - `_infer_requested_effect(...)` 覆盖 broker lifecycle notice：
     `期权被指派通知`、`已被指派`、`期权到期失效通知`、`已到期失效`，
     但要避免把“被指派正股收益/持仓/PnL”查询误判为 preview。
5. `agent_loop.py`
   - `protocol_error`、unsupported arguments、unknown tool、system-scoped
     arguments 不在 event-plan 外层终止；统一由 guard/pre-tool check 生成
     model-visible error observation。
   - repeated recoverable error 仍停止，避免无限 loop。
6. `runtime.py` / session trace
   - 即使 AgentLoop planning 在 provider protocol 层失败，也应尽量持久化
     compact session/audit trace，方便 `assistant_trace` 看到失败 route。
   - trace 只记录 compact diagnostics，不记录 raw provider payload。

回归用例：

- DeepSeek-style malformed `function_call.arguments` + deterministic
  `manual_assignment` candidate：生成 pending preview，不显示 JSON 错误。
- DeepSeek-style malformed `function_call.arguments` + 无 deterministic
  candidate：中文提示模型未生成可执行工具调用，未执行工具。
- `记录sy 账户的到期被指派平仓 ... 期权被指派通知 ...`：
  `requested_effect=preview_write`，进入 `manual_assignment` preview。
- `sy 期权被指派通知...` 不带“记录”但呈现 broker lifecycle notice：
  得到 preview 或 clarification，不降级成 assigned-stock PnL read。
- `sy 被指派正股收益怎么样`：仍是 read，走 assigned-stock evidence path。
- provider `output_text` JSON plan：仍拒绝，不执行，不 fallback。
- `raw_text` 不出现在 provider-visible preview arguments 的必要字段中，
  trace / assistant_trace 不泄漏完整 raw broker notice。

验收命令：

```bash
python3 -m pytest tests/test_assistant_model_events.py tests/test_assistant_runtime.py -k "provider_tool_call or manual_assignment or manual_expiry or requested_effect or deterministic_fallback" -q
python3 -m pytest tests/test_inbound_control.py tests/test_inbound_feishu_ws.py -q
./om assistant eval-context --mode scenarios
```

远端验收：

1. 升级到包含 Slice 9 的版本。
2. 在 ClawBot 输入：
   `记录sy 账户的到期被指派平仓 期权被指派通知: ... -2张PDD 260618 85.00P期权已被指派 ...`
3. 期望返回交易记录预览，不写入账本，并提示“确认记录/取消记录”。
4. `assistant_trace` 或 audit 能解释：
   LLM protocol error 是否发生、是否通过 model-visible observation 修复、
   是否 deterministic fallback、最终 preview operation id、未 apply。

### Slice 10: Model-Driven Capability Selection

触发背景：

- Slice 9 解决了 provider malformed arguments 和 preview fallback，但
  `preview_request_kind_from_text(...)` 仍在 host 侧提前把自然语言收窄成
  某一个 preview capability。
- 这提高了稳定性，却削弱了模型“理解意图、选择工具、观察结果、继续决策”的
  核心职责。

目标：

1. 普通自然语言默认进入模型驱动 tool loop。
2. provider-visible manifest 同时包含 read tools 和 preview capabilities。
3. host 不再用 broker notice classifier 选择具体 preview tool。
4. host 只保留 effect-level preview authority、scope/safety guard、budget、
   duplicate detection 和 trace。
5. deterministic natural-language parser 降级为 preview 内部 normalizer 和
   provider protocol malformed 的安全 fallback；显式 command 仍 deterministic。

已落地边界：

- `_planner_input_payload(...)` 不再生成 `preview_only` manifest。
- provider tool-call schema 不再按 read-only-only 过滤 preview capabilities。
- `manual_trade_open`、`manual_trade_close`、`manual_assignment`、
  `manual_expiry` 的 provider-visible schema 仍不包含 `raw_text`。
- `preview_authority` 只表达是否允许 pending preview，不选择具体 tool。
- 普通 read 查询即使 manifest 中可见 preview capability，也会因
  `preview_authority=false` 和 pre-tool safety 防止误创建 pending preview。
- 解释/分析 broker notice 或 fill notice 的问题仍是 read intent；例如
  `期权被指派通知是什么意思`、`成交提醒收益分析` 不会创建 pending preview。
- analysis view manifest 默认压缩为字段清单和关键聚合提示，详细
  `field_semantics` 由 `analysis_catalog` 作为 follow-up evidence 获取。

回归重点：

- broker assignment/expiry notice：模型可见 read + preview tools，并应选择
  对应 preview capability。
- assigned-stock 收益/状态问题：仍走 read evidence path，不被 preview
  authority 误拦截。
- notification explanation / fill analysis：保持 read-only，不被 raw notice
  关键词误判为 preview-write。
- model-driven manifest payload：常见收益分析、assignment notice、assigned-stock
  分析输入保持在预算内，不回到 50k+ manifest。
- provider malformed `arguments`：仍可使用 high-confidence deterministic
  preview fallback，但不恢复普通文本 JSON plan。

### Slice 11: Model-Turn Loop Cutover

已落地状态：

- 初始 provider call 和后续 continuation 都按 model turn 记录；
- `run_read_only_agent_loop(...)` 在有 `request` 和 `execute_tool_fn` 的生产
  runtime 中直接运行 `run_assistant_tool_event_loop(...)`；
- 可恢复的 read/preview effect mismatch、pre-tool denial、duplicate 和
  tool/runtime error 进入 event transcript，由 continuation 读取 observation；
- 不可恢复的 validation failure 以 `agent_loop.runtime=model_turn_loop`
  的 rejected trace 返回，不再回退到 legacy planning adapter；
- trace 使用 `runtime`、`model_turns`、`loop_stop_reason`、`answer_route`、
  `capability_selection`，不再写 `agent_loop.planner=llm_tool_plan`。

当前目标：

1. 初始 provider call 和后续 continuation 统一称为 model turn。
2. `run_read_only_agent_loop(...)` 直接调用 `run_assistant_tool_event_loop(...)`，
   由 loop 负责执行 read tools、terminal preview、clarification 和 final answer。
3. schema invalid、scope expansion、effect mismatch、preview authority mismatch、
   duplicate、tool runtime error 等可恢复问题，都作为 model-visible
   observation 回灌下一轮，而不是走 `planner_repair`。
4. preview request 是 loop terminal：host 创建 pending preview 后停止，不再
   继续 read/write。
5. diagnostics 和 trace 暴露 `model_turns`、`observations`、`stop_reason`、
   `answer_route`，不再把成功路径描述为 planner repair。

实现边界：

- `create_model_turn_events(...)` 是 model-turn 入口：它只返回 provider
  event transcript 派生的 `event_plan`、trace 或 error，不再暴露 JSON
  plan 字段。
- `_planning_outcome_from_event_model_turn_result(...)` 仅保留给缺少
  `request` / `execute_tool_fn` 的 adapter 调用；生产 runtime 具备这两个
  参数时直接进入 bounded model/tool loop。
- 外层仍使用 `PerceptionResult(tool_loop)` 承接 precomputed event-loop
  result，action wrapper 不会重新执行 provider events；但 preview
  operation response 的 `data.perception` / `data.reasoning` / assistant
  decision execution contract 必须切回 concrete operation，避免把内部
  envelope 暴露成用户/API 语义。
- explicit command（`/confirm`、`/cancel`、`/assigned-stock` 等）仍由
  deterministic command path 处理；自然语言 broker notice 不回到
  deterministic parser 作为主 broker。
- host 继续拥有写权限硬闸、preview payload normalization、raw_text 注入、
  account/symbol/effect safety 和 pending operation lifecycle。

验收：

- `6月收益分析` 可以执行 initial model turn、read tool、continuation
  final answer，trace 中不出现成功态 `planner_repair`。
- `analysis_catalog -> analysis_query -> final answer` 证明模型能基于证据缺口
  决定下一步。
- broker assignment/expiry notice 如果第一轮错选 read tool，应被 guard
  observation 拦截，下一轮选择对应 preview capability。
- duplicate/repeated recoverable failure 会停止，不会无限调用 provider。
- preview requested 后 terminal，不再继续工具调用。
- 现有 preview、read-only、follow-up、scope expansion、lowercase symbol、
  中文 alias scenario regression 仍通过。

已验证：

- `python3 -m pytest tests/test_assistant_runtime.py -q`
- `python3 -m pytest tests/test_assistant_model_events.py tests/test_assistant_model_continuation.py tests/test_assistant_event_executor.py tests/test_assistant_model_evidence.py -q`
- `python3 -m pytest tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_diagnostics.py -q`
- `./om assistant eval-context --mode scenarios`

### Slice 12: Intelligence Quality Hardening

Slice 12 的目标不是再做一次架构切换，而是在已经落地的 bounded
model-turn loop 上提升智能质量。判断标准从“是否像 Claude Code 一样循环”
转为“模型是否稳定选对工具、补齐证据、少误澄清、失败时可解释”。

#### 12.1 问题定义

当前机制已经具备：

- 模型通过 provider structured tool call 选择 read tool 或 preview
  capability；
- host 每次执行前做 schema、scope、risk、effect、budget 和 duplicate guard；
- 可恢复错误进入 model-visible observation，再给模型一次 continuation；
- preview / clarification / final answer / budget 都是一等 terminal。

剩余智能问题集中在四类：

1. 工具选择质量：模型有时能看到正确工具，但 selection hint 不够聚焦，
   容易在 `monthly_income_report`、`analysis_query`、
   `option_positions_read`、preview capability 之间选错。
2. 证据策略：模型知道可以继续调用工具，但不知道缺口应该由哪个 read tool
   恢复，或在已有证据足够时仍重复调用。
3. 误澄清：低风险 read 场景缺少可选 account、artifact 为空或 follow-up
   能安全继承上下文时，不应进入 clarification。
4. 诊断闭环：现在 trace 能记录 loop 过程，但还需要把“为什么选这个工具、
   为什么停止、缺口是否可恢复”变成回归可检查的字段。

#### 12.2 成功标准

用户层面：

- 用户继续用自然语言，不需要知道工具名。
- read-only 分析类问题能自动调用 1 到 3 个只读工具并组织答案。
- broker lifecycle notice 在具备 preview authority 时进入 pending preview；
  解释、收益、持仓类问题保持 read-only。
- 不把低风险缺数据变成追问；只在高风险写入 scope 缺失时澄清。
- 出错时用户看到的是 missing-data、clarification 或 unsupported，不看到
  provider JSON、planner、traceback、pre-tool safety internal error。

工程层面：

- 不新增 JSON plan、planner repair、runtime compatibility switch。
- 不新增第二套工具注册表或自然语言 deterministic business router。
- `AgentLoop` 仍只自动执行 `READ_AUTO`。
- 每个 final answer 都能从 `EvidenceBundle`、tool observation 或
  explicit missing-data 找到依据。
- trace 中能检查 `capability_selection`、`evidence_gap`、
  `allowed_next_actions`、`loop_stop_reason`、`answer_route`。

#### 12.3 设计原则

1. 模型负责智能决策：工具选择、是否继续、如何组织答案交给模型。
2. 代码负责边界：schema、scope、risk、effect、预算、去重和 evidence
   verification 仍由 host 执行。
3. 提示和 manifest 只提供选择依据，不替模型硬编码业务路由。
4. 所有增强必须能被 scenario/eval/trace 验证。
5. 改动优先落在现有 owner：`capability_catalog.py`、`tool_bindings.py`、
   `agent_loop.py`、`coverage_verifier.py`、`model_evidence.py`、
   `diagnostics.py`、`context_eval.py`。

#### 12.4 实施切片

##### Slice 12A: Loop Scenario Matrix

目标：先把“智能质量”变成可回归的场景，而不是靠体感。

改动点：

- 扩展现有 assistant scenario/eval fixture，覆盖用户真实入口：
  - `6月收益分析`；
  - `sy 6月收益来源`；
  - `被指派正股现在盈亏`；
  - `期权被指派通知...已被指派`；
  - `期权到期失效通知...已到期失效`；
  - `这个净收入怎么算` 这类 follow-up；
  - `为什么 PDD 没进候选`；
  - 中文 alias、lowercase symbol、账户缺省、scope expansion。
- 每个场景记录最小期望：
  - expected first tool family；
  - allowed terminal：final answer / preview / clarification / unsupported；
  - forbidden terminal：unexpected clarification、write mutation、legacy JSON
    planner error；
  - trace assertion：`loop_stop_reason`、`answer_route`、
    `capability_selection.selected`。

候选文件：

- `tests/fixtures/assistant_context_scenarios.jsonl`
- `src/application/assistant/context_eval.py`
- `tests/test_assistant_agent_eval.py`
- `tests/test_assistant_runtime.py`

验收：

- `./om assistant eval-context --mode scenarios`
- 新增或更新的 scenario 能失败定位到 tool selection、evidence gap、
  clarification gate 或 answer route。

##### Slice 12B: Capability Selection Hints

目标：让模型更容易选对工具，但不让 host 预先替模型选工具。

改动点：

- 在 provider-visible manifest 中补齐每个关键能力的选择边界：
  - `monthly_income_report`：月度/累计收益、收入来源、现金流组成；
  - `analysis_query`：跨账户、跨月份、分组、趋势、候选/交易/持仓组合分析；
  - `analysis_catalog`：字段未知或 view 不确定时先查；
  - `option_positions_read`：当前持仓、assigned stock、到期/指派后的持仓状态；
  - `manual_assignment` / `manual_expiry`：只处理 broker lifecycle notice 或
    explicit record preview，不处理解释/收益/持仓查询。
- 每个工具补 `not_for` 或等价 selection note，减少相邻工具混淆。
- `preview_authority` 继续是 effect-level hint，只说明是否允许 pending
  preview，不指定具体 preview tool。
- manifest 继续保持 bounded，不把完整 `./om-agent spec` 暴露给 assistant。

候选文件：

- `src/application/assistant/tool_bindings.py`
- `src/application/assistant/capability_catalog.py`
- `src/application/assistant/agent_loop.py`
- `tests/test_assistant_runtime.py`

验收：

- notice 解释类问题不会创建 preview。
- explicit lifecycle notice 能选择 preview capability。
- 月度收益分析优先走 `monthly_income_report`，必要时再补
  `analysis_query`。
- 字段未知的 open-ended analysis 能先走 `analysis_catalog`，再走
  `analysis_query`。

##### Slice 12C: Evidence Gap Guidance

目标：代码不替模型回答，但要把“缺什么证据、可由哪个工具恢复”反馈清楚。

改动点：

- 统一 evidence gap 结构：
  - `gap_type`；
  - `required_fact`；
  - `current_evidence_refs`；
  - `recoverable`；
  - `suggested_tool`；
  - `allowed_next_actions`。
- `coverage_verifier.py` 只判断缺口和可恢复性，不直接生成下一步 plan。
- `tool_result` observation 中压缩展示 gap summary，让 continuation 可以选择
  下一步。
- repeated same gap / same duplicate signature 后停止，避免无限补查。

候选文件：

- `src/application/assistant/coverage_verifier.py`
- `src/application/assistant/model_evidence.py`
- `src/application/assistant/agent_loop.py`
- `tests/test_assistant_event_executor.py`
- `tests/test_assistant_model_evidence.py`

验收：

- `monthly_income_report -> analysis_query -> final answer` 可以通过 gap
  guidance 触发。
- 工具结果为空时回答 missing-data，不误澄清。
- duplicate 或 repeated recoverable error 不超过 bounded continuation。

##### Slice 12D: Clarification False-positive Guard

目标：澄清只拦高风险缺槽，不阻断普通 read 智能。

改动点：

- 按 risk class 审计 clarification gate：
  - `READ_AUTO` 缺可选 account/month/symbol 时，优先用安全默认、上下文继承
    或 missing-data；
  - `SOFT_WRITE_PREVIEW` 缺 account、operation scope、broker notice raw text
    等关键字段时才澄清；
  - confirm/cancel/apply scope 不唯一必须澄清或拒绝。
- trace 记录 `clarification_reason`、`blocking_fields`、`risk_class`。
- eval 中新增 forbidden clarification 断言。

候选文件：

- `src/application/assistant/action_safety.py`
- `src/application/assistant/task_contract.py`
- `src/application/assistant/agent_loop.py`
- `src/application/assistant/session_store.py`
- `tests/test_assistant_runtime.py`

验收：

- 低风险 read 空结果不追问。
- follow-up 能安全继承明确上一轮 scope。
- 高风险 preview 缺 account 时返回现有 `clarification_request` schema。

##### Slice 12E: Trace / Diagnostics Contract

目标：把模型智能路径变成可审计资产，方便本地和远端 ClawBot 问题定位。

改动点：

- diagnostics/live probe 展示：
  - selected capability；
  - model turns；
  - tool observations；
  - evidence gaps；
  - stop reason；
  - answer route；
  - preview receipt 或 clarification request。
- `assistant_trace` compact view 保留用户可理解字段，不暴露 raw provider
  payload。
- 对 provider malformed / guard denial / preview fallback 记录稳定 reason
  code，方便远端日志检索。

候选文件：

- `src/application/assistant/diagnostics.py`
- `src/application/assistant/session.py`
- `src/application/assistant/session_store.py`
- `src/application/agent_tools/diagnostics.py`
- `tests/test_assistant_diagnostics.py`
- `tests/test_agent_plugin_contract.py`

验收：

- 远端出现用户反馈时，可以通过 trace 判断是 selection 错、schema 错、
  guard 拒绝、evidence 缺口还是 provider protocol error。
- trace schema 改动同步到 agent plugin contract 测试。

#### 12.5 开工顺序

1. 先做 Slice 12A，只补 eval/scenario 和 trace assertion，不改 runtime
   行为。
2. 根据失败场景进入 Slice 12B，最小调整 provider-visible manifest 和
   selection hints。
3. 如果失败来自“模型不知道下一步补什么”，再做 Slice 12C。
4. 如果失败来自误澄清，再做 Slice 12D。
5. 最后做 Slice 12E，把新字段纳入 diagnostics 和 contract。

每一步都必须先有失败场景或真实远端反馈对应，不做泛化重构。

#### 12.6 回归门禁

Slice 12 每个提交前至少运行：

```bash
python3 -m pytest tests/test_assistant_runtime.py tests/test_assistant_event_executor.py tests/test_assistant_model_events.py tests/test_assistant_model_continuation.py tests/test_assistant_model_evidence.py -q
python3 -m pytest tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_diagnostics.py -q
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q
python3 -m pytest tests/test_inbound_control.py tests/test_inbound_feishu_ws.py -q
./om assistant eval-context --mode scenarios
git diff --check
```

如果只改文档，只需要：

```bash
git diff --check
```

#### 12.7 不做项

- 不恢复普通文本 JSON plan。
- 不恢复 `PlannerPlan` runtime bridge。
- 不恢复 `planner_repair` 成功路径。
- 不新增 `legacy_json_plan_enabled` 或 `use_event_loop_v2` 开关。
- 不把 deterministic natural-language parser 提升为普通业务主路由。
- 不让模型自动 confirm/cancel/apply、写 ledger、写 position、改 config、
  发通知或操作 broker-facing state。
- 不把完整 Tool Gateway manifest 暴露给 inbound assistant。

#### 12.8 当前执行记录

2026-06-20 已完成 Slice 12A 的最小落地：

- `context_eval` scenario 输出新增 decision 摘要：
  `terminal`、`tool_call_count`、`first_tool`、`first_tool_family`、
  `requested_effect`、`requires_clarification` 和首个工具参数摘要。
- scenario fixture 从 10 个扩展到 17 个，新增覆盖：
  - `6月收益分析`；
  - assignment broker notice preview；
  - expiry broker notice preview；
  - assignment notice explanation 保持 read/no-tool；
  - assigned stock PnL read；
  - standalone candidate filter diagnosis；
  - `净收入` follow-up 继承 income scope。
- 每个新增场景都声明 allowed / forbidden terminal、first tool 或 forbidden
  tools，防止智能化升级回退成误澄清、误 preview 或错误 read 工具。
- CLI 文本输出现在展示 `terminal=... tool=...`，远端 ClawBot 问题可以先用
  `./om assistant eval-context --mode scenarios` 看离线路径是否符合预期。

已验证：

```bash
python3 -m pytest tests/test_assistant_context_eval.py -q
python3 -m pytest tests/test_cli_operator_commands.py::test_assistant_eval_context_command_renders_report -q
python3 -m pytest tests/test_assistant_agent_eval.py::test_assistant_context_eval_report_covers_planner_context_decisions -q
./om assistant eval-context --mode scenarios
git diff --check
```

## 17. Acceptance Criteria

用户层面：

- 用户可以用自然语言问复杂运营问题，不需要知道具体工具名。
- assistant 能在 bounded model-turn loop 中自动选择一个或多个只读工具，
  观察工具结果，再组织成一个答案。
- `6月收益分析` 走模型工具调用路径，而不是 deterministic shortcut。
- 不再暴露 `LLM planner returned invalid JSON.`。
- 不再暴露 `provider tool call arguments are not valid JSON`。
- broker lifecycle notice 可以生成 pending preview；失败时给 clarification
  或“未执行工具”的中文说明。
- 低风险 read 问题不频繁追问。
- 写入、通知、服务、release 仍需要明确人工确认。

工程层面：

- 所有 tool calls 都经过 `tool_execution` 和 registry metadata。
- 所有自动 tool calls 都是 `READ_AUTO`。
- 生产 AgentLoop 不依赖 output_text JSON plan。
- 生产 AgentLoop 不把 provider structured tool call 映射成
  `PlannerPlan` 作为执行合同。
- `execute_tool_plan(plan_payload)` 不再是 provider structured tool-call
  read loop 的执行路径。
- 生产成功路径不依赖 `planner_repair`；可恢复错误只能通过
  model-visible observation continuation 处理。
- 初始 provider call 和 continuation 使用同一 transcript 语义，trace 可显示
  model turns 和 stop reason。
- 所有用户可见事实都来自 `EvidenceBundle` 或明确 missing-data。
- trace 能解释 event transcript、capability selection、scope、risk、
  loop stop reason 和 answer route。
- trace 能解释 provider protocol error、deterministic preview fallback、
  host-owned `raw_text` 注入和 preview operation receipt。
- 没有旧 planner/output/answer 的并行实现路径。

## 18. Open Questions

已决策：

- provider event adapter 拆到 `model_events.py`。
- provider continuation transcript adapter 拆到 `model_continuation.py`。
- runtime 默认 event loop 已接入一次 bounded provider continuation；不加
  runtime compatibility switch。
- preview-write 的长原文 payload 由 host 注入；模型只选择 preview
  capability 和低风险 slot。
- provider protocol malformed 不恢复普通文本 JSON plan；只允许 bounded
  retry 或 high-confidence deterministic preview fallback。
- 旧 `plan_read_only_tools(...)` facade 已移除；诊断和测试统一使用
  `create_model_turn_events(...)`。

仍开放：

1. `TaskContract` host 派生第一版是否只覆盖 domain/task_mode/requested_effect/scope，
   还是同时派生 required_evidence / answer_shape？
2. `analysis_query` 的 schema 是否需要拆成更窄的 view/query builder tool，
   以减少模型直接写 SQL 的错误面？
3. read-only 并发是否第一版只允许同一 model turn 里多个互不依赖 read tool，
   还是先串行保持简单？
4. provider malformed arguments 的 compact retry 是否应复用同一 provider
   conversation，还是重新发一个更小 planner payload？
建议默认答案：

- `TaskContract` 第一版只派生执行护栏必需字段，answer 需求继续由
  evidence/verifier 补齐。
- `analysis_query` 先保持现状，但强制 view/column allowlist。
- 第一版串行执行 read tool；确认稳定后再引入 read-only batch concurrency。
- malformed arguments 第一版优先用更小 planner payload 重试一次；避免把
  半损坏 provider response 继续带入下一轮。
