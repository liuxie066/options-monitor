# Report Center

报告中心为所有报告类型提供一份**可枚举的注册表**、统一的**运行/历史入口**和**可导出的调度描述**。它不重写现有 tick、launchd 或报告实现。

## 定位与边界

- 统一登记：报告类型、频率、生成入口、渲染入口、状态目录、配置开关、迁移状态。
- 统一入口：`om report list / run / history / show / schedule`。
- 统一产物：迁移后的报告写入 `<runtime_root>/reports/<report_id>/<run_id>/`。
- 不改现有行为：未迁移报告仍由原实现运行；`om report run <legacy_id>` 返回 `REPORT_NOT_MIGRATED`，并给出原实现位置。
- 不接管现有调度：`local.options-monitor.code-health-review`、`com.options-monitor.code-entropy.daily`、`com.options-monitor.code-entropy.weekly` 保持原样。

## 注册表

定义在 `src/application/reports/registry.py` 的 `REPORT_REGISTRY`。每条声明：

| 字段 | 含义 |
|---|---|
| `report_id` | 稳定字符串，CLI 的唯一标识 |
| `display_name` / `description` | 中文展示名与一句话说明 |
| `schedules` | 一到多条调度：`daily` / `weekly` / `tick` / `manual`，含时区、时间、ISO weekday、launchd label、plist 路径 |
| `run_entry` | 生成入口（模块/函数或脚本） |
| `render_entries` | 渲染入口与产物格式线索 |
| `state_dir` | 现有或新的状态/历史目录约定 |
| `config_keys` | 控制该报告的配置键（逻辑点路径；没有则为空） |
| `migrated` | 是否由报告中心执行。当前只有 `config_review` 为 `true` |
| `retention` | 报告中心自己的保留策略（见下） |
| `legacy_probe` | 未迁移报告的上次运行读取方式 |

当前登记：

| `report_id` | 展示名 | 频率 | 迁移 |
|---|---|---|---|
| `daily_brief` | 每日决策简报 | tick 触发 | 否 |
| `close_advice` | 平仓建议报告 | tick 触发 | 否 |
| `code_health` | 代码健康每日巡检 | 每日 09:17（本机 launchd） | 否 |
| `code_entropy` | 代码熵评分卡 | 每日 04:20 + 每周日 01:20（本机 launchd） | 否 |
| `config_review` | 周度配置清单报告 | 每周一 08:00（Asia/Shanghai） | 是 |

## CLI

```bash
om report list [--json] [--configs-root PATH]
om report run <report_id> [--dry-run] [--json] [--configs-root PATH] [--prices-file PATH]
om report history <report_id> [--limit N] [--json]
om report show <report_id> <run_id> [--artifact NAME] [--json]
om report schedule [--format text|json]
```

- `list`：报告类型、频率、配置开关状态（从 `<configs-root>/system.json` 的 `defaults` / `markets` 读取）、上次运行与结果。未迁移报告用 `legacy_probe` 做只读探测。
- `run`：只对 `migrated: true` 的报告生效。`--dry-run` 只生成并打印，不创建运行目录，不写历史；`--no-deliver` 只在本地生成报告，跳过外部投递。
- `history`：按 UTC 时间倒序读取 `<runtime_root>/reports/<report_id>/*/run.json`。
- `show`：默认显示 markdown 产物；`--artifact report.json` 可查看结构化产物。
- `schedule`：输出每个报告的调度条目；JSON 里包含 launchd label、`StartCalendarInterval`、plist 路径是否存在、迁移状态和保留策略。

## 产物目录与运行清单

```
<runtime_root>/reports/<report_id>/<run_id>/
  run.json
  report.md
  report.json
```

`run_id` 形如 `20261112T014229Z-60951035`，以 UTC 时间开头，后接随机后缀，保证同秒并发也不会冲突。

`run.json`（`schema_version: report_run.v1`）记录：

- `report_id` / `run_id` / `status`（`running` / `succeeded` / `failed`）
- `started_at_utc` / `finished_at_utc` / `trigger`（`manual` / `schedule`）
- `command`：实际执行入口
- `artifacts`：文件名、相对路径、字节数、SHA-256
- `summary`：报告摘要（结构化）
- `delivery`：投递结果（`report_delivery.v1`；见下）
- `error`：失败时的 `code` / `message`

失败运行也会落 `run.json`，便于 `history` 复盘；生成失败不会留下半份 `report.md`。

## 调度描述与 plist 模板

`om report schedule --format json` 是调度事实的导出面，示例字段：

```json
{
  "schema_version": "report_schedule.v1",
  "timezone": "Asia/Shanghai",
  "reports": [
    {
      "report_id": "config_review",
      "migrated": true,
      "entries": [
        {
          "frequency": "weekly",
          "trigger": "launchd",
          "weekday": 1,
          "hour": 8,
          "minute": 0,
          "launchd": {
            "label": "com.options-monitor.report.config-review.weekly",
            "plist_present": false,
            "start_calendar_interval": {"Weekday": 1, "Hour": 8, "Minute": 0}
          },
          "command": ["om", "report", "run", "config_review", "--trigger", "schedule"]
        }
      ]
    }
  ]
}
```

- ISO weekday：`1=Monday .. 7=Sunday`；launchd 的 `StartCalendarInterval` 输出时把周日写成 `0`。
- `plist_present` 只检查 `~/Library/LaunchAgents/<label>.plist` 是否存在，不加载、不修改。
- `config_review` 的模板在 `deploy/launchd/com.options-monitor.report.config-review.weekly.plist.template`。本期只提供模板，不安装。

新增报告时，注册表加一条 + 补一个 plist 模板即可；不需要重新设计调度。

## 保留策略

`output_runs` 的现有默认清理策略是 **14 天 / 200 次**（`service_cleanup.py` 的 `output_runs_keep_days` / `output_runs_keep_count`）。周报套用这个策略只能留下约 2 份，无法做跨月/跨季对比，因此报告中心使用**按报告类型声明的独立策略**：

- `config_review`：`keep_count = 52`（每周一份，约一年）。
- 未迁移报告：继续由各自现有目录/清理策略管理，报告中心不接管。

本期只**声明**策略，不实现自动删除：删除属于需要单独授权的操作，且旧报告尚未迁移。后续若要执行，应在 `service_cleanup` 增加 `reports/` 类别并沿用 `run.json` 身份校验，而不是在每次 `run` 后静默删除。

## 现价来源（`config_review`）

边界来自本地用户配置（`user.hk.json` / `user.us.json`，位于 `configs/` 目录，不入库）。现价按以下顺序解析：

1. `--prices-file` 指定的 JSON（测试、人工快照、离线复算）；
2. `<runtime_root>/output_accounts/*/state/daily_decision_brief.<MARKET>.current.json` 中 `candidate_index[].representative.metrics.spot` 的最新观察；
3. 没有可用价格时标注“现价不可用”，边界仍完整展示，不阻断报告。

`--prices-file` 接受 `{"prices": {"NVDA": 173.73}}` 或 `{"NVDA": {"price": 173.73, "observed_at_utc": "..."}}`。

## 投递

报告注册表的 `delivery` 字段声明投递渠道，默认 `none`；当前只有 `config_review` 声明 `feishu_app`。

- 复用现有飞书应用通道：`src/application/channels/feishu_notification.py` 的 `send_feishu_app_message`（与 daily brief 同一个发送实现），不新增 HTTP 客户端，也不新增凭证。
- 目标与凭证来自现有 Bot 设置：`OM_FEISHU_BOT_APP_ID`、`OM_FEISHU_BOT_APP_SECRET`、`OM_FEISHU_BOT_USER_OPEN_ID`。launchd 不读 shell profile，因此 plist 通过 `OM_ENV_FILE` 指向实例 env 文件（本机为 `.env/options-monitor.env`）。
- 飞书 SDK 来自可选插件；未执行 `om plugins install feishu --confirm`（或源码环境的 pip 安装）时，投递记录为 skipped，报告照常生成。
- 幂等键由 `report_id + run_id` 派生，同一 run 重试不会重复发消息。

`run.json` 的 `delivery` 有三种终态：

| status | 含义 | CLI |
|---|---|---|
| `delivered` | 飞书返回 `message_id`，投递确认 | 退出码 0 |
| `skipped` | 未声明渠道、`--no-deliver`、插件未安装或凭证缺失；`reason` / `message` 明确写出原因 | 退出码 0 |
| `failed` | 已尝试发送但未确认；报告仍完整落盘 | 退出码 1，stderr 输出错误 |

`--dry-run` 不写运行目录，也不投递。投递失败不会回滚或删除 `report.md` / `report.json`。

## 迁移顺序与风险

本期**不迁移任何老报告**。建议顺序与风险如下：

1. `code_health`
   - 风险最低：状态目录、运行记录（`last-run.json`）和产物都在独立目录，launchd 只调用脚本。
   - 迁移风险：需要把状态目录搬到 `<runtime_root>/reports/`，或保留旧目录并让新框架只读；迁移期间必须避免两套 runner 同时写 `run.lock`。
2. `code_entropy`
   - 风险较低：`scorecards.jsonl` 已经是运行历史；但 daily/weekly 两种 mode 共享一个状态目录和 issue 去重账本。
   - 迁移风险：不能改变 `decisions.json` 的 issue 发布身份，否则会重复创建 issue；两个 plist 的 label 和预算要保持。
3. `close_advice`
   - 风险较高：产物在 `output_accounts/<account>/reports`，由 tick 在报价封存事务内生成，且与通知、恢复补报、manifest 校验强耦合。
   - 迁移风险：把产物改写到 `reports/` 会破坏现有账户报告契约和下游读取；正确做法是先让报告中心**读取**现有 manifest，再评估是否改写入路径。
4. `daily_brief`
   - 风险最高：涉及投递状态、失败恢复补报、决策历史修订和 `daily_decision_brief.v1` 身份语义。
   - 迁移风险：任何“只搬目录/只换入口”的做法都会丢失投递确认和恢复身份；应保持 repository 原样，仅登记和只读展示。

## 新增报告 checklist

1. 在 `REPORT_REGISTRY` 增加一条 `ReportDefinition`，声明频率、入口、状态目录、开关、迁移状态与保留策略。
2. 在 `src/application/reports/runner.py` 的 `RUNNERS` 注册生成函数（输入 `ReportRunContext`，输出 `ReportBuild`）。
3. 生成函数返回 markdown/JSON 产物内容，不直接写磁盘；落盘、SHA-256、`run.json` 由 `execute_report` 统一处理。
4. 添加测试：注册表覆盖、生成内容、CLI `run/history/show`、失败路径。
5. 在 `om report schedule` 输出中核对调度字段；需要定时运行就补 plist 模板，仍由人工确认后安装。
