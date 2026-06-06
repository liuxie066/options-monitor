# options-monitor 配置契约文档

仅定义配置契约与门禁，不重复 README/RUNBOOK 的操作细节。

## Canonical Configs（运行真源）

- `config.us.json`
- `config.hk.json`

规则：
- `config.us.json` / `config.hk.json` 是当前 runtime 的 canonical market configs。
- `config.yaml` 是新的人工编辑入口；运行前用 `./om config build --source yaml --market us|hk` 生成 market-specific runtime snapshot。
- 旧 layered JSON 不再是 authoring source；`configs/user.common.json` / `configs/user.<market>.json` 只作为 `./om config migrate-yaml` 的一次性迁移输入。
- 线上可以把这两份 canonical config 放在仓库外管理，例如 `/opt/options-monitor/configs/config.us.json` / `/opt/options-monitor/configs/config.hk.json`，并在运行入口显式传入绝对路径。
- 仓内同名文件仍是受支持的 repo-local runtime config 形态，适合本地开发和默认本地运行。
- 仓库跟踪 `configs/system.json` 与 `configs/examples/*.json` 模板；用户实际 runtime config / user config / common user config 不随版本发布。
- `.gitignore` 会忽略仓内 `config.us.json` / `config.hk.json`、`config.local*.json`、`config.market_*.json`、旧兼容文件名和 config 备份，避免代码更新覆盖用户本地配置。
- runtime 入口始终以传入的 market-specific canonical config 为准；生产 cron 若使用仓外配置，应显式传入对应绝对路径。

## YAML Config（推荐编辑入口）

`config.yaml` 只保存用户 override：账号、每个 market 的账户集合、symbols、少量 per-symbol override，以及少数运行行为配置。系统默认值由 `src/application/config_defaults.py` 的 `DEFAULT_CONFIG` 提供，构建时深合并。

规则：
- 缩进使用 2 个空格；tab 会被拒绝。
- `market` 必须显式传入 `us` 或 `hk`；不会隐式 fallback 到 `us`。
- `symbols` 保持字符串列表；每个 symbol 的 `dte`、`strike`、`combo_yield` 等配置放在 `markets.<market>.overrides.<symbol>`。
- Covered Call 在 `config.yaml` 里写 `covered_call`；生成的 runtime JSON、CSV 和 trace 仍使用内部 key `sell_call`。
- `combo_yield` 是 symbol 级 Combo Yield 开仓策略配置，不是全局 feature 开关；历史 `yield_enhancement` 只作为旧配置迁移输入。
- `close_advice` 是建议功能，可用 `features.close_advice: false` 关闭。
- Feishu / 交易 / 配置写入权限不放在 `config.yaml`，写入闸门属于 `options-monitor.env`，执行时仍需要命令级 `--apply` / `confirm`。

示例：

```bash
cp configs/examples/config.yaml.example config.yaml
./om config build --source yaml --market us --dry-run
./om config build --source yaml --market hk --dry-run
```

默认输出到 runtime root 下的 resolved snapshot：

```text
<runtime_root>/resolved/config.us.json
<runtime_root>/resolved/config.hk.json
```

也可以显式指定输出，继续兼容现有运行入口：

```bash
./om config build --source yaml --market us --output config.us.json
./om config validate --source yaml --market us --config-yaml config.yaml
./om config explain --source yaml --market us --key symbols.1.sell_put.min_dte
```

从旧 layered JSON 用户配置预览迁移，默认只 dry-run，不写文件：

```bash
./om config migrate-yaml --output config.yaml
./om config migrate-yaml --output config.yaml --hk-accounts lx
```

该命令会读取 `configs/user.common.json`、`configs/user.us.json`、`configs/user.hk.json`，输出拟议 YAML、来源、每个 market 的 accounts / symbols，并校验新 YAML 解析后的 runtime config 是否等价于旧 layered runtime config。
如果旧配置没有显式 market accounts，迁移工具会先按旧有效配置推导；需要收窄目标账户时，用 `--us-accounts` / `--hk-accounts` 只做 dry-run 预览。

确认预览后，显式加 `--apply` 才会写入 `config.yaml`。如果目标文件已存在，默认先写 `config.yaml.bak.<timestamp>` 备份；确认不需要备份时可加 `--no-backup`。写入后工具会从磁盘上的 `config.yaml` 重新执行 US/HK 两个市场的 YAML validate + build dry-run，不会顺手生成 `config.us.json` / `config.hk.json`。

```bash
./om config migrate-yaml --output config.yaml --hk-accounts lx --apply
```

## Legacy JSON Migration（一次性入口）

旧 layered JSON 入口已经退出正常 authoring / build / explain / service upgrade 主链路。保留的唯一入口是：

```bash
./om config migrate-yaml --output config.yaml
```

迁移工具可读取 `configs/user.common.json`、`configs/user.us.json`、`configs/user.hk.json` 生成 `config.yaml` 预览；确认后显式加 `--apply`。迁移完成后，用 `config.yaml` 作为唯一人工编辑入口，并通过 `./om config build --source yaml --market us|hk` 生成 runtime JSON。

## 版本更新保护

- 代码版本更新只跟踪代码、文档与 `configs/examples/` 模板，不覆盖用户 runtime config。
- 仓库不再保留本地 dev -> prod checkout 同步脚本；Linux / Mac 服务部署使用 `./om service render` 生成服务文件。
- CI guardrails 会拒绝提交根目录 `config.us.json` / `config.hk.json` / `config.json` / `config.market_*.json` 等 runtime config。
- 需要适配新版配置字段时，使用 `scripts/migrate_runtime_config.py` 先 dry-run，再 `--apply` 写入；脚本会先创建 `*.bak.YYYYmmdd-HHMMSS` 备份。

## Data Configs（可选迁移配置）

- `portfolio.runtime.json`

最小部署不需要配置 `portfolio.data_config`。期权持仓 SQLite 固定由 runtime root 派生到 `output_shared/state/option_positions.sqlite3`。
只有 legacy SQLite bootstrap 或自定义 Feishu env key 名时，才使用 `portfolio.runtime.json` 或显式 `portfolio.data_config`。

字段优先级、`config_path` / `config_key` / `portfolio.data_config` 的正式解释，请以 `CONFIGURATION_GUIDE.md` 为准；本文件只保留 canonical config 约定与迁移操作。

## 变更流程（编辑 canonical -> 校验）

1. 编辑仓外 canonical：`/opt/options-monitor/configs/config.us.json` / `/opt/options-monitor/configs/config.hk.json`。
2. 运行入口显式使用仓外路径：`--config /opt/options-monitor/configs/config.us.json`。
3. 校验配置：`./om config validate --config-path /opt/options-monitor/configs/config.us.json`。

## Runtime Config 迁移

仓库代码更新后，如果仓外 `config.us.json` / `config.hk.json` 仍保留旧字段，可先 dry-run：

```bash
./.venv/bin/python scripts/migrate_runtime_config.py \
  --config /opt/options-monitor/configs/config.us.json \
  --config /opt/options-monitor/configs/config.hk.json
```

确认输出后再写入；脚本会先创建 `*.bak.YYYYmmdd-HHMMSS` 备份：

```bash
./.venv/bin/python scripts/migrate_runtime_config.py \
  --config /opt/options-monitor/configs/config.us.json \
  --config /opt/options-monitor/configs/config.hk.json \
  --apply
```

不传 `--config` 时，脚本会兼容读取仓内 `config.us.json` / `config.hk.json`，适合开发机本地运行。

## 禁令

- 禁止把 `config.json` / `config.scheduled.json` / `config.market_*.json` 当作 runtime 入口。
- 禁止提交本地 runtime config 与 runtime secrets（凭证、token、私钥等）。
