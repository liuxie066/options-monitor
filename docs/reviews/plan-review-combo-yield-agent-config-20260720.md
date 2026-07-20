# Plan Review — Combo Yield Agent Config

## Scope

Review of `docs/plans/combo-yield-agent-config-plan-20260720.md` against current Assistant, YAML authoring, CLI, validation, and Combo Yield runtime boundaries.

## Adversarial lenses

- Architecture: config mutation stays in `config_yaml_symbols.py`; Assistant and CLI remain adapters.
- Coupling: `combo_yield.enabled` must not modify Sell Put/Call template membership because runtime independence is canonical.
- Safety: preview is read-only; confirmed apply keeps backup, validation, atomic write, and rebuild behavior.
- Compatibility: all new parameters are optional and existing public calls remain valid.
- Rollout: release precedes production config use so old production code never receives an unsupported edit request.

## Findings

No blocking findings after revision.

The initial outline omitted CLI parity. Because the low-level writer is already public through `om config symbol set`, leaving the CLI unable to express the new field would create two inconsistent facades over one mutation contract. The accepted plan now includes one flag and forwarding test.

## Rejected over-design

- No generic dotted-path writer: it would enlarge the write trust boundary and bypass field-specific validation.
- No Combo Yield policy editor: only enablement is requested.
- No changes to runtime scan orchestration: existing runtime already treats Combo Yield independently.

## Validation requirements

- Low-level dry-run proves no file write and exact changed path.
- Inbound preview proves no write; confirmation proves YAML write and rebuilt runtime value.
- CLI test proves argument forwarding.
- Existing YAML/Inbound/CLI suites prove regression safety.
- Production verification must be read-only after apply and must not execute tick/send paths.

## Decision

- Plan review: pass.
- Plan is code-generation-ready.
