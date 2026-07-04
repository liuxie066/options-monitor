# Assistant Tool Call Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve `./om assistant` model tool-calling quality so the model chooses the right tool, repairs recoverable mistakes once, gathers enough evidence, and fails with a useful trace instead of internal errors.

**Architecture:** Keep the existing event-native `AgentLoop`: provider structured tool calls enter `run_assistant_tool_event_loop`, host guards validate schema/scope/risk/budget/duplicates, `READ_AUTO` tools execute through `tool_execution` and `agent_tool_registry`, and preview/write requests exit through the deterministic preview lifecycle. The model owns intent understanding, tool selection, continuation, and synthesis; code owns guardrails, observation shape, evidence contracts, and traceability.

**Tech Stack:** Python 3, pytest, existing OM assistant modules, `./om assistant eval-context`, `./om-agent` diagnostics.

---

## Baseline

Current source already has the right runtime shape:

- `docs/OM_ASSISTANT_ARCHITECTURE.md` defines the authority path as `./om assistant -> AgentLoop -> tool_execution -> agent_tool_registry`.
- `src/application/assistant/agent_loop.py` already uses structured `ModelToolCallEvent`, `ToolGuardDecisionEvent`, `ToolResultEvent`, `ModelFinalAnswerEvent`, continuation, duplicate detection, budget exhaustion handling, and `repair_attempted` trace.
- Provider-visible manifest narrowing for preview requests already exists through `_planner_preview_read_tool_scope(...)`.
- Existing tests cover pre-tool denial repair, unsupported argument repair, duplicate-call final-answer retry, empty continuation retry, and budget final-answer retry.

Do not reintroduce `PlannerPlan`, text JSON plan parsing, a second tool registry, or a deterministic natural-language router for normal assistant messages.

## Success Criteria

- Natural-language read questions execute 1 to 3 safe read tools and answer from evidence.
- Explicit lifecycle/fill/config/admin requests choose a preview capability only when `preview_authority` allows it.
- Explanation,收益,持仓,状态类 questions stay read-only even when they contain words like `成交提醒`, `期权被指派通知`, or `立即更新了吗`.
- Recoverable model mistakes produce one model-visible observation and one correction opportunity; repeated same error stops.
- Low-risk empty read results become missing-data answers, not clarification loops.
- High-risk preview/write requests missing account or operation scope return the existing `clarification_request` schema.
- Trace can explain selected capability, guard decision, evidence gaps, allowed next actions, loop stop reason, answer route, and final-answer retry reason.
- Live diagnostics can run a small set of read-only provider probes and report per-probe selected tool, terminal event, and status without exposing raw provider payloads or secrets.

## File Ownership

- `src/application/assistant/agent_loop.py`: event loop, provider-visible manifest assembly, preview notes, recoverable-error loop behavior, trace fields.
- `src/application/agent_tools/positions.py`: `monthly_income_report` and `option_positions_read` planner notes and semantics.
- `src/application/agent_tools/analysis.py`: `analysis_catalog` and `analysis_query` planner notes, semantics, recipes, and output evidence.
- `src/application/assistant/coverage_verifier.py`: evidence-gap classification and suggested recovery metadata.
- `src/application/assistant/model_evidence.py`: model-visible observations and evidence event payloads.
- `src/application/assistant/diagnostics.py`: live probe and diagnostics payload.
- `src/application/assistant/context_eval.py`: scenario/eval reporting fields.
- `tests/fixtures/assistant_context_scenarios.jsonl`: regression scenarios for routing and terminal decisions.
- `tests/test_assistant_runtime.py`: manifest, preview authority, runtime repair, clarification, and trace assertions.
- `tests/test_assistant_event_executor.py`: pure event-loop behavior tests.
- `tests/test_assistant_model_evidence.py`: evidence and observation shape tests.
- `tests/test_assistant_diagnostics.py`: diagnostics contract tests.
- `tests/test_assistant_context_eval.py`: scenario fixture count and formatted eval output.

## Task 1: Establish The Intelligence Regression Matrix

**Files:**
- Modify: `tests/fixtures/assistant_context_scenarios.jsonl`
- Modify: `tests/test_assistant_context_eval.py`
- Modify: `src/application/assistant/context_eval.py` only if the existing formatted output cannot show the new decision fields

- [x] **Step 1: Add scenarios before runtime changes**

Add or update scenario fixture rows covering these exact user intents:

```text
scenario_income_source_prefers_monthly_income_report
question: sy 6月收益来源拆一下
expected terminal: read_tool_call
expected first_tool: monthly_income_report
forbidden_tools: runtime_status, option_positions_read, manual_trade_open

scenario_assignment_notice_explanation_stays_read
question: 解释一下这条期权被指派通知是什么意思
expected terminal: read_tool_call
expected first_tool_family: analysis
forbidden_tools: manual_assignment, manual_expiry

scenario_assigned_stock_pnl_prefers_positions
question: sy PDD 被指派正股现在浮盈亏怎么样
expected terminal: read_tool_call
expected first_tool: option_positions_read
expected arguments include: action=assigned-stock, refresh_quotes=true
forbidden_tools: manual_assignment, monthly_income_report

scenario_candidate_missing_prefers_filter_explain
question: 为什么 PDD 没进候选
expected terminal: read_tool_call
expected first_tool: candidate_filter_explain
forbidden_tools: analysis_query, monthly_income_report

scenario_low_risk_missing_data_no_clarification
question: 查一下一个不存在月份的收益
expected terminal: read_tool_call or final_answer
forbidden terminal: clarification_request
```

Use the existing JSONL schema already present in the file: `id`, `mode`, `family`, `question`, `context_projection`, `plan_payload`, and `expect`.

- [x] **Step 2: Update the fixture count test**

In `tests/test_assistant_context_eval.py`, update the expected scenario count by the exact number of added rows. Add assertions that formatted scenario output includes:

```python
assert "scenario_income_source_prefers_monthly_income_report" in text
assert "scenario_assignment_notice_explanation_stays_read" in text
assert "terminal=read_tool_call" in text
assert "forbidden" in text
```

- [x] **Step 3: Verify RED or PASS-with-coverage**

Run:

```bash
./om assistant eval-context --mode scenarios
python3 -m pytest tests/test_assistant_context_eval.py -q
```

Expected before behavior changes: either a focused failure naming the wrong tool/terminal, or pass if the current implementation already satisfies the new matrix. If it passes, keep the fixture because it locks the behavior.

## Task 2: Improve Capability Selection Hints Without Host Routing

**Files:**
- Modify: `src/application/agent_tools/positions.py`
- Modify: `src/application/agent_tools/analysis.py`
- Modify: `src/application/assistant/agent_loop.py`
- Test: `tests/test_assistant_runtime.py`

- [x] **Step 1: Write failing manifest tests**

Add focused assertions near existing manifest tests in `tests/test_assistant_runtime.py`:

```python
def test_agent_loop_manifest_selection_notes_distinguish_income_analysis_and_positions() -> None:
    payload = json.loads(_planner_input_text("sy 6月收益来源拆一下", conversation_context=None))
    tools = {tool["name"]: tool for tool in payload["tools"]}

    monthly_notes = " ".join(tools["monthly_income_report"]["planner_notes"])
    analysis_notes = " ".join(tools["analysis_query"]["planner_notes"])
    position_notes = " ".join(tools["option_positions_read"]["planner_notes"]) if "option_positions_read" in tools else ""

    assert "monthly income source" in monthly_notes
    assert "not for current assigned-stock holding PnL" in monthly_notes
    assert "cross-domain analytical" in analysis_notes
    if position_notes:
        assert "not for monthly income source breakdown" in position_notes
```

```python
def test_agent_loop_preview_notes_do_not_use_notice_explanation_for_preview() -> None:
    assignment_manifest = _planner_tool_manifest(
        include_read_tools=False,
        include_preview_capabilities=True,
        allowed_preview_intents=["manual_assignment"],
    )
    notes = " ".join(next(tool for tool in assignment_manifest if tool["name"] == "manual_assignment")["planner_notes"])

    assert "not for explaining assignment notices" in notes
    assert "not for assigned-stock PnL questions" in notes
    assert "pending preview" in notes
```

- [x] **Step 2: Run tests to verify the current wording gap**

Run:

```bash
python3 -m pytest tests/test_assistant_runtime.py::test_agent_loop_manifest_selection_notes_distinguish_income_analysis_and_positions tests/test_assistant_runtime.py::test_agent_loop_preview_notes_do_not_use_notice_explanation_for_preview -q
```

Expected: FAIL on missing selection notes.

- [x] **Step 3: Add only selection-boundary wording**

Make minimal metadata edits:

- In `_MONTHLY_INCOME_PLANNER_NOTES`, add one note: `Use for monthly income source/breakdown/composition questions; not for current assigned-stock holding PnL, which belongs to option_positions_read action=assigned-stock.`
- In `_OPTION_POSITIONS_PLANNER_NOTES`, add one note: `Use assigned-stock action for current assigned-stock holding PnL; not for monthly income source breakdown, realized income composition, or account performance summaries.`
- In `_ANALYSIS_QUERY_PLANNER_NOTES`, add one note: `Use for cross-domain analytical comparisons or grouped queries when a narrow renderer cannot answer; use monthly_income_report first for ordinary monthly income source/breakdown questions.`
- In `_planner_preview_notes("manual_assignment")`, add one note: `Not for explaining assignment notices, assigned-stock PnL questions, or status questions; those are read-only analysis/position requests.`
- In `_planner_preview_notes("manual_expiry")`, add one note: `Not for explaining expiry notices or status questions; those are read-only analysis/position requests.`

- [x] **Step 4: Verify GREEN**

Run:

```bash
python3 -m pytest tests/test_assistant_runtime.py::test_agent_loop_manifest_selection_notes_distinguish_income_analysis_and_positions tests/test_assistant_runtime.py::test_agent_loop_preview_notes_do_not_use_notice_explanation_for_preview -q
```

Expected: PASS.

## Task 3: Normalize Evidence Gap Guidance For Continuation

**Files:**
- Modify: `src/application/assistant/coverage_verifier.py`
- Modify: `src/application/assistant/model_evidence.py`
- Test: `tests/test_assistant_model_evidence.py`
- Test: `tests/test_assistant_event_executor.py`

- [x] **Step 1: Add a test for canonical gap payload**

Add this test to `tests/test_assistant_model_evidence.py`:

```python
def test_model_evidence_observation_exposes_recoverable_gap_guidance() -> None:
    result = build_response(
        tool_name="monthly_income_report",
        ok=True,
        data={
            "row_count": 1,
            "rows": [{"account": "lx", "month": "2026-06", "net_income_cny": 100}],
            "coverage": {
                "status": "recoverable_gap",
                "gaps": [
                    {
                        "kind": "analysis_breakdown_needed",
                        "recoverable_by": "analysis_query",
                        "suggested_tool": "analysis_query",
                        "suggested_views": ["account_monthly_income_components"],
                    }
                ],
            },
        },
    )
    adapter = adapt_tool_result(
        event_id="result_call_income",
        parent_event_id="guard_call_income",
        tool_call_id="call_income",
        tool_name="monthly_income_report",
        normalized_payload={"month": "2026-06", "include_rows": True},
        guard_decision=_allow_guard("call_income", "monthly_income_report"),
        output_contract=resolve_output_contract("monthly_income_report", {"include_rows": True}),
        raw_result=result,
    )

    observation = event_observation_from_tool_result(adapter, index=1)

    assert observation["evidence_gaps"][0]["gap_type"] == "analysis_breakdown_needed"
    assert observation["evidence_gaps"][0]["recoverable"] is True
    assert observation["evidence_gaps"][0]["suggested_tool"] == "analysis_query"
    assert observation["evidence_gaps"][0]["allowed_next_actions"] == ["call_suggested_read_tool", "answer_with_missing_data"]
```

Use existing local helper patterns in the file for `_allow_guard(...)`; if no helper exists, create a minimal test-local `ToolGuardDecisionEvent`.

- [x] **Step 2: Run RED**

Run:

```bash
python3 -m pytest tests/test_assistant_model_evidence.py::test_model_evidence_observation_exposes_recoverable_gap_guidance -q
```

Expected: FAIL because `event_observation_from_tool_result(...)` does not yet expose normalized `evidence_gaps`.

- [x] **Step 3: Implement the minimal normalizer**

In `src/application/assistant/model_evidence.py`, add a private helper:

```python
def _normalized_evidence_gaps(data: dict[str, Any]) -> list[dict[str, Any]]:
    coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
    raw_gaps = coverage.get("gaps") if isinstance(coverage.get("gaps"), list) else []
    normalized: list[dict[str, Any]] = []
    for item in raw_gaps:
        if not isinstance(item, dict):
            continue
        suggested_tool = str(item.get("suggested_tool") or item.get("recoverable_by") or "").strip()
        recoverable = bool(suggested_tool)
        normalized.append(
            {
                "gap_type": str(item.get("kind") or item.get("gap_type") or "evidence_gap"),
                "required_fact": str(item.get("required_fact") or item.get("reason") or ""),
                "current_evidence_refs": [str(ref) for ref in item.get("current_evidence_refs") or [] if str(ref).strip()],
                "recoverable": recoverable,
                "suggested_tool": suggested_tool or None,
                "allowed_next_actions": ["call_suggested_read_tool", "answer_with_missing_data"] if recoverable else ["answer_with_missing_data"],
            }
        )
    return normalized
```

Then in `event_observation_from_tool_result(...)`, after `data` is set:

```python
    gaps = _normalized_evidence_gaps(data) if isinstance(data, dict) else []
    if gaps:
        observation["evidence_gaps"] = gaps
```

- [x] **Step 4: Verify GREEN and multi-hop behavior**

Run:

```bash
python3 -m pytest tests/test_assistant_model_evidence.py::test_model_evidence_observation_exposes_recoverable_gap_guidance tests/test_assistant_event_executor.py::test_run_assistant_tool_event_loop_multihop_income_report_to_analysis_query -q
```

Expected: PASS.

## Task 4: Close One Recoverable Error Gap With Observation Repair

**Files:**
- Modify: `src/application/assistant/agent_loop.py` only if the RED test proves a gap
- Test: `tests/test_assistant_event_executor.py`

- [x] **Step 1: Add an event-loop repair matrix test**

Add this test to `tests/test_assistant_event_executor.py`:

```python
def test_run_assistant_tool_event_loop_repairs_unknown_tool_once() -> None:
    continuation_payloads: list[dict[str, Any]] = []
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={"summary": {"ok": True}})

    def _continue(payload: dict[str, Any]) -> dict[str, Any]:
        continuation_payloads.append(payload)
        output = json.loads(payload["input"][-1]["output"])
        if len(continuation_payloads) == 1:
            assert output["is_error"] is True
            assert output["content"]["error"]["code"] == "UNKNOWN_TOOL"
            assert output["content"]["guard_decision"]["decision"] == "unknown_tool"
            return {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_runtime_status_repaired",
                        "name": "runtime_status",
                        "arguments": "{}",
                    }
                ]
            }
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "运行状态已根据工具结果完成。"}],
                }
            ]
        }

    bad_event = ModelToolCallEvent(
        event_id="model_tool_call_bad",
        tool_call_id="call_bad_tool",
        tool_name="made_up_tool",
        arguments={},
        provider="openai",
    )

    outcome = run_assistant_tool_event_loop(
        question="看一下状态",
        request=AssistantRequest(text="看一下状态", sender_id="u1", config_key="us"),
        task_contract={"requested_effect": "read", "domain": "runtime", "scope": {}},
        initial_events=(bad_event,),
        execute_tool_fn=_execute,
        provider="openai",
        create_continuation_response_fn=_continue,
    )

    assert calls == [("runtime_status", {"config_key": "us"})]
    assert outcome.status == "done"
    assert outcome.stop_reason == "model_final_answer"
    assert outcome.trace["repair_attempted"] is True
    assert outcome.trace["capability_selection"]["selected_count"] == 2
```

- [x] **Step 2: Run the focused test**

Run:

```bash
python3 -m pytest tests/test_assistant_event_executor.py::test_run_assistant_tool_event_loop_repairs_unknown_tool_once -q
```

Expected: If PASS, do not edit runtime code. If FAIL, inspect the failing stop reason and make the smallest change in `_assistant_tool_loop_error_recoverable(...)` or the continuation branch so `UNKNOWN_TOOL` is treated like other recoverable guard observations exactly once.

- [x] **Step 3: Add repeated-error guard if needed**

If the test reveals repeated unknown-tool loops can continue indefinitely, add a second test that returns `made_up_tool` twice and assert:

```python
assert outcome.status == "stopped"
assert outcome.stop_reason == "repeated_recoverable_error"
assert outcome.trace["guard_denial_recoverable"] is True
```

Only change `recoverable_error_counts` handling if this assertion fails.

## Task 5: Clarification False-Positive Guard

**Files:**
- Modify: `src/application/assistant/agent_loop.py`
- Modify: `src/application/assistant/action_safety.py` only if current high-risk preview missing account behavior regresses
- Test: `tests/test_assistant_runtime.py`
- Test: `tests/test_assistant_event_executor.py`

- [x] **Step 1: Preserve low-risk read behavior**

Run the existing low-risk test:

```bash
python3 -m pytest tests/test_assistant_runtime.py::test_agent_loop_event_loop_low_risk_empty_read_stays_rendered_without_global_clarification -q
```

Expected: PASS. If it fails, fix the low-risk read route before changing preview behavior.

- [x] **Step 2: Preserve high-risk preview clarification**

Run:

```bash
python3 -m pytest tests/test_assistant_runtime.py::test_assistant_runtime_provider_preview_request_missing_account_returns_clarification -q
```

Expected: PASS. The returned error details must include `clarification_request` with `questions[0].slot == "account"`.

- [x] **Step 3: Add trace assertions only after a gap is found**

If either focused test lacks trace fields for diagnosis, add assertions for:

```python
assert trace["stop_category"] in {"clarification_request", "model_final_answer"}
assert trace.get("clarification_reason") in {None, "missing_account_scope", "missing_operation_scope"}
assert trace.get("risk_class") in {None, "READ_AUTO", "SOFT_WRITE_PREVIEW"}
```

Then add the minimal trace fields at the outcome site that already knows the reason. Do not add a new clarification subsystem.

## Task 6: Diagnostics Contract

**Files:**
- Modify: `src/application/assistant/diagnostics.py`
- Modify: `src/application/agent_tools/diagnostics.py`
- Test: `tests/test_assistant_diagnostics.py`
- Test: `tests/test_agent_plugin_contract.py`

- [x] **Step 1: Write diagnostics expectations**

Add a test that a live probe result or stored assistant trace compact payload can expose these keys without raw provider payload:

```python
expected_keys = {
    "selected_capability",
    "model_turns",
    "tool_observations",
    "evidence_gaps",
    "stop_reason",
    "answer_route",
}
for key in expected_keys:
    assert key in compact_trace
assert "raw_provider_payload" not in compact_trace
assert "api_key" not in json.dumps(compact_trace).lower()
```

- [x] **Step 2: Implement compact mapping**

Map existing `event_loop.trace`, `event_transcript`, `observations`, and `evidence_bundle` into diagnostics fields. Do not persist a second trace format; diagnostics should be a compact view over existing session trace.

- [x] **Step 3: Verify diagnostics and plugin contract**

Run:

```bash
python3 -m pytest tests/test_assistant_diagnostics.py tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q
```

Expected: PASS.

## Task 7: Final Validation

**Files:**
- No additional edits unless validation exposes a regression.

- [x] **Step 1: Run focused assistant suite**

Run:

```bash
python3 -m pytest tests/test_assistant_runtime.py tests/test_assistant_event_executor.py tests/test_assistant_model_continuation.py tests/test_assistant_model_evidence.py tests/test_assistant_context_eval.py -q
```

Expected: all tests pass.

- [x] **Step 2: Run scenario eval**

Run:

```bash
./om assistant eval-context --mode scenarios
```

Expected: all scenarios pass, and formatted output includes first tool, terminal, requested effect, and clarification status.

- [x] **Step 3: Run agent contract smoke**

Run:

```bash
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q
```

Expected: all tests pass.

- [x] **Step 4: Run diff hygiene**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors. Review `git status --short` and keep unrelated dirty files out of any future commit unless the user explicitly requests a commit.

## Task 8: Add Multi-Probe Live Planner Diagnostics

**Files:**
- Modify: `src/application/assistant/diagnostics.py`
- Modify: `src/interfaces/cli/assistant_ops.py`
- Test: `tests/test_assistant_diagnostics.py`
- Test: `tests/test_cli_operator_commands.py`

- [x] **Step 1: Add the application-layer RED test**

Add this test near the existing live-probe tests in `tests/test_assistant_diagnostics.py`:

```python
def test_llm_check_live_probe_supports_multiple_read_only_probe_texts(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    responses = [
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_status_1",
                    "name": "runtime_status",
                    "arguments": "{}",
                }
            ]
        },
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_income_1",
                    "name": "monthly_income_report",
                    "arguments": '{"account":"sy","month":"2026-06","include_rows":true}',
                }
            ]
        },
    ]

    def _create_tool_call_response(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return responses[len(calls) - 1]

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_texts=["状态", "sy 6月收益来源拆一下"],
        create_tool_call_response_fn=_create_tool_call_response,
    )

    checks = {item["name"]: item for item in out["checks"]}
    live_probe = checks["live_probe"]
    assert out["summary"]["ok"] is True
    assert len(calls) == 2
    assert live_probe["status"] == "ok"
    assert live_probe["value"]["probe_count"] == 2
    assert [probe["text"] for probe in live_probe["value"]["probes"]] == ["状态", "sy 6月收益来源拆一下"]
    assert [probe["selected_tool"] for probe in live_probe["value"]["probes"]] == [
        "runtime_status",
        "monthly_income_report",
    ]
    assert [probe["event_type"] for probe in live_probe["value"]["probes"]] == [
        "model_tool_call",
        "model_tool_call",
    ]
    serialized = json.dumps(live_probe["value"], ensure_ascii=False).lower()
    assert "api_key" not in serialized
    assert "sk-test" not in serialized
    assert "raw_provider_payload" not in serialized
```

- [x] **Step 2: Run RED**

Run:

```bash
python3 -m pytest tests/test_assistant_diagnostics.py::test_llm_check_live_probe_supports_multiple_read_only_probe_texts -q
```

Expected: FAIL with `TypeError: check_llm_planner() got an unexpected keyword argument 'live_texts'`.

- [x] **Step 3: Implement the smallest compatible application change**

In `src/application/assistant/diagnostics.py`, update the public function signature and `_live_probe_check(...)` call:

```python
def check_llm_planner(
    *,
    repo_root: str | Path,
    config_path: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
    live: bool = False,
    live_text: str = DEFAULT_LIVE_PROBE_TEXT,
    live_texts: list[str] | tuple[str, ...] | None = None,
    create_tool_call_response_fn: CreateToolCallResponseFn | None = None,
) -> dict[str, Any]:
```

Pass both values:

```python
    live_probe = _live_probe_check(
        runtime_settings=runtime_settings,
        effective_env=effective_env.values,
        live=bool(live),
        live_text=live_text,
        live_texts=live_texts,
        create_tool_call_response_fn=create_tool_call_response_fn,
    )
```

Add two private helpers below `_base_url_message(...)`:

```python
def _normalized_live_probe_texts(
    *,
    live_text: str,
    live_texts: list[str] | tuple[str, ...] | None,
) -> list[str]:
    if live_texts is None:
        return [str(live_text or DEFAULT_LIVE_PROBE_TEXT)]
    texts = [str(item).strip() for item in live_texts if str(item).strip()]
    return texts or [str(live_text or DEFAULT_LIVE_PROBE_TEXT)]


def _live_probe_summary(*, text: str, result: Any) -> dict[str, Any]:
    if result.error is not None:
        return {
            "text": text,
            "status": "error",
            "message": result.error.message,
            "terminal": result.error.code,
            "selected_tool": None,
            "event_type": None,
            "trace": dict(result.trace),
            "plan": None,
            "event_plan": None,
        }
    event_plan = result.event_plan.public_payload() if result.event_plan is not None else None
    steps = event_plan.get("steps") if isinstance(event_plan, dict) else []
    events = event_plan.get("events") if isinstance(event_plan, dict) else []
    first_step = steps[0] if steps and isinstance(steps[0], dict) else {}
    first_event = events[0] if events and isinstance(events[0], dict) else {}
    accepted = event_plan is not None
    return {
        "text": text,
        "status": "ok" if accepted else "error",
        "message": "provider returned a valid event-native plan"
        if accepted
        else "provider did not return an event-native plan",
        "terminal": "event_native_plan" if accepted else "missing_event_native_plan",
        "selected_tool": first_step.get("tool_name"),
        "event_type": first_event.get("event_type"),
        "trace": dict(result.trace),
        "plan": None,
        "event_plan": event_plan,
    }
```

Replace `_live_probe_check(...)` with a loop that preserves the existing single-probe keys:

```python
def _live_probe_check(
    *,
    runtime_settings: AssistantSettings,
    effective_env: dict[str, str],
    live: bool,
    live_text: str,
    live_texts: list[str] | tuple[str, ...] | None,
    create_tool_call_response_fn: CreateToolCallResponseFn | None,
) -> dict[str, Any]:
    if not live:
        return {
            "name": "live_probe",
            "status": "skipped",
            "message": "provider call skipped; pass --live to run a read-only planner probe",
        }

    probes: list[dict[str, Any]] = []
    for text in _normalized_live_probe_texts(live_text=live_text, live_texts=live_texts):
        result = create_model_turn_events(
            text,
            runtime_settings,
            conversation_context=None,
            environ=effective_env,
            create_tool_call_response_fn=create_tool_call_response_fn,
        )
        probes.append(_live_probe_summary(text=text, result=result))

    first = probes[0] if probes else _live_probe_summary(
        text=str(DEFAULT_LIVE_PROBE_TEXT),
        result=create_model_turn_events(
            DEFAULT_LIVE_PROBE_TEXT,
            runtime_settings,
            conversation_context=None,
            environ=effective_env,
            create_tool_call_response_fn=create_tool_call_response_fn,
        ),
    )
    accepted = all(probe.get("status") == "ok" for probe in probes)
    value = {
        "trace": first.get("trace"),
        "plan": None,
        "event_plan": first.get("event_plan"),
        "probe_count": len(probes),
        "probes": probes,
    }
    return {
        "name": "live_probe",
        "status": "ok" if accepted else "error",
        "message": "provider returned valid event-native plans"
        if accepted
        else "provider did not return a valid event-native plan for every probe",
        "value": value,
    }
```

If this implementation triggers a lint or mypy-style failure because `Any` is too broad, import `ModelTurnResult` from `src.application.assistant.agent_loop` and type `_live_probe_summary(..., result: ModelTurnResult)`.

- [x] **Step 4: Verify GREEN and old compatibility**

Run:

```bash
python3 -m pytest tests/test_assistant_diagnostics.py::test_llm_check_live_probe_supports_multiple_read_only_probe_texts tests/test_assistant_diagnostics.py::test_llm_check_live_probe_uses_read_only_tool_call_planning tests/test_assistant_diagnostics.py::test_llm_check_live_probe_rejects_missing_event_native_plan -q
```

Expected: PASS. The existing single-probe tests must still read `value["trace"]`, `value["plan"]`, and `value["event_plan"]`.

- [x] **Step 5: Add CLI argument forwarding**

Change both parser definitions in `src/interfaces/cli/assistant_ops.py` from single `--text` to append mode:

```python
assistant_llm_check.add_argument("--text", action="append", default=None, help="probe text used with --live; repeat for multiple probes")
```

```python
assistant_model_check.add_argument("--text", action="append", default=None, help="probe text used with --live; repeat for multiple probes")
```

Before each `check_llm_planner_fn(...)` call, derive the compatibility values inline:

```python
probe_texts = [str(item) for item in (args.text or []) if str(item).strip()]
```

For `assistant llm-check`, pass:

```python
            live_text=probe_texts[0] if probe_texts else "状态",
            live_texts=probe_texts if len(probe_texts) > 1 else None,
```

For `assistant model check`, pass the same two keyword arguments. Do not change command names or output schema.

- [x] **Step 6: Update CLI tests**

Update the existing single `--text 状态` expectation in `tests/test_cli_operator_commands.py` so the call includes the new optional keyword:

```python
        "live_text": "状态",
        "live_texts": None,
```

Add a second assertion in the same test or a new focused test:

```python
    calls.clear()
    rc = cli.main([
        "assistant",
        "llm-check",
        "--live",
        "--text",
        "状态",
        "--text",
        "sy 6月收益来源拆一下",
    ])
    assert rc == 0
    assert calls[-1]["live_text"] == "状态"
    assert calls[-1]["live_texts"] == ["状态", "sy 6月收益来源拆一下"]
```

- [x] **Step 7: Verify CLI and hygiene**

Run:

```bash
python3 -m pytest tests/test_assistant_diagnostics.py tests/test_cli_operator_commands.py -q
git diff --check
```

Expected: PASS and no whitespace errors.

**Follow-up evidence note:** A real `--live` multi-probe run sends assistant
instructions, tool manifests, and probe text to the configured external LLM
provider. Run it only after explicit operator approval. Local tests prove the
multi-probe diagnostic surface; they do not prove live provider selection
parity.

**Follow-up diagnostic note:** `assistant llm-check --live` and
`assistant model check --live` accept repeated `--expect-tool` values aligned
with repeated `--text` probes. The live probe result reports
`expected_tool` and `tool_match` per probe, and marks the check as error when
the selected tool differs from the expected tool.

They also accept repeated `--expect-event-type` values aligned with repeated
`--text` probes. The live probe result reports `expected_event_type` and
`event_type_match` per probe, and marks the check as error when the model
returns a different first event type, for example `model_final_answer` instead
of `model_tool_call`.

## Current Status

As of 2026-07-03, local implementation has moved past the original baseline in
these areas:

- Scenario coverage is expanded to 23 assistant context cases.
- Planner notes distinguish monthly income, positions, analysis, and preview
  boundaries.
- Model-visible evidence gaps are normalized for continuation.
- Unknown-tool repair is covered by regression tests.
- Compact assistant diagnostics hide raw provider payloads and secret-looking
  fields.
- `assistant llm-check` and `assistant model check` support repeated
  `--text`, `--expect-tool`, and `--expect-event-type`.

The remaining high-ROI gap is argument quality: a live probe can now prove that
the model chose `monthly_income_report`, but cannot yet fail the probe if the
model used `account=lx` when the user asked for `sy`.

## Task 9: Add Expected Argument Subset Checks

**Files:**
- Modify: `src/application/assistant/diagnostics.py`
- Modify: `src/interfaces/cli/assistant_ops.py`
- Test: `tests/test_assistant_diagnostics.py`
- Test: `tests/test_cli_operator_commands.py`

- [x] **Step 1: Add the application-layer RED test**

Add this test near the existing expected live-probe tests in
`tests/test_assistant_diagnostics.py`:

```python
def test_llm_check_live_probe_marks_expected_argument_mismatch(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        _assistant_config(
            llm={
                "enabled": True,
                "provider": "openai",
                "model": "gpt-5.2",
                "api_key_env": "OM_LLM_API_KEY",
                "confidence_min": 0.75,
            }
        ),
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_LLM_API_KEY=sk-test\n", encoding="utf-8")

    def _create_tool_call_response(**_kwargs: Any) -> dict[str, Any]:
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_income_1",
                    "name": "monthly_income_report",
                    "arguments": '{"account":"lx","month":"2026-06","include_rows":true}',
                }
            ]
        }

    out = check_llm_planner(
        repo_root=tmp_path,
        config_path=cfg_path,
        env_file=env_file,
        include_local_env_file=False,
        live=True,
        live_text="sy 6月收益来源拆一下",
        live_expected_tools=["monthly_income_report"],
        live_expected_event_types=["model_tool_call"],
        live_expected_arguments=[{"account": "sy", "month": "2026-06"}],
        create_tool_call_response_fn=_create_tool_call_response,
    )

    live_probe = {item["name"]: item for item in out["checks"]}["live_probe"]
    probe = live_probe["value"]["probes"][0]
    assert out["summary"]["ok"] is False
    assert live_probe["status"] == "error"
    assert probe["expected_arguments"] == {"account": "sy", "month": "2026-06"}
    assert probe["selected_arguments"]["account"] == "lx"
    assert probe["argument_match"] is False
```

- [x] **Step 2: Run RED**

Run:

```bash
python3 -m pytest tests/test_assistant_diagnostics.py::test_llm_check_live_probe_marks_expected_argument_mismatch -q
```

Expected: FAIL with `TypeError: check_llm_planner() got an unexpected keyword
argument 'live_expected_arguments'`.

- [x] **Step 3: Implement subset matching in diagnostics**

In `src/application/assistant/diagnostics.py`, add the new public argument:

```python
def check_llm_planner(
    *,
    repo_root: str | Path,
    config_path: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
    live: bool = False,
    live_text: str = DEFAULT_LIVE_PROBE_TEXT,
    live_texts: list[str] | tuple[str, ...] | None = None,
    live_expected_tools: list[str] | tuple[str, ...] | None = None,
    live_expected_event_types: list[str] | tuple[str, ...] | None = None,
    live_expected_arguments: list[dict[str, Any] | str] | tuple[dict[str, Any] | str, ...] | None = None,
    create_tool_call_response_fn: CreateToolCallResponseFn | None = None,
) -> dict[str, Any]:
```

Add helpers below `_normalized_expected_event_types(...)`:

```python
def _normalized_expected_arguments(
    live_expected_arguments: list[dict[str, Any] | str] | tuple[dict[str, Any] | str, ...] | None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in live_expected_arguments or []:
        parsed = json.loads(item) if isinstance(item, str) else item
        if not isinstance(parsed, dict):
            raise AgentToolError(code="INPUT_ERROR", message="--expect-arguments must be a JSON object")
        normalized.append(dict(parsed))
    return normalized


def _argument_subset_matches(selected: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(selected.get(key) == value for key, value in expected.items())
```

Import `json` and `AgentToolError` if they are not already imported. Keep this
as a shallow subset comparison; do not add recursive matching until a real live
probe needs nested assertions.

- [x] **Step 4: Wire expected arguments into probe summaries**

Update `_live_probe_summary(...)` to accept `expected_arguments`, derive the
first selected arguments from the first step, and attach comparison fields:

```python
selected_arguments = first_step.get("arguments") if isinstance(first_step.get("arguments"), dict) else {}
if expected_arguments:
    summary["expected_arguments"] = dict(expected_arguments)
    summary["selected_arguments"] = dict(selected_arguments)
    summary["argument_match"] = _argument_subset_matches(selected_arguments, expected_arguments)
```

For the error-result branch, set:

```python
if expected_arguments:
    summary["expected_arguments"] = dict(expected_arguments)
    summary["selected_arguments"] = {}
    summary["argument_match"] = False
```

Update `_live_probe_check(...)` so each probe receives the aligned expected
argument object and `accepted` also requires:

```python
and probe.get("argument_match", True) is not False
```

- [x] **Step 5: Add CLI argument forwarding**

In both parser definitions in `src/interfaces/cli/assistant_ops.py`, add:

```python
assistant_llm_check.add_argument("--expect-arguments", action="append", default=None, help="expected JSON argument subset for each live probe")
assistant_model_check.add_argument("--expect-arguments", action="append", default=None, help="expected JSON argument subset for each live probe")
```

Forward the value from both `check_llm_planner_fn(...)` calls:

```python
            live_expected_arguments=args.expect_arguments,
```

- [x] **Step 6: Add CLI forwarding tests**

In `tests/test_cli_operator_commands.py`, extend the existing `assistant
llm-check` and `assistant model check` multi-probe assertions:

```python
        "--expect-arguments",
        '{"config_key":"us"}',
        "--expect-arguments",
        '{"account":"sy","month":"2026-06"}',
```

Then assert:

```python
assert calls[-1]["live_expected_arguments"] == [
    '{"config_key":"us"}',
    '{"account":"sy","month":"2026-06"}',
]
```

Also add `"live_expected_arguments": None` to the single-probe expected call
dictionary.

- [x] **Step 7: Verify focused GREEN**

Run:

```bash
python3 -m pytest tests/test_assistant_diagnostics.py tests/test_cli_operator_commands.py -q
git diff --check
```

Observed: `65 passed` and no whitespace errors.

Additional local guard: diagnostics now rejects extra expected argument/tool/event
entries when they outnumber live probe texts, so a malformed parity gate cannot
silently ignore trailing expectations. Observed: `66 passed` for
`tests/test_assistant_diagnostics.py tests/test_cli_operator_commands.py`.

Additional local guard: diagnostics now rejects `--expect-*` inputs unless
`--live` is enabled, so expectation-based parity checks cannot pass while the
provider probe is skipped. Observed: `67 passed` for
`tests/test_assistant_diagnostics.py tests/test_cli_operator_commands.py`.

- [ ] **Step 8: Run the approved live parity probe**

Only after explicit operator approval, run:

```bash
./om assistant llm-check --live \
  --text 状态 --expect-event-type model_tool_call --expect-tool runtime_status --expect-arguments '{"config_key":"us"}' \
  --text 'sy 6月收益来源拆一下' --expect-event-type model_tool_call --expect-tool monthly_income_report --expect-arguments '{"account":"sy"}' \
  --text '为什么 PDD 没进候选' --expect-event-type model_tool_call --expect-tool candidate_filter_explain --expect-arguments '{"symbol":"PDD"}'
```

This command sends assistant instructions, tool manifests, and probe text to the
configured external LLM provider. Do not run it without explicit approval.

## Stop Conditions

Stop and report instead of guessing when any of these happen:

- A failing scenario requires changing `config.yaml`, runtime config snapshots, Feishu delivery, broker state, ledger state, or notification behavior.
- A fix would require exposing the full `./om-agent spec` to the model.
- A proposed repair needs more than one automatic correction loop for the same error signature.
- A preview/write path would execute mutation inside `run_assistant_tool_event_loop`.
- A deterministic natural-language parser would become the primary route for normal assistant messages.

## Execution Order

1. Task 1: lock the quality matrix.
2. Task 2: improve selection hints only where the matrix shows ambiguity.
3. Task 3: normalize evidence-gap observations.
4. Task 4: fill exactly one recoverable-error coverage gap if the RED test proves it.
5. Task 5: preserve low-risk no-clarification and high-risk clarification boundaries.
6. Task 6: expose compact diagnostics.
7. Task 7: run validation.
8. Task 8: add multi-probe live planner diagnostics only if stronger model-selection evidence is still needed after Task 7.
9. Task 9: add expected argument subset checks before using live probes as a model parity gate.

No commit is part of this plan unless the user explicitly asks for `提交` or release.
