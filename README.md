# options-monitor

`options-monitor` 是一个本地运行的期权收益与承保决策系统。它把行情、持仓、现金、期权仓位、策略阈值、账本和通知串起来，帮助用户判断什么时候可以卖期权收保费、哪张合约值得卖、风险预算是否合适、已开仓位是否还应该持有，以及这套策略最终赚得是否科学。

主要服务四个用户功能：

- `Sell Put`：卖 Put 机会扫描。收益优先模式下是收益筛选；承保模式下是卖下跌保险的评估，默认接货是可接受结果。
- `Covered Call`：已有正股上的卖 Call 机会扫描。收益优先模式下是收益筛选；承保模式下是卖上涨保险的评估，默认被行权卖出正股是可接受结果。
- `combo_yield`：当前 Combo Yield runtime key；作为平行开仓策略配对 short put + long call，用可接受的接货义务融资上行参与。
- `close_advice`：已有期权 lot 的生命周期管理，按开仓时记录的策略语义给出止盈、风险退出、继续持有或无法评估的平仓参考。

它不是自动交易系统，也不会替你下单。它的输出是 advisory-only，给出便于人工复核的候选、拒绝原因、平仓建议和复盘证据。

## 产品框架

完整产品域、模块定义和模块依赖见 [docs/PRODUCT_ARCHITECTURE.md](docs/PRODUCT_ARCHITECTURE.md)。

系统支持两种策略口径。它们不是同一种风控深度：

| 策略口径 | 本质 | 推荐含义 |
|---|---|---|
| `return_first` 收益优先 | 收益筛选器 | 收益条件合格，基础交易约束合格 |
| `insurance_underwriting` 承保 | 承保评估器 | 这张保单的保费、边界和基础风险相对合理 |

收益优先模式主要看 DTE、strike 范围、年化收益、单笔净收入、流动性、价差、Sell Put 现金是否够、Covered Call 股票是否够覆盖。它不系统性判断保费是否足够补偿 IV/RV 和事件风险。

承保模式才是“开保险公司”的主逻辑。它系统性看收益率、单笔净收入、IV/RV、IV-RV、事件风险、流动性、Sell Put 现金覆盖和 Covered Call 股票覆盖，用来判断当前保费是否足够，再按 strike 安全距离和保费边际推荐候选。

完整产品闭环：

```text
扫描机会 -> 生成建议 -> 人工决策 -> 记录开仓 -> 持仓监控
-> 平仓建议 -> 记录结果 -> 收益统计 -> 扫描质量复盘 -> 人工策略复盘证据
```

`Sell Put` / `Covered Call` 看当前配置里的策略意图生成开仓候选。`combo_yield` 按 Combo Yield 独立策略处理，不继承 Sell Put / Covered Call 的 underwriting gate。`close_advice` 比较特殊：它优先读取 lot 上的开仓策略快照；只有旧仓位没有策略快照时，才 fallback 到当前 symbol 配置或默认策略，并在输出中保留 `strategy_source`。

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

无参数安装会解析并安装最新 GitHub release，例如 `v1.2.339`；不会安装浮动 `main` 分支。需要复现、回滚或固定生产版本时，显式指定 release tag：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version v1.2.339 --prefix "$HOME/apps/options-monitor"
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

- **`collect`**：从已有扫描 run 生成脱敏证据包，用于本地分析或 handoff。
- **`shadow-replay`**：把历史扫描候选、拒绝原因和后续价格路径落地为本地 dataset，做反事实复盘与参数影响评估。
- **`strategy-lab`**：在 Shadow Replay 之上做策略进化实验，生成只读的 candidate-impact scorecard 和 advisory-only proposal。
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

### Symbols

推荐写入入口是 `config.yaml`，命令默认 dry-run，带 `--apply` 才会写入；需要同步生成运行时配置时加 `--rebuild-runtime-root`：

```bash
./om config symbol set --config-yaml config.yaml --market hk --symbol 09898 \
  --covered-call-enabled true --covered-call-min-strike 85 --sell-put-enabled false \
  --apply --rebuild-runtime-root .
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

修复历史 lot 的策略元数据也走 `adjust-lot`，先 dry-run，再确认写入审计事件：

```bash
./om option-positions adjust-lot --record-id <lot_id> --strategy combo_yield --leg-role enhancement_call --yield-enhancement-mode income_upside_enhancement --dry-run
```

到期生命周期证据和冲突可直接检查：

```bash
./om option-positions lifecycle list --status waiting_settlement_evidence --include-evidence
./om option-positions lifecycle inspect --case-id <case_id>
```

手工成交文本入账使用 runtime config 路径；确认写入前会打印目标 SQLite，发现 active/default store 已经漂移时会拒绝写入：

```bash
python3 -m src.application.option_intake --config /var/lib/options-monitor/config.hk.json --text "/om -sy open ..." --dry-run
python3 -m src.application.option_intake --config /var/lib/options-monitor/config.hk.json --text "/om -sy open ..." --confirm
```

过期自动平仓使用专用入口，不随 tick 扫描执行：

```bash
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --dry-run
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --confirm
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --confirm --no-send
```

入口会按 runtime config 的 `_generated.market` 过滤 open lots：`config.us.json` 只处理 US 标的，`config.hk.json` 只处理 HK 标的；不会跨市场扫描同一账户下的全部期权 lot。到期 +N 天的 eligible cutoff 按标的市场本地日期计算，US 使用美东时间，HK 使用香港时间。短仓期权还必须有到期后的 OpenD spot 证明已经价外才会自动写入过期平仓；价内/平值或缺少 spot 时会进入 assignment review，等待指派/行权结果。

月度收益报表：

```bash
./om option-positions report monthly-income --broker 富途 --account lx --month 2026-04
./om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"lx","month":"2026-04"}'
```

### 通知预览

不发送通知，只看最终文本：

```bash
./om notify preview
./om-agent run --tool preview_notification --input-json '{"alerts_path":"output_shared/reports/symbols_alerts.txt","changes_path":"output_shared/reports/symbols_changes.txt","account_label":"lx"}'
```

## 策略模型

### 策略口径

`sell_put.strategy` / `sell_call.strategy` 支持两种口径：

- `return_first`：收益优先。它是收益筛选器，主要用年化收益、单笔净收入、基础 DTE/strike、流动性、现金或股票覆盖能力筛出“收益条件合格”的候选；它不系统性判断保费是否足够补偿 IV/RV、Delta、事件、跳空、路径压力和组合集中度风险。
- `insurance_underwriting`：承保评估器。它先过滤收益率、单笔净收入、IV/RV、事件风险、流动性和覆盖能力，再按保费边际、strike 安全距离、净收入和价差排序。默认 profile 是 `insurance_underwriting`。

开仓配置不再接受 `strategy: short_vol`。`short_vol` 仍是 Close Advice、历史仓位和部分 replay 里的持仓 thesis 名称，不作为 Sell Put / Covered Call 的开仓配置值。

`close_advice` 不使用“当前默认策略”直接重判所有历史仓位。它优先使用 lot 上的 `strategy_snapshot`、`yield_enhancement_mode` 或其他开仓策略元数据；只有缺少开仓策略信息时，才 fallback 到当前 symbol 配置或模板默认值。`yield_enhancement_mode` 是持仓/平仓侧的历史字段，本轮不重命名。

### Sell Put

Sell Put 在 `return_first` 下是卖 Put 收益筛选；在 `insurance_underwriting` 下是卖下跌保险的承保评估。

`insurance_underwriting` profile 会把 short put 视为愿意接货前提下的保险承保，先确认收益、波动率边际、事件风险、流动性和现金覆盖，再把收益指标与 strike 安全距离纳入排序：

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

在 `insurance_underwriting` profile 下，收益率和单笔净收入是准入条件；通过过滤后的候选会按保费边际、距离 `max_strike` 的安全空间、净收入和价差排序。

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

`combo_yield` 是当前 runtime key；历史 `yield_enhancement` 只作为旧配置、旧 artifact 和既有持仓的兼容读取口径。产品语义上，Combo Yield 和 Sell Put / Covered Call 平行。它配对同 symbol、同到期、同乘数、put strike < call strike 的 short put + long call，寻找“可接受接货义务能够合理融资上行参与”的组合。

要点：

- 依赖 `sell_put.enabled=true`
- put 腿使用 Sell Put 的接货价格边界：`min_strike` 可空，`max_strike` 与 spot 共同形成上界
- call 腿使用结构价格边界：`call.strike >= max(spot, call.min_strike)`，`call.max_strike` 可选
- 不继承 Sell Put 的 `insurance_underwriting` RV、event 或 underwriting gate
- 启用收益增强后会为 long call 侧规划 required data，即使没有启用 Covered Call 扫描
- 核心约束包括 `funding_mode`、`min_combo_net_credit`、`min_net_credit_annualized`、`max_call_cost_to_put_credit`、`min_net_credit_retention`、`max_combo_spread_ratio` 和 call delta 区间
- 默认要求扣除 long call 成本和手续费后的净权利金年化不低于 8%

### Close Advice

`close_advice` 基于本地 `position_lots`、required data、报价和 lot 上的开仓策略快照生成建议，属于 advisory-only 逻辑，不会自动平仓。它回答的是“这张仓位开仓时的 thesis 现在还成立吗”，不是“按当前配置重新评价这张旧仓位”。

`optimizer_switch` 必须带有本地候选报告中的替代候选身份字段，例如 `alternative_symbol`、`alternative_contract_symbol`、`alternative_expiration` 和 `alternative_source_path`；没有明确替代候选时不能把“换仓”当作可执行建议。

当前支持的退出语义：

- 普通 Sell Put / Covered Call：`close`、`hold`、`not_evaluable`
- Short-vol lot：IV/RV edge 丢失、事件风险或路径风险可以触发 `risk_exit`
- 收益增强 short put 腿：`close_put_keep_call` 或 `hold_put_keep_call`
- 收益增强 long call 腿：`sell_call_take_profit`、`hold_call`、`hold_call_as_convexity`、`sell_call_salvage`、`hold_to_expiry_or_expire`

收益优先开仓的仓位主要按收益捕获逻辑评估。波动率溢价开仓的仓位才按承保 thesis 是否失效评估，例如 IV edge、事件风险、路径风险和风险预算是否恶化。非盈利买回不是 short-vol Sell Put / Covered Call 的默认强平理由：Sell Put 默认可接货，Covered Call 默认可被行权卖出正股。

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

Slash 只读查询会直接执行；非 slash、非确认类自然语言默认返回 `NATURAL_LANGUAGE_REBUILDING`，不会自动调用工具或降级成普通 LLM 回复。显式设置 `assistant.copilot.enabled=true` 后，自由文本只会进入 Copilot channel gate；当前没有 channel-ready scene，因此返回受控 `not_ready`，仍不调用旧 planner 或工具。写操作必须先返回预览并等待确认。链路带 sender allowlist、message_id 幂等和 SQLite audit。Inbound command facade 默认开启，当前 CLI namespace 仍是 `./om assistant ...`，例如 `/status`、`/positions sy`、`/income 2026-05`、`/model`、`/model use deepseek-default`、`/record-open ...`、`/record-close ...`、`设置 09898 covered call min strike 85`。`./om-agent` 是 Tool Gateway，不是 OM 自己的 Agent；`./om assistant handle` 是受控 Inbound Assistant 消息入口，不是自由问答 Copilot。当前架构边界以 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 为准，能力边界和 Inbound LLM 可见/可执行范围以 [docs/OM_AGENT_CAPABILITY_MAP.md](docs/OM_AGENT_CAPABILITY_MAP.md) 为准；接飞书、微信或 Hermes 前先看 [docs/INBOUND_CONTROL.md](docs/INBOUND_CONTROL.md)。

本地 Copilot v2 自由问答入口只用于本地/eval 只读验证，覆盖诊断、收益归因和当前暴露；当前不直接接飞书、微信或 Hermes 渠道：

```bash
./om copilot run --text "NVDA 为什么没有通过筛选" --config-key us
./om copilot eval --scene current_option_exposure --fixture current_option_exposure_model_ready --text "当前期权风险暴露集中在哪些标的" --model-action-json-file tests/fixtures/copilot/current_option_exposure_model_action.json
./om copilot eval --scene monthly_income_attribution --fixture june_income_attribution_basic --text "6月收益主要来自哪里" --model-action-json-file tests/fixtures/copilot/june_income_attribution_model_action.json
```

`run` 会读取本地只读工具证据；没有显式模型配置时，不会降级成普通聊天。`eval` 只消费固定 fixture 或显式 eval-only model action，用来回归 answer-quality 边界。

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
| 期权监控 / 扫描通知 | `./om run tick-cron --market hk --accounts lx sy --timeout 600` / `./om run tick-cron --market us --accounts lx sy --timeout 600` | cron 每 10 分钟唤醒，代码内判断业务窗口 | 写本地报告和运行状态，并按通知策略发送扫描/建议消息 |
| Strategy Lab 证据记录 | `./om service render --include-strategy-lab-recorder ...` 生成的 `options-monitor-strategy-lab-*.timer` | 独立低频 timer | 写本地 Shadow Replay dataset、mark path、outcome facts、required-data cache 和 receipt；不发通知、不改生产配置 |
| 调度状态检查 | `./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'` | 定时或人工检查 | 只读 |
| 自动交易监听 / 入账 | `python3 -m src.application.trades.auto_intake --config config.us.json --mode apply --yes` | 长驻进程 | 写本地 `option_positions`、intake state/status，并按 receipt 配置发送回执 |
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
| `./om-agent run --tool scan_opportunities ...` | 是 | 否 | 否 |
| `./om-agent run --tool get_close_advice ...` | 是 | 否 | 否 |
| `./om-agent run --tool query_cash_headroom ...` | 是 | 否 | 否 |
| `./om run tick --config ... --no-send` | 是 | 可能 | 否 |
| `./om run tick --config ...` | 是 | 可能 | 是 |
| `./om run tick-cron --market ...` | 是 | 可能 | 是 |
| `./om research strategy-lab update --write ...` | 是 | 否 | 否 |
| `python3 -m src.application.trades.auto_intake --mode apply --yes` | 是 | 否 | 是，默认发送入账回执 |
| `python3 -m src.application.option_intake --config ... --confirm` | 是 | 否 | 否 |
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
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：主要模块边界
- [tests/README.md](tests/README.md)：测试分层和运行方式

## 风险提示

本工具只做监控、筛选、报告和提醒，不构成投资建议。任何下单前都应自行复核价格、流动性、保证金、仓位暴露和事件风险。
