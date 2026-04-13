# options-monitor

期权监控与提醒（Sell Put / Covered Call），覆盖 **美股/港股**，支持 **多账户（lx/sy）分开计算约束、合并输出提醒**。

## 文档导航

- 快速上手（本文）：首日安装、配置、常用运行命令
- 运维值班 / 排障：`RUNBOOK.md`
- 配置单一来源与变更流程：`CONFIGS.md`
- Agent 约束：`SKILL.md`
- Guardrails：`docs/GUARDRAILS.md`

说明：发布流程仅在本地私有运维仓执行，不在本远端仓公开。

## Quick Start（首日）

### 1) 初始化环境

```bash
cd /home/node/.openclaw/workspace/options-monitor
./run_watchlist.sh
```

### 2) 准备本地运行配置

```bash
cp config.example.us.json config.us.json
cp config.example.hk.json config.hk.json
./.venv/bin/python scripts/sync_runtime_configs.py --apply
```

说明：`config.us.json` / `config.hk.json` 是运行入口；其它 `config.market_*` / `config.scheduled.json` / `config.json` 属于兼容派生文件，来源见 `CONFIGS.md`。

## First-Day Usage（高频命令）

说明：dev 主线统一运行入口为 `scripts/send_if_needed_multi.py`（内部委托 `scripts.multi_tick.main.main`）；线上定时调度入口保持 `scripts/send_if_needed.py` 不变。

### 生产 tick（按 scheduler 决策是否扫描/是否通知）

```bash
# US
./.venv/bin/python scripts/send_if_needed_multi.py --config config.us.json --market-config us --accounts lx sy

# HK
./.venv/bin/python scripts/send_if_needed_multi.py --config config.hk.json --market-config hk --accounts lx sy
```

### Watchlist 管理

```bash
./.venv/bin/python scripts/watchlist.py list
./.venv/bin/python scripts/watchlist.py add TSLA --put --use put_base --limit-exp 8
./.venv/bin/python scripts/watchlist.py add AAPL --put --accounts lx
./.venv/bin/python scripts/watchlist.py edit NVDA --set sell_put.min_strike=145 --set sell_put.max_strike=160
./.venv/bin/python scripts/watchlist.py rm TSLA
```

### 单标的调试（CLI）

```bash
# Sell Put
./.venv/bin/python scripts/cli/scan_sell_put_cli.py --symbols AAPL --min-annualized-net-return 0.08 --quiet

# Sell Call
./.venv/bin/python scripts/cli/scan_sell_call_cli.py --symbols AAPL --avg-cost 150 --shares 100 --min-annualized-net-return 0.08 --quiet
```

### 成交/手工输入 -> option_positions（Intake）

```bash
./.venv/bin/python scripts/cli/parse_option_message_cli.py --text "..."
./.venv/bin/python scripts/option_intake.py --market hk --account lx --text "..." --dry-run
```

## 输出与状态（定位文件）

- dev 输出目录：`output/`
- `output/raw/`：原始抓取
- `output/parsed/`：标准化 CSV
- `output/reports/`：候选/摘要/提醒文本
- prod scheduler 权威状态：
  - `options-monitor-prod/output_shared/state/scheduler_state_us.json`
  - `options-monitor-prod/output_shared/state/scheduler_state_hk.json`

## 高频问题（入口指引）

- OpenD 不可用 / 登录失效：先看 `RUNBOOK.md` 的排障章节。
- 配置漂移或通知目标不一致：按 `CONFIGS.md` 的同步流程执行 `sync_runtime_configs.py`。
- 发布或回滚需求：发布流程仅在本地私有运维仓执行。
