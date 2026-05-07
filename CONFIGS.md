# options-monitor 配置契约文档

仅定义配置契约与门禁，不重复 README/RUNBOOK 的操作细节。

## Canonical Configs（唯一真源）

- `config.us.json`
- `config.hk.json`

规则：
- `config.us.json` / `config.hk.json` 是当前 runtime 的 canonical market configs。
- 线上可以把这两份 canonical config 放在仓库外管理，例如 `/opt/options-monitor/configs/config.us.json` / `/opt/options-monitor/configs/config.hk.json`，并在运行入口显式传入绝对路径。
- 仓内同名文件仍是受支持的 repo-local runtime config 形态，适合本地开发、WebUI 初始化和默认本地运行。
- `.gitignore` 只忽略额外的本地 runtime 变体（例如 `config.local*.json`、`config.market_*.json`、旧兼容文件名），不会自动忽略 canonical 的 `config.us.json` / `config.hk.json`。
- runtime 入口始终以传入的 market-specific canonical config 为准；生产 cron 若使用仓外配置，应显式传入对应绝对路径。

## Data Configs（独立数据配置）

- `secrets/portfolio.sqlite.json`
- 可选：`secrets/portfolio.feishu.json`

运行时配置里的 `portfolio.data_config` 指向这类数据配置文件。

字段优先级、`config_path` / `config_key` / `portfolio.data_config` 的正式解释，请以 `CONFIGURATION_GUIDE.md` 为准；本文件只保留 canonical config 约定与迁移操作。

## 变更流程（编辑 canonical -> 校验）

1. 编辑仓外 canonical：`/opt/options-monitor/configs/config.us.json` / `/opt/options-monitor/configs/config.hk.json`。
2. 运行入口显式使用仓外路径：`--config /opt/options-monitor/configs/config.us.json`。
3. 校验配置：`./.venv/bin/python scripts/validate_config.py --config /opt/options-monitor/configs/config.us.json`。

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
