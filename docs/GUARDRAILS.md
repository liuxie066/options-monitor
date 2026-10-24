# Guardrails

## A) Local Commit Gate

Enable hooks once:

```bash
cd <repo-root>
bash scripts/setup_git_hooks.sh
```

Enabled checks:

- Reject commits if repo path/name matches `options-monitor-prod`
- Scan staged index content for high-confidence credentials, private data fingerprints, known personal email addresses, personal paths, and tracked runtime configs; findings are redacted
- Reject a repository-effective Git author email already classified as private; use a GitHub `noreply` identity for public commits
- Reject a missing living-doc authority/indexed target, broken local Markdown document links, and deterministic repository paths whose owner is absent from the staged Git index; validate explicit `module.py::symbol` references against staged source definitions/imports without importing business modules. Explicit historical, removed, retired, proposed, and example paths are exempt from repository path/symbol checks
- Require the first commit-message line to match `<type>(<scope>): <subject>`
- No trailer or co-author line is required by the hooks

## B) Remote Merge Gate (CI)

Workflow: `.github/workflows/guardrails.yml`

- Docs check: forbid treating `config.json` / `config.scheduled` / `config.market_*` as OM runtime entry, require the living-doc authority graph and local Markdown document targets to remain present, and reject missing repository paths or explicit Python module symbols in indexed living docs
- Runtime config tracking check: forbid committing root runtime configs such as `config.us.json` / `config.hk.json`; commit only templates under `configs/examples/`
- Sensitive artifact check: reject high-confidence provider credentials, private keys, credentialed URLs, known private runtime/financial/email fingerprints, and literal personal home or mounted-volume paths without printing the blocked value
- Public surface check: compare the declared public names of `src/`, `domain/` and `scripts/` against the merge base of the pull request and reject any name that disappeared without a recorded retirement
- Lint: run `python -m ruff check .`
- Standalone smoke: run `tests/run_smoke.py`
- Launcher spec smoke: render `./om-agent spec` through the public wrapper
- Full regression: run automatic pytest discovery for pull requests and VERSION-changing pushes; ordinary pushes to `main` reuse the required pull-request result. The complete collection is partitioned by test file across eight CI runners, each using four local worker processes. A worker crash fails the gate instead of restarting tests silently; the required `guardrails` check verifies that the shard union executed every collected test exactly once.

Trigger: `push` and `pull_request` to `main`. The merge policy requires pull requests and a passing, up-to-date `guardrails` result; the effective GitHub ruleset must be checked when verifying remote protection.

### Full regression

The workflow uses pinned `pytest-xdist` and `pytest -n 4 --dist loadfile --max-worker-restart=0 --durations=25 -o tmp_path_retention_policy=failed`. Each of eight runners automatically collects the complete suite, then the opt-in plugin `.github/ci/pytest_shard.py` assigns whole files by a stable SHA-256 partition. New files join automatically; no hand-maintained test allowlist or historical duration file controls coverage. Do not add test selectors, exclusions, or a serial carve-out to conceal shared-resource failures. Isolate conflicting resources with temporary paths, dynamic ports, worker-unique paths, or cross-process locks.

Every runner records the full collected IDs, assigned IDs, completed test protocols, revision, and failure state. `.github/ci/regression_manifest.py` requires all eight receipts for the same tested revision, identical full collections, the expected file assignment, and an exact union with no missing or duplicate execution. Ordinary pytest skips still complete a test protocol and retain their normal reporting. Failed tests, incomplete collection, worker crashes, cancellation, or missing receipts cannot satisfy the required gate. Matrix fail-fast is disabled so another shard's failure does not erase diagnostics.

Lightweight checks run alongside regression runners. PR regression starts without a VERSION-resolution job; that job runs only on main pushes. The required check name stays `guardrails` and succeeds only after the existing lint, documentation, sensitive-artifact, public-surface and smoke checks plus the verified full regression have passed. VERSION-changing pushes use the same aggregate result before release; ordinary main pushes retain their existing PR-result reuse policy. Pip download caching is keyed by the requirements and constraints, while every runner still installs dependencies into a fresh virtual environment.

This trades more concurrent runner capacity and repeated setup/collection for lower wall-clock latency. Queue time and runner variability can offset the benefit; measure the complete delivery interval separately from job duration. Local `scripts/release_preflight.sh --full` continues to run the unpartitioned suite on four workers.

Four workers overlap independent test files, `loadfile` keeps each test module in one process, and zero worker restarts makes crashes visible. Both CI and `scripts/release_preflight.sh --full` use pytest's built-in `tmp_path_retention_policy=failed` to remove passed tests' temporary directories as they finish, limiting disk use while retaining failed test-call directories and diagnostic logs. Scope OS-level mocks to the tested calls so fixture cleanup sees the real filesystem APIs. The serial command remains available for diagnosis. Collected counts and hosted-runner timings vary with the revision and environment; past optimization measurements are not fixed merge thresholds.

The dependency declaration and exact version pin live in `requirements/dev.txt` and `constraints/dev.txt`; the workflow owns the required invocation.

The retired-column registry tests share one module-scoped scan of the unchanged source tree; planted-input scans and the CLI check stay independent. Performance-query HTTP tests use a shorter server shutdown polling interval while preserving real requests, request timeouts, and thread cleanup assertions.

### Declared public surface

Every module under `src/`, `domain/` and `scripts/` has a public surface, and the pull-request gate compares that surface — for the modules a change touches — against the merge base of the pull request (not against a checked-in expectation, which a deletion could update in the same commit). A declared name that disappears fails the gate unless `docs/public_surface_retirements.json` records it.

- A module's surface is its `__all__` when that is statically resolvable — literal lists and tuples, starred references to module-level literals, `dict.keys()` and `sorted(...)`. A module without `__all__` declares the public names it defines itself, so a re-export must be listed in `__all__` to be protected. Any other way of building `__all__` — an augmented assignment, `.append(...)`, an assignment inside a guard, or more than one assignment — is a failure rather than a module read as declaring nothing: declare it as a single top-level literal list.
- A name counts as present when the module binds it anywhere at module scope, imports included, because `from x import utc_now as utc_now_iso` keeps the name reachable even though the module no longer defines it. On the declaring side a `def`/`class` behind an `if`/`try` counts too, since a version guard still ships that definition; a plain assignment behind a guard does not, because that is how a module spells a platform fallback (`except ImportError: fcntl = None`) or a temporary used by an error message rather than a name it promises. When a module does declare `__all__`, that list is the surface, so narrowing it is a removal.
- The gate reads regular files only. A module that is a symbolic link at either revision is refused rather than compared, because a link's blob is the path it points at, which can pass for valid Python.
- Content that cannot be read is a failure, never an empty surface: a base blob missing from the local object store (a partial clone, a pruned one), or a ledger that is in the base tree but cannot be read, is reported instead of skipped. The diff status is held to the same rule — `A` adds surface and is skipped, `D`/`M`/`T` are compared, and anything else (an unmerged `U`, say) fails rather than passing as nothing to check.
- The ledger is append-only, and the gate rejects a change that drops a recorded entry. An entry is one object with exactly `module`, `name` and `reason`. The gate enforces that `reason` is a sentence rather than a placeholder (at least 20 characters); whether it is a *true* sentence is the reviewer's call, which is why the entry has to be written into this checked-in file in the same change as the deletion, where it shows up next to it in the diff, rather than in an out-of-band tracker a reviewer would have to know to open. `name` `*` retires a whole module and is valid only while that module is really gone — so a deleted module or a module split into a package costs one entry, not one per name. The one exception to append-only is that `*` entry: restoring the module makes it false, so it may be dropped then — and only dropped, since rewriting it into another entry for the same module would exempt that name while it is still exported.
- Leaving a removal unrecorded fails, and so does deleting a recorded entry. Adding a name, or changing a symbol where it already lives, does not: the gate compares which names a module exposes, never how they are implemented. Moving a name to another module does fail for the module it left, unless that module still exposes it — an alias import counts.
- What the gate cannot check, and a reviewer must: whether a `reason` is true, whether an entry registered in an earlier change was honest when a later one matched it against a real deletion (that later diff shows no ledger change at all), and a name that only ever lived inside a guard at the base revision, which is not on the declaring side and so is not reported when it goes.

Run it locally against any revision:

```sh
./.venv/bin/python scripts/guardrails_check.py --check-public-surface --public-surface-base <rev>
```

The base revision is never defaulted: `--check-public-surface` without `--public-surface-base` fails instead of comparing nothing. The workflow runs this check on `pull_request` events and nowhere else — a push to `main` reuses the required pull-request result, which is the same policy the full regression already follows. The workflow passes a merge base, so a branch that trails `main` is not charged for deletions made on `main` itself. Resolving that merge base needs the history behind it, so the workflow checks out the full history instead of a single commit; when the base revision cannot be resolved the step fails with an error annotation rather than skipping the comparison.

## C) Symbol Canonicalization Rule

- Any entrypoint that accepts user-entered symbol, broker raw payload, or OpenD/Futu underlying identifier must canonicalize to the shared symbol format before business logic.
- Canonical market symbols are values like `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not be persisted as runtime symbol config or position symbols.
- Shared alias handling lives in `src/application/opend_utils.py::resolve_underlier_alias`; new entrypoints should reuse it instead of adding ad hoc `upper()` or market-specific parsing branches.

## D) Retired-Column SQL Registry

The lot-identity retirement pins live SQL that still names a retired column (`position_lots.expiration`, `position_lots.record_id`, `wheel_events.stock_lot_id`). `docs/retired_column_sql_registry.json` records SQL text, schema-helper arguments, index-name constants, and dynamic SQL against these tables even when an interpolated column is invisible to the scanner. Only the read-only lot-identity inventory and the position-projection migration retain reviewed old-column reads; the parity probe no longer does. Each exempt module must have a real hit.

Run it locally:

```sh
./.venv/bin/python scripts/retired_column_scan.py --check
./.venv/bin/python scripts/retired_column_scan.py --write   # after an intended drift
```

Any drift — a statement added, repointed, or removed — fails `tests/quality/test_retired_column_sql_registry.py`; an intended change regenerates the registry and reviews the diff. New non-exempt retired-column SQL and unreviewed dynamic SQL fail the quality gate. The R1 statement exceptions, window switch, dual-shape write path, and destructive lot-identity `apply` were retired in R2. Ordinary repository open requires the final shape; `inventory` and `verify` remain read-only diagnostics for older stores.

### 账本原子命令边界

成交归属 preview/apply 通过 `ledger.api.apply_trade_attribution` 进入账本 owner。应用负责授权和外部观察；命令在锁内重读、复验、写入、投影和读回，preview 回滚业务效果。`trades.attribution` 保留同一函数的旧入口。`tests/test_trade_attribution_ledger_command.py` 验证公共命令的预览、取消/失败回滚、幂等与真实入口冷导入；归属冲突、Combo/Wheel 和 Bot 操作保留各自集成回归。自动冲突记录通过 `ledger.api.record_trade_attribution_conflicts` 进入同一账本 owner，外部不再组织事务或逐分支写入；公共命令测试覆盖多分支实际写入、stale retry 与中途失败的全体回滚。Wheel 手动结束通过 `ledger.api.end_wheel_lifecycle` 复用账本命令；`tests/test_wheel_workflows.py` 验证旧入口与新 owner 同一性、预览无效果、真实事件写入后的异常/投影失败回滚，以及安全重试、账户/generation/request 身份拒绝。Call/Put 关联拒绝也由同一账本命令模块持有事务，旧工作流入口直接导出；`tests/test_wheel_ledger_commands.py` 覆盖锁内目标绑定、预览、幂等、首次写入与重试身份隔离，以及实际追加后的失败/投影异常全体回滚。分支 start/end 同样由 `ledger.api.decide_wheel_branch` 持有原子边界；行为测试覆盖预览后关闭窗口的拒绝、关闭后的 end、真实写入后回滚和重试。意图创建与取消由 `ledger.api.create_wheel_intent` / `cancel_wheel_intent` 及兼容 Call 命令持有同一事务，测试覆盖创建/取消的预览、原请求重试、身份隔离、实际事件追加后投影失败回滚及安全重试；通用 Call 分支绑定须在事务内完成。启用窗口和 policy binding 分别经 `ledger.api.change_wheel_activation_window`、`ledger.api.record_wheel_policy_binding` 提交；共享启用规则拒绝已被绑定替代的重试，绑定复用仓储 CAS 并在事务内核对配置文件身份与持久收据。新增命令测试覆盖写入后异常/读回失败/文件变化的回滚、同内容文件替换拒绝及并发重试唯一效果；配置发布、恢复与最终 readiness 保留原编排和集成测试。其它账本事务消费者仍须逐个核验。

账本外部依赖由 `tests/test_option_positions_legacy_retirement.py` 的 AST 门禁检查：直接、别名、相对导入及静态模块别名访问都必须经 `ledger.api`。已迁移的自动归属、Wheel 工作流、policy binding 与容量取数编排另禁止通过 facade 别名取得锁/事务、直接写业务事件/窗口/绑定及操作业务 SQL。该检查不执行生产模块，也不声称覆盖动态反射或证明 facade 的每个操作没有副作用；事务和只读保证仍由对应公共入口行为测试证明。

剩余协调锁经过逐调用点分类，保留范围如下；它们不允许应用自行开启账本 SQL 事务，也不证明跨库提交原子性。

| 应用 owner | 保留理由 | 行为证据 |
| --- | --- | --- |
| `trades.inbox` 的 enqueue、claim、review、commit scope 和两个 resume 入口 | 收件箱独立存储；原 ledger 锁约束 claim、payload version、身份冲突与经济提交的竞争。commit scope 在进入账本命令前关闭 Inbox 连接，继续持锁。 | `test_trade_intake_recovery.py`：claim 被撤销/抢占、其他入口冲突、进程崩溃后恢复、提交期间 review 竞争 |
| `trades.resolver.resolve_trade_deal` | apply 时绑定成交身份、幂等判断与后续账本命令选择；实际事件事务由已有 ledger 命令负责。 | `test_trades_resolver_close.py`、`test_trade_intake_recovery.py` |
| `trades.auto_intake` 的 receipt recovery、skipped recovery 主入口和 source completion | 绑定账本收据读取、Inbox CAS 与来源状态更新；恢复快照查询已归账本 owner，恢复授权和跨库补偿仍归应用。 | `test_trade_receipt_recovery.py`、`test_trades_auto_intake_cli.py` |
| `trades.auto_intake._process_payload._record_lookup_evidence` | provider 查询结束后，复用可重入 writer 锁绑定 Inbox 来源/复核事务及其回读，防止后续经济决策读取被竞争改写的结果；不开启账本 SQL 事务。 | `test_trades_auto_intake_cli.py`：回读时跨进程非阻塞锁探针、异常回读拒绝；`test_trades_inbox.py`：复核与来源同事务回滚、旧 claim 失效 |
| `positions.workflows.execute_broker_assigned_stock_sale` | 锁内匹配股票批次并调用现有账本卖出命令；事件写入、余额复验与投影仍由 ledger 事务负责。 | `test_assigned_stock_sale_intake.py`：真实事件/投影、幂等及失败回滚 |
| `bot.control.attribution_operations._finish_attribution_operation` | 将签名操作状态、取消/恢复与完整账本收据绑定，防止迟到 worker 重获写入资格；归属命令继续持有经济事务。 | `test_attribution_operations.py`：取消后迟到 worker、提交后收据恢复和读回失败 |

`test_remaining_ledger_coordination_locks_have_reviewed_owners` 固定上述 5 个模块、13 个函数的调用范围，并禁止非 ledger 运行代码重新导入/访问低层事务函数。该静态门禁识别相对导入、别名和新增/嵌套调用点；动态反射不在其证明范围。Inbox 自有 SQL、quality/research 独立物理诊断仍遵守各自 owner 和现有 SQL 门禁，不因共享账本查询而删除必要的独立验证。


成交容量入口只适配证据资格与配置，数量对账和容量计算复用 `domain.domain.risk_capacity.evaluate_booked_option_capacity`，Wheel 预留转换复用领域 intents owner。`tests/test_booked_option_capacity.py` 覆盖账本/券商错配、未知与真实零、跨账户、原币占用和预留转换；`tests/test_trade_attribution_ledger_command.py` 验证 ledger 与 Wheel 兼容入口实际委托同一领域判定，过期证据在委托前拒绝。

Wheel 纯配置、策略 hash 和启用窗口匹配由 `domain/domain/wheel/config.py` 统一实现；`src/application/wheel/config.py` 仅兼容导出。`tests/test_wheel_config.py` 验证导出与领域 owner 同一性，以及账户、标的市场、policy、缺失窗口的拒绝语义；分支决定和意图创建复用同一个纯启用校验，统一由账本命令在自身写事务内读取窗口。

持仓风险上下文由 `ledger.api.build_position_risk_context` 等入口组装，旧 `positions.context_builder` 直接导出同一实现；账本内部不反向依赖持仓或交易编排。生命周期纯读模型与回执时间计算由领域层持有，原交易入口保留同一函数。`tests/test_position_context_owner.py` 验证入口同一性、真实快照的无数据库/取数组装、输入与账本不变，以及既有回执 hash 语义；原上下文与生命周期测试继续验证业务结果。

Wheel 容量查询由 `ledger.api.read_wheel_capacity_context` 持有快照边界，复用既有只读仓储、决策快照和风险上下文 owner；不会初始化或修复账本。既有 writer lock 仍用于绑定账本与独立收件箱的证据，属于协调锁，不代表查询可以写业务数据。`tests/test_position_context_owner.py` 验证 SQLite query-only、跨进程锁实际持有、单次账本读取、账户隔离、缺失与真实空账户的区别，以及券商取数期间写入不会拼接进已冻结的两个上下文。

历史成交恢复快照的八表 SQL 和恢复 hash 由 `ledger.api.read_trade_intake_recovery_snapshot` 统一持有；`auto_intake` 仅提供已校验的账户与 Inbox/state 观察，不自行访问业务 SQL。`tests/test_trade_recovery_snapshot_owner.py` 以迁移前协议的固定 hash 验证兼容，并覆盖八表账户隔离、WAL 并发提交下的一致快照、缺失证据拒绝和无初始化/修复。AST 门禁同时约束恢复编排重新开 SQLite 连接或执行 SQL，以及 outbox 经 facade 别名取得锁/事务或直接 CAS 的绕行；既有跨库 claim/恢复协调仍按各自 owner 保留，不能由这项读取测试推导其原子性。
