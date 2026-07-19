# Gateflow Implementation — Daily Decision Brief S4

- **Gate**: implementation
- **Work unit**: `daily-decision-brief`
- **Slice**: S4 — CLI, Agent Tool, config and docs
- **Date**: 2026-07-19
- **Base**: accepted S3 commit `b3c405c6`
- **Status**: implementation complete; ready for code review
- **Artifact path**: `docs/gateflow/daily-decision-brief-s4-implementation-20260719.md`

## Objective and completion signal

Expose the canonical Daily Decision Brief through one human CLI and one pure-read Agent Tool, add a fail-fast/default-off public configuration contract, and document timing/actionability/safety semantics. Both read surfaces use the existing canonical repository and renderer; neither refreshes data, sends notifications, mutates delivery state, or enables production behavior.

Completion signals achieved:

- `./om daily-brief latest|day` supports latest, trading-day latest, exact revision, Markdown and JSON.
- Agent Tool `daily_decision_brief_read` supports latest/day/revision with structured output and bounded Markdown.
- Expired persisted LIVE briefs preserve stored `actionability` while returning `effective_actionability=planning_only` and rendering PLANNING.
- Missing artifacts return explicit `available=false` rather than parsing legacy notification text.
- Manifest proves read-only, no side effects, no confirmation, read-only risk, and idempotent annotation.
- `notifications.daily_brief` defaults to disabled; invalid object/boolean/limit contracts fail fast.
- Public docs state 09:40 normal first opportunity, 10:00 explicit process-failure recovery point, advisory-only behavior, closed-market planning-only semantics, and separate production authorization.

## Changed files

Production/public contract:

- `src/application/agent_tools/daily_brief.py`
- `src/application/agent_tool_registry.py`
- `src/interfaces/cli/daily_brief_ops.py`
- `src/interfaces/cli/main.py`
- `src/application/config_defaults.py`
- `src/application/config_validator.py`
- `configs/examples/user.common.example.json`
- `configs/system.json`
- `README.md`
- `docs/AGENT_WIKI.md`
- `docs/DEPENDENCY_GRAPH.md`
- `docs/dependency_graph.mmd`

Focused tests:

- `tests/test_daily_decision_brief_agent_tool.py`
- `tests/test_daily_decision_brief_cli.py`
- `tests/test_validate_config_notifications.py`

## Implementation decisions

1. **Accepted public interface preserved**
   - CLI follows the accepted plan: `daily-brief latest` and `daily-brief day`, optional market defaulting to US, `--json` for structured output, and exact revision only on the day command.
   - Agent Tool follows the accepted plan name and fields: `daily_decision_brief_read` with `account`, optional `market`, optional `date`, and optional non-negative integer `revision`.
   - A pre-artifact flattened CLI/tool-name draft was removed rather than retained as an unpublished alias; this avoids two public contracts and keeps the accepted plan authoritative.

2. **One shared read model**
   - CLI and Agent Tool call `read_daily_brief_view()`.
   - The view reads canonical latest/day/revision state and renders through `render_full_brief()`.
   - No legacy notification Markdown, scan invocation, quote refresh, route resolution, provider call, or pointer write is used.

3. **Stored versus effective actionability**
   - Structured output preserves the stored brief unchanged for audit.
   - `effective_daily_brief_actionability()` computes read-time actionability; expired LIVE data becomes planning-only.
   - Markdown is rendered from a shallow read projection carrying the effective actionability, preventing stale LIVE presentation without mutating persisted state.

4. **Strict input/error contract**
   - Tool schema makes `account` required, defaults market to US, validates date shape and rejects negative/non-integer/bool/string revisions as `INPUT_ERROR`.
   - Revision without date is rejected by the conditional validator.
   - CLI validation raises `AgentToolError`; the existing top-level CLI envelope returns structured errors without traceback.
   - Repository date validation remains the source of truth for actual calendar validity.

5. **Default-off configuration**
   - Defaults and generated `configs/system.json` contain the same disabled daily-brief block.
   - Validator rejects non-object blocks, non-boolean `enabled`, and display limits outside integer range `1..20`.
   - No strategy threshold, actionability override, routing key, scheduler, database, or production toggle was added.

## Validation

```text
python3 -m pytest -q \
  tests/test_daily_decision_brief_cli.py \
  tests/test_daily_decision_brief_agent_tool.py \
  tests/test_validate_config_notifications.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py \
  tests/test_config_yaml.py
176 passed

python3 -m ruff check <S4 production and focused test files>
All checks passed

python3 -m compileall -q <S4 production files>
passed

python3 scripts/generate_dependency_graph.py --check
475 production modules, 0 cycles

python3 scripts/guardrails_check.py --check-runtime-config-tracking
OK

git diff --check
passed
```

Direct public-surface smoke evidence:

```text
./om daily-brief latest --account lx --json
available=false, reason=not_found in this artifact-free workspace

./om daily-brief day --account lx --date 2026-07-19 --revision 0 --json
available=false, reason=not_found

./om-agent run --tool daily_decision_brief_read --input-json '{"account":"lx"}'
read-only structured unavailable result with default market US

./om-agent run --tool daily_decision_brief_read --input-json '{"account":"lx","date":"2026-07-19","revision":1.5}'
INPUT_ERROR at revision: expected integer
```

The accepted plan referenced non-existent `tests/test_layered_config.py` and `tests/test_config_validator.py`; the repository-equivalent suites are `tests/test_config_yaml.py` plus `tests/test_validate_config_notifications.py`. `test_default_config_matches_legacy_system_json` proves `DEFAULT_CONFIG == configs/system.json`.

## Docs decision

- README documents the public config, timing, delta relationship, closed-market semantics, fail-closed multi-market behavior, CLI and Agent Tool.
- `docs/AGENT_WIKI.md` documents the canonical read model, advisory boundary and no-side-effect contract.
- Generated dependency documentation was refreshed because S4 adds the Agent Tool and CLI adapter modules.
- `VERSION` and `CHANGELOG.md` remain unchanged; release, production enablement, canary, deployment and remote upgrade remain outside this work unit.

## Residual risks / uncovered areas

- Historical runtimes without `daily_decision_brief.v1` artifacts return unavailable; assigned to a later migration work unit.
- Real provider/noise/length behavior remains assigned to a separately authorized production canary; S4 performs no send.
- Read-time expiry depends on persisted `valid_until_utc`; upstream timestamp correctness is covered by S1/S2 and remains observable through structured freshness fields.
- End-to-end cross-module scenarios remain covered by approved S5.
- No unclassified residual risk.

## Gate transition

- **Current gate**: S4 code review.
- **Next entry point**: run `deepreview --base b3c405c6`, adjudicate findings, fix/re-review, then create the accepted S4 slice commit only after pass.
