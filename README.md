# options-monitor

期权监控与提醒工具，面向 Sell Put / Covered Call（日常卖 Put、备兑卖 Call）工作流。支持美股/港股、多账户、定时扫描、候选筛选排序、提醒发送，以及无候选时的监控心跳通知。

本文是用户手册，只覆盖安装、配置、5 分钟跑通、常用命令和排障入口。策略细节、数据 schema、配置契约和运维流程分别见文末导航。

## 你能用它做什么

- 扫描 watchlist 中的标的期权链。
- 按 DTE、行权价、收益门槛、流动性和事件标注筛选候选。
- 为 Sell Put 检查现金担保能力。
- 为 Covered Call 检查可覆盖股数。
- 生成候选 CSV、摘要、提醒文本。
- 按账户发送通知；没有候选时也发送一条“监控正常触发，本轮无候选”的心跳消息。

## 安装

要求：

- Python 3.10+
- 能访问行情源，例如 Yahoo 或 OpenD/Futu，按你的配置决定
- 如需通知或持仓上下文，需要准备对应的飞书/portfolio-management 配置

首次安装：

```bash
git clone <repo-url> options-monitor
cd options-monitor
./run_watchlist.sh
```

`run_watchlist.sh` 会自动创建 `.venv` 并安装 `requirements.txt` 中的依赖。

如果你只想手动准备环境：

```bash
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt
```

## 配置

线上推荐将真实运行配置放在仓库外管理，仓库内只保留 `configs/examples/*.json` 模板。例如：

```text
/opt/options-monitor/configs/config.us.json
/opt/options-monitor/configs/config.hk.json
/opt/options-monitor/secrets/portfolio.feishu.json
```

初始化时可从模板复制到仓外路径：

```bash
mkdir -p /opt/options-monitor/configs /opt/options-monitor/secrets
cp configs/examples/config.example.us.json /opt/options-monitor/configs/config.us.json
cp configs/examples/config.example.hk.json /opt/options-monitor/configs/config.hk.json
cp configs/examples/portfolio.feishu.example.json /opt/options-monitor/secrets/portfolio.feishu.json
```

开发机临时运行也可以复制到仓内同名文件；这些文件已被 `.gitignore` 忽略，不提交 Git。

日常只编辑 canonical runtime config：

- `/opt/options-monitor/configs/config.us.json`
- `/opt/options-monitor/configs/config.hk.json`

多账户列表统一写在配置的顶层 `accounts` 字段中，例如 `["lx", "sy"]`。没有显式传 `--accounts` 的辅助脚本会优先使用这个字段。

派生配置的来源、同步和禁止手工维护规则见 `CONFIGS.md`。

配置校验：

```bash
./.venv/bin/python scripts/validate_config.py --config /opt/options-monitor/configs/config.us.json
./.venv/bin/python scripts/validate_config.py --config /opt/options-monitor/configs/config.hk.json
```

## 外部服务与凭证配置

本项目会按配置使用外部服务。首次部署时，建议先按下面清单准备好本地配置和凭证；所有真实凭证都不要提交到 Git。

### 1) 飞书多维表（持仓与期权占用）

用途：

- 读取 `holdings` 表：现金、股票持仓、成本价，用于 Sell Put 现金余量和 Covered Call 可覆盖股数。
- 读取/维护 `option_positions` 表：已卖出的 put/call 占用，用于 cash-secured put 和 covered call 风控。

配置位置：

- runtime config 的 `portfolio.pm_config`。
- 新部署推荐指向仓外的 `/opt/options-monitor/secrets/portfolio.feishu.json`；可从 `configs/examples/portfolio.feishu.example.json` 复制后填写。
- 旧部署仍可继续使用 `../portfolio-management/config.json`，当前脚本默认值也保留这个兼容路径。

需要准备：

- Feishu App 的 `app_id` / `app_secret`。
- `holdings` 表的 `app_token/table_id`。
- `option_positions` 表的 `app_token/table_id`。
- 表字段需要符合 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md) 里的字段说明。

注意：

- 不要把 `app_secret`、tenant token、user token 写进仓库。
- `secrets/` 已被 `.gitignore` 忽略，适合放本地真实凭证。
- 写入飞书的操作，例如成交入库、自动关闭仓位，建议先用 `--dry-run`。

### 2) 富途 OpenD / Futu API（行情与期权链）

用途：

- 当标的配置里的 `fetch.source` 为 `opend` 时，通过本机 OpenD 拉行情、期权链、合约乘数等数据。
- 港股期权通常依赖 OpenD；美股可按配置在 OpenD 和 Yahoo 之间选择或降级。

需要准备：

- 本机或服务器已启动 OpenD。
- 富途账户已登录，行情权限可用。
- 默认连接 `127.0.0.1:11111`；如需调整，在配置或脚本参数里设置 host/port。

常用检查：

```bash
./.venv/bin/python scripts/opend_watchdog.py
./.venv/bin/python scripts/doctor_opend_required_fields.py --symbols NVDA 00700.HK
```

### 3) Yahoo / yfinance（可选行情源）

用途：

- 当 `fetch.source` 为 `yahoo` 时，使用 yfinance 拉取美股行情和期权链。
- 也可作为 OpenD 不可用时的美股降级来源。

注意：

- 当前示例配置不需要 Yahoo API Key。
- Yahoo/yfinance 可能被限流；生产监控中建议保留 OpenD 健康检查和降级策略。

### 4) Finnhub 等第三方行情源（如启用）

用途：

- 如果你的本地 `portfolio-management` 或自定义行情流程启用了 Finnhub，需要单独准备 API Key。
- 当前仓库示例配置默认不强制 Finnhub；只有在你自己的配置或外部依赖里引用时才需要。

建议：

- 将 Finnhub API Key 放在外部服务自己的本地配置或环境变量中。
- 不要把 API Key 写入 `config.*.json` 示例文件或提交到 Git。

### 5) 通知发送目标

用途：

- 发送候选提醒、无候选心跳、OpenD 告警等消息。

配置位置：

- 仓外 runtime config 的 `notifications`，例如 `/opt/options-monitor/configs/config.us.json`。

常见字段：

- `channel`: 发送通道，当前常用为 `feishu`；也可以使用本机 `openclaw` 已支持的其他通道。
- `target`: 发送目标，例如 `user:open_id` 或 `chat:chat_id`。
- `quiet_hours_beijing`: 可选，北京时间免打扰窗口；不需要时不要写 `null`，直接省略。
- `cash_footer_accounts` / `cash_footer_timeout_sec` / `cash_snapshot_max_age_sec`: 可选，现金摘要账户与查询参数。

兼容字段：

- `include_cash_footer`: 仅旧 `scripts/run_pipeline.py` 会读取；多账户主流程不把它作为发送开关，主示例不再配置。
- 不再推荐配置 `enabled` / `mode`，当前主流程不读取它们作为行为开关。

安全建议：

- 本地调试时优先使用 `--no-send` 或只查看 `output/reports/symbols_notification.txt`。
- 没确认前不要把生产群聊作为测试 target。

## 5 分钟跑通

下面命令使用仓内 `config.us.json` 作为开发机简写；生产环境请替换为仓外绝对路径，例如 `/opt/options-monitor/configs/config.us.json`。

1. 准备配置：

```bash
cp configs/examples/config.example.us.json config.us.json
```

2. 跑一次完整 pipeline：

```bash
./.venv/bin/python scripts/run_pipeline.py --config config.us.json
```

3. 查看输出：

```bash
ls output/reports
cat output/reports/symbols_notification.txt
```

4. 如果只想快速验证流程，不拉持仓上下文：

```bash
./.venv/bin/python scripts/run_pipeline.py --config config.us.json --no-context
```

## 常用工作流

### 单次 watchlist 扫描

```bash
OPTIONS_MONITOR_CONFIG=config.us.json ./run_watchlist.sh
OPTIONS_MONITOR_CONFIG=config.hk.json ./run_watchlist.sh
```

### 多账户 tick

```bash
./.venv/bin/python scripts/send_if_needed_multi.py --config /opt/options-monitor/configs/config.us.json --market-config us --accounts lx sy
./.venv/bin/python scripts/send_if_needed_multi.py --config /opt/options-monitor/configs/config.hk.json --market-config hk --accounts lx sy
```

这个入口会按 scheduler 判断是否需要扫描和通知；多账户运行会复用同一 tick 的 required data 与持仓上下文缓存。

当前 scheduler 只在交易日的交易时段内触发：开盘后 30 分钟通知一次，之后每小时通知一次，收盘前 10 分钟通知一次。港股午休等 `market_break_start` / `market_break_end` 时段会跳过。

### 单账户定时入口

```bash
./.venv/bin/python scripts/send_if_needed.py --config config.us.json
```

### Watchlist 管理

```bash
./.venv/bin/python scripts/watchlist.py --config config.us.json list
./.venv/bin/python scripts/watchlist.py --config config.us.json add TSLA --put --use put_base --limit-exp 8
./.venv/bin/python scripts/watchlist.py --config config.us.json add AAPL --call --accounts lx
./.venv/bin/python scripts/watchlist.py --config config.us.json edit NVDA --set sell_put.min_strike=145 --set sell_put.max_strike=160
./.venv/bin/python scripts/watchlist.py --config config.us.json rm TSLA
```

### 成交消息入库

先解析：

```bash
./.venv/bin/python scripts/cli/parse_option_message_cli.py --text "<成交消息>"
```

默认 dry-run：

```bash
./.venv/bin/python scripts/option_intake.py --market 富途 --text "<成交消息>" --dry-run
```

确认无误后再写入：

```bash
./.venv/bin/python scripts/option_intake.py --market 富途 --text "<成交消息>" --apply
```

## CLI 常用命令

### Sell Put 扫描

```bash
./.venv/bin/python scripts/cli/scan_sell_put_cli.py \
  --symbols AAPL \
  --min-annualized-net-return 0.08 \
  --min-net-income 50 \
  --min-open-interest 100 \
  --min-volume 10 \
  --max-spread-ratio 0.30 \
  --quiet
```

### Sell Call 扫描

```bash
./.venv/bin/python scripts/cli/scan_sell_call_cli.py \
  --symbols AAPL \
  --avg-cost 150 \
  --shares 100 \
  --min-annualized-net-return 0.08 \
  --min-net-income 50 \
  --min-open-interest 100 \
  --min-volume 10 \
  --max-spread-ratio 0.30 \
  --quiet
```

### 只跑到某个阶段

```bash
./.venv/bin/python scripts/run_pipeline.py --config config.us.json --stage fetch
./.venv/bin/python scripts/run_pipeline.py --config config.us.json --stage scan
./.venv/bin/python scripts/run_pipeline.py --config config.us.json --stage alert
./.venv/bin/python scripts/run_pipeline.py --config config.us.json --stage notify
```

### 只重渲染已有候选

```bash
./.venv/bin/python scripts/cli/render_sell_put_alerts_cli.py --report-dir output/reports --top 5 --layered
./.venv/bin/python scripts/cli/render_sell_call_alerts_cli.py --report-dir output/reports --top 5 --layered
```

### 健康检查

```bash
./.venv/bin/python scripts/healthcheck.py --config config.us.json --accounts lx sy
```

## Trace 与输出定位

常看文件：

- `output/raw/`：原始行情抓取结果。
- `output/parsed/`：标准化 required data CSV。
- `output/reports/`：候选 CSV、摘要、提醒文本。
- `output/state/`：单账户状态缓存。
- `output_runs/<run_id>/`：多账户 tick 的单次运行产物。
- `output_runs/<run_id>/accounts/<account>/`：多账户下单个账户的报告和状态。

多账户运行时，可重点看：

```bash
find output_runs -maxdepth 3 -type f | sort | tail -40
```

上下文复用可观测标记会写入账户级 context JSON：

- `context_source=shared_refresh`：本 tick 首次刷新共享上下文。
- `context_source=shared_slice`：从共享上下文按账户切片复用。
- `context_source=account_cache`：命中账户本地缓存。
- `context_source=direct_fetch`：回退到账户级直接拉取。

## 通知行为

- 有候选：发送候选提醒。
- 无候选但监控正常触发：发送心跳文案 `监控正常触发：本轮无候选。`
- 调度触发点：交易日交易时段内，开盘后 30 分钟、之后每小时、收盘前 10 分钟。
- quiet hours / no-send / 缺通知目标等发送门控仍会阻止发送。

## 文档导航

- `CONFIGS.md`：配置真源、派生配置同步、配置门禁。
- `CONFIGURATION_GUIDE.md`：配置字段说明。
- `RUNBOOK.md`：运维巡检、排障和应急操作。
- `docs/candidate_strategy.md`：候选筛选、过滤、排序契约。
- `docs/required_data_schema.md`：required data 字段契约。
- `docs/GUARDRAILS.md`：仓库 guardrails。
- `tests/README.md`：测试分层和新增测试规则。

## 风险提示

本工具只做监控、筛选和提醒，不构成投资建议。期权交易风险较高，任何下单都需要自行复核标的、价格、仓位、保证金、流动性和事件风险。
