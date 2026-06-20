# OM Assistant Tool Loop Completion Plan

本文档记录 `./om assistant` 事件式 tool loop 升级的剩余完成方案。
它是实施清单，不是新的架构设计入口。

架构目标以
[OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md](OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md)
为准；术语和入口边界以
[OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md) 为准。

本方案只升级 `./om assistant`。`./om-agent` 仍是 Tool Gateway，负责工具
manifest、注册、执行封装和权限硬闸，不是项目自己的 Agent。

## 1. 背景

这轮升级的目标不是继续扩大工具数量，而是把 assistant 从旧的
"LLM 输出 JSON plan -> host 解析执行" 收敛到事件式 tool loop：

```text
user message
  -> assistant context / contract projection
  -> model emits structured tool call / preview / clarification / final answer event
  -> host guard checks schema, scope, safety, budget, duplicate
  -> READ_AUTO tool executes through Tool Gateway
  -> tool result becomes event transcript evidence
  -> model continues or stops with final answer
```

这个方向借鉴 Claude Code 的循环形态：模型选择工具、观察结果、继续推理、
直到有足够证据回答。但 OM 不照搬 Claude Code 的完整权限模型。OM 的最大风险
集中在交易、持仓、配置、通知和 broker-facing state，因此自动循环只允许
`READ_AUTO`，写入类 intent 必须退出 read loop，进入既有 preview / confirm
生命周期。

## 2. 当前基线

当前代码层面已经完成的关键收口：

- provider 普通 `output_text` JSON plan 不再作为生产成功路径解析或执行。
- provider structured tool calls 已进入 event-native `assistant.tool_loop`。
- `assistant.tool_plan` / legacy JSON plan bridge 不再是默认执行合同。
- `parse_tool_plan_payload(...)`、`tool_plan_json_schema()`、
  `validate_tool_plan(...)` 和 `execute_tool_plan(...)` 不再出现在
  `src/application/assistant` 主包。
- `PlannerPlan` / `PlannerPlanStep` 已从 `src/application/assistant` 生产代码中移除。
- `synthesize_response_fn`、`AgentLoopSynthesizeFn`、
  `LlmSynthesisResult` 已从 assistant runtime/tests 的当前路径移除。
- tests 中新增和迁移的主流程用 event transcript、model events、
  tool result、evidence、guard 和 final answer 表达，不再把旧 plan dataclass
  当作默认抽象。

本轮已完成收尾验证和文档/诊断心智模型清理。后续改动需要继续确保所有公开说明、
诊断字段、eval 输出和回归门禁都指向 event-native loop，而不是旧 JSON plan。

## 3. 最终目标

完成后，`./om assistant` 的生产主路径必须满足：

- 模型通过 provider structured tool call 表达工具意图，不写普通文本 JSON plan。
- host 不从 Markdown、自然语言或 `output_text` 中提取 JSON plan。
- 普通自然语言默认进入模型驱动 capability selection；host 不再提前把 broker
  notice 收窄成唯一 preview tool。
- provider-visible manifest 同时包含按消息裁剪后的 read tools 和 preview
  capabilities，由模型选择工具。
- 自动 tool loop 只执行 `READ_AUTO` 工具。
- schema、scope、safety、duplicate、read-tool error 等可恢复问题进入 bounded
  observation，最多给模型一次修正机会。
- 写入、通知、ledger、trade event、position、配置和 broker-facing state 只能走
  preview、clarification、confirm/cancel 或 unsupported，不在 read loop 内 mutation。
- final answer 的事实来自 event transcript evidence 或明确的 missing-data 表述。
- trace 能解释 capability selection、tool guard、tool result、evidence、
  loop stop reason 和 answer route。

用户可见验收：

- `6月收益分析` 不再暴露 `LLM planner returned invalid JSON.`。
- broker notice 这类自然语言写意图不再暴露 raw
  `planned tool call failed pre-tool safety checks`。
- 模型误选工具时，用户看到的是可理解的 preview、clarification、unsupported
  或修正后的回答，而不是内部 planner/guard 错误。

## 4. 非目标和红线

本阶段不做：

- 不改造 `./om-agent`。
- 不新增第二套工具注册表、权限系统或全局对话状态机。
- 不恢复普通文本 JSON plan fallback。
- 不新增 `legacy_json_plan_enabled`、`use_event_loop_v2` 等长期兼容开关。
- 不把 deterministic natural-language parser 作为 broker notice 的主路由；
  但 explicit preview / command 已经有高置信 deterministic candidate 时，
  可以作为 provider protocol malformed 的安全兜底。
- 不用 preview-only manifest 作为自然语言业务路由；preview-only 只能作为
  历史风险收敛阶段的实现细节，不是当前模型智能化方向。
- 不让模型自动确认、取消、apply、发通知、写 ledger、写 trade event、写 position、
  改配置或触碰 broker-facing state。

显式 slash command 例外：

- `/confirm`、`/cancel`、`/apply`、`/assigned-stock` 等显式命令可以继续走
  deterministic command router。
- 普通自然语言消息不应被 slash command parser 抢走。

## 5. 实施项和验收点

### 5.1 文档清理

目标：公开文档不再把旧 JSON plan / `PlannerPlan` 讲成当前主路径。

需要检查并更新：

- [OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md)
- [OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md](OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md)
- [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md)
- [INDEX.md](INDEX.md)

处理原则：

- 可以保留历史背景，但必须明确这是 deprecated / removed / historical。
- 当前生产路径统一称为 event-native tool loop。
- 当前执行证据统一从 model event、tool guard、tool result、evidence、
  stop reason、answer route 描述。
- 不新增以 `tool_plan_json`、`PlannerPlan`、`execute_tool_plan` 为中心的说明。

### 5.2 诊断和 eval 表达收口

目标：诊断输出帮助使用者理解当前 event loop，而不是旧 planner internals。

需要确认：

- diagnostics live probe 展示 event plan / event transcript / selected capability。
- eval 输出能看到 tool event、tool result、evidence 和 final answer 路径。
- trace 中保留 `answer_route`、`scope_source`、`loop_stop_reason`、
  `tool_call_count`、`repair_attempted`、`capability_selection` 等解释字段。

不应新增：

- `tool_plan_json` 字段。
- 以 `PlannerPlan` 为主角的新诊断输出。
- 把 provider 普通文本 JSON plan 当作成功样例的 eval fixture。

### 5.3 回归门禁

目标：证明删除 JSON plan 心智模型后，assistant 主流程没有回退、误澄清或越权。

必须跑：

```bash
rg "PlannerPlan" src/application/assistant
rg "tool_plan_json_schema|parse_tool_plan_payload" src/application/assistant
rg "execute_tool_plan\\(" src/application/assistant
rg "synthesize_response_fn|LlmSynthesisResult|AgentLoopSynthesizeFn" src/application/assistant tests
python3 -m pytest tests/test_assistant_runtime.py tests/test_assistant_event_executor.py tests/test_assistant_model_events.py tests/test_assistant_model_continuation.py tests/test_assistant_model_evidence.py -q
python3 -m pytest tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_diagnostics.py -q
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q
python3 -m pytest tests/test_inbound_control.py tests/test_inbound_feishu_ws.py -q
./om assistant eval-context --mode scenarios
git diff --check
```

`rg` 命令的验收标准：

- 前四条 `rg` 在 `src/application/assistant` / tests 当前路径中无生产命中。
- docs 中可以出现历史名词，但必须标注为历史、已废弃或已移除。

### 5.4 用户场景回归

至少覆盖这些场景：

- read-only analysis：`6月收益分析` 能走模型 tool loop，不出现 invalid JSON planner。
- assignment broker notice：自然语言被指派通知不能暴露 raw pre-tool safety error；
  应进入 preview、clarification 或 unsupported。
- expiry broker notice：到期失效通知同上，不应自动写 position / trade event。
- follow-up：短 follow-up 保持上轮 scope，不因同义词跳到错误分析域。
- scope expansion：扩 scope 要有明确证据或澄清，不隐式扩大到无关账户/标的。
- lowercase symbol / 中文 alias：符号和账户归一化不能破坏 capability selection。

## 6. 开工顺序

按以下顺序执行：

1. 先跑第 5.3 的 `rg` 审计，确认代码层面没有 legacy bridge 回潮。
2. 更新第 5.1 涉及的 docs，把旧 planner 表述改成历史背景或删除。
3. 检查 diagnostics / eval 表达，必要时做最小代码或测试调整。
4. 跑第 5.3 的完整回归门禁。
5. 用第 5.4 的用户场景做最终人工或 fixture 验证。
6. 更新本文档的执行记录，写明通过的命令和剩余风险。

如果中途发现旧 bridge 仍承担非显而易见的生产职责，先停下来记录调用链和风险，
不要用兼容开关绕过。

## 7. 完成清单

完成时必须同时满足：

- `./om assistant` 当前生产路径是 event-native tool loop。
- `./om-agent` 仍只是 Tool Gateway，没有被改造成项目 Agent。
- 代码和 tests 不再依赖 `PlannerPlan` / `PlannerPlanStep` 作为 assistant 主流程抽象。
- provider 普通文本 JSON plan 不解析、不执行、不 fallback。
- provider structured `ModelToolCallEvent` 直接进入 event-native loop。
- recoverable error 以 bounded observation 给模型一次修正机会。
- write / preview / admin intent 不在 read loop 内自动 mutation。
- diagnostics、eval 和 docs 不再推荐 `assistant.tool_plan` 或 JSON plan。
- 第 5.3 的回归门禁通过。

## 8. 允许保留的历史表述

允许：

- 历史设计文档中解释为什么废弃 JSON plan。
- 架构文档中说明 `assistant.tool_plan` 是旧路径、已废弃或已拒绝。
- 测试中保留“普通文本 JSON plan 应被忽略/拒绝”的负向用例。

不允许：

- 恢复 `parse_tool_plan_payload(...)`、`tool_plan_json_schema()`、
  `validate_tool_plan(...)` 或 `execute_tool_plan(...)` 到 assistant 主包。
- 把 provider structured runtime 主路径转回 `PlannerPlan`。
- 为旧 JSON plan 加长期兼容开关。
- 用自然语言 deterministic parser 绕过模型工具选择。

## 9. 执行记录

### 2026-06-20

本轮按第 5.3 门禁执行，结果：

- `rg "PlannerPlan" src/application/assistant`: 无命中。
- `rg "tool_plan_json_schema|parse_tool_plan_payload" src/application/assistant`: 无命中。
- `rg "execute_tool_plan\\(" src/application/assistant`: 无命中。
- `rg "synthesize_response_fn|LlmSynthesisResult|AgentLoopSynthesizeFn" src/application/assistant tests`: 无命中。
- `python3 -m pytest tests/test_assistant_runtime.py tests/test_assistant_event_executor.py tests/test_assistant_model_events.py tests/test_assistant_model_continuation.py tests/test_assistant_model_evidence.py -q`: `205 passed`。
- `python3 -m pytest tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py tests/test_assistant_diagnostics.py -q`: `130 passed`。
- `python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q`: `97 passed`。
- `python3 -m pytest tests/test_inbound_control.py tests/test_inbound_feishu_ws.py -q`: `91 passed`。
- `./om assistant eval-context --mode scenarios`: `10/10 passed`。
- 用户场景子集：
  `python3 -m pytest tests/test_assistant_runtime.py::test_assistant_runtime_previews_futu_assignment_notice tests/test_assistant_runtime.py::test_assistant_runtime_previews_futu_expiry_notice tests/test_assistant_runtime.py::test_assistant_runtime_provider_preview_request_creates_assignment_preview tests/test_assistant_runtime.py::test_assistant_runtime_provider_preview_request_creates_expiry_preview tests/test_assistant_runtime.py::test_assistant_runtime_provider_preview_request_missing_account_returns_clarification tests/test_assistant_runtime.py::test_assistant_runtime_agent_loop_repairs_assignment_notice_wrong_read_tool tests/test_assistant_runtime.py::test_assistant_runtime_agent_loop_repairs_expiry_notice_wrong_read_tool tests/test_assistant_runtime.py::test_plan_read_only_tools_uses_provider_tool_call_not_output_text_json_plan tests/test_assistant_runtime.py::test_plan_read_only_tools_rejects_plain_text_json_plan_as_invalid_model_event tests/test_assistant_runtime.py::test_plan_read_only_tools_candidate_alias_and_lowercase_symbol_use_tool_calls tests/test_assistant_runtime.py::test_assistant_runtime_tool_loop_continuation_preview_request_creates_assignment_preview -q`:
  `11 passed`。
- `git diff --check`: passed。

本轮还完成：

- 当前文档中的 tool loop 状态收口为 event-native 主路径。
- diagnostics live probe 不再把 legacy planner plan 当成功结果接受。
- 新增 diagnostics 负向回归，确认 legacy planner plan 会报错并提示需要
  event-native tool loop。

### 2026-06-20 远端 ClawBot 发现

远端 `v1.2.320` 的 ClawBot 输入：

```text
记录sy 账户的到期被指派平仓 期权被指派通知: 您的保证金综合账户(2905) - 证券所持有的-2张PDD 260618 85.00P期权已被指派，详情请查看资金明细及持仓情况。【富途证券(香港)】
```

暴露了事件式 tool loop 的下一层问题：

- 外层 JSON plan 已废弃，错误不是 `LLM planner returned invalid JSON`。
- DeepSeek / Chat Completions 兼容 provider 的 tool-call `arguments`
  仍是 JSON 字符串，模型输出 malformed arguments 时会触发
  `provider tool call arguments are not valid JSON`。
- 同一条消息 deterministic candidate 已能识别为 `manual_assignment`，
  但候选仲裁选择了 LLM error，没有使用安全 preview fallback。
- `requested_effect` 对 broker lifecycle notice 的 host 推断不完整，可能把
  explicit preview 误判为 read，导致 `effect_mismatch`。
- planner payload 过大，且要求模型复制完整 `raw_text`，增加 malformed
  arguments 风险。

后续方案已并入
[OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md](OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md)
的 Slice 9：Provider Argument / Preview Hardening。

## 11. V3 Model-Driven Capability Selection

Slice 9 之后继续升级的核心变化：

- `./om assistant` 的普通自然语言入口不再由 host-side
  `preview_request_kind_from_text(...)` 决定具体业务 tool。
- planner payload 中同时提供 read tools 和 preview capabilities。
- host 只提供 `preview_authority`：这条消息是否具备创建 pending preview 的
  用户授权；它不指定 `manual_assignment`、`manual_expiry` 等具体工具。
- `preview_authority` 先尊重解释、分析、收益、状态等 read intent；只有
  explicit record/write/补录 或完整 broker lifecycle/fill notice 才允许 pending
  preview。
- 模型负责选择 read tool、preview capability、clarification 或 final-answer
  路径。
- preview capability 是 terminal action：host 创建 pending preview 后停止
  loop，后续 `/confirm` / `/cancel` 仍由 deterministic command 处理。
- deterministic natural-language parser 降级为 preview normalizer 和 provider
  protocol malformed fallback，不再是 broker notice 的主路由。

验收差异：

- broker notice 的 provider tool list 不再只有一个 preview tool；模型能看到
  read + preview 能力面。
- read 查询即使能看到 preview capabilities，也不能在没有
  `preview_authority` 时创建 pending preview。
- `期权被指派通知是什么意思`、`成交提醒收益分析` 等通知解释/分析问题必须保持
  read-only。
- planner manifest 保留 read + preview 能力面，但 analysis view 详情采用紧凑
  字段清单；需要完整字段语义时由模型调用 `analysis_catalog` 补证据。
- malformed provider arguments 仍不能恢复普通文本 JSON plan，只能走 bounded
  repair、clarification 或安全 deterministic preview fallback。
