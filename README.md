# options-monitor

期权监控与提醒（Sell Put / Covered Call），覆盖 **美股/港股**，支持 **多账户（lx/sy）分开计算约束、合并输出提醒**。

- 适用：日常候选扫描、仓位/现金约束核算、飞书提醒
- 不适用：把 Yahoo 当成严肃实时主数据源；用不完整字段做交易级决策

> 给 AI/Agent 的入口说明看 `SKILL.md`（白名单命令、关键约束）。

---

## Hard Rules（别踩）

1. **真实运行配置不要提交**
   - 仓库只保留 `config.example.us.json` / `config.example.hk.json`
   - 本机约定：实际运行配置文件名用 `config.us.json` / `config.hk.json`（本地文件，不要提交）

2. **dev → prod 有纪律**
   - 开发只在：`/home/node/.openclaw/workspace/options-monitor`（dev repo）
   - 生产运行在：`/home/node/.openclaw/workspace/options-monitor-prod`
   - 部署用：`scripts/deploy_to_prod.py`（不要直接改 prod）

3. **数据缺失必须显式提示**（字段缺失/源不可用/降级路径）

---

## Quickstart

### 1) 安装依赖（推荐一键）

```bash
cd /home/node/.openclaw/workspace/options-monitor
./run_watchlist.sh
```

### 2) 准备本地配置

```bash
cp config.example.us.json config.us.json
cp config.example.hk.json config.hk.json
```

---

## Cheatsheet（常用命令）

> 说明：这里是给人跑的；Agent/cron 的白名单命令以 `SKILL.md` 为准。

### A. 生产 tick（按 scheduler 决策是否扫描/是否通知）

```bash
# US
./.venv/bin/python scripts/send_if_needed_multi.py --config config.us.json --market-config us --accounts lx sy

# HK
./.venv/bin/python scripts/send_if_needed_multi.py --config config.hk.json --market-config hk --accounts lx sy
```

### B. Watchlist 管理

```bash
./.venv/bin/python scripts/watchlist.py list
./.venv/bin/python scripts/watchlist.py add TSLA --put --use put_base --limit-exp 8
./.venv/bin/python scripts/watchlist.py add AAPL --put --accounts lx
./.venv/bin/python scripts/watchlist.py edit NVDA --set sell_put.min_strike=145 --set sell_put.max_strike=160
./.venv/bin/python scripts/watchlist.py rm TSLA
```

### C. 成交/手工输入 → option_positions（Intake）

```bash
./.venv/bin/python scripts/parse_option_message.py --text "..."
./.venv/bin/python scripts/option_intake.py --market hk --account lx --text "..." --dry-run
```

---

## Config & State

- 配置：`config.local.us.json` / `config.local.hk.json`
- 输出目录（dev）：`output/`
  - `output/raw/`：原始抓取
  - `output/parsed/`：标准化 CSV
  - `output/reports/`：候选/摘要/提醒文本
- prod 的共享调度状态（权威）：
  - `options-monitor-prod/output_shared/state/scheduler_state_us.json`
  - `options-monitor-prod/output_shared/state/scheduler_state_hk.json`

---

## Troubleshooting（只列高频）

- **OpenD 不可用 / 登录失效**：先确认 OpenD 进程与端口，再看 `output/*/opend_metrics.json` 是否大量失败。
- **“字段缺失”**：不要硬跑，先把缺失字段打印出来并确认数据源是否支持。
- **“非交易时段：不监控”但你认为是交易时段**：优先检查 tick 用的是不是正确的 `--market-config` 与对应 `config.*.hk/us.json`。

---

## Dev → Prod 部署

```bash
cd /home/node/.openclaw/workspace/options-monitor
./.venv/bin/python scripts/deploy_to_prod.py --dry-run
./.venv/bin/python scripts/deploy_to_prod.py --apply
```

> prod 不是开发源，不要在 `options-monitor-prod` 里直接改代码。
