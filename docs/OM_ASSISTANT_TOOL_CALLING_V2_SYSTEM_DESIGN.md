# OM Assistant Tool Calling Event Model

本文档是 `./om assistant` 工具调用升级的系统分析和详细设计。
当前命名、维度边界仍以
[OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md) 为准；
能力、风险和可见工具边界以
[OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md) 为准。
待完成实施方案见
[OM_ASSISTANT_TOOL_LOOP_COMPLETION_PLAN.md](OM_ASSISTANT_TOOL_LOOP_COMPLETION_PLAN.md)。

本方案替换旧的 "LLM 输出完整 JSON plan，再由系统解析执行" 方向。
后续实现以事件式 Tool Calling Loop 为主路径，不保留旧 JSON plan
作为兼容开关。
文件名保留 `V2` 仅为保持已有链接稳定；本文档的当前语义是事件模型。

## 0. 当前实现现实和差距

当前源码已经完成主路径切换：默认 provider 规划入口使用结构化
tool/function call，不再把 provider 的普通 `output_text` JSON 当作生产
成功路径解析执行。当前 provider structured tool-call 主路径也已经不再经过
下面这条旧桥：

```text
provider structured tool call
-> ModelToolCallEvent
-> PlannerPlan
-> execute_tool_plan(plan_payload)
-> evidence / answer / trace
```

当前 provider path 已改为 `ModelToolCallEvent -> EventNativePlanningResult
-> assistant.tool_loop -> run_assistant_tool_event_loop(...)`。legacy JSON
planner/parser/schema/executor、`PlannerPlan` runtime bridge 和旧 synthesis
callback API 已从 `src/application/assistant` 当前主包清理；后续工作重点转为
文档、诊断、eval 和回归门禁收口，防止旧心智模型回潮。

因此本文档后续阶段的真实目标是：

- provider 普通文本 JSON plan 不再被解析或执行；
- provider structured tool calls 不再映射成 `PlannerPlan` 作为生产执行合同；
- `execute_tool_plan(plan_payload)` 不再是 provider structured tool-call 主路径；
- `tool_plan_json_schema()`、`parse_tool_plan_payload()`、legacy JSON planner
  不再回到 assistant 主包或 provider runtime path；
- 可恢复的 schema/scope/safety/tool 错误以 event/tool-result observation
  回喂模型一次，而不是在 plan 外层追加专用 repair 分支。

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

### 3.2 非目标

- 不把 `./om-agent` 改造成项目自己的 Agent。
- 不新增第二工具注册表。
- 不把完整 `./om-agent spec` 暴露给远端 Inbound Assistant。
- 不允许模型运行 shell、Python、SQL 写入、broker 操作或通知发送。
- 不照搬 Claude Code 的完整 MCP、文件权限 UI 或多 agent 系统。
- 不保留旧 JSON plan 主路径或旧 planner mode 开关。

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

## 14. Error Handling

| 错误 | 处理 |
|---|---|
| invalid model event | 一次 repair；仍失败则安全停止，不执行工具 |
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

- `plan_read_only_tools(...)` 默认调用 provider structured tool/function
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
  `context_validation`；模糊追问不会因为没有 JSON plan 而绕过 scope
  authority。
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
- `TaskContract` 仅在 planner 的 `symbol` / `symbols` 参数值内允许
  lowercase ticker 归一化，普通英文短语仍不会被当作 symbol。
- 已跑最小 gate，并额外跑 `./om assistant eval-context --format json`、
  `py_compile`、`git diff --check`。

### Slice 8: Remove PlannerPlan Runtime Bridge

目标：

- `plan_read_only_tools(...)` 的 provider structured path 返回 event-native
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

## 17. Acceptance Criteria

用户层面：

- 用户可以用自然语言问复杂运营问题，不需要知道具体工具名。
- assistant 能在一次初始 provider 响应中自动选择多个只读工具，并由
  现有 AgentLoop 组织成一个答案。
- `6月收益分析` 走模型工具调用路径，而不是 deterministic shortcut。
- 不再暴露 `LLM planner returned invalid JSON.`。
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
- 所有用户可见事实都来自 `EvidenceBundle` 或明确 missing-data。
- trace 能解释 event transcript、capability selection、scope、risk、
  loop stop reason 和 answer route。
- 没有旧 planner/output/answer 的并行实现路径。

## 18. Open Questions

已决策：

- provider event adapter 拆到 `model_events.py`。
- provider continuation transcript adapter 拆到 `model_continuation.py`。
- runtime 默认 event loop 已接入一次 bounded provider continuation；不加
  runtime compatibility switch。

仍开放：

1. `TaskContract` host 派生第一版是否只覆盖 domain/task_mode/requested_effect/scope，
   还是同时派生 required_evidence / answer_shape？
2. `analysis_query` 的 schema 是否需要拆成更窄的 view/query builder tool，
   以减少模型直接写 SQL 的错误面？
3. read-only 并发是否第一版只允许同一 model turn 里多个互不依赖 read tool，
   还是先串行保持简单？

建议默认答案：

- `TaskContract` 第一版只派生执行护栏必需字段，answer 需求继续由
  evidence/verifier 补齐。
- `analysis_query` 先保持现状，但强制 view/column allowlist。
- 第一版串行执行 read tool；确认稳定后再引入 read-only batch concurrency。
