# options-monitor Deploy

部署文档只覆盖：发布、自动发布、回滚/停用。

## 文档边界

- 快速上手与日常使用：`README.md`
- 运维值班与排障：`RUNBOOK.md`
- 配置单一来源与同步：`CONFIGS.md`

## Rule

- 开发只在：`/home/node/.openclaw/workspace/options-monitor`
- 生产只运行：`/home/node/.openclaw/workspace/options-monitor-prod`
- 不要在 prod 目录直接改代码
- 运行入口配置仅 `config.us.json` / `config.hk.json`
- 兼容派生配置（`config.scheduled.json` / `config.market_*.json` / `config.market_us.fallback_yahoo.json` / `config.json`）由 `scripts/sync_runtime_configs.py` 从 canonical 配置同步生成

## Standard Deploy

默认发布（不覆盖运行配置）：

```bash
cd /home/node/.openclaw/workspace/options-monitor
./.venv/bin/python scripts/deploy_to_prod.py --dry-run
./.venv/bin/python scripts/deploy_to_prod.py --apply
```

显式包含运行配置 + 白名单限制：

```bash
cd /home/node/.openclaw/workspace/options-monitor
./.venv/bin/python scripts/deploy_to_prod.py \
  --dry-run \
  --include-runtime-config \
  --runtime-config-allowlist runtime-config-allowlist.example.json

./.venv/bin/python scripts/deploy_to_prod.py \
  --apply \
  --include-runtime-config \
  --runtime-config-allowlist runtime-config-allowlist.example.json
```

约束：
- `--include-runtime-config` 若不提供 `--runtime-config-allowlist` 会被拒绝执行。
- `--include-runtime-config` + allowlist 仅更新白名单命中的既有字段，不新增字段，不整文件覆盖。

如需同步删除 prod 中已从 dev 移除的文件（同步范围内）：

```bash
./.venv/bin/python scripts/deploy_to_prod.py --apply --prune
```

## Release Checklist

1. 在 dev 仓完成变更并确认工作树干净。
2. 可选：执行 `./.venv/bin/python scripts/sync_runtime_configs.py --check`，确认无配置漂移。
3. 执行 `deploy_to_prod.py --dry-run` 检查差异。
4. 执行 `deploy_to_prod.py --apply`。
5. 发布后按 `RUNBOOK.md` 的三步检查验证运行状态。

## Auto Deploy（main -> prod）

Cron 建议每 2 分钟轮询：

```cron
*/2 * * * * /home/node/.openclaw/workspace/options-monitor/.venv/bin/python /home/node/.openclaw/workspace/options-monitor/scripts/auto_deploy_from_main.py >> /home/node/.openclaw/workspace/options-monitor/logs/auto_deploy_from_main.log 2>&1
```

自动发布开关（推荐）：

```bash
# pause
touch /home/node/.openclaw/workspace/options-monitor-prod/disable_autodeploy.flag

# resume
rm -f /home/node/.openclaw/workspace/options-monitor-prod/disable_autodeploy.flag
```

## Rollback / Stop-Ship

1. 先停自动发布（`disable_autodeploy.flag`），防止坏版本反复覆盖。
2. 回滚代码采用 `main` 上的 revert 流程（推荐），再执行一次标准发布。
3. 如仅需紧急止损，直接停用对应 auto deploy cron（见 `RUNBOOK.md`）。
