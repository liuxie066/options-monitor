# options-monitor 配置契约文档

仅定义配置契约与门禁，不重复 README/RUNBOOK 的操作细节。

## Canonical Configs（唯一真源）

- `config.us.json`
- `config.hk.json`

规则：
- 线上推荐将以上两份放在仓库外管理，例如 `/opt/options-monitor/configs/config.us.json` / `/opt/options-monitor/configs/config.hk.json`。
- 仓内同名文件仅作为开发机或临时本地运行的兼容默认，已被 `.gitignore` 忽略。
- runtime 入口配置以 canonical 为准，生产 cron 应显式传入仓外配置的绝对路径。

## Derived Configs（派生产物，禁止手工维护）

以下文件为派生产物，只能由同步脚本生成：
- `config.scheduled.json`
- `config.market_us.json`
- `config.market_hk.json`
- `config.market_us.fallback_yahoo.json`
- `config.json`

## 变更流程（编辑 canonical -> 同步 -> 校验）

1. 编辑仓外 canonical：`/opt/options-monitor/configs/config.us.json` / `/opt/options-monitor/configs/config.hk.json`。
2. 运行入口显式使用仓外路径：`--config /opt/options-monitor/configs/config.us.json`。
3. 若仍维护仓内兼容配置，再按需同步派生：`./.venv/bin/python scripts/sync_runtime_configs.py --apply`。
4. 校验一致性：`./.venv/bin/python scripts/sync_runtime_configs.py --check`。

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

- 禁止把派生配置当作维护入口或长期入口。
- 禁止提交本地 runtime config 与 runtime secrets（凭证、token、私钥等）。

## Derived Config Gate

- 环境变量：`OM_ALLOW_DERIVED_CONFIG`（读点：`scripts/multi_tick/main.py`）。
- 默认禁用：未设置/空值 => `allow_derived = false`。
- `true` / `1` / `on`（及其他 legacy truthy）=> 仍禁用，并标记 `OM_ALLOW_DERIVED_CONFIG_LEGACY_DISABLED`。
- 仅 `strict` 可临时放开（`allow_derived = true`），并应按迁移提示尽快回到 canonical config。
