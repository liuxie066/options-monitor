# Agent Core Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the assistant's free-form Q&A path from model-planned table rendering to a task-owned evidence and synthesis pipeline.

**Architecture:** `AgentTask` becomes the execution contract. `TaskProfile` is the only authority for evidence, views, answer requirements, and completion checks. The model is used for controlled synthesis over host-collected evidence, not for deciding the high-value OM evidence path.

**Tech Stack:** Python 3, existing `./om assistant` runtime, existing `analysis_query` read surface, pytest.

**Execution Status:** Implemented locally in the current working tree. The older `2026-07-05-agent-task-runtime.md` draft was removed because it still proposed catalog recipes and fake evidence-plan fields. Current verification uses assistant pytest suites, scenario eval, old-link grep, `git diff --check`, and a local-only mock LLM CLI probe; external provider live probes require explicit data-transfer approval because tool evidence contains private trading data.

---

## Scope

This refactor is not a full repo rewrite. It only changes the assistant free-form Q&A main path.

Current-state note:
- Some `TaskProfile` / `AgentTask` / `EvidencePlan` / `TaskCompletion` code may already exist in the working tree.
- Treat the steps below as cutover gates, not as proof that every named symbol is still missing.
- If a step is already implemented, verify it with the named test and move to the next deletion or merge gate.

Keep:
- OM read tools and their safety gates.
- `analysis_query` as the read-only generic data surface.
- `analysis_catalog` as an internal view/field catalog.
- Existing command-style tools and preview-write safety boundaries.

Delete or stop producing:
- Task recipes duplicated inside `analysis_catalog`.
- `selected_recipe` as an active planning authority.
- Profile-specific hardcoding in coverage and answer verification.
- Fake `EvidencePlan` fields that do not drive execution.
- Analysis-task raw-table final fallback.
- Duplicate month parsers outside `time_filters`.

## Deletion Design

The purpose of deletion is to remove competing authorities, not to reduce line count for its own sake.

| Delete / retire | Current problem | Replacement authority |
|---|---|---|
| `analysis_catalog` task recipes / `investigation_recipes` | Duplicates task intent and evidence rules outside the assistant task system | `TaskProfile.required_views` and `TaskProfile.required_answer` |
| `selected_recipe` in new task payloads | Lets a model/cached contract override host-owned task selection | `AgentTask.profile_names` |
| `EvidencePlan.source_tools`, `preferred_first_calls`, `max_followup_calls` | Describes a plan without being the executable plan | `EvidencePlan.calls` |
| Coverage verifier recipe branches | Hardcodes one task profile and diverges from profile definitions | Profile-driven required-view checks |
| Answer verifier profile-name branches | Makes answer shape enforcement drift by task | Profile-driven `completion_answer_keys` |
| Duplicate month extraction in task/runtime/loop code | Creates inconsistent scope for direct questions and follow-ups | `time_filters.extract_month_filters` |
| `_assistant_task_incomplete_text` as local fallback | Converts task failure into generic prose outside completion state | `TaskCompletion` status and explicit missing evidence |
| Task-shaped `analysis_result` final fallback | Turns evidence rows into the final answer, causing the observed ClawBot failure | Host evidence + model synthesis + final verification |
| Prompt instructions to fill recipe fields | Pushes host-owned planning back into model text | Structured tool calls plus host-owned task plan |

Do not delete:
- `analysis_catalog` itself. It remains the field/view catalog for unknown tasks and SQL safety.
- `analysis_result` renderer globally. It remains useful for non-task diagnostics and direct structured tool output.
- The model event loop. It remains the fallback for unknown tasks and the synthesis mechanism after evidence is collected.

## Merge Design

| Existing responsibility | Merge into | Rule after merge |
|---|---|---|
| Task recipe selection | `task_profiles.py` | Known OM analysis tasks are selected only by `TaskProfile` matching. |
| Scope/month parsing | `time_filters.py` | All month extraction goes through `extract_month_filters`. |
| Evidence selection | `evidence_planner.py` | Known profiles produce executable `EvidenceCall` objects before model planning. |
| Completion / evidence gaps | `task_completion.py` | The assistant checks successful evidence count and required views before synthesis. |
| Coverage verification | `coverage_verifier.py` reading `TaskProfile` | Coverage has no local task recipe table. |
| Answer verification | `answer_verifier.py` reading `TaskProfile` | Required answer keys and source-policy checks come from profile definitions. |
| User-visible task failure text | `agent_loop.py` consuming `TaskCompletion` | One explicit missing-evidence response path, no raw fallback for task-shaped questions. |

## Cutover Order

1. Lock behavior with tests for the exact June option-review failure and the `结论呢` follow-up.
2. Merge parsing and task profile authority first, because later steps depend on stable scope/profile selection.
3. Delete catalog recipes and `selected_recipe`, so no old task authority remains active.
4. Make `EvidencePlan.calls` executable and route known profiles through host-owned read evidence before model planning.
5. Replace fallback prose with `TaskCompletion`, then block raw table final answers for synthesis-required tasks.
6. Make coverage and answer verification profile-driven.
7. Update prompt/fixtures/docs only after code paths prove the new authority chain.
8. Run focused assistant tests, plugin contract tests, scenario eval, and `git diff --check`.

## Target Flow

```text
user text
-> derive AgentTask
-> build executable EvidencePlan
-> run host-owned read evidence calls
-> check TaskCompletion
-> synthesize answer from evidence bundle
-> final verifier blocks only unsafe/incomplete output
```

The old model-planner path remains only for tasks with no matching `TaskProfile`.

## Files

Modify:
- `src/application/assistant/task_profiles.py`: single authority for task profiles, required views, answer keys, completion checks.
- `src/application/assistant/task_runtime.py`: derive one `AgentTask` per request and follow-up context.
- `src/application/assistant/evidence_planner.py`: replace view-only plan with executable read calls.
- `src/application/assistant/task_completion.py`: replace fallback text helper with real completion status.
- `src/application/assistant/time_filters.py`: own single and multi-month parsing.
- `src/application/assistant/agent_loop.py`: consume host `EvidencePlan` before model planner for known task profiles.
- `src/application/assistant/coverage_verifier.py`: read profile requirements instead of hardcoded recipes.
- `src/application/assistant/answer_verifier.py`: read profile answer requirements instead of profile-name branches.
- `src/application/agent_tools/analysis.py`: remove `investigation_recipes` duplication; keep catalog views.
- `src/application/assistant/task_contract.py`: stop producing `selected_recipe`; keep backward-tolerant reads only if needed.
- `src/application/assistant/renderer.py`: prevent raw table renderer from being final answer for task-shaped analysis.
- `tests/test_assistant_task_runtime.py`: expand into task engine, evidence plan, completion, follow-up tests.
- `tests/test_analysis_tools.py`: remove recipe expectations from catalog tests.
- `tests/test_assistant_runtime.py`, `tests/test_assistant_event_executor.py`, fixtures: update route expectations.
- `docs/OM_ASSISTANT_ARCHITECTURE.md`: describe the single-authority task path.

Do not create new core files unless an existing file becomes clearly unreadable. The current four task files are enough.

---

### Task 1: Lock the Required Behavior With Failing Tests

**Files:**
- Modify: `tests/test_assistant_task_runtime.py`
- Modify: `tests/test_assistant_event_executor.py`

- [ ] **Step 1: Add task-owned option review tests**

Add tests that fail against the current shell implementation:

```python
def test_option_review_plan_builds_executable_evidence_calls() -> None:
    task = derive_agent_task(
        question="分析6月的期权操作有没有不合理，需要优化的地方",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )

    plan = plan_task_evidence(task)

    assert [call.tool_name for call in plan.calls] == ["analysis_query"]
    assert plan.calls[0].arguments["views"] == [
        "account_monthly_performance",
        "account_monthly_income_components",
        "monthly_income_cashflow_rows",
        "trade_events",
        "open_option_exposure",
        "strategy_config_by_symbol_account",
        "strategy_replay_read_surface",
    ]
    assert plan.calls[0].arguments["month"] == "2026-06"
```

```python
def test_option_review_partial_rows_are_not_complete() -> None:
    task = derive_agent_task(
        question="分析6月的期权操作有没有不合理，需要优化的地方",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context=None,
    )
    completion = check_task_completion(
        task=task,
        covered_views={"account_monthly_performance"},
        successful_tool_count=1,
    )

    assert completion.status == "need_more_evidence"
    assert "trade_events" in completion.missing_views
    assert completion.next_action == "followup_tool"
```

```python
def test_conclusion_followup_reuses_latest_compatible_task_when_multiple_turns_exist() -> None:
    task = derive_agent_task(
        question="结论呢",
        request_context={},
        today=date(2026, 7, 5),
        conversation_context={
            "context_projection": {
                "recent_turns": [
                    {"turn_id": "turn_noise", "user_summary": "今天状态", "assistant_summary": "正常"},
                    {
                        "turn_id": "turn_review",
                        "user_summary": "分析6月的期权操作有没有不合理，需要优化的地方",
                        "assistant_summary": "读取了交易和敞口证据",
                        "safe_slots": {"month": ["2026-06"]},
                        "evidence_refs": ["ev_review"],
                    },
                ],
                "available_evidence_refs": [
                    {
                        "ref_id": "ev_review",
                        "turn_id": "turn_review",
                        "source_tool": "analysis_query",
                        "safe_slots": {"month": ["2026-06"]},
                        "data_shape": {"views_used": ["trade_events", "open_option_exposure"]},
                    }
                ],
            }
        },
    )

    assert task.name == "option_operation_review"
    assert task.scope["requested_months"] == ["2026-06"]
```

- [ ] **Step 2: Run the new tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_assistant_task_runtime.py -q
```

Expected: fail because `EvidencePlan.calls`, `check_task_completion`, and multi-turn compatible follow-up do not exist yet.

---

### Task 2: Merge Month Parsing Into `time_filters`

**Files:**
- Modify: `src/application/assistant/time_filters.py`
- Modify: `src/application/assistant/task_runtime.py`
- Modify: `src/application/assistant/agent_loop.py`
- Modify: `src/application/assistant/task_contract.py`

- [ ] **Step 1: Move multi-month parser to `time_filters.py`**

Add:

```python
def extract_month_filters(text: str, *, today: date) -> list[str]:
    raw = str(text or "")
    compact = re.sub(r"\s+", "", raw)
    found: list[tuple[int, str]] = []
    for match in _MONTH_RE.finditer(raw):
        found.append((match.start(), f"{match.group(1)}-{match.group(2)}"))
    occupied = [(match.start(), match.end()) for match in _YEAR_MONTH_CN_RE.finditer(compact)]
    for match in _YEAR_MONTH_CN_RE.finditer(compact):
        month = _month_number(match.group(2))
        if month:
            found.append((match.start(), f"{int(match.group(1)):04d}-{month:02d}"))
    for match in _MONTH_CN_RE.finditer(compact):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        month = _month_number(match.group(1))
        if month:
            found.append((match.start(), f"{today.year:04d}-{month:02d}"))
    if not found:
        month = extract_month_filter(raw, today=today)
        return [month] if month else []
    out: list[str] = []
    seen: set[str] = set()
    for _position, month in sorted(found, key=lambda item: item[0]):
        if month not in seen:
            seen.add(month)
            out.append(month)
    return out
```

- [ ] **Step 2: Delete duplicate month parsers**

Delete:
- `src/application/assistant/task_runtime.py` local `_MONTH_*` regex constants, `_CN_MONTHS`, `_extract_all_months`, `_month_number`.
- `src/application/assistant/agent_loop.py` local `_extract_month_filters`, `_month_filter_number`, duplicate month constants if unused.
- `src/application/assistant/task_contract.py` private `_extract_months` body should delegate to `extract_month_filters`.

- [ ] **Step 3: Run month-related tests**

Run:

```bash
python3 -m pytest tests/test_assistant_task_runtime.py tests/test_assistant_runtime.py::test_planner_input_payload_includes_agent_task_evidence_plan_for_option_review -q
```

Expected: pass after imports are updated.

---

### Task 3: Make `TaskProfile` the Single Authority

**Files:**
- Modify: `src/application/assistant/task_profiles.py`
- Modify: `src/application/assistant/task_runtime.py`
- Modify: `src/application/assistant/task_contract.py`

- [ ] **Step 1: Extend `TaskProfile` with executable requirements**

Replace `primary_analysis_views`, `source_tools`, and `max_followup_calls` with fields that are actually used:

```python
@dataclass(frozen=True)
class TaskProfile:
    name: str
    domains: tuple[str, ...]
    task_modes: tuple[str, ...]
    trigger_terms: tuple[str, ...]
    required_evidence: tuple[str, ...]
    required_views: tuple[str, ...]
    required_answer: tuple[str, ...]
    answer_shape: tuple[str, ...]
    completion_answer_keys: tuple[str, ...]
    tool_name: str = "analysis_query"
```

- [ ] **Step 2: Update `option_operation_review` profile**

Use one exact authority:

```python
TaskProfile(
    name="option_operation_review",
    domains=("strategy", "position", "income", "general"),
    task_modes=("analyze", "recommend", "summarize"),
    trigger_terms=("期权操作", "期权交易", "交易记录", "复盘", "不合理", "优化"),
    required_views=(
        "account_monthly_performance",
        "account_monthly_income_components",
        "monthly_income_cashflow_rows",
        "trade_events",
        "open_option_exposure",
        "strategy_config_by_symbol_account",
        "strategy_replay_read_surface",
    ),
    required_answer=("overall_judgement", "operation_patterns", "optimization_options", "source_and_policy"),
    answer_shape=("judgement", "weak_patterns", "options", "evidence_boundary"),
    completion_answer_keys=("overall_judgement", "operation_patterns", "optimization_options", "source_and_policy"),
)
```

- [ ] **Step 3: Stop producing `selected_recipe`**

Delete from `task_runtime.py`:
- `_selected_recipe`
- `"selected_recipe": _selected_recipe(self)` in `task_contract_patch`

In `task_contract.py`, stop adding `selected_recipe` to new public payloads. Keep tolerant reads only if old stored traces require them.

- [ ] **Step 4: Run profile tests**

Run:

```bash
python3 -m pytest tests/test_assistant_task_runtime.py -q
```

Expected: profile tests pass after assertions switch from `primary_analysis_views` to `required_views`.

---

### Task 4: Delete Catalog Recipes and Recipe Verifier Path

**Files:**
- Modify: `src/application/agent_tools/analysis.py`
- Modify: `src/application/assistant/coverage_verifier.py`
- Modify: `tests/test_analysis_tools.py`
- Modify: `tests/test_assistant_evidence_session.py`
- Modify: `tests/test_agent_plugin_contract.py`

- [ ] **Step 1: Delete duplicated catalog recipes**

Delete:
- `_catalog_investigation_recipes`
- `"investigation_recipes": _catalog_investigation_recipes(specs)` from `_analysis_catalog_tool`
- `investigation_recipes` entries from `analysis_catalog` output contract fact fields.

Keep catalog view metadata and SQL safety hints.

- [ ] **Step 2: Delete recipe coverage gaps**

Delete from `coverage_verifier.py`:
- `_recipe_evidence_gaps`
- `_recipe_operation_gap` if it becomes unused
- recipe-specific tests that assert `selected_recipe`

Replace with generic profile gap logic in Task 5.

- [ ] **Step 3: Update tests**

Remove catalog assertions that expect:
- `income_analysis_breakdown`
- `strategy_replay_review`
- `option_operation_review`
- `action_lifecycle_audit`

Keep tests that prove `analysis_catalog` exposes whitelisted views and fields.

- [ ] **Step 4: Run catalog tests**

Run:

```bash
python3 -m pytest tests/test_analysis_tools.py tests/test_agent_plugin_contract.py -q
```

Expected: pass with catalog as view catalog only.

---

### Task 5: Replace View-Only `EvidencePlan` With Executable Calls

**Files:**
- Modify: `src/application/assistant/evidence_planner.py`
- Modify: `src/application/assistant/agent_loop.py`
- Modify: `tests/test_assistant_task_runtime.py`
- Modify: `tests/test_assistant_runtime.py`

- [ ] **Step 1: Replace fake plan fields**

Delete from `EvidencePlan`:
- `source_tools`
- `preferred_first_calls`
- `max_followup_calls`

Add:

```python
@dataclass(frozen=True)
class EvidenceCall:
    tool_name: str
    arguments: dict[str, Any]
    purpose: str

@dataclass(frozen=True)
class EvidencePlan:
    task_name: str
    calls: tuple[EvidenceCall, ...]
    required_views: tuple[str, ...]
    schema_version: str = EVIDENCE_PLAN_SCHEMA_VERSION
```

- [ ] **Step 2: Generate one host-owned call for option reviews**

For `analysis_query`, generate structured arguments first:

```python
arguments = {
    "views": list(profile.required_views),
    "month": months[0] if len(months) == 1 else None,
    "months": months if len(months) > 1 else [],
    "limit": 200,
}
```

If `analysis_query` does not yet accept `views`, implement the smallest adapter in `agent_loop.py` that converts this payload to the existing SQL payload before execution. Do not make the model write the SQL for known task profiles.

- [ ] **Step 3: Wire known profiles before model planner**

In `agent_loop.py`, when `agent_task.profile_names` is non-empty and the task is read-only:
- build `EvidencePlan`
- convert each `EvidenceCall` to the existing tool event type
- execute it through the existing tool executor
- continue to completion/synthesis

The model planner remains the fallback for unknown tasks.

- [ ] **Step 4: Run planner route tests**

Run:

```bash
python3 -m pytest tests/test_assistant_task_runtime.py tests/test_assistant_runtime.py::test_event_native_plan_keeps_host_agent_task_contract -q
```

Expected: known option review path uses host evidence calls; no fake plan fields remain.

---

### Task 6: Turn `TaskCompletion` Into a Real State

**Files:**
- Modify: `src/application/assistant/task_completion.py`
- Modify: `src/application/assistant/coverage_verifier.py`
- Modify: `src/application/assistant/agent_loop.py`
- Modify: `tests/test_assistant_task_runtime.py`

- [ ] **Step 1: Replace fallback helper with completion result**

Use:

```python
@dataclass(frozen=True)
class TaskCompletion:
    status: str
    missing_views: tuple[str, ...] = ()
    missing_answer: tuple[str, ...] = ()
    next_action: str = "synthesize"
    reason: str = ""


def check_task_completion(
    *,
    task: AgentTask,
    covered_views: set[str],
    successful_tool_count: int,
) -> TaskCompletion:
    if successful_tool_count <= 0:
        return TaskCompletion(status="need_more_evidence", next_action="followup_tool", reason="no_successful_evidence")
    required = set(task.required_views)
    missing = tuple(sorted(required - covered_views))
    if missing:
        return TaskCompletion(status="need_more_evidence", missing_views=missing, next_action="followup_tool")
    return TaskCompletion(status="ready_to_synthesize")
```

- [ ] **Step 2: Delete old fallback-only API**

Delete:
- `agent_loop._assistant_task_incomplete_text`
- task-shaped calls from `_assistant_tool_loop_response_text` into `user_fallback_from_tool_results`

Move user-facing fallback text to the place that handles `TaskCompletion.status`.

- [ ] **Step 3: Wire completion into AgentLoop**

After evidence execution:
- collect covered views from evidence bundle
- call `check_task_completion`
- if `need_more_evidence`, run missing evidence call once when recoverable
- if still incomplete, return explicit missing-evidence text
- if `ready_to_synthesize`, call synthesis path

- [ ] **Step 4: Run completion tests**

Run:

```bash
python3 -m pytest tests/test_assistant_task_runtime.py tests/test_assistant_event_executor.py::test_task_shaped_tool_loop_rejects_raw_analysis_fallback -q
```

Expected: incomplete task returns missing evidence, not raw table fallback.

---

### Task 7: Make Verifiers Profile-Driven

**Files:**
- Modify: `src/application/assistant/coverage_verifier.py`
- Modify: `src/application/assistant/answer_verifier.py`
- Modify: `src/application/assistant/task_profiles.py`
- Modify: `tests/test_assistant_task_runtime.py`

- [ ] **Step 1: Replace profile-name hardcoding in coverage**

Delete:
- `if "option_operation_review" not in _task_profile_names(...)`
- hardcoded `required_views = {...}`

Use:

```python
for profile in task_profiles_from_contract(task_contract):
    missing = sorted(set(profile.required_views) - _covered_views(datasets))
```

- [ ] **Step 2: Replace answer verifier hardcoding**

Delete:
- `if "option_operation_review" in task_profiles: ...`

Use:

```python
for profile in task_profiles_from_contract_payload(contract):
    enforced_missing_keys.update(profile.completion_answer_keys)
    strict_shape_keys.update(profile.completion_answer_keys)
    if "evidence_boundary" in profile.answer_shape:
        enforced_shape_keys["evidence_boundary"] = "source_and_policy"
```

- [ ] **Step 3: Keep `profile_by_name` because it now has a real caller**

Use `profile_by_name` in both verifiers. Do not duplicate profile maps.

- [ ] **Step 4: Run verifier tests**

Run:

```bash
python3 -m pytest tests/test_assistant_task_runtime.py tests/test_assistant_model_evidence.py -q
```

Expected: option review behavior still enforced, but no verifier branch mentions that profile name.

---

### Task 8: Remove Analysis-Task Raw Table Final Fallback

**Files:**
- Modify: `src/application/assistant/agent_loop.py`
- Modify: `src/application/assistant/renderer.py`
- Modify: `tests/test_assistant_event_executor.py`

- [ ] **Step 1: Keep raw renderer for non-task diagnostic use**

Do not delete table rendering globally. Delete only the path that lets task-shaped analysis questions finish as:
- `分析完成：共 N 行`
- `已完成工具调用，但当前结果没有可渲染的文本`

Current renderer functions such as `renderer._analysis_result_summary_line` and
`model_evidence._analysis_query_summary_line` may continue to exist for direct
tool rendering. The deletion target is the AgentLoop branch that allows those
strings to become the final answer when `AgentTask.requires_synthesis` is true.

- [ ] **Step 2: Use completion status for task-shaped responses**

For `AgentTask.requires_synthesis`:
- no final raw table answer
- no canonical renderer final answer unless profile says `task_mode=summarize` and no synthesis is required
- incomplete evidence returns missing evidence text

- [ ] **Step 3: Run fallback tests**

Run:

```bash
python3 -m pytest tests/test_assistant_event_executor.py::test_task_shaped_tool_loop_rejects_raw_analysis_fallback tests/test_assistant_event_executor.py::test_task_shaped_tool_loop_reports_no_successful_evidence -q
```

Expected: both pass without `agent_loop._assistant_task_incomplete_text`.

---

### Task 9: Update Prompt and Fixtures to Reflect Host-Owned Tasks

**Files:**
- Modify: `src/application/assistant/agent_loop.py`
- Modify: `tests/fixtures/assistant_context_scenarios.jsonl`
- Modify: `tests/fixtures/assistant_agent_eval.jsonl`
- Modify: `tests/test_assistant_context_eval.py`
- Modify: `tests/test_assistant_agent_eval.py`

- [ ] **Step 1: Delete obsolete planner prompt recipe rules**

Remove prompt lines that tell the model:
- to fill `selected_recipe.name`
- to choose `option_operation_review`
- to use `analysis_catalog` for known profile evidence discovery

Keep generic safety rules:
- read-only tools only
- do not invent SQL columns
- ask clarification when no safe plan exists

- [ ] **Step 2: Update fixtures**

For option review fixtures, expect:
- `agent_task.profile_names=["option_operation_review"]`
- host evidence plan source
- no `selected_recipe`
- no catalog recipe dependency

- [ ] **Step 3: Run scenario eval**

Run:

```bash
./om assistant eval-context --mode scenarios
```

Expected: all scenarios pass with no `selected_recipe` expectations.

---

### Task 10: End-to-End Validation

**Files:**
- Modify: `docs/OM_ASSISTANT_ARCHITECTURE.md`

- [ ] **Step 1: Document the new authority path**

Add this exact invariant:

```text
For known OM free-form analysis tasks, TaskProfile is the only authority for required evidence, required views, answer shape, and completion. analysis_catalog may expose available views, but it must not define task recipes.
```

- [ ] **Step 2: Run focused assistant tests**

Run:

```bash
python3 -m pytest tests/test_assistant_task_runtime.py tests/test_assistant_event_executor.py tests/test_assistant_runtime.py tests/test_assistant_model_evidence.py -q
```

Expected: pass.

- [ ] **Step 3: Run plugin contract tests**

Run:

```bash
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q
```

Expected: pass.

- [ ] **Step 4: Run formatting/diff guard**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

## Delete Checklist

- [ ] `analysis.py::_catalog_investigation_recipes`
- [ ] `analysis_catalog` output `investigation_recipes`
- [ ] active production of `TaskContract.selected_recipe`
- [ ] `task_runtime._selected_recipe`
- [ ] `coverage_verifier._recipe_evidence_gaps`
- [ ] `coverage_verifier` hardcoded `option_operation_review` view set
- [ ] `answer_verifier` hardcoded `option_operation_review` answer keys
- [ ] `EvidencePlan.source_tools`
- [ ] `EvidencePlan.preferred_first_calls`
- [ ] `EvidencePlan.max_followup_calls`
- [ ] `agent_loop._assistant_task_incomplete_text`
- [ ] duplicate month parser in `task_runtime.py`
- [ ] duplicate month parser in `agent_loop.py`
- [ ] task-shaped raw table final fallback
- [ ] task-shaped `user_fallback_from_tool_results` final fallback
- [ ] planner prompt instruction to fill `selected_recipe`

## Merge Checklist

- [ ] All month parsing goes through `time_filters.extract_month_filters`.
- [ ] Coverage requirements come from `TaskProfile.required_views`.
- [ ] Answer requirements come from `TaskProfile.completion_answer_keys`.
- [ ] Analysis task evidence calls come from `EvidencePlan.calls`.
- [ ] Follow-up task inheritance uses latest compatible turn/evidence ref, not `len(recent_turns) == 1`.
- [ ] `analysis_catalog` remains only a view catalog, not a task recipe catalog.
- [ ] Direct tool rendering remains available for diagnostics, but task-shaped free-form Q&A goes through synthesis.

## Acceptance Criteria

- [ ] `"分析6月的期权操作有没有不合理，需要优化的地方"` returns a synthesized judgement, weak patterns, concrete evidence, optimization options, and evidence boundary.
- [ ] `"结论呢"` after that task reuses the prior task/evidence and does not return a table.
- [ ] The codebase has one task authority: `TaskProfile`.
- [ ] The codebase has one month parser authority: `time_filters`.
- [ ] Known task profiles do not rely on model-written SQL as the first evidence path.
- [ ] No active path requires `selected_recipe`.
- [ ] `rg -n "selected_recipe|investigation_recipes|primary_analysis_views|preferred_first_calls|max_followup_calls|_assistant_task_incomplete_text" src/application tests` has no active-runtime hits except legacy compatibility tests.
