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

准备本地运行配置：

```bash
cp config.example.us.json config.us.json
cp config.example.hk.json config.hk.json
./.venv/bin/python scripts/sync_runtime_configs.py --apply
```

日常只编辑：

- `config.us.json`
- `config.hk.json`

派生配置的来源、同步和禁止手工维护规则见 `CONFIGS.md`。

配置校验：

```bash
./.venv/bin/python scripts/validate_config.py --config config.us.json
./.venv/bin/python scripts/validate_config.py --config config.hk.json
./.venv/bin/python scripts/sync_runtime_configs.py --check
```

## 5 分钟跑通

1. 准备配置：

```bash
cp config.example.us.json config.us.json
./.venv/bin/python scripts/sync_runtime_configs.py --apply
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
./.venv/bin/python scripts/send_if_needed_multi.py --config config.us.json --market-config us --accounts lx sy
./.venv/bin/python scripts/send_if_needed_multi.py --config config.hk.json --market-config hk --accounts lx sy
```

这个入口会按 scheduler 判断是否需要扫描和通知；多账户运行会复用同一 tick 的 required data 与持仓上下文缓存。

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
