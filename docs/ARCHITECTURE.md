# Architecture

`options-monitor` is an operations-sensitive local application. The code is
organized around stable entry points, application use cases, deterministic
domain rules, external adapters, and local state repositories.

## Layers

| Layer | Path | Owns |
|---|---|---|
| Interfaces | `src/interfaces/` | Human CLI, Tool Gateway, and Inbound Assistant request/response adaptation |
| Application | `src/application/` | Use-case orchestration, config assembly, pipeline execution, notification flow |
| Domain | `domain/domain/` | Deterministic strategy, scheduler, notification, position, and schema decisions |
| Infrastructure | `src/infrastructure/` | OpenD/Futu, Feishu, WeChat ClawBot, exchange-rate and subprocess adapters |
| Storage | `domain/storage/` | Local path conventions and repository-style reads/writes for run state and reports |

Rules:

- `domain/domain` must not import `src` or `scripts`.
- `src/application` must not import `scripts`.
- `scripts/` is reserved for operational wrappers, release helpers, diagnostics,
  and one-off tools that delegate into `src` or `domain`.
- Public behavior should enter through `./om`, `./om-agent`, or
  documented `python -m src.application...` entry points.

## Python Export Contract And Static Gate

Every module that defines `__all__` owns a literal list containing only names
bound in that module. Ruff rule `F822` enforces this repository-wide through the
existing `ruff check .` Makefile and CI commands.

The public current-decision export contract belongs to
`src.application.ledger.current_decision_projection`. Its 44-name export set
aggregates the existing common, assigned-stock, lifecycle, combo, quality,
payload, oracle, migration, and runtime owners. The facade binds each name to
the canonical object from its owner module and retains its other explicit
bindings.

`src.application.ledger.current_decision_runtime` exports its 15 bound runtime
and compatibility names. Internal bootstrap code continues to import the two
runtime operations it uses directly. The diagnostics module exports its four
current tool names; retired tools are not kept as unbound compatibility names.

The import path is:

```text
public consumer -> current_decision_projection facade -> current_decision_* owners
ledger bootstrap -> explicit runtime imports
```

Contract tests freeze the exact export-name sets without making list order part
of the API. They also execute wildcard imports and verify every facade binding
against its canonical owner by object identity. Validate the contract with:

```bash
./.venv/bin/python -m pytest -q tests/test_ledger_module_facades.py tests/test_agent_plugin_contract.py
./.venv/bin/python -c 'exec("from src.application.agent_tools.diagnostics import *"); exec("from src.application.ledger.current_decision_runtime import *"); exec("from src.application.ledger.current_decision_projection import *")'
./.venv/bin/python -m ruff check .
```

## Main Entry Points

`./om` is the human CLI. It forwards to `src.interfaces.cli.main`.

`./om-agent` is the structured Tool Gateway CLI. It forwards to
`src.interfaces.agent.cli`, where tool execution is routed through:

```text
src.interfaces.agent.cli
-> src.application.tool_execution
-> src.application.agent_tool_registry
-> src.application.agent_tools.<domain>.TOOLS
```

`src.application.agent_tool_registry` is a collector/manifest adapter. New Tool
Gateway tools should live in `src/application/agent_tools/` domain modules and
export `TOOLS`. All Tool Gateway tools execute through `AgentTool.call(ctx,
payload)`. Root-level `src/application/agent_tool_*.py` modules are reserved
for compatibility re-exports and shared helpers such as config/contracts; they
must not own tool implementations. The legacy
`src.application.agent_tool_handlers` switchboard has been removed.

`./om copilot ...` is the local/eval entry for the read-only Copilot. Channel
Copilot is read-first and may request deterministic Control previews. Both are part
of the human CLI surface, not the Tool Gateway manifest:

```text
./om copilot run|eval
-> src.interfaces.cli.copilot_ops
-> src.application.copilot.local_harness
-> Service prepares an ExecutionContract
-> Host prepares a SceneManifest, runs the Agent/Engine loop, records events,
   and admits the final AppResult
```

The same Service, Host, Scene, Agent, and pure-read tool projection serve local
and channel questions. Channel adapters call the Copilot channel facade rather
than Host or Agent directly. There is one Scene, `om_chat`; no per-question
Scene selection or channel Scene allowlist exists. Host-backed channel runs
persist their session, run, and event lifecycle in the Copilot Host store and a
sanitized summary in the inbound audit record.

## Release Reproducibility And Copilot Exception Evidence

This contract has two goals: every Release must consume one reviewed Python
dependency set, and an unexpected Copilot read-tool exception must retain enough
sanitized evidence to identify the failed stage and cause. Success means all
formal Release, install, and upgrade paths reach the same lock; an injected tool
exception retains its type, bounded redacted reason, run identity, and tool-call
identity in the durable event stream while ordinary CLI, model, channel, progress,
and final-answer surfaces keep the current generic failure.

This contract does not pin the Python interpreter patch version, add package
artifact hashes, change the locked Pi runtime, migrate package managers, publish
or deploy a release, persist tracebacks, instrument other exception classes, or
add a logging or event subsystem. Cache-tamper detection, artifact integrity, and
future Python-minor qualification remain separate work.

### Current Facts And Constraints

Python dependency intent is split across `requirements/runtime.txt`,
`requirements/server.txt`, and `requirements/dev.txt`. The current constraint
files pin only selected direct packages: the Futu SDK and multiple transitive
packages can resolve differently at different times. Guardrails installs the dev
requirements through those partial constraints; the reusable Release workflow
first runs the Agent plugin installer and then installs an unconstrained `pytest`.
The release-tag installer and both pip and uv service-upgrade paths consume the
same partial constraint family.

Service upgrade already hashes the requirement and constraint include graph when
it chooses a reusable environment. That cache prevents redundant installation for
identical declarations, but it cannot make a ranged dependency deterministic.
The existing include traversal follows nested `-r` and `-c` references, so it can
incorporate a release lock without another cache or resolver abstraction.

`execute_tool` currently catches an unexpected tool implementation exception and
returns `INTERNAL_ERROR` with the exception type and text in its message. Copilot
Host therefore rarely reaches its existing `except Exception` branch around
`call_read_tool`; compacting the returned error discards the cause only after raw
text has crossed the shared tool response boundary. The failed `tool_result` event
retains the generic observation, sanitized inputs, and `tool_call_id`, while
`AppEvent` supplies `run_id`. A cancellation detected after the call currently
returns before this failed event is recorded.

### Release Dependency Contract

`constraints/release.txt` is the sole owner of reviewed Python package versions.
It contains the complete exact runtime, server, and dev/test dependency union,
including transitive packages. Marker-qualified alternatives are allowed only
where universal resolution needs them. The requirement files remain the
human-readable dependency-intent owners. `packaging` becomes a direct runtime
requirement because the release checker runs under the runtime/server environment
before service activation and uses its standard PEP 508 name, requirement,
version, specifier, and marker implementations.

The existing top-level requirement file and the top-level, runtime, server, and
dev constraint files remain as transparent compatibility entry points.
`requirements.txt` contains comments and one direct `-r requirements/runtime.txt`;
`constraints.txt` contains comments and one direct `-c constraints/release.txt`;
each file under `constraints/` contains comments and one direct `-c release.txt`.
They contain no package entry, resolver option, or second include. Existing
commands therefore keep their public shape while Guardrails, release-tag
installation, and service upgrade select versions from one source:

```text
requirements/dev.txt + requirements/server.txt
-> explicit universal lock refresh
-> constraints/release.txt
-> compatibility constraint includes
-> Guardrails / reusable Release / install.sh / pip-or-uv service upgrade
```

A maintainer refreshes the lock explicitly at the supported generation floor:

```bash
uv pip compile --universal --python-version 3.12 \
  --output-file constraints/release.txt \
  requirements/dev.txt requirements/server.txt
```

`uv` remains a generation tool rather than a runtime dependency. Refreshing the
lock is the only normal path for upgrading `futu-api` or another Python package;
installer comments and operator documentation no longer describe floating SDK
upgrades as expected behavior.

The existing `scripts/release_check.py` owns dependency-lock semantics. Its normal
release check always performs structural validation. Its exact
`--dependency-lock-only` mode performs that validation plus clean installed-closure
and `pip check` validation, then exits without requiring VERSION or CHANGELOG work.
Both modes use the declared `packaging` dependency rather than a local requirement
or marker parser. Every non-comment, nonblank release-lock line must be one
parseable PEP 508 package requirement with exactly one `==` version and an
optional valid marker. Includes, resolver or index options, editable or direct
URL/path requirements, hashes, and every other specifier are rejected on all
marker branches. Marker applicability is evaluated only for installed-closure
comparison. The shared checks also reject:

- overlapping active entries for the same normalized name;
- a direct requirement missing from the lock or pinned outside its declared
  specifier;
- a compatibility requirement or constraint containing anything other than
  comments and its one direct relative include, or an include with a missing
  target or cycle; pip and uv must both accept every compatibility path;
- a package missing from or added to the clean installed closure, except the
  explicit bootstrap-tool allowlist for `pip`, `setuptools`, and `wheel`;
- an installed version different from the applicable locked version, or any
  `pip check` failure.

Here, stale means a structural mismatch between dependency intent, the applicable
lock, and the clean installed closure. It never means that a package index offers
a newer version.

The reusable Release workflow adds an unconditional dependency job with
`ubuntu-latest` and `macos-latest`, both on Python 3.12. Each matrix leg creates a
fresh virtual environment, installs `requirements/dev.txt` and
`requirements/server.txt` constrained by `constraints/release.txt`, and runs the
dependency-lock-only check. The publishing job depends on both legs, including
when `run_regression_gates=false`; its own install also uses the same lock and the
unconstrained `pip install pytest` step is removed. Changes to the top-level
requirement/constraint entry points or files under `requirements/**` and
`constraints/**` select the existing `service_release` release-test-plan gate.

Python 3.12 on Linux and macOS is the qualified Release dependency matrix. The
public runtime floor remains Python 3.12, so a later compatible interpreter may
consume the same lock and fail closed on incompatibility, but this unit makes no
claim that future Python minors were Release-qualified.

A missing, structurally stale, incompatible, or incomplete lock fails before
publication. Installers and both pip and uv service-upgrade modes reach the lock
through the compatibility constraints; neither may resolve without it. A failed
service-upgrade install retains the current pre-activation behavior and cannot
switch the active release symlink. Because service-upgrade dependency hashing
already traverses nested requirement and constraint includes, changing the lock
changes the reusable-environment identity without a second cache mechanism.

### Copilot Tool-Exception Contract

`execute_tool` gains the keyword-only `raise_unexpected` behavior that re-raises
only its unexpected `Exception` branch. Its default is `False` for the Tool
Gateway and every existing caller. `call_read_tool` passes
`raise_unexpected=True` for Copilot reads, including the time-bound
option-performance branch, so Host catches the original exception. `AgentToolError`
remains a declared tool failure and `SystemExit` remains `CONFIG_ERROR`.

Host builds the same generic `TOOL_EXCEPTION` response and model observation, then
adds these facts only to a copy used for the existing failed `tool_result` event:

- `failure_stage=tool_execution`;
- `exception_type`, normalized to one control-safe line and limited to 120 Unicode
  code points;
- `exception_reason`, passed through the existing redactor, normalized to one
  control-safe line, and limited to 240 Unicode code points.

The formatter uses a primitive `exc.args` value directly only when it is the sole
argument; multi-argument exceptions use a guarded `str(exc)` so common system
errors keep their descriptive reason. After redaction, every non-printable
character is replaced before the value is truncated. The complete formatting and
redaction path catches `BaseException`; any failure records the type and a fixed
unavailable-reason marker instead of masking `TOOL_EXCEPTION`. The event payload
is derived from a copy and never mutates the generic `failed_observation` returned
to the Agent.

The event's existing `run_id`, `tool_call_id`, and `tool_name` are the correlation
contract. Ordinary Copilot output stays generic. Explicit local operator
diagnostics, `--include-events` and `./om copilot events`, may display the bounded
redacted fields. Resume recovery continues to ignore failed tool events, so those
fields cannot become model evidence on a later run. The existing redactor removes
recognized secret forms and absolute local path prefixes; a basename may remain.

The failure flow is:

```text
call_read_tool raises
-> Host captures type and bounded redacted reason
-> generic TOOL_EXCEPTION response and model observation
-> event-only copy adds internal diagnostic fields
-> durable Host event store persists the failed tool_result
-> post-call cancellation may return CANCELLED
```

The failed event is committed before the post-call cancellation check. A cancel
request that wins after the tool call therefore changes the returned observation
to `CANCELLED` without erasing the diagnostic event for the completed failure.
Observation-normalization failures remain outside this slice.

### Implementation And Validation

The work has two independently verifiable behavior slices:

1. Add and validate the universal Release lock. Route the existing constraint
   entry points, reusable Release installs, and release-test-plan selection through
   it; declare the checker's `packaging` runtime dependency and update the Futu
   upgrade wording. Prove exact/direct compatibility, marker handling, stale extra
   and missing packages, pip and uv nested constraints, service-upgrade hash
   invalidation, both Python 3.12 matrix legs, and `pip check`. A runtime/server-only
   environment must run the normal release check through metadata validation
   without relying on dev dependencies.
2. Let Copilot reads re-raise only unexpected tool implementation exceptions to
   Host. Persist the event-only diagnostic copy before cancellation while keeping
   the Tool Gateway and model observation unchanged. Through the public
   `run_contract` facade, inject a real implementation exception and prove retry,
   cancellation, durable `CopilotHostStore` readback, default generic output,
   opt-in operator visibility, failed-event recovery exclusion, redaction and
   bounds, multi-argument errors, non-printable Unicode, and a pathological
   `__str__` fallback.

The first slice stays with `scripts/release_check.py`, the existing Release
workflow, compatibility constraint files, `release_test_plan`, installers, and
their current test owners. The second stays with `tool_execution`, Copilot tools
and Host, and `tests/test_copilot_phase1.py`; it introduces no public tool schema or
event type. Focused checks are followed by Ruff, the complete pytest suite,
dependency-graph checking, and `git diff --check`. Regenerate dependency-graph
artifacts only if the import graph changes.

Operational wording belongs in `docs/RELEASE_PROCESS.md` and
`docs/DEPLOY_LINUX_MAC.md`; Copilot trace wording belongs in
`docs/OM_COPILOT_V2_DESIGN.md`. These documents point to the same owners rather
than define a second lock or diagnostic contract.

One universal lock remains the minimum design. Platform-specific locks are added
only after a demonstrated resolver conflict. Manual mutation of a cached shared
virtual environment can still evade the current marker/executable cache check;
track that separate integrity hardening at `src/application/service_upgrade.py`.
Artifact hashes, wheel-only installation, build-isolation integrity, and future
Python-minor matrices remain Release supply-chain follow-up work. A novel unlabeled
secret can evade the current denylist, but the new cause is visible only through
explicit local operator event output and remains bounded. No unresolved product or
permission choice remains for these slices.

## Research, Shadow Replay, And Strategy Lab

Research and Shadow Replay are an independent offline evidence/replay module,
not part of the Inbound Assistant core and not a remote chat surface. Strategy
Lab is a separate strategy-experiment product surface. Its current executable
surface contains the fixed HK / `lx` Recipe, preview and confirmation, status,
explicit bounded 20-day research execution, Research Receipt reading, and targeted
history-K readiness. Hidden validation remains outside Phase 2.

```text
./om research ...
-> src.interfaces.cli.research
-> src.application.research
-> src.application.shadow_replay

./om strategy-lab readiness refresh-history-k ...
-> src.interfaces.cli.strategy_lab_ops
-> src.application.strategy_lab.service/readiness

./om strategy-lab recipes|preview|confirm-research|status|receipt ...
./om strategy-lab research execute ...
-> src.interfaces.cli.strategy_lab_ops
-> src.application.strategy_lab.service
-> src.application.strategy_lab.recipe/evidence/comparison/receipts
```

`src.application.research` owns redacted evidence collection, deterministic
checks, handoff rendering, remote archive mirroring, and local research bundle
writes. `src.application.shadow_replay` owns offline dataset construction,
mark/outcome lifecycle, status, analyze, and candidate-impact report logic.
`src.application.strategy_lab` owns the root experiment contract, Recipe preview,
bounded research evidence, comparison, Research Receipt, and targeted readiness; the
same-path `ExperimentStore` is the three-table state owner.
Shadow Replay maintenance stays on its own commands. Strategy Lab does not own
a recorder service or timer in Phase 2. Provider evidence is admitted only after the
non-blocking query lock and Tick/protection checks; one invocation consumes at most
one provider logical unit.

This side lane may read runtime artifacts, candidate/reject/trace evidence,
required-data snapshots, and archived run outputs. It must not mutate runtime
config, notification behavior, Feishu/ledger/trade state, broker-facing data,
or live tick scheduling. Research has no universal write flag. `collect` writes
only with `--write-outputs --confirm`; maintenance and archive pull/build
commonly use `--write`; `shadow-replay build`,
`shadow-replay candidate-impact-report`, `archive verify`, and commands given
explicit output paths materialize local artifacts as part of their own
contract. `archive prune-remote` is a separate destructive boundary and
deletes remote runtime artifacts only with `--confirm` after local
verification. Inspect the selected `./om research <subcommand> --help` before
execution.

Product boundary:

```text
Research = evidence infrastructure
Shadow Replay = counterfactual replay engine
Strategy Lab = strategy evolution product surface
```

### Research Side-Lane Startup Boundary

Research, Shadow Replay, Strategy Lab, and Formal Corpus execution must not be a
startup prerequisite for an ordinary CLI command or for importing the production
Tick entry points. They may load only after the selected command or Tick sidecar
actually needs them. This keeps a broken optional side lane from preventing an
unrelated operator command, Tick process, or cron wrapper from starting.

After an ordinary CLI import and parser build, the only permitted side-lane module
names are the `src.application.research` package node and
`src.application.research.redaction`. The latter is a pure shared sanitizer used by
Copilot and support-bundle code. Every other `src.application.research.*` module and
all `src.application.shadow_replay.*` and `src.application.strategy_lab.*` modules
are forbidden until the selected command needs them. Tick entry-point imports must
load none of those modules. Moving the sanitizer to another package is not required
without a separate ownership need.

The boundary is enforced at five seams:

- `src.application.research.__init__` binds two lazy forwarding functions without
  importing the Research facade or service.
- `src.interfaces.cli.research` binds the package-level Research collect forwarder.
- `src.interfaces.cli.strategy_lab_parser` owns argparse registration, while
  `strategy_lab_ops` retains Strategy Lab execution and Futu access.
- `service_deploy` and `service_drift` import Strategy Lab contract values only
  inside the operations that consume them.
- `tick_notification_flow` and `tick_cron` import Formal Corpus only inside their
  existing degraded, non-blocking call boundaries.

`src.application.recommendation_point` has no Research, Shadow Replay, or Strategy
Lab import and must remain independent. The generated dependency graph records
imports at any nesting level, so it documents ownership but cannot enforce this
startup boundary by itself.

The Research package keeps its existing `research_tool` and
`run_research_collect` package-level exports through two module-bound,
signature-preserving forwarding functions. Each function imports its canonical
owner only when called, and both names remain in the literal `__all__`, so the
package stays lazy without violating the repository-wide F822 export contract. The
Research CLI binds the `run_research_collect` forwarder under the same name so
existing injection and monkeypatch seams remain valid. Call behavior and signatures
are compatibility requirements; wrapper-to-owner function-object identity is not.

The CLI keeps its existing commands, arguments, help, responses, and module exports.
Strategy Lab parser construction moves to a lightweight interface module;
`strategy_lab_ops` re-exports the parser function and retains the execution handler
and its module-level dependency names. `src.interfaces.cli.main` imports only the
lightweight parser during registration and loads the handler after `strategy-lab`
is selected. The two ordinary service owners import Strategy Lab contract values
inside the operations that use them.

Tick keeps both Formal Corpus side effects and their present failure semantics:

```text
import multi_account_tick / tick_notification_flow / tick_cron
-> no Research, Shadow Replay, Strategy Lab, or Formal Corpus execution import

scheduled notification reaches recommendation-point archiving
-> load Formal Corpus inside the existing per-account archive try block
-> archive success, or audit formal_point_archive_failed and continue

tick-cron has OM_RUNTIME_ROOT and an unscoped symbol set
-> call a lightweight default function that loads Formal Corpus inside the existing try block
-> seal expectations, or emit FORMAL_EXPECTATION_DEGRADED* and launch Tick
```

The cron callable contract remains unchanged: omitting
`seal_formal_expectations_fn` selects the lazy default, an injected callable is used
as supplied, and explicit `None` disables sealing. A missing or broken Formal Corpus
module therefore follows the same degraded path as any other sealing failure.

No plugin registry, two-pass CLI dispatcher, new configuration, package-wide move,
or public command change is part of this boundary. Formal Corpus capture is not
removed or made optional beyond its existing best-effort contract.

Success requires all of the following:

- a fresh interpreter can import `src.interfaces.cli.main`, build global help, and
  parse a non-research command without loading outside the two permitted Research
  names;
- a fresh interpreter can import `multi_account_tick`, `tick_notification_flow`,
  and `tick_cron` without loading any side-lane module;
- Research and Strategy Lab commands still load their owners when selected and
  retain their existing behavior and injection seams;
- scheduled recommendation-point archiving and cron expectation sealing retain
  their success and degraded paths, including per-account archive failures and
  explicit cron sealing disablement;
- a startup-specific regression guard fails when a forbidden transitive dependency
  returns, while the generated dependency graph remains current and cycle-free.

Validation is split into two independently verifiable behavior slices:

1. Defer Formal Corpus imports to the existing notification archive and cron
   failure boundaries. Verify the three Tick imports, per-account degraded audit,
   default sealing failure, and explicit `None` behavior in isolation.
2. Make ordinary CLI startup side-lane-free by replacing the Research package's
   eager re-exports with bound lazy forwarders, preserving its package exports and
   Research monkeypatch seam, deferring the two service-contract imports, and
   separating Strategy Lab parser registration from handler loading. Verify global
   help, one non-research command, Research and Strategy Lab invocation, package
   forwarder loading, F822, and the permitted module ceiling.

The startup guard runs each entry point in a fresh subprocess and inspects
`sys.modules`; a direct-import AST check is insufficient because it misses package
initializers and transitive imports. A package-export regression also proves that
importing `src.application.research` does not load the facade or service, that each
bound forwarder loads and calls its owner only on invocation, and that the literal
`__all__` passes F822. Existing Research, Strategy Lab, Tick cron,
notification-flow, and recommendation-point tests preserve the public and runtime
contracts. The CLI guard treats the permitted names as a ceiling, so a later
dependency reduction remains valid. Focused tests are followed by the complete
pytest suite, Ruff, dependency graph `--check`, and `git diff --check`. If the parser
split changes the generated graph, regenerate both dependency-graph files before
rerunning `--check`.

The main compatibility risk is changing import-time names that current tests patch.
Bound package forwarders, the same-name Research CLI binding, the Strategy Lab
parser re-export, and the unchanged Strategy Lab execution module preserve those
names. The package wrappers preserve the existing call signatures, but not identity
with their canonical owner functions; repository-external identity comparison is
not observable locally and is not treated as a supported contract. A deferred import
moves an import error from process startup to feature invocation; that is intentional
for CLI commands. Tick imports and calls remain inside their current degraded
boundaries so a failure cannot stop later accounts or the Tick subprocess.

## Inbound Flow

Remote messages intentionally separate channel transport from application
execution. The current CLI namespace remains `./om assistant ...`:

```text
Feishu / future channels
-> src.application.channels.ChannelService
-> src.application.inbound.feishu(_ws)
-> sender allowlist / idempotency
-> explicit command or pending-operation reply?
   -> deterministic Control
   otherwise -> Copilot Service -> Host -> om_chat Agent
-> audit / operation persistence
-> channel reply
```

`src.application.channels` owns channel capability registration and dispatch, so
Inbound control and outbound notifications are capabilities of the
same channel model. `src.application.inbound` should stay thin: extract channel
payloads, enforce channel-specific receive/reply mechanics, and build the
transport request.
`AssistantRequest`, the inbound audit row, and the pending-operation store form
the deterministic Control boundary. Protocol command parsing, bound permission
responses, sender allowlist checks, previews, confirmations, applies, and
operation receipts are owned by `src.application.assistant`.

Every message that is not explicit Control protocol enters the read-first
Copilot path. Copilot Service prepares the execution contract, Host owns
session/run/event governance and the `om_chat` Scene, and Agent/Engine own the
generic model/tool loop. The model can use canonical pure-read tools and, on
channel runs, one generic Control-preview meta-tool. It cannot receive or call
confirmation, cancellation, apply, notification, config-write, ledger/trade,
broker-write, service-control, or upgrade handlers directly. The deterministic
Inbound service validates a preview request against the capability catalog
before Control creates the pending operation.

Control receipts are stored as conversation context for follow-ups. Each turn
also receives a fresh current-conversation pending snapshot from the operation
store, so stale or compacted chat history cannot become operation authority.

There is no business intent router, multi-Scene catalog, planner fallback,
evidence pipeline, or synthetic Assistant Agent session. Missing model
configuration or unavailable evidence produces an explicit Copilot failure; it
does not fall back to ordinary chat or deterministic business templates.

Model selection is a startup/configuration concern. `config.yaml` may define multiple
`assistant.models` profiles and an `assistant.active_model`, but
`config build-assistant` resolves that into one flat `assistant.llm` in
`config.assistant.json`. Control and tool execution do not choose models per
message.

Inbound uses one explicit command/permission contract. Slash commands never call
the model. Bound confirm/cancel phrases such as `确认升级` enter the permission
path only when they match an existing pending operation in the same
sender/channel/conversation scope. All other text enters Copilot. Deterministic
code must not recover natural-language business intent through unrelated command
fallback.
Any preview-write result can only enter an existing pending-operation path.
Confirm, cancel, apply, notifications, direct config writes, ledger/trade
writes, and service operations remain outside model authority.

Control emits one `ControlExecution`; Copilot emits one Host-owned `AppResult`.
Neither path recreates perception/reasoning/action/observation stages.

## Runtime Tick Flow

The live scan/notification flow has one public chain:

```text
./om run tick --config <runtime-config.json>
-> src.interfaces.cli.main
-> src.application.multi_account_tick.run_tick
-> src.application.multi_account_tick.main
```

Inside the tick use case:

```text
multi_account_tick
-> config contract + run/idempotency context
-> OpenD watchdog / project guard / trading-day guard
-> scheduler decision
-> account execution
-> notification preparation and delivery
-> final run state and audit writes
```

`multi_account_tick` should stay as the orchestration spine. Narrow helper
modules own the heavier subflows:

- `src.application.tick_run_context`: tick idempotency bucket/key construction
  and completion record writes.
- `src.application.tick_guard_flow`: project guard, load shedding, market-scoped
  config filtering, OpenD phone-verify gate, and watchdog admission.
- `src.application.tick_run_workspace`: run directory, required-data workspace,
  and shared state pointer.
- `src.application.tick_scheduler_context`: trading-day guard, scheduler state
  path, scheduler decision, and scheduler snapshot writes.
- `src.application.tick_account_execution`: account defaults, account worker
  limits, ordered concurrent account execution, per-account metrics, and
  scan-state marking.
- `src.application.tick_notification_flow`: notification preparation, quiet-hour
  decision, delivery, metrics, finalization, and notification idempotency
  completion.

Account execution is per account:

```text
account_run.run_one_account
-> required_data prefetch
-> event prefetch / event_snapshot.json
-> pipeline_runtime / pipeline_watchlist / pipeline_symbol
-> optional close advice
-> account metrics and account notification text
```

Expired-position maintenance is a separate
`option-positions auto-close-expired` service/timer workflow. It is not a stage
of the live tick account flow.

## Scan And Candidate Flow

Candidate scanning is intentionally split:

```text
src.application.pipeline_runtime
-> src.application.pipeline_watchlist
-> src.application.pipeline_symbol
-> src.application.scan_sell_put / scan_sell_call
-> src.application.candidate_scanning
-> domain.domain.engine.candidate_engine
```

The canonical candidate decisions live in `domain.domain.engine.candidate_engine`:

- input normalization
- hard constraints
- return floor
- risk filter
- ranking

Application scanners adapt files, pandas rows, context, and report output around
that domain engine. Avoid adding parallel ranking implementations in adapters.

Event-risk data is prepared at run scope, not inside candidate scanning:

```text
src.application.events.prefetch
-> src.application.events.store
-> output_runs/<run_id>/state/event_snapshot.json
-> src.application.events.annotator
-> domain.domain.short_vol_assessment
```

The candidate scan path must only read the run snapshot. It must not call the
external event source directly; source failures remain explicit as `error` or
`stale`, and short-vol fail-closed policy is enforced in the domain assessment.

Strategy terminology is centralized in `domain.domain.strategy_vocab`.
Application and interface code should use it to translate between stable
internal ids such as `sell_call` and user-facing names such as `Covered Call (CC)`.
Do not scatter display names, aliases, or section labels through notification,
report, or Tool Gateway manifest code.

## Option Positions Flow

The durable position model is:

```text
trade_events -> projection -> position_lots
```

Domain projection logic lives in `domain.domain.ledger.projection`.
Stored trade events are encoded at `src.application.ledger.event_codec`; runtime
writes use the canonical ledger event schema.
Lot record field construction and open/close patch helpers live under
`domain.domain.ledger.position_fields`.
Application services own SQLite loading, local bootstrap, repair, CLI facades,
and reports. Feishu `option_positions` is not a bootstrap input, sync target,
strategy input, or steady-state source of truth.
`src.application.ledger.api` is the public application boundary for all
non-ledger runtime code. `src.application.positions`, `src.application.trades`,
agent tools, CLI modules, web UI modules, pipeline context, and cash-headroom
queries must not import ledger internals such as service, preflight, resolver,
writer, publisher, repository, projection-verify, or read-model modules directly.
The API file is intentionally a thin facade: command/write operations live in
`src.application.ledger.commands`, query/read operations live in
`src.application.ledger.queries`, and typed read views such as
`PositionLotSnapshot` and `RiskPositionView` live in
`src.application.ledger.views`.
Runtime callers should call semantic ledger actions such as manual position
recording, broker trade recording, expired-close planning/recording, projection
refresh, lot selection, position snapshot reads, event review/repair, and
projection verification, rather than composing lower-level
`persist_*`, `preflight_*`, `require_*`, or `load_*` functions themselves.
Close writes share `CloseTargetResolution` as the ledger-owned target contract:
manual close resolves a unique strict lot, broker close resolves a strict exact
FIFO target set, and auto-close validates the explicit current lot before
writing. Aggregated `position_key` values are read-only and must not become
write targets.

Position-facing workflows live under `src.application.positions`; they operate
on projected lots and expose manual lot operations, expiry maintenance, and
maintenance receipts, plus risk context, inspection, and reporting.
Trade-facing workflows live under `src.application.trades`; they
operate on normalized trade deals, OpenD deal intake, idempotency state,
receipts, and event review/replay flows. Both route writes through
`src.application.ledger` instead of owning projection or matching rules locally.

For a first-seen Futu stock or ETF deal, trade intake may emit a post-settlement
account refresh hint to the separately installed portfolio-management service.
The hint contains no position delta: PM owns the full Futu holdings reread,
persistence, scheduling, concurrency, and retry policy. OM only records whether
the hint request was accepted or failed; acceptance is not synchronization
evidence. The root `portfolio_management.enabled` switch gates this hint and all
other production PM callers.

## Close Advice Flow

Close advice keeps deterministic policy in `domain.domain.close_advice`.
`src.application.close_advice_runner` assembles option-position inputs, required
data quotes, quality flags, Futu fee estimates, rows, and output files around
that domain logic.
Domain policy owns one fixed `strict_profit_capture.v1` rule for short puts and
short calls. It emits only `close`, `hold`, or `not_evaluable`; it does not pair
combo-yield legs, compare opening candidates, or produce roll,
replacement, reallocation, short-vol, or long-option exit actions. The runner
preserves those strict decisions, including fail-closed `not_evaluable` rows,
and publishes CSV/text reports plus their integrity manifest. The Close Advice
state and evidence contract is documented in
`docs/CLOSE_ADVICE_CONTRACT.md`.

## Config And Runtime State

The canonical human authoring source is `config.yaml`. Code-owned defaults live
in `src.application.config_defaults.DEFAULT_CONFIG`, and YAML overrides are
resolved by `src.application.config_yaml`.

Market runtime configs are generated snapshots:

- `config.us.json`
- `config.hk.json`

Runtime execution consumes those JSON snapshots rather than editing
`config.yaml` directly. First-run setup uses `src.application.config_yaml_init`
to create a starter YAML file and build market snapshots. Legacy JSON authoring
and its migration command are retired. Production upgrade requires a usable YAML
authoring source and fails closed before switching releases when it is absent.

Shared config section helpers such as symbol/watchlist and templates live in
`src.application.config_sections`; both loading and validation depend on that
neutral module to avoid loader/validator cycles.

Runtime state is local and intentionally explicit:

- shared state: `output_shared/state/`
- per-account output: `output_accounts/<account>/`
- run snapshots: `output_runs/<run_id>/`
- cache files: `cache/`

## Change Guidance

When adding code:

- Put pure business decisions in `domain/domain`.
- Put use-case orchestration in `src/application`.
- Put external system adapters in `src/infrastructure`.
- Put CLI, Tool Gateway, and Inbound Assistant argument/response adaptation in `src/interfaces`.
- Prefer a small facade-preserving move over changing public command behavior.
- Add or update boundary tests when moving ownership between layers.
