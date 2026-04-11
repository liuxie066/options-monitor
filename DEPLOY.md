# options-monitor Deploy

## Rule

- 开发只在：`/home/node/.openclaw/workspace/options-monitor`
- 生产只运行：`/home/node/.openclaw/workspace/options-monitor-prod`
- 不要在 prod 目录直接改代码
- options-monitor 运行入口配置仅 `config.us.json` / `config.hk.json`（不要将 `config.json`、`config.scheduled.json`、`config.market_us*`、`config.market_hk*` 视为主运行入口）

## Standard Commands

默认发布（不覆盖运行配置）：

```bash
cd /home/node/.openclaw/workspace/options-monitor
./.venv/bin/python scripts/deploy_to_prod.py --dry-run
./.venv/bin/python scripts/deploy_to_prod.py --apply
```

显式包含运行配置 + 白名单限制（仅允许命中的字段覆盖）：

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

> `--include-runtime-config` 若不提供 `--runtime-config-allowlist` 会被拒绝执行。

> `--include-runtime-config` + allowlist 只会更新白名单命中的既有字段；不会新增/整文件覆盖 runtime config。

如需让 prod 删除已从 dev 移除的文件（同步范围内）：

```bash
./.venv/bin/python scripts/deploy_to_prod.py --apply --prune
```

## Auto Deploy

Cron 建议每 2 分钟轮询：

```cron
*/2 * * * * /home/node/.openclaw/workspace/options-monitor/.venv/bin/python /home/node/.openclaw/workspace/options-monitor/scripts/auto_deploy_from_main.py >> /home/node/.openclaw/workspace/options-monitor/logs/auto_deploy_from_main.log 2>&1
```

自动发布开关（暂停/恢复）：

```bash
# pause
touch /home/node/.openclaw/workspace/options-monitor-prod/disable_autodeploy.flag

# resume
rm -f /home/node/.openclaw/workspace/options-monitor-prod/disable_autodeploy.flag
```
