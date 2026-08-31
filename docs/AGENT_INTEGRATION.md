# Tool Gateway Integration

The public Tool Gateway launcher is `./om-agent`.

`./om-agent` is a structured local tool-call entrypoint for external agents,
scripts, Codex, or operators. It is not OM's autonomous/project
Agent, and it should not own multi-step planning or message conversation
state. Current entry and layer terminology is defined in
[ARCHITECTURE.md](ARCHITECTURE.md); channel message handling is defined in
[INBOUND_CONTROL.md](INBOUND_CONTROL.md).

It exposes a stable JSON contract intended for local machine usage:

- `./om-agent add-account --market us|hk --account-label <label> --account-type futu|external_holdings --dry-run`
- `./om-agent spec`
- `./om-agent run --tool <name> --input-json '<json>'`

Capability boundaries, risk classes, Inbound Assistant exposure, and
verification rules are maintained in
[OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).
This document only describes integration contracts and invocation patterns.

也支持：

- `./om-agent run --tool <name> --input-file payload.json`

其中 `--input-file` 会覆盖 `--input-json`。

Implementation ownership:
- Tool implementation source of truth: `src/application/agent_tools/<domain>.py`
- Tool manifest collector: `src/application/agent_tool_registry.py`
- Tool write permission gate: `src/application/agent_tools/permissions.py`
- Tool response contract: `src/application/agent_tool_contracts.py`
- Runtime config helpers: `src/application/agent_tool_config.py`
- Runtime config initialization/account mutation helpers: `src/application/agent_tool_init_local.py`
- Public CLI owner: `src/interfaces/agent/cli.py`
- Runtime tick is not a separate single-account / multi-account split. The live chain is `./om run tick` -> `src.application.multi_account_tick.run_tick`; pass one account for single-account execution or multiple accounts for multi-account execution.

## Contract

All tool responses return:

```json
{
  "schema_version": "1.0",
  "tool_name": "healthcheck",
  "ok": true,
  "data": {},
  "warnings": [],
  "error": null,
  "meta": {}
}
```

Errors are normalized to stable codes such as:

- `CONFIG_ERROR`
- `INPUT_ERROR`
- `DEPENDENCY_MISSING`
- `PERMISSION_DENIED`
- `CONFIRMATION_REQUIRED`
- `INTERNAL_ERROR`

说明：
- 这些是顶层错误 envelope 的稳定代码。
- 某些底层诊断项（例如 OpenD readiness probe 的细粒度失败原因）可能会体现在 `checks[]` 中，而不是顶层错误 code 枚举中。

## Claude Code

Use the launcher as a local command tool. Typical pattern:

```bash
./om-agent spec
./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'
./om-agent run --tool version_update --input-json '{"bump":"patch"}'
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"sy"}'
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'
./om-agent run --tool option_performance_report --input-json '{"config_key":"us","account":"lx","period":"month","month":"2026-04"}'
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
./om-agent run --tool prepare_close_advice_inputs --input-json '{"config_key":"us"}'
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
PORTFOLIO_SERVICE_URL=http://127.0.0.1:8765 ./om-agent run --tool portfolio_query --input-json '{"view":"overview","accounts":["lx","sy"]}'
PORTFOLIO_SERVICE_URL=http://127.0.0.1:8765 ./om-agent run --tool portfolio_pnl_bridge --input-json '{"period":"mtd","as_of_month":"2026-07","accounts":["lx","sy"]}'
PORTFOLIO_SERVICE_URL=http://127.0.0.1:8765 ./om-agent run --tool portfolio_cash_bridge --input-json '{"period":"mtd","as_of_month":"2026-07","accounts":["lx","sy"]}'
PORTFOLIO_SERVICE_URL=http://127.0.0.1:8765 ./om-agent run --tool portfolio_assignment_scenario --input-json '{"accounts":["lx","sy"]}'
```

`portfolio_query` 是同机 portfolio-management 的纯读适配器。它只发送 GET，
默认连接 `http://127.0.0.1:8765`，并拒绝非 loopback 的
`PORTFOLIO_SERVICE_URL`。模型 payload 不能提供 URL/endpoint；支持的 view 为
`health|accounts|overview|holdings|cash|nav|distribution|full_report`。服务返回的
业务字段保留在结果顶层，并补充 `source`、`scope`、`freshness`。portfolio-management
返回 `success=false`、HTTP 错误、无效 JSON 或超时时，工具返回标准失败 envelope。

`portfolio_pnl_bridge` 和 `portfolio_cash_bridge` 都要求
`period=mtd|ytd`、`as_of_month=YYYY-MM` 和账户列表，并使用 PM 返回的实际期末日期
调用只读的 `option_performance_report`。PnL 桥使用 PM
`/api/v1/analysis/capital-facts` 的
期初/期末总资产、外部出入金和期间盈亏，以及
`pnl.period_total_net`；指派股票本金不会进入 PnL 方程。Cash 桥只使用 PM
现金事实尚未由 PM onboarding，因此 Cash 桥直接返回
`portfolio_cash_facts_not_onboarded`，不会请求占位接口，也不会拿总资产代替现金。
未来只有在 PM 独立交付 cash/MMF、期初期末和外部现金流契约后才启用该桥。
两者都要求 CNY、期末日期、FX 和实际费用覆盖对齐；缺失或不完整证据保持
`amount=null`，不会按 0 处理。
输出包含结构化 `steps[]`、显式对账残差和 `fallback_text`，不生成图片。

`portfolio_assignment_scenario` 只接受 `accounts`。它通过 PM 的
`portfolio.valuation_evidence.v1` 只读快照取得全部非期权资产、当前报价和显式 FX，
并从 OM canonical SQLite `position_lots` 读取 open short put/call。输出固定为 CNY
资金覆盖，MMF 计入现金，Long Option 不进入输入或输出。费用复用统一股票费用计算器；
缺少指派费用规则时净现金与净分布保持 `null/partial`。该工具不写 assignment、
持仓、报告或通知状态；上游实时估值可能刷新 portfolio-management 的既有行情 cache。

Sell Put 现金余量的标准 Tool Gateway 工具是 `query_cash_headroom`。它包装
`src.application.cash_headroom_query` 里的 `query_sell_put_cash(...)`，用于返回账户现金、
Sell Put 担保占用和剩余可用现金，并支持按账户和币种折算到 CNY。该工具是纯读入口，
不会为了查询而写本地 cache。

如果 payload 很长，优先用：

```bash
./om-agent run --tool get_close_advice --input-file payload.json
```

## Kimi Code

Use the same launcher contract. Kimi Code only needs a local command invocation and JSON parsing.

## Codex

Use the same launcher contract as Claude Code. For first-pass troubleshooting, prefer:

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

For MacBook-side Codex diagnosis of online quality, candidate-scan behavior,
or Strategy Lab analysis, use the independent Research / Shadow Replay side
lane instead of `om-agent` and instead of calling an online AI provider:

```bash
./om research collect --config-key us --scope full --output both --no-write-outputs
./om research shadow-replay status --min-sample 30
./om research shadow-replay candidate-impact-report --params <params.json> --market us --start-date <YYYY-MM-DD> --account lx --min-sample 30
./om research shadow-replay build --run-id <run-id>
./om research shadow-replay run-data-plan
```

Research / Shadow Replay remains an offline evidence side lane. Strategy Lab is
not an `om-agent` tool. Its current public surface is the root
`./om strategy-lab` operator command group: Recipe listing, preview, explicit
confirmation, status, bounded `research execute`, Research Receipt reading, and
targeted readiness. Each research invocation consumes at most one provider logical
evidence unit; Phase 2 has no timer or hidden validation. Shadow Replay
directly owns exploratory dataset construction, maintenance, analysis, and
candidate impact. See [Strategy Lab Current Implementation](STRATEGY_LAB_DESIGN.md).
Use
`review_readiness` to decide whether evidence is ready for manual strategy
review, and use `candidate-impact` / `candidate-impact-report` to compare how
explicit threshold variants would change the observed candidate set.
This workflow must not call online AI providers, mutate runtime config, write
trade state, or send notifications.

## Inbound Remote Messages

Use `./om assistant handle` when a remote messaging gateway needs to send user text into OM:

```bash
./om assistant handle --text '/positions sy' --sender ou_xxx --channel feishu --message-id msg_xxx
```

This is a controlled Inbound Assistant message entrypoint, not an `./om-agent`
tool and not a shell bridge. It performs sender allowlist checks, message
idempotency, and SQLite audit. Explicit commands and pending-operation replies
enter deterministic Control; every other message enters the single read-first
`om_chat` Copilot Scene when `assistant.copilot.enabled` is true. Copilot gets
canonical pure-read tools; the optional `portfolio` toolset is projected only
when `assistant.copilot.toolsets.portfolio` is also true. This setting does not
unregister `portfolio_query` from `./om-agent`. Copilot may request one validated deterministic Control
preview; it cannot confirm, cancel, apply, or receive direct notification,
config-write, ledger/trade, broker-write, service-control, or upgrade tools.

The Host persists structured conversation memory, durable runs, cancellation,
safe pure-read resume, coarse progress events, and an idempotent reply outbox.
These are Host governance mechanisms, not additional business-routing layers.

Remote channels require:

```bash
OM_FEISHU_BOT_USER_OPEN_ID='ou_xxx'
OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx'
```

The remote capability surface is intentionally smaller than the full
`om-agent` manifest. Inspect it with `./om assistant capabilities --format json`
and keep boundary decisions in [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).
Do not connect Feishu, WeChat, or Hermes to arbitrary shell execution. Gateways
should call only `./om assistant handle`. See [INBOUND_CONTROL.md](INBOUND_CONTROL.md).

For Feishu event JSON specifically, use the thin adapter:

```bash
OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx' \
./om inbound feishu --input-file feishu_event.json --format text
```

It extracts `im.message.receive_v1` text fields and then delegates to the same Inbound control path.

For the full Feishu loop, run the long-connection service:

```bash
./om inbound feishu-ws --check
./om inbound feishu-ws --config-key us --config-path /var/lib/options-monitor/config.us.json --lock-path /var/lib/options-monitor/locks/feishu-ws.lock
```

The long-connection client receives Feishu events through the authenticated SDK connection, delegates text messages to Inbound control, and replies through the Feishu message reply API. Successful Copilot replies and deterministic replies that contain rich Markdown are rendered as display-only Feishu Card JSON 2.0 Markdown so tables remain readable; short plain Control replies and errors stay as text. The reply outbox persists the final transport envelope before delivery, retries that exact envelope with a stable UUID, and remains compatible with legacy text rows. New envelopes also retain a top-level flattened `text` copy so a code rollback can drain pending rows through the legacy sender. A confirmed permanent card rejection may use the envelope's flattened text fallback; ambiguous or transient failures retry the original card.

Scheduled Daily Brief delivery independently uses a frozen Card JSON 2.0
envelope derived from its canonical decision view. A post fallback is allowed
only for a definite permanent Card rejection with no earlier transient or
ambiguous attempt; it uses a distinct fallback UUID. Provider business
rejections are definite failures, while timeouts and other ambiguous outcomes
remain unresolved and cannot advance the Daily Brief delivery pointer.

When `inbound.feishu_ws.ack_reaction` is configured, an independent bounded ACK lane adds the Reaction after the allowlisted text event has entered the business queue; the Reaction is best-effort and does not mean that Control, Copilot, a tool, or the final reply has completed. Unauthorized senders remain silent, and ACK failures or drops do not block business processing. Render it as a long-running service with `./om service render --include-feishu-ws ...`; no public callback URL or reverse proxy is required.

`openclaw_readiness` has been retired. Use `healthcheck` for environment readiness and
`runtime_status` for existing runtime artifacts.

## Service Deployment

Treat `./om-agent` as a local Tool Gateway command.

Recommended environment:

- keep repo-local `config.us.json` / `config.hk.json` as generated runtime snapshots
- complete first-time initialization with `./om config init --output config.yaml --runtime-output-dir .`
- use explicit `config_path` input only when you intentionally want to override the default repo-local config
- keep `OM_AGENT_ENABLE_WRITE_TOOLS` unset unless you explicitly want a Tool Gateway business/config write
- use `$RUNTIME/service.profile.json` from `./om service render` when production paths are not repo-local
- keep portfolio-management API on the same host and loopback; enable its `portfolio-management-api.service` explicitly

Recommended first commands:

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
```

Use `runtime_status` when you only want to inspect existing runtime files. It does not run a pipeline, send
notifications, or write state. It summarizes:

- `output_shared/state/last_run.json`
- `output_accounts/<account>/state/last_run.json`
- the latest `output_runs/<run_id>` pointer when available
- compatibility notification artifacts and their explicit `compatibility_only` authority
- freshness and per-account summary fields

普通调度通知的权威渲染面是 Daily Brief；`symbols_notification.txt` 只作为兼容
artifact 被 `runtime_status` 诊断，不能作为通知已投递的证据。

If the production layout uses non-default paths, pass them explicitly:

```bash
./om-agent run --tool runtime_status --input-json '{
  "profile_path": "/var/lib/options-monitor/service.profile.json"
}'
```

Default service safety posture:

- Prefer `healthcheck` or `runtime_status` before any runtime command.
- Do not run `./om run tick` or notification send commands unless the user explicitly asks for a live run.
- 先看 `spec` 中每个工具的 `risk_level`、`side_effects`、`requires_confirm` 和 `requires_env`。
- 纯读工具不写状态；`read_only=true` 且 `risk_level=local_write` 的 materialization 工具可能写本地 cache/report，但不写业务状态或远端。
- 真正被工具定义判定为 write request 的调用需要 `OM_AGENT_ENABLE_WRITE_TOOLS=true`；只有 `requires_confirm=true` 的工具还要求 `confirm=true` 或 `yes=true`。
- `add-account` / `edit-account` / `remove-account` are write-capable commands; use `--dry-run`
  first, then rerun with `OM_AGENT_ENABLE_WRITE_TOOLS=true` and `--confirm` only when the config write is intended.

## `spec` 的行为说明

`./om-agent spec` 输出的是当前环境下的 tool manifest。

工具定义里的 `risk_level`、`requires_confirm`、`requires_env` 和
`safe_default_input` 是代码声明，不会因为环境变量而改写。环境只会改变
`defaults.write_tools_enabled`，用于说明当前进程是否打开 Tool Gateway 写门禁。

调用方应先读取 `safe_default_input`，不要假设所有工具都要求显式选择市场。例如
`option_performance_report` 当前有安全默认 `config_key=us`，而大多数 runtime
诊断仍需要显式传 `config_key: us|hk` 或 `config_path`。

如果你打开了：

```bash
OM_AGENT_ENABLE_WRITE_TOOLS=true
```

那么 `spec` 里的默认能力描述也会随之变化。

## 写操作门禁

写权限由工具元数据和 payload 共同决定，不是按工具名硬编码。

门禁入口在 `src/application/tool_execution.py`，但“这个 payload 是否请求写入”
由 `src/application/agent_tools/<domain>.py` 的工具定义/写入策略决定，并由
`src/application/agent_tools/permissions.py` 统一执行 env/confirm 门禁。执行层不再按
具体工具名维护特殊分支。

当且仅当工具定义把当前 payload 判定为 write request 时，环境开关才是必需的：

1. 环境变量允许写：

```bash
OM_AGENT_ENABLE_WRITE_TOOLS=true
```

如果该工具同时声明 `requires_confirm=true`，调用 payload 还要显式确认
（例如 `confirm=true` 或 `yes=true`）。

以 `manage_symbols` 为例：

- `list` 永远允许
- 真正写入需要环境变量；该工具声明需要确认时还要显式确认

OpenClaw cron/readiness/profile workflows are retired from the public plugin contract.
