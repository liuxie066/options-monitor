# options-monitor

`options-monitor` 是一个本地运行的期权收益与承保决策系统。它把行情、持仓、现金、期权仓位、策略阈值、账本和通知串起来，帮助用户判断什么时候可以卖期权收保费、哪张合约值得卖、风险预算是否合适、已开仓位是否还应该持有，以及这套策略最终赚得是否科学。

主要服务四个用户功能：

- `Sell Put`：卖 Put 机会扫描。收益优先模式下是收益筛选；承保模式下是卖下跌保险的评估，默认接货是可接受结果。
- `Covered Call`：已有正股上的卖 Call 机会扫描。收益优先模式下是收益筛选；承保模式下是卖上涨保险的评估，默认被行权卖出正股是可接受结果。
- `combo_yield`：当前 Combo Yield runtime key；作为平行开仓策略配对 short put + long call，用可接受的接货义务融资上行参与。
- `close_advice`：已有期权 lot 的生命周期管理，给出止盈、继续持有、long-call 退出或无法评估的平仓参考。

它不是自动交易系统，也不会替你下单。它的输出是 advisory-only，给出便于人工复核的候选、拒绝原因、平仓建议和复盘证据。

## 产品框架

完整产品域、模块定义和模块依赖见 [docs/PRODUCT_ARCHITECTURE.md](docs/PRODUCT_ARCHITECTURE.md)。

系统支持两种策略口径。它们不是同一种风控深度：

| 策略口径 | 本质 | 推荐含义 |
|---|---|---|
| `return_first` 收益优先 | 收益筛选器 | 收益条件合格，基础交易约束合格 |
| `insurance_underwriting` 承保 | 承保评估器 | 这张保单的保费、边界和基础风险相对合理 |

收益优先模式主要看 DTE、strike 范围、年化收益、单笔净权利金、流动性、价差、Sell Put 现金是否够、Covered Call 股票是否够覆盖。它不系统性判断保费是否足够补偿 IV/RV 和事件风险。

承保模式才是“开保险公司”的主逻辑。它系统性看收益率、单笔净权利金、IV/RV、IV-RV、事件风险、流动性、Sell Put 现金覆盖和 Covered Call 股票覆盖，用来判断当前保费是否足够；通过硬筛后，优先按 Sell Put 的 strike 安全距离或 Covered Call 的 strike 上行距离推荐候选，再比较去重后的承保补偿分和流动性。

完整产品闭环：

```text
扫描机会 -> 生成建议 -> 人工决策 -> 记录开仓 -> 持仓监控
-> 平仓建议 -> 记录结果 -> 收益统计 -> 扫描质量复盘 -> 人工策略复盘证据
```

`Sell Put` / `Covered Call` 看当前配置里的策略意图生成开仓候选。`combo_yield` 是独立的组合策略；默认 `same_expiry_pair` 保留既有逻辑，`staggered_expiry_pair` 的 Funding Put 则复用完整 Sell Put underwriting 结果，再配对更晚到期的 Long Call。`close_advice` 比较特殊：它优先读取 lot 上的开仓策略快照；只有旧仓位没有策略快照时，才 fallback 到当前 symbol 配置或默认策略，并在输出中保留 `strategy_source`。

## 入口

| 入口 | 面向对象 | 适合做什么 |
|---|---|---|
| `om` | 人工 CLI | 配置构建、手动运行、持仓维护、只读查询 |
| `om-agent` | 程序 / 外部 agent / Tool Gateway | JSON manifest、结构化工具调用、只读诊断 |

源码目录内也可以直接使用 fallback：`./om` / `./om-agent`。

推荐顺序：

1. 首次启用先完成安装，然后运行 `om setup check`。
2. 日常人工操作优先 `om`。
3. 外部 agent 接入、排障和结构化读取优先 `om-agent`。

## 它做什么，不做什么

做什么：

- 为 `Sell Put` 扫描和筛选候选。
- 为 `Covered Call` 生成已有持仓的 covered call 候选。
- 为 `combo_yield` 评估 Combo Yield 组合。
- 为 `close_advice` 生成已有仓位的平仓参考。

不做什么：

- 不自动下单
- 不替你决定仓位
- 不默认发送通知、写 Feishu 或修改生产配置
- 不建议把有副作用的命令当成“先看看会发生什么”的探针

## 快速开始

### 1. 安装

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash

om setup check
```

无参数安装会解析并安装最新 GitHub release；不会安装浮动 `main` 分支。需要复现、回滚或固定生产版本时，显式指定 release tag：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version <release-tag> --prefix "$HOME/apps/options-monitor"
```

安装脚本会下载代码、checkout 指定 release、创建 `.venv`、安装依赖、更新 `current` symlink，并默认在 `$HOME/.local/bin` 创建 `om` / `om-agent` 用户级 wrapper。它不会写配置、不会写 secrets、不会启动服务、不会创建定时任务。
如果 `$HOME/.local/bin` 尚未加入 `PATH`，按安装输出提示先加入 PATH，或使用 fallback：`$HOME/apps/options-monitor/current/om setup check`。

手动安装、server/dev 依赖和目录布局见 [docs/INSTALL.md](docs/INSTALL.md)。

平台默认值：

| 平台 | 推荐 runtime root | 推荐 env-file | 服务管理器 |
|---|---|---|---|
| Linux | `/var/lib/options-monitor` | `/etc/options-monitor/options-monitor.env` | `systemd` |
| macOS | `$HOME/Library/Application Support/options-monitor` | `$HOME/Library/Application Support/options-monitor/options-monitor.env` | `launchd` |

如果要从飞书 long-connection 接收远端命令，安装时加 `--with-server`。

### 2. 初始化配置

当前推荐配置模型：

- 代码内 `DEFAULT_CONFIG` 提供系统默认值，用户不用维护系统默认文件。
- `config.yaml` 只保存用户 override，包括 accounts、markets、symbols 和非 secret 行为配置。
- env-file 只保存 secrets、Feishu 凭证和写入开关。
- `config build` 生成 market-specific runtime config，实际运行仍读取 JSON 快照。
- `config build` / `config explain` 读取 YAML authoring config。

源码 checkout 或本地手动运行可以直接在 repo root 维护忽略文件 `config.yaml`：

```bash
om config init --output config.yaml --runtime-output-dir .
$EDITOR config.yaml

om config validate --source yaml --market us
om config build --source yaml --market us --output config.us.json
om config validate --config-path config.us.json --market us
```

HK 同理：

```bash
om config validate --source yaml --market hk
om config build --source yaml --market hk --output config.hk.json
om config validate --config-path config.hk.json --market hk
```

生产服务建议把 `config.yaml` 和生成后的 runtime config 放在 `runtime_root` 或其他 release 外的持久路径，再显式传路径：

```bash
om config init --output /var/lib/options-monitor/config.yaml --runtime-output-dir /var/lib/options-monitor
om config build --source yaml --market us --config-yaml /var/lib/options-monitor/config.yaml --output /var/lib/options-monitor/config.us.json
om config build --source yaml --market hk --config-yaml /var/lib/options-monitor/config.yaml --output /var/lib/options-monitor/config.hk.json
om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --include-opend \
  --opend-root /home/liuxie/apps/futu-opend/current
```

需要远端持续记录 Strategy Lab / Shadow Replay 证据时，额外传：

```bash
om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --include-opend \
  --include-strategy-lab-recorder \
  --strategy-lab-recorder-source opend
```

这个 recorder 是显式 opt-in。它生成独立 timer：幂等构建 latest scanned run 的 Shadow Replay dataset、定期采样 mark path、每日尝试 settle outcome。它只写本地 replay dataset、required-data / OpenD cache / rate-limit state 和 receipt，不发通知、不运行 experiment/proposal、不修改 runtime config、交易状态、Feishu 或 broker-facing state。

生成的 runtime config 会记录 `_generated` 指纹。`config.yaml` 更新后需要重新
`config build --source yaml`。`run tick` / `run tick-cron` 会在陈旧 runtime config 上提前失败并给出重建命令。

完整首次运行流程见 [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)。

### 3. 先做只读验证

先检查配置本身是否合法：

```bash
om config validate --source yaml --market us
om config validate --config-path config.us.json --market us
om-agent run --tool config_validate --input-json '{"config_key":"us"}'
```

再检查本机前置条件、OpenD、SQLite 和通知配置：

```bash
om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
om doctor --config-key us
om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
```

解释某个配置值来自哪里：

```bash
om config explain --source yaml --market us --key option_positions.auto_close.enabled
om config explain --source yaml --market us --key symbol_defaults.fetch.limit_expirations
```

直接查看 runtime config 时，使用只读 `config get`；持久配置变更应改 `config.yaml` 后重新 `config build --source yaml`。写入语义统一为：
默认只读或 dry-run；`--apply` 允许本地文件/状态写入；`--confirm` 允许交易事件、Feishu、服务变更这类高风险写入；`--yes` 用于非交互脚本，等价显式确认并在输出里带 `audit_id`。写命令的 JSON 输出都会带 `dry_run`、`write_applied`、`backup_path`、`audit_id`、`rollback_hint`。

```bash
om config get --config-key us --key runtime.prefetch.max_workers
```

### 4. 第一轮真实运行

先禁发通知：

```bash
om run tick --config config.us.json --accounts lx sy --no-send
```

确认输出、候选和通知预览都合理，再进行正式运行：

```bash
om run tick --config config.us.json --accounts lx
om run tick --config config.us.json --accounts lx sy
```

### 5. Linux / Mac 服务化部署

长期运行时不要让运行产物散落在仓库目录。统一使用 `runtime_root`：

```text
<runtime_root>/output_runs/
<runtime_root>/output_shared/
<runtime_root>/output_accounts/
```

期权持仓 SQLite 固定在：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

渲染服务文件：

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us hk \
  --accounts lx sy \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --output-dir /tmp/options-monitor-service

./om service render \
  --target launchd \
  --runtime-root "$HOME/Library/Application Support/options-monitor" \
  --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env" \
  --markets us hk \
  --accounts lx sy \
  --config-yaml "$HOME/Library/Application Support/options-monitor/config.yaml" \
  --config-us "$HOME/Library/Application Support/options-monitor/config.us.json" \
  --config-hk "$HOME/Library/Application Support/options-monitor/config.hk.json" \
  --output-dir /tmp/options-monitor-service
```

完整步骤见 [`DEPLOY.md`](DEPLOY.md)。

## 常用工作流

### 监控与扫描

统一 tick 入口只有一条链路：

```bash
./om run tick --config config.us.json --accounts lx
./om run tick --config config.us.json --accounts lx sy
```

单账户只是传一个账户的特例；多账户直接把多个账户标签传给 `--accounts`。

只做扫描时：

```bash
./om scan --config-key us --symbols NVDA,TSLA --top-n 5
./om-agent run --tool scan_opportunities --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":5}'
```

### 候选排序解释

解释已有候选为什么这样排序：

```bash
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'
./om-agent run --tool candidate_rank_explain --input-json '{"run_id":"<run-id>","account":"lx","mode":"put","top_n":5}'
./om-agent run --tool candidate_rank_explain --input-json '{"candidate_path":"output_shared/reports/sell_call_candidates.csv","mode":"call","top_n":5}'
```

这个工具只读已有 CSV，不重跑扫描，不发送通知，也不改配置。

### 为什么某个 symbol 被过滤掉

如果线上有人反馈“某个 symbol 没进候选”或者“为什么这个账户没有看到它”，先看 `candidate_filter_explain`。

按 run/account 查：

```bash
./om-agent run --tool candidate_filter_explain --input-json '{"run_id":"<run-id>","account":"lx","symbol":"NVDA"}'
```

按 trace 文件查：

```bash
./om-agent run --tool candidate_filter_explain --input-json '{"trace_path":"output_shared/reports/candidate_filter_trace.jsonl","symbol":"NVDA"}'
```

它会把 trace 汇总到这几个函数维度：

- `sell_put`
- `sell_call`（trace/runtime 内部 key；`config.yaml` authoring 使用 `covered_call`，对外显示为 Covered Call）
- `close_advice`
- `combo_yield`
- `cash_reserve`
- `share_coverage`

适合回答这些问题：

- 这个 symbol 在某次 run 里是否被观察到。
- 它被哪个函数、stage 或 rule 过滤。
- 同一 symbol 在不同账户之间为什么结果不同。
- 需要补 trace、reject log 还是候选 CSV，才能继续诊断。

### 离线研究与策略复盘

`./om research` 提供不改动生产状态的离线证据与复盘能力：

- **`collect`**：从已有扫描 run 生成脱敏证据包，用于本地分析或 handoff；若存在 Combo Yield pair diagnostics，会同时汇总配对拒绝漏斗、跨账户去重计数和最近未通过项。
- **`shadow-replay`**：把历史扫描候选、拒绝原因和后续价格路径落地为本地 dataset，做反事实复盘与参数影响评估。
- **`strategy-lab`**：在 Shadow Replay 之上做策略进化实验；Sell Put / Covered Call 生成受控 hypotheses（含历史百分位 IV/RV variants）并输出生产观测顺序 vs 去重承保排序对照，Combo Yield 只做 group-level outcome evaluator；候选变化只供审阅，只有严格 outcome dominance 才生成 advisory-only proposal。
- **`archive`**：把远端 runtime 证据增量镜像到本地，便于磁盘有限的场景做离线复盘。

这些入口默认只读；需要写本地 replay artifact 时必须显式加 `--write`。完整操作手册见 [docs/SHADOW_REPLAY_RUNBOOK.md](docs/SHADOW_REPLAY_RUNBOOK.md) 和 [docs/STRATEGY_LAB_DESIGN.md](docs/STRATEGY_LAB_DESIGN.md)。

### Sell Put 现金余量

人工 CLI：

```bash
./om sell-put-cash --market 富途 --account lx
./om sell-put-cash --market 富途 --account sy
```

Tool Gateway：

```bash
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
```

### 平仓建议

人工 CLI：

```bash
./om close-advice --config-key us
```

推荐的 Tool Gateway 一站式入口：

```bash
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
```

如果要拆成两步排查输入准备和建议生成：

```bash
./om-agent run --tool prepare_close_advice_inputs --input-json '{"config_key":"us"}'
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

日常 tick 会先写正式 `close_advice.csv`；如果同一账户已有 `portfolio_capacity_shadow.csv`，还会额外写 `close_advice_reallocation_shadow.csv`。这个 reallocation shadow 只提示是否值得人工 review 换仓，不改正式 Close Advice、不改通知、不改候选排序，也不写持仓或配置。

### Symbols

推荐写入入口是 `config.yaml`，命令默认 dry-run，带 `--apply` 才会写入；需要同步生成运行时配置时加 `--rebuild-runtime-root`：

```bash
./om config symbol set --config-yaml config.yaml --market hk --symbol 09898 \
  --covered-call-enabled true --covered-call-min-strike 85 --sell-put-enabled false \
  --combo-yield-enabled true --apply --rebuild-runtime-root .
```

`symbols` CLI operates on an explicit generated runtime snapshot. Normal config authoring should still happen in `config.yaml`, followed by `om config build --source yaml`.

```bash
./om symbols --config config.us.json list
./om symbols --config config.us.json add TCOM --put --dry-run
./om symbols --config config.us.json add TCOM --put --apply
./om symbols --config config.us.json edit TCOM --set sell_put.max_strike=45 --apply
./om symbols --config config.us.json rm TCOM --apply
```

Tool Gateway 只读列出：

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

写入 `symbols[]` 时，需要显式确认和 `OM_AGENT_ENABLE_WRITE_TOOLS=true`。

### Option Positions

查看本地期权仓位：

```bash
./om option-positions list --broker 富途 --account lx --status open
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
```

新增仓位先用 `--dry-run`：

```bash
./om option-positions add --account lx --symbol 0700.HK --option-type put --side short --contracts 1 --currency HKD --strike 420 --multiplier 100 --exp 2026-04-29 --premium-per-share 1.2 --dry-run
./om option-positions add --account lx --symbol 0700.HK --option-type put --side short --contracts 1 --currency HKD --strike 420 --multiplier 100 --exp 2026-04-29 --premium-per-share 1.2 --confirm
```

普通买平、被指派、主动行权是不同账本语义，分别使用独立入口：

```bash
./om option-positions buy-close --account lx --symbol TIGR --option-type put --strike 6 --exp 2026-05-22 --contracts 10 --close-price 0.05 --dry-run
./om option-positions assign --account lx --symbol TIGR --option-type put --strike 6 --exp 2026-05-22 --contracts 10 --stock-side buy --stock-qty 1000 --stock-price 6 --dry-run
./om option-positions exercise --account lx --symbol AAPL --option-type call --strike 200 --exp 2026-05-22 --contracts 2 --stock-side buy --stock-qty 200 --stock-price 200 --dry-run
```

修复单个历史 lot 的策略元数据可走 `adjust-lot`。若要确认一组错期 Combo Yield，应使用 `pair-combo-yield` 同时校验并原子更新两腿，两个入口都先 dry-run：

```bash
./om option-positions adjust-lot --record-id <lot_id> --strategy combo_yield --leg-role participation_call --yield-enhancement-mode income_upside_enhancement --dry-run
./om option-positions pair-combo-yield --put-record-id <put_lot_id> --call-record-id <call_lot_id> --pair-intent-id <intent_id> --dry-run
```

到期生命周期证据和冲突可直接检查：

```bash
./om option-positions lifecycle list --status waiting_settlement_evidence --include-evidence
./om option-positions lifecycle inspect --case-id <case_id>
```

手工成交文本入账使用 runtime config 路径；确认写入前会打印目标 SQLite，发现 active/default store 已经漂移时会拒绝写入：

```bash
./.venv/bin/python -m src.application.option_intake --config /var/lib/options-monitor/config.hk.json --text "/om -sy open ..." --dry-run
./.venv/bin/python -m src.application.option_intake --config /var/lib/options-monitor/config.hk.json --text "/om -sy open ..." --confirm
```

过期自动平仓使用专用入口，不随 tick 扫描执行：

```bash
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --dry-run
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --confirm
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --confirm --no-send
```

入口会按 runtime config 的 `_generated.market` 过滤 open lots：`config.us.json` 只处理 US 标的，`config.hk.json` 只处理 HK 标的；不会跨市场扫描同一账户下的全部期权 lot。到期 +N 天的 eligible cutoff 按标的市场本地日期计算，US 使用美东时间，HK 使用香港时间。短仓期权还必须有到期后的 OpenD spot 证明已经价外才会自动写入过期平仓；价内/平值或缺少 spot 时会进入 assignment review，等待指派/行权结果。

被 Sell Put 指派后形成的 assigned-stock lot 可只读查看；卖出这类正股使用独立入口，默认仍是 dry-run，确认写入需要 `--confirm`：

```bash
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"assigned-stock","account":"lx"}'
./om option-positions assigned-stock-sale --target-stock-lot-id assigned-stock-assign_xxx --shares 100 --price 105 --trade-time-ms 1780000000000 --dry-run
```

期权收益统计以 `option-performance` / `option_performance_report` 为主入口，支持 MTD、YTD、自然月、自然年和日期范围：

```bash
./om option-performance report --config-key us --account lx --period mtd
./om option-performance report --config-key us --account lx --period ytd --as-of-date 2026-07-17
./om option-performance report --config-key us --account lx --period month --month 2026-06
./om option-performance report --config-key us --account lx --period year --year 2025
./om-agent run --tool option_performance_report --input-json '{"config_key":"us","account":"lx","period":"ytd","as_of_date":"2026-07-17"}'
```

利润、现金和交易活动是三个并列口径：利润看 `pnl`，现金变化看 `cash`，权利金活动看 `activity`。不要把权利金重复加到 PnL，也不要把指派买入正股的本金当作亏损。组合桥接同样分开：`portfolio_pnl_bridge` 对接总资产/PnL 恒等式，`portfolio_cash_bridge` 对接现金余额恒等式。

旧的 `./om option-positions report monthly-income` 和 `monthly_income_report` 仍可用于回滚，但已废弃，不再作为新消费者入口。

### 通知预览

不发送通知，只看最终文本：

```bash
./om notify preview
./om-agent run --tool preview_notification --input-json '{"alerts_path":"output_shared/reports/symbols_alerts.txt","changes_path":"output_shared/reports/symbols_changes.txt","account_label":"lx"}'
```

## 策略模型

### 策略口径

`sell_put.strategy` / `sell_call.strategy` 支持两种口径：

- `return_first`：收益优先。它是收益筛选器，主要用年化收益、单笔净权利金、基础 DTE/strike、流动性、现金或股票覆盖能力筛出“收益条件合格”的候选；它不系统性判断保费是否足够补偿 IV/RV、Delta、事件、跳空、路径压力和组合集中度风险。
- `insurance_underwriting`：承保评估器。它先过滤收益率、单笔净权利金、IV/RV、事件风险、流动性和覆盖能力；通过硬筛后，优先比较 Sell Put 的 strike 安全距离或 Covered Call 的 strike 上行距离，其次比较去重后的承保补偿分，再按价差和 open interest 排序，净权利金仅作最终同分项。默认 profile 是 `insurance_underwriting`。

开仓配置不再接受 `strategy: short_vol`。`short_vol` 仍是 Close Advice、历史仓位和部分 replay 里的持仓 thesis 名称，不作为 Sell Put / Covered Call 的开仓配置值。

`close_advice` 不使用“当前默认策略”直接重判所有历史仓位。它优先使用 lot 上的 `strategy_snapshot`、`yield_enhancement_mode` 或其他开仓策略元数据；只有缺少开仓策略信息时，才 fallback 到当前 symbol 配置或模板默认值。`yield_enhancement_mode` 是持仓/平仓侧的历史字段，本轮不重命名。

### Sell Put

Sell Put 在 `return_first` 下是卖 Put 收益筛选；在 `insurance_underwriting` 下是卖下跌保险的承保评估。

`insurance_underwriting` profile 会把 short put 视为愿意接货前提下的保险承保，先确认收益、波动率边际、事件风险、流动性和现金覆盖，再以 strike 安全距离为第一排序条件、去重后的承保补偿分为第二排序条件：

- `min_dte` / `max_dte`
- `min_strike` / `max_strike`
- `min_iv_rv_ratio`
- `min_iv_minus_rv`
- `reject_event_risk` / `event_source_fail_closed`
- `min_open_interest`
- `min_volume`
- `max_spread_ratio`
- `min_annualized_net_return`
- `min_net_income`

在 `insurance_underwriting` profile 下，收益率和单笔净权利金是准入条件；通过过滤后的候选依次按距离 `max_strike` 的安全空间降序、去重后的承保补偿分降序、价差升序、open interest 降序排序，净权利金仅作最终同分项。

候选过滤之后会叠加账户现金维度的 `cash_reserve` 后过滤。事件源不可用、expiry 前存在财报等事件、缺少 IV/RV、strike、spot 或 multiplier 等关键输入时，承保策略会 fail closed。

### Covered Call

Covered Call 在 `return_first` 下是已有正股上的卖 Call 收益筛选；在 `insurance_underwriting` 下是卖上涨保险的承保评估。

Covered Call 依赖真实持仓上下文。它在风险结构上和 Sell Put 同属 short vol / short gamma，只是现金、持仓和行权方向不同：

- `shares` / `avg_cost` 来自 holdings
- 已被 short call 锁定的股票会从可卖数量里扣掉
- `min_strike_cost_multiplier` 会抬高有效 strike 下限，避免推荐明显低于成本底线的 call
- 默认同样走 `insurance_underwriting` profile，会检查收益、IV/RV、IV-RV、事件风险、流动性和持仓覆盖，并输出短 gamma/vega 与 covered notional 等解释字段

`config.yaml` 里使用 `covered_call`；runtime JSON、CSV 和 trace 的内部策略 key 是 `sell_call`。用户可见名称统一为 `Covered Call`。

### Combo Yield

`combo_yield` 是当前 runtime key；历史 `yield_enhancement` 只作为旧配置、旧 artifact 和既有持仓的兼容读取口径。Combo Yield 和 Sell Put / Covered Call 平行，当前支持两种结构：

- `same_expiry_pair`：默认值，保留原有同 symbol、同到期配对逻辑。
- `staggered_expiry_pair`：一张较早到期的 Short Put 对应一张较晚到期的 Long Call；两腿同 symbol、同 currency、同 multiplier，且 `put strike < call strike`。

`staggered_expiry_pair` 的筛选要点：

- 依赖 `sell_put.enabled=true`；Funding Put 直接复用完整 Sell Put underwriting 后的候选，因此接货边界、现金、事件、收益、IV/RV 与流动性门槛不会因搭配 Long Call 而放宽。
- Put 使用 Sell Put 自己的 DTE 窗口；Call 使用 `combo_yield.call.min_dte/max_dte` 的独立更长期限窗口，并要求 `call.expiration > put.expiration`。
- Call 可配置 `min_strike` / `max_strike` 和 delta 区间；错期结构不额外要求 Call strike 高于 spot，但仍要求 Call strike 高于 Put strike。
- 两腿当前固定为 `1 Put : 1 Call`，multiplier 必须一致。
- 费用按当前费用模型估算：`put_net_credit = Put bid × multiplier - sell fees`，`call_total_cost = Call ask × multiplier + buy fees`。
- 默认 `credit_or_even`：要求 `combo_net_credit = put_net_credit - call_total_cost >= 0`，并要求 `call_cost_to_put_credit <= 1`，等价于 Put 净收入足以覆盖 Call 总成本。
- 错期组合不计算或硬筛组合年化、同到期 breakeven、expected-move scenario 指标；这些跨期限指标不具备同一时间基准。
- 同一 Funding Put 下先选 Call：已融资优先、Call delta 高者优先、资金利用率高者优先、Call DTE 更长者优先，再比较两腿最大 spread、Call OI 和合约标识。
- 通知中的组合组排序：已融资优先、Put 接货安全边界更大、Funding Put underwriting 补偿更好、Call delta 更高、资金利用率更高、Call DTE 更长、执行质量更好、两腿最小 OI 更高，最后按 symbol/合约稳定排序。
- `<report-dir>/<symbol>_combo_yield_pair_diagnostics.csv` 保留 Call 预筛和 Put+Call 配对尝试的逐行通过/拒绝证据及关键经济性指标。

示例中的 Call `60–120 DTE` 只用于说明独立期限窗口，不代表生产最优参数；上线前应以 Shadow Replay / outcome 证据校准：

```json
"combo_yield": {
  "enabled": true,
  "structure_mode": "staggered_expiry_pair",
  "funding_mode": "credit_or_even",
  "min_combo_net_credit": 0,
  "max_call_cost_to_put_credit": 1,
  "call": {
    "min_dte": 60,
    "max_dte": 120,
    "min_delta": 0.10,
    "max_delta": 0.45
  }
}
```

候选通知使用 `candidate_pair_id` 标识推荐组合；真实成交只有在显式提供 `pair_intent_id` 时才会自动归组。若两腿已经分别入账，可用精确 lot id 进行人工确认，命令不会按合约条件猜测仓位：

```bash
./om option-positions pair-combo-yield \
  --put-record-id <put_lot_id> \
  --call-record-id <call_lot_id> \
  --pair-intent-id <intent_id> \
  --dry-run
```

确认写入时，两条 adjustment event 在同一 SQLite 事务中落账；Put 标记为 `funding_put`，Call 标记为 `participation_call`，共同使用 `strategy_group_id=combo_yield:<account>:<pair_intent_id>`。

### Close Advice

`close_advice` 基于本地 `position_lots`、required data、报价和 lot 上的开仓策略快照生成建议，属于 advisory-only 逻辑，不会自动平仓。它回答的是“这张仓位开仓时的 thesis 现在还成立吗”，不是“按当前配置重新评价这张旧仓位”。

当前支持的退出语义：

- 普通 Sell Put / Covered Call：`close`、`hold`、`not_evaluable`
- Short-vol lot：仍按收益捕获决定 `close` / `hold`；IV/RV、Delta、事件和路径风险只作为观察字段
- 收益增强 short put 腿：`close_put_keep_call` 或 `hold_put_keep_call`
- 收益增强 long call 腿：`sell_call_take_profit`、`hold_call`、`hold_call_as_convexity`、`sell_call_salvage`、`hold_to_expiry_or_expire`

Short option 当前统一按收益捕获逻辑评估；`remaining_premium`、手续费后的 `realized_if_close`、`buy_to_close_cost` 和剩余年化用于解释买回经济性。缺少 RV/IV/Delta 不会覆盖已完成的定价判断。Sell Put 默认可接货，Covered Call 默认可被行权卖出正股。历史报告里的 `risk_exit` 仅保留只读展示兼容，不再映射为当前可执行平仓动作。

Short-vol 行还会输出 `remaining_stress_loss`、`remaining_reward_to_stress_loss` 和 `close_calibration_status`，用于离线校准“剩余收益 / 剩余压力风险”。替代机会只接受 lot 或 `strategy_snapshot` 中显式提供的 `replacement_annualized_return`；系统不会从候选数量推测替代交易。显式把 `assignment_acceptable` / `called_away_acceptable` 设为 false 时只标记人工复核，不自动生成平仓动作。

`strategy_exit_mode` 是平仓动作映射的状态机入口：普通 short option 使用 `standard_short_option`，收益增强 put 腿使用 `yield_enhancement_put_leg`，收益增强 long call 腿使用 `yield_enhancement_long_call_leg`。这些是持仓/平仓侧历史动作字段，本轮不重命名；渲染层只展示已决策的动作，不改变平仓判断。

收益增强组合会额外输出 `put_leg_realized_if_close`、`combo_call_cost`、`combo_call_value_if_close`、`combo_net_locked_if_close_put_keep_call`、`combo_net_if_close_both` 和 `combo_cost_basis_status`。只有配对 call 存在、成本和报价可计算时，put 腿才会显示 `close_both_optional`。

## 配置心智模型

### Runtime config 与 authoring config

运行时入口：

- `config.us.json`
- `config.hk.json`

推荐编辑源：

- `config.yaml`

持仓和本地仓位相关数据配置：

- 默认不需要单独配置文件；SQLite 固定在 `<runtime_root>/output_shared/state/option_positions.sqlite3`
- `portfolio.runtime.json` 默认不需要；只在 external_holdings 需要声明 Feishu 表引用 env 名时使用

原则上：

- 编辑 `config.yaml`
- 用 `om config build --source yaml --market us|hk` 生成 runtime config
- 用 `om config validate --source yaml --market us|hk` 检查 YAML override 与代码默认值合并后的配置
- 用 `om config validate --config-path ... --market us|hk` 检查 runtime config、市场时区契约和生成指纹
- 用 `config_validate` 做不含生成指纹检查的基础只读配置校验
- 用 `om settings doctor` 检查 env-file、Feishu Bot 和写入开关
- 遇到安装或运行问题时，用 `om support bundle --config-key us` 生成脱敏诊断包

### 数据来源

默认最小组合通常是：

- 行情与期权链：OpenD / Futu API
- 持仓与现金：OpenD / Futu API
- `option_positions`：本地 SQLite
- 通知：默认关闭，按需显式配置

Feishu 常见只用于这些场景：

- `external_holdings` / `holdings` 数据源
- 飞书通知

### 多账户

多账户的基本约定：

- 账户标签使用小写，例如 `lx`、`sy`
- 默认账户列表来自 runtime config 顶层 `accounts`
- 单账户和多账户走同一条 tick 链路
- 多账户问题先按账户维度排查，不要默认认为是全局 gate

## Agent / Tool Gateway 使用指南

这个仓库把文档拆成两层：

- [AGENTS.md](AGENTS.md)：给本地 agent 首先加载的短说明书，记录安全红线、入口层级和模块归属
- [docs/AGENT_GETTING_STARTED.md](docs/AGENT_GETTING_STARTED.md)：Tool Gateway 接入的最短路径
- [docs/AGENT_WIKI.md](docs/AGENT_WIKI.md)：给本地 agent 深入执行任务时看的手册，包含工具选择、Research、排障 playbook 和验证矩阵
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：当前系统架构和入口边界
- [docs/INBOUND_CONTROL.md](docs/INBOUND_CONTROL.md)：`./om assistant handle` 远程消息入口的当前安全边界
- [docs/OM_AGENT_CAPABILITY_MAP.md](docs/OM_AGENT_CAPABILITY_MAP.md)：Tool Gateway 与 Inbound Assistant 的能力边界、LLM 暴露面和验证方式

安装 agent 插件：

```bash
bash scripts/install_agent_plugin.sh
./om-agent spec
```

常用只读工具：

```bash
./om status --config-key us
./om runs --limit 10
./om logs --run-id <run-id> --lines 50
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_runs --input-json '{"limit":10}'
./om-agent run --tool runtime_logs --input-json '{"run_id":"<run-id>","kind":"tool","lines":50}'
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
./om-agent run --tool analysis_catalog --input-json '{"config_key":"us"}'
./om-agent run --tool analysis_query --input-json '{"config_key":"us","sql":"select month, account, realized_pnl_cny from monthly_income_return_summary order by month, account"}'
./om-agent run --tool close_advice_read --input-json '{"config_key":"us","query":{"option_type":"call","side":"long"}}'
```

受控远程消息入口：

```bash
./om assistant handle --text '/positions sy' --sender local --channel local --message-id local-1
./om inbound feishu --input-file feishu_event.json --format text
./om inbound feishu-ws --check
./om assistant commands --format text
./om assistant capabilities
./om assistant llm-check
./om assistant model current
```

显式命令和 pending-operation 回复进入确定性 Control；其他文本在 `assistant.copilot.enabled=true` 时进入唯一的 read-first `om_chat` Copilot Scene。Copilot 常规执行只能使用 Host 投影的纯读工具；`portfolio-management` 读取默认关闭，只有同时设置 `assistant.enabled=true`、`assistant.copilot.enabled=true` 和 `assistant.copilot.toolsets.portfolio=true` 才会把 `portfolio_query` 投影给 Copilot。这个配置不影响 `./om-agent` 的 `portfolio_query` 工具注册。遇到明确支持的变更请求时，Copilot 最多请求一个确定性 Control preview，生成 pending operation 给人确认。它不能自己确认、取消、apply，也不能直接写配置、仓位/交易、通知、服务控制、升级或 broker state。链路保留 sender allowlist、message_id 幂等和 SQLite audit。当前 CLI namespace 仍是 `./om assistant ...`，例如 `/status`、`/positions sy`、`/income 2026-05`、`/model`、`/model use deepseek-default`、`/record-open ...`、`/record-close ...`、`/record-expiry <富途通知>`、`设置 09898 covered call min strike 85`。`./om-agent` 是外部 Agent 使用的 Tool Gateway，不是 OM 自己的 Agent。架构边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，能力边界见 [docs/OM_AGENT_CAPABILITY_MAP.md](docs/OM_AGENT_CAPABILITY_MAP.md)，渠道契约见 [docs/INBOUND_CONTROL.md](docs/INBOUND_CONTROL.md)。

本地 Copilot 自由问答和 deterministic eval 使用同一个 `om_chat` Scene：

```bash
./om copilot run --text "NVDA 为什么没有通过筛选" --config-key us
./om copilot eval --fixture current_option_exposure_model_ready --text "当前期权风险暴露集中在哪些标的" --model-turn-json-file tests/fixtures/copilot/current_option_exposure_model_turns.json
./om copilot eval --fixture june_income_attribution_basic --text "6月收益主要来自哪里" --model-turn-json-file tests/fixtures/copilot/june_income_attribution_model_turns.json
```

`run` 默认读取本地只读工具证据回答，不直接写入；通道入口的显式变更请求也只产出 Control preview。没有显式模型配置时，不会降级成普通聊天。`eval` 只消费固定 fixture 或显式 eval-only model turn，用来回归 answer-quality 边界。

离线复盘（`./om research`）与 Inbound Assistant 是分开的模块，也不暴露为 `./om-agent` tool。它只做本地证据收集、Shadow Replay dataset 维护和 Strategy Lab 只读实验；需要写本地 artifact 时必须显式加 `--write`，不会改 runtime config、交易状态或通知。详细命令见 [docs/SHADOW_REPLAY_RUNBOOK.md](docs/SHADOW_REPLAY_RUNBOOK.md) 和 [docs/STRATEGY_LAB_DESIGN.md](docs/STRATEGY_LAB_DESIGN.md)。

写工具门禁：

```bash
OM_AGENT_ENABLE_WRITE_TOOLS=true ./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"edit","confirm":true,...}'
```

规则建议：

- 解释、调查、代码阅读类任务先读文件
- 优先 `./om-agent`，其次 `./om`
- 不要把 `python3 scripts/...` 当成第一入口
- 发送通知、写 Feishu、改生产配置、删除运行产物前必须有明确意图
- 有 `dry-run`、`validate`、`healthcheck`、`runtime_status` 时先走低风险路径

补充说明：

- `./om-agent spec` 才是当前公开工具清单的准确信源

## 定时与长驻任务

README 只记录公开入口和边界。生产 cron id、长驻服务启停和更细的运行手册见 [RUNBOOK.md](RUNBOOK.md)。

| 任务 | 推荐入口 | 运行方式 | 主要副作用 |
|---|---|---|---|
| 期权监控 / 扫描通知 | `./om run tick-cron --market hk --accounts lx sy --timeout 600` / `./om run tick-cron --market us --accounts lx sy --timeout 600` | cron 每 10 分钟唤醒，代码内判断业务窗口 | 写本地报告、portfolio capacity shadow 和运行状态；启用 Close Advice 时额外写 reallocation shadow；并按通知策略发送扫描/建议消息 |
| Strategy Lab 证据记录 | `./om service render --include-strategy-lab-recorder ...` 生成的 `options-monitor-strategy-lab-*.timer` | 独立低频 timer | 写本地 Shadow Replay dataset、mark path、outcome facts、required-data cache 和 receipt；不发通知、不改生产配置 |
| 调度状态检查 | `./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'` | 定时或人工检查 | 只读 |
| 自动交易监听 / 入账 | `./.venv/bin/python -m src.application.trades.auto_intake --config config.us.json --mode apply --yes` | 长驻进程 | 写本地 `option_positions`、intake state/status，并按 receipt 配置发送回执 |
| 过期自动平仓 | `./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --confirm` | 低频定时或人工触发，按 runtime config market 只处理对应市场标的；短仓需有价外 spot 证据 | 写本地 `option_positions`、运行状态，并按 receipt 配置发送任务级回执 |
| 版本检查 | `./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'` | 低频只读 | 只读 |
| 版本更新预览 | `./om-agent run --tool version_update --input-json '{"bump":"patch"}'` | dry-run | 不写 `VERSION` |

不要把 `version_update apply=true` 放进固定频率任务。它会递增本地 `VERSION`，不等于发布流程。

`tick-cron` 在拿到锁后会先校验 runtime config 的生成指纹；如果
`config.yaml` 更新后没有重新 build，
任务会以 `[CONFIG_ERROR]` 失败并打印 `./om config build ... --output ...`。`--allow-stale-config`
只作为临时应急绕过使用。

## 副作用边界

| 命令 / 工具 | 写本地 | 写远端 | 发通知 |
|---|---:|---:|---:|
| `./om-agent run --tool config_validate ...` | 否 | 否 | 否 |
| `./om-agent run --tool healthcheck ...` | 否 | 否 | 否 |
| `./om-agent run --tool runtime_status ...` | 否 | 否 | 否 |
| `./om-agent run --tool analysis_query ...` | 否 | 否 | 否 |
| `./om-agent run --tool scan_opportunities ...` | 是 | 否 | 否 |
| `./om-agent run --tool get_close_advice ...` | 是 | 否 | 否 |
| `./om-agent run --tool query_cash_headroom ...` | 是 | 否 | 否 |
| `./om run tick --config ... --no-send` | 是 | 可能 | 否 |
| `./om run tick --config ...` | 是 | 可能 | 是 |
| `./om run tick-cron --market ...` | 是 | 可能 | 是 |
| `./om research strategy-lab update --write ...` | 是 | 否 | 否 |
| `./.venv/bin/python -m src.application.trades.auto_intake --mode apply --yes` | 是 | 否 | 是，默认发送入账回执 |
| `./.venv/bin/python -m src.application.option_intake --config ... --confirm` | 是 | 否 | 否 |
| `./om option-positions auto-close-expired --confirm` | 是 | 否 | 是，默认发送过期自动平仓回执 |
| `./om option-positions auto-close-expired --confirm --no-send` | 是 | 否 | 否 |

## 排障顺序

建议顺序：

1. 先读相关代码、配置文档和测试。
2. 先跑 `config_validate`、`healthcheck`、`runtime_status`。
3. 需要解释候选时先用 `candidate_rank_explain` / `candidate_filter_explain`。
4. 只有在静态信息不足时，才跑真实 tick 或其他会写状态的命令。

常见问题先看哪里：

| 症状 | 先看什么 |
|---|---|
| 配置校验失败 | `CONFIGS.md`、`CONFIGURATION_GUIDE.md`、`./om config explain` |
| 某个 symbol 没进候选 | `candidate_filter_explain`、对应 `candidate_filter_trace.jsonl` |
| 两个账户结果看起来串了 | `scheduler_status`、账户级 source 配置、账户级状态文件 |
| 通知没发出来 | `preview_notification`、`notifications.channel`、secret 文件、通知 route |
| 自动交易监听没有回执 | `runtime_status` 的 `trade_intake.summary`、`auto_trade_intake_status.json`、`trade_intake.receipt.enabled`、通知 route |
| 自动交易监听因认证停止 | 检查 `auto_trade_intake_status.json` 的 `stage=auth_required` / `error_code=OPEND_NEEDS_PHONE_VERIFY`；完成 OpenD 手机验证后再人工启动服务，退出码 `78` 不会被 systemd 自动重启 |
| 过期自动平仓没有回执 | `runtime_status` 最新 run 里的 `auto_close_receipt` / `expired_position_maintenance`、`option_positions.auto_close.receipt.enabled`、通知 route；每日维护 cron 重跑时还要看 `receipt_key` 是否已确认发送 |
| 平仓建议异常 | `prepare_close_advice_inputs`、本地 `option_positions`、required data |

## 文档导航

- [docs/INDEX.md](docs/INDEX.md)：文档总索引，找不到入口时先看这里
- [CONFIGS.md](CONFIGS.md)：canonical config、配置来源和构建规则
- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)：字段说明、数据来源和配置边界
- [RUNBOOK.md](RUNBOOK.md)：运维巡检、定时任务、应急操作
- [docs/RELEASE_PROCESS.md](docs/RELEASE_PROCESS.md)：版本发布流程
- [docs/INSTALL.md](docs/INSTALL.md)：安装方式、release 目录布局和 installer 安全契约
- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)：普通用户首次运行路径
- [docs/DEPLOY_LINUX_MAC.md](docs/DEPLOY_LINUX_MAC.md)：Linux / macOS 服务化部署
- [docs/AGENT_GETTING_STARTED.md](docs/AGENT_GETTING_STARTED.md)：Tool Gateway 快速开始
- [docs/AGENT_WIKI.md](docs/AGENT_WIKI.md)：本地 agent 任务手册
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：当前系统架构和入口边界
- [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)：`./om-agent` Tool Gateway JSON 合同
- [docs/INBOUND_CONTROL.md](docs/INBOUND_CONTROL.md)：飞书、微信、Hermes 等远程消息入口的安全控制层
- [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md)：`om-agent` 工具说明
- [docs/candidate_strategy.md](docs/candidate_strategy.md)：候选生成和策略边界
- [docs/OPTION_POSITIONS_REPAIR.md](docs/OPTION_POSITIONS_REPAIR.md)：option positions 修复
- [docs/CLOSE_ADVICE_CONTRACT.md](docs/CLOSE_ADVICE_CONTRACT.md)：Close Advice 与 capital reallocation shadow 合同
- [tests/README.md](tests/README.md)：测试分层和运行方式

## 风险提示

本工具只做监控、筛选、报告和提醒，不构成投资建议。任何下单前都应自行复核价格、流动性、保证金、仓位暴露和事件风险。

## 期权监控通知与查询（默认关闭）

这套功能只做监控、报告和提醒，不会自动下单。它复用同一套 canonical 策略扫描、现有通知 route 和现有 10 分钟 timer，不新增第二个 scanner 或 sender。

启用配置仍是：

```json
{
  "notifications": {
    "daily_brief": {
      "enabled": false,
      "max_actions_per_priority": 5,
      "max_candidates_per_strategy": 3,
      "max_rejection_reasons": 5
    }
  }
}
```

启用后的节奏：

- timer 仍每 10 分钟唤醒，但只有市场当地 `09:40`、有效整点、有效半点和 `15:50` 执行策略扫描；`09:30` 不扫描，港股午休不扫描。
- `09:40`、有效整点和 `15:50` 固定发送完整报告；没有候选也发送持仓和资金，不退化为一句心跳。
- 有效半点发现当日尚未送达的新普通候选时，立即发送新增候选通知；没有新增候选则安静。
- 固定报告点同时发现新增候选时，只发送一份完整报告，确认送达后再把其中候选记为已提醒。
- pipeline 失败不会覆盖最近成功快照。固定点失败会明确说明“未形成可靠结果”，不会误报成“本轮无候选”。
- provider 失败时保留原消息、delivery key 和 message hash；后续 10 分钟唤醒只做 delivery-only 精确重试，不重新扫描。

固定报告示例：

```text
# OM · 决策简报 · lx

状态｜14:00 批次
市场｜港股
数据｜香港 14:00 / 北京 14:00

## 当前候选

**1｜9992.HK｜Sell Put｜08-28 HK$145 Put（首选）**
指标｜年化 25.2% · Delta -0.23

## 持仓

**0700.HK｜Sell Put｜07-30 HK$440 Put｜继续观察**

## 资金
现金总额｜HK$480,000.00
可用于期权开仓｜HK$225,000.00
9992.HK 08-28 HK$145 Put｜按当前现金最多 8 手

## 提醒
多个 Sell Put 候选共享同一现金额度，手数不能相加
```

资金只显示现金总额、可用于期权开仓的资金和每个候选的容量；不显示总资产、NAV 或证券市值。未知数据明确显示“暂不可用”，不会伪装成 `0`。候选事件事实仍只读取同一 run 的 `event_snapshot.json`；证据缺失或降级时显示“暂时无法确认”，不会伪装成确认无事件，也不会改变候选排序或身份。

随时查询读取最近一次**成功扫描**的 current 快照，不读取最后一次发送消息，也不修改 delivery state：

```bash
# 全部启用账户和市场
./om daily-brief latest

# 按账户或市场筛选
./om daily-brief latest --account lx
./om daily-brief latest --market HK
./om daily-brief latest --account lx --market US --json

# 运维历史读取仍要求明确账户和市场
./om daily-brief day --account lx --market US --date 2026-07-21
./om daily-brief day --account lx --market US --date 2026-07-21 --revision 0 --json
```

只读 Agent Tool：

```bash
./om-agent run --tool daily_decision_brief_read --input-json '{}'
./om-agent run --tool daily_decision_brief_read --input-json '{"market":"HK"}'
./om-agent run --tool daily_decision_brief_read --input-json '{"account":"lx","market":"US"}'
```

自然语言入口包括“期权监控”“最新期权报告”“港股期权”“美股期权”“lx 期权”和“sy 期权”。Markdown 不展示 revision、内部 identity、broker contract code、raw enum、ISO 时间或内部路径；这些事实仍保留在结构化审计数据中。

`notifications.daily_brief.enabled` 默认仍为 `false`。生产配置、delivery pointer 迁移、真实发送 canary、release 和远端升级都需要单独审批。
