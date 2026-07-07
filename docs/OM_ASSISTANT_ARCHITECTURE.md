# OM Assistant Architecture

This document is the current authority for the local Tool Gateway and inbound
assistant boundary.

## Current State

OM now keeps two separate surfaces:

| Surface | Entry | Purpose |
|---|---|---|
| Tool Gateway | `./om-agent` | Structured JSON tools for read diagnostics and gated write operations |
| Inbound Assistant | `./om assistant handle` | Channel-facing command handler for slash commands, permission replies, audit, and receipts |
| Copilot v2 Prototype | `./om assistant copilot-run` | Local-only read-first task runtime prototype; not wired to inbound channels |

Free-form natural-language execution is intentionally disabled while the next
task system is rebuilt. Non-slash, non-permission text returns
`NATURAL_LANGUAGE_REBUILDING` and does not call tools or fall back to a generic
LLM reply.

## Supported Flow

```text
channel message
-> sender allowlist
-> command / permission parser
-> reasoning over a known command contract
-> permission gate and tool execution
-> observation
-> renderer
-> audit/session/operation persistence
```

The assistant currently supports:

- slash commands such as `/help`, `/status`, `/income`, `/positions`, `/cash`,
  `/model`, `/trace`, `/upgrade`, `/confirm`, and `/cancel`;
- deterministic read tools routed through `agent_tool_registry`;
- write previews that require explicit confirmation before applying changes;
- permission responses for pending operations;
- model/profile diagnostics as configuration surfaces.

The assistant currently does not support:

- inbound free-form analysis questions;
- automatic tool planning from arbitrary natural language;
- model-authored answer synthesis over tool observations;
- fallback from failed free-form handling to the generic planner or ordinary LLM
  chat.

`copilot-run` is a local prototype for rebuilding that capability with an
explicit task frame, read-only evidence plan, evidence ledger, answer
verification, and trace. It may call the configured assistant LLM locally, but
it does not change `./om assistant handle` behavior.

## Tool Authority

`agent_tool_registry` is the canonical tool registry. Assistant code must not
create a parallel tool registry or hidden execution path.

Read and write boundaries stay in the existing tool metadata:

- read tools may execute directly when a slash command maps to them;
- write-capable tools must produce a preview unless an explicit confirmed path
  is used;
- write gates remain in `agent_tools/permissions.py` and operation-specific
  preview/confirm modules.

## Model Config Boundary

`assistant.llm`, `assistant.models`, and `assistant.active_model` may remain in
config so operators can inspect and manage future model profiles. These settings
do not enable free-form execution in the current runtime.

For compatibility, existing `assistant.agent_loop.enabled` config is still
accepted as a no-op compatibility field. Runtime metadata marks free-form
execution as disabled.

## Removed Runtime

The previous free-form stack has been removed from the active code path and from
source modules:

- `agent_loop.py`
- `copilot.py`
- `task_profiles.py`
- `model_events.py`
- `model_evidence.py`
- `answer_guard.py`
- `coverage_verifier.py`
- `task_completion.py`
- `llm_reply.py`
- `context_eval.py`
- `conversation_context.py`
- `context_projection.py`
- `context_validation.py`

Do not reintroduce these modules or rebuild them under a new name. The next
generation task system should be designed as a first-class execution model, with
explicit evidence acquisition, bounded actions, and evals that measure answer
quality without hardcoded business templates.

## Release Gate

Assistant changes should run the current minimal gate:

```bash
python3 -m pytest tests/test_assistant_runtime.py tests/test_inbound_control.py tests/test_assistant_permission_request.py tests/test_cli_operator_commands.py tests/test_assistant_diagnostics.py tests/test_architecture_guards.py
python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py tests/test_candidate_filter_trace.py tests/test_analysis_tools.py
```
