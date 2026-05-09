# options-monitor 配置契约文档

仅定义配置契约与门禁，不重复 README/RUNBOOK 的操作细节。

## Canonical Configs（唯一真源）

- `config.us.json`
- `config.hk.json`

规则：
- `config.us.json` / `config.hk.json` 是当前 runtime 的 canonical market configs。
- 如果使用分层配置，`configs/system.json` + `configs/user.<market>.json` 只是 authoring source；运行前用 `./om config build --market us|hk` 生成对应 canonical runtime config。
- 线上可以把这两份 canonical config 放在仓库外管理，例如 `/opt/options-monitor/configs/config.us.json` / `/opt/options-monitor/configs/config.hk.json`，并在运行入口显式传入绝对路径。
- 仓内同名文件仍是受支持的 repo-local runtime config 形态，适合本地开发、WebUI 初始化和默认本地运行。
- 仓库跟踪 `configs/system.json` 与 `configs/examples/*.json` 模板；用户实际 runtime config / user config 不随版本发布。
- `.gitignore` 会忽略仓内 `config.us.json` / `config.hk.json`、`config.local*.json`、`config.market_*.json`、旧兼容文件名和 config 备份，避免代码更新覆盖用户本地配置。
- runtime 入口始终以传入的 market-specific canonical config 为准；生产 cron 若使用仓外配置，应显式传入对应绝对路径。

## Layered Configs（推荐编辑入口）

可选的轻量分层入口：

- `configs/system.json`：系统默认值，随版本发布维护，包含 runtime、templates、schedule、trade_intake、intake aliases、symbol defaults 等通用默认。
- `configs/user.us.json` / `configs/user.hk.json`：本地用户覆盖文件，默认只维护账号和 symbols；通知目标等私有覆盖也放这里。这两类文件被 `.gitignore` 忽略，不随版本发布。
- `configs/examples/user.example.us.json` / `configs/examples/user.example.hk.json`：可复制的用户配置模板。

首次使用时先复制模板：

```bash
cp configs/examples/user.example.us.json configs/user.us.json
cp configs/examples/user.example.hk.json configs/user.hk.json
```

生成 canonical runtime config：

```bash
./om config build --market us
./om config build --market hk
```

低风险预览：

```bash
./om config build --market us --dry-run
```

生成后仍按 canonical config 校验与运行：

```bash
./om config validate --config-path config.us.json
./om run tick --config config.us.json --accounts lx
```

## 版本更新保护

- 代码更新和发布同步只更新代码、文档与 `configs/examples/` 模板，不覆盖用户 runtime config。
- `scripts/publish_to_prod.sh` 即使遇到被误跟踪的根目录 runtime config，也会显式跳过。
- CI guardrails 会拒绝提交根目录 `config.us.json` / `config.hk.json` / `config.json` / `config.market_*.json` 等 runtime config。
- 需要适配新版配置字段时，使用 `scripts/migrate_runtime_config.py` 先 dry-run，再 `--apply` 写入；脚本会先创建 `*.bak.YYYYmmdd-HHMMSS` 备份。

## Data Configs（独立数据配置）

- `secrets/portfolio.sqlite.json`
- 可选：`secrets/portfolio.external_holdings.json`

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
