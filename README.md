# options-monitor

期权监控与提醒工具，面向 Sell Put / Covered Call 工作流。支持美股/港股、多账户、定时扫描、候选筛选排序、平仓建议、成交自动入账，以及无候选时的监控心跳通知。

这份 README 只解决 6 个问题：

1. 这个项目现在是怎么工作的
2. 首次部署要准备什么
3. 配置文件应该放哪里
4. 数据源和存储边界是什么
5. 日常最常用的命令有哪些
6. 出问题时先去哪里看

更细的配置契约、字段定义、运维流程和测试约定，统一放在文末导航里。

## 你能用它做什么

- 扫描 `symbols` 中配置的标的期权链
- 按 DTE、行权价、收益门槛、流动性和事件标注筛选候选
- 为 Sell Put 检查现金担保能力
- 为 Covered Call 检查可覆盖股数
- 基于 open short put / short call 生成平仓建议
- 维护期权成交事件与持仓 lot 视图，支持人工录入和自动成交入账
- 输出候选 CSV、摘要、提醒文本和运行状态

## 当前系统边界

```mermaid
flowchart TD
    User["用户 / Cron / WebUI"] --> Entrypoints["运行入口"]
    Entrypoints --> Multi["多账户入口<br/>scripts/send_if_needed_multi.py<br/>scripts/multi_tick/main.py"]
    Entrypoints --> Single["单账户入口<br/>scripts/send_if_needed.py"]
    Entrypoints --> Pipeline["手动 Pipeline<br/>./om scan-pipeline"]
    Entrypoints --> Intake["成交入账<br/>scripts/auto_trade_intake.py<br/>scripts/option_intake.py"]

    Runtime["仓内 Runtime Config<br/>config.us.json<br/>config.hk.json"] --> Entrypoints
    Examples["仓内模板<br/>configs/examples/*.json"] -.复制初始化.-> Runtime

    Pipeline --> Fetch["行情与期权链获取"]
    Fetch --> Futu["futu / OpenD"]

    Pipeline --> Ctx["账户与持仓上下文"]
    Ctx --> FutuPortfolio["Futu / OpenD 持仓与现金"]
    Ctx -.optional fallback.-> Holdings["Feishu holdings"]
    Ctx --> SQLite["SQLite trade_events / position_lots"]

    Intake --> PositionCore["position lots 服务<br/>scripts/option_positions_core/*"]
    PositionCore --> SQLite
    PositionCore -.bootstrap only.-> FeishuBackup["Feishu legacy bootstrap 来源"]

    Pipeline --> Domain["确定性业务逻辑<br/>domain/domain/*"]
    Domain --> Alerts["候选 / 平仓建议 / 报告"]
    Alerts --> Notify["通知渲染与发送"]
    Notify --> Channel["Feishu 等通知渠道"]

    Pipeline --> Output["output/ output_shared/ output_runs/"]
```

最重要的几条边界：

- `domain/` 放确定性业务逻辑，尽量不直接做外部 IO。
- `scripts/` 放运行入口、适配器、报表、外部服务调用和运维脚本。
- 默认持仓/现金上下文来自 Futu / OpenD。
- Feishu holdings 现在是可选 fallback，不是最小配置前置。
- 期权持仓主模型现在是 `SQLite trade_events + position_lots`。
- 安装版默认使用仓内 runtime config。

## 5 分钟跑通

### 1) 安装依赖

```bash
git clone <repo-url> options-monitor
cd options-monitor
./run_watchlist.sh
```

如果你想手动装环境：

```bash
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt
```

如果你是要把它作为本地 Agent 工具来用，直接跑：

```bash
bash scripts/install_agent_plugin.sh
./run_webui.sh
./om-agent spec
```

如果你想直接打开本地 WebUI：

```bash
./run_webui.sh
```

默认地址是 `http://127.0.0.1:8000`，默认读取仓库根目录下的 `config.us.json` / `config.hk.json`。

首次初始化建议在 WebUI 中完成。WebUI 会在 OpenD 就绪后引导你生成：

- `config.us.json` 或 `config.hk.json`
- `secrets/portfolio.sqlite.json`

并把富途 `acc_id -> account label` 映射直接写进运行配置，避免新安装用户继续从模板里的 `REAL_12345678` 起步。

如果后面要追加账号：

```bash
./om-agent add-account --market us --account-label user2 --account-type futu --futu-acc-id <REAL_ACC_ID>
./om-agent add-account --market us --account-label lx --account-type futu --futu-acc-id <REAL_ACC_ID> --holdings-account "lx"
./om-agent add-account --market us --account-label ext1 --account-type external_holdings --holdings-account "Feishu EXT"
./om-agent edit-account --market us --account-label lx --futu-acc-id <NEW_REAL_ACC_ID> --holdings-account "lx"
./om-agent remove-account --market us --account-label ext1
```

其中 `external_holdings` 账号不写富途映射，持仓过滤会走 Feishu `holdings.account`。
如果要启用这类账号，建议把 `configs/examples/portfolio.external_holdings.example.json` 复制到本地 `secrets/` 下再填写 Feishu 凭证。
如果账号本身是富途主路径，但你还想在富途失败时回退到同一张 Feishu holdings 表，就保留 `account-type futu`，只额外加 `--holdings-account`。

### 2) 准备 runtime config

推荐直接在 WebUI 里完成首次初始化。

如果你要手工重建，也可以直接复制模板到仓内：

```bash
cp configs/examples/config.example.us.json config.us.json
cp configs/examples/config.example.hk.json config.hk.json
cp configs/examples/portfolio.sqlite.example.json secrets/portfolio.sqlite.json
```

### 3) 准备最小持仓事件 / lot 配置

最小配置默认是：

- 行情与期权链：OpenD
- 持仓与现金：OpenD
- `trade_events / position_lots`：SQLite

先准备 SQLite 持仓事件 / lot 配置：

```bash
mkdir -p secrets
cp configs/examples/portfolio.sqlite.example.json secrets/portfolio.sqlite.json
```

然后在运行配置里保持：

```json
{
  "portfolio": {
    "broker": "富途",
    "data_config": "secrets/portfolio.sqlite.json",
    "source": "futu"
  }
}
```

`portfolio.broker` 是唯一推荐配置名；历史 `portfolio.market` 只在加载边界兼容，内部不会继续保留。

`portfolio.source` 支持 `auto` / `futu` / `holdings`，但最小配置建议固定成 `futu`。
如果你后面要加 Feishu holdings fallback，再单独打开 `auto` 或 `holdings`。

### 4) 校验配置

```bash
./.venv/bin/python scripts/validate_config.py --config config.us.json
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

### 5) 跑一次完整 pipeline

```bash
./om scan-pipeline --config config.us.json
```

只想快速验证扫描链路，不拉上下文：

```bash
./om scan-pipeline --config config.us.json --no-context
```

### 6) 看输出

```bash
ls output/reports
cat output/reports/symbols_notification.txt
```

## 配置应该放哪里

日常只维护仓内 runtime config：

- `config.us.json`
- `config.hk.json`

最终保留的配置文件：

- 运行时必需：`config.us.json` / `config.hk.json`
- 数据配置必需：`secrets/portfolio.sqlite.json`
- 可选扩展：`secrets/portfolio.feishu.json`

仓库模板文件：

- `configs/examples/config.example.us.json`
- `configs/examples/config.example.hk.json`
- `configs/examples/portfolio.sqlite.example.json`
- `configs/examples/portfolio.external_holdings.example.json`
- `configs/examples/portfolio.feishu.example.json`

公开安装版默认只使用仓内：

1. `secrets/portfolio.sqlite.json`
2. `secrets/portfolio.feishu.json`

如果你还有旧的外部配置路径或 sibling repo 依赖，建议先迁回仓内，再走公开安装流程。

多账户列表统一写在运行配置顶层 `accounts`，例如：

```json
{
  "accounts": ["user1"]
}
```

更多配置规则见 [CONFIGS.md](CONFIGS.md) 和 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。

## 本地 Agent 工具入口

公开本地工具入口是：

```bash
./om-agent spec
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

环境变量约定：

- `OM_OUTPUT_DIR`
- `OM_AGENT_ENABLE_WRITE_TOOLS=true`

详细说明见：

- [Getting Started](docs/GETTING_STARTED.md)
- [Agent Integration](docs/AGENT_INTEGRATION.md)
- [Tool Reference](docs/TOOL_REFERENCE.md)
- [Release Process](docs/RELEASE_PROCESS.md)
- [Current PR Material](docs/PR_PRODUCTIZATION_PHASE1_2.md)

## 数据源和存储边界

### 行情与期权链

- 唯一在线来源：OpenD / Futu API
- 行情、期权链、合约乘数统一走同一条 OpenD 主路径

常用 futu / OpenD 检查：

```bash
./.venv/bin/python scripts/doctor_futu.py --symbols NVDA 0700.HK
./.venv/bin/python scripts/opend_watchdog.py
./.venv/bin/python scripts/doctor_opend_required_fields.py --symbols NVDA 00700.HK
```

### holdings

- 最小配置默认不依赖 Feishu holdings
- 持仓、现金、covered call 可覆盖股数默认来自 Futu / OpenD
- 当 `portfolio.source=holdings` 时，账户上下文会强制走 Feishu holdings
- 当 `portfolio.source=auto` 时，会优先尝试 futu 账户上下文，失败后回退到 holdings

### 持仓事件与 lot 视图

当前主模型是：

- `trade_events`：事实层
- `position_lots`：持仓 lot 视图

当前行为：

- 默认 SQLite 路径：`output_shared/state/option_positions.sqlite3`
- 可通过 `data_config.option_positions.sqlite_path` 覆盖
- steady-state 读取默认走 `position_lots`
- 开仓、平仓、到期自动平仓都会追加事件，再重建 `position_lots`
- 如果 SQLite 为空且 Feishu `option_positions` 已配置，首次启动会从 Feishu 做一次 bootstrap
- 如果本地还留有旧 `option_positions` 表、但 `position_lots` 为空，启动时会做一次本地迁移投影

### 通知发送

常见通知目标配置在 runtime config 的 `notifications` 中。
最小配置默认不启用 Feishu 通知；需要时再单独加 `notifications` 和 Feishu 凭证。

安全建议：

- 本地调试优先用 `--no-send`
- 没确认前不要把生产群聊作为测试 target

## 日常最常用的命令

### 多账户 tick

这是当前主入口。

```bash
./.venv/bin/python scripts/send_if_needed_multi.py \
  --config config.us.json \
  --market-config us \
  --accounts user1
```

港股：

```bash
./.venv/bin/python scripts/send_if_needed_multi.py \
  --config config.hk.json \
  --market-config hk \
  --accounts user1
```

这个入口会做：

- scheduler 判断
- required data 获取
- portfolio / position-lot context 构建
- Sell Put / Covered Call 扫描
- 平仓建议附加
- 消息渲染与发送

### 单账户入口

```bash
./.venv/bin/python scripts/send_if_needed.py --config config.us.json
```

### 单次 pipeline

```bash
./om scan-pipeline --config config.us.json
```

只跑到某个阶段：

```bash
./om scan-pipeline --config config.us.json --stage fetch
./om scan-pipeline --config config.us.json --stage scan
./om scan-pipeline --config config.us.json --stage alert
./om scan-pipeline --config config.us.json --stage notify
```

### Watchlist 管理

```bash
./.venv/bin/python scripts/watchlist.py --config config.us.json list
./.venv/bin/python scripts/watchlist.py --config config.us.json add TSLA --put --use put_base --limit-exp 8
./.venv/bin/python scripts/watchlist.py --config config.us.json add AAPL --call --accounts lx
./.venv/bin/python scripts/watchlist.py --config config.us.json edit NVDA --set sell_put.min_strike=145 --set sell_put.max_strike=160
./.venv/bin/python scripts/watchlist.py --config config.us.json rm TSLA
```

### position lots 维护

查看：

```bash
./.venv/bin/python scripts/option_positions.py list --market 富途 --account lx --status open
```

新增：

```bash
./.venv/bin/python scripts/option_positions.py add \
  --account lx \
  --symbol 0700.HK \
  --option-type put \
  --side short \
  --contracts 1 \
  --currency HKD \
  --strike 420 \
  --multiplier 100 \
  --exp 2026-04-29 \
  --dry-run
```

平仓：

```bash
./.venv/bin/python scripts/option_positions.py buy-close \
  --record-id <record_id> \
  --contracts 1 \
  --dry-run
```

### 成交入账

解析成交消息：

```bash
./.venv/bin/python scripts/parse_option_message.py --text "<成交消息>"
```

聊天文本入账：

```bash
./.venv/bin/python scripts/option_intake.py --market 富途 --text "<成交消息>" --dry-run
./.venv/bin/python scripts/option_intake.py --market 富途 --text "<成交消息>" --apply
```

自动成交入账本地回放：

```bash
python3 scripts/auto_trade_intake.py \
  --config config.us.json \
  --mode dry-run \
  --deal-json configs/examples/auto_trade_intake.open.example.json
```

平仓回放：

```bash
python3 scripts/auto_trade_intake.py \
  --config config.us.json \
  --mode dry-run \
  --deal-json configs/examples/auto_trade_intake.close.example.json
```

### 平仓建议

单独生成报告，不发消息：

```bash
./.venv/bin/python scripts/close_advice/main.py \
  --config config.us.json \
  --context output/state/option_positions_context.json \
  --required-data-root output \
  --output-dir output/reports
```

公开配置入口在 runtime config 顶层 `close_advice`，常用字段：

```json
{
  "close_advice": {
    "enabled": true,
    "quote_source": "auto",
    "notify_levels": ["strong", "medium"],
    "max_items_per_account": 5,
    "max_spread_ratio": 0.4,
    "strong_remaining_annualized_max": 0.08,
    "medium_remaining_annualized_max": 0.12
  }
}
```

默认输出：
- `output/reports/close_advice.csv`
- `output/reports/close_advice.txt`

## 业务口径摘要

### Sell Put

- 行权价必须低于当前股价，并落在配置允许区间内
- `min_dte <= dte <= max_dte`
- 现金担保金额不能超过账户可用额度
- 同时检查年化净收益率、单笔净收入、流动性和价差

### Covered Call

- 必须有足够股票覆盖 short call
- 可用股数会扣除已被其他 short call 锁定的部分
- 同时检查行权价、DTE、收益率、单笔净收入和流动性

### 平仓建议

- 只评估 `position_lots` 中仍 open 的 short put / short call
- 不处理 long option
- 不做自动下单
- 不会自动下单到券商
- 开仓权利金必须来自 `premium` 字段，或 `note` 中的 `premium_per_share`

更细的筛选、排序、拒绝原因和字段契约见 [docs/candidate_strategy.md](docs/candidate_strategy.md)。

## 输出和排障先看哪里

### 最常看的输出目录

- `output/raw/`：原始行情抓取结果
- `output/parsed/`：标准化 required data CSV
- `output/reports/`：候选 CSV、摘要、提醒文本
- `output/state/`：单账户状态缓存
- `output_shared/`：共享上下文和跨账户复用缓存
- `output_runs/<run_id>/`：多账户单次运行产物

### 多账户运行重点看

- `output_runs/<run_id>/accounts/<account>/`
- `output_runs/<run_id>/accounts/<account>/close_advice.csv`
- `output_runs/<run_id>/accounts/<account>/close_advice.txt`

快速定位：

```bash
find output_runs -maxdepth 3 -type f | sort | tail -40
```

### 持仓上下文的来源标记

这些标记会写入账户级 context JSON：

- `context_source=shared_refresh`：本 tick 首次刷新共享上下文
- `context_source=shared_slice`：从共享上下文按账户切片复用
- `context_source=account_cache`：命中账户本地缓存
- `context_source=direct_fetch`：回退到账户级直接拉取

### 健康检查

```bash
./.venv/bin/python scripts/healthcheck.py --config config.us.json --accounts lx sy
```

### 常见问题先查

- OpenD / futu 连不上：先跑 `scripts/opend_watchdog.py`
- 没候选：先看 `output/reports/` 和 required data
- 现金口径不对：先看 holdings 和 `option_positions_context.json`
- 平仓建议为空：先确认 `position_lots` 是否有 open short 仓位，并且 `premium` 或 `note.premium_per_share` 已填写
- 自动平仓/自动入账不生效：先用对应 dry-run 样例回放
- Feishu bootstrap 失效：先检查 `secrets/portfolio.sqlite.json` 和 legacy Feishu 表配置

## 通知行为

- 有候选：发送候选提醒
- 有 strong / medium 平仓建议：追加到账户提醒
- 无候选但监控正常触发：发送心跳文案
- `quiet_hours`、`--no-send`、缺通知目标等门控仍会阻止发送

## 文档导航

- [CONFIGS.md](CONFIGS.md)：配置真源、派生配置同步、配置门禁
- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)：配置字段说明
- [RUNBOOK.md](RUNBOOK.md)：运维巡检、排障和应急操作
- [docs/candidate_strategy.md](docs/candidate_strategy.md)：候选筛选与排序契约
- [tests/README.md](tests/README.md)：测试分层和新增测试规则

## 风险提示

本工具只做监控、筛选和提醒，不构成投资建议。期权交易风险较高，任何下单都需要自行复核标的、价格、仓位、保证金、流动性和事件风险。
