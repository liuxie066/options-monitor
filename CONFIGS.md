# options-monitor 配置契约文档

仅定义配置契约与门禁，不重复 README/RUNBOOK 的操作细节。

## Canonical Configs（唯一真源）

- `config.us.json`
- `config.hk.json`

规则：
- 仅允许直接编辑以上两份。
- runtime 入口配置以 canonical 为准。

## Derived Configs（派生产物，禁止手工维护）

以下文件为派生产物，只能由同步脚本生成：
- `config.scheduled.json`
- `config.market_us.json`
- `config.market_hk.json`
- `config.market_us.fallback_yahoo.json`
- `config.json`

## 变更流程（编辑 canonical -> 同步 -> 校验）

1. 编辑 canonical：`config.us.json` / `config.hk.json`。
2. 同步派生：`./.venv/bin/python scripts/sync_runtime_configs.py --apply`。
3. 校验一致性：`./.venv/bin/python scripts/sync_runtime_configs.py --check`。

## 禁令

- 禁止把派生配置当作维护入口或长期入口。
- 禁止提交本地 runtime secrets（凭证、token、私钥等）。

## Derived Config Gate

- 环境变量：`OM_ALLOW_DERIVED_CONFIG`（读点：`scripts/multi_tick/main.py`）。
- 默认禁用：未设置/空值 => `allow_derived = false`。
- `true` / `1` / `on`（及其他 legacy truthy）=> 仍禁用，并标记 `OM_ALLOW_DERIVED_CONFIG_LEGACY_DISABLED`。
- 仅 `strict` 可临时放开（`allow_derived = true`），并应按迁移提示尽快回到 canonical config。
