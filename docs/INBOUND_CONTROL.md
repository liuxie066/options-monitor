# Inbound Assistant Control

Inbound Assistant is the channel-facing command surface for Feishu and WeChat.
It is not the Tool Gateway and it is not a free-form autonomous agent.

## Runtime Boundary

```text
Feishu / WeChat message
-> channel adapter
-> ./om assistant handle
-> sender allowlist
-> command parser or permission-response parser
-> tool execution or pending-operation update
-> rendered reply
-> audit/session/operation store
```

Current behavior:

- slash commands execute deterministic command contracts;
- permission replies operate on pending previews;
- unsupported natural-language text returns `NATURAL_LANGUAGE_REBUILDING`;
- unsupported text does not call tools, does not synthesize an answer, and does
  not fall back to generic LLM chat.

## Supported Inputs

Use `/help` as the user-facing catalog. The stable categories are:

- status and diagnostics: `/status`, `/health`, `/trace`;
- read operations: `/income`, `/positions`, `/cash`, model/config inspection;
- write previews: symbol/trade/upgrade commands that create pending operations;
- confirmation flow: `/confirm ...`, `/cancel ...`, and bound channel replies.

Channel wrappers should call only `./om assistant handle` or the equivalent
application service. They must not import parser, policy, or tool modules
directly.

## Read Operations

Read operations are deterministic tool calls selected by explicit command
syntax. Tool implementations and metadata live under `src/application/agent_tools`
and are collected by `agent_tool_registry`.

Read commands may include a default `config_key` supplied by the channel
settings. Missing upstream data must be reported explicitly; the assistant must
not invent facts to fill a report.

## Write Operations

Write-capable requests use preview and confirmation:

```text
explicit command
-> preview receipt
-> pending operation
-> explicit confirmation
-> apply path
-> readback receipt
```

The preview does not mutate live config, positions, trade events, notifications,
or broker-facing state. Confirmation is required before an apply path runs.

## Reply Contract

Channel adapters may send the rendered `response_text` even when the inbound
result is not successful. For example, `NATURAL_LANGUAGE_REBUILDING` is an
assistant error but should still reply to the user with the rebuilding message.

Permission-denied handling remains special:

- unauthorized senders may stay silent depending on channel policy;
- allowed senders receive deterministic command results or explicit errors.

## Configuration

Relevant assistant config:

- `assistant.enabled`: enables the command surface;
- `assistant.default_market_scope`: default market for commands that need one;
- `assistant.context_window_messages`: retained for trace/session context and
  future rebuild work;
- `assistant.llm`, `assistant.models`, `assistant.active_model`: model config and
  diagnostics only in the current runtime.

For compatibility, `assistant.agent_loop.enabled` may still be present in older
configs. It is accepted as a no-op compatibility field and does not enable
free-form natural-language execution.

## Current Non-Goals

Do not add:

- hardcoded business-question templates for free-form questions;
- parallel tool registries;
- channel-specific business routing;
- generic LLM fallback answers;
- automatic write execution from natural language.

The next free-form task system should be rebuilt deliberately as a separate
design, with evals and evidence contracts first.
