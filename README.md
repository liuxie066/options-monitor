# options-monitor

期权监控与提醒工具，面向 **Sell Put / Covered Call** 工作流。

它解决的核心问题只有 4 件：

1. 扫描你关注的标的期权链
2. 按策略阈值筛选 Sell Put / Covered Call 候选
3. 结合账户持仓与现金判断是否可做
4. 生成提醒、平仓建议和运行结果

这份 README 只保留产品使用需要的信息：
- 安装
- 初始化
- 运行
- 排障入口

更细的配置契约和运维细节见：
- [CONFIGS.md](CONFIGS.md)
- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)
- [RUNBOOK.md](RUNBOOK.md)

---

## 1. 快速开始

### 1.1 安装依赖

```bash
git clone <repo-url> options-monitor
cd options-monitor
./run_watchlist.sh
```

如果你想手动安装环境：

这属于本地环境准备，不是运行期业务命令；这里出现 `python3 -m ...` 不代表 Agent 应默认用 Python 脚本探索项目行为。

```bash
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt
```

`requirements.txt` 已包含 `futu-api`，缺少 Futu SDK 时会随安装流程一起补齐。

### 1.2 给 Agent 使用（可选）

如果你是把它作为本地 Agent 工具来用，执行：

```bash
bash scripts/install_agent_plugin.sh
./om-agent spec
```

常用方式：

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

Codex / Claude Code / OpenClaw 排障优先用只读聚合入口：

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

OpenClaw 环境可以先跑：

```bash
./om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
```

`om-agent` 是面向程序/Agent 的结构化入口；`om` 是面向人工操作的 CLI 入口。

当前 tick / 扫描 / 通知主流程只有一条统一链路：`src/application/multi_account_tick.py`。
单账户运行只是传一个账户的特例，例如 `--accounts lx`；多账户运行传多个账户，
例如 `--accounts lx sy`。旧脚本名仍保留，但只作为兼容包装。

给 Codex、Claude Code、OpenClaw 这类代理使用时，建议遵守下面的默认约束：

- **先读后跑**：如果任务是“解释 / look into / check / why / explain”，先读代码、配置文档和测试，不要先执行脚本。
- **默认不要直接运行 runtime scripts**：不要把 `python3 scripts/...` 当作第一选择，除非用户明确指定脚本，或更高层入口不覆盖该能力。
- **入口优先级**：优先 `./om-agent`，其次 `./om`，再考虑 `python3 -m ...`，最后才是 `python3 scripts/...`。
- **高风险动作先确认**：发送通知、写入持仓、修改生产配置、删除运行产物前，需要用户明确要求。
- **优先 dry-run / validate / test**：如果一个问题可以通过健康检查、配置校验、测试或 dry-run 回答，就不要先跑真实流程。

如果你要给代理一个最短指令，可以直接用这一段：

> This repo is operations-sensitive. For explanation, investigation, or code-reading requests, inspect files and summarize first. Do not default to running `python3 scripts/...`. Prefer `./om-agent`, then `./om`, then `python3 -m ...`, and use `python3 scripts/...` only when the user explicitly asks for that script or no higher-level entry point exists. Never send notifications or mutate runtime state unless the user explicitly requests it.

---

## 2. 初始化

### 2.1 启动 WebUI

```bash
./run_webui.sh
```

默认地址：

```text
http://127.0.0.1:8000
```

WebUI 现在按 6 个模块组织：

- 行情设置
- 账户设置
- 选股策略
- 平仓建议
- 消息通知
- 高级设置

首次初始化建议直接在 WebUI 中完成。

---

### 2.2 手工初始化（可选）

如果你不用 WebUI，也可以手工复制模板：

```bash
cp configs/examples/config.example.us.json config.us.json
cp configs/examples/config.example.hk.json config.hk.json
mkdir -p secrets
cp configs/examples/portfolio.sqlite.example.json secrets/portfolio.sqlite.json
```

---

## 3. 配置文件说明

你日常维护的文件通常只有：

- `config.us.json`
- `config.hk.json`
- `secrets/portfolio.sqlite.json`
- `secrets/notifications.feishu.app.json`（如果启用通知）

### 最小配置默认数据来源

- 行情与期权链：OpenD / Futu API
- 持仓与现金：Futu / OpenD
- 期权持仓存储：SQLite
- Feishu `option_positions` 不是稳态主存储；只用于可选的空库 bootstrap 和远端镜像

---

## 4. 账户与持仓

- 支持多账户配置
- 持仓与现金默认来自 Futu / OpenD
- Feishu holdings 可作为 `holdings` 或 `external_holdings` 来源
- 运行时 Feishu holdings 连接只认 `portfolio.data_config.feishu.tables.holdings`
- 账户级 `bitable_*` 字段当前只是历史/预留展示，不参与运行时读取

如果你启用了 Feishu `option_positions` bootstrap：
- SQLite 非空时不会再触发 bootstrap
- SQLite 空库但远端读取失败时，healthcheck 和 WebUI 会明确显示 degraded 状态

如果你需要：
- 多账户配置
- 账户级持仓来源
- 每账户 OpenD 持仓连接
- 通知凭证与高级配置

请直接查看：

- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)
- [CONFIGS.md](CONFIGS.md)

---

## 5. 通知

当前正式通知链路支持：

- 飞书开放平台应用发个人消息

统一 tick 的通知语义是固定的：

- 同一个通知目标下，每个账户各发送一条消息
- 账户消息内容由 `scripts/notify_symbols.py` 和 `scripts/multi_tick/notify_format.py` 负责排版
- 主流程在 `src/application/multi_account_tick.py` 准备候选、现金 footer 和 heartbeat 消息
- 某个账户发送失败不会阻断其他账户；只有发送成功的账户会更新 notified 状态

通知凭证默认放在：

- `secrets/notifications.feishu.app.json`

具体字段和配置方式见 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。

---

## 6. 常用命令

对 Agent 来说，本节也建议按下面顺序选入口：

1. `./om-agent`（结构化、最适合代理）
2. `./om`（统一 CLI）
3. `python3 -m ...`
4. `python3 scripts/...`（仅兼容/兜底）

### 6.1 校验配置

Agent 默认使用结构化配置校验工具；它只读 runtime config，不检查 OpenD，也不运行 pipeline。

```bash
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool config_validate --input-json '{"config_path":"config.us.json"}'
```

### 6.2 健康检查

这是 Agent 排查问题时的首选入口之一，优先于直接跑 runtime pipeline。

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

如果你要指定配置路径：

```bash
./om-agent run --tool healthcheck --input-json '{"config_path":"config.us.json"}'
```

### 6.3 检查版本更新

```bash
./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'
```

人工 CLI 仍可使用 `./om version`。

### 6.4 查询 Sell Put 现金余量

Agent 使用 `query_cash_headroom`。它是 `scripts/query_sell_put_cash.py` 中
`query_sell_put_cash(...)` 的结构化入口，用于查询账户现金、Sell Put 担保占用和剩余可用现金。

```bash
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"sy"}'
```

人工 CLI 对应命令：

```bash
./om sell-put-cash --market 富途 --account lx
./om sell-put-cash --market 富途 --account sy
```

### 6.5 手动跑一次 pipeline

下面命令会触发真实运行；如果你只是排查问题，优先先做 healthcheck、配置校验或只跑更小的阶段。

```bash
./om scan-pipeline --config config.us.json
```

只跑某个阶段：

```bash
./om scan-pipeline --config config.us.json --stage fetch
./om scan-pipeline --config config.us.json --stage scan
./om scan-pipeline --config config.us.json --stage alert
./om scan-pipeline --config config.us.json --stage notify
```

### 6.6 统一 tick 运行

推荐：

```bash
./om run tick --config config.us.json --accounts lx
./om run tick --config config.us.json --accounts lx sy
```

`./om run tick` 是正式入口。传一个账户就是单账户运行，传多个账户就是多账户运行；
不传 `--accounts` 时读取 runtime config 顶层 `accounts`。统一链路会复用共享行情 /
required data，再按账户生成和发送通知。通知发送和 notified 状态更新的口径见第 5 节。

如果是 Agent 在排查问题，不要默认从这里开始；先做 healthcheck、配置校验，必要时再缩小到单阶段运行。

兼容入口：

```bash
python3 scripts/send_if_needed_multi.py --config config.us.json --accounts lx sy
```

对 Agent 来说，这个兼容入口不应作为默认首选；只有在 `./om` / `./om-agent` 不覆盖，
或用户明确指定脚本时再使用。该兼容入口会转调同一个
`src.application.multi_account_tick.run_tick`。

### 6.7 兼容入口

如果只是想确认系统状态，优先仍然是 `./om-agent run --tool healthcheck ...`，不是这个脚本入口。

```bash
python3 scripts/send_if_needed.py --config config.us.json
```

这是旧单账户定时脚本的兼容文件名；旧单账户业务实现已经退役，内部会转调统一 tick。
新定时任务应直接使用 `./om run tick`。

---

## 7. 常见排障

对 Claude Code、OpenClaw 这类代理，排障默认顺序建议是：

1. 先读相关代码、配置文档和测试
2. 先跑 `./om-agent run --tool healthcheck ...`
3. 再做配置校验、测试或 dry-run
4. 最后才考虑真实 runtime 命令

### 7.1 配置校验失败

如果是 Agent 在排查，先做健康检查：

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

再做配置校验（这是低风险校验脚本，不是 runtime 流程入口）：

```bash
python3 scripts/validate_config.py --config config.us.json
```

定时运行里的配置校验缓存只会在校验成功后写入；如果配置校验失败，下次 scheduled 加载仍会重新校验同一份配置，不需要为了重试而手工清理 validation cache。

优先检查：
- `notifications.target`
- `notifications.secrets_file`
- `trade_intake.account_mapping.futu`
- `account_settings.<account>.type`
- `symbols[]`

然后对照 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md) 和 [CONFIGS.md](CONFIGS.md) 核对配置来源与字段约束。

---

### 7.2 healthcheck 只看到一个 OpenD endpoint

先确认不是配置映射问题，再怀疑运行态：

优先检查：
- 账户映射是否正确
- OpenD 是否真的在线
- 账户级持仓连接配置是否填写完整

详细字段说明见 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。

如果只是 Agent 在分析问题，不要直接切到 runtime pipeline；先读配置和 healthcheck 输出。

---

### 7.3 两个账户持仓看起来一样

先做只读排查：

优先检查：
- 是否两个账户都被映射到了同一个 Futu account id
- 是否账户配置实际指向了同一份持仓来源
- 是否仍然回退到了全局持仓配置

具体配置优先级见 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。

必要时先复核 healthcheck / 配置输出，再决定是否运行多账户流程命令。

---

### 7.4 通知保存失败

先做低风险检查，不要默认直接重跑发送流程：

优先检查：
- `notifications.target` 是否为空
- `secrets/notifications.feishu.app.json` 是否存在
- `app_id / app_secret` 是否完整

如果需要进一步验证，优先使用配置校验、healthcheck 或相关测试，而不是直接触发通知发送。

---

## 8. 文档导航

- [CONFIGS.md](CONFIGS.md)：配置来源与 canonical config 约定
- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)：详细配置字段说明
- [RUNBOOK.md](RUNBOOK.md)：运维巡检与应急操作
- [tests/README.md](tests/README.md)：测试分层和运行方式

---

## 9. 风险提示

本工具只做监控、筛选和提醒，不构成投资建议。期权交易风险较高，任何下单都需要自行复核标的、价格、仓位、保证金、流动性和事件风险。
