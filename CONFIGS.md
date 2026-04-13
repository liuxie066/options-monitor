# options-monitor Config Source of Truth

配置文档只覆盖：canonical 配置、派生配置、变更与同步流程。

## 文档边界

- 快速上手与首日命令：`README.md`
- 发布/回滚：`DEPLOY.md`
- 运行排障：`RUNBOOK.md`

## Canonical Configs（唯一入口）

- `config.us.json`
- `config.hk.json`

规则：
- 只改这两份。
- options-monitor 运行入口只认这两份。

## Derived Configs（兼容派生）

以下文件不手工维护，由同步脚本生成：
- `config.scheduled.json`
- `config.market_us.json`
- `config.market_hk.json`
- `config.market_us.fallback_yahoo.json`
- `config.json`

同步命令：

```bash
cd /home/node/.openclaw/workspace/options-monitor
./.venv/bin/python scripts/sync_runtime_configs.py --apply
```

## Config Workflow（变更流程）

1. 编辑 canonical：`config.us.json` / `config.hk.json`。
2. 执行同步：`./.venv/bin/python scripts/sync_runtime_configs.py --apply`。
3. 可选校验：`./.venv/bin/python scripts/sync_runtime_configs.py --check`。
4. 如需发布到 prod，按 `DEPLOY.md` 执行 `deploy_to_prod.py`。

## Drift Handling（漂移处理）

- `--check` 非 0：表示 canonical 与派生文件不一致。
- 处理方式：重新执行 `--apply`，不要手工编辑派生文件。

## Scope Clarification

- `portfolio-management/config.json` 仅用于 PM 凭证与 Bitable 读取，不是 options-monitor 运行入口配置。
- 历史示例文件（如 `config.legacy.example.json` / `config.market_*.example.json` / `config.scheduled.example.json`）仅用于参考与回滚对照，不作为当前入口。

## Derived Config Gate（灰度）

- 环境变量：`OM_ALLOW_DERIVED_CONFIG`
- 当前读点：`scripts/multi_tick/main.py`（在调用 `ensure_runtime_canonical_config(..., allow_derived=...)` 前读取）
- 当前写点：无（仓库内没有对该变量的写入逻辑）
- 影响面（当前）：仅影响多账户 tick 入口对“兼容派生配置是否允许”的判定；默认未设置时保持现有行为，不强制关闭。
