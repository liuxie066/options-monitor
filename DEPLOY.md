# options-monitor Deploy

## Rule

- 开发只在：`/home/node/.openclaw/workspace/options-monitor`
- 生产只运行：`/home/node/.openclaw/workspace/options-monitor-prod`
- 不要在 prod 目录直接改代码

## Standard Commands

```bash
cd /home/node/.openclaw/workspace/options-monitor
./.venv/bin/python scripts/deploy_to_prod.py --dry-run
./.venv/bin/python scripts/deploy_to_prod.py --apply
```

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
