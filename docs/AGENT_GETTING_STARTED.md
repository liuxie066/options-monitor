# Agent Getting Started

这份文档只服务一种场景：

> 你要把 `options-monitor` 当作本地 Agent 工具来接入和调用。

如果你只是普通使用者，请先看 [GETTING_STARTED.md](GETTING_STARTED.md)。

---

## 1. 安装 Agent 插件

```bash
bash scripts/install_agent_plugin.sh
```

如果本地还没有 Python 环境：

```bash
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt -c constraints.txt
```

`requirements.txt` 已包含 `futu-api`，用于补齐本地 `futu` Python SDK。

---

## 2. 查看 Agent 工具清单

```bash
./om-agent spec
```

这会输出工具 manifest（JSON）。

---

## 3. 初始化运行配置

普通本地初始化走 `./om setup check` 和 `./om setup init`：

```bash
./om setup check
./om setup init --market us --account lx --futu-acc-id <futu-account-id>
```

首次初始化通常会生成：

- `config.us.json` 或 `config.hk.json`
- `portfolio.runtime.json`（可选；最小部署可不需要）

默认最小配置下：

- `option_positions` 只需要本地 SQLite
- Feishu 只在你启用 holdings / external_holdings 或 inbound Bot 时才需要通过 env-file 配置

---

## 4. 跑一个最基本的检查

```bash
./om doctor --config-key us
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

如果你想先确认“配置本身是否合法”，优先跑：

```bash
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
```

如果你配置了本地 env-file，先跑 settings 诊断确认密钥来源和写入开关：

```bash
./om settings doctor
```

配置优先级和工具边界的完整解释，以根目录 `CONFIGURATION_GUIDE.md` 为准。

healthcheck 会额外给出本地 `ledger_store` 和 `option_positions_bootstrap` 状态：

- `ledger_store` 用来确认当前读写的 SQLite 路径和 `trade_events` / `position_lots` 行数
- `option_positions_bootstrap` 只反映本地接管/legacy 迁移状态；Feishu `option_positions` 不再作为 bootstrap 输入

如果要显式指定配置路径：

```bash
./om-agent run --tool healthcheck --input-json '{"config_path":"config.us.json"}'
```

---

## 5. 跑一个只读工具

```bash
./om status --config-key us
./om runs --limit 10
./om logs --run-id <run-id> --lines 50
./om-agent run --tool runtime_runs --input-json '{"limit":10}'
./om-agent run --tool runtime_logs --input-json '{"run_id":"<run-id>","kind":"tool","lines":50}'
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

---

## 6. 收集 AI Cofunder 证据

如果目标是让 MacBook 上的 Codex 分析线上版本质量、持仓/交易一致性，或多账户策略影响，使用 AI Cofunder 证据交接：

```bash
./om-agent run --tool ai_cofunder --input-json '{"config_key":"us","scope":"full","output":"both","write_outputs":false}'
```

同一个能力也有人工 CLI：

```bash
./om ai-cofunder collect --config-key us --scope full --output both --no-write-outputs
```

它默认不写文件、不发送通知、不调用在线 AI。线上调度系统的状态需要通过 `scheduler_evidence` 或 `--scheduler-evidence-json` 传入。

---

## 7. 常见环境变量

- `OM_OUTPUT_DIR`：覆盖 agent 输出目录
- `OM_RUNTIME_ROOT`：覆盖运行时状态根目录；`option_positions` SQLite 位于 `<runtime_root>/output_shared/state/option_positions.sqlite3`
- `OM_AGENT_ENABLE_WRITE_TOOLS=true`：允许部分写操作工具

---

## 8. 下一步看哪里

- Agent 任务手册：[`AGENT_WIKI.md`](AGENT_WIKI.md)
- Agent JSON 合同：[`AGENT_INTEGRATION.md`](AGENT_INTEGRATION.md)
- 工具说明：[`TOOL_REFERENCE.md`](TOOL_REFERENCE.md)
- Linux / Mac 服务化部署：[`DEPLOY_LINUX_MAC.md`](DEPLOY_LINUX_MAC.md)
- 配置字段说明：[`../CONFIGURATION_GUIDE.md`](../CONFIGURATION_GUIDE.md)
