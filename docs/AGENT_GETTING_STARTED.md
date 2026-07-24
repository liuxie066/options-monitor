# Tool Gateway Getting Started

这份文档只服务一种场景：

> 你要把 `options-monitor` 当作本地 Tool Gateway 工具来接入和调用。

如果你只是普通使用者，请先看 [GETTING_STARTED.md](GETTING_STARTED.md)。

---

## 1. 安装 agent 插件

```bash
bash scripts/install_agent_plugin.sh
```

如果本地还没有 Python 环境：

```bash
python3.12 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt -c constraints.txt
```

`requirements.txt` 已包含 `futu-api`，用于补齐本地 `futu` Python SDK。

---

## 2. 查看 Tool Gateway 工具清单

```bash
./om-agent spec
```

这会输出工具 manifest（JSON）。

---

## 3. 初始化运行配置

普通本地初始化走 `./om setup check` 和 YAML starter：

```bash
./om setup check
./om config init --output config.yaml --runtime-output-dir . --futu-acc-id <futu-account-id>
```

首次初始化通常会生成：

- `config.yaml`
- `config.us.json` 和 `config.hk.json`
- `config.assistant.json`

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

## 6. 收集 Research / Shadow Replay 证据

如果目标是让 MacBook 上的 Codex 分析线上版本质量、持仓/交易一致性，或多账户策略影响，使用独立的 Research / Shadow Replay 侧线：

```bash
./om research collect --config-key us --scope full --output both --no-write-outputs
./om research shadow-replay status --min-sample 30
./om research shadow-replay candidate-impact-report --params <params.json> --market us --start-date <YYYY-MM-DD> --account lx --min-sample 30
./om research strategy-lab update --latest
./om research strategy-lab update --latest --build-dataset --write
./om research strategy-lab readiness --dataset output_shared/research/shadow_replay/datasets/<dataset-id> --min-sample 30
./om research strategy-lab readiness --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30
./om research strategy-lab experiment --dataset output_shared/research/shadow_replay/datasets/<dataset-id> --min-sample 30 --auto
./om research strategy-lab experiment --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30 --auto
./om research strategy-lab proposal --experiment output_shared/research/strategy_lab/experiment.json --markdown-output output_shared/research/strategy_lab/proposal.md
./om research strategy-lab llm-context --experiment output_shared/research/strategy_lab/experiment.json --proposal output_shared/research/strategy_lab/proposal.json --output output_shared/research/strategy_lab/llm_context.json
```

Research 不属于 `./om-agent` manifest，也不能修改 runtime config、交易状态或通知，但它不是统一的“零写入”命令组：`collect --no-write-outputs`、已有 dataset 上的 readiness/status 等是只读或显式禁止报告落盘；dataset build、mark/settle、proposal、llm-context 和带输出路径的 report 会写本地 research artifacts。执行前应查看具体子命令的 `--help` 和输出参数。`strategy-lab update` 默认 dry-run；只有显式 `--build-dataset --write` 才从 latest scanned run 构建本地 replay dataset。Strategy Lab 按 Sell Put / Covered Call / Combo Yield 三个 strategy family 分域，设计见 [STRATEGY_LAB_DESIGN.md](STRATEGY_LAB_DESIGN.md)。线上调度系统的状态需要通过 `scheduler_evidence` 或 `--scheduler-evidence-json` 传入。

---

## 7. 常见环境变量

- `OM_OUTPUT_DIR`：覆盖 Tool Gateway 工具输出目录
- `OM_RUNTIME_ROOT`：覆盖运行时状态根目录；`option_positions` SQLite 位于 `<runtime_root>/output_shared/state/option_positions.sqlite3`
- `OM_AGENT_ENABLE_WRITE_TOOLS=true`：允许部分写操作工具

---

## 8. 下一步看哪里

- 本地 agent 任务手册：[`AGENT_WIKI.md`](AGENT_WIKI.md)
- 当前架构边界：[`ARCHITECTURE.md`](ARCHITECTURE.md)
- Inbound 控制面：[`INBOUND_CONTROL.md`](INBOUND_CONTROL.md)
- Tool Gateway JSON 合同：[`AGENT_INTEGRATION.md`](AGENT_INTEGRATION.md)
- 工具说明：[`TOOL_REFERENCE.md`](TOOL_REFERENCE.md)
- Linux / Mac 服务化部署：[`DEPLOY_LINUX_MAC.md`](DEPLOY_LINUX_MAC.md)
- 配置字段说明：[`../CONFIGURATION_GUIDE.md`](../CONFIGURATION_GUIDE.md)
