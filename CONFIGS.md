# options-monitor 配置收敛（US / HK）

目标：把混杂的 config 文件收敛成两份最终入口配置：
- `config.us.json`
- `config.hk.json`

## 运行入口

### 美股
```bash
cd /home/node/.openclaw/workspace/options-monitor-prod
./.venv/bin/python scripts/send_if_needed_multi.py --config config.us.json --accounts lx sy --market-config all
```

### 港股
```bash
cd /home/node/.openclaw/workspace/options-monitor-prod
./.venv/bin/python scripts/send_if_needed_multi.py --config config.hk.json --accounts lx sy --market-config all
```

## 说明

- 两份配置都使用统一键名 `schedule`，避免 `schedule_hk` 这种分叉键。
- `config.hk.json` 的 `schedule` 来自历史 `config.market_hk.example.json` 的 `schedule_hk` 段。
- `config.us.json` 的 `schedule` 来自历史 `config.legacy.example.json` 的 `schedule` 段。

## 现存历史文件（建议保留但不再作为入口）

- `config.legacy.example.json`：旧的混合入口（含 US+HK symbols + US schedule）
- `config.market_us.example.json` / `config.market_hk.example.json`：历史市场拆分版本（键名不统一）
- `config.scheduled.example.json`：历史 scheduled 模式版本
- `config.market_us.fallback_yahoo.example.json`：历史 fallback 版本

建议：后续只维护 `config.us.json` / `config.hk.json`，其它作为归档与回滚参考。
